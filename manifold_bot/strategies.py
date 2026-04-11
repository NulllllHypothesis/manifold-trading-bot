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

# Category v2 — expanded taxonomy to stop `other` from swallowing 70% of markets.
# Insertion order = match order. First matching category wins. Specific topics
# (crypto, ai_tech) come first so "Will OpenAI IPO?" lands in ai_tech, not business.
# Business comes before economics so "Will Apple stock split?" lands in business
# (apple wins) while "Will S&P500 hit 6000?" still lands in economics (no company match).
_CATEGORY_KEYWORDS: Dict[str, List[str]] = {
    'crypto':        ['bitcoin', 'btc', 'ethereum', 'crypto', 'blockchain',
                      'defi', 'nft', 'solana', 'binance', 'coinbase', 'stablecoin',
                      'doge', 'dogecoin', 'xrp', 'ripple'],
    'ai_tech':       [' ai ', 'gpt', 'llm', 'openai', 'anthropic', 'claude', 'gemini',
                      'machine learning', 'artificial intelligence', 'neural', 'deepmind',
                      'chatgpt', 'language model', 'deepseek', 'chatbot', 'mistral',
                      'grok', 'xai'],
    'gaming':        [# Video game platforms, titles, releases, esports.
                      # Space-bounded terms avoid substring false positives
                      # (' steam ' won't match 'stream', ' switch ' won't match 'switches').
                      ' xbox ', ' playstation ', ' ps5 ', ' ps6 ',
                      ' nintendo ', ' switch ', ' steam ',
                      'video game', 'videogame', 'e-sport', 'esport', 'twitch',
                      'minecraft', 'fortnite', 'roblox', 'zelda', 'mario',
                      'pokemon', 'pokémon', 'call of duty', 'warcraft',
                      'league of legends', 'valorant', 'counter-strike',
                      ' dota ', ' dota2', 'gta ', ' gta6', 'starfield', 'elden ring',
                      'game release', 'game launch', 'gaming', 'speedrun'],
    'entertainment': [# Movies, TV, music, celebrities, awards.
                      'movie', 'film release', 'cinema', 'box office',
                      'oscar', 'academy award', 'emmy', 'grammy',
                      'golden globe', 'tony award', 'cannes',
                      'netflix', 'disney+', 'disney plus', ' hbo ',
                      'streaming service', 'amazon prime video', 'paramount+',
                      'tv show', 'tv series', 'sitcom', 'drama series',
                      'album', 'concert', 'tour dates', 'billboard', 'top 40',
                      'music video',
                      'celebrity', ' celeb ', 'hollywood', 'actor', 'actress',
                      'taylor swift', 'beyonce', 'beyoncé', 'drake', 'kanye',
                      'kardashian', 'rihanna', 'bieber',
                      'marvel', 'dc comics', 'star wars', 'harry potter',
                      'lord of the rings', 'game of thrones', 'house of the dragon',
                      'anime', 'manga', 'pixar', 'dreamworks'],
    'politics':      ['trump', 'biden', 'election', 'congress', 'senate', 'president',
                      'democrat', 'republican', 'vote', 'policy', 'legislation',
                      'supreme court', 'governor', 'parliament',
                      'war', 'ceasefire', 'military', 'nato', 'ukraine', 'russia',
                      'gaza', 'israel', 'iran', 'harris', 'political', 'sanction',
                      'tariff', 'geopolit'],
    'sports':        [# Only acronyms with real English-word substring collisions
                      # are space-padded:
                      #   ' nfl ' — without spaces matches inside 'inflation', 'conflict'
                      #   ' nba ' — without spaces matches inside 'unbalanced', 'unbanked'
                      #   ' mma ' — without spaces matches inside 'comma', 'summary'
                      # Others (mlb/nhl/ufc/fifa/f1) have low substring risk and are
                      # left unpadded so bracketed tokens like "[F1 China]" still match.
                      ' nba ', ' nfl ', ' mma ',
                      'mlb', 'nhl', 'fifa', 'ufc', 'f1', 'ncaa', 'cricket',
                      'tour de france', 'tour of flanders', 'giro d',
                      'world cup', 'olympics', 'championship', 'tennis', 'golf',
                      'soccer', 'football', 'basketball', 'baseball', 'premier league',
                      # Space-bounded so we match whole words only:
                      # ' win ' avoids Windows/winning; ' game ' avoids GameStop/gaming;
                      # ' team ' avoids steam; ' match '/' score '/' league '/' player '
                      # avoid mismatches in economics/science questions.
                      # _infer_market_category pads q with spaces so these also match
                      # at the start/end of a sentence.
                      ' win ', ' team ', ' match ', ' game ', ' score ', ' league ', ' player ',
                      'tournament', 'bundesliga', 'la liga', 'serie a',
                      'champions league', 'europa league', 'boxing',
                      'formula 1', 'wimbledon', 'super bowl', 'world series'],
    'science':       ['nasa', 'spacex', 'climate', 'vaccine', 'fda', 'cdc', 'pandemic',
                      'cancer', 'physics', 'biology', 'crispr', 'fusion',
                      'earthquake', 'hurricane', 'temperature', 'science', 'research',
                      'drug approval', 'clinical trial',
                      'artemis', 'moon landing', 'asteroid', 'satellite launch'],
    'business':      [# General corporate / company-specific markets. Space-padded
                      # for common English words to avoid substring false positives
                      # (' apple ' won't match 'pineapple', ' meta ' won't match
                      # 'metabolism', ' x corp' won't match 'xyz').
                      ' apple ', 'google', 'microsoft', ' meta ', 'facebook',
                      'tesla', 'nvidia', 'tiktok', 'twitter', ' x corp', ' xai ',
                      'uber', 'airbnb', 'walmart', 'mcdonalds', "mcdonald's",
                      'starbucks', 'nike', 'boeing', 'samsung', 'sony',
                      'spotify', 'shopify', 'amazon',
                      'company', 'corporation', 'corporate',
                      ' ceo ', ' cfo ', ' cto ', 'chief executive',
                      'startup', ' ipo ', 'acquires', 'acquisition', 'merger',
                      'bankruptcy', 'product launch', 'layoff', 'layoffs',
                      'earnings report', 'quarterly report', 'subscription'],
    'economics':     ['gdp', 'inflation', 'federal reserve', 'fed rate', 'interest rate',
                      'recession', 'stock market', 'nasdaq', 'sp500', 's&p', 'cpi',
                      'unemployment', 'treasury', 'stock', 'economy', 'dow', 'dollar',
                      'euro', 'trade deficit', 'budget deficit', 'debt ceiling'],
}

