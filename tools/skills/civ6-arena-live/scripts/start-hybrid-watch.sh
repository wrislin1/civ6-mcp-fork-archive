#!/usr/bin/env bash
set -euo pipefail

remote="${CIV6_ARENA_HOST:-riz@192.168.20.141}"
repo="${CIV6_ARENA_REPO:-projects/civ6-mcp}"

# ── Defaults ──────────────────────────────────────────────────────────────────
default_players=(
  "1:cli-claude:"
  "2:cli-codex:gpt-5.5"
  "3:local:gemma4-26b"
  "4:local:qwen3.6-27b"
)
default_max_puppet_turns=8
default_idle_poll_limit=3600
default_gateway_url="http://192.168.20.196:11444/v1"

# ── Usage ─────────────────────────────────────────────────────────────────────
usage() {
  cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Start the hybrid 4-civ arena watcher on the remote gaming PC ($remote).

Options:
  --config <path>           Repo-relative YAML experiment config
  --dry-run-args          Print civ-arena arguments and exit before SSH
  --player <spec>           Player spec (repeatable; default: 4-player preset)
                              Preset: ${default_players[*]}
  --run-id <id>             Run identifier
                              Default: hybrid-4civ-<ISO8601Z>
  --max-puppet-turns <n>    Max puppet turns TOTAL across all seats, not per player
                              (default: $default_max_puppet_turns; one 4-civ round = 4)
  --idle-poll-limit <n>     Idle poll limit in seconds   (default: $default_idle_poll_limit)
  --gateway-url <url>       LiteLLM gateway URL          (default: $default_gateway_url)
  -h, --help                Print this usage and exit

Environment overrides:
  CIV6_ARENA_HOST           Remote SSH target (default: riz@192.168.20.141)
  CIV6_ARENA_REPO           Remote repo path  (default: projects/civ6-mcp)
EOF
}

emit_dry_run_args() {
  printf '%s\n' "${arena_args[@]}"
}

# ── Arg parsing (must happen before SSH — exits 0 for --help) ─────────────────
config_path=""
config_supplied=0
config_owned_overrides=()
players=()
run_id=""
dry_run_args=0
run_id_supplied=0
max_puppet_turns="$default_max_puppet_turns"
idle_poll_limit="$default_idle_poll_limit"
gateway_url="$default_gateway_url"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)
      [[ $# -ge 2 ]] || { echo "error: --config requires an argument" >&2; exit 1; }
      config_supplied=1
      config_path="$2"; shift 2 ;;
    --player)
      [[ $# -ge 2 ]] || { echo "error: --player requires an argument" >&2; exit 1; }
      players+=("$2"); shift 2 ;;
    --run-id)
      [[ $# -ge 2 ]] || { echo "error: --run-id requires an argument" >&2; exit 1; }
      run_id_supplied=1
      run_id="$2"; shift 2 ;;
    --max-puppet-turns)
      [[ $# -ge 2 ]] || { echo "error: --max-puppet-turns requires an argument" >&2; exit 1; }
      config_owned_overrides+=("--max-puppet-turns")
      max_puppet_turns="$2"; shift 2 ;;
    --idle-poll-limit)
      [[ $# -ge 2 ]] || { echo "error: --idle-poll-limit requires an argument" >&2; exit 1; }
      config_owned_overrides+=("--idle-poll-limit")
      idle_poll_limit="$2"; shift 2 ;;
    --gateway-url)
      [[ $# -ge 2 ]] || { echo "error: --gateway-url requires an argument" >&2; exit 1; }
      config_owned_overrides+=("--gateway-url")
      gateway_url="$2"; shift 2 ;;
    --dry-run-args)
      dry_run_args=1; shift ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
  esac
done

if [[ "$config_supplied" -eq 1 && -z "$config_path" ]]; then
  echo "error: --config requires a non-empty path" >&2
  exit 1
fi

if [[ "$config_supplied" -eq 1 && ${#players[@]} -gt 0 ]]; then
  echo "error: --config and --player are mutually exclusive" >&2
  exit 1
fi

if [[ "$config_supplied" -eq 1 && ${#config_owned_overrides[@]} -gt 0 ]]; then
  rejected_flags="${config_owned_overrides[*]}"
  rejected_flags="${rejected_flags// /, }"
  echo "error: --config cannot be combined with config-owned override(s): $rejected_flags" >&2
  exit 1
fi

# Compute run_id locally so it is consistent between the .arena-runs/ filename
# and the --run-id value forwarded to civ-arena.
[[ -n "$run_id" ]] || run_id="hybrid-4civ-$(date -u +%Y%m%dT%H%M%SZ)"

# Use default 4-player roster when no --config or --player args were supplied.
if [[ "$config_supplied" -eq 0 && ${#players[@]} -eq 0 ]]; then
  players=("${default_players[@]}")
fi

# ── Build civ-arena argument vector ───────────────────────────────────────────
arena_args=()
if [[ "$config_supplied" -eq 1 ]]; then
  arena_args=(
    "--config" "$config_path"
    "--config-default-max-puppet-turns" "$max_puppet_turns"
    "--config-default-idle-poll-limit" "$idle_poll_limit"
    "--config-default-gateway-url" "$gateway_url"
  )
  if [[ "$run_id_supplied" -eq 1 ]]; then
    arena_args+=("--run-id" "$run_id")
  fi
else
  for spec in "${players[@]}"; do
    arena_args+=("--player" "$spec")
  done
  arena_args+=(
    "--gateway-url"       "$gateway_url"
    "--max-puppet-turns"  "$max_puppet_turns"
    "--idle-poll-limit"   "$idle_poll_limit"
    "--run-id"            "$run_id"
  )
fi

if [[ "$dry_run_args" -eq 1 ]]; then
  emit_dry_run_args
  exit 0
fi

# Encode args for forwarding over SSH.  printf '%q' produces bash-safe quoting;
# all current tokens are space-free so it is effectively a no-op, but the
# quoting makes the forwarding robust to edge-case values (e.g. URLs).
# The resulting string is embedded in a double-quoted SSH command string, which
# is intentional: SSH passes the whole string to the remote shell for evaluation,
# and the remote shell re-parses the individual tokens as $@ for bash -s.
quoted_args=$(printf ' %q' "${arena_args[@]}")

# ── Launch on remote ───────────────────────────────────────────────────────────
# SC2029: local expansion of $repo/$run_id/$quoted_args into the SSH command
# string is intentional — they seed the remote env and positional args.
# shellcheck disable=SC2029
ssh -o ConnectTimeout=10 "$remote" \
  "CIV6_ARENA_REPO='$repo' CIV6_ARENA_RUN_ID='$run_id' bash -s --$quoted_args" <<'REMOTE'
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"

repo="${CIV6_ARENA_REPO}"
run_id="${CIV6_ARENA_RUN_ID}"

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
out=".arena-runs/${run_id}.out"
err=".arena-runs/${run_id}.err"
pidfile=".arena-runs/${run_id}.pid"

# $@ = full civ-arena arg vector forwarded from the local launcher via bash -s
setsid uv run civ-arena "$@" >"$out" 2>"$err" < /dev/null &

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
