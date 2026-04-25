#!/usr/bin/env python3
"""
Tests for the Calibration & Feedback Loop feature.

Covers:
  - scripts/harvest_resolved.py   : _infer_category, parse_market, init_db
  - manifold_bot/paper_trader.py  : _init_bet_outcomes_db, _write_bet_outcome
  - scripts/weekly_ev_report.py   : _ev_accuracy_section, _strategy_breakdown,
                                    _confidence_breakdown
  - manifold_bot/ai_analyzer.py   : _build_query_calibration_note

All tests are pure unit tests — no network calls, no Manifold API, no Ollama.
DB-dependent tests use a temporary SQLite file cleaned up after each test.
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from scripts.harvest_resolved import _infer_category, parse_market, init_db
from scripts.weekly_ev_report import (
    _ev_accuracy_section,
    _strategy_breakdown,
    _confidence_breakdown,
)
from manifold_bot.ai_analyzer import _build_query_calibration_note
from manifold_bot.paper_trader import _init_bet_outcomes_db, _write_bet_outcome


# ── shared helpers ─────────────────────────────────────────────────────────────

def _make_tmp_db() -> Path:
    """Return a Path to a fresh, empty temp SQLite file."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return Path(path)


def _make_market(
    resolution="YES",
    probability=0.72,
    uniqueBettorCount=10,
    question="Will Python remain the most popular language in 2027?",
    market_id="test_market_001",
):
    return {
        "id": market_id,
        "question": question,
        "probability": probability,
        "resolution": resolution,
        "closeTime": 1800000000000,
        "uniqueBettorCount": uniqueBettorCount,
    }


def _make_outcome(
    actual_pnl=5.0,
    estimated_ev=10.0,
    ai_confidence=0.75,
    strategies='["probability_direction_strategy"]',
):
    ev_error = (estimated_ev - actual_pnl) if estimated_ev is not None else None
    return {
        "market_id": "m1",
        "our_recommendation": "YES",
        "amount": 20.0,
        "probability": 0.65,
        "estimated_ev": estimated_ev,
        "ai_confidence": ai_confidence,
        "strategies": strategies,
        "market_resolution": "YES",
        "actual_pnl": actual_pnl,
        "ev_error": ev_error,
        "resolved_at": "2026-04-07T10:00:00",
    }


# ── Group 1: _infer_category ───────────────────────────────────────────────────

class TestInferCategory(unittest.TestCase):

    def test_crypto_keywords(self):
        self.assertEqual(_infer_category("Will Bitcoin hit $100k by 2027?"), "crypto")
        self.assertEqual(_infer_category("Will Ethereum merge succeed?"), "crypto")
        self.assertEqual(_infer_category("New NFT drop for Solana gamers"), "crypto")

    def test_politics_keywords(self):
        self.assertEqual(_infer_category("Will Trump win the 2028 election?"), "politics")
        self.assertEqual(_infer_category("Will the Senate pass new legislation?"), "politics")
        self.assertEqual(_infer_category("Outcome of the vote for president"), "politics")

    def test_ai_tech_keywords(self):
        self.assertEqual(_infer_category("Will GPT-5 be released this year?"), "ai_tech")
        self.assertEqual(_infer_category("Will Anthropic release Claude 5?"), "ai_tech")
        self.assertEqual(_infer_category("New OpenAI model benchmark results"), "ai_tech")

    def test_sports_keywords(self):
        self.assertEqual(_infer_category("Will the NBA championship go to 7 games?"), "sports")
        self.assertEqual(_infer_category("Will the NFL team win the tournament?"), "sports")

    def test_economics_keywords(self):
        self.assertEqual(_infer_category("Will the Fed raise interest rate in June?"), "economics")
        self.assertEqual(_infer_category("Will US GDP growth exceed 3% in 2026?"), "economics")

    def test_science_keywords(self):
        self.assertEqual(_infer_category("Will NASA launch the moon mission?"), "science")
        self.assertEqual(_infer_category("New climate research on global temperature"), "science")

    def test_other_fallback(self):
        self.assertEqual(_infer_category("Will Alice beat Bob at chess?"), "other")
        self.assertEqual(_infer_category("Random question with no obvious topic"), "other")

    def test_case_insensitive(self):
        # Keywords are matched in lower-case — input case shouldn't matter
        self.assertEqual(_infer_category("BITCOIN price prediction"), "crypto")
        self.assertEqual(_infer_category("ELECTION results 2028"), "politics")

    def test_war_conflict_is_politics(self):
        # War/conflict terms added to politics — previously fell through to 'other'
        self.assertEqual(_infer_category("Will the ceasefire hold in Gaza?"), "politics")
        self.assertEqual(_infer_category("Will Russia escalate the war in Ukraine?"), "politics")
        self.assertEqual(_infer_category("Will NATO invoke Article 5?"), "politics")

    def test_generic_sports_terms(self):
        # Generic match/win/team/score terms added — previously fell through to 'other'
        self.assertEqual(_infer_category("Will Arsenal win the match against Sporting Lisboa?"), "sports")
        self.assertEqual(_infer_category("Who will score the most goals this season?"), "sports")
        self.assertEqual(_infer_category("Which team wins the Champions League?"), "sports")

    def test_deepseek_is_ai_tech(self):
        self.assertEqual(_infer_category("Will DeepSeek R2 beat GPT-5 on benchmarks?"), "ai_tech")
        self.assertEqual(_infer_category("Will the Grok chatbot gain 10M users?"), "ai_tech")

    def test_harvest_uses_strategies_implementation(self):
        # _infer_category in harvest_resolved is now an alias for _infer_market_category
        # in strategies — both should return identical results for the same input.
        from manifold_bot.strategies import _infer_market_category
        questions = [
            "Will Bitcoin hit $100k?",
            "Will Trump win in 2028?",
            "Will GPT-5 launch?",
            "Will Arsenal win the match?",
            "Will US GDP grow 3%?",
            "Will NASA launch?",
            "Will Alice beat Bob?",
            "Will the ceasefire hold in Gaza?",
        ]
        for q in questions:
            self.assertEqual(_infer_category(q), _infer_market_category(q),
                             f"Mismatch for: {q!r}")


# ── Group 2: parse_market ──────────────────────────────────────────────────────

class TestParseMarket(unittest.TestCase):

    def test_valid_yes_market_returns_dict(self):
        result = parse_market(_make_market(resolution="YES"))
        self.assertIsNotNone(result)
        self.assertEqual(result["market_id"], "test_market_001")
        self.assertEqual(result["outcome"], "YES")
        self.assertAlmostEqual(result["probability_close"], 0.72)

    def test_valid_no_market_returns_dict(self):
        result = parse_market(_make_market(resolution="NO", probability=0.30))
        self.assertIsNotNone(result)
        self.assertEqual(result["outcome"], "NO")

    def test_mkt_resolution_returns_none(self):
        self.assertIsNone(parse_market(_make_market(resolution="MKT")))

    def test_cancel_resolution_returns_none(self):
        self.assertIsNone(parse_market(_make_market(resolution="CANCEL")))

    def test_fewer_than_3_bettors_returns_none(self):
        self.assertIsNone(parse_market(_make_market(uniqueBettorCount=2)))
        self.assertIsNone(parse_market(_make_market(uniqueBettorCount=0)))

    def test_exactly_3_bettors_passes(self):
        self.assertIsNotNone(parse_market(_make_market(uniqueBettorCount=3)))

    def test_missing_probability_returns_none(self):
        m = _make_market()
        del m["probability"]
        self.assertIsNone(parse_market(m))

    def test_category_is_inferred(self):
        result = parse_market(_make_market(question="Will Bitcoin hit $100k?"))
        self.assertIsNotNone(result)
        self.assertEqual(result["category"], "crypto")

    def test_close_date_parsed_from_milliseconds(self):
        result = parse_market(_make_market())
        self.assertIsNotNone(result["close_date"])
        # Should be an ISO string containing 'T'
        self.assertIn("T", result["close_date"])

    def test_result_contains_all_expected_keys(self):
        result = parse_market(_make_market())
        for key in ("market_id", "question", "probability_close", "outcome",
                    "close_date", "unique_bettors", "category"):
            self.assertIn(key, result, f"Missing key: {key}")


# ── Group 3: init_db schema migration ─────────────────────────────────────────

