#!/usr/bin/env python3
"""
Regression tests for the unverifiable / thin-vanity market filter.

Motivating trade (CEIUnpQL26, 2026-04-20):
  question: "Will I complete all my pressing tasks this week?"
  category: None (inferred as 'other')
  unique_bettors: 2
  signals: ['probability_direction']  (momentum only)
  AI reasoning: hallucinated connection to "pet content trends"

Lessons:
  1. Personal/subjective questions ("Will I ...", "Will my ...") must be
     blocked at the research layer — AI can hallucinate itself into
     betting on them otherwise.
  2. The "vanity market" profile (other + momentum-only + thin bettors)
     is a distinct compound signal worth catching independently.

This test suite locks in both filters at their entry points and at the
trader backstop layer.
"""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from manifold_bot.strategies import (
    is_unverifiable_market,
    is_thin_other_momentum_market,
)


# ── is_unverifiable_market ───────────────────────────────────────────────────

class TestIsUnverifiableMarket(unittest.TestCase):
    def test_exact_reproducer_blocked(self):
        # The literal CEIUnpQL26 question — must always be blocked.
        self.assertTrue(
            is_unverifiable_market({
                'question': "Will I complete all my pressing tasks this week?",
            })
        )

    def test_will_i_variants_blocked(self):
        for q in [
            "Will I finish my thesis by June?",
            "Will I lose 5 pounds this month?",
            "Will I get promoted at work?",
        ]:
            with self.subTest(q=q):
                self.assertTrue(is_unverifiable_market({'question': q}))

    def test_will_my_variants_blocked(self):
        for q in [
            "Will my car survive another year?",
            "Will my cat still be alive next Christmas?",
            "Will my startup raise a Series A?",
        ]:
            with self.subTest(q=q):
                self.assertTrue(is_unverifiable_market({'question': q}))

    def test_other_personal_prefixes_blocked(self):
        for q in [
            "Do I have more than 10 unread emails tomorrow?",
            "Can I run a 5k in under 25 minutes by May?",
            "Should I quit my job?",
            "Am I going to finish this book?",
            "Could I bench 200lb by the end of 2026?",
            "Have I been productive this quarter?",
            "Did I meet my savings goal?",
            "Does my gym routine hit 5 days this week?",
        ]:
            with self.subTest(q=q):
                self.assertTrue(is_unverifiable_market({'question': q}))

    def test_public_markets_allowed(self):
        # These are all legitimate, publicly verifiable markets — must NOT
        # be blocked. If any start-with a personal prefix word they should
        # still be allowed as long as the structure isn't "Will <pronoun> …".
        for q in [
            "Will the Fed cut rates in May?",
            "Will Apple announce a new iPhone before September?",
            "Will Taylor Swift perform at the Super Bowl?",
            "Will Bitcoin close above $100k by end of Q2?",
            "Will Manchester United win the Premier League?",
            # Deliberately tricky — "will my" isn't in prefix position
            "Will the report be ready so my team can review?",
            # Contains "I" but not at prefix
            "By 2030, will AGI arrive? I think yes.",
        ]:
            with self.subTest(q=q):
                self.assertFalse(is_unverifiable_market({'question': q}))

    def test_empty_and_missing_question(self):
        self.assertFalse(is_unverifiable_market({}))
        self.assertFalse(is_unverifiable_market({'question': None}))
        self.assertFalse(is_unverifiable_market({'question': ''}))

    def test_case_insensitive(self):
        # Manifold questions can have any casing — filter must be robust.
        self.assertTrue(
            is_unverifiable_market({'question': "WILL I win the lottery?"})
        )
        self.assertTrue(
            is_unverifiable_market({'question': "will i get hired?"})
        )

    def test_leading_whitespace_tolerated(self):
        self.assertTrue(
            is_unverifiable_market({'question': "   Will I finish by Friday?"})
        )

    def test_punctuation_directly_after_pronoun_is_caught(self):
        # Reviewer fix (Low): the old startswith('will i ') check required
        # a trailing space, so these personal questions slipped through.
        # The regex now uses \b, which matches punctuation transitions too.
        for q in [
            "Will I, as CEO, step down by June?",
            "Should I, after all, quit my job?",
            "Can I?",
            "Should I?",
            "Did I, in retrospect, make the right call?",
            "Will my.",   # contrived but legal input
        ]:
            with self.subTest(q=q):
                self.assertTrue(
                    is_unverifiable_market({'question': q}),
                    f"expected block on: {q!r}",
                )

    def test_word_boundary_prevents_false_positive(self):
        # \b must prevent matches where the 'i' is the start of a longer
        # word like "island" or "iceberg" — i.e. "Will island..." is a
        # legitimate public question, not personal.
        for q in [
            "Will island nations lose coastline by 2050?",
            "Will iceberg A-76 fully melt by 2030?",
            "Will ice sheet coverage drop below 10M sqkm?",
            "Do iguanas outlive cats on average?",
            "Will myriad changes to the tax code pass?",  # "will myriad" ≠ "will my"
        ]:
            with self.subTest(q=q):
                self.assertFalse(
                    is_unverifiable_market({'question': q}),
                    f"unexpected block on: {q!r}",
                )


