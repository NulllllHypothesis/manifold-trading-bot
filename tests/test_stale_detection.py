#!/usr/bin/env python3
"""
Unit tests for V2 Phase 2.4 — stale position detection (STRANDED/ABANDONED).

Covers:
- classify_stale thresholds (still-open / grace / stranded / abandoned / resolved)
- run_detector integration (auto STRANDED, propose ABANDONED, dedup, API fail)
- paper_trader.mark_market_abandoned: status flip, balance, bet_outcomes row
- execute_abandon: happy path, dismiss, resolved-market guard, expiry
- resolve_positions scan now includes STRANDED legs (reconciliation path)
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

from scripts import detect_stale_positions
from scripts.detect_stale_positions import (
    classify_stale,
    run_detector,
    ABANDON_EXPIRY_HOURS,
)
from scripts import execute_abandon as execute_abandon_mod
from scripts.execute_abandon import (
    execute_abandon,
    find_proposal,
    MarketResolvedError,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _now_ms() -> float:
    return datetime.now(timezone.utc).timestamp() * 1000


def _ms_from_now(delta: timedelta) -> int:
    return int(_now_ms() + delta.total_seconds() * 1000)


def _pos(*, market_id='mkt_A', trade_id=1, outcome='YES', amount=5.0,
         entry_prob=0.5, status='OPEN', days_ago=1, question=None):
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    p = {
        'trade_id': trade_id,
        'market_id': market_id,
        'outcome': outcome,
        'amount': amount,
        'probability': entry_prob,
        'entry_probability': entry_prob,
        'status': status,
        'timestamp': ts,
    }
    if question is not None:
        p['question'] = question
    return p


# ── classify_stale ───────────────────────────────────────────────────────────

class TestClassifyStale(unittest.TestCase):
    def test_not_closed_yet(self):
        action, _ = classify_stale({'closeTime': _ms_from_now(timedelta(days=3))})
        self.assertEqual(action, 'SKIP_NOT_CLOSED')

    def test_within_grace_period(self):
        # 12h past close — well inside 48h grace
        action, _ = classify_stale({'closeTime': _ms_from_now(timedelta(hours=-12))})
        self.assertEqual(action, 'SKIP_GRACE')

    def test_just_under_48h_grace_is_still_grace(self):
        # 47h past close — last minute of grace
        action, _ = classify_stale({'closeTime': _ms_from_now(timedelta(hours=-47))})
        self.assertEqual(action, 'SKIP_GRACE')

    def test_just_over_48h_grace_is_stranded(self):
        # 49h past close — first minute of STRANDED
        action, _ = classify_stale({'closeTime': _ms_from_now(timedelta(hours=-49))})
        self.assertEqual(action, 'STRANDED')

    def test_30_days_past_close_is_still_stranded(self):
        action, _ = classify_stale({'closeTime': _ms_from_now(timedelta(days=-30))})
        self.assertEqual(action, 'STRANDED')

    def test_90_days_past_close_is_abandoned(self):
        action, _ = classify_stale({'closeTime': _ms_from_now(timedelta(days=-91))})
        self.assertEqual(action, 'ABANDON')

    def test_resolved_market_skipped(self):
        action, reason = classify_stale({
            'closeTime': _ms_from_now(timedelta(days=-10)),
            'isResolved': True,
        })
        self.assertEqual(action, 'SKIP_RESOLVED')

    def test_missing_closetime_skipped(self):
        action, _ = classify_stale({})
        self.assertEqual(action, 'SKIP_NO_CLOSETIME')

    def test_api_failure_returns_no_closetime(self):
        # fetch_market returning None must be a safe no-op, not an exception
        action, _ = classify_stale(None)
        self.assertEqual(action, 'SKIP_NO_CLOSETIME')


# ── run_detector integration ─────────────────────────────────────────────────

class TestRunDetector(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        tmp = Path(self.tmpdir.name)
        self.state_path = tmp / 'paper_trading_state.json'
        self.pending_path = tmp / 'pending_abandons.json'
        self._patchers = [
            patch.object(detect_stale_positions, '_STATE_PATH', self.state_path),
            patch.object(detect_stale_positions, '_PENDING_ABANDONS_PATH', self.pending_path),
        ]
        for p in self._patchers:
            p.start()
        self.telegram_sent = []

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        self.tmpdir.cleanup()

    def _notify(self, msg):
        self.telegram_sent.append(msg)

    def _write_state(self, positions):
        with open(self.state_path, 'w') as f:
            json.dump({'balance': 100.0, 'positions': positions, 'trades': []}, f)

    def _read_state(self):
        with open(self.state_path) as f:
            return json.load(f)

    def _read_pending(self):
        if not self.pending_path.exists():
            return []
        with open(self.pending_path) as f:
            return json.load(f)

    def test_stranded_auto_applied(self):
        self._write_state({'mkt_A': [_pos(market_id='mkt_A')]})

        def fetch(_id):
            return {'closeTime': _ms_from_now(timedelta(days=-10)),
                    'isResolved': False}

        counts = run_detector(fetch_market=fetch, notify=self._notify)
        self.assertEqual(counts['stranded'], 1)

        state = self._read_state()
        leg = state['positions']['mkt_A'][0]
        self.assertEqual(leg['status'], 'STRANDED')
        self.assertIn('stranded_at', leg)
        self.assertIn('stranded_reason', leg)
        # STRANDED must NOT emit a Telegram message — it's routine.
        self.assertEqual(self.telegram_sent, [])

    def test_abandon_proposed_not_auto_applied(self):
        self._write_state({'mkt_A': [_pos(market_id='mkt_A', amount=5.0,
                                          question='Will X happen?')]})

        def fetch(_id):
            return {'closeTime': _ms_from_now(timedelta(days=-100)),
                    'isResolved': False}

        counts = run_detector(fetch_market=fetch, notify=self._notify)
        self.assertEqual(counts['abandon_proposed'], 1)

        # Status must stay OPEN — write-off is propose-only.
        state = self._read_state()
        self.assertEqual(state['positions']['mkt_A'][0]['status'], 'OPEN')

        pending = self._read_pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['status'], 'pending')
        self.assertEqual(pending[0]['market_id'], 'mkt_A')
        self.assertEqual(pending[0]['total_amount'], 5.0)

        self.assertEqual(len(self.telegram_sent), 1)
        self.assertIn('Abandon Proposal', self.telegram_sent[0])
        self.assertIn('approve abandon', self.telegram_sent[0])

    def test_already_pending_abandon_not_re_proposed(self):
        self._write_state({'mkt_A': [_pos(market_id='mkt_A')]})
        now_iso = datetime.now().isoformat()
        # Existing pending proposal from a prior run
        with open(self.pending_path, 'w') as f:
            json.dump([{'id': 7, 'market_id': 'mkt_A', 'status': 'pending',
                        'proposed_at': now_iso}], f)

        def fetch(_id):
            return {'closeTime': _ms_from_now(timedelta(days=-100)),
                    'isResolved': False}

        counts = run_detector(fetch_market=fetch, notify=self._notify)
        self.assertEqual(counts['abandon_proposed'], 0)
        self.assertEqual(counts['abandon_skipped_already_pending'], 1)

    def test_api_failure_does_not_mutate_state(self):
        self._write_state({'mkt_A': [_pos(market_id='mkt_A')]})

        def fetch(_id):
            raise RuntimeError('API down')

        counts = run_detector(fetch_market=fetch, notify=self._notify)
        self.assertEqual(counts['api_failed'], 1)
        # State unchanged
        self.assertEqual(self._read_state()['positions']['mkt_A'][0]['status'], 'OPEN')

    def test_grace_period_holds_position_as_open(self):
        self._write_state({'mkt_A': [_pos(market_id='mkt_A')]})

        def fetch(_id):
            return {'closeTime': _ms_from_now(timedelta(hours=-12)),
                    'isResolved': False}

        counts = run_detector(fetch_market=fetch, notify=self._notify)
        self.assertEqual(counts['grace'], 1)
        self.assertEqual(counts['stranded'], 0)
        self.assertEqual(self._read_state()['positions']['mkt_A'][0]['status'], 'OPEN')

    def test_resolved_markets_deferred_to_resolve_positions(self):
        self._write_state({'mkt_A': [_pos(market_id='mkt_A')]})

        def fetch(_id):
            return {'closeTime': _ms_from_now(timedelta(days=-10)),
                    'isResolved': True, 'resolution': 'YES'}

        counts = run_detector(fetch_market=fetch, notify=self._notify)
        self.assertEqual(counts['resolved'], 1)
        # Must NOT flip to STRANDED — resolve_positions owns this transition.
        self.assertEqual(self._read_state()['positions']['mkt_A'][0]['status'], 'OPEN')

    def test_stranded_legs_not_re_processed_on_next_run(self):
        self._write_state({'mkt_A': [
            # Already stranded by a previous run
            {**_pos(market_id='mkt_A', trade_id=1), 'status': 'STRANDED',
             'stranded_at': datetime.now(timezone.utc).isoformat()},
        ]})

        fetch_calls = []
        def fetch(mid):
            fetch_calls.append(mid)
            return {'closeTime': _ms_from_now(timedelta(days=-10)),
                    'isResolved': False}

        counts = run_detector(fetch_market=fetch, notify=self._notify)
        # Nothing to do — OPEN legs are what we iterate.
        self.assertEqual(counts['stranded'], 0)
        self.assertEqual(fetch_calls, [])


# ── paper_trader.mark_market_abandoned ───────────────────────────────────────

class TestMarkMarketAbandoned(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        tmp = Path(self.tmpdir.name)
        self.db_path = tmp / 'calibration.db'
        self.state_file = tmp / 'paper_trading_state.json'

        with open(self.state_file, 'w') as f:
            json.dump({
                'balance': 100.0,
                'positions': {
                    'mkt_A': [
                        {'trade_id': 1, 'market_id': 'mkt_A',
                         'outcome': 'YES', 'amount': 5.0, 'probability': 0.5,
                         'entry_probability': 0.5, 'status': 'OPEN',
                         'timestamp': datetime.now().isoformat()},
                        {'trade_id': 2, 'market_id': 'mkt_A',
                         'outcome': 'YES', 'amount': 3.0, 'probability': 0.5,
                         'entry_probability': 0.5, 'status': 'STRANDED',
                         'timestamp': datetime.now().isoformat()},
                    ]
                },
                'trade_history': [],
            }, f)

        from manifold_bot import paper_trader
        self._patchers = [
            patch.object(paper_trader, '_DB_PATH', self.db_path),
        ]
        for p in self._patchers:
            p.start()
        paper_trader._init_bet_outcomes_db()

        from manifold_bot.paper_trader import PaperTrader
        self.trader = PaperTrader()
        self.trader.state_file = str(self.state_file)
        self.trader.load_state()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        self.tmpdir.cleanup()

    def test_flips_open_and_stranded_to_abandoned(self):
        loss = self.trader.mark_market_abandoned('mkt_A', reason='90d timeout')
        self.assertEqual(loss, -8.0)  # 5 + 3 = 8 total loss

        self.trader.load_state()
        legs = self.trader.positions['mkt_A']
        self.assertEqual(legs[0]['status'], 'ABANDONED')
        self.assertEqual(legs[1]['status'], 'ABANDONED')
        self.assertEqual(legs[0]['profit'], -5.0)
        self.assertEqual(legs[1]['profit'], -3.0)

    def test_writes_bet_outcomes_with_era_write_off(self):
        self.trader.mark_market_abandoned('mkt_A')

        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            "SELECT actual_pnl, era, market_resolution FROM bet_outcomes "
            "WHERE market_id='mkt_A' ORDER BY id"
        ).fetchall()
        conn.close()
        self.assertEqual(len(rows), 2)
        for pnl, era, mr in rows:
            self.assertEqual(era, 'write_off')
            self.assertEqual(mr, 'ABANDONED')
            self.assertLess(pnl, 0)

    def test_balance_reduced_by_total_loss(self):
        initial = self.trader.balance
        self.trader.mark_market_abandoned('mkt_A')
        # balance reloaded from disk
        self.trader.load_state()
        self.assertAlmostEqual(self.trader.balance, initial - 8.0, places=4)

    def test_already_abandoned_legs_skipped(self):
        # First call abandons; second call must be a no-op
        self.trader.mark_market_abandoned('mkt_A')
        self.trader.load_state()
        result = self.trader.mark_market_abandoned('mkt_A')
        self.assertIsNone(result)


# ── execute_abandon integration ──────────────────────────────────────────────

class TestExecuteAbandon(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        tmp = Path(self.tmpdir.name)
        self.pending_path = tmp / 'pending_abandons.json'
        self._patcher = patch.object(execute_abandon_mod, '_PENDING_ABANDONS_PATH',
                                     self.pending_path)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self.tmpdir.cleanup()

    def _proposal(self, **overrides):
        p = {
            'id': 1,
            'market_id': 'mkt_A',
            'trade_ids': [1],
            'question': 'Will X happen?',
            'outcome': 'YES',
            'total_amount': 5.0,
            'reason': '100.0d past close',
            'status': 'pending',
            'proposed_at': datetime.now().isoformat(),
        }
        p.update(overrides)
        return p

    def test_find_rejects_missing_id(self):
        with self.assertRaises(ValueError):
            find_proposal([], 1)

    def test_find_rejects_expired(self):
        old = (datetime.now() - timedelta(hours=ABANDON_EXPIRY_HOURS + 1)).isoformat()
        with self.assertRaises(ValueError) as ctx:
            find_proposal([self._proposal(proposed_at=old)], 1)
        self.assertIn('expired', str(ctx.exception))

    def test_find_rejects_non_pending(self):
        with self.assertRaises(ValueError):
            find_proposal([self._proposal(status='approved')], 1)

    def test_resolved_market_blocks_abandon(self):
        class FakeTrader:
            balance = 100.0
            def mark_market_abandoned(self, *a, **kw):
                raise AssertionError("abandon must not run on a resolved market")

        with patch.object(execute_abandon_mod.api_client, 'get_market',
                          return_value={'isResolved': True, 'resolution': 'YES'}):
            with self.assertRaises(MarketResolvedError):
                execute_abandon(self._proposal(), FakeTrader(), notify=lambda m: None)

    def test_happy_path_calls_trader_and_notifies(self):
        class FakeTrader:
            def __init__(self):
                self.balance = 92.0  # post-loss
                self.calls = []
            def mark_market_abandoned(self, market_id, reason=''):
                self.calls.append((market_id, reason))
                return -5.0

        trader = FakeTrader()
        notifications = []
        with patch.object(execute_abandon_mod.api_client, 'get_market',
                          return_value={'isResolved': False}):
            ok = execute_abandon(self._proposal(), trader,
                                 notify=lambda m: notifications.append(m))
        self.assertTrue(ok)
        self.assertEqual(trader.calls, [('mkt_A', '100.0d past close')])
        self.assertTrue(any('Written off' in m for m in notifications))


# ── resolve_positions reconciliation of STRANDED ─────────────────────────────

class TestResolveStrandedReconciliation(unittest.TestCase):
    """A market that was marked STRANDED earlier may eventually resolve. The
    resolution path (resolve_market / _mkt / _cancel) must pick up STRANDED
    legs too and reconcile them to WIN/LOSS with real P&L."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        tmp = Path(self.tmpdir.name)
        self.db_path = tmp / 'calibration.db'
        self.state_file = tmp / 'paper_trading_state.json'

        with open(self.state_file, 'w') as f:
            json.dump({
                'balance': 100.0,
                'positions': {
                    'mkt_A': [
                        {'trade_id': 1, 'market_id': 'mkt_A',
                         'outcome': 'YES', 'amount': 5.0, 'probability': 0.5,
                         'entry_probability': 0.5, 'status': 'STRANDED',
                         'payout_if_win': 10.0,
                         'profit_if_win': 5.0,
                         'profit_if_lose': -5.0,
                         'timestamp': datetime.now().isoformat()},
                    ]
                },
                'trade_history': [
                    {'trade_id': 1, 'market_id': 'mkt_A', 'status': 'STRANDED'},
                ],
            }, f)

        from manifold_bot import paper_trader
        self._patcher = patch.object(paper_trader, '_DB_PATH', self.db_path)
        self._patcher.start()
        paper_trader._init_bet_outcomes_db()

        from manifold_bot.paper_trader import PaperTrader
        self.trader = PaperTrader()
        self.trader.state_file = str(self.state_file)
        self.trader.load_state()

    def tearDown(self):
        self._patcher.stop()
        self.tmpdir.cleanup()

    def test_stranded_yes_resolves_to_win(self):
        self.trader.resolve_market('mkt_A', 'YES', question='Will X happen?')
        leg = self.trader.positions['mkt_A'][0]
        self.assertEqual(leg['status'], 'WIN')
        self.assertEqual(leg['profit'], 5.0)

    def test_stranded_no_resolves_to_lose(self):
        # Resolves opposite to our bet — we LOSE -$5.
        self.trader.resolve_market('mkt_A', 'NO', question='Will X happen?')
        leg = self.trader.positions['mkt_A'][0]
        self.assertEqual(leg['status'], 'LOSE')


if __name__ == '__main__':
    unittest.main()