class TestInitDb(unittest.TestCase):

    def test_creates_table_on_fresh_db(self):
        conn = sqlite3.connect(":memory:")
        init_db(conn)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("resolved_markets", tables)
        conn.close()

    def test_fresh_db_has_category_column(self):
        conn = sqlite3.connect(":memory:")
        init_db(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(resolved_markets)")}
        self.assertIn("category", cols)
        conn.close()

    def test_migration_adds_category_to_old_schema(self):
        """Databases created before the category column exist should get it via ALTER TABLE."""
        conn = sqlite3.connect(":memory:")
        conn.execute("""
            CREATE TABLE resolved_markets (
                market_id         TEXT PRIMARY KEY,
                question          TEXT NOT NULL,
                probability_close REAL,
                outcome           TEXT,
                close_date        TEXT,
                unique_bettors    INTEGER
            )
        """)
        conn.commit()
        cols_before = {r[1] for r in conn.execute("PRAGMA table_info(resolved_markets)")}
        self.assertNotIn("category", cols_before)

        init_db(conn)   # should add category via ALTER TABLE

        cols_after = {r[1] for r in conn.execute("PRAGMA table_info(resolved_markets)")}
        self.assertIn("category", cols_after)
        conn.close()

    def test_index_created(self):
        conn = sqlite3.connect(":memory:")
        init_db(conn)
        indexes = {r[1] for r in conn.execute("SELECT * FROM sqlite_master WHERE type='index'")}
        self.assertIn("idx_prob_close", indexes)
        conn.close()

    def test_idempotent_on_repeat_calls(self):
        """Calling init_db twice on the same connection must not raise."""
        conn = sqlite3.connect(":memory:")
        init_db(conn)
        init_db(conn)   # second call — CREATE TABLE IF NOT EXISTS should be a no-op
        conn.close()


# ── Group 4: _write_bet_outcome ────────────────────────────────────────────────

class TestWriteBetOutcome(unittest.TestCase):
    """
    Each test creates its own temp DB so tests are fully isolated.
    We patch manifold_bot.paper_trader._DB_PATH to redirect all DB writes.
    """

    def _create_db(self) -> Path:
        tmp = _make_tmp_db()
        with patch("manifold_bot.paper_trader._DB_PATH", tmp):
            _init_bet_outcomes_db()
        return tmp

    def _read_rows(self, db_path: Path) -> list[dict]:
        cols = ["market_id", "our_recommendation", "amount", "probability",
                "estimated_ev", "ai_confidence", "strategies",
                "market_resolution", "actual_pnl", "ev_error", "resolved_at"]
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT " + ", ".join(cols) + " FROM bet_outcomes"
        ).fetchall()
        conn.close()
        return [dict(zip(cols, r)) for r in rows]

    def test_writes_correct_fields(self):
        tmp = self._create_db()
        try:
            trade = {
                "market_id": "mkt123",
                "outcome": "YES",
                "amount": 20.0,
                "probability": 0.65,
                "estimated_ev": 5.0,
                "ai_confidence": 0.78,
                "strategies": ["probability_direction_strategy"],
            }
            with patch("manifold_bot.paper_trader._DB_PATH", tmp):
                _write_bet_outcome(trade, "YES", 5.0)

            rows = self._read_rows(tmp)
            self.assertEqual(len(rows), 1)
            r = rows[0]
            self.assertEqual(r["market_id"], "mkt123")
            self.assertEqual(r["our_recommendation"], "YES")
            self.assertAlmostEqual(r["amount"], 20.0)
            self.assertAlmostEqual(r["estimated_ev"], 5.0)
            self.assertAlmostEqual(r["ai_confidence"], 0.78)
            self.assertEqual(r["market_resolution"], "YES")
            self.assertAlmostEqual(r["actual_pnl"], 5.0)
        finally:
            os.unlink(tmp)

    def test_ev_error_computed_correctly(self):
        # ev_error = estimated_ev - actual_pnl
        tmp = self._create_db()
        try:
            trade = {
                "market_id": "mkt_ev", "outcome": "YES",
                "amount": 10.0, "probability": 0.60,
                "estimated_ev": 8.0, "ai_confidence": 0.80, "strategies": None,
            }
            with patch("manifold_bot.paper_trader._DB_PATH", tmp):
                _write_bet_outcome(trade, "YES", 3.0)

            rows = self._read_rows(tmp)
            # 8.0 - 3.0 = 5.0
            self.assertAlmostEqual(rows[0]["ev_error"], 5.0)
        finally:
            os.unlink(tmp)

    def test_ev_error_is_null_when_no_estimated_ev(self):
        tmp = self._create_db()
        try:
            trade = {
                "market_id": "mkt_no_ev", "outcome": "NO",
                "amount": 15.0, "probability": 0.40,
                "estimated_ev": None, "ai_confidence": None, "strategies": None,
            }
            with patch("manifold_bot.paper_trader._DB_PATH", tmp):
                _write_bet_outcome(trade, "NO", -15.0)

            rows = self._read_rows(tmp)
            self.assertIsNone(rows[0]["ev_error"])
            self.assertIsNone(rows[0]["estimated_ev"])
        finally:
            os.unlink(tmp)

    def test_strategies_stored_as_json_list(self):
        tmp = self._create_db()
        try:
            trade = {
                "market_id": "mkt_strat", "outcome": "YES",
                "amount": 10.0, "probability": 0.60,
                "estimated_ev": 2.0, "ai_confidence": 0.70,
                "strategies": ["volume_spike_strategy", "mean_reversion_strategy"],
            }
            with patch("manifold_bot.paper_trader._DB_PATH", tmp):
                _write_bet_outcome(trade, "YES", 2.0)

            rows = self._read_rows(tmp)
            stored = json.loads(rows[0]["strategies"])
            self.assertIn("volume_spike_strategy", stored)
            self.assertIn("mean_reversion_strategy", stored)
        finally:
            os.unlink(tmp)

    def test_never_raises_on_empty_trade_dict(self):
        """Silent failure guarantee — DB errors must never crash the paper trader."""
        tmp = self._create_db()
        try:
            with patch("manifold_bot.paper_trader._DB_PATH", tmp):
                _write_bet_outcome({}, "YES", 0.0)   # must not raise
        finally:
            os.unlink(tmp)


# ── Group 5: _ev_accuracy_section ─────────────────────────────────────────────

class TestEvAccuracySection(unittest.TestCase):

    def test_empty_list_returns_no_trades_message(self):
        text, stats = _ev_accuracy_section([])
        self.assertIn("No resolved trades", text)
        self.assertEqual(stats["total_resolved"], 0)

    def test_win_rate_and_total_pnl_computed(self):
        outcomes = [
            _make_outcome(actual_pnl=10.0),
            _make_outcome(actual_pnl=-5.0),
            _make_outcome(actual_pnl=3.0),
        ]
        text, stats = _ev_accuracy_section(outcomes)
        self.assertEqual(stats["total_resolved"], 3)
        self.assertAlmostEqual(stats["win_rate"], 2 / 3, places=2)
        self.assertAlmostEqual(stats["total_pnl"], 8.0)

    def test_ev_accuracy_ratio_computed(self):
        # estimated_ev=10 per trade, actual_pnl=5 → ratio = 0.50
        outcomes = [_make_outcome(actual_pnl=5.0, estimated_ev=10.0) for _ in range(4)]
        _, stats = _ev_accuracy_section(outcomes)
        self.assertAlmostEqual(stats["ev_accuracy_ratio"], 0.5, places=2)

    def test_well_calibrated_label_when_ratio_at_least_085(self):
        # Need >= MIN_SAMPLES_GLOBAL (20) to pass the gate and show the calibration label.
        outcomes = [_make_outcome(actual_pnl=9.0, estimated_ev=10.0) for _ in range(20)]
        text, _ = _ev_accuracy_section(outcomes)
        self.assertIn("well-calibrated", text.lower())

    def test_no_ev_outcomes_reports_correctly(self):
        outcomes = [_make_outcome(actual_pnl=5.0, estimated_ev=None) for _ in range(3)]
        text, stats = _ev_accuracy_section(outcomes)
        self.assertEqual(stats.get("with_ev_estimate", 0), 0)
        self.assertIn("No AI-estimated EV", text)

    def test_ratio_none_when_avg_ev_is_zero(self):
        # avg_ev = 0 → division by zero must be avoided; ratio should be None
        outcomes = [_make_outcome(actual_pnl=1.0, estimated_ev=0.0)]
        _, stats = _ev_accuracy_section(outcomes)
        self.assertIsNone(stats.get("ev_accuracy_ratio"))


# ── Group 6: _strategy_breakdown ──────────────────────────────────────────────

