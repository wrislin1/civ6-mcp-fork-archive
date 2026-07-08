#!/usr/bin/env bash
set -euo pipefail

remote="${CIV6_ARENA_HOST:-riz@192.168.20.141}"

echo "LOCAL_PROCS"
pgrep -af 'civ-mcp|civ-arena|codex exec|claude|ssh.*4318' || true

echo "LOCAL_SOCKETS"
ss -tnp 2>/dev/null | grep 4318 || true

echo "LOCAL_SAFE_API"
curl -sS --max-time 8 http://127.0.0.1:8000/api/overview 2>/dev/null | head -1 || true
echo

ssh -o ConnectTimeout=10 "$remote" 'bash -s' <<'REMOTE'
echo "REMOTE_WSL_PROCS"
ps -eo pid,ppid,pgid,stat,etime,comm,args \
  | grep -E '([c]iv-arena|[c]iv-mcp|[c]odex exec|[c]laude|ssh.*4318)' || true

echo "REMOTE_WSL_SOCKETS"
ss -tn 2>/dev/null | grep 4318 || true

echo "WINDOWS_NETSTAT_4318"
/mnt/c/Windows/System32/NETSTAT.EXE -ano 2>/dev/null | grep 4318 || true

echo "WINDOWS_CIV_TASKS"
/mnt/c/Windows/System32/tasklist.exe 2>/dev/null \
  | grep -iE 'CivilizationVI|2KLauncher|FireTuner' || true
REMOTE
