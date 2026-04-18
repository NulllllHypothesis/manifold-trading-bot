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

# Add project root to path so manifold_bot package is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from manifold_bot.manifold_api import api_client
from manifold_bot.paper_trader import PaperTrader
from manifold_bot.strategies import TradingStrategies
from manifold_bot.config import MIN_BET_AMOUNT, MAX_BET_AMOUNT, MIN_CONFIDENCE, MAX_POSITIONS, MAX_POSITIONS_PER_CATEGORY, MIN_LIQUIDITY
from manifold_bot.strategies import _infer_market_category
from automation.send_telegram import send_message as _tg

# Minimum resolved trades per category before adaptive cap kicks in (mirrors
# MIN_SAMPLES_PER_STRATEGY from weekly_ev_report.py but for categories).
_MIN_SAMPLES_PER_CATEGORY = 8


# ── Op3: trader-side observability counters ──────────────────────────────────
# Mirrors the research-side counters in auto_research.py.  Every recommendation
# that comes through run_trading_cycle() lands in exactly one of the rejection
# buckets below, or in 'passed'.  The end-of-run print is the fastest way to
# see why the book isn't turning over (max_positions vs category_cap vs
# ai_veto is a very different diagnosis).
_TRADER_COUNTERS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "trader_counters.jsonl",
)


def _new_trader_counters() -> dict:
    return {
        "timestamp":                None,   # set at run start
        "recommendations_loaded":   0,
        "rejected": {
            "ai_veto":              0,
            "low_confidence":       0,
            "existing_open":        0,
            "max_positions":        0,
            "category_cap":         0,
            "position_class_full":  0,      # V2 Phase 2.1: term/resolvability class cap
            "liquidity":            0,
            "kelly_no_edge":        0,
            "size_too_small":       0,
            "market_unverifiable":  0,
        },
        "passed_filter":            0,      # survived should_trade_market
        "top_ev_at_exec": [],               # top 5 ranked (market_id, ev_exec)
        "trades_executed":          0,
    }


# V2 Phase 2.1: slot caps by position_class.
# Three buckets for a 10-slot book. Favors short-term markets because they
# produce learning data faster. Total = 5 + 3 + 2 = 10 (matches MAX_POSITIONS).
_POSITION_CLASS_CAPS = {
    'short':             5,  # any short-term market (≤7 days)
    'medium':            3,  # any medium-term market (8-30 days)
    'long_or_uncertain': 2,  # long-term or unknown-term — tightest cap because
                             # capital sits longest and abandonment risk is highest
}


# Intermediate revisions of feature/v2-ai-smarter persisted position_class
# values from an earlier 4-bucket design. Map those to the current 3-bucket
# model on read so old-format positions don't become orphans that consume
# no cap. Once all such positions resolve, this map can be removed.
_OBSOLETE_POSITION_CLASS_MIGRATIONS = {
    'long_reliable': 'long_or_uncertain',  # merged: long + high resolvability
    'long_risky':    'long_or_uncertain',  # merged: long + medium/low resolvability
    'long_high':     'long_or_uncertain',  # even earlier name (pre-rename)
    'long_mid_low':  'long_or_uncertain',  # even earlier name (pre-rename)
}


def _normalize_position_class(pclass: Optional[str]) -> Optional[str]:
    """
    Map obsolete position_class values from intermediate branch revisions to
    the current 3-class model. Unknown or None values pass through unchanged.
    """
    if not pclass:
        return pclass
    return _OBSOLETE_POSITION_CLASS_MIGRATIONS.get(pclass, pclass)