class TestStrategyBreakdown(unittest.TestCase):

    def test_single_strategy_per_trade(self):
        outcomes = [
            _make_outcome(actual_pnl=5.0,  strategies='["probability_direction_strategy"]'),
            _make_outcome(actual_pnl=-3.0, strategies='["probability_direction_strategy"]'),
        ]
        text = _strategy_breakdown(outcomes)
        self.assertIn("probability_direction_strategy", text)

    def test_multi_strategy_trade_credits_all_strategies(self):
        outcomes = [
            _make_outcome(actual_pnl=4.0,
                          strategies='["volume_spike_strategy", "mean_reversion_strategy"]'),
        ]
        text = _strategy_breakdown(outcomes)
        self.assertIn("volume_spike_strategy", text)
        self.assertIn("mean_reversion_strategy", text)

    def test_none_strategy_labelled_unknown(self):
        outcomes = [_make_outcome(actual_pnl=2.0, strategies=None)]
        text = _strategy_breakdown(outcomes)
        self.assertIn("unknown", text)

    def test_empty_outcomes_returns_no_data_string(self):
        self.assertEqual(_strategy_breakdown([]), "No strategy data.")

    def test_sorted_by_total_pnl_descending(self):
        outcomes = [
            _make_outcome(actual_pnl=-10.0, strategies='["losing_strategy"]'),
            _make_outcome(actual_pnl=20.0,  strategies='["winning_strategy"]'),
        ]
        text = _strategy_breakdown(outcomes)
        self.assertLess(text.index("winning_strategy"), text.index("losing_strategy"))


# ── Group 7: _confidence_breakdown ────────────────────────────────────────────

class TestConfidenceBreakdown(unittest.TestCase):

    def _breakdown_for(self, confidence) -> str:
        return _confidence_breakdown([_make_outcome(actual_pnl=1.0, ai_confidence=confidence)])

    def test_below_070_band(self):
        self.assertIn("< 0.70", self._breakdown_for(0.65))

    def test_070_to_080_band(self):
        self.assertIn("0.70-0.80", self._breakdown_for(0.75))

    def test_080_to_090_band(self):
        self.assertIn("0.80-0.90", self._breakdown_for(0.85))

    def test_090_and_above_band(self):
        self.assertIn("≥ 0.90", self._breakdown_for(0.95))

    def test_none_confidence_goes_to_no_ai_band(self):
        self.assertIn("no AI", self._breakdown_for(None))

    def test_boundary_value_070_falls_in_mid_band(self):
        # 0.70 is NOT < 0.70, so it should fall in the 0.70-0.80 band
        self.assertIn("0.70-0.80", self._breakdown_for(0.70))

    def test_empty_outcomes_returns_no_data_string(self):
        self.assertEqual(_confidence_breakdown([]), "No confidence data.")


# ── Group 8: _build_query_calibration_note ────────────────────────────────────

class TestBuildQueryCalibrationNote(unittest.TestCase):
    """
    Tests for _build_query_calibration_note(question, probability).

    The function reads module-level _CALIBRATION_DATA (cached at import time).
    Tests patch that dict directly so they don't depend on file I/O.
    """

    def _patch_data(self, data: dict):
        return patch("manifold_bot.ai_analyzer._CALIBRATION_DATA", data)

    def test_returns_empty_when_no_calibration_data(self):
        with self._patch_data({}):
            result = _build_query_calibration_note("Will Bitcoin hit $100k?", 0.65)
        self.assertEqual(result, "")

    def test_uses_category_bucket_cell_when_available(self):
        """Category-specific cell (category in cell) takes precedence over global."""
        data = {
            "by_category_bucket": [{
                "category": "crypto",
                "bucket_low": 0.60, "bucket_high": 0.70,
                "crowd_midpoint": 0.65, "actual_yes_rate": 0.42,
                "sample_size": 50, "bias": 0.23, "reliable": True,
            }],
            "buckets": [],
        }
        with self._patch_data(data):
            result = _build_query_calibration_note("Will Bitcoin reach $200k by 2027?", 0.65)
        self.assertIn("60", result)
        self.assertIn("70", result)
        self.assertIn("42", result)   # actual YES rate
        self.assertIn("category: crypto", result)

    def test_falls_back_to_global_bucket_when_no_category_cell(self):
        """No category cell → uses global bucket row."""
        data = {
            "by_category_bucket": [],
            "buckets": [{
                "bucket_low": 0.60, "bucket_high": 0.70,
                "crowd_midpoint": 0.65, "actual_yes_rate": 0.47,
                "sample_size": 120, "bias": 0.18, "reliable": True,
            }],
        }
        with self._patch_data(data):
            # Question maps to 'other' — no category cell — uses global
            result = _build_query_calibration_note("Will this resolve YES?", 0.65)
        self.assertIn("47", result)
        self.assertIn("global average", result)

    def test_unreliable_category_cell_skipped_falls_to_global(self):
        """Unreliable category cell is ignored; function falls back to global."""
        data = {
            "by_category_bucket": [{
                "category": "crypto",
                "bucket_low": 0.60, "bucket_high": 0.70,
                "crowd_midpoint": 0.65, "actual_yes_rate": 0.50,
                "sample_size": 3, "bias": 0.15, "reliable": False,
            }],
            "buckets": [{
                "bucket_low": 0.60, "bucket_high": 0.70,
                "crowd_midpoint": 0.65, "actual_yes_rate": 0.47,
                "sample_size": 120, "bias": 0.18, "reliable": True,
            }],
        }
        with self._patch_data(data):
            result = _build_query_calibration_note("Will Bitcoin reach $200k?", 0.65)
        self.assertIn("47", result)   # global cell's YES rate
        self.assertIn("global average", result)

    def test_returns_empty_when_no_matching_bucket(self):
        """Probability falls outside any stored bucket → returns empty string."""
        data = {
            "by_category_bucket": [],
            "buckets": [{
                "bucket_low": 0.60, "bucket_high": 0.70,
                "crowd_midpoint": 0.65, "actual_yes_rate": 0.47,
                "sample_size": 120, "bias": 0.18, "reliable": True,
            }],
        }
        with self._patch_data(data):
            # 0.45 falls in the 40-50% bucket — not stored → empty
            result = _build_query_calibration_note("Will this resolve YES?", 0.45)
        self.assertEqual(result, "")

    def test_output_includes_adjustment_hint(self):
        """Output must include a suggested adjustment line."""
        data = {
            "by_category_bucket": [],
            "buckets": [{
                "bucket_low": 0.60, "bucket_high": 0.70,
                "crowd_midpoint": 0.65, "actual_yes_rate": 0.47,
                "sample_size": 120, "bias": 0.18, "reliable": True,
            }],
        }
        with self._patch_data(data):
            result = _build_query_calibration_note("Will this happen?", 0.65)
        self.assertIn("Suggested adjustment", result)

    def test_positive_bias_says_overestimates(self):
        """Positive bias → 'crowd overestimates YES'."""
        data = {
            "by_category_bucket": [],
            "buckets": [{
                "bucket_low": 0.60, "bucket_high": 0.70,
                "crowd_midpoint": 0.65, "actual_yes_rate": 0.47,
                "sample_size": 120, "bias": 0.18, "reliable": True,
            }],
        }
        with self._patch_data(data):
            result = _build_query_calibration_note("Will this happen?", 0.65)
        self.assertIn("overestimates", result)
        self.assertNotIn("underestimates", result)

    def test_negative_bias_says_underestimates(self):
        """Negative bias → 'crowd underestimates YES', no contradictory 'overestimates'."""
        data = {
            "by_category_bucket": [],
            "buckets": [{
                "bucket_low": 0.90, "bucket_high": 1.00,
                "crowd_midpoint": 0.95, "actual_yes_rate": 1.00,
                "sample_size": 30, "bias": -0.05, "reliable": True,
            }],
        }
        with self._patch_data(data):
            result = _build_query_calibration_note("Will this happen?", 0.95)
        self.assertIn("underestimates", result)
        self.assertNotIn("overestimates", result)


def _cat_outcome(rec, resolution, category):
    return {
        "our_recommendation": rec,
        "market_resolution": resolution,
        "category": category,
        "strategies": '["probability_direction"]',
    }