# Category-level bias loaded from data/calibration_table.json at import time.
# Falls back to the values recorded at the last manual calibration run so the
# strategy still works on a fresh checkout before calibration data is generated.
#
# Op5 note (2026-04-11): the fallback values below are the PRE category-v2
# biases. After the v2 reclassify moved markets between categories, every
# live bias value dropped (e.g. politics: 0.0623 → 0.0068, crypto: 0.0534 →
# 0.0395). These fallbacks are only used on a fresh checkout where the live
# calibration_table.json doesn't exist yet; the real runtime values come from
# _load_calibration_data() below.
_CATEGORY_BIAS_FALLBACK: Dict[str, float] = {
    'other':         0.0439,
    'sports':        0.0348,
    'economics':     0.0151,
    'politics':      0.0623,
    'ai_tech':       0.0019,
    'crypto':        0.0534,
    'science':       0.0543,
    'gaming':        0.0000,   # v2 — no data yet
    'entertainment': 0.0000,   # v2 — no data yet
    'business':      0.0000,   # v2 — no data yet
}


def _load_calibration_data() -> tuple[Dict[str, float], list]:
    """Return (category_bias, by_category_bucket_rows) from calibration_table.json.

    by_category_bucket gives the per-(category, 10% bucket) bias — this is more
    precise than the category-level aggregate and is what probability_bias_strategy
    now prefers when available.
    """
    try:
        _cal = Path(__file__).resolve().parent.parent / "data" / "calibration_table.json"
        data = json.loads(_cal.read_text())
        cat_bias = {row['category']: row['bias'] for row in data.get('by_category', [])}
        bucket_rows = data.get('by_category_bucket', []) or []
        if cat_bias:
            return cat_bias, bucket_rows
    except Exception:
        pass
    return _CATEGORY_BIAS_FALLBACK.copy(), []


_CATEGORY_BIAS, _BY_CATEGORY_BUCKET = _load_calibration_data()

# Minimum bias required for probability_bias to fire.
#
# Op5 (2026-04-11): lowered from 0.04 → 0.025 based on the post-v2 calibration
# distribution. At 0.04, only gaming (0.1501, n=17) passed the gate so the
# strategy was effectively dead. At 0.025, crypto/sports/science/other all
# clear at the category level, and three additional per-bucket cells unlock
# (sports 0-10%, other 0-10%, other 40-50%).
_MIN_BIAS_TO_TRADE = 0.025

# Minimum sample size for a per-bucket calibration cell to be used directly.
# Cells smaller than this fall back to the category-level bias aggregate.
_MIN_BUCKET_SAMPLES = 15


