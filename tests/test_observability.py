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
            "skipped_resolved", "skipped_low_liquidity", "skipped_stale", "skipped_noise",
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

    def test_ai_status_skip_vetoes(self):
        """New ai_status='skip' is the authoritative veto signal."""
        trader = self._make_trader_with_empty_book()
        reason = trader._trade_rejection_reason(
            "mkt_test",
            self._make_rec(ai_status="skip", ai_recommendation="SKIP", ai_returned_skip=True),
        )
        self.assertEqual(reason, "ai_veto")

    def test_ai_status_no_result_does_not_veto(self):
        """AI failure (no_result) must NOT veto — falls back to stat-only trading."""
        trader = self._make_trader_with_empty_book()
        reason = trader._trade_rejection_reason(
            "mkt_test",
            self._make_rec(
                ai_status="no_result",
                ai_recommendation=None,
                ai_returned_skip=False,
            ),
        )
        self.assertIsNone(reason)

    def test_ai_status_not_run_does_not_veto(self):
        """Markets not sent to AI should trade on stat alone."""
        trader = self._make_trader_with_empty_book()
        reason = trader._trade_rejection_reason(
            "mkt_test",
            self._make_rec(
                ai_status="not_run",
                ai_recommendation=None,
                ai_returned_skip=False,
            ),
        )
        self.assertIsNone(reason)

    def test_ai_status_agree_does_not_veto(self):
        """AI agreement should let trade proceed."""
        trader = self._make_trader_with_empty_book()
        reason = trader._trade_rejection_reason(
            "mkt_test",
            self._make_rec(ai_status="agree", ai_recommendation="YES"),
        )
        self.assertIsNone(reason)

    def test_legacy_record_without_ai_status_still_vetoes_on_skip(self):
        """Backward compatibility: old records with ai_recommendation='SKIP' still veto."""
        trader = self._make_trader_with_empty_book()
        rec = self._make_rec(ai_recommendation="SKIP", ai_returned_skip=True)
        rec.pop("ai_status", None)
        reason = trader._trade_rejection_reason("mkt_test", rec)
        self.assertEqual(reason, "ai_veto")

    def test_legacy_record_ai_returned_skip_without_skip_rec_does_not_veto(self):
        """Legacy records where ai_returned_skip=True but ai_recommendation is None
        (the buggy no_result case) must NOT veto — trust ai_recommendation only when
        ai_status is absent."""
        trader = self._make_trader_with_empty_book()
        rec = self._make_rec(ai_returned_skip=True, ai_recommendation=None)
        rec.pop("ai_status", None)
        reason = trader._trade_rejection_reason("mkt_test", rec)
        self.assertIsNone(reason)

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


# ── Schema v2 invariant (zero-candidate AI branch fix) ──────────────────────
#
# The reviewer caught a real production bug on 2026-04-11: when every above-floor
# recommendation was on AI-timeout cooldown, candidate_markets was empty, the
# AI metadata init loop never ran, and save_research() wrote schema_version=1.
# That halts auto_trader for the rest of the cycle.
#
# Fix: initialize AI defaults on every rec BEFORE the `if candidate_markets:`
# branch, so a zero-candidate path still produces schema v2 output.
#
# These tests lock that invariant in so no future refactor can silently remove
# the default-init block.

from automation.auto_research import MarketResearcher


class TestSchemaV2Invariant(unittest.TestCase):
    """save_research() must write schema_version=2 whenever the pipeline
    finishes, regardless of whether the AI candidate pass actually ran."""

    def _make_researcher_with_tempfile(self) -> tuple:
        """Return (researcher, tmp_path) with .research_file pointed at a temp file."""
        with patch("automation.auto_research.PaperTrader"):
            researcher = MarketResearcher()
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        tmp.close()
        researcher.research_file = tmp.name
        return researcher, tmp.name

    def _read(self, path: str) -> dict:
        with open(path) as f:
            return json.load(f)

    def test_zero_candidate_branch_writes_schema_v2(self):
        """
        A rec that went through the new default-init block (but was never an
        AI candidate) must serialise as schema v2. This locks in the fix:
        ai_was_candidate must be present even on recs that never hit the AI.
        """
        researcher, path = self._make_researcher_with_tempfile()
        try:
            rec = {
                "market_id": "mkt_x",
                "question": "Will it rain?",
                "recommendation": "YES",
                "confidence": 0.65,
                "probability": 0.5,
                "strategies": ["probability_direction"],
                # The defaults Op4-fix initializes on every rec:
                "ai_was_candidate":  False,
                "ai_returned_skip":  False,
                "ai_recommendation": None,
                "ai_confidence":     0.0,
                "ai_reasoning":      "",
                "ai_source":         None,
            }
            researcher.save_research([rec])
            data = self._read(path)
            self.assertEqual(data["latest"]["schema_version"], 2)
        finally:
            os.unlink(path)

    def test_missing_ai_was_candidate_still_falls_back_to_v1(self):
        """
        Belt-and-braces: if for some reason a rec is missing ai_was_candidate
        entirely (e.g. a caller hand-builds a rec and forgets the defaults),
        save_research() must still write v1 so the trader halts rather than
        trading on unvalidated data. This is the original safety guard we
        don't want to weaken.
        """
        researcher, path = self._make_researcher_with_tempfile()
        try:
            rec = {
                "market_id": "mkt_y",
                "question": "Will it rain?",
                "recommendation": "YES",
                "confidence": 0.65,
                # Intentionally missing every AI field
            }
            researcher.save_research([rec])
            data = self._read(path)
            self.assertEqual(data["latest"]["schema_version"], 1)
        finally:
            os.unlink(path)

    def test_empty_recommendations_writes_v1(self):
        """No recs at all → v1 (nothing to trade on, trader should halt)."""
        researcher, path = self._make_researcher_with_tempfile()
        try:
            researcher.save_research([])
            data = self._read(path)
            self.assertEqual(data["latest"]["schema_version"], 1)
        finally:
            os.unlink(path)

    def test_mixed_recs_with_one_missing_ai_field_writes_v1(self):
        """
        If ANY rec is missing ai_was_candidate the writer must fall back to v1.
        This is the existing safety invariant — the new fix is supposed to
        make this case unreachable via the normal pipeline, not to relax it.
        """
        researcher, path = self._make_researcher_with_tempfile()
        try:
            recs = [
                {"market_id": "a", "ai_was_candidate": False},
                {"market_id": "b"},   # missing
            ]
            researcher.save_research(recs)
            data = self._read(path)
            self.assertEqual(data["latest"]["schema_version"], 1)
        finally:
            os.unlink(path)