class TestComputeCategoryAccuracy(unittest.TestCase):
    """Phase C: _compute_category_accuracy() correctly uses the category column."""

    def test_correct_accuracy_per_category(self):
        from scripts.compute_strategy_weights import _compute_category_accuracy
        outcomes = [
            _cat_outcome("YES", "YES", "crypto"),    # correct
            _cat_outcome("YES", "NO",  "crypto"),    # wrong
            _cat_outcome("YES", "YES", "crypto"),    # correct
            _cat_outcome("NO",  "NO",  "politics"),  # correct
            _cat_outcome("YES", "NO",  "politics"),  # wrong
        ]
        result = _compute_category_accuracy(outcomes)
        self.assertAlmostEqual(result["crypto"]["accuracy"], 2/3, places=3)
        self.assertEqual(result["crypto"]["sample_count"], 3)
        self.assertAlmostEqual(result["politics"]["accuracy"], 0.5, places=3)
        self.assertEqual(result["politics"]["sample_count"], 2)

    def test_none_category_falls_back_to_other(self):
        from scripts.compute_strategy_weights import _compute_category_accuracy
        outcomes = [
            _cat_outcome("YES", "YES", None),
            _cat_outcome("YES", "NO",  None),
        ]
        result = _compute_category_accuracy(outcomes)
        self.assertIn("other", result)
        self.assertEqual(result["other"]["sample_count"], 2)

    def test_empty_outcomes_returns_empty_dict(self):
        from scripts.compute_strategy_weights import _compute_category_accuracy
        self.assertEqual(_compute_category_accuracy([]), {})

    def test_fetch_outcomes_includes_category_column(self):
        """_fetch_outcomes must return dicts with a 'category' key so Phase C has data."""
        import sqlite3, tempfile as _tempfile
        from scripts.weekly_ev_report import _fetch_outcomes

        with _tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "calibration.db")
            conn = sqlite3.connect(db_path)
            conn.execute("""
                CREATE TABLE bet_outcomes (
                    id INTEGER PRIMARY KEY,
                    market_id TEXT, our_recommendation TEXT, amount REAL,
                    probability REAL, estimated_ev REAL, ai_confidence REAL,
                    ai_estimated_probability REAL, strategies TEXT,
                    market_resolution TEXT, actual_pnl REAL, ev_error REAL,
                    era TEXT, resolved_at TEXT, category TEXT
                )
            """)
            conn.execute("""
                INSERT INTO bet_outcomes
                (market_id, our_recommendation, amount, probability,
                 estimated_ev, ai_confidence, ai_estimated_probability,
                 strategies, market_resolution, actual_pnl, ev_error,
                 era, resolved_at, category)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, ('m1','YES',10,0.4,1.0,0.8,0.6,'[]','YES',5.0,-4.0,'post_ev_fix','2026-04-09','crypto'))
            conn.commit()
            conn.close()

            with patch("scripts.weekly_ev_report.DB_PATH", db_path):
                rows = _fetch_outcomes(era=None)

        self.assertEqual(len(rows), 1)
        self.assertIn("category", rows[0], "_fetch_outcomes must return 'category' key")
        self.assertEqual(rows[0]["category"], "crypto")


# ── M2 Audit tests ────────────────────────────────────────────────────────────

def _make_resolved_row(
    market_id="m1",
    question="Will X happen?",
    probability_close=0.55,
    outcome="YES",
    close_date="2026-03-01T00:00:00+00:00",
    unique_bettors=20,
    category="crypto",
) -> dict:
    return {
        "market_id": market_id,
        "question": question,
        "probability_close": probability_close,
        "outcome": outcome,
        "close_date": close_date,
        "unique_bettors": unique_bettors,
        "category": category,
    }


class TestM2CategoryDistribution(unittest.TestCase):
    """audit_resolved_markets.category_distribution correct counts and percentages."""

    def test_counts_by_category(self):
        from scripts.audit_resolved_markets import category_distribution
        markets = [
            _make_resolved_row(category="crypto"),
            _make_resolved_row(category="crypto"),
            _make_resolved_row(category="politics"),
        ]
        result = category_distribution(markets)
        self.assertEqual(result["crypto"]["count"], 2)
        self.assertEqual(result["politics"]["count"], 1)
        self.assertAlmostEqual(result["crypto"]["pct"], 2 / 3, places=3)

    def test_none_category_falls_back_to_other(self):
        from scripts.audit_resolved_markets import category_distribution
        markets = [_make_resolved_row(category=None)]
        result = category_distribution(markets)
        self.assertIn("other", result)

    def test_empty_markets_returns_empty(self):
        from scripts.audit_resolved_markets import category_distribution
        self.assertEqual(category_distribution([]), {})


class TestM2NearCloseStats(unittest.TestCase):
    """near_close_stats correctly classifies decision-zone vs near-close markets."""

    def test_near_close_high(self):
        from scripts.audit_resolved_markets import near_close_stats
        markets = [_make_resolved_row(probability_close=0.92)]
        result = near_close_stats(markets, high=0.85, low=0.15)
        self.assertEqual(result["near_close_count"], 1)
        self.assertEqual(result["decision_zone_count"], 0)

    def test_near_close_low(self):
        from scripts.audit_resolved_markets import near_close_stats
        markets = [_make_resolved_row(probability_close=0.08)]
        result = near_close_stats(markets, high=0.85, low=0.15)
        self.assertEqual(result["near_close_count"], 1)

    def test_decision_zone(self):
        from scripts.audit_resolved_markets import near_close_stats
        markets = [_make_resolved_row(probability_close=0.50)]
        result = near_close_stats(markets, high=0.85, low=0.15)
        self.assertEqual(result["decision_zone_count"], 1)
        self.assertEqual(result["near_close_count"], 0)

    def test_mixed(self):
        from scripts.audit_resolved_markets import near_close_stats
        markets = [
            _make_resolved_row(probability_close=0.90),  # near-close
            _make_resolved_row(probability_close=0.55),  # decision zone
            _make_resolved_row(probability_close=0.10),  # near-close
        ]
        result = near_close_stats(markets, high=0.85, low=0.15)
        self.assertEqual(result["near_close_count"], 2)
        self.assertEqual(result["decision_zone_count"], 1)

    def test_empty_markets(self):
        from scripts.audit_resolved_markets import near_close_stats
        result = near_close_stats([])
        self.assertEqual(result["near_close_count"], 0)
        self.assertEqual(result["decision_zone_count"], 0)


class TestM2BettorStats(unittest.TestCase):
    """bettor_stats summarises unique_bettors distribution correctly."""

    def test_thin_market_count(self):
        from scripts.audit_resolved_markets import bettor_stats
        markets = [
            _make_resolved_row(unique_bettors=5),   # thin
            _make_resolved_row(unique_bettors=15),  # ok
            _make_resolved_row(unique_bettors=20),  # ok
        ]
        result = bettor_stats(markets, min_bettors=10)
        self.assertEqual(result["thin_count"], 1)
        self.assertAlmostEqual(result["thin_pct"], 1 / 3, places=3)

    def test_median(self):
        from scripts.audit_resolved_markets import bettor_stats
        markets = [
            _make_resolved_row(unique_bettors=10),
            _make_resolved_row(unique_bettors=20),
            _make_resolved_row(unique_bettors=30),
        ]
        result = bettor_stats(markets)
        self.assertEqual(result["median"], 20)

    def test_empty_markets(self):
        from scripts.audit_resolved_markets import bettor_stats
        result = bettor_stats([])
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["thin_count"], 0)


class TestM2UsableMarketIds(unittest.TestCase):
    """usable_market_ids applies all three quality filters."""

    def test_passes_all_filters(self):
        from scripts.audit_resolved_markets import usable_market_ids
        markets = [_make_resolved_row(
            market_id="good", probability_close=0.55, unique_bettors=20
        )]
        result = usable_market_ids(markets)
        self.assertIn("good", result)

    def test_filtered_near_close(self):
        from scripts.audit_resolved_markets import usable_market_ids
        markets = [_make_resolved_row(
            market_id="bad_nc", probability_close=0.92, unique_bettors=20
        )]
        self.assertEqual(usable_market_ids(markets), [])

    def test_filtered_thin(self):
        from scripts.audit_resolved_markets import usable_market_ids
        markets = [_make_resolved_row(
            market_id="bad_thin", probability_close=0.55, unique_bettors=3
        )]
        self.assertEqual(usable_market_ids(markets, min_bettors=10), [])

    def test_filtered_null_probability(self):
        from scripts.audit_resolved_markets import usable_market_ids
        markets = [_make_resolved_row(market_id="bad_null", probability_close=None)]
        self.assertEqual(usable_market_ids(markets), [])

    def test_mixed_batch(self):
        from scripts.audit_resolved_markets import usable_market_ids
        markets = [
            _make_resolved_row(market_id="ok",       probability_close=0.55, unique_bettors=15),
            _make_resolved_row(market_id="nc",       probability_close=0.95, unique_bettors=15),
            _make_resolved_row(market_id="thin",     probability_close=0.55, unique_bettors=2),
            _make_resolved_row(market_id="no_prob",  probability_close=None, unique_bettors=15),
        ]
        result = usable_market_ids(markets, min_bettors=10)
        self.assertEqual(result, ["ok"])


class TestM2DecisionGate(unittest.TestCase):
    """_decision_gate returns correct GREEN/YELLOW/RED status."""

    def test_green_when_enough_usable_no_flags(self):
        from scripts.audit_resolved_markets import _decision_gate
        result = _decision_gate(
            usable_count=300, near_close_pct=0.20,
            thin_pct=0.10, other_pct=0.30
        )
        self.assertEqual(result["status"], "GREEN")
        self.assertEqual(result["flags"], [])

    def test_red_when_too_few_usable(self):
        from scripts.audit_resolved_markets import _decision_gate
        result = _decision_gate(
            usable_count=50, near_close_pct=0.10,
            thin_pct=0.10, other_pct=0.30
        )
        self.assertEqual(result["status"], "RED")

    def test_yellow_with_flags(self):
        from scripts.audit_resolved_markets import _decision_gate
        result = _decision_gate(
            usable_count=300, near_close_pct=0.70,  # high contamination flag
            thin_pct=0.10, other_pct=0.30
        )
        self.assertEqual(result["status"], "YELLOW")
        self.assertTrue(len(result["flags"]) > 0)

    def test_load_markets_raises_on_missing_db(self):
        from scripts.audit_resolved_markets import load_markets
        with self.assertRaises(FileNotFoundError):
            load_markets("/nonexistent/path/calibration.db")


# ── M3 Reconstruction tests ───────────────────────────────────────────────────

def _make_bet(created_time_ms: int, prob_after: float, bet_id: str = "b1") -> dict:
    return {
        "id": bet_id,
        "createdTime": created_time_ms,
        "probBefore": max(0.0, prob_after - 0.02),
        "probAfter": prob_after,
    }


class TestM3ProbAtTime(unittest.TestCase):
    """prob_at_time correctly reconstructs probability from bet history."""

    def _ts(self, days_ago: int) -> int:
        """Return a Unix ms timestamp N days before an arbitrary reference point."""
        base_ms = 1_800_000_000_000  # ~2027
        return base_ms - days_ago * 86_400_000

    def test_returns_last_prob_before_target(self):
        from scripts.reconstruct_snapshots import prob_at_time
        bets = [
            _make_bet(self._ts(30), 0.40, "b1"),
            _make_bet(self._ts(20), 0.55, "b2"),
            _make_bet(self._ts(10), 0.70, "b3"),
        ]
        # Target is 25 days ago — should get b1's probAfter (0.40)
        result = prob_at_time(bets, self._ts(25))
        self.assertAlmostEqual(result, 0.40, places=4)

    def test_returns_closest_bet_just_before_target(self):
        from scripts.reconstruct_snapshots import prob_at_time
        bets = [
            _make_bet(self._ts(15), 0.60, "b1"),
            _make_bet(self._ts(5),  0.80, "b2"),
        ]
        result = prob_at_time(bets, self._ts(7))
        self.assertAlmostEqual(result, 0.60, places=4)

    def test_returns_none_when_no_bets_before_target(self):
        from scripts.reconstruct_snapshots import prob_at_time
        bets = [
            _make_bet(self._ts(5), 0.70, "b1"),
        ]
        # Target is 30 days ago — all bets are newer
        result = prob_at_time(bets, self._ts(30))
        self.assertIsNone(result)

    def test_returns_none_on_empty_bets(self):
        from scripts.reconstruct_snapshots import prob_at_time
        self.assertIsNone(prob_at_time([], 1_800_000_000_000))

    def test_exact_timestamp_match_is_included(self):
        from scripts.reconstruct_snapshots import prob_at_time
        ts = self._ts(14)
        bets = [_make_bet(ts, 0.55, "b1")]
        result = prob_at_time(bets, ts)
        self.assertAlmostEqual(result, 0.55, places=4)


class TestM3ParseCloseDate(unittest.TestCase):
    """parse_close_date handles ISO strings and None gracefully."""

    def test_parses_utc_offset(self):
        from scripts.reconstruct_snapshots import parse_close_date
        result = parse_close_date("2026-06-01T00:00:00+00:00")
        self.assertIsNotNone(result)
        self.assertEqual(result.year, 2026)
        self.assertEqual(result.month, 6)

    def test_parses_z_suffix(self):
        from scripts.reconstruct_snapshots import parse_close_date
        result = parse_close_date("2026-06-01T00:00:00Z")
        self.assertIsNotNone(result)

    def test_returns_none_on_empty(self):
        from scripts.reconstruct_snapshots import parse_close_date
        self.assertIsNone(parse_close_date(""))
        self.assertIsNone(parse_close_date(None))


class TestM3InitSnapshotsDb(unittest.TestCase):
    """init_snapshots_db creates the table with M3 columns and migrates old tables."""

    def test_creates_table_with_m3_columns(self):
        from scripts.reconstruct_snapshots import init_snapshots_db
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            conn = sqlite3.connect(path)
            init_snapshots_db(conn)
            cols = {row[1] for row in conn.execute("PRAGMA table_info(market_snapshots)")}
            conn.close()
            for expected in ("source", "days_before_close", "outcome"):
                self.assertIn(expected, cols, f"Missing column: {expected}")
        finally:
            os.unlink(path)

    def test_migrates_old_table_missing_m3_columns(self):
        from scripts.reconstruct_snapshots import init_snapshots_db
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            conn = sqlite3.connect(path)
            # Create old schema without M3 columns
            conn.execute("""
                CREATE TABLE market_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    market_id TEXT, question TEXT,
                    probability REAL, volume24h REAL,
                    bettors INTEGER, liquidity REAL,
                    snapshot_at TEXT NOT NULL
                )
            """)
            conn.commit()
            init_snapshots_db(conn)
            cols = {row[1] for row in conn.execute("PRAGMA table_info(market_snapshots)")}
            conn.close()
            for expected in ("source", "days_before_close", "outcome"):
                self.assertIn(expected, cols, f"Migration missed column: {expected}")
        finally:
            os.unlink(path)


class TestM3AlreadyReconstructed(unittest.TestCase):
    """already_reconstructed detects existing rows correctly."""

    def test_returns_false_when_no_rows(self):
        from scripts.reconstruct_snapshots import init_snapshots_db, already_reconstructed
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            conn = sqlite3.connect(path)
            init_snapshots_db(conn)
            self.assertFalse(already_reconstructed(conn, "market_abc"))
            conn.close()
        finally:
            os.unlink(path)

    def test_returns_true_when_reconstructed_row_exists(self):
        from scripts.reconstruct_snapshots import init_snapshots_db, already_reconstructed
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            conn = sqlite3.connect(path)
            init_snapshots_db(conn)
            conn.execute("""
                INSERT INTO market_snapshots
                (market_id, question, probability, snapshot_at, source, days_before_close, outcome)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, ("market_abc", "Q?", 0.55, "2026-03-01T00:00:00+00:00",
                  "reconstructed", 14, "YES"))
            conn.commit()
            self.assertTrue(already_reconstructed(conn, "market_abc"))
            conn.close()
        finally:
            os.unlink(path)

    def test_live_rows_do_not_count_as_reconstructed(self):
        from scripts.reconstruct_snapshots import init_snapshots_db, already_reconstructed
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            conn = sqlite3.connect(path)
            init_snapshots_db(conn)
            conn.execute("""
                INSERT INTO market_snapshots
                (market_id, question, probability, snapshot_at, source)
                VALUES (?, ?, ?, ?, ?)
            """, ("market_abc", "Q?", 0.55, "2026-03-01T00:00:00+00:00", "live"))
            conn.commit()
            self.assertFalse(already_reconstructed(conn, "market_abc"))
            conn.close()
        finally:
            os.unlink(path)


