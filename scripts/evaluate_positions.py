#!/usr/bin/env python3
"""
V2 Phase 2.3b — position evaluator with an autonomous, re-validating approver.

For every OPEN position in paper_trading_state.json, compute a position_score
using the Phase 2.2 repricing data, then classify the position as HOLD or
CLOSE. The two-stage propose→approve shape from Phase 2.3 (PR #15) survives,
but the approver is now a machine step in the same cron run instead of a
human glance at Telegram:

1. PROPOSE — when a market's snapshot score first breaches
   CLOSE_SCORE_THRESHOLD, record the proposal in pending_closes.json (as the
   approval era did).
2. RE-VALIDATE (the approver) — refetch the LIVE market probability straight
   from the API (not the :05 reprice snapshot, which is up to ~25 min old by
   the :30 run) and recompute the score on the fresh number. Then:
     - fresh score breaches by a CLEAR margin (<= threshold ×
       CLOSE_CONFIRM_MARGIN_FACTOR, i.e. <= -1.00 with defaults)
         → execute IMMEDIATELY in the same run ('immediate_clear_margin').
     - fresh score breaches but is borderline (between threshold and the
       clear margin) → hold the proposal one cycle; execute on the NEXT
       :30 run if a fresh refetch still breaches ('persisted_next_cycle').
       Filters one-tick AMM noise.
     - fresh score back above the threshold → 'recovered', no close.
     - refetch FAILS → defer to the next cycle. The evaluator NEVER
       executes a close on the stale snapshot alone.
   The rule that fired is stamped into close_context on the closed trade
   records, alongside the full score breakdown.
3. EXECUTE — via `scripts.execute_close.execute_close`, the same code path
   the human-approval CLI uses. Telegram is a NOTIFICATION of action taken
   (with the score breakdown and re-validation evidence), never a request
   for permission.

Why the human approval step was replaced
----------------------------------------
PR #15 shipped Phase 2.3 propose-only to collect evidence before letting the
machine act, explicitly planning that "after ~1 week of proposals +
approvals, Phase 2.3b can flip a flag to auto-approve". That observation
week structurally never happened: proposals expired unapproved after
CLOSE_EXPIRY_HOURS, and on the live box never even reached Telegram, so
losing positions sat open indefinitely. The human glance was only ever a
stand-in for missing evidence — Phase 2.3b replaces it with evidence: live
re-validation here, threshold backtesting in
scripts/backtest_close_thresholds.py, and the bet_outcomes / close_context /
scorekeeper trail for tuning.

Safety lives in code, not in human review:
  - MIN_HOLD_HOURS / staleness / awaiting-resolve guards in classify_position
    are unchanged and still gate every close.
  - Every execution is preceded by a fresh live refetch; a failed refetch
    defers the close rather than trusting stale data.
  - Closes are idempotent: executed legs become CLOSED_EARLY, so a re-run has
    nothing left to close.
  - A close failure on one market is logged + notified and never aborts the
    cron cycle or the remaining markets.
  - Markets that resolved between scoring and execution are skipped
    (MarketResolvedError) and left to resolve_positions.py at :10.
  - Recent human dismissals are honoured for DISMISS_COOLDOWN_HOURS.

`--propose-only` restores the old behavior (Telegram "approve close N" CTA,
human approves via `python3 scripts/execute_close.py --id N`).

Run hourly from cron at :30. Produces:

1. `pending_closes.json` — audit trail of close decisions (executed / failed /
   skipped_resolved / recovered / superseded / pending-awaiting-persistence),
   plus unresolved proposals when in --propose-only mode
2. Telegram message per executed close (or per proposal in --propose-only)

Exit codes:
  0 — ran cleanly (regardless of closes executed)
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
    CLOSE_CONFIRM_MARGIN_FACTOR,
    MIN_HOLD_HOURS,
)
from manifold_bot.paper_trader import PaperTrader
from automation.send_telegram import send_telegram_message
# Single source of truth for the 4-bucket → 3-bucket migration map. Importing
# rather than duplicating so a future change in one place propagates here.
from automation.auto_trader import _normalize_position_class
from manifold_bot.proposal_archive import rotate_if_new_week
# Reuse the exact close-execution path the human-approval CLI uses (resolved-
# market guard, AMM refetch, close_context stamping, notification) as a
# library call — never shelled out. _fetch_market is shared so re-validation
# and execution hit the API through one code path.
from scripts.execute_close import execute_close, MarketResolvedError, _fetch_market
# Same AMM mark-to-market formula the :05 repricer uses — re-validation must
# recompute P&L with identical math, only on a fresher probability.
from scripts.position_swap_checker import compute_unrealised_pnl


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


def _existing_pending_ids(pending: List[Dict], *, include_pending: bool = True) -> set:
    """Market IDs that should be suppressed from new close proposals.

    Two reasons a market is suppressed:

      1. It has an active 'pending' proposal (within CLOSE_EXPIRY_HOURS) —
         re-proposing would duplicate an open ask to the operator. Only
         applies when include_pending=True (propose-only mode). In execute
         mode a leftover pending proposal must NOT block the close — the
         evaluator executes and marks the old proposal 'superseded' instead
         (see _supersede_pending), which is how the live box transitions
         cleanly off the approval regime.
      2. It has a 'dismissed' proposal within DISMISS_COOLDOWN_HOURS of its
         dismissal — the operator already said "no" and nothing material has
         changed in an hour. Honoured in BOTH modes: an explicit recent human
         dismissal is real information, and the cooldown expires on its own
         so the bot still acts once it elapses.
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
            if not include_pending:
                continue
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


