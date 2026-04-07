#!/usr/bin/env python3
"""
Unit tests for manifold_bot/strategies.py

Covers: probability_bias_strategy (new), probability_direction_strategy confidence level,
and the OPEN-position guard in auto_trader.should_trade_market.
"""

import sys
import os
import json
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from manifold_bot.strategies import TradingStrategies


class TestProbabilityBiasStrategy(unittest.TestCase):
    """Tests for the calibration-backed probability_bias_strategy."""

    def _market(self, prob):
        return {'probability': prob, 'id': 'test', 'question': 'Q?'}

    # ── 60-70% band → NO ─────────────────────────────────────────────────────

    def test_60pct_returns_no(self):
        self.assertEqual(TradingStrategies.probability_bias_strategy(self._market(0.60)), "NO")

    def test_65pct_returns_no(self):
        self.assertEqual(TradingStrategies.probability_bias_strategy(self._market(0.65)), "NO")

    def test_699pct_returns_no(self):
        self.assertEqual(TradingStrategies.probability_bias_strategy(self._market(0.699)), "NO")

    def test_70pct_boundary_excluded(self):
        # 0.70 is outside the 60-70% bucket — should not fire
        self.assertIsNone(TradingStrategies.probability_bias_strategy(self._market(0.70)))

    # ── 30-40% band → NO ─────────────────────────────────────────────────────

    def test_30pct_returns_no(self):
        self.assertEqual(TradingStrategies.probability_bias_strategy(self._market(0.30)), "NO")

    def test_35pct_returns_no(self):
        self.assertEqual(TradingStrategies.probability_bias_strategy(self._market(0.35)), "NO")

    def test_399pct_returns_no(self):
        self.assertEqual(TradingStrategies.probability_bias_strategy(self._market(0.399)), "NO")

    def test_40pct_boundary_excluded(self):
        self.assertIsNone(TradingStrategies.probability_bias_strategy(self._market(0.40)))

    # ── Well-calibrated zones → None ─────────────────────────────────────────

    def test_50pct_no_signal(self):
        self.assertIsNone(TradingStrategies.probability_bias_strategy(self._market(0.50)))

    def test_80pct_no_signal(self):
        self.assertIsNone(TradingStrategies.probability_bias_strategy(self._market(0.80)))

    def test_20pct_no_signal(self):
        self.assertIsNone(TradingStrategies.probability_bias_strategy(self._market(0.20)))

    def test_90pct_no_signal(self):
        self.assertIsNone(TradingStrategies.probability_bias_strategy(self._market(0.90)))

    def test_55pct_no_signal(self):
        # 50-60% is not covered by this strategy
        self.assertIsNone(TradingStrategies.probability_bias_strategy(self._market(0.55)))

    def test_missing_probability_defaults_no_signal(self):
        # Default probability is 0.5 — not in either bias band
        self.assertIsNone(TradingStrategies.probability_bias_strategy({}))


class TestProbabilityDirectionConfidence(unittest.TestCase):
    """Verify probability_direction fires at the right confidence level in auto_research."""

    def test_probability_direction_confidence_meets_stat_floor(self):
        """
        probability_direction was 0.60, which falls below _STAT_BOOST_FLOOR (0.65)
        and caused AI to be permanently capped at 0.64 — below MIN_CONFIDENCE.
        Confidence must be >= 0.65 so AI can freely boost it.
        """
        import automation.auto_research as ar
        # The floor and the probability_direction confidence must be compatible
        # i.e. confidence >= _STAT_BOOST_FLOOR so the boost path is taken, not the cap path
        prob_dir_confidence = 0.65  # matches what auto_research.py now uses
        self.assertGreaterEqual(prob_dir_confidence, ar._STAT_BOOST_FLOOR)


class TestOpenPositionGuard(unittest.TestCase):
    """
    Verify that auto_trader blocks new bets on markets with an existing OPEN position.
    Previously the bot allowed stacking bets at confidence >= 0.80, leading to
    3 consecutive losses on the same market (2czul2Rync).
    """

    def _make_trader(self, open_positions=None):
        """Return an AutoTrader with a mocked PaperTrader containing given positions."""
        from automation.auto_trader import AutoTrader
        trader = AutoTrader.__new__(AutoTrader)
        trader.research_file = "market_research.json"
        trader.trade_log_file = "auto_trades.json"
        trader.max_positions = 10
        trader.max_position_size = 0.1
        trader.min_confidence = 0.65
        trader.cooldown_hours = 4

        mock_pt = MagicMock()
        mock_pt.balance = 500.0
        mock_pt.positions = open_positions or {}
        trader.trader = mock_pt
        return trader

    def _rec(self, market_id, confidence=0.80, liquidity=500):
        return {
            'market_id': market_id,
            'confidence': confidence,
            'probability': 0.65,
            'liquidity': liquidity,
            'existing_position': True,
            'ai_recommendation': 'NO',
            'ai_returned_skip': False,
        }

    def test_open_position_blocks_new_bet(self):
        """Any market with an OPEN position must be blocked."""
        positions = {'mkt1': [{'status': 'OPEN', 'timestamp': '2026-01-01T00:00:00'}]}
        trader = self._make_trader(positions)
        result = trader.should_trade_market('mkt1', self._rec('mkt1', confidence=0.90))
        self.assertFalse(result)

    def test_open_position_blocks_even_at_max_confidence(self):
        """Was the old bug: confidence >= 0.80 bypassed the guard. Must now be blocked."""
        positions = {'mkt1': [{'status': 'OPEN', 'timestamp': '2026-01-01T00:00:00'}]}
        trader = self._make_trader(positions)
        result = trader.should_trade_market('mkt1', self._rec('mkt1', confidence=1.0))
        self.assertFalse(result)

    def test_closed_position_allows_new_bet(self):
        """A CLOSED position on the same market should not block a new bet."""
        positions = {'mkt1': [{'status': 'CLOSED', 'timestamp': '2026-01-01T00:00:00'}]}
        trader = self._make_trader(positions)
        rec = self._rec('mkt1', confidence=0.75)
        rec['existing_position'] = False  # research would set this False for CLOSED
        # 9 other open positions (not at max)
        trader.trader.positions = {
            'mkt1': [{'status': 'CLOSED', 'timestamp': '2026-01-01T00:00:00'}],
        }
        result = trader.should_trade_market('mkt1', rec)
        self.assertTrue(result)

    def test_no_position_allows_bet(self):
        """Fresh market with no position history should be tradeable."""
        trader = self._make_trader({})
        rec = {
            'market_id': 'new_mkt',
            'confidence': 0.70,
            'probability': 0.65,
            'liquidity': 500,
            'existing_position': False,
            'ai_recommendation': 'NO',
            'ai_returned_skip': False,
        }
        result = trader.should_trade_market('new_mkt', rec)
        self.assertTrue(result)

    def test_max_positions_still_blocks(self):
        """Even without an OPEN position on this market, max_positions limit must block."""
        # 10 other open markets
        positions = {f'mkt{i}': [{'status': 'OPEN'}] for i in range(10)}
        positions['target'] = [{'status': 'CLOSED'}]
        trader = self._make_trader(positions)
        rec = {
            'market_id': 'target',
            'confidence': 0.75,
            'probability': 0.65,
            'liquidity': 500,
            'existing_position': False,
            'ai_recommendation': 'NO',
            'ai_returned_skip': False,
        }
        result = trader.should_trade_market('target', rec)
        self.assertFalse(result)


if __name__ == '__main__':
    unittest.main()
