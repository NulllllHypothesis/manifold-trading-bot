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
from manifold_bot.strategies import TradingStrategies, _infer_market_category
from manifold_bot.paper_trader import PaperTrader
from manifold_bot.ai_analyzer import batch_analyze
from manifold_bot.config import MIN_CONFIDENCE
from scripts.position_swap_checker import run_swap_check

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STRATEGY_WEIGHTS_PATH = os.path.join(_ROOT, "data", "strategy_weights.json")
_DEFAULT_STRATEGY_WEIGHT = 1.0


def _load_strategy_weights() -> dict:
    """Load strategy weights from data/strategy_weights.json; return defaults on any error."""
    try:
        with open(_STRATEGY_WEIGHTS_PATH) as f:
            data = json.load(f)
        return data.get("weights", {})
    except Exception:
        return {}

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
          ai_was_candidate: bool  — True iff the market was in the top-3 sent to AI.
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

            # Load strategy reliability weights once per run.
            strategy_weights = _load_strategy_weights()

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

                mean_rev_rec = TradingStrategies.mean_reversion_strategy(market)
                if mean_rev_rec:
                    raw_signals.append({'strategy': 'mean_reversion',
                                        'recommendation': mean_rev_rec,
                                        'confidence': 0.68,
                                        'family': 'contrarian'})

                bias_rec = TradingStrategies.probability_bias_strategy(market)
                if bias_rec:
                    raw_signals.append({'strategy': 'probability_bias',
                                        'recommendation': bias_rec,
                                        'confidence': 0.70,
                                        'family': 'contrarian'})

                creator_rec = TradingStrategies.creator_disagreement_strategy(market)
                if creator_rec:
                    raw_signals.append({'strategy': 'creator_disagreement',
                                        'recommendation': creator_rec,
                                        'confidence': 0.75,
                                        'family': 'fundamental'})

                thin_rec = TradingStrategies.thin_market_strategy(market)
                # thin_market is tracked separately — added only as confirmation

                # ── Apply strategy reliability weights ────────────────────────
                # Scale each signal's base confidence by its historical weight.
                # Weights are 0.5–1.2; clamped to [0.0, 1.0] so they can't push
                # a signal above 100% confidence before blending.
                for sig in raw_signals:
                    w = strategy_weights.get(sig['strategy'], _DEFAULT_STRATEGY_WEIGHT)
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

                # thin_market: add only if another signal agrees (confirmation-only)
                if thin_rec:
                    other_agreeing = any(
                        s['recommendation'] == thin_rec
                        for s in active_signals
                        if s['family'] in ('contrarian', 'fundamental')
                    )
                    if other_agreeing:
                        active_signals.append({'strategy': 'thin_market',
                                               'recommendation': thin_rec,
                                               'confidence': 0.65,
                                               'family': 'contrarian'})

                if not active_signals:
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
                    continue  # tied — no clear edge

                # Check if we already have a position
                existing_position = market_id in self.trader.positions

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
                    'strategies': [s['strategy'] for s in active_signals],
                    'priority_boost': round(priority_boost, 3),
                    'existing_position': existing_position,
                    'analyzed_at': datetime.now().isoformat()
                }

                recommendations.append(recommendation)

            # Sort by confidence (highest first) before AI pass
            recommendations.sort(key=lambda x: x['confidence'], reverse=True)

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

            def _composite(r):
                return r['confidence'] + r.get('priority_boost', 0.0) * 0.15

            above_floor.sort(key=_composite, reverse=True)
            below_floor.sort(key=_composite, reverse=True)
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
                        # Market was never sent to AI (not in top-3) — use None (not 'SKIP')
                        # to avoid conflating "AI considered and skipped" with "never analyzed".
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

            # Compute estimated_ev for every recommendation now that ai_estimated_probability
            # is set. This value is consumed by position_swap_checker to rank opportunities
            # and gate swap proposals (requires ev > 0). Uses a fixed reference bet for
            # cross-market comparability; auto_trader.py will recompute with actual size.
            _EV_REF = 25.0
            for rec in recommendations:
                ai_p = rec.get('ai_estimated_probability')
                direction = rec.get('recommendation')
                mkt_p = max(0.01, min(0.99, rec.get('probability', 0.5)))
                if ai_p is not None and direction in ('YES', 'NO'):
                    if direction == 'YES':
                        gross = _EV_REF / mkt_p
                        win_p = float(ai_p)
                    else:
                        gross = _EV_REF / (1.0 - mkt_p)
                        win_p = 1.0 - float(ai_p)
                    rec['estimated_ev'] = round(win_p * gross - _EV_REF, 4)
                else:
                    rec['estimated_ev'] = None

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
