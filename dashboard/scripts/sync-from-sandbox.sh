#!/usr/bin/env bash
#
# sync-from-sandbox.sh — Pull the bot's data files from the sandbox to a local
# snapshot directory the dashboard can read. Safe, read-only, idempotent.
#
# Usage:
#   ./scripts/sync-from-sandbox.sh           # one-shot pull
#   WATCH=60 ./scripts/sync-from-sandbox.sh  # loop, re-pull every 60s
#
# Credentials priority:
#   1. SSH key auth (silent, no password). Set up once with:
#        ssh-copy-id hackathon-server
#   2. sshpass + SANDBOX_PASS env var. Put SANDBOX_PASS in workspace/.env
#      (per CLAUDE.md convention) and export it before running.
#
# Env vars:
#   SANDBOX_SSH_HOST      SSH host alias or user@ip (default: hackathon-server)
#   SANDBOX_REMOTE_ROOT   Path on sandbox to pull from
#                         (default: /home/hackathon/.openclaw/workspace)
#   SANDBOX_SNAPSHOT_DIR  Where to put the snapshot locally
#                         (default: /tmp/manifold-sandbox-snapshot)
#   SANDBOX_PASS          Password for sshpass fallback (optional)
#   WATCH                 Seconds between pulls when set (e.g. WATCH=60)
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Load SANDBOX_PASS from repo .env if present and not already set.
# NOTE: use `eval "$(...)"` not `source <(...)` — the latter silently drops
# the assignment under bash 3.2 (macOS default) with `set -euo pipefail`.
if [ -z "${SANDBOX_PASS:-}" ] && [ -f "$REPO_ROOT/.env" ]; then
  _sandbox_pass_line="$(grep -E '^SANDBOX_PASS=' "$REPO_ROOT/.env" || true)"
  if [ -n "$_sandbox_pass_line" ]; then
    set -a; eval "$_sandbox_pass_line"; set +a
  fi
  unset _sandbox_pass_line
fi

SANDBOX_SSH_HOST="${SANDBOX_SSH_HOST:-hackathon-server}"
SANDBOX_REMOTE_ROOT="${SANDBOX_REMOTE_ROOT:-/home/hackathon/.openclaw/workspace}"
SANDBOX_SNAPSHOT_DIR="${SANDBOX_SNAPSHOT_DIR:-/tmp/manifold-sandbox-snapshot}"
WATCH="${WATCH:-}"

# Files we pull. Keep in sync with dashboard/src/lib/config.ts DATA_PATHS.
FILES=(
  "market_research.json"
  "auto_trades.json"
  "pending_swaps.json"
  "manifold_bot/paper_trading_state.json"
  "data/research_counters.jsonl"
  "data/trader_counters.jsonl"
  "data/strategy_weights.json"
  "data/calibration_table.json"
  "data/category_accuracy.json"
  "data/m2_audit.json"
  "data/eval_report.json"
  "data/backtest_report.json"
  "data/ai_timeout_cooldown.json"
  "data/news_cache.json"
  "data/calibration.db"
  "data/market_snapshots.db"
)

# Colours (tty only)
if [ -t 1 ]; then
  C_OK=$'\033[0;32m'; C_WARN=$'\033[0;33m'; C_ERR=$'\033[0;31m'
  C_DIM=$'\033[2m';   C_BOLD=$'\033[1m';   C_END=$'\033[0m'
else
  C_OK=""; C_WARN=""; C_ERR=""; C_DIM=""; C_BOLD=""; C_END=""
fi

# Choose ssh/rsync wrapper based on available auth
ssh_wrapper=()
rsync_rsh=()

detect_auth() {
  # Try SSH key auth first (quick BatchMode probe)
  if ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=no \
      "$SANDBOX_SSH_HOST" 'exit 0' 2>/dev/null; then
    ssh_wrapper=(ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=no)
    rsync_rsh=("ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=no")
    auth_mode="ssh-key"
    return 0
  fi

  # Fall back to sshpass
  if [ -n "${SANDBOX_PASS:-}" ]; then
    if ! command -v sshpass >/dev/null 2>&1; then
      echo "${C_ERR}✗ sshpass not installed.${C_END} Install: brew install hudochenkov/sshpass/sshpass" >&2
      return 1
    fi
    ssh_wrapper=(sshpass -p "$SANDBOX_PASS" ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=no)
    rsync_rsh=("sshpass -p $SANDBOX_PASS ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=no")
    auth_mode="sshpass"
    return 0
  fi

  cat >&2 <<EOF
${C_ERR}✗ No working auth method.${C_END}
Tried:
  • SSH key auth to ${C_BOLD}$SANDBOX_SSH_HOST${C_END} — denied
  • sshpass with \$SANDBOX_PASS — not set

Pick one:
  1. Set up SSH keys (recommended, one-time):
       ssh-copy-id $SANDBOX_SSH_HOST

  2. Put SANDBOX_PASS in workspace/.env:
       echo 'SANDBOX_PASS=yourpassword' >> $REPO_ROOT/.env
EOF
  return 1
}

sync_once() {
  local started_at elapsed file remote local size bytes_total=0 ok=0 fail=0
  started_at=$(date +%s)

  mkdir -p "$SANDBOX_SNAPSHOT_DIR"
  echo "${C_BOLD}==> Sync${C_END} $SANDBOX_SSH_HOST:$SANDBOX_REMOTE_ROOT → $SANDBOX_SNAPSHOT_DIR ${C_DIM}[$auth_mode]${C_END}"

  for file in "${FILES[@]}"; do
    remote="$SANDBOX_REMOTE_ROOT/$file"
    local="$SANDBOX_SNAPSHOT_DIR/$file"
    mkdir -p "$(dirname "$local")"

    # Use rsync for efficiency (skip unchanged files, show per-file status)
    if rsync -az --quiet \
        -e "${rsync_rsh[0]}" \
        "$SANDBOX_SSH_HOST:$remote" \
        "$local" 2>/dev/null; then
      if [ -f "$local" ]; then
        size=$(wc -c <"$local" | tr -d ' ')
        bytes_total=$((bytes_total + size))
        printf "  ${C_OK}✓${C_END} %-48s ${C_DIM}%10s bytes${C_END}\n" "$file" "$size"
        ok=$((ok + 1))
      else
        printf "  ${C_WARN}·${C_END} %-48s ${C_DIM}(empty on remote)${C_END}\n" "$file"
      fi
    else
      printf "  ${C_WARN}·${C_END} %-48s ${C_DIM}(missing on remote)${C_END}\n" "$file"
      fail=$((fail + 1))
    fi
  done

  elapsed=$(( $(date +%s) - started_at ))
  echo "${C_DIM}Done. ${C_END}${C_OK}$ok fetched${C_END}${C_DIM}, ${C_END}${C_WARN}$fail missing${C_END}${C_DIM}, ${C_END}${C_BOLD}$(numfmt --to=iec "$bytes_total" 2>/dev/null || echo "$bytes_total bytes")${C_END}${C_DIM} in ${elapsed}s${C_END}"
  echo
  echo "${C_BOLD}Run the dashboard against this snapshot:${C_END}"
  echo "  WORKSPACE_ROOT=$SANDBOX_SNAPSHOT_DIR pnpm dev"
}

auth_mode="(detecting)"
detect_auth || exit 1

if [ -n "$WATCH" ]; then
  echo "${C_BOLD}Watch mode:${C_END} re-syncing every ${WATCH}s. Ctrl-C to stop."
  while true; do
    sync_once
    sleep "$WATCH"
  done
else
  sync_once
fi
