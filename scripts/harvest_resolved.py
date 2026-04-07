#!/usr/bin/env python3
"""
Harvest resolved binary markets from Manifold API into SQLite.

What this builds:
  data/calibration.db → table: resolved_markets
  Columns:
    market_id          TEXT  — Manifold's unique market ID
    question           TEXT  — The question text
    probability_close  REAL  — Crowd probability when the market closed (0.0–1.0)
    outcome            TEXT  — 'YES' or 'NO' (how it actually resolved)
    close_date         TEXT  — ISO date string of when it closed
    uniqueBettors      INT   — Number of unique bettors (proxy for market quality)

Why we need this:
  Calibration step 2 reads this table to measure how often the crowd is right
  at each probability level. E.g. "when the crowd says 80%, does it resolve YES
  80% of the time, or only 65%?" That gap is the bias we inject into AI prompts.

Usage:
  python3 scripts/harvest_resolved.py           # fetch up to 1000 markets
  python3 scripts/harvest_resolved.py --limit 2000
  python3 scripts/harvest_resolved.py --refresh  # wipe and re-fetch everything
"""

import sys
import os
import sqlite3
import time
import argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from manifold_bot.manifold_api import ManifoldAPI
from manifold_bot.config import MANIFOLD_API_KEY

# ── Database ──────────────────────────────────────────────────────────────────

DB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
DB_PATH = os.path.join(DB_DIR, "calibration.db")


def _infer_category(question: str) -> str:
    """
    Infer a broad topic category from the question text using keyword matching.

    Categories:
      crypto    — Bitcoin, Ethereum, DeFi, NFT, blockchain
      politics  — elections, presidents, legislation, parties
      ai_tech   — AI models, LLMs, OpenAI, Anthropic, tech companies
      sports    — games, tournaments, championships, team names
      economics — markets, inflation, interest rates, GDP
      science   — climate, space, medicine, research outcomes
      other     — anything that doesn't match a more specific bucket

    This is intentionally rough — it only needs to be good enough to reveal
    whether calibration bias differs across broad topic areas.
    """
    q = question.lower()
    if any(w in q for w in ['bitcoin', 'btc', 'ethereum', 'eth', 'crypto', 'defi', 'nft', 'blockchain', 'solana', 'doge']):
        return 'crypto'
    if any(w in q for w in ['election', 'president', 'senate', 'congress', 'vote', 'democrat', 'republican', 'trump', 'biden', 'harris', 'political', 'legislation', 'parliament']):
        return 'politics'
    if any(w in q for w in ['gpt', 'llm', 'openai', 'anthropic', 'gemini', 'claude', 'deepseek', 'artificial intelligence', ' ai ', 'machine learning', 'neural', 'chatbot']):
        return 'ai_tech'
    if any(w in q for w in ['nba', 'nfl', 'nhl', 'mlb', 'soccer', 'football', 'basketball', 'baseball', 'tennis', 'golf', 'championship', 'tournament', 'match', 'game', 'win', 'score', 'league', 'team', 'player', 'season']):
        return 'sports'
    if any(w in q for w in ['stock', 'market', 'economy', 'inflation', 'gdp', 'fed', 'interest rate', 'recession', 'dow', 's&p', 'nasdaq', 'dollar', 'euro']):
        return 'economics'
    if any(w in q for w in ['climate', 'temperature', 'earthquake', 'hurricane', 'nasa', 'space', 'vaccine', 'drug', 'study', 'research', 'science']):
        return 'science'
    return 'other'


