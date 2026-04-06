"""Job dispatch and main monitoring loop."""

from __future__ import annotations

import logging
import sys
import time

from .alert import send_alert
from .config import Config
from .health import (
    check_heartbeat,
    classify_failure,
    is_heartbeat_stale,
    is_stall_warning,
    is_stalled,
)
from .machine import Machine
from .state import BatchState, JobState

log = logging.getLogger("orchestrator")


def build_machines(config: Config) -> dict[str, Machine]:
    """Create Machine objects from config."""
    return {
        name: Machine(config=mc, ssh_timeout=config.defaults.ssh_timeout)
        for name, mc in config.machines.items()
    }


def build_job_queue(config: Config) -> dict[str, JobState]:
    """Build the initial job queue from the job matrix."""
    jobs: dict[str, JobState] = {}
    for spec in config.jobs:
        for run_num in range(1, spec.runs + 1):
            model_short = spec.model.rsplit("/", 1)[-1]
            jid = f"{spec.machine}_{model_short}_{spec.scenario}_{run_num}"
            jobs[jid] = JobState(
                id=jid,
                machine=spec.machine,
                model=spec.model,
                scenario=spec.scenario,
                run_num=run_num,
            )
    return jobs


def clean_machine(machine: Machine) -> None:
    """Pre-launch cleanup on a machine."""
    machine.clear_completion_sentinel()
    machine.clean_autosaves()
    machine.clear_local_telemetry()


def dispatch_job(job: JobState, machine: Machine, is_retry: bool = False) -> bool:
    """Launch a job on a machine. Returns True on success."""
    job.transition("launching", "dispatched")

    if not machine.is_reachable():
        job.transition("failed", "machine unreachable")
        return False

    # Verify inspect CLI exists — if not, try uv sync
    pkg_ok, pkg_info = machine.verify_packages()
    if not pkg_ok:
        log.warning("Packages missing on %s: %s — running uv sync", machine.name, pkg_info)
        machine.sync_packages()
        pkg_ok, pkg_info = machine.verify_packages()
        if not pkg_ok:
            job.transition("failed", f"packages broken: {pkg_info}")
            return False
    log.info("Package check OK on %s: %s", machine.name, pkg_info)

    # Only clean autosaves on first attempt — retries resume from autosave
    if not is_retry:
        machine.clean_autosaves()
    machine.clear_completion_sentinel()
    machine.clear_heartbeat()

    # Kill stale processes — only kill the game on retries (fresh dispatches
    # must not destroy a running game from a previous orchestrator instance)
    machine.kill_runner()
    if is_retry:
        machine.kill_game()
    time.sleep(5)

    # Launch
    ok = machine.launch_runner(job.model, job.scenario, 1)
    if not ok:
        job.transition("failed", "launch command failed")
        return False

    job.transition("booting", "runner launched")
    job.started_at = time.time()
    job.boot_started_at = time.time()
    job.last_turn_change = time.time()
    return True


def handle_failure(
    job: JobState,
    machine: Machine,
    reason: str,
    config: Config,
) -> None:
    """Handle a job failure — retry or mark failed."""
    failure_type, is_boot = classify_failure(job, config.defaults)
    max_retries = (
        config.defaults.max_boot_retries
        if is_boot
        else config.defaults.max_game_retries
    )
    current_retries = job.boot_retries if is_boot else job.game_retries

    if current_retries < max_retries:
        if is_boot:
            job.boot_retries += 1
        else:
            job.game_retries += 1
        retry_num = job.boot_retries if is_boot else job.game_retries
        job.transition("pending", f"{reason} — retry {retry_num}/{max_retries}")
        send_alert(
            f"{failure_type}: {job.id} — retry {retry_num}/{max_retries}",
            config.defaults.alert_webhook,
        )
    else:
        job.fail_reason = f"{reason} after {max_retries} retries"
        job.transition("failed", job.fail_reason)
        send_alert(
            f"Job failed: {job.id} — {job.fail_reason}",
            config.defaults.alert_webhook,
        )


def post_game_pipeline(job: JobState, machine: Machine, config: Config) -> None:
    """Run post-game pipeline: discover run_id, sync to Convex, alert."""
    job.finished_at = time.time()

    # 1. Discover run_id
    run_id = machine.discover_run_id()
    if run_id:
        job.run_id = run_id
        log.info("  Run ID: %s", run_id)

    # 2. Sync to Convex
    log.info("  Syncing to Convex...")
    if machine.sync_to_convex():
        job.synced = True
        log.info("  Sync OK")
    else:
        log.warning("  Sync failed — data on machine, retry with 'sync' command")

    # 3. Alert
    elapsed = (job.finished_at - job.started_at) / 3600
    short_model = job.model.rsplit("/", 1)[-1]
    rid_str = job.run_id or "?"
    send_alert(
        f"✓ {rid_str} | {short_model} | {job.scenario} | T{job.turn} | {elapsed:.1f}h",
        config.defaults.alert_webhook,
    )

    # 4. Mark done
    job.transition("done", "completed")


