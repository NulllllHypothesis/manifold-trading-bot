#!/usr/bin/env python3
"""
Automated market research script for Manifold Markets.
Runs hourly to analyze markets and identify trading opportunities.
"""

import json
import sqlite3
import sys
import statistics as _stats
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional
import os

# Add project root to path so manifold_bot package is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from manifold_bot.manifold_api import api_client
from manifold_bot.strategies import TradingStrategies, _infer_market_category, _is_noise_market
from manifold_bot.paper_trader import PaperTrader
from manifold_bot.ai_analyzer import batch_analyze
from manifold_bot.config import MIN_CONFIDENCE, MIN_LIQUIDITY, NEWS_API_KEY, MAX_BET_AMOUNT
from manifold_bot.news_fetcher import fetch_headlines
from scripts.position_swap_checker import run_swap_check

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SNAPSHOTS_DB_PATH = os.path.join(_ROOT, "data", "market_snapshots.db")
_RESEARCH_COUNTERS_PATH = os.path.join(_ROOT, "data", "research_counters.jsonl")


# ── Op3: Strategy observability ───────────────────────────────────────────────
# Per-run counters covering every stage of the research pipeline. Used to
# diagnose why the live system is currently mono-strategy — before any base
# confidence retuning we need evidence of WHERE in the pipeline the non-
# probability_direction signals are being lost (never fire? dedup'd out?
# tied? vetoed by AI?).
#
# See PLAN.md Op3 for the full motivation and Op5 for the retune that these
# counters will inform.

def _new_research_counters() -> dict:
    """Return a fresh counters dict — one per analyze_markets() call."""
    return {
        "timestamp":           None,        # set at run start, ISO UTC
        "markets_fetched":     0,
        # Pre-strategy filter skips
        "skipped_resolved":    0,
        "skipped_low_liquidity": 0,
        "skipped_stale":       0,
        "skipped_noise":       0,
        # Raw strategy fires (counted BEFORE family dedup / weight scaling)
        "raw_fires": {
            "probability_direction": 0,
            "mean_reversion":        0,
            "probability_bias":      0,
            "creator_disagreement":  0,
            "thin_market":           0,
        },
        # Family dedup winners (one per family per market, max)
        "dedup_winners": {
            "momentum":    0,
            "contrarian":  0,
            "fundamental": 0,
        },
        "thin_market_confirmations": 0,   # thin_market +0.03 boost applied
        "no_active_signals":         0,   # all strategies silent → dropped
        "tied_votes_dropped":        0,   # yes_votes == no_votes → dropped
        # AI flow
        "ai_eligible":     0,   # recs with confidence above _STAT_BOOST_FLOOR
        "ai_cooled_down":  0,   # filtered out by AI-timeout cooldown
        "ai_analyzed":     0,   # actually sent to the AI
        "ai_agree":        0,
        "ai_disagree":     0,
        "ai_skip":         0,   # AI returned SKIP
        "ai_no_result":    0,   # timeout / missing response
        # Final output composition
        "final_recommendations": 0,
        "final_by_strategy_mix": {},    # e.g. {"probability_direction": 9}
        "final_by_category":     {},
    }


def _finalize_research_counters(counters: dict, recommendations: list) -> None:
    """
    Compute the 'final_*' breakdowns from the output recommendation list.
    Called once right before save_research() so the counters reflect the
    exact recs that will be written to market_research.json.
    """
    counters["final_recommendations"] = len(recommendations)
    by_mix: dict = {}
    by_cat: dict = {}
    for rec in recommendations:
        # Strategy mix signature: sorted tuple of strategy names, joined.
        # Excludes 'ai_analysis' because that is a post-hoc tag, not a signal.
        strats = sorted(s for s in (rec.get('strategies') or []) if s != 'ai_analysis')
        key = ",".join(strats) if strats else "(none)"
        by_mix[key] = by_mix.get(key, 0) + 1
        cat = rec.get('category') or 'other'
        by_cat[cat] = by_cat.get(cat, 0) + 1
    counters["final_by_strategy_mix"] = by_mix
    counters["final_by_category"]     = by_cat


def _print_research_counters(counters: dict) -> None:
    """Human-readable end-of-run summary block printed to the cron log."""
    print()
    print("  ── Research observability (Op3) ──────────────────────────────")
    print(f"    Markets fetched      : {counters['markets_fetched']}")
    print(f"    Skipped (resolved)   : {counters['skipped_resolved']}")
    print(f"    Skipped (low liq)    : {counters['skipped_low_liquidity']}")
    print(f"    Skipped (stale)      : {counters['skipped_stale']}")
    print(f"    Skipped (noise)      : {counters['skipped_noise']}")
    print(f"    Raw strategy fires:")
    for strat, n in counters['raw_fires'].items():
        print(f"      {strat:<22} {n:>5}")
    print(f"    Family dedup winners:")
    for fam, n in counters['dedup_winners'].items():
        print(f"      {fam:<22} {n:>5}")
    print(f"    Thin-market confirmations : {counters['thin_market_confirmations']}")
    print(f"    No active signals (drop)  : {counters['no_active_signals']}")
    print(f"    Tied YES/NO votes (drop)  : {counters['tied_votes_dropped']}")
    print(f"    AI flow:")
    print(f"      eligible     {counters['ai_eligible']:>4}"
          f"    cooled-down  {counters['ai_cooled_down']:>4}"
          f"    analyzed    {counters['ai_analyzed']:>4}")
    print(f"      agree        {counters['ai_agree']:>4}"
          f"    disagree     {counters['ai_disagree']:>4}"
          f"    skip        {counters['ai_skip']:>4}"
          f"    no-result   {counters['ai_no_result']:>4}")
    print(f"    Final recommendations: {counters['final_recommendations']}")
    if counters['final_by_strategy_mix']:
        print(f"    Strategy mix:")
        for mix, n in sorted(counters['final_by_strategy_mix'].items(), key=lambda x: -x[1]):
            print(f"      {mix:<40} {n:>3}")
    if counters['final_by_category']:
        print(f"    Category mix:")
        for cat, n in sorted(counters['final_by_category'].items(), key=lambda x: -x[1]):
            print(f"      {cat:<22} {n:>3}")
    print("  ──────────────────────────────────────────────────────────────")


def _append_research_counters(counters: dict) -> None:
    """Append one JSON line to data/research_counters.jsonl for trend analysis."""
    try:
        os.makedirs(os.path.dirname(_RESEARCH_COUNTERS_PATH), exist_ok=True)
        with open(_RESEARCH_COUNTERS_PATH, "a") as f:
            f.write(json.dumps(counters) + "\n")
    except Exception as e:
        # Observability must never break the research run itself.
        print(f"  Warning: could not append research counters ({e})")

