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

# ── Minimum-sample gates ────────────────────────────────────────────────────────
# Never adapt the system from tiny samples. These constants gate every
# adaptation decision: strategy weights, category caps, Kelly scaling.
# Below the gate, the relevant report section is shown but flagged as
# "insufficient data — for reference only, not used for adaptation".
MIN_SAMPLES_GLOBAL       = 20   # overall EV accuracy — below this, no global Kelly adjustment
MIN_SAMPLES_PER_STRATEGY = 10   # per-strategy reliability weight — below this, keep weight = 1.0
MIN_SAMPLES_PER_CATEGORY  = 8   # per-category accuracy — below this, keep default cap


def _fetch_outcomes(days: int = None, era: str = "post_ev_fix") -> list[dict]:
    """
    Load bet_outcomes rows, filtered by era and optionally by recency.

    Args:
        days: If set, only return rows from the last N days.
        era:  'post_ev_fix' (default) — only trusted rows where EV pipeline was
              working correctly.  Pass None to load all rows (e.g. for health checks).
    """
    if not os.path.exists(DB_PATH):
        return []

    conn = sqlite3.connect(DB_PATH)
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='bet_outcomes'"
    ).fetchone()
    if not exists:
        conn.close()
        return []

    conditions = []
    params: list = []

    if era:
        conditions.append("(era = ? OR (era IS NULL AND ? = 'pre_ev_fix'))")
        params.extend([era, era])

    if days:
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        conditions.append("resolved_at >= ?")
        params.append(cutoff)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    rows = conn.execute(f"""
        SELECT market_id, our_recommendation, amount, probability,
               estimated_ev, ai_confidence, ai_estimated_probability, strategies,
               market_resolution, actual_pnl, ev_error, era, resolved_at
        FROM bet_outcomes
        {where}
        ORDER BY resolved_at
    """, params).fetchall()
    conn.close()

    cols = ['market_id', 'our_recommendation', 'amount', 'probability',
            'estimated_ev', 'ai_confidence', 'ai_estimated_probability', 'strategies',
            'market_resolution', 'actual_pnl', 'ev_error', 'era', 'resolved_at']
    return [dict(zip(cols, row)) for row in rows]


def _direction_accuracy_section(outcomes: list[dict]) -> str:
    """
    Compute direction accuracy: did we bet the right way regardless of sizing?

    This is the simplest calibration check — no EV math needed, works even
    without estimated_ev.  A strategy with direction accuracy below 50% has
    literal negative edge.

    Also computes per-strategy direction accuracy to surface which strategies
    are calling direction correctly vs. just getting lucky on sizing.
    """
    if not outcomes:
        return "No resolved trades."

    total = len(outcomes)
    correct = sum(
        1 for o in outcomes
        if o.get("our_recommendation") == o.get("market_resolution")
    )
    acc = correct / total

    lines = [f"Direction correct: {correct}/{total} ({acc*100:.0f}%)"]

    gate_note = ""
    if total < MIN_SAMPLES_GLOBAL:
        gate_note = f"  ⚠️  {total} trades < {MIN_SAMPLES_GLOBAL} min — reference only, not used for adaptation"
        lines.append(gate_note)
    elif acc >= 0.60:
        lines.append("  → Strong direction signal — strategies finding real edge")
    elif acc >= 0.50:
        lines.append("  → Marginal edge — monitor over more trades")
    else:
        lines.append("  → Below 50% — strategies calling direction WRONG on average")

    # Per-strategy breakdown (only show strategies with >= 3 trades)
    by_strategy: dict[str, list[bool]] = {}
    for o in outcomes:
        raw = o.get("strategies")
        try:
            strats = json.loads(raw) if isinstance(raw, str) else (raw or [])
        except Exception:
            strats = [str(raw)] if raw else []
        correct_call = o.get("our_recommendation") == o.get("market_resolution")
        for s in (strats or ["unknown"]):
            by_strategy.setdefault(s, []).append(correct_call)

    strategy_lines = []
    for strat, calls in sorted(by_strategy.items(), key=lambda x: len(x[1]), reverse=True):
        if len(calls) < 3:
            continue
        strat_acc = sum(calls) / len(calls)
        flag = "✅" if strat_acc >= 0.60 else ("⚠️" if strat_acc >= 0.50 else "❌")
        strategy_lines.append(
            f"  {flag} {strat:<35} {sum(calls)}/{len(calls)} correct ({strat_acc*100:.0f}%)"
        )
    if strategy_lines:
        lines.append("\nBy strategy (≥3 trades):")
        lines.extend(strategy_lines)

    return "\n".join(lines)


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

    avg_estimated_ev = sum(o['estimated_ev'] for o in with_ev) / ev_count
    avg_pnl          = sum(o['actual_pnl']   for o in with_ev) / ev_count

    ratio = avg_pnl / avg_estimated_ev if abs(avg_estimated_ev) > 0.01 else None

    lines.append(f"\nAI-estimated EV trades: {ev_count} of {total}")
    lines.append(f"  Avg estimated EV:  ${avg_estimated_ev:+.2f}")
    lines.append(f"  Avg actual P&L:    ${avg_pnl:+.2f}")
    if ratio is not None:
        lines.append(f"  EV accuracy ratio: {ratio:.2f}×")
        if ev_count < MIN_SAMPLES_GLOBAL:
            lines.append(f"  ⚠️  {ev_count} EV trades < {MIN_SAMPLES_GLOBAL} min — reference only, not used for Kelly adjustment")
        elif ratio >= 0.85:  lines.append("  → Well-calibrated — trust Kelly sizing")
        elif ratio >= 0.50:  lines.append("  → Overestimates edge moderately — monitor")
        elif ratio >= 0.20:  lines.append("  → Significantly overestimates — consider scaling down")
        else:                lines.append("  → Severely miscalibrated — review AI prompts")
    else:
        lines.append(f"  EV accuracy ratio: N/A (near-zero EV)")

    stats.update({
        "avg_estimated_ev": round(avg_estimated_ev, 4),
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

    Only uses post_ev_fix era rows (where EV pipeline was working correctly).
    Pre-era rows are counted and noted but excluded from all accuracy calculations.

    Args:
        days:     How many days back to look (None = all time)
        telegram: If True, also write to telegram_weekly_ev.txt
    """
    outcomes     = _fetch_outcomes(days=days, era="post_ev_fix")
    all_outcomes = _fetch_outcomes(days=days, era=None)
    pre_era_count = len(all_outcomes) - len(outcomes)
    period   = f"last {days} days" if days else "all time"

    sep = "=" * 55
    lines = [
        sep,
        f"WEEKLY EV ACCURACY REPORT ({period})",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}",
        sep,
    ]

    if pre_era_count > 0:
        lines.append(f"\n(Excluded {pre_era_count} pre-EV-fix row(s) from calculations)")

    if not outcomes:
        if not os.path.exists(DB_PATH):
            lines.append("\nbet_outcomes DB not found yet.")
            lines.append("Markets need to resolve before this report has data.")
        else:
            lines.append(f"\nNo post-EV-fix resolved trades in the {period}.")
            lines.append("Markets are still open — check back after they resolve.")
        report = "\n".join(lines)
        print(report)
        return report

    # Direction accuracy (no EV needed — works on all resolved trades)
    lines.append("\nDIRECTION ACCURACY")
    lines.append("─" * 40)
    lines.append(_direction_accuracy_section(outcomes))

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
