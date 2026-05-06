"""Unified cross-platform markets schema — write side only (Tier-2 PR).

The strategic pivot 2026-04-27 demoted Manifold to one data source among
several. This module owns the SQLite table that holds normalized market
records from any platform, indexed by (platform, market_id).

What this module is NOT:
  - It is NOT a read-path consumer. No part of the existing trader,
    research, or evaluator reads from `markets_normalized`. The unified
    table is write-only for now — it accumulates while the existing
    Manifold-only pipeline keeps running unchanged. Read paths come in
    a follow-up PR alongside cross-platform matching.
  - It is NOT in `data/calibration.db` or `data/market_snapshots.db` —
    those have specific lifecycle assumptions (resolved-bet outcomes,
    live snapshots) that the cross-platform inventory shouldn't pollute.
    A new DB at `data/markets_normalized.db` keeps the boundary clean.

Schema:
  platform        TEXT NOT NULL    'manifold' | 'polymarket' | 'kalshi' | 'metaculus'
  market_id       TEXT NOT NULL    platform-native id (string for portability)
  question        TEXT NOT NULL    the market title
  probability     REAL             current YES probability, [0, 1] or NULL
  liquidity       REAL             USD or platform's native unit
  volume_24h      REAL             same
  close_time_ms   INTEGER          unix milliseconds (NULL if unknown)
  resolution      TEXT             'YES' / 'NO' / 'CANCEL' / 'MKT' / NULL when open
  tags            TEXT             JSON array of platform tags/categories
  source_url      TEXT             link to the market on its native platform
  fetched_at      TEXT NOT NULL    ISO-8601 UTC of the ingestion call
  PRIMARY KEY (platform, market_id)

Conventions:
  - Probabilities are always stored as [0, 1] floats. Polymarket gives
    us decimal strings like "0.555" — caller normalizes before insert.
  - close_time_ms is unix ms (matching Manifold's native unit), even for
    platforms that natively use ISO 8601. Caller converts.
  - resolution is None for active markets. Filled in by a later
    ingestion run when the market resolves on the source platform.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS markets_normalized (
    platform        TEXT NOT NULL,
    market_id       TEXT NOT NULL,
    question        TEXT NOT NULL,
    probability     REAL,
    liquidity       REAL,
    volume_24h      REAL,
    close_time_ms   INTEGER,
    resolution      TEXT,
    tags            TEXT,
    source_url      TEXT,
    fetched_at      TEXT NOT NULL,
    PRIMARY KEY (platform, market_id)
);

CREATE INDEX IF NOT EXISTS idx_norm_close_time  ON markets_normalized(close_time_ms);
CREATE INDEX IF NOT EXISTS idx_norm_fetched_at  ON markets_normalized(fetched_at);
CREATE INDEX IF NOT EXISTS idx_norm_resolution  ON markets_normalized(resolution);
"""