# ── AI timeout cooldown ────────────────────────────────────────────────────────
# Markets that repeatedly time out Ollama waste all 3 AI candidate slots every
# hour.  After _AI_TIMEOUT_MAX_FAILS failures within the cooldown window, the
# market is excluded from AI candidate selection for _AI_TIMEOUT_COOLDOWN_HOURS.
# State is persisted in data/ai_timeout_cooldown.json (gitignored runtime file).
_AI_TIMEOUT_COOLDOWN_PATH = os.path.join(_ROOT, "data", "ai_timeout_cooldown.json")
_AI_TIMEOUT_COOLDOWN_HOURS = 24   # how long a market stays on cooldown
# Raised from 2 → 4 (2026-04-13): Ollama on CPU legitimately takes 60-80s
# on cold prompts. A single slow batch puts 3 markets on cooldown at once,
# so fails=2 was cascading to block 87% of AI-eligible recs within a day.
# fails=4 means a market must time out consistently across multiple hours
# before exclusion — one or two cold starts won't permanently block it.
_AI_TIMEOUT_MAX_FAILS = 4         # failures within that window before exclusion

# ── AI analysis cache (Phase 1.3 of V2) ─────────────────────────────────────
# Analyze-once cache: store AI results per market so we don't re-analyze the
# same market every hour when nothing has changed. Invalidation rules:
#   - TTL: entry is >24h old
#   - Probability moved >5pp since last analysis
#   - 24h volume doubled since last analysis
#
# When a cache entry is valid, we skip AI for that market and reuse the
# cached result. This frees AI bandwidth to analyze NEW markets instead of
# cycling through the same top-3 every hour.
_AI_ANALYSIS_CACHE_PATH = os.path.join(_ROOT, "data", "ai_analysis_cache.json")
_AI_CACHE_TTL_HOURS = 24
_AI_CACHE_PROB_INVALIDATION = 0.05   # 5pp probability move invalidates cache
_AI_CACHE_VOLUME_MULTIPLIER = 2.0    # 2x volume24h growth invalidates cache


def _load_ai_analysis_cache() -> dict:
    """
    Load {market_id: {result, analyzed_at, market_state_at_analysis}} from
    data/ai_analysis_cache.json. Prunes entries older than the TTL so the
    file stays small. Returns empty dict on any read error.
    """
    try:
        if not os.path.exists(_AI_ANALYSIS_CACHE_PATH):
            return {}
        with open(_AI_ANALYSIS_CACHE_PATH) as f:
            cache = json.load(f)
        if not isinstance(cache, dict):
            return {}
        # Prune stale entries
        now = time.time()
        ttl_sec = _AI_CACHE_TTL_HOURS * 3600
        pruned = {}
        for mid, entry in cache.items():
            if not isinstance(entry, dict):
                continue
            analyzed_at = entry.get('analyzed_at', 0)
            if not isinstance(analyzed_at, (int, float)):
                continue
            if now - analyzed_at < ttl_sec:
                pruned[mid] = entry
        return pruned
    except Exception:
        return {}


def _save_ai_analysis_cache(cache: dict) -> None:
    """Persist AI analysis cache to disk. Silent on write errors."""
    try:
        os.makedirs(os.path.dirname(_AI_ANALYSIS_CACHE_PATH), exist_ok=True)
        with open(_AI_ANALYSIS_CACHE_PATH, 'w') as f:
            json.dump(cache, f)
    except Exception:
        pass


def _is_cache_valid(entry: dict, current_market: dict) -> bool:
    """
    Check if a cached AI result is still valid given current market state.

    Returns False if:
    - Entry is malformed
    - Market state moved enough to warrant re-analysis (prob > 5pp or vol 2x)
    - TTL already handled at load time in _load_ai_analysis_cache()
    """
    if not isinstance(entry, dict):
        return False
    prior_state = entry.get('market_state_at_analysis')
    if not isinstance(prior_state, dict):
        return False

    prior_prob = prior_state.get('probability')
    curr_prob = current_market.get('probability')
    if isinstance(prior_prob, (int, float)) and isinstance(curr_prob, (int, float)):
        if abs(float(curr_prob) - float(prior_prob)) > _AI_CACHE_PROB_INVALIDATION:
            return False

    prior_vol = prior_state.get('volume24h', 0)
    curr_vol = current_market.get('volume24Hours', current_market.get('volume24h', 0))
    if isinstance(prior_vol, (int, float)) and isinstance(curr_vol, (int, float)):
        if prior_vol > 0 and float(curr_vol) >= float(prior_vol) * _AI_CACHE_VOLUME_MULTIPLIER:
            return False

    return True


def _cache_ai_result(cache: dict, market_id: str, market: dict, ai_result: dict) -> None:
    """Write an AI result to the in-memory cache dict (caller persists later)."""
    cache[market_id] = {
        'result': ai_result,
        'analyzed_at': time.time(),
        'market_state_at_analysis': {
            'probability': market.get('probability'),
            'volume24h': market.get('volume24Hours', market.get('volume24h', 0)),
            'unique_bettors': market.get('uniqueBettorCount'),
        },
    }


def _load_ai_timeout_cooldown() -> dict:
    """
    Load {market_id: {fails, last_fail_at}} from data/ai_timeout_cooldown.json.
    Prunes entries older than _AI_TIMEOUT_COOLDOWN_HOURS so the file stays small.
    Returns empty dict on any read error (first run, disk error, etc.).
    """
    try:
        with open(_AI_TIMEOUT_COOLDOWN_PATH) as f:
            raw = json.load(f)
    except Exception:
        return {}
    cutoff = datetime.now(timezone.utc) - timedelta(hours=_AI_TIMEOUT_COOLDOWN_HOURS)
    pruned = {}
    for mid, entry in raw.items():
        try:
            last_fail = datetime.fromisoformat(entry.get("last_fail_at", "2000-01-01T00:00:00+00:00"))
            if last_fail > cutoff:
                pruned[mid] = entry
        except Exception:
            pass  # drop malformed entries
    return pruned


def _save_ai_timeout_cooldown(cooldown: dict) -> None:
    """Persist the cooldown dict.  Silently skips on write error."""
    try:
        os.makedirs(os.path.dirname(_AI_TIMEOUT_COOLDOWN_PATH), exist_ok=True)
        with open(_AI_TIMEOUT_COOLDOWN_PATH, "w") as f:
            json.dump(cooldown, f)
    except Exception as e:
        print(f"  Warning: could not save AI cooldown state ({e})")


def _is_in_ai_cooldown(market_id: str, cooldown: dict) -> bool:
    """Return True if this market has failed >= _AI_TIMEOUT_MAX_FAILS times recently."""
    entry = cooldown.get(market_id)
    if not entry:
        return False
    return entry.get("fails", 0) >= _AI_TIMEOUT_MAX_FAILS


