#!/usr/bin/env python3
"""
Category v2 — one-off reclassification / backfill for existing data.

Problem
-------
The original category classifier matched only 6 keyword groups (politics, sports,
crypto, ai_tech, economics, science). On Manifold, ~70% of markets don't contain
any of those keywords and fell through to `other`. That made everything keyed
on category — Kelly sizing, per-category caps, query-specific calibration priors,
M5 per-category eval breakdowns — significantly less useful than they look.

v2 expands the taxonomy with gaming / entertainment / business so `other` gets
much smaller. This script reapplies the current `_infer_market_category()` to
every row that was labeled under the old taxonomy and writes the new label back.

What it updates
---------------
1. data/calibration.db :: resolved_markets.category  (source of truth — keyed by market_id)
2. data/calibration.db :: bet_outcomes.category      (joined on market_id from resolved_markets)

What it does NOT update
-----------------------
- data/market_snapshots.db — has no category column; category is re-inferred
  at dataset-build time by build_training_dataset.py.
- data/training_dataset.jsonl — rebuild by running build_training_dataset.py
  after this script.
- data/eval_report.json — regenerate by running run_eval_harness.py after
  the dataset has been rebuilt.
- data/strategy_weights.json — will get new per-category stats next Monday
  when compute_strategy_weights.py runs.

Usage
-----
  python3 scripts/reclassify_categories.py
  python3 scripts/reclassify_categories.py --dry-run    # report counts, no writes
  python3 scripts/reclassify_categories.py --verbose    # list category changes per row

After running
-------------
  python3 scripts/analyze_calibration.py        # rebuild calibration_table.json
  python3 scripts/build_training_dataset.py     # rebuild training_dataset.jsonl
  python3 scripts/run_eval_harness.py --split test   # verify M5 baseline moved sanely
"""

import argparse
import os
import sqlite3
import sys
from collections import Counter

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

from manifold_bot.strategies import _infer_market_category

CALIBRATION_DB = os.path.join(ROOT_DIR, "data", "calibration.db")


# ── resolved_markets ──────────────────────────────────────────────────────────

def reclassify_resolved_markets(conn: sqlite3.Connection,
                                 dry_run: bool = False,
                                 verbose: bool = False) -> dict:
    """
    Rerun _infer_market_category() on every row of resolved_markets and update
    the category column where it changed.

    Returns a dict:
      {
        "total":       N,
        "changed":     M,
        "before":      {category: count, ...},
        "after":       {category: count, ...},
        "transitions": {(old, new): count, ...},
      }
    """
    rows = conn.execute(
        "SELECT market_id, question, category FROM resolved_markets WHERE question IS NOT NULL"
    ).fetchall()

    before      = Counter()
    after       = Counter()
    transitions = Counter()
    changes     = []   # (market_id, old, new) — only for --verbose

    for market_id, question, old_cat in rows:
        old_cat = old_cat or "other"   # NULL counts as `other` for diffing
        new_cat = _infer_market_category(question)

        before[old_cat] += 1
        after[new_cat]  += 1

        if old_cat != new_cat:
            transitions[(old_cat, new_cat)] += 1
            changes.append((market_id, old_cat, new_cat))

    if not dry_run and changes:
        conn.executemany(
            "UPDATE resolved_markets SET category = ? WHERE market_id = ?",
            [(new_cat, market_id) for (market_id, _, new_cat) in changes],
        )

    if verbose and changes:
        print("\n  Sample transitions (first 20):")
        for market_id, old, new in changes[:20]:
            q = conn.execute(
                "SELECT question FROM resolved_markets WHERE market_id = ?",
                (market_id,),
            ).fetchone()
            q_text = (q[0] or "")[:70] if q else ""
            print(f"    {old:14} → {new:14}  {q_text}")

    return {
        "total":       len(rows),
        "changed":     len(changes),
        "before":      dict(before),
        "after":       dict(after),
        "transitions": dict(transitions),
    }


# ── bet_outcomes ──────────────────────────────────────────────────────────────

