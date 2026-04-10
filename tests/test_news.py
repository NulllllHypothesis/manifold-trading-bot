"""
Tests for P3 — News Fetcher + news_strategy.

Covers:
  - manifold_bot/news_fetcher.py  (extract_keywords, fetch_headlines caching/no-key)
  - manifold_bot/strategies.py    (news_strategy direction, news_strategy_confidence)
  - manifold_bot/ai_analyzer.py   (_build_news_note, batch_analyze news_by_id pass-through)
"""

import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from manifold_bot.news_fetcher import extract_keywords, fetch_headlines
from manifold_bot.strategies import TradingStrategies
from manifold_bot.ai_analyzer import _build_news_note


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _headline(title, age_hours=12.0):
    return {"title": title, "source": "Reuters", "published_at": "2026-04-10T00:00:00Z", "age_hours": age_hours}


def _market(question="Will this resolve YES?", probability=0.5):
    return {"id": "abc", "question": question, "probability": probability}


# ─────────────────────────────────────────────────────────────────────────────
# extract_keywords
# ─────────────────────────────────────────────────────────────────────────────

class TestExtractKeywords(unittest.TestCase):

    def test_removes_stopwords(self):
        kw = extract_keywords("Will Bitcoin reach $100k by end of 2026?")
        self.assertIn("bitcoin", kw)
        self.assertNotIn("will", kw)
        self.assertNotIn("2026", kw)
        self.assertNotIn("end", kw)

    def test_removes_year_tokens(self):
        kw = extract_keywords("Will Trump win the 2028 presidential election?")
        self.assertNotIn("2028", kw)
        self.assertIn("trump", kw)

    def test_max_four_keywords(self):
        kw = extract_keywords("Will the Federal Reserve raise interest rates in Q2 2026?")
        self.assertLessEqual(len(kw.split()), 4)

    def test_empty_question_fallback(self):
        # Should not raise; returns something
        result = extract_keywords("")
        self.assertIsInstance(result, str)

    def test_all_stopwords_fallback(self):
        # Question made entirely of stopwords → falls back to truncated original
        result = extract_keywords("will it be")
        self.assertIsInstance(result, str)
        self.assertGreater(len(result), 0)

    def test_short_tokens_removed(self):
        # Tokens < 3 chars are dropped
        kw = extract_keywords("Will AI be good?")
        # "be" (2 chars) removed; "good" (4 chars) kept
        self.assertNotIn("be", kw.split())

    def test_meaningful_keywords_kept(self):
        kw = extract_keywords("Will Elon Musk leave Tesla by end of 2026?")
        for word in ["elon", "musk", "leave", "tesla"]:
            if word in kw:
                break
        else:
            self.fail(f"Expected at least one of elon/musk/leave/tesla in '{kw}'")


# ─────────────────────────────────────────────────────────────────────────────
# fetch_headlines
# ─────────────────────────────────────────────────────────────────────────────

