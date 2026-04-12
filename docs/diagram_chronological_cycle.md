# Diagram 2 — Chronological Lifecycle of the Bot

What happens, when, and in what order — from hourly cron to weekly learning.

```
════════════════════════════════════════════════════════════════════════════
  EVERY HOUR — The Core Trading Loop
════════════════════════════════════════════════════════════════════════════

  :00 UTC ─── auto_research.py ───────────────────────────────────────────

    ┌─ 1. FETCH
    │   Manifold API → 100 markets
    │   Write snapshots → market_snapshots.db (source='live')
    │     with days_before_close from closeTime (Op4)
    │
    ├─ 2. FILTER
    │   Drop: resolved, low liquidity (<$200), stale (>72h + no volume)
    │   ~38 markets survive
    │
    ├─ 3. SCORE
    │   Run 6 strategy functions on each survivor
    │     4 directional: probability_direction, mean_reversion,
    │                    probability_bias, creator_disagreement
    │     1 confirmation-only: thin_market
    │     1 ranking filter: volume_spike_priority
    │   Apply strategy weights (from strategy_weights.json)
    │   Family dedup: keep 1 signal per family (momentum/contrarian/fund.)
    │   Vote: YES vs NO majority → overall direction + avg confidence
    │   ~19 recommendations produced
    │
    ├─ 4. NEWS ENRICHMENT (top 3 by confidence)
    │   Keyword extraction (Ollama, regex fallback) → NewsAPI search
    │   Direction agree → boost; disagree → penalize
    │   Headlines stored for AI prompt
    │
    ├─ 5. AI ANALYSIS (top 3 by composite score)
    │   Send to llama3.2:3b first (DeepSeek API fallback if Ollama fails):
    │     market data + news headlines + calibration prior
    │   AI returns: YES/NO/SKIP + confidence + true_prob estimate
    │   Blend stat + AI confidence
    │   Compute estimated_ev_ref ($25) + estimated_ev_exec ($5)
    │
    ├─ 6. SAVE
    │   Write market_research.json (schema_version=2)
    │   Append to data/research_counters.jsonl (Op3)
    │   Print observability summary to cron log
    │
    └─ 7. SWAP CHECK (if book full ≥ 10 positions)
        Find all losing positions (unrealised P&L < 0)
        For each loser, find best replacement:
          exec_ev ≥ |loss| + $1.00 margin (Op5)
        Write pending_swaps.json → Telegram proposal
        Human approves/dismisses via execute_swap.py

  :10 UTC ─── resolve_positions.py ───────────────────────────────────────

    ┌─ 1. CHECK MANIFOLD
    │   For each OPEN position, poll Manifold API
    │
    ├─ 2. RESOLVE
    │   YES/NO  → resolve_market()   → win/loss P&L
    │   MKT     → resolve_market_mkt()  → proportional payout
    │   CANCEL  → resolve_market_cancel() → full refund
    │
    ├─ 3. RECORD
    │   Write bet_outcomes to calibration.db (EV calibration)
    │   Free position slot
    │   Update balance
    │
    └─ 4. NOTIFY
        Send Telegram: ✅/❌ + P&L + new balance
        (if any markets resolved)

  :20 UTC ─── auto_trader.py ─────────────────────────────────────────────

    ┌─ 1. LOAD
    │   Read market_research.json
    │   Refuse if schema_version < 2 (safety guard)
    │
    ├─ 2. FILTER (the gauntlet — 8 gates, checked in order)
    │   AI veto → low confidence → existing position → max positions →
    │   category cap → liquidity → (later: Kelly edge, bet size)
    │   Each rejection tallied by reason → data/trader_counters.jsonl
    │
    ├─ 3. RANK
    │   Sort survivors by estimated_ev_exec (Op2-correct)
    │
    ├─ 4. EXECUTE (top 1-2, with re-check between trades)
    │   Re-validate gates before EACH trade (prevents exceeding max)
    │   Fetch live probability from Manifold (abort if resolved/closed)
    │   Kelly sizing: position_size = balance × kelly_fraction × weight
    │   place_paper_bet() → paper_trading_state.json
    │
    └─ 5. LOG
        Append to data/trader_counters.jsonl (Op3)
        Print observability summary to cron log


════════════════════════════════════════════════════════════════════════════
  DAILY — Summary + Reporting
════════════════════════════════════════════════════════════════════════════

  19:00 UTC ─── daily_summary.py ─────────────────────────────────────────

    Read paper_trading_state.json + trade history
    Calculate: balance, P&L, win rate, open positions, category breakdown
    Mondays: append weekly EV accuracy section
    Send Telegram message to group


════════════════════════════════════════════════════════════════════════════
  WEEKLY — Learning + Data Refresh
════════════════════════════════════════════════════════════════════════════

  SUNDAY 02:00 ─── Calibration harvest ───────────────────────────────────

    harvest_resolved.py:
      Manifold API → 2000+ recently-resolved markets
      Write to calibration.db :: resolved_markets
      _infer_market_category() for each (v2 taxonomy)

    analyze_calibration.py:
      Build calibration_table.json:
        per-bucket crowd bias (10% buckets)
        per-(category, bucket) cells with sample_size + reliability
      This data feeds:
        → Layer 1: probability_bias_strategy (per-bucket lookup)
        → Layer 4: AI prompt (_build_query_calibration_note)

  SUNDAY 02:30 ─── M2 re-audit ──────────────────────────────────────────

    audit_resolved_markets.py:
      Quality filters: near-close contamination, thin markets, category skew
      Output: data/m2_audit.json with usable_market_ids
      → feeds M3 reconstruction

  SUNDAY 03:00 ─── M3 reconstruction ────────────────────────────────────

    reconstruct_snapshots.py:
      For each usable resolved market, fetch bet history
      Reconstruct probability at T-7d, T-14d, T-30d before close
      Write to market_snapshots.db (source='reconstructed')
      → feeds M4 training dataset

  SUNDAY 03:30 ─── Offline backtest ─────────────────────────────────────

    backtest_from_snapshots.py:
      Run mean_reversion + probability_bias against reconstructed snapshots
      Compute accuracy → weights
      Merge with live weights (live ≥ 10 trades wins over backtest)
      Update data/strategy_weights.json
      → feeds Layer 2 (strategy weight scaling) next hourly run

  MONDAY 07:00 ─── EV accuracy report ──────────────────────────────────

    weekly_ev_report.py:
      Compare predicted EV vs actual P&L on resolved bet_outcomes
      Send Telegram report

  MONDAY 07:30 ─── Live strategy weights ────────────────────────────────

    compute_strategy_weights.py:
      Read bet_outcomes (post_ev_fix era only)
      Compute per-strategy direction accuracy
      Compute per-(category, strategy) accuracy
      Preserve backtest weights for strategies with < 10 live samples
        (prevents Monday run from overwriting Sunday's backtest signal)
      Write data/strategy_weights.json + data/category_accuracy.json
      Git commit + push
      → feeds Layer 2 + Layer 6 (Kelly sizing) next hourly run


════════════════════════════════════════════════════════════════════════════
  LONG-TERM — ML Pipeline (building toward M6)
════════════════════════════════════════════════════════════════════════════

  ACCUMULATING (continuous) ──────────────────────────────────────────────

    Every hour: +~95 live snapshot rows to market_snapshots.db
      with days_before_close from closeTime (Op4)

    Every Sunday: +new reconstructed snapshots from newly-resolved markets

    Every position resolution: +1 row to bet_outcomes
      (learning data for strategy weights + EV accuracy)

  WHEN READY (manual trigger) ────────────────────────────────────────────

    build_training_dataset.py (M4):
      Join snapshots + resolved outcomes → training_dataset.jsonl
      Live rows: snap to nearest window {7, 14, 30} within ±1 day
      One row per (market_id, window), earliest snapshot wins
      Currently: 127 rows (all reconstructed, 0 live resolved yet)

    run_eval_harness.py (M5):
      Evaluate any model against 4 baselines on held-out test split
      Crowd baseline: Brier 0.1503, DirAcc 75.0% (n=8)

    finetune.py (M6):
      LoRA fine-tune llama3.2:3b on M4 dataset
      Completion-only loss masking (prompt tokens → -100)
      Output: data/finetuned_preds.jsonl keyed by example_id
      Promote to production only if clears M5 gate


════════════════════════════════════════════════════════════════════════════
  THE FEEDBACK LOOPS (how the bot improves itself)
════════════════════════════════════════════════════════════════════════════

  See docs/diagram_learning_loops.md for the full detailed version
  with data schemas, formulas, and interaction diagrams.

  Quick reference:

  LOOP 1 — Strategy Weights (weekly)
    trades resolve → bet_outcomes → Monday weights run
    → strategy_weights.json → Layer 2 scaling + Layer 7 Kelly

  LOOP 2 — Calibration Table (weekly)
    Sunday harvest → resolved markets → calibration_table.json
    → Layer 1 probability_bias + Layer 4 AI calibration prior

  LOOP 3 — Category Accuracy (weekly)
    bet_outcomes by category → Monday weights run
    → category_accuracy.json → Layer 6 adaptive caps (3→1 if <50%)

  LOOP 4 — ML Dataset Growth (multi-week)
    hourly snapshots + resolved outcomes → training_dataset.jsonl
    → M5 eval → M6 fine-tune (when dataset is large enough)
```
