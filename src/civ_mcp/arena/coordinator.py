from __future__ import annotations
import asyncio
import inspect
import sys
from datetime import datetime, timezone
from civ_mcp import lua as lq
from civ_mcp.arena import autoresolve, hook
from civ_mcp.arena.agent import load_playbook
from civ_mcp.arena.attention import (
    AttentionState,
    Decision,
    build_attention_query,
    cancel_remainder,
    evaluate,
    has_directive_lines,
    load_attention_state,
    note_sleep,
    note_wake,
    parse_attention_scan,
    parse_directive,
    render_digest,
    save_attention_state,
    scan_scalars,
)
from civ_mcp.arena.budget import explicit_n_ctx
from civ_mcp.arena.capabilities import build_caps_query, parse_caps
from civ_mcp.arena.config import CivOptions
from civ_mcp.arena.memory import (
    extract_standing_plan,
    format_memory_block,
    load_memory,
    save_memory,
)
from civ_mcp.arena.prompt_context import maybe_build_briefing
from civ_mcp.arena.task_tracker import (
    format_task_block,
    load_task_state,
    merge_tasks,
    parse_task_lines,
    run_pre_model_tasks,
    save_task_state,
)


async def _reconnect_with_retry(conn, attempts=5, delay=0.5):
    last = None
    for i in range(attempts):
        try:
            # Close any half-open writer from a prior failed attempt before reconnecting,
            # so repeated tries do not leak a socket/fd (connect() reassigns the writer).
            await conn.disconnect()
            await conn.connect(); return True
        except Exception as e:
            last = e
            if i < attempts - 1:          # no point sleeping after the final failed attempt
                await asyncio.sleep(delay)
    print(f"[arena] WARNING: reclaim connect failed after {attempts} attempts: {last!r}", file=sys.stderr)
    return False


async def _overview_snapshot(gs):
    """Bootstrap-free lightweight overview snapshot; returns dict or None on failure."""
    try:
        lines = await gs.conn.execute_write(lq.build_overview_query())
        ov = lq.parse_overview_response(lines)
        return {
            "score":    ov.score,
            "gold":     ov.gold,
            "science":  ov.science_yield,
            "culture":  ov.culture_yield,
            "faith":    ov.faith,
            "research": ov.current_research,
            "civic":    ov.current_civic,
            "cities":   ov.num_cities,
            "units":    ov.num_units,
        }
    except Exception:
        return None


# Reactive-only recovery for an orphaned first-meet greeting (the puppet local-player
# switch can leave one on screen — a session, or a view with no locatable session).
# NOT run automatically from the poll loop: it cannot tell an orphaned greeting from a
# leader scene the human is actively using, and force-hiding the latter blacks out the
# map. Invoked manually when a stuck greeting is actually reported.
async def _clear_blocking_diplomacy(conn) -> str:
    """Best-effort: if a diplomacy modal is blocking the idle human, clear it
    (close any real session, hide orphaned views, restore the in-game UI). Only
    acts when a view is actually visible; never raises into the poll loop."""
    try:
        lines = await conn.execute_write(lq.build_clear_blocking_diplomacy())
    except Exception:
        return "err"
    for line in lines:
        if line.startswith("CLEAR|"):
            return line
    return "?"


# Consecutive idle polls (~1s each) before the orphan-session sweep fires. An
# orphaned puppet greeting wedges the AI phase indefinitely, so a human seat
# idle this long with no capture is the wedge signature; a normal human turn
# is unaffected either way because the sweep skips the local player's own
# sessions entirely. Observed live (2026-07-07): session 1<->3 (two puppets
# first-meeting) froze turn 27 for minutes until closed by hand.
ORPHAN_SWEEP_IDLE_POLLS = 45


async def _sweep_orphan_sessions(conn) -> str:
    """Best-effort: close open diplomacy sessions not involving the local
    player (orphaned puppet greetings that wedge turn processing). Sessions
    involving the human are never touched; never raises into the poll loop."""
    try:
        lines = await conn.execute_write(lq.build_close_orphan_sessions())
    except Exception:
        return "err"
    for line in lines:
        if line.startswith("ORPHANS|"):
            return line
    return "?"


