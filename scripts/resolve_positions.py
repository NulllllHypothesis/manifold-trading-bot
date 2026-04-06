#!/usr/bin/env python3
"""
Position resolution checker for Manifold trading bot.

Polls the Manifold API for each open position and resolves any that
have settled on-chain. Frees up position slots so the bot can trade again.

Run manually:
    python3 scripts/resolve_positions.py

Or let the cron job run it automatically every 30 minutes.
"""

import sys
import os

# Add project root to path so manifold_bot package is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from manifold_bot.paper_trader import PaperTrader


def main() -> int:
    trader = PaperTrader()

    # Count open positions before resolution pass
    open_before = sum(
        1 for pos_list in trader.positions.values()
        for pos in (pos_list if isinstance(pos_list, list) else [pos_list])
        if pos.get('status') == 'OPEN'
    )

    if open_before == 0:
        print("No open positions to check.")
        return 0

    print(f"Checking {open_before} open position(s) for resolution...")
    print()

    balance_before = trader.balance
    resolved_count = trader.auto_resolve_markets()

    # Count open positions after resolution pass
    open_after = sum(
        1 for pos_list in trader.positions.values()
        for pos in (pos_list if isinstance(pos_list, list) else [pos_list])
        if pos.get('status') == 'OPEN'
    )

    balance_change = trader.balance - balance_before

    print()
    print("=" * 50)
    print(f"Resolution check complete")
    print(f"  Markets resolved : {resolved_count}")
    print(f"  Open positions   : {open_before} → {open_after}")
    print(f"  Balance change   : ${balance_change:+.2f}")
    print(f"  Current balance  : ${trader.balance:.2f}")
    print("=" * 50)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"Fatal error: {e}")
        sys.exit(1)