def _count_open_in_class(positions: dict, target_class: str) -> int:
    """
    Count OPEN positions whose position_class matches target_class.

    Four cases, in order:
    1. Position has persisted position_class matching a known current class
       → count it (fast path).
    2. Position has an OBSOLETE persisted class (long_reliable, long_risky,
       long_high, long_mid_low from intermediate branch revisions) → normalize
       to the current 3-class model, then count.
    3. Position is legacy with no position_class but has enough metadata
       (close_time_ms or question or category) to re-classify → classify
       on the fly and count.
    4. Position is truly metadata-poor (no position_class, no close_time_ms,
       no question, no category) → **not counted against any class cap**.
       Rationale: if we can't classify, it would be dishonest to silently
       dump it into one arbitrary bucket. Such positions still count against
       MAX_POSITIONS (the global total cap), so the bot never exceeds book
       size — it just won't let legacy mystery positions prevent new trades
       in any specific class.

    Cases 2 and 4 are only relevant during V2 transition. Once legacy
    positions resolve or are backfilled, both paths go to zero.
    """
    # Local import: strategies doesn't import from automation, so this avoids
    # any circular import risk at module load time.
    from manifold_bot.strategies import classify_position

    n = 0
    for pos_list in positions.values():
        for pos in pos_list:
            if pos.get('status') != 'OPEN':
                continue
            pclass = _normalize_position_class(pos.get('position_class'))
            if not pclass:
                # No persisted class — check if we can classify from metadata
                has_close_time = pos.get('close_time_ms') is not None
                has_question   = bool(pos.get('question'))
                has_category   = bool(pos.get('category'))
                if not (has_close_time or has_question or has_category):
                    # Truly metadata-poor legacy position — skip class accounting
                    continue
                synthetic = {
                    'question':          pos.get('question', ''),
                    'category':          pos.get('category'),
                    'closeTime':         pos.get('close_time_ms'),
                    'totalLiquidity':    pos.get('liquidity', 0),
                    'uniqueBettorCount': pos.get('unique_bettors', 0),
                }
                pclass = classify_position(synthetic)['position_class']
            if pclass == target_class:
                n += 1
    return n


def _print_trader_counters(counters: dict) -> None:
    print()
    print("  ── Trader observability (Op3) ───────────────────────────────")
    print(f"    Recommendations loaded : {counters['recommendations_loaded']}")
    print(f"    Rejections:")
    for reason, n in counters['rejected'].items():
        if n:
            print(f"      {reason:<22} {n:>4}")
    print(f"    Passed should_trade    : {counters['passed_filter']}")
    print(f"    Trades executed        : {counters['trades_executed']}")
    if counters['top_ev_at_exec']:
        print(f"    Top EV-ranked (exec):")
        for mid, ev in counters['top_ev_at_exec'][:5]:
            print(f"      {mid:<24} ${ev:+.2f}")
    print("  ─────────────────────────────────────────────────────────────")


def _append_trader_counters(counters: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_TRADER_COUNTERS_PATH), exist_ok=True)
        with open(_TRADER_COUNTERS_PATH, "a") as f:
            f.write(json.dumps(counters) + "\n")
    except Exception as e:
        print(f"  Warning: could not append trader counters ({e})")


def _load_weights_file(root: str) -> tuple[dict, dict, int, int]:
    """Load strategy_weights.json. Returns (weights, by_category, sample_count, min_threshold)."""
    path = os.path.join(root, "data", "strategy_weights.json")
    try:
        with open(path) as f:
            d = json.load(f)
        return d.get("weights", {}), d.get("by_category", {}), d.get("sample_count", 0), d.get("min_samples_threshold", 10)
    except Exception:
        return {}, {}, 0, 10


