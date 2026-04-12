# Diagram 1 — Market Analysis Layers

How the bot evaluates a single market, from raw data to trade decision.
Each layer filters, enriches, or gates the signal from the layer below.

```
┌─────────────────────────────────────────────────────────────────────────┐
│                                                                         │
│  LAYER 7: EXECUTION                                          auto_trader│
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │  Kelly sizing:  position_size = balance × kelly_fraction × weight │  │
│  │  Market validation: re-fetch from Manifold, abort if resolved     │  │
│  │  Place paper bet → paper_trading_state.json                       │  │
│  │  Store: estimated_ev, ai_confidence, strategies, category         │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                              ▲                                          │
│                              │ passes all gates                         │
│  LAYER 6: RISK GATES                                         auto_trader│
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │  1. AI veto (SKIP)           → reject                             │  │
│  │  2. Confidence < 65%         → reject                             │  │
│  │  3. Existing OPEN position   → reject                             │  │
│  │  4. Positions ≥ 10           → reject                             │  │
│  │  5. Category cap (3/cat)     → reject  ("other" uncapped)         │  │
│  │  6. Liquidity < $200         → reject                             │  │
│  │  7. Kelly ≤ 0 (no edge)     → reject                             │  │
│  │  8. Bet size < $1            → reject                             │  │
│  │                                                                   │  │
│  │  Each gate returns a named reason → Op3 trader counters           │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                              ▲                                          │
│                              │ schema_version = 2                       │
│  LAYER 5: EV COMPUTATION                                auto_research  │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │  For each rec with ai_estimated_probability:                      │  │
│  │                                                                   │  │
│  │    ev_per_dollar = win_prob × payout_per_dollar − 1               │  │
│  │                                                                   │  │
│  │    estimated_ev_ref  = $25 × ev_per_dollar  (ranking only)        │  │
│  │    estimated_ev_exec = $5  × ev_per_dollar  (swap decisions)      │  │
│  │                                                                   │  │
│  │  Write schema_version=2 → market_research.json                    │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                              ▲                                          │
│                              │ confidence blended                       │
│  LAYER 4: AI ANALYSIS          ai_analyzer + Ollama (DeepSeek fallback)│
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │  Top 3 candidates by composite score sent to llama3.2:3b          │  │
│  │                                                                   │  │
│  │  The AI prompt receives:                                          │  │
│  │    ┌──────────────────────────────────────────────────────────┐   │  │
│  │    │  Question + probability + volume + liquidity + close date│   │  │
│  │    │                                                          │   │  │
│  │    │  Recent news (headline titles only, from NewsAPI):        │   │  │
│  │    │    - "Bitcoin ETF approved by SEC"                        │   │  │
│  │    │    - "Crypto market rally continues"                      │   │  │
│  │    │                                                          │   │  │
│  │    │  Calibration prior (per-category, per-bucket):           │   │  │
│  │    │    "Past crypto markets at 60-70%: crowd overestimates   │   │  │
│  │    │     by +7pp. Treat 65% as closer to 58%."                │   │  │
│  │    └──────────────────────────────────────────────────────────┘   │  │
│  │                                                                   │  │
│  │  AI returns: YES/NO/SKIP + confidence + estimated_true_prob       │  │
│  │                                                                   │  │
│  │  Blending:                                                        │  │
│  │    agrees  → max(stat, 0.4×stat + 0.6×AI), cap: stat + 0.10      │  │
│  │    disagrees → stat × (0.4 + (1−ai_conf)×0.4)                    │  │
│  │    SKIP → stat unchanged                                          │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                              ▲                                          │
│                              │ top 3 by composite score                 │
│  LAYER 3: NEWS ENRICHMENT                    news_fetcher + auto_research│
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │  Top 3 recs by confidence get real-time headlines:                │  │
│  │                                                                   │  │
│  │    Question → keyword extraction (Ollama, regex fallback)         │  │
│  │              → NewsAPI search                                     │  │
│  │                                                                   │  │
│  │  News direction vs stat direction:                                │  │
│  │    agrees   → confidence + boost × recency_factor                 │  │
│  │    disagrees → confidence × penalty × recency_factor              │  │
│  │    neutral  → no change                                           │  │
│  │                                                                   │  │
│  │  Headlines stored in rec['news_context'] → passed to AI prompt    │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                              ▲                                          │
│                              │ sorted by confidence                     │
│  LAYER 2: SIGNAL AGGREGATION                             auto_research  │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │  A. Strategy weight scaling                                       │  │
│  │     confidence × weight  (0.5–1.2 from strategy_weights.json)     │  │
│  │     Fallback: by_category[cat][strat] → global[strat] → 1.0      │  │
│  │                                                                   │  │
│  │  B. Family deduplication  (1 winner per family)                   │  │
│  │     ┌────────────┬───────────────────────────────────────┐        │  │
│  │     │ momentum   │ probability_direction                 │        │  │
│  │     │ contrarian │ mean_reversion OR probability_bias    │        │  │
│  │     │ fundamental│ creator_disagreement                  │        │  │
│  │     │ filter     │ volume_spike (priority boost only)    │        │  │
│  │     └────────────┴───────────────────────────────────────┘        │  │
│  │     thin_market: confirmation-only (+0.03 if matches contrarian)  │  │
│  │                                                                   │  │
│  │  C. Vote aggregation                                              │  │
│  │     Count YES vs NO across surviving signals                      │  │
│  │     Majority wins → overall recommendation + avg confidence       │  │
│  │     Tie → dropped                                                 │  │
│  │                                                                   │  │
│  │  D. Composite score = confidence + priority_boost × 0.15          │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                              ▲                                          │
│                              │ raw signals (0–5 per market)             │
│  LAYER 1: STRATEGY EVALUATION                            strategies.py  │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │  Each strategy independently evaluates the market:                │  │
│  │                                                                   │  │
│  │  probability_direction (0.65) — momentum                          │  │
│  │    "Price is high + recent activity → bet YES"                    │  │
│  │    Guards: prob outside 45-55%, last bet < 48h, volume24h ≥ 5     │  │
│  │                                                                   │  │
│  │  mean_reversion (0.68) — contrarian                               │  │
│  │    "Price is extreme → bet against"                               │  │
│  │    Guards: prob >85% or <15%, bettors < 100, not closing soon     │  │
│  │                                                                   │  │
│  │  probability_bias (0.70) — contrarian                             │  │
│  │    "Historical crowd overconfidence in this (category, bucket)"   │  │
│  │    Guards: per-bucket bias ≥ 2.5pp (from calibration_table.json)  │  │
│  │    NEW: per-bucket lookup, negative bias → YES                    │  │
│  │                                                                   │  │
│  │  creator_disagreement (0.75) — fundamental                        │  │
│  │    "Creator thinks differently than crowd by ≥ 15pp"              │  │
│  │    Guards: resolutionProbability field must exist (~5% of markets) │  │
│  │                                                                   │  │
│  │  thin_market (0.65) — contrarian (confirmation-only)              │  │
│  │    "AMM pool imbalance > 70%"                                     │  │
│  │    Never votes independently — only boosts matching signals       │  │
│  │                                                                   │  │
│  │  volume_spike — filter (not directional)                          │  │
│  │    "24h volume > 2× batch median → priority boost 0.0–1.0"       │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                              ▲                                          │
│                              │ ~40 markets survive                      │
│  LAYER 0: DATA INGESTION + FILTERING                     auto_research  │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │  Manifold API → 100 markets                                       │  │
│  │                                                                   │  │
│  │  Write to market_snapshots.db (source='live', days_before_close)  │  │
│  │                                                                   │  │
│  │  Filter out:                                                      │  │
│  │    - Resolved markets                                             │  │
│  │    - Liquidity < $200                                             │  │
│  │    - Stale (last bet > 72h AND volume24h < 10)                    │  │
│  │                                                                   │  │
│  │  Category inference: _infer_market_category(question)             │  │
│  │    9 named categories + other fallback:                           │  │
│  │    crypto → ai_tech → gaming → entertainment → politics →        │  │
│  │    sports → science → business → economics → other               │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘

Historical production snapshot (2026-04-11 15:55 UTC):
  Layer 0: 100 fetched → 38 survive filters
  Layer 1: prob_dir=11, mean_rev=3, prob_bias=18, creator=0, thin=0
  Layer 2: momentum=11 winners, contrarian=18 winners, 18 no-signal drops
  Layer 3: 3 markets get news headlines
  Layer 4: 3 sent to AI → 3 SKIPped
  Layer 5: 19 final recs with EV computed
  Layer 6: all rejected (8 positions OPEN, max_positions not hit but
           other gates like AI veto / low confidence / existing position)
  Layer 7: 0 trades executed in that run (gates + book state blocked execution)
```
