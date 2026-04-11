#!/usr/bin/env python3
"""
Op3 — Strategy observability regression tests.

Covers the counter plumbing added in auto_research.py + auto_trader.py:
  - counters are well-formed (all expected keys present)
  - rejection reasons map correctly (ai_veto / low_confidence / existing_open /
    max_positions / category_cap / liquidity)
  - print/append helpers don't crash on empty or populated counters

These tests deliberately DO NOT exercise the full research pipeline. The goal
is to lock in the diagnostic shape of the counters so a future refactor can't
silently hide the mono-strategy issue the live bot is showing right now.
"""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from automation.auto_research import (
    _new_research_counters,
    _finalize_research_counters,
    _print_research_counters,
    _append_research_counters,
)
from automation.auto_trader import (
    AutoTrader,
    _new_trader_counters,
    _print_trader_counters,
    _append_trader_counters,
)


# ── Research counters shape ───────────────────────────────────────────────────

class TestResearchCountersShape(unittest.TestCase):
    """Op3 — _new_research_counters must be complete and well-formed."""

    def setUp(self):
        self.c = _new_research_counters()

    def test_top_level_keys_present(self):
        expected = {
            "timestamp", "markets_fetched",
            "skipped_resolved", "skipped_low_liquidity", "skipped_stale",
            "raw_fires", "dedup_winners",
            "thin_market_confirmations", "no_active_signals", "tied_votes_dropped",
            "ai_eligible", "ai_cooled_down", "ai_analyzed",
            "ai_agree", "ai_disagree", "ai_skip", "ai_no_result",
            "final_recommendations", "final_by_strategy_mix", "final_by_category",
        }
        self.assertEqual(set(self.c.keys()), expected)

    def test_raw_fires_has_all_strategies(self):
        """Every directional strategy the bot runs must have a counter slot."""
        expected = {
            "probability_direction", "mean_reversion", "probability_bias",
            "creator_disagreement", "thin_market",
        }
        self.assertEqual(set(self.c["raw_fires"].keys()), expected)
        for v in self.c["raw_fires"].values():
            self.assertEqual(v, 0)

    def test_dedup_winners_has_all_families(self):
        """Family-dedup counters must cover every family the research loop uses."""
        self.assertEqual(
            set(self.c["dedup_winners"].keys()),
            {"momentum", "contrarian", "fundamental"},
        )

    def test_counters_default_to_zero(self):
        for k in ("markets_fetched", "skipped_resolved", "skipped_stale",
                  "ai_analyzed", "ai_agree", "ai_disagree",
                  "thin_market_confirmations", "tied_votes_dropped"):
            self.assertEqual(self.c[k], 0)

    def test_final_mix_empty_by_default(self):
        self.assertEqual(self.c["final_by_strategy_mix"], {})
        self.assertEqual(self.c["final_by_category"], {})