def _stat_derived_probability(
    confidence: float,
    recommendation_direction: str | None,
    current_prob: float,
) -> float | None:
    """
    Stat-derived probability estimate used when AI doesn't provide one.

    The bot's confidence represents how strongly it believes the direction is
    correct, NOT the probability of the YES outcome. We map it to a probability
    using the recommendation direction:

        direction='YES' → our estimate of P(YES) = confidence
                          (we think YES is `confidence`-likely to happen)
        direction='NO'  → our estimate of P(YES) = 1 - confidence
                          (we think YES is only (1-confidence)-likely to happen)

    We intentionally DON'T apply calibration bias adjustments here — that would
    double-count against adjustments already baked into the strategies. The
    estimate is a deliberately simple read of what the bot's confidence means.

    This gets us off the "estimated_ev is always null" floor. It's not as good
    as a real probability estimate from an LLM, but it's better than nothing —
    and importantly it lets the EV calibration feedback loop actually collect
    data (weekly_ev_report.py needs a value to regress against).

    Guards:
    - confidence must be in (0, 1) to produce meaningful estimate
    - direction must be YES or NO (other values → None, no estimate)
    - Result is clamped to (0.01, 0.99) to avoid zero-edge-zero-payout cases
    - current_prob is accepted but not used (could be used for bias-aware
      variants in a future iteration)
    """
    if not isinstance(confidence, (int, float)) or not 0 < confidence < 1:
        return None
    if recommendation_direction not in ('YES', 'NO'):
        return None
    del current_prob  # reserved for future bias-aware extension
    p_yes = float(confidence) if recommendation_direction == 'YES' else 1.0 - float(confidence)
    return max(0.01, min(0.99, p_yes))


def _load_category_accuracy_file(root: str) -> dict:
    """Load category_accuracy.json. Returns {category: {accuracy, sample_count}} or {}."""
    path = os.path.join(root, "data", "category_accuracy.json")
    try:
        with open(path) as f:
            return json.load(f).get("categories", {})
    except Exception:
        return {}


