"""
Trading strategies for Manifold Markets.

Each strategy is a static method on TradingStrategies that takes a market dict
and returns "YES", "NO", or None.  auto_research.py calls these using family-aware
aggregation — correlated signals in the same family cannot stack.

Signal families
---------------
  momentum    — price-following signals (probability_direction)
  contrarian  — price-fading signals (mean_reversion, thin_market, probability_bias)
  fundamental — independent signals (creator_disagreement)
  filter      — priority scoring only, no directional vote (volume_spike_priority)

auto_research.py takes at most ONE signal from momentum and ONE from contrarian,
then adds the fundamental if present.  thin_market is contrarian but confirmation-
only: it only counts when at least one other contrarian or fundamental signal agrees.

Confidence scores (used by auto_research.py after family deduplication):
  probability_direction   0.65   momentum
  mean_reversion          0.68   contrarian
  probability_bias        0.70   contrarian (category-conditional)
  creator_disagreement    0.75   fundamental
  thin_market             0.65   contrarian (confirmation-only)
  volume_spike_priority   —      filter (returns 0.0–1.0 priority boost, not YES/NO)
"""

import json
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Category inference ────────────────────────────────────────────────────────
# Single source of truth for category → keyword mapping.
# scripts/harvest_resolved.py imports _infer_market_category from here so that
# calibration data category labels always match live-trading labels.

_CATEGORY_KEYWORDS: Dict[str, List[str]] = {
    'crypto':    ['bitcoin', 'btc', 'ethereum', 'crypto', 'blockchain',
                  'defi', 'nft', 'solana', 'binance', 'coinbase', 'stablecoin',
                  'doge', 'dogecoin', 'xrp', 'ripple'],
    'politics':  ['trump', 'biden', 'election', 'congress', 'senate', 'president',
                  'democrat', 'republican', 'vote', 'policy', 'legislation',
                  'supreme court', 'governor', 'parliament',
                  'war', 'ceasefire', 'military', 'nato', 'ukraine', 'russia',
                  'gaza', 'israel', 'iran', 'harris', 'political', 'sanction',
                  'tariff', 'geopolit'],
    'ai_tech':   [' ai ', 'gpt', 'llm', 'openai', 'anthropic', 'claude', 'gemini',
                  'machine learning', 'artificial intelligence', 'neural', 'deepmind',
                  'chatgpt', 'language model', 'deepseek', 'chatbot', 'mistral',
                  'grok', 'xai'],
    'sports':    ['nba', 'nfl', 'mlb', 'nhl', 'fifa', 'world cup', 'olympics',
                  'championship', 'tennis', 'golf', 'soccer', 'football',
                  'basketball', 'baseball', 'premier league',
                  # Space-bounded so we match whole words only:
                  # ' win ' avoids Windows/winning; ' game ' avoids GameStop/gaming;
                  # ' team ' avoids steam; ' match '/' score '/' league '/' player '
                  # avoid mismatches in economics/science questions.
                  # _infer_market_category pads q with spaces so these also match
                  # at the start/end of a sentence.
                  ' win ', ' team ', ' match ', ' game ', ' score ', ' league ', ' player ',
                  'tournament', 'bundesliga', 'la liga', 'serie a',
                  'champions league', 'europa league', 'ufc', 'mma', 'boxing',
                  'formula 1', 'f1', 'wimbledon', 'super bowl', 'world series'],
    'economics': ['gdp', 'inflation', 'federal reserve', 'fed rate', 'interest rate',
                  'recession', 'stock market', 'nasdaq', 'sp500', 's&p', 'cpi',
                  'unemployment', 'treasury', 'stock', 'economy', 'dow', 'dollar',
                  'euro', 'trade deficit', 'budget deficit', 'debt ceiling'],
    'science':   ['nasa', 'spacex', 'climate', 'vaccine', 'fda', 'cdc', 'pandemic',
                  'cancer', 'physics', 'biology', 'crispr', 'fusion',
                  'earthquake', 'hurricane', 'temperature', 'science', 'research',
                  'drug approval', 'clinical trial'],
}