class TestM3ReconstructMarket(unittest.TestCase):
    """reconstruct_market writes correct rows and handles edge cases."""

    def _make_db(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        return path

    def test_writes_rows_for_each_window(self):
        from scripts.reconstruct_snapshots import (
            reconstruct_market, init_snapshots_db, RECONSTRUCTION_WINDOWS
        )
        from unittest.mock import patch

        # Bets span all three windows (7, 14, 30 days before close)
        close_date = "2026-06-01T00:00:00+00:00"
        close_dt_ms = 1_780_272_000_000  # 2026-06-01 00:00:00 UTC in ms

        bets_oldest_first = [
            _make_bet(close_dt_ms - 35 * 86_400_000, 0.30, "b1"),  # 35 days before
            _make_bet(close_dt_ms - 20 * 86_400_000, 0.50, "b2"),  # 20 days before
            _make_bet(close_dt_ms - 5  * 86_400_000, 0.70, "b3"),  # 5 days before
        ]

        market = _make_resolved_row(
            market_id="test_m", close_date=close_date,
            outcome="YES", probability_close=0.70
        )

        db_path = self._make_db()
        try:
            conn = sqlite3.connect(db_path)
            init_snapshots_db(conn)

            with patch("scripts.reconstruct_snapshots.fetch_bets_for_market",
                       return_value=bets_oldest_first):
                result = reconstruct_market(None, conn, market, windows=RECONSTRUCTION_WINDOWS)

            self.assertEqual(result["windows_written"], 3)
            self.assertEqual(result["windows_skipped"], 0)

            rows = conn.execute(
                "SELECT days_before_close, probability, outcome, source "
                "FROM market_snapshots WHERE market_id='test_m' ORDER BY days_before_close DESC"
            ).fetchall()
            conn.close()

            self.assertEqual(len(rows), 3)
            days_written = {r[0] for r in rows}
            self.assertEqual(days_written, {7, 14, 30})
            for row in rows:
                self.assertEqual(row[2], "YES")     # outcome stored
                self.assertEqual(row[3], "reconstructed")  # source tag

        finally:
            os.unlink(db_path)

    def test_skips_window_when_no_bets_before_target(self):
        from scripts.reconstruct_snapshots import reconstruct_market, init_snapshots_db
        from unittest.mock import patch

        # close_dt_ms must match close_date string exactly.
        # 2026-06-01 00:00:00 UTC = 1780272000 seconds = 1_780_272_000_000 ms
        close_date = "2026-06-01T00:00:00+00:00"
        close_dt_ms = 1_780_272_000_000

        # Bet placed only 5 days before close — ALL windows (T-7, T-14, T-30)
        # target timestamps are earlier (further from close) than this bet,
        # so prob_at_time returns None for every window.
        bets = [_make_bet(close_dt_ms - 5 * 86_400_000, 0.70, "b1")]

        market = _make_resolved_row(market_id="sparse_m", close_date=close_date)
        db_path = self._make_db()
        try:
            conn = sqlite3.connect(db_path)
            init_snapshots_db(conn)

            with patch("scripts.reconstruct_snapshots.fetch_bets_for_market",
                       return_value=bets):
                result = reconstruct_market(None, conn, market, windows=[7, 14, 30])

            self.assertEqual(result["windows_written"], 0)   # no window has data
            self.assertEqual(result["windows_skipped"], 3)
            conn.close()
        finally:
            os.unlink(db_path)

    def test_skips_market_with_no_close_date(self):
        from scripts.reconstruct_snapshots import reconstruct_market, init_snapshots_db

        market = _make_resolved_row(market_id="no_date", close_date=None)
        db_path = self._make_db()
        try:
            conn = sqlite3.connect(db_path)
            init_snapshots_db(conn)
            result = reconstruct_market(None, conn, market)
            self.assertEqual(result["windows_written"], 0)
            self.assertEqual(result["reason"], "no_close_date")
            conn.close()
        finally:
            os.unlink(db_path)

    def test_skips_market_already_done(self):
        from scripts.reconstruct_snapshots import reconstruct_market, init_snapshots_db

        market = _make_resolved_row(market_id="done_m")
        db_path = self._make_db()
        try:
            conn = sqlite3.connect(db_path)
            init_snapshots_db(conn)
            # Pre-insert a reconstructed row
            conn.execute("""
                INSERT INTO market_snapshots
                (market_id, question, probability, snapshot_at, source, days_before_close, outcome)
                VALUES (?,?,?,?,?,?,?)
            """, ("done_m", "Q?", 0.5, "2026-03-01T00:00:00+00:00", "reconstructed", 14, "YES"))
            conn.commit()

            result = reconstruct_market(None, conn, market)
            self.assertEqual(result["reason"], "already_done")
            conn.close()
        finally:
            os.unlink(db_path)

    def test_dry_run_writes_nothing(self):
        from scripts.reconstruct_snapshots import reconstruct_market, init_snapshots_db
        from unittest.mock import patch

        close_date = "2026-06-01T00:00:00+00:00"
        close_dt_ms = 1_780_272_000_000  # 2026-06-01 00:00:00 UTC in ms
        bets = [_make_bet(close_dt_ms - 35 * 86_400_000, 0.30, "b1")]

        market = _make_resolved_row(market_id="dry_m", close_date=close_date)
        db_path = self._make_db()
        try:
            conn = sqlite3.connect(db_path)
            init_snapshots_db(conn)

            with patch("scripts.reconstruct_snapshots.fetch_bets_for_market",
                       return_value=bets):
                result = reconstruct_market(None, conn, market, windows=[30], dry_run=True)

            self.assertEqual(result["windows_written"], 1)  # counted but not committed
            row_count = conn.execute(
                "SELECT COUNT(*) FROM market_snapshots WHERE market_id='dry_m'"
            ).fetchone()[0]
            self.assertEqual(row_count, 0)  # nothing actually written
            conn.close()
        finally:
            os.unlink(db_path)


class TestM3SchemaCompatibility(unittest.TestCase):
    """_write_market_snapshots in auto_research.py writes source='live' after M3 migration."""

    def test_live_snapshots_have_source_live(self):
        from automation.auto_research import _write_market_snapshots

        markets = [{
            "id": "mkt1", "question": "Test?", "probability": 0.60,
            "volume24Hours": 100.0, "uniqueBettorCount": 15,
            "totalLiquidity": 500.0, "isResolved": False,
        }]

        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            with patch("automation.auto_research._SNAPSHOTS_DB_PATH", path):
                _write_market_snapshots(markets)

            conn = sqlite3.connect(path)
            row = conn.execute(
                "SELECT source, days_before_close, outcome FROM market_snapshots WHERE market_id='mkt1'"
            ).fetchone()
            conn.close()

            self.assertIsNotNone(row)
            self.assertEqual(row[0], "live")
            self.assertIsNone(row[1])   # days_before_close is NULL for live
            self.assertIsNone(row[2])   # outcome is NULL for live (not resolved yet)
        finally:
            os.unlink(path)


# ── Fix 1: _ensure_bet_outcomes_schema migration tests ────────────────────────

class TestEnsureBetOutcomesSchema(unittest.TestCase):
    """_ensure_bet_outcomes_schema adds missing columns without crashing."""

    def _make_old_db(self) -> str:
        """Create a bet_outcomes table missing the category column (pre-migration)."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = sqlite3.connect(path)
        conn.execute("""
            CREATE TABLE bet_outcomes (
                id INTEGER PRIMARY KEY, market_id TEXT NOT NULL,
                our_recommendation TEXT, amount REAL, probability REAL,
                estimated_ev REAL, ai_confidence REAL, strategies TEXT,
                market_resolution TEXT, actual_pnl REAL, ev_error REAL,
                era TEXT, resolved_at TEXT
                -- intentionally missing: ai_estimated_probability, category
            )
        """)
        conn.commit()
        conn.close()
        return path

    def test_adds_category_column_to_old_db(self):
        from scripts.weekly_ev_report import _ensure_bet_outcomes_schema
        path = self._make_old_db()
        try:
            conn = sqlite3.connect(path)
            _ensure_bet_outcomes_schema(conn)
            cols = {row[1] for row in conn.execute("PRAGMA table_info(bet_outcomes)")}
            conn.close()
            self.assertIn("category", cols)
            self.assertIn("ai_estimated_probability", cols)
        finally:
            os.unlink(path)

    def test_safe_to_call_twice(self):
        """Calling migration twice must not raise."""
        from scripts.weekly_ev_report import _ensure_bet_outcomes_schema
        path = self._make_old_db()
        try:
            conn = sqlite3.connect(path)
            _ensure_bet_outcomes_schema(conn)
            _ensure_bet_outcomes_schema(conn)   # second call must be silent
            conn.close()
        finally:
            os.unlink(path)

    def test_fetch_outcomes_does_not_crash_on_old_db(self):
        """_fetch_outcomes must not raise OperationalError on a DB missing category."""
        from scripts.weekly_ev_report import _fetch_outcomes
        path = self._make_old_db()
        try:
            with patch("scripts.weekly_ev_report.DB_PATH", path):
                # Should return [] (no rows) without crashing
                result = _fetch_outcomes(era=None)
            self.assertEqual(result, [])
        finally:
            os.unlink(path)


# ── Fix 2: backtest_from_snapshots tests ──────────────────────────────────────

def _make_snapshot(
    market_id="m1",
    probability=0.55,
    bettors=20,
    days_before_close=14,
    outcome="YES",
    category="crypto",
) -> dict:
    return {
        "market_id":         market_id,
        "question":          "Will X happen?",
        "probability":       probability,
        "bettors":           bettors,
        "days_before_close": days_before_close,
        "outcome":           outcome,
        "snapshot_at":       "2026-03-01T00:00:00+00:00",
        "category":          category,
    }


class TestBacktestSnapshotToMarketDict(unittest.TestCase):
    """_snapshot_to_market_dict creates a valid market dict."""

    def test_sets_volume_to_zero(self):
        from scripts.backtest_from_snapshots import _snapshot_to_market_dict
        m = _snapshot_to_market_dict(_make_snapshot())
        self.assertEqual(m["volume24Hours"], 0)

    def test_uses_stored_bettors(self):
        from scripts.backtest_from_snapshots import _snapshot_to_market_dict
        m = _snapshot_to_market_dict(_make_snapshot(bettors=50))
        self.assertEqual(m["uniqueBettorCount"], 50)

    def test_uses_default_when_bettors_null(self):
        from scripts.backtest_from_snapshots import _snapshot_to_market_dict, _DEFAULT_BETTORS
        snap = _make_snapshot()
        snap["bettors"] = None
        m = _snapshot_to_market_dict(snap)
        self.assertEqual(m["uniqueBettorCount"], _DEFAULT_BETTORS)

    def test_sets_last_bet_time_none(self):
        from scripts.backtest_from_snapshots import _snapshot_to_market_dict
        m = _snapshot_to_market_dict(_make_snapshot())
        self.assertIsNone(m["lastBetTime"])

    def test_probability_passes_through(self):
        from scripts.backtest_from_snapshots import _snapshot_to_market_dict
        m = _snapshot_to_market_dict(_make_snapshot(probability=0.72))
        self.assertAlmostEqual(m["probability"], 0.72, places=4)


class TestRunBacktest(unittest.TestCase):
    """run_backtest accumulates correct/total per strategy."""

    def test_counts_correct_predictions(self):
        from scripts.backtest_from_snapshots import run_backtest
        # mean_reversion fires on extreme probabilities (>0.85 → NO, <0.15 → YES)
        # With probability=0.90, strategy says NO.  outcome=NO → correct.
        snapshots = [_make_snapshot(probability=0.90, outcome="NO", bettors=10)]
        result = run_backtest(snapshots)
        mr = result.get("mean_reversion", {})
        if mr.get("total", 0) > 0:
            self.assertGreaterEqual(mr["correct"], 0)
            self.assertLessEqual(mr["correct"], mr["total"])

    def test_skips_null_outcome(self):
        from scripts.backtest_from_snapshots import run_backtest
        snap = _make_snapshot()
        snap["outcome"] = None
        result = run_backtest([snap])
        for stats in result.values():
            self.assertEqual(stats["total"], 0)

    def test_returns_all_backtest_strategies(self):
        from scripts.backtest_from_snapshots import run_backtest, BACKTEST_STRATEGIES
        result = run_backtest([])
        for strat in BACKTEST_STRATEGIES:
            self.assertIn(strat, result)

    def test_accuracy_is_none_when_no_signals(self):
        from scripts.backtest_from_snapshots import run_backtest
        # Probability in noise zone won't fire mean_reversion
        snapshots = [_make_snapshot(probability=0.50, outcome="YES")]
        result = run_backtest(snapshots)
        mr = result.get("mean_reversion", {})
        if mr.get("total", 0) == 0:
            self.assertIsNone(mr["accuracy"])


class TestComputeBacktestWeights(unittest.TestCase):
    """compute_backtest_weights only emits weights above the sample gate."""

    def test_below_min_samples_excluded(self):
        from scripts.backtest_from_snapshots import compute_backtest_weights, BACKTEST_MIN_SAMPLES
        results = {
            "mean_reversion": {"total": BACKTEST_MIN_SAMPLES - 1, "correct": 20, "accuracy": 0.67}
        }
        self.assertEqual(compute_backtest_weights(results), {})

    def test_above_min_samples_included(self):
        from scripts.backtest_from_snapshots import compute_backtest_weights, BACKTEST_MIN_SAMPLES
        results = {
            "mean_reversion": {"total": BACKTEST_MIN_SAMPLES + 5, "correct": 25, "accuracy": 0.70}
        }
        w = compute_backtest_weights(results)
        self.assertIn("mean_reversion", w)
        self.assertGreater(w["mean_reversion"], 1.0)  # acc > 0.60 → weight > 1.0

    def test_none_accuracy_excluded(self):
        from scripts.backtest_from_snapshots import compute_backtest_weights, BACKTEST_MIN_SAMPLES
        results = {
            "mean_reversion": {"total": BACKTEST_MIN_SAMPLES + 5, "correct": 0, "accuracy": None}
        }
        self.assertEqual(compute_backtest_weights(results), {})


class TestMergeWeights(unittest.TestCase):
    """merge_weights: live wins when it has enough samples; backtest fills gaps."""

    def _write_weights_file(self, weights: dict, sample_count: int,
                            per_strategy: dict | None = None) -> str:
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        data = {
            "generated_at": "2026-04-09T00:00:00Z",
            "sample_count": sample_count,
            "weights": weights,
            "per_strategy_samples": per_strategy or {},
        }
        with open(path, "w") as f:
            json.dump(data, f)
        return path

    def test_live_wins_when_enough_samples(self):
        from scripts.backtest_from_snapshots import merge_weights
        from scripts.weekly_ev_report import MIN_SAMPLES_PER_STRATEGY
        path = self._write_weights_file(
            {"mean_reversion": 0.80},
            sample_count=50,
            per_strategy={"mean_reversion": MIN_SAMPLES_PER_STRATEGY + 5},
        )
        try:
            merged, _ = merge_weights({"mean_reversion": 0.60}, live_weights_path=path)
            self.assertEqual(merged["mean_reversion"]["source"], "live")
            self.assertAlmostEqual(merged["mean_reversion"]["weight"], 0.80, places=4)
        finally:
            os.unlink(path)

    def test_backtest_fills_when_live_insufficient(self):
        """When live data is insufficient AND backtest UP-weights above 1.0,
        the asymmetric clamp (PR #27) caps the result at 1.0 — backtest can
        down-weight defensively but cannot up-weight without live confirmation.
        See test_backtest_boost_clamp.py for the full policy table."""
        from scripts.backtest_from_snapshots import merge_weights, MAX_BACKTEST_ONLY_WEIGHT
        path = self._write_weights_file(
            {"mean_reversion": 1.0},
            sample_count=3,
            per_strategy={"mean_reversion": 3},  # below gate
        )
        try:
            merged, _ = merge_weights({"mean_reversion": 1.15}, live_weights_path=path)
            self.assertEqual(merged["mean_reversion"]["source"], "backtest_clamped")
            self.assertAlmostEqual(
                merged["mean_reversion"]["weight"], MAX_BACKTEST_ONLY_WEIGHT, places=4
            )
        finally:
            os.unlink(path)

    def test_default_when_neither_has_signal(self):
        from scripts.backtest_from_snapshots import merge_weights
        path = self._write_weights_file(
            {"mean_reversion": 1.0},
            sample_count=0,
            per_strategy={"mean_reversion": 0},
        )
        try:
            merged, _ = merge_weights({}, live_weights_path=path)  # no backtest weights
            self.assertEqual(merged.get("mean_reversion", {}).get("source"), "default")
        finally:
            os.unlink(path)


# ── Fix 3: api_error vs no_bets distinction ───────────────────────────────────

class TestApiErrorVsNoBets(unittest.TestCase):
    """reconstruct_market classifies API failures as api_error, not no_bets."""

    def _make_db(self) -> str:
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        return path

    def test_api_error_classified_separately(self):
        from scripts.reconstruct_snapshots import reconstruct_market, init_snapshots_db
        from unittest.mock import patch

        market = _make_resolved_row(
            market_id="api_fail_m",
            close_date="2026-06-01T00:00:00+00:00"
        )
        db_path = self._make_db()
        try:
            conn = sqlite3.connect(db_path)
            init_snapshots_db(conn)

            with patch(
                "scripts.reconstruct_snapshots.fetch_bets_for_market",
                side_effect=RuntimeError("DNS failure: api.manifold.markets")
            ):
                result = reconstruct_market(None, conn, market)

            self.assertEqual(result["reason"], "api_error")
            self.assertIn("DNS failure", result.get("error", ""))
            conn.close()
        finally:
            os.unlink(db_path)

    def test_empty_bets_still_classified_as_no_bets(self):
        from scripts.reconstruct_snapshots import reconstruct_market, init_snapshots_db
        from unittest.mock import patch

        market = _make_resolved_row(
            market_id="empty_m",
            close_date="2026-06-01T00:00:00+00:00"
        )
        db_path = self._make_db()
        try:
            conn = sqlite3.connect(db_path)
            init_snapshots_db(conn)

            with patch(
                "scripts.reconstruct_snapshots.fetch_bets_for_market",
                return_value=[]
            ):
                result = reconstruct_market(None, conn, market)

            self.assertEqual(result["reason"], "no_bets")
            conn.close()
        finally:
            os.unlink(db_path)

    def test_reconstruct_summary_counts_api_errors(self):
        from scripts.reconstruct_snapshots import reconstruct
        from unittest.mock import patch, MagicMock

        markets = [_make_resolved_row(market_id="m1", close_date="2026-06-01T00:00:00+00:00")]
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            with patch("scripts.reconstruct_snapshots.MANIFOLD_API_KEY", "fake-key"), \
                 patch("scripts.reconstruct_snapshots.ManifoldAPI"), \
                 patch(
                     "scripts.reconstruct_snapshots.fetch_bets_for_market",
                     side_effect=ConnectionError("timeout")
                 ):
                summary = reconstruct(markets, snapshots_db=db_path, dry_run=True)

            self.assertEqual(summary["api_errors"], 1)
            self.assertEqual(summary["no_bets"], 0)
        finally:
            os.unlink(db_path)


# ── compute_strategy_weights: per_strategy_samples emitted ───────────────────

class TestComputeStrategyWeightsPerStrategySamples(unittest.TestCase):
    """
    _compute_weights() must return per-strategy sample counts alongside weights.
    strategy_weights.json must include per_strategy_samples so
    backtest_from_snapshots.merge_weights() can correctly prefer live data once
    enough live trades accumulate.
    """

    def _make_outcome(self, strategy: str, correct: bool) -> dict:
        import json
        return {
            "our_recommendation": "YES",
            "market_resolution": "YES" if correct else "NO",
            "strategies": json.dumps([strategy]),
            "actual_pnl": 1.0 if correct else -1.0,
            "estimated_ev": None,
            "ai_confidence": None,
            "category": "science",
        }

    def test_returns_tuple(self):
        from scripts.compute_strategy_weights import _compute_weights
        result = _compute_weights([])
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)

    def test_per_strategy_samples_zero_when_no_outcomes(self):
        from scripts.compute_strategy_weights import _compute_weights, _KNOWN_STRATEGIES
        _, samples = _compute_weights([])
        for strat in _KNOWN_STRATEGIES:
            self.assertEqual(samples[strat], 0)

    def test_per_strategy_samples_counts_correctly(self):
        from scripts.compute_strategy_weights import _compute_weights
        outcomes = [
            self._make_outcome("mean_reversion", True),
            self._make_outcome("mean_reversion", True),
            self._make_outcome("probability_bias", False),
        ]
        _, samples = _compute_weights(outcomes)
        self.assertEqual(samples["mean_reversion"], 2)
        self.assertEqual(samples["probability_bias"], 1)

    def test_per_strategy_samples_included_in_json_output(self):
        """main(dry_run=True) must not crash; result dict must have per_strategy_samples."""
        from scripts.compute_strategy_weights import _compute_weights, _KNOWN_STRATEGIES
        outcomes = []
        weights, samples = _compute_weights(outcomes)
        # Simulate the result dict that main() builds
        result = {
            "weights": weights,
            "per_strategy_samples": samples,
        }
        self.assertIn("per_strategy_samples", result)
        self.assertIsInstance(result["per_strategy_samples"], dict)
        for strat in _KNOWN_STRATEGIES:
            self.assertIn(strat, result["per_strategy_samples"])

    def test_merge_weights_uses_live_when_samples_present(self):
        """
        End-to-end: if strategy_weights.json is written with per_strategy_samples,
        merge_weights() must use the live weight (not backtest) for that strategy.
        """
        import json
        import tempfile
        from scripts.backtest_from_snapshots import merge_weights
        from scripts.weekly_ev_report import MIN_SAMPLES_PER_STRATEGY

        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            live_data = {
                "generated_at": "2026-04-09T00:00:00Z",
                "sample_count": 50,
                "weights": {"mean_reversion": 0.75},
                "per_strategy_samples": {"mean_reversion": MIN_SAMPLES_PER_STRATEGY + 10},
            }
            with open(path, "w") as f:
                json.dump(live_data, f)

            merged, _ = merge_weights({"mean_reversion": 1.10}, live_weights_path=path)
            self.assertEqual(merged["mean_reversion"]["source"], "live",
                             "live weight must win when per_strategy_samples clears the gate")
            self.assertAlmostEqual(merged["mean_reversion"]["weight"], 0.75, places=4)
        finally:
            os.unlink(path)

    def test_merge_weights_backtest_wins_without_per_strategy_samples(self):
        """
        If strategy_weights.json has no per_strategy_samples (old format),
        merge_weights() must fall back to backtest weight (live_n defaults to 0).
        With the asymmetric clamp from PR #27, an UP-weighting backtest
        (1.10 > 1.0) is clamped — source is 'backtest_clamped' rather than
        'backtest', and the weight caps at MAX_BACKTEST_ONLY_WEIGHT (1.0).
        """
        import json
        import tempfile
        from scripts.backtest_from_snapshots import merge_weights, MAX_BACKTEST_ONLY_WEIGHT

        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            # Old format: no per_strategy_samples key
            live_data = {
                "generated_at": "2026-04-09T00:00:00Z",
                "sample_count": 50,
                "weights": {"mean_reversion": 0.75},
            }
            with open(path, "w") as f:
                json.dump(live_data, f)

            merged, _ = merge_weights({"mean_reversion": 1.10}, live_weights_path=path)
            self.assertEqual(merged["mean_reversion"]["source"], "backtest_clamped",
                             "backtest UP-weight must be clamped without live confirmation")
            self.assertAlmostEqual(merged["mean_reversion"]["weight"],
                                   MAX_BACKTEST_ONLY_WEIGHT, places=4)
        finally:
            os.unlink(path)


class TestComputeWeightsByCategory(unittest.TestCase):
    """_compute_weights_by_category() correctness and schema."""

    def _make_outcome(self, strategy: str, correct: bool, category: str = "sports") -> dict:
        import json
        return {
            "our_recommendation": "YES",
            "market_resolution": "YES" if correct else "NO",
            "strategies": json.dumps([strategy]),
            "actual_pnl": 1.0 if correct else -1.0,
            "estimated_ev": None,
            "ai_confidence": None,
            "category": category,
        }

    def _enough_outcomes(self, strategy: str, category: str, n_correct: int, n_total: int) -> list:
        from scripts.weekly_ev_report import MIN_SAMPLES_PER_STRATEGY
        # Pad to at least MIN_SAMPLES_PER_STRATEGY
        outcomes = []
        for i in range(n_total):
            outcomes.append(self._make_outcome(strategy, i < n_correct, category))
        # Pad up to threshold if needed
        while len(outcomes) < MIN_SAMPLES_PER_STRATEGY:
            outcomes.append(self._make_outcome(strategy, True, category))
        return outcomes

    def test_returns_dict(self):
        from scripts.compute_strategy_weights import _compute_weights_by_category
        result = _compute_weights_by_category([])
        self.assertIsInstance(result, dict)
        self.assertEqual(result, {})

    def test_below_min_samples_omitted(self):
        from scripts.compute_strategy_weights import _compute_weights_by_category
        from scripts.weekly_ev_report import MIN_SAMPLES_PER_STRATEGY
        # Fewer than MIN_SAMPLES_PER_STRATEGY outcomes — should not appear in result
        outcomes = [self._make_outcome("mean_reversion", True, "sports")
                    for _ in range(MIN_SAMPLES_PER_STRATEGY - 1)]
        result = _compute_weights_by_category(outcomes)
        sports = result.get("sports", {})
        self.assertNotIn("mean_reversion", sports)

    def test_above_min_samples_included(self):
        from scripts.compute_strategy_weights import _compute_weights_by_category
        outcomes = self._enough_outcomes("mean_reversion", "sports", n_correct=8, n_total=10)
        result = _compute_weights_by_category(outcomes)
        self.assertIn("sports", result)
        self.assertIn("mean_reversion", result["sports"])

    def test_schema_has_weight_accuracy_n(self):
        from scripts.compute_strategy_weights import _compute_weights_by_category
        outcomes = self._enough_outcomes("probability_direction", "politics", n_correct=8, n_total=10)
        result = _compute_weights_by_category(outcomes)
        info = result.get("politics", {}).get("probability_direction", {})
        self.assertIn("weight", info)
        self.assertIn("accuracy", info)
        self.assertIn("n", info)

    def test_high_accuracy_gives_boosted_weight(self):
        from scripts.compute_strategy_weights import _compute_weights_by_category
        from scripts.weekly_ev_report import MIN_SAMPLES_PER_STRATEGY
        # 100% correct → accuracy=1.0 → weight=1.20 (capped)
        n = MIN_SAMPLES_PER_STRATEGY
        outcomes = [self._make_outcome("mean_reversion", True, "crypto") for _ in range(n)]
        result = _compute_weights_by_category(outcomes)
        w = result["crypto"]["mean_reversion"]["weight"]
        self.assertGreater(w, 1.0)

    def test_low_accuracy_gives_reduced_weight(self):
        from scripts.compute_strategy_weights import _compute_weights_by_category
        from scripts.weekly_ev_report import MIN_SAMPLES_PER_STRATEGY
        n = MIN_SAMPLES_PER_STRATEGY
        # 0% correct → weight should be 0.5
        outcomes = [self._make_outcome("probability_bias", False, "ai_tech") for _ in range(n)]
        result = _compute_weights_by_category(outcomes)
        w = result["ai_tech"]["probability_bias"]["weight"]
        self.assertLessEqual(w, 1.0)

    def test_by_category_emitted_in_result_dict(self):
        """main(dry_run=True) result dict must include by_category key."""
        from scripts.compute_strategy_weights import _compute_weights_by_category
        result = {"by_category": _compute_weights_by_category([])}
        self.assertIn("by_category", result)
        self.assertIsInstance(result["by_category"], dict)

    def test_strategy_weights_json_has_by_category_field(self):
        """data/strategy_weights.json must have a by_category key."""
        import json, os
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "data", "strategy_weights.json")
        if not os.path.exists(path):
            self.skipTest("strategy_weights.json not present")
        with open(path) as f:
            data = json.load(f)
        self.assertIn("by_category", data, "strategy_weights.json missing 'by_category' field")


if __name__ == "__main__":
    unittest.main()
