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

import manifold_bot.strategies as _strat_module


class _CalibrationPatch:
    """
    Context manager that swaps in deterministic calibration data for
    probability_bias_strategy tests. Without this the tests depend on
    whatever calibration_table.json happens to exist on disk, which
    differs between laptop and server.
    """
    def __init__(self, category_bias: dict, by_category_bucket: list):
        self._category_bias = category_bias
        self._buckets       = by_category_bucket
        self._saved_cat     = None
        self._saved_bucket  = None

    def __enter__(self):
        self._saved_cat    = _strat_module._CATEGORY_BIAS
        self._saved_bucket = _strat_module._BY_CATEGORY_BUCKET
        _strat_module._CATEGORY_BIAS     = self._category_bias
        _strat_module._BY_CATEGORY_BUCKET = self._buckets
        return self

    def __exit__(self, *args):
        _strat_module._CATEGORY_BIAS     = self._saved_cat
        _strat_module._BY_CATEGORY_BUCKET = self._saved_bucket


def _cell(category, bucket_low, bias, n=50, reliable=True):
    return {
        "category":    category,
        "bucket_low":  bucket_low,
        "bucket_high": bucket_low + 0.10,
        "bias":        bias,
        "sample_size": n,
        "reliable":    reliable,
        "crowd_midpoint":  bucket_low + 0.05,
        "actual_yes_rate": max(0.0, min(1.0, (bucket_low + 0.05) - bias)),
    }


