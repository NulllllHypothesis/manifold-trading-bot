#!/usr/bin/env python3
"""
Tests for MKT and CANCEL resolution types in paper_trader.py.

These were added in session 8 to unblock 3 zombie positions. Without
tests, the payout math for MKT (proportional) and CANCEL (refund)
has no regression guard.
"""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from manifold_bot.paper_trader import PaperTrader


class _PaperTraderTestBase(unittest.TestCase):
    """Base with a temp state file and mocked Telegram."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.tmp.close()
        # Patch Telegram so it never fires
        self.tg_patch = patch("manifold_bot.paper_trader._notify_resolution")
        self.tg_patch.start()
        # Patch the DB init so we don't touch the real calibration.db
        self.db_patch = patch("manifold_bot.paper_trader._init_bet_outcomes_db")
        self.db_patch.start()
        self.write_patch = patch("manifold_bot.paper_trader._write_bet_outcome")
        self.mock_write = self.write_patch.start()

    def tearDown(self):
        self.tg_patch.stop()
        self.db_patch.stop()
        self.write_patch.stop()
        if os.path.exists(self.tmp.name):
            os.unlink(self.tmp.name)

    def _make_trader(self, balance=1000.0, positions=None):
        """Create a PaperTrader with a known state."""
        state = {
            "balance": balance,
            "positions": positions or {},
            "trade_history": [],
        }
        with open(self.tmp.name, "w") as f:
            json.dump(state, f)
        with patch.object(PaperTrader, "__init__", lambda self_: None):
            trader = PaperTrader()
        trader.balance = balance
        trader.positions = positions or {}
        trader.trade_history = []
        trader.state_file = self.tmp.name
        trader.save_state = lambda: json.dump(
            {"balance": trader.balance, "positions": trader.positions,
             "trade_history": trader.trade_history},
            open(self.tmp.name, "w"), indent=2,
        )
        return trader


# ── resolve_market_mkt ──────────────────────���───────────────────────────────

class TestResolveMarketMkt(_PaperTraderTestBase):

    def _yes_position(self, amount=10.0, entry_prob=0.60):
        return {
            "trade_id": 1,
            "market_id": "mkt_test",
            "outcome": "YES",
            "amount": amount,
            "probability": entry_prob,
            "entry_probability": entry_prob,
            "status": "OPEN",
            "profit_if_win": amount / entry_prob - amount,
            "profit_if_lose": -amount,
        }

    def _no_position(self, amount=10.0, entry_prob=0.40):
        return {
            "trade_id": 2,
            "market_id": "mkt_test",
            "outcome": "NO",
            "amount": amount,
            "probability": entry_prob,
            "entry_probability": entry_prob,
            "status": "OPEN",
            "profit_if_win": amount / (1 - entry_prob) - amount,
            "profit_if_lose": -amount,
        }

    def test_yes_bet_mkt_resolves_at_same_prob(self):
        """YES bet at 60%, MKT resolves at 60% → break even."""
        pos = self._yes_position(amount=10.0, entry_prob=0.60)
        trader = self._make_trader(balance=990.0, positions={"mkt_test": [pos]})
        trader.resolve_market_mkt("mkt_test", 0.60)
        self.assertEqual(pos["status"], "WIN")  # payout=10, profit=0
        self.assertAlmostEqual(pos["profit"], 0.0, places=2)
        self.assertAlmostEqual(trader.balance, 990.0, places=2)

    def test_yes_bet_mkt_resolves_higher(self):
        """YES bet at 50%, MKT resolves at 80% → profit."""
        pos = self._yes_position(amount=10.0, entry_prob=0.50)
        trader = self._make_trader(balance=990.0, positions={"mkt_test": [pos]})
        trader.resolve_market_mkt("mkt_test", 0.80)
        # payout = 10 * 0.80 / 0.50 = 16, profit = 6
        self.assertEqual(pos["status"], "WIN")
        self.assertAlmostEqual(pos["profit"], 6.0, places=2)
        self.assertAlmostEqual(trader.balance, 996.0, places=2)

    def test_yes_bet_mkt_resolves_lower(self):
        """YES bet at 80%, MKT resolves at 20% → loss."""
        pos = self._yes_position(amount=10.0, entry_prob=0.80)
        trader = self._make_trader(balance=990.0, positions={"mkt_test": [pos]})
        trader.resolve_market_mkt("mkt_test", 0.20)
        # payout = 10 * 0.20 / 0.80 = 2.5, profit = -7.5
        self.assertEqual(pos["status"], "LOSE")
        self.assertAlmostEqual(pos["profit"], -7.5, places=2)
        self.assertAlmostEqual(trader.balance, 982.5, places=2)

    def test_no_bet_mkt_resolves_low(self):
        """NO bet at 40% (entry), MKT resolves at 20% → profit."""
        pos = self._no_position(amount=10.0, entry_prob=0.40)
        trader = self._make_trader(balance=990.0, positions={"mkt_test": [pos]})
        trader.resolve_market_mkt("mkt_test", 0.20)
        # payout = 10 * (1-0.20) / (1-0.40) = 10 * 0.80/0.60 = 13.33
        # profit = 13.33 - 10 = 3.33
        self.assertEqual(pos["status"], "WIN")
        self.assertAlmostEqual(pos["profit"], 3.33, places=2)

    def test_no_bet_mkt_resolves_high(self):
        """NO bet at 40%, MKT resolves at 90% → big loss."""
        pos = self._no_position(amount=10.0, entry_prob=0.40)
        trader = self._make_trader(balance=990.0, positions={"mkt_test": [pos]})
        trader.resolve_market_mkt("mkt_test", 0.90)
        # payout = 10 * 0.10/0.60 = 1.67, profit = -8.33
        self.assertEqual(pos["status"], "LOSE")
        self.assertAlmostEqual(pos["profit"], -8.33, places=2)

    def test_mkt_writes_bet_outcome(self):
        """MKT resolution must write to bet_outcomes for calibration."""
        pos = self._yes_position()
        trader = self._make_trader(positions={"mkt_test": [pos]})
        trader.resolve_market_mkt("mkt_test", 0.60)
        self.mock_write.assert_called_once()
        args = self.mock_write.call_args
        self.assertEqual(args[1]["market_resolution"], "MKT")

    def test_mkt_sets_actual_outcome(self):
        pos = self._yes_position()
        trader = self._make_trader(positions={"mkt_test": [pos]})
        trader.resolve_market_mkt("mkt_test", 0.75)
        self.assertEqual(pos["actual_outcome"], "MKT")

    def test_mkt_skips_non_open(self):
        """Already-resolved positions must not be double-counted."""
        pos = self._yes_position()
        pos["status"] = "WIN"
        trader = self._make_trader(positions={"mkt_test": [pos]})
        balance_before = trader.balance
        trader.resolve_market_mkt("mkt_test", 0.50)
        self.assertEqual(trader.balance, balance_before)  # unchanged


# ── resolve_market_cancel ────────────────────────────────────────────────────

class TestResolveMarketCancel(_PaperTraderTestBase):

    def _position(self, trade_id=1, amount=10.0, entry_prob=0.70, outcome="YES"):
        return {
            "trade_id": trade_id,
            "market_id": "mkt_cancel",
            "outcome": outcome,
            "amount": amount,
            "probability": entry_prob,
            "entry_probability": entry_prob,
            "status": "OPEN",
            "profit_if_win": amount / entry_prob - amount if outcome == "YES" else amount / (1-entry_prob) - amount,
            "profit_if_lose": -amount,
        }

    def test_cancel_refunds_full_amount(self):
        """CANCEL must refund the bet amount, not zero."""
        pos = self._position(amount=25.0)
        trader = self._make_trader(balance=975.0, positions={"mkt_cancel": [pos]})
        trader.resolve_market_cancel("mkt_cancel")
        self.assertEqual(pos["status"], "CANCELLED")
        self.assertAlmostEqual(pos["profit"], 0.0)
        # Balance should be 975 + 25 = 1000
        self.assertAlmostEqual(trader.balance, 1000.0, places=2)

    def test_cancel_sets_actual_outcome(self):
        pos = self._position()
        trader = self._make_trader(positions={"mkt_cancel": [pos]})
        trader.resolve_market_cancel("mkt_cancel")
        self.assertEqual(pos["actual_outcome"], "CANCEL")

    def test_cancel_writes_bet_outcome(self):
        pos = self._position()
        trader = self._make_trader(positions={"mkt_cancel": [pos]})
        trader.resolve_market_cancel("mkt_cancel")
        self.mock_write.assert_called_once()
        args = self.mock_write.call_args
        self.assertEqual(args[1]["market_resolution"], "CANCEL")
        self.assertAlmostEqual(args[1]["actual_pnl"], 0.0)

    def test_cancel_multiple_positions(self):
        """A market with 2 open positions: both should be refunded."""
        p1 = self._position(trade_id=1, amount=10.0)
        p2 = self._position(trade_id=2, amount=15.0, outcome="NO")
        trader = self._make_trader(balance=975.0, positions={"mkt_cancel": [p1, p2]})
        trader.resolve_market_cancel("mkt_cancel")
        self.assertEqual(p1["status"], "CANCELLED")
        self.assertEqual(p2["status"], "CANCELLED")
        # Refund both: 975 + 10 + 15 = 1000
        self.assertAlmostEqual(trader.balance, 1000.0, places=2)

    def test_cancel_skips_non_open(self):
        pos = self._position()
        pos["status"] = "WIN"
        trader = self._make_trader(balance=1000.0, positions={"mkt_cancel": [pos]})
        trader.resolve_market_cancel("mkt_cancel")
        self.assertEqual(trader.balance, 1000.0)  # no refund for already-resolved


# ── auto_resolve_markets dispatch ───────────────────────────────────���────────

class TestAutoResolveDispatch(_PaperTraderTestBase):

    def _position(self, market_id, outcome="YES", amount=10.0, entry_prob=0.50):
        return {
            "trade_id": hash(market_id) % 1000,
            "market_id": market_id,
            "outcome": outcome,
            "amount": amount,
            "probability": entry_prob,
            "entry_probability": entry_prob,
            "status": "OPEN",
            "profit_if_win": amount / entry_prob - amount,
            "profit_if_lose": -amount,
        }

    @patch("manifold_bot.paper_trader.api_client")
    def test_dispatches_yes_no_mkt_cancel(self, mock_api):
        """auto_resolve_markets must handle all 4 resolution types."""
        positions = {
            "mkt_yes":    [self._position("mkt_yes")],
            "mkt_no":     [self._position("mkt_no", outcome="NO")],
            "mkt_mkt":    [self._position("mkt_mkt")],
            "mkt_cancel": [self._position("mkt_cancel")],
            "mkt_open":   [self._position("mkt_open")],
        }
        trader = self._make_trader(balance=950.0, positions=positions)

        def get_market(mid):
            return {
                "mkt_yes":    {"isResolved": True, "resolution": "YES", "question": "Q1"},
                "mkt_no":     {"isResolved": True, "resolution": "NO", "question": "Q2"},
                "mkt_mkt":    {"isResolved": True, "resolution": "MKT", "resolutionProbability": 0.50, "question": "Q3"},
                "mkt_cancel": {"isResolved": True, "resolution": "CANCEL", "question": "Q4"},
                "mkt_open":   {"isResolved": False, "question": "Q5"},
            }[mid]

        mock_api.get_market.side_effect = get_market
        count = trader.auto_resolve_markets()

        self.assertEqual(count, 4)  # 4 resolved, 1 still open
        self.assertEqual(positions["mkt_yes"][0]["status"], "WIN")
        self.assertEqual(positions["mkt_no"][0]["status"], "WIN")  # NO bet, resolved NO
        self.assertIn(positions["mkt_mkt"][0]["status"], ("WIN", "LOSE"))
        self.assertEqual(positions["mkt_cancel"][0]["status"], "CANCELLED")
        self.assertEqual(positions["mkt_open"][0]["status"], "OPEN")


if __name__ == "__main__":
    unittest.main()
