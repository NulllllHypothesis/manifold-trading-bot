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

# ── Phase 2.3 / 2.3b — early close decision model ─────────────────────────────
#
# position_score per open position, used by scripts/evaluate_positions.py:
#
#   position_score = current_unrealised_pnl
#                    - days_held * DAILY_DECAY_COST
#                    - (position_class == 'long_or_uncertain' ? LONG_HORIZON_PENALTY : 0)
#
# Phase 2.3 (PR #15) shipped this propose-only: score below threshold emitted
# a Telegram "approve close N" ask. Phase 2.3b — explicitly planned in PR #15
# ("after ~1 week of proposals + approvals, Phase 2.3b can flip a flag to
# auto-approve") — keeps the two-stage propose→approve shape but makes the
# APPROVER a machine step: the evaluator records the proposal, then
# RE-VALIDATES it against a fresh live-API probability (not the :05 reprice
# snapshot) and executes only if the breach survives re-validation (see
# CLOSE_CONFIRM_MARGIN_FACTOR below). Telegram is a notification of action
# taken, never a request for permission. The original human path remains
# available via `evaluate_positions.py --propose-only` (debug/legacy only).
# Safety guards (MIN_HOLD_HOURS, reprice staleness, resolved-market check)
# live in code and still gate every close.
#
# Starting values are conservative. Tune against the bet_outcomes
# era='early_close' trail + each trade's recorded close_context, and re-run
# scripts/backtest_close_thresholds.py weekly as snapshot history accumulates.
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
# MIN_HOLD_HOURS: minimum age before a position is eligible to be scored
# against the threshold. Prevents a rounding-artifact pathology on brand-
# new long_or_uncertain positions: at age 0.0d with pnl 0.0, the score is
# exactly -LONG_HORIZON_PENALTY (-0.50), and a few minutes of time-decay
# (0.005h * DAILY_DECAY_COST/24 ≈ 1e-5) pushes it fractionally below the
# strict `<` cutoff. Evaluator would fire CLOSE on a trade the `:20`
# trader just placed, undoing its decision 10 minutes later with no new
# information. Two hours is a pragmatic floor: enough for at least one
# Phase 2.2 :05 reprice cycle, short enough that genuinely bad trades
# aren't held indefinitely.
MIN_HOLD_HOURS = 2.0
# CLOSE_CONFIRM_MARGIN_FACTOR — Phase 2.3b's autonomous approver.
#
# The human approval step in Phase 2.3 was a stand-in for missing evidence:
# "does this breach hold up, or did one stale/noisy tick of the :05 snapshot
# trip the threshold?" Re-validation supplies that evidence directly. When a
# market's SNAPSHOT score breaches CLOSE_SCORE_THRESHOLD, the evaluator
# refetches the live probability and recomputes the score fresh, then:
#
#   fresh_score <= CLOSE_SCORE_THRESHOLD * CLOSE_CONFIRM_MARGIN_FACTOR
#       → execute IMMEDIATELY in the same run ('immediate_clear_margin').
#         With defaults that is fresh score <= -1.00 — a 2× breach confirmed
#         on live data is clear-cut; waiting a cycle only adds decay.
#   CLOSE_SCORE_THRESHOLD > fresh_score > threshold × factor (borderline)
#       → record the proposal and wait ONE evaluator cycle; execute on the
#         next :30 run only if a fresh refetch still breaches the threshold
#         ('persisted_next_cycle'). This is the one-tick-noise filter.
#   fresh_score >= CLOSE_SCORE_THRESHOLD
#       → 'recovered': the snapshot breach was noise; no close.
#   refetch fails
#       → DEFER to the next cycle. The evaluator never executes a close on
#         the stale snapshot alone.
#
# NOTE: the margin math assumes CLOSE_SCORE_THRESHOLD < 0 (multiplying a
# negative threshold by 2 moves it DEEPER into close territory). If the
# threshold is ever raised to >= 0, revisit this.
CLOSE_CONFIRM_MARGIN_FACTOR = 2.0

# ── Phase 2.4 — stale position detection (STRANDED / ABANDONED) ───────────────
#
# Timeline, measured from each market's closeTime:
#
#   now < closeTime                                → normal OPEN (Phase 2.3 scores it)
#   closeTime ≤ now < closeTime + grace            → OPEN (creator often late 1-2d)
#   closeTime + grace ≤ now < closeTime + timeout  → STRANDED (auto; reversible)
#   now ≥ closeTime + timeout                      → ABANDONED (propose-only write-off)
#
# STRANDED is reversible: resolve_positions scans STRANDED too and converts
# → WIN/LOSS with real P&L if the creator eventually resolves.
# ABANDONED is terminal: profit = -amount booked, era='write_off' in bet_outcomes.
STALE_GRACE_HOURS = 48       # creators regularly resolve 1-2 days late
STALE_ABANDON_DAYS = 90      # 3 months past close with no resolution = dead

# ── Trader EV gate (negative-expected-value invariant) ────────────────────────
#
# Trader rejects any recommendation whose execution-time EV is at or below
# MIN_ESTIMATED_EV_FLOOR. Prefers `estimated_ev_exec` (real stake-size EV),
# falls back to legacy `estimated_ev`. If BOTH are None the gate does NOT
# fire — a missing EV is different from a negative one; we don't block
# recs that predate the EV pipeline.
#
# Starting floor is 0.0: "do not knowingly trade negative expected value."
# The EV model CAN be wrong, but letting the trader override its own
# estimate muddies the learning loop — Phase 3's ev_error data becomes
# harder to read when we deliberately kept trades the model said were bad.
# Loosen toward -$0.25 later if/when enough ev_error samples show the
# model is systematically too pessimistic.
MIN_ESTIMATED_EV_FLOOR = 0.0

# When AI didn't supply ai_estimated_probability, the trader has to derive a
# probability estimate from the bot's own confidence to compute EV. Confidence
# is NOT a calibrated direction-correctness probability — it's a hand-picked
# strategy-level base score (0.65 for prob_direction, 0.68 for mean_reversion,
# etc.) that gets scaled by strategy weights. Treating it as P(direction wins)
# directly (as the original implementation did) overstates edge dramatically
# and produces phantom positive EV on coin-flip markets.
#
# The corrected estimate is current_prob + bounded edge in the direction's
# favor, where the edge is shrunk by this factor:
#
#   edge = (confidence - 0.5) * STAT_PROB_EDGE_SHRINKAGE
#   p_yes = current_prob + edge   (or - edge for direction='NO')
#
# At STAT_PROB_EDGE_SHRINKAGE = 0.3, max effective edge at confidence=0.95 is
# 0.135pp from current_prob — small enough to keep EV honest, large enough
# that real signals still produce non-zero EV for the calibration loop.
# Tighten toward 0.2 if Phase 3 ev_error data shows the model is still
# overestimating; loosen toward 0.5 if it's too pessimistic.
STAT_PROB_EDGE_SHRINKAGE = 0.3