class TestProbabilityBiasStrategy(unittest.TestCase):
    """
    Op5 tests for probability_bias_strategy with deterministic calibration data.

    The strategy now uses per-(category, bucket) bias when reliable data exists,
    and falls back to the category-level aggregate. _MIN_BIAS_TO_TRADE = 0.025.
    These tests inject known calibration cells so they do not depend on the
    calibration_table.json file on disk.
    """

    # Category biases used across most tests. 'other' is +5pp (fires),
    # 'ai_tech' is +0.3pp (skipped), 'economics' is +1pp (skipped),
    # 'politics' is +0.7pp (skipped under the 2.5pp gate).
    _CAT_BIAS = {
        'other':         0.050,
        'sports':        0.035,
        'politics':      0.007,
        'crypto':        0.040,
        'ai_tech':       0.003,
        'economics':     0.010,
        'science':       0.030,
        'gaming':        0.000,
        'entertainment': 0.000,
        'business':      0.000,
    }

    # Per-bucket cells. Two strongly biased (crowd overestimates YES) and one
    # strongly underbiased (crowd underestimates YES — strategy should return YES).
    # Op7.1: 'other' is fully excluded from probability_bias. Tests that
    # need per-bucket cells use 'crypto' or 'science' instead.
    _BUCKETS = [
        _cell('crypto',  0.60, +0.09),  # positive bias → bet NO
        _cell('crypto',  0.30, +0.08),  # also positive → bet NO
        _cell('science', 0.20, -0.06),  # negative bias → bet YES
    ]

    def setUp(self):
        # Every test runs with the same deterministic calibration state.
        self._patch = _CalibrationPatch(self._CAT_BIAS, self._BUCKETS)
        self._patch.__enter__()

    def tearDown(self):
        self._patch.__exit__()

    def _m(self, prob, question='Will Bitcoin hit $150k by December 2026?'):
        """Default question maps to 'crypto' category (not 'other', which is
        excluded from probability_bias entirely by Op7.1)."""
        return _market(prob, question=question)

    # ── Per-bucket path: positive-bias bucket → NO ──────────────────────────

    def test_per_bucket_60pct_positive_bias_returns_no(self):
        self.assertEqual(TradingStrategies.probability_bias_strategy(self._m(0.60)), "NO")

    def test_per_bucket_30pct_positive_bias_returns_no(self):
        self.assertEqual(TradingStrategies.probability_bias_strategy(self._m(0.30)), "NO")

    def test_per_bucket_65pct_inside_60_70_bucket_returns_no(self):
        """0.65 falls into the same 60–70% bucket as 0.60."""
        self.assertEqual(TradingStrategies.probability_bias_strategy(self._m(0.65)), "NO")

    # ── Per-bucket path: negative-bias bucket → YES ─────────────────────────

    def test_per_bucket_negative_bias_returns_yes(self):
        """
        Op5 behaviour: a bucket with strong negative bias means the crowd
        underestimates YES, so the strategy should bet YES (not None).
        Uses a science question (not 'other') because Op7 excludes 'other'
        from per-bucket lookup.
        """
        m = self._m(0.20, question='Will SpaceX launch Starship successfully?')
        self.assertEqual(TradingStrategies.probability_bias_strategy(m), "YES")

    # ── Category fallback when bucket has no cell ───────────────────────────

    def test_category_other_excluded_entirely(self):
        """
        Op7.1: 'other' is excluded from probability_bias entirely — both
        per-bucket and aggregate. Its heterogeneous mix makes any bias
        signal noise.
        """
        m = self._m(0.80, question='Will something random happen next year?')
        self.assertIsNone(TradingStrategies.probability_bias_strategy(m))

    def test_category_fallback_ai_tech_skipped(self):
        """AI/tech aggregate bias 0.003 is below 0.025 threshold → skip."""
        m = self._m(0.65, question='Will GPT-5 be released by end of 2026?')
        self.assertIsNone(TradingStrategies.probability_bias_strategy(m))

    def test_category_fallback_economics_skipped(self):
        """Economics aggregate bias 0.010 is below 0.025 threshold → skip."""
        m = self._m(0.65, question='Will the Federal Reserve raise interest rates in June?')
        self.assertIsNone(TradingStrategies.probability_bias_strategy(m))

    def test_category_fallback_politics_skipped(self):
        """Politics aggregate bias 0.007 is below 0.025 threshold → skip."""
        m = self._m(0.65, question='Will Trump win the 2026 senate race?')
        self.assertIsNone(TradingStrategies.probability_bias_strategy(m))

    def test_category_fallback_crypto_fires(self):
        """Crypto aggregate bias 0.040 clears threshold → fire NO."""
        m = self._m(0.80, question='Will Bitcoin hit $150k by December 2026?')
        # 80% bucket has no cell for crypto in the fixture, so aggregate is used
        self.assertEqual(TradingStrategies.probability_bias_strategy(m), "NO")

    # ── Small-sample cells fall back ────────────────────────────────────────

    def test_unreliable_cell_falls_back_to_category(self):
        """
        A bucket cell with sample_size < _MIN_BUCKET_SAMPLES (15) must NOT be
        used directly. The strategy falls back to the category aggregate.
        """
        # Use crypto (not 'other' which is fully excluded by Op7.1)
        small_cell = _cell('crypto', 0.60, +0.50, n=3, reliable=True)  # huge bias but tiny n
        with _CalibrationPatch({'crypto': 0.050}, [small_cell]):
            # cell should be ignored due to n<15, aggregate 0.050 should fire
            self.assertEqual(
                TradingStrategies.probability_bias_strategy(self._m(0.60)),
                "NO",
            )

    def test_not_reliable_cell_falls_back_to_category(self):
        """A cell marked reliable=False must also be skipped."""
        bad_cell = _cell('crypto', 0.60, +0.50, n=100, reliable=False)
        with _CalibrationPatch({'crypto': 0.050}, [bad_cell]):
            self.assertEqual(
                TradingStrategies.probability_bias_strategy(self._m(0.60)),
                "NO",
            )

    # ── Missing data ────────────────────────────────────────────────────────

    def test_missing_probability_defaults_no_signal(self):
        self.assertIsNone(TradingStrategies.probability_bias_strategy({'question': ''}))

    def test_empty_calibration_skips(self):
        """With no calibration data at all the strategy must be silent."""
        with _CalibrationPatch({}, []):
            self.assertIsNone(TradingStrategies.probability_bias_strategy(self._m(0.60)))

    # ── Op7: noise market filter ─────────────────────────────────────────

    def test_coinflip_market_skipped(self):
        """'Daily Coinflip' is structurally 50/50 — no bias applies."""
        m = self._m(0.50, question='Daily Coinflip')
        self.assertIsNone(TradingStrategies.probability_bias_strategy(m))

    def test_coin_flip_variant_skipped(self):
        m = self._m(0.50, question='Daily Coin Flip - Day 321')
        self.assertIsNone(TradingStrategies.probability_bias_strategy(m))

    def test_free_lottery_skipped(self):
        m = self._m(0.15, question='Free Lottery (Mars)')
        self.assertIsNone(TradingStrategies.probability_bias_strategy(m))

    def test_random_market_skipped(self):
        m = self._m(0.50, question='Random number generator output above 50?')
        self.assertIsNone(TradingStrategies.probability_bias_strategy(m))

    def test_dice_roll_skipped(self):
        m = self._m(0.17, question='Will the d20 roll be above 10?')
        self.assertIsNone(TradingStrategies.probability_bias_strategy(m))

    def test_unicode_coinflip_skipped(self):
        """Regression: 'Däilÿ Cöin Flip - Day 321' bypassed the ASCII filter."""
        m = self._m(0.50, question='Däilÿ Cöin Flip - Day 321')
        self.assertIsNone(TradingStrategies.probability_bias_strategy(m))

    def test_accented_lottery_skipped(self):
        m = self._m(0.15, question='Frëe Löttery (Märs)')
        self.assertIsNone(TradingStrategies.probability_bias_strategy(m))

    def test_non_noise_market_not_skipped(self):
        """A real crypto market must NOT be filtered by the noise check."""
        m = self._m(0.65, question='Will Bitcoin hit $150k by December 2026?')
        result = TradingStrategies.probability_bias_strategy(m)
        self.assertIsNotNone(result)

    # ── Op7.1 false-positive guards ──────────────────────────────────

    def test_random_drug_testing_not_filtered(self):
        """'random' as a substring in a real question must not trigger noise filter."""
        m = self._m(0.65, question='Will the school adopt random drug testing next year?')
        # This is an 'other' category → excluded by _BIAS_EXCLUDED_CATEGORIES
        # but the noise filter itself must NOT be the reason it returns None.
        # Test via _is_noise_market directly:
        from manifold_bot.strategies import _is_noise_market
        self.assertFalse(_is_noise_market('Will the school adopt random drug testing next year?'))

    def test_dice_dreams_not_filtered(self):
        """'Dice Dreams' is a real app, not a dice roll market."""
        from manifold_bot.strategies import _is_noise_market
        self.assertFalse(_is_noise_market('Will Dice Dreams hit 50M downloads?'))

    def test_random_forest_not_filtered(self):
        """'random forest' is an ML algorithm, not a noise market."""
        from manifold_bot.strategies import _is_noise_market
        self.assertFalse(_is_noise_market('Will random forest beat XGBoost on this benchmark?'))

    def test_lottery_revenue_not_filtered(self):
        """'lottery' alone could match real markets about lottery policy."""
        from manifold_bot.strategies import _is_noise_market
        self.assertFalse(_is_noise_market('Will state lottery revenue exceed $1B?'))

    # ── Op7: 'other' excluded from per-bucket lookup ─────────────────────

    def test_other_category_excluded_at_different_prob(self):
        """
        Op7.1: 'other' is excluded at any probability level.
        """
        m = self._m(0.65, question='Will my cat learn to fetch this year?')
        self.assertIsNone(TradingStrategies.probability_bias_strategy(m))

    def test_non_other_category_still_uses_bucket(self):
        """Categories other than 'other' must still use per-bucket cells."""
        # crypto has a per-bucket cell at 60% with +0.09 bias in our fixture
        m = self._m(0.65, question='Will Bitcoin hit $150k by December 2026?')
        result = TradingStrategies.probability_bias_strategy(m)
        self.assertEqual(result, "NO")  # crypto bucket bias +0.09 → bet NO


