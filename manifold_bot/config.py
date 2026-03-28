"""
Manifold Markets API Configuration
Store your API key securely here.
"""

MANIFOLD_API_KEY = "22a41d7d-10e8-44bf-93d6-5dff623f27a9"
MANIFOLD_API_BASE = "https://api.manifold.markets"

# API endpoints
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

# Trading parameters
INITIAL_BALANCE = 1000  # Starting play money
MIN_BET_AMOUNT = 1
MAX_BET_AMOUNT = 100