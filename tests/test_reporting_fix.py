#!/usr/bin/env python3
"""Tests for the V2 reporting-fix PR.

Covers the three surfaces the user sees after a capital reset:

  1. PaperTrader.capital_epochs round-trips through save_state/load_state,
     and effective_baseline() returns the latest topup_to.
  2. place_paper_bet persists the blended `confidence` and `ai_status` on
     the leg (so the bet record itself knows what the gate saw and what
     the AI thought, separately from entry_confidence/ai_confidence).
  3. daily_summary.calculate_daily_metrics uses the effective baseline
     (latest capital epoch), not the hard-coded INITIAL_BALANCE, so total
     profit isn't reported as -$800 after a deliberate top-up.
  4. telegram_bot.cmd_portfolio prefers the blended `confidence` on each
     leg (falling back to legacy fields) and renders the AI status tag.
"""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from manifold_bot.paper_trader import PaperTrader
from manifold_bot import config as _config


def _fresh_trader(tmp_path: str, initial_balance: float = 1000.0) -> PaperTrader:
    """Construct a PaperTrader whose state lives at a temp path."""
    trader = PaperTrader(initial_balance=initial_balance)
    trader.state_file = tmp_path
    trader.positions = {}
    trader.trade_history = []
    trader.capital_epochs = []
    trader.balance = initial_balance
    return trader


def _reload_trader(tmp_path: str, initial_balance: float = 1000.0) -> PaperTrader:
    """Construct a PaperTrader and point it at an existing state file."""
    trader = PaperTrader(initial_balance=initial_balance)
    trader.state_file = tmp_path
    trader.load_state()
    return trader


class TestCapitalEpochs(unittest.TestCase):
    """capital_epochs must survive load→save→load round-trip and drive baseline."""

    def _fresh_state_path(self) -> str:
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(path)
        return path

    def test_round_trip_preserves_epochs(self):
        path = self._fresh_state_path()
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))

        trader = _fresh_trader(path)
        trader.capital_epochs = [
            {
                "started_at": "2026-04-20T14:08:00+03:00",
                "topup_from": 34.76,
                "topup_to": 100.00,
                "reason": "runway",
            }
        ]
        trader.save_state()

        reloaded = _reload_trader(path)
        self.assertEqual(len(reloaded.capital_epochs), 1)
        self.assertEqual(reloaded.capital_epochs[0]["topup_to"], 100.0)
        self.assertEqual(reloaded.capital_epochs[0]["started_at"],
                         "2026-04-20T14:08:00+03:00")

    def test_effective_baseline_no_epochs_is_initial(self):
        path = self._fresh_state_path()
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))

        trader = _fresh_trader(path)
        self.assertEqual(trader.effective_baseline(), float(_config.INITIAL_BALANCE))

    def test_effective_baseline_uses_latest_topup(self):
        path = self._fresh_state_path()
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))

        trader = _fresh_trader(path)
        trader.capital_epochs = [
            {"started_at": "2026-04-20", "topup_from": 30, "topup_to": 100},
            {"started_at": "2026-05-01", "topup_from": 50, "topup_to": 200},
        ]
        self.assertEqual(trader.effective_baseline(), 200.0)

    def test_save_state_does_not_drop_epochs_on_subsequent_save(self):
        """Regression — the original bug was that save_state() only persisted
        {balance, positions, trade_history, saved_at}, so any custom field
        set on state got silently overwritten on the next save."""
        path = self._fresh_state_path()
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))

        trader = _fresh_trader(path)
        trader.capital_epochs = [{"started_at": "2026-04-20", "topup_to": 100}]
        trader.save_state()

        # Simulate a subsequent save triggered by any state change
        trader.balance = 150.0
        trader.save_state()

        with open(path) as f:
            state = json.load(f)
        self.assertEqual(len(state["capital_epochs"]), 1)
        self.assertEqual(state["capital_epochs"][0]["topup_to"], 100)


