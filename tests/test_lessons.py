#!/usr/bin/env python3
"""
Tests for the bot learning loop (workbench-s7fo).

Covers:
  - manifold_bot/lessons.py: retro writes, exit-probability derivation,
    calibration error sign, deterministic lesson composition, aggregate
    stats, relevance-ordered lesson selection, and the prompt note builder.
  - paper_trader wiring: EVERY close/resolution path (binary, MKT, CANCEL,
    early close, abandonment) writes a retro to lessons.db NEXT TO the
    (patched) calibration.db.
  - ai_analyzer prompt injection: lessons note and probability-history note
    reach the LLM prompt; the LLM itself is mocked. Failures in the lessons
    layer never break analysis.
  - manifold_api.get_market_prob_history and auto_research._attach_prob_history
    (the current-events/history feed into analysis).
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from manifold_bot import lessons
from manifold_bot.lessons import (
    _compose_lesson,
    aggregate_stats,
    build_lessons_note,
    recent_lessons,
    write_retro,
)


def _make_trade(**overrides) -> dict:
    trade = {
        "trade_id": 1,
        "market_id": "mkt_1",
        "outcome": "YES",
        "amount": 10.0,
        "probability": 0.60,
        "entry_probability": 0.60,
        "ai_estimated_probability": 0.80,
        "ai_confidence": 0.7,
        "strategies": ["probability_direction"],
        "category": "sports",
        "question": "Will team X win?",
        "thesis": "Star player returned from injury; market lags the news.",
        "status": "OPEN",
    }
    trade.update(overrides)
    return trade


class _LessonsDbBase(unittest.TestCase):
    """Temp-dir lessons.db for every test."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db = Path(self.tmpdir) / "lessons.db"

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _rows(self):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM retros ORDER BY id").fetchall()]
        conn.close()
        return rows


# ── write_retro ──────────────────────────────────────────────────────────────

class TestWriteRetro(_LessonsDbBase):

    def test_binary_yes_win_writes_full_retro(self):
        ok = write_retro(_make_trade(), market_resolution="YES",
                         actual_pnl=6.67, db_path=self.db)
        self.assertTrue(ok)
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["market_id"], "mkt_1")
        self.assertEqual(r["our_bet"], "YES")
        self.assertEqual(r["resolution_type"], "YES")
        self.assertEqual(r["exit_probability"], 1.0)
        self.assertEqual(r["entry_probability"], 0.60)
        # calibration_error = estimate − realized = 0.80 − 1.0 = −0.20
        self.assertAlmostEqual(r["calibration_error"], -0.20, places=4)
        self.assertEqual(r["won"], 1)
        self.assertEqual(r["thesis"],
                         "Star player returned from injury; market lags the news.")
        self.assertTrue(r["lesson"])
        self.assertEqual(json.loads(r["strategies"]), ["probability_direction"])

    def test_binary_no_resolution_derives_exit_zero(self):
        write_retro(_make_trade(), market_resolution="NO",
                    actual_pnl=-10.0, db_path=self.db)
        r = self._rows()[0]
        self.assertEqual(r["exit_probability"], 0.0)
        # estimate 0.80 − realized 0.0 = +0.80 (overestimated YES)
        self.assertAlmostEqual(r["calibration_error"], 0.80, places=4)
        self.assertEqual(r["won"], 0)

    def test_mkt_resolution_uses_passed_exit_probability(self):
        write_retro(_make_trade(), market_resolution="MKT",
                    actual_pnl=-4.0, exit_probability=0.35, db_path=self.db)
        r = self._rows()[0]
        self.assertEqual(r["resolution_type"], "MKT")
        self.assertAlmostEqual(r["exit_probability"], 0.35, places=4)
        self.assertAlmostEqual(r["calibration_error"], 0.45, places=4)

    def test_cancel_has_no_calibration_error(self):
        write_retro(_make_trade(), market_resolution="CANCEL",
                    actual_pnl=0.0, db_path=self.db)
        r = self._rows()[0]
        self.assertIsNone(r["exit_probability"])
        self.assertIsNone(r["calibration_error"])
        self.assertIn("cancelled", r["lesson"].lower())

    def test_no_ai_estimate_yields_null_calibration_and_strategy_thesis(self):
        trade = _make_trade(ai_estimated_probability=None, thesis=None,
                            strategies=["volume_spike", "mean_reversion"])
        write_retro(trade, market_resolution="YES", actual_pnl=3.0,
                    db_path=self.db)
        r = self._rows()[0]
        self.assertIsNone(r["calibration_error"])
        self.assertEqual(r["thesis"], "Strategies: volume_spike, mean_reversion")

    def test_empty_trade_dict_does_not_raise(self):
        ok = write_retro({}, market_resolution="YES", actual_pnl=0.0,
                         db_path=self.db)
        self.assertTrue(ok)
        r = self._rows()[0]
        self.assertEqual(r["market_id"], "UNKNOWN")

    def test_failure_returns_false_instead_of_raising(self):
        # Point db_path at a directory — sqlite cannot open it as a DB file.
        bad = Path(self.tmpdir)  # exists and is a directory
        ok = write_retro(_make_trade(), market_resolution="YES",
                         actual_pnl=1.0, db_path=bad)
        self.assertFalse(ok)


