# Example: Full Analysis of One Market

How the bot processes a single market end-to-end, from raw API data to paper trade.

*Every formula, threshold, and gate below is verified against the actual code.
Where the example makes an assumption about data state (e.g. calibration cell
values), it is explicitly marked as "assumption for this walkthrough."*

---

## The Market

```
"Will Nvidia stock close above $200 before June 1, 2026?"

Raw market state:
- Crowd probability: 67%
- 24h volume: $900
- Liquidity: $1,400
- Unique bettors: 42
- Last bet: 6 hours ago
- Time to close: 26 days
- Creator resolutionProbability: 82%
- Current bot balance: $55.28
```

---

## STEP 1 — Category Classification

```
Question contains "Nvidia"
→ category = business

(manifold_bot/strategies.py :: _infer_market_category)
Match order: crypto → ai_tech → gaming → entertainment → politics →
             sports → science → business → economics → other
"nvidia" matches the business keyword list.
```

**Why this matters:**
- Category is used for calibration lookup (probability_bias per-bucket cell)
- Category is used for strategy weight fallback chain
- Category is used for trader position caps (MAX_POSITIONS_PER_CATEGORY = 3)

---

## STEP 2 — Raw Strategy Checks

Each strategy evaluates the market independently.

### Strategy 1: probability_direction (confidence 0.65, family: momentum)

```
Rule: if probability is outside 45–55% AND last bet < 48h AND volume24h ≥ 5

Here:
  67% is outside 45–55%   ✓
  Last bet 6h ago < 48h   ✓
  Volume $900 ≥ $5        ✓
  → fires YES @ 0.65
```

### Strategy 2: mean_reversion (confidence 0.68, family: contrarian)

```
Rule: only fires on extreme prices (>85% or <15%) with < 100 bettors
      and not closing within 7 days

Here:
  67% is NOT extreme (needs >85% or <15%)
  → does NOT fire
```

### Strategy 3: probability_bias (confidence 0.70, family: contrarian)

```
Rule: look up per-(category, bucket) bias from calibration_table.json
      If reliable cell exists (sample_size ≥ 15, reliable=True), use its bias.
      Otherwise fall back to category-level aggregate.
      Fire if |bias| ≥ 0.025 (2.5pp).
      Positive bias → bet NO (crowd overestimates YES)
      Negative bias → bet YES (crowd underestimates YES)

Here:
  Category = business, bucket = 60-70%
  *** ASSUMPTION for this walkthrough: ***
  Suppose the calibration table has a reliable per-bucket cell for
  (business, 60-70%) with bias = +0.050 and sample_size = 25.

  In reality, business is a new v2 category with limited calibration
  data. If no reliable cell exists AND the category aggregate bias is
  below 0.025, probability_bias would NOT fire for this market.
  The example proceeds as if the cell exists to demonstrate the full flow.

  bias = +0.050 ≥ 0.025 threshold ✓
  Positive bias → crowd overestimates YES
  → fires NO @ 0.70
```

### Strategy 4: creator_disagreement (confidence 0.75, family: fundamental)

```
Rule: if resolutionProbability differs from crowd by ≥ 15pp, follow creator

Here:
  Creator says 82%
  Crowd says 67%
  Gap = 82% - 67% = 15pp (exactly at threshold)
  → fires YES @ 0.75
```

*Note: this field is only present on ~5% of Manifold markets. This example
includes it to show the full strategy set, but in typical production runs
creator_disagreement rarely fires.*

### Strategy 5: thin_market (confirmation-only, family: contrarian)

```
Rule: AMM pool imbalance > 70%. Never votes independently — only adds
      +0.03 boost to an existing contrarian/fundamental signal when
      it agrees with the direction.

Here:
  Assume no significant pool imbalance
  → does NOT fire
```

### Strategy 6: volume_spike_priority (filter, not directional)

```
Rule: if 24h volume > 2× batch median, compute priority_boost 0.0–1.0
      This is NOT a YES/NO vote. It only affects candidate ranking.

Here:
  Assume this market is unusually active
  → priority_boost = 0.60
```

### Raw signal summary

```
probability_direction  → YES @ 0.65  (momentum)
probability_bias       → NO  @ 0.70  (contrarian)
creator_disagreement   → YES @ 0.75  (fundamental)
mean_reversion         → (silent)
thin_market            → (silent)
volume_spike           → boost 0.60 only
```

---

## STEP 3 — Strategy Weight Scaling

Each signal's confidence is multiplied by its learned reliability weight
from `data/strategy_weights.json`. Fallback chain:
`by_category[business][strategy]` → `global[strategy]` → `1.0`

```
For this walkthrough, all weights = 1.0 (no resolved trades yet):
  probability_direction  1.0 × 0.65 = 0.65
  probability_bias       1.0 × 0.70 = 0.70
  creator_disagreement   1.0 × 0.75 = 0.75
```

