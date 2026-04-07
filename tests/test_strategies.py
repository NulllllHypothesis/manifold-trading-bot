#!/usr/bin/env python3
"""
Unit tests for manifold_bot/strategies.py

Covers:
  - probability_bias_strategy: bucket ranges, category filtering
  - probability_direction_strategy: volume guard, staleness guard
  - mean_reversion_strategy: close-time guard, bettor threshold
  - volume_spike_priority: returns float priority, not YES/NO
  - SIGNAL_FAMILIES: family classification exists
  - auto_trader.should_trade_market: OPEN-position guard
"""

import sys
import os
import time
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from manifold_bot.strategies import TradingStrategies


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _market(prob, question='Will something happen?', volume24=10.0,
            last_bet_hours_ago=1.0, bettors=5, pool=None, close_days=30):
    """Build a minimal market dict for testing."""
    now_ms = time.time() * 1000
    return {
        'id': 'test_mkt',
        'probability': prob,
        'question': question,
        'volume24Hours': volume24,
        'lastBetTime': now_ms - last_bet_hours_ago * 3_600_000,
        'uniqueBettorCount': bettors,
        'pool': pool or {'YES': 50, 'NO': 50},
        'closeTime': now_ms + close_days * 86_400_000,
        'totalLiquidity': 500,
    }


# ─────────────────────────────────────────────────────────────────────────────
# probability_bias_strategy
# ─────────────────────────────────────────────────────────────────────────────

class TestProbabilityBiasStrategy(unittest.TestCase):
    """
    Tests for calibration-backed probability_bias_strategy.
    Uses 'politics' question text (category bias=6.2pp, above 4pp threshold).
    """

    def _m(self, prob, question='Will Trump win the election?'):
        return _market(prob, question=question)

    # ── 60-70% band → NO ─────────────────────────────────────────────────────

    def test_60pct_returns_no(self):
        self.assertEqual(TradingStrategies.probability_bias_strategy(self._m(0.60)), "NO")

    def test_65pct_returns_no(self):
        self.assertEqual(TradingStrategies.probability_bias_strategy(self._m(0.65)), "NO")

    def test_699pct_returns_no(self):
        self.assertEqual(TradingStrategies.probability_bias_strategy(self._m(0.699)), "NO")

    def test_70pct_boundary_excluded(self):
        self.assertIsNone(TradingStrategies.probability_bias_strategy(self._m(0.70)))

    # ── 30-40% band → NO ─────────────────────────────────────────────────────

    def test_30pct_returns_no(self):
        self.assertEqual(TradingStrategies.probability_bias_strategy(self._m(0.30)), "NO")

    def test_35pct_returns_no(self):
        self.assertEqual(TradingStrategies.probability_bias_strategy(self._m(0.35)), "NO")

    def test_399pct_returns_no(self):
        self.assertEqual(TradingStrategies.probability_bias_strategy(self._m(0.399)), "NO")

    def test_40pct_boundary_excluded(self):
        self.assertIsNone(TradingStrategies.probability_bias_strategy(self._m(0.40)))

    # ── Well-calibrated zones → None ─────────────────────────────────────────

    def test_50pct_no_signal(self):
        self.assertIsNone(TradingStrategies.probability_bias_strategy(self._m(0.50)))

    def test_80pct_no_signal(self):
        self.assertIsNone(TradingStrategies.probability_bias_strategy(self._m(0.80)))

    def test_20pct_no_signal(self):
        self.assertIsNone(TradingStrategies.probability_bias_strategy(self._m(0.20)))

    def test_55pct_no_signal(self):
        self.assertIsNone(TradingStrategies.probability_bias_strategy(self._m(0.55)))

    def test_missing_probability_defaults_no_signal(self):
        self.assertIsNone(TradingStrategies.probability_bias_strategy({'question': ''}))

    # ── Category filtering ────────────────────────────────────────────────────

    def test_ai_tech_category_skipped(self):
        """AI/tech markets are well-calibrated (bias ~0.19pp) — must be skipped."""
        m = self._m(0.65, question='Will GPT-5 be released by end of 2026?')
        self.assertIsNone(TradingStrategies.probability_bias_strategy(m))

    def test_economics_category_skipped(self):
        """Economics markets are well-calibrated (bias ~1.51pp) — must be skipped."""
        m = self._m(0.65, question='Will the Federal Reserve raise interest rates in June?')
        self.assertIsNone(TradingStrategies.probability_bias_strategy(m))

    def test_politics_category_fires(self):
        """Politics markets have meaningful bias (6.2pp) — must fire."""
        m = self._m(0.65, question='Will Trump win the 2026 senate race?')
        self.assertEqual(TradingStrategies.probability_bias_strategy(m), "NO")

    def test_crypto_category_fires(self):
        """Crypto markets have meaningful bias (5.3pp) — must fire."""
        m = self._m(0.65, question='Will Bitcoin hit $150k by December 2026?')
        self.assertEqual(TradingStrategies.probability_bias_strategy(m), "NO")


