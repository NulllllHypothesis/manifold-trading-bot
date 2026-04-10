#!/usr/bin/env python3
"""
Calibration analysis — measures crowd bias by probability bucket.

What this does:
  Reads data/calibration.db (built by harvest_resolved.py), groups resolved
  markets into 10-percentage-point probability buckets, and measures whether
  the crowd was over- or under-confident at each level.

Output → data/calibration_table.json
  A list of 10 buckets, each containing:
    bucket_low     — lower bound of the probability bucket (e.g. 0.40)
    bucket_high    — upper bound (e.g. 0.50)
    crowd_midpoint — center of the bucket (e.g. 0.45)
    actual_yes_rate— fraction of markets in this bucket that resolved YES
    sample_size    — number of markets in this bucket
    bias           — crowd_midpoint minus actual_yes_rate
                     > 0 means crowd OVER-estimates YES probability
                     < 0 means crowd UNDER-estimates YES probability

  Also emits by_category and by_category_bucket for query-specific AI priors.

Why this matters:
  When our AI sees a market at 65%, it should know that historically markets
  in the 60-70% range only resolve YES ~47% of the time. The AI can then
  factor in that 18pp crowd overconfidence when making its probability estimate.

Usage:
  python3 scripts/analyze_calibration.py
  python3 scripts/analyze_calibration.py --min-sample 10   # require 10+ markets per bucket
  python3 scripts/analyze_calibration.py --no-reclassify   # skip keyword reclassification
"""

import sys
import os
import sqlite3
import json
import argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from manifold_bot.strategies import _infer_market_category

DB_DIR  = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
DB_PATH = os.path.join(DB_DIR, "calibration.db")
OUT_PATH = os.path.join(DB_DIR, "calibration_table.json")

# Minimum samples in a (category, bucket) cell to include in by_category_bucket.
# Set higher than global min_sample because cells are sparse.
_MIN_CELL_SAMPLES = 15


def reclassify_categories() -> int:
    """
    Re-run _infer_market_category() over all resolved_markets rows and update
    the category column in place.

    This ensures historical calibration data uses the current keyword list —
    important because the keyword list has been expanded over time, and old
    rows were classified with a narrower set.  Running this after every
    harvest ensures the calibration table reflects current classification logic.

    Returns the number of rows updated.
    """
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT market_id, question FROM resolved_markets").fetchall()
    updated = 0
    for market_id, question in rows:
        new_cat = _infer_market_category(question or '')
        conn.execute(
            "UPDATE resolved_markets SET category = ? WHERE market_id = ?",
            (new_cat, market_id),
        )
        updated += 1
    conn.commit()
    conn.close()
    return updated


def analyze(min_sample: int = 5) -> list[dict]:
    """
    Compute calibration table from resolved_markets.

    Each bucket covers a 10pp range: [0.0-0.10), [0.10-0.20), ..., [0.90-1.00].
    We use CAST(probability_close * 10 AS INT) to assign each market to a bucket.
    """
    if not os.path.exists(DB_PATH):
        print(f"ERROR: {DB_PATH} not found. Run scripts/harvest_resolved.py first.")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    total_rows = conn.execute("SELECT COUNT(*) FROM resolved_markets").fetchone()[0]
    print(f"Loaded {total_rows} resolved markets from {DB_PATH}\n")

    # One query computes all buckets at once
    rows = conn.execute("""
        SELECT
            CAST(probability_close * 10 AS INT) AS bucket_idx,   -- 0..9
            COUNT(*) AS total,
            SUM(CASE WHEN outcome = 'YES' THEN 1 ELSE 0 END) AS yes_count
        FROM resolved_markets
        WHERE probability_close IS NOT NULL
        GROUP BY bucket_idx
        ORDER BY bucket_idx
    """).fetchall()
    conn.close()

    buckets = []
    print(f"{'Bucket':12} {'N':>6} {'Crowd %':>9} {'Actual %':>10} {'Bias':>8}  Status")
    print("─" * 65)

    for bucket_idx, total, yes_count in rows:
        # Guard against bad data (probability > 1.0 → bucket_idx > 9)
        if bucket_idx > 9 or bucket_idx < 0:
            continue

        bucket_low  = bucket_idx * 0.10
        bucket_high = bucket_low + 0.10
        crowd_midpoint = bucket_low + 0.05  # center of bucket

        actual_yes_rate = yes_count / total if total > 0 else None
        bias = (crowd_midpoint - actual_yes_rate) if actual_yes_rate is not None else None

        status = ""
        if total < min_sample:
            status = f"⚠️  only {total} samples — unreliable"
        elif bias is not None and abs(bias) > 0.10:
            direction = "overestimates YES" if bias > 0 else "underestimates YES"
            status = f"🔴 crowd {direction} by {abs(bias)*100:.0f}pp"
        elif bias is not None and abs(bias) > 0.05:
            direction = "overestimates YES" if bias > 0 else "underestimates YES"
            status = f"🟡 crowd {direction} by {abs(bias)*100:.0f}pp"
        else:
            status = "✅ well calibrated"

        print(
            f"{bucket_low*100:.0f}%-{bucket_high*100:.0f}%    "
            f"{total:>6}   "
            f"{crowd_midpoint*100:>6.0f}%   "
            f"{(actual_yes_rate or 0)*100:>7.0f}%   "
            f"{(bias or 0)*100:>+6.0f}pp  {status}"
        )

        buckets.append({
            "bucket_low":      round(bucket_low, 2),
            "bucket_high":     round(bucket_high, 2),
            "crowd_midpoint":  round(crowd_midpoint, 2),
            "actual_yes_rate": round(actual_yes_rate, 4) if actual_yes_rate is not None else None,
            "sample_size":     total,
            "bias":            round(bias, 4) if bias is not None else None,
            # reliable = enough samples for this bucket to be trustworthy
            "reliable":        total >= min_sample,
        })

    return buckets


