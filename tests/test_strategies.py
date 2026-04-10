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
from unittest.mock import MagicMock, patch

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

    def _m(self, prob, question='Will this prediction market resolve YES by end of 2026?'):
        """Default question maps to 'other' category (no matching keywords → always fires)."""
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

    def test_politics_category_skipped(self):
        """Politics bias dropped to ~3.6pp after reclassification — below 4pp threshold → skip."""
        m = self._m(0.65, question='Will Trump win the 2026 senate race?')
        self.assertIsNone(TradingStrategies.probability_bias_strategy(m))

    def test_crypto_category_fires(self):
        """Crypto markets have meaningful bias (6.1pp) — must fire."""
        m = self._m(0.65, question='Will Bitcoin hit $150k by December 2026?')
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
            'news':                  'fundamental',
            'volume_spike_priority': 'filter',
        }
        self.assertEqual(TradingStrategies.SIGNAL_FAMILIES, expected)

    def test_volume_spike_is_filter_not_directional(self):
        """volume_spike_priority must be in 'filter' family — it cannot be momentum or contrarian."""
        self.assertEqual(TradingStrategies.SIGNAL_FAMILIES['volume_spike_priority'], 'filter')

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
        mock_pt.max_positions = 10
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

    # ── Regression: stale existing_position flag (bug: market 2czul2Rync) ────
    #
    # Root cause: auto_research.py set existing_position at scan time.
    # By the time auto_trader.py ran (up to 20 minutes later), the position
    # was already OPEN in the live state. The old code trusted the research
    # flag and allowed re-entry, causing 3 consecutive bets on the same
    # market (2czul2Rync) and a -$190 loss.
    #
    # Fix: should_trade_market() queries self.trader.positions directly,
    # ignoring the stale research flag entirely.

    def test_stale_false_flag_blocked_by_live_open_position(self):
        """
        Research says existing_position=False (stale), but trader has an OPEN
        position in the live state. Must be blocked — live state wins.
        """
        positions = {'2czul2Rync': [{'status': 'OPEN', 'timestamp': '2026-01-01T00:00:00'}]}
        trader = self._make_trader(positions)
        rec = {
            'market_id': '2czul2Rync',
            'confidence': 0.80,
            'probability': 0.65,
            'liquidity': 500,
            'existing_position': False,   # stale — research ran before position opened
            'ai_recommendation': 'NO',
            'ai_returned_skip': False,
        }
        self.assertFalse(trader.should_trade_market('2czul2Rync', rec))

    def test_third_bet_on_same_market_blocked(self):
        """
        Simulate the exact 2czul2Rync scenario: two prior bets already in
        the position list, both OPEN. A third bet must be blocked regardless
        of confidence or what research says.
        """
        positions = {
            '2czul2Rync': [
                {'status': 'OPEN', 'timestamp': '2026-01-01T10:00:00'},
                {'status': 'OPEN', 'timestamp': '2026-01-01T11:00:00'},
            ]
        }
        trader = self._make_trader(positions)
        for confidence in (0.65, 0.80, 0.95, 1.0):
            with self.subTest(confidence=confidence):
                rec = {
                    'market_id': '2czul2Rync',
                    'confidence': confidence,
                    'probability': 0.65,
                    'liquidity': 500,
                    'existing_position': False,  # stale flag — must be ignored
                    'ai_recommendation': 'NO',
                    'ai_returned_skip': False,
                }
                self.assertFalse(
                    trader.should_trade_market('2czul2Rync', rec),
                    f"Third bet allowed at confidence={confidence} — regression!"
                )

    def test_research_flag_true_also_blocked(self):
        """
        When research correctly reports existing_position=True AND live state
        has an OPEN position, must also be blocked (belt-and-suspenders check).
        """
        positions = {'mkt_dup': [{'status': 'OPEN', 'timestamp': '2026-01-01T00:00:00'}]}
        trader = self._make_trader(positions)
        rec = {
            'market_id': 'mkt_dup',
            'confidence': 0.75,
            'probability': 0.65,
            'liquidity': 500,
            'existing_position': True,
            'ai_recommendation': 'NO',
            'ai_returned_skip': False,
        }
        self.assertFalse(trader.should_trade_market('mkt_dup', rec))


