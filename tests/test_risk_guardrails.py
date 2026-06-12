#!/usr/bin/env python3
"""
Tests for per-trade risk assessment + drawdown circuit breaker (workbench-g4wz).

Covers, with NO network and NO real state file:

  1. manifold_bot/risk.py pure functions — exposure accounting, the three
     per-trade gates (size vs balance %, per-market exposure, total
     concurrent exposure), and the breaker hysteresis state machine.
  2. PaperTrader.check_circuit_breaker — lazy init (peak = current equity so
     deploying onto an already-down book doesn't instantly halt), peak
     ratchet, capital-epoch reset, a synthetic drawdown that TRIPS the
     breaker, and hysteresis recovery.
  3. PaperTrader.place_paper_bet — refuses trades on each gate, refuses while
     the breaker is tripped, and stamps the additive `risk` snapshot on every
     approved trade record (same consumer contract as `inference_cost`:
     legacy rows simply lack the field).
  4. State-file round trip — `risk_state` persists and reloads; pre-guardrail
     state files load unchanged.
  5. AutoTrader.run_trading_cycle — halts the whole cycle (before research
     load) when the breaker is tripped, and attributes per-trade risk
     rejections to the `risk_guard` counter.
"""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from manifold_bot.config import (
    DRAWDOWN_HALT_PCT,
    DRAWDOWN_RESUME_PCT,
    MAX_MARKET_EXPOSURE_PCT,
    MAX_TOTAL_EXPOSURE_PCT,
    MAX_TRADE_BALANCE_PCT,
)
from manifold_bot.paper_trader import PaperTrader
from manifold_bot.risk import (
    assess_trade_risk,
    drawdown_pct,
    equity,
    evaluate_circuit_breaker,
    market_open_exposure,
    open_exposure,
)


def _pos(amount, status='OPEN'):
    return {'amount': amount, 'status': status, 'outcome': 'YES'}


def _make_trader(balance=1000.0, positions=None, capital_epochs=None,
                 risk_state=None):
    """Stub PaperTrader without touching the real state file — same isolation
    pattern as tests/test_inference_cost.py / test_resolution_types.py."""
    with patch.object(PaperTrader, "__init__", lambda self_: None):
        trader = PaperTrader()
    trader.initial_balance = 1000.0
    trader.balance = balance
    trader.positions = positions or {}
    trader.trade_history = []
    trader.capital_epochs = capital_epochs or []
    trader.risk_state = risk_state
    trader.save_state = lambda: None
    return trader


# ── 1. Pure risk math ─────────────────────────────────────────────────────────

class TestExposureAccounting(unittest.TestCase):

    def test_only_open_positions_count(self):
        positions = {
            'm1': [_pos(5), _pos(5, status='WIN')],
            'm2': [_pos(3, status='LOSS'), _pos(2)],
            'm3': [_pos(4, status='CLOSED_EARLY')],
        }
        self.assertAlmostEqual(open_exposure(positions), 7.0)
        self.assertAlmostEqual(market_open_exposure(positions, 'm1'), 5.0)
        self.assertAlmostEqual(market_open_exposure(positions, 'm3'), 0.0)
        self.assertAlmostEqual(equity(100.0, positions), 107.0)

    def test_empty_book(self):
        self.assertEqual(open_exposure({}), 0.0)
        self.assertEqual(open_exposure(None), 0.0)
        self.assertEqual(market_open_exposure({}, 'm1'), 0.0)


