#!/usr/bin/env python3
"""
M4 — Dataset formatter: market_snapshots.db + resolved_markets → training JSONL.

What this produces
------------------
data/training_dataset.jsonl   — one JSON object per snapshot row, used by:
  - M5 eval harness  (flat features, baseline Brier score computation)
  - M6 fine-tuning   (has prompt/completion fields for LoRA)

Each row represents a market at a specific decision time (T-7, T-14, or T-30
days before close) where we know the final outcome. This is the dataset we train
and evaluate on — NOT the close-time resolved_markets table that simulate_strategy_weights.py
uses. Decision-time snapshots are what the bot actually observes when trading.

Data sources (joined by market_id)
-----------------------------------
1. market_snapshots.db  source='reconstructed'
   - 127 rows at T-7/14/30 with outcome already stored (from M3)
   - Fields: market_id, question, probability, bettors, liquidity, days_before_close, outcome

2. market_snapshots.db  source='live'
   - 3103 rows without outcome (snapshots taken by the hourly logger)
   - outcome JOIN'd from calibration.db resolved_markets where available
   - Gives ~additional rows for markets that have since resolved

3. calibration.db resolved_markets
   - 2016 rows, used to: (a) supply outcomes for live snapshots, (b) supply category

Row format
----------
{
  "market_id":        str,
  "question":         str,
  "category":         str,   -- from resolved_markets or inferred from question
  "crowd_probability": float, -- probability at snapshot time (decision time)
  "days_before_close": int,   -- T-7, T-14, or T-30
  "unique_bettors":   int | null,
  "liquidity":        float | null,
  "snapshot_source":  "reconstructed" | "live",
  "outcome":          "YES" | "NO",
  "true_probability": float,  -- 1.0 for YES, 0.0 for NO
  "split":            "train" | "val" | "test",  -- deterministic 80/10/10 by market_id
  "prompt":           str,    -- LLM fine-tuning input  (M6)
  "completion":       str,    -- LLM fine-tuning target (M6)
}

Split assignment
----------------
Deterministic by market_id hash — all snapshots for the same market go to the
same split to prevent T-30 leaking information about T-7 of the same market.
  hash(market_id) % 10 in [0]       → test  (10%)
  hash(market_id) % 10 in [1]       → val   (10%)
  hash(market_id) % 10 in [2..9]    → train (80%)

Usage
-----
  python3 scripts/build_training_dataset.py
  python3 scripts/build_training_dataset.py --dry-run   # print stats, no file write
  python3 scripts/build_training_dataset.py --min-bettors 5
"""

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

from manifold_bot.strategies import _infer_market_category

SNAPSHOTS_DB   = os.path.join(ROOT_DIR, "data", "market_snapshots.db")
CALIBRATION_DB = os.path.join(ROOT_DIR, "data", "calibration.db")
OUTPUT_PATH    = os.path.join(ROOT_DIR, "data", "training_dataset.jsonl")


# ── Split assignment ──────────────────────────────────────────────────────────

def _split(market_id: str) -> str:
    """Deterministic 80/10/10 split by market_id hash."""
    bucket = int(hashlib.md5(market_id.encode()).hexdigest(), 16) % 10
    if bucket == 0:
        return "test"
    if bucket == 1:
        return "val"
    return "train"


# ── Prompt builder ────────────────────────────────────────────────────────────

_CATEGORY_LABELS = {
    "politics":  "Politics / geopolitics",
    "ai_tech":   "AI / technology",
    "sports":    "Sports",
    "economics": "Economics / finance",
    "crypto":    "Crypto / blockchain",
    "science":   "Science / health",
    "other":     "General",
}


def _build_prompt(question: str, category: str, crowd_prob: float,
                  days_before_close: int, unique_bettors: int | None) -> str:
    cat_label = _CATEGORY_LABELS.get(category, "General")
    bettors_line = f"Unique bettors: {unique_bettors}" if unique_bettors else ""
    parts = [
        "Predict the probability that this prediction market resolves YES.\n",
        f"Question: {question}",
        f"Category: {cat_label}",
        f"Crowd probability: {crowd_prob * 100:.1f}%",
        f"Days until close: {days_before_close}",
    ]
    if bettors_line:
        parts.append(bettors_line)
    parts.append("\nEstimate the true probability (0.00 to 1.00):")
    return "\n".join(parts)


def _build_completion(true_probability: float) -> str:
    return f"{true_probability:.2f}"


# ── Data loading ──────────────────────────────────────────────────────────────

def _load_category_map(calibration_db: str) -> dict[str, str]:
    """Load market_id → category from resolved_markets."""
    if not os.path.exists(calibration_db):
        return {}
    conn = sqlite3.connect(calibration_db)
    rows = conn.execute("SELECT market_id, category FROM resolved_markets").fetchall()
    conn.close()
    return {r[0]: r[1] for r in rows if r[1]}


def _load_outcome_map(calibration_db: str) -> dict[str, str]:
    """Load market_id → outcome ('YES'/'NO') from resolved_markets."""
    if not os.path.exists(calibration_db):
        return {}
    conn = sqlite3.connect(calibration_db)
    rows = conn.execute(
        "SELECT market_id, outcome FROM resolved_markets WHERE outcome IN ('YES','NO')"
    ).fetchall()
    conn.close()
    return {r[0]: r[1] for r in rows}


