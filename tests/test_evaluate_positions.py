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
from manifold_bot import paper_trader


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

    def test_xkcd_reproducer_brand_new_long_with_flat_pnl_holds(self):
        # Reviewer-agent-caught rounding artifact: a brand-new
        # long_or_uncertain trade with flat P&L sits exactly at
        # -LONG_HORIZON_PENALTY, and fractional time-decay pushes it
        # just below the `<` cutoff. Without the min-hold guard, this
        # produced a CLOSE proposal ~10 minutes after the :20 trader
        # placed the trade (real case: xkcd-about-AI-2026, 2026-04-20).
        # The too_new guard must intervene BEFORE the score check.
        p = _pos(
            unrealised=0.0,
            days_ago=0.0001,   # literally just placed, < 1 minute
            position_class='long_or_uncertain',
        )
        action, score, reason = classify_position(p)
        self.assertEqual(action, 'HOLD')
        self.assertIsNone(score)
        self.assertEqual(reason, 'too_new')

    def test_too_new_guard_blocks_even_deeply_negative_scores(self):
        # A brand-new trade that would otherwise score -3 also gets
        # held with too_new. We specifically choose to wait for one
        # reprice cycle before trusting ANY score, even an extreme one,
        # because a brand-new extreme score is almost certainly stamped
        # at-entry data that the evaluator can't verify yet.
        p = _pos(
            unrealised=-3.0,
            days_ago=0.02,     # ~29 minutes, well under 2h floor
            position_class='short',
        )
        action, score, reason = classify_position(p)
        self.assertEqual(action, 'HOLD')
        self.assertEqual(reason, 'too_new')

    def test_past_min_hold_a_losing_position_still_closes(self):
        # Regression guard: once past the min-hold threshold, a
        # genuinely bad trade must still be flagged for CLOSE.
        p = _pos(
            unrealised=-3.0,
            days_ago=1,        # well past 2h
            position_class='short',
        )
        action, score, reason = classify_position(p)
        self.assertEqual(action, 'CLOSE')
        self.assertLess(score, -0.5)

    def test_min_hold_hours_parameter_override(self):
        # Callers can tighten or loosen the floor. A test-harness
        # setting min_hold_hours=0 disables the guard so pre-existing
        # tests that exercise the score path with young positions keep
        # working. Default value matches production config (2h).
        p = _pos(
            unrealised=-3.0,
            days_ago=0.02,
            position_class='short',
        )
        # With guard on (default): held as too_new
        action, _, reason = classify_position(p)
        self.assertEqual(reason, 'too_new')
        # With guard off: falls through to score-based decision
        action, score, reason = classify_position(p, min_hold_hours=0.0)
        self.assertEqual(action, 'CLOSE')

    def test_min_hold_boundary_exact(self):
        # Exactly at the boundary should NOT be too_new — the check is
        # strict `<`. A trade at exactly 2.0h is eligible for scoring.
        p = _pos(
            unrealised=-3.0,
            days_ago=2.0 / 24,   # exactly 2h
            position_class='short',
        )
        action, _, reason = classify_position(p)
        self.assertEqual(action, 'CLOSE')
        self.assertNotEqual(reason, 'too_new')


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
        h, c, n = run_evaluator(notify=self._notify, execute=False)
        self.assertEqual((h, c, n), (0, 0, 0))
        self.assertEqual(self.telegram_sent, [])

    def test_profitable_positions_all_hold_no_proposals(self):
        self._write_state(_state({
            'mkt_A': [_pos(market_id='mkt_A', unrealised=1.0, days_ago=2)],
            'mkt_B': [_pos(market_id='mkt_B', unrealised=0.5, days_ago=1)],
        }))
        h, c, n = run_evaluator(notify=self._notify, execute=False)
        self.assertEqual((h, c, n), (2, 0, 0))
        self.assertEqual(self.telegram_sent, [])
        self.assertEqual(self._read_pending(), [])

    def test_losing_position_produces_one_proposal_with_telegram(self):
        self._write_state(_state({
            'mkt_A': [_pos(market_id='mkt_A', unrealised=-3.0, days_ago=5,
                           question='Will Foo happen?')],
        }))
        h, c, n = run_evaluator(notify=self._notify, execute=False)
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
        h, c, n = run_evaluator(notify=self._notify, execute=False)
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
        h, c, n = run_evaluator(notify=self._notify, execute=False)
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
        h, c, n = run_evaluator(notify=self._notify, execute=False)
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

            def close_position_early(self, market_id, current_prob, close_context=None):
                self.calls.append((market_id, current_prob))
                self.close_context = close_context
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
        # The why-of-the-close is stamped through to the trade records.
        self.assertEqual(trader.close_context['close_type'], 'manual_approval')
        self.assertEqual(trader.close_context['position_score'], -4.15)

    def test_execute_close_falls_back_to_snapshot_prob_on_api_failure(self):
        # If live API fails, close at the snapshot price from the proposal.
        proposal = self._make_proposal()

        class FakeTrader:
            balance = 100.0
            def close_position_early(self, market_id, current_prob, close_context=None):
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
            def close_position_early(self, market_id, current_prob, close_context=None):
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

        h, c, n = run_evaluator(notify=self._notify, execute=False)

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

        h, c, n = run_evaluator(notify=self._notify, execute=False)
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

        h, c, n = run_evaluator(notify=self._notify, execute=False)

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

        h, c, n = run_evaluator(notify=self._notify, execute=False)

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

        h, c, n = run_evaluator(notify=self._notify, execute=False)
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
        h, c, n = run_evaluator(notify=self._notify, execute=False)
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
        h, c, n = run_evaluator(notify=self._notify, execute=False)
        self.assertEqual(n, 0)

    def test_single_leg_markets_still_work_unchanged(self):
        # Regression guard: the aggregate-of-one should equal that one leg.
        self._write_state({
            'mkt_single': [_pos(market_id='mkt_single', trade_id=1,
                                unrealised=-3.0, days_ago=5)],
        })
        h, c, n = run_evaluator(notify=self._notify, execute=False)
        self.assertEqual(n, 1)
        with open(self.pending_path) as f:
            pending = json.load(f)
        self.assertAlmostEqual(pending[0]['current_unrealised_pnl'], -3.0, places=4)
        self.assertEqual(pending[0]['n_legs'], 1)

    def test_fresh_leg_protects_market_from_too_new_bypass(self):
        # Reviewer-caught edge: multi-leg markets weight days_held by
        # amount, so a large/old leg could drag the average over 2h while
        # a fresh leg was still within its protection window. But
        # close_position_early closes EVERY leg, so the aggregate CLOSE
        # would liquidate the fresh leg too. Fix: use min-leg-age for the
        # too_new guard, not the weighted average.
        #
        # Reproducer: old losing 24h leg (weighted-avg push) + fresh flat
        # 0.5h leg (should still be in protection window) → too_new HOLD,
        # not CLOSE.
        self._write_state({
            'mkt_mix_age': [
                _pos(market_id='mkt_mix_age', trade_id=1,
                     amount=10.0, unrealised=-3.0, days_ago=1.0,
                     current_prob=0.3),
                _pos(market_id='mkt_mix_age', trade_id=2,
                     amount=10.0, unrealised=0.0, days_ago=0.5 / 24,
                     current_prob=0.5),
            ],
        })
        h, c, n = run_evaluator(notify=self._notify, execute=False)
        # Weighted avg would be (24 + 0.5)/2 = 12.25h → past 2h floor.
        # Aggregate pnl -$3 would drop score below threshold → CLOSE.
        # But min-leg-age is 0.5h → too_new must hold the whole market.
        self.assertEqual(n, 0)
        self.assertFalse(self.pending_path.exists())

    def test_all_legs_past_min_hold_still_closes(self):
        # Regression guard: when every leg is past the min-hold window
        # AND the aggregate score is below threshold, the market closes
        # as normal. Min-leg guard must not become a blanket mute.
        self._write_state({
            'mkt_all_old': [
                _pos(market_id='mkt_all_old', trade_id=1,
                     amount=10.0, unrealised=-3.0, days_ago=1.0),
                _pos(market_id='mkt_all_old', trade_id=2,
                     amount=10.0, unrealised=-3.0, days_ago=0.5),
            ],
        })
        h, c, n = run_evaluator(notify=self._notify, execute=False)
        self.assertEqual(n, 1)


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

        h, c, n = run_evaluator(notify=lambda m: None, execute=False)
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
        h, c, n = run_evaluator(notify=self._notify, execute=False)
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
        h, c, n = run_evaluator(notify=self._notify, execute=False)
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
        h, c, n = run_evaluator(notify=self._notify, execute=False)
        # Score = -0.20 - 1*0.05 = -0.25 → HOLD (above -0.50 threshold)
        self.assertEqual(n, 0)

    def test_medium_term_with_low_resolvability_stays_medium(self):
        # Reviewer fix (Medium): Phase 2.1's 3-bucket model derives class
        # from term ONLY — resolvability is a soft label and does NOT
        # demote medium markets into long_or_uncertain. The earlier draft
        # of _position_class_from_term applied a resolvability demotion,
        # which diverged from the shipped slot-cap model.
        now_ms = datetime.now(timezone.utc).timestamp() * 1000
        ten_days_ms = int(now_ms + 10 * 86_400_000)
        legs = [{
            'trade_id': 1, 'market_id': 'mkt_med_low',
            'outcome': 'YES', 'amount': 10.0,
            'probability': 0.5, 'entry_probability': 0.5,
            'status': 'OPEN',
            'timestamp': (datetime.now(timezone.utc) - timedelta(days=5)).isoformat(),
            'current_unrealised_pnl': -0.10,
            'current_probability': 0.49,
            'last_repriced_at': datetime.now(timezone.utc).isoformat(),
            'position_class': 'medium',
            'resolvability': 'low',   # must NOT trigger long penalty
            'close_time_ms': ten_days_ms,
            'is_resolved_on_api': False,
        }]
        self._write_state({'mkt_med_low': legs})
        h, c, n = run_evaluator(notify=self._notify, execute=False)
        # Correct score: -0.10 - 5*0.05 = -0.35 → HOLD (above -0.50)
        # Buggy score   : -0.10 - 5*0.05 - 0.50 = -0.85 → CLOSE
        self.assertEqual(n, 0, "medium term + low resolvability must remain 'medium'")

    def test_fallback_normalizes_obsolete_position_class_values(self):
        # Reviewer fix (Low): the fallback reducer (no close_time_ms) must
        # normalise legacy 4-bucket values through _normalize_position_class
        # before comparing. Without normalisation, a legacy leg with
        # position_class='long_reliable' (no close_time_ms) silently skips
        # LONG_HORIZON_PENALTY because position_score only matches the
        # exact 3-bucket name 'long_or_uncertain'.
        for legacy in ['long_reliable', 'long_risky', 'long_high', 'long_mid_low']:
            with self.subTest(legacy=legacy):
                legs = [_pos(market_id=f'mkt_{legacy}', trade_id=1,
                             unrealised=-0.10, days_ago=5,
                             position_class=legacy)]
                # Ensure no close_time_ms so the fallback path is exercised.
                legs[0].pop('close_time_ms', None)
                self._write_state({f'mkt_{legacy}': legs})
                if self.pending_path.exists():
                    self.pending_path.unlink()

                h, c, n = run_evaluator(notify=self._notify, execute=False)
                # With normalisation → long_or_uncertain → score
                #   -0.10 - 5*0.05 - 0.50 = -0.85 → CLOSE
                # Without normalisation → unknown class → score
                #   -0.10 - 5*0.05 = -0.35 → HOLD (bug)
                self.assertEqual(n, 1,
                                 f"{legacy!r} must normalise to long_or_uncertain")