class TestFetchHeadlines(unittest.TestCase):

    def test_returns_empty_when_no_api_key(self):
        with patch("manifold_bot.news_fetcher.NEWS_API_KEY", ""):
            result = fetch_headlines("mktid", "Will Bitcoin rise?")
        self.assertEqual(result, [])

    def test_returns_cached_result_without_api_call(self):
        """Cache hit should return stored value without touching the network."""
        cached = [_headline("Bitcoin surges to new ATH")]
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        cache_key = f"mkt_cache_test:{today}"

        with patch("manifold_bot.news_fetcher.NEWS_API_KEY", "test-key"), \
             patch("manifold_bot.news_fetcher._load_cache", return_value={cache_key: cached}), \
             patch("manifold_bot.news_fetcher._save_cache") as mock_save, \
             patch("urllib.request.urlopen") as mock_http:
            result = fetch_headlines("mkt_cache_test", "Will Bitcoin rise?")

        self.assertEqual(result, cached)
        mock_http.assert_not_called()
        mock_save.assert_not_called()

    def test_filters_removed_articles(self):
        """Articles with title '[Removed]' must be excluded."""
        api_response = {
            "status": "ok",
            "articles": [
                {"title": "[Removed]", "source": {"name": "X"}, "publishedAt": "2026-04-10T00:00:00Z"},
                {"title": "Real headline", "source": {"name": "Y"}, "publishedAt": "2026-04-10T00:00:00Z"},
            ]
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(api_response).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("manifold_bot.news_fetcher.NEWS_API_KEY", "test-key"), \
             patch("manifold_bot.news_fetcher._load_cache", return_value={}), \
             patch("manifold_bot.news_fetcher._save_cache"), \
             patch("urllib.request.urlopen", return_value=mock_resp):
            result = fetch_headlines("mkt1", "Will something happen?")

        titles = [h["title"] for h in result]
        self.assertNotIn("[Removed]", titles)
        self.assertIn("Real headline", titles)

    def test_returns_empty_on_api_error_status(self):
        api_response = {"status": "error", "message": "apiKeyInvalid"}
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(api_response).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("manifold_bot.news_fetcher.NEWS_API_KEY", "bad-key"), \
             patch("manifold_bot.news_fetcher._load_cache", return_value={}), \
             patch("manifold_bot.news_fetcher._save_cache"), \
             patch("urllib.request.urlopen", return_value=mock_resp):
            result = fetch_headlines("mkt2", "Will it rain?")

        self.assertEqual(result, [])

    def test_returns_empty_on_network_exception(self):
        with patch("manifold_bot.news_fetcher.NEWS_API_KEY", "test-key"), \
             patch("manifold_bot.news_fetcher._load_cache", return_value={}), \
             patch("manifold_bot.news_fetcher._save_cache"), \
             patch("urllib.request.urlopen", side_effect=OSError("timeout")):
            result = fetch_headlines("mkt3", "Will the economy recover?")

        self.assertEqual(result, [])

    def test_result_structure(self):
        """Returned dicts must have required keys."""
        api_response = {
            "status": "ok",
            "articles": [{
                "title": "Test headline",
                "source": {"name": "BBC"},
                "publishedAt": "2026-04-10T06:00:00Z",
            }]
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(api_response).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("manifold_bot.news_fetcher.NEWS_API_KEY", "test-key"), \
             patch("manifold_bot.news_fetcher._load_cache", return_value={}), \
             patch("manifold_bot.news_fetcher._save_cache"), \
             patch("urllib.request.urlopen", return_value=mock_resp):
            result = fetch_headlines("mkt4", "Will this happen?")

        self.assertEqual(len(result), 1)
        h = result[0]
        self.assertIn("title", h)
        self.assertIn("source", h)
        self.assertIn("published_at", h)
        self.assertIn("age_hours", h)
        self.assertEqual(h["title"], "Test headline")
        self.assertEqual(h["source"], "BBC")


# ─────────────────────────────────────────────────────────────────────────────
# news_strategy direction
# ─────────────────────────────────────────────────────────────────────────────

class TestNewsStrategyDirection(unittest.TestCase):

    def test_returns_none_on_empty_headlines(self):
        self.assertIsNone(TradingStrategies.news_strategy(_market(), []))

    def test_yes_signal_on_positive_headlines(self):
        headlines = [
            _headline("Bitcoin wins approval from SEC"),
            _headline("Crypto market achieves record highs"),
            _headline("ETF confirmed for launch next month"),
        ]
        result = TradingStrategies.news_strategy(_market(), headlines)
        self.assertEqual(result, "YES")

    def test_no_signal_on_negative_headlines(self):
        headlines = [
            _headline("Bitcoin fails to break resistance"),
            _headline("SEC rejects crypto ETF application"),
            _headline("Market drops amid regulatory fears"),
        ]
        result = TradingStrategies.news_strategy(_market(), headlines)
        self.assertEqual(result, "NO")

    def test_none_on_mixed_equal_headlines(self):
        headlines = [
            _headline("Bitcoin wins approval"),   # YES lean
            _headline("Crypto market drops"),     # NO lean
        ]
        result = TradingStrategies.news_strategy(_market(), headlines)
        self.assertIsNone(result)

    def test_none_on_neutral_headlines(self):
        headlines = [
            _headline("Analysts watching Bitcoin closely"),
            _headline("Market participants await Fed decision"),
        ]
        result = TradingStrategies.news_strategy(_market(), headlines)
        self.assertIsNone(result)

    def test_single_strong_yes_headline(self):
        headlines = [_headline("Deal confirmed between parties")]
        result = TradingStrategies.news_strategy(_market(), headlines)
        self.assertEqual(result, "YES")

    def test_single_strong_no_headline(self):
        headlines = [_headline("Proposal rejected by committee")]
        result = TradingStrategies.news_strategy(_market(), headlines)
        self.assertEqual(result, "NO")

    def test_majority_wins(self):
        # 3 NO-leaning, 1 YES-leaning → NO
        headlines = [
            _headline("Project fails review"),
            _headline("Funding canceled abruptly"),
            _headline("Launch delayed indefinitely"),
            _headline("Small team achieves milestone"),  # YES
        ]
        result = TradingStrategies.news_strategy(_market(), headlines)
        self.assertEqual(result, "NO")


# ─────────────────────────────────────────────────────────────────────────────
# news_strategy_confidence
# ─────────────────────────────────────────────────────────────────────────────

class TestNewsStrategyConfidence(unittest.TestCase):

    def test_empty_returns_floor(self):
        self.assertEqual(TradingStrategies.news_strategy_confidence([]), 0.60)

    def test_fresh_under_24h(self):
        headlines = [_headline("Breaking news", age_hours=6.0)]
        self.assertEqual(TradingStrategies.news_strategy_confidence(headlines), 0.80)

    def test_recent_under_72h(self):
        headlines = [_headline("Recent news", age_hours=48.0)]
        self.assertEqual(TradingStrategies.news_strategy_confidence(headlines), 0.70)

    def test_old_over_72h(self):
        headlines = [_headline("Old news", age_hours=120.0)]
        self.assertEqual(TradingStrategies.news_strategy_confidence(headlines), 0.60)

    def test_uses_freshest_headline(self):
        headlines = [
            _headline("Old news", age_hours=100.0),
            _headline("Breaking news", age_hours=2.0),
        ]
        self.assertEqual(TradingStrategies.news_strategy_confidence(headlines), 0.80)

    def test_exactly_24h_boundary(self):
        # 24.0 is NOT < 24, falls into the < 72 bracket
        headlines = [_headline("Exactly 24h old", age_hours=24.0)]
        self.assertEqual(TradingStrategies.news_strategy_confidence(headlines), 0.70)

    def test_exactly_72h_boundary(self):
        # 72.0 is NOT < 72, falls into else → 0.60
        headlines = [_headline("Exactly 72h old", age_hours=72.0)]
        self.assertEqual(TradingStrategies.news_strategy_confidence(headlines), 0.60)


# ─────────────────────────────────────────────────────────────────────────────
# _build_news_note (AI prompt injection)
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildNewsNote(unittest.TestCase):

    def test_empty_returns_empty_string(self):
        self.assertEqual(_build_news_note([]), "")

    def test_none_equivalent(self):
        self.assertEqual(_build_news_note(None or []), "")

    def test_contains_headlines(self):
        note = _build_news_note(["Bitcoin wins approval", "ETF confirmed"])
        self.assertIn("Bitcoin wins approval", note)
        self.assertIn("ETF confirmed", note)

    def test_header_present(self):
        note = _build_news_note(["Some headline"])
        self.assertIn("Recent news", note)

    def test_caps_at_three_headlines(self):
        headlines = [f"Headline {i}" for i in range(10)]
        note = _build_news_note(headlines)
        # Only first 3 should appear
        self.assertIn("Headline 0", note)
        self.assertIn("Headline 2", note)
        self.assertNotIn("Headline 3", note)

    def test_ends_with_newlines_for_prompt_spacing(self):
        note = _build_news_note(["Test headline"])
        self.assertTrue(note.endswith("\n\n"))


# ─────────────────────────────────────────────────────────────────────────────
# news_by_id pass-through in batch_analyze
# ─────────────────────────────────────────────────────────────────────────────

class TestBatchAnalyzeNewsPassThrough(unittest.TestCase):

    def test_news_context_passed_to_analyze_market(self):
        """batch_analyze must forward news_by_id headlines to analyze_market."""
        market = {"id": "mkt_news", "question": "Will X happen?", "probability": 0.6}
        headlines = ["Breaking: X confirmed by officials"]

        captured_ctx = []

        def fake_analyze(mkt, news_context=None):
            captured_ctx.append(news_context)
            return {
                "recommendation": "YES",
                "confidence": 0.75,
                "estimated_true_probability": 0.75,
                "reasoning": "test",
                "risk_factors": "none",
                "source": "ollama",
                "market_id": "mkt_news",
            }

        from manifold_bot import ai_analyzer
        original = ai_analyzer.analyze_market
        ai_analyzer.analyze_market = fake_analyze
        try:
            results = ai_analyzer.batch_analyze(
                [market],
                max_markets=1,
                delay=0,
                per_market_timeout=10,
                news_by_id={"mkt_news": headlines},
            )
        finally:
            ai_analyzer.analyze_market = original

        self.assertIn("mkt_news", results)
        self.assertEqual(len(captured_ctx), 1)
        self.assertEqual(captured_ctx[0], headlines)

    def test_no_news_context_when_key_absent(self):
        """When news_by_id is None, analyze_market must receive news_context=None."""
        market = {"id": "mkt_no_news", "question": "Will Y happen?", "probability": 0.4}

        captured_ctx = []

        def fake_analyze(mkt, news_context=None):
            captured_ctx.append(news_context)
            return {
                "recommendation": "SKIP",
                "confidence": 0.0,
                "estimated_true_probability": 0.4,
                "reasoning": "test",
                "risk_factors": "none",
                "source": "ollama",
                "market_id": "mkt_no_news",
            }

        from manifold_bot import ai_analyzer
        original = ai_analyzer.analyze_market
        ai_analyzer.analyze_market = fake_analyze
        try:
            ai_analyzer.batch_analyze(
                [market],
                max_markets=1,
                delay=0,
                per_market_timeout=10,
                news_by_id=None,
            )
        finally:
            ai_analyzer.analyze_market = original

        self.assertEqual(len(captured_ctx), 1)
        self.assertIsNone(captured_ctx[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