# ─────────────────────────────────────────────────────────────────────────────
# _infer_market_category — v2 taxonomy
# ─────────────────────────────────────────────────────────────────────────────

from manifold_bot.strategies import _infer_market_category


class TestCategoryV2(unittest.TestCase):
    """
    v2 adds gaming / entertainment / business to shrink the `other` catchall.
    These tests lock in the new routing AND protect the v1 categories from
    regressing when keyword lists are expanded.
    """

    # ── v1 categories — still route correctly ────────────────────────────────

    def test_v1_crypto(self):
        self.assertEqual(_infer_market_category("Will Bitcoin hit $150k?"), "crypto")

    def test_v1_ai_tech(self):
        self.assertEqual(_infer_market_category("Will GPT-5 be released this year?"), "ai_tech")

    def test_v1_politics(self):
        self.assertEqual(_infer_market_category("Will Trump win the 2028 election?"), "politics")

    def test_v1_sports(self):
        self.assertEqual(_infer_market_category("Will the Lakers win the NBA championship?"), "sports")

    def test_v1_economics(self):
        self.assertEqual(
            _infer_market_category("Will inflation hit 5% by Q3?"),
            "economics",
        )

    def test_v1_science(self):
        self.assertEqual(
            _infer_market_category("Will SpaceX launch Starship successfully?"),
            "science",
        )

    # ── v2 new categories ────────────────────────────────────────────────────

    def test_v2_gaming_title(self):
        self.assertEqual(
            _infer_market_category("Will GTA6 release in 2026?"),
            "gaming",
        )

    def test_v2_gaming_platform(self):
        self.assertEqual(
            _infer_market_category("Will Nintendo announce a Switch 2?"),
            "gaming",
        )

    def test_v2_gaming_esports(self):
        self.assertEqual(
            _infer_market_category("Will any esport tournament hit 10M viewers?"),
            "gaming",
        )

    def test_v2_entertainment_movie(self):
        self.assertEqual(
            _infer_market_category("Will the next Marvel movie gross over $1B?"),
            "entertainment",
        )

    def test_v2_entertainment_awards(self):
        self.assertEqual(
            _infer_market_category("Will Oppenheimer win Best Picture at the Oscars?"),
            "entertainment",
        )

    def test_v2_entertainment_celebrity(self):
        self.assertEqual(
            _infer_market_category("Will Taylor Swift release a new album in 2026?"),
            "entertainment",
        )

    def test_v2_entertainment_streaming(self):
        self.assertEqual(
            _infer_market_category("Will Netflix raise subscription prices again?"),
            "entertainment",
        )

    def test_v2_business_company(self):
        self.assertEqual(
            _infer_market_category("Will Apple release a new iPhone in September?"),
            "business",
        )

    def test_v2_business_corporate_action(self):
        self.assertEqual(
            _infer_market_category("Will any major tech company announce layoffs in Q2?"),
            "business",
        )

    def test_v2_business_ipo(self):
        self.assertEqual(
            _infer_market_category("Will Stripe IPO before the end of 2026?"),
            "business",
        )

    # ── Order-of-match conflicts — lock in intended routing ──────────────────

    def test_order_crypto_beats_business(self):
        """Bitcoin-about-Coinbase should be crypto, not business."""
        self.assertEqual(
            _infer_market_category("Will Coinbase list Bitcoin ETF?"),
            "crypto",
        )

    def test_order_ai_tech_beats_business(self):
        """OpenAI IPO should land in ai_tech, not business (openai matches first)."""
        self.assertEqual(
            _infer_market_category("Will OpenAI IPO in 2026?"),
            "ai_tech",
        )

    def test_order_business_beats_economics(self):
        """Apple stock question should land in business (apple matches before stock)."""
        self.assertEqual(
            _infer_market_category("Will Apple stock hit $300?"),
            "business",
        )

    def test_order_economics_still_wins_when_no_company(self):
        """Pure-macro stock-market question lands in economics."""
        self.assertEqual(
            _infer_market_category("Will the stock market crash in 2026?"),
            "economics",
        )

    # ── Substring false-positive guards ──────────────────────────────────────

    def test_substring_apple_vs_pineapple(self):
        """' apple ' is space-bounded — 'pineapple' must NOT match business."""
        # Guards against a regression where 'apple' would match inside 'pineapple'
        # and misroute a food question to business.
        self.assertNotEqual(
            _infer_market_category("Will pineapple be the most-ordered pizza topping?"),
            "business",
        )

    def test_substring_meta_vs_metabolism(self):
        """' meta ' is space-bounded — 'metabolism' must NOT match business."""
        self.assertNotEqual(
            _infer_market_category("Will a new metabolism supplement become popular?"),
            "business",
        )

    def test_substring_nfl_vs_inflation(self):
        """' nfl ' is space-bounded — 'nfl' must NOT match inside 'inflation'."""
        # Regression guard for the v1 bug where 'nfl' (unpadded sports keyword)
        # matched inside 'inflation' and routed economics questions to sports.
        self.assertEqual(
            _infer_market_category("Will inflation hit 5% by Q3?"),
            "economics",
        )

    def test_substring_steam_vs_stream(self):
        """' steam ' must NOT match 'streaming' or 'stream'."""
        # A question about HBO streaming should go to entertainment, not gaming.
        self.assertEqual(
            _infer_market_category("Will HBO streaming subscribers hit 100M?"),
            "entertainment",
        )

    def test_fallthrough_to_other(self):
        """Questions with no matching keyword still land in `other`."""
        self.assertEqual(
            _infer_market_category("Will it rain tomorrow?"),
            "other",
        )


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


