#!/usr/bin/env python3
"""Hourly Polymarket market-data ingestion.

Pulls active markets from the Polymarket Gamma API, normalizes each into
the unified `markets_normalized` schema, and upserts into
`data/markets_normalized.db`.

Cron slot: `:15` UTC, between auto_trader (:20) and the resolve step (:10),
on a slot otherwise unused.

Behavior:
  - Best-effort: one bad market is logged and skipped, doesn't abort the run
  - Idempotent: re-running on the same market replaces the prior row
    (INSERT OR REPLACE on PK (platform, market_id))
  - Read-only against Polymarket. We never trade on it from this script.

This script is the WRITE side of the Tier-2 multi-platform pivot. No
read path consumes `markets_normalized` yet — that comes in a later PR
along with cross-platform matching.

Usage:
    python3 scripts/ingest_polymarket.py
    python3 scripts/ingest_polymarket.py --max-pages 1   # smoke test (≤100 markets)
    python3 scripts/ingest_polymarket.py --dry-run        # don't write
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from manifold_bot import polymarket_api
from manifold_bot.markets_normalized import init_db, normalize_polymarket_market, upsert_markets


_DB_PATH = _ROOT / "data" / "markets_normalized.db"


def main(*, max_pages: int | None, dry_run: bool, page_size: int = 100) -> int:
    fetched_at = datetime.now(timezone.utc).isoformat()

    if not dry_run:
        init_db(_DB_PATH)

    fetched = 0
    skipped_malformed = 0
    rows_to_write: list[dict] = []

    print(f"[{fetched_at}] starting Polymarket ingest (dry_run={dry_run}, max_pages={max_pages})")
    skipped_normalize_error = 0
    try:
        # The OUTER try only catches API/network/iteration failures —
        # those genuinely should bail out of the loop. Per-market
        # normalize errors are caught INSIDE the loop so one malformed
        # market can't truncate the rest of the run (reviewer-caught bug:
        # a single non-dict event entry raised AttributeError and killed
        # ~all subsequent markets in a run).
        for raw in polymarket_api.iter_all_active_markets(page_size=page_size, max_pages=max_pages):
            fetched += 1
            try:
                row = normalize_polymarket_market(raw, fetched_at=fetched_at)
            except Exception as e:
                skipped_normalize_error += 1
                mid = (raw.get("id") if isinstance(raw, dict) else "?") if raw else "?"
                print(f"  warning: normalize failed for market {mid} ({type(e).__name__}: {e}); skipping")
                continue
            if row is None:
                skipped_malformed += 1
                continue
            rows_to_write.append(row)
    except Exception as e:
        # API/iteration failure: log and proceed to write whatever we
        # successfully collected. Better partial data than no data.
        print(f"  warning: polymarket fetch interrupted ({type(e).__name__}: {e})")

    print(f"  fetched: {fetched}, normalized: {len(rows_to_write)}, "
          f"skipped_malformed: {skipped_malformed}, skipped_normalize_error: {skipped_normalize_error}")

    if dry_run:
        print(f"  [dry-run] would write {len(rows_to_write)} rows to {_DB_PATH}")
        if rows_to_write:
            sample = rows_to_write[0]
            print(f"  sample row: platform={sample['platform']} market_id={sample['market_id']} "
                  f"question={sample['question'][:60]!r} prob={sample['probability']} "
                  f"liquidity={sample['liquidity']}")
        return 0

    n_written = upsert_markets(_DB_PATH, rows_to_write)
    print(f"  wrote {n_written} rows to {_DB_PATH}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-pages", type=int, default=None,
                        help="cap pagination (None = no cap; 1 = smoke test)")
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch + normalize but don't write to DB")
    parser.add_argument("--page-size", type=int, default=100,
                        help="markets per request (default 100, max 500)")
    args = parser.parse_args()
    sys.exit(main(max_pages=args.max_pages, dry_run=args.dry_run, page_size=args.page_size))
