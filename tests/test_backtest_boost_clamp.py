#!/usr/bin/env python3
"""Tests for the asymmetric backtest-boost clamp (PR #27).

Rule: a backtest may DOWN-weight a strategy (defensive — useful when the
strategy is genuinely bad) but cannot UP-weight above 1.0 without live
confirmation (n >= MIN_SAMPLES_PER_STRATEGY). Boost weights without live
data are dangerous: an over-fit reconstruction can manufacture apparent
edge that pushes the trader into MORE trades on a fictional signal.

Both write paths to data/strategy_weights.json honor the same clamp:
  - scripts/backtest_from_snapshots.py — at merge time
  - scripts/compute_strategy_weights.py — at preserve-existing time
"""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from scripts.backtest_from_snapshots import MAX_BACKTEST_ONLY_WEIGHT


class TestBacktestMergeClamp(unittest.TestCase):
    """backtest_from_snapshots.merge_weights applies the clamp at merge."""

    def _run_merge(self, live_weights, backtest_weights, live_per_strat):
        from scripts.backtest_from_snapshots import merge_weights

        fd, path = tempfile.mkstemp(suffix='.json')
        os.close(fd)
        try:
            with open(path, 'w') as f:
                json.dump({
                    "weights": live_weights,
                    "per_strategy_samples": live_per_strat,
                }, f)
            merged, _note = merge_weights(backtest_weights, live_weights_path=path)
            return merged
        finally:
            os.unlink(path)

    def test_backtest_boost_above_one_is_clamped_when_no_live_data(self):
        """The exact case from the Apr-12 backtest: probability_bias=1.2 with
        0 live samples. Must be clamped to 1.0."""
        merged = self._run_merge(
            live_weights={'probability_bias': 1.0},
            backtest_weights={'probability_bias': 1.2},
            live_per_strat={'probability_bias': 0},
        )
        self.assertEqual(merged['probability_bias']['weight'], 1.0)
        self.assertEqual(merged['probability_bias']['source'], 'backtest_clamped')

    def test_backtest_below_one_is_preserved_defensive(self):
        """Backtest can DOWN-weight without live confirmation — that's the
        defensive direction and is exactly what we want (warns us off a
        bad strategy)."""
        merged = self._run_merge(
            live_weights={'mean_reversion': 1.0},
            backtest_weights={'mean_reversion': 0.7},
            live_per_strat={'mean_reversion': 0},
        )
        self.assertEqual(merged['mean_reversion']['weight'], 0.7)
        self.assertEqual(merged['mean_reversion']['source'], 'backtest')

    def test_backtest_exactly_one_is_unclamped(self):
        merged = self._run_merge(
            live_weights={'mean_reversion': 1.0},
            backtest_weights={'mean_reversion': 1.0},
            live_per_strat={'mean_reversion': 0},
        )
        self.assertEqual(merged['mean_reversion']['weight'], 1.0)
        self.assertEqual(merged['mean_reversion']['source'], 'backtest')

    def test_live_boost_overrides_backtest_clamp_with_sufficient_samples(self):
        """Once live data clears MIN_SAMPLES_PER_STRATEGY, the live weight
        wins regardless of the clamp — live data is ground truth and the
        per-formula cap (1.20) handles the upper bound."""
        from scripts.backtest_from_snapshots import MIN_SAMPLES_PER_STRATEGY
        merged = self._run_merge(
            live_weights={'probability_bias': 1.15},
            backtest_weights={'probability_bias': 1.2},
            live_per_strat={'probability_bias': MIN_SAMPLES_PER_STRATEGY},
        )
        self.assertEqual(merged['probability_bias']['weight'], 1.15)
        self.assertEqual(merged['probability_bias']['source'], 'live')


class TestComputeWeightsPreserveClamp(unittest.TestCase):
    """compute_strategy_weights main() applies the same clamp when
    preserving existing file weights for under-sampled strategies."""

    def _run_main(self, existing_file_weights, outcomes):
        """Run main() against a temp WEIGHTS_PATH seeded with given weights."""
        from scripts import compute_strategy_weights as cs

        fd, weights_path = tempfile.mkstemp(suffix='.json')
        os.close(fd)
        fd, cat_path = tempfile.mkstemp(suffix='.json')
        os.close(fd)
        try:
            with open(weights_path, 'w') as f:
                json.dump({"weights": existing_file_weights}, f)

            with patch.object(cs, '_fetch_outcomes', return_value=outcomes), \
                 patch.object(cs, '_WEIGHTS_PATH', weights_path), \
                 patch.object(cs, '_CATEGORY_ACCURACY_PATH', cat_path):
                cs.main(dry_run=False)

            with open(weights_path) as f:
                return json.load(f)
        finally:
            os.unlink(weights_path)
            if os.path.exists(cat_path):
                os.unlink(cat_path)

    def test_preserved_weight_above_one_is_clamped(self):
        """Apr-12 backtest seeded probability_bias=1.2. With zero live
        samples, Monday's compute_strategy_weights run must NOT propagate
        the 1.2 — clamp to 1.0."""
        result = self._run_main(
            existing_file_weights={
                'probability_bias': 1.2,
                'probability_direction': 1.0,
                'mean_reversion': 1.0,
                'creator_disagreement': 1.0,
                'ai_analysis': 1.0,
            },
            outcomes=[],  # no resolved trades → all live samples = 0
        )
        self.assertEqual(result['weights']['probability_bias'], 1.0)

    def test_preserved_weight_below_one_is_kept(self):
        """Sub-1.0 weights survive preservation — defensive direction OK."""
        result = self._run_main(
            existing_file_weights={
                'probability_bias': 0.7,  # backtest said this strategy is bad
                'probability_direction': 1.0,
                'mean_reversion': 1.0,
                'creator_disagreement': 1.0,
                'ai_analysis': 1.0,
            },
            outcomes=[],
        )
        self.assertEqual(result['weights']['probability_bias'], 0.7)

    def test_preserved_weight_exactly_one_is_kept(self):
        result = self._run_main(
            existing_file_weights={
                'probability_bias': 1.0,
                'probability_direction': 1.0,
                'mean_reversion': 1.0,
                'creator_disagreement': 1.0,
                'ai_analysis': 1.0,
            },
            outcomes=[],
        )
        self.assertEqual(result['weights']['probability_bias'], 1.0)


class TestClampConstant(unittest.TestCase):
    """The clamp constant is exposed and reasonable."""

    def test_max_backtest_only_weight_is_one(self):
        """1.0 is neutral — backtest can't push above neutral without live
        confirmation. If this default changes, rethink the asymmetry rationale."""
        self.assertEqual(MAX_BACKTEST_ONLY_WEIGHT, 1.0)


if __name__ == '__main__':
    unittest.main()
