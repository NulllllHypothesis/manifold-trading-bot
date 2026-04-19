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


# ── Reviewer fix 4 (Medium): score at market granularity, not per-leg ────────

class TestMarketLevelAggregation(unittest.TestCase):
    """close_position_early closes ALL OPEN legs in a market in one call.
    A losing leg in an otherwise-profitable market must NOT emit a close
    proposal — the market-level aggregate P&L is what would actually be
    realised on approval. Previously the evaluator scored each leg
    independently then deduped at proposal time, which meant a losing leg
    in a net-positive market still fired a proposal."""

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

    def _write_state(self, positions):
        with open(self.state_path, 'w') as f:
            json.dump({'balance': 100.0, 'positions': positions, 'trades': []}, f)

    def test_net_profitable_market_with_one_losing_leg_does_not_propose(self):
        # Reviewer reproducer: two YES legs on same market at current prob 0.30.
        # Leg A entered at 0.50 → unrealised -4.0. Leg B entered at 0.10 →
        # unrealised +20.0. Market aggregate is +16.0 — closing the whole
        # market would realise a profit. No CLOSE proposal should fire.
        self._write_state({
            'mkt_mixed': [
                _pos(market_id='mkt_mixed', trade_id=1, outcome='YES',
                     amount=10.0, entry_prob=0.50,
                     unrealised=-4.0, days_ago=5,
                     current_prob=0.30),
                _pos(market_id='mkt_mixed', trade_id=2, outcome='YES',
                     amount=10.0, entry_prob=0.10,
                     unrealised=20.0, days_ago=5,
                     current_prob=0.30),
            ],
        })

        h, c, n = run_evaluator(notify=self._notify)

        self.assertEqual(n, 0, "net-profitable market must not emit a CLOSE proposal")
        self.assertEqual(self.telegram_sent, [])

    def test_aggregate_losing_market_proposes_with_summed_pnl(self):
        # Inverse of the profitable case: one leg -6, one leg -4 → -$10
        # aggregate. Score with 5-day decay = -10.25 → CLOSE. Proposal must
        # carry the SUMMED pnl (-10), not an individual leg's pnl.
        self._write_state({
            'mkt_loser': [
                _pos(market_id='mkt_loser', trade_id=1, amount=10.0,
                     entry_prob=0.50, unrealised=-6.0, days_ago=5,
                     current_prob=0.20),
                _pos(market_id='mkt_loser', trade_id=2, amount=10.0,
                     entry_prob=0.50, unrealised=-4.0, days_ago=5,
                     current_prob=0.20),
            ],
        })

        h, c, n = run_evaluator(notify=self._notify)

        self.assertEqual(n, 1)
        with open(self.pending_path) as f:
            pending = json.load(f)
        self.assertEqual(len(pending), 1)
        self.assertAlmostEqual(pending[0]['current_unrealised_pnl'], -10.0, places=4)
        # amount is summed across legs
        self.assertAlmostEqual(pending[0]['amount'], 20.0, places=4)
        self.assertEqual(pending[0]['n_legs'], 2)

    def test_aggregate_uses_amount_weighted_days_held(self):
        # Two legs, different ages. Amount-weighted average age:
        # (10 * 10d + 30 * 2d) / 40 = (100 + 60) / 40 = 4d
        # Aggregate pnl -$3 + -$3 = -$6. Score = -6 - 4*0.05 = -6.20.
        # If we used oldest-leg age (10d) we'd over-penalise; weighted is
        # the honest reflection of capital age in the market.
        self._write_state({
            'mkt_ages': [
                _pos(market_id='mkt_ages', trade_id=1, amount=10.0,
                     unrealised=-3.0, days_ago=10,
                     current_prob=0.3),
                _pos(market_id='mkt_ages', trade_id=2, amount=30.0,
                     unrealised=-3.0, days_ago=2,
                     current_prob=0.3),
            ],
        })

        h, c, n = run_evaluator(notify=self._notify)
        self.assertEqual(n, 1)
        with open(self.pending_path) as f:
            pending = json.load(f)
        # Score: -6 - 4*0.05 = -6.20 (short horizon, no penalty)
        self.assertAlmostEqual(pending[0]['position_score'], -6.20, places=2)

    def test_aggregate_stale_if_any_leg_stale(self):
        # One fresh leg, one stale leg → market treated as stale_reprice.
        fresh = datetime.now(timezone.utc).isoformat()
        stale = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
        self._write_state({
            'mkt_mix_stale': [
                _pos(market_id='mkt_mix_stale', trade_id=1, unrealised=-3.0,
                     days_ago=5, last_repriced_at=fresh),
                _pos(market_id='mkt_mix_stale', trade_id=2, unrealised=-3.0,
                     days_ago=5, last_repriced_at=stale),
            ],
        })
        h, c, n = run_evaluator(notify=self._notify)
        self.assertEqual(n, 0)  # held with stale_reprice

    def test_aggregate_hold_if_any_leg_resolved(self):
        # If any leg sees the market as resolved, defer to resolve_positions
        # rather than close the market ourselves.
        self._write_state({
            'mkt_any_resolved': [
                _pos(market_id='mkt_any_resolved', trade_id=1, unrealised=-3.0,
                     days_ago=5, is_resolved_on_api=False),
                _pos(market_id='mkt_any_resolved', trade_id=2, unrealised=-3.0,
                     days_ago=5, is_resolved_on_api=True),
            ],
        })
        h, c, n = run_evaluator(notify=self._notify)
        self.assertEqual(n, 0)

    def test_single_leg_markets_still_work_unchanged(self):
        # Regression guard: the aggregate-of-one should equal that one leg.
        self._write_state({
            'mkt_single': [_pos(market_id='mkt_single', trade_id=1,
                                unrealised=-3.0, days_ago=5)],
        })
        h, c, n = run_evaluator(notify=self._notify)
        self.assertEqual(n, 1)
        with open(self.pending_path) as f:
            pending = json.load(f)
        self.assertAlmostEqual(pending[0]['current_unrealised_pnl'], -3.0, places=4)
        self.assertEqual(pending[0]['n_legs'], 1)