# ── _compose_lesson ──────────────────────────────────────────────────────────

class TestComposeLesson(unittest.TestCase):

    def test_overconfident_yes_loss_names_direction_and_magnitude(self):
        lesson = _compose_lesson("YES", "sports", 0.85, 0.0, "NO", -10.0)
        self.assertIn("85pp too favorable", lesson)
        self.assertIn("sports", lesson)
        self.assertIn("lost", lesson)

    def test_no_bet_direction_flips_toward_bet_sign(self):
        # Bet NO with estimate 0.20; resolved YES (exit 1.0).
        # err = 0.20 − 1.0 = −0.80 → toward_bet = +0.80 (too bullish on NO).
        lesson = _compose_lesson("NO", "politics", 0.20, 1.0, "YES", -10.0)
        self.assertIn("80pp too favorable", lesson)

    def test_won_with_close_estimate_confirms_edge(self):
        lesson = _compose_lesson("YES", "crypto", 0.92, 1.0, "YES", 5.0)
        self.assertIn("edge was real", lesson)

    def test_abandoned_warns_about_resolution_risk(self):
        lesson = _compose_lesson("YES", "other", 0.7, None, "ABANDONED", -5.0)
        self.assertIn("never resolved", lesson)

    def test_early_close_mentions_early_close(self):
        lesson = _compose_lesson("YES", "ai_tech", 0.80, 0.90, "CLOSED_EARLY", 2.5)
        self.assertIn("closed early", lesson)


# ── aggregate_stats ──────────────────────────────────────────────────────────

class TestAggregateStats(_LessonsDbBase):

    def test_empty_store_returns_n_zero(self):
        stats = aggregate_stats(db_path=self.db)  # file doesn't even exist
        self.assertEqual(stats["n"], 0)
        self.assertIsNone(stats["win_rate"])

    def test_stats_math_and_cancel_exclusion(self):
        # Win: YES bet, est 0.80, resolved YES → err −0.20, toward-bet −20pp
        write_retro(_make_trade(market_id="m1"), "YES", 6.0, db_path=self.db)
        # Loss: YES bet, est 0.80, resolved NO → err +0.80, toward-bet +80pp
        write_retro(_make_trade(market_id="m2"), "NO", -10.0, db_path=self.db)
        # Cancel: must be excluded entirely
        write_retro(_make_trade(market_id="m3"), "CANCEL", 0.0, db_path=self.db)

        stats = aggregate_stats(db_path=self.db)
        self.assertEqual(stats["n"], 2)
        self.assertEqual(stats["wins"], 1)
        self.assertEqual(stats["losses"], 1)
        self.assertAlmostEqual(stats["win_rate"], 0.5)
        self.assertAlmostEqual(stats["total_pnl"], -4.0)
        self.assertEqual(stats["n_with_estimate"], 2)
        # signed: (−20 + 80) / 2 = +30 ; toward-bet same (both YES bets)
        self.assertAlmostEqual(stats["mean_signed_error_pp"], 30.0)
        self.assertAlmostEqual(stats["mean_error_toward_bet_pp"], 30.0)
        self.assertAlmostEqual(stats["mean_abs_error_pp"], 50.0)

    def test_toward_bet_sign_flips_for_no_bets(self):
        # NO bet, est 0.20, resolved YES → signed err −80pp, toward-bet +80pp
        write_retro(_make_trade(outcome="NO", ai_estimated_probability=0.20),
                    "YES", -10.0, db_path=self.db)
        stats = aggregate_stats(db_path=self.db)
        self.assertAlmostEqual(stats["mean_signed_error_pp"], -80.0)
        self.assertAlmostEqual(stats["mean_error_toward_bet_pp"], 80.0)