# ─────────────────────────────────────────────────────────────────────────────
# probability_direction_strategy
# ─────────────────────────────────────────────────────────────────────────────

class TestProbabilityDirectionStrategy(unittest.TestCase):

    def test_high_prob_active_market_returns_yes(self):
        m = _market(0.75, volume24=20.0, last_bet_hours_ago=1.0)
        self.assertEqual(TradingStrategies.probability_direction_strategy(m), "YES")

    def test_low_prob_active_market_returns_no(self):
        m = _market(0.25, volume24=20.0, last_bet_hours_ago=1.0)
        self.assertEqual(TradingStrategies.probability_direction_strategy(m), "NO")

    def test_coinflip_zone_returns_none(self):
        for prob in (0.45, 0.50, 0.55):
            with self.subTest(prob=prob):
                self.assertIsNone(TradingStrategies.probability_direction_strategy(_market(prob)))

    def test_stale_market_returns_none(self):
        """Last bet > 48h ago — signal is too old."""
        m = _market(0.75, volume24=20.0, last_bet_hours_ago=50.0)
        self.assertIsNone(TradingStrategies.probability_direction_strategy(m))

    def test_low_volume_returns_none(self):
        """24h volume < 5 — insufficient recent activity."""
        m = _market(0.75, volume24=2.0, last_bet_hours_ago=1.0)
        self.assertIsNone(TradingStrategies.probability_direction_strategy(m))

    def test_zero_volume_returns_none(self):
        m = _market(0.75, volume24=0.0, last_bet_hours_ago=1.0)
        self.assertIsNone(TradingStrategies.probability_direction_strategy(m))

    def test_exactly_5_volume_fires(self):
        """Volume exactly at threshold should fire."""
        m = _market(0.75, volume24=5.0, last_bet_hours_ago=1.0)
        self.assertEqual(TradingStrategies.probability_direction_strategy(m), "YES")

    def test_confidence_meets_stat_floor(self):
        """probability_direction confidence (0.65) must meet _STAT_BOOST_FLOOR."""
        import automation.auto_research as ar
        self.assertGreaterEqual(0.65, ar._STAT_BOOST_FLOOR)


# ─────────────────────────────────────────────────────────────────────────────
# mean_reversion_strategy
# ─────────────────────────────────────────────────────────────────────────────

class TestMeanReversionStrategy(unittest.TestCase):

    def test_high_extreme_thin_market_returns_no(self):
        m = _market(0.90, bettors=10, close_days=30)
        self.assertEqual(TradingStrategies.mean_reversion_strategy(m), "NO")

    def test_low_extreme_thin_market_returns_yes(self):
        m = _market(0.10, bettors=10, close_days=30)
        self.assertEqual(TradingStrategies.mean_reversion_strategy(m), "YES")

    def test_deep_market_returns_none(self):
        """More than 100 bettors — don't fight genuine consensus."""
        m = _market(0.90, bettors=150, close_days=30)
        self.assertIsNone(TradingStrategies.mean_reversion_strategy(m))

    def test_near_close_returns_none(self):
        """Market closing in < 7 days — convergence is correct, not mispricing."""
        m = _market(0.90, bettors=10, close_days=3)
        self.assertIsNone(TradingStrategies.mean_reversion_strategy(m))

    def test_within_normal_range_returns_none(self):
        """0.20–0.85 is not extreme enough."""
        for prob in (0.20, 0.50, 0.80, 0.85):
            with self.subTest(prob=prob):
                self.assertIsNone(TradingStrategies.mean_reversion_strategy(_market(prob, bettors=5)))

    def test_exactly_at_threshold_high_fires(self):
        """Just above 0.85 should fire."""
        m = _market(0.86, bettors=5, close_days=30)
        self.assertEqual(TradingStrategies.mean_reversion_strategy(m), "NO")

    def test_exactly_at_threshold_low_fires(self):
        m = _market(0.14, bettors=5, close_days=30)
        self.assertEqual(TradingStrategies.mean_reversion_strategy(m), "YES")


# ─────────────────────────────────────────────────────────────────────────────
# volume_spike_priority
# ─────────────────────────────────────────────────────────────────────────────

