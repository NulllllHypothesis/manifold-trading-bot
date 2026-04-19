#!/usr/bin/env python3
"""
Execute or dismiss a pending abandon proposal by ID.

Reads pending_abandons.json, finds the entry with the given ID, then:
  approve: every OPEN/STRANDED leg in the market is flipped to ABANDONED
           with profit = -amount (full write-off). balance reduced. One
           bet_outcomes row per leg with era='write_off'.
  dismiss: marks status="dismissed" without trading.

Usage:
    python3 scripts/execute_abandon.py --id 1             # approve #1
    python3 scripts/execute_abandon.py --id 2 --dismiss   # dismiss #2

Telegram reply pattern (same as close/swap):
    "approve abandon 1" → python3 scripts/execute_abandon.py --id 1
    "dismiss abandon 2" → python3 scripts/execute_abandon.py --id 2 --dismiss

The proposal expires after ABANDON_EXPIRY_HOURS (72h — longer than the
close proposal 4h window, since a terminal write-off deserves more
operator consideration). Running after expiry exits 1.
"""

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from manifold_bot.paper_trader import PaperTrader
from manifold_bot.manifold_api import api_client
from automation.send_telegram import send_telegram_message


_ROOT = Path(__file__).resolve().parent.parent
_PENDING_ABANDONS_PATH = _ROOT / "pending_abandons.json"

# Must match detect_stale_positions.ABANDON_EXPIRY_HOURS so proposer and
# approver agree on when a proposal is expired.
ABANDON_EXPIRY_HOURS = 72


class MarketResolvedError(RuntimeError):
    """The market resolved after the proposal was created.

    If Manifold has now resolved the market, the honest thing to do is let
    resolve_positions.py handle it on the true resolution path — do NOT
    write off the capital as a loss when a real WIN/LOSS/MKT outcome now
    exists on the API.
    """


# ── File helpers ─────────────────────────────────────────────────────────────

def load_proposals() -> list:
    if not _PENDING_ABANDONS_PATH.exists():
        raise FileNotFoundError(
            "pending_abandons.json not found. No abandon proposals exist yet, "
            "or they've all expired."
        )
    with open(_PENDING_ABANDONS_PATH) as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("pending_abandons.json is malformed — expected a list.")
    return data


def save_proposals(proposals: list) -> None:
    with open(_PENDING_ABANDONS_PATH, 'w') as f:
        json.dump(proposals, f, indent=2)


def find_proposal(proposals: list, abandon_id: int) -> dict:
    """Locate + validate one proposal by ID. Raises ValueError on any problem."""
    matches = [p for p in proposals if p.get('id') == abandon_id]
    if not matches:
        available = [p['id'] for p in proposals]
        raise ValueError(
            f"No abandon proposal with id={abandon_id}. "
            f"Available IDs: {available or 'none'}"
        )
    proposal = matches[0]

    status = proposal.get('status')
    if status != 'pending':
        raise ValueError(
            f"Abandon proposal #{abandon_id} is already {status!r} — "
            "cannot act on it again."
        )

    proposed_at = datetime.fromisoformat(proposal.get('proposed_at', '2000-01-01'))
    age = datetime.now() - proposed_at
    if age > timedelta(hours=ABANDON_EXPIRY_HOURS):
        raise ValueError(
            f"Abandon proposal #{abandon_id} expired "
            f"{age.total_seconds()/3600:.1f}h ago "
            f"(limit {ABANDON_EXPIRY_HOURS}h). Wait for the detector to re-propose."
        )

    return proposal


# ── Execution ────────────────────────────────────────────────────────────────

def execute_abandon(
    proposal: dict,
    trader: PaperTrader,
    *,
    notify=send_telegram_message,
) -> bool:
    """Apply the write-off. Returns True on success.

    Before writing the loss, refetch the market. If it now resolves, raise
    MarketResolvedError — the honest accounting path is the resolution, not
    a synthetic write-off. Same contract as execute_close.
    """
    market_id = proposal['market_id']

    try:
        market = api_client.get_market(market_id)
    except Exception as e:
        print(f"  Warning: could not refetch market {market_id}: {e}")
        market = None

    if market is not None and market.get('isResolved'):
        raise MarketResolvedError(
            f"Market {market_id} has resolved "
            f"(resolution={market.get('resolution')!r}) — "
            "resolve_positions.py will book the real outcome. "
            "Dismiss this abandon proposal."
        )

    reason = proposal.get('reason', 'abandoned by operator')
    loss = trader.mark_market_abandoned(market_id, reason=reason)
    if loss is None:
        print(f"  mark_market_abandoned returned None — nothing to abandon for {market_id}")
        return False

    q = (proposal.get('question') or market_id)[:70]
    total = proposal.get('total_amount') or 0
    try:
        notify(
            f"💀 Abandon #{proposal['id']} executed\n"
            f"\"{q}\"\n"
            f"  Written off ${total:.2f} as realised LOSS (era=write_off)\n"
            f"  New balance: ${trader.balance:.2f}"
        )
    except Exception as e:
        print(f"  Warning: telegram notify failed: {e}")
    return True


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    parser.add_argument('--id', type=int, required=True, help='Abandon proposal ID')
    parser.add_argument('--dismiss', action='store_true',
                        help='Dismiss instead of approve')
    args = parser.parse_args()

    try:
        proposals = load_proposals()
        proposal = find_proposal(proposals, args.id)
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if args.dismiss:
        proposal['status'] = 'dismissed'
        proposal['dismissed_at'] = datetime.now().isoformat()
        save_proposals(proposals)
        print(f"Abandon proposal #{args.id} dismissed.")
        return 0

    trader = PaperTrader()
    try:
        ok = execute_abandon(proposal, trader)
    except MarketResolvedError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    if not ok:
        return 1

    proposal['status'] = 'approved'
    proposal['executed_at'] = datetime.now().isoformat()
    save_proposals(proposals)
    print(f"Abandon proposal #{args.id} executed successfully.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