def _supersede_pending(pending: List[Dict], market_id: str) -> None:
    """Mark any still-'pending' proposal for market_id as 'superseded'.

    Called just before the evaluator auto-executes a close on that market.
    The old proposal's open ask ("approve close N") is moot once the bot acts
    on its own decision; leaving it 'pending' would invite a double-close via
    execute_close.py --id N. 'superseded' is terminal, like 'expired'.
    """
    for p in pending:
        if p.get('market_id') == market_id and p.get('status') == 'pending':
            p['status'] = 'superseded'
            p['superseded_at'] = datetime.now().isoformat()


def _find_active_pending(pending: List[Dict], market_id: str) -> Optional[Dict]:
    """First still-'pending' proposal for market_id, or None.

    _auto_expire_old_closes has already run by the time this is called, so
    anything still 'pending' was proposed within CLOSE_EXPIRY_HOURS — i.e. a
    recent evaluator cycle. Its existence IS the persistence signal: the
    score breached the threshold on a previous cycle too. (Approval-era and
    --propose-only leftovers qualify as well — they were the same breach
    signal, just waiting on a human who never came.)
    """
    for p in pending:
        if p.get('market_id') == market_id and p.get('status') == 'pending':
            return p
    return None


def _mark_recovered_pending(pending: List[Dict], market_id: str, note: str) -> bool:
    """Mark active pending proposal(s) for market_id as 'recovered'.

    Called when the signal that created the proposal no longer breaches the
    threshold — the noise filter worked, the position stays open. 'recovered'
    is terminal, like 'expired': a future breach starts a fresh proposal and
    must pass re-validation from scratch (we deliberately do NOT let an old
    breach + a new breach straddle a recovery and count as 'persistence').
    Returns True if anything was mutated.
    """
    changed = False
    for p in pending:
        if p.get('market_id') == market_id and p.get('status') == 'pending':
            p['status'] = 'recovered'
            p['recovered_at'] = datetime.now().isoformat()
            p['note'] = note
            changed = True
    return changed


