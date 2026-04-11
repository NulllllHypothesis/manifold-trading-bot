#!/usr/bin/env python3
"""
Unit tests for scripts/finetune.py — M6 LoRA fine-tune scaffolding.

Coverage
--------
Only pure-Python functions are tested here; anything that imports torch,
transformers, or peft is excluded so the suite runs on the local Mac
environment without the ML stack installed.

Functions covered:
  _parse_prediction  — float extraction from generated text
  load_dataset       — JSONL loading + train/val/test split
  _format_example    — field extraction for tokenisation
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.finetune import _parse_prediction, load_dataset, _format_example


# ── _parse_prediction ─────────────────────────────────────────────────────────

class TestParsePrediction(unittest.TestCase):

    def test_bare_float(self):
        self.assertAlmostEqual(_parse_prediction("0.73"), 0.73, places=4)

    def test_float_with_surrounding_text(self):
        # Model sometimes outputs "The probability is 0.65."
        self.assertAlmostEqual(_parse_prediction("The probability is 0.65."), 0.65, places=4)

    def test_one_point_zero(self):
        # Should clip to 1 - epsilon, not return exactly 1.0
        result = _parse_prediction("1.00")
        self.assertLess(result, 1.0)
        self.assertGreater(result, 0.99)

    def test_zero(self):
        result = _parse_prediction("0")
        self.assertGreater(result, 0.0)
        self.assertLess(result, 0.01)

    def test_fallback_on_no_match(self):
        self.assertAlmostEqual(_parse_prediction("no numbers here"), 0.5, places=4)

    def test_fallback_on_empty(self):
        self.assertAlmostEqual(_parse_prediction(""), 0.5, places=4)

    def test_ignores_out_of_range(self):
        # "42" is out of [0, 1] — should fall back
        self.assertAlmostEqual(_parse_prediction("42"), 0.5, places=4)

    def test_picks_first_valid_float(self):
        # "0.72 or maybe 0.80" — should return the first one
        result = _parse_prediction("0.72 or maybe 0.80")
        self.assertAlmostEqual(result, 0.72, places=4)

    def test_clipped_away_from_zero(self):
        result = _parse_prediction("0.00")
        self.assertGreater(result, 0.0)

    def test_clipped_away_from_one(self):
        result = _parse_prediction("1.0")
        self.assertLess(result, 1.0)


# ── load_dataset ──────────────────────────────────────────────────────────────

def _make_jsonl(rows: list[dict]) -> str:
    """Write rows to a temp file and return the path."""
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    return path


class TestLoadDataset(unittest.TestCase):

    def _example(self, split: str, market_id: str = "m1") -> dict:
        return {
            "example_id":       f"{market_id}__reconstructed__7",
            "market_id":        market_id,
            "split":            split,
            "prompt":           "Predict the probability...",
            "completion":       "0.70",
            "crowd_probability": 0.65,
            "true_probability": 1.0,
            "outcome":          "YES",
            "category":         "other",
            "days_before_close": 7,
        }

    def test_basic_split(self):
        rows = [
            self._example("train", "a"),
            self._example("val",   "b"),
            self._example("test",  "c"),
        ]
        path = _make_jsonl(rows)
        try:
            splits = load_dataset(path)
            self.assertEqual(len(splits["train"]), 1)
            self.assertEqual(len(splits["val"]),   1)
            self.assertEqual(len(splits["test"]),  1)
        finally:
            os.unlink(path)

    def test_multiple_train_rows(self):
        rows = [self._example("train", f"m{i}") for i in range(5)]
        path = _make_jsonl(rows)
        try:
            splits = load_dataset(path)
            self.assertEqual(len(splits["train"]), 5)
            self.assertEqual(len(splits["val"]),   0)
            self.assertEqual(len(splits["test"]),  0)
        finally:
            os.unlink(path)

    def test_returns_all_three_keys_even_when_empty(self):
        rows = [self._example("train", "x")]
        path = _make_jsonl(rows)
        try:
            splits = load_dataset(path)
            self.assertIn("train", splits)
            self.assertIn("val",   splits)
            self.assertIn("test",  splits)
        finally:
            os.unlink(path)

    def test_skips_unknown_split_label(self):
        rows = [
            self._example("train", "a"),
            self._example("unknown_split", "b"),  # should be ignored
        ]
        path = _make_jsonl(rows)
        try:
            splits = load_dataset(path)
            total = sum(len(v) for v in splits.values())
            self.assertEqual(total, 1)
        finally:
            os.unlink(path)

    def test_skips_blank_lines(self):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "w") as f:
            f.write("\n")
            f.write(json.dumps(self._example("train", "a")) + "\n")
            f.write("   \n")
        try:
            splits = load_dataset(path)
            self.assertEqual(len(splits["train"]), 1)
        finally:
            os.unlink(path)

    def test_missing_file_exits(self):
        with self.assertRaises(SystemExit):
            load_dataset("/tmp/this_file_definitely_does_not_exist_m6.jsonl")

    def test_preserves_example_id(self):
        ex = self._example("test", "mymarket")
        path = _make_jsonl([ex])
        try:
            splits = load_dataset(path)
            row = splits["test"][0]
            self.assertEqual(row["example_id"], "mymarket__reconstructed__7")
        finally:
            os.unlink(path)


# ── _format_example ───────────────────────────────────────────────────────────

class TestFormatExample(unittest.TestCase):

    def _make_example(self) -> dict:
        return {
            "example_id":       "m1__reconstructed__7",
            "market_id":        "m1",
            "split":            "train",
            "prompt":           "Predict the probability...\nQuestion: Will X happen?",
            "completion":       "0.73",
            "crowd_probability": 0.70,
            "true_probability": 1.0,
        }

    def test_returns_prompt_and_completion(self):
        ex = self._make_example()
        out = _format_example(ex)
        self.assertIn("prompt", out)
        self.assertIn("completion", out)

    def test_prompt_value_matches(self):
        ex = self._make_example()
        out = _format_example(ex)
        self.assertEqual(out["prompt"], ex["prompt"])

    def test_completion_value_matches(self):
        ex = self._make_example()
        out = _format_example(ex)
        self.assertEqual(out["completion"], ex["completion"])

    def test_no_extra_fields(self):
        ex = self._make_example()
        out = _format_example(ex)
        self.assertEqual(set(out.keys()), {"prompt", "completion"})

    def test_does_not_concatenate(self):
        # The old (buggy) version concatenated prompt+completion into a "text" field.
        # Confirm the new version does NOT do this — concatenation happens inside
        # _tokenize_with_completion_labels so masking can be computed.
        ex = self._make_example()
        out = _format_example(ex)
        self.assertNotIn("text", out)


if __name__ == "__main__":
    unittest.main()