# Category-level bias loaded from data/calibration_table.json at import time.
# Falls back to the values recorded at the last manual calibration run so the
# strategy still works on a fresh checkout before calibration data is generated.
_CATEGORY_BIAS_FALLBACK: Dict[str, float] = {
    'other':     0.0439,
    'sports':    0.0348,
    'economics': 0.0151,
    'politics':  0.0623,
    'ai_tech':   0.0019,
    'crypto':    0.0534,
    'science':   0.0543,
}


def _load_category_bias() -> Dict[str, float]:
    """Return category→bias mapping from calibration_table.json, or fallback dict."""
    try:
        _cal = Path(__file__).resolve().parent.parent / "data" / "calibration_table.json"
        data = json.loads(_cal.read_text())
        loaded = {row['category']: row['bias'] for row in data.get('by_category', [])}
        if loaded:
            return loaded
    except Exception:
        pass
    return _CATEGORY_BIAS_FALLBACK.copy()


_CATEGORY_BIAS: Dict[str, float] = _load_category_bias()

# Minimum category-level bias required for probability_bias to fire.
# ai_tech (0.19pp) and economics (1.51pp) are below this and will be skipped.
_MIN_BIAS_TO_TRADE = 0.04


def _infer_market_category(question: str) -> str:
    """Return the category inferred from market question text.

    The search string is padded with a leading and trailing space so that
    space-bounded keywords (e.g. ' win ', ' game ') also match when the word
    appears at the very start or end of the question.
    """
    q = ' ' + question.lower() + ' '
    for category, keywords in _CATEGORY_KEYWORDS.items():
        if any(kw in q for kw in keywords):
            return category
    return 'other'


