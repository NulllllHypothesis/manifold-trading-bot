#!/usr/bin/env python3
"""Tests for Polymarket ingestion (read-only, write-side only).

Three layers covered:
  1. polymarket_api.py — HTTP client surface, with requests mocked.
  2. markets_normalized.py — schema regression + upsert + Polymarket
     normalize round-trip on a fixture that mirrors the real API shape.
  3. ingest_polymarket.py — end-to-end smoke against mocked API + temp DB.

We never hit the real Polymarket API in tests. The fixture in this file
matches the field shape of one verified live response (2026-05-06).
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from manifold_bot import polymarket_api
from manifold_bot.markets_normalized import (
    init_db,
    normalize_polymarket_market,
    upsert_markets,
)


# ── Fixture: one real-shape Polymarket market dict ──────────────────────────

def _sample_market(market_id: str = "540816", **overrides) -> dict:
    base = {
        "id": market_id,
        "slug": f"sample-market-{market_id}",
        "question": "Will X happen by year-end?",
        "outcomes": ["Yes", "No"],
        "outcomePrices": ["0.555", "0.445"],
        "lastTradePrice": 0.555,
        "liquidity": 52488.5234,
        "volume24hr": 3568.523375,
        "endDate": "2026-07-31T12:00:00Z",
        "active": True,
        "closed": False,
        "events": [
            {
                "title": "Sample Event",
                "tags": [
                    {"label": "Politics", "slug": "politics"},
                    {"label": "World", "slug": "world"},
                ],
            }
        ],
    }
    base.update(overrides)
    return base


# ── 1. polymarket_api client ────────────────────────────────────────────────

class TestPolymarketClient(unittest.TestCase):
    """Mock requests.get, verify we hit the right URL with the right
    params and parse responses correctly."""

    def test_get_markets_hits_gamma_endpoint_with_filter_params(self):
        captured = {}

        class _MockResp:
            status_code = 200
            def json(self): return [_sample_market("1")]
            def raise_for_status(self): pass

        def _fake_get(url, params=None, headers=None, timeout=None):
            captured["url"] = url
            captured["params"] = params
            captured["headers"] = headers
            return _MockResp()

        with mock.patch("requests.get", side_effect=_fake_get):
            out = polymarket_api.get_markets(limit=50, offset=10, active=True, closed=False)

        self.assertEqual(captured["url"], "https://gamma-api.polymarket.com/markets")
        self.assertEqual(captured["params"]["limit"], 50)
        self.assertEqual(captured["params"]["offset"], 10)
        self.assertEqual(captured["params"]["active"], "true")
        self.assertEqual(captured["params"]["closed"], "false")
        self.assertEqual(captured["headers"]["Accept"], "application/json")
        self.assertIn("manifold-trading-bot", captured["headers"]["User-Agent"])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["id"], "1")

    def test_get_market_returns_none_on_404(self):
        import requests

        class _MockResp:
            status_code = 404
            def raise_for_status(self):
                e = requests.HTTPError("404 Not Found")
                e.response = self
                raise e
            def json(self): return {}

        with mock.patch("requests.get", return_value=_MockResp()):
            self.assertIsNone(polymarket_api.get_market("does-not-exist"))

    def test_iter_all_active_markets_paginates_until_short_page(self):
        """Generator should keep advancing offset until a page is shorter
        than page_size, then stop."""
        page1 = [_sample_market(str(i)) for i in range(5)]
        page2 = [_sample_market(str(i)) for i in range(5, 8)]  # short page → stop
        pages = [page1, page2]

        def _fake_get_markets(*args, **kwargs):
            return pages.pop(0) if pages else []

        with mock.patch.object(polymarket_api, "get_markets", side_effect=_fake_get_markets), \
             mock.patch("time.sleep"):  # don't actually sleep in tests
            collected = list(polymarket_api.iter_all_active_markets(page_size=5))

        self.assertEqual(len(collected), 8)
        self.assertEqual(collected[0]["id"], "0")
        self.assertEqual(collected[-1]["id"], "7")

    def test_iter_all_respects_max_pages(self):
        """When max_pages is set, the generator stops before naturally
        exhausting — useful for first-run smoke ingest."""
        # Each page is full so the natural loop wouldn't terminate.
        full_page = [_sample_market(str(i)) for i in range(5)]

        def _fake_get_markets(*args, **kwargs):
            return list(full_page)

        with mock.patch.object(polymarket_api, "get_markets", side_effect=_fake_get_markets), \
             mock.patch("time.sleep"):
            collected = list(polymarket_api.iter_all_active_markets(page_size=5, max_pages=2))

        # max_pages=2 means: first page yielded, sleep, second page yielded,
        # then we hit the cap. Total = 10 markets.
        self.assertEqual(len(collected), 10)


# ── 2. markets_normalized schema + normalize ─────────────────────────────────

class TestSchema(unittest.TestCase):
    """Lock in the exact column shape of markets_normalized."""

    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(path)
        self.db_path = Path(path)

    def tearDown(self):
        if self.db_path.exists():
            os.unlink(self.db_path)

    def test_init_db_creates_table_and_indexes(self):
        init_db(self.db_path)
        conn = sqlite3.connect(self.db_path)
        try:
            cols = {r[1]: r[2] for r in conn.execute("PRAGMA table_info(markets_normalized)")}
            expected = {
                "platform":      "TEXT",
                "market_id":     "TEXT",
                "question":      "TEXT",
                "probability":   "REAL",
                "liquidity":     "REAL",
                "volume_24h":    "REAL",
                "close_time_ms": "INTEGER",
                "resolution":    "TEXT",
                "tags":          "TEXT",
                "source_url":    "TEXT",
                "fetched_at":    "TEXT",
            }
            self.assertEqual(cols, expected)

            indexes = {r[1] for r in conn.execute(
                "SELECT * FROM sqlite_master WHERE type='index' AND tbl_name='markets_normalized'"
            )}
            for ix in ("idx_norm_close_time", "idx_norm_fetched_at", "idx_norm_resolution"):
                self.assertIn(ix, indexes)
        finally:
            conn.close()

    def test_init_db_idempotent(self):
        """Calling init_db twice should not raise or duplicate anything."""
        init_db(self.db_path)
        init_db(self.db_path)  # must not raise

    def test_upsert_replaces_on_conflict(self):
        init_db(self.db_path)
        ts = "2026-05-06T08:00:00+00:00"
        ts2 = "2026-05-06T09:00:00+00:00"
        upsert_markets(self.db_path, [{
            "platform": "polymarket", "market_id": "X", "question": "Q1",
            "probability": 0.50, "fetched_at": ts,
        }])
        # Same PK, different probability — should replace.
        upsert_markets(self.db_path, [{
            "platform": "polymarket", "market_id": "X", "question": "Q1",
            "probability": 0.75, "fetched_at": ts2,
        }])
        conn = sqlite3.connect(self.db_path)
        try:
            rows = list(conn.execute("SELECT probability, fetched_at FROM markets_normalized"))
        finally:
            conn.close()
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0][0], 0.75)
        self.assertEqual(rows[0][1], ts2)

    def test_upsert_requires_required_fields(self):
        init_db(self.db_path)
        with self.assertRaises(KeyError):
            upsert_markets(self.db_path, [{
                "platform": "polymarket",
                # missing market_id, question, fetched_at
            }])


class TestNormalizePolymarket(unittest.TestCase):
    """Field-mapping correctness from raw Gamma response → unified row."""

    def test_basic_market_normalizes_cleanly(self):
        raw = _sample_market()
        row = normalize_polymarket_market(raw, fetched_at="2026-05-06T08:00:00+00:00")
        self.assertEqual(row["platform"], "polymarket")
        self.assertEqual(row["market_id"], "540816")
        self.assertEqual(row["question"], "Will X happen by year-end?")
        self.assertAlmostEqual(row["probability"], 0.555)
        self.assertAlmostEqual(row["liquidity"], 52488.5234)
        self.assertAlmostEqual(row["volume_24h"], 3568.523375)
        # endDate "2026-07-31T12:00:00Z" → unix ms
        expected_ms = int(datetime(2026, 7, 31, 12, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
        self.assertEqual(row["close_time_ms"], expected_ms)
        self.assertIsNone(row["resolution"])
        self.assertEqual(row["source_url"], "https://polymarket.com/market/sample-market-540816")
        self.assertEqual(json.loads(row["tags"]), ["Politics", "World"])

    def test_missing_id_returns_none(self):
        raw = _sample_market()
        del raw["id"]
        self.assertIsNone(normalize_polymarket_market(raw))

    def test_missing_question_returns_none(self):
        raw = _sample_market()
        del raw["question"]
        self.assertIsNone(normalize_polymarket_market(raw))

    def test_malformed_outcome_prices_yields_none_probability(self):
        raw = _sample_market(outcomePrices=["not-a-number", "0.4"])
        row = normalize_polymarket_market(raw)
        self.assertIsNone(row["probability"])

    def test_missing_outcome_prices_yields_none_probability(self):
        raw = _sample_market()
        del raw["outcomePrices"]
        row = normalize_polymarket_market(raw)
        self.assertIsNone(row["probability"])

    def test_malformed_end_date_yields_none_close_time(self):
        raw = _sample_market(endDate="not-a-date")
        row = normalize_polymarket_market(raw)
        self.assertIsNone(row["close_time_ms"])

    def test_no_events_no_tags(self):
        raw = _sample_market(events=[])
        row = normalize_polymarket_market(raw)
        self.assertEqual(json.loads(row["tags"]), [])

    def test_tags_dedupe_across_events(self):
        raw = _sample_market(events=[
            {"tags": [{"label": "AI", "slug": "ai"}]},
            {"tags": [{"label": "AI", "slug": "ai"}, {"label": "Tech", "slug": "tech"}]},
        ])
        row = normalize_polymarket_market(raw)
        self.assertEqual(json.loads(row["tags"]), ["AI", "Tech"])

    def test_string_liquidity_coerces_to_float(self):
        """Polymarket sometimes returns numeric fields as strings — should
        still parse without crashing the whole row."""
        raw = _sample_market(liquidity="1234.56", volume24hr="789.0")
        row = normalize_polymarket_market(raw)
        self.assertAlmostEqual(row["liquidity"], 1234.56)
        self.assertAlmostEqual(row["volume_24h"], 789.0)

    def test_outcome_prices_as_json_encoded_string(self):
        """LIVE Gamma response shape: outcomePrices is a JSON-encoded
        string like '["0.555", "0.445"]', not a parsed list. Verified
        against api 2026-05-06. Initial test fixture used the parsed-list
        form which doesn't match live behavior."""
        raw = _sample_market()
        raw["outcomePrices"] = '["0.612", "0.388"]'  # the actual live shape
        row = normalize_polymarket_market(raw)
        self.assertAlmostEqual(row["probability"], 0.612)

    def test_outcome_prices_as_garbage_string(self):
        raw = _sample_market(outcomePrices="not-json")
        row = normalize_polymarket_market(raw)
        self.assertIsNone(row["probability"])

    def test_outcome_prices_as_empty_string(self):
        raw = _sample_market(outcomePrices="")
        row = normalize_polymarket_market(raw)
        self.assertIsNone(row["probability"])


