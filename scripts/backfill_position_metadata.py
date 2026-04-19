#!/usr/bin/env python3
"""
V2 Phase 1.4 — legacy position metadata backfill.

One-shot script to enrich OPEN / STRANDED positions in
`paper_trading_state.json` that predate the Phase 2.1 metadata plumbing.
After this runs, every live position carries `close_time_ms`, `term`,
`resolvability`, and `position_class`, so Phase 2.1's slot-cap accounting,
Phase 2.3's evaluator, and Phase 2.4's stale detector all operate on a
fully classified book instead of a half-legacy one.

Data sources, in priority order:

1. **Manifold API `/v0/market/{marketId}`** — authoritative for `question`,
   `category`, `close_time_ms`. Also feeds the derivers for `term`,
   `resolvability`, `position_class`.

2. **auto_trades.json** — historical trade log; recovers `strategies`,
   `estimated_ev`, `question` (fallback), and the blended `confidence`
   for trades that predate Phase 2.1. `ai_confidence` is NOT captured in
   this log (only the blended `confidence`), so it stays None — that's a
   known unrecoverable gap, documented here and not silently backfilled
   with a wrong value.

Existing values are always preserved — a field only gets written if it
was missing or None. Rerunning the script is safe and idempotent.

Usage:
    python3 scripts/backfill_position_metadata.py           # dry-run, prints diff
    python3 scripts/backfill_position_metadata.py --apply   # actually write

Exit codes:
  0 — ran cleanly
  1 — unrecoverable error (state corrupt, etc.)
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from manifold_bot.manifold_api import api_client
from manifold_bot.strategies import classify_position as _classify_market


_STATE_PATH       = _ROOT / "manifold_bot" / "paper_trading_state.json"
_AUTO_TRADES_PATH = _ROOT / "auto_trades.json"


# ── State I/O ────────────────────────────────────────────────────────────────

def _load_state() -> Optional[Dict]:
    if not _STATE_PATH.exists():
        return None
    with open(_STATE_PATH) as f:
        return json.load(f)


def _save_state_atomic(state: Dict) -> None:
    tmp_path = _STATE_PATH.with_suffix('.json.tmp')
    with open(tmp_path, 'w') as f:
        json.dump(state, f, indent=2)
    os.replace(tmp_path, _STATE_PATH)


def _load_auto_trades_index() -> Dict[str, Dict]:
    """Build a `market_id -> trade-record` index from auto_trades.json.

    Multiple entries per market fall back to the LATEST one (we care about
    what the bot last thought of this market, not the original entry — the
    original entry's strategies might be outdated by re-entry).
    """
    if not _AUTO_TRADES_PATH.exists():
        return {}
    try:
        with open(_AUTO_TRADES_PATH) as f:
            data = json.load(f)
    except Exception:
        return {}

    trades = data.get('trades', []) if isinstance(data, dict) else data
    if not isinstance(trades, list):
        return {}

    # Sort by timestamp ascending so the last write wins → latest record.
    trades_sorted = sorted(trades, key=lambda t: t.get('timestamp', ''))
    index: Dict[str, Dict] = {}
    for t in trades_sorted:
        mid = t.get('market_id')
        if mid:
            index[mid] = t
    return index


# ── Backfill per-leg ─────────────────────────────────────────────────────────

_BACKFILL_FIELDS_FROM_MARKET = (
    'question', 'category', 'close_time_ms',
    'term', 'resolvability', 'position_class',
)

_BACKFILL_FIELDS_FROM_TRADES = (
    'strategies', 'estimated_ev', 'confidence',
    'question',  # fallback if API didn't return one
)


def backfill_leg(
    leg: Dict,
    market: Optional[Dict],
    trade_record: Optional[Dict],
    *,
    now_ms: Optional[float] = None,
) -> Dict[str, Any]:
    """Enrich one position dict in place. Returns a dict of fields written.

    Never overwrites an existing non-empty value — the leg's current state
    is treated as ground truth. Only fills fields whose current value is
    None, empty string, empty list, or missing entirely.
    """
    written: Dict[str, Any] = {}

    # Phase 1: derive everything from the Manifold market if we have one
    if market is not None:
        classified = _classify_market(market, now_ms=now_ms)
        candidates = {
            'question':       market.get('question'),
            'category':       market.get('category') or classified.get('category'),
            'close_time_ms':  classified.get('close_time_ms'),
            'term':           classified.get('term'),
            'resolvability':  classified.get('resolvability'),
            'position_class': classified.get('position_class'),
        }
        for field in _BACKFILL_FIELDS_FROM_MARKET:
            val = candidates.get(field)
            if val is None or val == '' or val == []:
                continue
            if _is_missing(leg.get(field)):
                leg[field] = val
                written[field] = val

    # Phase 2: fall back to auto_trades.json for fields we still lack
    if trade_record is not None:
        for field in _BACKFILL_FIELDS_FROM_TRADES:
            val = trade_record.get(field)
            if val is None or val == '' or val == []:
                continue
            if _is_missing(leg.get(field)):
                leg[field] = val
                written[field] = val

    return written


def _is_missing(value) -> bool:
    """True when the field is absent, None, empty string, or empty list."""
    if value is None:
        return True
    if isinstance(value, (str, list, dict)) and not value:
        return True
    return False


# ── Main flow ────────────────────────────────────────────────────────────────

def _iter_enrichable_legs(state: Dict):
    """Yield (market_id, leg_dict) for OPEN / STRANDED legs only.

    WIN/LOSE/CLOSED_EARLY/ABANDONED are terminal — enriching them after
    the fact doesn't help any downstream system (they're excluded from
    slot accounting, evaluator, and stale detector by status filter).
    """
    for market_id, legs in (state.get('positions') or {}).items():
        if not isinstance(legs, list):
            continue
        for leg in legs:
            if isinstance(leg, dict) and leg.get('status') in ('OPEN', 'STRANDED'):
                yield market_id, leg


def _sync_trade_history(state: Dict, written_by_trade_id: Dict) -> int:
    """Mirror backfilled fields into trade_history entries by trade_id.

    trade_history is the reporting-layer record; keeping it in sync means
    daily summaries and the dashboard see the same enrichment we just
    applied to the position record. Returns count of updated history rows.
    """
    n = 0
    for th in state.get('trade_history', []) or []:
        written = written_by_trade_id.get(th.get('trade_id'))
        if not written:
            continue
        for field, value in written.items():
            if _is_missing(th.get(field)):
                th[field] = value
        n += 1
    return n


def run_backfill(
    *,
    fetch_market=None,
    apply_changes: bool = False,
    verbose: bool = False,
) -> Dict[str, int]:
    """Backfill every OPEN / STRANDED leg. Returns counts of changes.

    fetch_market is injectable for tests. When apply_changes is False the
    script runs in dry-run mode: computes diffs, prints them, does NOT
    write to state.
    """
    if fetch_market is None:
        fetch_market = api_client.get_market

    state = _load_state()
    if state is None:
        print(f"No state file at {_STATE_PATH} — nothing to backfill.")
        return {'legs': 0, 'fields_written': 0, 'api_failed': 0,
                'trade_history_updated': 0}

    trade_index = _load_auto_trades_index()
    if verbose:
        print(f"Loaded auto_trades.json index: {len(trade_index)} market(s)")

    counts = {'legs': 0, 'fields_written': 0, 'api_failed': 0,
              'trade_history_updated': 0}
    written_by_trade_id: Dict[int, Dict] = {}

    # Batch one API call per unique market_id — a market with multiple
    # legs only needs one fetch.
    market_cache: Dict[str, Optional[Dict]] = {}

    for market_id, leg in _iter_enrichable_legs(state):
        counts['legs'] += 1

        if market_id not in market_cache:
            try:
                market_cache[market_id] = fetch_market(market_id)
            except Exception as e:
                if verbose:
                    print(f"  [{market_id[:12]}] API fetch failed: {e}")
                market_cache[market_id] = None
                counts['api_failed'] += 1

        market = market_cache[market_id]
        trade_rec = trade_index.get(market_id)
        before = {k: leg.get(k) for k in
                  _BACKFILL_FIELDS_FROM_MARKET + _BACKFILL_FIELDS_FROM_TRADES}

        written = backfill_leg(leg, market, trade_rec)
        if written:
            counts['fields_written'] += len(written)
            written_by_trade_id[leg.get('trade_id')] = written
            if verbose:
                tag = 'DRY' if not apply_changes else 'APPLY'
                print(f"  [{tag}] [{market_id[:12]}] trade_id={leg.get('trade_id')} "
                      f"filled: {', '.join(sorted(written.keys()))}")
                for field, new_val in sorted(written.items()):
                    old = before.get(field)
                    print(f"      {field}: {old!r} -> {new_val!r}")
        elif verbose:
            print(f"  [{market_id[:12]}] trade_id={leg.get('trade_id')} "
                  f"— already complete")

    # Mirror into trade_history so the reporting layer matches the position
    # record. Only counted / applied when we're actually writing state.
    if written_by_trade_id:
        counts['trade_history_updated'] = _sync_trade_history(state, written_by_trade_id)

    if apply_changes and counts['fields_written'] > 0:
        _save_state_atomic(state)
        print(f"State file written: {_STATE_PATH}")

    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    parser.add_argument('--apply', action='store_true',
                        help='Actually write changes (default: dry-run)')
    parser.add_argument('--verbose', '-v', action='store_true', default=True,
                        help='Per-leg diff output (default on)')
    parser.add_argument('--quiet', '-q', action='store_true',
                        help='Suppress per-leg diff output')
    args = parser.parse_args()
    verbose = args.verbose and not args.quiet

    mode = 'APPLY' if args.apply else 'DRY-RUN'
    print(f"==> Backfilling metadata ({mode}) at {datetime.now(timezone.utc).isoformat()}")

    try:
        counts = run_backfill(apply_changes=args.apply, verbose=verbose)
    except Exception as e:
        print(f"[backfill_position_metadata] unrecoverable error: {e}", file=sys.stderr)
        return 1

    print(f"    Legs inspected:         {counts['legs']}")
    print(f"    Fields written:         {counts['fields_written']}")
    print(f"    API fetch failures:     {counts['api_failed']}")
    print(f"    trade_history updated:  {counts['trade_history_updated']}")
    if not args.apply:
        print("    (dry-run — rerun with --apply to persist)")
    return 0


if __name__ == '__main__':
    sys.exit(main())
