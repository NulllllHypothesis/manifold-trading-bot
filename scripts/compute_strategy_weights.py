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

from scripts.weekly_ev_report import _fetch_outcomes, MIN_SAMPLES_PER_STRATEGY, MIN_SAMPLES_PER_CATEGORY
from scripts.backtest_from_snapshots import MAX_BACKTEST_ONLY_WEIGHT

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WEIGHTS_PATH = os.path.join(_ROOT, "data", "strategy_weights.json")
_CATEGORY_ACCURACY_PATH = os.path.join(_ROOT, "data", "category_accuracy.json")

# Known strategies — always emitted so consumers can rely on all keys being present.
# Must match every strategy name that auto_research.py can write into the strategies list.
#
# Excluded:
#   volume_spike_priority — this is the filter family strategy; it is never added to
#     active_signals (no directional vote) and therefore never appears in bet_outcomes.
#     "volume_spike" was here as a stale entry from before the rename; removed in session 4.
#   thin_market — confirmation-only boost; never the sole trigger; strategy weight
#     is never looked up for it (it's not in active_signals).
_KNOWN_STRATEGIES = [
    "probability_direction",
    "mean_reversion",
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


def _compute_weights(outcomes: list[dict]) -> tuple[dict, dict]:
    """
    Returns (weights, per_strategy_samples) for all known strategies.

    weights:               {strategy_name: weight_scalar}
    per_strategy_samples:  {strategy_name: sample_count}  — written to
                           strategy_weights.json so merge_weights() in
                           backtest_from_snapshots.py can correctly prefer
                           live data once it clears MIN_SAMPLES_PER_STRATEGY.
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
    per_strategy_samples = {}
    for strat in _KNOWN_STRATEGIES:
        stats = per_strategy.get(strat)
        n = stats["total"] if stats else 0
        per_strategy_samples[strat] = n
        if stats is None or n < MIN_SAMPLES_PER_STRATEGY:
            weights[strat] = _DEFAULT_WEIGHT
            continue

        acc = stats["correct"] / n
        weights[strat] = round(_weight_from_accuracy(acc, n, MIN_SAMPLES_PER_STRATEGY), 4)

    return weights, per_strategy_samples


def _weight_from_accuracy(acc: float, n: int, min_samples: int) -> float:
    """Convert accuracy + sample count into a weight scalar (shared formula)."""
    if n < min_samples:
        return _DEFAULT_WEIGHT
    if acc >= 0.60:
        return min(1.0 + (acc - 0.50) * 2, 1.20)
    if acc >= 0.50:
        return 1.0
    return max(0.5, acc / 0.50)


def _compute_weights_by_category(outcomes: list[dict]) -> dict:
    """
    Returns {category: {strategy: weight}} for all (category, strategy) pairs
    with enough resolved trades.

    Fallback chain at runtime (in auto_research.py / auto_trader.py):
      by_category[category][strategy]  if n >= MIN_SAMPLES_PER_STRATEGY
        → global weights[strategy]     if n >= MIN_SAMPLES_PER_STRATEGY
          → 1.0 default

    Schema per entry stored in strategy_weights.json["by_category"]:
      { "strategy": weight, ... }  keyed by strategy name.
      Strategies below the sample threshold are omitted (caller uses global fallback).
    """
    # {category: {strategy: {total, correct}}}
    per_cat_strat: dict[str, dict[str, dict]] = {}

    for o in outcomes:
        cat = o.get("category") or "other"
        strat_list = _parse_strategies(o.get("strategies"))
        if not strat_list:
            continue
        direction_correct = o.get("our_recommendation") == o.get("market_resolution")
        if cat not in per_cat_strat:
            per_cat_strat[cat] = {}
        for s in strat_list:
            if s not in per_cat_strat[cat]:
                per_cat_strat[cat][s] = {"total": 0, "correct": 0}
            per_cat_strat[cat][s]["total"] += 1
            if direction_correct:
                per_cat_strat[cat][s]["correct"] += 1

    result: dict[str, dict] = {}
    for cat, strat_map in per_cat_strat.items():
        cat_weights: dict[str, dict] = {}
        for strat in _KNOWN_STRATEGIES:
            stats = strat_map.get(strat)
            if stats is None:
                continue
            n = stats["total"]
            if n < MIN_SAMPLES_PER_STRATEGY:
                continue   # omit; caller falls back to global
            acc = stats["correct"] / n
            w = round(_weight_from_accuracy(acc, n, MIN_SAMPLES_PER_STRATEGY), 4)
            cat_weights[strat] = {"weight": w, "accuracy": round(acc, 4), "n": n}
        if cat_weights:
            result[cat] = cat_weights

    return result


def _compute_category_accuracy(outcomes: list[dict]) -> dict:
    """
    Returns {category: {"accuracy": float, "sample_count": int}} for every
    category seen in bet_outcomes.  Categories below MIN_SAMPLES_PER_CATEGORY
    are included in the output (auto_trader.py checks the threshold itself).
    """
    per_cat: dict[str, dict] = {}

    for o in outcomes:
        cat = o.get("category") or "other"
        correct = o.get("our_recommendation") == o.get("market_resolution")
        if cat not in per_cat:
            per_cat[cat] = {"total": 0, "correct": 0}
        per_cat[cat]["total"] += 1
        if correct:
            per_cat[cat]["correct"] += 1

    result = {}
    for cat, stats in per_cat.items():
        total = stats["total"]
        acc = round(stats["correct"] / total, 4) if total > 0 else 0.0
        result[cat] = {"accuracy": acc, "sample_count": total}

    return result


def main(dry_run: bool = False) -> None:
    outcomes = _fetch_outcomes(era="post_ev_fix")
    weights, per_strategy_samples = _compute_weights(outcomes)
    by_category = _compute_weights_by_category(outcomes)
    cat_accuracy = _compute_category_accuracy(outcomes)

    # Preserve backtest-sourced weights for strategies where live data is
    # insufficient. Without this, the Monday 07:30 run would overwrite the
    # Sunday 03:30 backtest weights with 1.0 defaults every week, losing
    # the signal from 267 reconstructed snapshots.
    #
    # Merge rule: if a strategy has < MIN_SAMPLES_PER_STRATEGY live trades,
    # keep whatever weight exists in the current file (which may have been
    # set by backtest_from_snapshots.py on Sunday).
    existing: dict = {}
    if os.path.exists(_WEIGHTS_PATH):
        try:
            with open(_WEIGHTS_PATH) as f:
                existing = json.load(f)
        except Exception:
            existing = {}

    existing_weights = existing.get("weights", {})
    for strat in list(weights.keys()):
        n = per_strategy_samples.get(strat, 0)
        if n < MIN_SAMPLES_PER_STRATEGY:
            # Not enough live data — preserve the existing weight (which
            # might be from backtest, or a previous live computation, or
            # the default 1.0). Only live data with sufficient samples
            # should override.
            #
            # Asymmetric clamp (mirrors backtest_from_snapshots): without
            # current live confirmation we cap preserved weights at
            # MAX_BACKTEST_ONLY_WEIGHT (1.0). The backtest path now never
            # writes > 1.0, but stale rows from before that change (or a
            # legitimate-but-now-undersampled previous live boost) can still
            # carry > 1.0. Treat both the same: no boost without live data.
            prev = existing_weights.get(strat)
            if prev is not None:
                preserved = min(float(prev), MAX_BACKTEST_ONLY_WEIGHT)
                if preserved != weights[strat]:
                    if preserved < float(prev):
                        print(f"  {strat}: clamping preserved weight {prev} → "
                              f"{preserved} (live samples {n} < "
                              f"{MIN_SAMPLES_PER_STRATEGY}, no boost without "
                              f"live confirmation)")
                    else:
                        print(f"  {strat}: preserving existing weight {preserved} "
                              f"(live samples {n} < {MIN_SAMPLES_PER_STRATEGY})")
                    weights[strat] = preserved

    result = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "sample_count": len(outcomes),
        "min_samples_threshold": MIN_SAMPLES_PER_STRATEGY,
        "weights": weights,
        "per_strategy_samples": per_strategy_samples,
        "by_category": by_category,
    }

    print(f"\nStrategy weights ({len(outcomes)} post-fix resolved trades):")
    for strat, w in weights.items():
        n = per_strategy_samples.get(strat, 0)
        if n >= MIN_SAMPLES_PER_STRATEGY:
            # Recompute accuracy for display (not stored separately)
            correct = sum(
                1 for o in outcomes
                if strat in _parse_strategies(o.get("strategies"))
                and o.get("our_recommendation") == o.get("market_resolution")
            )
            note = f"{correct}/{n} correct ({correct/n*100:.0f}%)"
        else:
            note = f"<{MIN_SAMPLES_PER_STRATEGY} samples — default"
        print(f"  {strat:<28} weight={w:.4f}  [{note}]")

    if by_category:
        print(f"\nBy-category weights (min {MIN_SAMPLES_PER_STRATEGY} samples per cell):")
        for cat, strats in sorted(by_category.items()):
            for strat, info in sorted(strats.items()):
                print(f"  {cat:<15} {strat:<28} weight={info['weight']:.4f}  "
                      f"[acc={info['accuracy']*100:.0f}%  n={info['n']}]")
    else:
        print(f"\nBy-category weights: no data yet (need ≥{MIN_SAMPLES_PER_STRATEGY} samples per category/strategy)")

    print(f"\nCategory accuracy (min {MIN_SAMPLES_PER_CATEGORY} samples for adaptive cap):")
    if cat_accuracy:
        for cat, info in sorted(cat_accuracy.items()):
            gate = "✓ active" if info["sample_count"] >= MIN_SAMPLES_PER_CATEGORY else f"< {MIN_SAMPLES_PER_CATEGORY} samples"
            cap_note = "→ cap reduced to 1" if (info["sample_count"] >= MIN_SAMPLES_PER_CATEGORY and info["accuracy"] < 0.50) else ""
            print(f"  {cat:<20} acc={info['accuracy']*100:.0f}%  n={info['sample_count']}  [{gate}] {cap_note}")
    else:
        print("  No resolved trades with category data yet.")

    if dry_run:
        print("\n[dry-run] Not writing to disk.")
        return

    with open(_WEIGHTS_PATH, "w") as f:
        json.dump(result, f, indent=2)
    print(f"\nWritten → {_WEIGHTS_PATH}")

    cat_result = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "sample_count": len(outcomes),
        "min_samples_per_category": MIN_SAMPLES_PER_CATEGORY,
        "categories": cat_accuracy,
    }
    with open(_CATEGORY_ACCURACY_PATH, "w") as f:
        json.dump(cat_result, f, indent=2)
    print(f"Written → {_CATEGORY_ACCURACY_PATH}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