def reclassify_bet_outcomes(conn: sqlite3.Connection,
                             dry_run: bool = False) -> dict:
    """
    Re-label bet_outcomes rows by joining with resolved_markets on market_id
    to recover the question text, then rerunning _infer_market_category().

    Rows where the market_id is not present in resolved_markets (rare — e.g.
    markets that resolved but were never harvested) are left unchanged.
    """
    rows = conn.execute("""
        SELECT bo.id, bo.market_id, rm.question, bo.category
        FROM bet_outcomes bo
        LEFT JOIN resolved_markets rm ON rm.market_id = bo.market_id
    """).fetchall()

    before   = Counter()
    after    = Counter()
    changed  = 0
    no_question = 0

    updates = []
    for row_id, market_id, question, old_cat in rows:
        old_cat = old_cat or "other"
        before[old_cat] += 1

        if not question:
            # No question text available — can't reclassify. Leave as-is.
            no_question += 1
            after[old_cat] += 1
            continue

        new_cat = _infer_market_category(question)
        after[new_cat] += 1
        if old_cat != new_cat:
            changed += 1
            updates.append((new_cat, row_id))

    if not dry_run and updates:
        conn.executemany(
            "UPDATE bet_outcomes SET category = ? WHERE id = ?",
            updates,
        )

    return {
        "total":        len(rows),
        "changed":      changed,
        "no_question":  no_question,
        "before":       dict(before),
        "after":        dict(after),
    }


# ── Reporting ─────────────────────────────────────────────────────────────────

def _print_distribution(label: str, before: dict, after: dict) -> None:
    keys = sorted(set(before) | set(after))
    width = max(len(k) for k in keys)
    print(f"\n  {label}:")
    print(f"    {'category':<{width}}  {'before':>8}  {'after':>8}  {'delta':>8}")
    print(f"    {'-' * width}  {'-' * 8}  {'-' * 8}  {'-' * 8}")
    for k in keys:
        b = before.get(k, 0)
        a = after.get(k, 0)
        delta = a - b
        sign = "+" if delta > 0 else ""
        print(f"    {k:<{width}}  {b:>8}  {a:>8}  {sign}{delta:>7}")


def _print_transitions(transitions: dict) -> None:
    if not transitions:
        return
    print(f"\n  Top transitions (old → new):")
    sorted_t = sorted(transitions.items(), key=lambda x: -x[1])
    for (old, new), n in sorted_t[:15]:
        print(f"    {old:<14} → {new:<14}  {n:>5}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main(dry_run: bool = False, verbose: bool = False) -> None:
    print("=" * 72)
    print("  CATEGORY V2 — RECLASSIFY / BACKFILL")
    print("=" * 72)
    print(f"\n  DB         : {CALIBRATION_DB}")
    print(f"  Mode       : {'DRY RUN' if dry_run else 'WRITE'}")

    if not os.path.exists(CALIBRATION_DB):
        print(f"\nERROR: {CALIBRATION_DB} not found.")
        sys.exit(1)

    conn = sqlite3.connect(CALIBRATION_DB)
    try:
        # Step 1: resolved_markets
        print("\n[1/2] Reclassifying resolved_markets...")
        rm_stats = reclassify_resolved_markets(conn, dry_run=dry_run, verbose=verbose)
        print(f"  {rm_stats['changed']:>5} of {rm_stats['total']} rows relabeled")
        _print_distribution("resolved_markets", rm_stats["before"], rm_stats["after"])
        _print_transitions(rm_stats["transitions"])

        # Step 2: bet_outcomes  (only after resolved_markets, uses its questions)
        print("\n[2/2] Reclassifying bet_outcomes...")
        bo_stats = reclassify_bet_outcomes(conn, dry_run=dry_run)
        print(f"  {bo_stats['changed']:>5} of {bo_stats['total']} rows relabeled")
        if bo_stats["no_question"]:
            print(f"  {bo_stats['no_question']:>5} row(s) skipped — market_id not in resolved_markets")
        _print_distribution("bet_outcomes", bo_stats["before"], bo_stats["after"])

        if not dry_run:
            conn.commit()
            print("\n  ✓ Committed.")
        else:
            print("\n  (dry-run: no changes written)")

    finally:
        conn.close()

    print("\nNext steps:")
    print("  python3 scripts/analyze_calibration.py      # rebuild calibration_table.json")
    print("  python3 scripts/build_training_dataset.py   # rebuild training_dataset.jsonl")
    print("  python3 scripts/run_eval_harness.py --split test")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Category v2 reclassify + backfill")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change without writing")
    parser.add_argument("--verbose", action="store_true",
                        help="Print sample transitions per row")
    args = parser.parse_args()
    main(dry_run=args.dry_run, verbose=args.verbose)
