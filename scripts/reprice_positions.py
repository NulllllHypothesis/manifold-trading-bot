#!/usr/bin/env python3
"""
V2 Phase 2.2 — active position repricing.

Every OPEN position in paper_trading_state.json gets its current market
probability fetched from Manifold, unrealised P&L computed via AMM formula,
and a snapshot written to calibration.db:position_snapshots.

Run hourly from cron. Produces two outputs:

1. In-place update on each OPEN position:
     - current_probability     — what Manifold says the market is at right now
     - current_unrealised_pnl  — what we'd get if we closed at that price
     - last_repriced_at        — ISO timestamp of this run
     - is_resolved_on_api      — True if Manifold already resolved this market
                                 (resolve_positions.py will finalize next :10)

2. Append-only snapshots in calibration.db:position_snapshots for trend analysis:
     trade_id, market_id, snapshot_at, current_probability, unrealised_pnl,
     days_held, is_resolved

Why this matters
----------------
Until now the bot placed a bet and forgot it. Had no way to know if a position
was losing. Phase 2.3 (early close + swap) needs this data to make close
decisions at real AMM prices; Phase 2.4 (STRANDED detection) needs it to
decide whether a past-close market is actually unreachable.

Safety
------
- Read-only against paper_trading_state.json during the API phase — only writes
  happen at the end, atomically, after every API call completes (or fails
  cleanly).
- API failures per market are tolerated: that position keeps its previous
  repricing snapshot and we log the failure. The cron retries next hour.
- If the paper_trading_state.json file doesn't exist at all, exit 0 (nothing
  to do, not an error).

Exit codes:
  0 — ran cleanly, updated N positions (N may be 0 if no positions open)
  1 — unrecoverable error (file corrupt, DB unwritable, etc.)
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Project root path so sibling packages resolve
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from manifold_bot.manifold_api import api_client
from scripts.position_swap_checker import compute_unrealised_pnl


# ── Paths ────────────────────────────────────────────────────────────────────

_STATE_PATH = _ROOT / "manifold_bot" / "paper_trading_state.json"
_DB_PATH    = _ROOT / "data" / "calibration.db"


# ── DB helpers ───────────────────────────────────────────────────────────────

def _init_position_snapshots_db() -> None:
    """
    Create the position_snapshots table if it doesn't exist yet.

    One row per (position, hourly repricing run). Append-only — never update
    or delete rows so we can chart unrealised P&L trajectory over time.
    """
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS position_snapshots (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id            INTEGER,     -- from paper_trading_state.json
            market_id           TEXT NOT NULL,
            snapshot_at         TEXT NOT NULL,   -- ISO UTC
            current_probability REAL,            -- 0-1 from Manifold API
            entry_probability   REAL,            -- stored for offline re-analysis
            outcome             TEXT,            -- YES or NO (our side)
            amount              REAL,            -- our stake in dollars
            unrealised_pnl      REAL,            -- AMM mark-to-market estimate
            days_held           REAL,            -- days since position opened
            is_resolved         INTEGER,         -- 1 if Manifold API says resolved
            api_error           INTEGER          -- 1 if fetch failed this run
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_position_snapshots_market
        ON position_snapshots(market_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_position_snapshots_time
        ON position_snapshots(snapshot_at)
    """)
    conn.commit()
    conn.close()


