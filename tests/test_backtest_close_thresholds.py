#!/usr/bin/env python3
"""
Tests for scripts/backtest_close_thresholds.py — the threshold backtest tool.

Built around a small fixture calibration.db + state file with hand-computable
trajectories:

  mkt_loser  — drifts 0 → -1 → -2.5 → -4, resolves LOSE at -5.0. Any sane
               threshold should have closed it early; delta is positive.
  mkt_winner — drifts 0 → +1 → +2, resolves WIN at +5.0. Must never trigger.
  mkt_open   — still OPEN at -0.2; hold outcome is the censored last mark.
"""

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.backtest_close_thresholds import (
    load_trajectories,
    load_outcomes,
    simulate_market,
    run_backtest,
    format_table,
)


_SNAPSHOT_SCHEMA = """
    CREATE TABLE position_snapshots (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_id            INTEGER,
        market_id           TEXT NOT NULL,
        snapshot_at         TEXT NOT NULL,
        current_probability REAL,
        entry_probability   REAL,
        outcome             TEXT,
        amount              REAL,
        unrealised_pnl      REAL,
        days_held           REAL,
        is_resolved         INTEGER,
        api_error           INTEGER
    )
"""


def _snap(market_id, snapshot_at, pnl, days, amount=10.0, api_error=0):
    return (1, market_id, snapshot_at, 0.5, 0.5, 'YES', amount, pnl, days, 0, api_error)


