#!/usr/bin/env python3
"""
Tests for real per-trade inference-cost capture (workbench-q512).

Covers the full chain with NO network and NO live API key:

  1. _compute_deepseek_cost_usd — token usage x published pricing arithmetic,
     including the cache-hit/cache-miss split and all "can't price honestly →
     None" edge cases.
  2. _call_deepseek_api — captures the API-reported `usage` object into the
     caller-supplied meta dict (mocked requests.post).
  3. analyze_market — attaches `inference_cost` to the result: real USD on the
     DeepSeek path, 0.0 on the (free, local) Ollama path, and OMITS the field
     when usage is unavailable so downstream estimate-fallback applies.
  4. PaperTrader.place_paper_bet — persists `inference_cost` on the trade
     record, and stays backward compatible when the argument is omitted.

The scorekeeper's manifold_bot adapter (workbench repo) prefers an explicit
`inference_cost` field on a trade record over its flat
--inference-cost-per-ai-call estimate; these tests pin the producing side of
that contract.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from manifold_bot import ai_analyzer
from manifold_bot.ai_analyzer import (
    DEEPSEEK_MODEL,
    DEEPSEEK_PRICING_USD_PER_MTOK,
    _call_deepseek_api,
    _compute_deepseek_cost_usd,
    analyze_market,
)
from manifold_bot.paper_trader import PaperTrader


# A realistic DeepSeek usage payload: ~1.1k prompt tokens (system prompt is
# stable across calls so most of it cache-hits after the first market of a
# batch), 400 max output tokens.
USAGE_FULL = {
    "prompt_tokens": 1100,
    "prompt_cache_hit_tokens": 700,
    "prompt_cache_miss_tokens": 400,
    "completion_tokens": 350,
    "total_tokens": 1450,
}

_P = DEEPSEEK_PRICING_USD_PER_MTOK[DEEPSEEK_MODEL]
EXPECTED_COST_FULL = round(
    (
        700 * _P["input_cache_hit"]
        + 400 * _P["input_cache_miss"]
        + 350 * _P["output"]
    )
    / 1_000_000,
    8,
)

VALID_ANALYSIS_JSON = json.dumps(
    {
        "recommendation": "YES",
        "confidence": 0.8,
        "estimated_true_probability": 0.75,
        "reasoning": "Base rate is materially higher than the market price.",
        "risk_factors": "Event could be cancelled.",
    }
)

MARKET = {
    "id": "mkt123",
    "question": "Will the thing happen?",
    "probability": 0.4,
    "volume": 1000,
    "totalLiquidity": 500,
    "closeTime": 1790000000000,
}


def _deepseek_response(content=VALID_ANALYSIS_JSON, usage=USAGE_FULL, status=200):
    """Build a mock requests.Response for the DeepSeek chat-completions call."""
    resp = MagicMock()
    resp.status_code = status
    body = {"choices": [{"message": {"content": content}}]}
    if usage is not None:
        body["usage"] = usage
    resp.json.return_value = body
    return resp


class TestComputeDeepseekCostUsd(unittest.TestCase):
    """Pure pricing arithmetic — the part money claims trace back to."""

    def test_full_usage_payload(self):
        cost = _compute_deepseek_cost_usd(USAGE_FULL, DEEPSEEK_MODEL)
        self.assertIsNotNone(cost)
        self.assertAlmostEqual(cost, EXPECTED_COST_FULL, places=10)
        # Sanity: a single flash-tier analysis call is a fraction of a cent.
        self.assertGreater(cost, 0.0)
        self.assertLess(cost, 0.01)

    def test_cache_split_priced_differently_than_all_miss(self):
        all_miss = dict(USAGE_FULL,
                        prompt_cache_hit_tokens=0,
                        prompt_cache_miss_tokens=1100)
        self.assertGreater(
            _compute_deepseek_cost_usd(all_miss, DEEPSEEK_MODEL),
            _compute_deepseek_cost_usd(USAGE_FULL, DEEPSEEK_MODEL),
        )

    def test_fallback_to_prompt_tokens_when_no_cache_split(self):
        # Older/minimal payload shape: bill everything at the miss rate.
        usage = {"prompt_tokens": 1000, "completion_tokens": 100}
        expected = round(
            (1000 * _P["input_cache_miss"] + 100 * _P["output"]) / 1_000_000, 8
        )
        self.assertAlmostEqual(
            _compute_deepseek_cost_usd(usage, DEEPSEEK_MODEL), expected, places=10
        )

    def test_unknown_model_returns_none(self):
        self.assertIsNone(_compute_deepseek_cost_usd(USAGE_FULL, "some-future-model"))

    def test_missing_usage_returns_none(self):
        self.assertIsNone(_compute_deepseek_cost_usd(None, DEEPSEEK_MODEL))
        self.assertIsNone(_compute_deepseek_cost_usd("not-a-dict", DEEPSEEK_MODEL))

    def test_unusable_token_counts_return_none(self):
        # No prompt accounting at all
        self.assertIsNone(
            _compute_deepseek_cost_usd({"completion_tokens": 5}, DEEPSEEK_MODEL)
        )
        # Negative counts are corrupt data, not a $0 bill
        self.assertIsNone(
            _compute_deepseek_cost_usd(
                {"prompt_tokens": -5, "completion_tokens": 5}, DEEPSEEK_MODEL
            )
        )
        # Non-numeric counts
        self.assertIsNone(
            _compute_deepseek_cost_usd(
                {"prompt_tokens": "lots", "completion_tokens": 5}, DEEPSEEK_MODEL
            )
        )

    def test_zero_tokens_is_zero_cost_not_none(self):
        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        self.assertEqual(_compute_deepseek_cost_usd(usage, DEEPSEEK_MODEL), 0.0)


class TestCallDeepseekApiUsageCapture(unittest.TestCase):
    """_call_deepseek_api must surface the API-reported usage via meta."""

    @patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"})
    @patch("requests.post")
    def test_meta_captures_usage(self, mock_post):
        mock_post.return_value = _deepseek_response()
        meta = {}
        raw = _call_deepseek_api("prompt", meta=meta)
        self.assertEqual(raw, VALID_ANALYSIS_JSON)
        self.assertEqual(meta.get("usage"), USAGE_FULL)

    @patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"})
    @patch("requests.post")
    def test_meta_absent_usage_left_unset(self, mock_post):
        mock_post.return_value = _deepseek_response(usage=None)
        meta = {}
        raw = _call_deepseek_api("prompt", meta=meta)
        self.assertEqual(raw, VALID_ANALYSIS_JSON)
        self.assertNotIn("usage", meta)

    @patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"})
    @patch("requests.post")
    def test_meta_none_is_supported(self, mock_post):
        # Existing call sites pass no meta — must keep working unchanged.
        mock_post.return_value = _deepseek_response()
        self.assertEqual(_call_deepseek_api("prompt"), VALID_ANALYSIS_JSON)

    @patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""})
    def test_no_api_key_returns_none(self):
        meta = {}
        self.assertIsNone(_call_deepseek_api("prompt", meta=meta))
        self.assertNotIn("usage", meta)


class TestAnalyzeMarketInferenceCost(unittest.TestCase):
    """End-to-end: analyze_market attaches the right inference_cost."""

    def setUp(self):
        # Redirect the llm_calls.jsonl log into a temp dir so tests never
        # touch the real logs/ directory.
        self._tmpdir = tempfile.TemporaryDirectory()
        self._log_patch = patch.object(
            ai_analyzer, "LLM_LOG_DIR", Path(self._tmpdir.name)
        )
        self._log_patch.start()

    def tearDown(self):
        self._log_patch.stop()
        self._tmpdir.cleanup()

    @patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"})
    @patch("requests.post")
    @patch.object(ai_analyzer, "_call_ollama", return_value=None)
    def test_deepseek_path_real_cost(self, mock_ollama, mock_post):
        mock_post.return_value = _deepseek_response()
        result = analyze_market(MARKET)
        self.assertIsNotNone(result)
        self.assertEqual(result["source"], "deepseek_api")
        self.assertAlmostEqual(result["inference_cost"], EXPECTED_COST_FULL, places=10)

    @patch.object(ai_analyzer, "_call_ollama", return_value=VALID_ANALYSIS_JSON)
    def test_ollama_path_costs_zero(self, mock_ollama):
        result = analyze_market(MARKET)
        self.assertIsNotNone(result)
        self.assertEqual(result["source"], "ollama")
        self.assertEqual(result["inference_cost"], 0.0)

    @patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"})
    @patch("requests.post")
    @patch.object(ai_analyzer, "_call_ollama", return_value=None)
    def test_deepseek_without_usage_omits_field(self, mock_ollama, mock_post):
        # If the API reports no usage we cannot price the call — the field
        # must be ABSENT (not 0.0) so the scorekeeper falls back to its
        # flat per-call estimate instead of counting the trade as free.
        mock_post.return_value = _deepseek_response(usage=None)
        result = analyze_market(MARKET)
        self.assertIsNotNone(result)
        self.assertEqual(result["source"], "deepseek_api")
        self.assertNotIn("inference_cost", result)

    @patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"})
    @patch("requests.post")
    @patch.object(ai_analyzer, "_call_ollama", return_value=None)
    def test_cost_logged_to_llm_calls_jsonl(self, mock_ollama, mock_post):
        mock_post.return_value = _deepseek_response()
        analyze_market(MARKET)
        log_path = Path(self._tmpdir.name) / "llm_calls.jsonl"
        self.assertTrue(log_path.exists())
        record = json.loads(log_path.read_text().strip().splitlines()[-1])
        self.assertEqual(record["usage"], USAGE_FULL)
        self.assertAlmostEqual(
            record["inference_cost_usd"], EXPECTED_COST_FULL, places=10
        )


class TestPlacePaperBetPersistsInferenceCost(unittest.TestCase):
    """The trade record in paper_trading_state.json carries the field."""

    def _make_trader(self):
        # Same isolation pattern as tests/test_resolution_types.py: bypass
        # __init__ (which loads the real state file) and stub save_state.
        with patch.object(PaperTrader, "__init__", lambda self_: None):
            trader = PaperTrader()
        trader.initial_balance = 1000.0
        trader.balance = 1000.0
        trader.positions = {}
        trader.trade_history = []
        trader.capital_epochs = []
        trader.save_state = lambda: None
        return trader

    def test_inference_cost_stored_on_trade(self):
        trader = self._make_trader()
        ok = trader.place_paper_bet(
            market_id="mkt123",
            outcome="YES",
            amount=5,
            probability=0.4,
            ai_status="agree",
            inference_cost=0.000158,
        )
        self.assertTrue(ok)
        self.assertEqual(trader.trade_history[-1]["inference_cost"], 0.000158)

    def test_omitted_inference_cost_is_none_backward_compatible(self):
        # Existing call sites (scripts/, swaps, demos) don't pass the new
        # argument — the trade must still be placed, with a None cost that
        # downstream consumers treat as "unknown, use estimate".
        trader = self._make_trader()
        ok = trader.place_paper_bet(
            market_id="mkt456",
            outcome="NO",
            amount=5,
            probability=0.6,
        )
        self.assertTrue(ok)
        trade = trader.trade_history[-1]
        self.assertIn("inference_cost", trade)
        self.assertIsNone(trade["inference_cost"])

    def test_zero_cost_survives_roundtrip_distinct_from_none(self):
        # 0.0 (free local analysis) must not collapse into None (unknown).
        trader = self._make_trader()
        trader.place_paper_bet(
            market_id="mkt789",
            outcome="YES",
            amount=5,
            probability=0.5,
            inference_cost=0.0,
        )
        serialized = json.loads(json.dumps(trader.trade_history[-1]))
        self.assertEqual(serialized["inference_cost"], 0.0)
        self.assertIsNotNone(serialized["inference_cost"])


if __name__ == "__main__":
    unittest.main()
