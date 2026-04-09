#!/usr/bin/env python3
"""
M3 — Reconstruct decision-time snapshots from resolved market bet history.

Problem this solves:
  The M1 snapshot logger captures market state hourly starting from now, but
  live markets take months to resolve. We need training examples where we know
  the outcome. The 1,121 resolved markets in calibration.db have known outcomes,
  but all we have is their final probability at close — not what they looked
  like 7, 14, or 30 days earlier when a real trading decision would have been made.

How it works:
  For each resolved market that passes quality filters (from M2 audit):
    1. Fetch full bet history via Manifold API (/v0/bets?contractId=<id>)
    2. Reconstruct probability at T-7d, T-14d, T-30d before close
       by finding the last bet before each target timestamp
    3. Write a synthetic snapshot row to data/market_snapshots.db
       tagged source='reconstructed', days_before_close=7/14/30

Schema additions to market_snapshots:
  source            TEXT  — 'live' (M1) or 'reconstructed' (M3)
  days_before_close INT   — days before market close (NULL for live snapshots)
  outcome           TEXT  — 'YES'/'NO' (NULL for live — not resolved yet)

Why three time windows?
  - T-30d: earliest useful signal (market well underway but outcome distant)
  - T-14d: middle ground
  - T-7d:  last week — still uncertain enough to trade

Result:
  ~3× more training examples than waiting for live snapshots to accumulate.
  More importantly: examples at decision-time, not near-close.

Usage:
  python3 scripts/reconstruct_snapshots.py              # reads m2_audit.json for IDs
  python3 scripts/reconstruct_snapshots.py --limit 100  # process first 100 markets only
  python3 scripts/reconstruct_snapshots.py --dry-run    # print what would happen, no writes
  python3 scripts/reconstruct_snapshots.py --market-id <id>  # process single market

Rate limiting:
  Manifold API doesn't publish a rate limit but we've seen 429s above ~5 req/s.
  This script sleeps 0.25s between requests (~4 req/s) to stay polite.
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

from manifold_bot.manifold_api import ManifoldAPI
from manifold_bot.config import MANIFOLD_API_KEY

AUDIT_PATH    = os.path.join(ROOT_DIR, "data", "m2_audit.json")
SNAPSHOTS_DB  = os.path.join(ROOT_DIR, "data", "market_snapshots.db")
CALIBRATION_DB = os.path.join(ROOT_DIR, "data", "calibration.db")

# Time windows to reconstruct (days before close)
RECONSTRUCTION_WINDOWS = [30, 14, 7]

# API request delay in seconds
API_DELAY = 0.25

# Max bets to fetch per market (Manifold paginates at 1000 per request)
BETS_PER_PAGE = 1000


# ── DB setup ──────────────────────────────────────────────────────────────────

def init_snapshots_db(conn: sqlite3.Connection) -> None:
    """
    Ensure market_snapshots table exists with M3 columns.
    Migrates existing tables (adds source / days_before_close / outcome if absent).
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS market_snapshots (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id         TEXT NOT NULL,
            question          TEXT,
            probability       REAL,
            volume24h         REAL,
            bettors           INTEGER,
            liquidity         REAL,
            snapshot_at       TEXT NOT NULL,
            source            TEXT DEFAULT 'live',
            days_before_close INTEGER,
            outcome           TEXT
        )
    """)
    # Schema migrations for tables created before M3
    existing = {row[1] for row in conn.execute("PRAGMA table_info(market_snapshots)")}
    for col, defn in [
        ("source",            "TEXT DEFAULT 'live'"),
        ("days_before_close", "INTEGER"),
        ("outcome",           "TEXT"),
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE market_snapshots ADD COLUMN {col} {defn}")

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_snapshots_market
        ON market_snapshots(market_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_snapshots_time
        ON market_snapshots(snapshot_at)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_snapshots_source
        ON market_snapshots(source)
    """)
    conn.commit()


def already_reconstructed(conn: sqlite3.Connection, market_id: str) -> bool:
    """Return True if this market already has reconstructed rows (skip on re-run)."""
    count = conn.execute(
        "SELECT COUNT(*) FROM market_snapshots WHERE market_id=? AND source='reconstructed'",
        (market_id,)
    ).fetchone()[0]
    return count > 0


# ── Bet history helpers ───────────────────────────────────────────────────────

def fetch_bets_for_market(api: ManifoldAPI, market_id: str) -> list[dict]:
    """
    Fetch all bets for a market using cursor-based pagination.

    Manifold returns bets newest-first.  We paginate until the API returns
    an empty page.  Returns the full list oldest-first for easier time-window
    scanning.

    Each bet dict has at minimum:
      createdTime  — Unix ms timestamp
      probAfter    — market probability immediately after this bet
      probBefore   — market probability before this bet
    """
    all_bets: list[dict] = []
    before_id = None

    while True:
        params: dict = {"contractId": market_id, "limit": BETS_PER_PAGE}
        if before_id:
            params["before"] = before_id

        try:
            page = api._make_request("GET", "/v0/bets", params=params)
        except Exception as e:
            print(f"    API error fetching bets for {market_id}: {e}")
            break

        if not page:
            break

        all_bets.extend(page)

        if len(page) < BETS_PER_PAGE:
            break  # last page

        before_id = page[-1]["id"]
        time.sleep(API_DELAY)

    # Return oldest-first so we can walk forward in time
    return list(reversed(all_bets))


