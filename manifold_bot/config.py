"""
Manifold Markets API Configuration
"""
import os

# Load .env from the project root if the key isn't already in the environment.
# This lets scripts and tests work without manually exporting env vars.
_env_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env')
if os.path.exists(_env_file):
    with open(_env_file) as _fh:
        for _line in _fh:
            _line = _line.strip()
            if _line and not _line.startswith('#') and '=' in _line:
                _k, _v = _line.split('=', 1)
                os.environ.setdefault(_k.strip(), _v.strip())

MANIFOLD_API_KEY = os.environ.get("MANIFOLD_API_KEY", "")
if not MANIFOLD_API_KEY:
    print("WARNING: MANIFOLD_API_KEY not set. Export it: export MANIFOLD_API_KEY=your-key-here")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_GROUP_ID  = os.environ.get("TELEGRAM_GROUP_ID", "-5240775171")

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
# Throttled to $5 on 2026-04-09 while M2/M3 (backtesting + feedback loop repair) are underway.
# The bot keeps running so M1 snapshots accumulate, but can't blow the remaining balance.
# Restore to $25 once compute_strategy_weights.py has real signal (weights diverge from 1.0).
MAX_BET_AMOUNT = 5
# Trading confidence threshold — single source of truth.
# Dependents (all coupled — update together if this changes):
#   auto_trader.py:   AutoTrader.min_confidence = MIN_CONFIDENCE
#   auto_research.py: _WEAK_STAT_CAP   = MIN_CONFIDENCE - 0.01  (0.64 — cap for weak-stat markets)
#   auto_research.py: _STAT_BOOST_FLOOR = 0.65                  (hardcoded — decoupled so changing MIN_CONFIDENCE doesn't shift the floor unexpectedly)
#   auto_research.py: _MAX_AI_BOOST    = MIN_CONFIDENCE + 0.10  (0.75 — cap for exactly-floor markets)
MIN_CONFIDENCE = 0.65

# Maximum total open positions across all categories.
# auto_trader.py reads this as AutoTrader.max_positions.
MAX_POSITIONS = 10

# Maximum open positions allowed per market category (crypto/politics/ai_tech/etc).
# Prevents the bot from going all-in on one topic during a volatile week.
# 3 out of 10 total slots per category means no single category can dominate.
MAX_POSITIONS_PER_CATEGORY = 3

# Minimum market liquidity (total pool size) required to trade.
# Single source of truth — auto_research.py filters at scan time so no AI slot
# is wasted on a market the trader will reject; auto_trader.py and
# position_swap_checker.py both enforce this at execution time.
MIN_LIQUIDITY = 200

# NewsAPI.org key for the P3 news fetcher.
# Free tier: 100 requests/day. Get a key at https://newsapi.org/register
# Set in .env: NEWS_API_KEY=<your-key>
# When absent, news_fetcher.fetch_headlines() returns [] and news_strategy never fires.
NEWS_API_KEY = os.environ.get("NEWS_API_KEY", "")

# ── Phase 2.3 — early close / swap decision model ──────────────────────────────
#
# position_score per open position, used by scripts/evaluate_positions.py:
#
#   position_score = current_unrealised_pnl
#                    - days_held * DAILY_DECAY_COST
#                    - (position_class == 'long_or_uncertain' ? LONG_HORIZON_PENALTY : 0)
#
# If the score drops below CLOSE_SCORE_THRESHOLD, the evaluator emits a CLOSE
# proposal (human-approved via execute_close.py, same approval pattern as swaps).
# Auto-execution is deliberately off — we collect data first, tune thresholds
# against real outcomes, then consider turning on auto-close.
#
# Starting values are conservative. Tune after a week of evaluator runs against
# the Phase 2.2 snapshot trail.
#
# DAILY_DECAY_COST: dollars of "time value" lost per day held. $0.05/day means
# a $5 bet breaks even on decay alone in 100 days — slow enough not to punish
# short-term positions, fast enough to eventually pressure stale ones.
DAILY_DECAY_COST = 0.05
# LONG_HORIZON_PENALTY: flat penalty on position_class == 'long_or_uncertain'.
# These positions tie up a slot without a clear resolution path; the penalty
# pushes the evaluator to prefer closing one if a better opportunity exists.
LONG_HORIZON_PENALTY = 0.50
# CLOSE_SCORE_THRESHOLD: a position with score strictly below this triggers a
# CLOSE proposal. -0.50 means "we're already down 50¢ net of time cost" —
# tolerant of small unrealised losses, aggressive on persistent ones.
CLOSE_SCORE_THRESHOLD = -0.50