# ── is_thin_other_momentum_market ────────────────────────────────────────────

class TestIsThinOtherMomentumMarket(unittest.TestCase):
    def test_exact_reproducer_blocked(self):
        # The CEIUnpQL26 profile — fires on all three legs independently.
        self.assertTrue(
            is_thin_other_momentum_market(
                market={'category': 'other', 'uniqueBettorCount': 2},
                signals=['probability_direction'],
            )
        )

    def test_ai_analysis_tag_does_not_save_it(self):
        # Research pipeline appends 'ai_analysis' as a post-hoc tag. Must
        # be stripped before evaluating the stat-signal shape.
        self.assertTrue(
            is_thin_other_momentum_market(
                market={'category': 'other', 'uniqueBettorCount': 2},
                signals=['probability_direction', 'ai_analysis'],
            )
        )

    def test_empty_category_treated_as_other(self):
        for cat in ('', None, 'unknown'):
            with self.subTest(category=cat):
                self.assertTrue(
                    is_thin_other_momentum_market(
                        market={'category': cat, 'uniqueBettorCount': 2},
                        signals=['probability_direction'],
                    )
                )

    def test_enough_bettors_allowed(self):
        # 3+ bettors → not thin enough to block. Legitimate early-stage
        # markets need room to grow.
        self.assertFalse(
            is_thin_other_momentum_market(
                market={'category': 'other', 'uniqueBettorCount': 3},
                signals=['probability_direction'],
            )
        )
        self.assertFalse(
            is_thin_other_momentum_market(
                market={'category': 'other', 'uniqueBettorCount': 20},
                signals=['probability_direction'],
            )
        )

    def test_multi_strategy_confirmation_allowed(self):
        # If more than one stat strategy fires, it's not the vanity profile.
        self.assertFalse(
            is_thin_other_momentum_market(
                market={'category': 'other', 'uniqueBettorCount': 2},
                signals=['probability_direction', 'mean_reversion'],
            )
        )

    def test_missing_category_falls_back_to_question_inference(self):
        # Reviewer fix (Medium, round 1): when the raw API payload OMITS
        # `category`, the filter infers from question text — same path
        # auto_research uses when stamping the recommendation.
        for q in [
            "Will Congress pass the budget this week?",   # → politics
            "Will the president sign the executive order?",  # → politics
            "Will Bitcoin close above $100k by end of Q2?",  # → crypto
        ]:
            with self.subTest(q=q):
                self.assertFalse(
                    is_thin_other_momentum_market(
                        # No 'category' key — simulates raw API payload.
                        market={'question': q, 'uniqueBettorCount': 2},
                        signals=['probability_direction'],
                    ),
                    f"unexpected block on inferred-category market: {q!r}",
                )

    def test_raw_other_category_also_reinfers_from_question(self):
        # Reviewer fix (Medium, round 2): when the raw payload EXPLICITLY
        # carries category='other', we still re-infer from question text.
        # auto_research.py unconditionally overwrites the raw category via
        # _infer_market_category when stamping the recommendation; the
        # filter must match that canonicalisation or it will block legit
        # public markets that Manifold happens to tag 'other'.
        for q in [
            "Will Congress pass the budget this week?",
            "Will the president sign the executive order?",
            "Will Bitcoin close above $100k by end of Q2?",
        ]:
            with self.subTest(q=q):
                self.assertFalse(
                    is_thin_other_momentum_market(
                        market={
                            'question': q,
                            'category': 'other',   # raw API says 'other'
                            'uniqueBettorCount': 2,
                        },
                        signals=['probability_direction'],
                    ),
                    f"raw 'other' must re-infer; failed on: {q!r}",
                )

    def test_raw_other_stays_blocked_when_inference_also_returns_other(self):
        # Sanity: re-inference must not make the filter toothless. A raw
        # 'other' category combined with an uncategorisable question
        # (inference also returns 'other') remains blocked when the other
        # vanity signals are present.
        self.assertTrue(
            is_thin_other_momentum_market(
                market={
                    'question': 'Will we get 5 likes on this dumb thing?',
                    'category': 'other',
                    'uniqueBettorCount': 2,
                },
                signals=['probability_direction'],
            )
        )

    def test_genuinely_uncategorisable_still_blocks(self):
        # Sanity: when the question can't be inferred into any known
        # category AND all other vanity signals are present, we still
        # block. Filter shouldn't become toothless from the fallback.
        self.assertTrue(
            is_thin_other_momentum_market(
                market={
                    'question': 'Will we get 5 likes on this dumb thing?',
                    'uniqueBettorCount': 2,
                },
                signals=['probability_direction'],
            )
        )

    def test_known_category_allowed(self):
        # Any recognised category bypasses this gate — thin-bettor markets
        # in politics/sports/etc. are a different concern (liquidity, stale).
        for cat in ('politics', 'sports', 'crypto', 'ai_tech'):
            with self.subTest(category=cat):
                self.assertFalse(
                    is_thin_other_momentum_market(
                        market={'category': cat, 'uniqueBettorCount': 2},
                        signals=['probability_direction'],
                    )
                )

    def test_missing_bettors_treated_as_thin(self):
        # Defensive: if uniqueBettorCount is None/missing, treat as zero
        # bettors → blocked. Better to miss than to trade on unknown.
        self.assertTrue(
            is_thin_other_momentum_market(
                market={'category': 'other'},
                signals=['probability_direction'],
            )
        )

    def test_custom_min_bettors(self):
        # Callers can tune the threshold — default is 3; we might move to
        # 5 after a week of observation.
        self.assertTrue(
            is_thin_other_momentum_market(
                market={'category': 'other', 'uniqueBettorCount': 4},
                signals=['probability_direction'],
                min_bettors=5,
            )
        )


