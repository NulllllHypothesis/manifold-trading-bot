#!/usr/bin/env python3
"""
M2 — Audit the resolved_markets corpus.

Before building M3 (snapshot reconstruction) we need to understand what's
actually in calibration.db. Three failure modes would make M3 useless:

  1. Near-close contamination — markets where probability_close was already
     >85% or <15% when they resolved. The outcome was obvious; reconstructing
     a snapshot 7 days earlier would just show ~90% → not a useful training
     example because there's no real decision to make.

  2. Category skew — if 80%+ of markets are "other", category-level weights
     and caps have no signal to learn from.

  3. Thin markets — markets with < 10 unique bettors are noisy. Crowd wisdom
     requires enough independent bettors.

Output:
  - Console report (always)
  - data/m2_audit.json  (machine-readable, consumed by reconstruct_snapshots.py
                         to know which market IDs are worth reconstructing)

Usage:
  python3 scripts/audit_resolved_markets.py
  python3 scripts/audit_resolved_markets.py --min-bettors 15 --near-close-threshold 0.80
  python3 scripts/audit_resolved_markets.py --quiet     # suppress per-category table
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

DB_PATH  = os.path.join(ROOT_DIR, "data", "calibration.db")
OUT_PATH = os.path.join(ROOT_DIR, "data", "m2_audit.json")

# ── Thresholds ────────────────────────────────────────────────────────────────

DEFAULT_MIN_BETTORS        = 10    # markets with fewer bettors than this are too noisy
DEFAULT_NEAR_CLOSE_HIGH    = 0.85  # probability_close above this → outcome was obvious
DEFAULT_NEAR_CLOSE_LOW     = 0.15  # probability_close below this → outcome was obvious
MIN_USABLE_FOR_M3          = 200   # below this M3 probably isn't worth it yet


# ── Core analysis functions ───────────────────────────────────────────────────

def load_markets(db_path: str) -> list[dict]:
    """
    Load all rows from resolved_markets.

    Returns a list of dicts. Works even if the DB is empty (returns []).
    Raises FileNotFoundError if the DB file doesn't exist at all.
    """
    if not os.path.exists(db_path):
        raise FileNotFoundError(
            f"calibration.db not found at {db_path}. "
            "Run scripts/harvest_resolved.py first."
        )
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT market_id, question, probability_close, outcome, "
        "close_date, unique_bettors, category FROM resolved_markets"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def category_distribution(markets: list[dict]) -> dict[str, dict]:
    """
    Count markets per category.

    Returns {category: {count, pct}} sorted by count descending.
    """
    counts: dict[str, int] = {}
    for m in markets:
        cat = m.get("category") or "other"
        counts[cat] = counts.get(cat, 0) + 1
    total = max(len(markets), 1)
    return {
        cat: {"count": cnt, "pct": round(cnt / total, 4)}
        for cat, cnt in sorted(counts.items(), key=lambda x: -x[1])
    }


def near_close_stats(
    markets: list[dict],
    high: float = DEFAULT_NEAR_CLOSE_HIGH,
    low:  float = DEFAULT_NEAR_CLOSE_LOW,
) -> dict:
    """
    Report what fraction of markets had an already-obvious outcome at close.

    A market at 92% YES probability at close resolved YES 92%+ of the time —
    there's no interesting decision to reconstruct. We want markets in the
    'decision zone' (low < p < high) where the outcome was genuinely uncertain.
    """
    total = len(markets)
    if total == 0:
        return {"near_close_count": 0, "near_close_pct": 0.0,
                "decision_zone_count": 0, "decision_zone_pct": 0.0,
                "threshold_high": high, "threshold_low": low}

    near_close = [
        m for m in markets
        if m.get("probability_close") is not None
        and (m["probability_close"] > high or m["probability_close"] < low)
    ]
    decision_zone = total - len(near_close)
    return {
        "near_close_count":    len(near_close),
        "near_close_pct":      round(len(near_close) / total, 4),
        "decision_zone_count": decision_zone,
        "decision_zone_pct":   round(decision_zone / total, 4),
        "threshold_high":      high,
        "threshold_low":       low,
    }


def bettor_stats(markets: list[dict], min_bettors: int = DEFAULT_MIN_BETTORS) -> dict:
    """
    Summarise unique_bettors distribution and flag thin markets.
    """
    counts = [m.get("unique_bettors") or 0 for m in markets]
    if not counts:
        return {"total": 0, "thin_count": 0, "thin_pct": 0.0,
                "median": 0, "p25": 0, "p75": 0, "min": 0, "max": 0,
                "min_bettors_threshold": min_bettors}

    counts_sorted = sorted(counts)
    n = len(counts_sorted)
    median = counts_sorted[n // 2]
    p25    = counts_sorted[n // 4]
    p75    = counts_sorted[(3 * n) // 4]
    thin   = sum(1 for c in counts if c < min_bettors)

    return {
        "total":                n,
        "thin_count":           thin,
        "thin_pct":             round(thin / n, 4),
        "median":               median,
        "p25":                  p25,
        "p75":                  p75,
        "min":                  counts_sorted[0],
        "max":                  counts_sorted[-1],
        "min_bettors_threshold": min_bettors,
    }


def date_range(markets: list[dict]) -> dict:
    """Return earliest/latest close_date in the corpus."""
    dates = [m["close_date"] for m in markets if m.get("close_date")]
    if not dates:
        return {"earliest": None, "latest": None, "span_days": None}
    dates_sorted = sorted(dates)
    earliest = dates_sorted[0]
    latest   = dates_sorted[-1]
    try:
        # ISO format from harvest_resolved.py includes timezone offset
        def _parse(s):
            # Handle both '+00:00' and 'Z' suffixes
            s = s.replace("Z", "+00:00")
            return datetime.fromisoformat(s)
        span = (_parse(latest) - _parse(earliest)).days
    except Exception:
        span = None
    return {"earliest": earliest, "latest": latest, "span_days": span}


def usable_market_ids(
    markets: list[dict],
    min_bettors: int  = DEFAULT_MIN_BETTORS,
    near_close_high: float = DEFAULT_NEAR_CLOSE_HIGH,
    near_close_low:  float = DEFAULT_NEAR_CLOSE_LOW,
) -> list[str]:
    """
    Return market IDs that pass all three quality filters:
      1. unique_bettors >= min_bettors
      2. probability_close in decision zone (low, high)
      3. probability_close is not None

    These are the markets M3 should reconstruct snapshots for.
    """
    result = []
    for m in markets:
        prob = m.get("probability_close")
        if prob is None:
            continue
        if m.get("unique_bettors", 0) < min_bettors:
            continue
        if prob > near_close_high or prob < near_close_low:
            continue
        result.append(m["market_id"])
    return result


def build_report(
    markets: list[dict],
    min_bettors: int  = DEFAULT_MIN_BETTORS,
    near_close_high: float = DEFAULT_NEAR_CLOSE_HIGH,
    near_close_low:  float = DEFAULT_NEAR_CLOSE_LOW,
) -> dict:
    """Assemble the full audit report dict."""
    cat_dist   = category_distribution(markets)
    nc_stats   = near_close_stats(markets, near_close_high, near_close_low)
    bet_stats  = bettor_stats(markets, min_bettors)
    dr         = date_range(markets)
    usable_ids = usable_market_ids(markets, min_bettors, near_close_high, near_close_low)

    # Decision gate
    other_pct = cat_dist.get("other", {}).get("pct", 0.0)
    recommendation = _decision_gate(
        usable_count=len(usable_ids),
        near_close_pct=nc_stats["near_close_pct"],
        thin_pct=bet_stats["thin_pct"],
        other_pct=other_pct,
    )

    return {
        "generated_at":          datetime.now(timezone.utc).isoformat(),
        "total_markets":         len(markets),
        "category_distribution": cat_dist,
        "near_close_stats":      nc_stats,
        "bettor_stats":          bet_stats,
        "date_range":            dr,
        "usable_for_m3":         len(usable_ids),
        "usable_market_ids":     usable_ids,
        "decision_gate":         recommendation,
        "thresholds": {
            "min_bettors":       min_bettors,
            "near_close_high":   near_close_high,
            "near_close_low":    near_close_low,
            "min_usable_for_m3": MIN_USABLE_FOR_M3,
        },
    }


def _decision_gate(
    usable_count: int,
    near_close_pct: float,
    thin_pct: float,
    other_pct: float,
) -> dict:
    """
    Produce a human-readable go/no-go recommendation for M3.

    Rules:
      - GREEN : usable_count >= MIN_USABLE_FOR_M3 and no severe data quality issues
      - YELLOW: usable_count >= MIN_USABLE_FOR_M3 but one quality flag raised
      - RED   : usable_count < MIN_USABLE_FOR_M3 — not worth building M3 yet
    """
    flags = []
    if near_close_pct > 0.60:
        flags.append(
            f"near-close contamination is high ({near_close_pct*100:.0f}% of markets "
            "had probability > 85% or < 15% at close — reconstructed snapshots may be low-signal)"
        )
    if thin_pct > 0.40:
        flags.append(
            f"many thin markets ({thin_pct*100:.0f}% have < {DEFAULT_MIN_BETTORS} bettors)"
        )
    if other_pct > 0.70:
        flags.append(
            f"category skew: {other_pct*100:.0f}% of markets are 'other' — "
            "category-level weights will lack granularity"
        )

    if usable_count < MIN_USABLE_FOR_M3:
        status = "RED"
        action = (
            f"Only {usable_count} usable markets (< {MIN_USABLE_FOR_M3} threshold). "
            "Run harvest_resolved.py --limit 3000 to collect more data before building M3."
        )
    elif flags:
        status = "YELLOW"
        action = (
            f"{usable_count} usable markets — M3 is viable but review flags before proceeding."
        )
    else:
        status = "GREEN"
        action = (
            f"{usable_count} usable markets — proceed with M3 reconstruction."
        )

    return {"status": status, "action": action, "flags": flags}


# ── Console printing ──────────────────────────────────────────────────────────

def print_report(report: dict, quiet: bool = False):
    total = report["total_markets"]
    print("=" * 65)
    print("M2 AUDIT — RESOLVED MARKETS CORPUS")
    print("=" * 65)
    print(f"\nTotal resolved markets:  {total}")

    dr = report["date_range"]
    if dr["earliest"]:
        print(f"Date range:              {dr['earliest'][:10]} → {dr['latest'][:10]} "
              f"({dr['span_days']} days)")

    # Near-close
    nc = report["near_close_stats"]
    print(f"\nDecision-zone markets:   {nc['decision_zone_count']} / {total} "
          f"({nc['decision_zone_pct']*100:.0f}%)  "
          f"[prob between {nc['threshold_low']*100:.0f}% and {nc['threshold_high']*100:.0f}%]")
    print(f"Near-close (low signal): {nc['near_close_count']} / {total} "
          f"({nc['near_close_pct']*100:.0f}%)  "
          f"[prob >{nc['threshold_high']*100:.0f}% or <{nc['threshold_low']*100:.0f}%]")

    # Bettor stats
    bs = report["bettor_stats"]
    print(f"\nBettor count:            median={bs['median']}, p25={bs['p25']}, p75={bs['p75']}, "
          f"min={bs['min']}, max={bs['max']}")
    print(f"Thin markets:            {bs['thin_count']} / {total} "
          f"({bs['thin_pct']*100:.0f}%)  [< {bs['min_bettors_threshold']} bettors]")

    # Category distribution
    if not quiet:
        print(f"\n{'─'*65}")
        print("CATEGORY DISTRIBUTION")
        print(f"{'─'*65}")
        print(f"  {'Category':15} {'Count':>6}  {'%':>6}")
        print(f"  {'─'*15} {'─'*6}  {'─'*6}")
        for cat, info in report["category_distribution"].items():
            bar = "█" * int(info["pct"] * 30)
            print(f"  {cat:15} {info['count']:>6}  {info['pct']*100:>5.1f}%  {bar}")

    # Usable count
    print(f"\n{'─'*65}")
    usable = report["usable_for_m3"]
    print(f"Usable for M3:           {usable}  "
          f"(pass: bettors ≥ {report['thresholds']['min_bettors']}, "
          f"prob in decision zone)")

    # Decision gate
    gate = report["decision_gate"]
    status_icon = {"GREEN": "✅", "YELLOW": "⚠️ ", "RED": "❌"}.get(gate["status"], "?")
    print(f"\n{'='*65}")
    print(f"DECISION GATE: {status_icon} {gate['status']}")
    print(f"  {gate['action']}")
    for flag in gate["flags"]:
        print(f"  ⚠  {flag}")
    print(f"{'='*65}")


# ── Entry point ───────────────────────────────────────────────────────────────

def run_audit(
    db_path: str = DB_PATH,
    out_path: str = OUT_PATH,
    min_bettors: int = DEFAULT_MIN_BETTORS,
    near_close_high: float = DEFAULT_NEAR_CLOSE_HIGH,
    near_close_low:  float = DEFAULT_NEAR_CLOSE_LOW,
    quiet: bool = False,
) -> dict:
    """Run the full audit and return the report dict. Saves to out_path."""
    markets = load_markets(db_path)
    report  = build_report(markets, min_bettors, near_close_high, near_close_low)
    print_report(report, quiet=quiet)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nAudit saved → {out_path}")
    print("M3 reads this file to know which market IDs are worth reconstructing.\n")
    return report


def main():
    parser = argparse.ArgumentParser(
        description="M2 audit: analyse resolved_markets corpus quality"
    )
    parser.add_argument(
        "--min-bettors", type=int, default=DEFAULT_MIN_BETTORS,
        help=f"Min unique bettors to consider a market usable (default: {DEFAULT_MIN_BETTORS})"
    )
    parser.add_argument(
        "--near-close-threshold", type=float, default=DEFAULT_NEAR_CLOSE_HIGH,
        help=f"Probability above which a market is 'near-close' (default: {DEFAULT_NEAR_CLOSE_HIGH})"
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Suppress per-category table"
    )
    args = parser.parse_args()
    run_audit(
        min_bettors=args.min_bettors,
        near_close_high=args.near_close_threshold,
        near_close_low=1.0 - args.near_close_threshold,
        quiet=args.quiet,
    )


if __name__ == "__main__":
    main()
