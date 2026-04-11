#!/usr/bin/env python3
"""
Op5 diagnostic — measure strategy coverage on the live bot.

Reads the Op3 counter artifacts + the calibration table + the latest
research_counters / market_research files and reports:

  1. 24h strategy fire rates — which strategies are firing, how often,
     and with what deduplication outcome.
  2. Category coverage of the live market population (not just resolved
     markets).
  3. Per-category and per-(category, bucket) bias after category v2,
     including which cells WOULD unlock probability_bias at a proposed
     threshold.
  4. "No active signals" cluster breakdown — how much of the sample
     each hour is producing zero signals and what those markets look
     like, so we can decide if there's room for a new strategy.

This script writes NOTHING — it is read-only analysis. Run it on the
server (where the live files live) OR locally by pointing --root at a
downloaded copy. Output is plain text so it composes cleanly into
commit messages and reviews.

Usage
-----
  python3 scripts/analyze_strategy_coverage.py
  python3 scripts/analyze_strategy_coverage.py --proposed-bias-threshold 0.025
  python3 scripts/analyze_strategy_coverage.py --hours 48
"""

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from typing import Optional

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_RESEARCH_COUNTERS = os.path.join(ROOT_DIR, "data", "research_counters.jsonl")
_TRADER_COUNTERS   = os.path.join(ROOT_DIR, "data", "trader_counters.jsonl")
_MARKET_RESEARCH   = os.path.join(ROOT_DIR, "market_research.json")
_CALIBRATION_TABLE = os.path.join(ROOT_DIR, "data", "calibration_table.json")
_CALIBRATION_DB    = os.path.join(ROOT_DIR, "data", "calibration.db")


# ── Loaders ──────────────────────────────────────────────────────────────────

