"""
Trading strategies for Manifold Markets
"""

from typing import Dict, List, Optional
from .manifold_api import api_client


class TradingStrategies:
    """Collection of trading strategies"""
    
    @staticmethod
    def momentum_strategy(market: Dict, lookback_hours: int = 24) -> Optional[str]:
        """
        Momentum strategy: Bet in direction of recent price movement
        
        Returns:
            "YES", "NO", or None (no trade)
        """
        # This is a simplified version - in reality you'd need historical data
        probability = market.get('probability', 0.5)
        
        # Simple rule: if probability > 0.5, bet YES; if < 0.5, bet NO
        # Add threshold to avoid trading near 0.5
        threshold = 0.05
        
        if probability > 0.5 + threshold:
            return "YES"
        elif probability < 0.5 - threshold:
            return "NO"
        
        return None
    
    @staticmethod
    def mean_reversion_strategy(market: Dict) -> Optional[str]:
        """
        Mean reversion: Bet against extreme probabilities
        
        Returns:
            "YES", "NO", or None (no trade)
        """
        probability = market.get('probability', 0.5)
        
        # Bet against extreme probabilities
        if probability > 0.8:  # Very high probability
            return "NO"  # Bet it will decrease
        elif probability < 0.2:  # Very low probability
            return "YES"  # Bet it will increase
        
        return None
    
    @staticmethod
    def volume_spike_strategy(market: Dict, avg_volume: float) -> Optional[str]:
        """
        Volume spike: Bet in direction of high volume movement
        
        Returns:
            "YES", "NO", or None (no trade)
        """
        current_volume = market.get('volume', 0)
        probability = market.get('probability', 0.5)
        
        # If volume is significantly above average
        if current_volume > avg_volume * 2:  # 2x average volume
            # Bet in direction of probability movement
            # (Simplified - would need historical data for actual movement)
            if probability > 0.5:
                return "YES"
            else:
                return "NO"
        
        return None
    
    @staticmethod
    def find_arbitrage_opportunities(markets: List[Dict]) -> List[Dict]:
        """
        Find arbitrage opportunities between related markets
        
        Returns:
            List of arbitrage opportunities
        """
        opportunities = []
        
        # Group markets by topic/keyword
        market_groups = {}
        for market in markets:
            question = market.get('question', '').lower()
            tags = market.get('tags', [])
            
            # Simple grouping by common keywords
            keywords = ['bitcoin', 'ethereum', 'trump', 'biden', 'tesla', 'apple']
            for keyword in keywords:
                if keyword in question or any(keyword in tag.lower() for tag in tags):
                    if keyword not in market_groups:
                        market_groups[keyword] = []
                    market_groups[keyword].append(market)
        
        # Look for price discrepancies within groups
        for keyword, group_markets in market_groups.items():
            if len(group_markets) < 2:
                continue
            
            # Calculate average probability
            probabilities = [m.get('probability', 0.5) for m in group_markets]
            avg_prob = sum(probabilities) / len(probabilities)
            
            # Find markets significantly different from average
            for market in group_markets:
                prob = market.get('probability', 0.5)
                diff = abs(prob - avg_prob)
                
                if diff > 0.1:  # 10% difference threshold
                    opportunity = {
                        'market_id': market.get('id'),
                        'question': market.get('question'),
                        'probability': prob,
                        'group_avg': avg_prob,
                        'difference': diff,
                        'suggested_bet': 'YES' if prob < avg_prob else 'NO'
                    }
                    opportunities.append(opportunity)
        
        return opportunities
    
    @staticmethod
    def calculate_kelly_criterion(probability: float, payout_ratio: float) -> float:
        """
        Calculate Kelly Criterion bet size
        
        Args:
            probability: Your estimated probability of winning
            payout_ratio: Payout if win (e.g., 2.0 for 1:1 odds)
        
        Returns:
            Fraction of bankroll to bet (0-1)
        """
        if payout_ratio <= 1:
            return 0
        
        q = 1 - probability
        kelly = (probability * payout_ratio - q) / payout_ratio
        
        # Cap at reasonable levels
        return max(0, min(kelly, 0.25))  # Max 25% of bankroll


def analyze_market_for_trading(market: Dict) -> Dict:
    """
    Comprehensive market analysis for trading decisions
    
    Returns:
        Dict with analysis and trading signals
    """
    analysis = {
        'market_id': market.get('id'),
        'question': market.get('question'),
        'current_probability': market.get('probability'),
        'volume_24h': market.get('volume24h', 0),
        'liquidity': market.get('liquidity', 0),
        'is_resolved': market.get('isResolved', False),
        'signals': {},
        'recommended_action': None,
        'confidence': 0
    }
    
    if analysis['is_resolved']:
        return analysis
    
    # Apply strategies
    signals = {}
    
    # Momentum signal
    momentum = TradingStrategies.momentum_strategy(market)
    if momentum:
        signals['momentum'] = momentum
    
    # Mean reversion signal
    mean_rev = TradingStrategies.mean_reversion_strategy(market)
    if mean_rev:
        signals['mean_reversion'] = mean_rev
    
    # Volume analysis (simplified)
    if market.get('volume', 0) > 1000:  # High volume threshold
        signals['high_volume'] = True
    
    # Combine signals
    analysis['signals'] = signals
    
    # Make recommendation based on signals
    if signals:
        # Simple voting system
        yes_votes = sum(1 for s in signals.values() if s == 'YES')
        no_votes = sum(1 for s in signals.values() if s == 'NO')
        
        if yes_votes > no_votes:
            analysis['recommended_action'] = 'YES'
            analysis['confidence'] = yes_votes / len(signals)
        elif no_votes > yes_votes:
            analysis['recommended_action'] = 'NO'
            analysis['confidence'] = no_votes / len(signals)
    
    return analysis