#!/usr/bin/env python3
"""
M5 — Eval harness: measure every model candidate against 4 baselines.

Non-negotiable before any fine-tuned model goes to production.
Without this you cannot know if fine-tuning helped or just made the
model sound more confident on the same wrong answers.

Metrics (computed on the held-out TEST split from training_dataset.jsonl)
--------------------------------------------------------------------------
  Brier score     — mean((prediction - true_probability)^2); lower is better
  Log loss        — mean(-true_prob*log(p) - (1-true_prob)*log(1-p)); lower is better
  Directional acc — how often sign(prediction - 0.5) == sign(true_probability - 0.5)
  Calibration     — predicted vs actual YES rate per probability bucket

Baselines (always computed, no model required)
-----------------------------------------------
  1. crowd      — crowd_probability from the snapshot (the floor to beat)
  2. always_0.5 — always predicts 50% (naive baseline)
  3. always_0.8 — always predicts 80% (overconfident YES baseline)
  4. random     — uniform random in [0,1]  (seeded, reproducible)

Model slots (computed only when a predictions file is provided)
---------------------------------------------------------------
  5. stat_system     — pass --stat-predictions <jsonl>
  6. deepseek_prompt — pass --deepseek-predictions <jsonl>
  7. finetuned       — pass --finetuned-predictions <jsonl>

Promotion rule
--------------
A fine-tuned model is promoted only if:
  - Brier score < crowd Brier score  (beats crowd)
  - Brier score < deepseek Brier score  (beats current production)
  - Directional accuracy >= crowd directional accuracy

Predictions file format
-----------------------
One JSON per line:  {"example_id": "...", "prediction": 0.37}
example_id is the stable per-snapshot key emitted by build_training_dataset.py:
  format: "{market_id}__{source}__{days_before_close}"
  e.g.  "abc123__reconstructed__7"
Missing example_ids get a default prediction of 0.5.

Do NOT key by market_id — the same market can appear at T-7, T-14, and T-30,
and all three snapshots must be scored independently.

Usage
-----
  # Baselines only
  python3 scripts/run_eval_harness.py

  # With a model predictions file
  python3 scripts/run_eval_harness.py --finetuned-predictions data/finetuned_preds.jsonl

  # Use a specific dataset file
  python3 scripts/run_eval_harness.py --dataset data/training_dataset.jsonl

  # Evaluate on full dataset instead of test split only
  python3 scripts/run_eval_harness.py --split all
"""

import argparse
import json
import math
import os
import random
import sys
from collections import defaultdict

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

DATASET_PATH  = os.path.join(ROOT_DIR, "data", "training_dataset.jsonl")
REPORT_PATH   = os.path.join(ROOT_DIR, "data", "eval_report.json")

_RANDOM_SEED  = 42
_EPSILON      = 1e-7   # clip predictions to avoid log(0)


# ── Data loading ──────────────────────────────────────────────────────────────

def load_dataset(path: str, split: str = "test") -> list[dict]:
    """
    Load examples from training_dataset.jsonl, filtered by split.
    split="all" returns all rows regardless of split label.
    """
    if not os.path.exists(path):
        print(f"ERROR: {path} not found. Run build_training_dataset.py first.")
        return []
    examples = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            ex = json.loads(line)
            if split == "all" or ex.get("split") == split:
                examples.append(ex)
    return examples


def load_predictions(path: str) -> dict[str, float]:
    """Load a predictions file → {example_id: prediction}."""
    if not path or not os.path.exists(path):
        return {}
    preds = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            preds[obj["example_id"]] = float(obj["prediction"])
    return preds


# ── Metrics ───────────────────────────────────────────────────────────────────

def _clip(p: float) -> float:
    return max(_EPSILON, min(1.0 - _EPSILON, p))


def brier_score(predictions: list[float], true_probs: list[float]) -> float:
    """Mean squared error between predictions and true probabilities."""
    n = len(predictions)
    if n == 0:
        return float("nan")
    return sum((p - t) ** 2 for p, t in zip(predictions, true_probs)) / n


def log_loss(predictions: list[float], true_probs: list[float]) -> float:
    """Binary cross-entropy."""
    n = len(predictions)
    if n == 0:
        return float("nan")
    total = 0.0
    for p, t in zip(predictions, true_probs):
        p = _clip(p)
        total += -(t * math.log(p) + (1 - t) * math.log(1 - p))
    return total / n


def directional_accuracy(predictions: list[float], true_probs: list[float]) -> float:
    """Fraction where prediction and true value agree on which side of 0.5 they are."""
    n = len(predictions)
    if n == 0:
        return float("nan")
    correct = sum(
        1 for p, t in zip(predictions, true_probs)
        if (p >= 0.5) == (t >= 0.5)
    )
    return correct / n


