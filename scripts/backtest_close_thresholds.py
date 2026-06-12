#!/usr/bin/env python3
"""
Backtest CLOSE_SCORE_THRESHOLD / DAILY_DECAY_COST against recorded history.

Replays every position trajectory we have evidence for — hourly rows in
`data/calibration.db:position_snapshots` (written by the :05 repricer) joined
with final outcomes from `paper_trading_state.json:trade_history` — and asks,
for each (threshold, decay) pair on a grid:

    "Had auto-close been active with CLOSE_SCORE_THRESHOLD=X and
     DAILY_DECAY_COST=Y, the close-vs-hold P&L delta would have been Z."

Concretely, per market trajectory:
  - walk snapshots oldest→newest; skip rows younger than MIN_HOLD_HOURS
    (the live evaluator's guard);
  - score each row with the live formula
        score = unrealised_pnl − days_held × decay
                − (long_or_uncertain ? LONG_HORIZON_PENALTY : 0)
  - the FIRST row whose score breaches the candidate threshold is the
    hypothetical close: close_pnl = that row's unrealised_pnl;
  - hold_pnl = the realized P&L the position actually ended with (WIN /
    LOSE / CLOSED_EARLY / ABANDONED legs summed), or — for still-open
    positions — the LAST snapshot's mark-to-market (flagged `censored`);
  - delta = close_pnl − hold_pnl. Positive delta = closing at that
    threshold would have beaten holding.

Output: a ranked table on stdout plus (optionally) the full grid as JSON.

HONESTY DISCLAIMER — read before trusting the numbers
-----------------------------------------------------
This tool compresses "observe proposals for a week" (PR #15's deferral plan
for Phase 2.3b) into compute, but it can only replay the history that exists.
As of the 2026-06-11 revival, position_snapshots holds roughly ONE DAY of
hourly data over a handful of positions, with zero resolved outcomes — at
that scale every hold_pnl is a censored mark-to-market, and the output is
DIRECTIONAL AT BEST, not authoritative. It is designed to be re-run weekly as
snapshots and resolutions accumulate (a follow-up could cron it alongside the
Monday weight update); only act on its ranking once a meaningful share of
positions are uncensored. Other modeling gaps, deliberately accepted for
simplicity:
  - closes are simulated at the snapshot's AMM mark with no slippage model;
  - the Phase 2.3b re-validation step (fresh refetch, persistence filter) is
    NOT simulated — this grades thresholds, not execution mechanics;
  - LONG_HORIZON_PENALTY is held at its config value rather than gridded;
  - multi-leg markets are aggregated by summing per-leg P&L at each
    timestamp (amount-weighted days_held), mirroring the live evaluator.

Usage:
    python3 scripts/backtest_close_thresholds.py
    python3 scripts/backtest_close_thresholds.py --json /tmp/backtest.json
    python3 scripts/backtest_close_thresholds.py \
        --db data/calibration.db \
        --state manifold_bot/paper_trading_state.json \
        --thresholds -0.25 -0.5 -1.0 --decays 0.02 0.05 0.1

Exit codes:
  0 — ran (even if there is no data yet; it says so honestly)
  1 — unrecoverable error (DB unreadable, state file corrupt)
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from manifold_bot.config import (
    CLOSE_SCORE_THRESHOLD,
    DAILY_DECAY_COST,
    LONG_HORIZON_PENALTY,
    MIN_HOLD_HOURS,
)
# Same 4-bucket → 3-bucket normalization the live evaluator applies, so a
# legacy 'long_reliable' leg is penalised here exactly as it would be live.
from automation.auto_trader import _normalize_position_class

_DEFAULT_DB    = _ROOT / "data" / "calibration.db"
_DEFAULT_STATE = _ROOT / "manifold_bot" / "paper_trading_state.json"

# Grid defaults bracket the current config values (threshold -0.50, decay
# 0.05) on both sides so the report always answers "should we be more or
# less aggressive than today?".
DEFAULT_THRESHOLDS = [-0.25, -0.50, -0.75, -1.00, -1.50, -2.00]
DEFAULT_DECAYS     = [0.02, 0.05, 0.10, 0.20]

# Trade statuses that mean "this position's P&L is final".
_TERMINAL_STATUSES = {'WIN', 'LOSE', 'CLOSED_EARLY', 'ABANDONED'}


# ── Data loading ─────────────────────────────────────────────────────────────

def load_trajectories(db_path: Path) -> Dict[str, List[Dict]]:
    """market_id → chronological list of aggregated snapshot points.

    Snapshots are stored per leg; the live evaluator decides at market
    granularity, so legs sharing a (market_id, snapshot_at) are collapsed:
    P&L summed, days_held amount-weighted (falling back to max when amounts
    are missing). Rows flagged api_error are skipped — they carry stale
    numbers the live evaluator would also have refused to act on.
    """
    if not db_path.exists():
        return {}
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("""
            SELECT market_id, snapshot_at, unrealised_pnl, days_held, amount
            FROM position_snapshots
            WHERE COALESCE(api_error, 0) = 0
              AND unrealised_pnl IS NOT NULL
              AND days_held IS NOT NULL
            ORDER BY market_id, snapshot_at
        """).fetchall()
    except sqlite3.OperationalError:
        # Table doesn't exist yet — repricer hasn't run on this install.
        return {}
    finally:
        conn.close()

    # Group legs by (market, timestamp) first…
    grouped: Dict[str, Dict[str, List]] = {}
    for market_id, snapshot_at, pnl, days, amount in rows:
        grouped.setdefault(market_id, {}).setdefault(snapshot_at, []).append(
            (float(pnl), float(days), float(amount) if amount else 0.0)
        )

    # …then collapse each timestamp into one market-level point.
    trajectories: Dict[str, List[Dict]] = {}
    for market_id, by_ts in grouped.items():
        points = []
        for snapshot_at in sorted(by_ts):
            legs = by_ts[snapshot_at]
            total_pnl = sum(p for p, _, _ in legs)
            weight = sum(a for _, _, a in legs)
            if weight > 0:
                days = sum(d * a for _, d, a in legs) / weight
            else:
                days = max(d for _, d, _ in legs)
            points.append({
                'snapshot_at': snapshot_at,
                'unrealised_pnl': round(total_pnl, 4),
                'days_held': round(days, 4),
            })
        trajectories[market_id] = points
    return trajectories


def load_outcomes(state_path: Path) -> Dict[str, Dict]:
    """market_id → {'final_pnl', 'resolved', 'position_class'} from trade_history.

    'resolved' is True only when every leg reached a terminal status —
    then final_pnl is the genuine realized P&L. Otherwise the backtest falls
    back to the last snapshot's mark and flags the market censored.
    """
    if not state_path.exists():
        return {}
    with open(state_path) as f:
        state = json.load(f)

    by_market: Dict[str, List[Dict]] = {}
    for trade in state.get('trade_history', []) or []:
        mid = trade.get('market_id')
        if mid:
            by_market.setdefault(mid, []).append(trade)

    outcomes = {}
    for market_id, legs in by_market.items():
        terminal = all(l.get('status') in _TERMINAL_STATUSES for l in legs)
        final_pnl = None
        if terminal:
            final_pnl = round(sum(float(l.get('profit') or 0.0) for l in legs), 4)
        pclass = None
        for l in legs:
            if l.get('position_class'):
                pclass = _normalize_position_class(l['position_class'])
                break
        outcomes[market_id] = {
            'final_pnl': final_pnl,
            'resolved': terminal,
            'position_class': pclass,
        }
    return outcomes


# ── Simulation ───────────────────────────────────────────────────────────────

def simulate_market(
    trajectory: List[Dict],
    outcome: Optional[Dict],
    threshold: float,
    decay: float,
    *,
    long_penalty: float = LONG_HORIZON_PENALTY,
    min_hold_hours: float = MIN_HOLD_HOURS,
) -> Optional[Dict]:
    """Replay one market trajectory under a candidate (threshold, decay).

    Returns {'triggered', 'close_pnl', 'hold_pnl', 'delta', 'censored',
    'trigger_at'} or None when the trajectory is empty.
    """
    if not trajectory:
        return None
    outcome = outcome or {}
    penalty = long_penalty if outcome.get('position_class') == 'long_or_uncertain' else 0.0

    trigger = None
    for point in trajectory:
        if point['days_held'] * 24.0 < min_hold_hours:
            continue  # the live evaluator's too_new guard
        score = point['unrealised_pnl'] - point['days_held'] * decay - penalty
        if score < threshold:
            trigger = point
            break

    censored = not outcome.get('resolved')
    hold_pnl = (outcome.get('final_pnl') if not censored
                else trajectory[-1]['unrealised_pnl'])

    if trigger is None:
        return {
            'triggered': False, 'close_pnl': None, 'hold_pnl': hold_pnl,
            'delta': 0.0, 'censored': censored, 'trigger_at': None,
        }
    close_pnl = trigger['unrealised_pnl']
    return {
        'triggered': True,
        'close_pnl': close_pnl,
        'hold_pnl': hold_pnl,
        'delta': round(close_pnl - hold_pnl, 4),
        'censored': censored,
        'trigger_at': trigger['snapshot_at'],
    }


def run_backtest(
    db_path: Path = _DEFAULT_DB,
    state_path: Path = _DEFAULT_STATE,
    thresholds: Optional[List[float]] = None,
    decays: Optional[List[float]] = None,
    *,
    long_penalty: float = LONG_HORIZON_PENALTY,
    min_hold_hours: float = MIN_HOLD_HOURS,
) -> Dict:
    """Grade the full grid. Returns a JSON-serializable report dict with
    `meta` (data volume + honesty caveats) and `grid` ranked by total_delta
    (best close-vs-hold improvement first)."""
    thresholds = thresholds or DEFAULT_THRESHOLDS
    decays = decays or DEFAULT_DECAYS

    trajectories = load_trajectories(Path(db_path))
    outcomes = load_outcomes(Path(state_path))

    n_snapshots = sum(len(t) for t in trajectories.values())
    span_hours = 0.0
    all_ts = [p['snapshot_at'] for t in trajectories.values() for p in t]
    if len(all_ts) >= 2:
        try:
            lo = datetime.fromisoformat(min(all_ts).replace('Z', '+00:00'))
            hi = datetime.fromisoformat(max(all_ts).replace('Z', '+00:00'))
            span_hours = round((hi - lo).total_seconds() / 3600.0, 1)
        except ValueError:
            pass
    n_resolved = sum(1 for m in trajectories if outcomes.get(m, {}).get('resolved'))

    grid = []
    for threshold in thresholds:
        for decay in decays:
            n_triggered = 0
            n_censored_triggered = 0
            total_delta = 0.0
            total_close = 0.0
            total_hold = 0.0
            for market_id, trajectory in trajectories.items():
                res = simulate_market(trajectory, outcomes.get(market_id),
                                      threshold, decay,
                                      long_penalty=long_penalty,
                                      min_hold_hours=min_hold_hours)
                if res is None or not res['triggered']:
                    continue
                n_triggered += 1
                if res['censored']:
                    n_censored_triggered += 1
                total_delta += res['delta']
                total_close += res['close_pnl']
                total_hold += res['hold_pnl'] if res['hold_pnl'] is not None else 0.0
            grid.append({
                'close_score_threshold': threshold,
                'daily_decay_cost': decay,
                'is_current_config': (threshold == CLOSE_SCORE_THRESHOLD
                                      and decay == DAILY_DECAY_COST),
                'n_triggered': n_triggered,
                'n_censored_triggered': n_censored_triggered,
                'total_close_pnl': round(total_close, 4),
                'total_hold_pnl': round(total_hold, 4),
                'total_delta': round(total_delta, 4),
            })

    grid.sort(key=lambda g: (-g['total_delta'], g['close_score_threshold']))
    return {
        'meta': {
            'generated_at': datetime.now(timezone.utc).isoformat(),
            'db_path': str(db_path),
            'state_path': str(state_path),
            'n_markets': len(trajectories),
            'n_snapshots': n_snapshots,
            'data_span_hours': span_hours,
            'n_markets_with_resolved_outcome': n_resolved,
            'current_config': {
                'close_score_threshold': CLOSE_SCORE_THRESHOLD,
                'daily_decay_cost': DAILY_DECAY_COST,
                'long_horizon_penalty': long_penalty,
                'min_hold_hours': min_hold_hours,
            },
            'caveat': (
                'Directional, not authoritative: deltas for unresolved '
                'positions use the last mark-to-market snapshot as the hold '
                'outcome (censored). Re-run weekly as snapshot history and '
                'real resolutions accumulate; trust the ranking only once '
                'n_markets_with_resolved_outcome is a meaningful share of '
                'n_markets.'
            ),
        },
        'grid': grid,
    }


# ── Reporting ────────────────────────────────────────────────────────────────

def format_table(report: Dict) -> str:
    meta = report['meta']
    lines = [
        f"Close-threshold backtest — {meta['n_markets']} markets, "
        f"{meta['n_snapshots']} snapshots spanning {meta['data_span_hours']}h, "
        f"{meta['n_markets_with_resolved_outcome']} with resolved outcomes",
        "",
        f"{'rank':>4}  {'threshold':>9}  {'decay':>6}  {'trig':>4}  "
        f"{'cens':>4}  {'close P&L':>10}  {'hold P&L':>10}  {'delta':>8}",
    ]
    for i, g in enumerate(report['grid'], 1):
        marker = '  ← current config' if g['is_current_config'] else ''
        lines.append(
            f"{i:>4}  {g['close_score_threshold']:>9.2f}  "
            f"{g['daily_decay_cost']:>6.2f}  {g['n_triggered']:>4}  "
            f"{g['n_censored_triggered']:>4}  {g['total_close_pnl']:>10.2f}  "
            f"{g['total_hold_pnl']:>10.2f}  {g['total_delta']:>+8.2f}{marker}"
        )
    lines += [
        "",
        "delta = (P&L if auto-close had fired at this threshold) − (P&L of what",
        "actually happened / current mark). Positive = closing would have won.",
        f"CAVEAT: {meta['caveat']}",
    ]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    parser.add_argument('--db', type=Path, default=_DEFAULT_DB,
                        help='calibration.db with position_snapshots')
    parser.add_argument('--state', type=Path, default=_DEFAULT_STATE,
                        help='paper_trading_state.json with trade_history')
    parser.add_argument('--thresholds', type=float, nargs='+',
                        default=DEFAULT_THRESHOLDS,
                        help='CLOSE_SCORE_THRESHOLD candidates')
    parser.add_argument('--decays', type=float, nargs='+',
                        default=DEFAULT_DECAYS,
                        help='DAILY_DECAY_COST candidates')
    parser.add_argument('--json', type=Path, default=None,
                        help='also write the full report as JSON to this path')
    args = parser.parse_args()

    try:
        report = run_backtest(args.db, args.state,
                              thresholds=args.thresholds, decays=args.decays)
    except Exception as e:
        print(f"[backtest_close_thresholds] unrecoverable error: {e}", file=sys.stderr)
        return 1

    if report['meta']['n_markets'] == 0:
        print("No position_snapshots history found — nothing to backtest yet. "
              "Let the :05 repricer accumulate data and re-run.")
        return 0

    print(format_table(report))
    if args.json:
        with open(args.json, 'w') as f:
            json.dump(report, f, indent=2)
        print(f"\nFull report written to {args.json}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
