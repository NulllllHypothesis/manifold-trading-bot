# Heartbeat disabled.
#
# OpenClaw reads this file on every heartbeat tick. Per the OpenClaw convention,
# a file that is empty or contains ONLY commented lines makes the agent reply
# HEARTBEAT_OK and do nothing.
#
# To re-enable heartbeat work, add actionable, uncommented instructions below.
# Do NOT add status-check prose here — that causes the agent to re-derive its
# own idea of the bot's health from stale cron logs and fire false alerts.
# Observability is handled by:
#   - Op3 cron counters (data/research_counters.jsonl, data/trader_counters.jsonl)
#   - The hourly cron log files (/tmp/research.log, /tmp/trader.log)
#   - The daily summary at 19:00 UTC