class TestAdaptiveCategoryCapPhaseC(unittest.TestCase):
    """Phase C: _effective_category_cap() and _get_strategy_weight() correctness."""

    def _make_trader(self):
        from automation.auto_trader import AutoTrader
        trader = AutoTrader.__new__(AutoTrader)
        trader.max_positions = 10
        trader.max_position_size = 0.1
        trader.min_confidence = 0.65
        mock_pt = MagicMock()
        mock_pt.balance = 500.0
        mock_pt.positions = {}
        trader.trader = mock_pt
        return trader

    def test_no_data_returns_default_cap(self):
        trader = self._make_trader()
        trader._category_accuracy = {}
        from manifold_bot.config import MAX_POSITIONS_PER_CATEGORY
        self.assertEqual(trader._effective_category_cap("crypto"), MAX_POSITIONS_PER_CATEGORY)

    def test_below_min_samples_returns_default_cap(self):
        from automation.auto_trader import _MIN_SAMPLES_PER_CATEGORY
        from manifold_bot.config import MAX_POSITIONS_PER_CATEGORY
        trader = self._make_trader()
        trader._category_accuracy = {
            "crypto": {"accuracy": 0.30, "sample_count": _MIN_SAMPLES_PER_CATEGORY - 1}
        }
        # Not enough samples — don't penalise yet
        self.assertEqual(trader._effective_category_cap("crypto"), MAX_POSITIONS_PER_CATEGORY)

    def test_poor_accuracy_with_sufficient_samples_reduces_cap(self):
        from automation.auto_trader import _MIN_SAMPLES_PER_CATEGORY
        trader = self._make_trader()
        trader._category_accuracy = {
            "crypto": {"accuracy": 0.40, "sample_count": _MIN_SAMPLES_PER_CATEGORY + 2}
        }
        self.assertEqual(trader._effective_category_cap("crypto"), 1)

    def test_good_accuracy_returns_default_cap(self):
        from automation.auto_trader import _MIN_SAMPLES_PER_CATEGORY
        from manifold_bot.config import MAX_POSITIONS_PER_CATEGORY
        trader = self._make_trader()
        trader._category_accuracy = {
            "politics": {"accuracy": 0.65, "sample_count": _MIN_SAMPLES_PER_CATEGORY + 2}
        }
        self.assertEqual(trader._effective_category_cap("politics"), MAX_POSITIONS_PER_CATEGORY)

    def test_unknown_category_returns_default_cap(self):
        from manifold_bot.config import MAX_POSITIONS_PER_CATEGORY
        trader = self._make_trader()
        trader._category_accuracy = {"crypto": {"accuracy": 0.30, "sample_count": 20}}
        self.assertEqual(trader._effective_category_cap("sports"), MAX_POSITIONS_PER_CATEGORY)

    def test_strategy_weight_uses_max_across_strategies(self):
        trader = self._make_trader()
        trader._strategy_weights = {
            "probability_direction": 0.6,
            "mean_reversion": 1.1,
            "volume_spike": 0.8,
        }
        self.assertAlmostEqual(trader._get_strategy_weight(["probability_direction", "mean_reversion"]), 1.1)

    def test_strategy_weight_missing_attr_falls_back_to_one(self):
        """Test that _get_strategy_weight is safe even if __new__ skipped __init__."""
        from automation.auto_trader import AutoTrader
        trader = AutoTrader.__new__(AutoTrader)
        # _strategy_weights not set at all — getattr guard should kick in
        self.assertEqual(trader._get_strategy_weight(["mean_reversion"]), 1.0)

    def test_strategy_weight_category_specific_overrides_global(self):
        """by_category weight takes precedence over global weight for matching category."""
        trader = self._make_trader()
        trader._strategy_weights = {"probability_direction": 0.8}
        trader._by_category_weights = {
            "sports": {"probability_direction": {"weight": 1.2, "accuracy": 0.65, "n": 15}}
        }
        w = trader._get_strategy_weight(["probability_direction"], category="sports")
        self.assertAlmostEqual(w, 1.2)

    def test_strategy_weight_falls_back_to_global_for_unmatched_category(self):
        """When category has no data for a strategy, use global weight."""
        trader = self._make_trader()
        trader._strategy_weights = {"mean_reversion": 0.9}
        trader._by_category_weights = {
            "sports": {}   # no data for mean_reversion in sports
        }
        w = trader._get_strategy_weight(["mean_reversion"], category="sports")
        self.assertAlmostEqual(w, 0.9)

    def test_strategy_weight_category_not_in_by_category_uses_global(self):
        """When category is entirely absent from by_category, fall back to global."""
        trader = self._make_trader()
        trader._strategy_weights = {"probability_direction": 1.1}
        trader._by_category_weights = {}
        w = trader._get_strategy_weight(["probability_direction"], category="crypto")
        self.assertAlmostEqual(w, 1.1)

    def test_strategy_weight_max_across_cat_and_global(self):
        """Takes max across strategies: one from category, one from global."""
        trader = self._make_trader()
        trader._strategy_weights = {
            "probability_direction": 0.7,
            "mean_reversion": 1.15,
        }
        trader._by_category_weights = {
            "crypto": {"probability_direction": {"weight": 0.6, "accuracy": 0.48, "n": 12}}
        }
        # probability_direction: 0.6 (category wins over global 0.7)
        # mean_reversion: 1.15 (global fallback, not in category)
        # max = 1.15
        w = trader._get_strategy_weight(["probability_direction", "mean_reversion"], category="crypto")
        self.assertAlmostEqual(w, 1.15)

    def test_category_cap_missing_attr_falls_back_to_default(self):
        """Test that _effective_category_cap is safe even if __new__ skipped __init__."""
        from automation.auto_trader import AutoTrader
        from manifold_bot.config import MAX_POSITIONS_PER_CATEGORY
        trader = AutoTrader.__new__(AutoTrader)
        # _category_accuracy not set at all — getattr guard should kick in
        self.assertEqual(trader._effective_category_cap("crypto"), MAX_POSITIONS_PER_CATEGORY)

    def test_other_category_uncapped(self):
        """'other' returns self.max_positions (AutoTrader attr), not per-category cap."""
        from manifold_bot.config import MAX_POSITIONS_PER_CATEGORY
        trader = self._make_trader()   # _make_trader sets trader.max_positions = 10
        trader._category_accuracy = {}
        cap = trader._effective_category_cap("other")
        self.assertEqual(cap, 10)
        self.assertGreater(cap, MAX_POSITIONS_PER_CATEGORY,
                           "other cap must exceed per-category cap (10 > 3)")

    def test_other_category_uncapped_ignores_accuracy(self):
        """'other' must be uncapped even if accuracy data says it's a poor category."""
        from automation.auto_trader import _MIN_SAMPLES_PER_CATEGORY
        trader = self._make_trader()
        trader._category_accuracy = {
            "other": {"accuracy": 0.20, "sample_count": _MIN_SAMPLES_PER_CATEGORY + 5}
        }
        # Despite bad accuracy, 'other' is catch-all — never penalised
        self.assertEqual(trader._effective_category_cap("other"), 10)


