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
            "skipped_unverifiable", "skipped_thin_other_momentum",
            "raw_fires", "dedup_winners",
            "thin_market_confirmations", "no_active_signals", "tied_votes_dropped",
            "ai_eligible", "ai_cooled_down", "ai_skipped_held", "ai_analyzed",
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
            "category_cap", "position_class_full", "liquidity", "kelly_no_edge",
            "size_too_small", "market_unverifiable", "negative_ev",
            "solo_momentum_low_res",
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
            # Phase 2-post-CEIUnpQL26: the trader's "vanity market" backstop
            # gate checks unique_bettors on the recommendation. A normal
            # market has plenty of bettors; this default reflects "healthy"
            # so other gate tests don't trip the new filter by accident.
            "unique_bettors": 20,
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

    def test_legacy_real_skip_record_still_vetoes(self):
        """Real legacy skip shape: ai_recommendation='SKIP', ai_returned_skip=True,
        ai_confidence=0.0, ai_reasoning='', ai_source=None. Must still veto."""
        trader = self._make_trader_with_empty_book()
        rec = self._make_rec(
            ai_recommendation="SKIP",
            ai_returned_skip=True,
            ai_confidence=0.0,
            ai_reasoning="",
            ai_source=None,
        )
        rec.pop("ai_status", None)
        reason = trader._trade_rejection_reason("mkt_test", rec)
        self.assertEqual(reason, "ai_veto")

    def test_legacy_no_result_record_also_vetoes_for_safety(self):
        """Real legacy no_result shape is IDENTICAL to real legacy skip shape:
        ai_recommendation='SKIP', ai_returned_skip=True, ai_confidence=0.0,
        ai_reasoning='', ai_source=None. They are indistinguishable in stored
        market_research.json files.

        We default to the SAFE choice: veto both. The next research run will
        rewrite the file with explicit ai_status fields, fixing future cycles.
        This test locks in the conservative legacy behavior."""
        trader = self._make_trader_with_empty_book()
        # Same shape as above — that's the whole point of the bug
        rec = self._make_rec(
            ai_recommendation="SKIP",
            ai_returned_skip=True,
            ai_confidence=0.0,
            ai_reasoning="",
            ai_source=None,
        )
        rec.pop("ai_status", None)
        reason = trader._trade_rejection_reason("mkt_test", rec)
        # Can't distinguish from real skip — veto is the safe default
        self.assertEqual(reason, "ai_veto")

    def test_new_no_result_record_does_not_veto(self):
        """After this PR, new producer writes ai_status='no_result' + ai_recommendation=None
        for AI failures. Trader must NOT veto these — they fall through to stat-only."""
        trader = self._make_trader_with_empty_book()
        reason = trader._trade_rejection_reason(
            "mkt_test",
            self._make_rec(
                ai_status="no_result",
                ai_recommendation=None,
                ai_returned_skip=False,
                ai_confidence=0.0,
            ),
        )
        self.assertIsNone(reason)

    def test_low_confidence_reason(self):
        trader = self._make_trader_with_empty_book()
        reason = trader._trade_rejection_reason(
            "mkt_test",
            self._make_rec(confidence=0.50),
        )
        self.assertEqual(reason, "low_confidence")

    # ── solo probability_direction in low-resolvability ──────────────────────

    def test_solo_momentum_low_res_blocked(self):
        """The empirically problematic combo: only-vote=probability_direction
        with resolvability=low. Both legs of the post-Apr-20 epoch losses
        matched this pattern."""
        trader = self._make_trader_with_empty_book()
        reason = trader._trade_rejection_reason(
            "mkt_test",
            self._make_rec(
                strategies=["probability_direction"],
                resolvability="low",
            ),
        )
        self.assertEqual(reason, "solo_momentum_low_res")

    def test_solo_momentum_high_res_passes(self):
        """Same solo signal in a high-resolvability market is fine — the
        market itself is predictable enough that single-vote momentum is
        a defensible gate."""
        trader = self._make_trader_with_empty_book()
        reason = trader._trade_rejection_reason(
            "mkt_test",
            self._make_rec(
                strategies=["probability_direction"],
                resolvability="high",
            ),
        )
        self.assertIsNone(reason)

    def test_solo_momentum_medium_res_passes(self):
        trader = self._make_trader_with_empty_book()
        reason = trader._trade_rejection_reason(
            "mkt_test",
            self._make_rec(
                strategies=["probability_direction"],
                resolvability="medium",
            ),
        )
        self.assertIsNone(reason)

    def test_multi_signal_low_res_passes(self):
        """Two independent voting signals override the gate — the second
        signal supplies the confirmation that solo momentum lacks."""
        trader = self._make_trader_with_empty_book()
        reason = trader._trade_rejection_reason(
            "mkt_test",
            self._make_rec(
                strategies=["probability_direction", "probability_bias"],
                resolvability="low",
            ),
        )
        self.assertIsNone(reason)

    def test_thin_market_does_not_count_as_extra_voter(self):
        """thin_market is a confirmation-only +0.03 boost, never an
        independent vote. ['probability_direction', 'thin_market'] is still
        effectively solo momentum and must be blocked in low-res."""
        trader = self._make_trader_with_empty_book()
        reason = trader._trade_rejection_reason(
            "mkt_test",
            self._make_rec(
                strategies=["probability_direction", "thin_market"],
                resolvability="low",
            ),
        )
        self.assertEqual(reason, "solo_momentum_low_res")

    def test_solo_probability_bias_low_res_currently_passes(self):
        """Scope guard: this PR only blocks solo probability_direction.
        Solo probability_bias in low-res is also empirically risky but is
        out of scope for this fix; locks in the narrow rule. Revisit after
        Monday's strategy weights have data."""
        trader = self._make_trader_with_empty_book()
        reason = trader._trade_rejection_reason(
            "mkt_test",
            self._make_rec(
                strategies=["probability_bias"],
                resolvability="low",
            ),
        )
        self.assertIsNone(reason)

    def test_missing_resolvability_does_not_trigger(self):
        """A recommendation without resolvability (legacy or pre-classify)
        falls through this gate — None is not 'low'."""
        trader = self._make_trader_with_empty_book()
        rec = self._make_rec(strategies=["probability_direction"])
        rec.pop('resolvability', None)
        reason = trader._trade_rejection_reason("mkt_test", rec)
        self.assertIsNone(reason)

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

    # ── Negative-EV gate (phase 3 prep) ──────────────────────────────────────

    def test_negative_ev_exec_rejected(self):
        # Live-book reproducer: "Do you like 8% odds?" was opened at
        # estimated_ev_exec = -1.41. The trader must block this even
        # though confidence cleared the floor — don't knowingly trade
        # negative expected value.
        trader = self._make_trader_with_empty_book()
        reason = trader._trade_rejection_reason(
            "mkt_test",
            self._make_rec(estimated_ev_exec=-1.41),
        )
        self.assertEqual(reason, "negative_ev")

    def test_exactly_zero_ev_also_rejected(self):
        # MIN_ESTIMATED_EV_FLOOR=0.0 and the gate is `<=`. A trade with
        # EV exactly at the floor provides no edge — no reason to take it.
        trader = self._make_trader_with_empty_book()
        reason = trader._trade_rejection_reason(
            "mkt_test",
            self._make_rec(estimated_ev_exec=0.0),
        )
        self.assertEqual(reason, "negative_ev")

    def test_positive_ev_exec_passes(self):
        # Sanity: a healthy positive-EV rec still passes all gates.
        trader = self._make_trader_with_empty_book()
        reason = trader._trade_rejection_reason(
            "mkt_test",
            self._make_rec(estimated_ev_exec=0.50),
        )
        self.assertIsNone(reason)

    def test_missing_exec_falls_back_to_legacy_estimated_ev(self):
        # Pre-migration research files don't carry estimated_ev_exec.
        # The gate must still fire on legacy estimated_ev when exec
        # is absent — otherwise this backstop doesn't work on older
        # recommendations regenerated by a stale research run.
        trader = self._make_trader_with_empty_book()
        reason = trader._trade_rejection_reason(
            "mkt_test",
            self._make_rec(estimated_ev=-0.64),   # real shape from Anthropic ARR trade
        )
        self.assertEqual(reason, "negative_ev")

    def test_missing_both_ev_fields_does_not_fire_gate(self):
        # A rec with no EV stamped at all is different from one with
        # negative EV. Pre-EV-pipeline records must still be tradable
        # on confidence alone — the gate protects against KNOWN negative
        # EV, not unknown.
        trader = self._make_trader_with_empty_book()
        rec = self._make_rec()
        rec.pop("estimated_ev", None)
        rec.pop("estimated_ev_exec", None)
        reason = trader._trade_rejection_reason("mkt_test", rec)
        self.assertIsNone(reason)

    def test_exec_ev_beats_legacy_ev_when_both_present(self):
        # When both fields are stamped, exec takes precedence. If
        # estimated_ev_exec is positive but legacy estimated_ev is
        # negative, the trade passes — exec reflects the real stake
        # size we'd actually trade.
        trader = self._make_trader_with_empty_book()
        reason = trader._trade_rejection_reason(
            "mkt_test",
            self._make_rec(estimated_ev_exec=0.25, estimated_ev=-2.0),
        )
        self.assertIsNone(reason)

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


