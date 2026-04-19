#!/usr/bin/env python3
"""
Unit tests for V2 Phase 2.3 — early-close evaluator + approval flow.

Covers:
- position_score pure math
- classify_position decision rules (HOLD / CLOSE / awaiting_resolve / no_reprice)
- run_evaluator integration (Telegram injection, deduplication, expiry rollover)
- execute_close approval + dismissal paths
- paper_trader._write_bet_outcome records era='early_close' for early-close
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

from scripts import evaluate_positions
from scripts.evaluate_positions import (
    position_score,
    classify_position,
    run_evaluator,
    CLOSE_EXPIRY_HOURS,
)
from scripts import execute_close as execute_close_mod
from scripts.execute_close import execute_close, find_proposal, MarketResolvedError


# ── Helpers ──────────────────────────────────────────────────────────────────

def _pos(
    *,
    market_id='mkt_A',
    trade_id=1,
    outcome='YES',
    amount=10.0,
    entry_prob=0.5,
    status='OPEN',
    days_ago=1,
    current_prob=None,
    unrealised=None,
    last_repriced_at=None,
    position_class='short',
    is_resolved_on_api=False,
    question=None,
):
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
        'position_class': position_class,
        'is_resolved_on_api': is_resolved_on_api,
    }
    if current_prob is not None:
        p['current_probability'] = current_prob
    if unrealised is not None:
        p['current_unrealised_pnl'] = unrealised
        # Default to a fresh repricing timestamp when the caller supplies an
        # unrealised P&L — tests that exercise staleness pass an explicit
        # last_repriced_at to override this.
        p['last_repriced_at'] = last_repriced_at or datetime.now(timezone.utc).isoformat()
    elif last_repriced_at is not None:
        p['last_repriced_at'] = last_repriced_at
    if question is not None:
        p['question'] = question
    return p


def _state(positions_by_market, balance=100.0):
    return {'balance': balance, 'positions': positions_by_market, 'trades': []}


# ── position_score ───────────────────────────────────────────────────────────

class TestPositionScore(unittest.TestCase):
    def test_flat_position_just_subtracts_decay(self):
        # $0 unrealised, 10 days held at $0.05/day → -0.50
        self.assertAlmostEqual(
            position_score(0.0, 10.0, 'short', daily_decay_cost=0.05, long_horizon_penalty=0.5),
            -0.5, places=4
        )

    def test_profitable_short_position_scores_positive(self):
        # +$2 unrealised, 5 days × 0.05 = 0.25 decay → 1.75
        self.assertAlmostEqual(
            position_score(2.0, 5.0, 'short', daily_decay_cost=0.05, long_horizon_penalty=0.5),
            1.75, places=4
        )

    def test_long_or_uncertain_gets_extra_penalty(self):
        # Same numbers as flat case but with long_or_uncertain → -0.50 - 0.50 = -1.00
        self.assertAlmostEqual(
            position_score(0.0, 10.0, 'long_or_uncertain',
                           daily_decay_cost=0.05, long_horizon_penalty=0.5),
            -1.0, places=4
        )

    def test_medium_does_not_get_long_penalty(self):
        self.assertAlmostEqual(
            position_score(0.0, 10.0, 'medium',
                           daily_decay_cost=0.05, long_horizon_penalty=0.5),
            -0.5, places=4
        )

    def test_missing_unrealised_returns_none(self):
        self.assertIsNone(position_score(None, 5.0, 'short'))

    def test_missing_days_held_returns_none(self):
        self.assertIsNone(position_score(1.0, None, 'short'))


# ── classify_position ────────────────────────────────────────────────────────

class TestClassifyPosition(unittest.TestCase):
    def test_profitable_position_holds(self):
        p = _pos(unrealised=1.5, days_ago=3, position_class='short')
        action, score, reason = classify_position(p)
        self.assertEqual(action, 'HOLD')
        self.assertGreater(score, 0)

    def test_deeply_losing_position_closes(self):
        # -$3 unrealised ≫ threshold of -0.50
        p = _pos(unrealised=-3.0, days_ago=5, position_class='short')
        action, score, reason = classify_position(p)
        self.assertEqual(action, 'CLOSE')
        self.assertLess(score, -0.5)
        self.assertIn('pnl=', reason)
        self.assertIn('age=', reason)

    def test_small_loss_above_threshold_holds(self):
        # -$0.10 unrealised, 1 day old → score ~ -0.15, above -0.50 threshold
        p = _pos(unrealised=-0.10, days_ago=1, position_class='short')
        action, _, _ = classify_position(p)
        self.assertEqual(action, 'HOLD')

    def test_long_horizon_penalty_can_tip_into_close(self):
        # +$0.10 unrealised, 5 days → short would score -0.15 (HOLD).
        # But long_or_uncertain adds -0.50 → -0.65 (CLOSE).
        p = _pos(unrealised=0.10, days_ago=5, position_class='long_or_uncertain')
        action, score, reason = classify_position(p)
        self.assertEqual(action, 'CLOSE')
        self.assertIn('long_horizon_penalty', reason)

    def test_missing_reprice_data_holds_with_no_reprice_reason(self):
        # Position that hasn't been touched by Phase 2.2 yet — no
        # current_unrealised_pnl. We explicitly do NOT treat this as a
        # flat score because that's indistinguishable from a genuinely
        # flat position.
        p = _pos()  # no unrealised kw → no current_unrealised_pnl field
        action, score, reason = classify_position(p)
        self.assertEqual(action, 'HOLD')
        self.assertIsNone(score)
        self.assertEqual(reason, 'no_reprice')

    def test_resolved_market_holds_awaiting_resolve(self):
        # resolve_positions.py at :10 will handle this — evaluator must
        # not interfere, even if the score would otherwise say CLOSE.
        p = _pos(unrealised=-5.0, days_ago=10, is_resolved_on_api=True)
        action, _, reason = classify_position(p)
        self.assertEqual(action, 'HOLD')
        self.assertEqual(reason, 'awaiting_resolve')


# ── run_evaluator integration ────────────────────────────────────────────────

class TestRunEvaluator(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        tmp = Path(self.tmpdir.name)
        self.state_path = tmp / 'paper_trading_state.json'
        self.pending_path = tmp / 'pending_closes.json'

        self._patchers = [
            patch.object(evaluate_positions, '_STATE_PATH', self.state_path),
            patch.object(evaluate_positions, '_PENDING_CLOSES_PATH', self.pending_path),
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

    def _write_state(self, state):
        with open(self.state_path, 'w') as f:
            json.dump(state, f)

    def _write_pending(self, proposals):
        with open(self.pending_path, 'w') as f:
            json.dump(proposals, f)

    def _read_pending(self):
        if not self.pending_path.exists():
            return []
        with open(self.pending_path) as f:
            return json.load(f)

    def test_no_state_file_returns_zero_and_not_an_error(self):
        h, c, n = run_evaluator(notify=self._notify)
        self.assertEqual((h, c, n), (0, 0, 0))
        self.assertEqual(self.telegram_sent, [])

    def test_profitable_positions_all_hold_no_proposals(self):
        self._write_state(_state({
            'mkt_A': [_pos(market_id='mkt_A', unrealised=1.0, days_ago=2)],
            'mkt_B': [_pos(market_id='mkt_B', unrealised=0.5, days_ago=1)],
        }))
        h, c, n = run_evaluator(notify=self._notify)
        self.assertEqual((h, c, n), (2, 0, 0))
        self.assertEqual(self.telegram_sent, [])
        self.assertEqual(self._read_pending(), [])

    def test_losing_position_produces_one_proposal_with_telegram(self):
        self._write_state(_state({
            'mkt_A': [_pos(market_id='mkt_A', unrealised=-3.0, days_ago=5,
                           question='Will Foo happen?')],
        }))
        h, c, n = run_evaluator(notify=self._notify)
        self.assertEqual((h, c, n), (0, 1, 1))

        pending = self._read_pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['market_id'], 'mkt_A')
        self.assertEqual(pending[0]['status'], 'pending')
        self.assertLess(pending[0]['position_score'], -0.5)

        self.assertEqual(len(self.telegram_sent), 1)
        msg = self.telegram_sent[0]
        self.assertIn('Close Proposal', msg)
        self.assertIn('approve close', msg)
        self.assertIn('Will Foo happen?', msg)

    def test_does_not_re_propose_while_previous_still_pending(self):
        # A previous run already proposed mkt_A; evaluator should see the
        # active pending proposal and not re-propose, avoiding Telegram spam.
        now_iso = datetime.now().isoformat()
        self._write_pending([{
            'id': 7, 'market_id': 'mkt_A', 'status': 'pending',
            'proposed_at': now_iso,
        }])
        self._write_state(_state({
            'mkt_A': [_pos(market_id='mkt_A', unrealised=-3.0, days_ago=5)],
        }))
        h, c, n = run_evaluator(notify=self._notify)
        # Still classified as CLOSE, but no new proposal emitted.
        self.assertEqual(c, 1)
        self.assertEqual(n, 0)
        self.assertEqual(self.telegram_sent, [])

    def test_expired_pending_proposal_no_longer_blocks_new_proposal(self):
        # A proposal older than CLOSE_EXPIRY_HOURS should auto-expire and
        # unblock a new proposal for the same market. Avoids the stale-block
        # problem the swap checker had (see Op5 auto-dismiss).
        old_iso = (datetime.now() - timedelta(hours=CLOSE_EXPIRY_HOURS + 1)).isoformat()
        self._write_pending([{
            'id': 3, 'market_id': 'mkt_A', 'status': 'pending',
            'proposed_at': old_iso,
        }])
        self._write_state(_state({
            'mkt_A': [_pos(market_id='mkt_A', unrealised=-3.0, days_ago=5)],
        }))
        h, c, n = run_evaluator(notify=self._notify)
        self.assertEqual(n, 1)

        pending = self._read_pending()
        # Old one expired, new one pending.
        statuses = sorted(p['status'] for p in pending)
        self.assertEqual(statuses, ['expired', 'pending'])
        # New proposal must have a fresh id higher than the stale one.
        new_ids = [p['id'] for p in pending if p['status'] == 'pending']
        self.assertTrue(all(i > 3 for i in new_ids))

    def test_non_open_positions_ignored(self):
        self._write_state(_state({
            'mkt_A': [_pos(market_id='mkt_A', status='WON', unrealised=-5.0, days_ago=5)],
        }))
        h, c, n = run_evaluator(notify=self._notify)
        self.assertEqual((h, c, n), (0, 0, 0))


# ── execute_close integration ────────────────────────────────────────────────

class TestExecuteClose(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        tmp = Path(self.tmpdir.name)
        self.pending_path = tmp / 'pending_closes.json'
        self._patcher = patch.object(execute_close_mod, '_PENDING_CLOSES_PATH', self.pending_path)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self.tmpdir.cleanup()

    def _make_proposal(self, close_id=1, status='pending', hours_ago=0.1):
        return {
            'id': close_id,
            'market_id': 'mkt_A',
            'outcome': 'YES',
            'amount': 10.0,
            'entry_probability': 0.5,
            'current_probability': 0.3,
            'current_unrealised_pnl': -4.0,
            'position_score': -4.15,
            'reason': 'pnl=$-4.00, age=3.0d',
            'status': status,
            'proposed_at': (datetime.now() - timedelta(hours=hours_ago)).isoformat(),
        }

    def test_find_proposal_rejects_missing_id(self):
        with self.assertRaises(ValueError) as ctx:
            find_proposal([], 1)
        self.assertIn('No close proposal with id=1', str(ctx.exception))

    def test_find_proposal_rejects_non_pending_status(self):
        proposals = [self._make_proposal(status='dismissed')]
        with self.assertRaises(ValueError) as ctx:
            find_proposal(proposals, 1)
        self.assertIn("'dismissed'", str(ctx.exception))

    def test_find_proposal_rejects_expired(self):
        proposals = [self._make_proposal(hours_ago=CLOSE_EXPIRY_HOURS + 1)]
        with self.assertRaises(ValueError) as ctx:
            find_proposal(proposals, 1)
        self.assertIn('expired', str(ctx.exception))

    def test_execute_close_calls_trader_and_notifies(self):
        proposal = self._make_proposal()

        class FakeTrader:
            def __init__(self):
                self.balance = 100.0
                self.calls = []

            def close_position_early(self, market_id, current_prob):
                self.calls.append((market_id, current_prob))
                self.balance += -4.0  # simulate loss realisation
                return -4.0

        trader = FakeTrader()
        notifications = []

        # Patch api_client.get_market so we don't hit the real API.
        with patch.object(execute_close_mod.api_client, 'get_market',
                          return_value={'probability': 0.3}):
            ok = execute_close(proposal, trader, notify=lambda m: notifications.append(m))

        self.assertTrue(ok)
        self.assertEqual(trader.calls, [('mkt_A', 0.3)])
        self.assertTrue(any('executed' in m for m in notifications))

    def test_execute_close_falls_back_to_snapshot_prob_on_api_failure(self):
        # If live API fails, close at the snapshot price from the proposal.
        proposal = self._make_proposal()

        class FakeTrader:
            balance = 100.0
            def close_position_early(self, market_id, current_prob):
                FakeTrader._last_prob = current_prob
                return -4.0

        with patch.object(execute_close_mod.api_client, 'get_market',
                          side_effect=RuntimeError('API down')):
            execute_close(proposal, FakeTrader(), notify=lambda m: None)

        self.assertEqual(FakeTrader._last_prob, 0.3)  # from proposal


# ── Era recording on early close ─────────────────────────────────────────────

class TestEarlyCloseEraRecording(unittest.TestCase):
    """Phase 2.3 requires CLOSED_EARLY rows to write era='early_close' so the
    weekly EV audit can segment them from true-resolution outcomes."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        tmp = Path(self.tmpdir.name)
        self.db_path = tmp / 'calibration.db'

        from manifold_bot import paper_trader
        self._patcher = patch.object(paper_trader, '_DB_PATH', self.db_path)
        self._patcher.start()
        paper_trader._init_bet_outcomes_db()

    def tearDown(self):
        self._patcher.stop()
        self.tmpdir.cleanup()

    def test_early_close_writes_era_early_close(self):
        from manifold_bot.paper_trader import _write_bet_outcome

        trade = {
            'market_id': 'mkt_A',
            'outcome': 'YES',
            'amount': 10.0,
            'probability': 0.5,
            'estimated_ev': 1.0,
            'ai_confidence': 0.7,
            'strategies': ['probability_direction'],
        }
        _write_bet_outcome(
            trade,
            market_resolution='CLOSED_EARLY',
            actual_pnl=-4.0,
            era='early_close',
        )

        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT market_resolution, actual_pnl, era FROM bet_outcomes"
        ).fetchone()
        conn.close()
        self.assertEqual(row, ('CLOSED_EARLY', -4.0, 'early_close'))

    def test_resolution_rows_still_default_to_post_ev_fix(self):
        # Regression guard — only explicit callers pass era='early_close'.
        from manifold_bot.paper_trader import _write_bet_outcome

        trade = {'market_id': 'mkt_B', 'outcome': 'NO', 'amount': 5.0, 'probability': 0.4}
        _write_bet_outcome(trade, market_resolution='NO', actual_pnl=5.0)

        conn = sqlite3.connect(self.db_path)
        era = conn.execute("SELECT era FROM bet_outcomes WHERE market_id='mkt_B'").fetchone()[0]
        conn.close()
        self.assertEqual(era, 'post_ev_fix')