*Once the bot has ≥ 10 resolved trades per strategy, these weights will
diverge — strategies that lose money get weights < 1.0, winners get > 1.0.*

---

## STEP 4 — Family Deduplication

The bot keeps at most ONE winner from each directional family (highest
confidence within the family). This prevents correlated signals from
stacking as independent evidence.

```
momentum    → probability_direction  YES @ 0.65  (only momentum signal)
contrarian  → probability_bias       NO  @ 0.70  (only contrarian signal)
fundamental → creator_disagreement   YES @ 0.75  (only fundamental signal)
filter      → volume_spike           boost only  (not directional)

All three directional signals survive — they're from different families.
```

---

## STEP 5 — Vote Aggregation + Initial Confidence

```
Votes:
  YES: probability_direction (0.65) + creator_disagreement (0.75) = 2 votes
  NO:  probability_bias (0.70)                                    = 1 vote

Majority wins → overall recommendation = YES

Initial confidence = average of the winning side only:
  (0.65 + 0.75) / 2 = 0.70
```

---

## STEP 6 — Composite Score (for AI candidate ranking)

```
composite = confidence + priority_boost × 0.15

  = 0.70 + 0.60 × 0.15
  = 0.70 + 0.09
  = 0.79

This score decides whether the market makes it into the top 3
candidates sent to the AI. Higher composite = higher priority.
```

---

## STEP 7 — News Enrichment

The bot extracts search keywords from the question using Ollama
(regex fallback if Ollama keyword extraction fails), then
queries NewsAPI for recent headlines.

```
Question → Ollama → "nvidia stock price"
NewsAPI search → 3 headlines:

  - "Nvidia supplier expands AI-chip production"
  - "Analysts raise Nvidia revenue expectations"
  - "Demand for AI compute remains strong"

News strategy analysis:
  Headlines lean YES (positive sentiment keywords)
  news_conf = 0.80 (fresh — highest headline is 6h old)

Recency-scaled boost:
  recency_factor = (0.80 - 0.60) / 0.20 = 1.0  (full effect)
  boost = 0.05 × 1.0 = 0.05

News direction agrees with stat direction (both YES):
  old confidence = 0.70
  new confidence = 0.70 + 0.05 = 0.75

Headlines are stored in rec['news_context'] for the AI prompt.
```

---

## STEP 8 — AI Prompt

The AI receives a structured prompt with three layers of context:

```
┌──────────────────────────────────────────────────────────────┐
│ Analyze this prediction market:                              │
│                                                              │
│ Question: Will Nvidia stock close above $200 before June 1?  │
│ Current probability: 67%                                     │
│ Trading volume: $900                                         │
│ Liquidity: $1,400                                            │
│ Close date: 2026-06-01                                       │
│                                                              │
│ Recent news (last 7 days):                                   │
│   - "Nvidia supplier expands AI-chip production"             │
│   - "Analysts raise Nvidia revenue expectations"             │
│   - "Demand for AI compute remains strong"                   │
│                                                              │
│ Historical prior (category: business, 60-70% bucket):        │
│   Past Manifold markets like this: 25 resolved               │
│   Crowd said ~65%, actually resolved YES 60%                 │
│   → crowd overestimates YES by +5pp                          │
│   Suggested adjustment: treat 67% as closer to 62%.          │
│                                                              │
│ Respond with JSON: recommendation, confidence,               │
│ estimated_true_probability, reasoning, risk_factors           │
└──────────────────────────────────────────────────────────────┘
```

AI returns:

```json
{
  "recommendation": "YES",
  "confidence": 0.82,
  "estimated_true_probability": 0.79,
  "reasoning": "Price looks low given strong supplier/demand signals.",
  "risk_factors": "Broad market correction could drag Nvidia down."
}
```

---

## STEP 9 — AI Confidence Blending

AI agrees with the stat recommendation (both YES), so confidence blends upward.

```
Formula (agreement):
  blended = max(stat_conf, 0.4 × stat_conf + 0.6 × ai_conf)

  stat_conf = 0.75
  ai_conf   = 0.82

  0.4 × 0.75 = 0.300
  0.6 × 0.82 = 0.492
  blended    = 0.792

Agreement cap (AI cannot boost more than +0.10 above stat):
  cap = stat_conf + 0.10 = 0.85

  0.792 < 0.85 → stays at 0.792

Final research output:
  recommendation = YES
  confidence = 0.792
  ai_estimated_probability = 0.79
  strategies = [probability_direction, probability_bias,
                creator_disagreement, ai_analysis]
```

*If the AI had DISAGREED (returned NO), the penalty formula would apply:*
```
  confidence = stat × (0.4 + (1 - ai_conf) × 0.4)
  = 0.75 × (0.4 + (1 - 0.82) × 0.4)
  = 0.75 × (0.4 + 0.072)
  = 0.75 × 0.472
  = 0.354  (well below the 65% trading threshold → blocked)
```

---

## STEP 10 — EV Calculation