def _revalidate_close(
    aggregate: Dict,
    legs: List[Dict],
    *,
    threshold: float = CLOSE_SCORE_THRESHOLD,
    confirm_factor: float = CLOSE_CONFIRM_MARGIN_FACTOR,
) -> Dict:
    """Stage-two approver: refetch the live market and re-score on fresh data.

    The :05 reprice snapshot that produced the CLOSE classification is ~25
    minutes old by the :30 evaluator run (and up to REPRICE_STALE_HOURS in
    the worst accepted case). Before any close executes, fetch the CURRENT
    probability straight from the API and recompute the position score on it
    with the same AMM P&L math the repricer uses. Returns a re-validation
    record for the audit trail:

      status:
        'clear_breach'        — fresh score breaches the threshold by at
                                least CLOSE_CONFIRM_MARGIN_FACTOR× —
                                unambiguous, eligible for same-run execution
        'borderline_breach'   — fresh score breaches, but inside the margin —
                                wait one cycle and confirm persistence
        'recovered'           — fresh score back at/above the threshold; the
                                snapshot breach was one-tick noise
        'refetch_failed'      — fetch failed or returned no usable
                                probability; the caller must DEFER — never
                                act on the stale snapshot alone
        'resolved_on_refetch' — market resolved since the snapshot; the
                                resolver at :10 owns it
      rule: None here; the caller stamps 'immediate_clear_margin' or
            'persisted_next_cycle' when an execution rule actually fires
      fresh_probability / fresh_score / checked_at: the evidence, persisted
            on the audit record and (via close_context) on closed trades
    """
    out = {
        'rule': None,
        'status': 'refetch_failed',
        'fresh_probability': None,
        'fresh_score': None,
        'checked_at': datetime.now().isoformat(),
    }

    market = _fetch_market(aggregate['market_id'])
    if market is None:
        return out
    if market.get('isResolved'):
        out['status'] = 'resolved_on_refetch'
        return out
    prob = market.get('probability')
    if prob is None:
        # A market payload without a probability (multi-choice, malformed…)
        # gives us nothing to re-score on — same handling as a failed fetch.
        return out

    fresh_prob = float(prob)
    out['fresh_probability'] = fresh_prob
    fresh_pnl = sum(compute_unrealised_pnl(leg, fresh_prob) for leg in legs)
    fresh_score = position_score(
        fresh_pnl,
        _days_held_from_position(aggregate),
        aggregate.get('position_class'),
    )
    if fresh_score is None:
        # Can't honestly re-score → defer, exactly like a failed fetch.
        return out
    out['fresh_score'] = fresh_score

    if fresh_score >= threshold:
        out['status'] = 'recovered'
    elif fresh_score <= threshold * confirm_factor:
        out['status'] = 'clear_breach'
    else:
        out['status'] = 'borderline_breach'
    return out


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
    position_class = pos.get('position_class')
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
        # Full component breakdown of the score at decision time. Persisted on
        # the audit record AND stamped onto the closed trades (via
        # execute_close's close_context) so the learning loop can study which
        # components drove each close once the market eventually resolves.
        'score_breakdown': {
            'unrealised_pnl': pos.get('current_unrealised_pnl'),
            'days_held': _days_held_from_position(pos),
            'position_class': position_class,
            'daily_decay_cost': DAILY_DECAY_COST,
            'long_horizon_penalty': (
                LONG_HORIZON_PENALTY if position_class == 'long_or_uncertain' else 0.0
            ),
            'threshold': CLOSE_SCORE_THRESHOLD,
        },
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

def _execute_record(record: Dict, trader: PaperTrader, notify) -> bool:
    """Execute one close decision, mutating `record` with the outcome.

    Returns True when a close was actually realised. Never raises — any
    failure on one market must not abort the cron cycle or the remaining
    markets. Outcome statuses written onto the record:

      'executed'         — position closed, P&L realised
      'skipped_resolved' — market resolved between scoring and execution;
                           resolve_positions.py at :10 owns it (retried
                           harmlessly next run until the resolver flags it)
      'noop'             — nothing left to close (legs already closed by an
                           earlier run/path — the idempotent re-run case)
      'failed'           — unexpected error; logged + notified, retried
                           next run since 'failed' never suppresses
    """
    try:
        ok = execute_close(record, trader, notify=notify, auto=True)
    except MarketResolvedError as e:
        record['status'] = 'skipped_resolved'
        record['note'] = str(e)
        print(f"  [{record['market_id'][:12]}] skipped — market already resolved")
        return False
    except Exception as e:
        record['status'] = 'failed'
        record['error'] = str(e)
        print(f"[evaluate_positions] auto-close failed for {record['market_id']}: {e}",
              file=sys.stderr)
        try:
            q = (record.get('question') or record['market_id'])[:70]
            notify(
                f"⚠️ Auto-close FAILED on \"{q}\"\n"
                f"  {e}\n"
                f"  Will retry next evaluator run."
            )
        except Exception:
            pass  # notification failure must never escalate
        return False

    if ok:
        record['status'] = 'executed'
        record['executed_at'] = datetime.now().isoformat()
        return True
    record['status'] = 'noop'
    record['note'] = 'no open legs to close (already closed?)'
    return False


