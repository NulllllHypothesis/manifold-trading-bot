#!/usr/bin/env python3
"""
V2 Phase 2.3 — position evaluator.

For every OPEN position in paper_trading_state.json, compute a position_score
using the Phase 2.2 repricing data, then classify the position as HOLD or
CLOSE. Emit CLOSE proposals to pending_closes.json (one per losing position)
and notify Telegram with an "approve close N" CTA.

**Scope of this phase: propose only.** Auto-execution is deliberately off —
evaluator never mutates paper_trading_state.json. A human approves via
`python3 scripts/execute_close.py --id N` (or dismisses).

Why a separate propose/execute split
------------------------------------
1. CLOSE_SCORE_THRESHOLD, DAILY_DECAY_COST, and LONG_HORIZON_PENALTY are
   starting guesses. We want to see which positions the evaluator flags
   against real outcomes before we let the machine act on them.
2. `position_swap_checker.py` already proposes swaps the same way when the
   book is full. Keeping the approval UX consistent across close/swap means
   the operator learns one muscle-memory CTA.
3. If the evaluator mis-scores, the blast radius is a Telegram ping, not a
   realized loss.

Run hourly from cron at :30. Produces:

1. `pending_closes.json` — list of unresolved close proposals
2. Telegram message per new proposal

Exit codes:
  0 — ran cleanly (regardless of proposals generated)
  1 — unrecoverable error (state file corrupt, etc.)
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from manifold_bot.config import (
    DAILY_DECAY_COST,
    LONG_HORIZON_PENALTY,
    CLOSE_SCORE_THRESHOLD,
)
from automation.send_telegram import send_telegram_message


_STATE_PATH         = _ROOT / "manifold_bot" / "paper_trading_state.json"
_PENDING_CLOSES_PATH = _ROOT / "pending_closes.json"

# Match the 4h expiry used by position_swap_checker for consistency.
CLOSE_EXPIRY_HOURS = 4

# A repricing snapshot older than this is stale. Phase 2.2 runs hourly at
# :05, so any live position should be ≤1h stale in normal operation; 2h is
# one missed cycle plus slack. If Phase 2.2 has missed multiple cycles we'd
# rather HOLD with reason 'stale_reprice' than emit CLOSE proposals based on
# prices that are a day old. Phase 2.2 leaves previous repricing fields on
# state when a fetch fails, so last_repriced_at freshness is the only way
# to tell "I have fresh data" from "I have stale data".
REPRICE_STALE_HOURS = 2


# ── Scoring ──────────────────────────────────────────────────────────────────

def position_score(
    unrealised_pnl: Optional[float],
    days_held: Optional[float],
    position_class: Optional[str],
    daily_decay_cost: float = DAILY_DECAY_COST,
    long_horizon_penalty: float = LONG_HORIZON_PENALTY,
) -> Optional[float]:
    """
    Compute the close-decision score for one position.

        score = unrealised_pnl
                - days_held × daily_decay_cost
                - (long_or_uncertain ? long_horizon_penalty : 0)

    Returns None if either unrealised_pnl or days_held is unknown — the
    evaluator treats None as "not enough data yet, hold" rather than forcing
    a score of 0 (which would be indistinguishable from a truly-flat position).

    Pure function. No I/O. Default cost parameters pulled from config so
    tests can override without patching the module.
    """
    if unrealised_pnl is None or days_held is None:
        return None

    score = float(unrealised_pnl) - float(days_held) * float(daily_decay_cost)
    if position_class == 'long_or_uncertain':
        score -= float(long_horizon_penalty)
    return round(score, 4)


def classify_position(
    position: Dict,
    threshold: float = CLOSE_SCORE_THRESHOLD,
    stale_hours: float = REPRICE_STALE_HOURS,
    _now=None,  # injectable for tests
) -> Tuple[str, Optional[float], str]:
    """
    Classify one position as HOLD / CLOSE given its current repricing state.

    Returns (action, score, reason):
      - action: 'HOLD' or 'CLOSE'
      - score: the computed position_score, or None if insufficient data
      - reason: human-readable short string for the Telegram proposal

    Decision rules (evaluated in order):
      1. is_resolved_on_api=True → HOLD with reason 'awaiting_resolve' —
         resolve_positions.py at :10 handles these; we don't interfere.
      2. No repricing data → HOLD with reason 'no_reprice' (Phase 2.2 hasn't
         touched this position yet — wait one cycle).
      3. last_repriced_at older than stale_hours → HOLD with reason
         'stale_reprice'. Phase 2.2 preserves prior repricing fields on
         fetch failure, so this is the only way to distinguish "fresh data
         says close" from "stale data from a missed cycle says close".
      4. score < threshold → CLOSE with reason summarising why.
      5. Otherwise HOLD.
    """
    if position.get('is_resolved_on_api'):
        return ('HOLD', None, 'awaiting_resolve')

    unrealised = position.get('current_unrealised_pnl')
    days_held = _days_held_from_position(position)
    pclass = position.get('position_class')

    if unrealised is None or days_held is None:
        return ('HOLD', None, 'no_reprice')

    if _is_reprice_stale(position.get('last_repriced_at'), stale_hours, _now=_now):
        return ('HOLD', None, 'stale_reprice')

    score = position_score(unrealised, days_held, pclass)
    if score is None:
        return ('HOLD', None, 'no_reprice')

    if score < threshold:
        return ('CLOSE', score, _close_reason(unrealised, days_held, pclass))
    return ('HOLD', score, 'score_above_threshold')


def _close_reason(unrealised_pnl: float, days_held: float, position_class: Optional[str]) -> str:
    """Short human-readable rationale. Used on the Telegram CTA."""
    parts = [f"pnl=${unrealised_pnl:+.2f}", f"age={days_held:.1f}d"]
    if position_class == 'long_or_uncertain':
        parts.append("long_horizon_penalty")
    return ", ".join(parts)


def _is_reprice_stale(
    last_repriced_at: Optional[str],
    stale_hours: float,
    _now=None,
) -> bool:
    """True if the repricing snapshot is missing or older than stale_hours.

    Missing is treated as stale: a position with no last_repriced_at marker
    has never been priced at all, or we've lost the timestamp — either way
    we shouldn't score it as if we have fresh data. Caller handles "missing
    current_unrealised_pnl" separately upstream via the 'no_reprice' branch.
    """
    if not last_repriced_at:
        return True
    try:
        ts = datetime.fromisoformat(last_repriced_at.replace('Z', '+00:00'))
    except (ValueError, AttributeError):
        return True  # unparseable → treat as stale, never scored as fresh
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    now = _now if _now is not None else datetime.now(timezone.utc)
    return (now - ts) > timedelta(hours=stale_hours)


def _days_held_from_position(position: Dict) -> Optional[float]:
    """
    Prefer the value computed by the last repricing run (it's recorded on
    every snapshot), but fall back to deriving from `timestamp` so a position
    that's been repriced but lost its days_held field still scores.
    """
    # Phase 2.2 snapshot stores days_held in the DB, not on the position record.
    # So we always derive it here from the `timestamp` field.
    entry_ts = position.get('timestamp')
    if not entry_ts:
        return None
    try:
        entry_dt = datetime.fromisoformat(entry_ts.replace('Z', '+00:00'))
    except (ValueError, AttributeError):
        return None
    if entry_dt.tzinfo is None:
        entry_dt = entry_dt.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - entry_dt
    return max(0.0, delta.total_seconds() / 86_400.0)


# ── State / proposal I/O ─────────────────────────────────────────────────────

def _load_state() -> Optional[Dict]:
    if not _STATE_PATH.exists():
        return None
    with open(_STATE_PATH) as f:
        return json.load(f)


def _load_pending_closes() -> List[Dict]:
    """Read pending close proposals. Empty list if missing or corrupt."""
    if not _PENDING_CLOSES_PATH.exists():
        return []
    try:
        with open(_PENDING_CLOSES_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_pending_closes(proposals: List[Dict]) -> None:
    with open(_PENDING_CLOSES_PATH, 'w') as f:
        json.dump(proposals, f, indent=2)


def _iter_open_positions(state: Dict):
    """Yield (market_id, position) for every OPEN position in the state."""
    positions_by_market = state.get('positions', {}) or {}
    for market_id, trades in positions_by_market.items():
        if not isinstance(trades, list):
            continue
        for pos in trades:
            if isinstance(pos, dict) and pos.get('status') == 'OPEN':
                yield market_id, pos


def _existing_pending_ids(pending: List[Dict]) -> set:
    """Market IDs that already have a non-expired, non-dismissed close proposal.

    Matches the expiry/status semantics of position_swap_checker:pending_swaps
    so we don't spam Telegram with the same CLOSE proposal every hour.
    """
    cutoff = datetime.now() - timedelta(hours=CLOSE_EXPIRY_HOURS)
    active = set()
    for p in pending:
        if p.get('status') != 'pending':
            continue
        try:
            proposed_at = datetime.fromisoformat(p.get('proposed_at', '2000-01-01'))
        except ValueError:
            continue
        if proposed_at > cutoff:
            active.add(p.get('market_id'))
    return active


def _auto_expire_old_closes(pending: List[Dict]) -> bool:
    """Mark any pending proposal past its expiry as 'expired'. Returns True if mutated."""
    cutoff = datetime.now() - timedelta(hours=CLOSE_EXPIRY_HOURS)
    changed = False
    for p in pending:
        if p.get('status') != 'pending':
            continue
        try:
            proposed_at = datetime.fromisoformat(p.get('proposed_at', '2000-01-01'))
        except ValueError:
            continue
        if proposed_at < cutoff:
            p['status'] = 'expired'
            changed = True
    return changed


# ── Proposal building + notifications ────────────────────────────────────────

def _build_proposal(close_id: int, market_id: str, pos: Dict, score: float, reason: str) -> Dict:
    return {
        'id': close_id,
        'market_id': market_id,
        'trade_id': pos.get('trade_id'),
        'question': pos.get('question') or market_id,
        'outcome': pos.get('outcome'),
        'amount': pos.get('amount'),
        'entry_probability': pos.get('entry_probability') or pos.get('probability'),
        'current_probability': pos.get('current_probability'),
        'current_unrealised_pnl': pos.get('current_unrealised_pnl'),
        'position_score': score,
        'reason': reason,
        'proposed_at': datetime.now().isoformat(),
        'status': 'pending',
    }


def _format_telegram_message(proposal: Dict) -> str:
    close_id = proposal['id']
    q = (proposal.get('question') or proposal['market_id'])[:70]
    entry = proposal.get('entry_probability') or 0
    now = proposal.get('current_probability') or 0
    pnl = proposal.get('current_unrealised_pnl') or 0
    score = proposal.get('position_score')
    score_str = f"{score:+.2f}" if isinstance(score, (int, float)) else "?"

    return (
        f"🔻 Close Proposal #{close_id}\n"
        f"{proposal['outcome']} on \"{q}\"\n"
        f"  Entered {entry:.0%}, now {now:.0%}, unrealised ${pnl:+.2f}\n"
        f"  Score: {score_str}  ({proposal.get('reason', '')})\n"
        "\n"
        f"Reply \"approve close {close_id}\" to sell at current price.\n"
        f"Reply \"dismiss close {close_id}\" to skip.\n"
        f"(Expires in {CLOSE_EXPIRY_HOURS}h)"
    )


# ── Main flow ────────────────────────────────────────────────────────────────

def run_evaluator(
    *,
    notify=send_telegram_message,
    verbose: bool = False,
) -> Tuple[int, int, int]:
    """
    Evaluate every open position. Returns (n_hold, n_close, n_new_proposals).

    `notify` is injectable so tests can capture Telegram sends without
    requiring a live bot token.
    """
    state = _load_state()
    if state is None:
        if verbose:
            print(f"No state file at {_STATE_PATH} — nothing to evaluate.")
        return (0, 0, 0)

    pending = _load_pending_closes()
    if _auto_expire_old_closes(pending):
        _save_pending_closes(pending)

    already_proposed = _existing_pending_ids(pending)
    next_id = max((p.get('id', 0) for p in pending), default=0) + 1

    new_proposals: List[Dict] = []
    n_hold = 0
    n_close = 0

    # Track market_ids already proposed THIS RUN so a market with multiple
    # OPEN trades doesn't generate duplicate proposals. close_position_early
    # closes every OPEN trade for the market at once, so one proposal per
    # market is sufficient — a second Telegram CTA would be confusing.
    proposed_this_run: set = set()

    for market_id, pos in _iter_open_positions(state):
        action, score, reason = classify_position(pos)

        if action == 'HOLD':
            n_hold += 1
            if verbose:
                s = f"{score:+.2f}" if isinstance(score, (int, float)) else "?"
                print(f"  [{market_id[:12]}] HOLD  score={s}  ({reason})")
            continue

        # action == 'CLOSE'
        n_close += 1
        if market_id in already_proposed:
            if verbose:
                print(f"  [{market_id[:12]}] CLOSE score={score:+.2f} — proposal already pending, skipping")
            continue
        if market_id in proposed_this_run:
            if verbose:
                print(f"  [{market_id[:12]}] CLOSE score={score:+.2f} — already proposed this run, skipping")
            continue

        proposal = _build_proposal(next_id, market_id, pos, score, reason)
        new_proposals.append(proposal)
        proposed_this_run.add(market_id)
        next_id += 1
        if verbose:
            print(f"  [{market_id[:12]}] CLOSE score={score:+.2f} — proposed #{proposal['id']}")

    if new_proposals:
        pending.extend(new_proposals)
        _save_pending_closes(pending)
        for p in new_proposals:
            try:
                notify(_format_telegram_message(p))
            except Exception as e:
                print(f"[evaluate_positions] warning: telegram send failed: {e}", file=sys.stderr)

    return (n_hold, n_close, len(new_proposals))


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    parser.add_argument('--verbose', '-v', action='store_true', help='Print per-position classification')
    args = parser.parse_args()

    print(f"==> Evaluating positions at {datetime.now().isoformat()}")
    try:
        hold, close, new = run_evaluator(verbose=args.verbose)
    except Exception as e:
        print(f"[evaluate_positions] unrecoverable error: {e}", file=sys.stderr)
        return 1

    print(f"    HOLD:         {hold}")
    print(f"    CLOSE flagged:{close}")
    print(f"    New proposals:{new}  (others already pending)")
    return 0


if __name__ == '__main__':
    sys.exit(main())
