#!/usr/bin/env python3
"""
Offline backtest from reconstructed snapshots → strategy weights.

Why this exists:
  compute_strategy_weights.py reads bet_outcomes (live resolved trades).
  Live paper trades take months to resolve, so weights stay at 1.0 defaults
  until enough trades accumulate.

  This script runs the same accuracy measurement using M3 reconstructed
  snapshots instead — historical market states at T-7/14/30 days before close
  where the outcome is already known. Same math, different data source.

  Result: strategy weights that reflect real signal TODAY instead of in 6 months.

Which strategies can be backtested:
  Only strategies whose signals are computable from reconstructed snapshot fields.
  Reconstructed rows have: probability, question, category (via join), days_before_close.
  They do NOT have: volume24h, exact bettor count at snapshot time, lastBetTime.

  - mean_reversion     ✓  (needs probability + bettors — uses stored bettors or default)
  - probability_bias   ✓  (needs probability + calibration table — both available)
  - probability_direction  PARTIAL (skipped when volume24h is NULL, which is all reconstructed rows)

  The backtest weights are labelled source='backtest' so callers can decide how
  to blend them with live bet_outcomes weights.

Merge logic (written to strategy_weights.json):
  For each strategy:
    - If live weight has >= MIN_SAMPLES_PER_STRATEGY → use live weight (trust real trades)
    - Else if backtest weight has >= BACKTEST_MIN_SAMPLES → use backtest weight
    - Else → keep default 1.0

Usage:
  python3 scripts/backtest_from_snapshots.py
  python3 scripts/backtest_from_snapshots.py --dry-run
  python3 scripts/backtest_from_snapshots.py --window 14  # only use T-14 snapshots
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

from manifold_bot.strategies import TradingStrategies
from scripts.weekly_ev_report import MIN_SAMPLES_PER_STRATEGY

SNAPSHOTS_DB   = os.path.join(ROOT_DIR, "data", "market_snapshots.db")
CALIBRATION_DB = os.path.join(ROOT_DIR, "data", "calibration.db")
WEIGHTS_PATH   = os.path.join(ROOT_DIR, "data", "strategy_weights.json")
REPORT_PATH    = os.path.join(ROOT_DIR, "data", "backtest_report.json")

# Min samples for a backtest weight to be trusted (separate from live gate).
# Lower than live because we have more data but it's older and less representative.
BACKTEST_MIN_SAMPLES = 30

# Asymmetric clamp on backtest-only weights (no live data to confirm):
# a backtest may DOWN-weight a strategy below 1.0 (defensive, helpful when the
# strategy is bad), but cannot UP-weight above 1.0 without live confirmation.
# Boost weights without live data are dangerous: an over-fit reconstruction
# can manufacture apparent edge that pushes the trader to take MORE trades on
# a fictional signal — exactly the failure mode we saw with probability_bias
# at 1.2 on Apr-12 (0 live samples, backtest-seeded boost compounded with the
# old confidence-as-probability bug to produce phantom +EV).
#
# Live data with n >= MIN_SAMPLES_PER_STRATEGY can still raise weights up to
# the per-formula cap (1.20). This clamp ONLY applies when the source is
# 'backtest' (no live confirmation yet).
MAX_BACKTEST_ONLY_WEIGHT = 1.0

# Strategies that can be meaningfully evaluated from reconstructed snapshots.
# probability_direction is excluded because its volume24h guard always fires
# on reconstructed rows (volume24h is NULL — not available from bet history).
BACKTEST_STRATEGIES = [
    "mean_reversion",
    "probability_bias",
]

# Default bettor count for reconstructed markets (real value unavailable at
# snapshot time, but M2 filtered these to ≥10 bettors, so 20 is conservative).
_DEFAULT_BETTORS = 20


# ── Load snapshots ────────────────────────────────────────────────────────────

def load_reconstructed_snapshots(
    snapshots_db: str = SNAPSHOTS_DB,
    calibration_db: str = CALIBRATION_DB,
    window: int | None = None,
) -> list[dict]:
    """
    Load reconstructed snapshot rows and join with resolved_markets for category.

    Args:
        window: if set, only load snapshots with days_before_close == window
    """
    if not os.path.exists(snapshots_db):
        return []

    conn = sqlite3.connect(snapshots_db)

    window_filter = ""
    params: list = []
    if window is not None:
        window_filter = "AND ms.days_before_close = ?"
        params.append(window)

    # LEFT JOIN to get category from resolved_markets if available.
    # market_snapshots doesn't store category itself.
    rows = conn.execute(f"""
        SELECT
            ms.market_id,
            ms.question,
            ms.probability,
            ms.bettors,
            ms.days_before_close,
            ms.outcome,
            ms.snapshot_at
        FROM market_snapshots ms
        WHERE ms.source = 'reconstructed'
          AND ms.outcome IS NOT NULL
          AND ms.probability IS NOT NULL
          {window_filter}
        ORDER BY ms.market_id, ms.days_before_close
    """, params).fetchall()
    conn.close()

    cols = ["market_id", "question", "probability", "bettors",
            "days_before_close", "outcome", "snapshot_at"]
    snapshots = [dict(zip(cols, r)) for r in rows]

    # Enrich with category from resolved_markets (best-effort join)
    if snapshots and os.path.exists(calibration_db):
        market_ids = list({s["market_id"] for s in snapshots})
        calib_conn = sqlite3.connect(calibration_db)
        placeholders = ",".join("?" * len(market_ids))
        cat_rows = calib_conn.execute(
            f"SELECT market_id, category FROM resolved_markets WHERE market_id IN ({placeholders})",
            market_ids
        ).fetchall()
        calib_conn.close()
        cat_map = {r[0]: r[1] for r in cat_rows}
        for s in snapshots:
            s["category"] = cat_map.get(s["market_id"], "other")
    else:
        for s in snapshots:
            s["category"] = "other"

    return snapshots


# ── Synthetic market dict ─────────────────────────────────────────────────────

def _snapshot_to_market_dict(snapshot: dict) -> dict:
    """
    Convert a reconstructed snapshot row into the market dict format that
    TradingStrategies expects.

    Fields not available from bet history are set to conservative defaults:
      - volume24Hours=0   → probability_direction always skips (correct: we can't
                            evaluate it without real volume data)
      - uniqueBettorCount → use stored value if available, else _DEFAULT_BETTORS
      - lastBetTime=None  → probability_direction's staleness guard is bypassed
                            (field absent means "don't know", strategy may or
                            may not fire depending on its None-handling)
      - closeTime         → approximate from snapshot_at + days_before_close
    """
    days = snapshot.get("days_before_close") or 7
    try:
        snapshot_dt = datetime.fromisoformat(
            snapshot["snapshot_at"].replace("Z", "+00:00")
        )
        close_ts_ms = int((snapshot_dt + timedelta(days=days)).timestamp() * 1000)
    except Exception:
        close_ts_ms = None

    return {
        "id":               snapshot["market_id"],
        "question":         snapshot.get("question", ""),
        "probability":      snapshot["probability"],
        "volume24Hours":    0,          # not available — causes volume-gated strategies to skip
        "uniqueBettorCount": snapshot.get("bettors") or _DEFAULT_BETTORS,
        "lastBetTime":      None,       # not available for historical snapshots
        "closeTime":        close_ts_ms,
        "category":         snapshot.get("category", "other"),
    }


# ── Backtest runner ───────────────────────────────────────────────────────────

def run_backtest(snapshots: list[dict]) -> dict[str, dict]:
    """
    Run strategies against all snapshots and collect direction accuracy.

    Returns:
        {strategy_name: {"total": int, "correct": int, "accuracy": float}}
    """
    strategies = TradingStrategies()
    strategy_fns = {
        "mean_reversion":   strategies.mean_reversion_strategy,
        "probability_bias": strategies.probability_bias_strategy,
    }

    per_strategy: dict[str, dict] = {
        s: {"total": 0, "correct": 0} for s in BACKTEST_STRATEGIES
    }

    for snapshot in snapshots:
        market = _snapshot_to_market_dict(snapshot)
        outcome = snapshot.get("outcome")
        if outcome not in ("YES", "NO"):
            continue

        for strat_name, fn in strategy_fns.items():
            try:
                recommendation = fn(market)
            except Exception:
                continue  # strategy raised — skip this snapshot/strategy combo

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


# ── Weight computation ────────────────────────────────────────────────────────

def _accuracy_to_weight(accuracy: float) -> float:
    """Same formula as compute_strategy_weights._compute_weights."""
    if accuracy >= 0.60:
        return min(1.0 + (accuracy - 0.50) * 2, 1.20)
    elif accuracy >= 0.50:
        return 1.0
    else:
        return max(0.5, accuracy / 0.50)


def compute_backtest_weights(backtest_results: dict[str, dict]) -> dict[str, float]:
    """
    Convert backtest accuracy to weight scalars.
    Returns {strategy: weight} only for strategies that cleared BACKTEST_MIN_SAMPLES.
    """
    weights = {}
    for strat, stats in backtest_results.items():
        if stats["total"] < BACKTEST_MIN_SAMPLES or stats["accuracy"] is None:
            continue
        weights[strat] = round(_accuracy_to_weight(stats["accuracy"]), 4)
    return weights


def merge_weights(
    backtest_weights: dict[str, float],
    live_weights_path: str = WEIGHTS_PATH,
) -> tuple[dict, str]:
    """
    Merge backtest weights with live bet_outcomes weights.

    Merge rule (per strategy):
      1. Live weight clears MIN_SAMPLES_PER_STRATEGY → use live (real trades win)
      2. Backtest weight clears BACKTEST_MIN_SAMPLES  → use backtest
      3. Otherwise → keep 1.0 default

    Returns:
        (merged_weights_dict, source_annotation_string)
    """
    live_data: dict = {}
    if os.path.exists(live_weights_path):
        with open(live_weights_path) as f:
            live_data = json.load(f)

    live_weights   = live_data.get("weights", {})
    live_samples   = live_data.get("sample_count", 0)
    live_per_strat = live_data.get("per_strategy_samples", {})

    # Known strategies from the live weights file
    all_strategies = set(live_weights.keys()) | set(backtest_weights.keys())

    merged: dict[str, dict] = {}
    for strat in sorted(all_strategies):
        live_w       = live_weights.get(strat, 1.0)
        backtest_w   = backtest_weights.get(strat)
        live_n       = live_per_strat.get(strat, 0)

        if live_n >= MIN_SAMPLES_PER_STRATEGY:
            source = "live"
            final_w = live_w
        elif backtest_w is not None:
            # Asymmetric clamp: backtest can down-weight (defensive) but not
            # up-weight without live confirmation. See MAX_BACKTEST_ONLY_WEIGHT.
            if backtest_w > MAX_BACKTEST_ONLY_WEIGHT:
                source = "backtest_clamped"
                final_w = MAX_BACKTEST_ONLY_WEIGHT
            else:
                source = "backtest"
                final_w = backtest_w
        else:
            source = "default"
            final_w = 1.0

        merged[strat] = {
            "weight": round(final_w, 4),
            "source": source,
            "live_samples": live_n,
        }

    return merged, f"live_trades={live_samples}"


# ── Main ──────────────────────────────────────────────────────────────────────

def main(dry_run: bool = False, window: int | None = None) -> None:
    print("=" * 65)
    print("OFFLINE BACKTEST — RECONSTRUCTED SNAPSHOTS → STRATEGY WEIGHTS")
    print("=" * 65)

    snapshots = load_reconstructed_snapshots(window=window)
    if not snapshots:
        print("\nNo reconstructed snapshots found.")
        print("Run: python3 scripts/reconstruct_snapshots.py")
        return

    by_window = {}
    for s in snapshots:
        d = s.get("days_before_close")
        by_window[d] = by_window.get(d, 0) + 1
    print(f"\nSnapshots loaded: {len(snapshots)}")
    for d, n in sorted(by_window.items()):
        print(f"  T-{d:>2}d: {n}")

    print(f"\nRunning strategies: {', '.join(BACKTEST_STRATEGIES)}")
    backtest_results = run_backtest(snapshots)

    print(f"\n{'─'*65}")
    print(f"BACKTEST RESULTS  (min {BACKTEST_MIN_SAMPLES} samples to update weight)")
    print(f"{'─'*65}")
    print(f"  {'Strategy':28} {'N':>5}  {'Correct':>7}  {'Accuracy':>9}  {'Weight':>7}  Note")
    print(f"  {'─'*28} {'─'*5}  {'─'*7}  {'─'*9}  {'─'*7}  {'─'*20}")

    backtest_weights = compute_backtest_weights(backtest_results)
    for strat in BACKTEST_STRATEGIES:
        stats = backtest_results.get(strat, {"total": 0, "correct": 0, "accuracy": None})
        n    = stats["total"]
        acc  = stats["accuracy"]
        w    = backtest_weights.get(strat)
        if n < BACKTEST_MIN_SAMPLES:
            note = f"< {BACKTEST_MIN_SAMPLES} samples — default"
            w_str = "  1.0000"
        elif acc is None:
            note = "no signal"
            w_str = "  1.0000"
        else:
            note = "✓ used" if w is not None else ""
            w_str = f"  {w:.4f}" if w is not None else "  1.0000"
        acc_str = f"{acc*100:.1f}%" if acc is not None else "   —"
        print(f"  {strat:28} {n:>5}  {stats['correct']:>7}  {acc_str:>9}  {w_str}  {note}")

    # Merge with live weights
    merged, live_note = merge_weights(backtest_weights)

    print(f"\n{'─'*65}")
    print(f"MERGED WEIGHTS  ({live_note})")
    print(f"{'─'*65}")
    print(f"  {'Strategy':28} {'Weight':>7}  Source")
    for strat, info in sorted(merged.items()):
        print(f"  {strat:28} {info['weight']:>7.4f}  {info['source']}")

    # Write report
    report = {
        "generated_at":     datetime.now(timezone.utc).isoformat(),
        "snapshots_used":   len(snapshots),
        "windows":          by_window,
        "backtest_results": backtest_results,
        "backtest_weights": backtest_weights,
        "merged_weights":   merged,
        "dry_run":          dry_run,
    }

    if not dry_run:
        os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
        with open(REPORT_PATH, "w") as f:
            json.dump(report, f, indent=2)
        print(f"\nReport saved → {REPORT_PATH}")

        # Update strategy_weights.json if it exists (only update weights
        # for strategies covered by backtest; leave others unchanged)
        if os.path.exists(WEIGHTS_PATH):
            with open(WEIGHTS_PATH) as f:
                existing = json.load(f)
            updated = False
            for strat, info in merged.items():
                # Both 'backtest' and 'backtest_clamped' are backtest-sourced;
                # the clamp only changed the value, not the provenance.
                if info["source"] in ("backtest", "backtest_clamped"):
                    existing.setdefault("weights", {})[strat] = info["weight"]
                    updated = True
            if updated:
                existing["backtest_updated_at"] = report["generated_at"]
                with open(WEIGHTS_PATH, "w") as f:
                    json.dump(existing, f, indent=2)
                print(f"Weights updated → {WEIGHTS_PATH}  (backtest fills gaps)")
            else:
                print("No weight updates needed (live data sufficient or no backtest signal).")
        else:
            print(f"  {WEIGHTS_PATH} not found — run compute_strategy_weights.py first.")
    else:
        print("\n[dry-run] Not writing to disk.")

    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Offline backtest from reconstructed snapshots → strategy weights"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Print results without writing to disk")
    parser.add_argument("--window", type=int, default=None,
                        help="Only use snapshots from T-N days (default: all windows)")
    args = parser.parse_args()
    main(dry_run=args.dry_run, window=args.window)