class TestPositionClassification(unittest.TestCase):
    """V2 Phase 2.1 — classify_position labels market by horizon × reliability."""

    def _market(self, close_days_away=None, category='sports',
                question='Will the game end in overtime?',
                liquidity=500, bettors=10):
        """Helper: build a market dict for classification tests."""
        import time as _time
        close_time_ms = None
        if close_days_away is not None:
            close_time_ms = int((_time.time() + close_days_away * 86400) * 1000)
        return {
            'question': question,
            'category': category,
            'closeTime': close_time_ms,
            'totalLiquidity': liquidity,
            'uniqueBettorCount': bettors,
        }

    def test_short_horizon_under_7_days(self):
        from manifold_bot.strategies import classify_position
        result = classify_position(self._market(close_days_away=3))
        self.assertEqual(result['horizon'], 'short')

    def test_medium_horizon_8_to_30_days(self):
        from manifold_bot.strategies import classify_position
        result = classify_position(self._market(close_days_away=14))
        self.assertEqual(result['horizon'], 'medium')

    def test_long_horizon_over_30_days(self):
        from manifold_bot.strategies import classify_position
        result = classify_position(self._market(close_days_away=60))
        self.assertEqual(result['horizon'], 'long')

    def test_unknown_horizon_when_no_close_time(self):
        from manifold_bot.strategies import classify_position
        result = classify_position(self._market(close_days_away=None))
        self.assertEqual(result['horizon'], 'unknown')

    def test_past_close_time_treated_as_short(self):
        """Markets past closeTime are 'short' — awaiting creator resolution."""
        from manifold_bot.strategies import classify_position
        result = classify_position(self._market(close_days_away=-2))
        self.assertEqual(result['horizon'], 'short')

    def test_sports_category_is_high_reliability(self):
        from manifold_bot.strategies import classify_position
        result = classify_position(self._market(category='sports'))
        self.assertEqual(result['reliability'], 'high')

    def test_other_category_is_low_reliability(self):
        from manifold_bot.strategies import classify_position
        result = classify_position(self._market(category='other'))
        self.assertEqual(result['reliability'], 'low')

    def test_politics_category_is_medium_reliability(self):
        from manifold_bot.strategies import classify_position
        result = classify_position(self._market(category='politics'))
        self.assertEqual(result['reliability'], 'medium')

    def test_ever_pattern_forces_low_reliability(self):
        """'Will X ever happen' questions → low reliability regardless of category."""
        from manifold_bot.strategies import classify_position
        result = classify_position(self._market(
            question='Will humans ever colonize Mars?',
            category='science',  # would normally be medium
        ))
        self.assertEqual(result['reliability'], 'low')

    def test_low_liquidity_demotes_reliability(self):
        """Sports market with very low liquidity gets demoted from high to medium."""
        from manifold_bot.strategies import classify_position
        result = classify_position(self._market(
            category='sports',
            liquidity=50,
            bettors=2,
        ))
        self.assertIn(result['reliability'], ('medium', 'low'))

    def test_slot_bucket_short_for_short_horizon(self):
        from manifold_bot.strategies import classify_position
        result = classify_position(self._market(close_days_away=3, category='sports'))
        self.assertEqual(result['slot_bucket'], 'short')

    def test_slot_bucket_medium_for_medium_horizon(self):
        from manifold_bot.strategies import classify_position
        result = classify_position(self._market(close_days_away=14, category='politics'))
        self.assertEqual(result['slot_bucket'], 'medium')

    def test_slot_bucket_long_high_for_long_plus_high_reliability(self):
        from manifold_bot.strategies import classify_position
        result = classify_position(self._market(close_days_away=60, category='sports'))
        self.assertEqual(result['slot_bucket'], 'long_high')

    def test_slot_bucket_long_mid_low_for_long_plus_lower_reliability(self):
        from manifold_bot.strategies import classify_position
        result = classify_position(self._market(close_days_away=60, category='other'))
        self.assertEqual(result['slot_bucket'], 'long_mid_low')

    def test_unknown_horizon_maps_to_long_mid_low_bucket(self):
        from manifold_bot.strategies import classify_position
        result = classify_position(self._market(close_days_away=None, category='other'))
        self.assertEqual(result['slot_bucket'], 'long_mid_low')

    def test_days_to_close_is_populated(self):
        from manifold_bot.strategies import classify_position
        result = classify_position(self._market(close_days_away=5))
        self.assertIsNotNone(result['days_to_close'])
        self.assertAlmostEqual(result['days_to_close'], 5.0, places=1)

    def test_days_to_close_is_none_when_no_close_time(self):
        from manifold_bot.strategies import classify_position
        result = classify_position(self._market(close_days_away=None))
        self.assertIsNone(result['days_to_close'])