def _load_jsonl(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
    return out


def _load_json(path: str) -> Optional[dict]:
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def _filter_recent(entries: list[dict], hours: int) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    recent = []
    for e in entries:
        ts = e.get("timestamp")
        if not ts:
            continue
        try:
            t = datetime.fromisoformat(ts)
        except ValueError:
            continue
        if t >= cutoff:
            recent.append(e)
    return recent


# ── Section 1: strategy fire rates ───────────────────────────────────────────

def report_strategy_fires(research_rows: list[dict]) -> None:
    print()
    print("=" * 72)
    print("  1. Strategy fire rates")
    print("=" * 72)

    if not research_rows:
        print("\n  No research counter entries in window. Nothing to report.")
        return

    n_runs = len(research_rows)
    total_markets = sum(r.get("markets_fetched", 0) for r in research_rows)

    # Sum raw_fires and dedup_winners across runs
    raw = Counter()
    dedup = Counter()
    no_signals = 0
    tied = 0
    final = 0

    ai_eligible = 0
    ai_cooled = 0
    ai_analyzed = 0
    ai_agree = 0
    ai_disagree = 0
    ai_skip = 0
    ai_no_result = 0

    for r in research_rows:
        for k, v in (r.get("raw_fires") or {}).items():
            raw[k] += v
        for k, v in (r.get("dedup_winners") or {}).items():
            dedup[k] += v
        no_signals += r.get("no_active_signals", 0)
        tied += r.get("tied_votes_dropped", 0)
        final += r.get("final_recommendations", 0)
        ai_eligible  += r.get("ai_eligible", 0)
        ai_cooled    += r.get("ai_cooled_down", 0)
        ai_analyzed  += r.get("ai_analyzed", 0)
        ai_agree     += r.get("ai_agree", 0)
        ai_disagree  += r.get("ai_disagree", 0)
        ai_skip      += r.get("ai_skip", 0)
        ai_no_result += r.get("ai_no_result", 0)

    print(f"\n  Runs in window:           {n_runs}")
    print(f"  Markets fetched (total):  {total_markets}")
    print(f"  Markets with 0 signals:   {no_signals} ({100*no_signals/total_markets:.1f}%)" if total_markets else "")
    print(f"  Dropped at tie step:      {tied}")
    print(f"  Final recommendations:    {final}")
    print()
    print("  Raw fires per strategy (across all runs):")
    for strat in ("probability_direction", "mean_reversion", "probability_bias",
                  "creator_disagreement", "thin_market"):
        n = raw[strat]
        rate = 100 * n / total_markets if total_markets else 0
        bar = "#" * min(40, n // max(1, n_runs))
        marker = "  ← SILENT" if n == 0 else ""
        print(f"    {strat:<24} {n:>5}  ({rate:4.1f}% of markets) {bar}{marker}")

    print()
    print("  Dedup winners per family:")
    for fam in ("momentum", "contrarian", "fundamental"):
        n = dedup[fam]
        print(f"    {fam:<14} {n:>5}")

    print()
    print("  AI flow totals:")
    print(f"    eligible    {ai_eligible:>4}   cooled    {ai_cooled:>4}   analyzed  {ai_analyzed:>4}")
    print(f"    agree       {ai_agree:>4}   disagree  {ai_disagree:>4}")
    print(f"    skip        {ai_skip:>4}   no-result {ai_no_result:>4}")
    if ai_eligible > 0:
        cooled_pct = 100 * ai_cooled / ai_eligible
        print(f"    cooldown rate:  {cooled_pct:.0f}% of eligible recs blocked by AI cooldown")


# ── Section 2: category coverage of the live market pool ────────────────────

def report_category_mix(research_rows: list[dict]) -> None:
    print()
    print("=" * 72)
    print("  2. Category mix of final recommendations")
    print("=" * 72)
    if not research_rows:
        print("  No data.")
        return

    cats = Counter()
    mixes = Counter()
    for r in research_rows:
        for k, v in (r.get("final_by_category") or {}).items():
            cats[k] += v
        for k, v in (r.get("final_by_strategy_mix") or {}).items():
            mixes[k] += v

    total = sum(cats.values())
    if total == 0:
        print("  No final recommendations recorded.")
        return

    print()
    print(f"  Across {len(research_rows)} runs, {total} total final recs:")
    for cat, n in sorted(cats.items(), key=lambda x: -x[1]):
        print(f"    {cat:<14} {n:>4}  ({100*n/total:5.1f}%)")

    print()
    print("  Strategy mixes of final recs:")
    for mix, n in sorted(mixes.items(), key=lambda x: -x[1]):
        print(f"    {mix:<40} {n:>4}")


# ── Section 3: calibration table vs bias threshold ───────────────────────────

def report_bias_cells(calibration_table: Optional[dict],
                      proposed_threshold: float,
                      min_cell_samples: int = 15) -> None:
    print()
    print("=" * 72)
    print(f"  3. Calibration cells vs bias threshold")
    print("=" * 72)

    if not calibration_table:
        print("  No calibration_table.json found.")
        return

    by_cat    = calibration_table.get("by_category", [])
    by_bucket = calibration_table.get("by_category_bucket", [])
    current_threshold = 0.04   # _MIN_BIAS_TO_TRADE in strategies.py

    print()
    print(f"  Current gate:  {current_threshold:.4f}")
    print(f"  Proposed gate: {proposed_threshold:.4f}")
    print()
    print("  Category-level bias (reliable = sample_size >= min_cell_samples):")
    print(f"    {'category':<14} {'bias':>8}  {'n':>5}  {'@ current':>11}  {'@ proposed':>11}")
    print(f"    {'-'*14} {'-'*8}  {'-'*5}  {'-'*11}  {'-'*11}")
    for row in sorted(by_cat, key=lambda r: -r.get("bias", 0)):
        cat  = row.get("category", "?")
        bias = row.get("bias", 0)
        n    = row.get("sample_size", 0)
        fires_current  = "YES" if bias >= current_threshold else "no"
        fires_proposed = "YES" if bias >= proposed_threshold else "no"
        changed = " ← NEW" if fires_proposed == "YES" and fires_current == "no" else ""
        print(f"    {cat:<14} {bias:+.4f}  {n:>5}  {fires_current:>11}  {fires_proposed:>11}{changed}")

    print()
    print("  Per (category, bucket) cells ≥ proposed threshold:")
    print(f"    (only showing reliable cells with sample_size >= {min_cell_samples})")
    print()

    # Filter to reliable cells that cross proposed threshold
    passing_now = 0
    passing_proposed = 0
    for row in by_bucket:
        if not row.get("reliable"):
            continue
        if (row.get("sample_size") or 0) < min_cell_samples:
            continue
        bias = row.get("bias", 0)
        if bias >= current_threshold:
            passing_now += 1
        if bias >= proposed_threshold:
            passing_proposed += 1

    print(f"    Cells firing under current ({current_threshold}):  {passing_now}")
    print(f"    Cells firing under proposed ({proposed_threshold}): {passing_proposed}")

    if passing_proposed > passing_now:
        print()
        print("  New cells unlocked at proposed threshold:")
        for row in sorted(by_bucket, key=lambda r: -r.get("bias", 0)):
            if not row.get("reliable"):
                continue
            if (row.get("sample_size") or 0) < min_cell_samples:
                continue
            bias = row.get("bias", 0)
            if proposed_threshold <= bias < current_threshold:
                cat = row.get("category", "?")
                lo  = row.get("bucket_low", 0)
                hi  = row.get("bucket_high", 0)
                n   = row.get("sample_size", 0)
                print(f"    {cat:<14} {lo:.0%}-{hi:.0%} bias={bias:+.4f} n={n}")


# ── Section 4: live market_research snapshot ─────────────────────────────────

def report_current_research(mr: Optional[dict]) -> None:
    print()
    print("=" * 72)
    print("  4. Current market_research.json snapshot")
    print("=" * 72)

    if not mr:
        print("  No market_research.json found.")
        return

    latest = mr.get("latest", {})
    schema = latest.get("schema_version")
    ts     = latest.get("timestamp", "?")
    recs   = latest.get("recommendations", [])

    print(f"\n  schema_version: {schema}")
    print(f"  timestamp:      {ts}")
    print(f"  recommendations: {len(recs)}")

    if not recs:
        return

    # Count AI-field coverage
    has_ai_field = sum(1 for r in recs if "ai_was_candidate" in r)
    ai_ran = sum(1 for r in recs if r.get("ai_was_candidate"))
    print(f"  recs with ai_was_candidate field: {has_ai_field}/{len(recs)}")
    print(f"  recs where AI actually ran:       {ai_ran}/{len(recs)}")

    # Per-strategy breakdown
    by_strat = Counter()
    for r in recs:
        for s in r.get("strategies") or []:
            if s != "ai_analysis":
                by_strat[s] += 1
    print("  strategies in recommendations:")
    for strat, n in by_strat.most_common():
        print(f"    {strat:<24} {n}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Op5 strategy coverage diagnostic")
    parser.add_argument("--root", default=ROOT_DIR,
                        help="Workspace root (default: repo root)")
    parser.add_argument("--hours", type=int, default=24,
                        help="How many hours of research counter history to analyse")
    parser.add_argument("--proposed-bias-threshold", type=float, default=0.025,
                        help="Proposed _MIN_BIAS_TO_TRADE to evaluate")
    args = parser.parse_args()

    root = args.root
    print()
    print("#" * 72)
    print("#  Op5 strategy coverage diagnostic")
    print("#" * 72)
    print(f"  root:                 {root}")
    print(f"  hours window:         {args.hours}")
    print(f"  proposed bias gate:   {args.proposed_bias_threshold}")

    research_rows = _load_jsonl(os.path.join(root, "data", "research_counters.jsonl"))
    recent = _filter_recent(research_rows, args.hours)

    report_strategy_fires(recent)
    report_category_mix(recent)

    calibration = _load_json(os.path.join(root, "data", "calibration_table.json"))
    report_bias_cells(calibration, args.proposed_bias_threshold)

    mr = _load_json(os.path.join(root, "market_research.json"))
    report_current_research(mr)

    print()
    print("# End of report")
    print()


if __name__ == "__main__":
    main()
