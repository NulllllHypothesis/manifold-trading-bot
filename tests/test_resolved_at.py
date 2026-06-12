#!/usr/bin/env python3
"""
Tests for explicit resolved_at stamping at resolution time (paper_trader.py).

Until this field existed, the only place resolution *time* was recorded was
the position monitor's hourly position_snapshots — i.e. detection time,
biased late by up to the snapshot cadence and silently absent if the monitor
was down. Downstream consumers (the workbench scorekeeper adapter) had to
approximate resolved_at from that lag. These tests pin the new contract:

  - every resolve path (YES/NO, MKT, CANCEL) stamps `resolved_at` on the
    closing position record AND its trade_history twin;
  - the default is detection time, now (UTC), as an ISO-8601 string;
  - auto_resolve_markets() passes the Manifold API's authoritative
    `resolutionTime` (epoch ms) through, so records carry the TRUE
    resolution time when the API reports one;
  - already-terminal legs (CLOSED_EARLY / WIN / LOSE / ABANDONED) are
    never stamped retroactively.
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from manifold_bot.paper_trader import PaperTrader, _market_resolved_at_iso


class _PaperTraderTestBase(unittest.TestCase):
    """Base with a temp state file and mocked Telegram/DB side-effects.

    Mirrors tests/test_resolution_types.py so resolve_* runs without touching
    the real calibration.db or Telegram.
    """

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.tmp.close()
        self.tg_patch = patch("manifold_bot.paper_trader._notify_resolution")
        self.tg_patch.start()
        self.db_patch = patch("manifold_bot.paper_trader._init_bet_outcomes_db")
        self.db_patch.start()
        self.write_patch = patch("manifold_bot.paper_trader._write_bet_outcome")
        self.write_patch.start()

    def tearDown(self):
        self.tg_patch.stop()
        self.db_patch.stop()
        self.write_patch.stop()
        if os.path.exists(self.tmp.name):
            os.unlink(self.tmp.name)

    def _make_trader(self, balance=1000.0, positions=None, trade_history=None):
        """Create a PaperTrader with a known state."""
        positions = positions or {}
        trade_history = trade_history or []
        with patch.object(PaperTrader, "__init__", lambda self_: None):
            trader = PaperTrader()
        trader.balance = balance
        trader.positions = positions
        trader.trade_history = trade_history
        trader.state_file = self.tmp.name
        trader.save_state = lambda: json.dump(
            {"balance": trader.balance, "positions": trader.positions,
             "trade_history": trader.trade_history},
            open(self.tmp.name, "w"), indent=2,
        )
        return trader

    @staticmethod
    def _position(trade_id=1, market_id="mkt_test", outcome="YES",
                  amount=10.0, entry_prob=0.60, status="OPEN"):
        return {
            "trade_id": trade_id,
            "market_id": market_id,
            "outcome": outcome,
            "amount": amount,
            "probability": entry_prob,
            "entry_probability": entry_prob,
            "status": status,
            "profit_if_win": amount / entry_prob - amount,
            "profit_if_lose": -amount,
        }

    @staticmethod
    def _assert_iso_utc(testcase, value):
        """resolved_at must be a parseable ISO-8601 timestamp."""
        testcase.assertIsInstance(value, str)
        parsed = datetime.fromisoformat(value)
        testcase.assertIsNotNone(parsed.tzinfo, "resolved_at must be tz-aware")


# ── resolve_market (binary YES/NO) ───────────────────────────────────────────

class TestResolveMarketStampsResolvedAt(_PaperTraderTestBase):

    def test_default_is_detection_time_utc(self):
        """No resolved_at passed → stamped with now (UTC), ISO-8601."""
        pos = self._position()
        trader = self._make_trader(positions={"mkt_test": [pos]})
        before = datetime.now(timezone.utc)
        trader.resolve_market("mkt_test", "YES")
        after = datetime.now(timezone.utc)
        self._assert_iso_utc(self, pos["resolved_at"])
        stamped = datetime.fromisoformat(pos["resolved_at"])
        self.assertGreaterEqual(stamped, before)
        self.assertLessEqual(stamped, after)

    def test_explicit_resolved_at_passes_through(self):
        """Caller-supplied timestamp (the API's true resolution time) wins."""
        pos = self._position()
        trader = self._make_trader(positions={"mkt_test": [pos]})
        trader.resolve_market("mkt_test", "NO",
                              resolved_at="2026-06-11T08:00:00+00:00")
        self.assertEqual(pos["resolved_at"], "2026-06-11T08:00:00+00:00")

    def test_trade_history_twin_gets_resolved_at(self):
        """The sync loop must copy resolved_at onto the trade_history record."""
        pos = self._position(trade_id=7)
        history_twin = dict(pos)  # distinct dict — forces the copy path
        trader = self._make_trader(positions={"mkt_test": [pos]},
                                   trade_history=[history_twin])
        trader.resolve_market("mkt_test", "YES",
                              resolved_at="2026-06-11T08:00:00+00:00")
        self.assertEqual(history_twin["resolved_at"],
                         "2026-06-11T08:00:00+00:00")
        self.assertEqual(history_twin["status"], "WIN")

    def test_terminal_legs_are_not_stamped(self):
        """CLOSED_EARLY / WIN legs are skipped — no retroactive stamping."""
        closed = self._position(trade_id=1, status="CLOSED_EARLY")
        won = self._position(trade_id=2, status="WIN")
        live = self._position(trade_id=3, status="OPEN")
        trader = self._make_trader(
            positions={"mkt_test": [closed, won, live]})
        trader.resolve_market("mkt_test", "YES")
        self.assertNotIn("resolved_at", closed)
        self.assertNotIn("resolved_at", won)
        self.assertIn("resolved_at", live)

    def test_stranded_legs_are_stamped(self):
        """STRANDED positions reconcile on late resolution — stamped too."""
        pos = self._position(status="STRANDED")
        trader = self._make_trader(positions={"mkt_test": [pos]})
        trader.resolve_market("mkt_test", "YES",
                              resolved_at="2026-06-11T08:00:00+00:00")
        self.assertEqual(pos["resolved_at"], "2026-06-11T08:00:00+00:00")

    def test_resolved_at_survives_save_state(self):
        """The stamp must persist into paper_trading_state.json."""
        pos = self._position(trade_id=9)
        trader = self._make_trader(positions={"mkt_test": [pos]},
                                   trade_history=[pos])  # shared dict, as in prod
        trader.resolve_market("mkt_test", "YES",
                              resolved_at="2026-06-11T08:00:00+00:00")
        with open(self.tmp.name) as f:
            state = json.load(f)
        self.assertEqual(state["trade_history"][0]["resolved_at"],
                         "2026-06-11T08:00:00+00:00")


# ── resolve_market_mkt / resolve_market_cancel ───────────────────────────────

class TestMktAndCancelStampResolvedAt(_PaperTraderTestBase):

    def test_mkt_stamps_resolved_at(self):
        pos = self._position()
        twin = dict(pos)
        trader = self._make_trader(positions={"mkt_test": [pos]},
                                   trade_history=[twin])
        trader.resolve_market_mkt("mkt_test", 0.60,
                                  resolved_at="2026-06-11T09:30:00+00:00")
        self.assertEqual(pos["resolved_at"], "2026-06-11T09:30:00+00:00")
        self.assertEqual(twin["resolved_at"], "2026-06-11T09:30:00+00:00")

    def test_mkt_defaults_to_detection_time(self):
        pos = self._position()
        trader = self._make_trader(positions={"mkt_test": [pos]})
        trader.resolve_market_mkt("mkt_test", 0.60)
        self._assert_iso_utc(self, pos["resolved_at"])

    def test_cancel_stamps_resolved_at(self):
        pos = self._position()
        twin = dict(pos)
        trader = self._make_trader(positions={"mkt_test": [pos]},
                                   trade_history=[twin])
        trader.resolve_market_cancel("mkt_test",
                                     resolved_at="2026-06-11T10:00:00+00:00")
        self.assertEqual(pos["resolved_at"], "2026-06-11T10:00:00+00:00")
        self.assertEqual(twin["resolved_at"], "2026-06-11T10:00:00+00:00")
        self.assertEqual(pos["status"], "CANCELLED")

    def test_cancel_defaults_to_detection_time(self):
        pos = self._position()
        trader = self._make_trader(positions={"mkt_test": [pos]})
        trader.resolve_market_cancel("mkt_test")
        self._assert_iso_utc(self, pos["resolved_at"])


# ── _market_resolved_at_iso (API resolutionTime → ISO) ───────────────────────

class TestMarketResolvedAtIso(unittest.TestCase):

    def test_epoch_ms_converts_to_iso_utc(self):
        # 2026-06-11T08:00:00Z = 1781164800000 ms
        out = _market_resolved_at_iso({"resolutionTime": 1781164800000})
        self.assertEqual(out, "2026-06-11T08:00:00+00:00")

    def test_missing_field_returns_none(self):
        self.assertIsNone(_market_resolved_at_iso({}))
        self.assertIsNone(_market_resolved_at_iso({"resolutionTime": None}))

    def test_garbage_returns_none_never_raises(self):
        self.assertIsNone(_market_resolved_at_iso({"resolutionTime": "soon"}))
        self.assertIsNone(_market_resolved_at_iso({"resolutionTime": float("inf")}))
        self.assertIsNone(_market_resolved_at_iso({"resolutionTime": 1e30}))


# ── auto_resolve_markets pass-through ────────────────────────────────────────

class TestAutoResolvePassesResolutionTime(_PaperTraderTestBase):

    RESOLUTION_TIME_MS = 1781164800000  # 2026-06-11T08:00:00Z
    EXPECTED_ISO = "2026-06-11T08:00:00+00:00"

    def _run_auto_resolve(self, market_payload, pos):
        trader = self._make_trader(positions={"mkt_test": [pos]})
        with patch("manifold_bot.paper_trader.api_client") as mock_api:
            mock_api.get_market.return_value = market_payload
            count = trader.auto_resolve_markets()
        return count

    def test_binary_resolution_carries_api_time(self):
        pos = self._position()
        count = self._run_auto_resolve({
            "isResolved": True,
            "resolution": "YES",
            "question": "Will it?",
            "resolutionTime": self.RESOLUTION_TIME_MS,
        }, pos)
        self.assertEqual(count, 1)
        self.assertEqual(pos["resolved_at"], self.EXPECTED_ISO)

    def test_mkt_resolution_carries_api_time(self):
        pos = self._position()
        count = self._run_auto_resolve({
            "isResolved": True,
            "resolution": "MKT",
            "resolutionProbability": 0.42,
            "question": "How likely?",
            "resolutionTime": self.RESOLUTION_TIME_MS,
        }, pos)
        self.assertEqual(count, 1)
        self.assertEqual(pos["resolved_at"], self.EXPECTED_ISO)

    def test_cancel_resolution_carries_api_time(self):
        pos = self._position()
        count = self._run_auto_resolve({
            "isResolved": True,
            "resolution": "CANCEL",
            "question": "Voided?",
            "resolutionTime": self.RESOLUTION_TIME_MS,
        }, pos)
        self.assertEqual(count, 1)
        self.assertEqual(pos["resolved_at"], self.EXPECTED_ISO)

    def test_no_resolution_time_falls_back_to_detection_time(self):
        """API omits resolutionTime → resolve_market defaults to now (UTC)."""
        pos = self._position()
        before = datetime.now(timezone.utc)
        count = self._run_auto_resolve({
            "isResolved": True,
            "resolution": "YES",
            "question": "Will it?",
        }, pos)
        self.assertEqual(count, 1)
        stamped = datetime.fromisoformat(pos["resolved_at"])
        self.assertGreaterEqual(stamped, before)


if __name__ == "__main__":
    unittest.main()
