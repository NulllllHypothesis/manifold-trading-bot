#!/usr/bin/env python3
"""
Simulate strategy performance against all 2016+ resolved markets → real weights NOW.

Why this exists:
  compute_strategy_weights.py needs live bet_outcomes to accumulate (months).
  backtest_from_snapshots.py uses ~127 reconstructed snapshots and only tests
  2 strategies.

  We have 2016 resolved markets in calibration.db with:
    - probability_close  — crowd's last probability before the market closed
    - outcome            — actual YES/NO resolution
    - unique_bettors     — proxy for market depth
    - question           — for category inference

  That's enough to simulate all 3 stat strategies and get real accuracy numbers
  today instead of waiting months for live trades to resolve.

Strategies simulated:
  - probability_direction   — follows crowd direction (fires when prob != 0.45-0.55)
  - mean_reversion          — fades extremes (fires on prob > 0.85 or < 0.15, thin market)
  - probability_bias        — exploits calibration bias (fires on 30-40% and 60-70% buckets)

Not simulated (missing data):
  - creator_disagreement    — needs resolutionProbability (not in resolved_markets)
  - ai_analysis             — requires LLM call

Simulation setup:
  - probability         = probability_close (crowd's settled pre-close price)
  - uniqueBettorCount   = unique_bettors from DB
  - volume24Hours       = 10  (bypasses volume guard — we simulate "active market")
  - lastBetTime         = None (bypasses staleness guard)
  - closeTime           = far future (bypasses near-close guard in mean_reversion)

Merge logic (written to strategy_weights.json):
  For each strategy:
    1. Live bet_outcomes cleared MIN_SAMPLES → use live weight
    2. Simulation cleared SIM_MIN_SAMPLES   → use simulation weight
    3. Else → keep 1.0 default

Usage:
  python3 scripts/simulate_strategy_weights.py
  python3 scripts/simulate_strategy_weights.py --dry-run
  python3 scripts/simulate_strategy_weights.py --min-bettors 5  # filter noisy markets
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

from manifold_bot.strategies import TradingStrategies
from scripts.weekly_ev_report import MIN_SAMPLES_PER_STRATEGY

CALIBRATION_DB = os.path.join(ROOT_DIR, "data", "calibration.db")
WEIGHTS_PATH   = os.path.join(ROOT_DIR, "data", "strategy_weights.json")
REPORT_PATH    = os.path.join(ROOT_DIR, "data", "simulation_report.json")

# Minimum samples for simulation weight to override the 1.0 default.
SIM_MIN_SAMPLES = 25

# Strategies to simulate — all three are run and reported for visibility.
SIM_STRATEGIES = [
    "probability_direction",
    "mean_reversion",
    "probability_bias",
]

# Strategies reported but NOT applied to strategy_weights.json.
#
# probability_direction and mean_reversion both have live guards that filter
# markets based on recency, volume, and time-to-close. The simulation bypasses
# those guards (volume24Hours=10, lastBetTime=None, closeTime=far future) because
# resolved_markets doesn't carry that data — but that means we're measuring
# "what did the market know at near-close" rather than "how would the strategy
# behave at decision time."
#
# Concretely:
#   probability_direction 89.7%: final price converged to outcome — trivially high
#   mean_reversion        1.7%:  fighting the converged final price — trivially low
#
# These numbers are measurement artifacts, not strategy accuracy. Applying them
# as Kelly inputs would be a mistake. They need decision-time snapshot data
# (M3 → backtest_from_snapshots.py) to be evaluated correctly.
#
# probability_bias has no recency/volume guards. Its signal (bucket-level crowd
# overestimation) is equally valid at close time and at decision time, so it IS
# safe to apply from this simulation.
_INFORMATIONAL_ONLY = frozenset({"probability_direction", "mean_reversion"})

# Fake far-future close time so mean_reversion's near-close guard doesn't fire
# when running in informational/display mode.
_FAR_FUTURE_MS = int((datetime.now(timezone.utc) + timedelta(days=365)).timestamp() * 1000)


def load_resolved_markets(db_path: str, min_bettors: int = 3) -> list[dict]:
    """Load all YES/NO resolved markets from calibration.db."""
    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(db_path)
    rows = conn.execute("""
        SELECT market_id, question, probability_close, outcome, unique_bettors, category
        FROM resolved_markets
        WHERE outcome IN ('YES', 'NO')
          AND probability_close IS NOT NULL
          AND unique_bettors >= ?
        ORDER BY market_id
    """, (min_bettors,)).fetchall()
    conn.close()
    cols = ["market_id", "question", "probability_close", "outcome", "unique_bettors", "category"]
    return [dict(zip(cols, r)) for r in rows]


def _to_market_dict(row: dict) -> dict:
    """
    Convert a resolved_markets row into the market dict format strategies expect.

    Simulation assumptions:
      volume24Hours = 10    → bypasses probability_direction's volume guard (≥5)
      lastBetTime   = None  → bypasses probability_direction's staleness guard
      closeTime     = far   → bypasses mean_reversion's near-close guard (<7 days)
    """
    return {
        "id":               row["market_id"],
        "question":         row["question"],
        "probability":      row["probability_close"],
        "uniqueBettorCount": row["unique_bettors"],
        "volume24Hours":    10,          # simulate "active market" for volume guard
        "lastBetTime":      None,        # no staleness data; bypass guard
        "closeTime":        _FAR_FUTURE_MS,  # not near-close; bypass guard
        "resolutionProbability": None,   # creator_disagreement won't fire
    }


def run_simulation(markets: list[dict]) -> dict[str, dict]:
    """
    Run all SIM_STRATEGIES against every market and record accuracy.

    Returns:
        {strategy: {"total": int, "correct": int, "accuracy": float}}
    """
    strategies = TradingStrategies()
    strategy_fns = {
        "probability_direction": strategies.probability_direction_strategy,
        "mean_reversion":        strategies.mean_reversion_strategy,
        "probability_bias":      strategies.probability_bias_strategy,
    }

    per_strategy: dict[str, dict] = {s: {"total": 0, "correct": 0} for s in SIM_STRATEGIES}

    for row in markets:
        market = _to_market_dict(row)
        outcome = row["outcome"]  # 'YES' or 'NO'

        for strat_name, fn in strategy_fns.items():
            try:
                recommendation = fn(market)
            except Exception:
                continue
            if recommendation is None:
                continue  # strategy didn't fire on this market

            per_strategy[strat_name]["total"] += 1
            if recommendation == outcome:
                per_strategy[strat_name]["correct"] += 1

    results = {}
    for strat, stats in per_strategy.items():
        total = stats["total"]
        accuracy = round(stats["correct"] / total, 4) if total > 0 else None
        results[strat] = {
            "total":    total,
            "correct":  stats["correct"],
            "accuracy": accuracy,
        }
    return results


def _accuracy_to_weight(accuracy: float) -> float:
    """Same formula as compute_strategy_weights._weight_from_accuracy."""
    if accuracy >= 0.60:
        return min(1.0 + (accuracy - 0.50) * 2, 1.20)
    elif accuracy >= 0.50:
        return 1.0
    else:
        return max(0.5, accuracy / 0.50)


def compute_sim_weights(sim_results: dict[str, dict]) -> dict[str, float]:
    """
    Convert simulation accuracy to weight scalars for strategies that:
    1. Cleared SIM_MIN_SAMPLES, and
    2. Are not in _INFORMATIONAL_ONLY (guards bypassed → numbers are artifacts)
    """
    weights = {}
    for strat, stats in sim_results.items():
        if strat in _INFORMATIONAL_ONLY:
            continue
        n = stats["total"]
        acc = stats["accuracy"]
        if n < SIM_MIN_SAMPLES or acc is None:
            continue
        weights[strat] = round(_accuracy_to_weight(acc), 4)
    return weights


def merge_into_weights_file(sim_weights: dict[str, float], dry_run: bool = False) -> dict:
    """
    Merge simulation weights into strategy_weights.json.

    Merge rule (per strategy):
      1. Live bet_outcomes cleared MIN_SAMPLES_PER_STRATEGY → use live (trust real trades)
      2. Simulation cleared SIM_MIN_SAMPLES                 → use simulation weight
      3. Otherwise                                          → keep 1.0 default
    """
    if not os.path.exists(WEIGHTS_PATH):
        print(f"  {WEIGHTS_PATH} not found — run compute_strategy_weights.py first.")
        return {}

    with open(WEIGHTS_PATH) as f:
        existing = json.load(f)

    live_weights = existing.get("weights", {})
    live_per_strat = existing.get("per_strategy_samples", {})

    updated_strategies = []
    for strat, sim_w in sim_weights.items():
        live_n = live_per_strat.get(strat, 0)
        if live_n >= MIN_SAMPLES_PER_STRATEGY:
            # Real trades take precedence — don't override
            continue
        # Use simulation weight
        if not dry_run:
            existing.setdefault("weights", {})[strat] = sim_w
        updated_strategies.append(strat)

    if updated_strategies and not dry_run:
        existing["simulation_updated_at"] = datetime.now(timezone.utc).isoformat()
        existing["simulation_source"] = f"{len(updated_strategies)} strategies from {CALIBRATION_DB}"
        with open(WEIGHTS_PATH, "w") as f:
            json.dump(existing, f, indent=2)

    return existing


def main(dry_run: bool = False, min_bettors: int = 3) -> None:
    print("=" * 65)
    print("STRATEGY SIMULATION — RESOLVED MARKETS → WEIGHTS")
    print("=" * 65)

    markets = load_resolved_markets(CALIBRATION_DB, min_bettors=min_bettors)
    if not markets:
        print(f"\nNo resolved markets found in {CALIBRATION_DB}")
        print("Run: python3 scripts/harvest_resolved.py")
        return

    print(f"\nMarkets loaded: {len(markets)}  (min_bettors={min_bettors})")

    # Show category breakdown
    by_cat: dict[str, int] = {}
    for m in markets:
        cat = m.get("category") or "other"
        by_cat[cat] = by_cat.get(cat, 0) + 1
    for cat, n in sorted(by_cat.items(), key=lambda x: -x[1]):
        print(f"  {cat:<15} {n}")

    print(f"\nRunning strategies: {', '.join(SIM_STRATEGIES)}")
    sim_results = run_simulation(markets)

    sim_weights = compute_sim_weights(sim_results)

    print(f"\n{'─'*65}")
    print(f"SIMULATION RESULTS  (min {SIM_MIN_SAMPLES} samples to update weight)")
    print(f"{'─'*65}")
    print(f"  {'Strategy':28} {'Fired':>6}  {'Correct':>7}  {'Accuracy':>9}  {'Weight':>7}  Note")
    print(f"  {'─'*28} {'─'*6}  {'─'*7}  {'─'*9}  {'─'*7}  {'─'*20}")

    for strat in SIM_STRATEGIES:
        stats = sim_results.get(strat, {"total": 0, "correct": 0, "accuracy": None})
        n    = stats["total"]
        acc  = stats["accuracy"]
        w    = sim_weights.get(strat)
        if strat in _INFORMATIONAL_ONLY:
            note  = "informational only — guards bypassed, not applied"
            w_str = "  (n/a)"
        elif n < SIM_MIN_SAMPLES:
            note  = f"< {SIM_MIN_SAMPLES} samples"
            w_str = "  1.0000"
        elif acc is None:
            note  = "no signal"
            w_str = "  1.0000"
        else:
            note  = "✓ updates weight"
            w_str = f"  {w:.4f}" if w is not None else "  1.0000"
        acc_str = f"{acc*100:.1f}%" if acc is not None else "   —"
        print(f"  {strat:28} {n:>6}  {stats['correct']:>7}  {acc_str:>9}  {w_str}  {note}")

    print(f"\n{'─'*65}")
    print(f"MERGED RESULT")
    print(f"{'─'*65}")
    merged = merge_into_weights_file(sim_weights, dry_run=dry_run)
    if merged:
        final_weights = merged.get("weights", {})
        sim_applied = set() if dry_run else set(sim_weights.keys())
        for strat, w in sorted(final_weights.items()):
            live_n = merged.get("per_strategy_samples", {}).get(strat, 0)
            if live_n >= MIN_SAMPLES_PER_STRATEGY:
                source = "live"
            elif strat in sim_applied:
                source = "simulation"
            else:
                source = "default"
            suffix = "  [dry-run: not written]" if dry_run and strat in sim_weights else ""
            print(f"  {strat:28} {w:.4f}  [{source}]{suffix}")

    # Write report
    report = {
        "generated_at":  datetime.now(timezone.utc).isoformat(),
        "markets_used":  len(markets),
        "min_bettors":   min_bettors,
        "sim_results":   sim_results,
        "sim_weights":   sim_weights,
        "dry_run":       dry_run,
    }
    if not dry_run:
        with open(REPORT_PATH, "w") as f:
            json.dump(report, f, indent=2)
        print(f"\nReport → {REPORT_PATH}")
        if sim_weights:
            print(f"Weights → {WEIGHTS_PATH}  (simulation fills gaps)")
        else:
            print("No weight updates (no strategy cleared the sample threshold).")
    else:
        print("\n[dry-run] Not writing to disk.")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Simulate strategy weights from resolved markets"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Print results without writing to disk")
    parser.add_argument("--min-bettors", type=int, default=3,
                        help="Minimum unique bettors per market (default: 3)")
    args = parser.parse_args()
    main(dry_run=args.dry_run, min_bettors=args.min_bettors)