class ScriptedPolicy:
    """Deterministic no-LLM policy for the dry-run gate: observe, then skip unit 0."""
    async def __call__(self, gs, player_id: int, turn: int, **kwargs) -> dict:
        await gs.get_game_overview()
        await gs.get_units()
        try:
            await gs.skip_unit(0)
        except Exception as e:
            return {"summary": f"scripted: skip failed {e!r}", "actions": []}
        return {"summary": "scripted: observed + skipped unit 0", "actions": [{"tool": "skip_unit"}]}


def _policy_accepts_kwarg(policy, name: str) -> bool:
    try:
        # Introspect the callable itself, not policy.__call__: for a plain
        # function policy, `.__call__` is a method-wrapper whose signature is
        # (*args, **kwargs), which would spuriously report every kwarg as
        # accepted and then raise TypeError at the call site. inspect.signature
        # on the object unwraps bound methods / functions / partials correctly.
        signature = inspect.signature(policy)
    except (TypeError, ValueError):
        return False
    return any(
        param.kind == inspect.Parameter.VAR_KEYWORD or param.name == name
        for param in signature.parameters.values()
    )


async def run_arena(conn, gs, config, policy=None, policy_for=None, transcript=None) -> dict:
    if policy_for is None:
        if policy is None:
            raise ValueError("run_arena needs policy or policy_for")
        policy_for = lambda _pid: policy
    puppet_ids = set(config.puppet_ids or [p.player_id for p in config.players])
    run_id = getattr(config, "run_id", "")
    if not run_id:
        # Memory/task state is keyed by run_id; an empty one collapses the
        # per-run directory onto transcript_dir itself, silently sharing
        # standing plans and tasks across unrelated runs.
        from civ_mcp.run_id import generate_run_id

        # A fresh id per call is intentional: an empty run_id means "isolated
        # run", so two calls must NOT share a memory/task directory. This id is
        # used for the state paths AND the transcript record below (via the
        # local run_id), so records stay joinable to the state dir without
        # mutating config.
        run_id = generate_run_id()
    played, slept, game_turns, log = 0, 0, 0, []
    _tx_on = transcript is not None and getattr(transcript, "enabled", True)
    try:
        await hook.inject(conn, sorted(puppet_ids))
        remaining = config.max_puppet_turns
        deadline_polls = config.idle_poll_limit  # consecutive-idle poll budget; refilled on every captured turn
        idle_streak = 0  # consecutive idle polls since the last puppet capture
        max_game_turns = getattr(config, "max_game_turns", 0)  # tolerate old test-stub configs
        while (
            remaining > 0 and deadline_polls > 0
            and (max_game_turns <= 0 or game_turns < max_game_turns)
        ):
            st = await hook.poll(conn)
            if st.active and st.local in puppet_ids:
                idle_streak = 0
                # A captured puppet turn is ACTIVITY: refill the idle budget.
                # deadline_polls means "consecutive polls with nothing to do",
                # not a whole-run cap that slept turns burn through without
                # consuming max_puppet_turns (review-2 f8).
                deadline_polls = config.idle_poll_limit
                pol = policy_for(st.local)
                exclusive = bool(getattr(pol, "needs_exclusive_tuner", False))
                opts = getattr(pol, "options", CivOptions())
                transcript_dir = config.transcript_dir
                attention_mode = opts.attention.mode
                attention_on = attention_mode in ("auto", "model", "hybrid")
                state_before = (
                    await _overview_snapshot(gs) if (_tx_on or attention_on) else None
                )

                # --- Load standing memory / task tracker state and run deterministic
                # pre-model task follow-through. This MUST happen before the exclusive
                # disconnect below: it uses `gs`, which is backed by the live `conn` —
                # a CLI turn's exclusive disconnect leaves no connection for these reads.
                memory_error = ""
                task_tracker_error = ""
                try:
                    memory = load_memory(transcript_dir, run_id, st.local) if opts.memory.enabled else None
                    memory_block = format_memory_block(
                        memory,
                        current_turn=st.turn,
                        max_age_turns=opts.memory.max_age_turns,
                    )
                except Exception as e:
                    memory = None
                    memory_block = ""
                    memory_error = repr(e)
                    print(f"[arena] standing memory load failed: {e!r}", file=sys.stderr)

                active_tasks_before: tuple = ()
                updated_tasks: tuple = ()
                task_results: list = []
                active_tasks_after: tuple = ()
                task_block = ""
                # Latest task set that is safe to merge captured TASK lines onto.
                # None only when the load itself failed (no trustworthy base) --
                # a later pre-model failure (save, formatting) must not cost us
                # the TASK/CANCEL lines the model emits this turn.
                task_capture_base: tuple | None = None
                if opts.task_tracker.enabled:
                    try:
                        task_state = load_task_state(transcript_dir, run_id, st.local)
                        # Loaded state carries failed tombstones alongside active
                        # tasks; both must reach run_pre_model_tasks (which skips
                        # non-active) and the capture merge, or the restatement
                        # guard loses its memory of exhausted tasks.
                        loaded_tasks = task_state.tasks
                        active_tasks_before = tuple(
                            t for t in loaded_tasks if t.status == "active"
                        )
                        task_capture_base = loaded_tasks
                        updated_tasks, task_results = await run_pre_model_tasks(
                            gs, loaded_tasks, turn=st.turn
                        )
                        task_capture_base = updated_tasks
                        pre_model_state = save_task_state(
                            transcript_dir, run_id, st.local, updated_tasks
                        )
                        active_tasks_after = tuple(
                            t for t in pre_model_state.tasks if t.status == "active"
                        )
                        task_block = format_task_block(
                            updated_tasks,
                            task_results,
                            max_tasks=opts.task_tracker.max_tasks,
                        )
                    except Exception as e:
                        updated_tasks = ()
                        task_results = []
                        active_tasks_after = ()
                        task_block = ""
                        task_tracker_error = repr(e)
                        print(f"[arena] task tracker pre-model failed: {e!r}", file=sys.stderr)

                # --- Attention skip-evaluation (spec §2-4): once per captured puppet
                # turn, decide whether this civ can sleep through it. Every failure
                # here degrades toward MORE model turns (fail-open), never a blind
                # skip -- see attention.py module docstring.
                att_state = None
                att_scan = None
                digest_block = ""
                decision = None
                if attention_on:
                    try:
                        att_state = load_attention_state(transcript_dir, run_id, st.local)
                        scan_lines = await conn.execute_read(
                            build_attention_query(st.local, opts.attention.threat_radius)
                        )
                        att_scan = parse_attention_scan(scan_lines)
                    except Exception as e:
                        att_scan = None
                        print(f"[arena] attention scan failed; waking: {e!r}", file=sys.stderr)
                    if att_state is None:
                        # first load raised: fresh state (== load's own failure
                        # result) without touching disk again -- fail-open, the
                        # SCAN_ERROR wake path takes it from here (the scan
                        # never ran in this branch) (review catch)
                        att_state = AttentionState(run_id=run_id, player_id=st.local)
                    task_event = any(
                        r.get("status") not in (None, "active") for r in task_results
                    )
                    try:
                        decision = evaluate(
                            attention_mode, att_state, att_scan, state_before,
                            max_streak=opts.attention.max_streak, task_event=task_event,
                        )
                    except Exception as e:
                        # Corrupt persisted values (dict-shaped but wrong-typed,
                        # e.g. last_snapshot={"units":"5"} or directive
                        # wake_if=5) pass load's shape check and explode inside
                        # evaluate's comparisons. Contract (attention.py module
                        # docstring): state corrupt -> reset + wake, never
                        # abort (review-2 finding 1). note_wake on the fresh
                        # state rewrites the baselines and its save self-heals
                        # the file.
                        att_state = AttentionState(run_id=run_id, player_id=st.local)
                        decision = Decision("wake", "STATE_CORRUPT")
                        print(f"[arena] attention evaluate failed; reset + wake: {e!r}",
                              file=sys.stderr)
                if decision is not None and decision.action == "sleep":
                    prev_snapshot = att_state.last_snapshot
                    task_notes = [
                        f"{r.get('kind', '?')} {r.get('action', '')}: {r.get('result', '')}"
                        for r in task_results
                    ]
                    att_state = note_sleep(
                        att_state, turn=st.turn, snapshot=state_before,
                        scan_scalars=scan_scalars(att_scan),
                        task_notes=task_notes, notifications=list(att_scan.notifications),
                    )
                    try:
                        save_attention_state(transcript_dir, run_id, st.local, att_state)
                    except Exception as e:
                        print(f"[arena] attention state save failed: {e!r}", file=sys.stderr)
                    attention_fields = {
                        "mode": attention_mode, "decision": "slept",
                        "directive": att_state.directive,
                        "skips_remaining": att_state.skips_remaining,
                        "streak": att_state.streak, "wake_cause": None,
                    }
                    log.append({
                        "player": st.local, "turn": st.turn,
                        "slept": True, "attention": attention_fields,
                    })
                    if _tx_on:
                        _num = ("score", "gold", "science", "culture", "faith", "cities", "units")
                        if prev_snapshot is not None and state_before is not None:
                            # A partial snapshot in the arena-owned state file
                            # (dict-shaped but missing a numeric key, or a
                            # wrong-typed value) means the delta is unknowable
                            # -- record None, degrade not abort (review catch:
                            # load validates dict shape, not key presence or
                            # value types).
                            try:
                                state_delta = {
                                    k: state_before[k] - prev_snapshot[k] for k in _num
                                }
                                state_delta["research"] = state_before["research"]
                                state_delta["civic"] = state_before["civic"]
                            except (KeyError, TypeError):
                                state_delta = None
                        else:
                            state_delta = None
                        _pol_backend = getattr(pol, "backend", None)
                        transcript.write({
                            "schema_version": 1,
                            "run_id": run_id,
                            "ts": datetime.now(timezone.utc).isoformat(),
                            "player_id": st.local,
                            "turn": st.turn,
                            "provider": getattr(pol, "provider", "local"),
                            "model": getattr(_pol_backend, "model", getattr(pol, "model", "")),
                            "driver": "cli" if str(getattr(pol, "provider", "local")).startswith("cli") else "in_process",
                            "turn_kind": "slept",
                            "slept": True,
                            "step_count": 0,
                            "usd": 0.0,
                            "prompt_tokens": 0,
                            "completion_tokens": 0,
                            "state_before": prev_snapshot,
                            "state_after": state_before,
                            "state_delta": state_delta,
                            "standing_memory": {
                                "loaded": bool(memory), "injected": False,
                                "injected_chars": 0, "captured_chars": 0,
                                "error": memory_error,
                            },
                            "task_tracker": {
                                "active_before": len(active_tasks_before),
                                "pre_model_results": task_results,
                                "active_after": len(active_tasks_after),
                                "error": task_tracker_error,
                            },
                            "attention": attention_fields,
                        })
                    await hook.finish_units(conn, st.local)
                    await hook.restore_local(conn, 0)
                    slept += 1
                    game_turns += 1
                    deadline_polls -= 1
                    continue
                if decision is not None and att_state.slept:
                    try:
                        digest_block = render_digest(
                            att_state, wake_turn=st.turn,
                            wake_cause=decision.wake_cause or "",
                            wake_detail=decision.wake_detail,
                        )
                    except Exception as e:
                        # A tampered slept record (e.g. missing "turn") must
                        # cost the digest, not the run (final-review pinhole).
                        digest_block = ""
                        print(f"[arena] wake digest render failed: {e!r}", file=sys.stderr)

                # Gate every injected kwarg on the policy's signature (the
                # briefing precedent): a pre-slice-3 policy with a bare
                # (gs, player_id, turn) __call__ must keep working.
                policy_kwargs = {
                    name: value
                    for name, value in (
                        ("memory_block", memory_block),
                        ("task_block", task_block),
                        ("digest_block", digest_block),
                    )
                    if _policy_accepts_kwarg(pol, name)
                }
                # Capability snapshot (spec §1): once per puppet turn, cheap
                # GameCore read. Signature-gated like every injected kwarg;
                # ANY failure fails open (no kwarg -> agent uses full tier).
                if _policy_accepts_kwarg(pol, "caps"):
                    caps = None
                    try:
                        cap_lines = await conn.execute_read(
                            build_caps_query(st.local)
                        )
                        caps = parse_caps(cap_lines)
                        if caps is None:
                            print(
                                "[arena] capability snapshot unparseable; "
                                "fail-open full toolset",
                                file=sys.stderr,
                            )
                    except Exception as e:
                        print(
                            f"[arena] capability snapshot failed; "
                            f"fail-open full toolset: {e!r}",
                            file=sys.stderr,
                        )
                    if caps is not None:
                        policy_kwargs["caps"] = caps
                if (
                    exclusive
                    and opts.briefing.enabled
                    and _policy_accepts_kwarg(pol, "briefing")
                ):
                    try:
                        playbook_chars = (
                            len(load_playbook()) if opts.playbook == "condensed" else 0
                        )
                        policy_kwargs["briefing"] = await maybe_build_briefing(
                            gs,
                            opts,
                            n_ctx=explicit_n_ctx(opts.context_budget),
                            playbook_chars=playbook_chars,
                            tool_schema_chars=0,
                        )
                    except Exception as e:
                        # A per-civ briefing-build failure (a missing playbook
                        # file, a budget-calc raise) must degrade THIS civ to no
                        # briefing, never abort the whole multi-civ run --
                        # mirroring the memory/task-tracker load guards above and
                        # the promotion-sweep guard below. Omitting the kwarg is
                        # the same state a non-exclusive turn uses, so the policy
                        # already tolerates its absence.
                        print(f"[arena] briefing build failed: {e!r}", file=sys.stderr)

                if exclusive and conn.is_connected:
                    await conn.disconnect()       # free the single tuner slot for the CLI
                try:
                    result = await pol(gs, st.local, st.turn, **policy_kwargs)
                except Exception as e:
                    # A single failed LLM turn -- e.g. the gateway 500s on a malformed/
                    # truncated tool call (openai.InternalServerError) -- must degrade THIS
                    # puppet turn, never abort the whole multi-turn run. Mirrors the
                    # sweep/memory/task/briefing guards below and the human-safety invariant:
                    # reclaim the tuner (an exclusive CLI turn released it), hand the seat
                    # back to the human, consume the puppet-turn budget, and continue.
                    # Exception (not BaseException) so a CancelledError/Ctrl-C still unwinds
                    # to the finally's guarded handback.
                    print(f"[arena] puppet turn seat {st.local} turn {st.turn} failed, "
                          f"skipping: {e!r}", file=sys.stderr)
                    log.append({"turn": st.turn, "player_id": st.local,
                                "skipped": True, "error": repr(e)})
                    if attention_on and att_state is not None and att_state.skips_remaining > 0:
                        # Spec section 3: ANY wake cancels the directive remainder.
                        # This failed turn WAS a wake decision -- note_wake never
                        # runs on this path, so cancel here or the seat resumes a
                        # stale sleep right after the system misbehaved
                        # (final-review Important 2). Keeps the slept accumulator
                        # so the digest survives to the eventual successful wake.
                        att_state = cancel_remainder(att_state)
                        try:
                            save_attention_state(transcript_dir, run_id, st.local, att_state)
                        except Exception as save_exc:
                            print(f"[arena] attention state save failed: {save_exc!r}",
                                  file=sys.stderr)
                    if not conn.is_connected:
                        await _reconnect_with_retry(conn)
                    await hook.finish_units(conn, st.local)
                    await hook.restore_local(conn, 0)
                    remaining -= 1
                    game_turns += 1
                    deadline_polls -= 1
                    continue
                if exclusive and not conn.is_connected:
                    await _reconnect_with_retry(conn)   # reclaim before we end the turn
                try:
                    swept = await autoresolve.sweep_promotions(gs)
                except Exception as e:
                    swept = []
                    print(f"[arena] promotion sweep failed: {e!r}", file=sys.stderr)

                # --- Capture this turn's standing plan / tasks from the final summary.
                # Runs whenever standing-plan capture is enabled, since memory and
                # task tracking both parse the same STANDING PLAN block.
                captured_plan = ""
                final_summary = ""
                if opts.standing_plan_enabled or opts.attention_directives_enabled:
                    final_summary = (
                        result.get("transcript", {}).get("final_summary")
                        or result.get("summary", "")
                    )
                if opts.standing_plan_enabled:
                    captured_plan = extract_standing_plan(
                        final_summary,
                        opts.standing_plan_capture_chars,
                    )
                # Save even when the turn-start load/format failed: save_memory
                # is a full atomic overwrite with no dependence on the loaded
                # object, so persisting the model's fresh plan both keeps it and
                # self-heals a poison file. Gating on `not memory_error` instead
                # discarded the new plan and left the bad file to fail every
                # subsequent turn.
                if opts.memory.enabled and captured_plan:
                    try:
                        save_memory(
                            transcript_dir, run_id, st.local, st.turn, captured_plan,
                            opts.memory.max_chars,
                        )
                    except Exception as e:
                        memory_error = repr(e)
                        print(f"[arena] standing memory save failed: {e!r}", file=sys.stderr)
                if opts.task_tracker.enabled and task_capture_base is not None:
                    try:
                        # Parse from the raw summary, not the captured plan: the
                        # capture clamp must never cost us a trailing TASK line.
                        new_tasks = parse_task_lines(final_summary, st.turn)
                        merged = merge_tasks(task_capture_base, new_tasks, opts.task_tracker.max_tasks)
                        captured_state = save_task_state(transcript_dir, run_id, st.local, merged)
                        active_tasks_after = tuple(
                            t for t in captured_state.tasks if t.status == "active"
                        )
                    except Exception as e:
                        task_tracker_error = repr(e)
                        print(f"[arena] task tracker capture failed: {e!r}", file=sys.stderr)

                # Attention needs the POST-play snapshot as the next wake
                # baseline even with transcripts off (review-2 finding 2) --
                # note_wake's state_before fallback would otherwise bake the
                # puppet's own turn into the next quiet-turn delta.
                state_after = (
                    await _overview_snapshot(gs) if (_tx_on or attention_on) else None
                )
                directive = None
                directive_ack = ""
                wake_attention_fields = None
                if attention_on and att_state is not None:
                    if opts.attention_directives_enabled:
                        directive = parse_directive(final_summary, opts.attention.max_skip)
                        if directive is not None:
                            note = " (clamped)" if directive.clamped else ""
                            directive_ack = f"SKIP {directive.skip} accepted{note}"
                            if directive.unknown_tokens:
                                directive_ack += (
                                    f"; unknown tokens dropped: {','.join(directive.unknown_tokens)}"
                                )
                        elif has_directive_lines(final_summary):
                            directive_ack = "directive not recognized"
                    wake_cause = decision.wake_cause if decision is not None else None
                    wake_attention_fields = {
                        "mode": attention_mode, "decision": "woke",
                        "wake_cause": wake_cause,
                        "directive": (
                            {"skip": directive.skip, "wake_if": list(directive.wake_if)}
                            if directive else None
                        ),
                        "digest_chars": len(digest_block),
                        "directive_ack": directive_ack,
                    }
                    att_state = note_wake(
                        att_state, turn=st.turn,
                        wake_cause=wake_cause or "", directive=directive,
                        directive_ack=directive_ack,
                        snapshot=state_after if state_after is not None else state_before,
                        scan_scalars=scan_scalars(att_scan) if att_scan is not None else None,
                    )
                    try:
                        save_attention_state(transcript_dir, run_id, st.local, att_state)
                    except Exception as e:
                        print(f"[arena] attention state save failed: {e!r}", file=sys.stderr)
                _log_entry = {
                    k: v
                    for k, v in result.items()
                    if k not in ("transcript", "promotion_sweep")
                }
                # Report what actually reached the model, not what was loaded:
                # the kwarg gate strips memory_block for a policy whose __call__
                # doesn't accept it, and analyze.behavior_metrics counts these
                # as standing-memory turns -- so a stripped block must read as
                # not injected.
                injected_block = policy_kwargs.get("memory_block", "")
                # Same rule on the capture side: extraction also feeds the task
                # tracker, so with memory disabled captured_plan can be non-empty
                # while nothing is ever saved or injectable -- report 0 or a
                # tracker-only civ reads as a standing-memory-captured turn.
                _standing_memory_fields = {
                    "loaded": bool(memory),
                    "injected": bool(injected_block),
                    "injected_chars": len(injected_block),
                    "captured_chars": len(captured_plan) if opts.memory.enabled else 0,
                    "error": memory_error,
                }
                _task_tracker_fields = {
                    "active_before": len(active_tasks_before),
                    "pre_model_results": task_results,
                    "active_after": len(active_tasks_after),
                    "error": task_tracker_error,
                }
                log.append({
                    "player": st.local,
                    "turn": st.turn,
                    **_log_entry,
                    "promotion_sweep": swept,
                    "standing_memory": _standing_memory_fields,
                    "task_tracker": _task_tracker_fields,
                })
                if _tx_on and result.get("transcript"):
                    payload = result["transcript"]
                    steps = payload.get("steps", [])
                    if state_before is not None and state_after is not None:
                        _num = ("score", "gold", "science", "culture", "faith", "cities", "units")
                        state_delta = {k: state_after[k] - state_before[k] for k in _num}
                        state_delta["research"] = state_after["research"]
                        state_delta["civic"]    = state_after["civic"]
                    else:
                        state_delta = None
                    _pol_backend = getattr(pol, "backend", None)
                    record = {
                        **payload,
                        "schema_version": 1,
                        "run_id":   run_id,
                        "ts":       datetime.now(timezone.utc).isoformat(),
                        "player_id": st.local,
                        "turn":     st.turn,
                        "provider": getattr(pol, "provider", "local"),
                        "model":    getattr(_pol_backend, "model", getattr(pol, "model", "")),
                        "driver":   "cli" if str(getattr(pol, "provider", "local")).startswith("cli") else "in_process",
                        "step_count": len(steps),
                        "usd":      float(result.get("usage", {}).get("usd", 0.0)),
                        "state_before": state_before,
                        "state_after":  state_after,
                        "state_delta":  state_delta,
                        "promotion_sweep": swept,
                        "standing_memory": _standing_memory_fields,
                        "task_tracker": _task_tracker_fields,
                        "turn_kind": "played",
                    }
                    if wake_attention_fields is not None:
                        record["attention"] = wake_attention_fields
                    transcript.write(record)
                # End this puppet's turn and hand control back toward the human.
                # DESIGN NOTE — the turn-end method is validated by the live dry-run gate (Task 9).
                # Primary (verified in the feasibility spike): finish_units(K) + restore_local(0).
                # If the live gate shows the engine does NOT advance / hand back cleanly, add an
                # InGame `UI.RequestAction(ActionTypes.ACTION_ENDTURN)` HERE — while local == K,
                # before restore_local. NEVER add it in the finally block (local is already 0 there).
                await hook.finish_units(conn, st.local)
                await hook.restore_local(conn, 0)
                played += 1
                remaining -= 1
                game_turns += 1
            else:
                # Human seat is idle. Do NOT auto-clear VIEW-level diplomacy here:
                # _clear_blocking_diplomacy cannot distinguish an orphaned first-meet
                # greeting from a leader scene the human is actively using (declaring
                # war, denouncing, trading), and force-hiding the latter mid-transition
                # can black out the map — it stays a reactive/manual tool.
                #
                # SESSION-level orphans are different: an open session between two
                # non-local players (a greeting queued for/between puppet seats) can
                # never be clicked by the human and wedges the AI phase indefinitely,
                # so after a long idle streak sweep those closed. The sweep skips
                # every session involving the local player by construction.
                idle_streak += 1
                if idle_streak % ORPHAN_SWEEP_IDLE_POLLS == 0:
                    swept_sessions = await _sweep_orphan_sessions(conn)
                    if swept_sessions not in ("ORPHANS|none", "?", "err"):
                        print(f"[arena] orphan diplomacy sessions closed after "
                              f"{idle_streak} idle polls: {swept_sessions}",
                              file=sys.stderr)
                        log.append({"turn": st.turn, "orphan_sweep": swept_sessions})
                await asyncio.sleep(1.0)
            deadline_polls -= 1
        return {"puppet_turns_played": played, "turns_slept": slept, "log": log}
    finally:
        # Human safety invariant: ALWAYS hand control back. Reclaim a released connection first,
        # then restore the human, then disable — run all three best-effort so a failure in one
        # never skips the others. Each step is guarded against BaseException (not just Exception)
        # so an asyncio.CancelledError mid-handback (e.g. Ctrl-C during connect/restore) cannot
        # skip a later step.
        #
        # Re-raise policy: only interrupts (BaseException that is NOT a plain Exception —
        # CancelledError, KeyboardInterrupt, SystemExit) are re-raised; ordinary cleanup failures
        # (a dead-socket ConnectionError from reclaim, a transient hook.disable blip) are logged
        # and swallowed, matching the original best-effort contract. This is load-bearing: when
        # cancellation originates in the TRY BODY (Ctrl-C during the long CLI turn) it is already
        # in flight as we run cleanup; re-raising an ordinary cleanup Exception here would REPLACE
        # that in-flight CancelledError and swallow the cancellation. Swallowing best-effort
        # Exceptions lets the body's CancelledError keep propagating; re-raising a cleanup-origin
        # interrupt still surfaces it. Either way cancellation is propagated, never swallowed.
        first_exc = None
        steps = []
        if not conn.is_connected:
            steps.append(("reclaim-retry", lambda: _reconnect_with_retry(conn)))
        steps.append(("restore_local(0)", lambda: hook.restore_local(conn, 0)))
        steps.append(("hook.disable", lambda: hook.disable(conn)))
        for label, step in steps:
            try:
                await step()
            except BaseException as e:
                if first_exc is None:
                    first_exc = e
                print(f"[arena] WARNING: {label} failed in cleanup: {e!r}", file=sys.stderr)
        if first_exc is not None and not isinstance(first_exc, Exception):
            raise first_exc
