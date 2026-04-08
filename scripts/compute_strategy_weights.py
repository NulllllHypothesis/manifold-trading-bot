#!/usr/bin/env python3
"""
Compute per-strategy reliability weights from resolved trade history.

Reads bet_outcomes (post_ev_fix era only), computes direction accuracy
per strategy, and writes data/strategy_weights.json.

Weight formula:
  - direction_accuracy >= 0.60  → weight = 1.0 + (accuracy - 0.50) * 2   (up to 1.20)
  - direction_accuracy 0.50-0.60 → weight = 1.0  (neutral — no penalty, no boost)
  - direction_accuracy < 0.50   → weight = max(0.5, accuracy / 0.50)      (down to 0.5)
  - sample count < MIN_SAMPLES_PER_STRATEGY → weight = 1.0  (not enough data)

Weights are consumed by auto_research.py to scale recommendation confidence.

Usage:
  python3 scripts/compute_strategy_weights.py           # update data/strategy_weights.json
  python3 scripts/compute_strategy_weights.py --dry-run # print without writing
"""

import sys
import os
import json
import sqlite3
import argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.weekly_ev_report import _fetch_outcomes, MIN_SAMPLES_PER_STRATEGY

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WEIGHTS_PATH = os.path.join(_ROOT, "data", "strategy_weights.json")

# Known strategies — always emitted so consumers can rely on all keys being present.
# Must match every strategy name that auto_research.py can write into the strategies list.
# thin_market is excluded: it's a confirmation-only filter that is never the sole trigger,
# and it is never stored as a standalone entry in bet_outcomes strategies lists.
_KNOWN_STRATEGIES = [
    "probability_direction",
    "mean_reversion",
    "volume_spike",
    "probability_bias",
    "creator_disagreement",
    "ai_analysis",
]

# Default weight used when a strategy has insufficient data or is unseen.
_DEFAULT_WEIGHT = 1.0


def _parse_strategies(raw) -> list[str]:
    """
    Parse the strategies field from bet_outcomes.

    paper_trader.py stores strategies as a JSON array string via json.dumps(),
    e.g. '["probability_direction", "mean_reversion"]'. Handle both that format
    and a plain comma-separated string for backwards compatibility.
    """
    if not raw:
        return []
    if isinstance(raw, list):
        return [s.strip() for s in raw if s.strip()]
    raw = raw.strip()
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
            return [s.strip() for s in parsed if s.strip()]
        except (json.JSONDecodeError, TypeError):
            pass
    return [s.strip() for s in raw.split(",") if s.strip()]


def _compute_weights(outcomes: list[dict]) -> dict:
    """
    Returns {strategy_name: weight} for all known strategies.
    """
    per_strategy: dict[str, dict] = {}  # {name: {total, correct}}

    for o in outcomes:
        strat_list = _parse_strategies(o.get("strategies"))
        if not strat_list:
            continue

        direction_correct = o.get("our_recommendation") == o.get("market_resolution")
        for s in strat_list:
            if s not in per_strategy:
                per_strategy[s] = {"total": 0, "correct": 0}
            per_strategy[s]["total"] += 1
            if direction_correct:
                per_strategy[s]["correct"] += 1

    weights = {}
    for strat in _KNOWN_STRATEGIES:
        stats = per_strategy.get(strat)
        if stats is None or stats["total"] < MIN_SAMPLES_PER_STRATEGY:
            weights[strat] = _DEFAULT_WEIGHT
            continue

        acc = stats["correct"] / stats["total"]
        if acc >= 0.60:
            w = 1.0 + (acc - 0.50) * 2   # 1.0 → 1.20 as acc goes 0.50 → 0.60+
            w = min(w, 1.20)
        elif acc >= 0.50:
            w = 1.0
        else:
            w = max(0.5, acc / 0.50)

        weights[strat] = round(w, 4)

    return weights


def main(dry_run: bool = False) -> None:
    outcomes = _fetch_outcomes(era="post_ev_fix")
    weights = _compute_weights(outcomes)

    result = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "sample_count": len(outcomes),
        "min_samples_threshold": MIN_SAMPLES_PER_STRATEGY,
        "weights": weights,
    }

    print(f"\nStrategy weights ({len(outcomes)} post-fix resolved trades):")
    for strat, w in weights.items():
        stats: dict = {}
        for o in outcomes:
            if strat in _parse_strategies(o.get("strategies")):
                if not stats:
                    stats = {"total": 0, "correct": 0}
                stats["total"] += 1
                if o.get("our_recommendation") == o.get("market_resolution"):
                    stats["correct"] += 1
        if stats:
            acc = stats["correct"] / stats["total"]
            note = f"{stats['correct']}/{stats['total']} correct ({acc*100:.0f}%)"
        else:
            note = f"<{MIN_SAMPLES_PER_STRATEGY} samples — default"
        print(f"  {strat:<28} weight={w:.4f}  [{note}]")

    if dry_run:
        print("\n[dry-run] Not writing to disk.")
        return

    with open(_WEIGHTS_PATH, "w") as f:
        json.dump(result, f, indent=2)
    print(f"\nWritten → {_WEIGHTS_PATH}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
