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

echo "REMOTE_GIT"
git rev-parse --abbrev-ref HEAD
git rev-parse HEAD
git status --short | sed -n '1,120p'

echo "PROCS"
ps -eo pid,ppid,pgid,stat,etime,comm,args \
  | grep -E '([c]iv-arena|[c]odex exec|[c]iv-mcp|[u]v run [c]iv-arena)' || true

echo "LATEST_RUNS"
ls -1t .arena-runs/*.out 2>/dev/null | head -5 || true

latest="$(ls -1t .arena-runs/*.out 2>/dev/null | head -1 || true)"
if [[ -n "$latest" ]]; then
  echo "LATEST_OUT $latest"
  sed -n '1,260p' "$latest"
  err="${latest%.out}.err"
  if [[ -f "$err" ]]; then
    echo "LATEST_ERR $err"
    sed -n '1,160p' "$err"
  fi
fi

echo "COST_TAIL"
tail -30 arena_cost.hybrid.loop.jsonl 2>/dev/null || true
tail -30 arena_cost.hybrid.main.jsonl 2>/dev/null || true

echo "HOOK"
uv run python - <<'PY' || true
import asyncio
from civ_mcp.connection import GameConnection
from civ_mcp.arena import hook

async def main():
    conn = GameConnection()
    try:
        await conn.connect()
        print(await hook.poll(conn))
    except Exception as exc:
        print(type(exc).__name__ + ": " + str(exc))
    finally:
        try:
            await conn.disconnect()
        except Exception:
            pass

asyncio.run(main())
PY
REMOTE