class TestAssessTradeRisk(unittest.TestCase):

    def test_clean_pass_returns_fields(self):
        reason, fields = assess_trade_risk(5.0, 1000.0, {}, 'm1')
        self.assertIsNone(reason)
        self.assertEqual(fields['balance_before'], 1000.0)
        self.assertAlmostEqual(fields['trade_pct_of_balance'], 0.005)
        self.assertEqual(fields['open_positions_before'], 0)
        self.assertEqual(fields['open_exposure_before'], 0.0)
        self.assertEqual(fields['market_exposure_before'], 0.0)
        self.assertEqual(
            fields['limits']['max_trade_balance_pct'], MAX_TRADE_BALANCE_PCT)
        self.assertEqual(
            fields['limits']['drawdown_halt_pct'], DRAWDOWN_HALT_PCT)

    def test_size_above_balance_pct_rejected(self):
        # $5 on a $40 balance is 12.5% > the 10% per-trade cap.
        reason, _ = assess_trade_risk(5.0, 40.0, {}, 'm1')
        self.assertEqual(reason, 'risk_size_pct')

    def test_size_exactly_at_cap_passes(self):
        # Inclusive boundary: exactly MAX_TRADE_BALANCE_PCT of balance is OK.
        reason, _ = assess_trade_risk(50.0 * MAX_TRADE_BALANCE_PCT, 50.0, {}, 'm1')
        self.assertNotEqual(reason, 'risk_size_pct')

    def test_zero_or_negative_balance_rejected(self):
        self.assertEqual(assess_trade_risk(1.0, 0.0, {}, 'm1')[0], 'risk_size_pct')
        self.assertEqual(assess_trade_risk(1.0, -5.0, {}, 'm1')[0], 'risk_size_pct')

    def test_per_market_exposure_rejected(self):
        # balance 50, $4 already OPEN in m1 → equity 54, market cap $5.40.
        # Adding $5 would take m1 to $9 — over the cap. A different market
        # with the same book passes.
        positions = {'m1': [_pos(4.0)]}
        reason, fields = assess_trade_risk(5.0, 50.0, positions, 'm1')
        self.assertEqual(reason, 'risk_market_exposure')
        self.assertAlmostEqual(fields['market_exposure_before'], 4.0)
        reason_other, _ = assess_trade_risk(5.0, 50.0, positions, 'm2')
        self.assertIsNone(reason_other)

    def test_total_exposure_rejected(self):
        # 9 markets, $41 OPEN total, balance 50 → equity 91, total cap $45.50.
        # Adding $5 → $46 deployed: over the cap. Size gate passes ($5 = 10%
        # of $50); the new market has no prior exposure so the market gate
        # passes; only the TOTAL gate fires.
        positions = {f'm{i}': [_pos(5.0)] for i in range(8)}
        positions['m8'] = [_pos(1.0)]
        reason, fields = assess_trade_risk(5.0, 50.0, positions, 'new_mkt')
        self.assertEqual(reason, 'risk_total_exposure')
        self.assertAlmostEqual(fields['open_exposure_before'], 41.0)

    def test_closed_positions_do_not_fill_caps(self):
        positions = {'m1': [_pos(100.0, status='WIN'), _pos(200.0, status='LOSS')]}
        reason, _ = assess_trade_risk(5.0, 1000.0, positions, 'm1')
        self.assertIsNone(reason)


class TestBreakerStateMachine(unittest.TestCase):

    def test_drawdown_pct(self):
        self.assertAlmostEqual(drawdown_pct(750.0, 1000.0), 0.25)
        self.assertEqual(drawdown_pct(1100.0, 1000.0), 0.0)  # above peak
        self.assertEqual(drawdown_pct(500.0, 0.0), 0.0)      # invalid peak
        self.assertEqual(drawdown_pct(500.0, None), 0.0)

    def test_trips_at_halt_threshold(self):
        eq = 1000.0 * (1.0 - DRAWDOWN_HALT_PCT)
        tripped, dd = evaluate_circuit_breaker(eq, 1000.0, already_tripped=False)
        self.assertTrue(tripped)
        self.assertAlmostEqual(dd, DRAWDOWN_HALT_PCT)

    def test_does_not_trip_below_threshold(self):
        eq = 1000.0 * (1.0 - DRAWDOWN_HALT_PCT) + 1.0
        tripped, _ = evaluate_circuit_breaker(eq, 1000.0, already_tripped=False)
        self.assertFalse(tripped)

    def test_hysteresis_stays_tripped_between_resume_and_halt(self):
        # Drawdown recovered to 22% — between resume (20%) and halt (25%).
        # A tripped breaker must STAY tripped; an untripped one must not trip.
        eq = 1000.0 * (1.0 - (DRAWDOWN_RESUME_PCT + DRAWDOWN_HALT_PCT) / 2.0)
        self.assertTrue(evaluate_circuit_breaker(eq, 1000.0, already_tripped=True)[0])
        self.assertFalse(evaluate_circuit_breaker(eq, 1000.0, already_tripped=False)[0])

    def test_clears_at_resume_threshold(self):
        eq = 1000.0 * (1.0 - DRAWDOWN_RESUME_PCT)
        tripped, _ = evaluate_circuit_breaker(eq, 1000.0, already_tripped=True)
        self.assertFalse(tripped)


