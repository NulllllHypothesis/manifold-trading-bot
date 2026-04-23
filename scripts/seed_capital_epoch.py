#!/usr/bin/env python3
"""One-shot migration: record the 2026-04-20 Phase 3 capital top-up as a
first-class entry in paper_trading_state.json under capital_epochs[].

Before this migration daily_summary reported total profit against the
hard-coded INITIAL_BALANCE=$1000 baseline, which is wrong after the
deliberate $34.76 → $100 top-up. After the migration effective_baseline()
returns $100 and the summary reports profit against it.

Idempotent — if a capital_epochs entry already exists, this script prints
the current state and exits 0 without writing.

Run once on the sandbox and once locally (or just once wherever the state
file lives that cron uses) after merging.
"""

from __future__ import annotations

import json
import os
import sys


_EPOCH = {
    "started_at": "2026-04-20T14:08:00+03:00",
    "topup_from": 34.76,
    "topup_to": 100.00,
    "realised_before_topup": 4.21,
    "reason": "Phase 3 runway reset — pre-Monday 2026-04-27 cycle needed "
              "~$100 to fund 15-20 new short-term bets at $5 each and give "
              "compute_strategy_weights.py enough post_ev_fix rows to clear "
              "min_samples=10. Principle #4 honoured: pre-epoch P&L is not "
              "directly comparable to post-epoch P&L.",
}


def main(state_path: str) -> int:
    if not os.path.exists(state_path):
        print(f"State file not found: {state_path}", file=sys.stderr)
        return 1

    with open(state_path, "r") as f:
        state = json.load(f)

    existing = state.get("capital_epochs") or []
    if existing:
        print(f"capital_epochs already present ({len(existing)} entries) — no-op.")
        for e in existing:
            print(f"  {e.get('started_at')}: ${e.get('topup_from')} → ${e.get('topup_to')}")
        return 0

    state["capital_epochs"] = [_EPOCH]

    with open(state_path, "w") as f:
        json.dump(state, f, indent=2)

    print(f"Seeded capital_epochs into {state_path}")
    print(f"  {_EPOCH['started_at']}: ${_EPOCH['topup_from']} → ${_EPOCH['topup_to']}")
    return 0


if __name__ == "__main__":
    default = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "manifold_bot",
        "paper_trading_state.json",
    )
    path = sys.argv[1] if len(sys.argv) > 1 else default
    sys.exit(main(path))