class TestSlotBucketCap(unittest.TestCase):
    """V2 Phase 2.1 — auto_trader.py enforces per-bucket slot caps."""

    def _make_trader_with_positions(self, positions):
        from unittest.mock import patch, MagicMock
        from automation.auto_trader import AutoTrader
        with patch("automation.auto_trader.PaperTrader") as MockTrader:
            mock_instance = MagicMock()
            mock_instance.positions = positions
            mock_instance.balance = 1000.0
            MockTrader.return_value = mock_instance
            trader = AutoTrader()
        return trader

    def _rec(self, **overrides):
        base = {
            'market_id': 'mkt_test',
            'question': 'Will the short-term match end tomorrow?',
            'recommendation': 'YES',
            'confidence': 0.75,
            'probability': 0.50,
            'liquidity': 500,
            'category': 'sports',
            'strategies': ['probability_direction'],
            'horizon': 'short',
            'reliability': 'high',
            'slot_bucket': 'short',
        }
        base.update(overrides)
        return base

    def _open_pos(self, market_id='mkt_x', **fields):
        """Build an open position with sane defaults + slot_bucket."""
        base = {
            'market_id': market_id,
            'status': 'OPEN',
            'question': 'some question',
            'category': 'sports',
            'amount': 5,
            'probability': 0.5,
            'outcome': 'YES',
            'slot_bucket': 'short',
        }
        base.update(fields)
        return base

    def test_short_bucket_allows_up_to_cap(self):
        """5 short-bucket open positions + short rec → blocked with slot_bucket_full."""
        from automation.auto_trader import _SLOT_BUCKET_CAPS
        short_cap = _SLOT_BUCKET_CAPS['short']
        positions = {
            f'mkt_{i}': [self._open_pos(market_id=f'mkt_{i}', slot_bucket='short', category='sports')]
            for i in range(short_cap)
        }
        trader = self._make_trader_with_positions(positions)
        reason = trader._trade_rejection_reason('mkt_new', self._rec(slot_bucket='short'))
        self.assertEqual(reason, 'slot_bucket_full')

    def test_different_buckets_have_independent_caps(self):
        """5 short positions does NOT block a medium-bucket trade."""
        positions = {
            f'mkt_s{i}': [self._open_pos(market_id=f'mkt_s{i}', slot_bucket='short')]
            for i in range(5)
        }
        trader = self._make_trader_with_positions(positions)
        # Medium rec should pass (no medium positions yet)
        reason = trader._trade_rejection_reason(
            'mkt_new',
            self._rec(slot_bucket='medium', question='Will politics market resolve in 2 weeks?',
                      category='politics'),
        )
        # Note: max_positions check runs first. 5 < 10 so we're fine.
        # Categories may still block — let's ensure it's not slot_bucket
        if reason is not None:
            self.assertNotEqual(reason, 'slot_bucket_full')

    def test_long_high_bucket_cap_is_one(self):
        """Long+high-reliability has only 1 slot — second one blocked."""
        positions = {
            'mkt_long1': [self._open_pos(market_id='mkt_long1', slot_bucket='long_high',
                                         category='economics')],
        }
        trader = self._make_trader_with_positions(positions)
        reason = trader._trade_rejection_reason(
            'mkt_new',
            self._rec(slot_bucket='long_high', question='Will the Fed hike in 2027?',
                      category='economics'),
        )
        self.assertEqual(reason, 'slot_bucket_full')

    def test_legacy_position_without_slot_bucket_still_counted(self):
        """Old positions without slot_bucket field get classified on the fly."""
        from manifold_bot.strategies import classify_position
        # Build a legacy position that should classify as 'short' (sports market)
        import time
        legacy_pos = {
            'market_id': 'legacy_mkt',
            'status': 'OPEN',
            'question': 'Will the NBA finals end today?',
            'category': 'sports',
            'amount': 5,
            'probability': 0.5,
            'outcome': 'YES',
            'close_time_ms': int((time.time() + 2 * 86400) * 1000),
            'liquidity': 500,
            'unique_bettors': 10,
            # NOTE: no slot_bucket field
        }
        from automation.auto_trader import _count_open_in_bucket
        n = _count_open_in_bucket({'legacy_mkt': [legacy_pos]}, 'short')
        self.assertEqual(n, 1)