def _record_ai_timeout(market_ids: list, cooldown: dict) -> None:
    """Increment fail counter for each market_id and update the timestamp."""
    now = datetime.now(timezone.utc).isoformat()
    for mid in market_ids:
        entry = cooldown.setdefault(mid, {"fails": 0})
        entry["fails"] = entry.get("fails", 0) + 1
        entry["last_fail_at"] = now


def _write_market_snapshots(markets: list) -> None:
    """
    Write one snapshot row per non-resolved market to data/market_snapshots.db.

    Called after every hourly market fetch (~100 rows/run).  After 2–3 months,
    these rows can be joined against resolved_markets to produce training data
    for the ML pipeline (M1 in PLAN.md).  M3 adds reconstructed rows to the
    same table tagged source='reconstructed'.

    Schema: market_id, question, probability, volume24h, bettors, liquidity,
            snapshot_at, source, days_before_close, outcome
    """
    os.makedirs(os.path.dirname(_SNAPSHOTS_DB_PATH), exist_ok=True)
    snapshot_at_dt = datetime.now(timezone.utc)
    snapshot_at    = snapshot_at_dt.isoformat()
    snapshot_at_ms = snapshot_at_dt.timestamp() * 1000.0

    conn = sqlite3.connect(_SNAPSHOTS_DB_PATH)
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
    # Migrate tables created before M3 columns were added
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(market_snapshots)")}
    for col, defn in [
        ("source",            "TEXT DEFAULT 'live'"),
        ("days_before_close", "INTEGER"),
        ("outcome",           "TEXT"),
    ]:
        if col not in existing_cols:
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

    rows = []
    for m in markets:
        if not m.get("id") or m.get("isResolved"):
            continue

        # Op4: compute days_before_close from the market's closeTime at snapshot
        # time. Without this, live rows are unusable for M4/M5/M6 training because
        # build_training_dataset.py skips rows where days_before_close is None
        # (near-close prices converge to the outcome and would inflate accuracy).
        # closeTime is Manifold's unix-ms close timestamp; we convert to integer
        # days and floor non-positive values to 0 so already-closed markets are
        # visibly distinct from "field missing" (None).
        close_ms = m.get("closeTime")
        days_before_close: Optional[int] = None
        if close_ms is not None:
            try:
                delta_ms = float(close_ms) - snapshot_at_ms
                days_before_close = max(0, int(delta_ms // (24 * 60 * 60 * 1000)))
            except (TypeError, ValueError):
                days_before_close = None

        rows.append((
            m.get("id"),
            (m.get("question") or "")[:200],
            m.get("probability"),
            m.get("volume24Hours") or 0.0,
            m.get("uniqueBettorCount") or 0,
            m.get("totalLiquidity") or 0.0,
            snapshot_at,
            "live",
            days_before_close,
            None,   # outcome — not resolved yet
        ))

    conn.executemany("""
        INSERT INTO market_snapshots
        (market_id, question, probability, volume24h, bettors, liquidity,
         snapshot_at, source, days_before_close, outcome)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, rows)

    conn.commit()
    conn.close()
    print(f"  Snapshots: {len(rows)} rows → market_snapshots.db")
_STRATEGY_WEIGHTS_PATH = os.path.join(_ROOT, "data", "strategy_weights.json")
_DEFAULT_STRATEGY_WEIGHT = 1.0


def _load_strategy_weights() -> tuple[dict, dict]:
    """Load strategy weights from data/strategy_weights.json.

    Returns (global_weights, by_category_weights); both default to {} on any error.
    """
    try:
        with open(_STRATEGY_WEIGHTS_PATH) as f:
            data = json.load(f)
        return data.get("weights", {}), data.get("by_category", {})
    except Exception:
        return {}, {}

# Single source of truth for the trading threshold — imported from config.py.
# auto_trader.py also imports MIN_CONFIDENCE; changing it there updates both.
_WEAK_STAT_CAP = MIN_CONFIDENCE - 0.01   # cap for low-stat markets (stat < floor): 0.64
# Stat must be strictly above floor before AI can boost freely.
# At exactly 0.65, AI is still capped to stat_conf + 0.10 to prevent turbo-boosting a borderline signal.
_STAT_BOOST_FLOOR = 0.65                 # markets below this floor are capped at _WEAK_STAT_CAP
# The AI boost cap is relative: blended confidence is capped at stat_conf + 0.10.
# This means a market with stat_conf=0.65 is capped at 0.75, stat_conf=0.9 is capped at 1.0, etc.
_AI_BOOST_MARGIN = 0.10

# Sentinel value for markets never sent to AI (distinct from 'SKIP' which means
# the AI considered the market and chose to skip it).
_NOT_ANALYZED = 'NOT_ANALYZED'

# Volume spike detection multiplier for 24h volume comparison.
# Previously this threshold was calibrated against all-time volume magnitudes.
# After switching to volume24Hours (which is orders of magnitude smaller than
# all-time volume), a 2x multiplier is still appropriate: a market seeing twice
# the median 24h volume of the batch is a genuine spike signal regardless of
# the absolute scale. The key insight is that we are comparing 24h volume
# against 24h median, so the ratio is dimensionally consistent and 2x remains
# a reasonable threshold for detecting abnormal short-term activity.
VOLUME_SPIKE_MULTIPLIER = 2.0

# Number of top candidates to enrich with news headlines.
# Free tier: 100 req/day.  3 candidates × 24 runs = 72 max (with daily cache,
# much fewer in practice since the same market often stays in top-3 for hours).
_NEWS_CANDIDATES = 3

class MarketResearcher:
    """Automated market research and analysis"""

    def __init__(self):
        self.research_file = os.path.join(_ROOT, "market_research.json")
        self.trader = PaperTrader()  # Will auto-load state from __init__

    def analyze_markets(self, limit: int = 50) -> List[Dict]:
        """
        Analyze markets and identify trading opportunities

        Returns:
            List of market recommendations with confidence scores

        Schema notes (schema_version=2):
          ai_was_candidate: bool  — True iff the market was in the top-3 sent to AI.
          ai_returned_skip: bool  — True iff the market was a candidate AND AI returned SKIP.
          ai_recommendation: str | None
            - None            : market was never sent to AI (ai_was_candidate=False).
                                Previously this was set to 'SKIP', which was misleading.
            - 'SKIP'          : AI was sent this market and explicitly said SKIP.
            - 'YES' / 'NO'    : AI's directional recommendation.
        """
        print(f"[{datetime.now()}] Starting market analysis...")

        # Op3: per-run observability counters.  Populated inline at each stage
        # of the pipeline below and emitted at the end of the try block.
        counters = _new_research_counters()
        counters["timestamp"] = datetime.now(timezone.utc).isoformat()

        try:
            # Get recent markets
            markets = api_client.get_markets(limit=limit)
            print(f"  Retrieved {len(markets)} markets")
            counters["markets_fetched"] = len(markets)

            # M1: write hourly snapshot for ML training pipeline.
            # Best-effort: a snapshot failure (disk full, lock, schema) must never
            # abort the research cycle — market analysis is the primary job here.
            try:
                _write_market_snapshots(markets)
            except Exception as snap_err:
                print(f"  Warning: snapshot write failed ({snap_err}) — continuing without snapshots")

            # Compute median 24h volume once (outside the loop) to avoid O(n²) recomputation.
            # volume24Hours is the correct signal: total volume inflates for old markets.
            missing_vol24 = sum(1 for m in markets if m.get('volume24Hours') is None)
            if missing_vol24:
                print(f"  Note: {missing_vol24}/{len(markets)} markets missing volume24Hours, counted as 0")
            volumes24 = [m.get('volume24Hours') or 0 for m in markets]
            median_volume24h = _stats.median(volumes24) if volumes24 else 0

            # Log the median so operators can verify the spike multiplier is reasonable
            # for the current batch. If median_volume24h is consistently near 0, the
            # multiplier may need revisiting.
            print(f"  Median 24h volume across batch: {median_volume24h:.2f} "
                  f"(spike threshold: >{VOLUME_SPIKE_MULTIPLIER}x = {VOLUME_SPIKE_MULTIPLIER * median_volume24h:.2f})")

            # Check whether creator_disagreement's API field is present.
            # This strategy has the highest weight (0.75) but fires only when
            # resolutionProbability is set — which is rare. Log once per run so
            # we know if the field is always absent (meaning the strategy never fires).
            TradingStrategies.log_creator_disagreement_field_presence(markets)

            # Load strategy reliability weights once per run.
            strategy_weights, _by_category_weights = _load_strategy_weights()

            recommendations = []

            for market in markets:
                market_id = market.get('id')
                question = market.get('question', 'Unknown')[:100]
                probability = market.get('probability', 0.5)
                volume = market.get('volume', 0)
                liquidity = market.get('totalLiquidity', 0)

                # Skip closed/resolved markets
                if market.get('isResolved', False):
                    counters["skipped_resolved"] += 1
                    continue

                # Skip low liquidity markets — threshold matches auto_trader.py (MIN_LIQUIDITY).
                # Previously this was 100 while the trader rejected < 200, causing AI slots
                # to be wasted on markets that could never be traded.
                if liquidity < MIN_LIQUIDITY:
                    counters["skipped_low_liquidity"] += 1
                    continue

                # Skip stale markets — last bet > 72h ago AND near-zero 24h volume
                if TradingStrategies.is_stale_market(market):
                    counters["skipped_stale"] += 1
                    continue

                # Skip structurally random markets (coinflip, free lottery, etc.)
                # before ANY strategy evaluates them. These markets resolve at
                # ~50% by construction — no strategy signal is meaningful.
                # Previously this filter was only inside probability_bias_strategy,
                # so noise markets still entered via probability_direction.
                if _is_noise_market(question):
                    counters["skipped_noise"] += 1
                    continue

                # ── Run all strategies ───────────────────────────────────────
                #
                # Signal families prevent correlated signals from stacking:
                #   momentum    — at most 1 signal taken (highest confidence)
                #   contrarian  — at most 1 signal taken (highest confidence)
                #   fundamental — always independent (creator_disagreement)
                #   filter      — priority boost only, no directional vote
                #
                # thin_market is contrarian but confirmation-only: it only counts
                # when at least one other contrarian or fundamental signal agrees.

                # Volume spike → priority boost (0.0–1.0), not a directional vote
                priority_boost = TradingStrategies.volume_spike_priority(
                    market, median_volume24h, spike_multiplier=VOLUME_SPIKE_MULTIPLIER
                )

                # Collect raw signals before family deduplication
                raw_signals = []

                prob_dir_rec = TradingStrategies.probability_direction_strategy(market)
                if prob_dir_rec:
                    raw_signals.append({'strategy': 'probability_direction',
                                        'recommendation': prob_dir_rec,
                                        'confidence': 0.65,
                                        'family': 'momentum'})
                    counters["raw_fires"]["probability_direction"] += 1

                mean_rev_rec = TradingStrategies.mean_reversion_strategy(market)
                if mean_rev_rec:
                    raw_signals.append({'strategy': 'mean_reversion',
                                        'recommendation': mean_rev_rec,
                                        'confidence': 0.68,
                                        'family': 'contrarian'})
                    counters["raw_fires"]["mean_reversion"] += 1

                bias_rec = TradingStrategies.probability_bias_strategy(market)
                if bias_rec:
                    raw_signals.append({'strategy': 'probability_bias',
                                        'recommendation': bias_rec,
                                        'confidence': 0.70,
                                        'family': 'contrarian'})
                    counters["raw_fires"]["probability_bias"] += 1

                creator_rec = TradingStrategies.creator_disagreement_strategy(market)
                if creator_rec:
                    raw_signals.append({'strategy': 'creator_disagreement',
                                        'recommendation': creator_rec,
                                        'confidence': 0.75,
                                        'family': 'fundamental'})
                    counters["raw_fires"]["creator_disagreement"] += 1

                thin_rec = TradingStrategies.thin_market_strategy(market)
                if thin_rec:
                    counters["raw_fires"]["thin_market"] += 1
                # thin_market is tracked separately — added only as confirmation

                # ── Apply strategy reliability weights ────────────────────────
                # Scale each signal's base confidence by its historical weight.
                # Weights are 0.5–1.2; clamped to [0.0, 1.0] so they can't push
                # a signal above 100% confidence before blending.
                # Fallback chain: by_category[cat][strategy] → global[strategy] → 1.0
                market_category = _infer_market_category(question)
                cat_weights = _by_category_weights.get(market_category, {})
                for sig in raw_signals:
                    strat = sig['strategy']
                    if strat in cat_weights:
                        w = cat_weights[strat].get("weight", _DEFAULT_STRATEGY_WEIGHT)
                    else:
                        w = strategy_weights.get(strat, _DEFAULT_STRATEGY_WEIGHT)
                    sig['confidence'] = round(min(1.0, sig['confidence'] * w), 4)

                # ── Family deduplication ──────────────────────────────────────
                # From each family take the single highest-confidence signal.
                # Multiple momentum or contrarian signals are correlated (they
                # read the same underlying price movement) — averaging them
                # inflates apparent confidence without adding real information.
                active_signals = []
                for family in ('momentum', 'contrarian', 'fundamental'):
                    family_sigs = [s for s in raw_signals if s['family'] == family]
                    if family_sigs:
                        # Take the strongest signal from this family
                        best = max(family_sigs, key=lambda s: s['confidence'])
                        active_signals.append(best)
                        counters["dedup_winners"][family] += 1

                # thin_market: confirmation-only — when it agrees with the selected
                # contrarian/fundamental signal, give that signal a small confidence
                # boost (+0.03) rather than appending an independent vote.
                # Appending would break the family-dedup guarantee (two contrarian
                # signals) AND would reduce confidence by pulling the average down
                # (thin_market's 0.65 is below mean_reversion's 0.68).
                thin_market_fired = False
                if thin_rec:
                    for _sig in active_signals:
                        if _sig['family'] in ('contrarian', 'fundamental') and _sig['recommendation'] == thin_rec:
                            _sig['confidence'] = round(min(1.0, _sig['confidence'] + 0.03), 4)
                            thin_market_fired = True
                            counters["thin_market_confirmations"] += 1
                            break

                if not active_signals:
                    counters["no_active_signals"] += 1
                    continue

                # ── Determine overall recommendation ──────────────────────────
                yes_votes = sum(1 for s in active_signals if s['recommendation'] == 'YES')
                no_votes  = sum(1 for s in active_signals if s['recommendation'] == 'NO')

                if yes_votes > no_votes:
                    overall_rec = 'YES'
                    confidence = sum(s['confidence'] for s in active_signals
                                     if s['recommendation'] == 'YES') / yes_votes
                elif no_votes > yes_votes:
                    overall_rec = 'NO'
                    confidence = sum(s['confidence'] for s in active_signals
                                     if s['recommendation'] == 'NO') / no_votes
                else:
                    counters["tied_votes_dropped"] += 1
                    continue  # tied — no clear edge

                # Check if we already have an OPEN position (closed/resolved don't count)
                existing_position = any(
                    p.get('status') == 'OPEN'
                    for p in self.trader.positions.get(market_id, [])
                )

                recommendation = {
                    'market_id': market_id,
                    'question': question,
                    'category': _infer_market_category(question),
                    'probability': probability,
                    'volume': volume,
                    'volume24h': market.get('volume24Hours') or 0,
                    'liquidity': liquidity,
                    'unique_bettors': market.get('uniqueBettorCount') or 0,
                    'last_bet_time_ms': market.get('lastBetTime'),
                    'recommendation': overall_rec,
                    'confidence': round(confidence, 2),
                    'strategies': [s['strategy'] for s in active_signals] + (['thin_market'] if thin_market_fired else []),
                    'priority_boost': round(priority_boost, 3),
                    'existing_position': existing_position,
                    'analyzed_at': datetime.now().isoformat()
                }

                recommendations.append(recommendation)

            # Sort by confidence (highest first) before AI pass
            recommendations.sort(key=lambda x: x['confidence'], reverse=True)

            # ── News enrichment ─────────────────────────────────────────────
            # Fetch recent headlines for the top _NEWS_CANDIDATES (3) recs.
            # Only runs when NEWS_API_KEY is configured; degrades gracefully
            # (skips silently) on any network/API error.
            #
            # For each candidate:
            #   - news_context: top-3 headline titles attached to rec (passed
            #     to AI prompt for fresh context beyond the model's cutoff)
            #   - direction agreement: +0.05 confidence boost (same logic as
            #     thin_market confirmation) and 'news' appended to strategies
            #   - direction disagreement: ×0.92 confidence penalty
            #
            # Confidence is re-clamped to [0, 1] and re-sorted afterward so
            # news-boosted markets can move up the AI candidate queue.
            news_by_market_id: dict = {}
            if NEWS_API_KEY:
                markets_by_id = {m.get('id'): m for m in markets}
                for rec in recommendations[:_NEWS_CANDIDATES]:
                    mid = rec['market_id']
                    try:
                        headlines = fetch_headlines(mid, rec['question'])
                    except Exception as _news_err:
                        print(f"  News fetch error for {mid}: {_news_err}")
                        headlines = []
                    if not headlines:
                        continue

                    # Store top-3 titles for AI prompt injection
                    rec['news_context'] = [h['title'] for h in headlines[:3]]
                    news_by_market_id[mid] = rec['news_context']

                    mkt = markets_by_id.get(mid, {})
                    news_dir  = TradingStrategies.news_strategy(mkt, headlines)
                    news_conf = TradingStrategies.news_strategy_confidence(headlines)
                    # Scale news impact by recency: fresh (0.80) → full effect,
                    # recent (0.70) → half, old (0.60) → zero.
                    # Note: "news" is intentionally NOT added to rec['strategies'].
                    # It has no learned weight in strategy_weights.json, so adding it
                    # would cause _get_strategy_weight to default to 1.0 and silently
                    # cancel any penalty weights on the other strategies in the list.
                    recency_factor = (news_conf - 0.60) / 0.20  # 0.0 → 1.0
                    if news_dir == rec['recommendation']:
                        boost = round(0.05 * recency_factor, 3)
                        rec['confidence'] = round(min(1.0, rec['confidence'] + boost), 3)
                        print(f"  News [{news_conf:.0%}] {rec['question'][:60]}... → {news_dir} (agrees +{boost})")
                    elif news_dir:
                        penalty = round(1.0 - 0.08 * recency_factor, 3)
                        rec['confidence'] = round(rec['confidence'] * penalty, 3)
                        print(f"  News [{news_conf:.0%}] {rec['question'][:60]}... → {news_dir} (disagrees ×{penalty})")
                    else:
                        print(f"  News: no clear signal for {rec['question'][:60]}...")

                if news_by_market_id:
                    recommendations.sort(key=lambda x: x['confidence'], reverse=True)

            # ── AI metadata defaults ─────────────────────────────────────────
            # Initialize AI fields on EVERY recommendation before the candidate
            # pass runs.  The defaults encode "this rec was never sent to AI",
            # which is the correct state for:
            #   - recs below the top-3 in a normal run
            #   - ALL recs when candidate_markets is empty (e.g. every
            #     above-floor rec is on AI-timeout cooldown)
            #   - ALL recs when the batch_analyze() call raises and ai_results
            #     stays empty
            #
            # Previously these fields were only set inside the `if candidate_markets`
            # block, so a zero-candidate run produced recs without `ai_was_candidate`,
            # which caused save_research() to write schema_version=1, which then
            # halted the trader for the rest of the cycle. Setting defaults up-front
            # guarantees schema_version=2 is written whenever the research pipeline
            # itself runs to completion.
            for rec in recommendations:
                rec['ai_was_candidate']  = False
                rec['ai_returned_skip']  = False
                rec['ai_recommendation'] = None
                rec['ai_confidence']     = 0.0
                rec['ai_reasoning']      = ''
                rec['ai_source']         = None

            # AI candidate selection — 3 markets, ranked by composite score.
            #
            # Composite score = confidence + priority_boost * 0.15
            #   - confidence is the primary signal (stat quality)
            #   - priority_boost (0–1 from volume_spike_priority) adds up to 0.15
            #     to push spiking markets ahead of equally-confident quiet ones
            #   - weight (0.15) ensures volume is a tiebreaker, not a hijacker
            #
            # Only markets above _STAT_BOOST_FLOOR are candidates (AI is most
            # useful when the stat signal is already meaningful). If fewer than
            # _AI_CANDIDATE_COUNT pass the floor, we fill from below-floor by
            # composite score so the AI always has something to evaluate.
            _AI_CANDIDATE_COUNT = 3
            above_floor = [
                r for r in recommendations if r['confidence'] >= _STAT_BOOST_FLOOR
            ]
            below_floor = [
                r for r in recommendations if r['confidence'] < _STAT_BOOST_FLOOR
            ]
            counters["ai_eligible"] = len(above_floor)

            def _composite(r):
                return r['confidence'] + r.get('priority_boost', 0.0) * 0.15

            above_floor.sort(key=_composite, reverse=True)
            below_floor.sort(key=_composite, reverse=True)

            # Load AI timeout cooldown and remove markets that have failed too many times.
            # This prevents the same 2-3 markets from monopolising all AI slots every hour
            # when Ollama consistently times out on them.
            ai_timeout_cooldown = _load_ai_timeout_cooldown()
            n_cooled = sum(
                1 for r in (above_floor + below_floor)
                if _is_in_ai_cooldown(r['market_id'], ai_timeout_cooldown)
            )
            counters["ai_cooled_down"] = n_cooled
            if n_cooled:
                print(f"  AI cooldown: skipping {n_cooled} market(s) with >= {_AI_TIMEOUT_MAX_FAILS} recent timeouts")
                above_floor = [r for r in above_floor if not _is_in_ai_cooldown(r['market_id'], ai_timeout_cooldown)]
                below_floor  = [r for r in below_floor  if not _is_in_ai_cooldown(r['market_id'], ai_timeout_cooldown)]

            recent_top = (above_floor + below_floor)[:_AI_CANDIDATE_COUNT]
            top_candidate_ids = {r['market_id'] for r in recent_top}
            candidate_markets = [m for m in markets if m.get('id') in top_candidate_ids]

            # Guard: if the count doesn't match, the market objects use a different ID field
            # (e.g. 'slug' or 'url') and AI analysis cannot be reliably performed.
            # Rather than silently writing schema_version=2 with all ai_was_candidate=False
            # (which would cause auto_trader.py to trade without AI validation), we raise a
            # clear error so the caller can catch it and write schema_version=1 instead,
            # causing the trader's schema guard to correctly block trading.
            # Note: candidate_markets may be smaller than _AI_CANDIDATE_COUNT if fewer
            # qualifying recommendations exist — that is fine; the guard only catches ID mismatches.
            if len(candidate_markets) != len(recent_top):
                sample_keys = list(markets[0].keys())[:8] if markets else []
                raise ValueError(
                    f"candidate_markets length mismatch: expected {len(recent_top)}, "
                    f"got {len(candidate_markets)}. Market objects may not use 'id' as their "
                    f"primary key. Sample market keys: {sample_keys}. "
                    f"Cannot safely write schema_version=2; aborting AI pass to prevent "
                    f"unvalidated trading."
                )

            if candidate_markets:
                # ── AI analysis cache check (Phase 1.3 of V2) ────────────────
                # Split candidates into "cache hit" (reuse prior AI result) and
                # "fresh" (need new AI call). Cache hits don't burn AI bandwidth
                # and don't count against the per-cycle max_markets limit.
                ai_cache = _load_ai_analysis_cache()
                cache_hit_results = {}
                fresh_candidates = []
                for mkt in candidate_markets:
                    mid = mkt.get('id')
                    cached = ai_cache.get(mid)
                    if cached and _is_cache_valid(cached, mkt):
                        cache_hit_results[mid] = cached['result']
                    else:
                        fresh_candidates.append(mkt)

                if cache_hit_results:
                    print(f"  AI cache: {len(cache_hit_results)} hit(s), {len(fresh_candidates)} need fresh analysis")

                print(f"  Running AI analysis on {len(fresh_candidates)} fresh candidate(s) (skipping {len(cache_hit_results)} cached)...")
                counters["ai_analyzed"] = len(candidate_markets)  # count total analyzed (incl. cache hits)
                counters.setdefault("ai_cache_hits", 0)
                counters["ai_cache_hits"] = len(cache_hit_results)
                ai_results = dict(cache_hit_results)  # start with cache hits

                # Snapshot pre-AI confidence scores so we can restore them if the AI
                # pass fails partway through. Without this, a mid-loop timeout would
                # leave some recs with AI-blended confidences and no ai_was_candidate
                # field, causing schema_version=1 to be written with partially-blended
                # scores that would be traded on in a future cycle with no indication
                # which markets were AI-validated.
                pre_ai_confidence = {r['market_id']: r['confidence'] for r in recommendations}

                try:
                    # delay=2 makes the rate-limit intent explicit at the call site for both
                    # Ollama and any DeepSeek API fallback. Ollama may no-op the delay
                    # internally, but the caller's intent is clear and not reliant on
                    # undocumented internal behaviour.
                    # per_market_timeout=90 prevents a hung Ollama call from stalling the
                    # hourly cron job indefinitely.
                    fresh_ai_results = batch_analyze(
                        fresh_candidates,
                        max_markets=3,
                        delay=2,
                        per_market_timeout=90,
                        news_by_id=news_by_market_id,
                    ) if fresh_candidates else {}
                    ai_results.update(fresh_ai_results)

                    # Write successful fresh results back to cache for future hourly runs.
                    # Skip caching cache-hit results — they're already in the cache.
                    # Skip caching None/failed results — only cache positive outcomes.
                    for mkt in fresh_candidates:
                        mid = mkt.get('id')
                        fresh = fresh_ai_results.get(mid)
                        if fresh is not None:
                            _cache_ai_result(ai_cache, mid, mkt, fresh)
                    _save_ai_analysis_cache(ai_cache)
                    # Warn if any result came from the paid API fallback
                    for r in ai_results.values():
                        if r.get('source') == 'deepseek_api':
                            print("  Warning: AI fallback to DeepSeek API was used — Ollama may be down. Check server.")
                            break
                    # Record per-market failures: candidate got no result at all (not SKIP,
                    # which is a valid response — only missing entries indicate a timeout).
                    # Only count fresh candidates that failed — cache hits never fail here.
                    fresh_ids = {m.get('id') for m in fresh_candidates}
                    missing_ai = [mid for mid in fresh_ids if fresh_ai_results.get(mid) is None]
                    counters["ai_no_result"] += len(missing_ai)
                    if missing_ai:
                        print(f"  AI no-result for {len(missing_ai)} fresh candidate(s) — recording for cooldown")
                        _record_ai_timeout(missing_ai, ai_timeout_cooldown)
                    _save_ai_timeout_cooldown(ai_timeout_cooldown)
                except TimeoutError as e:
                    print(f"  Warning: AI analysis timed out ({e}), continuing without AI scores")
                    # Restore pre-AI confidence scores to prevent partially-blended
                    # confidences from being written to schema_version=1 output.
                    for rec in recommendations:
                        if rec['market_id'] in pre_ai_confidence:
                            rec['confidence'] = pre_ai_confidence[rec['market_id']]
                    _record_ai_timeout(list(top_candidate_ids), ai_timeout_cooldown)
                    _save_ai_timeout_cooldown(ai_timeout_cooldown)
                    ai_results = {}
                except Exception as e:
                    print(f"  Warning: AI analysis failed ({e}), continuing without AI scores")
                    # Restore pre-AI confidence scores to prevent partially-blended
                    # confidences from being written to schema_version=1 output.
                    for rec in recommendations:
                        if rec['market_id'] in pre_ai_confidence:
                            rec['confidence'] = pre_ai_confidence[rec['market_id']]
                    _record_ai_timeout(list(top_candidate_ids), ai_timeout_cooldown)
                    _save_ai_timeout_cooldown(ai_timeout_cooldown)
                    ai_results = {}

                for rec in recommendations:
                    ai = ai_results.get(rec['market_id'])
                    is_ai_candidate = rec['market_id'] in top_candidate_ids

                    # ai_was_candidate: was this market sent to AI at all?
                    # ai_status: explicit state of AI analysis. One of:
                    #   'not_run'    — market wasn't in the top-3 AI candidates
                    #   'no_result'  — AI invoked but timeout / parse error / no response
                    #   'skip'       — AI ran and explicitly said "no edge here"
                    #   'agree'      — AI recommendation matches stat recommendation
                    #   'disagree'   — AI disagrees with stat
                    # ai_returned_skip: DEPRECATED legacy field. Kept for backward
                    # compatibility with auto_trader.py until both sides move to ai_status.
                    rec['ai_was_candidate'] = is_ai_candidate

                    if not is_ai_candidate:
                        # Market was never sent to AI (not in top-3) — use None (not 'SKIP')
                        # to avoid conflating "AI considered and skipped" with "never analyzed".
                        rec['ai_status'] = 'not_run'
                        rec['ai_returned_skip'] = False
                        rec['ai_recommendation'] = None
                        rec['ai_confidence'] = 0.0
                        rec['ai_reasoning'] = ''
                        rec['ai_source'] = None
                        continue

                    # Market was a candidate — check what AI returned.
                    if not ai:
                        # AI was invoked but returned no usable result (timeout,
                        # parse error, etc.). Already counted in ai_no_result above.
                        # This is NOT a skip — we had no signal either way.
                        # Previous versions wrote ai_recommendation='SKIP' here,
                        # causing the trader to veto. Now we mark ai_status='no_result'
                        # and leave ai_recommendation=None, so the trader falls back
                        # to stat-only trading for this market.
                        rec['ai_status'] = 'no_result'
                        rec['ai_returned_skip'] = False  # was True previously — caused veto
                        rec['ai_recommendation'] = None
                        rec['ai_confidence'] = 0.0
                        rec['ai_reasoning'] = ''
                        rec['ai_source'] = None
                        # No counter increment here — ai_no_result already counted it
                        continue

                    if ai['recommendation'] == 'SKIP':
                        # AI explicitly returned SKIP — it analyzed the market and
                        # decided there's no edge. This IS a real signal — veto the trade.
                        rec['ai_status'] = 'skip'
                        rec['ai_returned_skip'] = True  # legacy flag kept for trader compat
                        rec['ai_recommendation'] = 'SKIP'
                        rec['ai_confidence'] = 0.0
                        rec['ai_reasoning'] = ''
                        rec['ai_source'] = None
                        counters["ai_skip"] += 1
                        continue

                    rec['ai_recommendation'] = ai['recommendation']
                    rec['ai_confidence'] = ai['confidence']
                    # Sanitize: guarantee ASCII-safe output for JSON/log/Telegram display.
                    # Encode to ASCII replacing any non-ASCII bytes (including Unicode
                    # printable-but-dangerous chars like U+202E RLO or zero-width joiners),
                    # then decode back to str and cap at 300 characters.
                    ai_reasoning = ai.get('reasoning', '')[:300].encode('ascii', errors='replace').decode('ascii')
                    rec['ai_reasoning'] = ai_reasoning
                    rec['ai_source'] = ai['source']
                    rec['ai_estimated_probability'] = ai['estimated_true_probability']

                    # Blend statistical + AI confidence
                    stat_conf = rec['confidence']
                    if ai['recommendation'] == rec['recommendation']:
                        counters["ai_agree"] += 1
                        rec['ai_status'] = 'agree'
                        rec['ai_returned_skip'] = False
                        # Only blend upward on agreement: a low-confidence AI agreement
                        # must not reduce a strong statistical signal. Take the maximum of
                        # stat_conf and the blended value so the stat signal is always the
                        # floor, then apply the upper cap.
                        blended = max(stat_conf, round(0.4 * stat_conf + 0.6 * ai['confidence'], 3))
                        if stat_conf < _STAT_BOOST_FLOOR:
                            # Stat too weak — cap below trading threshold
                            blended = min(blended, _WEAK_STAT_CAP)
                        else:
                            # Cap is relative to stat_conf: at most stat_conf + _AI_BOOST_MARGIN.
                            # e.g. stat_conf=0.65 → cap=0.75; stat_conf=0.9 → cap=1.0.
                            blended = min(blended, stat_conf + _AI_BOOST_MARGIN)
                        rec['confidence'] = blended
                        rec['strategies'] = rec['strategies'] + ['ai_analysis']
                    elif ai['recommendation'] in ('YES', 'NO'):
                        counters["ai_disagree"] += 1
                        rec['ai_status'] = 'disagree'
                        rec['ai_returned_skip'] = False
                        # AI disagrees — scale the confidence multiplier by AI confidence.
                        # Formula: confidence_multiplier = 0.4 + (1 - ai_conf) * 0.4
                        #   ai_conf=1.0 → confidence_multiplier=0.40 (AI certain → hardest reduction, keeps 40% of stat score)
                        #   ai_conf=0.0 → confidence_multiplier=0.80 (AI uncertain → softest reduction, keeps 80% of stat score)
                        # Clamp ai_conf to [0.0, 1.0] to guard against malformed AI responses.
                        ai_conf = max(0.0, min(1.0, ai['confidence']))
                        confidence_multiplier = 0.4 + (1 - ai_conf) * 0.4
                        rec['confidence'] = max(0.0, round(stat_conf * confidence_multiplier, 3))
                    else:
                        # AI returned something unexpected — treat as no_result
                        rec['ai_status'] = 'no_result'
                        rec['ai_returned_skip'] = False

            # Compute two EV figures for every recommendation now that
            # ai_estimated_probability is set:
            #
            #   estimated_ev_ref  — at a fixed $25 reference stake. Used ONLY for
            #                       cross-market ranking. Stake-independent ordering
            #                       is useful because it lets us compare raw edge
            #                       quality without having to know the current
            #                       position sizing throttle.
            #
            #   estimated_ev_exec — at the actual executable stake under the current
            #                       MAX_BET_AMOUNT throttle. This is what swap
            #                       decisions MUST compare against real unrealised
            #                       losses, because a "+$4 EV" swap computed at $25
            #                       reference is actually "+$0.80 EV" at the live
            #                       $5 cap.
            #
            # estimated_ev remains as an alias for estimated_ev_ref so older
            # consumers (backtests, reports) don't break.
            _EV_REF = 25.0
            _EV_EXEC = float(MAX_BET_AMOUNT)
            for rec in recommendations:
                ai_p = rec.get('ai_estimated_probability')
                direction = rec.get('recommendation')
                mkt_p = max(0.01, min(0.99, rec.get('probability', 0.5)))

                if ai_p is not None and direction in ('YES', 'NO'):
                    if direction == 'YES':
                        win_p = float(ai_p)
                        payout_per_dollar = 1.0 / mkt_p
                    else:
                        win_p = 1.0 - float(ai_p)
                        payout_per_dollar = 1.0 / (1.0 - mkt_p)

                    # EV(stake) = stake * (win_p * payout_per_dollar - 1)
                    ev_per_dollar = win_p * payout_per_dollar - 1.0
                    rec['estimated_ev_ref']  = round(_EV_REF  * ev_per_dollar, 4)
                    rec['estimated_ev_exec'] = round(_EV_EXEC * ev_per_dollar, 4)
                    rec['estimated_ev']      = rec['estimated_ev_ref']  # legacy alias
                else:
                    rec['estimated_ev_ref']  = None
                    rec['estimated_ev_exec'] = None
                    rec['estimated_ev']      = None

            # Re-sort after AI pass
            recommendations.sort(key=lambda x: x['confidence'], reverse=True)

            print(f"  Found {len(recommendations)} trading opportunities")

            # Op3: finalize + emit counters (best-effort — never abort the run)
            try:
                _finalize_research_counters(counters, recommendations)
                _print_research_counters(counters)
                _append_research_counters(counters)
            except Exception as ctr_err:
                print(f"  Warning: research counter emit failed ({ctr_err})")

            return recommendations

        except Exception as e:
            print(f"  Error analyzing markets: {e}")
            # Emit whatever counters we managed to populate before the failure —
            # the mix of "skipped_*" and "raw_fires" is diagnostic for the
            # exception itself.
            try:
                _finalize_research_counters(counters, [])
                _print_research_counters(counters)
                _append_research_counters(counters)
            except Exception:
                pass
            return []

    def save_research(self, recommendations: List[Dict]):
        """Save research results to file"""
        # Determine schema version based on whether AI candidate fields are present.
        # If any recommendation is missing 'ai_was_candidate' it means the AI pass was
        # skipped or aborted (e.g. due to a candidate_markets mismatch), so we must write
        # schema_version=1 to ensure auto_trader.py's schema guard blocks trading rather
        # than proceeding without AI validation.
        has_ai_fields = all(
            'ai_was_candidate' in rec
            for rec in recommendations
        ) if recommendations else False
        schema_version = 2 if has_ai_fields else 1

        research_data = {
            'schema_version': schema_version,  # v2 adds ai_was_candidate, ai_returned_skip, ai_recommendation fields
            'timestamp': datetime.now().isoformat(),
            'recommendations': recommendations,
            'total_markets_analyzed': len(recommendations)
        }

        # Load existing research history
        history = []
        if os.path.exists(self.research_file):
            try:
                with open(self.research_file, 'r') as f:
                    existing = json.load(f)
                    history = existing.get('history', [])[-23:]  # Keep last 24 hours
            except:
                history = []

        # Add new research
        history.append(research_data)

        # Save
        data = {
            'latest': research_data,
            'history': history
        }

        with open(self.research_file, 'w') as f:
            json.dump(data, f, indent=2)

        print(f"  Research saved to {self.research_file} (schema_version={schema_version})")

    def run_hourly_research(self):
        """Main research loop"""
        print(f"\n{'='*60}")
        print(f"MARKET RESEARCH - {datetime.now()}")
        print(f"{'='*60}")

        # Analyze markets
        recommendations = self.analyze_markets(limit=100)

        # Save results
        if recommendations:
            self.save_research(recommendations)

            # Print top 5 recommendations
            print(f"\nTop 5 Trading Opportunities:")
            for i, rec in enumerate(recommendations[:5], 1):
                pos_flag = "📌" if rec['existing_position'] else "🆕"
                print(f"{i}. {pos_flag} {rec['question']}")
                print(f"   Probability: {rec['probability']*100:.1f}% | Rec: {rec['recommendation']} | Confidence: {rec['confidence']*100:.0f}%")
                print(f"   Strategies: {', '.join(rec['strategies'])}")
        else:
            print("No trading opportunities found this hour")

        print(f"\nResearch completed at {datetime.now()}")
        return recommendations

def main():
    """Main function"""
    researcher = MarketResearcher()

    # Research always runs regardless of position count.
    # Previously it skipped at max positions, but this caused a 1-hour lag:
    # resolve_positions.py frees slots at :10 while research runs at :00, so
    # research would skip, then the trader at :20 had stale recommendations,
    # and position_swap_checker at :40 had no estimated_ev to rank swaps with.
    recommendations = researcher.run_hourly_research()

    # Trigger swap check immediately after research completes.
    # This replaces the standalone :40 cron job — swaps are only evaluated
    # when fresh research exists, not on a blind timer. run_swap_check() is
    # a no-op when positions are not full or no qualifying pairs are found.
    print(f"\n--- Swap check (triggered by research) ---")
    run_swap_check()

    # Return number of recommendations for cron job monitoring
    return len(recommendations)

if __name__ == "__main__":
    try:
        num_recs = main()
        sys.exit(0)
    except Exception as e:
        print(f"Fatal error: {e}")
        sys.exit(1)
