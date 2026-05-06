"""Read-only Polymarket Gamma API client.

This is the first non-Manifold data source for the bot. Polymarket markets
are real-money (USDC on Polygon), so the data has stronger resolution
discipline than Manifold's play-money markets. Initially we ingest for:

  1. Building a multi-platform corpus for the future LoRA fine-tune.
  2. Cross-platform mispricing detection (Manifold vs Polymarket on the
     same question).
  3. Eventually serving the assistant-UI surface that lets a user see
     positions across platforms.

Scope of this module:
  - Read-only HTTP client. We never place trades on Polymarket from here.
  - Public endpoints only (no API key required for /markets).
  - Mirrors `manifold_api.py`'s public surface where it makes sense:
    `get_markets()`, `get_market(market_id)`.

Endpoint: https://gamma-api.polymarket.com/markets
Docs:     https://docs.polymarket.com/developers/gamma-api

Field shape (verified 2026-05-06 against live API):
  id                string   (e.g., "540816")  ← used as market_id
  slug              string   (URL-safe identifier)
  question          string   (the market title)
  outcomes          [str]    (e.g., ["Yes", "No"])
  outcomePrices     [str]    (current prices as decimal strings, e.g. ["0.555", "0.445"])
  lastTradePrice    number   (most recent trade)
  liquidity         number   (USD)
  volume24hr        number   (USD)
  endDate           string   (ISO 8601 — e.g. "2026-07-31T12:00:00Z")
  events            [obj]    (nested event metadata; no separate `tags` field)
  active            bool
  closed            bool

Resolution fields are absent on active markets and present (with
varying shapes) on resolved markets — we handle that defensively in
the normalize step rather than assuming a fixed field.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional


GAMMA_API_BASE = "https://gamma-api.polymarket.com"

# Be polite to a free public endpoint. Polymarket doesn't publish a hard
# rate limit but sustained scraping isn't neighborly. 0.5s between calls
# = 7,200 calls/h, way more than we need (one full ingest per hour).
_INTER_CALL_SLEEP_SECONDS = 0.5

# User-Agent tells operators what's hitting their server. Worth being
# explicit so they can email us instead of blackholing the IP.
_USER_AGENT = "manifold-trading-bot/v2 (research; github.com/NulllllHypothesis/manifold-trading-bot)"

_REQUEST_TIMEOUT_SECONDS = 30


def _get(path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    """Single HTTP GET to the Gamma API. Returns parsed JSON, or raises."""
    import requests
    url = f"{GAMMA_API_BASE}{path}"
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
    response = requests.get(
        url, params=params or {}, headers=headers,
        timeout=_REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()


def get_markets(
    *,
    limit: int = 100,
    offset: int = 0,
    active: Optional[bool] = True,
    closed: Optional[bool] = False,
) -> List[Dict]:
    """Fetch a page of Polymarket markets.

    Args:
        limit:   page size (Polymarket's max is 500; default 100 keeps
                 individual pages small for incremental ingestion).
        offset:  cursor for pagination — caller iterates by calling
                 repeatedly with offset += len(returned page) until
                 the response is shorter than `limit`.
        active:  filter to active markets only (default True).
        closed:  filter to non-closed markets only (default False).

    Returns:
        List of raw market dicts as returned by the API. Use
        `normalize_market()` to convert each into the unified schema.

    Caller responsibilities:
        - Sleep `_INTER_CALL_SLEEP_SECONDS` between successive calls
          (the ingest script's loop handles this).
        - Handle exceptions — this raises requests.HTTPError on non-200
          responses. The ingest script's outer try/except logs and moves
          on so one bad page doesn't kill the whole run.
    """
    params: Dict[str, Any] = {"limit": limit, "offset": offset}
    if active is not None:
        params["active"] = "true" if active else "false"
    if closed is not None:
        params["closed"] = "true" if closed else "false"
    return _get("/markets", params=params)


def get_market(market_id: str) -> Optional[Dict]:
    """Fetch a single market by its Polymarket id (string).

    Returns None on 404 (market doesn't exist or has been hidden).
    Raises on other HTTP errors.
    """
    import requests
    try:
        return _get(f"/markets/{market_id}")
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return None
        raise


def iter_all_active_markets(page_size: int = 100, max_pages: Optional[int] = None):
    """Generator that yields every active, non-closed market across all pages.

    Yields one raw market dict at a time so the caller can stream them
    into the database without holding everything in memory.

    Sleeps `_INTER_CALL_SLEEP_SECONDS` between page fetches.

    Args:
        page_size:  markets per request (default 100).
        max_pages:  cap pagination — useful for tests and for the first
                    ingestion run while we tune behavior. None = unlimited.
    """
    offset = 0
    pages = 0
    while True:
        page = get_markets(limit=page_size, offset=offset, active=True, closed=False)
        if not page:
            return
        for market in page:
            yield market
        if len(page) < page_size:
            return  # short page = end of results
        offset += len(page)
        pages += 1
        if max_pages is not None and pages >= max_pages:
            return
        time.sleep(_INTER_CALL_SLEEP_SECONDS)
