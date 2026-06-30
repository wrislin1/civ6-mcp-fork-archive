---
name: civ6-arena-live
description: Use when operating or debugging live civ6-mcp arena watcher runs on the gaming PC, including human handoff, puppet turns, Codex CLI civs, FireTuner, and cleanup.
---

# Civ6 Arena Live

## Overview

Live arena operation has two separate states: the Civilization game turn state and the external watcher process state. Verify both before telling the user to end a turn or before claiming the game is safely back to the human.

## Environment

- WSL gaming PC: `riz@192.168.20.141`
- Windows side: `ssh -p 2222 wrisl@192.168.20.141`
- Remote repo: `~/projects/civ6-mcp`
- Known-good hybrid watchers (both verified live):
  - Codex: player 1 `local:qwen3-coder:30b`, player 2 `cli-codex:gpt-5.5`
  - Claude: player 1 `local:gemma4:26b`, player 2 `cli-claude:` (empty model = Claude default)
  - both with `--max-puppet-turns 2` and `--idle-poll-limit 1800`

## Operating Pattern

CLI-provider pre-flight (when a watcher uses `cli-claude` or `cli-codex`), on the target host before arming — a failed seat only surfaces mid-handoff otherwise:

1. The provider binary is on PATH **and authenticated** (e.g. a trivial `claude -p` / `codex exec` actually returns). A host that has only ever run one provider may not have the other set up.
2. The gateway is reachable and the local model id is actually served (`curl .../v1/models`) — model names drift (`gemma4:26b`, not `gemma4-26b-cpp`).
3. `.mcp.json` is present in the repo CWD (project auto-discovery needs it).
4. Tip: a diagnostic `claude -p` calling `get_game_overview` with no game loaded should return the FireTuner `4318` connection error — that proves the civ6 tools load on this host.

Before telling the user to end turn:

1. Check for existing `civ-arena`, `codex exec`, and `civ-mcp` processes.
2. Start exactly one watcher if none is intentionally running.
3. Confirm it is alive and record `RUN_ID`, `PID`, `OUT`, and `ERR`.
4. Only then tell the user to end the turn.

After the user says it is back to them:

1. Read the watcher output and cost tail.
2. Confirm `puppet_turns_played: 2`.
3. Confirm hook state is `PuppetState(local=0, active=False, ...)`.
4. Confirm no arena/Codex/MCP processes remain.
5. Start the next watcher only if the user wants the next cycle armed.

## Important Invariants

- The current watcher is per-cycle. It exits after `--max-puppet-turns 2`; it is not a daemon.
- `PuppetState(local=0, active=False)` means human control is back.
- A direct hook poll can fail while an arena or Codex MCP child owns the single FireTuner connection. Do not treat that alone as proof Civ is down.
- End-of-session means no watcher process is running unless the user explicitly asks to keep one armed.

## Landing code on `.141`

`origin` is the **`.141` non-bare checkout** with `main` checked out, so `git push origin main` is rejected (`denyCurrentBranch`). To land work:

1. Push the *feature branch* to origin (a non-checked-out ref is accepted).
2. SSH in and `git merge --ff-only <branch>` inside `~/projects/civ6-mcp` — this advances `main` and updates its working tree in place.
3. `git fetch origin` on the dev machine (riz-llm) to sync the `origin/main` tracking ref.

`origin` is the gaming PC, not GitHub; there is no GitHub remote on riz-llm.

## Scripts

Run from the repo root:

- `tools/skills/civ6-arena-live/scripts/arena-live-status.sh`
- `tools/skills/civ6-arena-live/scripts/start-hybrid-watch.sh`
- `tools/skills/civ6-arena-live/scripts/stop-arena-watchers.sh`

The stop script is dry-run by default; pass `--yes` to terminate matching watcher process groups.

