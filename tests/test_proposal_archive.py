#!/usr/bin/env python3
"""Tests for manifold_bot/proposal_archive.py — weekly rotation of
pending close / swap / abandon proposal lists.

Operator asked for weekly ID resets after seeing close proposal #152.
This module's `rotate_if_new_week()` is shared by evaluate_positions,
position_swap_checker, and detect_stale_positions to provide that
behavior consistently.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from manifold_bot.proposal_archive import rotate_if_new_week, _iso_week_key


class TestIsoWeekKey(unittest.TestCase):

    def test_format(self):
        # Monday 2026-04-27 is in ISO week 18 of 2026
        self.assertEqual(_iso_week_key(datetime(2026, 4, 27, tzinfo=timezone.utc)), "2026-W18")

    def test_year_boundary(self):
        # Some early-Jan dates belong to the prior year's last ISO week
        # 2026-01-01 was a Thursday — ISO week 1 of 2026
        self.assertEqual(_iso_week_key(datetime(2026, 1, 1, tzinfo=timezone.utc)), "2026-W01")


class TestRotateIfNewWeek(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ts(self, *args):
        return datetime(*args, tzinfo=timezone.utc).isoformat()

    def test_empty_list_returns_unchanged(self):
        out = rotate_if_new_week([], self.tmp, "pending_closes",
                                 now=datetime(2026, 5, 5, tzinfo=timezone.utc))
        self.assertEqual(out, [])

    def test_same_week_no_rotation(self):
        # Both proposed_at and now in the same ISO week (week 19 of 2026)
        pending = [
            {"id": 1, "status": "pending", "proposed_at": self._ts(2026, 5, 5, 12)},
            {"id": 2, "status": "pending", "proposed_at": self._ts(2026, 5, 6, 12)},
        ]
        now = datetime(2026, 5, 7, tzinfo=timezone.utc)
        out = rotate_if_new_week(pending, self.tmp, "pending_closes", now=now)
        self.assertIs(out, pending)  # exact same object — no rotation occurred
        self.assertFalse(any(self.tmp.iterdir()))  # no archive file written

    def test_new_week_archives_full_list_and_renumbers_active(self):
        """Across week boundary: archive everything, keep only pending entries
        renumbered from 1."""
        pending = [
            {"id": 50, "status": "pending",   "proposed_at": self._ts(2026, 4, 30, 14), "market_id": "A"},
            {"id": 51, "status": "approved",  "proposed_at": self._ts(2026, 4, 30, 15), "market_id": "B"},
            {"id": 52, "status": "dismissed", "proposed_at": self._ts(2026, 4, 30, 16), "market_id": "C"},
            {"id": 53, "status": "pending",   "proposed_at": self._ts(2026, 4, 30, 17), "market_id": "D"},
            {"id": 54, "status": "expired",   "proposed_at": self._ts(2026, 4, 30, 18), "market_id": "E"},
        ]
        # latest in week 18 (Apr 30); now is in week 19 (May 5)
        now = datetime(2026, 5, 5, 12, tzinfo=timezone.utc)
        out = rotate_if_new_week(pending, self.tmp, "pending_closes", now=now)

        # Archive should exist with all 5 entries
        archive = self.tmp / "pending_closes_archive_2026-W18.jsonl"
        self.assertTrue(archive.exists())
        with open(archive) as f:
            lines = [json.loads(L) for L in f]
        self.assertEqual(len(lines), 5)
        self.assertEqual([r["market_id"] for r in lines], ["A", "B", "C", "D", "E"])

        # Returned list keeps only status==pending, renumbered from 1
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["id"], 1)
        self.assertEqual(out[0]["market_id"], "A")
        self.assertEqual(out[1]["id"], 2)
        self.assertEqual(out[1]["market_id"], "D")

    def test_archive_appends_across_runs(self):
        """If two rotations occur with the same archive_key (rare but possible
        if clock jumps), the archive file appends rather than overwrites."""
        first = [{"id": 1, "status": "pending", "proposed_at": self._ts(2026, 4, 30, 14), "market_id": "A"}]
        # Set "now" to next week — triggers rotation
        now = datetime(2026, 5, 5, 12, tzinfo=timezone.utc)
        rotate_if_new_week(first, self.tmp, "pending_closes", now=now)

        second = [{"id": 1, "status": "pending", "proposed_at": self._ts(2026, 4, 28, 14), "market_id": "B"}]
        rotate_if_new_week(second, self.tmp, "pending_closes", now=now)

        archive = self.tmp / "pending_closes_archive_2026-W18.jsonl"
        with open(archive) as f:
            lines = [json.loads(L) for L in f]
        self.assertEqual(len(lines), 2)
        self.assertEqual({r["market_id"] for r in lines}, {"A", "B"})

    def test_malformed_timestamps_no_rotation(self):
        pending = [{"id": 1, "status": "pending", "proposed_at": "not-a-date"}]
        now = datetime(2026, 5, 5, tzinfo=timezone.utc)
        out = rotate_if_new_week(pending, self.tmp, "pending_closes", now=now)
        # No parseable timestamps → return unchanged, don't archive
        self.assertIs(out, pending)
        self.assertFalse(any(self.tmp.iterdir()))

    def test_renumbering_preserves_other_fields(self):
        """The id is overwritten but every other field (market_id, score,
        question, etc.) survives the rotation."""
        pending = [
            {"id": 100, "status": "pending", "proposed_at": self._ts(2026, 4, 30, 14),
             "market_id": "X", "score": -3.5, "question": "Will X happen?"},
        ]
        now = datetime(2026, 5, 5, tzinfo=timezone.utc)
        out = rotate_if_new_week(pending, self.tmp, "pending_closes", now=now)
        self.assertEqual(out[0]["id"], 1)
        self.assertEqual(out[0]["market_id"], "X")
        self.assertEqual(out[0]["score"], -3.5)
        self.assertEqual(out[0]["question"], "Will X happen?")

    def test_does_not_mutate_input_list(self):
        """rotate_if_new_week must not mutate the input list — the caller's
        reference shouldn't change unexpectedly."""
        pending = [
            {"id": 50, "status": "pending", "proposed_at": self._ts(2026, 4, 30, 14)},
        ]
        now = datetime(2026, 5, 5, tzinfo=timezone.utc)
        rotate_if_new_week(pending, self.tmp, "pending_closes", now=now)
        # Original entry unchanged
        self.assertEqual(pending[0]["id"], 50)


if __name__ == "__main__":
    unittest.main()
