#!/usr/bin/env python3
"""Tests for the post-dismissal cooldown on close + swap proposals.

Bug: once the operator dismissed a proposal, the suppression check only
looked at status=='pending' entries, so the next hourly run re-proposed the
same market because dismissed rows fell out of the suppression set. This
flooded Telegram with identical proposals on markets where scores barely
drift between runs.

Fix: both systems now treat a 'dismissed' proposal as a suppression signal
for DISMISS_COOLDOWN_HOURS after dismissed_at (falling back to proposed_at
for older rows that lack dismissed_at).
"""

import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.evaluate_positions import (
    _existing_pending_ids,
    CLOSE_EXPIRY_HOURS,
    DISMISS_COOLDOWN_HOURS as CLOSE_DISMISS_COOLDOWN_HOURS,
)
from scripts import position_swap_checker
from scripts.position_swap_checker import (
    recently_dismissed_close_ids,
    DISMISS_COOLDOWN_HOURS as SWAP_DISMISS_COOLDOWN_HOURS,
)


def _iso_hours_ago(hours: float) -> str:
    return (datetime.now() - timedelta(hours=hours)).isoformat()


class TestClosedProposalCooldown(unittest.TestCase):
    """evaluate_positions._existing_pending_ids must suppress fresh
    dismissals for DISMISS_COOLDOWN_HOURS."""

    def test_fresh_pending_is_suppressed(self):
        pending = [{
            'market_id': 'mkt_A',
            'status':    'pending',
            'proposed_at': _iso_hours_ago(1),
        }]
        self.assertIn('mkt_A', _existing_pending_ids(pending))

    def test_expired_pending_is_not_suppressed(self):
        pending = [{
            'market_id': 'mkt_A',
            'status':    'pending',
            'proposed_at': _iso_hours_ago(CLOSE_EXPIRY_HOURS + 1),
        }]
        self.assertNotIn('mkt_A', _existing_pending_ids(pending))

    def test_fresh_dismissal_is_suppressed(self):
        pending = [{
            'market_id': 'mkt_A',
            'status':    'dismissed',
            'proposed_at':  _iso_hours_ago(2),
            'dismissed_at': _iso_hours_ago(1),
        }]
        self.assertIn('mkt_A', _existing_pending_ids(pending))

    def test_dismissal_past_cooldown_is_not_suppressed(self):
        pending = [{
            'market_id': 'mkt_A',
            'status':    'dismissed',
            'proposed_at':  _iso_hours_ago(CLOSE_DISMISS_COOLDOWN_HOURS + 2),
            'dismissed_at': _iso_hours_ago(CLOSE_DISMISS_COOLDOWN_HOURS + 1),
        }]
        self.assertNotIn('mkt_A', _existing_pending_ids(pending))

    def test_legacy_dismissal_falls_back_to_proposed_at(self):
        """Older rows didn't carry dismissed_at. Proposal 1h ago still counts."""
        pending = [{
            'market_id': 'mkt_A',
            'status':    'dismissed',
            'proposed_at': _iso_hours_ago(1),
        }]
        self.assertIn('mkt_A', _existing_pending_ids(pending))

    def test_approved_does_not_suppress(self):
        """An approved proposal means the position is already gone; the
        market_id might be re-acquired later and should not be locked out."""
        pending = [{
            'market_id': 'mkt_A',
            'status':    'approved',
            'proposed_at': _iso_hours_ago(1),
        }]
        self.assertNotIn('mkt_A', _existing_pending_ids(pending))


class TestSwapProposalCooldown(unittest.TestCase):
    """position_swap_checker.recently_dismissed_close_ids returns close_market_ids
    whose most recent swap dismissal is within the cooldown."""

    def _patch_swaps(self, swaps):
        return patch.object(position_swap_checker, 'load_pending_swaps',
                            return_value=swaps)

    def test_fresh_swap_dismissal_is_in_cooldown(self):
        swaps = [{
            'close_market_id': 'mkt_close_A',
            'open_market_id':  'mkt_open_X',
            'status':          'dismissed',
            'proposed_at':     _iso_hours_ago(2),
            'dismissed_at':    _iso_hours_ago(1),
        }]
        with self._patch_swaps(swaps):
            ids = recently_dismissed_close_ids()
        self.assertEqual(ids, {'mkt_close_A'})

    def test_swap_dismissal_past_cooldown_excluded(self):
        swaps = [{
            'close_market_id': 'mkt_close_A',
            'open_market_id':  'mkt_open_X',
            'status':          'dismissed',
            'proposed_at':     _iso_hours_ago(SWAP_DISMISS_COOLDOWN_HOURS + 2),
            'dismissed_at':    _iso_hours_ago(SWAP_DISMISS_COOLDOWN_HOURS + 1),
        }]
        with self._patch_swaps(swaps):
            ids = recently_dismissed_close_ids()
        self.assertEqual(ids, set())

    def test_swap_dismissal_legacy_falls_back_to_proposed_at(self):
        swaps = [{
            'close_market_id': 'mkt_close_A',
            'status':          'dismissed',
            'proposed_at':     _iso_hours_ago(1),
        }]
        with self._patch_swaps(swaps):
            ids = recently_dismissed_close_ids()
        self.assertEqual(ids, {'mkt_close_A'})

    def test_pending_and_approved_not_in_cooldown_set(self):
        swaps = [
            {'close_market_id': 'mkt_close_P', 'status': 'pending',
             'proposed_at': _iso_hours_ago(1)},
            {'close_market_id': 'mkt_close_A', 'status': 'approved',
             'proposed_at': _iso_hours_ago(1), 'executed_at': _iso_hours_ago(1)},
        ]
        with self._patch_swaps(swaps):
            ids = recently_dismissed_close_ids()
        self.assertEqual(ids, set())

    def test_cooldown_suppresses_per_close_market_not_globally(self):
        """Dismissing one close market must not lock out every other market."""
        swaps = [{
            'close_market_id': 'mkt_close_A',
            'status':          'dismissed',
            'proposed_at':     _iso_hours_ago(2),
            'dismissed_at':    _iso_hours_ago(1),
        }]
        with self._patch_swaps(swaps):
            ids = recently_dismissed_close_ids()
        self.assertIn('mkt_close_A', ids)
        self.assertNotIn('mkt_close_B', ids)


if __name__ == '__main__':
    unittest.main()