def load_examples(
    snapshots_db: str = SNAPSHOTS_DB,
    calibration_db: str = CALIBRATION_DB,
    min_bettors: int = 0,
) -> list[dict]:
    """
    Load and join all snapshot rows that have a known outcome.

    Returns list of dicts ready to serialise.
    """
    if not os.path.exists(snapshots_db):
        print(f"ERROR: {snapshots_db} not found. Run reconstruct_snapshots.py first.")
        return []

    cat_map     = _load_category_map(calibration_db)
    outcome_map = _load_outcome_map(calibration_db)

    conn = sqlite3.connect(snapshots_db)
    rows = conn.execute("""
        SELECT market_id, question, probability, bettors, liquidity,
               days_before_close, source, outcome
        FROM market_snapshots
        WHERE probability IS NOT NULL
    """).fetchall()
    conn.close()

    examples = []
    for (market_id, question, probability, bettors,
         liquidity, days_before_close, source, stored_outcome) in rows:

        # Resolve outcome: stored (reconstructed) or joined from calibration.db (live)
        outcome = stored_outcome if stored_outcome in ("YES", "NO") else outcome_map.get(market_id)
        if not outcome:
            continue  # market hasn't resolved — skip

        if min_bettors and bettors is not None and bettors < min_bettors:
            continue

        # Skip near-close snapshots from the live logger (days_before_close=0 or None
        # for live rows means the market was already at/near resolution — same convergence
        # problem as using probability_close. Keep only explicit decision-time windows.
        if source == "live" and (days_before_close is None or days_before_close < 1):
            continue

        category = cat_map.get(market_id) or _infer_market_category(question or "")
        true_prob = 1.0 if outcome == "YES" else 0.0
        split     = _split(market_id)

        prompt     = _build_prompt(question or "", category, probability,
                                   days_before_close or 7, bettors)
        completion = _build_completion(true_prob)

        examples.append({
            "market_id":        market_id,
            "question":         question or "",
            "category":         category,
            "crowd_probability": round(probability, 6),
            "days_before_close": days_before_close,
            "unique_bettors":   bettors,
            "liquidity":        liquidity,
            "snapshot_source":  source,
            "outcome":          outcome,
            "true_probability": true_prob,
            "split":            split,
            "prompt":           prompt,
            "completion":       completion,
        })

    return examples


# ── Stats ─────────────────────────────────────────────────────────────────────

def _print_stats(examples: list[dict]) -> None:
    total = len(examples)
    if not total:
        print("  No examples.")
        return

    by_split:  dict[str, int] = {}
    by_source: dict[str, int] = {}
    by_cat:    dict[str, int] = {}
    by_window: dict[int, int] = {}
    yes_count = 0

    for ex in examples:
        by_split[ex["split"]]               = by_split.get(ex["split"], 0) + 1
        by_source[ex["snapshot_source"]]    = by_source.get(ex["snapshot_source"], 0) + 1
        by_cat[ex["category"]]              = by_cat.get(ex["category"], 0) + 1
        w = ex.get("days_before_close") or 0
        by_window[w]                        = by_window.get(w, 0) + 1
        if ex["outcome"] == "YES":
            yes_count += 1

    print(f"  Total examples : {total}")
    print(f"  YES rate       : {yes_count/total*100:.1f}%")
    print()
    print("  Split breakdown:")
    for s in ("train", "val", "test"):
        n = by_split.get(s, 0)
        print(f"    {s:<6} {n:>5}  ({n/total*100:.0f}%)")
    print()
    print("  Source:")
    for src, n in sorted(by_source.items()):
        print(f"    {src:<15} {n}")
    print()
    print("  Days-before-close:")
    for w, n in sorted(by_window.items()):
        print(f"    T-{w:<3}  {n}")
    print()
    print("  Category:")
    for cat, n in sorted(by_cat.items(), key=lambda x: -x[1]):
        print(f"    {cat:<15} {n}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main(dry_run: bool = False, min_bettors: int = 0) -> None:
    print("=" * 65)
    print("M4 — BUILD TRAINING DATASET")
    print("=" * 65)
    print(f"\nSources:")
    print(f"  Snapshots DB  : {SNAPSHOTS_DB}")
    print(f"  Calibration DB: {CALIBRATION_DB}")
    print(f"  Output        : {OUTPUT_PATH}")

    examples = load_examples(min_bettors=min_bettors)
    if not examples:
        print("\nNo examples produced. Check that market_snapshots.db exists and")
        print("reconstruct_snapshots.py has been run.")
        return

    print(f"\nDataset stats:")
    _print_stats(examples)

    # Show a sample prompt/completion pair
    train_ex = [e for e in examples if e["split"] == "train"]
    if train_ex:
        ex = train_ex[0]
        print("Sample prompt/completion (first train example):")
        print(f"  PROMPT:\n    " + ex["prompt"].replace("\n", "\n    "))
        print(f"  COMPLETION: {ex['completion']}")
        print()

    if dry_run:
        print("[dry-run] Not writing to disk.")
        return

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        for ex in examples:
            f.write(json.dumps(ex) + "\n")

    size_kb = os.path.getsize(OUTPUT_PATH) / 1024
    print(f"Written: {OUTPUT_PATH}  ({len(examples)} rows, {size_kb:.1f} KB)")
    print(f"\nNext: python3 scripts/run_eval_harness.py  (M5)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="M4 — Build training dataset")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print stats without writing output file")
    parser.add_argument("--min-bettors", type=int, default=0,
                        help="Minimum unique bettors per snapshot (default: 0)")
    args = parser.parse_args()
    main(dry_run=args.dry_run, min_bettors=args.min_bettors)