class TestFinalizeResearchCounters(unittest.TestCase):
    """_finalize_research_counters populates the 'final_*' breakdowns."""

    def _rec(self, market_id, strategies, category="other"):
        return {
            "market_id": market_id,
            "strategies": strategies,
            "category": category,
        }

    def test_groups_by_strategy_signature(self):
        c = _new_research_counters()
        recs = [
            self._rec("a", ["probability_direction"]),
            self._rec("b", ["probability_direction"]),
            self._rec("c", ["mean_reversion"]),
        ]
        _finalize_research_counters(c, recs)
        self.assertEqual(c["final_recommendations"], 3)
        self.assertEqual(c["final_by_strategy_mix"]["probability_direction"], 2)
        self.assertEqual(c["final_by_strategy_mix"]["mean_reversion"], 1)

    def test_sorts_strategies_for_stable_grouping(self):
        """mix signature is sorted so {A,B} == {B,A}."""
        c = _new_research_counters()
        recs = [
            self._rec("a", ["mean_reversion", "probability_bias"]),
            self._rec("b", ["probability_bias", "mean_reversion"]),
        ]
        _finalize_research_counters(c, recs)
        # Both recs should land in the same bucket
        self.assertEqual(len(c["final_by_strategy_mix"]), 1)
        # And that bucket should hold both
        n = next(iter(c["final_by_strategy_mix"].values()))
        self.assertEqual(n, 2)

    def test_excludes_ai_analysis_from_mix(self):
        """'ai_analysis' is a post-hoc tag, not a directional signal."""
        c = _new_research_counters()
        recs = [
            self._rec("a", ["probability_direction", "ai_analysis"]),
            self._rec("b", ["probability_direction"]),
        ]
        _finalize_research_counters(c, recs)
        # Both should count as the same mix
        self.assertEqual(c["final_by_strategy_mix"]["probability_direction"], 2)

    def test_empty_strategies_land_in_none_bucket(self):
        c = _new_research_counters()
        recs = [self._rec("a", [])]
        _finalize_research_counters(c, recs)
        self.assertEqual(c["final_by_strategy_mix"]["(none)"], 1)

    def test_category_breakdown(self):
        c = _new_research_counters()
        recs = [
            self._rec("a", ["probability_direction"], category="sports"),
            self._rec("b", ["probability_direction"], category="sports"),
            self._rec("c", ["probability_direction"], category="politics"),
        ]
        _finalize_research_counters(c, recs)
        self.assertEqual(c["final_by_category"]["sports"], 2)
        self.assertEqual(c["final_by_category"]["politics"], 1)


# ── Trader counters shape ────────────────────────────────────────────────────

class TestTraderCountersShape(unittest.TestCase):
    """Op3 — _new_trader_counters must cover every rejection reason."""

    def setUp(self):
        self.c = _new_trader_counters()

    def test_top_level_keys_present(self):
        expected = {
            "timestamp", "recommendations_loaded",
            "rejected", "passed_filter",
            "top_ev_at_exec", "trades_executed",
        }
        self.assertEqual(set(self.c.keys()), expected)

    def test_rejection_reasons_present(self):
        """Every gate in _trade_rejection_reason() must have a counter slot."""
        expected = {
            "ai_veto", "low_confidence", "existing_open", "max_positions",
            "category_cap", "liquidity", "kelly_no_edge", "size_too_small",
            "market_unverifiable",
        }
        self.assertEqual(set(self.c["rejected"].keys()), expected)
        for v in self.c["rejected"].values():
            self.assertEqual(v, 0)


# ── _trade_rejection_reason wiring ───────────────────────────────────────────

class TestTradeRejectionReason(unittest.TestCase):
    """
    Every rejection path should map to the string that matches its
    counter key.  This test locks in that mapping so renaming one side
    without the other is a loud failure.
    """

    def _make_trader_with_empty_book(self) -> AutoTrader:
        with patch("automation.auto_trader.PaperTrader") as MockTrader:
            mock_instance = MagicMock()
            mock_instance.positions = {}   # no open positions
            mock_instance.balance = 1000.0
            MockTrader.return_value = mock_instance
            trader = AutoTrader()
        return trader

    def _make_rec(self, **overrides):
        base = {
            "market_id": "mkt_test",
            "question": "Will it rain tomorrow?",
            "recommendation": "YES",
            "confidence": 0.75,
            "probability": 0.50,
            "liquidity": 500,
            "category": "other",
            "ai_recommendation": "YES",
            "ai_returned_skip": False,
            "strategies": ["probability_direction"],
        }
        base.update(overrides)
        return base

    def test_none_on_pass(self):
        trader = self._make_trader_with_empty_book()
        reason = trader._trade_rejection_reason("mkt_test", self._make_rec())
        self.assertIsNone(reason)

    def test_ai_veto_reason(self):
        trader = self._make_trader_with_empty_book()
        reason = trader._trade_rejection_reason(
            "mkt_test",
            self._make_rec(ai_returned_skip=True, ai_recommendation="SKIP"),
        )
        self.assertEqual(reason, "ai_veto")

    def test_low_confidence_reason(self):
        trader = self._make_trader_with_empty_book()
        reason = trader._trade_rejection_reason(
            "mkt_test",
            self._make_rec(confidence=0.50),
        )
        self.assertEqual(reason, "low_confidence")

    def test_existing_open_reason(self):
        trader = self._make_trader_with_empty_book()
        trader.trader.positions = {"mkt_test": [{"status": "OPEN"}]}
        reason = trader._trade_rejection_reason("mkt_test", self._make_rec())
        self.assertEqual(reason, "existing_open")

    def test_max_positions_reason(self):
        trader = self._make_trader_with_empty_book()
        # Fill the book with max_positions OPEN positions on other markets
        trader.trader.positions = {
            f"other_{i}": [{"status": "OPEN", "category": "other", "question": "q"}]
            for i in range(trader.max_positions)
        }
        reason = trader._trade_rejection_reason("mkt_test", self._make_rec())
        self.assertEqual(reason, "max_positions")

    def test_liquidity_reason(self):
        trader = self._make_trader_with_empty_book()
        reason = trader._trade_rejection_reason(
            "mkt_test",
            self._make_rec(liquidity=50),   # below MIN_LIQUIDITY=200
        )
        self.assertEqual(reason, "liquidity")

    def test_should_trade_market_bool_wrapper_still_works(self):
        """Tests from test_strategies.py still call should_trade_market(...) as a bool."""
        trader = self._make_trader_with_empty_book()
        self.assertTrue(trader.should_trade_market("mkt_test", self._make_rec()))
        self.assertFalse(
            trader.should_trade_market(
                "mkt_test",
                self._make_rec(confidence=0.50),
            )
        )


