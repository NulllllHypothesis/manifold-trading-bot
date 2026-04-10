#!/usr/bin/env python3
"""
M6 — LoRA fine-tune meta-llama/Llama-3.2-3B on the M4 training dataset.

What this does
--------------
1. Loads data/training_dataset.jsonl (produced by build_training_dataset.py).
2. Fine-tunes the base model (HuggingFace format, NOT GGUF) with LoRA on the
   train split only.
3. After training, runs inference on every example (train + val + test) and
   writes data/finetuned_preds.jsonl keyed by example_id.
4. Prints a quick summary so you can immediately evaluate with run_eval_harness.py.

This is intentionally an OFFLINE EXPERIMENT FIRST.  No Ollama integration,
no production deployment.  Evaluate first; deploy only if the model clears the
M5 promotion gate (Brier < 0.1503 AND DirAcc >= 75.0% on the 8-example test
split).

Requirements
------------
  pip install -r requirements-train.txt

  The base model (~6 GB download) is fetched from HuggingFace on first run.
  Set HF_HOME or TRANSFORMERS_CACHE to a path with enough disk space.

  No GPU required.  Training runs in fp32 on CPU.  Expected time on the
  i5-1340P server: ~2-4 hours for the default 3 epochs over 95 train examples.

Usage
-----
  # Full run (train + predict)
  python3 scripts/finetune.py

  # Dry run — check dataset loading and config only, no training
  python3 scripts/finetune.py --dry-run

  # Predict only — skip training, load adapter from a previous run
  python3 scripts/finetune.py --predict-only --adapter-path data/lora_adapter

  # Custom epochs / batch size
  python3 scripts/finetune.py --epochs 5 --batch-size 4

Output
------
  data/lora_adapter/          — saved LoRA adapter weights (mergeable later)
  data/finetuned_preds.jsonl  — {"example_id": "...", "prediction": 0.37}
                                 (one line per example, ALL splits)

Evaluate immediately after:
  python3 scripts/run_eval_harness.py \\
      --finetuned-predictions data/finetuned_preds.jsonl \\
      --split test
"""

import argparse
import json
import math
import os
import re
import sys
import time
from pathlib import Path

ROOT_DIR     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATASET_PATH = os.path.join(ROOT_DIR, "data", "training_dataset.jsonl")
ADAPTER_PATH = os.path.join(ROOT_DIR, "data", "lora_adapter")
PREDS_PATH   = os.path.join(ROOT_DIR, "data", "finetuned_preds.jsonl")

# Base model — pulled from HuggingFace on first run (~6 GB)
BASE_MODEL = "meta-llama/Llama-3.2-3B"

# LoRA config — conservative settings for CPU training on 127 examples
LORA_R         = 8     # rank; 8 is standard for 3B
LORA_ALPHA     = 16    # scale = alpha / r = 2.0
LORA_DROPOUT   = 0.05
TARGET_MODULES = ["q_proj", "v_proj"]   # minimal; add k_proj/o_proj if overfitting

# Training config
DEFAULT_EPOCHS     = 3
DEFAULT_BATCH_SIZE = 2      # small — 127 examples, CPU, fp32
LEARNING_RATE      = 3e-4
MAX_SEQ_LEN        = 256    # prompt + completion well under this

_EPSILON = 1e-4  # clip parsed predictions away from 0/1


# ── Dataset loading ───────────────────────────────────────────────────────────

def load_dataset(path: str) -> dict[str, list[dict]]:
    """
    Load training_dataset.jsonl and split into train/val/test lists.
    Returns {"train": [...], "val": [...], "test": [...]}.
    """
    if not os.path.exists(path):
        print(f"ERROR: {path} not found. Run build_training_dataset.py first.")
        sys.exit(1)

    splits: dict[str, list[dict]] = {"train": [], "val": [], "test": []}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            ex = json.loads(line)
            s  = ex.get("split", "train")
            if s in splits:
                splits[s].append(ex)

    return splits


def _format_example(ex: dict) -> dict[str, str]:
    """Strip to the fields the tokeniser needs."""
    return {"prompt": ex["prompt"], "completion": ex["completion"]}