class AutoTrader:
    """Automated trading with risk management"""

    def __init__(self):
        _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.research_file = os.path.join(_root, "market_research.json")
        self.trade_log_file = os.path.join(_root, "auto_trades.json")
        self.trader = PaperTrader()  # Will auto-load state from __init__

        # Risk management parameters
        self.max_positions = MAX_POSITIONS
        self.max_position_size = 0.1  # 10% of balance per trade
        self.min_confidence = MIN_CONFIDENCE

        # Phase B: strategy reliability weights — scale Kelly fraction per strategy
        self._strategy_weights, self._by_category_weights, self._weights_sample_count, _ = _load_weights_file(_root)

        # Phase C: category accuracy — drive adaptive exposure caps
        self._category_accuracy = _load_category_accuracy_file(_root)

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

    def _get_strategy_weight(self, strategies: list, category: str = "other") -> float:
        """
        Return the effective strategy weight for a recommendation.

        Fallback chain per strategy:
          1. by_category[category][strategy]  — most specific, needs enough category trades
          2. global weights[strategy]         — cross-category baseline
          3. 1.0 default

        Uses the maximum weight across all strategies that fired — the strongest
        reliable signal drives sizing; we don't average down good signals with
        neutral ones.
        """
        global_weights = getattr(self, '_strategy_weights', {})
        by_cat = getattr(self, '_by_category_weights', {})
        cat_weights = by_cat.get(category, {})

        if not strategies:
            return 1.0

        return max(
            (cat_weights[s].get("weight", 1.0) if s in cat_weights else global_weights.get(s, 1.0)
             for s in strategies),
            default=1.0,
        )

    def _effective_category_cap(self, category: str) -> int:
        """
        Return the position cap for a category, reduced to 1 if own trade history
        shows < 50% directional accuracy with sufficient sample size.

        "other" is always uncapped (uses max_positions) because it is a catch-all
        bucket: positions in it are uncorrelated by construction, so the standard
        per-category concentration limit is not meaningful here.

        Phase C: adaptive caps based on per-category resolved trade history.
        Below _MIN_SAMPLES_PER_CATEGORY the default cap (MAX_POSITIONS_PER_CATEGORY)
        is always used — never penalise a category on too-small a sample.
        """
        if category == 'other':
            return self.max_positions

        cat_accuracy = getattr(self, '_category_accuracy', {})
        info = cat_accuracy.get(category)
        if info is None:
            return MAX_POSITIONS_PER_CATEGORY
        if info.get("sample_count", 0) < _MIN_SAMPLES_PER_CATEGORY:
            return MAX_POSITIONS_PER_CATEGORY
        if info.get("accuracy", 1.0) < 0.50:
            return 1
        return MAX_POSITIONS_PER_CATEGORY

    def _trade_rejection_reason(self, market_id: str, recommendation: Dict) -> Optional[str]:
        """
        Run every risk gate and return a short rejection reason string, or
        None when the market passes all filters.  Used by run_trading_cycle
        to count rejections by reason (Op3 observability).

        The returned reason strings match the keys in
        _new_trader_counters()['rejected'] exactly.
        """
        # AI veto — only when AI EXPLICITLY said "skip" after analyzing the market.
        # This is different from "AI failed to produce a result" (timeout / parse error).
        # Previously those two states collapsed together and both triggered a veto, which
        # meant AI outages silently blocked stat-only trades. Now we check ai_status as
        # the authoritative field:
        #
        #   ai_status='skip'       → REAL AI rejection. Veto.
        #   ai_status='no_result'  → AI failed. Fall through to stat-only trading.
        #   ai_status='not_run'    → Market wasn't in top-3 AI candidates. No AI signal.
        #                             Trade on stat alone.
        #   ai_status='agree'      → AI confirmed stat signal. Trade.
        #   ai_status='disagree'   → AI disagreed; confidence already scaled down in research.
        #                             Trade if confidence still clears floor.
        #
        # Backward compatibility: old recommendations without ai_status use ai_returned_skip
        # (which for historical records reflected the buggy "no_result becomes skip" logic).
        # We only treat it as veto if ai_recommendation is explicitly 'SKIP' and there's no
        # ai_status field present — otherwise trust ai_status.
        ai_status = recommendation.get('ai_status')
        if ai_status == 'skip':
            print(f"  AI vetoed this market (ai_status=skip) — skipping regardless of confidence")
            return "ai_veto"
        if ai_status is None:
            # Legacy record (pre-ai_status). In the old producer, BOTH "AI no_result"
            # and "real AI SKIP" produced identical output shape:
            #   ai_recommendation='SKIP', ai_returned_skip=True, ai_confidence=0.0,
            #   ai_reasoning='', ai_source=None
            # They are literally indistinguishable in stored market_research.json.
            #
            # We can't safely tell them apart, so we default to the SAFE choice:
            # veto any legacy SKIP record. This means a small transition cost —
            # the first hourly research run after this PR lands will rewrite
            # market_research.json with ai_status fields, and subsequent trader
            # runs will correctly distinguish no_result from skip.
            #
            # For the ~1 hour before that first new research run completes, the
            # trader continues to veto legacy SKIP records. Better to miss a
            # trade than to trade on a market the AI genuinely rejected.
            if recommendation.get('ai_recommendation') == 'SKIP':
                print(f"  AI vetoed this market (legacy SKIP record — pre-ai_status) — skipping")
                return "ai_veto"

        # Check confidence threshold
        if recommendation['confidence'] < self.min_confidence:
            print(f"  Confidence too low: {recommendation['confidence']*100:.0f}% < {self.min_confidence*100:.0f}%")
            return "low_confidence"

        # Block any new bet on a market that already has an OPEN position.
        # Previously the bot allowed "adding to position" at confidence >= 0.80,
        # which led to three consecutive losing bets on the same market (2czul2Rync).
        # Use the swap mechanism instead of stacking bets on a single market.
        market_positions = self.trader.positions.get(market_id, [])
        has_open = any(p.get('status') == 'OPEN' for p in market_positions)
        if has_open:
            print(f"  Already have an OPEN position in this market — use swap to replace it")
            return "existing_open"

        # Check max positions limit
        open_positions = sum(1 for pos_list in self.trader.positions.values()
                           for pos in pos_list if pos.get('status') == 'OPEN')

        if open_positions >= self.max_positions:
            print(f"  Max positions reached ({open_positions}/{self.max_positions})")
            return "max_positions"

        # V2 Phase 2.1 — Check position_class cap (term × resolvability).
        # Biases the book toward fast-resolving reliable markets so the learning
        # loop gets fed faster. Long-term risky markets get at most 1 slot
        # because they tie up capital longest with highest abandonment risk.
        position_class = recommendation.get('position_class')
        if position_class and position_class in _POSITION_CLASS_CAPS:
            cap = _POSITION_CLASS_CAPS[position_class]
            open_in_class = _count_open_in_class(self.trader.positions, position_class)
            if open_in_class >= cap:
                print(f"  Position class full: {position_class} has {open_in_class}/{cap} open positions")
                return "position_class_full"

        # Check category exposure cap — prevent over-concentration in one topic.
        # Category comes from the research recommendation; fall back to inference
        # from the question text when not present (e.g. manual test calls).
        rec_category = recommendation.get('category') or _infer_market_category(
            recommendation.get('question', '')
        )
        open_in_category = sum(
            1
            for pos_list in self.trader.positions.values()
            for pos in pos_list
            if pos.get('status') == 'OPEN'
            # Skip positions with no category AND no question — they predate this
            # feature and we cannot determine their category. Counting them as
            # 'other' (the _infer_market_category('') fallback) would incorrectly
            # fill the cap and block all new 'other' trades.
            and (pos.get('category') or pos.get('question'))
            and (pos.get('category') or _infer_market_category(pos.get('question', ''))) == rec_category
        )
        effective_cap = self._effective_category_cap(rec_category)
        if open_in_category >= effective_cap:
            print(f"  Category cap reached: {rec_category} has {open_in_category}/{effective_cap} open positions")
            return "category_cap"

        # Check market liquidity — threshold matches auto_research.py (MIN_LIQUIDITY).
        if recommendation.get('liquidity', 0) < MIN_LIQUIDITY:
            print(f"  Liquidity too low: ${recommendation.get('liquidity', 0):.0f}")
            return "liquidity"

        return None

    def should_trade_market(self, market_id: str, recommendation: Dict) -> bool:
        """
        Check if we should trade a specific market based on risk rules.

        Returns True when the market passes every gate. This is a thin bool
        wrapper around _trade_rejection_reason() so that Op3 observability
        can also see the specific rejection reason without requiring every
        caller (tests included) to handle a string return type.
        """
        return self._trade_rejection_reason(market_id, recommendation) is None

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

            # Phase B: scale half-Kelly by strategy reliability weight.
            # weight range 0.5–1.2 → Kelly multiplier 25%–60%.
            # Weights below min-sample threshold are stored as 1.0, so the default
            # half-Kelly (50%) applies automatically until data accumulates.
            # Phase C: use category-specific weight when available (by_category fallback chain).
            strategies = recommendation.get('strategies', [])
            category = recommendation.get('category') or _infer_market_category(
                recommendation.get('question', '')
            )
            weight = self._get_strategy_weight(strategies, category)
            kelly_fraction *= 0.5 * weight
            position_size = self.trader.balance * kelly_fraction
            sizing_method = f"kelly(ai_prob={ai_estimated_prob:.2f}, net_odds={net_odds:.2f}, k={kelly_fraction:.3f}, w={weight:.2f})"
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

        # Verify market exists, is open, and get current probability.
        # If the market can't be fetched, abort — never bet on an unverifiable market.
        try:
            market = api_client.get_market(market_id)
            if market.get('isResolved'):
                print(f"  Market is already resolved — skipping")
                return False
            close_time_ms = market.get('closeTime', 9e12)
            if close_time_ms < time.time() * 1000:
                print(f"  Market is closed — skipping")
                return False
            current_prob = market.get('probability', recommendation['probability'])
        except Exception as e:
            print(f"  Could not verify market exists ({e}) — skipping to avoid phantom bet")
            return False

        # Calculate position size (Kelly when AI estimate available, else confidence-scaled)
        amount = self.calculate_position_size(recommendation, outcome)

        # A return value of 0.0 means no edge or size too small — skip the trade entirely
        # rather than placing a minimum bet that contradicts the model's signal.
        if amount == 0.0:
            print(f"  ⏭️  Trade skipped: no edge or position size too small")
            return False

        print(f"  Position size: ${amount:.2f} ({amount/self.trader.balance*100:.1f}% of balance)")

        # Compute estimated EV before placing the bet — it is deterministic given
        # current_prob, ai_estimated_probability (or stat fallback), outcome, and amount.
        # Passing it into place_paper_bet ensures the position record in
        # paper_trading_state.json has a non-null ev, so resolve_market() writes
        # it correctly to bet_outcomes for calibration.
        #
        # Probability estimate priority:
        #   1. AI estimate (ai_estimated_probability) if present
        #   2. Stat-derived fallback: map confidence to probability using the bot's
        #      recommendation direction. This ensures EV is NEVER null — previously
        #      AI failures meant calibration got zero data.
        ai_prob = recommendation.get('ai_estimated_probability')
        p_estimate = ai_prob

        if p_estimate is None:
            p_estimate = _stat_derived_probability(
                confidence=confidence,
                recommendation_direction=recommendation.get('recommendation'),
                current_prob=current_prob,
            )

        if p_estimate is not None:
            if outcome == 'YES':
                payout_if_win = amount / current_prob if current_prob > 0 else 0
                win_prob = float(p_estimate)
            else:  # NO
                payout_if_win = amount / (1 - current_prob) if current_prob < 1 else 0
                win_prob = 1.0 - float(p_estimate)
            estimated_ev = win_prob * payout_if_win - amount
        else:
            estimated_ev = None  # truly unknown — don't fake a value

        # Place paper trade — estimated_ev now flows into the position record so
        # resolve_market() → _write_bet_outcome() can store it in bet_outcomes.
        success = self.trader.place_paper_bet(
            market_id=market_id,
            outcome=outcome,
            amount=amount,
            probability=current_prob,
            estimated_ev=estimated_ev,
            ai_confidence=recommendation.get('ai_confidence', confidence),
            ai_estimated_probability=ai_prob,
            strategies=recommendation.get('strategies', []),
            category=recommendation.get('category'),
            question=recommendation.get('question'),
            # V2 Phase 2.1 — persist classification on the position record so
            # _count_open_in_class() reads the exact class the trader approved.
            # Without this, the on-the-fly classify_position() fallback sees
            # no close_time_ms and misclassifies every position as long_risky.
            term=recommendation.get('term'),
            resolvability=recommendation.get('resolvability'),
            position_class=recommendation.get('position_class'),
            close_time_ms=recommendation.get('close_time_ms'),
        )

        if success:
            print(f"  ✅ Trade executed successfully")
            try:
                _tg(
                    f"🎯 *Trade placed: {outcome}*\n"
                    f"_{recommendation['question'][:80]}_\n"
                    f"Prob: {current_prob*100:.1f}% | Size: ${amount:.0f} | Conf: {confidence*100:.0f}%\n"
                    f"Balance: ${self.trader.balance:.2f}"
                )
            except Exception:
                pass

            # Build reasoning
            reasoning_parts = []
            if recommendation.get('strategies'):
                reasoning_parts.append(f"Strategies: {', '.join(recommendation['strategies'])}")
            if recommendation.get('ai_reasoning'):
                reasoning_parts.append(f"AI: {recommendation['ai_reasoning']}")
            reasoning = ' | '.join(reasoning_parts) if reasoning_parts else 'No reasoning provided'

            # Log trade
            strategies = recommendation.get('strategies', [])
            trade_category = recommendation.get('category') or _infer_market_category(
                recommendation.get('question', '')
            )
            self.log_trade({
                'market_id': market_id,
                'question': recommendation['question'],
                'outcome': outcome,
                'amount': amount,
                'probability': current_prob,
                'confidence': confidence,
                'estimated_ev': estimated_ev,
                'timestamp': datetime.now().isoformat(),
                'strategies': strategies,
                'strategy_weight_at_trade_time': self._get_strategy_weight(strategies, trade_category),
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

        # Op3: per-run observability counters
        counters = _new_trader_counters()
        from datetime import timezone
        counters["timestamp"] = datetime.now(timezone.utc).isoformat()

        print(f"Starting balance: ${self.trader.balance:.2f}")
        open_positions = sum(1 for pos_list in self.trader.positions.values()
                                   for pos in pos_list if pos.get('status') == 'OPEN')
        print(f"Open positions: {open_positions}")

        # Load latest research
        research = self.load_latest_research()
        if not research or not research.get('recommendations'):
            print("No research recommendations available")
            _print_trader_counters(counters)
            _append_trader_counters(counters)
            return 0

        recommendations = research['recommendations']
        print(f"Loaded {len(recommendations)} research recommendations")
        counters["recommendations_loaded"] = len(recommendations)

        # Filter and sort recommendations.  Use the reason-returning variant
        # so we can tally rejections by category (Op3).
        tradable_recs = []
        for rec in recommendations:
            reason = self._trade_rejection_reason(rec['market_id'], rec)
            if reason is None:
                tradable_recs.append(rec)
                counters["passed_filter"] += 1
            else:
                counters["rejected"][reason] = counters["rejected"].get(reason, 0) + 1

        if not tradable_recs:
            print("No tradable opportunities after risk filtering")
            _print_trader_counters(counters)
            _append_trader_counters(counters)
            return 0

        # Sort by estimated EV (highest first) — confidence is a gate, not a ranking signal.
        # Op2: prefer estimated_ev_exec (honest stake-size EV) over the $25 ref EV;
        # legacy estimated_ev is the fallback for pre-migration research files.
        def _exec_ev(r):
            ev = r.get('estimated_ev_exec')
            if ev is None:
                ev = r.get('estimated_ev')
            return ev if ev is not None else 0.0

        tradable_recs.sort(key=_exec_ev, reverse=True)

        # Record top-5 ranked recs for observability before we start trimming
        counters["top_ev_at_exec"] = [
            (r.get('market_id', '?'), _exec_ev(r))
            for r in tradable_recs[:5]
        ]

        print(f"\nFound {len(tradable_recs)} tradable opportunities")

        # Execute top 1-2 trades (risk management).
        # IMPORTANT: re-check should_trade_market() before EACH trade, not just
        # once at filter time. The pre-filter above builds a candidate list but
        # placing trade #1 changes the positions dict (adds a new OPEN entry),
        # so trade #2 must re-validate max_positions / category_cap / etc.
        # Without this re-check the bot could exceed MAX_POSITIONS by placing
        # 2 trades in one cycle when it started at MAX-1 open.
        max_trades_per_cycle = 2
        trades_executed = 0

        for rec in tradable_recs[:max_trades_per_cycle]:
            if trades_executed >= max_trades_per_cycle:
                break

            # Re-validate before execution — positions may have changed since
            # the initial filter pass (e.g. trade #1 in this cycle added one).
            recheck = self._trade_rejection_reason(rec['market_id'], rec)
            if recheck is not None:
                print(f"\n  Re-check rejected {rec['market_id'][:14]}: {recheck}")
                continue

            print(f"\n{'─'*40}")
            if self.execute_trade(rec):
                trades_executed += 1

        counters["trades_executed"] = trades_executed

        print(f"\n{'='*60}")
        print(f"Trading cycle complete")
        print(f"Trades executed: {trades_executed}")
        print(f"Ending balance: ${self.trader.balance:.2f}")
        print(f"{'='*60}")

        # Op3: emit counters (best-effort — never abort the cycle)
        try:
            _print_trader_counters(counters)
            _append_trader_counters(counters)
        except Exception as ctr_err:
            print(f"  Warning: trader counter emit failed ({ctr_err})")

        return trades_executed

_FLAG_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "autotrader_disabled.flag")


def main():
    """Main function"""
    if os.path.exists(_FLAG_FILE):
        print("🚫 Auto-trader is DISABLED (autotrader_disabled.flag exists). No trades will be placed.")
        print("   To re-enable: /autotrader on  (or delete autotrader_disabled.flag)")
        return 0

    trader = AutoTrader()
    trades_executed = trader.run_trading_cycle()

    # Return number of trades executed
    return trades_executed

if __name__ == "__main__":
    try:
        num_trades = main()
        sys.exit(0)
    except Exception as e:
        print(f"Fatal error: {e}")
        sys.exit(1)