def calibration_curve(predictions: list[float], true_probs: list[float],
                      n_buckets: int = 5) -> list[dict]:
    """
    Bucket predictions into n_buckets equal-width bins and compute:
      - mean predicted probability
      - actual YES rate
      - count
    """
    buckets: dict[int, dict] = {i: {"pred_sum": 0.0, "true_sum": 0.0, "count": 0}
                                 for i in range(n_buckets)}
    for p, t in zip(predictions, true_probs):
        b = min(int(p * n_buckets), n_buckets - 1)
        buckets[b]["pred_sum"] += p
        buckets[b]["true_sum"] += t
        buckets[b]["count"]    += 1

    result = []
    for i, stats in sorted(buckets.items()):
        n = stats["count"]
        lo = i / n_buckets
        hi = (i + 1) / n_buckets
        result.append({
            "bucket":      f"{lo*100:.0f}-{hi*100:.0f}%",
            "n":           n,
            "mean_pred":   round(stats["pred_sum"] / n, 3) if n > 0 else None,
            "actual_rate": round(stats["true_sum"] / n, 3) if n > 0 else None,
        })
    return result


# ── Model runners ─────────────────────────────────────────────────────────────

def _predictions_for(examples: list[dict], pred_map: dict[str, float],
                     default: float = 0.5) -> list[float]:
    return [pred_map.get(ex["example_id"], default) for ex in examples]


def _random_predictions(examples: list[dict], seed: int = _RANDOM_SEED) -> list[float]:
    rng = random.Random(seed)
    return [rng.random() for _ in examples]


# ── Evaluation ────────────────────────────────────────────────────────────────

def evaluate_model(name: str, predictions: list[float],
                   true_probs: list[float], examples: list[dict]) -> dict:
    """Compute all metrics for one model."""
    bs   = brier_score(predictions, true_probs)
    ll   = log_loss(predictions, true_probs)
    da   = directional_accuracy(predictions, true_probs)
    cal  = calibration_curve(predictions, true_probs)

    # Per-category Brier score
    by_cat: dict[str, list] = defaultdict(list)
    for p, t, ex in zip(predictions, true_probs, examples):
        by_cat[ex.get("category", "other")].append((p, t))
    cat_brier = {
        cat: round(brier_score([x[0] for x in pairs], [x[1] for x in pairs]), 4)
        for cat, pairs in sorted(by_cat.items())
    }

    return {
        "name":                name,
        "n":                   len(predictions),
        "brier_score":         round(bs, 4),
        "log_loss":            round(ll, 4),
        "directional_accuracy": round(da, 4),
        "calibration":         cal,
        "brier_by_category":   cat_brier,
    }


def check_promotion(results: dict[str, dict]) -> dict:
    """
    Apply the promotion rule: fine-tuned beats crowd AND deepseek on Brier score,
    AND directional accuracy >= crowd.
    """
    crowd    = results.get("crowd")
    deepseek = results.get("deepseek_prompt")
    ft       = results.get("finetuned")

    if ft is None:
        return {"eligible": False, "reason": "no finetuned predictions provided"}
    if crowd is None:
        return {"eligible": False, "reason": "crowd baseline missing"}

    beats_crowd    = ft["brier_score"] < crowd["brier_score"]
    dir_ok         = ft["directional_accuracy"] >= crowd["directional_accuracy"]
    beats_deepseek = True  # default pass when deepseek not available
    if deepseek is not None:
        beats_deepseek = ft["brier_score"] < deepseek["brier_score"]

    promoted = beats_crowd and beats_deepseek and dir_ok
    reasons  = []
    if not beats_crowd:
        reasons.append(f"Brier {ft['brier_score']} >= crowd {crowd['brier_score']}")
    if not beats_deepseek and deepseek:
        reasons.append(f"Brier {ft['brier_score']} >= deepseek {deepseek['brier_score']}")
    if not dir_ok:
        reasons.append(f"dir_acc {ft['directional_accuracy']} < crowd {crowd['directional_accuracy']}")

    return {
        "eligible": promoted,
        "reason":   "PROMOTED" if promoted else "; ".join(reasons),
    }


# ── Display ───────────────────────────────────────────────────────────────────