# ── Reviewer fix 1 (High): execute_close re-checks isResolved ────────────────

class TestExecuteCloseResolvedMarketGuard(unittest.TestCase):
    """High-severity fix: a market may resolve between proposal creation and
    human approval. If we still call close_position_early on it, we both
    mislabel the true resolution as CLOSED_EARLY in bet_outcomes (breaking
    era segmentation) and realise the wrong economics for MKT / CANCEL /
    NO resolutions. execute_close must bail out and let resolve_positions.py
    handle the real resolution path."""

    def _proposal(self):
        return {
            'id': 1,
            'market_id': 'mkt_resolved',
            'outcome': 'YES',
            'amount': 10.0,
            'entry_probability': 0.5,
            'current_probability': 0.6,
            'current_unrealised_pnl': 1.0,
            'status': 'pending',
            'proposed_at': datetime.now().isoformat(),
        }

    def test_resolved_yes_blocks_close(self):
        class FakeTrader:
            balance = 100.0
            def close_position_early(self, *a, **kw):
                raise AssertionError("close_position_early must not be called for a resolved market")

        with patch.object(execute_close_mod.api_client, 'get_market',
                          return_value={'probability': 1.0, 'isResolved': True,
                                        'resolution': 'YES'}):
            with self.assertRaises(MarketResolvedError) as ctx:
                execute_close(self._proposal(), FakeTrader(), notify=lambda m: None)
        self.assertIn('mkt_resolved', str(ctx.exception))
        self.assertIn("'YES'", str(ctx.exception))

    def test_resolved_mkt_blocks_close(self):
        # MKT resolution uses resolutionProbability, not the AMM probability;
        # closing at current 'probability' would realise wrong economics.
        class FakeTrader:
            balance = 100.0
            def close_position_early(self, *a, **kw):
                raise AssertionError("close_position_early must not be called for a resolved market")

        with patch.object(execute_close_mod.api_client, 'get_market',
                          return_value={'probability': 0.8, 'isResolved': True,
                                        'resolution': 'MKT', 'resolutionProbability': 0.6}):
            with self.assertRaises(MarketResolvedError):
                execute_close(self._proposal(), FakeTrader(), notify=lambda m: None)

    def test_resolved_cancel_blocks_close(self):
        class FakeTrader:
            balance = 100.0
            def close_position_early(self, *a, **kw):
                raise AssertionError("close_position_early must not be called for a resolved market")

        with patch.object(execute_close_mod.api_client, 'get_market',
                          return_value={'probability': 0.5, 'isResolved': True,
                                        'resolution': 'CANCEL'}):
            with self.assertRaises(MarketResolvedError):
                execute_close(self._proposal(), FakeTrader(), notify=lambda m: None)

    def test_unresolved_market_still_closes_normally(self):
        # Regression guard: the new guard must not block the happy path.
        proposal = self._proposal()

        class FakeTrader:
            balance = 100.0
            calls = []
            def close_position_early(self, market_id, current_prob):
                FakeTrader.calls.append((market_id, current_prob))
                return 1.0

        with patch.object(execute_close_mod.api_client, 'get_market',
                          return_value={'probability': 0.6, 'isResolved': False}):
            ok = execute_close(proposal, FakeTrader(), notify=lambda m: None)
        self.assertTrue(ok)
        self.assertEqual(FakeTrader.calls, [('mkt_resolved', 0.6)])