# ── recent_lessons ───────────────────────────────────────────────────────────

class TestRecentLessons(_LessonsDbBase):

    def test_same_category_first_then_backfill(self):
        write_retro(_make_trade(market_id="old_sports", category="sports"),
                    "YES", 1.0, db_path=self.db)
        write_retro(_make_trade(market_id="pol", category="politics"),
                    "NO", -10.0, db_path=self.db)
        write_retro(_make_trade(market_id="new_sports", category="sports"),
                    "NO", -10.0, db_path=self.db)

        picked = recent_lessons(category="sports", limit=3, db_path=self.db)
        self.assertEqual(len(picked), 3)
        # Sports first (newest of them first), politics backfills last.
        self.assertEqual([p["market_id"] for p in picked][:2],
                         ["new_sports", "old_sports"])
        self.assertEqual(picked[2]["market_id"], "pol")

    def test_limit_respected(self):
        for i in range(5):
            write_retro(_make_trade(market_id=f"m{i}"), "YES", 1.0,
                        db_path=self.db)
        picked = recent_lessons(category=None, limit=2, db_path=self.db)
        self.assertEqual(len(picked), 2)

    def test_missing_db_returns_empty(self):
        self.assertEqual(recent_lessons(db_path=Path(self.tmpdir) / "nope.db"), [])


# ── build_lessons_note ───────────────────────────────────────────────────────

class TestBuildLessonsNote(_LessonsDbBase):

    def test_empty_store_returns_empty_string(self):
        self.assertEqual(build_lessons_note(db_path=self.db), "")

    def test_note_contains_record_bias_and_lessons(self):
        write_retro(_make_trade(market_id="m1"), "NO", -10.0, db_path=self.db)
        write_retro(_make_trade(market_id="m2", category="politics"),
                    "NO", -10.0, db_path=self.db)
        note = build_lessons_note(category="sports", db_path=self.db)
        self.assertIn("track record (2 resolved paper trades)", note)
        self.assertIn("0W/2L", note)
        self.assertIn("overconfident", note)  # both estimates 80pp too high
        self.assertIn("Lessons from your most relevant recent trades:", note)
        self.assertIn("LOSS -$10.00", note)

    def test_underconfident_direction_reported(self):
        # YES bets, est 0.55, resolved YES → toward-bet −45pp (underconfident)
        write_retro(_make_trade(ai_estimated_probability=0.55), "YES", 6.0,
                    db_path=self.db)
        note = build_lessons_note(db_path=self.db)
        self.assertIn("underconfident", note)


# ── paper_trader wiring: every close path writes a retro ────────────────────

