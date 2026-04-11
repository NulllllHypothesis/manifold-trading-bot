#!/usr/bin/env python3
"""
Op4 — Tests for live snapshot days_before_close computation and backfill.

Covers
------
  - _write_market_snapshots(): new rows get correct days_before_close from
    closeTime at snapshot time; missing/malformed closeTime → None; already-
    closed markets read as 0 (not negative).
  - backfill_days_before_close._days_before(): integer-day floor math.
  - backfill_days_before_close._load_resolved_close_ms_map(): reads
    resolved_markets.close_date and converts to unix ms.
  - build_training_dataset.load_examples(): one live row kept per
    (market_id, window), window ∈ {7, 14, 30}; other windows skipped.
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.backfill_days_before_close import (
    _days_before,
    _snapshot_at_to_ms,
    _load_resolved_close_ms_map,
    backfill,
    _MS_PER_DAY,
)
from scripts.build_training_dataset import (
    load_examples,
    LIVE_DECISION_WINDOWS,
)
import automation.auto_research as auto_research


# ── _days_before math ─────────────────────────────────────────────────────────

class TestDaysBeforeMath(unittest.TestCase):

    def test_exact_7_days(self):
        snap = 1_700_000_000_000
        close = snap + 7 * _MS_PER_DAY
        self.assertEqual(_days_before(close, snap), 7)

    def test_floor_to_7_days(self):
        """A snapshot 7.5 days before close floors to 7, not 8."""
        snap = 1_700_000_000_000
        close = snap + 7 * _MS_PER_DAY + 12 * 60 * 60 * 1000  # 7d 12h
        self.assertEqual(_days_before(close, snap), 7)

    def test_already_closed_clamps_to_zero(self):
        """A snapshot taken AFTER close reads as 0, not negative."""
        snap = 1_700_000_000_000
        close = snap - 3 * _MS_PER_DAY
        self.assertEqual(_days_before(close, snap), 0)

    def test_exact_close_moment(self):
        snap = 1_700_000_000_000
        self.assertEqual(_days_before(snap, snap), 0)

    def test_30_day_window(self):
        snap = 1_700_000_000_000
        close = snap + 30 * _MS_PER_DAY
        self.assertEqual(_days_before(close, snap), 30)


class TestSnapshotAtToMs(unittest.TestCase):

    def test_valid_iso(self):
        dt = datetime(2026, 4, 11, 9, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(_snapshot_at_to_ms(dt.isoformat()), int(dt.timestamp() * 1000))

    def test_malformed_returns_none(self):
        self.assertIsNone(_snapshot_at_to_ms("not-a-date"))

    def test_empty_returns_none(self):
        self.assertIsNone(_snapshot_at_to_ms(""))


# ── _write_market_snapshots days_before_close ────────────────────────────────

class TestWriteMarketSnapshotsDBC(unittest.TestCase):
    """Op4: live rows must get days_before_close from closeTime at snapshot time."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self._patcher = patch(
            "automation.auto_research._SNAPSHOTS_DB_PATH",
            self.tmp.name,
        )
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        if os.path.exists(self.tmp.name):
            os.unlink(self.tmp.name)

    def _fetch_rows(self) -> list[tuple]:
        conn = sqlite3.connect(self.tmp.name)
        try:
            return conn.execute(
                "SELECT market_id, days_before_close FROM market_snapshots ORDER BY market_id"
            ).fetchall()
        finally:
            conn.close()

    def _write_with_fake_now(self, markets: list[dict], now: datetime) -> None:
        """
        Call _write_market_snapshots with datetime.now() pinned to `now`.
        The module uses datetime.now(timezone.utc); we can't monkeypatch the
        class method portably, so we freeze auto_research's datetime reference.
        """
        class _FakeDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return now
        with patch("automation.auto_research.datetime", _FakeDatetime):
            auto_research._write_market_snapshots(markets)

    def test_7_days_exact(self):
        now = datetime(2026, 4, 11, 9, 0, 0, tzinfo=timezone.utc)
        close_ms = int((now + timedelta(days=7)).timestamp() * 1000)
        self._write_with_fake_now([{
            "id": "mkt_a",
            "question": "Will X?",
            "probability": 0.5,
            "closeTime": close_ms,
        }], now)
        rows = self._fetch_rows()
        self.assertEqual(rows, [("mkt_a", 7)])

    def test_30_days_exact(self):
        now = datetime(2026, 4, 11, 9, 0, 0, tzinfo=timezone.utc)
        close_ms = int((now + timedelta(days=30)).timestamp() * 1000)
        self._write_with_fake_now([{
            "id": "mkt_b",
            "question": "Will Y?",
            "probability": 0.5,
            "closeTime": close_ms,
        }], now)
        self.assertEqual(self._fetch_rows(), [("mkt_b", 30)])

    def test_missing_closetime_is_none(self):
        now = datetime(2026, 4, 11, 9, 0, 0, tzinfo=timezone.utc)
        self._write_with_fake_now([{
            "id": "mkt_c",
            "question": "Will Z?",
            "probability": 0.5,
            # no closeTime
        }], now)
        self.assertEqual(self._fetch_rows(), [("mkt_c", None)])

    def test_malformed_closetime_is_none(self):
        now = datetime(2026, 4, 11, 9, 0, 0, tzinfo=timezone.utc)
        self._write_with_fake_now([{
            "id": "mkt_d",
            "question": "Will W?",
            "probability": 0.5,
            "closeTime": "not a number",
        }], now)
        self.assertEqual(self._fetch_rows(), [("mkt_d", None)])

    def test_already_closed_clamps_to_zero(self):
        now = datetime(2026, 4, 11, 9, 0, 0, tzinfo=timezone.utc)
        close_ms = int((now - timedelta(days=5)).timestamp() * 1000)
        self._write_with_fake_now([{
            "id": "mkt_e",
            "question": "Already done",
            "probability": 0.5,
            "closeTime": close_ms,
        }], now)
        self.assertEqual(self._fetch_rows(), [("mkt_e", 0)])

    def test_resolved_markets_are_skipped(self):
        """isResolved=True markets are filtered out entirely — no row inserted."""
        now = datetime(2026, 4, 11, 9, 0, 0, tzinfo=timezone.utc)
        close_ms = int((now + timedelta(days=10)).timestamp() * 1000)
        self._write_with_fake_now([
            {"id": "mkt_f", "question": "resolved", "probability": 0.5,
             "closeTime": close_ms, "isResolved": True},
            {"id": "mkt_g", "question": "open",     "probability": 0.5,
             "closeTime": close_ms},
        ], now)
        self.assertEqual(self._fetch_rows(), [("mkt_g", 10)])