def prob_at_time(bets_oldest_first: list[dict], target_ts_ms: int) -> float | None:
    """
    Reconstruct the market probability at target_ts_ms.

    Algorithm: find the last bet whose createdTime <= target_ts_ms and
    return its probAfter.  If no bet has been placed yet by that time,
    return None (market hadn't started, skip this window).

    bets_oldest_first: bets sorted oldest → newest
    target_ts_ms: target timestamp in milliseconds since epoch
    """
    last_prob = None
    for bet in bets_oldest_first:
        bet_ts = bet.get("createdTime", 0)
        if bet_ts <= target_ts_ms:
            prob = bet.get("probAfter")
            if prob is not None:
                last_prob = float(prob)
        else:
            break  # bets are oldest-first, no need to continue
    return last_prob


def parse_close_date(close_date_str: str) -> datetime | None:
    """Parse the close_date string stored in resolved_markets."""
    if not close_date_str:
        return None
    try:
        s = close_date_str.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except Exception:
        return None


# ── Per-market reconstruction ─────────────────────────────────────────────────

def reconstruct_market(
    api: ManifoldAPI,
    conn: sqlite3.Connection,
    market: dict,
    windows: list[int] = RECONSTRUCTION_WINDOWS,
    dry_run: bool = False,
) -> dict:
    """
    Reconstruct snapshots for one market.

    Returns a stats dict: {market_id, windows_written, windows_skipped, reason}
    """
    market_id  = market["market_id"]
    question   = market.get("question", "")
    outcome    = market.get("outcome")
    close_date = parse_close_date(market.get("close_date", ""))

    if close_date is None:
        return {"market_id": market_id, "windows_written": 0,
                "windows_skipped": len(windows), "reason": "no_close_date"}

    if already_reconstructed(conn, market_id):
        return {"market_id": market_id, "windows_written": 0,
                "windows_skipped": len(windows), "reason": "already_done"}

    bets = fetch_bets_for_market(api, market_id)
    time.sleep(API_DELAY)

    if not bets:
        return {"market_id": market_id, "windows_written": 0,
                "windows_skipped": len(windows), "reason": "no_bets"}

    written = 0
    skipped = 0
    rows_to_insert = []

    for days in windows:
        target_dt  = close_date - timedelta(days=days)
        target_ms  = int(target_dt.timestamp() * 1000)
        prob       = prob_at_time(bets, target_ms)

        if prob is None:
            skipped += 1
            continue  # market hadn't started betting by this time window

        snapshot_at = target_dt.isoformat()
        rows_to_insert.append((
            market_id,
            question,
            prob,
            None,   # volume24h — not available from bet history
            None,   # bettors   — not available for historical point
            None,   # liquidity — not available for historical point
            snapshot_at,
            "reconstructed",
            days,
            outcome,
        ))
        written += 1

    if rows_to_insert and not dry_run:
        conn.executemany("""
            INSERT INTO market_snapshots
            (market_id, question, probability, volume24h, bettors, liquidity,
             snapshot_at, source, days_before_close, outcome)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, rows_to_insert)
        conn.commit()

    return {
        "market_id":       market_id,
        "windows_written": written,
        "windows_skipped": skipped,
        "reason":          "ok" if written > 0 else "all_windows_empty",
    }


# ── Load market list ──────────────────────────────────────────────────────────

def load_usable_markets(
    audit_path: str  = AUDIT_PATH,
    calibration_db:  str = CALIBRATION_DB,
    market_id_filter: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    """
    Load the list of markets to reconstruct.

    Priority:
      1. --market-id flag  → just that one market
      2. m2_audit.json     → the pre-filtered usable list
      3. calibration.db    → fallback (all markets, no quality filter)
    """
    if market_id_filter:
        # Single market mode — load from calibration.db
        if not os.path.exists(calibration_db):
            raise FileNotFoundError(f"calibration.db not found at {calibration_db}")
        conn = sqlite3.connect(calibration_db)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM resolved_markets WHERE market_id=?", (market_id_filter,)
        ).fetchone()
        conn.close()
        if not row:
            raise ValueError(f"Market {market_id_filter} not found in calibration.db")
        return [dict(row)]

    if os.path.exists(audit_path):
        with open(audit_path) as f:
            audit = json.load(f)
        ids = audit.get("usable_market_ids", [])
        if not ids:
            print(f"  m2_audit.json has no usable_market_ids — falling back to calibration.db")
        else:
            # Load full rows from calibration.db for these IDs
            if not os.path.exists(calibration_db):
                raise FileNotFoundError(f"calibration.db not found at {calibration_db}")
            conn = sqlite3.connect(calibration_db)
            conn.row_factory = sqlite3.Row
            placeholders = ",".join("?" * len(ids))
            rows = conn.execute(
                f"SELECT * FROM resolved_markets WHERE market_id IN ({placeholders})",
                ids
            ).fetchall()
            conn.close()
            markets = [dict(r) for r in rows]
            if limit:
                markets = markets[:limit]
            return markets

    # Fallback: load everything from calibration.db (no quality filter)
    print(f"  m2_audit.json not found — run audit_resolved_markets.py first for quality filtering")
    conn = sqlite3.connect(calibration_db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM resolved_markets").fetchall()
    conn.close()
    markets = [dict(r) for r in rows]
    if limit:
        markets = markets[:limit]
    return markets


# ── Main ──────────────────────────────────────────────────────────────────────

def reconstruct(
    markets: list[dict],
    snapshots_db: str = SNAPSHOTS_DB,
    dry_run: bool = False,
    verbose: bool = False,
) -> dict:
    """
    Reconstruct snapshots for all markets in the list.

    Returns a summary dict.
    """
    if not MANIFOLD_API_KEY:
        print("ERROR: MANIFOLD_API_KEY not set. Check your .env file.")
        sys.exit(1)

    api  = ManifoldAPI(MANIFOLD_API_KEY)
    os.makedirs(os.path.dirname(snapshots_db), exist_ok=True)
    conn = sqlite3.connect(snapshots_db)
    init_snapshots_db(conn)

    total_written  = 0
    total_skipped  = 0
    already_done   = 0
    no_bets        = 0
    errors         = 0

    print(f"\n{'='*65}")
    print(f"M3 SNAPSHOT RECONSTRUCTION")
    print(f"{'='*65}")
    print(f"Markets to process: {len(markets)}")
    print(f"Time windows:       T-{', T-'.join(str(d) for d in RECONSTRUCTION_WINDOWS)} days")
    print(f"Dry run:            {dry_run}")
    print(f"{'─'*65}\n")

    for i, market in enumerate(markets, 1):
        mid = market["market_id"]
        try:
            result = reconstruct_market(api, conn, market, dry_run=dry_run)
        except Exception as e:
            print(f"  [{i}/{len(markets)}] {mid[:20]}... ERROR: {e}")
            errors += 1
            continue

        reason = result["reason"]
        if reason == "already_done":
            already_done += 1
        elif reason == "no_bets":
            no_bets += 1

        total_written += result["windows_written"]
        total_skipped += result["windows_skipped"]

        if verbose or result["windows_written"] == 0:
            print(f"  [{i}/{len(markets)}] {mid[:25]:25} "
                  f"wrote={result['windows_written']} "
                  f"skipped={result['windows_skipped']} "
                  f"({reason})")
        elif i % 50 == 0 or i == len(markets):
            pct = i / len(markets) * 100
            print(f"  Progress: {i}/{len(markets)} ({pct:.0f}%)  "
                  f"rows written so far: {total_written}")

        time.sleep(API_DELAY)

    conn.close()

    summary = {
        "markets_processed":   len(markets),
        "rows_written":        total_written,
        "windows_skipped":     total_skipped,
        "already_done":        already_done,
        "no_bets":             no_bets,
        "errors":              errors,
        "dry_run":             dry_run,
    }

    print(f"\n{'='*65}")
    print(f"RECONSTRUCTION COMPLETE")
    print(f"{'─'*65}")
    print(f"  Markets processed:   {len(markets)}")
    print(f"  Snapshot rows written: {total_written}")
    print(f"  Windows skipped:     {total_skipped}  (market hadn't started by window)")
    print(f"  Already done:        {already_done}  (skipped re-run)")
    print(f"  No bets found:       {no_bets}")
    print(f"  Errors:              {errors}")
    print(f"  DB:                  {snapshots_db}")
    print(f"{'='*65}\n")

    if not dry_run and total_written > 0:
        print("Next steps:")
        print("  1. Run scripts/compute_strategy_weights.py to pick up new signal")
        print("  2. Check if strategy weights diverge from 1.0")
        print("  3. If yes → restore MAX_BET_AMOUNT in manifold_bot/config.py")

    return summary


def main():
    parser = argparse.ArgumentParser(
        description="M3: reconstruct decision-time snapshots from bet history"
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Process only the first N usable markets (default: all)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would happen without writing to DB"
    )
    parser.add_argument(
        "--market-id", type=str, default=None,
        help="Process a single market ID (for debugging)"
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Print one line per market (default: print every 50)"
    )
    args = parser.parse_args()

    markets = load_usable_markets(
        market_id_filter=args.market_id,
        limit=args.limit,
    )
    reconstruct(markets, dry_run=args.dry_run, verbose=args.verbose)


if __name__ == "__main__":
    main()