def _tokenize_with_completion_labels(
    examples: dict,
    tokenizer,
    max_seq_len: int,
) -> dict:
    """
    Tokenize each (prompt, completion) pair so that:
      - prompt tokens → labels = -100  (excluded from loss)
      - completion tokens → labels = token_id  (trained on)
      - padding tokens → labels = -100

    This is the correct objective for probability regression from a CLM:
    gradient flows only through the answer tokens, not the boilerplate prompt.

    Strategy:
      1. Tokenize "prompt + \\n" with BOS (gives prompt_len including BOS).
      2. Tokenize completion alone without BOS but with EOS appended.
      3. Concatenate; mask prompt slice to -100; pad to max_seq_len.
    """
    all_input_ids      = []
    all_attention_mask = []
    all_labels         = []

    for prompt, completion in zip(examples["prompt"], examples["completion"]):
        # Step 1: prompt (with BOS + separator newline)
        prompt_enc = tokenizer(
            prompt + "\n",
            add_special_tokens=True,
            truncation=True,
            max_length=max_seq_len,
        )
        prompt_ids = prompt_enc["input_ids"]
        prompt_len = len(prompt_ids)

        # Step 2: completion tokens (no BOS; append EOS so the model learns to stop)
        completion_ids = tokenizer(
            completion,
            add_special_tokens=False,
        )["input_ids"] + [tokenizer.eos_token_id]

        # Step 3: concatenate and truncate
        input_ids = (prompt_ids + completion_ids)[:max_seq_len]
        seq_len   = len(input_ids)

        # Labels: mask prompt, keep completion; align to seq_len
        label_raw  = [-100] * prompt_len + completion_ids
        labels     = label_raw[:seq_len]
        # Pad label list if completion was truncated (shouldn't happen at 256 tokens)
        if len(labels) < seq_len:
            labels += [-100] * (seq_len - len(labels))

        # Pad everything to max_seq_len
        pad_len        = max_seq_len - seq_len
        input_ids      = input_ids  + [tokenizer.pad_token_id] * pad_len
        attention_mask = [1] * seq_len + [0] * pad_len
        labels         = labels     + [-100] * pad_len

        all_input_ids.append(input_ids)
        all_attention_mask.append(attention_mask)
        all_labels.append(labels)

    return {
        "input_ids":       all_input_ids,
        "attention_mask":  all_attention_mask,
        "labels":          all_labels,
    }


# ── Inference helper ──────────────────────────────────────────────────────────

def _parse_prediction(generated: str, fallback: float = 0.5) -> float:
    """
    Extract the first float in [0, 1] from the model's generated text.
    The model is trained to output bare floats like "0.73".
    """
    # Look for a number that looks like a probability: 0.XX or .XX or 1.00 / 0 / 1
    matches = re.findall(r"\b(1\.0+|0\.\d+|\.\d+|[01])\b", generated)
    for m in matches:
        try:
            v = float(m)
            if 0.0 <= v <= 1.0:
                return max(_EPSILON, min(1.0 - _EPSILON, v))
        except ValueError:
            continue
    return fallback


def predict_all(model, tokenizer, examples: list[dict],
                max_new_tokens: int = 8) -> list[float]:
    """
    Run greedy decoding on each example's prompt and return a list of
    predicted probabilities (one per example, same order).
    """
    import torch

    model.eval()
    predictions = []
    n = len(examples)
    for i, ex in enumerate(examples):
        if (i + 1) % 20 == 0 or i == 0:
            print(f"  Predicting {i+1}/{n}...", flush=True)

        inputs = tokenizer(
            ex["prompt"],
            return_tensors="pt",
            truncation=True,
            max_length=MAX_SEQ_LEN,
        )
        with torch.no_grad():
            output_ids = model.generate(
                **inputs,
                max_new_tokens=max_new_tokens,
                do_sample=False,
                pad_token_id=tokenizer.eos_token_id,
            )
        # Decode only the newly generated tokens
        new_tokens = output_ids[0, inputs["input_ids"].shape[1]:]
        generated  = tokenizer.decode(new_tokens, skip_special_tokens=True).strip()
        predictions.append(_parse_prediction(generated))

    return predictions