def analyze_by_category(min_sample: int = 5) -> list[dict]:
    """
    Compute actual YES rate broken down by topic category.

    This reveals whether calibration bias is category-specific — e.g. the crowd
    might be well-calibrated on politics but heavily overconfident on crypto.
    That means the AI should apply stronger corrections to crypto markets.
    """
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT
            COALESCE(category, 'other') AS cat,
            COUNT(*) AS total,
            SUM(CASE WHEN outcome = 'YES' THEN 1 ELSE 0 END) AS yes_count,
            AVG(probability_close) AS avg_prob
        FROM resolved_markets
        WHERE probability_close IS NOT NULL
        GROUP BY cat
        ORDER BY total DESC
    """).fetchall()
    conn.close()

    print("\n" + "─" * 65)
    print("CALIBRATION BY CATEGORY")
    print("─" * 65)
    print(f"{'Category':12} {'N':>6} {'Avg crowd':>10} {'Actual YES':>11} {'Bias':>8}  Status")
    print("─" * 65)

    categories = []
    for cat, total, yes_count, avg_prob in rows:
        actual_rate = yes_count / total if total > 0 else None
        bias = (avg_prob - actual_rate) if (actual_rate is not None and avg_prob is not None) else None

        status = ""
        if total < min_sample:
            status = f"⚠️  only {total} samples"
        elif bias is not None and abs(bias) > 0.10:
            direction = "over" if bias > 0 else "under"
            status = f"🔴 crowd {direction}estimates YES by {abs(bias)*100:.0f}pp"
        elif bias is not None and abs(bias) > 0.05:
            direction = "over" if bias > 0 else "under"
            status = f"🟡 {direction}estimate {abs(bias)*100:.0f}pp"
        else:
            status = "✅ calibrated"

        print(
            f"{cat:12} {total:>6}   "
            f"{(avg_prob or 0)*100:>7.0f}%   "
            f"{(actual_rate or 0)*100:>8.0f}%   "
            f"{(bias or 0)*100:>+6.0f}pp  {status}"
        )

        categories.append({
            "category":      cat,
            "sample_size":   total,
            "avg_crowd_prob": round(avg_prob, 4) if avg_prob is not None else None,
            "actual_yes_rate": round(actual_rate, 4) if actual_rate is not None else None,
            "bias":           round(bias, 4) if bias is not None else None,
            "reliable":       total >= min_sample,
        })

    return categories


def analyze_by_category_bucket(min_cell: int = _MIN_CELL_SAMPLES) -> list[dict]:
    """
    Compute calibration stats for every (category, 10pp bucket) cell.

    Used by ai_analyzer.py to build query-specific calibration priors:
    instead of telling the AI "globally the 60-70% bucket is biased +18pp",
    we tell it "politics markets at 60-70% resolved YES 54% of the time (n=142)".

    Only cells with >= min_cell samples are marked reliable and used by the AI.
    Below that threshold the AI falls back to the global bucket stats.

    Schema per entry:
      category         — topic category
      bucket_low/high  — 10pp probability range
      crowd_midpoint   — centre of the bucket
      actual_yes_rate  — fraction that resolved YES
      sample_size      — n in this cell
      bias             — crowd_midpoint − actual_yes_rate
      reliable         — sample_size >= min_cell
    """
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT
            COALESCE(category, 'other') AS cat,
            CAST(probability_close * 10 AS INT) AS bucket_idx,
            COUNT(*) AS total,
            SUM(CASE WHEN outcome = 'YES' THEN 1 ELSE 0 END) AS yes_count
        FROM resolved_markets
        WHERE probability_close IS NOT NULL AND outcome IS NOT NULL
        GROUP BY cat, bucket_idx
        ORDER BY cat, bucket_idx
    """).fetchall()
    conn.close()

    print("\n" + "─" * 65)
    print(f"CALIBRATION BY CATEGORY × BUCKET  (reliable = n ≥ {min_cell})")
    print("─" * 65)
    print(f"  {'Category':<12} {'Bucket':<10} {'n':>5}  {'YES%':>6}  {'Bias':>7}  Reliable")
    print("─" * 65)

    cells = []
    for cat, bucket_idx, total, yes_count in rows:
        if bucket_idx > 9 or bucket_idx < 0:
            continue
        bucket_low  = bucket_idx * 0.10
        bucket_high = bucket_low + 0.10
        crowd_mid   = bucket_low + 0.05

        actual_yes_rate = yes_count / total if total > 0 else None
        bias = round(crowd_mid - actual_yes_rate, 4) if actual_yes_rate is not None else None
        reliable = total >= min_cell

        if reliable or total >= 5:   # print even borderline cells for visibility
            bias_str = f"{(bias or 0)*100:+.1f}pp"
            print(
                f"  {cat:<12} {int(bucket_low*100)}-{int(bucket_high*100)}%    "
                f"{total:>5}  {(actual_yes_rate or 0)*100:>5.1f}%  {bias_str:>7}  "
                f"{'✓' if reliable else '–'}"
            )

        cells.append({
            "category":        cat,
            "bucket_low":      round(bucket_low, 2),
            "bucket_high":     round(bucket_high, 2),
            "crowd_midpoint":  round(crowd_mid, 2),
            "actual_yes_rate": round(actual_yes_rate, 4) if actual_yes_rate is not None else None,
            "sample_size":     total,
            "bias":            bias,
            "reliable":        reliable,
        })

    return cells


