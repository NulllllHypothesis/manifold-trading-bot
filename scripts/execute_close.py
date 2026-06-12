#!/usr/bin/env python3
"""
Execute or dismiss a pending early-close proposal by ID.

NOTE: since Phase 2.3b, `evaluate_positions.py` executes its own close
decisions by default — re-validated against a fresh live probability rather
than human-approved — calling `execute_close()` below as a library function.
This CLI remains the manual path — it's used when the evaluator runs in
`--propose-only` mode, or to act on a leftover proposal by hand.

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
from manifold_bot.config import CLOSE_SCORE_THRESHOLD
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

def _fetch_market(market_id: str):
    """Single-call API fetch; returns the raw market dict or None on failure."""
    try:
        return api_client.get_market(market_id)
    except Exception as e:
        print(f"  Warning: could not fetch market {market_id}: {e}")
        return None


def _current_prob_from_market(market, fallback: float) -> float:
    """Extract probability from a fetched market dict, falling back to snapshot.

    Split out from _fetch_market so callers can inspect other fields on the
    same response (is_resolved, resolution) without double-fetching.
    """
    if market is not None:
        prob = market.get('probability')
        if prob is not None:
            return float(prob)
    return float(fallback)


class MarketResolvedError(RuntimeError):
    """Raised when a proposal tries to close a market that already resolved.

    High-severity safety check: between proposal creation (at :30) and human
    approval, a market may genuinely resolve. If we close it ourselves at the
    current probability we'd both (a) mislabel a real resolution as
    CLOSED_EARLY in bet_outcomes, breaking era segmentation, and (b) realise
    the wrong economics for MKT / CANCEL / NO resolutions (the 'probability'
    field at resolution time doesn't reflect the true payout).

    Correct behaviour: bail out and let resolve_positions.py at :10 handle
    the finalization through resolve_market / resolve_market_mkt / cancel.
    """


def execute_close(proposal: dict, trader: PaperTrader, *,
                  notify=send_telegram_message, auto: bool = False) -> bool:
    """Close the position at current AMM price. Returns True on success.

    Refetches the market right before closing to check `isResolved`. If the
    market resolved after the proposal was created, raises MarketResolvedError
    — we must NOT simulate a manual close on a resolved market. The caller
    (main()) converts that into a non-zero exit code and leaves the proposal
    in 'pending' so the operator can dismiss it explicitly.

    auto=True is the evaluator's autonomous path: the Telegram message is a
    notification of an action already taken (with the score breakdown that
    drove it), not a request for permission. auto=False keeps the original
    human-approved wording.
    """
    market_id = proposal['market_id']
    snapshot_prob = proposal.get('current_probability', 0.5) or 0.5

    market = _fetch_market(market_id)
    if market is not None and market.get('isResolved'):
        raise MarketResolvedError(
            f"Market {market_id} has resolved "
            f"(resolution={market.get('resolution')!r}) — "
            "resolve_positions.py at :10 will finalize this. "
            "Dismiss the close proposal instead of approving it."
        )

    current_prob = _current_prob_from_market(market, snapshot_prob)
    print(f"  Closing {market_id} at probability {current_prob:.3f}")

    # Stamp WHY onto the closed trade records so the learning loop can later
    # compare close-time reasoning against the market's eventual resolution.
    # 'revalidation' records which Phase 2.3b approval rule fired
    # ('immediate_clear_margin' | 'persisted_next_cycle'); None for manual
    # approvals and pre-revalidation proposals.
    revalidation = proposal.get('revalidation') or {}
    close_context = {
        'close_type': 'auto' if auto else 'manual_approval',
        'position_score': proposal.get('position_score'),
        'close_reason': proposal.get('reason'),
        'score_breakdown': proposal.get('score_breakdown'),
        'closed_at_probability': current_prob,
        'revalidation': revalidation.get('rule'),
        'revalidation_fresh_score': revalidation.get('fresh_score'),
    }
    pnl = trader.close_position_early(market_id, current_prob,
                                      close_context=close_context)
    if pnl is None:
        print(f"  close_position_early returned None — nothing to close for {market_id}")
        return False

    q = (proposal.get('question') or market_id)[:70]
    score = proposal.get('position_score')
    score_str = f"{score:+.2f}" if isinstance(score, (int, float)) else "?"
    if auto:
        header = f"🤖 Auto-closed #{proposal['id']}"
        why = (f"  Why: score {score_str} < threshold {CLOSE_SCORE_THRESHOLD:+.2f} "
               f"({proposal.get('reason', '')})\n")
        if revalidation.get('rule'):
            fs = revalidation.get('fresh_score')
            fs_str = f"{fs:+.2f}" if isinstance(fs, (int, float)) else "?"
            why += f"  Re-validated live: {revalidation['rule']} (fresh score {fs_str})\n"
    else:
        header = f"✅ Close #{proposal['id']} executed"
        why = f"  Score: {score_str}  ({proposal.get('reason', '')})\n"
    try:
        notify(
            f"{header}\n"
            f"{proposal.get('outcome')} on \"{q}\"\n"
            f"  Realised ${pnl:+.2f} at {current_prob:.0%}\n"
            f"{why}"
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
    try:
        ok = execute_close(proposal, trader)
    except MarketResolvedError as e:
        # Leave proposal as 'pending' — operator should dismiss it explicitly
        # once they understand why, or just wait for resolve_positions.py to
        # finalize the market and the proposal to auto-expire.
        print(f"Error: {e}", file=sys.stderr)
        return 1
    if not ok:
        return 1

    proposal['status'] = 'approved'
    proposal['executed_at'] = datetime.now().isoformat()
    save_proposals(proposals)
    print(f"Close proposal #{args.id} executed successfully.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