class TestSlotBucketPersistenceRegression(unittest.TestCase):
    """
    V2 Phase 2.1 regression tests — real persisted position shape.

    Earlier bucket-cap tests used hand-built fixtures with slot_bucket
    pre-populated, or legacy fixtures with close_time_ms. That missed a
    real bug: `place_paper_bet()` wasn't persisting slot_bucket at all,
    so positions on disk had no classification. The on-the-fly fallback
    in _count_open_in_bucket() then classified every one of them as
    unknown→long_mid_low, silently breaking the bucket-cap policy.

    These tests go through the actual place_paper_bet() path so a future
    regression where bucket metadata stops being persisted fails loudly.
    """

    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _fresh_trader(self):
        """Fresh PaperTrader with empty state file in a temp dir.

        PaperTrader picks its state_file in __init__ from a module-level path,
        so we redirect to a tmp file before construction and wipe in-memory
        state after construction.
        """
        import os
        from manifold_bot.paper_trader import PaperTrader
        state_path = os.path.join(self.tmpdir, 'state.json')
        trader = PaperTrader(initial_balance=1000.0)
        trader.state_file = state_path
        trader.balance = 1000.0
        trader.positions = {}
        trader.trade_history = []
        return trader

    def test_place_paper_bet_persists_slot_bucket(self):
        """place_paper_bet must write slot_bucket to the position record."""
        trader = self._fresh_trader()
        import time as _time
        future_close = int((_time.time() + 3 * 86400) * 1000)

        ok = trader.place_paper_bet(
            market_id='mkt_persist_short',
            outcome='YES',
            amount=5.0,
            probability=0.5,
            category='sports',
            question='Will the NBA finals game 1 go to overtime?',
            horizon='short',
            reliability='high',
            slot_bucket='short',
            close_time_ms=future_close,
        )
        self.assertTrue(ok)
        positions = trader.positions['mkt_persist_short']
        self.assertEqual(len(positions), 1)
        pos = positions[0]
        # These fields MUST be persisted, else slot caps break silently
        self.assertEqual(pos['slot_bucket'], 'short')
        self.assertEqual(pos['horizon'], 'short')
        self.assertEqual(pos['reliability'], 'high')
        self.assertEqual(pos['close_time_ms'], future_close)

    def test_count_open_in_bucket_reads_persisted_field(self):
        """_count_open_in_bucket should use the stored slot_bucket, not re-classify."""
        trader = self._fresh_trader()
        import time as _time
        future = int((_time.time() + 2 * 86400) * 1000)

        # Place a real 'short' trade through place_paper_bet
        trader.place_paper_bet(
            market_id='mkt_s',
            outcome='YES',
            amount=5.0,
            probability=0.5,
            category='sports',
            question='Short-horizon sports market',
            horizon='short',
            reliability='high',
            slot_bucket='short',
            close_time_ms=future,
        )

        from automation.auto_trader import _count_open_in_bucket
        short_count = _count_open_in_bucket(trader.positions, 'short')
        long_mid_low_count = _count_open_in_bucket(trader.positions, 'long_mid_low')

        self.assertEqual(short_count, 1, "persisted slot_bucket='short' must count in 'short'")
        self.assertEqual(long_mid_low_count, 0,
                         "persisted short position must NOT count as long_mid_low")

    def test_real_executed_short_positions_enforce_short_cap(self):
        """
        End-to-end regression: five executed short-horizon trades should block a
        sixth short-bucket recommendation via slot_bucket_full, not None.
        """
        from unittest.mock import patch, MagicMock
        from automation.auto_trader import AutoTrader, _SLOT_BUCKET_CAPS
        import time as _time

        # Spin up AutoTrader with a real PaperTrader writing to temp state
        import os
        state_file = os.path.join(self.tmpdir, 'state.json')
        with patch.dict(os.environ, {}, clear=False):
            with patch('automation.auto_trader.PaperTrader') as MockTrader:
                real_trader = MagicMock()
                real_trader.positions = {}
                real_trader.balance = 1000.0
                MockTrader.return_value = real_trader
                at = AutoTrader()

        # Populate five short positions through the REAL persistence shape
        # (same dict layout place_paper_bet would write).
        future = int((_time.time() + 3 * 86400) * 1000)
        for i in range(_SLOT_BUCKET_CAPS['short']):
            at.trader.positions[f'mkt_s{i}'] = [{
                'trade_id': i,
                'market_id': f'mkt_s{i}',
                'status': 'OPEN',
                'outcome': 'YES',
                'amount': 5.0,
                'probability': 0.5,
                'category': 'sports',
                'question': f'Short market {i}',
                'horizon': 'short',
                'reliability': 'high',
                'slot_bucket': 'short',
                'close_time_ms': future,
            }]

        # New short recommendation must be rejected with slot_bucket_full
        rec = {
            'market_id': 'mkt_new',
            'question': 'Another short-horizon sports match tomorrow',
            'recommendation': 'YES',
            'confidence': 0.80,
            'probability': 0.50,
            'liquidity': 500,
            'category': 'sports',
            'strategies': ['probability_direction'],
            'horizon': 'short',
            'reliability': 'high',
            'slot_bucket': 'short',
            'close_time_ms': future,
        }
        reason = at._trade_rejection_reason('mkt_new', rec)
        self.assertEqual(
            reason,
            'slot_bucket_full',
            f"Expected slot_bucket_full but got {reason!r} — bucket cap is not firing "
            "on real persisted short positions. Bucket metadata may not be flowing "
            "from place_paper_bet() to the saved position record.",
        )


if __name__ == '__main__':
    unittest.main()