# ── _load_resolved_close_ms_map ──────────────────────────────────────────────

class TestLoadResolvedCloseMsMap(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        # Create a resolved_markets table with the fields the loader expects
        conn = sqlite3.connect(self.tmp.name)
        conn.execute("""
            CREATE TABLE resolved_markets (
                market_id  TEXT PRIMARY KEY,
                question   TEXT,
                probability_close REAL,
                outcome    TEXT,
                close_date TEXT,
                unique_bettors INTEGER,
                category   TEXT
            )
        """)
        self.close_dt = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
        conn.executemany(
            "INSERT INTO resolved_markets (market_id, question, close_date) VALUES (?, ?, ?)",
            [
                ("mkt_good", "Q1", self.close_dt.isoformat()),
                ("mkt_bad",  "Q2", "not a date"),
                ("mkt_null", "Q3", None),
            ],
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        if os.path.exists(self.tmp.name):
            os.unlink(self.tmp.name)

    def test_parses_iso_to_ms(self):
        with patch("scripts.backfill_days_before_close.CALIBRATION_DB", self.tmp.name):
            m = _load_resolved_close_ms_map()
        self.assertIn("mkt_good", m)
        self.assertEqual(m["mkt_good"], int(self.close_dt.timestamp() * 1000))

    def test_malformed_dates_dropped(self):
        with patch("scripts.backfill_days_before_close.CALIBRATION_DB", self.tmp.name):
            m = _load_resolved_close_ms_map()
        self.assertNotIn("mkt_bad", m)
        # NULL close_date rows are filtered out by the WHERE clause
        self.assertNotIn("mkt_null", m)

    def test_missing_db_returns_empty(self):
        with patch("scripts.backfill_days_before_close.CALIBRATION_DB", "/nonexistent.db"):
            self.assertEqual(_load_resolved_close_ms_map(), {})


# ── backfill() end-to-end against in-memory DBs ──────────────────────────────

class TestBackfillEndToEnd(unittest.TestCase):

    def setUp(self):
        # Fake snapshots DB with two live NULL rows
        self.snap_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.snap_tmp.close()
        conn = sqlite3.connect(self.snap_tmp.name)
        conn.execute("""
            CREATE TABLE market_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT,
                question TEXT,
                probability REAL,
                volume24h REAL,
                bettors INTEGER,
                liquidity REAL,
                snapshot_at TEXT,
                source TEXT,
                days_before_close INTEGER,
                outcome TEXT
            )
        """)
        snap_dt_a = datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc)
        snap_dt_b = datetime(2026, 4, 5, 12, 0, 0, tzinfo=timezone.utc)
        # Also an already-backfilled row to confirm idempotency
        snap_dt_c = datetime(2026, 4, 3, 12, 0, 0, tzinfo=timezone.utc)
        conn.executemany(
            "INSERT INTO market_snapshots (market_id, probability, snapshot_at, source, days_before_close) VALUES (?, ?, ?, ?, ?)",
            [
                ("mkt_resolvable", 0.5, snap_dt_a.isoformat(), "live", None),
                ("mkt_resolvable", 0.5, snap_dt_b.isoformat(), "live", None),
                ("mkt_untouched",  0.5, snap_dt_c.isoformat(), "live", 99),  # has value — must not be overwritten
                ("mkt_unknown",    0.5, snap_dt_a.isoformat(), "live", None),
            ],
        )
        conn.commit()
        conn.close()

        # Fake calibration DB with resolved_markets.close_date
        self.cal_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.cal_tmp.close()
        conn = sqlite3.connect(self.cal_tmp.name)
        conn.execute("""
            CREATE TABLE resolved_markets (
                market_id TEXT PRIMARY KEY,
                question TEXT,
                probability_close REAL,
                outcome TEXT,
                close_date TEXT,
                unique_bettors INTEGER,
                category TEXT
            )
        """)
        # mkt_resolvable closes 2026-04-20 → 19 days after 04-01 and 15 days after 04-05
        self.close_dt = datetime(2026, 4, 20, 12, 0, 0, tzinfo=timezone.utc)
        conn.execute(
            "INSERT INTO resolved_markets (market_id, close_date) VALUES (?, ?)",
            ("mkt_resolvable", self.close_dt.isoformat()),
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        for p in (self.snap_tmp.name, self.cal_tmp.name):
            if os.path.exists(p):
                os.unlink(p)

    def test_writes_correct_values_and_skips_already_set(self):
        with patch("scripts.backfill_days_before_close.CALIBRATION_DB", self.cal_tmp.name):
            stats = backfill(
                snapshots_db=self.snap_tmp.name,
                dry_run=False,
                use_api=False,   # mkt_unknown has no resolved_markets entry → unresolvable
            )

        self.assertEqual(stats["total"], 3)              # the untouched row is excluded by the WHERE clause
        self.assertEqual(stats["resolved_by_db"], 2)
        self.assertEqual(stats["unresolvable"], 1)
        self.assertEqual(stats["updated"], 2)

        conn = sqlite3.connect(self.snap_tmp.name)
        rows = conn.execute(
            "SELECT market_id, snapshot_at, days_before_close FROM market_snapshots ORDER BY id"
        ).fetchall()
        conn.close()

        # Row 1: 2026-04-01 → 2026-04-20 = 19 days
        self.assertEqual(rows[0][2], 19)
        # Row 2: 2026-04-05 → 2026-04-20 = 15 days
        self.assertEqual(rows[1][2], 15)
        # Row 3: already-backfilled, never touched
        self.assertEqual(rows[2][2], 99)
        # Row 4: unresolvable, still NULL
        self.assertIsNone(rows[3][2])

    def test_dry_run_writes_nothing(self):
        with patch("scripts.backfill_days_before_close.CALIBRATION_DB", self.cal_tmp.name):
            stats = backfill(
                snapshots_db=self.snap_tmp.name,
                dry_run=True,
                use_api=False,
            )
        self.assertEqual(stats["updated"], 2)  # counted as would-be-updated
        conn = sqlite3.connect(self.snap_tmp.name)
        rows = conn.execute(
            "SELECT days_before_close FROM market_snapshots WHERE market_id = 'mkt_resolvable'"
        ).fetchall()
        conn.close()
        # dry-run must not have written values
        for (dbc,) in rows:
            self.assertIsNone(dbc)


# ── build_training_dataset live-row filtering ────────────────────────────────

class TestLiveRowFiltering(unittest.TestCase):
    """
    load_examples() must pick at most one live row per (market_id, window)
    where window ∈ {7, 14, 30}, mirroring M3's decision-time semantics.
    """

    def setUp(self):
        # Snapshots DB with multiple live rows per market across several windows
        self.snap_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.snap_tmp.close()
        conn = sqlite3.connect(self.snap_tmp.name)
        conn.execute("""
            CREATE TABLE market_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT,
                question TEXT,
                probability REAL,
                volume24h REAL,
                bettors INTEGER,
                liquidity REAL,
                snapshot_at TEXT,
                source TEXT,
                days_before_close INTEGER,
                outcome TEXT
            )
        """)
        # mkt_live: 4 snapshots — two at T-7, one at T-14, one at T-9 (off-window)
        base = datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc)
        conn.executemany(
            """INSERT INTO market_snapshots
               (market_id, question, probability, bettors, liquidity,
                snapshot_at, source, days_before_close, outcome)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                ("mkt_live", "Will X?", 0.40, 5, 500,
                 (base + timedelta(hours=0)).isoformat(), "live", 7, None),
                ("mkt_live", "Will X?", 0.45, 5, 500,
                 (base + timedelta(hours=2)).isoformat(), "live", 7, None),
                ("mkt_live", "Will X?", 0.50, 5, 500,
                 (base + timedelta(hours=4)).isoformat(), "live", 14, None),
                ("mkt_live", "Will X?", 0.48, 5, 500,
                 (base + timedelta(hours=6)).isoformat(), "live", 9, None),
                # Skip: off-window
                ("mkt_live", "Will X?", 0.52, 5, 500,
                 (base + timedelta(hours=8)).isoformat(), "live", 25, None),
            ],
        )
        conn.commit()
        conn.close()

        # Calibration DB: mkt_live resolved YES
        self.cal_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.cal_tmp.close()
        conn = sqlite3.connect(self.cal_tmp.name)
        conn.execute("""
            CREATE TABLE resolved_markets (
                market_id TEXT PRIMARY KEY,
                question TEXT,
                probability_close REAL,
                outcome TEXT,
                close_date TEXT,
                unique_bettors INTEGER,
                category TEXT
            )
        """)
        conn.execute(
            "INSERT INTO resolved_markets (market_id, question, outcome, category) VALUES (?, ?, ?, ?)",
            ("mkt_live", "Will X?", "YES", "other"),
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        for p in (self.snap_tmp.name, self.cal_tmp.name):
            if os.path.exists(p):
                os.unlink(p)

    def test_decision_windows_constant(self):
        self.assertEqual(set(LIVE_DECISION_WINDOWS), {7, 14, 30})

    def test_one_row_per_window(self):
        examples = load_examples(
            snapshots_db=self.snap_tmp.name,
            calibration_db=self.cal_tmp.name,
        )
        # Expect exactly 2 rows: T-7 (earliest of the two) and T-14
        self.assertEqual(len(examples), 2)
        windows = sorted(e["days_before_close"] for e in examples)
        self.assertEqual(windows, [7, 14])

    def test_earliest_snapshot_per_window_wins(self):
        """The T-7 row kept must be the FIRST chronological 7d observation."""
        examples = load_examples(
            snapshots_db=self.snap_tmp.name,
            calibration_db=self.cal_tmp.name,
        )
        t7 = next(e for e in examples if e["days_before_close"] == 7)
        # First row had probability=0.40; second had 0.45 — we want the 0.40
        self.assertAlmostEqual(t7["crowd_probability"], 0.40, places=3)

    def test_offwindow_rows_are_skipped(self):
        examples = load_examples(
            snapshots_db=self.snap_tmp.name,
            calibration_db=self.cal_tmp.name,
        )
        for e in examples:
            self.assertIn(e["days_before_close"], LIVE_DECISION_WINDOWS)

    def test_example_id_includes_window(self):
        examples = load_examples(
            snapshots_db=self.snap_tmp.name,
            calibration_db=self.cal_tmp.name,
        )
        ids = {e["example_id"] for e in examples}
        self.assertIn("mkt_live__live__7",  ids)
        self.assertIn("mkt_live__live__14", ids)


if __name__ == "__main__":
    unittest.main()