def init_db(db_path: Path) -> None:
    """Create the table + indexes if they don't exist. Idempotent."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_SCHEMA_DDL)
        conn.commit()
    finally:
        conn.close()


def upsert_markets(db_path: Path, rows: List[Dict[str, Any]]) -> int:
    """Insert or update normalized market rows.

    Each row must have at least `platform`, `market_id`, `question`,
    `fetched_at`. Other fields are optional and may be None.

    Returns the number of rows written.

    Uses INSERT OR REPLACE on the (platform, market_id) primary key —
    so re-running ingest on the same market overwrites the prior row
    with fresher prices/liquidity/etc., which is the expected behavior
    for a periodic snapshot.
    """
    if not rows:
        return 0
    conn = sqlite3.connect(db_path)
    try:
        conn.executemany(
            """
            INSERT OR REPLACE INTO markets_normalized
                (platform, market_id, question, probability, liquidity,
                 volume_24h, close_time_ms, resolution, tags, source_url,
                 fetched_at)
            VALUES (:platform, :market_id, :question, :probability, :liquidity,
                    :volume_24h, :close_time_ms, :resolution, :tags, :source_url,
                    :fetched_at)
            """,
            [_normalize_row_defaults(r) for r in rows],
        )
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def _normalize_row_defaults(row: Dict[str, Any]) -> Dict[str, Any]:
    """Fill in None for any optional column missing from `row`.

    Required columns (platform, market_id, question, fetched_at) raise
    KeyError if absent — callers shouldn't insert silently-malformed
    rows.
    """
    required = ("platform", "market_id", "question", "fetched_at")
    for k in required:
        if k not in row or row[k] is None:
            raise KeyError(f"required field '{k}' missing or None in: {row}")
    optional = ("probability", "liquidity", "volume_24h", "close_time_ms",
                "resolution", "tags", "source_url")
    return {**{k: None for k in optional}, **row}


# ── Polymarket-specific normalize ───────────────────────────────────────────

def normalize_polymarket_market(raw: Dict[str, Any], fetched_at: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Convert a raw Polymarket Gamma API market dict into our unified row.

    Returns None when the raw dict is missing fields that would make the
    row meaningless (no id, no question). Callers should drop None
    results and continue the loop — one bad market doesn't kill the
    whole ingest.

    Field mapping:
        id              → market_id (str)
        question        → question
        outcomePrices[0] → probability  (parsed from string to float)
        liquidity       → liquidity
        volume24hr      → volume_24h
        endDate         → close_time_ms (ISO 8601 → unix ms)
        slug            → source_url ('https://polymarket.com/market/{slug}')
        events[*].tags  → tags (JSON-encoded; empty list if missing)
        — resolution stays None for active markets (this function is only
          called on active+open Gamma responses for now)
    """
    market_id = raw.get("id")
    question = raw.get("question")
    if not market_id or not question:
        return None

    # outcomePrices comes from Gamma as a JSON-encoded STRING like
    # '["0.555", "0.445"]', not as a parsed list. Tests originally used
    # the parsed-list form; live response uses the encoded-string form.
    # Accept both — index 0 is the YES price (= P(YES)).
    probability: Optional[float] = None
    op_raw = raw.get("outcomePrices")
    op_list: Optional[list] = None
    if isinstance(op_raw, list):
        op_list = op_raw
    elif isinstance(op_raw, str) and op_raw.strip().startswith("["):
        try:
            decoded = json.loads(op_raw)
            if isinstance(decoded, list):
                op_list = decoded
        except json.JSONDecodeError:
            op_list = None
    if op_list:
        try:
            probability = float(op_list[0])
        except (ValueError, TypeError, IndexError):
            probability = None

    # endDate is ISO 8601 like "2026-07-31T12:00:00Z".
    close_time_ms: Optional[int] = None
    end_date = raw.get("endDate")
    if isinstance(end_date, str) and end_date:
        try:
            dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            close_time_ms = int(dt.timestamp() * 1000)
        except ValueError:
            close_time_ms = None

    slug = raw.get("slug")
    source_url = f"https://polymarket.com/market/{slug}" if slug else None

    # Tags: events is a nested array of event objects, each potentially with
    # its own tags array. Flatten to a single list of unique tag strings.
    # Guard every level — Gamma sometimes returns events as strings, None,
    # or with `tags` as a non-list. Reviewer caught a single malformed
    # event raising AttributeError and crashing the whole ingest run.
    tags_set = set()
    events = raw.get("events")
    if isinstance(events, list):
        for event in events:
            if not isinstance(event, dict):
                continue
            event_tags = event.get("tags")
            if not isinstance(event_tags, list):
                continue
            for t in event_tags:
                if isinstance(t, dict):
                    label = t.get("label") or t.get("slug")
                    if label:
                        tags_set.add(label)
                elif isinstance(t, str):
                    tags_set.add(t)
    tags_json = json.dumps(sorted(tags_set))

    if fetched_at is None:
        fetched_at = datetime.now(timezone.utc).isoformat()

    return {
        "platform":      "polymarket",
        "market_id":     str(market_id),
        "question":      str(question),
        "probability":   probability,
        "liquidity":     _safe_float(raw.get("liquidity")),
        "volume_24h":    _safe_float(raw.get("volume24hr")),
        "close_time_ms": close_time_ms,
        "resolution":    None,
        "tags":          tags_json,
        "source_url":    source_url,
        "fetched_at":    fetched_at,
    }


def _safe_float(v: Any) -> Optional[float]:
    """Best-effort float coercion. Returns None for non-numeric input."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