class _FixtureMixin:
    def _build_fixture(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        tmp = Path(self.tmpdir.name)
        self.db_path = tmp / 'calibration.db'
        self.state_path = tmp / 'paper_trading_state.json'

        conn = sqlite3.connect(self.db_path)
        conn.execute(_SNAPSHOT_SCHEMA)
        rows = [
            # mkt_loser: steady bleed, eventually resolves LOSE (-5.0)
            _snap('mkt_loser', '2026-06-01T00:00:00+00:00', 0.0, 0.5),
            _snap('mkt_loser', '2026-06-02T00:00:00+00:00', -1.0, 1.5),
            _snap('mkt_loser', '2026-06-03T00:00:00+00:00', -2.5, 2.5),
            _snap('mkt_loser', '2026-06-04T00:00:00+00:00', -4.0, 3.5),
            # mkt_winner: steady gain, resolves WIN (+5.0)
            _snap('mkt_winner', '2026-06-01T00:00:00+00:00', 0.0, 0.5),
            _snap('mkt_winner', '2026-06-02T00:00:00+00:00', 1.0, 1.5),
            _snap('mkt_winner', '2026-06-03T00:00:00+00:00', 2.0, 2.5),
            # mkt_open: small dip, still OPEN — censored
            _snap('mkt_open', '2026-06-03T00:00:00+00:00', 0.0, 0.5),
            _snap('mkt_open', '2026-06-04T00:00:00+00:00', -0.2, 1.5),
            # an api_error row that must be ignored even though it screams CLOSE
            _snap('mkt_open', '2026-06-04T01:00:00+00:00', -50.0, 1.55, api_error=1),
        ]
        conn.executemany(
            "INSERT INTO position_snapshots VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
        conn.close()

        state = {
            'balance': 100.0,
            'positions': {},
            'trade_history': [
                {'trade_id': 1, 'market_id': 'mkt_loser', 'status': 'LOSE',
                 'amount': 10.0, 'profit': -5.0, 'position_class': 'short'},
                {'trade_id': 2, 'market_id': 'mkt_winner', 'status': 'WIN',
                 'amount': 10.0, 'profit': 5.0, 'position_class': 'short'},
                {'trade_id': 3, 'market_id': 'mkt_open', 'status': 'OPEN',
                 'amount': 10.0, 'profit': None, 'position_class': 'medium'},
            ],
        }
        with open(self.state_path, 'w') as f:
            json.dump(state, f)

    def setUp(self):
        self._build_fixture()

    def tearDown(self):
        self.tmpdir.cleanup()


class TestDataLoading(_FixtureMixin, unittest.TestCase):
    def test_trajectories_grouped_and_ordered(self):
        traj = load_trajectories(self.db_path)
        self.assertEqual(set(traj), {'mkt_loser', 'mkt_winner', 'mkt_open'})
        self.assertEqual([p['unrealised_pnl'] for p in traj['mkt_loser']],
                         [0.0, -1.0, -2.5, -4.0])

    def test_api_error_rows_excluded(self):
        traj = load_trajectories(self.db_path)
        # The -50 api_error row must not appear in mkt_open's trajectory.
        self.assertEqual(len(traj['mkt_open']), 2)
        self.assertEqual(traj['mkt_open'][-1]['unrealised_pnl'], -0.2)

    def test_outcomes_resolved_vs_censored(self):
        out = load_outcomes(self.state_path)
        self.assertTrue(out['mkt_loser']['resolved'])
        self.assertEqual(out['mkt_loser']['final_pnl'], -5.0)
        self.assertFalse(out['mkt_open']['resolved'])
        self.assertIsNone(out['mkt_open']['final_pnl'])

    def test_missing_db_returns_empty(self):
        self.assertEqual(load_trajectories(Path(self.tmpdir.name) / 'nope.db'), {})


class TestSimulateMarket(unittest.TestCase):
    _LOSER = [
        {'snapshot_at': 't0', 'unrealised_pnl': 0.0, 'days_held': 0.5},
        {'snapshot_at': 't1', 'unrealised_pnl': -1.0, 'days_held': 1.5},
        {'snapshot_at': 't2', 'unrealised_pnl': -2.5, 'days_held': 2.5},
    ]
    _LOSE_OUTCOME = {'final_pnl': -5.0, 'resolved': True, 'position_class': 'short'}

    def test_loser_triggers_at_first_breach_and_beats_holding(self):
        # threshold -0.5, decay 0.05: t0 scores -0.025 (no), t1 scores
        # -1.075 (breach) → close at -1.0 vs hold -5.0 → delta +4.0.
        res = simulate_market(self._LOSER, self._LOSE_OUTCOME, -0.5, 0.05)
        self.assertTrue(res['triggered'])
        self.assertEqual(res['trigger_at'], 't1')
        self.assertAlmostEqual(res['close_pnl'], -1.0)
        self.assertAlmostEqual(res['delta'], 4.0, places=4)
        self.assertFalse(res['censored'])

    def test_winner_never_triggers(self):
        winner = [
            {'snapshot_at': 't0', 'unrealised_pnl': 0.0, 'days_held': 0.5},
            {'snapshot_at': 't1', 'unrealised_pnl': 2.0, 'days_held': 1.5},
        ]
        res = simulate_market(winner, {'final_pnl': 5.0, 'resolved': True,
                                       'position_class': 'short'}, -0.5, 0.05)
        self.assertFalse(res['triggered'])
        self.assertEqual(res['delta'], 0.0)

    def test_min_hold_hours_guard_respected(self):
        # A deep dip inside the MIN_HOLD window must not count as a trigger
        # — mirrors the live evaluator's too_new guard.
        spiky = [
            {'snapshot_at': 't0', 'unrealised_pnl': -3.0, 'days_held': 0.04},  # ~1h old
            {'snapshot_at': 't1', 'unrealised_pnl': 0.5, 'days_held': 1.0},
        ]
        res = simulate_market(spiky, {'final_pnl': 1.0, 'resolved': True,
                                      'position_class': 'short'},
                              -0.5, 0.05, min_hold_hours=2.0)
        self.assertFalse(res['triggered'])

    def test_long_or_uncertain_gets_penalty(self):
        flatline = [{'snapshot_at': 't0', 'unrealised_pnl': 0.0, 'days_held': 1.0}]
        # short: score -0.05 → no trigger at -0.5. long_or_uncertain:
        # -0.05 - 0.5 = -0.55 → triggers.
        res_short = simulate_market(flatline, {'final_pnl': 0.0, 'resolved': True,
                                               'position_class': 'short'}, -0.5, 0.05)
        res_long = simulate_market(flatline, {'final_pnl': 0.0, 'resolved': True,
                                              'position_class': 'long_or_uncertain'},
                                   -0.5, 0.05)
        self.assertFalse(res_short['triggered'])
        self.assertTrue(res_long['triggered'])

    def test_censored_market_uses_last_mark_as_hold(self):
        res = simulate_market(self._LOSER, {'final_pnl': None, 'resolved': False,
                                            'position_class': 'short'}, -0.5, 0.05)
        self.assertTrue(res['censored'])
        # hold = last mark (-2.5), close at -1.0 → delta +1.5
        self.assertAlmostEqual(res['delta'], 1.5, places=4)


class TestRunBacktest(_FixtureMixin, unittest.TestCase):
    def test_grid_ranked_by_delta_and_current_config_flagged(self):
        report = run_backtest(self.db_path, self.state_path)
        grid = report['grid']
        self.assertEqual(len(grid), 24)  # 6 thresholds × 4 decays
        deltas = [g['total_delta'] for g in grid]
        self.assertEqual(deltas, sorted(deltas, reverse=True))
        current = [g for g in grid if g['is_current_config']]
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]['close_score_threshold'], -0.50)
        self.assertEqual(current[0]['daily_decay_cost'], 0.05)

    def test_default_threshold_catches_the_loser_not_the_winner(self):
        report = run_backtest(self.db_path, self.state_path,
                              thresholds=[-0.5], decays=[0.05])
        g = report['grid'][0]
        # Only mkt_loser triggers: close at -1.0 vs resolved -5.0 → +4.0.
        self.assertEqual(g['n_triggered'], 1)
        self.assertEqual(g['n_censored_triggered'], 0)
        self.assertAlmostEqual(g['total_delta'], 4.0, places=4)

    def test_meta_reports_data_volume_and_caveat(self):
        report = run_backtest(self.db_path, self.state_path)
        meta = report['meta']
        self.assertEqual(meta['n_markets'], 3)
        self.assertEqual(meta['n_snapshots'], 9)  # api_error row excluded
        self.assertEqual(meta['n_markets_with_resolved_outcome'], 2)
        self.assertIn('Directional', meta['caveat'])

    def test_report_is_json_serializable_and_table_renders(self):
        report = run_backtest(self.db_path, self.state_path)
        json.dumps(report)  # must not raise
        table = format_table(report)
        self.assertIn('threshold', table)
        self.assertIn('← current config', table)

    def test_empty_db_produces_empty_report_not_crash(self):
        empty_db = Path(self.tmpdir.name) / 'empty.db'
        sqlite3.connect(empty_db).close()
        report = run_backtest(empty_db, self.state_path)
        self.assertEqual(report['meta']['n_markets'], 0)
        self.assertTrue(all(g['n_triggered'] == 0 for g in report['grid']))


if __name__ == '__main__':
    unittest.main()
