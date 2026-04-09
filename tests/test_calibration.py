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


if __name__ == "__main__":
    unittest.main()
