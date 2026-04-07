#!/usr/bin/env python3
"""
Weekly EV accuracy report.

Reads the bet_outcomes table (written by paper_trader.py at market resolution)
and compares estimated_ev vs actual_pnl to measure model accuracy.

This is the core calibration feedback loop:
  - If EV=+$10 trades average +$3 actual → AI overestimates edge by 3.3×
  - If EV=+$10 trades average +$9 actual → model is well-calibrated
  - Breakdowns by strategy and confidence band reveal which combinations have real edge

Usage:
  python3 scripts/weekly_ev_report.py           # print to console
  python3 scripts/weekly_ev_report.py --telegram # also write telegram_weekly_ev.txt
"""

import sys
import os
import sqlite3
import json
import argparse
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB_DIR  = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
DB_PATH = os.path.join(DB_DIR, "calibration.db")


def _fetch_outcomes(days: int = None) -> list[dict]:
    """Load bet_outcomes rows, optionally filtered to the last N days."""
    if not os.path.exists(DB_PATH):
        return []

    conn = sqlite3.connect(DB_PATH)
    # Check table exists
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='bet_outcomes'"
    ).fetchone()
    if not exists:
        conn.close()
        return []

    if days:
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        rows = conn.execute("""
            SELECT market_id, our_recommendation, amount, probability,
                   estimated_ev, ai_confidence, strategies,
                   market_resolution, actual_pnl, ev_error, resolved_at
            FROM bet_outcomes
            WHERE resolved_at >= ?
            ORDER BY resolved_at
        """, (cutoff,)).fetchall()
    else:
        rows = conn.execute("""
            SELECT market_id, our_recommendation, amount, probability,
                   estimated_ev, ai_confidence, strategies,
                   market_resolution, actual_pnl, ev_error, resolved_at
            FROM bet_outcomes
            ORDER BY resolved_at
        """).fetchall()
    conn.close()

    cols = ['market_id', 'our_recommendation', 'amount', 'probability',
            'estimated_ev', 'ai_confidence', 'strategies',
            'market_resolution', 'actual_pnl', 'ev_error', 'resolved_at']
    return [dict(zip(cols, row)) for row in rows]


def _ev_accuracy_section(outcomes: list[dict]) -> tuple[str, dict]:
    """
    Compute overall EV accuracy.

    Returns (text_block, stats_dict).

    Key metrics:
      - ev_accuracy_ratio: avg(actual_pnl) / avg(estimated_ev)
        1.0 = perfectly calibrated
        0.3 = AI overestimates edge by 3×
        > 1.0 = AI underestimates (trades outperform predictions)
    """
    with_ev  = [o for o in outcomes if o['estimated_ev'] is not None]
    total    = len(outcomes)
    ev_count = len(with_ev)

    lines = []
    stats = {"total_resolved": total, "with_ev_estimate": ev_count}

    if total == 0:
        lines.append("No resolved trades yet.")
        return "\n".join(lines), stats

    total_pnl    = sum(o['actual_pnl'] for o in outcomes)
    win_count    = sum(1 for o in outcomes if o['actual_pnl'] > 0)
    win_rate     = win_count / total if total > 0 else 0

    lines.append(f"Resolved trades:  {total}")
    lines.append(f"Win rate:         {win_rate*100:.0f}%  ({win_count}W / {total-win_count}L)")
    lines.append(f"Total actual P&L: ${total_pnl:+.2f}")
    stats.update({"win_rate": round(win_rate, 3), "total_pnl": round(total_pnl, 2)})

    if ev_count == 0:
        lines.append("No AI-estimated EV trades yet (all used confidence-scaled sizing).")
        return "\n".join(lines), stats

    avg_ev  = sum(o['estimated_ev'] for o in with_ev) / ev_count
    avg_pnl = sum(o['actual_pnl']   for o in with_ev) / ev_count
    ratio   = avg_pnl / avg_ev if avg_ev != 0 else None

    lines.append(f"\nAI-estimated EV trades: {ev_count} of {total}")
    lines.append(f"  Avg estimated EV:  ${avg_ev:+.2f}")
    lines.append(f"  Avg actual P&L:    ${avg_pnl:+.2f}")
    if ratio is not None:
        lines.append(f"  EV accuracy ratio: {ratio:.2f}×")
        if   ratio >= 0.85:  lines.append("  → Model well-calibrated — trust Kelly sizing")
        elif ratio >= 0.50:  lines.append("  → Model overestimates edge moderately — monitor")
        elif ratio >= 0.20:  lines.append("  → Model significantly overestimates — scale down bets")
        else:                lines.append("  → Model severely miscalibrated — review AI prompts")

    stats.update({
        "avg_estimated_ev": round(avg_ev, 4),
        "avg_actual_pnl": round(avg_pnl, 4),
        "ev_accuracy_ratio": round(ratio, 3) if ratio is not None else None,
    })
    return "\n".join(lines), stats