# ── Trader backstop integration ──────────────────────────────────────────────

class TestTraderBackstop(unittest.TestCase):
    """Even if the research layer misses (stale market_research.json, or a
    producer change), the trader must still reject these markets with
    reason='market_unverifiable'."""

    def _make_trader(self):
        # Minimal AutoTrader — we only need _trade_rejection_reason to run.
        # Avoid hitting the real state file.
        import automation.auto_trader as at
        trader = at.AutoTrader.__new__(at.AutoTrader)
        trader.min_confidence = 0.65
        trader.max_positions = 10

        # Stub the underlying PaperTrader enough that the other gates don't fire.
        class StubPaperTrader:
            positions = {}
            balance = 100.0
        trader.trader = StubPaperTrader()
        return trader

    def _base_rec(self, **overrides):
        base = {
            'market_id': 'xyz',
            'recommendation': 'YES',
            'confidence': 0.80,
            'probability': 0.5,
            'liquidity': 1000,
            'ai_status': 'agree',
            'strategies': ['probability_direction'],
            'question': 'A fine public question',
            'category': 'politics',
            'unique_bettors': 20,
        }
        base.update(overrides)
        return base

    def test_trader_rejects_unverifiable(self):
        trader = self._make_trader()
        rec = self._base_rec(
            question="Will I complete all my pressing tasks this week?",
            category='other',
            unique_bettors=2,
        )
        reason = trader._trade_rejection_reason(rec['market_id'], rec)
        self.assertEqual(reason, 'market_unverifiable')

    def test_trader_rejects_thin_vanity_profile(self):
        trader = self._make_trader()
        rec = self._base_rec(
            question="Will the board approve next quarter budget?",  # publicly fine
            category='other',
            unique_bettors=2,
            strategies=['probability_direction'],
        )
        reason = trader._trade_rejection_reason(rec['market_id'], rec)
        self.assertEqual(reason, 'market_unverifiable')

    def test_trader_allows_healthy_rec(self):
        trader = self._make_trader()
        rec = self._base_rec()
        reason = trader._trade_rejection_reason(rec['market_id'], rec)
        self.assertIsNone(reason)

    def test_trader_allows_thin_but_known_category(self):
        # A thin-bettor market in a known category should not be rejected
        # by the vanity-profile gate (may still be rejected by other
        # gates — liquidity, stale — which we stubbed out above).
        trader = self._make_trader()
        rec = self._base_rec(
            category='politics',
            unique_bettors=2,
            strategies=['probability_direction'],
        )
        reason = trader._trade_rejection_reason(rec['market_id'], rec)
        self.assertIsNone(reason)


if __name__ == '__main__':
    unittest.main()