# ── Reviewer fix 2 (Medium): stale repricing → no score, no proposal ─────────

class TestStaleRepriceClassification(unittest.TestCase):
    """A position with stale last_repriced_at must be treated as no-fresh-data,
    not scored against yesterday's price. Phase 2.2 leaves previous repricing
    fields on state when a fetch fails, so staleness is the only signal that
    separates 'fresh data says close' from 'stale data says close'."""

    def test_stale_last_repriced_at_holds_with_stale_reprice_reason(self):
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        p = _pos(unrealised=-3.0, days_ago=5, last_repriced_at=old_ts)
        action, score, reason = classify_position(p)
        self.assertEqual(action, 'HOLD')
        self.assertIsNone(score)
        self.assertEqual(reason, 'stale_reprice')

    def test_missing_last_repriced_at_with_unrealised_still_treated_stale(self):
        # Defensive: a position that somehow has current_unrealised_pnl but
        # no last_repriced_at is in an inconsistent state — HOLD, don't CLOSE.
        p = _pos(unrealised=-3.0, days_ago=5)
        del p['last_repriced_at']
        action, _, reason = classify_position(p)
        self.assertEqual(action, 'HOLD')
        self.assertEqual(reason, 'stale_reprice')

    def test_unparseable_last_repriced_at_treated_stale(self):
        p = _pos(unrealised=-3.0, days_ago=5, last_repriced_at='not-a-date')
        action, _, reason = classify_position(p)
        self.assertEqual(action, 'HOLD')
        self.assertEqual(reason, 'stale_reprice')

    def test_fresh_last_repriced_at_allows_close(self):
        # Regression guard: within the staleness window a losing position
        # must still CLOSE as before.
        fresh_ts = datetime.now(timezone.utc).isoformat()
        p = _pos(unrealised=-3.0, days_ago=5, last_repriced_at=fresh_ts)
        action, _, _ = classify_position(p)
        self.assertEqual(action, 'CLOSE')

    def test_just_under_two_hours_is_still_fresh(self):
        # Boundary: 1h59m should still be fresh under the default 2h window.
        ts = (datetime.now(timezone.utc) - timedelta(hours=1, minutes=59)).isoformat()
        p = _pos(unrealised=-3.0, days_ago=5, last_repriced_at=ts)
        action, _, _ = classify_position(p)
        self.assertEqual(action, 'CLOSE')


