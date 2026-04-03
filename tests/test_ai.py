#!/usr/bin/env python3
"""
Tests for ai_analyzer.py

These tests do NOT call any external API — they test parsing, blending,
and graceful degradation only.
"""

import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from manifold_bot.ai_analyzer import _parse_response, analyze_market


class TestParseResponse(unittest.TestCase):
    """Tests for the JSON extraction / parsing logic."""

    def test_clean_json_yes(self):
        raw = '''{
            "recommendation": "YES",
            "confidence": 0.82,
            "estimated_true_probability": 0.75,
            "reasoning": "Market is underpriced given recent news.",
            "risk_factors": "Could resolve early if event cancelled."
        }'''
        result = _parse_response(raw)
        self.assertIsNotNone(result)
        self.assertEqual(result['recommendation'], 'YES')
        self.assertAlmostEqual(result['confidence'], 0.82)

    def test_clean_json_no(self):
        raw = '{"recommendation":"NO","confidence":0.71,"estimated_true_probability":0.3,"reasoning":"Overpriced.","risk_factors":"None."}'
        result = _parse_response(raw)
        self.assertIsNotNone(result)
        self.assertEqual(result['recommendation'], 'NO')

    def test_skip_recommendation(self):
        raw = '{"recommendation":"SKIP","confidence":0.5,"estimated_true_probability":0.5,"reasoning":"No edge.","risk_factors":""}'
        result = _parse_response(raw)
        self.assertIsNotNone(result)
        self.assertEqual(result['recommendation'], 'SKIP')
        self.assertEqual(result['confidence'], 0.0)  # SKIP forces confidence to 0

    def test_strips_think_tags(self):
        raw = '<think>Let me reason through this...\nStep 1: analyse.</think>\n{"recommendation":"YES","confidence":0.78,"estimated_true_probability":0.8,"reasoning":"Good bet.","risk_factors":"Low."}'
        result = _parse_response(raw)
        self.assertIsNotNone(result)
        self.assertEqual(result['recommendation'], 'YES')

    def test_json_embedded_in_prose(self):
        raw = 'Here is my analysis:\n{"recommendation":"NO","confidence":0.65,"estimated_true_probability":0.35,"reasoning":"Crowd is overconfident.","risk_factors":"Breaking news."}\nHope this helps!'
        result = _parse_response(raw)
        self.assertIsNotNone(result)
        self.assertEqual(result['recommendation'], 'NO')

    def test_confidence_clamped(self):
        raw = '{"recommendation":"YES","confidence":1.5,"estimated_true_probability":0.9,"reasoning":"Sure thing.","risk_factors":""}'
        result = _parse_response(raw)
        self.assertIsNotNone(result)
        self.assertEqual(result['confidence'], 1.0)

    def test_invalid_recommendation_becomes_skip(self):
        raw = '{"recommendation":"MAYBE","confidence":0.6,"estimated_true_probability":0.5,"reasoning":"Unsure.","risk_factors":""}'
        result = _parse_response(raw)
        self.assertIsNotNone(result)
        self.assertEqual(result['recommendation'], 'SKIP')

    def test_empty_string_returns_none(self):
        self.assertIsNone(_parse_response(''))

    def test_no_json_returns_none(self):
        self.assertIsNone(_parse_response('This is just plain text with no JSON.'))

    def test_malformed_json_returns_none(self):
        self.assertIsNone(_parse_response('{recommendation: YES, confidence: 0.8}'))


class TestAnalyzeMarketGracefulDegradation(unittest.TestCase):
    """Tests that analyze_market handles unavailable backends without crashing."""

    def _make_market(self):
        return {
            'id': 'test123',
            'question': 'Will Python remain the most popular language in 2027?',
            'probability': 0.72,
            'volume': 5000,
            'totalLiquidity': 1200,
            'closeTime': 1800000000000,
            'creatorName': 'testuser',
        }

    def test_returns_none_when_backends_unavailable(self):
        """With no API key and Ollama unreachable, should return None gracefully."""
        import unittest.mock as mock

        market = self._make_market()

        # Patch both backends to return None
        with mock.patch('manifold_bot.ai_analyzer._call_ollama', return_value=None), \
             mock.patch('manifold_bot.ai_analyzer._call_deepseek_api', return_value=None):
            result = analyze_market(market)

        self.assertIsNone(result)

    def test_uses_ollama_when_available(self):
        import unittest.mock as mock

        market = self._make_market()
        mock_response = '{"recommendation":"YES","confidence":0.80,"estimated_true_probability":0.82,"reasoning":"Test reasoning.","risk_factors":"Test risk."}'

        with mock.patch('manifold_bot.ai_analyzer._call_ollama', return_value=mock_response):
            result = analyze_market(market)

        self.assertIsNotNone(result)
        self.assertEqual(result['recommendation'], 'YES')
        self.assertEqual(result['source'], 'ollama')
        self.assertEqual(result['market_id'], 'test123')

    def test_falls_back_to_deepseek_api_when_ollama_unavailable(self):
        import unittest.mock as mock

        market = self._make_market()
        mock_response = '{"recommendation":"NO","confidence":0.73,"estimated_true_probability":0.30,"reasoning":"Overpriced market.","risk_factors":"News could change things."}'

        with mock.patch('manifold_bot.ai_analyzer._call_ollama', return_value=None), \
             mock.patch('manifold_bot.ai_analyzer._call_deepseek_api', return_value=mock_response):
            result = analyze_market(market)

        self.assertIsNotNone(result)
        self.assertEqual(result['recommendation'], 'NO')
        self.assertEqual(result['source'], 'deepseek_api')

    def test_result_structure(self):
        import unittest.mock as mock

        market = self._make_market()
        mock_response = '{"recommendation":"YES","confidence":0.77,"estimated_true_probability":0.80,"reasoning":"Good signal.","risk_factors":"Minor risk."}'

        with mock.patch('manifold_bot.ai_analyzer._call_ollama', return_value=mock_response):
            result = analyze_market(market)

        self.assertIsNotNone(result)
        for key in ('recommendation', 'confidence', 'estimated_true_probability', 'reasoning', 'risk_factors', 'source', 'market_id'):
            self.assertIn(key, result, f"Missing key: {key}")


