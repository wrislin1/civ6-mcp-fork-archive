#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: stop-arena-watchers.sh [--yes]

Dry-run by default. Pass --yes to terminate matching civ-arena/Codex/civ-mcp
watcher process groups on the gaming PC.
EOF
}

confirm=0
case "${1:-}" in
  --yes) confirm=1 ;;
  -h|--help) usage; exit 0 ;;
  "") ;;
  *) usage >&2; exit 2 ;;
esac

remote="${CIV6_ARENA_HOST:-riz@192.168.20.141}"

ssh -o ConnectTimeout=10 "$remote" "CONFIRM='$confirm' bash -s" <<'REMOTE'
set -euo pipefail

matches="$(
  ps -eo pid,ppid,pgid,stat,etime,comm,args \
    | grep -E '([c]iv-arena|[c]odex exec|[c]iv-mcp|[u]v run [c]iv-arena)' || true
)"

if [[ -z "$matches" ]]; then
  echo "No arena/Codex/MCP watcher processes found."
  exit 0
fi

echo "MATCHING_PROCESSES"
echo "$matches"

pgids="$(awk '{print $3}' <<<"$matches" | sort -n -u)"

if [[ "$CONFIRM" != 1 ]]; then
  echo
  echo "Dry-run only. Re-run with --yes to kill process groups:"
  echo "$pgids" | sed 's/^/  - /'
  exit 0
fi

while IFS= read -r pgid; do
  [[ -n "$pgid" ]] || continue
  echo "TERM process group $pgid"
  kill -TERM -- "-$pgid" 2>/dev/null || true
done <<<"$pgids"

sleep 3

remaining="$(
  ps -eo pid,ppid,pgid,stat,etime,comm,args \
    | grep -E '([c]iv-arena|[c]odex exec|[c]iv-mcp|[u]v run [c]iv-arena)' || true
)"

if [[ -n "$remaining" ]]; then
  echo "Remaining after TERM:"
  echo "$remaining"
  awk '{print $3}' <<<"$remaining" | sort -n -u | while IFS= read -r pgid; do
    [[ -n "$pgid" ]] || continue
    echo "KILL process group $pgid"
    kill -KILL -- "-$pgid" 2>/dev/null || true
  done
  sleep 1
fi

echo "FINAL_PROCESSES"
ps -eo pid,ppid,pgid,stat,etime,comm,args \
  | grep -E '([c]iv-arena|[c]odex exec|[c]iv-mcp|[u]v run [c]iv-arena)' || true
REMOTE