def _strategy_breakdown(outcomes: list[dict]) -> str:
    """
    Break down actual P&L by strategy.

    strategies column is a JSON-encoded list. A single trade may have
    multiple strategies — we credit all of them.
    """
    by_strategy: dict[str, list[float]] = {}

    for o in outcomes:
        raw = o.get('strategies')
        if raw:
            try:
                strats = json.loads(raw) if isinstance(raw, str) else raw
            except Exception:
                strats = [str(raw)]
        else:
            strats = ['unknown']

        for s in (strats or ['unknown']):
            by_strategy.setdefault(s, []).append(o['actual_pnl'])

    if not by_strategy:
        return "No strategy data."

    lines = []
    # Sort by total P&L descending
    sorted_strats = sorted(by_strategy.items(), key=lambda x: sum(x[1]), reverse=True)
    for strat, pnls in sorted_strats:
        n = len(pnls)
        total = sum(pnls)
        avg = total / n
        wins = sum(1 for p in pnls if p > 0)
        lines.append(
            f"  {strat:<35} {n:>3} trades  "
            f"total ${total:+.2f}  avg ${avg:+.2f}  "
            f"win {wins}/{n}"
        )
    return "\n".join(lines)


def _confidence_breakdown(outcomes: list[dict]) -> str:
    """
    Break down actual P&L by AI confidence band.

    Bands: <0.70, 0.70-0.80, 0.80-0.90, ≥0.90
    This shows whether higher AI confidence actually predicts better outcomes.
    """
    bands = {
        "< 0.70": [],
        "0.70-0.80": [],
        "0.80-0.90": [],
        "≥ 0.90": [],
        "no AI": [],
    }
    for o in outcomes:
        c = o.get('ai_confidence')
        pnl = o['actual_pnl']
        if c is None:
            bands["no AI"].append(pnl)
        elif c < 0.70:
            bands["< 0.70"].append(pnl)
        elif c < 0.80:
            bands["0.70-0.80"].append(pnl)
        elif c < 0.90:
            bands["0.80-0.90"].append(pnl)
        else:
            bands["≥ 0.90"].append(pnl)

    lines = []
    for band, pnls in bands.items():
        if not pnls:
            continue
        n = len(pnls)
        total = sum(pnls)
        avg = total / n
        wins = sum(1 for p in pnls if p > 0)
        lines.append(
            f"  Confidence {band:<12} {n:>3} trades  "
            f"avg ${avg:+.2f}  win {wins}/{n}"
        )
    return "\n".join(lines) if lines else "No confidence data."


def generate_report(days: int = 7, telegram: bool = False) -> str:
    """
    Generate the full weekly EV report. Returns formatted text.

    Args:
        days:     How many days back to look (None = all time)
        telegram: If True, also write to telegram_weekly_ev.txt
    """
    outcomes = _fetch_outcomes(days=days)
    period   = f"last {days} days" if days else "all time"

    sep = "=" * 55
    lines = [
        sep,
        f"WEEKLY EV ACCURACY REPORT ({period})",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}",
        sep,
    ]

    if not outcomes:
        if not os.path.exists(DB_PATH):
            lines.append("\nbet_outcomes DB not found yet.")
            lines.append("Markets need to resolve before this report has data.")
        else:
            lines.append(f"\nNo resolved trades in the {period}.")
            lines.append("Markets are still open — check back after they resolve.")
        report = "\n".join(lines)
        print(report)
        return report

    # Overall EV accuracy
    lines.append("\nOVERALL EV ACCURACY")
    lines.append("─" * 40)
    ev_text, stats = _ev_accuracy_section(outcomes)
    lines.append(ev_text)

    # Strategy breakdown
    lines.append("\nBY STRATEGY")
    lines.append("─" * 40)
    lines.append(_strategy_breakdown(outcomes))

    # Confidence breakdown
    lines.append("\nBY AI CONFIDENCE BAND")
    lines.append("─" * 40)
    lines.append(_confidence_breakdown(outcomes))

    lines.append("\n" + sep)

    report = "\n".join(lines)
    print(report)

    if telegram:
        out_file = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "telegram_weekly_ev.txt"
        )
        with open(out_file, "w") as f:
            f.write(report)
        print(f"\nSaved to {out_file}")

    return report


def main():
    parser = argparse.ArgumentParser(description="Weekly EV accuracy report")
    parser.add_argument("--days",     type=int, default=7,  help="Days to look back (default: 7, 0=all time)")
    parser.add_argument("--telegram", action="store_true",  help="Write output to telegram_weekly_ev.txt")
    args = parser.parse_args()

    days = args.days if args.days > 0 else None
    generate_report(days=days, telegram=args.telegram)


if __name__ == "__main__":
    main()
