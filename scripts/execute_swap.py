#!/usr/bin/env python3
"""
Execute or dismiss a pending position swap by ID.

Reads pending_swaps.json, finds the entry with the given ID, then:
  approve:  closes the weak position, opens the new one, marks status="approved"
  dismiss:  marks status="dismissed" without trading

Usage:
    python3 scripts/execute_swap.py --id 1           # approve swap #1
    python3 scripts/execute_swap.py --id 2 --dismiss # dismiss swap #2

OpenClaw will call this when you reply to the Telegram proposal:
    "approve swap 1"  → python3 scripts/execute_swap.py --id 1
    "dismiss swap 2"  → python3 scripts/execute_swap.py --id 2 --dismiss

The swap expires after SWAP_EXPIRY_HOURS. Running after expiry exits with code 1.
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

# ── Paths ──────────────────────────────────────────────────────────────────────
WORKSPACE = Path(__file__).resolve().parent.parent
PENDING_SWAPS_FILE = WORKSPACE / "pending_swaps.json"

SWAP_EXPIRY_HOURS = 4   # must match position_swap_checker.py


# ── File helpers ───────────────────────────────────────────────────────────────

def load_swaps() -> list:
    if not PENDING_SWAPS_FILE.exists():
        raise FileNotFoundError(
            "pending_swaps.json not found. "
            "The bot hasn't proposed any swaps yet, or all have expired."
        )
    with open(PENDING_SWAPS_FILE) as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("pending_swaps.json is malformed — expected a list.")
    return data


def save_swaps(swaps: list) -> None:
    with open(PENDING_SWAPS_FILE, 'w') as f:
        json.dump(swaps, f, indent=2)


def find_swap(swaps: list, swap_id: int) -> dict:
    """Find and validate a swap by ID. Raises ValueError on any problem."""
    matches = [s for s in swaps if s.get('id') == swap_id]
    if not matches:
        available = [s['id'] for s in swaps]
        raise ValueError(
            f"No swap with id={swap_id}. "
            f"Available IDs: {available or 'none'}"
        )
    swap = matches[0]

    status = swap.get('status')
    if status != 'pending':
        raise ValueError(
            f"Swap #{swap_id} is already {status!r} — cannot act on it again."
        )

    proposed_at = datetime.fromisoformat(swap.get('proposed_at', '2000-01-01'))
    age = datetime.now() - proposed_at
    if age > timedelta(hours=SWAP_EXPIRY_HOURS):
        raise ValueError(
            f"Swap #{swap_id} expired {age.total_seconds()/3600:.1f}h ago "
            f"(limit {SWAP_EXPIRY_HOURS}h). Run the checker to get fresh proposals."
        )

    return swap


# ── Execution helpers ──────────────────────────────────────────────────────────

def get_current_prob(market_id: str, fallback: float) -> float:
    try:
        market = api_client.get_market(market_id)
        return float(market.get('probability', fallback))
    except Exception as e:
        print(f"  Warning: could not fetch current prob for {market_id}: {e}")
        return fallback


def execute_swap(swap: dict, trader: PaperTrader) -> bool:
    """Close the weak position and open the new one. Returns True on success."""
    close_id = swap['close_market_id']
    open_id = swap['open_market_id']
    open_rec = swap['open_recommendation']

    # ── 1. Close weak position ────────────────────────────────────────────────
    print(f"\n[1/2] Closing position: {close_id}")
    print(f"      {swap['close_question'][:80]}")
    current_close_prob = get_current_prob(close_id, swap['close_current_probability'])
    pnl = trader.close_position_early(close_id, current_close_prob)

    if pnl is None:
        print("  No open positions found to close — aborting swap.")
        return False
    print(f"  Closed. Realised P&L: ${pnl:+.2f}")

    # ── 2. Open new position ──────────────────────────────────────────────────
    print(f"\n[2/2] Opening new position: {open_id}")
    print(f"      {swap['open_question'][:80]}")

    current_open_prob = get_current_prob(open_id, open_rec.get('probability', 0.5))
    outcome = open_rec.get('recommendation')
    confidence = open_rec.get('confidence', 0)
    estimated_ev = open_rec.get('estimated_ev')
    ai_confidence = open_rec.get('ai_confidence', confidence)
    strategies = swap.get('open_strategies', open_rec.get('strategies', []))

    from manifold_bot.config import MIN_BET_AMOUNT, MAX_BET_AMOUNT, MIN_CONFIDENCE
    base_size = trader.balance * 0.10
    confidence_multiplier = min(confidence / max(MIN_CONFIDENCE, 0.01), 2.0)
    raw_size = base_size * confidence_multiplier
    amount = round(min(max(raw_size, MIN_BET_AMOUNT), MAX_BET_AMOUNT) / 5) * 5
    amount = max(amount, MIN_BET_AMOUNT)

    if outcome not in ('YES', 'NO'):
        print(f"  Invalid outcome {outcome!r} — aborting.")
        return False

    success = trader.place_paper_bet(
        market_id=open_id,
        outcome=outcome,
        amount=amount,
        probability=current_open_prob,
        estimated_ev=estimated_ev,
        ai_confidence=ai_confidence,
        strategies=strategies,
    )

    if not success:
        print("  Failed to place new bet — position closed but no new trade opened.")
        return False

    print(f"  Opened {outcome} ${amount:.0f} @ {current_open_prob:.1%}")
    return True


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Execute or dismiss a pending swap")
    parser.add_argument('--id', type=int, required=True, help="Swap ID to act on")
    parser.add_argument('--dismiss', action='store_true', help="Dismiss instead of execute")
    args = parser.parse_args()

    print("=" * 60)
    print(f"{'DISMISS' if args.dismiss else 'EXECUTE'} POSITION SWAP #{args.id}")
    print("=" * 60)

    try:
        swaps = load_swaps()
        swap = find_swap(swaps, args.id)
    except (FileNotFoundError, ValueError) as e:
        print(f"Cannot act on swap: {e}")
        return 1

    print(f"\nProposal #{swap['id']} (proposed {swap['proposed_at']})")
    print(f"Close: {swap['close_outcome']} on \"{swap['close_question'][:60]}\"")
    print(f"  Entered {swap.get('close_entry_probability', 0):.0%}, "
          f"unrealised ${swap.get('close_unrealised_pnl', 0):+.2f}")
    print(f"Open:  {swap['open_recommendation'].get('recommendation')} "
          f"on \"{swap['open_question'][:60]}\"")
    print(f"  AI confidence {swap['open_recommendation'].get('confidence', 0):.0%}, "
          f"EV +${swap['open_recommendation'].get('estimated_ev', 0):.2f}")

    # ── Dismiss path ──────────────────────────────────────────────────────────
    if args.dismiss:
        swap['status'] = 'dismissed'
        swap['executed_at'] = datetime.now().isoformat()
        save_swaps(swaps)
        msg = (
            f"🚫 Swap #{swap['id']} dismissed.\n"
            f"Kept: {swap['close_outcome']} on \"{swap['close_question'][:60]}\""
        )
        send_telegram_message(msg)
        print(f"\nSwap #{swap['id']} dismissed.")
        return 0

    # ── Approve path ──────────────────────────────────────────────────────────
    trader = PaperTrader()
    success = execute_swap(swap, trader)

    if success:
        swap['status'] = 'approved'
        swap['executed_at'] = datetime.now().isoformat()
        save_swaps(swaps)
        msg = (
            f"✅ Swap #{swap['id']} executed.\n"
            f"Closed: {swap['close_outcome']} on \"{swap['close_question'][:60]}\"\n"
            f"  Realised ${swap.get('close_unrealised_pnl', 0):+.2f} on close\n"
            f"Opened: {swap['open_recommendation'].get('recommendation')} "
            f"on \"{swap['open_question'][:60]}\"\n"
            f"  Balance: ${trader.balance:.2f}"
        )
        send_telegram_message(msg)
        print(f"\nSwap #{swap['id']} complete.")
        return 0
    else:
        swap['status'] = 'failed'
        swap['executed_at'] = datetime.now().isoformat()
        save_swaps(swaps)
        print(f"\nSwap #{swap['id']} failed. Check output above.")
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"Fatal error: {e}")
        sys.exit(1)
