#!/usr/bin/env python3
"""
Op4 — One-off backfill: populate days_before_close on existing live snapshots.

Context
-------
Before Op4, automation/auto_research.py wrote every live row with
days_before_close=NULL. build_training_dataset.py skips those rows so ~3100+
accumulated live snapshots on the server are currently invisible to the ML
track. Op4 fixed the forward path (new rows get days_before_close at snapshot
time) — this script reclaims the historical rows.

Algorithm
---------
For every live snapshot with days_before_close IS NULL:

  1. Resolve the market's closeTime (unix ms):
     a. Fast path: data/calibration.db :: resolved_markets.close_date
        (already-harvested resolved markets — one local query, no network)
     b. Slow path: Manifold API get_market(market_id).closeTime
        (only used when the market hasn't been harvested yet)

  2. Compute days_before_close = (close_ms - snapshot_at_ms) // 86_400_000.
     Clamped to max(0, ...) so already-closed observations read as 0, not
     negative. None if closeTime still can't be determined.

  3. UPDATE market_snapshots SET days_before_close = ? WHERE id = ?.

Usage
-----
  python3 scripts/backfill_days_before_close.py              # full backfill
  python3 scripts/backfill_days_before_close.py --dry-run    # counts only
  python3 scripts/backfill_days_before_close.py --no-api     # skip API fallback
  python3 scripts/backfill_days_before_close.py --limit 500  # test on a slice

Safety
------
- Only updates rows where days_before_close IS NULL. Existing values are
  never overwritten, so re-runs are idempotent.
- API fallback is rate-limited by a small delay between calls and aborts
  gracefully on any exception per market.
"""

import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime
from typing import Optional

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

SNAPSHOTS_DB   = os.path.join(ROOT_DIR, "data", "market_snapshots.db")
CALIBRATION_DB = os.path.join(ROOT_DIR, "data", "calibration.db")

_MS_PER_DAY = 24 * 60 * 60 * 1000


# ── close_ms resolution ───────────────────────────────────────────────────────

