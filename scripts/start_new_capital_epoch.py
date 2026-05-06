#!/usr/bin/env python3
"""Start a new capital epoch: reset paper-trading balance and append an
audit entry to capital_epochs[].

Use this when the bot is in capital starvation (Kelly sizing producing
amounts below MIN_BET_AMOUNT) and you want a clean restart while preserving
the historical record. Mechanism mirrors the 2026-04-20 reset that
seed_capital_epoch.py recorded retroactively — this script does it
prospectively.

What it does:
  1. Reads `manifold_bot/paper_trading_state.json`.
  2. Records the pre-reset balance.
  3. Resets balance to --target (default $100).
  4. Appends an entry to state['capital_epochs'][] with started_at,
     topup_from, topup_to, and a reason string (--reason or interactive).
  5. Writes state back.
  6. Does NOT touch positions or trade_history — those keep their
     existing pnl trajectory. The new epoch only changes the baseline
     against which `daily_summary` and `/portfolio` measure profit.

Idempotency:
  This script is NOT idempotent — running it twice creates two epochs
  and resets balance twice. Operator must check before running.

Usage:
  python3 scripts/start_new_capital_epoch.py --target 100 --reason "post-PR-30 unfreeze"
  python3 scripts/start_new_capital_epoch.py --target 100 --reason "..." --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone


def main(state_path: str, target: float, reason: str, dry_run: bool) -> int:
    if not os.path.exists(state_path):
        print(f"State file not found: {state_path}", file=sys.stderr)
        return 1

    with open(state_path, "r") as f:
        state = json.load(f)

    pre_balance = float(state.get("balance", 0.0))
    epochs = state.get("capital_epochs") or []

    new_epoch = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "topup_from": round(pre_balance, 4),
        "topup_to": round(float(target), 4),
        "reason": reason,
    }

    print("=== New Capital Epoch ===")
    print(f"  state file:   {state_path}")
    print(f"  pre-balance:  ${pre_balance:.2f}")
    print(f"  target:       ${target:.2f}")
    print(f"  delta:        ${target - pre_balance:+.2f}")
    print(f"  reason:       {reason}")
    print(f"  prior epochs: {len(epochs)}")
    if epochs:
        last = epochs[-1]
        print(f"  last epoch:   {last.get('started_at','?')} ${last.get('topup_from','?')} → ${last.get('topup_to','?')}")

    if dry_run:
        print("\n[dry-run] Not writing to disk.")
        return 0

    state["balance"] = float(target)
    state["capital_epochs"] = epochs + [new_epoch]

    with open(state_path, "w") as f:
        json.dump(state, f, indent=2)

    print(f"\nWrote new epoch to {state_path}.")
    print(f"Balance now: ${target:.2f}; epochs: {len(epochs) + 1}.")
    return 0


if __name__ == "__main__":
    default_state = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "manifold_bot",
        "paper_trading_state.json",
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", default=default_state, help="path to paper_trading_state.json")
    parser.add_argument("--target", type=float, default=100.0, help="new balance after reset (default $100)")
    parser.add_argument("--reason", required=True, help="audit reason recorded in the epoch entry")
    parser.add_argument("--dry-run", action="store_true", help="print what would happen, don't write")
    args = parser.parse_args()
    sys.exit(main(args.state, args.target, args.reason, args.dry_run))