class TestVolumeSpikePriority(unittest.TestCase):

    def test_returns_float_not_direction(self):
        """Must return a float, not YES/NO — it is a priority boost, not a trade signal."""
        result = TradingStrategies.volume_spike_priority(_market(0.70, volume24=100.0), avg_volume=10.0)
        self.assertIsInstance(result, float)
        self.assertNotIn(result, ('YES', 'NO'))

    def test_no_spike_returns_zero(self):
        result = TradingStrategies.volume_spike_priority(_market(0.70, volume24=5.0), avg_volume=10.0)
        self.assertEqual(result, 0.0)

    def test_mild_spike_returns_positive(self):
        """3× median is a mild spike — should return non-zero priority."""
        result = TradingStrategies.volume_spike_priority(_market(0.70, volume24=30.0), avg_volume=10.0)
        self.assertGreater(result, 0.0)

    def test_large_spike_capped_at_one(self):
        """Very large spike should cap at 1.0."""
        result = TradingStrategies.volume_spike_priority(_market(0.70, volume24=10000.0), avg_volume=10.0)
        self.assertEqual(result, 1.0)

    def test_zero_avg_volume_returns_zero(self):
        """No average to compare against — no signal."""
        result = TradingStrategies.volume_spike_priority(_market(0.70, volume24=100.0), avg_volume=0.0)
        self.assertEqual(result, 0.0)


# ─────────────────────────────────────────────────────────────────────────────
# Signal family classification
# ─────────────────────────────────────────────────────────────────────────────

class TestSignalFamilies(unittest.TestCase):

    def test_all_strategies_have_family(self):
        expected = {
            'probability_direction': 'momentum',
            'mean_reversion':        'contrarian',
            'thin_market':           'contrarian',
            'probability_bias':      'contrarian',
            'creator_disagreement':  'fundamental',
            'volume_spike':          'filter',
        }
        self.assertEqual(TradingStrategies.SIGNAL_FAMILIES, expected)

    def test_volume_spike_is_filter_not_directional(self):
        """volume_spike must be in 'filter' family — it cannot be momentum or contrarian."""
        self.assertEqual(TradingStrategies.SIGNAL_FAMILIES['volume_spike'], 'filter')

    def test_probability_bias_is_contrarian(self):
        self.assertEqual(TradingStrategies.SIGNAL_FAMILIES['probability_bias'], 'contrarian')


# ─────────────────────────────────────────────────────────────────────────────
# auto_trader.should_trade_market — OPEN-position guard
# ─────────────────────────────────────────────────────────────────────────────

class TestOpenPositionGuard(unittest.TestCase):
    """
    Verify that auto_trader blocks new bets on markets with an existing OPEN position.
    Previously the bot allowed stacking bets at confidence >= 0.80, leading to
    3 consecutive losses on the same market (2czul2Rync).
    """

    def _make_trader(self, open_positions=None):
        from automation.auto_trader import AutoTrader
        trader = AutoTrader.__new__(AutoTrader)
        trader.research_file = "market_research.json"
        trader.trade_log_file = "auto_trades.json"
        trader.max_positions = 10
        trader.max_position_size = 0.1
        trader.min_confidence = 0.65

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
        positions = {'mkt1': [{'status': 'OPEN', 'timestamp': '2026-01-01T00:00:00'}]}
        trader = self._make_trader(positions)
        self.assertFalse(trader.should_trade_market('mkt1', self._rec('mkt1', confidence=0.90)))

    def test_open_position_blocks_even_at_max_confidence(self):
        """Was the old bug: confidence >= 0.80 bypassed the guard."""
        positions = {'mkt1': [{'status': 'OPEN', 'timestamp': '2026-01-01T00:00:00'}]}
        trader = self._make_trader(positions)
        self.assertFalse(trader.should_trade_market('mkt1', self._rec('mkt1', confidence=1.0)))

    def test_closed_position_allows_new_bet(self):
        trader = self._make_trader({'mkt1': [{'status': 'CLOSED', 'timestamp': '2026-01-01T00:00:00'}]})
        rec = self._rec('mkt1', confidence=0.75)
        rec['existing_position'] = False
        self.assertTrue(trader.should_trade_market('mkt1', rec))

    def test_no_position_allows_bet(self):
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
        self.assertTrue(trader.should_trade_market('new_mkt', rec))

    def test_max_positions_still_blocks(self):
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
        self.assertFalse(trader.should_trade_market('target', rec))


if __name__ == '__main__':
    unittest.main()
