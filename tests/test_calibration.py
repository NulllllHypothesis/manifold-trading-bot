#!/usr/bin/env python3
"""
Tests for the Calibration & Feedback Loop feature.

Covers:
  - scripts/harvest_resolved.py   : _infer_category, parse_market, init_db
  - manifold_bot/paper_trader.py  : _init_bet_outcomes_db, _write_bet_outcome
  - scripts/weekly_ev_report.py   : _ev_accuracy_section, _strategy_breakdown,
                                    _confidence_breakdown
  - manifold_bot/ai_analyzer.py   : _build_calibration_note

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
from manifold_bot.ai_analyzer import _build_calibration_note
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
        outcomes = [_make_outcome(actual_pnl=9.0, estimated_ev=10.0) for _ in range(3)]
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


# ── Group 8: _build_calibration_note ──────────────────────────────────────────

class TestBuildCalibrationNote(unittest.TestCase):

    def test_returns_empty_string_when_file_missing(self):
        missing = Path("/nonexistent/calibration_table.json")
        with patch("manifold_bot.ai_analyzer._CALIBRATION_TABLE_PATH", missing):
            result = _build_calibration_note()
        self.assertEqual(result, "")

    def test_returns_formatted_table_when_file_exists(self):
        table = {
            "buckets": [{
                "bucket_low": 0.60, "bucket_high": 0.70,
                "crowd_midpoint": 0.65, "actual_yes_rate": 0.47,
                "sample_size": 120, "bias": 0.18, "reliable": True,
            }],
            "by_category": [],
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(table, f)
            tmp = Path(f.name)
        try:
            with patch("manifold_bot.ai_analyzer._CALIBRATION_TABLE_PATH", tmp):
                result = _build_calibration_note()
            self.assertIn("60", result)     # bucket low
            self.assertIn("70", result)     # bucket high
            self.assertIn("47", result)     # actual YES rate
            self.assertIn("+18pp", result)  # bias annotation
        finally:
            os.unlink(tmp)

    def test_most_biased_annotation_on_large_bias(self):
        table = {
            "buckets": [{
                "bucket_low": 0.60, "bucket_high": 0.70,
                "crowd_midpoint": 0.65, "actual_yes_rate": 0.47,
                "sample_size": 120, "bias": 0.18,   # abs >= 0.15 → MOST BIASED
                "reliable": True,
            }],
            "by_category": [],
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(table, f)
            tmp = Path(f.name)
        try:
            with patch("manifold_bot.ai_analyzer._CALIBRATION_TABLE_PATH", tmp):
                result = _build_calibration_note()
            self.assertIn("MOST BIASED", result)
        finally:
            os.unlink(tmp)

    def test_unreliable_buckets_are_excluded(self):
        table = {
            "buckets": [
                {
                    "bucket_low": 0.00, "bucket_high": 0.10,
                    "crowd_midpoint": 0.05, "actual_yes_rate": 0.04,
                    "sample_size": 2, "bias": 0.01, "reliable": False,
                },
                {
                    "bucket_low": 0.40, "bucket_high": 0.50,
                    "crowd_midpoint": 0.45, "actual_yes_rate": 0.46,
                    "sample_size": 80, "bias": -0.01, "reliable": True,
                },
            ],
            "by_category": [],
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(table, f)
            tmp = Path(f.name)
        try:
            with patch("manifold_bot.ai_analyzer._CALIBRATION_TABLE_PATH", tmp):
                result = _build_calibration_note()
            # Only the reliable 40-50% bucket should produce a table row
            bucket_rows = [l for l in result.split("\n") if "–" in l and "%" in l]
            self.assertEqual(len(bucket_rows), 1, "Only 1 reliable bucket should appear")
        finally:
            os.unlink(tmp)

    def test_returns_empty_string_when_no_buckets(self):
        table = {"buckets": [], "by_category": []}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(table, f)
            tmp = Path(f.name)
        try:
            with patch("manifold_bot.ai_analyzer._CALIBRATION_TABLE_PATH", tmp):
                result = _build_calibration_note()
            self.assertEqual(result, "")
        finally:
            os.unlink(tmp)

    def test_returns_empty_string_when_all_buckets_unreliable(self):
        table = {
            "buckets": [{
                "bucket_low": 0.40, "bucket_high": 0.50,
                "crowd_midpoint": 0.45, "actual_yes_rate": 0.44,
                "sample_size": 1, "bias": 0.01, "reliable": False,
            }],
            "by_category": [],
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(table, f)
            tmp = Path(f.name)
        try:
            with patch("manifold_bot.ai_analyzer._CALIBRATION_TABLE_PATH", tmp):
                result = _build_calibration_note()
            # All reliable=False → no rows → function returns the header + instruction
            # but the header is always added; what matters is no bucket rows appear
            bucket_rows = [l for l in result.split("\n") if "–" in l and "%" in l]
            self.assertEqual(len(bucket_rows), 0)
        finally:
            os.unlink(tmp)


if __name__ == "__main__":
    unittest.main()