def _insert_snapshot_row(conn: sqlite3.Connection, row: Dict) -> None:
    """Insert one snapshot. Any missing value goes in as NULL."""
    conn.execute("""
        INSERT INTO position_snapshots
        (trade_id, market_id, snapshot_at, current_probability, entry_probability,
         outcome, amount, unrealised_pnl, days_held, is_resolved, api_error)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        row.get('trade_id'),
        row.get('market_id'),
        row.get('snapshot_at'),
        row.get('current_probability'),
        row.get('entry_probability'),
        row.get('outcome'),
        row.get('amount'),
        row.get('unrealised_pnl'),
        row.get('days_held'),
        1 if row.get('is_resolved') else 0,
        1 if row.get('api_error') else 0,
    ))


# ── Position math ────────────────────────────────────────────────────────────

def _compute_days_held(position: Dict, now_iso: Optional[str] = None) -> Optional[float]:
    """
    Days since the position was opened, from its `timestamp` field.

    Returns None if the timestamp is missing or unparseable — the caller
    treats None as "unknown" and writes NULL into the snapshot.
    """
    entry_ts = position.get('timestamp')
    if not entry_ts:
        return None
    try:
        entry_dt = datetime.fromisoformat(entry_ts.replace('Z', '+00:00'))
    except (ValueError, AttributeError):
        return None

    if entry_dt.tzinfo is None:
        # Assume UTC for naive timestamps — that's the convention throughout
        # the codebase (datetime.now().isoformat() is used at write time).
        entry_dt = entry_dt.replace(tzinfo=timezone.utc)

    now_dt = (
        datetime.fromisoformat(now_iso.replace('Z', '+00:00'))
        if now_iso
        else datetime.now(timezone.utc)
    )
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)

    delta_seconds = (now_dt - entry_dt).total_seconds()
    return max(0.0, delta_seconds / 86_400.0)


def reprice_position(
    position: Dict,
    current_prob: float,
    is_resolved: bool,
    now_iso: Optional[str] = None,
) -> Dict:
    """
    Compute the repricing payload for a single open position.

    Returns a dict with the snapshot fields — caller writes both to the
    position record (for the live state file) and to the DB snapshot table.

    Pure function given inputs; no I/O. Makes it easy to unit-test.
    """
    if now_iso is None:
        now_iso = datetime.now(timezone.utc).isoformat()

    unrealised_pnl = compute_unrealised_pnl(position, current_prob)
    days_held = _compute_days_held(position, now_iso)

    return {
        'trade_id': position.get('trade_id'),
        'market_id': position.get('market_id'),
        'snapshot_at': now_iso,
        'current_probability': float(current_prob),
        'entry_probability': position.get('entry_probability') or position.get('probability'),
        'outcome': position.get('outcome'),
        'amount': position.get('amount'),
        'unrealised_pnl': round(unrealised_pnl, 4),
        'days_held': round(days_held, 3) if days_held is not None else None,
        'is_resolved': is_resolved,
        'api_error': False,
    }


# ── State I/O ────────────────────────────────────────────────────────────────

def _load_state() -> Optional[Dict]:
    """Load paper_trading_state.json. Returns None if missing (not an error)."""
    if not _STATE_PATH.exists():
        return None
    with open(_STATE_PATH) as f:
        return json.load(f)


def _save_state_atomic(state: Dict) -> None:
    """
    Write state to a temp file then atomic rename.

    If we crash mid-write, the original file stays intact — the next cron
    run just redoes the repricing. Preferable to a partial save that could
    confuse the trader on its next :20 run.
    """
    tmp_path = _STATE_PATH.with_suffix('.json.tmp')
    with open(tmp_path, 'w') as f:
        json.dump(state, f, indent=2)
    os.replace(tmp_path, _STATE_PATH)


def _iter_open_positions(state: Dict):
    """Yield (market_id, index, position) tuples for every OPEN position.

    Each paper_trading_state.json['positions'] entry is a list — this flattens
    all open positions across all markets so the caller can reprice in one pass.
    """
    positions_by_market = state.get('positions', {}) or {}
    for market_id, trades in positions_by_market.items():
        if not isinstance(trades, list):
            continue
        for idx, pos in enumerate(trades):
            if isinstance(pos, dict) and pos.get('status') == 'OPEN':
                yield market_id, idx, pos


# ── Main flow ────────────────────────────────────────────────────────────────

def reprice_all_positions(
    fetch_market=None,
    verbose: bool = False,
) -> Tuple[int, int, int]:
    """
    Reprice every open position. Returns (n_priced, n_failed, n_resolved).

    fetch_market: callable(market_id) → dict | None
                  Defaults to api_client.get_market. Injected for tests.
    """
    if fetch_market is None:
        fetch_market = api_client.get_market

    state = _load_state()
    if state is None:
        if verbose:
            print(f"No state file at {_STATE_PATH} — nothing to reprice.")
        return (0, 0, 0)

    _init_position_snapshots_db()
    conn = sqlite3.connect(_DB_PATH)

    now_iso = datetime.now(timezone.utc).isoformat()
    n_priced = 0
    n_failed = 0
    n_resolved = 0

    try:
        for market_id, idx, pos in _iter_open_positions(state):
            # Fetch current market state. Any exception → log + skip this
            # position (don't abort the whole run).
            try:
                market = fetch_market(market_id)
            except Exception as e:
                if verbose:
                    print(f"  [{market_id}] fetch failed: {e}")
                # Record the error in the snapshot table so dashboards can
                # see which markets are failing and for how long
                _insert_snapshot_row(conn, {
                    'trade_id': pos.get('trade_id'),
                    'market_id': market_id,
                    'snapshot_at': now_iso,
                    'outcome': pos.get('outcome'),
                    'amount': pos.get('amount'),
                    'entry_probability': pos.get('entry_probability') or pos.get('probability'),
                    'api_error': True,
                })
                n_failed += 1
                continue

            if not market:
                if verbose:
                    print(f"  [{market_id}] API returned nothing")
                n_failed += 1
                continue

            current_prob = market.get('probability')
            is_resolved = bool(market.get('isResolved'))

            # Some markets (rare) come back without a probability — treat as failure.
            if current_prob is None or not isinstance(current_prob, (int, float)):
                if verbose:
                    print(f"  [{market_id}] no probability in API response")
                n_failed += 1
                continue

            snapshot = reprice_position(pos, float(current_prob), is_resolved, now_iso)
            _insert_snapshot_row(conn, snapshot)

            # Update the in-memory position dict — _save_state_atomic writes
            # the whole state back to disk once we're done.
            pos['current_probability']     = snapshot['current_probability']
            pos['current_unrealised_pnl']  = snapshot['unrealised_pnl']
            pos['last_repriced_at']        = now_iso
            pos['is_resolved_on_api']      = is_resolved

            n_priced += 1
            if is_resolved:
                n_resolved += 1

            if verbose:
                pnl_str = f"${snapshot['unrealised_pnl']:+.2f}"
                resolved_tag = " [RESOLVED]" if is_resolved else ""
                print(f"  [{market_id[:12]}] entry={pos.get('entry_probability'):.3f} "
                      f"→ now={current_prob:.3f}  pnl={pnl_str}  "
                      f"days={snapshot['days_held']:.1f}{resolved_tag}")

        conn.commit()
    finally:
        conn.close()

    # Only save state if at least one position was successfully repriced —
    # otherwise leave the file untouched (nothing useful to update).
    if n_priced > 0:
        _save_state_atomic(state)

    return (n_priced, n_failed, n_resolved)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    parser.add_argument('--verbose', '-v', action='store_true', help='Print per-position details')
    args = parser.parse_args()

    print(f"==> Repricing positions at {datetime.now(timezone.utc).isoformat()}")
    try:
        priced, failed, resolved = reprice_all_positions(verbose=args.verbose)
    except Exception as e:
        print(f"[reprice_positions] unrecoverable error: {e}", file=sys.stderr)
        return 1

    print(f"    Priced:   {priced}")
    print(f"    Failed:   {failed}")
    print(f"    Resolved: {resolved} (resolve_positions.py will finalize at :10)")
    return 0


if __name__ == '__main__':
    sys.exit(main())