class TestLegPersistence(unittest.TestCase):
    """Leg records must carry the blended confidence and ai_status."""

    def _fresh_state_path(self) -> str:
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(path)
        return path

    def test_place_paper_bet_persists_confidence_and_ai_status(self):
        path = self._fresh_state_path()
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))

        trader = _fresh_trader(path, initial_balance=100.0)

        ok = trader.place_paper_bet(
            market_id="mk1",
            outcome="YES",
            amount=5.0,
            probability=0.5,
            ai_confidence=0.0,       # AI didn't vote
            confidence=0.74,         # blended (stat-only) gate saw 74%
            ai_status="not_run",
        )
        self.assertTrue(ok)

        leg = trader.positions["mk1"][0]
        self.assertEqual(leg["confidence"], 0.74)
        self.assertEqual(leg["ai_confidence"], 0.0)
        self.assertEqual(leg["ai_status"], "not_run")

    def test_place_paper_bet_defaults_blended_confidence_to_none(self):
        path = self._fresh_state_path()
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))

        trader = _fresh_trader(path, initial_balance=100.0)

        ok = trader.place_paper_bet(
            market_id="mk2",
            outcome="NO",
            amount=5.0,
            probability=0.4,
        )
        self.assertTrue(ok)

        leg = trader.positions["mk2"][0]
        self.assertIn("confidence", leg)
        self.assertIsNone(leg["confidence"])
        self.assertIn("ai_status", leg)
        self.assertIsNone(leg["ai_status"])


class TestDailySummaryBaseline(unittest.TestCase):
    """calculate_daily_metrics must honour capital_epochs when present."""

    def test_metrics_use_latest_epoch_topup(self):
        from automation.daily_summary import DailySummary

        summary = DailySummary()
        data = {
            "state": {
                "balance": 196.89,
                "trade_history": [],
                "positions": {},
                "capital_epochs": [
                    {
                        "started_at": "2026-04-20T14:08:00+03:00",
                        "topup_from": 34.76,
                        "topup_to": 100.00,
                    }
                ],
            }
        }
        metrics = summary.calculate_daily_metrics(data)
        self.assertAlmostEqual(metrics["initial_balance"], 100.0)
        self.assertAlmostEqual(metrics["total_profit"], 96.89, places=2)

    def test_metrics_fall_back_to_initial_balance(self):
        from automation.daily_summary import DailySummary
        from manifold_bot.config import INITIAL_BALANCE

        summary = DailySummary()
        data = {
            "state": {
                "balance": 1050.0,
                "trade_history": [],
                "positions": {},
            }
        }
        metrics = summary.calculate_daily_metrics(data)
        self.assertEqual(metrics["initial_balance"], float(INITIAL_BALANCE))
        self.assertEqual(metrics["total_profit"], 1050.0 - INITIAL_BALANCE)


class TestBaselineFromState(unittest.TestCase):
    """PaperTrader.baseline_from_state is the single helper both
    daily_summary and telegram_bot /portfolio call."""

    def test_empty_state_returns_default(self):
        self.assertEqual(
            PaperTrader.baseline_from_state({}, default=1000.0), 1000.0
        )

    def test_latest_epoch_wins(self):
        state = {"capital_epochs": [
            {"topup_to": 100},
            {"topup_to": 250},
        ]}
        self.assertEqual(
            PaperTrader.baseline_from_state(state, default=1000.0), 250.0
        )

    def test_none_state_returns_default(self):
        self.assertEqual(
            PaperTrader.baseline_from_state(None, default=1000.0), 1000.0
        )


class TestPortfolioTelegram(unittest.TestCase):
    """cmd_portfolio P&L must be measured against effective baseline."""

    def test_portfolio_uses_effective_baseline(self):
        from automation import telegram_bot

        state = {
            "balance": 82.19,
            "capital_epochs": [{"topup_to": 100.00}],
            "positions": {},
            "trade_history": [],
        }
        with patch.object(telegram_bot, "_load_state", return_value=state):
            msg = telegram_bot.cmd_portfolio()

        # Effective P&L: $82.19 - $100 = -$17.81 (-17.8%)
        self.assertIn("$-17.81", msg)
        self.assertIn("-17.8%", msg)
        # Must not show the old broken computation
        self.assertNotIn("-917.81", msg)
        self.assertNotIn("-91.8%", msg)

    def test_portfolio_falls_back_to_initial_balance(self):
        from automation import telegram_bot

        state = {
            "balance": 1050.0,
            "positions": {},
            "trade_history": [],
        }
        with patch.object(telegram_bot, "_load_state", return_value=state):
            msg = telegram_bot.cmd_portfolio()

        # $1050 - $1000 = +$50 (+5.0%)
        self.assertIn("+50.00", msg)
        self.assertIn("+5.0%", msg)