class TestSnapshotLogger(unittest.TestCase):
    """M1: _write_market_snapshots() writes rows and is best-effort."""

    def test_writes_non_resolved_markets_to_db(self):
        import tempfile, sqlite3
        from automation.auto_research import _write_market_snapshots
        markets = [
            {"id": "mkt1", "question": "Will X?", "probability": 0.6,
             "volume24Hours": 100.0, "uniqueBettorCount": 10,
             "totalLiquidity": 500.0, "isResolved": False},
            {"id": "mkt2", "question": "Will Y?", "probability": 0.3,
             "volume24Hours": 50.0, "uniqueBettorCount": 5,
             "totalLiquidity": 200.0, "isResolved": True},  # resolved — should be excluded
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "market_snapshots.db")
            with patch("automation.auto_research._SNAPSHOTS_DB_PATH", db_path):
                _write_market_snapshots(markets)
            conn = sqlite3.connect(db_path)
            rows = conn.execute("SELECT market_id FROM market_snapshots").fetchall()
            conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "mkt1")

    def test_snapshot_failure_does_not_propagate(self):
        """A broken DB path must not raise — the call site wraps in try/except."""
        from automation.auto_research import _write_market_snapshots
        markets = [{"id": "m", "question": "Q?", "probability": 0.5,
                    "volume24Hours": 0, "uniqueBettorCount": 0,
                    "totalLiquidity": 100, "isResolved": False}]
        with patch("automation.auto_research._SNAPSHOTS_DB_PATH", "/nonexistent/path/db.sqlite"):
            # Should raise — but the call site in analyze_markets() catches it.
            # Here we just verify the exception type is not silently swallowed inside
            # _write_market_snapshots itself (it shouldn't be — caller handles it).
            with self.assertRaises(Exception):
                _write_market_snapshots(markets)


# ─────────────────────────────────────────────────────────────────────────────
# MIN_LIQUIDITY alignment
# ─────────────────────────────────────────────────────────────────────────────