# ── Reviewer fix 3 (Medium): intra-run dedup for multi-trade markets ─────────

class TestIntraRunDedupe(unittest.TestCase):
    """A single market can legitimately hold multiple OPEN trades (legacy
    positions, sequential averaging-in, etc.). close_position_early closes
    ALL open trades for that market in one call, so one proposal is enough.
    Without intra-run dedup the operator would receive two Telegram CTAs
    for the same market and the second approval would fail with 'nothing
    to close'."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        tmp = Path(self.tmpdir.name)
        self.state_path = tmp / 'paper_trading_state.json'
        self.pending_path = tmp / 'pending_closes.json'
        self._patchers = [
            patch.object(evaluate_positions, '_STATE_PATH', self.state_path),
            patch.object(evaluate_positions, '_PENDING_CLOSES_PATH', self.pending_path),
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

    def test_two_open_trades_same_market_produce_one_proposal(self):
        state = {
            'balance': 100.0,
            'positions': {
                'mkt_dup': [
                    _pos(market_id='mkt_dup', trade_id=1, unrealised=-3.0, days_ago=5),
                    _pos(market_id='mkt_dup', trade_id=2, unrealised=-3.0, days_ago=5,
                         amount=5.0),
                ]
            },
            'trades': [],
        }
        with open(self.state_path, 'w') as f:
            json.dump(state, f)

        h, c, n = run_evaluator(notify=self._notify)

        self.assertEqual(n, 1, "only one proposal should be emitted for one market")
        self.assertEqual(len(self.telegram_sent), 1)

        with open(self.pending_path) as f:
            pending = json.load(f)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['market_id'], 'mkt_dup')

    def test_distinct_markets_still_get_distinct_proposals(self):
        # Regression guard: dedup is per-market, not global.
        state = {
            'balance': 100.0,
            'positions': {
                'mkt_A': [_pos(market_id='mkt_A', trade_id=1, unrealised=-3.0, days_ago=5)],
                'mkt_B': [_pos(market_id='mkt_B', trade_id=2, unrealised=-3.0, days_ago=5)],
            },
            'trades': [],
        }
        with open(self.state_path, 'w') as f:
            json.dump(state, f)

        h, c, n = run_evaluator(notify=self._notify)
        self.assertEqual(n, 2)


if __name__ == '__main__':
    unittest.main()