def save_table(buckets: list[dict], categories: list[dict], cat_buckets: list[dict]):
    """Save the calibration table to JSON for use by ai_analyzer.py."""
    os.makedirs(DB_DIR, exist_ok=True)
    output = {
        "generated_at":  datetime.utcnow().isoformat() + "Z",
        "generated_at_unix": int(datetime.utcnow().timestamp()),
        "source_db":     DB_PATH,
        "min_cell_samples": _MIN_CELL_SAMPLES,
        "description":   (
            "Crowd calibration table. 'buckets' = global 10pp bucket stats. "
            "'by_category' = per-category summary. "
            "'by_category_bucket' = per-(category, bucket) stats for query-specific AI priors. "
            "bias > 0 means crowd overestimates YES probability."
        ),
        "buckets": buckets,
        "by_category": categories,
        "by_category_bucket": cat_buckets,
    }
    with open(OUT_PATH, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nCalibration table saved → {OUT_PATH}")
    print(f"(Commit this file — it's small JSON, not binary, and useful to track over time.)")


def main():
    parser = argparse.ArgumentParser(description="Analyze crowd calibration from resolved markets")
    parser.add_argument("--min-sample", type=int, default=5,
                        help="Minimum markets per bucket to mark as reliable (default: 5)")
    parser.add_argument("--no-reclassify", action="store_true",
                        help="Skip re-running _infer_market_category on existing rows")
    args = parser.parse_args()

    print("=" * 65)
    print("CALIBRATION ANALYSIS")
    print("=" * 65 + "\n")

    if not args.no_reclassify:
        updated = reclassify_categories()
        print(f"Reclassified {updated} rows with current keyword list.\n")

    buckets = analyze(min_sample=args.min_sample)
    categories = analyze_by_category(min_sample=args.min_sample)
    cat_buckets = analyze_by_category_bucket()
    save_table(buckets, categories, cat_buckets)

    # Summary stats
    reliable = [b for b in buckets if b["reliable"] and b["bias"] is not None]
    if reliable:
        avg_abs_bias = sum(abs(b["bias"]) for b in reliable) / len(reliable)
        max_bias = max(reliable, key=lambda b: abs(b["bias"]))
        print(f"\nSummary:")
        print(f"  Average absolute bias: {avg_abs_bias*100:.1f}pp")
        print(f"  Worst bucket: {max_bias['bucket_low']*100:.0f}-{max_bias['bucket_high']*100:.0f}% "
              f"→ crowd says {max_bias['crowd_midpoint']*100:.0f}%, "
              f"actual {(max_bias['actual_yes_rate'] or 0)*100:.0f}% "
              f"(bias {max_bias['bias']*100:+.0f}pp)")

        reliable_cells = [c for c in cat_buckets if c["reliable"]]
        print(f"  Reliable (category, bucket) cells: {len(reliable_cells)} "
              f"(n ≥ {_MIN_CELL_SAMPLES})")


if __name__ == "__main__":
    main()