# ── Live-EV guard in execute_trade ───────────────────────────────────────────

class TestExecuteTradeLiveEvGuard(unittest.TestCase):
    """The pre-flight gate in _trade_rejection_reason reads stale rec EV.
    But between :00 research and :20 trade the market can move, so
    execute_trade re-fetches the live market and recomputes EV. The
    invariant "do not knowingly trade negative EV" must hold at PLACEMENT
    time, not just at pre-flight time — hence the live-EV guard.
    """

    def _make_trader(self, starting_balance=100.0):
        with patch("automation.auto_trader.PaperTrader") as MockTrader:
            mock_instance = MagicMock()
            mock_instance.positions = {}
            mock_instance.balance = starting_balance
            mock_instance.place_paper_bet = MagicMock(return_value=True)
            MockTrader.return_value = mock_instance
            trader = AutoTrader()
        return trader, mock_instance

    def _make_rec(self, **overrides):
        base = {
            "market_id": "mkt_live_shift",
            "question": "Public question?",
            "recommendation": "YES",
            "confidence": 0.75,
            "probability": 0.50,                    # research-time market prob
            "liquidity": 500,
            "category": "politics",
            "unique_bettors": 20,
            "ai_recommendation": "YES",
            "ai_returned_skip": False,
            "strategies": ["probability_direction"],
            # Research said this was a good bet
            "estimated_ev": 0.40,
            "estimated_ev_exec": 0.40,
            "ai_estimated_probability": 0.70,       # our edge estimate
            "ai_confidence": 0.75,
        }
        base.update(overrides)
        return base

    def test_live_fetch_that_flips_ev_negative_blocks_placement(self):
        # Pre-flight EV was +$0.40 → passes _trade_rejection_reason.
        # But by :20 the market has moved from 0.50 → 0.90 (way past our
        # p_estimate=0.70). Live-EV recompute yields negative. The trader
        # MUST skip placement.
        trader, paper = self._make_trader()

        # Market moved hard against us between research and execution
        with patch("automation.auto_trader.api_client.get_market",
                   return_value={"probability": 0.90, "isResolved": False,
                                 "closeTime": 9999999999999}):
            result = trader.execute_trade(self._make_rec())

        self.assertFalse(result, "trade should have been skipped on live-EV guard")
        paper.place_paper_bet.assert_not_called()

    def test_live_skip_increments_rejected_negative_ev_counter(self):
        # Reviewer-caught accounting gap: the live guard blocks the trade
        # but the rec already incremented `passed_filter` in the caller.
        # To keep the reason tally honest, execute_trade increments
        # `rejected.negative_ev` on the counters dict when the live guard
        # fires. Without this, live-drift skips would show up only as
        # `passed_filter - trades_executed`, obscuring the cause.
        trader, paper = self._make_trader()
        counters = _new_trader_counters()

        with patch("automation.auto_trader.api_client.get_market",
                   return_value={"probability": 0.90, "isResolved": False,
                                 "closeTime": 9999999999999}):
            trader.execute_trade(self._make_rec(), counters=counters)

        self.assertEqual(counters["rejected"]["negative_ev"], 1)
        paper.place_paper_bet.assert_not_called()

    def test_live_fetch_that_keeps_ev_positive_still_places(self):
        # Regression guard: when the market hasn't moved enough to flip
        # EV negative, the trade still goes through.
        trader, paper = self._make_trader()

        with patch("automation.auto_trader.api_client.get_market",
                   return_value={"probability": 0.55, "isResolved": False,
                                 "closeTime": 9999999999999}):
            result = trader.execute_trade(self._make_rec())

        self.assertTrue(result)
        paper.place_paper_bet.assert_called_once()

    def test_live_ev_guard_inactive_when_p_estimate_unknown(self):
        # If the rec carries no ai_estimated_probability AND the stat
        # fallback somehow can't derive one (shouldn't happen in practice,
        # but let's be explicit), estimated_ev is None. Per the same
        # principle as the pre-flight gate, a None EV doesn't fire the
        # guard — unknown is different from negative.
        trader, paper = self._make_trader()

        rec = self._make_rec()
        rec["ai_estimated_probability"] = None
        # Force the stat-derived fallback to also return None by zeroing
        # confidence and stripping direction. This is contrived — real
        # code always produces a p_estimate — but locks in the contract.
        rec["confidence"] = 0.0
        rec["recommendation"] = None

        with patch("automation.auto_trader.api_client.get_market",
                   return_value={"probability": 0.50, "isResolved": False,
                                 "closeTime": 9999999999999}):
            # Shouldn't crash; execute_trade exits via its own no-edge
            # paths (calculate_position_size returns 0) long before we'd
            # hit the EV guard. This test exists to prove the guard
            # tolerates the None-EV path without raising.
            trader.execute_trade(rec)   # not asserting outcome — just no exception


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
    Stat-derived P(YES) fallback for estimated_ev.

    Behavior change (PR #26): the fallback used to set P(YES)=confidence,
    conflating signal reliability with outcome probability and inflating
    EV estimates. Replaced with current_prob ± bounded edge in the
    direction's favor, where edge = (confidence - 0.5) * SHRINKAGE.

    With STAT_PROB_EDGE_SHRINKAGE = 0.3 (default):
      conf=0.70, dir=YES, current=0.50 → edge=0.06, p=0.56
      conf=0.70, dir=NO,  current=0.50 → edge=0.06, p=0.44
      conf=0.95, dir=YES, current=0.50 → edge=0.135, p=0.635
    """

    def _shrinkage(self):
        from manifold_bot.config import STAT_PROB_EDGE_SHRINKAGE
        return STAT_PROB_EDGE_SHRINKAGE

    def test_yes_direction_tilts_above_current_prob_by_bounded_edge(self):
        from automation.auto_trader import _stat_derived_probability
        p = _stat_derived_probability(confidence=0.70, recommendation_direction='YES', current_prob=0.50)
        expected = 0.50 + (0.70 - 0.50) * self._shrinkage()
        self.assertAlmostEqual(p, expected, places=6)

    def test_no_direction_tilts_below_current_prob_by_bounded_edge(self):
        from automation.auto_trader import _stat_derived_probability
        p = _stat_derived_probability(confidence=0.70, recommendation_direction='NO', current_prob=0.50)
        expected = 0.50 - (0.70 - 0.50) * self._shrinkage()
        self.assertAlmostEqual(p, expected, places=6)

    def test_high_confidence_does_not_imply_high_probability(self):
        """Regression: the bug being fixed. conf=0.84 NO at 52% market used
        to produce P(YES)=0.16 (treating confidence as 1-P). Now it should
        stay near 0.52 — confidence is signal reliability, not outcome
        probability."""
        from automation.auto_trader import _stat_derived_probability
        p = _stat_derived_probability(confidence=0.84, recommendation_direction='NO', current_prob=0.52)
        # Old broken behavior: p ≈ 0.16. New behavior: only mild tilt below 0.52.
        self.assertGreater(p, 0.40)
        self.assertLess(p, 0.52)

    def test_edge_is_anchored_to_current_market_price(self):
        """Anchor changes with market price — the same confidence in a
        different market produces a different P(YES). The old impl ignored
        current_prob entirely."""
        from automation.auto_trader import _stat_derived_probability
        p_at_50 = _stat_derived_probability(confidence=0.70, recommendation_direction='YES', current_prob=0.50)
        p_at_75 = _stat_derived_probability(confidence=0.70, recommendation_direction='YES', current_prob=0.75)
        # Both tilt up by the same edge, so the difference between them
        # equals the difference in current_prob.
        self.assertAlmostEqual(p_at_75 - p_at_50, 0.25, places=6)

    def test_extreme_confidence_clamped_to_finite_range(self):
        from automation.auto_trader import _stat_derived_probability
        p = _stat_derived_probability(confidence=0.999, recommendation_direction='YES', current_prob=0.95)
        # 0.95 + (0.499 * 0.3) = 1.0997 → clamped to 0.99
        self.assertLessEqual(p, 0.99)

    def test_extreme_low_clamped_to_finite_range(self):
        from automation.auto_trader import _stat_derived_probability
        p = _stat_derived_probability(confidence=0.999, recommendation_direction='NO', current_prob=0.05)
        # 0.05 - (0.499 * 0.3) = -0.0997 → clamped to 0.01
        self.assertGreaterEqual(p, 0.01)

    def test_low_confidence_produces_tiny_edge(self):
        from automation.auto_trader import _stat_derived_probability
        p = _stat_derived_probability(confidence=0.55, recommendation_direction='YES', current_prob=0.50)
        # edge = 0.05 * 0.3 = 0.015 → p = 0.515. Modest, as it should be.
        expected = 0.50 + 0.05 * self._shrinkage()
        self.assertAlmostEqual(p, expected, places=6)

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

    def test_invalid_current_prob_returns_none(self):
        """Out-of-range current_prob signals upstream data corruption — return
        None rather than producing a bogus clamped estimate."""
        from automation.auto_trader import _stat_derived_probability
        self.assertIsNone(_stat_derived_probability(confidence=0.65, recommendation_direction='YES', current_prob=0.0))
        self.assertIsNone(_stat_derived_probability(confidence=0.65, recommendation_direction='YES', current_prob=1.0))
        self.assertIsNone(_stat_derived_probability(confidence=0.65, recommendation_direction='YES', current_prob=None))
        self.assertIsNone(_stat_derived_probability(confidence=0.65, recommendation_direction='YES', current_prob=-0.1))


class TestComputeRecEv(unittest.TestCase):
    """Research-side EV computation now flows through the same stat-derived
    probability helper as the trader. Locks in two regressions:
      1. Stat-only recs no longer get None EV (the research-layer hole the
         reviewer flagged on PR #26 — pre-flight negative_ev gate and
         cross-market EV ranking were blind to stat-only recs).
      2. AI estimate still wins when present.
    """

    def test_stat_only_rec_now_has_non_none_ev(self):
        """Pre-fix: ai_p missing → estimated_ev None → gate blind, ranking 0.
        Post-fix: stat fallback fills in honest EV."""
        from automation.auto_research import _compute_rec_ev
        rec = {
            'recommendation': 'NO',
            'confidence':     0.865,
            'probability':    0.524,
            # no ai_estimated_probability
        }
        ev_ref, ev_exec = _compute_rec_ev(rec, ev_ref_stake=25.0, ev_exec_stake=5.0)
        self.assertIsNotNone(ev_ref)
        self.assertIsNotNone(ev_exec)
        # ev_exec is at the executable stake, ev_ref at the reference stake.
        # Their ratio equals the stake ratio (5/25 = 0.2).
        self.assertAlmostEqual(ev_exec / ev_ref, 0.2, places=4)

    def test_ai_probability_takes_precedence_over_stat_fallback(self):
        """When AI provided a probability, the fallback must not engage."""
        from automation.auto_research import _compute_rec_ev
        rec_ai = {
            'recommendation': 'YES',
            'confidence':     0.65,
            'probability':    0.50,
            'ai_estimated_probability': 0.80,
        }
        rec_stat_only = dict(rec_ai)
        rec_stat_only.pop('ai_estimated_probability')

        ev_ai_ref, _   = _compute_rec_ev(rec_ai,        25.0, 5.0)
        ev_stat_ref, _ = _compute_rec_ev(rec_stat_only, 25.0, 5.0)

        # AI claims 0.80 win prob; stat fallback derives a far smaller edge
        # (current_prob + bounded shrinkage). Their EVs must differ, and
        # AI's bigger claimed edge produces the larger EV.
        self.assertNotAlmostEqual(ev_ai_ref, ev_stat_ref, places=3)
        self.assertGreater(ev_ai_ref, ev_stat_ref)

    def test_phantom_ev_regression_research_side(self):
        """The 4-23 crypto trade (conf=0.865 NO @ 52.4%) used to log
        estimated_ev=None at research time → trader couldn't reject pre-flight
        on negative EV. Post-fix the research layer surfaces honest stat-only
        EV, well below any phantom value the old execution-time path emitted."""
        from automation.auto_research import _compute_rec_ev
        rec = {
            'recommendation': 'NO',
            'confidence':     0.865,
            'probability':    0.524,
        }
        _, ev_exec = _compute_rec_ev(rec, ev_ref_stake=25.0, ev_exec_stake=5.0)
        self.assertLess(ev_exec, 2.0,
                        f"stat-only EV must not exceed ~$2 on a coin-flip "
                        f"market; got {ev_exec}")

    def test_invalid_direction_returns_none_pair(self):
        from automation.auto_research import _compute_rec_ev
        ev_ref, ev_exec = _compute_rec_ev(
            {'recommendation': 'SKIP', 'confidence': 0.7, 'probability': 0.5},
            25.0, 5.0,
        )
        self.assertIsNone(ev_ref)
        self.assertIsNone(ev_exec)

    def test_missing_confidence_returns_none_pair(self):
        """Without confidence the stat-derived helper returns None — EV
        cannot be computed and both fields stay None."""
        from automation.auto_research import _compute_rec_ev
        ev_ref, ev_exec = _compute_rec_ev(
            {'recommendation': 'YES', 'probability': 0.5},  # no confidence
            25.0, 5.0,
        )
        self.assertIsNone(ev_ref)
        self.assertIsNone(ev_exec)


class TestAiAnalysisCache(unittest.TestCase):
    """
    AI analyze-once cache (Phase 1.3): reuse prior AI results when the market
    state hasn't changed meaningfully. Frees AI bandwidth for NEW markets
    instead of re-analyzing the same top-3 every hour.
    """

    def _market(self, market_id: str, prob: float,
                question: str = "Will GPT-5 be released this year?") -> dict:
        """Small realistic market fixture that clears the normal research filters."""
        import time
        now_ms = time.time() * 1000
        return {
            "id": market_id,
            "question": question,
            "probability": prob,
            "volume24Hours": 50.0,
            "uniqueBettorCount": 10,
            "totalLiquidity": 500,
            "isResolved": False,
            "lastBetTime": now_ms - 3_600_000,
            "closeTime": now_ms + 7 * 86_400_000,
        }

    def test_valid_cache_entry_when_state_unchanged(self):
        from automation.auto_research import _is_cache_valid
        entry = {
            'result': {'recommendation': 'YES', 'confidence': 0.75},
            'analyzed_at': 1234567890,
            'market_state_at_analysis': {'probability': 0.60, 'volume24h': 100},
        }
        current_market = {'probability': 0.61, 'volume24Hours': 105}
        self.assertTrue(_is_cache_valid(entry, current_market))

    def test_invalidates_on_large_prob_move(self):
        from automation.auto_research import _is_cache_valid
        entry = {
            'result': {'recommendation': 'YES'},
            'analyzed_at': 1234567890,
            'market_state_at_analysis': {'probability': 0.60, 'volume24h': 100},
        }
        # 10pp probability move → invalidate
        current_market = {'probability': 0.70, 'volume24Hours': 105}
        self.assertFalse(_is_cache_valid(entry, current_market))

    def test_invalidates_on_double_volume(self):
        from automation.auto_research import _is_cache_valid
        entry = {
            'result': {'recommendation': 'YES'},
            'analyzed_at': 1234567890,
            'market_state_at_analysis': {'probability': 0.60, 'volume24h': 100},
        }
        # 2x volume → invalidate
        current_market = {'probability': 0.61, 'volume24Hours': 200}
        self.assertFalse(_is_cache_valid(entry, current_market))

    def test_small_prob_move_stays_valid(self):
        from automation.auto_research import _is_cache_valid
        entry = {
            'result': {'recommendation': 'YES'},
            'analyzed_at': 1234567890,
            'market_state_at_analysis': {'probability': 0.60, 'volume24h': 100},
        }
        # 3pp move → still valid (threshold is 5pp)
        current_market = {'probability': 0.63, 'volume24Hours': 110}
        self.assertTrue(_is_cache_valid(entry, current_market))

    def test_malformed_entry_invalidates(self):
        from automation.auto_research import _is_cache_valid
        self.assertFalse(_is_cache_valid(None, {'probability': 0.5}))
        self.assertFalse(_is_cache_valid("not a dict", {'probability': 0.5}))
        self.assertFalse(_is_cache_valid({'result': {}}, {'probability': 0.5}))  # missing state

    def test_cache_ai_result_stores_state_snapshot(self):
        from automation.auto_research import _cache_ai_result
        cache = {}
        market = {'probability': 0.65, 'volume24Hours': 150, 'uniqueBettorCount': 20}
        result = {'recommendation': 'YES', 'confidence': 0.80}
        _cache_ai_result(cache, 'mkt_xyz', market, result)

        self.assertIn('mkt_xyz', cache)
        self.assertEqual(cache['mkt_xyz']['result'], result)
        self.assertEqual(cache['mkt_xyz']['market_state_at_analysis']['probability'], 0.65)
        self.assertEqual(cache['mkt_xyz']['market_state_at_analysis']['volume24h'], 150)
        self.assertIsInstance(cache['mkt_xyz']['analyzed_at'], float)

    def test_load_cache_prunes_expired_entries(self):
        from automation.auto_research import _load_ai_analysis_cache, _AI_CACHE_TTL_HOURS
        import tempfile
        import json as _json
        import time as _time

        now = _time.time()
        cache_contents = {
            'fresh': {
                'result': {'recommendation': 'YES'},
                'analyzed_at': now - 3600,  # 1h ago
                'market_state_at_analysis': {'probability': 0.6, 'volume24h': 100},
            },
            'expired': {
                'result': {'recommendation': 'NO'},
                'analyzed_at': now - (_AI_CACHE_TTL_HOURS + 1) * 3600,  # beyond TTL
                'market_state_at_analysis': {'probability': 0.4, 'volume24h': 50},
            },
        }

        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as tmp:
            _json.dump(cache_contents, tmp)
            tmp_path = tmp.name

        try:
            with patch('automation.auto_research._AI_ANALYSIS_CACHE_PATH', tmp_path):
                loaded = _load_ai_analysis_cache()
            self.assertIn('fresh', loaded)
            self.assertNotIn('expired', loaded)
        finally:
            os.unlink(tmp_path)

    def test_cache_hits_survive_fresh_batch_exception(self):
        """
        End-to-end regression test for the mixed cache/fresh failure path.

        If one fresh AI batch raises TimeoutError, cached AI results must still
        be applied to their markets, and only the fresh candidate IDs should be
        recorded in cooldown.
        """
        from automation.auto_research import MarketResearcher

        markets = [
            self._market("mkt_cached", 0.80),
            self._market("mkt_fresh_1", 0.82),
            self._market("mkt_fresh_2", 0.85),
        ]
        cached_entry = {
            "result": {"recommendation": "SKIP"},
            "analyzed_at": 1234567890.0,
            "market_state_at_analysis": {"probability": 0.80, "volume24h": 50.0},
        }

        with patch("automation.auto_research.PaperTrader") as MockTrader, \
             patch("automation.auto_research.api_client") as mock_api, \
             patch("automation.auto_research._write_market_snapshots"), \
             patch("automation.auto_research._load_strategy_weights", return_value=({}, {})), \
             patch("automation.auto_research.NEWS_API_KEY", None), \
             patch("automation.auto_research._load_ai_timeout_cooldown", return_value={}), \
             patch("automation.auto_research._save_ai_timeout_cooldown"), \
             patch("automation.auto_research._record_ai_timeout") as mock_record_timeout, \
             patch("automation.auto_research._load_ai_analysis_cache", return_value={"mkt_cached": cached_entry}), \
             patch("automation.auto_research.batch_analyze", side_effect=TimeoutError("boom")):

            mock_api.get_markets.return_value = markets
            mock_instance = MagicMock()
            mock_instance.positions = {}
            MockTrader.return_value = mock_instance

            researcher = MarketResearcher()
            recs = researcher.analyze_markets(limit=10)

        self.assertGreaterEqual(len(recs), 3, "Expected recommendations for all three test markets")

        by_market = {rec["market_id"]: rec for rec in recs}
        cached = by_market["mkt_cached"]
        fresh_1 = by_market["mkt_fresh_1"]
        fresh_2 = by_market["mkt_fresh_2"]

        # Cached AI decision survives the fresh-batch timeout.
        self.assertTrue(cached["ai_was_candidate"])
        self.assertEqual(cached["ai_status"], "skip")
        self.assertEqual(cached["ai_recommendation"], "SKIP")
        self.assertTrue(cached["ai_returned_skip"])

        # Fresh candidates fall back to explicit no_result.
        self.assertEqual(fresh_1["ai_status"], "no_result")
        self.assertEqual(fresh_2["ai_status"], "no_result")

        # Cooldown should only be recorded for the fresh IDs, never the cache hit.
        mock_record_timeout.assert_called_once()
        timed_out_ids = mock_record_timeout.call_args.args[0]
        self.assertEqual(set(timed_out_ids), {"mkt_fresh_1", "mkt_fresh_2"})
        self.assertNotIn("mkt_cached", timed_out_ids)


if __name__ == "__main__":
    unittest.main()
