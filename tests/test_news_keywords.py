#!/usr/bin/env python3
"""
Tests for Ollama-powered keyword extraction in news_fetcher.py.

Covers:
  - _extract_keywords_ollama response parsing (mocked Ollama)
  - _extract_keywords_regex fallback behaviour
  - extract_keywords() fallback chain (Ollama → regex)
  - Stopword filtering in both paths
"""

import json
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from manifold_bot.news_fetcher import (
    extract_keywords,
    _extract_keywords_regex,
    _extract_keywords_ollama,
)


# ── _extract_keywords_regex ──────────────────────────────────────────────────

class TestRegexKeywords(unittest.TestCase):

    def test_strips_stopwords_and_digits(self):
        result = _extract_keywords_regex("Will Bitcoin hit $150k by December 2026?")
        self.assertEqual(result, "bitcoin")

    def test_takes_first_two(self):
        result = _extract_keywords_regex("Will SpaceX launch Starship successfully?")
        # 'launch' is not a stopword, so it comes before 'starship'
        self.assertEqual(result, "spacex launch")

    def test_strips_generic_verbs(self):
        """'hit', 'win', 'release' are in the extended stopword list."""
        result = _extract_keywords_regex("Will someone release something?")
        self.assertEqual(result, "someone something")

    def test_fallback_on_empty(self):
        """If no keywords survive, use the truncated question."""
        result = _extract_keywords_regex("Will it be?")
        # All words are stopwords or < 3 chars; falls back to question[:50]
        self.assertTrue(len(result) > 0)

    def test_strips_numeric_tokens(self):
        result = _extract_keywords_regex("Above 98 dollars by Q3 2026?")
        # '98', '2026', 'q3' all filtered; 'dollars' survives
        self.assertNotIn("98", result)
        self.assertNotIn("2026", result)


# ── _extract_keywords_ollama (mocked) ────────────────────────────────────────

def _mock_ollama_response(text):
    """Build a mock urllib response that returns the given text as Ollama output."""
    resp_data = json.dumps({"response": text}).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.read.return_value = resp_data
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


class TestOllamaKeywords(unittest.TestCase):

    @patch("manifold_bot.news_fetcher.urllib.request.urlopen")
    def test_parses_clean_response(self, mock_urlopen):
        mock_urlopen.return_value = _mock_ollama_response("bitcoin price forecast")
        result = _extract_keywords_ollama("Will Bitcoin hit $150k?")
        self.assertEqual(result, "bitcoin price forecast")

    @patch("manifold_bot.news_fetcher.urllib.request.urlopen")
    def test_strips_punctuation_from_response(self, mock_urlopen):
        mock_urlopen.return_value = _mock_ollama_response('"James Bond" casting news.')
        result = _extract_keywords_ollama("Next James Bond?")
        self.assertIn("james", result)
        self.assertIn("bond", result)
        self.assertNotIn('"', result)

    @patch("manifold_bot.news_fetcher.urllib.request.urlopen")
    def test_limits_to_3_tokens(self, mock_urlopen):
        mock_urlopen.return_value = _mock_ollama_response("one two three four five")
        result = _extract_keywords_ollama("test question")
        words = result.split()
        self.assertLessEqual(len(words), 3)

    @patch("manifold_bot.news_fetcher.urllib.request.urlopen")
    def test_strips_stopwords_from_ollama_output(self, mock_urlopen):
        mock_urlopen.return_value = _mock_ollama_response("the bitcoin will hit price")
        result = _extract_keywords_ollama("Will Bitcoin hit $150k?")
        self.assertNotIn("the", result.split())
        self.assertNotIn("will", result.split())
        self.assertNotIn("hit", result.split())

    @patch("manifold_bot.news_fetcher.urllib.request.urlopen")
    def test_empty_response_returns_empty(self, mock_urlopen):
        mock_urlopen.return_value = _mock_ollama_response("")
        result = _extract_keywords_ollama("test")
        self.assertEqual(result, "")

    @patch("manifold_bot.news_fetcher.urllib.request.urlopen")
    def test_all_stopwords_response_returns_empty(self, mock_urlopen):
        mock_urlopen.return_value = _mock_ollama_response("the will be to")
        result = _extract_keywords_ollama("test")
        self.assertEqual(result, "")

    @patch("manifold_bot.news_fetcher.urllib.request.urlopen")
    def test_timeout_returns_empty(self, mock_urlopen):
        mock_urlopen.side_effect = TimeoutError("timed out")
        result = _extract_keywords_ollama("test")
        self.assertEqual(result, "")

    @patch("manifold_bot.news_fetcher.urllib.request.urlopen")
    def test_connection_error_returns_empty(self, mock_urlopen):
        mock_urlopen.side_effect = ConnectionError("refused")
        result = _extract_keywords_ollama("test")
        self.assertEqual(result, "")


# ── extract_keywords() fallback chain ────────────────────────────────────────

class TestKeywordFallbackChain(unittest.TestCase):

    @patch("manifold_bot.news_fetcher._extract_keywords_ollama")
    def test_uses_ollama_when_available(self, mock_ollama):
        mock_ollama.return_value = "bitcoin forecast"
        result = extract_keywords("Will Bitcoin hit $150k?")
        self.assertEqual(result, "bitcoin forecast")
        mock_ollama.assert_called_once()

    @patch("manifold_bot.news_fetcher._extract_keywords_ollama")
    def test_falls_back_to_regex_when_ollama_fails(self, mock_ollama):
        mock_ollama.return_value = ""  # Ollama returned nothing
        result = extract_keywords("Will Bitcoin hit $150k?")
        # Should get the regex result
        self.assertEqual(result, "bitcoin")

    @patch("manifold_bot.news_fetcher._extract_keywords_ollama")
    def test_falls_back_to_regex_on_ollama_returning_none(self, mock_ollama):
        """If Ollama returns something falsy (None), regex fallback kicks in."""
        mock_ollama.return_value = None
        result = extract_keywords("Will SpaceX launch Starship?")
        # Should get regex result, not crash
        self.assertTrue(len(result) > 0)
        self.assertIn("spacex", result)


if __name__ == "__main__":
    unittest.main()