def _load_resolved_close_ms_map() -> dict[str, int]:
    """
    {market_id: close_ms} for every resolved market we've already harvested.
    Reads ISO close_date strings and converts to unix ms.
    """
    if not os.path.exists(CALIBRATION_DB):
        return {}
    conn = sqlite3.connect(CALIBRATION_DB)
    try:
        rows = conn.execute(
            "SELECT market_id, close_date FROM resolved_markets WHERE close_date IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()

    out: dict[str, int] = {}
    for market_id, close_date in rows:
        try:
            dt = datetime.fromisoformat(close_date)
            out[market_id] = int(dt.timestamp() * 1000)
        except (TypeError, ValueError):
            continue
    return out


def _fetch_close_ms_via_api(market_id: str) -> Optional[int]:
    """Call Manifold API for a single market's closeTime. Returns None on failure."""
    try:
        from manifold_bot.manifold_api import api_client
        m = api_client.get_market(market_id)
        ct = m.get("closeTime")
        return int(ct) if ct is not None else None
    except Exception:
        return None


# ── Core backfill ────────────────────────────────────────────────────────────

def _snapshot_at_to_ms(snapshot_at: str) -> Optional[int]:
    """Parse the stored ISO snapshot_at back into unix ms."""
    try:
        dt = datetime.fromisoformat(snapshot_at)
        return int(dt.timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def _days_before(close_ms: int, snapshot_ms: int) -> int:
    """Floor-divide the delta into integer days, clamped to 0 for already-closed."""
    return max(0, int((close_ms - snapshot_ms) // _MS_PER_DAY))


def backfill(
    snapshots_db: str = SNAPSHOTS_DB,
    dry_run:      bool = False,
    use_api:      bool = True,
    limit:        Optional[int] = None,
    api_delay_s:  float = 0.2,
) -> dict:
    """
    Do the backfill.  Returns a stats dict for reporting.
    """
    if not os.path.exists(snapshots_db):
        print(f"ERROR: {snapshots_db} not found.")
        return {"error": "missing_db"}

    # Build local close_ms lookup once
    print("Loading resolved_markets close-time map...")
    close_map = _load_resolved_close_ms_map()
    print(f"  {len(close_map)} markets with local close_date")

    conn = sqlite3.connect(snapshots_db)
    query = """
        SELECT id, market_id, snapshot_at
        FROM market_snapshots
        WHERE source = 'live' AND days_before_close IS NULL
        ORDER BY snapshot_at ASC
    """
    if limit:
        query += f" LIMIT {int(limit)}"
    rows = conn.execute(query).fetchall()
    print(f"  {len(rows)} live rows with NULL days_before_close")

    # Cache API results so we only fetch each market once
    api_cache: dict[str, Optional[int]] = {}
    stats = {
        "total":            len(rows),
        "updated":          0,
        "resolved_by_db":   0,
        "resolved_by_api":  0,
        "api_attempts":     0,
        "unresolvable":     0,
        "bad_snapshot_at":  0,
    }

    updates: list[tuple[int, int]] = []  # (days_before_close, row_id)

    for row_id, market_id, snapshot_at in rows:
        snapshot_ms = _snapshot_at_to_ms(snapshot_at)
        if snapshot_ms is None:
            stats["bad_snapshot_at"] += 1
            continue

        # (a) local lookup
        close_ms = close_map.get(market_id)
        if close_ms is not None:
            stats["resolved_by_db"] += 1
        elif use_api:
            # (b) API fallback — one request per unique market_id
            if market_id in api_cache:
                close_ms = api_cache[market_id]
            else:
                stats["api_attempts"] += 1
                close_ms = _fetch_close_ms_via_api(market_id)
                api_cache[market_id] = close_ms
                time.sleep(api_delay_s)
            if close_ms is not None:
                stats["resolved_by_api"] += 1

        if close_ms is None:
            stats["unresolvable"] += 1
            continue

        dbc = _days_before(close_ms, snapshot_ms)
        updates.append((dbc, row_id))
        stats["updated"] += 1

    if updates and not dry_run:
        print(f"Writing {len(updates)} updates...")
        conn.executemany(
            "UPDATE market_snapshots SET days_before_close = ? WHERE id = ?",
            updates,
        )
        conn.commit()

    conn.close()
    return stats


# ── Reporting ─────────────────────────────────────────────────────────────────

def _print_stats(stats: dict, dry_run: bool) -> None:
    if "error" in stats:
        print(f"Error: {stats['error']}")
        return

    print()
    print("  ── Op4 backfill summary ──")
    print(f"    Total NULL rows         : {stats['total']}")
    print(f"    Resolved by DB (fast)   : {stats['resolved_by_db']}")
    print(f"    Resolved by API (slow)  : {stats['resolved_by_api']}")
    print(f"    API attempts            : {stats['api_attempts']}")
    print(f"    Bad snapshot_at         : {stats['bad_snapshot_at']}")
    print(f"    Unresolvable            : {stats['unresolvable']}")
    print(f"    Updates {'(dry-run)' if dry_run else 'written'}        : {stats['updated']}")
    print()


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Op4 — backfill live days_before_close")
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute values but do not write them back")
    parser.add_argument("--no-api", action="store_true",
                        help="Skip Manifold API fallback (only use local resolved_markets)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Limit to N rows (useful for a dry-run smoke test)")
    parser.add_argument("--api-delay", type=float, default=0.2,
                        help="Seconds to sleep between API calls (default 0.2)")
    args = parser.parse_args()

    print("=" * 72)
    print("  Op4 — BACKFILL live days_before_close")
    print("=" * 72)
    print(f"  Mode   : {'DRY RUN' if args.dry_run else 'WRITE'}")
    print(f"  API    : {'OFF' if args.no_api else 'ON'}")
    if args.limit:
        print(f"  Limit  : {args.limit}")
    print()

    stats = backfill(
        dry_run     = args.dry_run,
        use_api     = not args.no_api,
        limit       = args.limit,
        api_delay_s = args.api_delay,
    )
    _print_stats(stats, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