class TradingStrategies:
    """Collection of trading strategies."""

    # Signal family classification — used by auto_research.py to prevent
    # correlated signals from stacking as if they were independent evidence.
    SIGNAL_FAMILIES: Dict[str, str] = {
        'probability_direction': 'momentum',
        'mean_reversion':        'contrarian',
        'thin_market':           'contrarian',   # confirmation-only within family
        'probability_bias':      'contrarian',
        'creator_disagreement':  'fundamental',
        'news':                  'fundamental',  # post-loop enrichment; see news_strategy()
        'volume_spike_priority': 'filter',       # priority boost, not a directional vote
    }

    # ------------------------------------------------------------------ #
    # Pre-filter
    # ------------------------------------------------------------------ #

    @staticmethod
    def is_stale_market(market: Dict, max_age_hours: int = 72) -> bool:
        """
        Returns True if a market should be skipped due to inactivity.

        A market is stale when:
          - Last bet was > max_age_hours ago, AND
          - 24h volume is under 10 mana

        We skip the staleness check if lastBetTime is absent (can't tell).
        """
        last_bet_ms = market.get('lastBetTime')
        volume24 = market.get('volume24Hours') or 0

        if last_bet_ms is None:
            return False  # no data — give it the benefit of the doubt

        age_hours = (time.time() * 1000 - last_bet_ms) / 3_600_000
        return age_hours > max_age_hours and volume24 < 10

    # ------------------------------------------------------------------ #
    # Core strategies
    # ------------------------------------------------------------------ #

    @staticmethod
    def probability_direction_strategy(market: Dict) -> Optional[str]:
        """
        Follow the direction of the current probability — but only when the market
        shows recent, meaningful activity.

        "The price moved" is not an edge by itself.  We want to follow price when
        active participants are reinforcing a direction, not just a stale number
        sitting in the market.  The two guards below enforce this:

          1. lastBetTime ≤ 48h  — someone acted on this recently
          2. volume24Hours ≥ 5  — not a ghost market with one old bet

        Rules:
          - Skip the 45-55% coinflip zone (no directional edge).
          - Skip if last bet was > 48 hours ago.
          - Skip if 24h volume < 5 mana (insufficient recent activity).
          - Otherwise follow the crowd direction.

        Family: momentum.  Confidence: 0.65.
        """
        probability = market.get('probability', 0.5)

        # Coinflip zone — crowd is saying "I don't know"
        if 0.45 <= probability <= 0.55:
            return None

        # Staleness guard — only trust a directional signal if someone acted on it recently
        last_bet_ms = market.get('lastBetTime')
        if last_bet_ms is not None:
            hours_since = (time.time() * 1000 - last_bet_ms) / 3_600_000
            if hours_since > 48:
                return None

        # Volume guard — a direction without recent activity is noise, not signal
        volume24 = market.get('volume24Hours') or 0
        if volume24 < 5:
            return None

        if probability > 0.55:
            return "YES"
        elif probability < 0.45:
            return "NO"

        return None

    @staticmethod
    def mean_reversion_strategy(market: Dict) -> Optional[str]:
        """
        Bet against extreme probabilities — but only in shallow, long-dated markets.

        A market at 85% with 400 bettors is probably right (deep consensus).
        A market at 85% with 3 bettors may be mispriced (thin crowd).
        A market at 85% closing tomorrow is almost certainly correct (converged).

        Rules:
          - Fires at probability > 0.85 (bet NO) or < 0.15 (bet YES).
            Raised from 0.80/0.20 — markets in the 80-85% range are often genuinely
            likely; fading them requires sharper conviction.
          - Skipped if uniqueBettorCount > 100 (deep crowd, don't fight consensus).
          - Skipped if market closes within 7 days (near-close extremes are usually
            the crowd converging on correct information, not overconfidence).

        Family: contrarian.  Confidence: 0.68.
        """
        probability = market.get('probability', 0.5)
        bettor_count = market.get('uniqueBettorCount') or 0

        is_extreme_high = probability > 0.85
        is_extreme_low  = probability < 0.15

        if not (is_extreme_high or is_extreme_low):
            return None

        if bettor_count > 100:
            return None  # too many bettors — don't fight genuine consensus

        # Near-close markets converge to truth; mean reversion is wrong-footed there
        close_time_ms = market.get('closeTime')
        if close_time_ms:
            days_to_close = (close_time_ms - time.time() * 1000) / (86_400_000)
            if days_to_close < 7:
                return None

        return "NO" if is_extreme_high else "YES"

    @staticmethod
    def volume_spike_priority(market: Dict, avg_volume: float, spike_multiplier: float = 2.0) -> float:
        """
        Returns a candidate priority boost (0.0–1.0) when a market shows unusual
        recent trading activity.  Does NOT produce a directional vote.

        Volume tells you *where to look*, not *which side to take*.  A spike means
        new information arrived and the market is live; the direction of that
        information is what the other strategies (or AI) determine.

        Demoted from a directional strategy to a filter because "high volume,
        follow the current price" is usually not an edge — the price has likely
        already absorbed the new information by the time we observe the spike.

        Priority scale:
          2× median → 0.25 boost   (mild spike — market is active)
          3× median → 0.50 boost   (clear spike)
          4× median → 0.75 boost   (strong spike)
          5×+ median → 1.0 boost   (very high activity)

        Family: filter.
        """
        volume24 = market.get('volume24Hours') or 0
        if avg_volume <= 0 or volume24 <= avg_volume * spike_multiplier:
            return 0.0
        ratio = volume24 / avg_volume
        # Linear scale from spike_multiplier (→0) to spike_multiplier*3 (→1.0)
        return min(1.0, (ratio - spike_multiplier) / (spike_multiplier * 2))

    @staticmethod
    def creator_disagreement_strategy(market: Dict) -> Optional[str]:
        """
        Bet toward the creator's resolution probability when it differs from the crowd.

        The creator of a market often has inside information — especially on niche
        topics they wrote the question about.  If the crowd says 55% but the creator
        thinks 80%, that 25pp gap is worth trading.

        Threshold: 15pp gap required to act.

        Confidence: 0.75 (strongest signal — creator has asymmetric information).

        NOTE: The Manifold API field `resolutionProbability` is only present on
        some market types and is frequently absent or null on open markets.  If
        this strategy never fires in practice, verify that the field is actually
        being returned by logging `creator_hits` in auto_research.py:

            creator_hits = sum(1 for m in markets if m.get('resolutionProbability') is not None)
            print(f"[creator_disagreement] resolutionProbability present in {creator_hits}/{len(markets)} markets")

        If creator_hits is consistently 0, the field name may differ in the API
        response (e.g. nested under a sub-object) and this strategy will never
        fire, making its 0.75 confidence weight misleading.
        """
        probability = market.get('probability', 0.5)
        resolution_prob = market.get('resolutionProbability')

        if resolution_prob is None:
            logger.debug(
                "creator_disagreement_strategy: resolutionProbability absent for market %s — strategy skipped. "
                "If this is always the case, verify the API field name is correct.",
                market.get('id', '<unknown>'),
            )
            return None

        gap = resolution_prob - probability
        if abs(gap) < 0.15:
            return None

        # Bet toward creator's estimate
        return "YES" if gap > 0 else "NO"

    @staticmethod
    def log_creator_disagreement_field_presence(markets: List[Dict]) -> None:
        """
        Diagnostic helper: count how many markets in a batch have the
        `resolutionProbability` field populated.

        Call this from auto_research.py after fetching the market batch so you
        can verify the field is actually present before relying on the 0.75
        confidence weight assigned to creator_disagreement_strategy.

        Example usage in auto_research.py::

            TradingStrategies.log_creator_disagreement_field_presence(markets)

        This prints and logs a line such as::

            [creator_disagreement] resolutionProbability present in 3/200 markets
        """
        total = len(markets)
        creator_hits = sum(
            1 for m in markets if m.get('resolutionProbability') is not None
        )
        message = (
            f"[creator_disagreement] resolutionProbability present in "
            f"{creator_hits}/{total} markets"
        )
        print(message)
        logger.info(message)
        if creator_hits == 0:
            warning = (
                "[creator_disagreement] WARNING: resolutionProbability was not found "
                "in any market in this batch. The field may be absent, null, or named "
                "differently in the API response. The 0.75 confidence weight for "
                "creator_disagreement_strategy is effectively wasted until this is resolved."
            )
            print(warning)
            logger.warning(warning)

    @staticmethod
    def thin_market_strategy(market: Dict) -> Optional[str]:
        """
        Bet toward the underrepresented (thin) side of the AMM liquidity pool.

        How Manifold's CFMM works
        -------------------------
        Manifold uses a constant-product AMM with a YES pool (Y) and a NO pool (N).
        The invariant is:

            Y × N = k   (constant)

        The implied probability of YES is:

            P(YES) = N / (Y + N)

        So a *large* NO pool relative to the YES pool means a *high* implied
        probability of YES — and conversely, a *large* YES pool means a *low*
        implied probability of YES.

        Worked example
        --------------
        Suppose Y = 10, N = 90  →  total = 100
          P(YES) = 90 / 100 = 0.90   (market says YES is very likely)
          YES pool share = 10 / 100  = 0.10  → thin YES side

        A thin YES pool does NOT mean the market is underestimating YES.
        It means YES is *already priced very high* (90%).  Betting YES here
        follows momentum and pushes the price even higher.

        Correct mapping
        ---------------
        The intention is to detect *underpriced* outcomes and bet toward them:

          • Thin YES pool (YES share < threshold)
              → YES pool is small → NO pool is large → P(YES) is HIGH
              → YES is already expensive; the *underpriced* side is NO
              → Bet NO

          • Thin NO pool (NO share < threshold)
              → NO pool is small → YES pool is large → P(YES) is LOW
              → NO is already expensive; the *underpriced* side is YES
              → Bet YES

        Imbalance threshold: 0.70 (thin side holds < 15% of total pool,
        since (1 − 0.70) / 2 = 0.15).

        Confidence: 0.65.
        """
        pool = market.get('pool')
        if not isinstance(pool, dict):
            return None

        yes_pool = pool.get('YES') or 0
        no_pool = pool.get('NO') or 0
        total = yes_pool + no_pool
        if total <= 0:
            return None

        imbalance = abs(yes_pool - no_pool) / total
        if imbalance < 0.70:
            return None

        # Thin YES pool → P(YES) is high → YES is overpriced → bet NO.
        # Thin NO  pool → P(YES) is low  → NO  is overpriced → bet YES.
        return "NO" if yes_pool < no_pool else "YES"

    @staticmethod
    def probability_bias_strategy(market: Dict) -> Optional[str]:
        """
        Exploit systematic crowd overconfidence — but only in categories where
        the bias is empirically reliable.

        Calibration findings (data/calibration_table.json):
          60–70% bucket: crowd says ~65%, actual YES rate is ~47% → bias +18pp
          30–40% bucket: crowd says ~35%, actual YES rate is ~24% → bias +11pp

        However, the bias is NOT uniform across market categories.  By category:
          ai_tech:   +0.19pp — crowd is near-perfectly calibrated → SKIP
          economics: +1.51pp — well-calibrated → SKIP
          sports:    +3.48pp — mild bias, below threshold → SKIP
          politics:  +6.23pp — meaningful bias → FIRE
          crypto:    +5.34pp — meaningful bias → FIRE
          other:     +4.39pp — meaningful bias → FIRE
          science:   +5.43pp — meaningful bias → FIRE

        Applying a universal NO in the 60-70% range on an AI/tech or economics market
        is a mistake: the crowd there tends to be correct.  This strategy now checks
        category first and skips markets where the data says the crowd is reliable.

        Threshold: _MIN_BIAS_TO_TRADE = 4pp.  Categories below this are skipped.

        Family: contrarian.  Confidence: 0.70.
        """
        probability = market.get('probability', 0.5)

        # Check price bucket first — only the two strongly biased ranges
        if not (0.60 <= probability < 0.70 or 0.30 <= probability < 0.40):
            return None

        # Check category — skip well-calibrated topics
        category = _infer_market_category(market.get('question', ''))
        cat_bias = _CATEGORY_BIAS.get(category, _CATEGORY_BIAS['other'])
        if cat_bias < _MIN_BIAS_TO_TRADE:
            logger.debug(
                "probability_bias: skipping %s market (category=%s, bias=%.4f < threshold %.4f)",
                market.get('id', '?'), category, cat_bias, _MIN_BIAS_TO_TRADE,
            )
            return None

        # Both biased buckets show crowd overestimates YES → bet NO
        return "NO"

    # ------------------------------------------------------------------ #
    # News strategy (post-loop enrichment — called from auto_research.py
    # on the top-N candidates after the main per-market loop)
    # ------------------------------------------------------------------ #

    # Words that suggest the subject of a headline is succeeding / being confirmed.
    _NEWS_YES_WORDS: frozenset = frozenset({
        'wins', 'win', 'won', 'confirms', 'confirmed', 'confirms',
        'approves', 'approved', 'approval', 'signs', 'signed',
        'passes', 'passed', 'launches', 'launched', 'announces',
        'achieves', 'achieved', 'beats', 'beat', 'surpasses',
        'advances', 'advance', 'record', 'breakthrough', 'success',
        'succeeds', 'expands', 'rises', 'rising',
    })

    # Words that suggest the subject of a headline is failing / being blocked.
    _NEWS_NO_WORDS: frozenset = frozenset({
        'fails', 'failed', 'failure', 'cancels', 'canceled', 'cancelled',
        'delays', 'delayed', 'denies', 'denied', 'rejects', 'rejected',
        'rejection', 'drops', 'dropped', 'falls', 'fell', 'blocks',
        'blocked', 'ban', 'bans', 'banned', 'crisis', 'collapses',
        'collapsed', 'cut', 'cuts', 'declines', 'decline', 'loses',
        'lost', 'loss', 'reversed', 'abandons', 'abandoned',
    })

    @staticmethod
    def news_strategy(market: Dict, headlines: list) -> Optional[str]:
        """
        Fundamental signal based on recent news headline sentiment.

        Called by auto_research.py on the top-N candidates AFTER the main
        per-market strategy loop (not during it — fetching news for all 100
        markets would blow the 100 req/day free-tier limit).

        Direction heuristic:
          - For each headline title, count YES_WORDS vs NO_WORDS hits.
          - A headline "leans YES" when yes_hits > no_hits, and vice versa.
          - If yes-leaning headlines outnumber no-leaning headlines: signal YES.
          - If no-leaning headlines outnumber yes-leaning:             signal NO.
          - If tied or neither:                                        return None.

        This is intentionally simple — it catches clear cases (confirmed approvals,
        obvious failures) without trying to understand market-question context.
        Ambiguous or mixed coverage returns None and lets other signals decide.

        Family: fundamental (competes with creator_disagreement for the one
        fundamental slot in auto_research.py's family-dedup; only applied
        post-loop as a confidence modifier rather than via the dedup path).

        Confidence: 0.80 / 0.70 / 0.60 based on recency — see news_strategy_confidence().
        """
        if not headlines:
            return None

        yes_lean = 0
        no_lean = 0
        for h in headlines:
            words = set(h.get('title', '').lower().split())
            yes_hits = len(words & TradingStrategies._NEWS_YES_WORDS)
            no_hits  = len(words & TradingStrategies._NEWS_NO_WORDS)
            if yes_hits > no_hits:
                yes_lean += 1
            elif no_hits > yes_hits:
                no_lean += 1

        if yes_lean > no_lean:
            return 'YES'
        elif no_lean > yes_lean:
            return 'NO'
        return None

    @staticmethod
    def news_strategy_confidence(headlines: list) -> float:
        """
        Return confidence (0.60–0.80) based on recency of the freshest headline.

        Scale:
          < 24 h → 0.80  (breaking news — highest conviction)
          < 72 h → 0.70  (recent news — moderate conviction)
          else   → 0.60  (week-old news — minimum viable conviction)

        Returns 0.60 when headlines is empty (safe fallback).
        """
        if not headlines:
            return 0.60
        min_age = min(h.get('age_hours', float('inf')) for h in headlines)
        if min_age < 24:
            return 0.80
        elif min_age < 72:
            return 0.70
        return 0.60

    # ------------------------------------------------------------------ #
    # Utilities
    # ------------------------------------------------------------------ #

    @staticmethod
    def calculate_kelly_criterion(probability: float, payout_ratio: float) -> float:
        """
        Calculate Kelly Criterion bet size fraction.

        Args:
            probability:  Your estimated probability of winning (0.0–1.0).
            payout_ratio: Net odds — profit per unit staked if you win.
                          e.g. betting YES at market prob 0.30 → net_odds = 0.70/0.30 = 2.33
                          e.g. betting YES at market prob 0.70 → net_odds = 0.30/0.70 = 0.43

        Returns:
            Fraction of bankroll to bet (0.0–0.25).  Returns 0.0 when there is no positive edge.

        Formula: f* = (p × b − q) / b   where b = net odds, p = win prob, q = lose prob.

        Note: net odds can be less than 1 (when the favourite side is below 50%).  This is valid —
        the `max(0, ...)` clamp handles the no-edge case, so no early return is needed.
        """
        if payout_ratio <= 0:
            return 0.0

        q = 1.0 - probability
        kelly = (probability * payout_ratio - q) / payout_ratio
        return max(0.0, min(kelly, 0.25))


