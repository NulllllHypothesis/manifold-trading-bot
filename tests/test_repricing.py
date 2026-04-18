#!/usr/bin/env python3
"""
Unit tests for scripts/reprice_positions.py (V2 Phase 2.2).

Covers:
- reprice_position pure function — unrealised P&L math at current prob
- _compute_days_held — various timestamp shapes, timezone handling
- reprice_all_positions integration — happy path, per-market API failure,
  empty state file, atomic save only when at least one position repriced
- _init_position_snapshots_db idempotency
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import reprice_positions
from scripts.reprice_positions import (
    reprice_position,
    reprice_all_positions,
    _compute_days_held,
    _init_position_snapshots_db,
    _iter_open_positions,
)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _make_position(
    market_id='mkt_A',
    trade_id=1,
    outcome='YES',
    amount=10.0,
    entry_prob=0.5,
    status='OPEN',
    days_ago=1,
):
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    return {
        'trade_id': trade_id,
        'market_id': market_id,
        'outcome': outcome,
        'amount': amount,
        'probability': entry_prob,
        'entry_probability': entry_prob,
        'status': status,
        'timestamp': ts,
    }


def _make_state(positions_by_market):
    return {
        'balance': 100.0,
        'positions': positions_by_market,
        'trades': [],
    }


# ─── reprice_position pure function ──────────────────────────────────────────

class TestRepricePosition(unittest.TestCase):
    def test_yes_position_with_market_moved_up_has_positive_pnl(self):
        # Entered YES at 50%, market now 75% → we should be up 50%.
        pos = _make_position(outcome='YES', amount=10.0, entry_prob=0.5)
        snap = reprice_position(pos, current_prob=0.75, is_resolved=False)

        # current_value = 10 * 0.75 / 0.5 = 15, pnl = +5
        self.assertAlmostEqual(snap['unrealised_pnl'], 5.0, places=4)
        self.assertEqual(snap['current_probability'], 0.75)
        self.assertFalse(snap['is_resolved'])
        self.assertFalse(snap['api_error'])

    def test_no_position_with_market_moved_down_has_positive_pnl(self):
        # Entered NO at 50%, market now 25% → our NO is winning.
        pos = _make_position(outcome='NO', amount=10.0, entry_prob=0.5)
        snap = reprice_position(pos, current_prob=0.25, is_resolved=False)

        # current_value = 10 * (1-0.25) / (1-0.5) = 15, pnl = +5
        self.assertAlmostEqual(snap['unrealised_pnl'], 5.0, places=4)

    def test_yes_position_with_market_moved_against_has_negative_pnl(self):
        pos = _make_position(outcome='YES', amount=10.0, entry_prob=0.6)
        snap = reprice_position(pos, current_prob=0.3, is_resolved=False)

        # current_value = 10 * 0.3 / 0.6 = 5, pnl = -5
        self.assertAlmostEqual(snap['unrealised_pnl'], -5.0, places=4)

    def test_snapshot_carries_forward_all_identifiers(self):
        pos = _make_position(market_id='abc123', trade_id=42,
                             outcome='YES', amount=3.0, entry_prob=0.4)
        now = '2026-04-18T12:00:00+00:00'
        snap = reprice_position(pos, current_prob=0.4, is_resolved=False, now_iso=now)

        self.assertEqual(snap['market_id'], 'abc123')
        self.assertEqual(snap['trade_id'], 42)
        self.assertEqual(snap['outcome'], 'YES')
        self.assertEqual(snap['amount'], 3.0)
        self.assertEqual(snap['entry_probability'], 0.4)
        self.assertEqual(snap['snapshot_at'], now)

    def test_is_resolved_flag_propagates(self):
        pos = _make_position()
        snap = reprice_position(pos, current_prob=0.5, is_resolved=True)
        self.assertTrue(snap['is_resolved'])


# ─── _compute_days_held ──────────────────────────────────────────────────────

class TestComputeDaysHeld(unittest.TestCase):
    def test_simple_one_day_gap(self):
        pos = {'timestamp': '2026-04-17T12:00:00+00:00'}
        now = '2026-04-18T12:00:00+00:00'
        self.assertAlmostEqual(_compute_days_held(pos, now), 1.0, places=3)

    def test_naive_timestamp_treated_as_utc(self):
        pos = {'timestamp': '2026-04-17T12:00:00'}
        now = '2026-04-18T12:00:00+00:00'
        self.assertAlmostEqual(_compute_days_held(pos, now), 1.0, places=3)

    def test_zulu_suffix_parsed(self):
        pos = {'timestamp': '2026-04-17T12:00:00Z'}
        now = '2026-04-18T12:00:00+00:00'
        self.assertAlmostEqual(_compute_days_held(pos, now), 1.0, places=3)

    def test_missing_timestamp_returns_none(self):
        self.assertIsNone(_compute_days_held({}))

    def test_unparseable_timestamp_returns_none(self):
        self.assertIsNone(_compute_days_held({'timestamp': 'not-a-date'}))

    def test_future_timestamp_clamped_to_zero(self):
        # Clock skew shouldn't produce negative days.
        pos = {'timestamp': '2026-04-19T12:00:00+00:00'}
        now = '2026-04-18T12:00:00+00:00'
        self.assertEqual(_compute_days_held(pos, now), 0.0)


# ─── _iter_open_positions ────────────────────────────────────────────────────

class TestIterOpenPositions(unittest.TestCase):
    def test_only_open_yielded(self):
        state = _make_state({
            'mkt_A': [
                _make_position(market_id='mkt_A', status='OPEN'),
                _make_position(market_id='mkt_A', status='WON'),
            ],
            'mkt_B': [_make_position(market_id='mkt_B', status='OPEN')],
        })
        got = list(_iter_open_positions(state))
        ids = [mid for mid, _, _ in got]
        self.assertEqual(sorted(ids), ['mkt_A', 'mkt_B'])
        self.assertEqual(len(got), 2)

    def test_empty_positions_yields_nothing(self):
        self.assertEqual(list(_iter_open_positions({'positions': {}})), [])

    def test_non_list_value_skipped(self):
        # Malformed entry shouldn't crash.
        state = {'positions': {'mkt_A': 'not-a-list'}}
        self.assertEqual(list(_iter_open_positions(state)), [])


# ─── DB idempotency ──────────────────────────────────────────────────────────

class TestInitSnapshotsDB(unittest.TestCase):
    def test_safe_to_call_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / 'calibration.db'
            with patch.object(reprice_positions, '_DB_PATH', db_path):
                _init_position_snapshots_db()
                _init_position_snapshots_db()  # second call must not raise

            # Table should exist.
            conn = sqlite3.connect(db_path)
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='position_snapshots'"
            ).fetchall()
            conn.close()
            self.assertEqual(len(rows), 1)


# ─── Integration: reprice_all_positions ──────────────────────────────────────

class TestRepriceAllPositions(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        tmp = Path(self.tmpdir.name)
        self.state_path = tmp / 'paper_trading_state.json'
        self.db_path = tmp / 'calibration.db'

        self._patchers = [
            patch.object(reprice_positions, '_STATE_PATH', self.state_path),
            patch.object(reprice_positions, '_DB_PATH', self.db_path),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        self.tmpdir.cleanup()

    def _write_state(self, state):
        with open(self.state_path, 'w') as f:
            json.dump(state, f)

    def _snapshots(self):
        if not self.db_path.exists():
            return []
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            "SELECT market_id, current_probability, unrealised_pnl, "
            "is_resolved, api_error FROM position_snapshots "
            "ORDER BY market_id"
        ).fetchall()
        conn.close()
        return rows

    def test_missing_state_file_returns_zero_and_is_not_an_error(self):
        # state file does not exist — should return (0,0,0) cleanly
        self.assertFalse(self.state_path.exists())
        priced, failed, resolved = reprice_all_positions(fetch_market=lambda _id: None)
        self.assertEqual((priced, failed, resolved), (0, 0, 0))

    def test_happy_path_two_positions_repriced(self):
        self._write_state(_make_state({
            'mkt_A': [_make_position(market_id='mkt_A', outcome='YES',
                                     amount=10.0, entry_prob=0.5, trade_id=1)],
            'mkt_B': [_make_position(market_id='mkt_B', outcome='NO',
                                     amount=10.0, entry_prob=0.5, trade_id=2)],
        }))

        fetched = {
            'mkt_A': {'probability': 0.75, 'isResolved': False},
            'mkt_B': {'probability': 0.25, 'isResolved': False},
        }
        priced, failed, resolved = reprice_all_positions(
            fetch_market=lambda mid: fetched[mid]
        )

        self.assertEqual(priced, 2)
        self.assertEqual(failed, 0)
        self.assertEqual(resolved, 0)

        # State file should now have repricing fields on each position.
        with open(self.state_path) as f:
            state = json.load(f)
        a = state['positions']['mkt_A'][0]
        b = state['positions']['mkt_B'][0]
        self.assertEqual(a['current_probability'], 0.75)
        self.assertAlmostEqual(a['current_unrealised_pnl'], 5.0, places=2)
        self.assertIn('last_repriced_at', a)
        self.assertFalse(a['is_resolved_on_api'])
        self.assertAlmostEqual(b['current_unrealised_pnl'], 5.0, places=2)

        # DB snapshots written.
        snaps = self._snapshots()
        self.assertEqual(len(snaps), 2)

    def test_resolved_flag_counted_and_propagated(self):
        self._write_state(_make_state({
            'mkt_A': [_make_position(market_id='mkt_A', outcome='YES', entry_prob=0.5)],
        }))

        priced, failed, resolved = reprice_all_positions(
            fetch_market=lambda _id: {'probability': 1.0, 'isResolved': True}
        )
        self.assertEqual(priced, 1)
        self.assertEqual(resolved, 1)

        with open(self.state_path) as f:
            state = json.load(f)
        self.assertTrue(state['positions']['mkt_A'][0]['is_resolved_on_api'])

    def test_api_failure_on_one_market_does_not_abort(self):
        self._write_state(_make_state({
            'mkt_A': [_make_position(market_id='mkt_A', entry_prob=0.5)],
            'mkt_B': [_make_position(market_id='mkt_B', entry_prob=0.5)],
        }))

        def fetch(mid):
            if mid == 'mkt_A':
                raise RuntimeError('boom')
            return {'probability': 0.6, 'isResolved': False}

        priced, failed, resolved = reprice_all_positions(fetch_market=fetch)
        self.assertEqual(priced, 1)
        self.assertEqual(failed, 1)

        # B should be updated; A should NOT have repricing fields.
        with open(self.state_path) as f:
            state = json.load(f)
        self.assertNotIn('current_probability', state['positions']['mkt_A'][0])
        self.assertEqual(state['positions']['mkt_B'][0]['current_probability'], 0.6)

        # DB should have both rows — one real, one api_error
        snaps = self._snapshots()
        by_market = {mid: (prob, pnl, res, err) for mid, prob, pnl, res, err in snaps}
        self.assertEqual(by_market['mkt_A'][3], 1)  # api_error=1
        self.assertEqual(by_market['mkt_B'][3], 0)

    def test_api_returns_none_counts_as_failure(self):
        self._write_state(_make_state({
            'mkt_A': [_make_position(market_id='mkt_A', entry_prob=0.5)],
        }))
        priced, failed, _ = reprice_all_positions(fetch_market=lambda _id: None)
        self.assertEqual(priced, 0)
        self.assertEqual(failed, 1)

    def test_api_returns_market_without_probability_counts_as_failure(self):
        self._write_state(_make_state({
            'mkt_A': [_make_position(market_id='mkt_A', entry_prob=0.5)],
        }))
        priced, failed, _ = reprice_all_positions(
            fetch_market=lambda _id: {'isResolved': False}
        )
        self.assertEqual(priced, 0)
        self.assertEqual(failed, 1)

    def test_closed_positions_are_skipped(self):
        self._write_state(_make_state({
            'mkt_A': [_make_position(market_id='mkt_A', status='WON')],
        }))

        called = []
        def fetch(mid):
            called.append(mid)
            return {'probability': 0.5, 'isResolved': False}

        priced, failed, _ = reprice_all_positions(fetch_market=fetch)
        self.assertEqual(priced, 0)
        self.assertEqual(failed, 0)
        self.assertEqual(called, [])  # never called the API

    def test_state_not_rewritten_when_no_positions_repriced(self):
        # If every position fails, we leave the state file untouched
        # rather than writing back a no-op save.
        self._write_state(_make_state({
            'mkt_A': [_make_position(market_id='mkt_A', entry_prob=0.5)],
        }))
        mtime_before = self.state_path.stat().st_mtime

        # Sleep-free: fetch raises for every market
        reprice_all_positions(fetch_market=lambda _id: (_ for _ in ()).throw(RuntimeError('x')))

        mtime_after = self.state_path.stat().st_mtime
        self.assertEqual(mtime_before, mtime_after)


if __name__ == '__main__':
    unittest.main()
