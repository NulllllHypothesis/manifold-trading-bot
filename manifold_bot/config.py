"""
Manifold Markets API Configuration
"""
import os

MANIFOLD_API_KEY = os.environ.get("MANIFOLD_API_KEY", "")
if not MANIFOLD_API_KEY:
    print("WARNING: MANIFOLD_API_KEY not set. Export it: export MANIFOLD_API_KEY=your-key-here")

MANIFOLD_API_BASE = "https://api.manifold.markets"

ENDPOINTS = {
    "markets": "/v0/markets",
    "market": "/v0/market/{marketId}",
    "bets": "/v0/bets",
    "bet": "/v0/bet",
    "me": "/v0/me",
    "users": "/v0/users",
    "groups": "/v0/groups",
    "comments": "/v0/comments",
    "search_markets": "/v0/search-markets"
}

INITIAL_BALANCE = 1000
MIN_BET_AMOUNT = 1
MAX_BET_AMOUNT = 100
# Trading confidence threshold — single source of truth.
# Dependents (all coupled — update together if this changes):
#   auto_trader.py:   AutoTrader.min_confidence = MIN_CONFIDENCE
#   auto_research.py: _WEAK_STAT_CAP   = MIN_CONFIDENCE - 0.01  (0.59 — cap for weak-stat markets)
#   auto_research.py: _STAT_BOOST_FLOOR = MIN_CONFIDENCE        (0.60 — floor before AI can boost)
#   auto_research.py: _MAX_AI_BOOST    = MIN_CONFIDENCE + 0.10  (0.70 — cap for exactly-floor markets)
MIN_CONFIDENCE = 0.60