```
For a YES bet:
  market probability (m) = 0.67
  our estimated true probability (p) = 0.79

Payout per dollar if we win:
  1 / m = 1 / 0.67 = 1.4925

EV per $1 bet:
  p × payout_per_dollar − 1
  = 0.79 × 1.4925 − 1
  = 1.1791 − 1
  = +$0.1791

Two EV fields written:

  estimated_ev_exec = MAX_BET_AMOUNT × 0.1791 = $5 × 0.1791 = +$0.90
  (used for swap decisions — honest at the executable stake size)

  estimated_ev_ref  = $25 × 0.1791 = +$4.48
  (used only for cross-market ranking)

  estimated_ev = estimated_ev_ref  (legacy alias)
```

---

## STEP 11 — Trader Risk Gates

The trader checks 8 gates in order. Each returns a named rejection reason
if the market fails that gate, tallied in Op3 trader counters.

```
1. AI veto (SKIP)?             → No                      ✓ pass
2. Confidence ≥ 65%?           → 79.2% ≥ 65%             ✓ pass
3. Already have OPEN position? → No                      ✓ pass
4. Positions ≥ 10?             → Assume 9 (1 free slot)  ✓ pass
5. Category cap reached?       → business has 0/3 open   ✓ pass
6. Liquidity ≥ $200?           → $1,400 ≥ $200           ✓ pass
7. Kelly edge > 0?             → (checked in sizing)     ✓ pass
8. Bet size ≥ $1?              → (checked after sizing)  ✓ pass

All gates passed → proceed to sizing.
```

---

## STEP 12 — Kelly Criterion Sizing

```
For a YES bet at market probability 0.67:

Net odds:
  b = (1 − m) / m = 0.33 / 0.67 = 0.4925

Our win probability:
  p = 0.79 (from AI estimate)
  q = 1 − p = 0.21

Raw Kelly fraction:
  kelly = (p × b − q) / b
  = (0.79 × 0.4925 − 0.21) / 0.4925
  = (0.3891 − 0.21) / 0.4925
  = 0.1791 / 0.4925
  = 0.3637

Half-Kelly × strategy weight:
  kelly_fraction = 0.3637 × 0.5 × 1.00 = 0.1819

Position size:
  balance × kelly_fraction
  = $55.28 × 0.1819
  = $10.05

Apply caps:
  MAX_BET_AMOUNT = $5
  50% of balance = $27.64
  → capped at $5.00

Round to nearest $5:
  → $5.00

Final executable size: $5.00
```

---

## STEP 13 — Trade Execution

```
Re-check: _trade_rejection_reason() called again before execution
(prevents exceeding MAX_POSITIONS if another trade was placed first
 in the same cycle)
→ still passes all gates

Fetch live market from Manifold API:
→ not resolved, not closed, current prob confirmed

Place paper bet:
  market: Nvidia $200
  direction: YES
  amount: $5.00
  entry_probability: 0.67
  estimated_ev: $0.90 (exec)
  ai_confidence: 0.82
  strategies: [probability_direction, probability_bias,
               creator_disagreement, ai_analysis]
  category: business

State updated:
  paper_trading_state.json: new position added
  Balance: $55.28 → $50.28
  Open positions: 9 → 10
```

---

## STEP 14 — Later: Resolution + Learning

```
IF market resolves YES (Nvidia above $200):
  Payout = $5.00 / 0.67 = $7.46
  Profit = $7.46 − $5.00 = +$2.46
  Status = WIN

IF market resolves NO (Nvidia below $200):
  Payout = $0
  Profit = −$5.00
  Status = LOSE

IF market resolves MKT at 75%:
  Payout = $5.00 × 0.75 / 0.67 = $5.60
  Profit = $5.60 − $5.00 = +$0.60
  Status = WIN

IF market is CANCELLED:
  Refund = $5.00
  Profit = $0.00
  Status = CANCELLED

Either way, the outcome feeds four learning loops:

Loop 1 → bet_outcomes in calibration.db
          → Monday: compute_strategy_weights.py
          → strategy_weights.json updated
          → Next hourly run uses new weights in Step 3 + Step 12

Loop 2 → resolved_markets in calibration.db (via Sunday harvest)
          → analyze_calibration.py
          → calibration_table.json updated
          → Next hourly run uses new bias data in Step 3 (probability_bias)
          → AI prompt gets updated calibration prior in Step 8

Loop 3 → category_accuracy.json
          → _effective_category_cap() adjusts
          → If business accuracy drops below 50% with ≥8 samples,
            cap reduces from 3 → 1

Loop 4 → market_snapshots.db (hourly accumulation)
          → When this market resolves, its live snapshots become
            training examples for M4/M5/M6
```

---

## One-Line Summary

This market got categorized → triggered 3 strategies → survived family dedup → won the YES/NO vote → got boosted by fresh news → got confirmed by AI → showed +$0.90 exec EV → passed 8 risk gates → got sized by half-Kelly at $5 → became a paper trade that feeds the weekly learning loops.