# ── 3. End-to-end ingest smoke ───────────────────────────────────────────────

class TestIngestSmoke(unittest.TestCase):
    """End-to-end: mocked API → script → DB. Locks in that the pieces
    actually compose."""

    def test_ingest_writes_normalized_rows(self):
        from scripts import ingest_polymarket

        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(path)
        db_path = Path(path)

        markets = [_sample_market(str(i)) for i in range(3)]

        with mock.patch.object(ingest_polymarket.polymarket_api,
                               "iter_all_active_markets",
                               return_value=iter(markets)), \
             mock.patch.object(ingest_polymarket, "_DB_PATH", db_path):
            rc = ingest_polymarket.main(max_pages=1, dry_run=False, page_size=100)

        self.assertEqual(rc, 0)
        self.assertTrue(db_path.exists())

        conn = sqlite3.connect(db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM markets_normalized").fetchone()[0]
            self.assertEqual(count, 3)
            platforms = {r[0] for r in conn.execute("SELECT DISTINCT platform FROM markets_normalized")}
            self.assertEqual(platforms, {"polymarket"})
        finally:
            conn.close()
            os.unlink(db_path)

    def test_ingest_dry_run_does_not_write(self):
        from scripts import ingest_polymarket

        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(path)  # ensure file doesn't exist
        db_path = Path(path)

        markets = [_sample_market("1")]

        with mock.patch.object(ingest_polymarket.polymarket_api,
                               "iter_all_active_markets",
                               return_value=iter(markets)), \
             mock.patch.object(ingest_polymarket, "_DB_PATH", db_path):
            rc = ingest_polymarket.main(max_pages=1, dry_run=True, page_size=100)

        self.assertEqual(rc, 0)
        # Dry run must NOT create the DB
        self.assertFalse(db_path.exists())


if __name__ == "__main__":
    unittest.main()