# ── Reviewer fix 5 (Medium): deterministic position_class on mixed legs ──────

class TestAggregatePositionClassDeterministic(unittest.TestCase):
    """Per-leg position_class is a snapshot at entry time. A market with legs
    opened at different times can legitimately carry mixed classes (one leg
    opened 60d ago as long_or_uncertain, another opened this week as short).
    Picking 'first truthy value' made LONG_HORIZON_PENALTY depend on JSON
    leg ordering — reviewer reproduced this with two identical -$0.10 legs:
    ['long_or_uncertain', 'short'] → CLOSE, ['short', 'long_or_uncertain']
    → HOLD. Fix: derive aggregate class from shared close_time_ms when
    available, otherwise use a conservative order-independent reducer."""

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

    def _write_state(self, positions):
        with open(self.state_path, 'w') as f:
            json.dump({'balance': 100.0, 'positions': positions, 'trades': []}, f)

    def _run(self, class_order):
        """Reviewer's reproducer: identical -$0.10 legs, differing only in
        persisted position_class. Returns (n_new_proposals, score_if_any).
        """
        legs = [
            _pos(market_id='mkt_rep', trade_id=i + 1, amount=10.0,
                 entry_prob=0.50, unrealised=-0.10, days_ago=1,
                 current_prob=0.49, position_class=cls)
            for i, cls in enumerate(class_order)
        ]
        self._write_state({'mkt_rep': legs})

        # Isolate the per-run file state.
        if self.pending_path.exists():
            self.pending_path.unlink()

        h, c, n = run_evaluator(notify=lambda m: None)
        score = None
        if self.pending_path.exists():
            with open(self.pending_path) as f:
                pending = json.load(f)
            if pending:
                score = pending[0]['position_score']
        return n, score

    def test_same_outcome_regardless_of_leg_order(self):
        # Reviewer reproducer exactly: both orders must yield the same
        # classification. The conservative fallback (any long_or_uncertain
        # wins) applies the penalty in both cases.
        n1, s1 = self._run(['long_or_uncertain', 'short'])
        n2, s2 = self._run(['short', 'long_or_uncertain'])
        self.assertEqual(n1, n2, "leg ordering must not flip the decision")
        self.assertEqual(s1, s2, "score must be identical across orderings")

    def test_close_time_ms_beats_persisted_class(self):
        # A leg persisted as long_or_uncertain, but close_time_ms says the
        # market is now 3 days from closing → aggregate class is 'short',
        # no long_horizon_penalty applied.
        now_ms = datetime.now(timezone.utc).timestamp() * 1000
        three_days_ms = int(now_ms + 3 * 86_400_000)
        legs = [{
            'trade_id': 1, 'market_id': 'mkt_cts',
            'outcome': 'YES', 'amount': 10.0,
            'probability': 0.5, 'entry_probability': 0.5,
            'status': 'OPEN',
            'timestamp': (datetime.now(timezone.utc) - timedelta(days=5)).isoformat(),
            'current_unrealised_pnl': -0.10,
            'current_probability': 0.49,
            'last_repriced_at': datetime.now(timezone.utc).isoformat(),
            'position_class': 'long_or_uncertain',  # stale persisted value
            'resolvability': 'high',
            'close_time_ms': three_days_ms,  # authoritative
            'is_resolved_on_api': False,
        }]
        self._write_state({'mkt_cts': legs})
        h, c, n = run_evaluator(notify=self._notify)
        # Score without long penalty: -0.10 - 5*0.05 = -0.35  → HOLD
        # Score with    long penalty: -0.10 - 5*0.05 - 0.50 = -0.85 → CLOSE
        # If close_time_ms beats persisted class, we should HOLD.
        self.assertEqual(n, 0, "close_time_ms of 3d must override long_or_uncertain class")

    def test_fallback_any_long_or_uncertain_wins_on_mixed_legs(self):
        # No close_time_ms on any leg → fallback reducer. Mix of short +
        # long_or_uncertain → aggregate is long_or_uncertain (conservative).
        legs = [
            _pos(market_id='mkt_fb', trade_id=1, unrealised=-0.10, days_ago=1,
                 position_class='short'),
            _pos(market_id='mkt_fb', trade_id=2, unrealised=-0.10, days_ago=1,
                 position_class='long_or_uncertain'),
        ]
        self._write_state({'mkt_fb': legs})
        h, c, n = run_evaluator(notify=self._notify)
        # Score = -0.20 - 1*0.05 - 0.50 = -0.75 → CLOSE
        self.assertEqual(n, 1)
        with open(self.pending_path) as f:
            pending = json.load(f)
        self.assertAlmostEqual(pending[0]['position_score'], -0.75, places=2)

    def test_all_legs_short_never_apply_long_penalty(self):
        legs = [
            _pos(market_id='mkt_short', trade_id=1, unrealised=-0.10,
                 days_ago=1, position_class='short'),
            _pos(market_id='mkt_short', trade_id=2, unrealised=-0.10,
                 days_ago=1, position_class='short'),
        ]
        self._write_state({'mkt_short': legs})
        h, c, n = run_evaluator(notify=self._notify)
        # Score = -0.20 - 1*0.05 = -0.25 → HOLD (above -0.50 threshold)
        self.assertEqual(n, 0)


if __name__ == '__main__':
    unittest.main()
