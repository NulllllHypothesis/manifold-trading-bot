#!/usr/bin/env python3
"""
Execute or dismiss a pending early-close proposal by ID.

Reads pending_closes.json, finds the entry with the given ID, then:
  approve:  closes the position at current AMM price, marks status="approved"
  dismiss:  marks status="dismissed" without trading

Usage:
    python3 scripts/execute_close.py --id 1            # approve close #1
    python3 scripts/execute_close.py --id 2 --dismiss  # dismiss close #2

OpenClaw will call this when the operator replies to the Telegram proposal:
    "approve close 1"  → python3 scripts/execute_close.py --id 1
    "dismiss close 2"  → python3 scripts/execute_close.py --id 2 --dismiss

Unlike `execute_swap.py`, this does not open a new position — it only realises
the loss (or profit) on an existing one, freeing the slot.

The proposal expires after CLOSE_EXPIRY_HOURS. Running after expiry exits 1.
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
_PENDING_CLOSES_PATH = _ROOT / "pending_closes.json"

# Must match evaluate_positions.CLOSE_EXPIRY_HOURS — the proposer and the
# approver both need the same cutoff or we get inconsistent "expired"
# errors depending on which side you read.
CLOSE_EXPIRY_HOURS = 4


# ── File helpers ─────────────────────────────────────────────────────────────

def load_proposals() -> list:
    if not _PENDING_CLOSES_PATH.exists():
        raise FileNotFoundError(
            "pending_closes.json not found. No close proposals exist yet, "
            "or they've all expired."
        )
    with open(_PENDING_CLOSES_PATH) as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("pending_closes.json is malformed — expected a list.")
    return data


def save_proposals(proposals: list) -> None:
    with open(_PENDING_CLOSES_PATH, 'w') as f:
        json.dump(proposals, f, indent=2)


def find_proposal(proposals: list, close_id: int) -> dict:
    """Locate + validate one proposal by ID. Raises ValueError on any problem."""
    matches = [p for p in proposals if p.get('id') == close_id]
    if not matches:
        available = [p['id'] for p in proposals]
        raise ValueError(
            f"No close proposal with id={close_id}. "
            f"Available IDs: {available or 'none'}"
        )
    proposal = matches[0]

    status = proposal.get('status')
    if status != 'pending':
        raise ValueError(
            f"Close proposal #{close_id} is already {status!r} — "
            "cannot act on it again."
        )

    proposed_at = datetime.fromisoformat(proposal.get('proposed_at', '2000-01-01'))
    age = datetime.now() - proposed_at
    if age > timedelta(hours=CLOSE_EXPIRY_HOURS):
        raise ValueError(
            f"Close proposal #{close_id} expired {age.total_seconds()/3600:.1f}h ago "
            f"(limit {CLOSE_EXPIRY_HOURS}h). Wait for the evaluator to re-propose."
        )

    return proposal


# ── Execution ────────────────────────────────────────────────────────────────

def _current_prob(market_id: str, fallback: float) -> float:
    """Fetch live probability; fall back to the snapshot price if API fails.

    The fallback keeps the approval flow usable even when the Manifold API
    is briefly down — the operator already saw the snapshot price in the
    Telegram proposal, so closing at that price is reasonable.
    """
    try:
        market = api_client.get_market(market_id)
        prob = market.get('probability')
        if prob is not None:
            return float(prob)
    except Exception as e:
        print(f"  Warning: could not fetch current prob for {market_id}: {e}")
    return float(fallback)


def execute_close(proposal: dict, trader: PaperTrader, *, notify=send_telegram_message) -> bool:
    """Close the position at current AMM price. Returns True on success."""
    market_id = proposal['market_id']
    snapshot_prob = proposal.get('current_probability', 0.5) or 0.5

    current_prob = _current_prob(market_id, snapshot_prob)
    print(f"  Closing {market_id} at probability {current_prob:.3f}")

    pnl = trader.close_position_early(market_id, current_prob)
    if pnl is None:
        print(f"  close_position_early returned None — nothing to close for {market_id}")
        return False

    q = (proposal.get('question') or market_id)[:70]
    try:
        notify(
            f"✅ Close #{proposal['id']} executed\n"
            f"{proposal.get('outcome')} on \"{q}\"\n"
            f"  Realised ${pnl:+.2f} at {current_prob:.0%}\n"
            f"  New balance: ${trader.balance:.2f}"
        )
    except Exception as e:
        print(f"  Warning: telegram notify failed: {e}")
    return True


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    parser.add_argument('--id', type=int, required=True, help='Close proposal ID')
    parser.add_argument('--dismiss', action='store_true', help='Dismiss instead of approve')
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
        print(f"Close proposal #{args.id} dismissed.")
        return 0

    trader = PaperTrader()
    ok = execute_close(proposal, trader)
    if not ok:
        return 1

    proposal['status'] = 'approved'
    proposal['executed_at'] = datetime.now().isoformat()
    save_proposals(proposals)
    print(f"Close proposal #{args.id} executed successfully.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