class TestLiquidityThresholdAlignment(unittest.TestCase):
    """
    auto_research.py, auto_trader.py, and position_swap_checker.py must all
    use the same liquidity floor so no AI slot is spent on a market the trader
    will subsequently reject.
    """

    def test_min_liquidity_in_config(self):
        from manifold_bot.config import MIN_LIQUIDITY
        self.assertEqual(MIN_LIQUIDITY, 200)

    def test_research_imports_min_liquidity(self):
        """auto_research.py must import MIN_LIQUIDITY (not use a hardcoded value)."""
        import automation.auto_research as ar
        # If the import was wrong this line would raise ImportError / AttributeError
        self.assertIs(ar.MIN_LIQUIDITY, __import__('manifold_bot.config', fromlist=['MIN_LIQUIDITY']).MIN_LIQUIDITY)

    def test_trader_imports_min_liquidity(self):
        from automation.auto_trader import AutoTrader
        from manifold_bot.config import MIN_LIQUIDITY
        # Build a rec just below the threshold; should_trade_market must reject it
        trader = AutoTrader.__new__(AutoTrader)
        mock_pt = MagicMock()
        mock_pt.balance = 500.0
        mock_pt.positions = {}
        trader.trader = mock_pt
        trader.max_positions = 10
        trader.max_position_size = 0.1
        trader.min_confidence = 0.65
        rec = {
            'market_id': 'low_liq',
            'confidence': 0.80,
            'probability': 0.65,
            'liquidity': MIN_LIQUIDITY - 1,
            'existing_position': False,
            'ai_recommendation': 'YES',
            'ai_returned_skip': False,
        }
        self.assertFalse(trader.should_trade_market('low_liq', rec))

    def test_trader_allows_at_min_liquidity(self):
        from automation.auto_trader import AutoTrader
        from manifold_bot.config import MIN_LIQUIDITY
        trader = AutoTrader.__new__(AutoTrader)
        mock_pt = MagicMock()
        mock_pt.balance = 500.0
        mock_pt.positions = {}
        mock_pt.max_positions = 10
        trader.trader = mock_pt
        trader.max_positions = 10
        trader.max_position_size = 0.1
        trader.min_confidence = 0.65
        rec = {
            'market_id': 'ok_liq',
            'confidence': 0.80,
            'probability': 0.65,
            'liquidity': MIN_LIQUIDITY,
            'existing_position': False,
            'ai_recommendation': 'YES',
            'ai_returned_skip': False,
        }
        self.assertTrue(trader.should_trade_market('ok_liq', rec))


# ─────────────────────────────────────────────────────────────────────────────
# AI timeout cooldown
# ─────────────────────────────────────────────────────────────────────────────