# ── Autonomy change: evaluator executes its own close decisions ──────────────

class _FakeExecTrader:
    """Stand-in for PaperTrader in execute-mode evaluator tests.

    When given a state_path it mimics close_position_early's real persistence:
    every OPEN leg of the market becomes CLOSED_EARLY and the state file is
    rewritten — which is exactly what makes auto-close idempotent across runs.
    """

    def __init__(self, state_path=None, pnl=-3.0, fail_market_ids=()):
        self.balance = 100.0
        self.calls = []
        self.state_path = state_path
        self.pnl = pnl
        self.fail_market_ids = set(fail_market_ids)

    def close_position_early(self, market_id, current_prob, close_context=None):
        if market_id in self.fail_market_ids:
            raise RuntimeError('simulated close failure')
        self.calls.append((market_id, current_prob, close_context))
        self.balance += self.pnl
        if self.state_path and Path(self.state_path).exists():
            with open(self.state_path) as f:
                state = json.load(f)
            for leg in state.get('positions', {}).get(market_id, []):
                if leg.get('status') == 'OPEN':
                    leg['status'] = 'CLOSED_EARLY'
            with open(self.state_path, 'w') as f:
                json.dump(state, f)
        return self.pnl


class TestRunEvaluatorAutoExecute(unittest.TestCase):
    """Default evaluator behavior since the autonomy change: CLOSE decisions
    are executed in the same run; Telegram is notification-of-action, never a
    request for permission."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        tmp = Path(self.tmpdir.name)
        self.state_path = tmp / 'paper_trading_state.json'
        self.pending_path = tmp / 'pending_closes.json'
        self._patchers = [
            patch.object(evaluate_positions, '_STATE_PATH', self.state_path),
            patch.object(evaluate_positions, '_PENDING_CLOSES_PATH', self.pending_path),
            # execute_close refetches the market before closing; keep it off
            # the network and unresolved by default.
            patch.object(execute_close_mod.api_client, 'get_market',
                         return_value={'probability': 0.3, 'isResolved': False}),
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

    def _forbidden_trader_factory(self):
        self.fail('trader must not be instantiated in this scenario')

    # ── execute vs propose-only ──────────────────────────────────────────────

    def test_execute_mode_closes_losing_position_and_notifies_action(self):
        self._write_state(_state({
            'mkt_A': [_pos(market_id='mkt_A', unrealised=-3.0, days_ago=5,
                           question='Will Foo happen?')],
        }))
        trader = _FakeExecTrader(state_path=self.state_path)
        h, c, n = run_evaluator(notify=self._notify, execute=True,
                                trader_factory=lambda: trader)
        self.assertEqual((h, c, n), (0, 1, 1))

        # The close actually happened, with the why stamped through.
        self.assertEqual(len(trader.calls), 1)
        market_id, prob, ctx = trader.calls[0]
        self.assertEqual(market_id, 'mkt_A')
        self.assertEqual(prob, 0.3)
        self.assertEqual(ctx['close_type'], 'auto')
        self.assertIn('score_breakdown', ctx)

        # Audit record persisted as executed, with the score components the
        # learning loop needs.
        pending = self._read_pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['status'], 'executed')
        self.assertIn('executed_at', pending[0])
        breakdown = pending[0]['score_breakdown']
        self.assertEqual(breakdown['unrealised_pnl'], -3.0)
        self.assertEqual(breakdown['threshold'],
                         evaluate_positions.CLOSE_SCORE_THRESHOLD)

        # Telegram is a notification of action — never an approval ask.
        self.assertEqual(len(self.telegram_sent), 1)
        msg = self.telegram_sent[0]
        self.assertIn('Auto-closed', msg)
        self.assertIn('Why: score', msg)
        self.assertNotIn('approve close', msg)

    def test_propose_only_keeps_old_behavior_and_never_touches_trader(self):
        self._write_state(_state({
            'mkt_A': [_pos(market_id='mkt_A', unrealised=-3.0, days_ago=5)],
        }))
        h, c, n = run_evaluator(notify=self._notify, execute=False,
                                trader_factory=self._forbidden_trader_factory)
        self.assertEqual((h, c, n), (0, 1, 1))
        self.assertEqual(self._read_pending()[0]['status'], 'pending')
        self.assertIn('approve close', self.telegram_sent[0])

    # ── safety guards stay in force ──────────────────────────────────────────

    def test_min_hold_hours_respected_in_execute_mode(self):
        # Deeply losing but younger than MIN_HOLD_HOURS → HOLD, no execution,
        # no trader instantiation at all.
        self._write_state(_state({
            'mkt_A': [_pos(market_id='mkt_A', unrealised=-50.0, days_ago=0.02)],
        }))
        h, c, n = run_evaluator(notify=self._notify, execute=True,
                                trader_factory=self._forbidden_trader_factory)
        self.assertEqual((h, c, n), (1, 0, 0))
        self.assertEqual(self.telegram_sent, [])
        self.assertEqual(self._read_pending(), [])

    def test_execute_mode_is_idempotent_across_reruns(self):
        # First run closes; the state mutation means a re-run has nothing
        # left to close — exactly one close call total, no second notification.
        self._write_state(_state({
            'mkt_A': [_pos(market_id='mkt_A', unrealised=-3.0, days_ago=5)],
        }))
        trader = _FakeExecTrader(state_path=self.state_path)
        run_evaluator(notify=self._notify, execute=True,
                      trader_factory=lambda: trader)
        h, c, n = run_evaluator(notify=self._notify, execute=True,
                                trader_factory=lambda: trader)
        self.assertEqual((h, c, n), (0, 0, 0))
        self.assertEqual(len(trader.calls), 1)
        self.assertEqual(len(self.telegram_sent), 1)
        # Audit trail still shows exactly one executed record.
        executed = [p for p in self._read_pending() if p['status'] == 'executed']
        self.assertEqual(len(executed), 1)

    def test_dismissal_cooldown_respected_in_execute_mode(self):
        # A recent explicit human dismissal is real information — the bot
        # honours the cooldown instead of immediately overriding it.
        self._write_pending([{
            'id': 4, 'market_id': 'mkt_A', 'status': 'dismissed',
            'proposed_at': datetime.now().isoformat(),
            'dismissed_at': datetime.now().isoformat(),
        }])
        self._write_state(_state({
            'mkt_A': [_pos(market_id='mkt_A', unrealised=-3.0, days_ago=5)],
        }))
        h, c, n = run_evaluator(notify=self._notify, execute=True,
                                trader_factory=self._forbidden_trader_factory)
        self.assertEqual((c, n), (1, 0))
        self.assertEqual(self.telegram_sent, [])

    # ── transition off the approval regime ───────────────────────────────────

    def test_stale_pending_proposal_is_superseded_not_blocking(self):
        # Live-box transition case: an unactioned 'pending' proposal from the
        # approval era must not block the close. The bot executes and marks
        # the old ask superseded so execute_close.py --id N can't double-close.
        self._write_pending([{
            'id': 9, 'market_id': 'mkt_A', 'status': 'pending',
            'proposed_at': datetime.now().isoformat(),
        }])
        self._write_state(_state({
            'mkt_A': [_pos(market_id='mkt_A', unrealised=-3.0, days_ago=5)],
        }))
        trader = _FakeExecTrader(state_path=self.state_path)
        h, c, n = run_evaluator(notify=self._notify, execute=True,
                                trader_factory=lambda: trader)
        self.assertEqual(n, 1)
        pending = self._read_pending()
        by_id = {p['id']: p for p in pending}
        self.assertEqual(by_id[9]['status'], 'superseded')
        self.assertIn('superseded_at', by_id[9])
        executed = [p for p in pending if p['status'] == 'executed']
        self.assertEqual(len(executed), 1)
        self.assertEqual(executed[0]['market_id'], 'mkt_A')

    def test_expired_pending_proposals_still_auto_expire(self):
        old_iso = (datetime.now() - timedelta(hours=CLOSE_EXPIRY_HOURS + 1)).isoformat()
        self._write_pending([{
            'id': 3, 'market_id': 'mkt_gone', 'status': 'pending',
            'proposed_at': old_iso,
        }])
        self._write_state(_state({}))
        run_evaluator(notify=self._notify, execute=True,
                      trader_factory=self._forbidden_trader_factory)
        self.assertEqual(self._read_pending()[0]['status'], 'expired')

    # ── failure containment ──────────────────────────────────────────────────

    def test_close_failure_is_contained_and_other_markets_still_close(self):
        self._write_state(_state({
            'mkt_A': [_pos(market_id='mkt_A', trade_id=1, unrealised=-3.0, days_ago=5)],
            'mkt_B': [_pos(market_id='mkt_B', trade_id=2, unrealised=-3.0, days_ago=5)],
        }))
        trader = _FakeExecTrader(state_path=self.state_path,
                                 fail_market_ids={'mkt_A'})
        # Must not raise despite mkt_A's close blowing up.
        h, c, n = run_evaluator(notify=self._notify, execute=True,
                                trader_factory=lambda: trader)
        self.assertEqual((h, c, n), (0, 2, 1))

        statuses = {p['market_id']: p['status'] for p in self._read_pending()}
        self.assertEqual(statuses['mkt_A'], 'failed')
        self.assertEqual(statuses['mkt_B'], 'executed')
        self.assertTrue(any('FAILED' in m for m in self.telegram_sent))
        self.assertTrue(any('Auto-closed' in m for m in self.telegram_sent))

    def test_resolved_market_is_skipped_not_closed(self):
        self._write_state(_state({
            'mkt_A': [_pos(market_id='mkt_A', unrealised=-3.0, days_ago=5)],
        }))
        trader = _FakeExecTrader(state_path=self.state_path)
        with patch.object(execute_close_mod.api_client, 'get_market',
                          return_value={'probability': 0.3, 'isResolved': True,
                                        'resolution': 'YES'}):
            h, c, n = run_evaluator(notify=self._notify, execute=True,
                                    trader_factory=lambda: trader)
        self.assertEqual(n, 0)
        self.assertEqual(trader.calls, [])
        self.assertEqual(self._read_pending()[0]['status'], 'skipped_resolved')


# ── Phase 2.3b: re-validation is the approver ────────────────────────────────

class TestRevalidationApprover(unittest.TestCase):
    """The two-stage propose→approve shape from PR #15 survives, but the
    approver is a machine step: a fresh live refetch (not the :05 snapshot)
    must confirm the breach before any close executes.

      - clear-margin breach (fresh score <= threshold ×
        CLOSE_CONFIRM_MARGIN_FACTOR) → executes in the same run
      - borderline breach → recorded, waits one cycle, executes next run
        only if a fresh refetch still breaches
      - recovery on fresh data → no close, ever
      - failed refetch → defer; the evaluator NEVER executes on the stale
        snapshot alone
    """

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

    def _read_pending(self):
        if not self.pending_path.exists():
            return []
        with open(self.pending_path) as f:
            return json.load(f)

    def _forbidden_trader_factory(self):
        self.fail('trader must not be instantiated in this scenario')

    @staticmethod
    def _market(prob, resolved=False):
        return {'probability': prob, 'isResolved': resolved}

    def _patch_prob(self, prob):
        """Patch the shared api_client so the re-validation refetch (and
        execute_close's own pre-close refetch) sees a live probability."""
        return patch.object(execute_close_mod.api_client, 'get_market',
                            return_value=self._market(prob))

    # Snapshot setup used throughout: YES @ entry 0.5, $10, 5 days old,
    # class 'short' → decay 5 × 0.05 = 0.25.
    #   snapshot unrealised -3.00 → snapshot score -3.25 (deep breach)
    #   snapshot unrealised -0.50 → snapshot score -0.75 (breach, not deep)
    # Fresh refetch at probability p → fresh pnl = 10·p/0.5 − 10 = 20p − 10:
    #   p=0.300 → pnl −4.00 → fresh score −4.25  (clear margin, ≤ −1.00)
    #   p=0.475 → pnl −0.50 → fresh score −0.75  (borderline: −1.00 < s < −0.50)
    #   p=0.550 → pnl +1.00 → fresh score +0.75  (recovered)

    def _write_losing_state(self, unrealised=-0.5):
        self._write_state(_state({
            'mkt_A': [_pos(market_id='mkt_A', unrealised=unrealised, days_ago=5,
                           question='Will Foo happen?')],
        }))

    # ── immediate clear-margin path ──────────────────────────────────────────

    def test_clear_margin_breach_executes_in_same_run(self):
        self._write_losing_state(unrealised=-3.0)
        trader = _FakeExecTrader(state_path=self.state_path)
        with self._patch_prob(0.3):
            h, c, n = run_evaluator(notify=self._notify, execute=True,
                                    trader_factory=lambda: trader)
        self.assertEqual((h, c, n), (0, 1, 1))
        self.assertEqual(len(trader.calls), 1)

        record = self._read_pending()[0]
        self.assertEqual(record['status'], 'executed')
        self.assertEqual(record['revalidation']['rule'], 'immediate_clear_margin')
        self.assertAlmostEqual(record['revalidation']['fresh_probability'], 0.3)
        self.assertAlmostEqual(record['revalidation']['fresh_score'], -4.25, places=2)

        # The rule that fired is stamped through to the closed trade records.
        _, _, ctx = trader.calls[0]
        self.assertEqual(ctx['revalidation'], 'immediate_clear_margin')
        self.assertAlmostEqual(ctx['revalidation_fresh_score'], -4.25, places=2)

        # Notification carries the re-validation evidence.
        self.assertEqual(len(self.telegram_sent), 1)
        self.assertIn('Re-validated live: immediate_clear_margin', self.telegram_sent[0])

    # ── borderline: wait one cycle, then persistence decides ─────────────────

    def test_borderline_first_crossing_waits_one_cycle(self):
        self._write_losing_state(unrealised=-0.5)
        with self._patch_prob(0.475):
            h, c, n = run_evaluator(notify=self._notify, execute=True,
                                    trader_factory=self._forbidden_trader_factory)
        self.assertEqual((h, c, n), (0, 1, 0))

        pending = self._read_pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['status'], 'pending')
        self.assertEqual(pending[0]['revalidation']['status'], 'borderline_breach')
        self.assertIsNone(pending[0]['revalidation']['rule'])
        # No Telegram noise for "will confirm next cycle".
        self.assertEqual(self.telegram_sent, [])

    def test_borderline_persists_and_executes_next_cycle(self):
        self._write_losing_state(unrealised=-0.5)
        with self._patch_prob(0.475):
            run_evaluator(notify=self._notify, execute=True,
                          trader_factory=self._forbidden_trader_factory)

        trader = _FakeExecTrader(state_path=self.state_path)
        with self._patch_prob(0.475):
            h, c, n = run_evaluator(notify=self._notify, execute=True,
                                    trader_factory=lambda: trader)
        self.assertEqual(n, 1)
        self.assertEqual(len(trader.calls), 1)

        by_status = {}
        for p in self._read_pending():
            by_status.setdefault(p['status'], []).append(p)
        # The cycle-1 proposal is superseded by the cycle-2 execution record.
        self.assertEqual(len(by_status.get('superseded', [])), 1)
        executed = by_status.get('executed', [])
        self.assertEqual(len(executed), 1)
        self.assertEqual(executed[0]['revalidation']['rule'], 'persisted_next_cycle')
        self.assertTrue(any('persisted_next_cycle' in m for m in self.telegram_sent))

    def test_borderline_recovers_on_fresh_refetch_no_close(self):
        self._write_losing_state(unrealised=-0.5)
        with self._patch_prob(0.475):
            run_evaluator(notify=self._notify, execute=True,
                          trader_factory=self._forbidden_trader_factory)

        # Next cycle: snapshot still says CLOSE (state unchanged), but the
        # live market has moved back in our favour → no close, proposal ends
        # 'recovered' (terminal — a future breach must re-prove itself).
        with self._patch_prob(0.55):
            h, c, n = run_evaluator(notify=self._notify, execute=True,
                                    trader_factory=self._forbidden_trader_factory)
        self.assertEqual(n, 0)

        pending = self._read_pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['status'], 'recovered')
        self.assertEqual(pending[0]['revalidation']['status'], 'recovered')
        self.assertEqual(self.telegram_sent, [])

    def test_snapshot_recovery_marks_pending_recovered(self):
        # Variant: the recovery shows up in the :05 snapshot itself (HOLD
        # with score_above_threshold) — the waiting proposal is terminated
        # without any refetch.
        self._write_losing_state(unrealised=-0.5)
        with self._patch_prob(0.475):
            run_evaluator(notify=self._notify, execute=True,
                          trader_factory=self._forbidden_trader_factory)

        self._write_losing_state(unrealised=+1.0)
        with self._patch_prob(0.475):
            h, c, n = run_evaluator(notify=self._notify, execute=True,
                                    trader_factory=self._forbidden_trader_factory)
        self.assertEqual((h, c, n), (1, 0, 0))
        record = self._read_pending()[0]
        self.assertEqual(record['status'], 'recovered')
        self.assertIn('above threshold', record['note'])

    # ── refetch failure: defer, never execute on stale data ──────────────────

    def test_refetch_failure_defers_and_never_executes_on_stale_data(self):
        self._write_losing_state(unrealised=-3.0)  # deep breach on the snapshot

        with patch.object(execute_close_mod.api_client, 'get_market',
                          side_effect=ConnectionError('api down')):
            h, c, n = run_evaluator(notify=self._notify, execute=True,
                                    trader_factory=self._forbidden_trader_factory)
        self.assertEqual((h, c, n), (0, 1, 0))
        pending = self._read_pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['status'], 'pending')
        self.assertEqual(pending[0]['revalidation']['status'], 'refetch_failed')

        # Still failing next cycle: defer again on the SAME record — no
        # duplicate proposals, still no execution.
        with patch.object(execute_close_mod.api_client, 'get_market',
                          side_effect=ConnectionError('api still down')):
            h, c, n = run_evaluator(notify=self._notify, execute=True,
                                    trader_factory=self._forbidden_trader_factory)
        self.assertEqual(n, 0)
        pending = self._read_pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['status'], 'pending')

        # API back: the prior pending acts as the persistence signal and the
        # close finally executes — on FRESH data.
        trader = _FakeExecTrader(state_path=self.state_path)
        with self._patch_prob(0.3):
            h, c, n = run_evaluator(notify=self._notify, execute=True,
                                    trader_factory=lambda: trader)
        self.assertEqual(n, 1)
        self.assertEqual(len(trader.calls), 1)
        executed = [p for p in self._read_pending() if p['status'] == 'executed']
        self.assertEqual(len(executed), 1)
        self.assertEqual(executed[0]['revalidation']['rule'], 'persisted_next_cycle')

    def test_market_payload_without_probability_defers(self):
        # A fetch that "succeeds" but has no usable probability is the same
        # as a failed fetch: defer, don't act.
        self._write_losing_state(unrealised=-3.0)
        with patch.object(execute_close_mod.api_client, 'get_market',
                          return_value={'isResolved': False}):
            h, c, n = run_evaluator(notify=self._notify, execute=True,
                                    trader_factory=self._forbidden_trader_factory)
        self.assertEqual(n, 0)
        self.assertEqual(self._read_pending()[0]['revalidation']['status'],
                         'refetch_failed')

    # ── propose-only mode never refetches ────────────────────────────────────

    def test_propose_only_mode_does_not_refetch(self):
        self._write_losing_state(unrealised=-3.0)
        with patch.object(execute_close_mod.api_client, 'get_market',
                          side_effect=AssertionError('propose-only must not hit the API')):
            h, c, n = run_evaluator(notify=self._notify, execute=False,
                                    trader_factory=self._forbidden_trader_factory)
        self.assertEqual((h, c, n), (0, 1, 1))
        self.assertEqual(self._read_pending()[0]['status'], 'pending')