# ── 2. PaperTrader.check_circuit_breaker ─────────────────────────────────────

class TestCheckCircuitBreaker(unittest.TestCase):

    def test_first_run_initializes_peak_to_current_equity(self):
        # Deploying onto a book that is already down must NOT trip the
        # breaker — peak starts at today's equity, not the historical baseline.
        trader = _make_trader(balance=425.0, positions={'m1': [_pos(5.0)]})
        self.assertFalse(trader.check_circuit_breaker())
        self.assertAlmostEqual(trader.risk_state['peak_equity'], 430.0)
        self.assertFalse(trader.risk_state['breaker_tripped'])

    def test_peak_ratchets_up_with_equity(self):
        trader = _make_trader(balance=1000.0)
        trader.check_circuit_breaker()
        trader.balance = 1200.0
        trader.check_circuit_breaker()
        self.assertAlmostEqual(trader.risk_state['peak_equity'], 1200.0)

    def test_synthetic_drawdown_trips_breaker(self):
        # Peak $1000, equity $700 → 30% drawdown ≥ 25% halt threshold.
        trader = _make_trader(balance=1000.0)
        self.assertFalse(trader.check_circuit_breaker())
        trader.balance = 700.0  # synthetic loss
        with patch('manifold_bot.paper_trader._notify_breaker') as notify:
            self.assertTrue(trader.check_circuit_breaker())
            notify.assert_called_once()
        rs = trader.risk_state
        self.assertTrue(rs['breaker_tripped'])
        self.assertIsNotNone(rs['breaker_tripped_at'])
        self.assertAlmostEqual(rs['last_drawdown_pct'], 0.30)

    def test_recovery_clears_breaker_with_hysteresis(self):
        trader = _make_trader(balance=700.0,
                              risk_state={'peak_equity': 1000.0, 'epoch_count': 0,
                                          'breaker_tripped': True,
                                          'breaker_tripped_at': '2026-06-12T00:00:00',
                                          'breaker_cleared_at': None,
                                          'last_drawdown_pct': 0.30})
        # 22% drawdown: between resume (20%) and halt (25%) — still halted.
        trader.balance = 780.0
        self.assertTrue(trader.check_circuit_breaker())
        # 15% drawdown: recovered past the resume threshold — cleared.
        trader.balance = 850.0
        self.assertFalse(trader.check_circuit_breaker())
        self.assertIsNotNone(trader.risk_state['breaker_cleared_at'])

    def test_capital_epoch_reset_clears_breaker_and_peak(self):
        trader = _make_trader(balance=700.0,
                              risk_state={'peak_equity': 1000.0, 'epoch_count': 0,
                                          'breaker_tripped': True,
                                          'breaker_tripped_at': '2026-06-12T00:00:00',
                                          'breaker_cleared_at': None,
                                          'last_drawdown_pct': 0.30})
        trader.capital_epochs = [
            {'started_at': '2026-06-12T08:00:00', 'topup_from': 700.0,
             'topup_to': 1000.0, 'note': 'deliberate re-stake'}
        ]
        trader.balance = 1000.0
        self.assertFalse(trader.check_circuit_breaker())
        self.assertFalse(trader.risk_state['breaker_tripped'])
        self.assertEqual(trader.risk_state['epoch_count'], 1)
        self.assertAlmostEqual(trader.risk_state['peak_equity'], 1000.0)