class TestAiTimeoutCooldown(unittest.TestCase):
    """
    Unit tests for the AI timeout cooldown helpers in auto_research.py.
    Verifies that markets with too many recent timeouts are excluded from
    the AI candidate pool.
    """

    def setUp(self):
        from automation.auto_research import (
            _is_in_ai_cooldown,
            _record_ai_timeout,
            _AI_TIMEOUT_MAX_FAILS,
            _AI_TIMEOUT_COOLDOWN_HOURS,
        )
        self._is_in_cooldown = _is_in_ai_cooldown
        self._record = _record_ai_timeout
        self._max_fails = _AI_TIMEOUT_MAX_FAILS
        self._cooldown_hours = _AI_TIMEOUT_COOLDOWN_HOURS

    def _recent_ts(self):
        """ISO timestamp just now (well within cooldown window)."""
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()

    def _expired_ts(self):
        """ISO timestamp older than the cooldown window."""
        from datetime import datetime, timezone, timedelta
        return (datetime.now(timezone.utc) - timedelta(hours=self._cooldown_hours + 1)).isoformat()

    def test_no_entry_returns_false(self):
        self.assertFalse(self._is_in_cooldown("mkt_new", {}))

    def test_below_max_fails_returns_false(self):
        cooldown = {"mkt_a": {"fails": self._max_fails - 1, "last_fail_at": self._recent_ts()}}
        self.assertFalse(self._is_in_cooldown("mkt_a", cooldown))

    def test_at_max_fails_returns_true(self):
        cooldown = {"mkt_b": {"fails": self._max_fails, "last_fail_at": self._recent_ts()}}
        self.assertTrue(self._is_in_cooldown("mkt_b", cooldown))

    def test_above_max_fails_returns_true(self):
        cooldown = {"mkt_c": {"fails": self._max_fails + 3, "last_fail_at": self._recent_ts()}}
        self.assertTrue(self._is_in_cooldown("mkt_c", cooldown))

    def test_record_increments_fail_count(self):
        cooldown = {}
        self._record(["mkt_x"], cooldown)
        self.assertEqual(cooldown["mkt_x"]["fails"], 1)
        self._record(["mkt_x"], cooldown)
        self.assertEqual(cooldown["mkt_x"]["fails"], 2)

    def test_record_sets_timestamp(self):
        from datetime import datetime, timezone
        cooldown = {}
        self._record(["mkt_ts"], cooldown)
        ts = cooldown["mkt_ts"]["last_fail_at"]
        parsed = datetime.fromisoformat(ts)
        age_secs = abs((datetime.now(timezone.utc) - parsed).total_seconds())
        self.assertLess(age_secs, 5, "Timestamp should be within 5 seconds of now")

    def test_record_multiple_markets(self):
        cooldown = {}
        self._record(["m1", "m2", "m3"], cooldown)
        self.assertIn("m1", cooldown)
        self.assertIn("m2", cooldown)
        self.assertIn("m3", cooldown)

    def test_load_cooldown_prunes_expired_entries(self):
        """_load_ai_timeout_cooldown must drop entries older than the cooldown window."""
        import json
        import tempfile
        from automation.auto_research import _load_ai_timeout_cooldown, _AI_TIMEOUT_COOLDOWN_PATH
        stale_data = {
            "stale_mkt": {"fails": 5, "last_fail_at": self._expired_ts()},
            "fresh_mkt": {"fails": 2, "last_fail_at": self._recent_ts()},
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(stale_data, f)
            tmp_path = f.name
        try:
            with patch("automation.auto_research._AI_TIMEOUT_COOLDOWN_PATH", tmp_path):
                result = _load_ai_timeout_cooldown()
            self.assertNotIn("stale_mkt", result, "Expired entry should be pruned")
            self.assertIn("fresh_mkt", result, "Recent entry should survive")
        finally:
            os.unlink(tmp_path)

    def test_load_cooldown_returns_empty_on_missing_file(self):
        from automation.auto_research import _load_ai_timeout_cooldown
        with patch("automation.auto_research._AI_TIMEOUT_COOLDOWN_PATH", "/nonexistent/path.json"):
            result = _load_ai_timeout_cooldown()
        self.assertEqual(result, {})


# ─────────────────────────────────────────────────────────────────────────────
# Swap checker liquidity gate
# ─────────────────────────────────────────────────────────────────────────────

class TestSwapCheckerLiquidityGate(unittest.TestCase):
    """
    find_best_opportunity() must reject swap targets with liquidity < MIN_LIQUIDITY
    so that a proposed swap is never rejected by the trader at execution time.
    """

    def _research(self, recs):
        return {"recommendations": recs}

    def _rec(self, market_id, liquidity, ev=5.0, confidence=0.80):
        return {
            "market_id": market_id,
            "confidence": confidence,
            "liquidity": liquidity,
            "recommendation": "YES",
            "ai_recommendation": "YES",
            "ai_returned_skip": False,
            "estimated_ev": ev,
        }

    def test_low_liquidity_rec_rejected(self):
        from scripts.position_swap_checker import find_best_opportunity
        from manifold_bot.config import MIN_LIQUIDITY
        recs = [self._rec("low_liq", liquidity=MIN_LIQUIDITY - 1)]
        result = find_best_opportunity(self._research(recs), existing_ids=set())
        self.assertIsNone(result)

    def test_exact_min_liquidity_accepted(self):
        from scripts.position_swap_checker import find_best_opportunity
        from manifold_bot.config import MIN_LIQUIDITY
        recs = [self._rec("ok_liq", liquidity=MIN_LIQUIDITY)]
        result = find_best_opportunity(self._research(recs), existing_ids=set())
        self.assertIsNotNone(result)
        self.assertEqual(result["market_id"], "ok_liq")

    def test_above_min_liquidity_accepted(self):
        from scripts.position_swap_checker import find_best_opportunity
        from manifold_bot.config import MIN_LIQUIDITY
        recs = [self._rec("rich_liq", liquidity=MIN_LIQUIDITY + 500)]
        result = find_best_opportunity(self._research(recs), existing_ids=set())
        self.assertIsNotNone(result)

    def test_best_ev_chosen_among_valid_liquidity(self):
        """When multiple recs pass the gate, highest EV wins."""
        from scripts.position_swap_checker import find_best_opportunity
        from manifold_bot.config import MIN_LIQUIDITY
        recs = [
            self._rec("low_liq",  liquidity=MIN_LIQUIDITY - 1, ev=100.0),  # rejected
            self._rec("med_liq",  liquidity=MIN_LIQUIDITY,      ev=3.0),
            self._rec("high_liq", liquidity=MIN_LIQUIDITY + 100, ev=8.0),
        ]
        result = find_best_opportunity(self._research(recs), existing_ids=set())
        self.assertEqual(result["market_id"], "high_liq")


if __name__ == '__main__':
    unittest.main()