# ── Training ──────────────────────────────────────────────────────────────────

def train(
    train_examples: list[dict],
    epochs: int,
    batch_size: int,
    adapter_path: str,
) -> None:
    """Fine-tune the base model with LoRA on train_examples."""
    print("\nLoading HuggingFace libraries...", flush=True)
    from transformers import AutoTokenizer, AutoModelForCausalLM, TrainingArguments
    from peft import LoraConfig, get_peft_model, TaskType
    from datasets import Dataset

    print(f"Loading base model: {BASE_MODEL}", flush=True)
    print("(First run downloads ~6 GB — may take a few minutes)", flush=True)
    t0 = time.time()

    tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL, use_fast=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    model = AutoModelForCausalLM.from_pretrained(
        BASE_MODEL,
        torch_dtype="auto",   # fp32 on CPU
        device_map="cpu",
    )
    print(f"Model loaded in {time.time()-t0:.0f}s  "
          f"({model.num_parameters()/1e6:.0f}M parameters)", flush=True)

    # Apply LoRA
    lora_config = LoraConfig(
        task_type    = TaskType.CAUSAL_LM,
        r            = LORA_R,
        lora_alpha   = LORA_ALPHA,
        lora_dropout = LORA_DROPOUT,
        target_modules = TARGET_MODULES,
        bias         = "none",
    )
    model = get_peft_model(model, lora_config)
    model.print_trainable_parameters()

    # Build HuggingFace Dataset
    formatted  = [_format_example(ex) for ex in train_examples]
    hf_dataset = Dataset.from_list(formatted)

    from functools import partial
    tokenize_fn = partial(
        _tokenize_with_completion_labels,
        tokenizer=tokenizer,
        max_seq_len=MAX_SEQ_LEN,
    )
    # Completion-only loss: prompt tokens masked to -100, only answer tokens trained.
    tokenised = hf_dataset.map(
        tokenize_fn,
        batched=True,
        remove_columns=hf_dataset.column_names,
    )

    # TrainingArguments — CPU-friendly
    training_args = TrainingArguments(
        output_dir              = adapter_path,
        num_train_epochs        = epochs,
        per_device_train_batch_size = batch_size,
        learning_rate           = LEARNING_RATE,
        fp16                    = False,
        bf16                    = False,
        logging_steps           = 5,
        save_strategy           = "epoch",
        save_total_limit        = 1,
        report_to               = "none",   # no wandb / tensorboard
        dataloader_num_workers  = 0,        # avoid multiprocessing issues on CPU
        no_cuda                 = True,
    )

    from transformers import Trainer, default_data_collator
    # Labels are already set with prompt-masking; use default_data_collator
    # so it passes them through unchanged (DataCollatorForLanguageModeling
    # would overwrite labels = input_ids and undo the masking).

    trainer = Trainer(
        model          = model,
        args           = training_args,
        train_dataset  = tokenised,
        data_collator  = default_data_collator,
    )

    print(f"\nStarting training: {len(train_examples)} examples, "
          f"{epochs} epochs, batch_size={batch_size}", flush=True)
    t1 = time.time()
    trainer.train()
    elapsed = time.time() - t1
    print(f"Training complete in {elapsed/60:.1f} min", flush=True)

    # Save LoRA adapter only (not full model weights — saves ~6 GB)
    model.save_pretrained(adapter_path)
    tokenizer.save_pretrained(adapter_path)
    print(f"Adapter saved → {adapter_path}", flush=True)


# ── Load adapter for prediction ───────────────────────────────────────────────

