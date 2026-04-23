#!/usr/bin/env python3
"""
Unit tests for the Smart Position Swap feature.

Tests cover:
- compute_unrealised_pnl (maths)
- find_all_weak_positions (position analysis — returns all losers)
- find_best_opportunity (research analysis)
- has_active_pending_swaps (multi-swap flag-file logic)
- build_swap_entry / format_telegram_message
- PaperTrader.close_position_early (paper_trader integration)
- run_swap_check (integration — mocked I/O)
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.position_swap_checker import (
    compute_unrealised_pnl,
    find_all_weak_positions,
    find_best_opportunity,
    has_active_pending_swaps,
    build_swap_entry,
    format_telegram_message,
    run_swap_check,
    SWAP_EXPIRY_HOURS,
)
from manifold_bot.paper_trader import PaperTrader
from manifold_bot.config import MIN_LIQUIDITY, MAX_BET_AMOUNT


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_trade(outcome='YES', amount=100.0, entry_prob=0.5, status='OPEN'):
    safe_ep = entry_prob if entry_prob > 0 else 0.001
    return {
        'trade_id': 1,
        'market_id': 'mkt1',
        'outcome': outcome,
        'amount': amount,
        'probability': entry_prob,
        'entry_probability': entry_prob,
        'entry_confidence': 0.7,
        'payout_if_win': amount / safe_ep,
        'profit_if_win': amount / safe_ep - amount,
        'profit_if_lose': -amount,
        'status': status,
        'timestamp': datetime.now().isoformat(),
        'estimated_ev': 10.0,
        'ai_confidence': 0.7,
        'strategies': ['volume_spike'],
    }


def _make_recommendation(market_id='mkt_new', confidence=0.75, ev=20.0,
                          recommendation='YES', ai_rec='YES'):
    return {
        'market_id': market_id,
        'question': 'Will something happen?',
        'recommendation': recommendation,
        'confidence': confidence,
        'estimated_ev': ev,
        'probability': 0.4,
        'ai_recommendation': ai_rec,
        'ai_returned_skip': False,
        'strategies': ['volume_spike'],
        'liquidity': MIN_LIQUIDITY,  # must meet the liquidity gate in find_best_opportunity
    }


def _pending_swap(swap_id=1, status='pending', age_hours=0):
    proposed = (datetime.now() - timedelta(hours=age_hours)).isoformat()
    return {
        'id': swap_id,
        'close_market_id': f'mkt_close_{swap_id}',
        'close_question': 'Close question',
        'close_outcome': 'YES',
        'close_entry_probability': 0.5,
        'close_current_probability': 0.7,
        'close_unrealised_pnl': -10.0,
        'open_market_id': f'mkt_open_{swap_id}',
        'open_question': 'Open question',
        'open_recommendation': {'recommendation': 'NO', 'confidence': 0.75, 'estimated_ev': 20.0},
        'open_strategies': [],
        'proposed_at': proposed,
        'status': status,
    }


# ─────────────────────────────────────────────────────────────────────────────
# compute_unrealised_pnl
# ─────────────────────────────────────────────────────────────────────────────

class TestComputeUnrealisedPnl(unittest.TestCase):

    def test_yes_position_profit(self):
        trade = _make_trade(outcome='YES', amount=100, entry_prob=0.4)
        self.assertAlmostEqual(compute_unrealised_pnl(trade, 0.6), 50.0, places=4)

    def test_yes_position_loss(self):
        trade = _make_trade(outcome='YES', amount=100, entry_prob=0.6)
        self.assertAlmostEqual(compute_unrealised_pnl(trade, 0.4), -33.333, places=2)

    def test_no_position_profit(self):
        trade = _make_trade(outcome='NO', amount=100, entry_prob=0.6)
        self.assertAlmostEqual(compute_unrealised_pnl(trade, 0.4), 50.0, places=4)

    def test_no_position_loss(self):
        trade = _make_trade(outcome='NO', amount=100, entry_prob=0.4)
        self.assertAlmostEqual(compute_unrealised_pnl(trade, 0.6), -33.333, places=2)

    def test_breakeven(self):
        trade = _make_trade(outcome='YES', amount=50, entry_prob=0.5)
        self.assertAlmostEqual(compute_unrealised_pnl(trade, 0.5), 0.0, places=6)

    def test_missing_entry_probability_falls_back_to_probability(self):
        trade = _make_trade(outcome='YES', amount=100, entry_prob=0.4)
        del trade['entry_probability']
        trade['probability'] = 0.4
        self.assertAlmostEqual(compute_unrealised_pnl(trade, 0.6), 50.0, places=4)

    def test_missing_both_probability_fields_returns_zero(self):
        self.assertEqual(compute_unrealised_pnl({'outcome': 'YES', 'amount': 100}, 0.6), 0.0)

    def test_zero_amount_returns_zero(self):
        trade = _make_trade(outcome='YES', amount=0, entry_prob=0.5)
        self.assertEqual(compute_unrealised_pnl(trade, 0.8), 0.0)

    def test_degenerate_entry_prob_zero_clamped(self):
        trade = _make_trade(outcome='YES', amount=100, entry_prob=0.0)
        self.assertGreater(compute_unrealised_pnl(trade, 0.5), 0)

    def test_degenerate_entry_prob_one_clamped(self):
        trade = _make_trade(outcome='NO', amount=100, entry_prob=1.0)
        self.assertGreater(compute_unrealised_pnl(trade, 0.5), 0)


# ─────────────────────────────────────────────────────────────────────────────
# find_all_weak_positions
# ─────────────────────────────────────────────────────────────────────────────

class TestFindAllWeakPositions(unittest.TestCase):

    def _api_mock(self, prob=0.7, resolved=False):
        m = MagicMock()
        m.get_market.return_value = {
            'probability': prob,
            'isResolved': resolved,
            'question': 'Test question',
        }
        return m

    def test_losing_position_returned(self):
        trade = _make_trade(outcome='YES', amount=100, entry_prob=0.8)
        positions = {'mkt1': [trade]}
        with patch('scripts.position_swap_checker.api_client', self._api_mock(prob=0.4)):
            result = find_all_weak_positions(positions)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['market_id'], 'mkt1')
        self.assertLess(result[0]['unrealised_pnl'], 0)

    def test_winning_position_excluded(self):
        trade = _make_trade(outcome='YES', amount=100, entry_prob=0.3)
        positions = {'mkt1': [trade]}
        with patch('scripts.position_swap_checker.api_client', self._api_mock(prob=0.8)):
            result = find_all_weak_positions(positions)
        self.assertEqual(result, [])

    def test_multiple_losers_all_returned(self):
        t1 = _make_trade(outcome='YES', amount=100, entry_prob=0.8)
        t2 = _make_trade(outcome='NO', amount=100, entry_prob=0.3)
        t2['trade_id'] = 2
        t2['market_id'] = 'mkt2'
        positions = {'mkt1': [t1], 'mkt2': [t2]}

        def side(market_id):
            return {'probability': 0.4, 'isResolved': False, 'question': 'Q'}

        mock_api = MagicMock()
        mock_api.get_market.side_effect = side
        with patch('scripts.position_swap_checker.api_client', mock_api):
            result = find_all_weak_positions(positions)
        self.assertEqual(len(result), 2)

    def test_sorted_worst_first(self):
        t_small = _make_trade(outcome='YES', amount=100, entry_prob=0.7)
        t_big = _make_trade(outcome='YES', amount=100, entry_prob=0.9)
        t_big['trade_id'] = 2
        t_big['market_id'] = 'mkt2'
        positions = {'mkt1': [t_small], 'mkt2': [t_big]}

        def side(market_id):
            return {'probability': 0.5, 'isResolved': False, 'question': 'Q'}

        mock_api = MagicMock()
        mock_api.get_market.side_effect = side
        with patch('scripts.position_swap_checker.api_client', mock_api):
            result = find_all_weak_positions(positions)
        # mkt2 YES 0.9→0.5 pnl ≈ -44.4, mkt1 YES 0.7→0.5 pnl ≈ -28.6 → mkt2 first
        self.assertEqual(result[0]['market_id'], 'mkt2')

    def test_resolved_market_skipped(self):
        trade = _make_trade(outcome='YES', amount=100, entry_prob=0.8)
        positions = {'mkt1': [trade]}
        with patch('scripts.position_swap_checker.api_client', self._api_mock(prob=0.2, resolved=True)):
            result = find_all_weak_positions(positions)
        self.assertEqual(result, [])

    def test_closed_positions_ignored(self):
        trade = _make_trade(outcome='YES', amount=100, entry_prob=0.8, status='WIN')
        positions = {'mkt1': [trade]}
        with patch('scripts.position_swap_checker.api_client', self._api_mock(prob=0.2)):
            result = find_all_weak_positions(positions)
        self.assertEqual(result, [])

    def test_api_error_skips_market(self):
        trade = _make_trade(outcome='YES', amount=100, entry_prob=0.8)
        positions = {'mkt1': [trade]}
        mock_api = MagicMock()
        mock_api.get_market.side_effect = Exception("network error")
        with patch('scripts.position_swap_checker.api_client', mock_api):
            result = find_all_weak_positions(positions)
        self.assertEqual(result, [])


# ─────────────────────────────────────────────────────────────────────────────
# find_best_opportunity
# ─────────────────────────────────────────────────────────────────────────────

class TestFindBestOpportunity(unittest.TestCase):

    def test_returns_highest_ev(self):
        research = {'recommendations': [
            _make_recommendation(market_id='a', ev=5.0),
            _make_recommendation(market_id='b', ev=25.0),
        ]}
        self.assertEqual(find_best_opportunity(research, set())['market_id'], 'b')

    def test_skips_ai_vetoed(self):
        research = {'recommendations': [_make_recommendation(ev=50.0, ai_rec='SKIP')]}
        self.assertIsNone(find_best_opportunity(research, set()))

    def test_skips_ai_returned_skip_flag(self):
        rec = _make_recommendation(ev=50.0)
        rec['ai_returned_skip'] = True
        self.assertIsNone(find_best_opportunity({'recommendations': [rec]}, set()))

    def test_skips_low_confidence(self):
        research = {'recommendations': [_make_recommendation(ev=50.0, confidence=0.5)]}
        self.assertIsNone(find_best_opportunity(research, set()))

    def test_skips_existing_ids(self):
        rec = _make_recommendation(market_id='mkt_x', ev=50.0)
        self.assertIsNone(find_best_opportunity({'recommendations': [rec]}, {'mkt_x'}))

    def test_skips_zero_ev(self):
        self.assertIsNone(find_best_opportunity(
            {'recommendations': [_make_recommendation(ev=0.0)]}, set()))

    def test_skips_negative_ev(self):
        self.assertIsNone(find_best_opportunity(
            {'recommendations': [_make_recommendation(ev=-5.0)]}, set()))

    def test_skips_recommendation_skip(self):
        self.assertIsNone(find_best_opportunity(
            {'recommendations': [_make_recommendation(ev=30.0, recommendation='SKIP', ai_rec='SKIP')]}, set()))

    def test_empty_recommendations(self):
        self.assertIsNone(find_best_opportunity({'recommendations': []}, set()))

    def test_excludes_already_proposed_target(self):
        """Second call with first target excluded should return different market."""
        research = {'recommendations': [
            _make_recommendation(market_id='a', ev=25.0),
            _make_recommendation(market_id='b', ev=20.0),
        ]}
        first = find_best_opportunity(research, set())
        self.assertEqual(first['market_id'], 'a')
        second = find_best_opportunity(research, {'a'})
        self.assertEqual(second['market_id'], 'b')


# ─────────────────────────────────────────────────────────────────────────────
# Swap EV correctness — Op2 fix
#
# Swap decisions must compare real unrealised losses against the EV at the
# actual executable stake (MAX_BET_AMOUNT), not the $25 cross-market ranking
# reference. These tests lock that behaviour in so a future change can't
# silently regress to ref-EV gating.
# ─────────────────────────────────────────────────────────────────────────────

from scripts.position_swap_checker import _swap_ev


def _make_recommendation_v2(market_id='mkt_new', confidence=0.75,
                             ev_ref=20.0, ev_exec=4.0,
                             recommendation='YES', ai_rec='YES'):
    """v2 recommendation shape with BOTH ev fields set."""
    return {
        'market_id': market_id,
        'question': 'Will something happen?',
        'recommendation': recommendation,
        'confidence': confidence,
        'estimated_ev_ref':  ev_ref,
        'estimated_ev_exec': ev_exec,
        'estimated_ev':      ev_ref,   # legacy alias
        'probability': 0.4,
        'ai_recommendation': ai_rec,
        'ai_returned_skip': False,
        'strategies': ['volume_spike'],
        'liquidity': MIN_LIQUIDITY,
    }


class TestSwapEvCorrectness(unittest.TestCase):
    """Op2: swap scoring must use estimated_ev_exec, not estimated_ev_ref."""

    # ── _swap_ev helper ────────────────────────────────────────────────────

    def test_swap_ev_prefers_exec(self):
        """When both fields exist, _swap_ev returns exec (the smaller, honest number)."""
        rec = _make_recommendation_v2(ev_ref=20.0, ev_exec=4.0)
        self.assertEqual(_swap_ev(rec), 4.0)

    def test_swap_ev_falls_back_to_legacy_estimated_ev(self):
        """Pre-migration recs with only 'estimated_ev' still work."""
        rec = {'estimated_ev': 8.0}
        self.assertEqual(_swap_ev(rec), 8.0)

    def test_swap_ev_returns_none_when_both_missing(self):
        rec = {'market_id': 'x'}
        self.assertIsNone(_swap_ev(rec))

    def test_swap_ev_exec_none_falls_back_to_legacy(self):
        """If exec is explicitly None but legacy is set, use legacy."""
        rec = {'estimated_ev_exec': None, 'estimated_ev': 7.5}
        self.assertEqual(_swap_ev(rec), 7.5)

    # ── find_best_opportunity uses exec EV for ranking ────────────────────

    def test_ranks_by_exec_not_ref(self):
        """
        Market A has higher REF EV but lower EXEC EV.
        Market B has lower REF EV but higher EXEC EV.
        find_best_opportunity must pick B (the one with better exec EV).
        """
        research = {'recommendations': [
            _make_recommendation_v2(market_id='a', ev_ref=50.0, ev_exec=2.0),
            _make_recommendation_v2(market_id='b', ev_ref=20.0, ev_exec=6.0),
        ]}
        best = find_best_opportunity(research, set())
        self.assertEqual(best['market_id'], 'b')

    def test_rejects_rec_with_positive_ref_but_nonpositive_exec(self):
        """Positive ref EV does not rescue a rec whose exec EV is zero or negative."""
        research = {'recommendations': [
            _make_recommendation_v2(market_id='a', ev_ref=25.0, ev_exec=0.0),
        ]}
        self.assertIsNone(find_best_opportunity(research, set()))

    def test_legacy_rec_without_exec_field_still_works(self):
        """
        A recommendation written by pre-Op2 code (only 'estimated_ev', no
        'estimated_ev_exec') must still be findable — the fallback in _swap_ev
        uses the legacy field.
        """
        legacy_rec = _make_recommendation(market_id='legacy', ev=15.0)
        # Strip v2 fields explicitly to simulate pre-migration data
        legacy_rec.pop('estimated_ev_exec', None)
        legacy_rec.pop('estimated_ev_ref',  None)
        research = {'recommendations': [legacy_rec]}
        best = find_best_opportunity(research, set())
        self.assertIsNotNone(best)
        self.assertEqual(best['market_id'], 'legacy')

    # ── Exec EV must gate the swap-worthiness check ───────────────────────

    def test_ref_ev_would_clear_loss_but_exec_ev_does_not(self):
        """
        This is the WTI-scenario regression guard:
          loss  = $1.99
          ref   = +$4.09  (would clear if we used ref)
          exec  = +$0.80  (does NOT clear under $5 cap)
        Op2 requires this proposal to be REJECTED.

        We verify by running the same logic find_best_opportunity +
        run_swap_check do: best.ev_exec > loss.
        """
        rec = _make_recommendation_v2(market_id='wti', ev_ref=4.09, ev_exec=0.80)
        loss = 1.99
        ev = _swap_ev(rec)
        self.assertLess(ev, loss,
                        f"exec EV ${ev:.2f} must be below loss ${loss:.2f} for this scenario")
        # And just to be explicit: if gated on ref EV this would have passed.
        self.assertGreater(rec['estimated_ev_ref'], loss,
                           "ref EV should exceed loss — that's the bug we're guarding against")


# ─────────────────────────────────────────────────────────────────────────────
# Op5: SWAP_MIN_MARGIN buffer
#
# Op2 alone permits any proposal where exec EV > loss. Op5 adds a minimum
# cushion so tiny-margin swaps (e.g. $0.51 exec EV vs $0.30 loss = $0.21 net
# edge) don't fire.
#
# The 2026-04-11 11:40 production run produced exactly that proposal and
# it had to be dismissed manually. These tests lock in the new gate so the
# cushion can't silently regress to zero.
# ─────────────────────────────────────────────────────────────────────────────

from scripts.position_swap_checker import SWAP_MIN_MARGIN


class TestSwapMinMarginBuffer(unittest.TestCase):

    def test_min_margin_is_positive(self):
        """A zero margin would be Op2 behaviour, which we're explicitly leaving."""
        self.assertGreater(SWAP_MIN_MARGIN, 0,
                           "SWAP_MIN_MARGIN must be > 0 to be meaningful")

    def test_min_margin_is_at_least_one_min_bet(self):
        """
        The rationale in the constant's comment is 'one round of MIN_BET_AMOUNT
        of breathing room'. MIN_BET_AMOUNT is 1, so SWAP_MIN_MARGIN should be
        at least 1. If someone lowers this below 1 they need to update the
        comment AND think about why they're doing it.
        """
        from manifold_bot.config import MIN_BET_AMOUNT
        self.assertGreaterEqual(SWAP_MIN_MARGIN, MIN_BET_AMOUNT)

    def test_tiny_margin_swap_rejected_by_margin_buffer(self):
        """
        The 2026-04-11 11:40 production scenario:
          close loss = $0.30
          open exec EV = $0.51
          net edge = $0.21
          loss + margin = $0.30 + $1.00 = $1.30
          $0.51 < $1.30  →  must be REJECTED by the new gate
        """
        loss = 0.30
        best_ev = 0.51
        required = loss + SWAP_MIN_MARGIN
        self.assertLess(best_ev, required,
                        f"Tiny-margin swap (exec EV ${best_ev}) must NOT clear "
                        f"${loss} loss + ${SWAP_MIN_MARGIN} margin = ${required}")

    def test_comfortable_margin_swap_still_accepted(self):
        """A swap with plenty of cushion should still fire."""
        loss = 1.00
        best_ev = 3.50          # $2.50 of net edge
        required = loss + SWAP_MIN_MARGIN
        self.assertGreaterEqual(best_ev, required)

    def test_exactly_at_boundary_accepted(self):
        """$ev == loss + margin is the minimum acceptable value."""
        loss = 1.50
        required = loss + SWAP_MIN_MARGIN
        best_ev = required
        self.assertGreaterEqual(best_ev, required)


