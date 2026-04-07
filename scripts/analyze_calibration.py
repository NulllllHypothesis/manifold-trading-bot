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

Why this matters:
  When our AI sees a market at 65%, it should know that historically markets
  in the 60-70% range only resolve YES ~47% of the time. The AI can then
  factor in that 18pp crowd overconfidence when making its probability estimate.

Usage:
  python3 scripts/analyze_calibration.py
  python3 scripts/analyze_calibration.py --min-sample 10   # require 10+ markets per bucket
"""

import sys
import os
import sqlite3
import json
import argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB_DIR  = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
DB_PATH = os.path.join(DB_DIR, "calibration.db")
OUT_PATH = os.path.join(DB_DIR, "calibration_table.json")


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


def save_table(buckets: list[dict]):
    """Save the calibration table to JSON for use by ai_analyzer.py."""
    os.makedirs(DB_DIR, exist_ok=True)
    output = {
        "generated_at":  datetime.utcnow().isoformat() + "Z",
        "source_db":     DB_PATH,
        "description":   (
            "Crowd calibration table. For each 10pp probability bucket, "
            "'actual_yes_rate' is the historical rate at which Manifold markets "
            "in that bucket resolved YES. 'bias' > 0 means crowd overestimates. "
            "Inject this into AI prompts to correct for systematic over/under-confidence."
        ),
        "buckets": buckets,
    }
    with open(OUT_PATH, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nCalibration table saved → {OUT_PATH}")
    print(f"(Commit this file — it's small JSON, not binary, and useful to track over time.)")


def main():
    parser = argparse.ArgumentParser(description="Analyze crowd calibration from resolved markets")
    parser.add_argument("--min-sample", type=int, default=5,
                        help="Minimum markets per bucket to mark as reliable (default: 5)")
    args = parser.parse_args()

    print("=" * 65)
    print("CALIBRATION ANALYSIS")
    print("=" * 65 + "\n")

    buckets = analyze(min_sample=args.min_sample)
    save_table(buckets)

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


if __name__ == "__main__":
    main()