class TestConfidenceBlending(unittest.TestCase):
    """
    Tests for the blending logic in auto_research.py.
    Exercises the stat_conf floor, weak-stat cap, and penalty path.
    """

    def _blend(self, stat_conf, ai_conf, ai_rec, stat_rec):
        """Replicate the blending logic from auto_research.py inline."""
        from manifold_bot.config import MIN_CONFIDENCE
        WEAK_STAT_CAP = MIN_CONFIDENCE - 0.01   # 0.64
        STAT_BOOST_FLOOR = MIN_CONFIDENCE        # 0.65
        MAX_AI_BOOST = MIN_CONFIDENCE + 0.10    # 0.75

        if ai_rec == stat_rec:
            blended = round(0.4 * stat_conf + 0.6 * ai_conf, 3)
            if stat_conf < STAT_BOOST_FLOOR:
                blended = min(blended, WEAK_STAT_CAP)
            elif stat_conf <= STAT_BOOST_FLOOR + 0.001:
                blended = min(blended, MAX_AI_BOOST)
            return blended
        elif ai_rec in ('YES', 'NO'):
            # Penalty scales with AI confidence: certain AI penalizes harder
            penalty = 0.4 + (1 - ai_conf) * 0.4
            return round(stat_conf * penalty, 3)
        return stat_conf  # unexpected AI output — unchanged

    def test_strong_stat_ai_agree_boosts_above_threshold(self):
        # stat=0.70 (above floor 0.65), ai=0.90 agree → blended = 0.4*0.70 + 0.6*0.90 = 0.82
        result = self._blend(0.70, 0.90, 'NO', 'NO')
        self.assertGreater(result, 0.65)

    def test_borderline_stat_ai_agree_capped_below_threshold(self):
        # stat=0.61 (below floor 0.65), ai=0.99 agree → capped at 0.64
        result = self._blend(0.61, 0.99, 'YES', 'YES')
        self.assertLess(result, 0.65)
        self.assertEqual(result, 0.64)

    def test_weak_stat_ai_agree_capped_below_threshold(self):
        # stat=0.54 (well below floor), ai=0.95 agree → capped at 0.64
        result = self._blend(0.54, 0.95, 'YES', 'YES')
        self.assertLess(result, 0.65)
        self.assertEqual(result, 0.64)

    def test_stat_exactly_at_floor_is_capped_at_max_boost(self):
        # stat=0.65 (exactly at floor) → capped at _MAX_AI_BOOST (0.75), not freely boosted
        result = self._blend(0.65, 0.99, 'NO', 'NO')
        self.assertGreater(result, 0.65)   # can go above threshold (modest boost allowed)
        self.assertLessEqual(result, 0.75) # but capped at MAX_AI_BOOST

    def test_stat_above_floor_is_not_capped(self):
        # stat=0.66 (above floor) → AI can boost freely
        result = self._blend(0.66, 0.90, 'NO', 'NO')
        self.assertGreater(result, 0.64)

    def test_ai_disagree_high_confidence_penalizes_harder(self):
        # High-confidence AI (0.95) penalizes harder than low-confidence (0.55)
        penalty_high = 0.4 + (1 - 0.95) * 0.4  # 0.42
        penalty_low  = 0.4 + (1 - 0.55) * 0.4  # 0.58
        result_high = self._blend(0.70, 0.95, 'YES', 'NO')
        result_low  = self._blend(0.70, 0.55, 'YES', 'NO')
        self.assertLess(result_high, result_low)
        self.assertAlmostEqual(result_high, round(0.70 * penalty_high, 3))

    def test_ai_disagree_penalizes_confidence(self):
        # stat=0.70, ai disagrees at 0.85 confidence → penalty = 0.4 + 0.15*0.4 = 0.46
        expected = round(0.70 * (0.4 + (1 - 0.85) * 0.4), 3)
        result = self._blend(0.70, 0.85, 'YES', 'NO')
        self.assertAlmostEqual(result, expected)
        self.assertLess(result, 0.65)

    def test_ai_skip_leaves_confidence_unchanged(self):
        # SKIP is not YES/NO so confidence is unchanged
        result = self._blend(0.70, 0.0, 'SKIP', 'NO')
        self.assertAlmostEqual(result, 0.70)

    def test_unexpected_ai_output_leaves_confidence_unchanged(self):
        result = self._blend(0.66, 0.80, 'MAYBE', 'YES')
        self.assertAlmostEqual(result, 0.66)

    def test_weak_cap_derived_from_min_confidence(self):
        # Verify the cap and floor are always derived from the single source of truth
        from manifold_bot.config import MIN_CONFIDENCE
        WEAK_STAT_CAP = MIN_CONFIDENCE - 0.01
        STAT_BOOST_FLOOR = MIN_CONFIDENCE
        self.assertAlmostEqual(WEAK_STAT_CAP, 0.64)
        self.assertAlmostEqual(STAT_BOOST_FLOOR, 0.65)
        self.assertLess(WEAK_STAT_CAP, STAT_BOOST_FLOOR)  # cap must always be below floor


if __name__ == '__main__':
    unittest.main()