# ── 3. place_paper_bet enforcement + risk snapshot ───────────────────────────

class TestPlacePaperBetRiskGuards(unittest.TestCase):

    def test_refuses_oversized_trade_vs_balance(self):
        trader = _make_trader(balance=40.0)
        ok = trader.place_paper_bet('m1', 'YES', 5.0, 0.5)
        self.assertFalse(ok)
        self.assertEqual(trader.trade_history, [])
        self.assertEqual(trader.balance, 40.0)  # nothing deducted

    def test_refuses_per_market_exposure(self):
        trader = _make_trader(balance=50.0, positions={'m1': [_pos(4.0)]})
        self.assertFalse(trader.place_paper_bet('m1', 'YES', 5.0, 0.5))
        # Same trade in an unexposed market is fine.
        self.assertTrue(trader.place_paper_bet('m2', 'YES', 5.0, 0.5))

    def test_refuses_total_exposure(self):
        positions = {f'm{i}': [_pos(5.0)] for i in range(8)}
        positions['m8'] = [_pos(1.0)]
        trader = _make_trader(balance=50.0, positions=positions)
        self.assertFalse(trader.place_paper_bet('new_mkt', 'YES', 5.0, 0.5))

    def test_refuses_while_breaker_tripped(self):
        trader = _make_trader(balance=700.0,
                              risk_state={'peak_equity': 1000.0, 'epoch_count': 0,
                                          'breaker_tripped': True,
                                          'breaker_tripped_at': '2026-06-12T00:00:00',
                                          'breaker_cleared_at': None,
                                          'last_drawdown_pct': 0.30})
        ok = trader.place_paper_bet('m1', 'YES', 5.0, 0.5)
        self.assertFalse(ok)
        self.assertEqual(trader.trade_history, [])

    def test_approved_trade_carries_risk_snapshot(self):
        trader = _make_trader(balance=1000.0)
        ok = trader.place_paper_bet('m1', 'YES', 5.0, 0.5, ai_status='agree')
        self.assertTrue(ok)
        trade = trader.trade_history[-1]
        risk = trade['risk']
        self.assertEqual(risk['balance_before'], 1000.0)
        self.assertAlmostEqual(risk['trade_pct_of_balance'], 0.005)
        self.assertEqual(risk['open_positions_before'], 0)
        self.assertEqual(risk['open_exposure_before'], 0.0)
        self.assertEqual(risk['drawdown_pct_at_entry'], 0.0)
        self.assertAlmostEqual(risk['peak_equity'], 1000.0)
        self.assertEqual(risk['limits']['max_total_exposure_pct'],
                         MAX_TOTAL_EXPOSURE_PCT)
        self.assertEqual(risk['limits']['max_market_exposure_pct'],
                         MAX_MARKET_EXPOSURE_PCT)
        # The snapshot must be JSON-serializable — it lives in
        # paper_trading_state.json.
        json.dumps(risk)

    def test_legacy_trade_rows_without_risk_field_are_fine(self):
        # The contract is additive: consumers .get('risk') — make sure
        # nothing in the trader itself assumes the field exists on old rows.
        trader = _make_trader(balance=1000.0,
                              positions={'old': [{'amount': 5.0, 'status': 'OPEN',
                                                  'outcome': 'YES'}]})
        self.assertTrue(trader.place_paper_bet('m1', 'YES', 5.0, 0.5))


# ── 4. State-file round trip ─────────────────────────────────────────────────

