#!/usr/bin/env python3
"""
Automated market research script for Manifold Markets.
Runs hourly to analyze markets and identify trading opportunities.
"""

import json
import sys
import time
import statistics as _stats
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import os

# Add project root to path so manifold_bot package is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time as _time

from manifold_bot.manifold_api import api_client
from manifold_bot.strategies import TradingStrategies
from manifold_bot.paper_trader import PaperTrader
from manifold_bot.ai_analyzer import batch_analyze
from manifold_bot.config import MIN_CONFIDENCE

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

        Schema notes (schema_version=2):
          ai_was_candidate: bool  — True iff the market was in the top-5 sent to AI.
          ai_returned_skip: bool  — True iff the market was a candidate AND AI returned SKIP.
          ai_recommendation: str | None
            - None            : market was never sent to AI (ai_was_candidate=False).
                                Previously this was set to 'SKIP', which was misleading.
            - 'SKIP'          : AI was sent this market and explicitly said SKIP.
            - 'YES' / 'NO'    : AI's directional recommendation.
        """
        print(f"[{datetime.now()}] Starting market analysis...")

        try:
            # Get recent markets
            markets = api_client.get_markets(limit=limit)
            print(f"  Retrieved {len(markets)} markets")

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

                # Skip stale markets — last bet > 72h ago AND near-zero 24h volume
                if TradingStrategies.is_stale_market(market):
                    continue

                # Apply trading strategies
                strategies = []

                # Probability direction — requires recent activity (lastBetTime guard built in)
                prob_dir_rec = TradingStrategies.probability_direction_strategy(market)
                if prob_dir_rec:
                    strategies.append({
                        'strategy': 'probability_direction',
                        'recommendation': prob_dir_rec,
                        'confidence': 0.65
                    })

                # Mean reversion — guarded by uniqueBettorCount (don't fight genuine consensus)
                mean_rev_rec = TradingStrategies.mean_reversion_strategy(market)
                if mean_rev_rec:
                    strategies.append({
                        'strategy': 'mean_reversion',
                        'recommendation': mean_rev_rec,
                        'confidence': 0.70
                    })

                # Volume spike — uses volume24Hours vs batch median (not all-time volume).
                # The multiplier VOLUME_SPIKE_MULTIPLIER (2.0) is calibrated for 24h volumes:
                # we compare market.volume24Hours against median_volume24h, so the ratio is
                # dimensionally consistent and 2x is appropriate (same scale, same signal).
                volume_rec = TradingStrategies.volume_spike_strategy(
                    market, median_volume24h, spike_multiplier=VOLUME_SPIKE_MULTIPLIER
                )
                if volume_rec:
                    strategies.append({
                        'strategy': 'volume_spike',
                        'recommendation': volume_rec,
                        'confidence': 0.65
                    })

                # Creator disagreement — fires when creator's resolution estimate diverges ≥15pp
                creator_rec = TradingStrategies.creator_disagreement_strategy(market)
                if creator_rec:
                    strategies.append({
                        'strategy': 'creator_disagreement',
                        'recommendation': creator_rec,
                        'confidence': 0.75
                    })

                # Thin market arb — fires when one side of the AMM pool holds < 15% of total
                thin_rec = TradingStrategies.thin_market_strategy(market)
                if thin_rec:
                    strategies.append({
                        'strategy': 'thin_market',
                        'recommendation': thin_rec,
                        'confidence': 0.65
                    })

                # Calibration bias — bet NO in 60-70% and 30-40% ranges where the crowd
                # historically overestimates YES by 18pp and 11pp respectively.
                bias_rec = TradingStrategies.probability_bias_strategy(market)
                if bias_rec:
                    strategies.append({
                        'strategy': 'probability_bias',
                        'recommendation': bias_rec,
                        'confidence': 0.70
                    })

                if strategies:
                    # Calculate overall recommendation by weighted average of agreeing votes
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
                        'volume24h': market.get('volume24Hours') or 0,
                        'liquidity': liquidity,
                        'unique_bettors': market.get('uniqueBettorCount') or 0,
                        'last_bet_time_ms': market.get('lastBetTime'),
                        'recommendation': overall_rec,
                        'confidence': round(confidence, 2),
                        'strategies': [s['strategy'] for s in strategies],
                        'existing_position': existing_position,
                        'analyzed_at': datetime.now().isoformat()
                    }

                    recommendations.append(recommendation)

            # Sort by confidence (highest first) before AI pass
            recommendations.sort(key=lambda x: x['confidence'], reverse=True)

            # AI candidate selection — recency boost.
            # Among recommendations above the stat floor (worth AI review), prefer markets with
            # recent bet activity: a freshly-traded market's AI analysis is more valuable than
            # one where the probability hasn't moved in a week.
            # Fallback: if fewer than 5 are above the floor, fill from remaining by confidence.
            above_floor = [
                r for r in recommendations if r['confidence'] >= _STAT_BOOST_FLOOR
            ]
            below_floor = [
                r for r in recommendations if r['confidence'] < _STAT_BOOST_FLOOR
            ]
            # Sort above-floor by recency (most recently bet first); treat missing as oldest.
            above_floor.sort(
                key=lambda r: r.get('last_bet_time_ms') or 0,
                reverse=True,
            )
            recent_top = (above_floor + below_floor)[:5]
            top_candidate_ids = {r['market_id'] for r in recent_top}
            candidate_markets = [m for m in markets if m.get('id') in top_candidate_ids]

            # Guard: if the count doesn't match, the market objects use a different ID field
            # (e.g. 'slug' or 'url') and AI analysis cannot be reliably performed.
            # Rather than silently writing schema_version=2 with all ai_was_candidate=False
            # (which would cause auto_trader.py to trade without AI validation), we raise a
            # clear error so the caller can catch it and write schema_version=1 instead,
            # causing the trader's schema guard to correctly block trading.
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
                print(f"  Running AI analysis on top {len(candidate_markets)} candidates...")
                ai_results = {}

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
                    ai_results = batch_analyze(
                        candidate_markets,
                        max_markets=3,
                        delay=2,
                        per_market_timeout=90,
                    )
                    # Warn if any result came from the paid API fallback
                    for r in ai_results.values():
                        if r.get('source') == 'deepseek_api':
                            print("  Warning: AI fallback to DeepSeek API was used — Ollama may be down. Check server.")
                            break
                except TimeoutError as e:
                    print(f"  Warning: AI analysis timed out ({e}), continuing without AI scores")
                    # Restore pre-AI confidence scores to prevent partially-blended
                    # confidences from being written to schema_version=1 output.
                    for rec in recommendations:
                        if rec['market_id'] in pre_ai_confidence:
                            rec['confidence'] = pre_ai_confidence[rec['market_id']]
                    ai_results = {}
                except Exception as e:
                    print(f"  Warning: AI analysis failed ({e}), continuing without AI scores")
                    # Restore pre-AI confidence scores to prevent partially-blended
                    # confidences from being written to schema_version=1 output.
                    for rec in recommendations:
                        if rec['market_id'] in pre_ai_confidence:
                            rec['confidence'] = pre_ai_confidence[rec['market_id']]
                    ai_results = {}

                for rec in recommendations:
                    ai = ai_results.get(rec['market_id'])
                    is_ai_candidate = rec['market_id'] in top_candidate_ids

                    # ai_was_candidate: was this market sent to AI at all?
                    # ai_returned_skip: AI ran but said SKIP (vs not in top 5)
                    rec['ai_was_candidate'] = is_ai_candidate
                    rec['ai_returned_skip'] = False

                    if not is_ai_candidate:
                        # Market was never sent to AI — use None (not 'SKIP') to avoid
                        # conflating "AI considered and skipped" with "never analyzed".
                        rec['ai_recommendation'] = None
                        rec['ai_confidence'] = 0.0
                        rec['ai_reasoning'] = ''
                        rec['ai_source'] = None
                        continue

                    # Market was a candidate — check what AI returned.
                    if not ai or ai['recommendation'] == 'SKIP':
                        # AI was invoked for this market and returned SKIP (or failed to
                        # return a usable result, which we also treat as SKIP).
                        rec['ai_recommendation'] = 'SKIP'
                        rec['ai_confidence'] = 0.0
                        rec['ai_reasoning'] = ''
                        rec['ai_source'] = None
                        rec['ai_returned_skip'] = True
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
                        # AI disagrees — scale the confidence multiplier by AI confidence.
                        # Formula: confidence_multiplier = 0.4 + (1 - ai_conf) * 0.4
                        #   ai_conf=1.0 → confidence_multiplier=0.40 (AI certain → hardest reduction, keeps 40% of stat score)
                        #   ai_conf=0.0 → confidence_multiplier=0.80 (AI uncertain → softest reduction, keeps 80% of stat score)
                        # Clamp ai_conf to [0.0, 1.0] to guard against malformed AI responses.
                        ai_conf = max(0.0, min(1.0, ai['confidence']))
                        confidence_multiplier = 0.4 + (1 - ai_conf) * 0.4
                        rec['confidence'] = max(0.0, round(stat_conf * confidence_multiplier, 3))
                    # if ai returned something unexpected, leave confidence unchanged

            # Re-sort after AI pass
            recommendations.sort(key=lambda x: x['confidence'], reverse=True)

            print(f"  Found {len(recommendations)} trading opportunities")
            return recommendations

        except Exception as e:
            print(f"  Error analyzing markets: {e}")
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

    # Skip research if at max positions (now 10, increased from 5)
    open_positions = sum(
        1 for trades in researcher.trader.positions.values()
        if any(t.get('status') == 'OPEN' for t in (trades if isinstance(trades, list) else [trades]))
    )
    max_positions = 10  # Increased from 5 to match auto_trader.py
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
