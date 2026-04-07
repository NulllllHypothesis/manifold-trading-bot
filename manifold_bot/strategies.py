"""
Trading strategies for Manifold Markets.

Each strategy is a static method on TradingStrategies that takes a market dict
and returns "YES", "NO", or None.  auto_research.py calls these and votes.

Strategy inventory:
  probability_direction   — follow momentum if recently active (lastBetTime guard)
  mean_reversion          — bet against extremes, guarded by uniqueBettorCount
  volume_spike            — detect recent activity burst via volume24Hours
  creator_disagreement    — bet toward creator's resolution estimate when crowd diverges
  thin_market             — bet toward the underrepresented (thin) side of the liquidity pool
  is_stale_market         — pre-filter: returns True if market has no recent activity

Confidence scores (used by auto_research.py voting):
  probability_direction   0.65
  mean_reversion          0.70
  volume_spike            0.65
  creator_disagreement    0.75
  thin_market             0.65
  probability_bias        0.70
"""

import logging
import time
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class TradingStrategies:
    """Collection of trading strategies."""

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
        Follow the direction of the current probability if the market is active.

        Rules:
          - Skip the 45-55% coinflip zone (no edge).
          - Skip if last bet was > 48 hours ago (stale signal).
          - Otherwise bet with the crowd direction.

        Confidence: 0.65 (meets stat floor — AI can boost or veto).
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

        if probability > 0.55:
            return "YES"
        elif probability < 0.45:
            return "NO"

        return None

    @staticmethod
    def mean_reversion_strategy(market: Dict) -> Optional[str]:
        """
        Bet against extreme probabilities — but only when the crowd is thin.

        Logic: a market at 85% with 400 bettors is probably right.
        A market at 85% with 3 bettors is probably mispriced.

        Rules:
          - Fires at probability > 0.80 (bet NO) or < 0.20 (bet YES).
          - Skipped if uniqueBettorCount > 100 (deep crowd consensus).

        Confidence: 0.70.
        """
        probability = market.get('probability', 0.5)
        bettor_count = market.get('uniqueBettorCount') or 0

        if probability > 0.80:
            if bettor_count > 100:
                return None  # too many bettors — don't fight genuine consensus
            return "NO"

        if probability < 0.20:
            if bettor_count > 100:
                return None
            return "YES"

        return None

    @staticmethod
    def volume_spike_strategy(market: Dict, avg_volume: float, spike_multiplier: float = 2.0) -> Optional[str]:
        """
        Detect unusual recent trading activity using volume24Hours.

        Uses 24h volume (not all-time volume) so a 2-year-old market with high
        historical volume doesn't trigger on a quiet day.  avg_volume should be
        the median volume24Hours across all markets in the current fetch batch.

        spike_multiplier: how many times the median a market must trade to count
        as a spike.  Default 2.0 — calibrated for 24h volume (not all-time).

        Confidence: 0.65.
        """
        volume24 = market.get('volume24Hours') or 0
        probability = market.get('probability', 0.5)

        if avg_volume > 0 and volume24 > avg_volume * spike_multiplier:
            return "YES" if probability > 0.5 else "NO"

        return None

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
        Exploit systematic crowd overconfidence measured from 1,100+ resolved markets.

        Calibration findings (data/calibration_table.json):
          60–70% bucket: crowd says ~65%, actual YES rate is ~47% → bias +18pp (LARGEST)
          30–40% bucket: crowd says ~35%, actual YES rate is ~24% → bias +11pp

        In both ranges the crowd overestimates YES, so the edge is to bet NO.

        Confidence: 0.70 — data-backed with N=53 (60-70%) and N=63 (30-40%) samples.
        Does NOT fire in the 40-60% zone where bias is noisier.
        """
        probability = market.get('probability', 0.5)

        # 60-70%: strongest calibration edge (+18pp overconfidence)
        if 0.60 <= probability < 0.70:
            return "NO"

        # 30-40%: secondary calibration edge (+11pp overconfidence)
        if 0.30 <= probability < 0.40:
            return "NO"

        return None

    # ------------------------------------------------------------------ #
    # Utilities
    # ------------------------------------------------------------------ #

    @staticmethod
    def find_arbitrage_opportunities(markets: List[Dict]) -> List[Dict]:
        """
        Find arbitrage opportunities between related markets.
        Groups markets by common keyword and flags outliers.
        """
        opportunities = []
        market_groups: Dict[str, List[Dict]] = {}

        for market in markets:
            question = market.get('question', '').lower()
            keywords = ['bitcoin', 'ethereum', 'trump', 'biden', 'tesla', 'apple']
            for keyword in keywords:
                if keyword in question:
                    market_groups.setdefault(keyword, []).append(market)

        for keyword, group_markets in market_groups.items():
            if len(group_markets) < 2:
                continue

            probs = [m.get('probability', 0.5) for m in group_markets]
            avg_prob = sum(probs) / len(probs)

            for market in group_markets:
                prob = market.get('probability', 0.5)
                diff = abs(prob - avg_prob)
                if diff > 0.1:
                    opportunities.append({
                        'market_id': market.get('id'),
                        'question': market.get('question'),
                        'probability': prob,
                        'group_avg': avg_prob,
                        'difference': diff,
                        'suggested_bet': 'YES' if prob < avg_prob else 'NO',
                    })

        return opportunities

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

    # volume_spike needs the batch median — pass 0 here so it never fires solo
    rec = TradingStrategies.volume_spike_strategy(market, avg_volume=0)
    if rec:
        signals['volume_spike'] = rec

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