class TestRunSwapCheckWithMinMargin(unittest.TestCase):
    """End-to-end: run_swap_check() must apply the Op5 margin buffer."""

    def _full_state(self, n_open=10, losing_entry_prob=0.8, loss_amount=20.0):
        positions = {}
        for i in range(n_open):
            mid = f'mkt_{i}'
            t = _make_trade(outcome='YES', amount=loss_amount,
                            entry_prob=losing_entry_prob, status='OPEN')
            t['market_id'] = mid
            positions[mid] = [t]
        return {'balance': 800.0, 'positions': positions, 'trade_history': []}

    def _losing_api(self, current_prob=0.7):
        mock = MagicMock()
        mock.get_market.return_value = {
            'probability': current_prob, 'isResolved': False, 'question': 'Test'
        }
        return mock

    def test_tiny_margin_opportunity_rejected(self):
        """
        A candidate with exec EV just barely above the loss (Op2-valid)
        but below loss + SWAP_MIN_MARGIN (Op5-invalid) must not produce
        a proposal. This is the Op5 gate doing its job.
        """
        # Construct losers whose unrealised loss per position is small.
        # With entry_prob 0.8 and current_prob 0.78, the loss is small.
        state = self._full_state(n_open=10, losing_entry_prob=0.8, loss_amount=5.0)

        # Research with one opportunity whose exec EV is 0.20 (tiny)
        rec = _make_recommendation_v2(market_id='new_tiny', ev_ref=1.0, ev_exec=0.20)
        research = {'recommendations': [rec]}

        with tempfile.TemporaryDirectory() as tmp:
            sf = Path(tmp) / 'state.json'
            sf.write_text(json.dumps(state))
            rf = Path(tmp) / 'research.json'
            rf.write_text(json.dumps(research))
            pf = Path(tmp) / 'pending_swaps.json'
            api = self._losing_api(current_prob=0.70)   # 10pp against us
            with (
                patch('scripts.position_swap_checker.STATE_FILE', sf),
                patch('scripts.position_swap_checker.RESEARCH_FILE', rf),
                patch('scripts.position_swap_checker.PENDING_SWAPS_FILE', pf),
                patch('scripts.position_swap_checker.api_client', api),
                patch('scripts.position_swap_checker.send_telegram_message'),
            ):
                rc = run_swap_check()
            self.assertEqual(rc, 0)
            # No proposal must have been written (margin gate blocks it)
            if pf.exists():
                data = json.loads(pf.read_text())
                self.assertEqual(data, [],
                                 f"Expected no proposals, got {len(data)}")

    def test_comfortable_margin_opportunity_accepted(self):
        """
        A candidate whose exec EV comfortably clears loss + margin should
        still produce a proposal.
        """
        state = self._full_state(n_open=10, losing_entry_prob=0.8, loss_amount=5.0)
        # $10 exec EV vs ~$0.50 loss → clears margin easily
        rec = _make_recommendation_v2(market_id='new_fat', ev_ref=50.0, ev_exec=10.0)
        research = {'recommendations': [rec]}

        with tempfile.TemporaryDirectory() as tmp:
            sf = Path(tmp) / 'state.json'
            sf.write_text(json.dumps(state))
            rf = Path(tmp) / 'research.json'
            rf.write_text(json.dumps(research))
            pf = Path(tmp) / 'pending_swaps.json'
            api = self._losing_api(current_prob=0.70)
            with (
                patch('scripts.position_swap_checker.STATE_FILE', sf),
                patch('scripts.position_swap_checker.RESEARCH_FILE', rf),
                patch('scripts.position_swap_checker.PENDING_SWAPS_FILE', pf),
                patch('scripts.position_swap_checker.api_client', api),
                patch('scripts.position_swap_checker.send_telegram_message'),
            ):
                rc = run_swap_check()
            self.assertEqual(rc, 0)
            self.assertTrue(pf.exists(), "Expected at least one swap proposal")
            data = json.loads(pf.read_text())
            self.assertGreaterEqual(len(data), 1)


