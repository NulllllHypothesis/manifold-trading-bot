"""
Trading strategies for Manifold Markets.

Each strategy is a static method on TradingStrategies that takes a market dict
and returns "YES", "NO", or None.  auto_research.py calls these and votes.

Strategy inventory:
  probability_direction   — follow momentum if recently active (lastBetTime guard)
  mean_reversion          — bet against extremes, guarded by uniqueBettorCount
  volume_spike            — detect recent activity burst via volume24Hours
  creator_disagreement    — bet toward creator's resolution estimate when crowd diverges
  thin_market             — bet toward the thin side of the liquidity pool (arb)
  is_stale_market         — pre-filter: returns True if market has no recent activity

Confidence scores (used by auto_research.py voting):
  probability_direction   0.60
  mean_reversion          0.70
  volume_spike            0.65
  creator_disagreement    0.75
  thin_market             0.65
"""

import time
from typing import Dict, List, Optional


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

        Confidence: 0.60 (weakest — just following momentum).
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
    def volume_spike_strategy(market: Dict, avg_volume: float) -> Optional[str]:
        """
        Detect unusual recent trading activity using volume24Hours.

        Uses 24h volume (not all-time volume) so a 2-year-old market with high
        historical volume doesn't trigger on a quiet day.  avg_volume should be
        the median volume24Hours across all markets in the current fetch batch.

        Confidence: 0.65.
        """
        volume24 = market.get('volume24Hours') or 0
        probability = market.get('probability', 0.5)

        if avg_volume > 0 and volume24 > avg_volume * 2:
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
        """
        probability = market.get('probability', 0.5)
        resolution_prob = market.get('resolutionProbability')

        if resolution_prob is None:
            return None

        gap = resolution_prob - probability
        if abs(gap) < 0.15:
            return None

        # Bet toward creator's estimate
        return "YES" if gap > 0 else "NO"

    @staticmethod
    def thin_market_strategy(market: Dict) -> Optional[str]:
        """
        Bet toward the thin side of the AMM liquidity pool.

        In Manifold's CFMM, when one side of the pool is very thin relative to
        the other, a small bet moves the price a lot.  Betting toward the thin
        side pushes the price back toward fair value — this is essentially
        on-chain arbitrage.

        Imbalance threshold: 0.70 (thin side holds < 15% of total pool).

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

        # Bet toward the thin side
        return "YES" if yes_pool < no_pool else "NO"

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
