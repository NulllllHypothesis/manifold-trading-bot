#!/usr/bin/env python3
"""
Print the OS crontab block for the Manifold trading bot.

SCHEDULER: Linux OS crontab (NOT OpenClaw cron).
OpenClaw agentTurn cron jobs were disabled on 2026-04-09 because they route
through an LLM agent that Telegram-narrates every run regardless of settings.
OS cron runs scripts directly — zero agent, zero narration.

Usage:
  python3 automation/setup_cron_jobs.py

  Then paste the printed block into `crontab -e` on the server, or pipe it:
    python3 automation/setup_cron_jobs.py | crontab -

  View the live crontab:
    crontab -l

  Edit:
    crontab -e

Logs (on server):
  /tmp/research.log        hourly market research
  /tmp/reprice.log         hourly position repricing (Phase 2.2)
  /tmp/resolution.log      hourly position resolution
  /tmp/trader.log          hourly trading
  /tmp/evaluate.log        hourly position evaluator (Phase 2.3, propose-only)
  /tmp/stale.log           daily stale detection (Phase 2.4, STRANDED/ABANDONED)
  /tmp/daily_summary.log   daily summary
  /tmp/harvest.log         Sunday ML pipeline (harvest/M2/M3/backtest)
  /tmp/ev_report.log       Monday EV report
  /tmp/weights.log         Monday strategy weights
"""

import os
import sys

WORKSPACE = "/home/hackathon/.openclaw/workspace"

CRONTAB = f"""SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
WORKSPACE={WORKSPACE}

# Market research — hourly :00
0 * * * * cd $WORKSPACE && git pull origin main -q && python3 automation/auto_research.py >> /tmp/research.log 2>&1

# Position repricing — hourly :05 (Phase 2.2)
# Runs BEFORE resolve_positions.py at :10 so resolving markets get a final
# pre-resolution snapshot captured. Measure-only — no close decisions.
5 * * * * cd $WORKSPACE && git pull origin main -q && python3 scripts/reprice_positions.py >> /tmp/reprice.log 2>&1

# Position resolution — hourly :10
10 * * * * cd $WORKSPACE && git pull origin main -q && python3 scripts/resolve_positions.py >> /tmp/resolution.log 2>&1

# Polymarket market-data ingestion — hourly :15 (Tier-2 multi-platform pivot)
# Read-only fetcher that writes to data/markets_normalized.db. No part of the
# existing trader/research pipeline reads from this table yet — it accumulates
# while we plan cross-platform matching. Slot :15 is between resolve (:10) and
# trade (:20), and is otherwise unused.
15 * * * * cd $WORKSPACE && git pull origin main -q && python3 scripts/ingest_polymarket.py >> /tmp/polymarket_ingest.log 2>&1

# Auto trading — hourly :20
20 * * * * cd $WORKSPACE && git pull origin main -q && python3 automation/auto_trader.py >> /tmp/trader.log 2>&1

# Position evaluator — hourly :30 (Phase 2.3, propose-only)
# Scores every OPEN position using Phase 2.2 repricing data and emits CLOSE
# proposals to pending_closes.json for human approval. Never auto-executes.
# Runs AFTER :20 trade so newly-opened positions don't get scored before
# they've been repriced once at :05 next hour.
30 * * * * cd $WORKSPACE && git pull origin main -q && python3 scripts/evaluate_positions.py >> /tmp/evaluate.log 2>&1

# Stale position detection — daily 13:00 UTC (Phase 2.4)
# Auto-flips past-close-and-grace positions to STRANDED (reversible,
# no P&L impact). Propose-only for ABANDONED at 90d timeout (realises
# full loss; operator approves via execute_abandon.py). Slow-moving
# signal — no reason to run hourly.
0 13 * * * cd $WORKSPACE && git pull origin main -q && python3 scripts/detect_stale_positions.py >> /tmp/stale.log 2>&1

# Daily summary — 19:00 UTC
0 19 * * * cd $WORKSPACE && git pull origin main -q && python3 automation/daily_summary.py >> /tmp/daily_summary.log 2>&1

# Weekly calibration harvest — Sunday 02:00 UTC
0 2 * * 0 cd $WORKSPACE && git pull origin main -q && python3 scripts/harvest_resolved.py --limit 2000 && python3 scripts/analyze_calibration.py >> /tmp/harvest.log 2>&1

# Weekly M2 re-audit — Sunday 02:30 UTC (after harvest)
30 2 * * 0 cd $WORKSPACE && python3 scripts/audit_resolved_markets.py --quiet >> /tmp/harvest.log 2>&1

# Weekly M3 reconstruction — Sunday 03:00 UTC (after M2)
0 3 * * 0 cd $WORKSPACE && python3 scripts/reconstruct_snapshots.py >> /tmp/harvest.log 2>&1

# Weekly backtest → strategy weights — Sunday 03:30 UTC (after M3)
30 3 * * 0 cd $WORKSPACE && python3 scripts/backtest_from_snapshots.py >> /tmp/harvest.log 2>&1

# Weekly EV report — Monday 07:00 UTC
0 7 * * 1 cd $WORKSPACE && git pull origin main -q && python3 scripts/weekly_ev_report.py --telegram >> /tmp/ev_report.log 2>&1

# Weekly strategy weights — Monday 07:30 UTC
30 7 * * 1 cd $WORKSPACE && git pull origin main -q && python3 scripts/compute_strategy_weights.py && git add data/strategy_weights.json && git diff --cached --quiet || git commit -m "chore: update strategy weights [skip ci]" >> /tmp/weights.log 2>&1
"""


def main():
    print("=" * 60)
    print("MANIFOLD BOT — OS CRONTAB SETUP")
    print("=" * 60)
    print()
    print("Paste the block below into `crontab -e` on the server,")
    print("or run:  python3 automation/setup_cron_jobs.py | crontab -")
    print()
    print("-" * 60)
    print(CRONTAB)
    print("-" * 60)
    print()
    print("SCHEDULE SUMMARY (all UTC):")
    print("  :00 hourly   — market research (swap check runs inside)")
    print("  :05 hourly   — position repricing (measure-only)")
    print("  :10 hourly   — position resolution")
    print("  :15 hourly   — Polymarket ingestion (multi-platform pivot, write-only)")
    print("  :20 hourly   — auto trading")
    print("  :30 hourly   — position evaluator (propose-only, human-approved closes)")
    print("  13:00 daily  — stale detection (auto STRANDED, propose ABANDONED)")
    print("  19:00 daily  — daily summary")
    print("  Sun 02:00    — calibration harvest")
    print("  Sun 02:30    — M2 re-audit (quality gate)")
    print("  Sun 03:00    — M3 snapshot reconstruction")
    print("  Sun 03:30    — backtest → strategy weights")
    print("  Mon 07:00    — weekly EV report → Telegram")
    print("  Mon 07:30    — live strategy weights → git commit")
    print()
    print("VERIFY AFTER INSTALL:")
    print("  crontab -l                   # confirm entries are live")
    print("  tail -f /tmp/research.log    # watch next :00 run")
    print()
    print("NOTE: OpenClaw cron jobs on this server are ALL DISABLED.")
    print("Do not re-enable them — they send every run to Telegram via LLM.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