class TestCloseContextStamping(unittest.TestCase):
    """PaperTrader.close_position_early stamps close_context onto the closed
    trade records so the learning loop can study close-time reasoning."""

    def _trader_with_open(self):
        trader = paper_trader.PaperTrader.__new__(paper_trader.PaperTrader)
        trader.initial_balance = 100.0
        trader.balance = 90.0
        trader.positions = {}
        trader.trade_history = []
        trader.performance_metrics = {}
        trader.capital_epochs = []
        trader.state_file = tempfile.mktemp(suffix='.json')
        trade = {
            'trade_id': 1,
            'timestamp': datetime.now().isoformat(),
            'market_id': 'mkt_ctx',
            'outcome': 'YES',
            'amount': 10.0,
            'probability': 0.5,
            'entry_probability': 0.5,
            'status': 'OPEN',
        }
        trader.positions['mkt_ctx'] = [trade]
        trader.trade_history.append(trade)
        return trader

    def test_close_context_lands_on_trade_record(self):
        trader = self._trader_with_open()
        ctx = {'close_type': 'auto', 'position_score': -1.2, 'close_reason': 'pnl=$-1.00, age=4.0d'}
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(paper_trader, '_DB_PATH', Path(tmp) / 'cal.db'):
                trader.close_position_early('mkt_ctx', 0.4, close_context=ctx)
        self.assertEqual(trader.positions['mkt_ctx'][0]['close_context'], ctx)

    def test_omitting_close_context_leaves_trade_unchanged(self):
        trader = self._trader_with_open()
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(paper_trader, '_DB_PATH', Path(tmp) / 'cal.db'):
                trader.close_position_early('mkt_ctx', 0.4)
        self.assertNotIn('close_context', trader.positions['mkt_ctx'][0])


if __name__ == '__main__':
    unittest.main()
