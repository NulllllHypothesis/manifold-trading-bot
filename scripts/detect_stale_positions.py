#!/usr/bin/env python3
"""
V2 Phase 2.4 — stale position detection (STRANDED / ABANDONED).

For every OPEN position whose market has already closed but not resolved,
classify against two thresholds measured from `closeTime`:

    closeTime + 48h  →  STRANDED  (auto-applied, reversible, no P&L)
    closeTime + 90d  →  ABANDONED (propose-only, terminal write-off)

STRANDED is a slot-liberation event, not a close. The position's amount
stays on record; it's just excluded from slot accounting and unrealised-P&L
rollups. If the creator eventually resolves the market, resolve_positions
picks up STRANDED positions too and reconciles them to WIN/LOSS with real
P&L (see manifold_bot.paper_trader.resolve_market*: the status filter was
widened to `('OPEN', 'STRANDED')`).

ABANDONED is terminal: `profit = -amount` booked, `status='ABANDONED'`,
a row written to bet_outcomes with era='write_off'. Because this realises
a real-dollar loss, the operator approves each one via execute_abandon.py
(mirrors the Phase 2.3 propose/approve UX).

Scope of this phase
-------------------
**In scope**: the two past-close branches (STRANDED auto, ABANDONED propose).
**Out of scope**:
  - "Position still tradable but old and losing" — already handled hourly
    by Phase 2.3's evaluator via position_score. No need to duplicate.
  - Creator-activity signal (lastActive > 30d) — secondary; defer.
  - Dashboard sections for STRANDED/ABANDONED — Phase 2.5.

Schedule: daily at 13:00 UTC. This signal moves slowly — a market doesn't
cross the 48h grace or 90d timeout more than once a day per position.

Exit codes:
  0 — ran cleanly
  1 — unrecoverable error (state corrupt, etc.)
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from manifold_bot.config import STALE_GRACE_HOURS, STALE_ABANDON_DAYS
from manifold_bot.manifold_api import api_client
from automation.send_telegram import send_telegram_message


_STATE_PATH           = _ROOT / "manifold_bot" / "paper_trading_state.json"
_PENDING_ABANDONS_PATH = _ROOT / "pending_abandons.json"

# Abandon proposals live longer than close proposals (4h) because the
# operator may want to see a few days of evidence that the creator isn't
# coming back before approving a terminal write-off.
ABANDON_EXPIRY_HOURS = 72


# ── Classification ───────────────────────────────────────────────────────────

def classify_stale(
    market: Optional[Dict],
    *,
    _now_ms: Optional[float] = None,
    grace_hours: float = STALE_GRACE_HOURS,
    abandon_days: float = STALE_ABANDON_DAYS,
) -> Tuple[str, str]:
    """Classify a single market into one of four states. Pure function.

    Returns (action, reason):
      - 'SKIP_RESOLVED' / 'market already resolved' — resolve_positions handles
      - 'SKIP_NOT_CLOSED' / 'market still open'     — normal OPEN, Phase 2.3 scores
      - 'SKIP_GRACE'     / 'within 48h grace'       — creator often late, hold
      - 'STRANDED'       / 'past close + grace'     — auto-apply
      - 'ABANDON'        / 'past close + 90d'       — propose write-off
      - 'SKIP_NO_CLOSETIME' / 'closeTime missing'   — can't classify; hold

    If `market` is None (API failure), returns SKIP_NO_CLOSETIME so the
    caller holds this position for the next cron cycle rather than acting
    on stale data.
    """
    if market is None:
        return ('SKIP_NO_CLOSETIME', 'api_fetch_failed')

    if market.get('isResolved'):
        return ('SKIP_RESOLVED', 'resolve_positions_will_handle')

    close_time_ms = market.get('closeTime')
    if not isinstance(close_time_ms, (int, float)) or close_time_ms <= 0:
        return ('SKIP_NO_CLOSETIME', 'market_has_no_closetime')

    now_ms = _now_ms if _now_ms is not None else datetime.now(timezone.utc).timestamp() * 1000
    hours_past_close = (now_ms - close_time_ms) / 3_600_000

    if hours_past_close < 0:
        return ('SKIP_NOT_CLOSED', 'market_still_open')
    if hours_past_close < grace_hours:
        return ('SKIP_GRACE', f'{hours_past_close:.1f}h past close (grace {grace_hours}h)')

    days_past_close = hours_past_close / 24
    if days_past_close >= abandon_days:
        return ('ABANDON', f'{days_past_close:.1f}d past close (timeout {abandon_days}d)')
    return ('STRANDED', f'{days_past_close:.1f}d past close')


# ── State / proposal I/O ─────────────────────────────────────────────────────

def _load_state() -> Optional[Dict]:
    if not _STATE_PATH.exists():
        return None
    with open(_STATE_PATH) as f:
        return json.load(f)


def _save_state_atomic(state: Dict) -> None:
    """Atomic write via temp + os.replace. Preserves prior file on crash."""
    tmp_path = _STATE_PATH.with_suffix('.json.tmp')
    with open(tmp_path, 'w') as f:
        json.dump(state, f, indent=2)
    os.replace(tmp_path, _STATE_PATH)


def _load_pending_abandons() -> List[Dict]:
    if not _PENDING_ABANDONS_PATH.exists():
        return []
    try:
        with open(_PENDING_ABANDONS_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_pending_abandons(proposals: List[Dict]) -> None:
    with open(_PENDING_ABANDONS_PATH, 'w') as f:
        json.dump(proposals, f, indent=2)


def _iter_open_markets(state: Dict):
    """Yield (market_id, [open_legs]) for every market with ≥1 OPEN leg.

    STRANDED legs are NOT iterated — they've already been handled in a
    previous run. They're reconciled via resolve_positions, not here.
    """
    positions_by_market = state.get('positions', {}) or {}
    for market_id, trades in positions_by_market.items():
        if not isinstance(trades, list):
            continue
        open_legs = [t for t in trades if isinstance(t, dict) and t.get('status') == 'OPEN']
        if open_legs:
            yield market_id, open_legs


def _existing_pending_abandon_ids(pending: List[Dict]) -> set:
    """Market IDs that already have a non-expired pending abandon proposal."""
    cutoff = datetime.now() - timedelta(hours=ABANDON_EXPIRY_HOURS)
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


def _auto_expire_old_abandons(pending: List[Dict]) -> bool:
    cutoff = datetime.now() - timedelta(hours=ABANDON_EXPIRY_HOURS)
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


# ── State mutations ──────────────────────────────────────────────────────────

def _mark_legs_stranded(legs: List[Dict], now_iso: str, reason: str) -> int:
    """Flip each OPEN leg to STRANDED in-place. Returns count of mutated legs."""
    n = 0
    for leg in legs:
        if leg.get('status') != 'OPEN':
            continue
        leg['status'] = 'STRANDED'
        leg['stranded_at'] = now_iso
        leg['stranded_reason'] = reason
        n += 1
    return n


def _build_abandon_proposal(
    abandon_id: int,
    market_id: str,
    legs: List[Dict],
    reason: str,
) -> Dict:
    """Build a proposal for a market-level write-off.

    Proposal carries per-leg trade_ids + the total amount at stake so the
    operator sees the full loss on the Telegram CTA. execute_abandon.py
    applies the write-off to every OPEN leg in the market on approval.
    """
    total_amount = sum(float(leg.get('amount') or 0) for leg in legs)
    trade_ids = [leg.get('trade_id') for leg in legs if leg.get('status') == 'OPEN']
    first = legs[0] if legs else {}
    return {
        'id': abandon_id,
        'market_id': market_id,
        'trade_ids': trade_ids,
        'question': first.get('question') or market_id,
        'outcome': first.get('outcome'),
        'total_amount': total_amount,
        'reason': reason,
        'proposed_at': datetime.now().isoformat(),
        'status': 'pending',
    }


def _format_abandon_telegram(proposal: Dict) -> str:
    aid = proposal['id']
    q = (proposal.get('question') or proposal['market_id'])[:70]
    total = proposal.get('total_amount') or 0
    n_legs = len(proposal.get('trade_ids') or []) or 1
    legs_tag = f" ({n_legs} legs)" if n_legs > 1 else ""
    return (
        f"💀 Abandon Proposal #{aid}{legs_tag}\n"
        f"\"{q}\"\n"
        f"  Status: {proposal.get('reason', '')}\n"
        f"  Would write off ${total:.2f} as realised LOSS\n"
        "\n"
        f"Reply \"approve abandon {aid}\" to book the loss.\n"
        f"Reply \"dismiss abandon {aid}\" to wait longer.\n"
        f"(Expires in {ABANDON_EXPIRY_HOURS}h)"
    )


# ── Main flow ────────────────────────────────────────────────────────────────

def run_detector(
    *,
    fetch_market=None,
    notify=send_telegram_message,
    verbose: bool = False,
) -> Dict[str, int]:
    """Walk every market with OPEN legs, classify, mark or propose.

    Returns counts:
      { 'open': N, 'grace': N, 'stranded': N, 'abandon_proposed': N,
        'abandon_skipped_already_pending': N, 'api_failed': N, 'resolved': N }

    fetch_market is injectable for tests. State file writes are atomic.
    """
    if fetch_market is None:
        fetch_market = api_client.get_market

    state = _load_state()
    if state is None:
        if verbose:
            print(f"No state file at {_STATE_PATH} — nothing to detect.")
        return {'open': 0, 'grace': 0, 'stranded': 0,
                'abandon_proposed': 0, 'abandon_skipped_already_pending': 0,
                'api_failed': 0, 'resolved': 0}

    pending = _load_pending_abandons()
    if _auto_expire_old_abandons(pending):
        _save_pending_abandons(pending)
    already_proposed = _existing_pending_abandon_ids(pending)
    next_id = max((p.get('id', 0) for p in pending), default=0) + 1

    counts = {'open': 0, 'grace': 0, 'stranded': 0,
              'abandon_proposed': 0, 'abandon_skipped_already_pending': 0,
              'api_failed': 0, 'resolved': 0}

    now_iso = datetime.now(timezone.utc).isoformat()
    state_mutated = False
    new_abandon_proposals: List[Dict] = []

    for market_id, legs in _iter_open_markets(state):
        try:
            market = fetch_market(market_id)
        except Exception as e:
            if verbose:
                print(f"  [{market_id[:12]}] fetch failed: {e}")
            counts['api_failed'] += 1
            continue

        action, reason = classify_stale(market)

        if action == 'SKIP_NOT_CLOSED':
            counts['open'] += 1
            if verbose:
                print(f"  [{market_id[:12]}] OPEN ({reason})")
        elif action == 'SKIP_GRACE':
            counts['grace'] += 1
            if verbose:
                print(f"  [{market_id[:12]}] GRACE ({reason})")
        elif action == 'SKIP_RESOLVED':
            counts['resolved'] += 1
            if verbose:
                print(f"  [{market_id[:12]}] RESOLVED on API — resolve_positions will handle")
        elif action == 'SKIP_NO_CLOSETIME':
            # No usable closeTime. Treat as hold — can't classify honestly.
            counts['api_failed'] += 1
            if verbose:
                print(f"  [{market_id[:12]}] no-closetime ({reason})")
        elif action == 'STRANDED':
            n = _mark_legs_stranded(legs, now_iso, reason)
            counts['stranded'] += n
            state_mutated = True
            if verbose:
                print(f"  [{market_id[:12]}] STRANDED ({reason}) — {n} leg(s) marked")
        elif action == 'ABANDON':
            if market_id in already_proposed:
                counts['abandon_skipped_already_pending'] += 1
                if verbose:
                    print(f"  [{market_id[:12]}] ABANDON — already pending proposal, skip")
                continue
            proposal = _build_abandon_proposal(next_id, market_id, legs, reason)
            new_abandon_proposals.append(proposal)
            next_id += 1
            counts['abandon_proposed'] += 1
            if verbose:
                print(f"  [{market_id[:12]}] ABANDON proposed #{proposal['id']} ({reason})")

    if state_mutated:
        _save_state_atomic(state)

    if new_abandon_proposals:
        pending.extend(new_abandon_proposals)
        _save_pending_abandons(pending)
        for p in new_abandon_proposals:
            try:
                notify(_format_abandon_telegram(p))
            except Exception as e:
                print(f"[detect_stale_positions] warning: telegram send failed: {e}",
                      file=sys.stderr)

    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    parser.add_argument('--verbose', '-v', action='store_true', help='Per-market log')
    args = parser.parse_args()

    print(f"==> Detecting stale positions at {datetime.now(timezone.utc).isoformat()}")
    try:
        counts = run_detector(verbose=args.verbose)
    except Exception as e:
        print(f"[detect_stale_positions] unrecoverable error: {e}", file=sys.stderr)
        return 1

    print(f"    Still OPEN:           {counts['open']}")
    print(f"    In 48h grace:         {counts['grace']}")
    print(f"    Marked STRANDED:      {counts['stranded']}")
    print(f"    ABANDON proposed:     {counts['abandon_proposed']}")
    print(f"    ABANDON already pend: {counts['abandon_skipped_already_pending']}")
    print(f"    Resolved (deferred):  {counts['resolved']}")
    print(f"    API failed:           {counts['api_failed']}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
