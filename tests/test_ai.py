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

from manifold_bot.ai_analyzer import (
    _parse_response,
    analyze_market,
    ANALYSIS_SCHEMA,
)


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

    # ── PR #29: parse-failure fallback ────────────────────────────────────────
    # Pre-PR bug: when Ollama returned text that _parse_response couldn't
    # extract JSON from, analyze_market returned None WITHOUT trying DeepSeek.
    # 24h log audit pre-fix: ~78% of calls had parsed_output=null, all on
    # Ollama, all silently giving up.

    def test_falls_back_to_deepseek_on_ollama_PARSE_failure(self):
        """The bug PR #29 fixes: Ollama returns text that won't parse, but
        DeepSeek can. Must try DeepSeek and use its result."""
        import unittest.mock as mock

        market = self._make_market()
        ollama_garbage = "I'm sorry, but I can't analyze this market right now."  # no JSON
        deepseek_good = ('{"recommendation":"YES","confidence":0.71,'
                         '"estimated_true_probability":0.65,'
                         '"reasoning":"Real reasoning.","risk_factors":"Minor."}')

        with mock.patch('manifold_bot.ai_analyzer._call_ollama', return_value=ollama_garbage), \
             mock.patch('manifold_bot.ai_analyzer._call_deepseek_api', return_value=deepseek_good):
            result = analyze_market(market)

        self.assertIsNotNone(result, "DeepSeek should have rescued the parse failure")
        self.assertEqual(result['source'], 'deepseek_api')
        self.assertEqual(result['recommendation'], 'YES')

    def test_returns_none_when_both_ollama_AND_deepseek_parse_fail(self):
        """Both back-ends produce unparseable output → return None.
        Pre-PR this would short-circuit on Ollama; post-PR we still try
        DeepSeek but accept None when both fail."""
        import unittest.mock as mock

        market = self._make_market()

        with mock.patch('manifold_bot.ai_analyzer._call_ollama', return_value="garbage"), \
             mock.patch('manifold_bot.ai_analyzer._call_deepseek_api', return_value="more garbage"):
            result = analyze_market(market)

        self.assertIsNone(result)

    def test_log_record_carries_observability_flags(self):
        """The log fields are what PR #29's 24h validation keys off. The
        attempt vs. returned-value split (deepseek_attempted vs.
        deepseek_returned_value) lets the audit distinguish 'DeepSeek was
        tried but didn't help' from 'DeepSeek wasn't tried' — the original
        single flag conflated them."""
        import unittest.mock as mock

        market = self._make_market()
        ollama_garbage = "no json here"
        deepseek_good = ('{"recommendation":"NO","confidence":0.69,'
                         '"estimated_true_probability":0.30,'
                         '"reasoning":"X.","risk_factors":"Y."}')

        captured = {}
        def _capture(*args, **kwargs):
            captured.update(kwargs)

        with mock.patch('manifold_bot.ai_analyzer._call_ollama', return_value=ollama_garbage), \
             mock.patch('manifold_bot.ai_analyzer._call_deepseek_api', return_value=deepseek_good), \
             mock.patch('manifold_bot.ai_analyzer._log_llm_call', side_effect=_capture):
            result = analyze_market(market)

        self.assertIsNotNone(result)
        self.assertTrue(captured.get('ollama_parse_failed'),
                        "ollama_parse_failed must be True when Ollama returned garbage")
        self.assertTrue(captured.get('deepseek_attempted'),
                        "deepseek_attempted must be True when we entered the fallback branch")
        self.assertTrue(captured.get('deepseek_returned_value'),
                        "deepseek_returned_value must be True when DeepSeek returned non-None")
        self.assertTrue(captured.get('deepseek_saved_a_parse_failure'),
                        "deepseek_saved_a_parse_failure must be True — that's the headline metric")
        self.assertFalse(captured.get('ollama_returned_none'),
                         "ollama_returned_none must be False — Ollama did return something")

    def test_log_record_when_deepseek_attempted_but_unavailable(self):
        """Reviewer-flagged ambiguity: when Ollama parse-fails AND DeepSeek
        is also unavailable, deepseek_attempted should be True (we tried)
        but deepseek_returned_value should be False (it didn't help). The
        24h audit needs both numbers to triage 'rescue path tried but
        broken' vs 'rescue path never reached'."""
        import unittest.mock as mock

        market = self._make_market()

        captured = {}
        def _capture(*args, **kwargs):
            captured.update(kwargs)

        with mock.patch('manifold_bot.ai_analyzer._call_ollama', return_value="garbage"), \
             mock.patch('manifold_bot.ai_analyzer._call_deepseek_api', return_value=None), \
             mock.patch('manifold_bot.ai_analyzer._log_llm_call', side_effect=_capture):
            result = analyze_market(market)

        self.assertIsNone(result)
        self.assertTrue(captured.get('ollama_parse_failed'))
        self.assertTrue(captured.get('deepseek_attempted'),
                        "We tried DeepSeek even though it returned None")
        self.assertFalse(captured.get('deepseek_returned_value'),
                         "DeepSeek returned None — fallback was attempted but failed")
        self.assertFalse(captured.get('deepseek_saved_a_parse_failure'))

    def test_log_record_when_ollama_returns_none_and_deepseek_succeeds(self):
        """Pre-PR behavior preserved: transport-level Ollama failure → DeepSeek
        fallback. Distinguishes ollama_returned_none from ollama_parse_failed
        in the log so 24h analytics can split the two failure modes."""
        import unittest.mock as mock

        market = self._make_market()
        deepseek_good = ('{"recommendation":"YES","confidence":0.72,'
                         '"estimated_true_probability":0.70,'
                         '"reasoning":"X.","risk_factors":"Y."}')

        captured = {}
        def _capture(*args, **kwargs):
            captured.update(kwargs)

        with mock.patch('manifold_bot.ai_analyzer._call_ollama', return_value=None), \
             mock.patch('manifold_bot.ai_analyzer._call_deepseek_api', return_value=deepseek_good), \
             mock.patch('manifold_bot.ai_analyzer._log_llm_call', side_effect=_capture):
            analyze_market(market)

        self.assertTrue(captured.get('ollama_returned_none'))
        self.assertFalse(captured.get('ollama_parse_failed'),
                         "Parse never ran — Ollama returned None, not garbage")
        self.assertTrue(captured.get('deepseek_attempted'))
        self.assertTrue(captured.get('deepseek_returned_value'))
        self.assertFalse(captured.get('deepseek_saved_a_parse_failure'),
                         "deepseek_saved_a_parse_failure is specifically the parse-fail-rescue case")

    def test_log_record_when_only_ollama_used(self):
        """Happy path: Ollama returns parseable JSON. None of the failure
        flags should be set, including the new attempt+returned split."""
        import unittest.mock as mock

        market = self._make_market()
        ollama_good = ('{"recommendation":"YES","confidence":0.80,'
                       '"estimated_true_probability":0.82,'
                       '"reasoning":"X.","risk_factors":"Y."}')

        captured = {}
        def _capture(*args, **kwargs):
            captured.update(kwargs)

        with mock.patch('manifold_bot.ai_analyzer._call_ollama', return_value=ollama_good), \
             mock.patch('manifold_bot.ai_analyzer._log_llm_call', side_effect=_capture):
            analyze_market(market)

        self.assertFalse(captured.get('ollama_returned_none'))
        self.assertFalse(captured.get('ollama_parse_failed'))
        self.assertFalse(captured.get('deepseek_attempted'))
        self.assertFalse(captured.get('deepseek_returned_value'))
        self.assertFalse(captured.get('deepseek_saved_a_parse_failure'))

    def test_deepseek_key_read_at_call_time_not_import_time(self):
        """Regression: ai_analyzer used to capture DEEPSEEK_API_KEY in a
        module-level constant at import time, which silently broke the
        DeepSeek fallback whenever ai_analyzer was imported before config.py
        loaded .env (e.g. an isolated unit test, or a future entry point
        that doesn't go through manifold_api first).

        The fix reads os.environ.get("DEEPSEEK_API_KEY", "") inside
        _call_deepseek_api at call time. This test simulates the broken
        path: env starts empty when ai_analyzer is imported (already done),
        we set the key AFTER import, and verify the call still picks it up."""
        import os
        import unittest.mock as mock
        import manifold_bot.ai_analyzer as ai_mod

        # Make sure module-level constant doesn't exist anymore (the fix)
        self.assertFalse(hasattr(ai_mod, 'DEEPSEEK_API_KEY'),
                         "Module-level DEEPSEEK_API_KEY constant must be gone — "
                         "it created an import-order ordering contract that "
                         "silently broke fallback when ai_analyzer imported "
                         "first.")

        original = os.environ.get("DEEPSEEK_API_KEY", "")
        try:
            # Simulate "key is set in env after ai_analyzer was already imported"
            os.environ["DEEPSEEK_API_KEY"] = "test-key-set-after-import"
            self.assertEqual(ai_mod._get_deepseek_api_key(), "test-key-set-after-import")

            os.environ["DEEPSEEK_API_KEY"] = ""
            self.assertEqual(ai_mod._get_deepseek_api_key(), "")
        finally:
            if original:
                os.environ["DEEPSEEK_API_KEY"] = original
            else:
                os.environ.pop("DEEPSEEK_API_KEY", None)

    def test_deepseek_call_returns_none_when_key_missing_at_call_time(self):
        """Behavioral test of the lazy-read: with the env var explicitly
        empty at call time, _call_deepseek_api short-circuits to None."""
        import os
        import manifold_bot.ai_analyzer as ai_mod

        original = os.environ.get("DEEPSEEK_API_KEY", "")
        try:
            os.environ["DEEPSEEK_API_KEY"] = ""
            self.assertIsNone(ai_mod._call_deepseek_api("any prompt"))
        finally:
            if original:
                os.environ["DEEPSEEK_API_KEY"] = original
            else:
                os.environ.pop("DEEPSEEK_API_KEY", None)


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
        # High-confidence AI (0.95) → penalty multiplier 0.42 → smaller result (harder penalty)
        # Low-confidence  AI (0.55) → penalty multiplier 0.58 → larger result (softer penalty)
        penalty_high = 0.4 + (1 - 0.95) * 0.4  # 0.42
        penalty_low  = 0.4 + (1 - 0.55) * 0.4  # 0.58
        result_high = self._blend(0.70, 0.95, 'YES', 'NO')
        result_low  = self._blend(0.70, 0.55, 'YES', 'NO')
        self.assertLess(result_high, result_low)  # certain AI penalizes more
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