# ── Print / append helpers don't crash ───────────────────────────────────────

class TestCounterEmitHelpers(unittest.TestCase):
    """
    _print_research_counters / _print_trader_counters must be robust to
    empty counters (first run) and populated counters (steady state). The
    cron log is the most important observability surface so a crash here
    would blind us at exactly the wrong time.
    """

    def test_print_research_counters_empty_does_not_crash(self):
        _print_research_counters(_new_research_counters())

    def test_print_research_counters_populated(self):
        c = _new_research_counters()
        c["markets_fetched"] = 50
        c["raw_fires"]["probability_direction"] = 9
        c["dedup_winners"]["momentum"] = 9
        c["ai_analyzed"] = 3
        c["ai_agree"] = 2
        c["final_recommendations"] = 9
        c["final_by_strategy_mix"] = {"probability_direction": 9}
        c["final_by_category"] = {"other": 7, "politics": 2}
        _print_research_counters(c)

    def test_print_trader_counters_empty_does_not_crash(self):
        _print_trader_counters(_new_trader_counters())

    def test_print_trader_counters_populated(self):
        c = _new_trader_counters()
        c["recommendations_loaded"] = 9
        c["rejected"]["max_positions"] = 8
        c["rejected"]["liquidity"] = 1
        c["top_ev_at_exec"] = [("mkt_a", 0.82), ("mkt_b", 0.41)]
        _print_trader_counters(c)

    def test_append_research_counters_writes_jsonl(self):
        """Counters should survive a round-trip through JSON."""
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "research_counters.jsonl")
            with patch("automation.auto_research._RESEARCH_COUNTERS_PATH", path):
                c = _new_research_counters()
                c["markets_fetched"] = 42
                _append_research_counters(c)
                _append_research_counters(c)
            with open(path) as f:
                lines = [json.loads(ln) for ln in f if ln.strip()]
            self.assertEqual(len(lines), 2)
            self.assertEqual(lines[0]["markets_fetched"], 42)

    def test_append_trader_counters_writes_jsonl(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "trader_counters.jsonl")
            with patch("automation.auto_trader._TRADER_COUNTERS_PATH", path):
                c = _new_trader_counters()
                c["trades_executed"] = 2
                _append_trader_counters(c)
            with open(path) as f:
                lines = [json.loads(ln) for ln in f if ln.strip()]
            self.assertEqual(len(lines), 1)
            self.assertEqual(lines[0]["trades_executed"], 2)


if __name__ == "__main__":
    unittest.main()