def poll_jobs(
    state: BatchState,
    machines: dict[str, Machine],
    config: Config,
) -> None:
    """Single poll cycle — check all active jobs."""
    for jid, job in state.jobs.items():
        if job.state not in ("booting", "running"):
            continue

        machine = machines.get(job.machine)
        if not machine:
            continue

        # Read heartbeat
        hb_info = check_heartbeat(machine, job, config.defaults)

        if hb_info is not None:
            phase = hb_info["phase"]

            # Transition booting → running on first turn advance
            if job.state == "booting" and job.turn > 0:
                job.transition("running", f"T{job.turn}")

            # Check for error/finished phases
            if phase == "finished":
                job.transition("completing", "game finished")
                post_game_pipeline(job, machine, config)
                continue

            # Heartbeat stale check
            if is_heartbeat_stale(hb_info, job, config.defaults):
                # Double-check after short delay
                time.sleep(5)
                hb2 = machine.read_heartbeat()
                hb2_ts = hb2.get("ts", 0) if hb2 else 0
                if time.time() - hb2_ts > config.defaults.playing_timeout:
                    log.warning(
                        "Heartbeat stale: %s (phase=%s, T%d, age=%.0fs)",
                        jid,
                        phase,
                        job.turn,
                        hb_info["age"],
                    )
                    machine.kill_runner()
                    machine.kill_game()
                    handle_failure(job, machine, "heartbeat stale", config)
                    continue

            # Stall detection (turn not advancing)
            if job.state == "running" and is_stalled(job, config.defaults):
                log.error("Stall timeout: %s at T%d", jid, job.turn)
                machine.kill_runner()
                machine.kill_game()
                handle_failure(job, machine, f"stall at T{job.turn}", config)
                continue

            if job.state == "running" and is_stall_warning(job, config.defaults):
                stall_min = (time.time() - job.last_turn_change) / 60
                log.warning("Stall: %s at T%d for %.0fm", jid, job.turn, stall_min)

        else:
            # No heartbeat — check grace period
            launch_age = time.time() - job.started_at
            if launch_age < 180:
                continue  # Still booting

            # Runner dead?
            if not machine.is_game_running():
                time.sleep(5)
                if not machine.is_game_running():
                    log.warning("Runner died (no heartbeat): %s", jid)
                    handle_failure(job, machine, "runner died (no heartbeat)", config)
                    continue

        # Check sentinel (game completed normally)
        if job.state in ("booting", "running") and time.time() - job.started_at > 180:
            if machine.check_completed():
                machine.clear_completion_sentinel()
                job.transition("completing", "sentinel found")
                post_game_pipeline(job, machine, config)


def run_batch(config: Config, state: BatchState | None = None) -> None:
    """Main orchestrator loop — dispatch and monitor until all jobs complete."""
    machines = build_machines(config)

    # Build or resume job queue
    if state is None:
        state = BatchState(
            started_at=time.time(),
            jobs=build_job_queue(config),
            config_snapshot={
                "scenario": config.scenario,
                "job_count": sum(s.runs for s in config.jobs),
            },
        )
        # Initial cleanup on all machines in the job matrix
        machine_names = {s.machine for s in config.jobs}
        for name in machine_names:
            m = machines.get(name)
            if m and m.is_reachable():
                clean_machine(m)
                log.info("  %s: cleaned", name)

    # Print schedule
    total = len(state.jobs)
    print(f"\nScheduled {total} jobs:")
    for jid, j in state.jobs.items():
        short = j.model.rsplit("/", 1)[-1]
        print(
            f"  {j.machine:<12} {short:<25} {j.scenario:<20} run {j.run_num}  [{j.state}]"
        )
    print()

    state.save()
    poll_count = 0

    while True:
        # Dispatch pending jobs to idle machines
        dispatching: set[str] = set()
        for jid, job in state.jobs.items():
            if job.state != "pending":
                continue
            machine = machines.get(job.machine)
            if (
                not machine
                or state.machine_busy(job.machine)
                or job.machine in dispatching
            ):
                continue

            dispatching.add(job.machine)
            is_retry = job.boot_retries > 0 or job.game_retries > 0
            log.info("Launching %s on %s", jid, job.machine)
            dispatch_job(job, machine, is_retry=is_retry)

        # Poll active jobs
        poll_jobs(state, machines, config)

        # Persist state
        state.save()

        # Check termination
        if state.all_terminal():
            counts = state.summary()
            done = counts.get("done", 0)
            failed = counts.get("failed", 0)
            print(f"\n{'═' * 50}")
            print(f"  All jobs complete: {done} done, {failed} failed")
            print(f"{'═' * 50}")
            send_alert(
                f"CivBench batch complete: {done} done, {failed} failed",
                config.defaults.alert_webhook,
            )
            break

        # Status line
        counts = state.summary()
        active_info = " | ".join(
            f"{j.machine}:{j.last_heartbeat_phase or '?'}:T{j.turn}"
            for j in state.jobs.values()
            if j.state in ("booting", "running")
        )
        sys.stdout.write(
            f"\r  [{counts.get('done', 0)}ok {counts.get('running', 0) + counts.get('booting', 0)}run "
            f"{counts.get('pending', 0)}wait {counts.get('failed', 0)}fail] {active_info}    "
        )
        sys.stdout.flush()

        poll_count += 1
        if poll_count % 10 == 0:
            active_parts = []
            for j in state.jobs.values():
                if j.state in ("booting", "running"):
                    elapsed_h = (time.time() - j.started_at) / 3600
                    active_parts.append(
                        f"{j.machine}:{j.last_heartbeat_phase}:T{j.turn}:{elapsed_h:.1f}h"
                    )
            log.info(
                "STATUS: [%d done %d run %d pend %d fail] %s",
                counts.get("done", 0),
                counts.get("running", 0) + counts.get("booting", 0),
                counts.get("pending", 0),
                counts.get("failed", 0),
                " | ".join(active_parts),
            )

        last_poll = time.time()
        time.sleep(config.defaults.poll_interval)

        # Sleep detection
        wake_gap = time.time() - last_poll - config.defaults.poll_interval
        if wake_gap > config.defaults.poll_interval * 2:
            log.warning(
                "Time jump (%.0fs) — resetting stall timers",
                wake_gap + config.defaults.poll_interval,
            )
            for job in state.jobs.values():
                if job.state in ("booting", "running"):
                    job.last_turn_change = time.time()