class TestRiskStatePersistence(unittest.TestCase):

    def _trader_with_tmp_state(self, path):
        with patch.object(PaperTrader, "__init__", lambda self_: None):
            trader = PaperTrader()
        trader.initial_balance = 1000.0
        trader.balance = 1000.0
        trader.positions = {}
        trader.trade_history = []
        trader.capital_epochs = []
        trader.risk_state = None
        trader.state_file = path
        return trader

    def test_risk_state_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'state.json')
            t1 = self._trader_with_tmp_state(path)
            t1.check_circuit_breaker()  # initializes + saves
            self.assertTrue(os.path.exists(path))

            t2 = self._trader_with_tmp_state(path)
            t2.load_state()
            self.assertIsNotNone(t2.risk_state)
            self.assertAlmostEqual(t2.risk_state['peak_equity'], 1000.0)
            self.assertFalse(t2.risk_state['breaker_tripped'])

    def test_pre_guardrail_state_file_loads_clean(self):
        # A state file written before this feature has no risk_state key —
        # it must load with risk_state=None and save without inventing one
        # until the breaker is first evaluated.
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'state.json')
            with open(path, 'w') as f:
                json.dump({'balance': 425.0, 'positions': {},
                           'trade_history': [], 'capital_epochs': []}, f)
            t = self._trader_with_tmp_state(path)
            t.load_state()
            self.assertIsNone(t.risk_state)
            t.save_state()
            with open(path) as f:
                self.assertNotIn('risk_state', json.load(f))


# ── 5. AutoTrader integration ────────────────────────────────────────────────

class TestAutoTraderDrawdownHalt(unittest.TestCase):

    def _make_auto_trader(self, trader):
        from automation import auto_trader as at_mod
        with patch.object(at_mod.PaperTrader, '__init__', lambda self_: None):
            at = at_mod.AutoTrader()
        at.trader = trader
        return at, at_mod

    def test_cycle_halts_before_research_when_breaker_tripped(self):
        trader = _make_trader(balance=700.0,
                              risk_state={'peak_equity': 1000.0, 'epoch_count': 0,
                                          'breaker_tripped': True,
                                          'breaker_tripped_at': '2026-06-12T00:00:00',
                                          'breaker_cleared_at': None,
                                          'last_drawdown_pct': 0.30})
        at, at_mod = self._make_auto_trader(trader)
        captured = {}

        def _capture(counters):
            captured.update(counters)

        with patch.object(at_mod, '_append_trader_counters', _capture), \
             patch.object(at, 'load_latest_research',
                          side_effect=AssertionError('research must not load when halted')):
            result = at.run_trading_cycle()

        self.assertEqual(result, 0)
        self.assertTrue(captured.get('drawdown_halted'))
        self.assertEqual(captured.get('trades_executed'), 0)

    def test_cycle_proceeds_when_breaker_not_tripped(self):
        trader = _make_trader(balance=1000.0)
        at, at_mod = self._make_auto_trader(trader)
        captured = {}

        def _capture(counters):
            captured.update(counters)

        with patch.object(at_mod, '_append_trader_counters', _capture), \
             patch.object(at, 'load_latest_research', return_value=None):
            result = at.run_trading_cycle()

        self.assertEqual(result, 0)
        self.assertFalse(captured.get('drawdown_halted'))

    def test_execute_trade_attributes_risk_guard_counter(self):
        # Balance $40 → a $5 trade is 12.5% of balance, over the 10% cap.
        # execute_trade's pre-flight must attribute the skip to risk_guard
        # (the authoritative copy in place_paper_bet would refuse it anyway).
        from automation.auto_trader import _new_trader_counters
        trader = _make_trader(balance=40.0)
        at, at_mod = self._make_auto_trader(trader)
        counters = _new_trader_counters()
        rec = {
            'market_id': 'mkt1',
            'recommendation': 'YES',
            'confidence': 0.7,
            'probability': 0.5,
            'question': 'Will the thing happen?',
        }
        fake_market = {'probability': 0.5, 'isResolved': False,
                       'closeTime': 9e15}
        with patch.object(at_mod.api_client, 'get_market', return_value=fake_market), \
             patch.object(at, 'calculate_position_size', return_value=5.0):
            ok = at.execute_trade(rec, counters=counters)

        self.assertFalse(ok)
        self.assertEqual(counters['rejected']['risk_guard'], 1)
        self.assertEqual(trader.trade_history, [])


if __name__ == '__main__':
    unittest.main()