def _bucket_bias_for(category: str, probability: float) -> Optional[float]:
    """Look up the per-bucket bias for a (category, 10% bucket) cell.

    Returns None if:
      - there is no cell matching (category, bucket)
      - the cell is not marked reliable
      - the cell has fewer than _MIN_BUCKET_SAMPLES samples

    When None, probability_bias_strategy falls back to the coarser
    _CATEGORY_BIAS[category] aggregate.
    """
    if not _BY_CATEGORY_BUCKET:
        return None
    prob_pct = max(0.0, min(1.0, probability))
    bucket_idx = min(int(prob_pct * 10), 9)
    bucket_low = bucket_idx * 0.10
    for cell in _BY_CATEGORY_BUCKET:
        if (cell.get("category") == category
                and abs(cell.get("bucket_low", -1) - bucket_low) < 0.001
                and cell.get("reliable")
                and (cell.get("sample_size") or 0) >= _MIN_BUCKET_SAMPLES):
            return cell.get("bias")
    return None


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

        SILENT-BY-DESIGN (Op5, 2026-04-11):
        Op3 counter evidence (2026-04-11 11:39 UTC): raw_fires=0/100 in first
        production run. The auto_research.py one-per-run probe already tells
        us why: `resolutionProbability present in 5/100 markets`. Only ~5% of
        open markets have the field populated at all, so fire rate is
        structurally capped at ~5% × (fraction with a ≥15pp gap). This is
        sparse-by-design, not a bug. Do NOT widen the 15pp gap or loosen the
        guard — the strategy's value comes from ONLY firing on strong creator
        disagreement, and widening would drown the signal in noise. If the
        24-hour fire rate stays at exactly zero over a week of counter data,
        consider demoting the strategy's 0.75 confidence to avoid bloating the
        family-dedup weighting.
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

        CONFIRMATION-ONLY (Op5, 2026-04-11):
        Op3 counters show thin_market raw_fires=0 in the first production
        run. That is expected: auto_research.py wires thin_market as a
        confirmation-only signal — it adds a +0.03 boost to an existing
        contrarian/fundamental signal when it agrees, but never votes
        independently. So thin_market can only "fire" in counters when:
          1. It matches the pool-imbalance guard, AND
          2. A contrarian/fundamental signal of the same direction also
             fires on the same market.
        In the first production run there were only 3 contrarian signals
        total and none of them were matching thin-market imbalances, so
        thin_market_confirmations=0 is the correct output. This is NOT a
        bug — do not "fix" by making thin_market vote independently,
        because then we'd be stacking correlated imbalance signals with
        the contrarian family and breaking the family-dedup invariant.
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
        Exploit systematic crowd overconfidence — using per-(category, bucket)
        calibration data when available, with a category-level aggregate fallback.

        Lookup order (most-specific first):
          1. (category, 10%-bucket) cell with sample_size ≥ _MIN_BUCKET_SAMPLES
             and reliable=True  →  use cell bias directly.
          2. category aggregate in _CATEGORY_BIAS                    →  fallback.

        A market fires if the chosen bias is ≥ _MIN_BIAS_TO_TRADE AND the
        current probability falls in a bucket with the same sign of bias.
        The direction is set by the sign of the bias:
          bias > 0  →  crowd overestimates YES  →  bet NO
          bias < 0  →  crowd underestimates YES →  bet YES

        Op5 changes (2026-04-11):
          - Removed the hardcoded two-bucket filter (60-70% and 30-40%).
            Any 10% bucket with reliable calibration data now qualifies.
          - Switched the primary lookup from category-level aggregate to
            per-(category, bucket) cell — the calibration table already
            contains this more precise data, we just weren't using it.
          - Lowered _MIN_BIAS_TO_TRADE from 0.04 → 0.025 to match the
            post-v2-reclassify bias distribution.

        Family: contrarian.  Confidence: 0.70.
        """
        # Missing probability field → treat as malformed market, do not trade.
        probability = market.get('probability')
        if probability is None:
            return None

        category = _infer_market_category(market.get('question', ''))

        # Primary lookup: per-(category, bucket) bias.
        bias = _bucket_bias_for(category, probability)
        source = "bucket"

        # Fallback: category-level aggregate.
        if bias is None:
            bias = _CATEGORY_BIAS.get(category, _CATEGORY_BIAS.get('other', 0.0))
            source = "category"

        if abs(bias) < _MIN_BIAS_TO_TRADE:
            logger.debug(
                "probability_bias: skipping %s (%s=%s, bias=%.4f < threshold %.4f)",
                market.get('id', '?'), source, category, bias, _MIN_BIAS_TO_TRADE,
            )
            return None

        # Positive bias → crowd overestimates YES → bet NO.
        # Negative bias → crowd underestimates YES → bet YES.
        return "NO" if bias > 0 else "YES"

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
