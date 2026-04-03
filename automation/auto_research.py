#!/usr/bin/env python3
"""
Automated market research script for Manifold Markets.
Runs hourly to analyze markets and identify trading opportunities.
"""

import json
import sys
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import os

# Add project root to path so manifold_bot package is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from manifold_bot.manifold_api import api_client
from manifold_bot.strategies import TradingStrategies
from manifold_bot.paper_trader import PaperTrader
from manifold_bot.ai_analyzer import batch_analyze

class MarketResearcher:
    """Automated market research and analysis"""

    def __init__(self):
        self.research_file = "market_research.json"
        self.trader = PaperTrader()  # Will auto-load state from __init__

    def analyze_markets(self, limit: int = 50) -> List[Dict]:
        """
        Analyze markets and identify trading opportunities

        Returns:
            List of market recommendations with confidence scores
        """
        print(f"[{datetime.now()}] Starting market analysis...")

        try:
            # Get recent markets
            markets = api_client.get_markets(limit=limit)
            print(f"  Retrieved {len(markets)} markets")

            recommendations = []

            for market in markets:
                market_id = market.get('id')
                question = market.get('question', 'Unknown')[:100]
                probability = market.get('probability', 0.5)
                volume = market.get('volume', 0)
                liquidity = market.get('totalLiquidity', 0)

                # Skip closed/resolved markets
                if market.get('isResolved', False):
                    continue

                # Skip low liquidity markets
                if liquidity < 100:
                    continue

                # Apply trading strategies
                strategies = []

                # Probability direction strategy
                prob_dir_rec = TradingStrategies.probability_direction_strategy(market)
                if prob_dir_rec:
                    strategies.append({
                        'strategy': 'probability_direction',
                        'recommendation': prob_dir_rec,
                        'confidence': 0.6
                    })

                # Mean reversion strategy
                mean_rev_rec = TradingStrategies.mean_reversion_strategy(market)
                if mean_rev_rec:
                    strategies.append({
                        'strategy': 'mean_reversion',
                        'recommendation': mean_rev_rec,
                        'confidence': 0.7
                    })

                # Volume spike strategy — use median volume of fetched markets as baseline
                avg_volume = sum(m.get('volume', 0) for m in markets) / max(len(markets), 1)
                volume_rec = TradingStrategies.volume_spike_strategy(market, avg_volume)
                if volume_rec:
                    strategies.append({
                        'strategy': 'volume_spike',
                        'recommendation': volume_rec,
                        'confidence': 0.65
                    })

                if strategies:
                    # Calculate overall recommendation
                    yes_votes = sum(1 for s in strategies if s['recommendation'] == 'YES')
                    no_votes = sum(1 for s in strategies if s['recommendation'] == 'NO')

                    if yes_votes > no_votes:
                        overall_rec = 'YES'
                        confidence = sum(s['confidence'] for s in strategies if s['recommendation'] == 'YES') / yes_votes
                    elif no_votes > yes_votes:
                        overall_rec = 'NO'
                        confidence = sum(s['confidence'] for s in strategies if s['recommendation'] == 'NO') / no_votes
                    else:
                        continue  # Skip ties

                    # Check if we already have a position
                    existing_position = market_id in self.trader.positions

                    recommendation = {
                        'market_id': market_id,
                        'question': question,
                        'probability': probability,
                        'volume': volume,
                        'liquidity': liquidity,
                        'recommendation': overall_rec,
                        'confidence': round(confidence, 2),
                        'strategies': [s['strategy'] for s in strategies],
                        'existing_position': existing_position,
                        'analyzed_at': datetime.now().isoformat()
                    }

                    recommendations.append(recommendation)

            # Sort by confidence (highest first) before AI pass
            recommendations.sort(key=lambda x: x['confidence'], reverse=True)

            # AI analysis — only top 5 candidates (local model is CPU-only, ~60s per market)
            top_candidates = recommendations[:5]
            top_candidate_ids = {r['market_id'] for r in top_candidates}
            candidate_markets = [m for m in markets if m.get('id') in top_candidate_ids]

            # Warn if market objects didn't match — means Manifold returned a different ID field
            if len(candidate_markets) != len(top_candidates):
                print(f"  Warning: expected {len(top_candidates)} candidate markets, found {len(candidate_markets)}. AI pass may be partial.")

            if candidate_markets:
                print(f"  Running AI analysis on top {len(candidate_markets)} candidates...")
                try:
                    ai_results = batch_analyze(candidate_markets, max_markets=5, delay=0.5)
                except Exception as e:
                    print(f"  Warning: AI analysis failed ({e}), continuing without AI scores")
                    ai_results = {}

                for rec in recommendations:
                    ai = ai_results.get(rec['market_id'])
                    is_ai_candidate = rec['market_id'] in top_candidate_ids

                    # Mark clearly whether AI actually ran or this market was out of scope
                    if not ai or ai['recommendation'] == 'SKIP':
                        rec['ai_recommendation'] = 'SKIP'
                        rec['ai_confidence'] = 0.0
                        rec['ai_reasoning'] = ''
                        rec['ai_source'] = None
                        rec['ai_ran'] = is_ai_candidate  # True = ran but said SKIP; False = not in top 5
                        continue

                    rec['ai_recommendation'] = ai['recommendation']
                    rec['ai_confidence'] = ai['confidence']
                    rec['ai_reasoning'] = ai['reasoning']
                    rec['ai_source'] = ai['source']
                    rec['ai_estimated_probability'] = ai['estimated_true_probability']
                    rec['ai_ran'] = True

                    # Blend statistical + AI confidence
                    # Require stat_conf >= 0.55 before AI can boost above threshold —
                    # prevents a weak stat signal getting lifted purely by AI confidence
                    stat_conf = rec['confidence']
                    if ai['recommendation'] == rec['recommendation']:
                        if stat_conf >= 0.55:
                            # AI agrees and stats are reasonable — blend with AI weighted higher
                            rec['confidence'] = round(0.4 * stat_conf + 0.6 * ai['confidence'], 3)
                        else:
                            # Stats too weak — AI can only bring it up to a capped level
                            rec['confidence'] = round(min(0.4 * stat_conf + 0.6 * ai['confidence'], 0.64), 3)
                        rec['strategies'] = rec['strategies'] + ['ai_analysis']
                    else:
                        # AI disagrees — penalise confidence significantly
                        rec['confidence'] = round(stat_conf * 0.4, 3)

            # Re-sort after AI pass
            recommendations.sort(key=lambda x: x['confidence'], reverse=True)

            print(f"  Found {len(recommendations)} trading opportunities")
            return recommendations

        except Exception as e:
            print(f"  Error analyzing markets: {e}")
            return []

    def save_research(self, recommendations: List[Dict]):
        """Save research results to file"""
        research_data = {
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

        print(f"  Research saved to {self.research_file}")

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

    # Skip research if at max positions (5)
    open_positions = sum(
        1 for trades in researcher.trader.positions.values()
        if any(t.get('status') == 'OPEN' for t in (trades if isinstance(trades, list) else [trades]))
    )
    max_positions = 5
    if open_positions >= max_positions:
        print(f"At max positions ({open_positions}/{max_positions}). Skipping research.")
        return 0

    recommendations = researcher.run_hourly_research()

    # Return number of recommendations for cron job monitoring
    return len(recommendations)

if __name__ == "__main__":
    try:
        num_recs = main()
        sys.exit(0)
    except Exception as e:
        print(f"Fatal error: {e}")
        sys.exit(1)
