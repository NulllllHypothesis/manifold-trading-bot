#!/usr/bin/env python3
"""
Automated trading script for Manifold Markets.
Executes trades based on research recommendations with risk management.
"""

import json
import sys
import os
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import random

# Add project root to path so manifold_bot package is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from manifold_bot.manifold_api import api_client
from manifold_bot.paper_trader import PaperTrader
from manifold_bot.strategies import TradingStrategies
from manifold_bot.config import MIN_BET_AMOUNT, MAX_BET_AMOUNT, MIN_CONFIDENCE

class AutoTrader:
    """Automated trading with risk management"""

    def __init__(self):
        self.research_file = "market_research.json"
        self.trade_log_file = "auto_trades.json"
        self.trader = PaperTrader()  # Will auto-load state from __init__

        # Risk management parameters
        self.max_positions = 10  # Increased from 5 to allow more diversification (currently 8 open)
        self.max_position_size = 0.1  # 10% of balance per trade
        self.min_confidence = MIN_CONFIDENCE  # Now 0.60 (reduced from 0.65)
        self.cooldown_hours = 4  # Reduced from 6 hours to allow faster re-entry

    def load_latest_research(self) -> Optional[Dict]:
        """Load the latest market research"""
        if not os.path.exists(self.research_file):
            print("No research file found")
            return None

        try:
            with open(self.research_file, 'r') as f:
                data = json.load(f)

            # Determine whether the file is flat (written directly by save_research())
            # or nested under a 'latest' key.
            # auto_research.py writes research_data directly to the file (flat structure),
            # so schema_version lives at the top level of data, not under data['latest'].
            # We support both layouts for forward-compatibility:
            #   - Flat:   { "schema_version": 2, "recommendations": [...], ... }
            #   - Nested: { "latest": { "schema_version": 2, "recommendations": [...] }, "history": [...] }
            if 'latest' in data:
                # Nested layout
                latest = data['latest']
            else:
                # Flat layout — the whole object is the research record
                latest = data

            schema = (latest or {}).get('schema_version', 1)
            if schema < 2:
                # OpenClaw captures stdout and forwards it to Telegram — this alert reaches the group
                print(f"🚨 TRADING HALTED: market_research.json is schema v{schema} (pre-AI). "
                      f"Re-run auto_research.py before next trading cycle.")
                return None
            return latest
        except Exception as e:
            print(f"Error loading research: {e}")
            return None

    def should_trade_market(self, market_id: str, recommendation: Dict) -> bool:
        """
        Check if we should trade a specific market based on risk rules

        Returns:
            bool: True if trade should be executed
        """
        # Check confidence threshold
        if recommendation['confidence'] < self.min_confidence:
            print(f"  Confidence too low: {recommendation['confidence']*100:.0f}% < {self.min_confidence*100:.0f}%")
            return False

        # Check if we already have a position
        if recommendation.get('existing_position', False):
            print(f"  Already have position in this market")

            # Check cooldown period
            market_positions = self.trader.positions.get(market_id, [])
            if market_positions:
                latest_trade = max(market_positions, key=lambda x: x.get('timestamp', ''))
                trade_time = datetime.fromisoformat(latest_trade.get('timestamp', '2000-01-01'))
                hours_since = (datetime.now() - trade_time).total_seconds() / 3600

                if hours_since < self.cooldown_hours:
                    print(f"  In cooldown period ({hours_since:.1f}h < {self.cooldown_hours}h)")
                    return False

            # Allow adding to existing position if confidence is high
            if recommendation['confidence'] < 0.8:
                print(f"  Confidence not high enough to add to position")
                return False

        # Check max positions limit
        open_positions = sum(1 for pos_list in self.trader.positions.values()
                           for pos in pos_list if pos.get('status') == 'OPEN')

        if open_positions >= self.max_positions:
            print(f"  Max positions reached ({open_positions}/{self.max_positions})")
            return False

        # Check market liquidity
        if recommendation.get('liquidity', 0) < 200:
            print(f"  Liquidity too low: ${recommendation.get('liquidity', 0):.0f}")
            return False

        return True

    def calculate_position_size(self, recommendation: Dict, outcome: str) -> float:
        """
        Calculate position size using Kelly Criterion when AI probability estimate is available,
        falling back to confidence-scaled sizing otherwise.

        Args:
            recommendation: Dict containing market recommendation data including 'confidence',
                            'probability', and optionally 'ai_estimated_probability'.
            outcome: The trade direction, either 'YES' or 'NO'.

        Kelly logic:
          - net_odds for YES at market prob m = (1-m)/m
          - net_odds for NO  at market prob m = m/(1-m)
          - Kelly fraction = (win_prob × net_odds - lose_prob) / net_odds
          - We use half-Kelly (×0.5) to reduce variance — standard conservative practice.
          - If Kelly fraction <= 0 (negative edge), returns 0.0 to signal no trade.

        Fallback (no AI estimate):
          - base 10% of balance scaled linearly by confidence.

        Either path is capped at MAX_BET_AMOUNT and 50% of balance.

        Returns:
            float: The position size in dollars, or 0.0 if the trade should be skipped.

        Raises:
            TypeError: If recommendation is not a dict.
        """
        if not isinstance(recommendation, dict):
            raise TypeError(f'recommendation must be dict, got {type(recommendation)}')

        confidence = recommendation['confidence']
        market_prob = recommendation.get('probability', 0.5)
        # Guard against degenerate probabilities (resolved / edge cases)
        market_prob = max(0.01, min(0.99, market_prob))

        ai_estimated_prob = recommendation.get('ai_estimated_probability')

        if ai_estimated_prob is not None:
            # Kelly sizing — use the AI's estimated true probability as our edge estimate.
            if outcome == 'YES':
                our_win_prob = float(ai_estimated_prob)
                net_odds = (1.0 - market_prob) / market_prob
            else:  # NO
                our_win_prob = 1.0 - float(ai_estimated_prob)
                net_odds = market_prob / (1.0 - market_prob)

            kelly_fraction = TradingStrategies.calculate_kelly_criterion(our_win_prob, net_odds)

            # If Kelly fraction is zero or negative, we have no edge (or are wrong-sided).
            # Return 0.0 so execute_trade can skip this trade entirely.
            if kelly_fraction <= 0:
                print(f"  Sizing: kelly(ai_prob={ai_estimated_prob:.2f}, net_odds={net_odds:.2f}) → no edge (k={kelly_fraction:.3f}), skipping")
                return 0.0

            # Half-Kelly reduces bet size when model has uncertainty — lowers ruin risk
            kelly_fraction *= 0.5
            position_size = self.trader.balance * kelly_fraction
            sizing_method = f"kelly(ai_prob={ai_estimated_prob:.2f}, net_odds={net_odds:.2f}, k={kelly_fraction:.3f})"
        else:
            # No AI estimate available — fall back to confidence-scaled sizing
            base_size = self.trader.balance * self.max_position_size
            confidence_multiplier = min(confidence / self.min_confidence, 2.0)
            position_size = base_size * confidence_multiplier
            sizing_method = f"confidence_scaled(conf={confidence:.2f})"

        # Apply bounds — note: we do NOT clamp to MIN_BET_AMOUNT before this check,
        # so a genuinely tiny Kelly size below MIN_BET_AMOUNT is treated as no trade.
        if position_size < MIN_BET_AMOUNT:
            print(f"  Sizing: {sizing_method} → ${position_size:.2f} below MIN_BET_AMOUNT, skipping")
            return 0.0

        position_size = min(position_size, MAX_BET_AMOUNT)
        position_size = min(position_size, self.trader.balance * 0.5)

        # Round to nearest $5
        rounded_size = round(position_size / 5) * 5

        # If rounding collapsed the size to zero and Kelly drove this (near-zero edge),
        # return 0.0 to allow the caller to skip the trade entirely rather than
        # falling back to MIN_BET_AMOUNT on a near-zero-edge market.
        if rounded_size == 0 and ai_estimated_prob is not None:
            print(f"  Sizing: {sizing_method} → rounded to $0, skipping (Kelly near-zero edge)")
            return 0.0

        position_size = rounded_size

        # After rounding, re-check the minimum (rounding down could push below minimum)
        if position_size < MIN_BET_AMOUNT:
            print(f"  Sizing: {sizing_method} → ${position_size:.2f} below MIN_BET_AMOUNT after rounding, skipping")
            return 0.0

        print(f"  Sizing: {sizing_method} → ${position_size:.2f}")
        return position_size

    def execute_trade(self, recommendation: Dict) -> bool:
        """
        Execute a trade based on recommendation

        Returns:
            bool: True if trade successful
        """
        market_id = recommendation['market_id']
        outcome = recommendation['recommendation']
        confidence = recommendation['confidence']

        print(f"\nExecuting trade:")
        print(f"  Market: {recommendation['question'][:80]}...")
        print(f"  Probability: {recommendation['probability']*100:.1f}%")
        print(f"  Recommendation: {outcome} (confidence: {confidence*100:.0f}%)")

        # Get current market probability
        try:
            market = api_client.get_market(market_id)
            current_prob = market.get('probability', recommendation['probability'])
        except:
            current_prob = recommendation['probability']
            print(f"  Warning: Could not fetch current probability, using research value")

        # Calculate position size (Kelly when AI estimate available, else confidence-scaled)
        amount = self.calculate_position_size(recommendation, outcome)

        # A return value of 0.0 means no edge or size too small — skip the trade entirely
        # rather than placing a minimum bet that contradicts the model's signal.
        if amount == 0.0:
            print(f"  ⏭️  Trade skipped: no edge or position size too small")
            return False

        print(f"  Position size: ${amount:.2f} ({amount/self.trader.balance*100:.1f}% of balance)")

        # Place paper trade
        success = self.trader.place_paper_bet(
            market_id=market_id,
            outcome=outcome,
            amount=amount,
            probability=current_prob
        )

        if success:
            print(f"  ✅ Trade executed successfully")

            # Build reasoning
            reasoning_parts = []
            if recommendation.get('strategies'):
                reasoning_parts.append(f"Strategies: {', '.join(recommendation['strategies'])}")
            if recommendation.get('ai_reasoning'):
                reasoning_parts.append(f"AI: {recommendation['ai_reasoning']}")
            reasoning = ' | '.join(reasoning_parts) if reasoning_parts else 'No reasoning provided'

            # Log trade
            self.log_trade({
                'market_id': market_id,
                'question': recommendation['question'],
                'outcome': outcome,
                'amount': amount,
                'probability': current_prob,
                'confidence': confidence,
                'timestamp': datetime.now().isoformat(),
                'strategies': recommendation.get('strategies', []),
                'reasoning': reasoning,
                'balance_after': self.trader.balance
            })

            # Save state
            self.trader.save_state()
            return True
        else:
            print(f"  ❌ Trade failed")
            return False

    def log_trade(self, trade_data: Dict):
        """Log auto-trade to file"""
        # Load existing trades
        trades = []
        if os.path.exists(self.trade_log_file):
            try:
                with open(self.trade_log_file, 'r') as f:
                    data = json.load(f)
                    trades = data.get('trades', [])
            except:
                trades = []

        # Add new trade
        trades.append(trade_data)

        # Keep only last 100 trades
        if len(trades) > 100:
            trades = trades[-100:]

        # Save
        data = {
            'total_trades': len(trades),
            'trades': trades
        }

        with open(self.trade_log_file, 'w') as f:
            json.dump(data, f, indent=2)

    def run_trading_cycle(self):
        """Main trading cycle"""
        print(f"\n{'='*60}")
        print(f"AUTO TRADING CYCLE - {datetime.now()}")
        print(f"{'='*60}")

        print(f"Starting balance: ${self.trader.balance:.2f}")
        open_positions = sum(1 for pos_list in self.trader.positions.values()
                                   for pos in pos_list if pos.get('status') == 'OPEN')
        print(f"Open positions: {open_positions}")

        # Load latest research
        research = self.load_latest_research()
        if not research or not research.get('recommendations'):
            print("No research recommendations available")
            return 0

        recommendations = research['recommendations']
        print(f"Loaded {len(recommendations)} research recommendations")

        # Filter and sort recommendations
        tradable_recs = []
        for rec in recommendations:
            if self.should_trade_market(rec['market_id'], rec):
                tradable_recs.append(rec)

        if not tradable_recs:
            print("No tradable opportunities after risk filtering")
            return 0

        # Sort by confidence (highest first)
        tradable_recs.sort(key=lambda x: x['confidence'], reverse=True)

        print(f"\nFound {len(tradable_recs)} tradable opportunities")

        # Execute top 1-2 trades (risk management)
        max_trades_per_cycle = 2
        trades_executed = 0

        for rec in tradable_recs[:max_trades_per_cycle]:
            if trades_executed >= max_trades_per_cycle:
                break

            print(f"\n{'─'*40}")
            if self.execute_trade(rec):
                trades_executed += 1

        print(f"\n{'='*60}")
        print(f"Trading cycle complete")
        print(f"Trades executed: {trades_executed}")
        print(f"Ending balance: ${self.trader.balance:.2f}")
        print(f"{'='*60}")

        return trades_executed

def main():
    """Main function"""
    trader = AutoTrader()
    trades_executed = trader.run_trading_cycle()

    # Return number of trades executed
    return trades_executed

if __name__ == "__main__":
    try:
        num_trades = main()
        sys.