class TestPositionsTelegram(unittest.TestCase):
    """cmd_positions must prefer blended confidence and render AI status tag."""

    def test_prefers_blended_confidence_and_shows_ai_tag(self):
        from automation import telegram_bot

        state = {
            "positions": {
                "mk1": [{
                    "status": "OPEN",
                    "outcome": "YES",
                    "amount": 5.0,
                    "probability": 0.5,
                    "entry_probability": 0.5,
                    "entry_confidence": 0.0,   # legacy (ai_confidence mirror)
                    "ai_confidence": 0.0,
                    "confidence": 0.74,        # blended — what gate saw
                    "ai_status": "not_run",
                    "question": "Will X happen?",
                }],
            }
        }
        with patch.object(telegram_bot, "_load_state", return_value=state), \
             patch.object(telegram_bot, "_load_research", return_value={}):
            msg = telegram_bot.cmd_positions()

        self.assertIn("conf 74%", msg)
        self.assertIn("AI not_run", msg)
        self.assertNotIn("conf 0%", msg)

    def test_falls_back_to_entry_confidence_for_legacy_legs(self):
        from automation import telegram_bot

        state = {
            "positions": {
                "mk1": [{
                    "status": "OPEN",
                    "outcome": "NO",
                    "amount": 5.0,
                    "probability": 0.4,
                    "entry_probability": 0.4,
                    "entry_confidence": 0.65,   # pre-reporting-fix leg
                    "ai_confidence": 0.65,
                    "question": "Legacy leg",
                }],
            }
        }
        with patch.object(telegram_bot, "_load_state", return_value=state), \
             patch.object(telegram_bot, "_load_research", return_value={}):
            msg = telegram_bot.cmd_positions()

        self.assertIn("conf 65%", msg)
        self.assertNotIn("· AI ", msg)


class TestStartNewCapitalEpoch(unittest.TestCase):
    """scripts/start_new_capital_epoch.py — operational reset used when
    the bot is in capital starvation. Locks in: balance reset to target,
    epoch entry appended (not replacing prior epochs), reason persisted,
    dry-run path doesn't mutate."""

    def _write_state(self, balance: float, epochs: list = None) -> str:
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        state = {
            "balance": balance,
            "positions": {},
            "trade_history": [],
            "capital_epochs": epochs or [],
        }
        with open(path, "w") as f:
            json.dump(state, f)
        return path

    def test_resets_balance_and_appends_epoch(self):
        from scripts.start_new_capital_epoch import main
        path = self._write_state(balance=16.76, epochs=[
            {"started_at": "2026-04-20", "topup_from": 34.76, "topup_to": 100.00},
        ])
        try:
            rc = main(path, target=100.0, reason="post-PR-30 unfreeze test", dry_run=False)
            self.assertEqual(rc, 0)
            with open(path) as f:
                state = json.load(f)
            self.assertEqual(state["balance"], 100.0)
            self.assertEqual(len(state["capital_epochs"]), 2)  # prior + new
            new_epoch = state["capital_epochs"][-1]
            self.assertEqual(new_epoch["topup_from"], 16.76)
            self.assertEqual(new_epoch["topup_to"], 100.0)
            self.assertEqual(new_epoch["reason"], "post-PR-30 unfreeze test")
        finally:
            os.unlink(path)

    def test_dry_run_does_not_mutate(self):
        from scripts.start_new_capital_epoch import main
        path = self._write_state(balance=16.76, epochs=[])
        try:
            rc = main(path, target=100.0, reason="dry test", dry_run=True)
            self.assertEqual(rc, 0)
            with open(path) as f:
                state = json.load(f)
            self.assertEqual(state["balance"], 16.76)  # unchanged
            self.assertEqual(state["capital_epochs"], [])  # unchanged
        finally:
            os.unlink(path)

    def test_missing_state_file_returns_nonzero(self):
        from scripts.start_new_capital_epoch import main
        rc = main("/nonexistent/path.json", target=100.0, reason="x", dry_run=False)
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