def _print_results(all_results: list[dict]) -> None:
    if not all_results:
        return
    print(f"\n{'─'*72}")
    print(f"  {'Model':22} {'Brier↓':>8}  {'LogLoss↓':>9}  {'DirAcc↑':>8}  {'N':>5}")
    print(f"  {'─'*22} {'─'*8}  {'─'*9}  {'─'*8}  {'─'*5}")
    for r in all_results:
        print(f"  {r['name']:22} {r['brier_score']:>8.4f}  {r['log_loss']:>9.4f}"
              f"  {r['directional_accuracy']:>8.1%}  {r['n']:>5}")

    # Show calibration for crowd baseline as reference
    crowd = next((r for r in all_results if r["name"] == "crowd"), None)
    if crowd and crowd.get("calibration"):
        print(f"\n  Crowd calibration:")
        print(f"    {'Bucket':>10}  {'N':>5}  {'Pred':>6}  {'Actual':>6}")
        for b in crowd["calibration"]:
            if b["n"] > 0:
                pred   = f"{b['mean_pred']*100:.0f}%" if b["mean_pred"] is not None else "  —"
                actual = f"{b['actual_rate']*100:.0f}%" if b["actual_rate"] is not None else "  —"
                print(f"    {b['bucket']:>10}  {b['n']:>5}  {pred:>6}  {actual:>6}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main(
    dataset_path: str = DATASET_PATH,
    split: str = "test",
    stat_preds_path: str | None = None,
    deepseek_preds_path: str | None = None,
    finetuned_preds_path: str | None = None,
    dry_run: bool = False,
) -> None:
    print("=" * 72)
    print("M5 — EVAL HARNESS")
    print("=" * 72)

    examples = load_dataset(dataset_path, split=split)
    if not examples:
        print(f"\nNo examples found in {dataset_path} (split={split}).")
        print("Run: python3 scripts/build_training_dataset.py")
        return

    true_probs = [ex["true_probability"] for ex in examples]

    print(f"\nDataset : {dataset_path}")
    print(f"Split   : {split}  ({len(examples)} examples)")
    yes_rate = sum(t for t in true_probs) / len(true_probs)
    print(f"YES rate: {yes_rate*100:.1f}%")

    # ── Baselines ──────────────────────────────────────────────────────────
    crowd_preds  = [ex["crowd_probability"] for ex in examples]
    half_preds   = [0.5] * len(examples)
    eight_preds  = [0.8] * len(examples)
    random_preds = _random_predictions(examples)

    all_results = [
        evaluate_model("crowd",      crowd_preds,  true_probs, examples),
        evaluate_model("always_0.5", half_preds,   true_probs, examples),
        evaluate_model("always_0.8", eight_preds,  true_probs, examples),
        evaluate_model("random",     random_preds, true_probs, examples),
    ]

    # ── Optional model predictions ─────────────────────────────────────────
    for label, path in [
        ("stat_system",     stat_preds_path),
        ("deepseek_prompt", deepseek_preds_path),
        ("finetuned",       finetuned_preds_path),
    ]:
        if path:
            pmap = load_predictions(path)
            preds = _predictions_for(examples, pmap)
            all_results.append(evaluate_model(label, preds, true_probs, examples))

    results_by_name = {r["name"]: r for r in all_results}

    _print_results(all_results)

    # ── Promotion check ────────────────────────────────────────────────────
    promo = check_promotion(results_by_name)
    if finetuned_preds_path:
        print(f"\n  Promotion: {promo['reason']}")

    # ── Write report ───────────────────────────────────────────────────────
    report = {
        "evaluated_at": __import__("datetime").datetime.utcnow().isoformat() + "Z",
        "dataset":      dataset_path,
        "split":        split,
        "n_examples":   len(examples),
        "results":      {r["name"]: r for r in all_results},
        "promotion":    promo,
    }

    if not dry_run:
        with open(REPORT_PATH, "w") as f:
            json.dump(report, f, indent=2)
        print(f"\n  Report → {REPORT_PATH}")

    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="M5 — Eval harness")
    parser.add_argument("--dataset", default=DATASET_PATH,
                        help="Path to training_dataset.jsonl")
    parser.add_argument("--split", default="test",
                        choices=["train", "val", "test", "all"],
                        help="Which split to evaluate (default: test)")
    parser.add_argument("--stat-predictions",     default=None,
                        help="JSONL file with stat system predictions")
    parser.add_argument("--deepseek-predictions", default=None,
                        help="JSONL file with DeepSeek predictions")
    parser.add_argument("--finetuned-predictions", default=None,
                        help="JSONL file with fine-tuned model predictions")
    parser.add_argument("--dry-run", action="store_true",
                        help="Don't write eval_report.json")
    args = parser.parse_args()

    main(
        dataset_path         = args.dataset,
        split                = args.split,
        stat_preds_path      = args.stat_predictions,
        deepseek_preds_path  = args.deepseek_predictions,
        finetuned_preds_path = args.finetuned_predictions,
        dry_run              = args.dry_run,
    )
