#!/usr/bin/env bash
set -euo pipefail

remote="${CIV6_ARENA_HOST:-riz@192.168.20.141}"
repo="${CIV6_ARENA_REPO:-projects/civ6-mcp}"

ssh -o ConnectTimeout=10 "$remote" "CIV6_ARENA_REPO='$repo' bash -s" <<'REMOTE'
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"

repo="${CIV6_ARENA_REPO}"
case "$repo" in
  /*) cd "$repo" ;;
  *) cd "$HOME/$repo" ;;
esac

existing="$(
  ps -eo pid,ppid,pgid,stat,etime,comm,args \
    | grep -E '([c]iv-arena|[c]odex exec|[c]iv-mcp|[u]v run [c]iv-arena)' || true
)"
if [[ -n "$existing" ]]; then
  echo "Existing arena/Codex/MCP process found; not starting another watcher." >&2
  echo "$existing" >&2
  exit 2
fi

mkdir -p .arena-runs
run_id="hybrid-$(date -u +%Y%m%dT%H%M%SZ)"
out=".arena-runs/${run_id}.out"
err=".arena-runs/${run_id}.err"
pidfile=".arena-runs/${run_id}.pid"

setsid uv run civ-arena \
  --player 1:local:qwen3-coder:30b \
  --player 2:cli-codex:gpt-5.5 \
  --max-puppet-turns 2 \
  --idle-poll-limit 1800 \
  --cost-path arena_cost.hybrid.loop.jsonl \
  >"$out" 2>"$err" < /dev/null &

pid="$!"
echo "$pid" > "$pidfile"
sleep 2

if ! kill -0 "$pid" 2>/dev/null; then
  echo "Watcher exited immediately." >&2
  echo "OUT=$out" >&2
  sed -n '1,160p' "$out" >&2 || true
  echo "ERR=$err" >&2
  sed -n '1,160p' "$err" >&2 || true
  exit 1
fi

printf 'RUN_ID=%s\nPID=%s\nOUT=%s\nERR=%s\n' "$run_id" "$pid" "$out" "$err"
ps -eo pid,ppid,pgid,stat,etime,comm,args \
  | grep -E '([c]iv-arena|[c]odex exec|[c]iv-mcp|[u]v run [c]iv-arena)' || true
REMOTE
