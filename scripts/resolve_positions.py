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
from automation.send_telegram import send_message as _tg


def main() -> int:
    trader = PaperTrader()

    # Use PaperTrader's own count helper so this script never needs to know
    # the internal positions data structure.  If PaperTrader.count_open_positions()
    # doesn't exist yet, we fall back to a local count AND assert the two agree,
    # which will surface any structural mismatch immediately rather than silently
    # miscounting.
    def _local_count(t: PaperTrader) -> int:
        """Fallback counter – mirrors auto_resolve_markets() iteration exactly."""
        return sum(
            1 for pos_list in t.positions.values()
            for pos in (pos_list if isinstance(pos_list, list) else [pos_list])
            if pos.get('status') == 'OPEN'
        )

    def count_open(t: PaperTrader) -> int:
        if callable(getattr(t, 'count_open_positions', None)):
            official = t.count_open_positions()
            # Cross-check: local traversal must agree with the official helper.
            local = _local_count(t)
            assert official == local, (
                f"count_open_positions() returned {official} but local traversal "
                f"found {local}. The positions data structure may have changed; "
                f"update both PaperTrader.count_open_positions() and this script."
            )
            return official
        # PaperTrader helper not yet available – use local count directly.
        return _local_count(t)

    open_before = count_open(trader)

    if open_before == 0:
        print("No open positions to check.")
        return 0

    print(f"Checking {open_before} open position(s) for resolution...")
    print()

    balance_before = trader.balance

    # State persistence (saving positions/balance to disk) is delegated entirely
    # to PaperTrader.auto_resolve_markets(), which performs an atomic write after
    # each market is resolved.  If this call raises (e.g. network timeout, JSON
    # decode error) mid-pass, whatever markets were already resolved will have
    # been persisted atomically by that method before the exception propagated,
    # so no state is lost and no partial/inconsistent write can occur here.
    resolved_count = trader.auto_resolve_markets()

    open_after = count_open(trader)

    balance_change = trader.balance - balance_before

    print()
    print("=" * 50)
    print(f"Resolution check complete")
    print(f"  Markets resolved : {resolved_count}")
    print(f"  Open positions   : {open_before} → {open_after}")
    print(f"  Balance change   : ${balance_change:+.2f}")
    print(f"  Current balance  : ${trader.balance:.2f}")
    print("=" * 50)

    if resolved_count > 0:
        try:
            sign = "+" if balance_change >= 0 else ""
            _tg(
                f"📊 *{resolved_count} market(s) resolved*\n"
                f"P&L: {sign}${balance_change:.2f} | Balance: ${trader.balance:.2f}\n"
                f"Positions: {open_before} → {open_after}"
            )
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"Fatal error: {e}")
        sys.exit(1)
