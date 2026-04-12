# Diagram 3 — Learning Loops

How the bot improves itself over time. Four independent feedback loops,
each operating on a different timescale, each feeding back into a
specific layer of the analysis pipeline.

```
┌─────────────────────────────────────────────────────────────────────────┐
│                                                                         │
│  THE FULL LEARNING PICTURE                                              │
│                                                                         │
│  Every trade the bot places is also a measurement. When that trade      │
│  resolves (days to weeks later), the outcome becomes training data      │
│  that adjusts four different parts of the pipeline. The bot literally   │
│  bets, watches what happens, and adjusts how it bets next time.         │
│                                                                         │
│  ┌──────────┐     ┌──────────┐     ┌──────────┐     ┌──────────┐      │
│  │  LOOP 1  │     │  LOOP 2  │     │  LOOP 3  │     │  LOOP 4  │      │
│  │ Strategy │     │Calibrate │     │ Category │     │ ML Data  │      │
│  │ Weights  │     │  Table   │     │ Accuracy │     │  Growth  │      │
│  └────┬─────┘     └────┬─────┘     └────┬─────┘     └────┬─────┘      │
│       │                │                │                │              │
│       ▼                ▼                ▼                ▼              │
│  ┌─────────┐     ┌─────────┐     ┌──────────┐     ┌─────────┐         │
│  │Layer 2  │     │Layer 1  │     │ Layer 6  │     │ Layer 4  │         │
│  │Signal   │     │Strategy │     │ Risk     │     │ AI Veto  │         │
│  │Scaling  │     │Firing   │     │ Gates    │     │ Quality  │         │
│  │+ Kelly  │     │+ AI     │     │          │     │          │         │
│  └─────────┘     └─────────┘     └──────────┘     └─────────┘         │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘


════════════════════════════════════════════════════════════════════════════
  LOOP 1 — Strategy Weights
  Timescale: weekly (Monday 07:30 UTC)
  Feeds into: Layer 2 (signal aggregation) + Layer 7 (Kelly sizing)
════════════════════════════════════════════════════════════════════════════

  What it learns: "Which strategies actually make money?"

  ┌─────────────────────────────────────────────────────────────────────┐
  │                                                                     │
  │  HOURLY: Bot places a trade                                         │
  │    │                                                                │
  │    │  The trade record stores:                                      │
  │    │    strategies: ["probability_direction", "probability_bias"]    │
  │    │    estimated_ev: +$0.90                                        │
  │    │    category: "business"                                        │
  │    │    ai_estimated_probability: 0.79                              │
  │    │                                                                │
  │    ▼                                                                │
  │  DAYS/WEEKS LATER: Market resolves                                  │
  │    │                                                                │
  │    │  resolve_positions.py writes to bet_outcomes:                   │
  │    │    market_resolution: "YES"                                     │
  │    │    actual_pnl: +$2.46                                          │
  │    │    ev_error: estimated_ev − actual_pnl = +$0.90 − $2.46       │
  │    │    era: "post_ev_fix"                                          │
  │    │    category: "business"                                        │
  │    │    strategies: '["probability_direction","probability_bias"]'   │
  │    │                                                                │
  │    ▼                                                                │
  │  MONDAY 07:30 UTC: compute_strategy_weights.py runs                 │
  │    │                                                                │
  │    │  For each strategy with ≥ 10 resolved trades (post_ev_fix):    │
  │    │                                                                │
  │    │    1. Count: how many trades using this strategy won            │
  │    │       vs lost (direction accuracy)                             │
  │    │                                                                │
  │    │    2. Convert accuracy to weight:                               │
  │    │       accuracy ≥ 60% → weight = 1.0 + (accuracy − 0.5) × 2    │
  │    │         e.g. 70% accuracy → weight = 1.40 (max capped at 1.2)  │
  │    │       accuracy < 50% → weight = max(0.5, accuracy / 0.5)       │
  │    │         e.g. 30% accuracy → weight = 0.60 (penalty)            │
  │    │                                                                │
  │    │    3. Also compute per-(category, strategy) weights             │
  │    │       Same formula, but scoped to one category                 │
  │    │       e.g. "probability_bias in crypto" might have a           │
  │    │       different weight than "probability_bias in politics"      │
  │    │                                                                │
  │    ▼                                                                │
  │  OUTPUT: data/strategy_weights.json                                 │
  │    {                                                                │
  │      "weights": {                                                   │
  │        "probability_direction": 1.0,                                │
  │        "probability_bias": 1.2,     ← this strategy is winning      │
  │        "mean_reversion": 0.7,       ← this one is losing            │
  │      },                                                             │
  │      "by_category": {                                               │
  │        "crypto": {"probability_bias": {"weight": 1.2, ...}},       │
  │        "politics": {"probability_bias": {"weight": 0.8, ...}},     │
  │      },                                                             │
  │      "per_strategy_samples": {"probability_direction": 15, ...}     │
  │    }                                                                │
  │                                                                     │
  │    ▼                                                                │
  │  NEXT HOURLY RUN: weights applied in two places                     │
  │                                                                     │
  │    Layer 2 (auto_research.py):                                      │
  │      Each strategy's base confidence is multiplied by its weight    │
  │      BEFORE family dedup. A strategy with weight 0.7 effectively    │
  │      starts at 0.65 × 0.7 = 0.455 — below the 0.65 trading         │
  │      threshold, so it can't survive the confidence gate alone.      │
  │      This is how the bot STOPS betting on losing strategies.        │
  │                                                                     │
  │    Layer 7 (auto_trader.py):                                        │
  │      Kelly fraction is scaled by half-Kelly × weight.               │
  │      A winning strategy (weight 1.2) gets 60% Kelly.               │
  │      A losing strategy (weight 0.7) gets 35% Kelly.                │
  │      This is how the bot bets LESS on unreliable strategies.        │
  │                                                                     │
  └─────────────────────────────────────────────────────────────────────┘

  Current state (2026-04-12):
    Resolved trades: 6 post-fix (need ≥ 10 per strategy to activate)
    All weights: 1.0 (default) except probability_bias = 1.2 (from backtest)
    Learning gate: NOT YET ACTIVE — accumulating data


════════════════════════════════════════════════════════════════════════════
  LOOP 2 — Calibration Table
  Timescale: weekly (Sunday 02:00 UTC)
  Feeds into: Layer 1 (probability_bias) + Layer 4 (AI calibration prior)
════════════════════════════════════════════════════════════════════════════

  What it learns: "Where is the crowd systematically wrong?"

  ┌─────────────────────────────────────────────────────────────────────┐
  │                                                                     │
  │  SUNDAY 02:00 UTC: harvest_resolved.py runs                         │
  │    │                                                                │
  │    │  Fetches 2000+ recently-resolved markets from Manifold API     │
  │    │  Writes to calibration.db :: resolved_markets table            │
  │    │  Each row:                                                     │
  │    │    market_id, question, probability_close, outcome,            │
  │    │    close_date, unique_bettors, category                        │
  │    │                                                                │
  │    │  Category assigned by _infer_market_category() — same          │
  │    │  classifier used in live trading (single source of truth)      │
  │    │                                                                │
  │    ▼                                                                │
  │  SUNDAY 02:00 UTC: analyze_calibration.py runs                      │
  │    │                                                                │
  │    │  Groups all resolved markets into 10% probability buckets      │
  │    │  (0-10%, 10-20%, ... 90-100%)                                  │
  │    │                                                                │
  │    │  For each bucket:                                              │
  │    │    crowd_midpoint = average probability at close               │
  │    │    actual_yes_rate = fraction that actually resolved YES        │
  │    │    bias = crowd_midpoint − actual_yes_rate                     │
  │    │                                                                │
  │    │  Example finding:                                              │
  │    │    60-70% bucket: crowd said ~65%, actual YES rate = 58%       │
  │    │    → bias = +7pp (crowd overestimates YES here)                │
  │    │                                                                │
  │    │  Also computes per-(category, bucket) cells:                   │
  │    │    crypto 60-70%: bias = +9pp, n = 25, reliable = true         │
  │    │    politics 60-70%: bias = +2pp, n = 12, reliable = false      │
  │    │                                                                │
  │    ▼                                                                │
  │  OUTPUT: data/calibration_table.json                                │
  │    {                                                                │
  │      "by_category": [                                               │
  │        {"category": "crypto", "bias": 0.0395, "sample_size": 80},  │
  │        {"category": "other",  "bias": 0.0259, "sample_size": 1182} │
  │      ],                                                             │
  │      "by_category_bucket": [                                        │
  │        {"category": "crypto", "bucket_low": 0.6,                   │
  │         "bias": 0.09, "sample_size": 25, "reliable": true},        │
  │        ...92 total cells...                                         │
  │      ]                                                              │
  │    }                                                                │
  │                                                                     │
  │    ▼                                                                │
  │  NEXT HOURLY RUN: calibration data used in two places               │
  │                                                                     │
  │    Layer 1 — probability_bias_strategy (strategies.py):             │
  │      _bucket_bias_for(category, probability) looks up the cell      │
  │      If bias ≥ 2.5pp → fire (positive = bet NO, negative = bet YES)│
  │      If no reliable cell → fall back to category aggregate          │
  │      This is how the bot DISCOVERS crowd blind spots.               │
  │                                                                     │
  │    Layer 4 — AI prompt (ai_analyzer.py):                            │
  │      _build_query_calibration_note(question, probability)           │
  │      Injects a natural-language prior into the AI prompt:           │
  │        "Historical prior: crypto markets at 60-70%                  │
  │         crowd overestimates YES by +9pp.                            │
  │         Treat current 67% as closer to 58%."                        │
  │      This is how the AI knows about PAST crowd errors.              │
  │                                                                     │
  └─────────────────────────────────────────────────────────────────────┘

  Current state (2026-04-12):
    resolved_markets: 2016 rows
    calibration_table: regenerated 2026-04-12 02:00 UTC
    10 categories, 92 per-bucket cells
    Category v2 reclassify applied (282 rows relabeled)


════════════════════════════════════════════════════════════════════════════
  LOOP 3 — Category Accuracy
  Timescale: weekly (Monday 07:30 UTC, same run as Loop 1)
  Feeds into: Layer 6 (trader risk gates — category exposure cap)
════════════════════════════════════════════════════════════════════════════

  What it learns: "Are WE losing money in specific categories?"

  ┌─────────────────────────────────────────────────────────────────────┐
  │                                                                     │
  │  This is different from Loop 2 (which measures the CROWD's errors). │
  │  Loop 3 measures OUR OWN trade accuracy per category.               │
  │                                                                     │
  │  MONDAY 07:30 UTC: compute_strategy_weights.py also computes        │
  │    │                                                                │
  │    │  For each category with ≥ 8 resolved trades:                   │
  │    │    accuracy = (trades where our direction was correct) / total  │
  │    │                                                                │
  │    ▼                                                                │
  │  OUTPUT: data/category_accuracy.json                                │
  │    {                                                                │
  │      "categories": {                                                │
  │        "crypto":   {"accuracy": 0.72, "sample_count": 15},         │
  │        "politics": {"accuracy": 0.35, "sample_count": 10},         │
  │        "other":    {"accuracy": 0.50, "sample_count": 22}          │
  │      }                                                              │
  │    }                                                                │
  │                                                                     │
  │    ▼                                                                │
  │  NEXT HOURLY RUN: category_accuracy used in Layer 6                 │
  │                                                                     │
  │    auto_trader.py :: _effective_category_cap(category)               │
  │                                                                     │
  │    Default cap: 3 positions per category                            │
  │    Exception: "other" is uncapped (catch-all, positions uncorrelated)│
  │                                                                     │
  │    If accuracy < 50% AND sample_count ≥ 8:                          │
  │      cap reduced from 3 → 1                                        │
  │                                                                     │
  │    Example from the table above:                                    │
  │      crypto:   72% accuracy → cap stays at 3 (doing well)          │
  │      politics: 35% accuracy → cap drops to 1 (losing money here)   │
  │      other:    50% accuracy → cap stays at 3 (borderline, no cut)  │
  │                                                                     │
  │    This is how the bot REDUCES EXPOSURE to categories it's bad at.  │
  │    It doesn't stop trading them entirely — it just limits to 1      │
  │    position instead of 3, so a bad streak in one topic doesn't      │
  │    wipe out the whole portfolio.                                    │
  │                                                                     │
  └─────────────────────────────────────────────────────────────────────┘

  Current state (2026-04-12):
    Only "other" has samples: accuracy = 0.0, sample_count = 6
    Gate NOT active (need ≥ 8 per category)
    All categories at default cap = 3


════════════════════════════════════════════════════════════════════════════
  LOOP 4 — ML Dataset Growth
  Timescale: weeks to months (continuous accumulation)
  Feeds into: Layer 4 (AI veto quality, if M6 is ever deployed)
════════════════════════════════════════════════════════════════════════════

  What it learns: "Can we train a better AI for the market analysis step?"

  ┌─────────────────────────────────────────────────────────────────────┐
  │                                                                     │
  │  This is the slowest loop. It doesn't change anything today — it    │
  │  accumulates data for a future fine-tuned model that could replace  │
  │  or augment the generic llama3.2:3b used in Layer 4.                │
  │                                                                     │
  │  CONTINUOUS: Every hourly research run writes live snapshots         │
  │    │                                                                │
  │    │  _write_market_snapshots() in auto_research.py                 │
  │    │  ~95 rows per hour → market_snapshots.db (source='live')       │
  │    │  Each row includes days_before_close from closeTime (Op4)      │
  │    │                                                                │
  │    │  Current accumulation: 7,172 live rows                         │
  │    │                                                                │
  │    ▼                                                                │
  │  WEEKLY: Sunday harvest adds resolved outcomes                      │
  │    │                                                                │
  │    │  resolved_markets in calibration.db gets new entries           │
  │    │  These can be joined with live snapshots by market_id          │
  │    │                                                                │
  │    ▼                                                                │
  │  WEEKLY: Sunday M3 reconstruction adds historical snapshots         │
  │    │                                                                │
  │    │  reconstruct_snapshots.py replays bet history for              │
  │    │  newly-resolved markets → probability at T-7d, T-14d, T-30d   │
  │    │  Writes to market_snapshots.db (source='reconstructed')        │
  │    │                                                                │
  │    ▼                                                                │
  │  ON DEMAND: build_training_dataset.py (M4)                          │
  │    │                                                                │
  │    │  Joins snapshots + outcomes → training_dataset.jsonl            │
  │    │                                                                │
  │    │  Reconstructed rows: included directly (T-7/14/30 exact)       │
  │    │  Live rows: snap to nearest window {7, 14, 30} within ±1 day  │
  │    │             one row per (market_id, window), earliest wins     │
  │    │                                                                │
  │    │  Each row becomes a prompt + completion pair:                   │
  │    │    PROMPT:  "Question: ..., Crowd prob: 67%, Category: ...,    │
  │    │             Days until close: 7"                               │
  │    │    COMPLETION: "0.00" (resolved NO) or "1.00" (resolved YES)   │
  │    │                                                                │
  │    │  Current dataset: 127 rows (all reconstructed)                 │
  │    │  Expected growth: as live markets resolve, new rows appear     │
  │    │  automatically without code changes                            │
  │    │                                                                │
  │    ▼                                                                │
  │  ON DEMAND: run_eval_harness.py (M5)                                │
  │    │                                                                │
  │    │  Evaluates any model against 4 baselines:                      │
  │    │    crowd, always_0.5, always_0.8, random                       │
  │    │                                                                │
  │    │  Metrics: Brier score, log loss, directional accuracy          │
  │    │                                                                │
  │    │  Promotion gate (held-out test split, n=8):                    │
  │    │    Brier < 0.1503 AND DirAcc ≥ 75.0%                          │
  │    │    (must beat the crowd on both metrics)                       │
  │    │                                                                │
  │    ▼                                                                │
  │  ON DEMAND: finetune.py (M6) — NOT YET RUN                         │
  │    │                                                                │
  │    │  LoRA fine-tune llama3.2:3b on the M4 dataset                  │
  │    │  Completion-only loss masking (prompt tokens → -100)            │
  │    │  Output: data/finetuned_preds.jsonl keyed by example_id        │
  │    │                                                                │
  │    │  If the model clears the M5 gate:                              │
  │    │    merge LoRA adapter → convert to GGUF → push to Ollama       │
  │    │    → replace the generic llama3.2:3b in Layer 4                │
  │    │    → AI becomes specifically trained on prediction markets      │
  │    │                                                                │
  │    │  If it doesn't clear:                                          │
  │    │    wait for more data, try again later                         │
  │    │    the dataset grows every week automatically                  │
  │    │                                                                │
  │    │  Status: scaffolding ready, deferred until dataset > 500 rows  │
  │    │                                                                │
  └─────────────────────────────────────────────────────────────────────┘


════════════════════════════════════════════════════════════════════════════
  HOW THE LOOPS INTERACT
════════════════════════════════════════════════════════════════════════════

  The four loops are independent — each reads different data and writes
  to different files. But they share one common input: resolved trades.

  Every time a position resolves, it feeds ALL loops simultaneously:

  ┌─────────────────────────────────────────────────────────────────────┐
  │                                                                     │
  │                    ┌──────────────────────┐                         │
  │                    │  POSITION RESOLVES   │                         │
  │                    │  (resolve_positions)  │                         │
  │                    └──────────┬───────────┘                         │
  │                               │                                     │
  │              ┌────────────────┼────────────────┐                    │
  │              ▼                ▼                ▼                    │
  │    ┌─────────────┐  ┌──────────────┐  ┌──────────────┐             │
  │    │ bet_outcomes │  │   resolved   │  │   snapshot   │             │
  │    │   (SQLite)   │  │   markets    │  │   row gains  │             │
  │    │              │  │  (harvest)   │  │   an outcome │             │
  │    └──────┬──────┘  └──────┬───────┘  └──────┬───────┘             │
  │           │                │                 │                      │
  │     ┌─────┴──────┐   ┌────┴────┐       ┌────┴────┐                │
  │     ▼            ▼   ▼         ▼       ▼         │                 │
  │  ┌──────┐  ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐  │                 │
  │  │LOOP 1│  │LOOP 3│ │LOOP 2│ │LOOP 2│ │LOOP 4│  │                 │
  │  │Weight│  │Cat   │ │Calib │ │AI    │ │ML    │  │                 │
  │  │Scale │  │Caps  │ │Bias  │ │Prior │ │Data  │  │                 │
  │  └──┬───┘  └──┬───┘ └──┬───┘ └──┬───┘ └──┬───┘  │                 │
  │     │         │        │        │        │       │                 │
  │     ▼         ▼        ▼        ▼        ▼       │                 │
  │  Layer 2   Layer 6  Layer 1  Layer 4  Layer 4    │                 │
  │  + Layer 7          (bias)   (prompt) (future)   │                 │
  │                                                   │                 │
  │  "Bet more   "Cut     "Find   "Tell   "Train    │                 │
  │   on what    exposure  crowd   AI      a better  │                 │
  │   works"     where     blind   where   AI"       │                 │
  │              we lose"  spots"  crowd              │                 │
  │                                errs"             │                 │
  └─────────────────────────────────────────────────────────────────────┘

  Timeline of a single trade's learning impact:

  Hour 0:     Trade placed (probability_bias NO $5 on crypto market)
  Hour 1-720: Market is open, position sits in portfolio
  Hour 720:   Market resolves YES → we LOST
  Hour 720:   bet_outcomes gets a row (strategies, category, pnl, ev_error)
  Hour 720:   Telegram notification: ❌ LOSE −$5.00
  Sunday:     harvest_resolved.py picks up this market
              analyze_calibration.py recomputes the crypto 60-70% cell
              → maybe the bias there was overstated → cell bias shrinks
              → next week probability_bias fires less aggressively on crypto
  Monday:     compute_strategy_weights.py sees probability_bias lost this one
              → if enough losses accumulate: weight drops below 1.0
              → next week probability_bias starts at 0.70 × 0.8 = 0.56
              → below 0.65 threshold → effectively disabled until it recovers
  Monday:     category_accuracy recomputed
              → crypto accuracy drops
              → if below 50% with ≥8 samples → cap reduces 3→1
              → bot can only hold 1 crypto position at a time
  Ongoing:    The live snapshot from hour 0 (with the price at decision time)
              is now joinable with the resolved outcome
              → becomes a training example for M4/M6
              → the fine-tuned model learns "this kind of market at this
                 price resolved NO — don't trust the crowd here"
```

---

## Summary: What each loop controls

| Loop | What it measures | What it changes | How fast it adapts | Min samples |
|------|-----------------|----------------|-------------------|-------------|
| **1. Strategy Weights** | Our win rate per strategy | How much we trust each strategy's signal | Weekly (Monday) | 10 per strategy |
| **2. Calibration Table** | Crowd's historical bias | Where probability_bias fires + what the AI is told | Weekly (Sunday) | 15 per (category, bucket) cell |
| **3. Category Accuracy** | Our win rate per category | How many positions we allow per topic | Weekly (Monday) | 8 per category |
| **4. ML Dataset** | Prediction vs outcome on held-out data | (Future) Quality of the AI model itself | Months | 500+ rows for trustworthy M6 |