class TestPaperTraderWritesRetros(unittest.TestCase):
    """
    Drives the real PaperTrader close paths (Telegram mocked, calibration DB
    patched into a temp dir) and asserts retros land in lessons.db NEXT TO
    the patched calibration.db — verifying the path derivation contract.
    """

    def setUp(self):
        from manifold_bot.paper_trader import PaperTrader
        self.tmpdir = tempfile.mkdtemp()
        self.calib_db = Path(self.tmpdir) / "calibration.db"
        self.lessons_db = Path(self.tmpdir) / "lessons.db"
        self.state_file = Path(self.tmpdir) / "state.json"

        self.tg_patch = patch("manifold_bot.paper_trader._notify_resolution")
        self.tg_patch.start()
        self.db_patch = patch("manifold_bot.paper_trader._DB_PATH", self.calib_db)
        self.db_patch.start()

        with patch.object(PaperTrader, "__init__", lambda s: None):
            self.trader = PaperTrader()
        self.trader.balance = 1000.0
        self.trader.positions = {}
        self.trader.trade_history = []
        self.trader.capital_epochs = []
        self.trader.initial_balance = 1000.0
        self.trader.state_file = str(self.state_file)

    def tearDown(self):
        self.tg_patch.stop()
        self.db_patch.stop()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _add_position(self, market_id="mkt_x", outcome="YES", amount=10.0,
                      entry=0.50):
        trade = _make_trade(
            market_id=market_id, outcome=outcome, amount=amount,
            probability=entry, entry_probability=entry,
            profit_if_win=amount / entry - amount if outcome == "YES"
            else amount / (1 - entry) - amount,
            profit_if_lose=-amount,
        )
        self.trader.positions[market_id] = [trade]
        self.trader.trade_history.append(trade)
        return trade

    def _retros(self):
        if not self.lessons_db.exists():
            return []
        conn = sqlite3.connect(self.lessons_db)
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM retros ORDER BY id").fetchall()]
        conn.close()
        return rows

    def test_resolve_market_binary_writes_retro(self):
        self._add_position()
        self.trader.resolve_market("mkt_x", "YES", question="Q?")
        rows = self._retros()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["resolution_type"], "YES")
        self.assertEqual(rows[0]["exit_probability"], 1.0)

    def test_resolve_market_mkt_passes_resolution_prob(self):
        self._add_position()
        self.trader.resolve_market_mkt("mkt_x", 0.42, question="Q?")
        rows = self._retros()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["resolution_type"], "MKT")
        self.assertAlmostEqual(rows[0]["exit_probability"], 0.42, places=4)

    def test_resolve_market_cancel_writes_retro(self):
        self._add_position()
        self.trader.resolve_market_cancel("mkt_x", question="Q?")
        rows = self._retros()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["resolution_type"], "CANCEL")
        self.assertIsNone(rows[0]["exit_probability"])

    def test_close_position_early_passes_amm_prob(self):
        self._add_position()
        self.trader.close_position_early("mkt_x", current_prob=0.75)
        rows = self._retros()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["resolution_type"], "CLOSED_EARLY")
        self.assertAlmostEqual(rows[0]["exit_probability"], 0.75, places=4)

    def test_mark_market_abandoned_writes_retro(self):
        self._add_position()
        self.trader.mark_market_abandoned("mkt_x", reason="creator gone")
        rows = self._retros()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["resolution_type"], "ABANDONED")
        self.assertIsNone(rows[0]["calibration_error"])

    def test_retro_failure_does_not_break_resolution(self):
        self._add_position()
        with patch("manifold_bot.lessons.write_retro",
                   side_effect=RuntimeError("boom")):
            self.trader.resolve_market("mkt_x", "YES", question="Q?")
        # Resolution itself must have completed.
        trade = self.trader.positions["mkt_x"][0]
        self.assertEqual(trade["status"], "WIN")


# ── ai_analyzer prompt injection (LLM mocked) ───────────────────────────────

_VALID_LLM_JSON = json.dumps({
    "recommendation": "SKIP",
    "confidence": 0.0,
    "estimated_true_probability": 0.5,
    "reasoning": "test",
    "risk_factors": "test",
})


class TestPromptInjection(_LessonsDbBase):

    def _capture_prompt(self, market):
        from manifold_bot import ai_analyzer
        captured = {}

        def fake_ollama(prompt, meta=None):
            captured["prompt"] = prompt
            return _VALID_LLM_JSON

        with mock.patch("manifold_bot.ai_analyzer._call_ollama",
                        side_effect=fake_ollama), \
             mock.patch("manifold_bot.ai_analyzer._log_llm_call"):
            result = ai_analyzer.analyze_market(market)
        self.assertIsNotNone(result)
        return captured["prompt"]

    def _market(self, **overrides):
        market = {
            "id": "mkt_prompt",
            "question": "Will team X win the championship?",
            "probability": 0.60,
            "volume": 1000,
            "totalLiquidity": 500,
        }
        market.update(overrides)
        return market

    def test_lessons_note_reaches_prompt(self):
        write_retro(_make_trade(), "NO", -10.0, db_path=self.db)
        with patch.object(lessons, "_DB_PATH", self.db):
            prompt = self._capture_prompt(self._market())
        self.assertIn("Your own trading track record", prompt)
        self.assertIn("overconfident", prompt)
        self.assertIn("Lessons from your most relevant recent trades:", prompt)

    def test_no_lessons_db_means_no_note_and_no_crash(self):
        with patch.object(lessons, "_DB_PATH", Path(self.tmpdir) / "nope.db"):
            prompt = self._capture_prompt(self._market())
        self.assertNotIn("Your own trading track record", prompt)

    def test_lessons_layer_exception_never_breaks_analysis(self):
        with patch("manifold_bot.lessons.build_lessons_note",
                   side_effect=RuntimeError("boom")):
            prompt = self._capture_prompt(self._market())
        self.assertNotIn("Your own trading track record", prompt)

    def test_prob_history_reaches_prompt(self):
        now_ms = time.time() * 1000
        history = [
            {"t": now_ms - 8 * 86_400_000, "p": 0.40},
            {"t": now_ms - 2 * 86_400_000, "p": 0.50},
            {"t": now_ms - 3_600_000, "p": 0.58},
        ]
        with patch.object(lessons, "_DB_PATH", Path(self.tmpdir) / "nope.db"):
            prompt = self._capture_prompt(self._market(prob_history=history))
        self.assertIn("Market probability history", prompt)
        self.assertIn("7d ago: 40%", prompt)
        self.assertIn("latest trade: 58%", prompt)
        self.assertIn("(rising)", prompt)

    def test_no_history_collapses_cleanly(self):
        with patch.object(lessons, "_DB_PATH", Path(self.tmpdir) / "nope.db"):
            prompt = self._capture_prompt(self._market())
        self.assertNotIn("Market probability history", prompt)


