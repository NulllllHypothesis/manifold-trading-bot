#!/usr/bin/env bash
#
# check-connection.sh — diagnose the sandbox connection for the dashboard.
# Pure read-only probe. Never modifies anything on either side.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

if [ -z "${SANDBOX_PASS:-}" ] && [ -f "$REPO_ROOT/.env" ]; then
  set -a; source <(grep -E '^SANDBOX_PASS=' "$REPO_ROOT/.env" || true); set +a
fi

SANDBOX_SSH_HOST="${SANDBOX_SSH_HOST:-hackathon-server}"
SANDBOX_REMOTE_ROOT="${SANDBOX_REMOTE_ROOT:-/home/hackathon/.openclaw/workspace}"

if [ -t 1 ]; then
  C_OK=$'\033[0;32m'; C_WARN=$'\033[0;33m'; C_ERR=$'\033[0;31m'
  C_DIM=$'\033[2m';   C_BOLD=$'\033[1m';   C_END=$'\033[0m'
else
  C_OK=""; C_WARN=""; C_ERR=""; C_DIM=""; C_BOLD=""; C_END=""
fi

row() { printf "  %-32s %s\n" "$1" "$2"; }

echo "${C_BOLD}Dashboard ↔ Sandbox connection report${C_END}"
echo "  $(date)"
echo

echo "${C_BOLD}Local tooling${C_END}"
for tool in ssh rsync sshpass jq; do
  if command -v "$tool" >/dev/null 2>&1; then
    row "$tool" "${C_OK}$(command -v "$tool")${C_END}"
  else
    row "$tool" "${C_WARN}not installed${C_END}"
  fi
done
echo

echo "${C_BOLD}Configuration${C_END}"
row "SANDBOX_SSH_HOST"     "$SANDBOX_SSH_HOST"
row "SANDBOX_REMOTE_ROOT"  "$SANDBOX_REMOTE_ROOT"
row "SANDBOX_PASS in env"  "$([ -n "${SANDBOX_PASS:-}" ] && echo "${C_OK}yes${C_END}" || echo "${C_DIM}no${C_END}")"
row "~/.ssh/config entry"  "$(grep -A1 "^Host $SANDBOX_SSH_HOST" ~/.ssh/config 2>/dev/null | grep HostName | awk '{print $2}' || echo "${C_WARN}not found${C_END}")"
echo

echo "${C_BOLD}TCP reachability${C_END}"
host=$(ssh -G "$SANDBOX_SSH_HOST" 2>/dev/null | awk '/^hostname / {print $2}')
port=$(ssh -G "$SANDBOX_SSH_HOST" 2>/dev/null | awk '/^port / {print $2}')
if [ -z "$port" ]; then port=22; fi
if nc -z -w5 "$host" "$port" 2>/dev/null; then
  row "$host:$port" "${C_OK}reachable${C_END}"
else
  row "$host:$port" "${C_ERR}unreachable (Tailscale not connected?)${C_END}"
  echo "${C_ERR}✗ Stopping — fix network first.${C_END}"
  exit 1
fi
echo

echo "${C_BOLD}SSH auth${C_END}"
if ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=no \
    "$SANDBOX_SSH_HOST" 'echo OK' 2>/dev/null | grep -q OK; then
  row "SSH key auth" "${C_OK}working (preferred)${C_END}"
  AUTH="ssh"
elif [ -n "${SANDBOX_PASS:-}" ] && command -v sshpass >/dev/null 2>&1; then
  if sshpass -p "$SANDBOX_PASS" ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no \
      "$SANDBOX_SSH_HOST" 'echo OK' 2>/dev/null | grep -q OK; then
    row "sshpass + SANDBOX_PASS" "${C_OK}working${C_END}"
    AUTH="sshpass"
  else
    row "sshpass + SANDBOX_PASS" "${C_ERR}rejected${C_END}"
    AUTH=""
  fi
else
  row "SSH key auth" "${C_WARN}rejected${C_END}"
  row "sshpass + SANDBOX_PASS" "${C_DIM}no password configured${C_END}"
  AUTH=""
fi

if [ -z "$AUTH" ]; then
  echo
  echo "${C_ERR}✗ No auth path available.${C_END} Fix one of:"
  echo "  a. SSH key:   ssh-copy-id $SANDBOX_SSH_HOST"
  echo "  b. Password:  echo 'SANDBOX_PASS=…' >> workspace/.env"
  exit 1
fi
echo

echo "${C_BOLD}Remote filesystem${C_END}"
if [ "$AUTH" = "ssh" ]; then
  SSH="ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=no"
else
  SSH="sshpass -p $SANDBOX_PASS ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=no"
fi

remote_ls=$($SSH "$SANDBOX_SSH_HOST" "cd $SANDBOX_REMOTE_ROOT && ls -la --time-style=+%Y-%m-%dT%H:%M:%S market_research.json manifold_bot/paper_trading_state.json data/research_counters.jsonl data/trader_counters.jsonl data/strategy_weights.json data/calibration_table.json 2>&1" 2>&1 || true)

if echo "$remote_ls" | grep -q "No such"; then
  row "workspace root"  "${C_ERR}not found at $SANDBOX_REMOTE_ROOT${C_END}"
else
  row "workspace root"  "${C_OK}$SANDBOX_REMOTE_ROOT${C_END}"
  echo
  echo "${C_DIM}  key files (remote mtime → size):${C_END}"
  echo "$remote_ls" | while read -r line; do
    if echo "$line" | grep -qE "^-"; then
      size=$(echo "$line" | awk '{print $5}')
      mtime=$(echo "$line" | awk '{print $6}')
      name=$(echo "$line" | awk '{for(i=7;i<=NF;i++) printf "%s ",$i; print ""}' | sed 's/ $//')
      printf "    %-48s %-20s %10s bytes\n" "$name" "$mtime" "$size"
    fi
  done
fi
echo

echo "${C_BOLD}Sandbox processes${C_END}"
procs=$($SSH "$SANDBOX_SSH_HOST" "pgrep -af 'openclaw|auto_research|auto_trader|resolve_positions' 2>/dev/null | head -5" 2>&1 || true)
if [ -n "$procs" ]; then
  echo "$procs" | while read -r line; do
    row " " "${C_DIM}$line${C_END}"
  done
else
  row "openclaw / cron jobs" "${C_WARN}no active bot processes matched${C_END}"
fi
echo

echo "${C_BOLD}Recent research activity${C_END}"
last_counter=$($SSH "$SANDBOX_SSH_HOST" "tail -1 $SANDBOX_REMOTE_ROOT/data/research_counters.jsonl 2>/dev/null | head -c 500" 2>&1 || true)
if [ -n "$last_counter" ]; then
  ts=$(echo "$last_counter" | sed -n 's/.*"timestamp": *"\([^"]*\)".*/\1/p')
  fetched=$(echo "$last_counter" | sed -n 's/.*"markets_fetched": *\([0-9]*\).*/\1/p')
  final=$(echo "$last_counter" | sed -n 's/.*"final_recommendations": *\([0-9]*\).*/\1/p')
  row "last research run"  "$ts"
  row "markets fetched"    "$fetched"
  row "final recommendations" "$final"
else
  row "research counters" "${C_WARN}empty or unreadable${C_END}"
fi
echo

echo "${C_OK}${C_BOLD}All checks passed.${C_END}"
echo "Run the sync:"
echo "  ${C_BOLD}pnpm sync${C_END}         # one-shot"
echo "  ${C_BOLD}pnpm dev:sandbox${C_END}  # sync then boot dev server against the snapshot"