class TestAnalyzeMarketsZeroCandidatePath(unittest.TestCase):
    """
    End-to-end guard for the zero-candidate path.

    This test stubs out the Manifold API, the snapshot writer, the weights
    loader, and the AI cooldown state so we can drive analyze_markets() to
    the exact failure mode the reviewer observed in production: every above-
    floor rec is on AI-timeout cooldown, so candidate_markets is empty, so
    without the Op4-fix default init the resulting recs would be missing
    ai_was_candidate and save_research() would write schema v1.
    """

    def _market(self, market_id: str, prob: float,
                question: str = "Will GPT-5 be released this year?") -> dict:
        # Uses an ai_tech question so probability_bias (which keys on category
        # aggregate bias) doesn't fire and create tied votes with
        # probability_direction. ai_tech has near-zero bias (0.003) so
        # probability_bias stays silent, leaving probability_direction as
        # the sole signal → no ties → recommendations are produced.
        import time
        now_ms = time.time() * 1000
        return {
            "id":                 market_id,
            "question":           question,
            "probability":        prob,
            "volume24Hours":      50.0,
            "uniqueBettorCount":  10,
            "totalLiquidity":     500,
            "isResolved":         False,
            "lastBetTime":        now_ms - 3_600_000,   # 1h ago
            "closeTime":          now_ms + 7 * 86_400_000,
        }

    def test_all_recs_get_ai_defaults_when_candidates_empty(self):
        from automation.auto_research import MarketResearcher

        # Three markets, all with strong directional signal (probability_direction
        # fires at prob >= 0.70). After family dedup and confidence scaling they
        # will all clear _STAT_BOOST_FLOOR = 0.65, so they will all land in
        # above_floor. Then we put every market on AI-timeout cooldown so the
        # filter strips them out and candidate_markets ends up empty.
        markets = [
            self._market("mkt_a", 0.80),
            self._market("mkt_b", 0.82),
            self._market("mkt_c", 0.85),
        ]
        cooldown = {
            "mkt_a": {"fails": 5, "last_fail_at": "2026-04-11T00:00:00+00:00"},
            "mkt_b": {"fails": 5, "last_fail_at": "2026-04-11T00:00:00+00:00"},
            "mkt_c": {"fails": 5, "last_fail_at": "2026-04-11T00:00:00+00:00"},
        }

        with patch("automation.auto_research.PaperTrader") as MockTrader, \
             patch("automation.auto_research.api_client") as mock_api, \
             patch("automation.auto_research._write_market_snapshots"), \
             patch("automation.auto_research._load_strategy_weights", return_value=({}, {})), \
             patch("automation.auto_research._load_ai_timeout_cooldown", return_value=cooldown), \
             patch("automation.auto_research._save_ai_timeout_cooldown"), \
             patch("automation.auto_research._is_in_ai_cooldown", return_value=True), \
             patch("automation.auto_research.batch_analyze") as mock_batch:

            mock_api.get_markets.return_value = markets
            mock_instance = MagicMock()
            mock_instance.positions = {}
            MockTrader.return_value = mock_instance

            researcher = MarketResearcher()
            recs = researcher.analyze_markets(limit=10)

            # batch_analyze must NOT have been called because every candidate
            # was filtered out as cooled-down.
            mock_batch.assert_not_called()

            # This is the whole point of the fix: every rec must still have
            # the AI metadata defaults set, so save_research() writes v2.
            self.assertGreater(len(recs), 0, "Expected at least one recommendation")
            for rec in recs:
                self.assertIn("ai_was_candidate",  rec, f"rec {rec.get('market_id')} missing ai_was_candidate")
                self.assertIn("ai_returned_skip",  rec)
                self.assertIn("ai_recommendation", rec)
                self.assertIn("ai_confidence",     rec)
                self.assertIn("ai_source",         rec)
                # All defaults because no AI actually ran for them
                self.assertFalse(rec["ai_was_candidate"])
                self.assertFalse(rec["ai_returned_skip"])
                self.assertIsNone(rec["ai_recommendation"])

    def test_save_research_on_zero_candidate_recs_writes_v2(self):
        """
        Full round-trip: analyze_markets() produces recs on the zero-candidate
        path, we hand them to save_research(), the JSON file on disk must
        report schema_version=2 (not 1).
        """
        from automation.auto_research import MarketResearcher

        markets = [
            self._market("mkt_a", 0.80),
            self._market("mkt_b", 0.82),
        ]
        cooldown = {
            "mkt_a": {"fails": 5, "last_fail_at": "2026-04-11T00:00:00+00:00"},
            "mkt_b": {"fails": 5, "last_fail_at": "2026-04-11T00:00:00+00:00"},
        }

        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        tmp.close()
        try:
            with patch("automation.auto_research.PaperTrader") as MockTrader, \
                 patch("automation.auto_research.api_client") as mock_api, \
                 patch("automation.auto_research._write_market_snapshots"), \
                 patch("automation.auto_research._load_strategy_weights", return_value=({}, {})), \
                 patch("automation.auto_research._load_ai_timeout_cooldown", return_value=cooldown), \
                 patch("automation.auto_research._save_ai_timeout_cooldown"), \
                 patch("automation.auto_research._is_in_ai_cooldown", return_value=True):

                mock_api.get_markets.return_value = markets
                mock_instance = MagicMock()
                mock_instance.positions = {}
                MockTrader.return_value = mock_instance

                researcher = MarketResearcher()
                researcher.research_file = tmp.name
                recs = researcher.analyze_markets(limit=10)
                researcher.save_research(recs)

            with open(tmp.name) as f:
                data = json.load(f)

            self.assertEqual(data["latest"]["schema_version"], 2,
                             "zero-candidate path must still write schema_version=2")
        finally:
            os.unlink(tmp.name)


