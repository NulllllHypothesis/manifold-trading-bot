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
    MIN_HOLD_HOURS,
)
from automation.send_telegram import send_telegram_message
# Single source of truth for the 4-bucket → 3-bucket migration map. Importing
# rather than duplicating so a future change in one place propagates here.
from automation.auto_trader import _normalize_position_class


_STATE_PATH         = _ROOT / "manifold_bot" / "paper_trading_state.json"
_PENDING_CLOSES_PATH = _ROOT / "pending_closes.json"

# Threshold boundaries for deriving CURRENT term from close_time_ms —
# identical to the values used at entry time by Phase 2.1's _classify_term,
# but applied at evaluator run-time so a market that was "long" at entry
# is re-classified as "short" when closeTime is within a week.
_SHORT_TERM_DAYS  = 7
_MEDIUM_TERM_DAYS = 30

# Match the 4h expiry used by position_swap_checker for consistency.
CLOSE_EXPIRY_HOURS = 4

# Dismissal cooldown — once the operator dismisses a close proposal, suppress
# any further proposal on that market for this many hours. Without it the
# evaluator re-generates the same proposal every hour because statistical
# scores drift ~0 between runs, spamming Telegram.
DISMISS_COOLDOWN_HOURS = 48

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
    min_hold_hours: float = MIN_HOLD_HOURS,
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
      4. YOUNGEST-leg age < min_hold_hours → HOLD with reason 'too_new'.
         Uses the youngest leg of a market aggregate (not the weighted
         average) because close_position_early closes every leg at once —
         if any one leg is still within its minimum hold window the whole
         market must be held. Prevents the rounding-artifact pathology on
         brand-new long_or_uncertain trades (flat pnl + long_horizon_penalty
         lands exactly on the threshold, and fractional time-decay sneaks
         it below `<`), and also prevents a stale older leg from dragging
         the weighted average over 2h while a fresh sibling leg still
         needs protection.
      5. score < threshold → CLOSE with reason summarising why.
      6. Otherwise HOLD.
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

    # The too_new guard uses the YOUNGEST leg's age for market aggregates
    # (close_position_early closes every leg together — one fresh leg
    # inside its minimum hold window protects the whole market from a
    # CLOSE proposal). For single-leg positions the aggregate doesn't
    # stamp _min_leg_days_held, so we fall back to days_held — which for
    # one leg equals that leg's own age anyway.
    min_leg = position.get('_min_leg_days_held')
    age_for_too_new = min_leg if isinstance(min_leg, (int, float)) else days_held
    if age_for_too_new * 24.0 < float(min_hold_hours):
        return ('HOLD', None, 'too_new')

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
    Prefer the amount-weighted aggregate days_held when a market aggregate
    supplies it (`_aggregate_days_held`). Otherwise derive from `timestamp`.
    """
    # Market aggregates precompute amount-weighted days — honour that
    # instead of re-deriving from the oldest-leg timestamp, which would
    # overstate the age of the capital in the market.
    agg = position.get('_aggregate_days_held')
    if agg is not None:
        return float(agg)

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
    """Yield (market_id, position) for every OPEN position in the state.

    Kept for backwards compatibility with older call sites. The evaluator now
    uses _iter_open_markets instead so it scores at market level — see the
    docstring on _aggregate_market_legs for why.
    """
    positions_by_market = state.get('positions', {}) or {}
    for market_id, trades in positions_by_market.items():
        if not isinstance(trades, list):
            continue
        for pos in trades:
            if isinstance(pos, dict) and pos.get('status') == 'OPEN':
                yield market_id, pos


def _iter_open_markets(state: Dict):
    """Yield (market_id, [open_legs]) for every market with ≥1 OPEN leg.

    A single market can hold multiple OPEN trades (legacy averaging-in,
    position swaps that re-added on the same side). Close execution happens
    at market granularity — `PaperTrader.close_position_early` closes ALL
    OPEN legs in the given market in one call — so close DECISIONS must
    also be made at market granularity. Scoring legs individually then
    deduping at proposal time is unsafe because a single losing leg in an
    otherwise profitable market would still emit a proposal that, once
    approved, liquidates the whole market including the winning legs.
    """
    positions_by_market = state.get('positions', {}) or {}
    for market_id, trades in positions_by_market.items():
        if not isinstance(trades, list):
            continue
        open_legs = [t for t in trades if isinstance(t, dict) and t.get('status') == 'OPEN']
        if open_legs:
            yield market_id, open_legs


def _aggregate_market_legs(market_id: str, legs: List[Dict]) -> Dict:
    """Collapse multiple OPEN legs into one market-level position dict.

    Aggregation rules:
      - current_unrealised_pnl  → sum across legs (what we'd realise if we
                                   closed the whole market right now)
      - amount                  → sum across legs (total capital at stake)
      - days_held               → amount-weighted average (represents average
                                   capital age; matches the decay model)
      - entry_probability       → amount-weighted average (display only; the
                                   proposal shows this so the operator can
                                   compare entry vs current)
      - current_probability     → first leg's value (identical across legs
                                   for the same market)
      - outcome                 → first leg's value if all match; 'MIXED'
                                   otherwise (rare but possible: YES on one
                                   leg, NO on another)
      - position_class          → first leg's value (identical by definition)
      - question                → first leg's value
      - is_resolved_on_api      → True if ANY leg is flagged (conservative:
                                   if even one leg saw a resolution, defer)
      - last_repriced_at        → OLDEST timestamp across legs. If the
                                   oldest leg's reprice is stale, we don't
                                   trust the aggregate.
      - timestamp               → OLDEST timestamp (used by the fallback
                                   days_held calculator).

    Single-leg markets pass through with equivalent values — the aggregate
    of one leg equals that leg.
    """
    total_pnl = 0.0
    total_amount = 0.0
    any_pnl = False
    any_amount = False
    weighted_days = 0.0
    weighted_entry = 0.0
    weighted_base = 0.0  # denominator for weighted averages (sum of amounts with known values)
    min_leg_days: Optional[float] = None  # youngest leg; drives the too_new guard
    outcomes = set()
    oldest_repriced: Optional[datetime] = None
    oldest_timestamp: Optional[datetime] = None
    any_resolved = False
    question = None
    current_probability = None

    # Derive a deterministic, order-independent position_class from
    # close_time_ms (preferred) or a conservative reducer on persisted
    # per-leg values. See _derive_aggregate_position_class for rationale.
    position_class = _derive_aggregate_position_class(legs)

    for leg in legs:
        # Sum pnl where present. Missing pnl on one leg poisons the
        # aggregate — we can't honestly score a market when any leg lacks
        # fresh data. Caller handles the `no_reprice` branch via a
        # separate check after aggregation.
        pnl = leg.get('current_unrealised_pnl')
        if pnl is not None:
            total_pnl += float(pnl)
            any_pnl = True

        amount = leg.get('amount') or 0.0
        if amount:
            total_amount += float(amount)
            any_amount = True

        leg_days = _days_held_from_position(leg)
        if leg_days is not None and amount:
            weighted_days += leg_days * float(amount)
            weighted_base += float(amount)
        # Track the youngest leg across the market for the too_new guard.
        # close_position_early closes EVERY OPEN leg in one call, so if any
        # single leg is still within its minimum hold window the whole
        # market must be treated as too_new — weighted age can mask a
        # fresh leg behind an older/larger sibling.
        if leg_days is not None:
            if min_leg_days is None or leg_days < min_leg_days:
                min_leg_days = leg_days

        entry_p = leg.get('entry_probability') or leg.get('probability')
        if entry_p is not None and amount:
            weighted_entry += float(entry_p) * float(amount)

        outcome = leg.get('outcome')
        if outcome:
            outcomes.add(outcome)

        last_ts = _parse_iso(leg.get('last_repriced_at'))
        if last_ts is not None:
            oldest_repriced = last_ts if oldest_repriced is None else min(oldest_repriced, last_ts)
        else:
            # Any leg missing a reprice timestamp must force stale — use a
            # sentinel far in the past so the staleness gate fires.
            oldest_repriced = datetime.min.replace(tzinfo=timezone.utc)

        open_ts = _parse_iso(leg.get('timestamp'))
        if open_ts is not None:
            oldest_timestamp = open_ts if oldest_timestamp is None else min(oldest_timestamp, open_ts)

        if leg.get('is_resolved_on_api'):
            any_resolved = True
        question = question or leg.get('question')
        if current_probability is None:
            current_probability = leg.get('current_probability')

    if not outcomes:
        outcome = None
    elif len(outcomes) == 1:
        outcome = next(iter(outcomes))
    else:
        outcome = 'MIXED'

    return {
        'market_id': market_id,
        'trade_id': None,  # aggregate — individual legs have their own ids
        'n_legs': len(legs),
        'outcome': outcome,
        'amount': total_amount if any_amount else None,
        'current_unrealised_pnl': total_pnl if any_pnl else None,
        'entry_probability': (weighted_entry / weighted_base) if weighted_base else None,
        'current_probability': current_probability,
        'position_class': position_class,
        'question': question,
        'is_resolved_on_api': any_resolved,
        'last_repriced_at': oldest_repriced.isoformat() if oldest_repriced else None,
        'timestamp': oldest_timestamp.isoformat() if oldest_timestamp else None,
        # Precomputed amount-weighted days_held so the scorer doesn't have
        # to re-derive it from `timestamp` (which would give the oldest
        # leg's age, not the weighted average).
        '_aggregate_days_held': (weighted_days / weighted_base) if weighted_base else None,
        # Youngest leg's days_held. The too_new guard uses THIS, not the
        # weighted average, because close_position_early closes every leg
        # at once — if any single leg is still within its minimum hold
        # window the whole market must be held.
        '_min_leg_days_held': min_leg_days,
    }


def _derive_aggregate_position_class(
    legs: List[Dict],
    *,
    _now_ms: Optional[float] = None,
) -> Optional[str]:
    """Deterministic, order-independent position_class for a market aggregate.

    Per-leg `position_class` is a snapshot of the market at entry time — a
    leg opened 45 days ago as `long_or_uncertain` might be a short-term
    position today because closeTime is now a week away. Picking "first
    truthy value" from the legs list made the LONG_HORIZON_PENALTY depend
    on JSON ordering (a market with legs `[long_or_uncertain, short]`
    closed but `[short, long_or_uncertain]` held, despite identical pnl).

    Strategy:
    1. If ANY leg has a usable `close_time_ms`, derive the CURRENT term
       from it (short ≤7d, medium ≤30d, else long). closeTime is
       intrinsic to the market and doesn't change per-leg, so taking the
       first non-null value gives the same answer regardless of leg order.
       Combine with the first non-null `resolvability` (also intrinsic to
       the market — never changes after creation).
    2. Otherwise fall back to a conservative reducer on persisted
       `position_class` values: if ANY leg says `long_or_uncertain`,
       apply the penalty. Deterministic (doesn't depend on ordering),
       conservative (applies penalty on mixed-class markets rather than
       ignoring it), and correct when all legs agree.

    Returns None only when no leg provides either close_time_ms or a
    persisted position_class — callers should handle None by NOT applying
    the long_horizon_penalty (no honest signal).
    """
    # Path 1: close_time_ms is the authoritative source
    close_time_ms: Optional[float] = None
    for leg in legs:
        ct = leg.get('close_time_ms')
        if isinstance(ct, (int, float)) and ct > 0:
            close_time_ms = ct
            break

    if close_time_ms is not None:
        now_ms = _now_ms if _now_ms is not None else datetime.now(timezone.utc).timestamp() * 1000
        days_to_close = (close_time_ms - now_ms) / 86_400_000
        return _position_class_from_term(days_to_close)

    # Path 2: fallback — any long_or_uncertain wins, with obsolete-value
    # normalization so legacy 4-bucket values (long_reliable / long_risky /
    # long_high / long_mid_low) also map into long_or_uncertain. Without
    # this step, pre-Phase-2.1 legs silently skip LONG_HORIZON_PENALTY
    # because position_score only matches the exact 3-bucket name.
    persisted = [
        _normalize_position_class(leg.get('position_class'))
        for leg in legs
        if leg.get('position_class')
    ]
    if not persisted:
        return None
    if 'long_or_uncertain' in persisted:
        return 'long_or_uncertain'
    if 'medium' in persisted:
        return 'medium'
    if 'short' in persisted:
        return 'short'
    # All entries are unknown/legacy-unmapped values — return the first
    # deterministically (score will just not apply the penalty).
    return sorted(set(persisted))[0]


def _position_class_from_term(days_to_close: float) -> str:
    """Map current days-to-close to a 3-bucket position_class.

    Matches Phase 2.1's _derive_position_class exactly — `resolvability`
    is intentionally NOT used at this layer. Phase 2.1 collapsed from 4
    buckets to 3 precisely to keep enforcement simple on a 10-slot book,
    and to stop single misclassifications from costing a full slot. Adding
    a resolvability demotion here would re-introduce that complexity and
    diverge from the shipped slot-cap semantics in auto_trader._POSITION_CLASS_CAPS.

      days ≤ 0  (already past close, awaiting resolution) → short
      days ≤ 7                                              → short
      days ≤ 30                                             → medium
      days > 30                                             → long_or_uncertain
    """
    if days_to_close <= _SHORT_TERM_DAYS:
        return 'short'
    if days_to_close <= _MEDIUM_TERM_DAYS:
        return 'medium'
    return 'long_or_uncertain'


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _existing_pending_ids(pending: List[Dict]) -> set:
    """Market IDs that should be suppressed from new close proposals.

    Two reasons a market is suppressed:

      1. It has an active 'pending' proposal (within CLOSE_EXPIRY_HOURS) —
         re-proposing would duplicate an open ask to the operator.
      2. It has a 'dismissed' proposal within DISMISS_COOLDOWN_HOURS of its
         dismissal — the operator already said "no" and nothing material has
         changed in an hour; re-pinging them is noise. Cooldown expires so
         genuinely-new situations (big price move, close-time nearing) still
         get another proposal once it elapses.
    """
    now = datetime.now()
    pending_cutoff  = now - timedelta(hours=CLOSE_EXPIRY_HOURS)
    dismiss_cutoff  = now - timedelta(hours=DISMISS_COOLDOWN_HOURS)
    suppressed = set()
    for p in pending:
        status = p.get('status')
        market_id = p.get('market_id')
        if not market_id:
            continue
        if status == 'pending':
            try:
                proposed_at = datetime.fromisoformat(p.get('proposed_at', '2000-01-01'))
            except ValueError:
                continue
            if proposed_at > pending_cutoff:
                suppressed.add(market_id)
        elif status == 'dismissed':
            # Fall back to proposed_at if dismissed_at is missing (older rows).
            ts_raw = p.get('dismissed_at') or p.get('proposed_at', '2000-01-01')
            try:
                dismissed_at = datetime.fromisoformat(ts_raw)
            except ValueError:
                continue
            if dismissed_at > dismiss_cutoff:
                suppressed.add(market_id)
    return suppressed


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
        'n_legs': pos.get('n_legs', 1),
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
    n_legs = proposal.get('n_legs', 1)
    legs_tag = f" ({n_legs} legs)" if n_legs and n_legs > 1 else ""

    return (
        f"🔻 Close Proposal #{close_id}{legs_tag}\n"
        f"{proposal['outcome']} on \"{q}\"\n"
        f"  Avg entry {entry:.0%}, now {now:.0%}, unrealised ${pnl:+.2f}\n"
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

    # Scoring/classification is now per-market, aggregating all OPEN legs
    # in that market. close_position_early closes every leg in one call, so
    # a losing leg in an otherwise-profitable market must NOT emit a close
    # proposal — the aggregate pnl is what gets realised.
    for market_id, legs in _iter_open_markets(state):
        aggregate = _aggregate_market_legs(market_id, legs)
        action, score, reason = classify_position(aggregate)

        if action == 'HOLD':
            n_hold += 1
            if verbose:
                s = f"{score:+.2f}" if isinstance(score, (int, float)) else "?"
                legs_tag = f" ({len(legs)} legs)" if len(legs) > 1 else ""
                print(f"  [{market_id[:12]}] HOLD  score={s}  ({reason}){legs_tag}")
            continue

        # action == 'CLOSE'
        n_close += 1
        if market_id in already_proposed:
            if verbose:
                print(f"  [{market_id[:12]}] CLOSE score={score:+.2f} — proposal already pending, skipping")
            continue

        proposal = _build_proposal(next_id, market_id, aggregate, score, reason)
        new_proposals.append(proposal)
        next_id += 1
        if verbose:
            legs_tag = f" ({len(legs)} legs)" if len(legs) > 1 else ""
            print(f"  [{market_id[:12]}] CLOSE score={score:+.2f} — proposed #{proposal['id']}{legs_tag}")

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