def run_evaluator(
    *,
    notify=send_telegram_message,
    verbose: bool = False,
    execute: bool = True,
    trader_factory=PaperTrader,
) -> Tuple[int, int, int]:
    """
    Evaluate every open position. Returns (n_hold, n_close, n_actioned) where
    n_actioned counts executed closes (execute=True, the default) or new
    proposals emitted (execute=False, the legacy --propose-only mode).

    `notify` is injectable so tests can capture Telegram sends without
    requiring a live bot token. `trader_factory` is injectable for the same
    reason; it is only invoked when there is at least one close to execute,
    so propose-only runs (and all-HOLD runs) never touch trading state.
    """
    state = _load_state()
    if state is None:
        if verbose:
            print(f"No state file at {_STATE_PATH} — nothing to evaluate.")
        return (0, 0, 0)

    pending = _load_pending_closes()
    if _auto_expire_old_closes(pending):
        _save_pending_closes(pending)

    # Weekly ID rotation: if the latest proposed_at is in a prior ISO week,
    # archive the full file and renumber active entries from 1. Operator
    # asked for this after IDs hit #152 (cumulative since system start).
    rotated = rotate_if_new_week(pending, _ROOT / "data", "pending_closes")
    if rotated is not pending:
        pending = rotated
        _save_pending_closes(pending)

    # In execute mode a leftover 'pending' proposal (from the approval era, a
    # --propose-only run, or last cycle's borderline breach) must not BLOCK
    # anything — it is the persistence signal the re-validation step consumes
    # (and it gets superseded at execution time so execute_close.py --id N
    # can't double-close). Recent human dismissals suppress in both modes.
    suppressed = _existing_pending_ids(pending, include_pending=not execute)
    next_id = max((p.get('id', 0) for p in pending), default=0) + 1

    new_records: List[Dict] = []
    pending_dirty = False  # in-place mutations of existing records
    n_hold = 0
    n_close = 0
    n_actioned = 0
    trader = None  # built lazily on the first close to execute

    # Scoring/classification is now per-market, aggregating all OPEN legs
    # in that market. close_position_early closes every leg in one call, so
    # a losing leg in an otherwise-profitable market must NOT emit a close
    # proposal — the aggregate pnl is what gets realised.
    for market_id, legs in _iter_open_markets(state):
        aggregate = _aggregate_market_legs(market_id, legs)
        action, score, reason = classify_position(aggregate)
        legs_tag = f" ({len(legs)} legs)" if len(legs) > 1 else ""

        if action == 'HOLD':
            n_hold += 1
            # A pending proposal whose market is now scoring above the
            # threshold has recovered — terminate it so a later breach has
            # to re-prove itself from scratch. Only the score-based HOLD
            # counts; guard HOLDs (stale_reprice, too_new, …) say "can't
            # judge right now", not "the signal went away".
            if execute and reason == 'score_above_threshold':
                if _mark_recovered_pending(pending, market_id,
                                           'snapshot score back above threshold'):
                    pending_dirty = True
            if verbose:
                s = f"{score:+.2f}" if isinstance(score, (int, float)) else "?"
                print(f"  [{market_id[:12]}] HOLD  score={s}  ({reason}){legs_tag}")
            continue

        # action == 'CLOSE'
        n_close += 1
        if market_id in suppressed:
            if verbose:
                why = 'proposal already pending' if not execute else 'dismissal cooldown'
                print(f"  [{market_id[:12]}] CLOSE score={score:+.2f} — {why}, skipping")
            continue

        if not execute:
            record = _build_proposal(next_id, market_id, aggregate, score, reason)
            next_id += 1
            new_records.append(record)
            n_actioned += 1
            if verbose:
                print(f"  [{market_id[:12]}] CLOSE score={score:+.2f} — proposed #{record['id']}{legs_tag}")
            continue

        # ── Execute mode: re-validation is the approver (Phase 2.3b) ────────
        # The snapshot score breached. Before any money moves, refetch the
        # live probability and re-score on fresh data. `prior` is last
        # cycle's still-pending proposal for this market — its existence
        # means the breach already survived at least one full cycle.
        prior = _find_active_pending(pending, market_id)
        reval = _revalidate_close(aggregate, legs)

        if reval['status'] == 'refetch_failed':
            # Never execute on stale data. Keep (or create) the pending
            # record so the next cycle picks it up as a persistence check.
            if prior is None:
                record = _build_proposal(next_id, market_id, aggregate, score, reason)
                next_id += 1
                record['revalidation'] = reval
                new_records.append(record)
            else:
                prior['revalidation'] = reval
                pending_dirty = True
            if verbose:
                print(f"  [{market_id[:12]}] CLOSE score={score:+.2f} — "
                      f"refetch failed, deferred to next cycle{legs_tag}")
            continue

        if reval['status'] == 'resolved_on_refetch':
            record = prior
            if record is None:
                record = _build_proposal(next_id, market_id, aggregate, score, reason)
                next_id += 1
                new_records.append(record)
            else:
                pending_dirty = True
            record['revalidation'] = reval
            record['status'] = 'skipped_resolved'
            record['note'] = ('market resolved on re-validation refetch — '
                              'resolve_positions.py at :10 owns it')
            if verbose:
                print(f"  [{market_id[:12]}] CLOSE score={score:+.2f} — "
                      f"skipped, market already resolved{legs_tag}")
            continue

        if reval['status'] == 'recovered':
            # The fresh number doesn't breach — the snapshot was one-tick
            # noise. Terminal: a future breach starts over.
            record = prior
            if record is None:
                record = _build_proposal(next_id, market_id, aggregate, score, reason)
                next_id += 1
                new_records.append(record)
            else:
                pending_dirty = True
            record['revalidation'] = reval
            record['status'] = 'recovered'
            record['recovered_at'] = datetime.now().isoformat()
            record['note'] = 'fresh refetch no longer breaches threshold'
            if verbose:
                fs = reval['fresh_score']
                print(f"  [{market_id[:12]}] CLOSE score={score:+.2f} — recovered "
                      f"on fresh refetch (fresh {fs:+.2f}), no close{legs_tag}")
            continue

        # Fresh data still breaches. Decide which execution rule (if any)
        # fires this cycle:
        #   - a prior pending proposal means the signal PERSISTED across
        #     cycles → execute now, whatever the breach size;
        #   - no prior + clear margin → unambiguous, execute immediately;
        #   - no prior + borderline → record it and wait one cycle.
        if prior is not None:
            reval['rule'] = 'persisted_next_cycle'
        elif reval['status'] == 'clear_breach':
            reval['rule'] = 'immediate_clear_margin'
        else:  # first crossing, borderline breach
            record = _build_proposal(next_id, market_id, aggregate, score, reason)
            next_id += 1
            record['revalidation'] = reval
            new_records.append(record)
            if verbose:
                fs = reval['fresh_score']
                print(f"  [{market_id[:12]}] CLOSE score={score:+.2f} — borderline "
                      f"(fresh {fs:+.2f}), awaiting persistence next cycle{legs_tag}")
            continue

        record = _build_proposal(next_id, market_id, aggregate, score, reason)
        next_id += 1
        record['revalidation'] = reval
        # Any open ask for this market (approval-era leftover or last cycle's
        # borderline proposal) is moot once we act — supersede it so
        # execute_close.py --id N can't double-close later.
        _supersede_pending(pending, market_id)
        pending_dirty = True
        if trader is None:
            trader = trader_factory()
        if _execute_record(record, trader, notify):
            n_actioned += 1
        if verbose:
            print(f"  [{market_id[:12]}] CLOSE score={score:+.2f} — "
                  f"{record['status']} ({reval['rule']}){legs_tag}")
        new_records.append(record)

    if new_records:
        pending.extend(new_records)
    if new_records or pending_dirty:
        _save_pending_closes(pending)
    if new_records and not execute:
        for p in new_records:
            try:
                notify(_format_telegram_message(p))
            except Exception as e:
                print(f"[evaluate_positions] warning: telegram send failed: {e}", file=sys.stderr)

    return (n_hold, n_close, n_actioned)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    parser.add_argument('--verbose', '-v', action='store_true', help='Print per-position classification')
    parser.add_argument('--propose-only', action='store_true',
                        help='Legacy behavior: emit Telegram "approve close N" proposals '
                             'instead of executing closes (default: execute)')
    args = parser.parse_args()

    print(f"==> Evaluating positions at {datetime.now().isoformat()}")
    try:
        hold, close, acted = run_evaluator(verbose=args.verbose,
                                           execute=not args.propose_only)
    except Exception as e:
        print(f"[evaluate_positions] unrecoverable error: {e}", file=sys.stderr)
        return 1

    print(f"    HOLD:         {hold}")
    print(f"    CLOSE flagged:{close}")
    if args.propose_only:
        print(f"    New proposals:{acted}  (others already pending)")
    else:
        print(f"    Executed:     {acted}  (others skipped/failed — see pending_closes.json)")
    return 0


if __name__ == '__main__':
    sys.exit(main())