class TestStatDerivedProbability(unittest.TestCase):
    """
    Stat-derived probability fallback for estimated_ev.

    Used when AI doesn't provide ai_estimated_probability — ensures EV is
    computed on every trade, not just AI-analyzed ones. Otherwise the EV
    calibration feedback loop has no data to work with.
    """

    def test_yes_direction_maps_confidence_to_p_yes(self):
        from automation.auto_trader import _stat_derived_probability
        p = _stat_derived_probability(confidence=0.70, recommendation_direction='YES', current_prob=0.50)
        self.assertEqual(p, 0.70)

    def test_no_direction_maps_confidence_to_one_minus_p(self):
        from automation.auto_trader import _stat_derived_probability
        p = _stat_derived_probability(confidence=0.70, recommendation_direction='NO', current_prob=0.50)
        self.assertAlmostEqual(p, 0.30, places=6)  # 1 - 0.70

    def test_extreme_confidence_clamped(self):
        from automation.auto_trader import _stat_derived_probability
        # confidence > 0.99 would produce p > 0.99 and then 1-p = 0 for NO
        # — but confidence is always < 1 so we clamp internally
        p = _stat_derived_probability(confidence=0.999, recommendation_direction='YES', current_prob=0.5)
        self.assertLessEqual(p, 0.99)
        self.assertGreater(p, 0.95)

    def test_low_confidence_still_produces_estimate(self):
        from automation.auto_trader import _stat_derived_probability
        p = _stat_derived_probability(confidence=0.55, recommendation_direction='YES', current_prob=0.50)
        self.assertEqual(p, 0.55)

    def test_invalid_confidence_returns_none(self):
        from automation.auto_trader import _stat_derived_probability
        self.assertIsNone(_stat_derived_probability(confidence=0.0, recommendation_direction='YES', current_prob=0.5))
        self.assertIsNone(_stat_derived_probability(confidence=1.0, recommendation_direction='YES', current_prob=0.5))
        self.assertIsNone(_stat_derived_probability(confidence=None, recommendation_direction='YES', current_prob=0.5))
        self.assertIsNone(_stat_derived_probability(confidence="0.65", recommendation_direction='YES', current_prob=0.5))

    def test_invalid_direction_returns_none(self):
        from automation.auto_trader import _stat_derived_probability
        self.assertIsNone(_stat_derived_probability(confidence=0.65, recommendation_direction='SKIP', current_prob=0.5))
        self.assertIsNone(_stat_derived_probability(confidence=0.65, recommendation_direction=None, current_prob=0.5))
        self.assertIsNone(_stat_derived_probability(confidence=0.65, recommendation_direction='', current_prob=0.5))


if __name__ == "__main__":
    unittest.main()