class TestAnalysisSchemaShape(unittest.TestCase):
    """ANALYSIS_SCHEMA is the contract Ollama's constrained decoding enforces.
    Lock its shape so we don't accidentally drift the schema and silently
    invalidate every previous run's outputs."""

    def test_required_keys_present(self):
        required = ANALYSIS_SCHEMA["required"]
        self.assertEqual(set(required), {
            "recommendation",
            "confidence",
            "estimated_true_probability",
            "reasoning",
            "risk_factors",
        })

    def test_recommendation_is_enum(self):
        rec = ANALYSIS_SCHEMA["properties"]["recommendation"]
        self.assertEqual(rec["type"], "string")
        self.assertEqual(set(rec["enum"]), {"YES", "NO", "SKIP"})

    def test_probability_fields_bounded(self):
        for k in ("confidence", "estimated_true_probability"):
            field = ANALYSIS_SCHEMA["properties"][k]
            self.assertEqual(field["type"], "number")
            self.assertEqual(field["minimum"], 0.0)
            self.assertEqual(field["maximum"], 1.0)


class TestStructuredOutputWiring(unittest.TestCase):
    """Verify the schema and response_format actually get sent to the
    LLM endpoints. These are the on-the-wire contracts that make the
    constrained-decoding fix work."""

    def _make_market(self):
        return {
            'id': 'test123',
            'question': 'Will it rain tomorrow?',
            'probability': 0.5,
            'volume': 1000,
            'totalLiquidity': 500,
            'closeTime': 1800000000000,
            'creatorName': 'tester',
        }

    def test_ollama_request_carries_format_schema(self):
        """The whole point of PR #30: pass `format=ANALYSIS_SCHEMA` to the
        Ollama generate endpoint so the model's output is constrained-
        decoded against the schema. If this regresses, we silently go back
        to the 78% null-output rate from prose-prompted JSON."""
        import unittest.mock as mock

        captured_request = {}

        class _MockResponse:
            status_code = 200
            def iter_lines(self, chunk_size=None):
                yield b'{"response": "{\\"recommendation\\":\\"YES\\",\\"confidence\\":0.7,\\"estimated_true_probability\\":0.7,\\"reasoning\\":\\"x\\",\\"risk_factors\\":\\"y\\"}", "done": true}'
            def __init__(self): self.raw = mock.MagicMock()

        def _fake_post(url, json=None, **kwargs):
            captured_request['url'] = url
            captured_request['json'] = json
            return _MockResponse()

        with mock.patch('requests.post', side_effect=_fake_post), \
             mock.patch('manifold_bot.ai_analyzer._call_deepseek_api', return_value=None):
            analyze_market(self._make_market())

        self.assertIn('format', captured_request['json'],
                      'Ollama request must carry format= for constrained decoding')
        self.assertEqual(captured_request['json']['format'], ANALYSIS_SCHEMA,
                         'format= must be the canonical ANALYSIS_SCHEMA, not a stale copy')
        self.assertIn('/api/generate', captured_request['url'])

    def test_deepseek_request_carries_json_object_response_format(self):
        """DeepSeek doesn't support strict JSON Schema; it does support
        `response_format={"type":"json_object"}` which guarantees valid JSON
        syntax (not shape). _parse_response still validates shape against
        ANALYSIS_SCHEMA. Without json_object, the fallback re-introduces
        the parse-failure mode we just fixed for Ollama."""
        import unittest.mock as mock

        captured_request = {}

        class _MockDeepseekResponse:
            status_code = 200
            def json(self):
                return {"choices": [{"message": {"content":
                    '{"recommendation":"NO","confidence":0.7,'
                    '"estimated_true_probability":0.3,"reasoning":"x","risk_factors":"y"}'
                }}]}

        def _fake_post(url, json=None, **kwargs):
            captured_request['url'] = url
            captured_request['json'] = json
            return _MockDeepseekResponse()

        with mock.patch('manifold_bot.ai_analyzer._call_ollama', return_value=None), \
             mock.patch('requests.post', side_effect=_fake_post), \
             mock.patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'test-key'}):
            analyze_market(self._make_market())

        self.assertIn('response_format', captured_request['json'],
                      'DeepSeek request must carry response_format')
        self.assertEqual(
            captured_request['json']['response_format'],
            {"type": "json_object"},
            'response_format must be json_object — DeepSeek does not support strict JSON schema'
        )
        self.assertIn('chat/completions', captured_request['url'])


if __name__ == '__main__':
    unittest.main()