def init_db(conn: sqlite3.Connection):
    """Create tables if they don't exist yet, migrating schema when needed."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS resolved_markets (
            market_id         TEXT PRIMARY KEY,
            question          TEXT NOT NULL,
            probability_close REAL,          -- crowd prob at market close
            outcome           TEXT,          -- 'YES' or 'NO'
            close_date        TEXT,          -- ISO datetime string
            unique_bettors    INTEGER,
            category          TEXT           -- inferred topic bucket
        )
    """)
    # Schema migration: add category column to existing databases that predate it
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(resolved_markets)")}
    if 'category' not in existing_cols:
        conn.execute("ALTER TABLE resolved_markets ADD COLUMN category TEXT")
    # Index for fast bucket queries in step 2
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_prob_close
        ON resolved_markets(probability_close)
    """)
    conn.commit()


def count_rows(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM resolved_markets").fetchone()[0]


# ── Fetching ──────────────────────────────────────────────────────────────────

def fetch_resolved_page(api: ManifoldAPI, before: str = None, limit: int = 1000):
    """
    Fetch one page of resolved markets from Manifold.

    Manifold's pagination works like a cursor:
      - First call: no `before` param → returns the newest markets
      - Next call:  before=<last_market_id> → returns markets older than that one
    We keep fetching until we have enough or the API returns an empty page.
    """
    params = {
        "limit": min(limit, 1000),   # Manifold caps at 1000 per request
    }
    if before:
        params["before"] = before

    return api._make_request("GET", "/v0/markets", params=params)


def parse_market(m: dict) -> dict | None:
    """
    Extract the fields we care about from a raw Manifold market object.
    Returns None if the market is unusable (no resolution, wrong type, etc.).

    Key fields from Manifold API:
      m['id']                  — unique market ID
      m['question']            — the question text
      m['probability']         — crowd probability at market close (0.0-1.0)
      m['resolution']          — 'YES', 'NO', 'MKT', 'CANCEL', etc.
      m['closeTime']           — Unix timestamp ms when market closed
      m['uniqueBettorCount']   — how many different people bet on this market
    """
    # Only keep YES/NO resolutions — MKT (resolved to probability) and CANCEL
    # are not useful for calibration because there's no clear ground truth.
    resolution = m.get("resolution", "")
    if resolution not in ("YES", "NO"):
        return None

    # Markets with very few bettors are noisy — skip markets with < 3 bettors
    if (m.get("uniqueBettorCount") or 0) < 3:
        return None

    # `probability` is the crowd's last probability before close
    prob = m.get("probability")
    if prob is None:
        return None

    close_ts = m.get("closeTime")
    close_date = None
    if close_ts:
        # Manifold timestamps are milliseconds since epoch
        close_date = datetime.utcfromtimestamp(close_ts / 1000).isoformat()

    question = m.get("question", "")
    return {
        "market_id": m["id"],
        "question": question,
        "probability_close": float(prob),
        "outcome": resolution,
        "close_date": close_date,
        "unique_bettors": m.get("uniqueBettorCount", 0),
        "category": _infer_category(question),
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def harvest(target: int = 1000, refresh: bool = False):
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    if refresh:
        print("--refresh: wiping existing data")
        conn.execute("DELETE FROM resolved_markets")
        conn.commit()

    existing = count_rows(conn)
    print(f"DB already has {existing} rows. Target: {target}")

    if existing >= target and not refresh:
        print(f"Already have enough data ({existing} >= {target}). Use --refresh to re-fetch.")
        conn.close()
        return existing

    api = ManifoldAPI(MANIFOLD_API_KEY)
    if not MANIFOLD_API_KEY:
        print("ERROR: MANIFOLD_API_KEY not set. Check your .env file.")
        sys.exit(1)

    inserted = 0
    skipped_dup = 0
    skipped_bad = 0
    before = None       # pagination cursor
    page_num = 0

    print(f"\nFetching resolved BINARY markets from Manifold API...")
    print(f"(Each page = up to 1000 markets. We stop when we reach {target} total rows.)\n")

    while count_rows(conn) < target:
        page_num += 1
        print(f"Page {page_num}: fetching (cursor={before or 'start'})...", end=" ", flush=True)

        try:
            markets = fetch_resolved_page(api, before=before)
        except Exception as e:
            print(f"\nAPI error on page {page_num}: {e}")
            break

        if not markets:
            print("empty — no more data from API")
            break

        print(f"{len(markets)} markets received")

        page_inserted = 0
        for m in markets:
            parsed = parse_market(m)
            if parsed is None:
                skipped_bad += 1
                continue
            try:
                conn.execute("""
                    INSERT OR IGNORE INTO resolved_markets
                    (market_id, question, probability_close, outcome, close_date, unique_bettors, category)
                    VALUES (:market_id, :question, :probability_close, :outcome, :close_date, :unique_bettors, :category)
                """, parsed)
                if conn.execute("SELECT changes()").fetchone()[0] > 0:
                    inserted += 1
                    page_inserted += 1
                else:
                    skipped_dup += 1
            except Exception as e:
                print(f"  DB error: {e}")

        conn.commit()
        print(f"  → inserted {page_inserted} | total rows: {count_rows(conn)}")

        # Set cursor to the last market ID for next page
        before = markets[-1]["id"]

        # Small delay to be polite to Manifold's API
        time.sleep(0.3)

    final_count = count_rows(conn)
    conn.close()

    print(f"\n{'='*55}")
    print(f"Harvest complete")
    print(f"  Rows in DB:       {final_count}")
    print(f"  Newly inserted:   {inserted}")
    print(f"  Skipped (dup):    {skipped_dup}")
    print(f"  Skipped (bad):    {skipped_bad}")
    print(f"  DB path:          {DB_PATH}")
    print(f"{'='*55}")
    print(f"\nBackup command (run locally):")
    print(f"  scp hackathon@100.116.161.112:/home/hackathon/.openclaw/workspace/data/calibration.db ~/Desktop/calibration_backup.db")

    return final_count


def main():
    parser = argparse.ArgumentParser(description="Harvest resolved markets into SQLite")
    parser.add_argument("--limit", type=int, default=1000, help="Target number of rows (default: 1000)")
    parser.add_argument("--refresh", action="store_true", help="Wipe existing data and re-fetch")
    args = parser.parse_args()

    harvest(target=args.limit, refresh=args.refresh)


if __name__ == "__main__":
    main()
