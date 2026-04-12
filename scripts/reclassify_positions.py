#!/usr/bin/env python3
"""
One-off: reclassify category labels on existing open positions in
paper_trading_state.json using the current _infer_market_category().

Problem: positions placed before category v2 carry stale labels (or
no label at all). The daily summary reads these stored labels, so
the "BY CATEGORY" breakdown shows 9 × other / 2 × ai_tech even
though the v2 classifier would route some of those differently.

What it does:
  - For every OPEN position with a stored question, reruns
    _infer_market_category() and overwrites the category field.
  - Positions with no question text are left as-is (can't classify).
  - Prints a before → after diff.

Safety:
  - Only touches positions in paper_trading_state.json, not any DB.
  - Writes to disk only when --write is passed (default is dry-run).

Usage:
  python3 scripts/reclassify_positions.py              # dry-run
  python3 scripts/reclassify_positions.py --write       # apply
"""

import argparse
import json
import os
import sys

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

from manifold_bot.strategies import _infer_market_category

STATE_PATH = os.path.join(ROOT_DIR, "manifold_bot", "paper_trading_state.json")


def main():
    parser = argparse.ArgumentParser(description="Reclassify open position categories")
    parser.add_argument("--write", action="store_true",
                        help="Actually write changes (default is dry-run)")
    args = parser.parse_args()

    if not os.path.exists(STATE_PATH):
        print(f"ERROR: {STATE_PATH} not found.")
        return

    with open(STATE_PATH) as f:
        state = json.load(f)

    positions = state.get("positions", {})
    changed = 0
    no_question = 0

    for mid, pos_list in positions.items():
        for p in pos_list:
            if p.get("status") != "OPEN":
                continue

            question = p.get("question") or ""
            if not question:
                no_question += 1
                continue

            old_cat = p.get("category") or "other"
            new_cat = _infer_market_category(question)

            if old_cat != new_cat:
                print(f"  {mid[:14]:<16} {old_cat:<14} → {new_cat:<14}  {question[:50]}")
                p["category"] = new_cat
                changed += 1

    print(f"\nChanged: {changed}")
    if no_question:
        print(f"Skipped (no question text): {no_question}")

    if args.write and changed > 0:
        with open(STATE_PATH, "w") as f:
            json.dump(state, f, indent=2)
        print(f"Written → {STATE_PATH}")
    elif changed > 0:
        print("(dry-run — pass --write to apply)")


if __name__ == "__main__":
    main()