class TestBuildHistoryNote(unittest.TestCase):

    def test_fewer_than_two_points_returns_empty(self):
        from manifold_bot.ai_analyzer import _build_history_note
        self.assertEqual(_build_history_note({}), "")
        self.assertEqual(
            _build_history_note({"prob_history": [{"t": 1, "p": 0.5}]}), "")

    def test_falling_trend_detected(self):
        from manifold_bot.ai_analyzer import _build_history_note
        now_ms = time.time() * 1000
        note = _build_history_note({"prob_history": [
            {"t": now_ms - 2 * 86_400_000, "p": 0.70},
            {"t": now_ms - 3_600_000, "p": 0.40},
        ]})
        self.assertIn("(falling)", note)
        self.assertIn("24h ago: 70%", note)

    def test_intraday_only_points_anchor_on_earliest(self):
        from manifold_bot.ai_analyzer import _build_history_note
        now_ms = time.time() * 1000
        note = _build_history_note({"prob_history": [
            {"t": now_ms - 5 * 3_600_000, "p": 0.50},
            {"t": now_ms - 3_600_000, "p": 0.52},
        ]})
        self.assertIn("5h ago: 50%", note)
        self.assertIn("(stable)", note)

    def test_malformed_points_never_raise(self):
        from manifold_bot.ai_analyzer import _build_history_note
        self.assertEqual(_build_history_note(
            {"prob_history": [{"t": None, "p": None}, {"x": 1}]}), "")


# ── prob-history plumbing: API client + auto_research enrichment ────────────

class TestGetMarketProbHistory(unittest.TestCase):

    def test_sorts_oldest_first_and_drops_null_probafter(self):
        from manifold_bot.manifold_api import ManifoldAPI
        api = ManifoldAPI(api_key="test")
        bets_newest_first = [
            {"createdTime": 3000, "probAfter": 0.60},
            {"createdTime": 2000, "probAfter": None},   # cancelled limit order
            {"createdTime": 1000, "probAfter": 0.40},
        ]
        with patch.object(api, "get_user_bets", return_value=bets_newest_first):
            points = api.get_market_prob_history("mkt_1")
        self.assertEqual(points, [
            {"t": 1000, "p": 0.40},
            {"t": 3000, "p": 0.60},
        ])


class TestAttachProbHistory(unittest.TestCase):

    def test_enriches_only_first_n_and_swallows_failures(self):
        from automation import auto_research
        candidates = [{"id": "a"}, {"id": "b"}, {"id": "c"}, {"id": "d"}]

        def fake_history(mid, limit=100):
            if mid == "b":
                raise RuntimeError("API down")
            return [{"t": 1, "p": 0.5}]

        mock_api = MagicMock()
        mock_api.get_market_prob_history.side_effect = fake_history
        with patch.object(auto_research, "api_client", mock_api):
            auto_research._attach_prob_history(candidates, max_n=3)

        self.assertIn("prob_history", candidates[0])
        self.assertNotIn("prob_history", candidates[1])  # failure swallowed
        self.assertIn("prob_history", candidates[2])
        self.assertNotIn("prob_history", candidates[3])  # beyond max_n

    def test_missing_id_skipped(self):
        from automation import auto_research
        candidates = [{"question": "no id"}]
        mock_api = MagicMock()
        with patch.object(auto_research, "api_client", mock_api):
            auto_research._attach_prob_history(candidates, max_n=3)
        mock_api.get_market_prob_history.assert_not_called()


if __name__ == "__main__":
    unittest.main()