def load_model_for_inference(adapter_path: str):
    """Load base model + LoRA adapter for inference."""
    from transformers import AutoTokenizer, AutoModelForCausalLM
    from peft import PeftModel

    print(f"Loading adapter from {adapter_path}...", flush=True)
    tokenizer = AutoTokenizer.from_pretrained(adapter_path, use_fast=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    base = AutoModelForCausalLM.from_pretrained(
        BASE_MODEL,
        torch_dtype = "auto",
        device_map  = "cpu",
    )
    model = PeftModel.from_pretrained(base, adapter_path)
    return model, tokenizer


# ── Main ──────────────────────────────────────────────────────────────────────

def main(
    dataset_path:  str  = DATASET_PATH,
    adapter_path:  str  = ADAPTER_PATH,
    preds_path:    str  = PREDS_PATH,
    epochs:        int  = DEFAULT_EPOCHS,
    batch_size:    int  = DEFAULT_BATCH_SIZE,
    dry_run:       bool = False,
    predict_only:  bool = False,
) -> None:
    print("=" * 72)
    print("M6 — LORA FINE-TUNE  (train → finetuned_preds.jsonl → M5 eval)")
    print("=" * 72)

    splits = load_dataset(dataset_path)
    train_n = len(splits["train"])
    val_n   = len(splits["val"])
    test_n  = len(splits["test"])
    total   = train_n + val_n + test_n

    print(f"\nDataset : {dataset_path}")
    print(f"  train={train_n}  val={val_n}  test={test_n}  total={total}")
    print(f"Base model   : {BASE_MODEL}")
    print(f"LoRA rank    : {LORA_R}  alpha={LORA_ALPHA}  targets={TARGET_MODULES}")
    print(f"Adapter path : {adapter_path}")
    print(f"Preds path   : {preds_path}")

    if dry_run:
        print("\n[dry-run] Config looks good. Exiting without training.")
        return

    if not predict_only:
        if train_n == 0:
            print("ERROR: no train examples found.")
            sys.exit(1)
        train(splits["train"], epochs=epochs, batch_size=batch_size,
              adapter_path=adapter_path)
    else:
        print("\n[predict-only] Skipping training — loading existing adapter.")

    if not os.path.exists(adapter_path):
        print(f"ERROR: adapter not found at {adapter_path}. Run training first.")
        sys.exit(1)

    # ── Inference on all splits ────────────────────────────────────────────
    model, tokenizer = load_model_for_inference(adapter_path)

    all_examples = splits["train"] + splits["val"] + splits["test"]
    print(f"\nRunning inference on {len(all_examples)} examples...", flush=True)
    predictions = predict_all(model, tokenizer, all_examples)

    # ── Write predictions file ─────────────────────────────────────────────
    os.makedirs(os.path.dirname(preds_path) if os.path.dirname(preds_path) else ".", exist_ok=True)
    with open(preds_path, "w") as f:
        for ex, pred in zip(all_examples, predictions):
            f.write(json.dumps({"example_id": ex["example_id"], "prediction": round(pred, 4)}) + "\n")

    print(f"\nPredictions → {preds_path}  ({len(all_examples)} rows)")

    # Quick sanity: mean prediction
    mean_pred = sum(predictions) / len(predictions)
    print(f"Mean prediction: {mean_pred:.3f}  (crowd mean is ~0.60)")

    print("\nEvaluate with:")
    print(f"  python3 scripts/run_eval_harness.py \\")
    print(f"      --finetuned-predictions {preds_path} \\")
    print(f"      --split test")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="M6 — LoRA fine-tune + predict")
    parser.add_argument("--dataset",      default=DATASET_PATH,
                        help="Path to training_dataset.jsonl")
    parser.add_argument("--adapter-path", default=ADAPTER_PATH,
                        help="Where to save/load the LoRA adapter")
    parser.add_argument("--preds-path",   default=PREDS_PATH,
                        help="Where to write finetuned_preds.jsonl")
    parser.add_argument("--epochs",       type=int, default=DEFAULT_EPOCHS)
    parser.add_argument("--batch-size",   type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--dry-run",      action="store_true",
                        help="Validate config and dataset loading without training")
    parser.add_argument("--predict-only", action="store_true",
                        help="Skip training; load existing adapter and run inference")
    args = parser.parse_args()

    main(
        dataset_path  = args.dataset,
        adapter_path  = args.adapter_path,
        preds_path    = args.preds_path,
        epochs        = args.epochs,
        batch_size    = args.batch_size,
        dry_run       = args.dry_run,
        predict_only  = args.predict_only,
    )