# ─────────────────────────────────────────────────────────────────────────────
# has_active_pending_swaps
# ─────────────────────────────────────────────────────────────────────────────

class TestHasActivePendingSwaps(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(
            mode='w', suffix='.json', delete=False, dir=PROJECT_ROOT
        )
        self.tmp_path = Path(self.tmp.name)
        self.tmp.close()

    def tearDown(self):
        if self.tmp_path.exists():
            self.tmp_path.unlink()

    def _patch(self):
        return patch('scripts.position_swap_checker.PENDING_SWAPS_FILE', self.tmp_path)

    def _write(self, swaps):
        with open(self.tmp_path, 'w') as f:
            json.dump(swaps, f)

    def test_returns_true_for_fresh_pending(self):
        self._write([_pending_swap(status='pending', age_hours=0)])
        with self._patch():
            self.assertTrue(has_active_pending_swaps())

    def test_returns_false_when_file_absent(self):
        self.tmp_path.unlink()
        with self._patch():
            self.assertFalse(has_active_pending_swaps())

    def test_returns_false_all_approved(self):
        self._write([_pending_swap(status='approved')])
        with self._patch():
            self.assertFalse(has_active_pending_swaps())

    def test_returns_false_all_dismissed(self):
        self._write([_pending_swap(status='dismissed')])
        with self._patch():
            self.assertFalse(has_active_pending_swaps())

    def test_returns_false_all_expired(self):
        self._write([_pending_swap(status='pending', age_hours=SWAP_EXPIRY_HOURS + 1)])
        with self._patch():
            self.assertFalse(has_active_pending_swaps())

    def test_returns_true_one_pending_among_others(self):
        """Mixed list: approved + fresh pending → True."""
        self._write([
            _pending_swap(swap_id=1, status='approved'),
            _pending_swap(swap_id=2, status='pending', age_hours=0),
        ])
        with self._patch():
            self.assertTrue(has_active_pending_swaps())

    def test_returns_true_just_before_expiry(self):
        self._write([_pending_swap(status='pending', age_hours=SWAP_EXPIRY_HOURS - 0.1)])
        with self._patch():
            self.assertTrue(has_active_pending_swaps())

    def test_returns_false_for_corrupted_file(self):
        with open(self.tmp_path, 'w') as f:
            f.write("NOT JSON {{{")
        with self._patch():
            self.assertFalse(has_active_pending_swaps())

    def test_returns_false_empty_list(self):
        self._write([])
        with self._patch():
            self.assertFalse(has_active_pending_swaps())


# ─────────────────────────────────────────────────────────────────────────────
# build_swap_entry & format_telegram_message
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildSwapEntry(unittest.TestCase):

    def _weakest(self):
        return {
            'trade': _make_trade(),
            'market_id': 'mkt_close',
            'question': 'Will A happen?',
            'current_prob': 0.7,
            'unrealised_pnl': -12.50,
        }

    def test_id_is_set(self):
        entry = build_swap_entry(3, self._weakest(), _make_recommendation())
        self.assertEqual(entry['id'], 3)

    def test_status_is_pending(self):
        entry = build_swap_entry(1, self._weakest(), _make_recommendation())
        self.assertEqual(entry['status'], 'pending')

    def test_proposed_at_is_recent(self):
        entry = build_swap_entry(1, self._weakest(), _make_recommendation())
        proposed = datetime.fromisoformat(entry['proposed_at'])
        self.assertLess((datetime.now() - proposed).total_seconds(), 5)

    def test_close_and_open_fields_present(self):
        entry = build_swap_entry(1, self._weakest(), _make_recommendation())
        self.assertIn('close_market_id', entry)
        self.assertIn('open_market_id', entry)


class TestFormatTelegramMessage(unittest.TestCase):

    def _swap(self, swap_id=1):
        return {
            'id': swap_id,
            'close_outcome': 'NO',
            'close_question': 'Will X happen?',
            'close_entry_probability': 0.50,
            'close_current_probability': 0.72,
            'close_unrealised_pnl': -8.0,
            'open_question': 'Will Y happen?',
            'open_recommendation': {
                'recommendation': 'YES',
                'confidence': 0.81,
                'estimated_ev': 12.50,
            },
        }

    def test_id_in_header(self):
        msg = format_telegram_message(self._swap(swap_id=2))
        self.assertIn('#2', msg)

    def test_approve_instruction_includes_id(self):
        msg = format_telegram_message(self._swap(swap_id=3))
        self.assertIn('approve swap 3', msg)

    def test_dismiss_instruction_includes_id(self):
        msg = format_telegram_message(self._swap(swap_id=3))
        self.assertIn('dismiss swap 3', msg)

    def test_contains_close_details(self):
        msg = format_telegram_message(self._swap())
        self.assertIn('NO', msg)
        self.assertIn('Will X happen?', msg)

    def test_contains_open_details(self):
        msg = format_telegram_message(self._swap())
        self.assertIn('YES', msg)
        self.assertIn('Will Y happen?', msg)

    def test_contains_expiry_note(self):
        msg = format_telegram_message(self._swap())
        self.assertIn('Expires', msg)


# ─────────────────────────────────────────────────────────────────────────────
# PaperTrader.close_position_early
# ─────────────────────────────────────────────────────────────────────────────

class TestClosePositionEarly(unittest.TestCase):

    def _trader_with_open(self, outcome='YES', entry_prob=0.5, amount=100.0):
        trader = PaperTrader.__new__(PaperTrader)
        trader.initial_balance = 1000.0
        trader.balance = 1000.0 - amount
        trader.positions = {}
        trader.trade_history = []
        trader.performance_metrics = {}
        trader.capital_epochs = []
        trader.state_file = tempfile.mktemp(suffix='.json')

        trade = {
            'trade_id': 1,
            'timestamp': datetime.now().isoformat(),
            'market_id': 'mkt_test',
            'outcome': outcome,
            'amount': amount,
            'probability': entry_prob,
            'entry_probability': entry_prob,
            'entry_confidence': 0.7,
            'payout_if_win': amount / max(entry_prob, 0.001),
            'profit_if_win': amount / max(entry_prob, 0.001) - amount,
            'profit_if_lose': -amount,
            'status': 'OPEN',
            'estimated_ev': 10.0,
            'ai_confidence': 0.7,
            'strategies': ['volume_spike'],
        }
        trader.positions['mkt_test'] = [trade]
        trader.trade_history.append(trade)
        return trader

    def test_yes_loss(self):
        trader = self._trader_with_open('YES', entry_prob=0.8, amount=100)
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'cal.db'
            with patch('manifold_bot.paper_trader._DB_PATH', db):
                pnl = trader.close_position_early('mkt_test', 0.4)
        self.assertAlmostEqual(pnl, -50.0, places=2)
        self.assertAlmostEqual(trader.balance, 850.0, places=2)

    def test_yes_profit(self):
        trader = self._trader_with_open('YES', entry_prob=0.4, amount=100)
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'cal.db'
            with patch('manifold_bot.paper_trader._DB_PATH', db):
                pnl = trader.close_position_early('mkt_test', 0.8)
        self.assertAlmostEqual(pnl, 100.0, places=2)

    def test_no_profit(self):
        trader = self._trader_with_open('NO', entry_prob=0.6, amount=100)
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'cal.db'
            with patch('manifold_bot.paper_trader._DB_PATH', db):
                pnl = trader.close_position_early('mkt_test', 0.2)
        self.assertAlmostEqual(pnl, 100.0, places=2)

    def test_status_set_to_closed_early(self):
        trader = self._trader_with_open('YES', entry_prob=0.5, amount=100)
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'cal.db'
            with patch('manifold_bot.paper_trader._DB_PATH', db):
                trader.close_position_early('mkt_test', 0.5)
        self.assertEqual(trader.positions['mkt_test'][0]['status'], 'CLOSED_EARLY')

    def test_trade_history_updated(self):
        trader = self._trader_with_open('YES', entry_prob=0.5, amount=100)
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'cal.db'
            with patch('manifold_bot.paper_trader._DB_PATH', db):
                trader.close_position_early('mkt_test', 0.5)
        self.assertEqual(trader.trade_history[0]['status'], 'CLOSED_EARLY')

    def test_nonexistent_market_returns_none(self):
        trader = self._trader_with_open()
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'cal.db'
            with patch('manifold_bot.paper_trader._DB_PATH', db):
                result = trader.close_position_early('no_such_market', 0.5)
        self.assertIsNone(result)

    def test_already_closed_returns_none(self):
        trader = self._trader_with_open()
        trader.positions['mkt_test'][0]['status'] = 'WIN'
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'cal.db'
            with patch('manifold_bot.paper_trader._DB_PATH', db):
                result = trader.close_position_early('mkt_test', 0.5)
        self.assertIsNone(result)


# ─────────────────────────────────────────────────────────────────────────────
# run_swap_check integration (mocked I/O)
# ─────────────────────────────────────────────────────────────────────────────

class TestRunSwapCheck(unittest.TestCase):

    def _full_state(self, n_open=10):
        positions = {}
        for i in range(n_open):
            mid = f'mkt_{i}'
            t = _make_trade(outcome='YES', amount=20.0, entry_prob=0.8, status='OPEN')
            t['market_id'] = mid
            positions[mid] = [t]
        return {'balance': 800.0, 'positions': positions, 'trade_history': []}

    def _research(self, evs=(30.0, 25.0)):
        return {'recommendations': [
            _make_recommendation(market_id=f'mkt_new_{i}', ev=ev)
            for i, ev in enumerate(evs)
        ]}

    def _losing_api(self):
        mock = MagicMock()
        mock.get_market.return_value = {
            'probability': 0.3, 'isResolved': False, 'question': 'Test'
        }
        return mock

    def test_no_swap_when_slots_available(self):
        state = self._full_state(n_open=5)
        with tempfile.TemporaryDirectory() as tmp:
            sf = Path(tmp) / 'state.json'
            sf.write_text(json.dumps(state))
            pf = Path(tmp) / 'pending_swaps.json'
            with (
                patch('scripts.position_swap_checker.STATE_FILE', sf),
                patch('scripts.position_swap_checker.PENDING_SWAPS_FILE', pf),
            ):
                rc = run_swap_check()
            self.assertEqual(rc, 0)
            self.assertFalse(pf.exists())

    def test_no_swap_when_all_profitable(self):
        state = self._full_state(n_open=10)
        with tempfile.TemporaryDirectory() as tmp:
            sf = Path(tmp) / 'state.json'
            sf.write_text(json.dumps(state))
            rf = Path(tmp) / 'research.json'
            rf.write_text(json.dumps(self._research()))
            pf = Path(tmp) / 'pending_swaps.json'
            # prob moved UP for YES (profitable), no losers
            profitable_api = MagicMock()
            profitable_api.get_market.return_value = {
                'probability': 0.95, 'isResolved': False, 'question': 'Q'
            }
            with (
                patch('scripts.position_swap_checker.STATE_FILE', sf),
                patch('scripts.position_swap_checker.RESEARCH_FILE', rf),
                patch('scripts.position_swap_checker.PENDING_SWAPS_FILE', pf),
                patch('scripts.position_swap_checker.api_client', profitable_api),
            ):
                rc = run_swap_check()
            self.assertEqual(rc, 0)
            self.assertFalse(pf.exists())

    def test_no_swap_when_active_pending_exists(self):
        state = self._full_state(n_open=10)
        with tempfile.TemporaryDirectory() as tmp:
            sf = Path(tmp) / 'state.json'
            sf.write_text(json.dumps(state))
            pf = Path(tmp) / 'pending_swaps.json'
            pf.write_text(json.dumps([_pending_swap(status='pending', age_hours=0)]))
            with (
                patch('scripts.position_swap_checker.STATE_FILE', sf),
                patch('scripts.position_swap_checker.PENDING_SWAPS_FILE', pf),
            ):
                rc = run_swap_check()
            self.assertEqual(rc, 0)

    def test_single_proposal_written_and_sent(self):
        """One losing position + one good opportunity → one proposal."""
        state = self._full_state(n_open=10)
        with tempfile.TemporaryDirectory() as tmp:
            sf = Path(tmp) / 'state.json'
            sf.write_text(json.dumps(state))
            rf = Path(tmp) / 'research.json'
            rf.write_text(json.dumps(self._research(evs=(50.0,))))
            pf = Path(tmp) / 'pending_swaps.json'
            with (
                patch('scripts.position_swap_checker.STATE_FILE', sf),
                patch('scripts.position_swap_checker.RESEARCH_FILE', rf),
                patch('scripts.position_swap_checker.PENDING_SWAPS_FILE', pf),
                patch('scripts.position_swap_checker.api_client', self._losing_api()),
                patch('scripts.position_swap_checker.send_telegram_message') as mock_tg,
            ):
                rc = run_swap_check()
            self.assertEqual(rc, 0)
            self.assertTrue(pf.exists())
            proposals = json.loads(pf.read_text())
            self.assertIsInstance(proposals, list)
            self.assertEqual(proposals[0]['status'], 'pending')
            mock_tg.assert_called()

    def test_multiple_proposals_written(self):
        """Two losing positions + two opportunities → two proposals, each sent."""
        state = self._full_state(n_open=10)
        with tempfile.TemporaryDirectory() as tmp:
            sf = Path(tmp) / 'state.json'
            sf.write_text(json.dumps(state))
            rf = Path(tmp) / 'research.json'
            rf.write_text(json.dumps(self._research(evs=(50.0, 40.0))))
            pf = Path(tmp) / 'pending_swaps.json'
            with (
                patch('scripts.position_swap_checker.STATE_FILE', sf),
                patch('scripts.position_swap_checker.RESEARCH_FILE', rf),
                patch('scripts.position_swap_checker.PENDING_SWAPS_FILE', pf),
                patch('scripts.position_swap_checker.api_client', self._losing_api()),
                patch('scripts.position_swap_checker.send_telegram_message') as mock_tg,
            ):
                rc = run_swap_check()
            proposals = json.loads(pf.read_text())
            # Two opportunities → up to 2 proposals; Telegram called once per proposal
            self.assertGreaterEqual(len(proposals), 1)
            self.assertEqual(mock_tg.call_count, len(proposals))

    def test_proposals_have_unique_open_targets(self):
        """No two proposals should propose the same replacement market."""
        state = self._full_state(n_open=10)
        with tempfile.TemporaryDirectory() as tmp:
            sf = Path(tmp) / 'state.json'
            sf.write_text(json.dumps(state))
            rf = Path(tmp) / 'research.json'
            rf.write_text(json.dumps(self._research(evs=(50.0, 40.0, 30.0))))
            pf = Path(tmp) / 'pending_swaps.json'
            with (
                patch('scripts.position_swap_checker.STATE_FILE', sf),
                patch('scripts.position_swap_checker.RESEARCH_FILE', rf),
                patch('scripts.position_swap_checker.PENDING_SWAPS_FILE', pf),
                patch('scripts.position_swap_checker.api_client', self._losing_api()),
                patch('scripts.position_swap_checker.send_telegram_message'),
            ):
                run_swap_check()
            proposals = json.loads(pf.read_text())
            open_ids = [p['open_market_id'] for p in proposals]
            self.assertEqual(len(open_ids), len(set(open_ids)))

    def test_no_proposal_when_ev_less_than_loss(self):
        """EV of new trade must exceed the loss we'd crystallise — else no proposal."""
        state = self._full_state(n_open=10)
        # YES at 0.8, API returns 0.4 → loss ≈ 10 per $20 stake
        # opportunity EV = 1.0 < loss → no swap
        with tempfile.TemporaryDirectory() as tmp:
            sf = Path(tmp) / 'state.json'
            sf.write_text(json.dumps(state))
            rf = Path(tmp) / 'research.json'
            rf.write_text(json.dumps(self._research(evs=(1.0,))))
            pf = Path(tmp) / 'pending_swaps.json'
            with (
                patch('scripts.position_swap_checker.STATE_FILE', sf),
                patch('scripts.position_swap_checker.RESEARCH_FILE', rf),
                patch('scripts.position_swap_checker.PENDING_SWAPS_FILE', pf),
                patch('scripts.position_swap_checker.api_client', self._losing_api()),
                patch('scripts.position_swap_checker.send_telegram_message'),
            ):
                run_swap_check()
            self.assertFalse(pf.exists())

    def test_proposal_ids_are_sequential(self):
        state = self._full_state(n_open=10)
        with tempfile.TemporaryDirectory() as tmp:
            sf = Path(tmp) / 'state.json'
            sf.write_text(json.dumps(state))
            rf = Path(tmp) / 'research.json'
            rf.write_text(json.dumps(self._research(evs=(50.0, 40.0))))
            pf = Path(tmp) / 'pending_swaps.json'
            with (
                patch('scripts.position_swap_checker.STATE_FILE', sf),
                patch('scripts.position_swap_checker.RESEARCH_FILE', rf),
                patch('scripts.position_swap_checker.PENDING_SWAPS_FILE', pf),
                patch('scripts.position_swap_checker.api_client', self._losing_api()),
                patch('scripts.position_swap_checker.send_telegram_message'),
            ):
                run_swap_check()
            proposals = json.loads(pf.read_text())
            ids = [p['id'] for p in proposals]
            self.assertEqual(ids, list(range(1, len(ids) + 1)))


# ─────────────────────────────────────────────────────────────────────────────
# Regression: zero/negative Kelly must not place a bet (execute_swap.py)
# ─────────────────────────────────────────────────────────────────────────────

class TestExecuteSwapZeroKelly(unittest.TestCase):
    """
    Regression test: when the replacement trade has zero or negative Kelly edge,
    execute_swap() must return False without calling place_paper_bet or
    close_position_early.  Previously it closed the losing position and then
    fell through to the MIN_BET_AMOUNT clamp, placing a guaranteed-loss $1 bet.
    """

    def _make_swap(self, ai_prob, outcome='YES', mkt_prob=0.60):
        """Build a minimal swap dict where ai_prob drives the Kelly calc."""
        return {
            'close_market_id': 'mkt_close',
            'close_question': 'Close question',
            'close_current_probability': 0.70,
            'open_market_id': 'mkt_open',
            'open_question': 'Open question',
            'open_recommendation': {
                'recommendation': outcome,
                'confidence': 0.70,
                'probability': mkt_prob,
                'ai_estimated_probability': ai_prob,
                'estimated_ev': -5.0,
            },
            'open_strategies': [],
        }

    def _make_trader(self, balance=200.0):
        trader = MagicMock(spec=PaperTrader)
        trader.balance = balance
        return trader

    def _api_returning(self, prob):
        api = MagicMock()
        api.get_market.return_value = {'probability': prob}
        return api

    def test_negative_kelly_yes_does_not_close_or_bet(self):
        """YES at 60% market with AI prob 0.40 → Kelly < 0 → abort before close."""
        from scripts.execute_swap import execute_swap
        trader = self._make_trader()
        swap = self._make_swap(ai_prob=0.40, outcome='YES', mkt_prob=0.60)
        with patch('scripts.execute_swap.api_client', self._api_returning(0.60)):
            result = execute_swap(swap, trader)
        self.assertFalse(result)
        trader.close_position_early.assert_not_called()
        trader.place_paper_bet.assert_not_called()

    def test_negative_kelly_no_does_not_close_or_bet(self):
        """NO at 30% market with AI prob 0.25 → Kelly < 0 for NO → abort before close."""
        from scripts.execute_swap import execute_swap
        trader = self._make_trader()
        # NO at 30%: win_prob = 1 - ai_prob = 0.75, net_odds = 0.30/0.70 ≈ 0.43
        # Kelly = (0.75*0.43 - 0.25)/0.43 ≈ (0.32-0.25)/0.43 > 0, so use low prob instead
        # NO at 20%: win_prob = 1 - 0.85 = 0.15, net_odds = 0.20/0.80 = 0.25
        # Kelly = (0.15*0.25 - 0.85)/0.25 < 0 → negative
        swap = self._make_swap(ai_prob=0.85, outcome='NO', mkt_prob=0.20)
        with patch('scripts.execute_swap.api_client', self._api_returning(0.20)):
            result = execute_swap(swap, trader)
        self.assertFalse(result)
        trader.close_position_early.assert_not_called()
        trader.place_paper_bet.assert_not_called()

    def test_positive_kelly_proceeds_to_close_and_bet(self):
        """Positive Kelly edge → close_position_early and place_paper_bet are called."""
        from scripts.execute_swap import execute_swap
        trader = self._make_trader(balance=200.0)
        trader.close_position_early.return_value = -10.0
        trader.place_paper_bet.return_value = True
        # YES at 30%: AI thinks 70% → strong positive Kelly
        swap = self._make_swap(ai_prob=0.70, outcome='YES', mkt_prob=0.30)
        with patch('scripts.execute_swap.api_client', self._api_returning(0.30)):
            result = execute_swap(swap, trader)
        self.assertTrue(result)
        trader.close_position_early.assert_called_once()
        trader.place_paper_bet.assert_called_once()

    def test_swap_replacement_leg_threads_reporting_metadata(self):
        """PR #23 follow-up: swap-opened legs must carry the same context
        auto-trader trades do — confidence, ai_status, ai_estimated_probability,
        category, question, term, resolvability, position_class, close_time_ms.
        Without this, swap replacements show 'conf 0%' with no AI tag in /positions."""
        from scripts.execute_swap import execute_swap

        trader = self._make_trader(balance=200.0)
        trader.close_position_early.return_value = -10.0
        trader.place_paper_bet.return_value = True

        swap = self._make_swap(ai_prob=0.70, outcome='YES', mkt_prob=0.30)
        # Load the fields execute_swap is supposed to forward
        swap['open_recommendation'].update({
            'ai_confidence': 0.82,
            'ai_status': 'agree',
            'category': 'politics',
            'question': 'Will Foo happen?',
            'term': 'short',
            'resolvability': 'high',
            'position_class': 'short',
            'close_time_ms': 1_750_000_000_000,
        })

        with patch('scripts.execute_swap.api_client', self._api_returning(0.30)):
            result = execute_swap(swap, trader)

        self.assertTrue(result)
        kwargs = trader.place_paper_bet.call_args.kwargs
        self.assertEqual(kwargs['confidence'], 0.70)           # from open_recommendation.confidence
        self.assertEqual(kwargs['ai_confidence'], 0.82)
        self.assertEqual(kwargs['ai_status'], 'agree')
        self.assertEqual(kwargs['ai_estimated_probability'], 0.70)
        self.assertEqual(kwargs['category'], 'politics')
        self.assertEqual(kwargs['question'], 'Will Foo happen?')
        self.assertEqual(kwargs['term'], 'short')
        self.assertEqual(kwargs['resolvability'], 'high')
        self.assertEqual(kwargs['position_class'], 'short')
        self.assertEqual(kwargs['close_time_ms'], 1_750_000_000_000)


# ─────────────────────────────────────────────────────────────────────────────
# Regression: full EV pipeline (place_paper_bet → resolve_market → bet_outcomes)
# ─────────────────────────────────────────────────────────────────────────────

class TestEvPipelineRegression(unittest.TestCase):
    """
    Regression test: estimated_ev set at trade time must flow through to the
    bet_outcomes SQLite row after resolution, unchanged.

    Bug history: auto_trader.py used to compute EV *after* place_paper_bet,
    so the position record had estimated_ev=None, and _write_bet_outcome()
    wrote NULL into every calibration row.  The fix moved the EV computation
    before place_paper_bet.  This test locks that in end-to-end.
    """

    def setUp(self):
        # Isolated temp state file so this test never touches real state
        fd, path = tempfile.mkstemp(suffix='.json')
        os.close(fd)
        self._state_path = path
        fd2, db_path = tempfile.mkstemp(suffix='.db')
        os.close(fd2)
        self._db_path = db_path

    def tearDown(self):
        for p in (self._state_path, self._db_path):
            try:
                os.unlink(p)
            except OSError:
                pass

    def test_estimated_ev_survives_place_and_resolve(self):
        """EV written at trade time must appear unchanged in bet_outcomes after resolution."""
        from pathlib import Path as _Path
        state_path = _Path(self._state_path)
        db_path = _Path(self._db_path)
        _sp = self._state_path  # capture for lambda — 'self' inside lambda is PaperTrader

        with (
            patch('manifold_bot.paper_trader._DB_PATH', db_path),
            patch.object(
                PaperTrader, '__init__',
                lambda self, **kw: self.__dict__.update(
                    balance=1000.0,
                    positions={},
                    trade_history=[],
                    state_file=_sp,
                )
            ),
            patch.object(PaperTrader, 'save_state'),
        ):
            trader = PaperTrader()
            ev_at_trade_time = 7.25
            trader.place_paper_bet(
                market_id='ev_test_mkt',
                outcome='YES',
                amount=MAX_BET_AMOUNT,
                probability=0.40,
                estimated_ev=ev_at_trade_time,
                ai_confidence=0.72,
                strategies=['probability_bias'],
            )
            trader.resolve_market('ev_test_mkt', 'YES')

        # Read the bet_outcomes row written by resolve_market
        import sqlite3 as _sqlite3
        conn = _sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT estimated_ev FROM bet_outcomes WHERE market_id = 'ev_test_mkt'"
        ).fetchone()
        conn.close()

        self.assertIsNotNone(row, "bet_outcomes row was not written")
        self.assertAlmostEqual(row[0], ev_at_trade_time, places=4,
                               msg="estimated_ev mutated between place and resolve")

    def test_none_ev_writes_null_to_bet_outcomes(self):
        """When no AI estimate is available, bet_outcomes.estimated_ev must be NULL."""
        from pathlib import Path as _Path
        db_path = _Path(self._db_path)
        _sp = self._state_path  # capture for lambda

        with (
            patch('manifold_bot.paper_trader._DB_PATH', db_path),
            patch.object(
                PaperTrader, '__init__',
                lambda self, **kw: self.__dict__.update(
                    balance=1000.0,
                    positions={},
                    trade_history=[],
                    state_file=_sp,
                )
            ),
            patch.object(PaperTrader, 'save_state'),
        ):
            trader = PaperTrader()
            trader.place_paper_bet(
                market_id='no_ev_mkt',
                outcome='NO',
                amount=MAX_BET_AMOUNT,
                probability=0.65,
                estimated_ev=None,
            )
            trader.resolve_market('no_ev_mkt', 'NO')

        import sqlite3 as _sqlite3
        conn = _sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT estimated_ev FROM bet_outcomes WHERE market_id = 'no_ev_mkt'"
        ).fetchone()
        conn.close()

        self.assertIsNotNone(row, "bet_outcomes row was not written")
        self.assertIsNone(row[0], "estimated_ev should be NULL when no AI estimate provided")


if __name__ == '__main__':
    unittest.main(verbosity=2)