def analyze_market_for_trading(market: Dict) -> Dict:
    """
    Convenience wrapper used by the interactive CLI and scripts.
    auto_research.py calls TradingStrategies directly.
    """
    analysis = {
        'market_id': market.get('id'),
        'question': market.get('question'),
        'current_probability': market.get('probability'),
        'volume_24h': market.get('volume24Hours', 0),
        'liquidity': market.get('totalLiquidity', market.get('liquidity', 0)),
        'is_resolved': market.get('isResolved', False),
        'signals': {},
        'recommended_action': None,
        'confidence': 0,
    }

    if analysis['is_resolved']:
        return analysis

    signals = {}

    rec = TradingStrategies.probability_direction_strategy(market)
    if rec:
        signals['probability_direction'] = rec

    rec = TradingStrategies.mean_reversion_strategy(market)
    if rec:
        signals['mean_reversion'] = rec

    # volume_spike_priority needs the batch median — pass 0 here so it never fires solo
    TradingStrategies.volume_spike_priority(market, avg_volume=0)

    rec = TradingStrategies.creator_disagreement_strategy(market)
    if rec:
        signals['creator_disagreement'] = rec

    rec = TradingStrategies.thin_market_strategy(market)
    if rec:
        signals['thin_market'] = rec

    analysis['signals'] = signals

    if signals:
        yes_votes = sum(1 for s in signals.values() if s == 'YES')
        no_votes = sum(1 for s in signals.values() if s == 'NO')

        if yes_votes > no_votes:
            analysis['recommended_action'] = 'YES'
            analysis['confidence'] = yes_votes / len(signals)
        elif no_votes > yes_votes:
            analysis['recommended_action'] = 'NO'
            analysis['confidence'] = no_votes / len(signals)

    return analysis
