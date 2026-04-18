# Manifold Trading Bot — Plan V2

> **Status**: Planning document. Nothing in here is implemented yet.
> **Predecessor**: See [PLAN.md](PLAN.md) for V1 (everything built through the 15-page dashboard).
> **Written**: 2026-04-17
> **Scope**: Address the structural problems the dashboard exposed in the live system.

---

## Why V2 Exists

V1 built the observability layer. The dashboard can now see every part of the pipeline. What it's showing us is uncomfortable:

- The book is jammed (10/10 positions, most 10+ days old)
- The AI layer is acting as a brake, not a source of edge (0 trades from 46 recent trader runs, 368 `max_positions` rejections)
- The learning pipeline is built but not producing live artifacts (snapshots don't overlap with resolved markets)
- `estimated_ev` is null on every trade because AI never produces a probability estimate

V1 answered "what is the bot doing?" V2 answers "**why isn't the bot learning, and how do we unblock it?**"

---

## Core Thesis

The system has three dead loops that each block the next:

```
  1. AI failure      →  treated as SKIP  →  veto  →  no trades
           ↓
  2. No trades       →  no resolutions   →  no bet_outcomes
           ↓
  3. No outcomes     →  no weight updates →  no learning →  back to stat-only
```

V2 cuts all three loops. It also adds two new capabilities the dashboard has been asking for:
- **Position lifecycle categorization** (short-term vs long-term, managed differently)
- **External data sources** beyond Manifold (more training data, cross-platform signals)

---

## Phase 1 — Unblock the Pipeline (Week 1)

The pipeline is built correctly. Specific bugs and wrong defaults are blocking it. Fix those first.

### 1.1 — Separate AI states (`ai_status` field)

**Problem**: When AI times out, `auto_research.py` writes `ai_recommendation = 'SKIP'` and `ai_returned_skip = True`. The trader then vetoes identically to a real SKIP. AI failure becomes AI rejection.

**Fix**: Add explicit `ai_status` field with 5 states:
- `not_run` — market wasn't in the top-3 candidates
- `no_result` — AI was invoked but timed out / parse error / no response
- `skip` — AI ran and explicitly said "no edge here"
- `agree` — AI recommendation matches stat recommendation
- `disagree` — AI disagrees with stat

**Changes:**
- `auto_research.py`: writes `ai_status` correctly for each case
- `auto_trader.py`: vetoes only on `ai_status == 'skip'`, falls through to stat-only trading on `no_result`
- Schema version bump to v3 (since field shape changed)

**Impact**: AI timeouts stop blocking trades. The bot trades on stat signals when AI is unavailable, which is how it worked before AI was added.

**Estimated effort**: 1 day.

---

### 1.2 — Stat-derived probability fallback for EV

**Problem**: `estimated_ev = P(win) × payout - stake` requires a probability estimate. Currently only AI provides one. AI rarely produces one (0% of current research), so `estimated_ev` is null on every trade record. The entire EV calibration loop is dark.

**Fix**: Compute a stat-derived probability estimate when AI doesn't provide one. Sources:
- **Confidence-derived**: `p_estimate = confidence_scaled_to_probability(direction, confidence)` — e.g., confidence 0.70 toward YES means we think YES is 70% likely
- **Bias-adjusted**: apply the per-bucket crowd bias from `calibration_table.json` to the market probability — if crowd systematically overestimates YES at 60-70% by +10%, adjust our estimate down
- **Strategy-derived**: different strategies have different implied probabilities (mean reversion toward 50%, probability_bias toward the unbiased value)

**Changes:**
- New function `estimate_win_probability(rec, calibration_table)` in `auto_trader.py`
- EV computation uses: `ai_estimated_probability` if present, else stat-derived fallback
- `bet_outcomes` table stores `estimated_ev`, `p_estimate_source` (ai/stat/bias), and the raw p estimate

**Impact**: Every trade logs `estimated_ev`. `weekly_ev_report.py` finally has data to report on. We can measure whether our size decisions are picking profitable trades.

**Estimated effort**: 1-2 days.

---

### 1.3 — Analyze-once AI cache

**Problem**: The same top-3 markets are re-picked every hour. They all fail AI. After 4 hours, they hit the cooldown. But by then 10+ other markets are also cycling into the failed set. Result: 7-10 out of 10-11 eligible markets are cooled down at any given time.

**Fix**: Cache AI results per market. Invalidate only when market state changes meaningfully.

**Cache entry shape:**
```json
{
  "market_id": "abc123",
  "analyzed_at": "2026-04-17T09:00:00Z",
  "result": { "recommendation": "YES", "confidence": 0.75, "estimated_probability": 0.72 },
  "market_state_at_analysis": { "probability": 0.65, "volume24h": 450, "unique_bettors": 32 }
}
```

**Invalidation rules** (re-analyze if any of these):
- Cached entry is >24h old
- Market probability has moved >5pp since analysis
- 24h volume has doubled since analysis
- New high-relevance news has been fetched

**Changes:**
- New file: `data/ai_analysis_cache.json`
- `auto_research.py`: before sending to AI, check cache. If hit, use cached result. If miss or invalidated, send to AI.
- Cache pruning: remove entries for resolved markets (they're no longer tradable)

**Impact**: Each market gets analyzed once per meaningful state change. AI bandwidth goes to NEW markets. Cooldown becomes a rarely-triggered safety net instead of the primary gating mechanism.

**Estimated effort**: 1 day.

---

### 1.4 — Legacy position metadata backfill

**Problem**: 8 of 10 open positions have no `category`, no `question`, no `strategies`. These positions pollute: category slot tracking, strategy weight computation, and daily summary reports.

**Fix**: One-shot script to enrich legacy positions.

**Sources:**
- `auto_trades.json` — may have some trade records with metadata
- Manifold API — `/v0/market/{marketId}` returns question + category
- Research history in `market_research.json.history` — may have the original recommendation

**Changes:**
- New script: `scripts/backfill_legacy_positions.py`
- Writes enriched positions back to `paper_trading_state.json` atomically
- Logs how many positions were enriched from each source

**Impact**: Clean data for the learning loop. Accurate dashboard slot tracking.

**Estimated effort**: 0.5 day.

---

## Phase 2 — Active Portfolio Management (Week 2)

The bot currently operates like a vault: trade goes in, nothing comes out until Manifold resolves it. V2 turns this into a managed book.

### 2.1 — Position lifecycle categorization

**The idea** (user's): not all positions are equal. Some markets have obvious close dates (sports match tomorrow, election on Tuesday). Others are open-ended ("Will crypto ever be banned?"). Short-term and long-term positions should be managed differently and sized differently.

**Categories:**

| Type | Definition | Expected resolution | Slot policy |
|------|-----------|---------------------|-------------|
| **Short-term** | closeTime < 7 days | Almost certain to resolve within slot budget | Liberal — can hold many, they'll free up |
| **Medium-term** | 7-30 days | Usually resolves | Standard cap |
| **Long-term** | 30-90 days | Often drags, sometimes resolves late | Tight cap — high opportunity cost |
| **Open-ended** | >90 days or no closeTime | Likely never resolves without creator attention | Avoid entirely or cap at 1-2 |

**Detection:**
- **Primary signal**: `closeTime` from Manifold API
- **Secondary signal**: keyword patterns in question text
  - Sports: contains match/game/final/vs pattern → short-term
  - Dated political: "by Nov 2026", "before Q1" → deadline parsed from text
  - Open-ended: no time marker, abstract question → probably long-term
- **Tertiary signal**: historical resolution data for the creator (do they usually resolve on time?)

**Changes:**
- New function `classify_market_horizon(market)` in `manifold_bot/strategies.py`
- Recommendations include `horizon` field: `short` / `medium` / `long` / `openended`
- `auto_trader.py` uses horizon to choose position cap:
  - `MAX_SHORT_TERM_POSITIONS = 7`
  - `MAX_MEDIUM_TERM_POSITIONS = 4`
  - `MAX_LONG_TERM_POSITIONS = 2`
  - `MAX_OPENENDED_POSITIONS = 1` (or 0)
- Dashboard Portfolio page adds horizon column + per-horizon slot usage table

**Impact**: Bot naturally balances a mix of fast-resolving and slow-burning trades. Book never gets jammed with 10 open-ended positions. Short-term positions are encouraged — they're the ones that actually produce learning data quickly.

**Estimated effort**: 3 days (detection logic is the hard part).

---

### 2.2 — Active repricing using current market probability

**Problem**: The bot places a bet and forgets it. We never check if the market has moved against us. A $5 YES bet at 65% probability might be worth $1.50 now — we have no way to know.

**Fix**: Hourly repricing loop.

**Every cycle (hourly):**
1. For each OPEN position, fetch current probability from Manifold API (`/v0/market/{marketId}`)
2. Compute unrealised P&L using the AMM pricing formula
3. Store snapshot: `{ market_id, current_probability, unrealised_pnl, snapshot_at }`
4. Apply close-decision policy (§ 2.3)

**Unrealised P&L formula** (for YES position):
```
current_value = amount × (current_prob / entry_prob)
unrealised_pnl = current_value - amount
```
(For NO: swap the probabilities.)

**Changes:**
- New script: `scripts/reprice_positions.py` (hourly cron)
- New table in `calibration.db`: `position_snapshots` (one row per position per hour)
- `paper_trading_state.json` positions gain `last_repriced_at` and `current_unrealised_pnl` fields
- Dashboard Portfolio page shows live unrealised P&L per position

**Impact**: We actually know what our positions are worth right now. Dashboard shows real numbers instead of entry values. Enables § 2.3.

**Estimated effort**: 2 days.

---

### 2.3 — Early close and swap using real market prices

**Problem**: `position_swap_checker.py` already proposes swaps when the book is full. But it uses ad-hoc "losing position" logic. Now that we have live unrealised P&L (§ 2.2), we can build proper close/swap decisions.

**Close decision policy**:

For each open position, compute `position_score`:
```
position_score =
    + current_unrealised_pnl              # current worth
    - days_held × DAILY_DECAY_COST        # time value lost
    - (is_long_horizon ? LONG_HORIZON_PENALTY : 0)
```

Then for each position, consider three actions:
- **Hold**: position_score is positive or market is short-term close to resolving
- **Close early**: position_score is negative AND no better opportunity pending
- **Swap**: position_score is negative AND a pending recommendation has higher expected EV

**Changes:**
- Refactor `position_swap_checker.py` to use `position_score`
- Split into two scripts:
  - `scripts/evaluate_positions.py` — classify each position as hold/close/swap
  - `scripts/execute_swap.py` — execute swap proposals (already exists)
- New cron: position evaluation at :30 past the hour
- Dashboard Portfolio page: action badge (HOLD / CLOSE / SWAP) per position

**Close mechanics** (real close, not fake):
- Fetch current market probability
- Compute sale value using AMM
- Mark position as `CLOSED` with `profit = current_value - amount` (could be negative)
- Record in bet_outcomes with era `early_close`
- Free the slot

**Impact**: Bot actively manages the book. No more fake P&L=0 zeroing out. Real losses get realized, capital gets recycled, learning loop gets fed.

**Estimated effort**: 3-4 days.

---

### 2.4 — Dashboard: Position Management page

**Purpose**: Show the operator the full lifecycle of every position.

**Sections:**
- Horizon distribution (pie/bar chart of short/medium/long/openended)
- Slot usage by horizon with adaptive caps
- Per-position table with: current probability, unrealised P&L, days held, horizon, position_score, recommended action
- Historical close decisions (last 30 days): close reason, final P&L, would have been better to hold?

**Changes:**
- New page: `dashboard/src/app/position-management/page.tsx`
- New data adapter: `dashboard/src/lib/data/positions.ts`
- Pulls from `position_snapshots` table + `paper_trading_state.json`

**Impact**: Operator can see exactly what the portfolio management layer is doing and second-guess its decisions.

**Estimated effort**: 2 days.

---

## Phase 3 — Learning Unlock (Week 3-4)

The backtest pipeline exists. In this workspace it's not producing artifacts because of data coverage gaps. Fix the gaps, not the pipeline.

### 3.1 — Expanded harvest + snapshot reconstruction

**Problem**:
- `harvest_resolved.py` has pulled 2016 resolved markets
- `m2_audit` says 267 are usable for M3
- `reconstruct_snapshots.py` produced 0 reconstructed rows in the current workspace (`market_snapshots.db` has 7683 live rows, 0 reconstructed)
- Live snapshots only start from 2026-04-09, resolved corpus ends 2026-04-07 — no overlap

**Fix**: Three actions.

**3.1.1 — Bigger harvest**

Run `harvest_resolved.py --limit 10000` (up from 2000). This is the easy part. Overnight job. Adds ~8000 more resolved markets to the corpus.

**3.1.2 — Reconstruct snapshots aggressively**

Current reconstruction uses `/v0/market/{id}/bets` to fetch bet history, then replays probability at T-7/T-14/T-30 days before close. Failures fall into categories:
- **API errors**: add retry with backoff
- **Sparse bet history**: markets with <3 bets can't be reconstructed — accept and skip
- **Time coverage**: if market closed before enough bet history exists, skip
- **Malformed bets**: add schema validation, skip malformed records

Target: 2000+ reconstructed snapshots from 10000 resolved markets.

**3.1.3 — Backfill live snapshot table from historical bet replay**

Currently `market_snapshots.db` has 7683 live rows starting 2026-04-09. For any market that resolved 2026-04-09 or later AND has live snapshot data AND has resolution record — we can directly label the existing snapshot rows with the outcome. No reconstruction needed.

**Changes:**
- `harvest_resolved.py`: bump limit, add `--verbose` progress
- `reconstruct_snapshots.py`: add retry logic, better failure categorization, progress logging
- New script: `scripts/label_live_snapshots.py` — joins `market_snapshots.db` (live rows) with `calibration.db:resolved_markets` on `market_id`, writes labels

**Impact**: Goes from 127 samples to 2000+ samples. That's enough for:
- Strategy weights to diverge meaningfully (min 10-sample gate per strategy = no issue at 2000 total)
- Category accuracy to activate (8-sample gate per category × 10 categories = 80 samples needed)
- Per-bucket calibration refinement

**Estimated effort**: 2-3 days (mostly reconstruction fixes).

---

### 3.2 — Lower activation gates

**Problem**: The gates are set conservatively (strategy: 10 samples, category: 8 samples, global EV: 20 samples). Combined with the slow data collection, they block adaptation for months.

**Fix**: With 2000+ backtest samples per § 3.1, we can lower these gates without taking on noise risk.

**Proposed new gates:**
- `_MIN_SAMPLES_PER_STRATEGY`: 10 → 20 (scale with total samples)
- `_MIN_SAMPLES_PER_CATEGORY`: 8 → 15
- `_MIN_BUCKET_SAMPLES`: 15 → 25 (per (category, probability-bucket) cell)
- `MIN_SAMPLES_GLOBAL_EV`: 20 → 30

Wait — these are HIGHER, not lower? Yes. The gates were low because we had no data. With 2000+ samples, we can afford stricter statistical significance AND still activate adaptation.

**The real fix**: make gates *dependent on total sample count*, not absolute. If total samples = 50, gate each strategy at 10 (activates at small scale). If total = 2000, gate each at 30 (high confidence).

**Changes:**
- New function `compute_adaptive_gate(total_samples, base_gate)` in `compute_strategy_weights.py`
- Dashboard Learning Loop page: show current gate value with rationale

**Impact**: As data grows, gates auto-tune. No more "stuck at 1.0 forever" state.

**Estimated effort**: 0.5 day.

---

### 3.3 — Run the backtest → production weight pipeline end-to-end

**Problem**: The pipeline exists in code but hasn't produced live weight artifacts. It needs one successful end-to-end run.

**Steps** (all existing scripts):
1. `harvest_resolved.py --limit 10000` (§ 3.1.1)
2. `audit_resolved_markets.py` (M2 gate)
3. `reconstruct_snapshots.py` + `label_live_snapshots.py` (§ 3.1.2, 3.1.3)
4. `backtest_from_snapshots.py` — produces `data/backtest_report.json`
5. `compute_strategy_weights.py` — merges backtest weights with live weights
6. `build_training_dataset.py` → `data/training_dataset.jsonl`
7. Git commit the updated `strategy_weights.json`
8. Next hourly research run uses the new weights

**Changes:**
- Add monitoring: each step writes a status JSON with row counts and exit code
- Add a master script: `scripts/run_learning_pipeline.py` that runs all steps and reports status
- Dashboard Learning Loop page: "Last full pipeline run" timestamp with success/failure per step

**Impact**: From "we have scripts" to "we have live adaptive weights from real data."

**Estimated effort**: 1-2 days (mostly monitoring wrappers).

---

## Phase 4 — Multi-Source Data Expansion (Week 4-5)

The user pointed out: why limit ourselves to Manifold? There are other prediction platforms and real-world data sources with resolved markets and better coverage.

### 4.1 — Polymarket integration

**What**: Polymarket is a crypto-based prediction market platform. Has its own API.

**Why**: More resolved markets, different market-maker mechanics, cross-platform arbitrage detection.

**What we'd use it for**:
- **Backtest data**: download resolved Polymarket markets, reconstruct their pre-resolution snapshots, use as additional training data
- **Cross-platform arbitrage**: if Manifold says YES at 65% and Polymarket says YES at 80%, there's a signal

**Changes:**
- New module: `manifold_bot/polymarket_api.py` — parallel to `manifold_api.py`
- Category taxonomy mapping between platforms
- Extended `calibration.db` schema: add `source` column to `resolved_markets` ('manifold'/'polymarket')

**Impact**: Potentially 10x the training data. Cross-platform signal as a new strategy.

**Estimated effort**: 4-5 days.

---

### 4.2 — Kalshi integration (read-only)

**What**: Kalshi is a CFTC-regulated prediction market. Real-money, high-liquidity, mostly US politics and events.

**Why**: Kalshi markets tend to be more efficient (real money attracts serious traders). Using Kalshi prices as a prior for Manifold questions could be powerful — if Manifold says 40% and Kalshi says 60% on the same question, Kalshi is more trustworthy.

**What we'd use it for**:
- **Prior for AI prompts**: when analyzing a market that has a Kalshi equivalent, inject the Kalshi probability as additional context
- **Validation**: if our stat signal disagrees with both Manifold and Kalshi, we're probably wrong

**Challenges**: question matching across platforms is hard (different phrasing, different scopes).

**Changes:**
- `manifold_bot/kalshi_api.py` — read-only
- New script: `scripts/match_questions_across_platforms.py` — uses AI to find equivalent questions

**Impact**: Better priors. Quality filter on our own decisions.

**Estimated effort**: 5-7 days (question matching is hard).

---

### 4.3 — Real-world data sources for short-term markets

**What**: For short-term markets (sports, elections with known dates), real-world data sources give us ground truth faster than the market itself.

**Examples:**
- **Sports**: ESPN API, TheSportsDB, sportradar — live scores, schedules
- **Elections**: election commission feeds, state SOS websites
- **Stock market**: yfinance for earnings-related markets
- **Weather**: NOAA API for weather-dependent markets

**What we'd use it for**:
- **Faster resolution detection**: the NFL game ended 2 hours ago, we know the outcome, but Manifold creator hasn't resolved yet. We can close the position at high confidence.
- **Pre-resolution repricing**: game is tied in the 4th quarter with 2 minutes left — we know the position's true value is near 50/50 regardless of what Manifold says

**Changes:**
- New module: `manifold_bot/real_world_data.py` — plugin architecture for data sources
- Per-category source mapping (sports → ESPN, politics → election APIs, etc.)
- `reprice_positions.py` uses real-world data when available

**Impact**: Short-term positions resolve effectively at game-end, not whenever the creator gets around to it. Huge boost to book turnover.

**Estimated effort**: 5-7 days (per-category source integration).

---

### 4.4 — Metaculus for long-term calibration

**What**: Metaculus is a forecasting site with community predictions on long-term questions.

**Why**: They have a Brier score leaderboard. Their top forecasters are consistently well-calibrated. We can use their aggregated predictions as a calibration baseline.

**What we'd use it for**:
- **Backtest baseline**: "our strategy achieved Brier 0.15 on these 500 markets; Metaculus achieved 0.12" → we need to improve
- **Prior injection**: when our stats disagree strongly with Metaculus consensus, flag for manual review

**Changes:**
- Read-only scraper for Metaculus API
- New evaluation script: `scripts/evaluate_vs_metaculus.py`

**Impact**: External quality benchmark. Prevents us from fooling ourselves with in-sample metrics.

**Estimated effort**: 3 days.

---

## Phase 5 — Architecture Upgrades (Month 2)

Once the bot is actually trading and learning, the observability layer needs to level up too.

### 5.1 — Event-sourced audit log

**Problem**: The Audit page derives events from counter files (lossy). We can only reconstruct what happened from side effects.

**Fix**: Bot emits structured events to an append-only log at every meaningful action.

**Event types:**
- `market_fetched`, `strategy_fired`, `ai_invoked`, `ai_result`, `news_fetched`
- `recommendation_produced`, `trade_proposed`, `trade_rejected` (with reason), `trade_executed`
- `position_opened`, `position_repriced`, `position_closed`
- `weight_updated`, `calibration_updated`

**Storage**: SQLite table `events` in `calibration.db`, or separate `events.db`. Append-only. Indexed by timestamp and event type.

**Changes:**
- New module: `manifold_bot/event_logger.py` — single emit function
- Every auto_research.py, auto_trader.py, paper_trader.py code path emits appropriate events
- Dashboard Audit page reads `events.db` directly, gets true chronological detail

**Impact**: Full causal traceability. Dashboard no longer infers events from snapshots.

**Estimated effort**: 3-4 days.

---

### 5.2 — Dashboard API service (dashboard_api/)

**Problem**: Dashboard is static — reads file snapshots synced every 60s. Nothing is live.

**Fix**: Build out `dashboard_api/` (currently empty README) as a FastAPI service that:
1. Subscribes to the event log (§ 5.1)
2. Exposes Server-Sent Events (SSE) endpoints for the dashboard
3. Also exposes REST endpoints for on-demand queries

**Dashboard changes:**
- Pages that need live updates subscribe to SSE streams
- Status bar updates in real-time (no 60s delay)
- Overview alerts appear as soon as they fire

**Deployment:**
- `dashboard_api/` runs as a systemd service on the sandbox
- Accessible via SSH tunnel or Tailscale

**Changes:**
- Flesh out `dashboard_api/main.py` with FastAPI
- Add SSE middleware
- Dashboard: add `useEventStream()` hook for subscribing to live events

**Impact**: Dashboard becomes actually real-time. Operators see events as they happen.

**Estimated effort**: 5-7 days.

---

### 5.3 — Dashboard v2 write surface

**Problem**: Dashboard is read-only. To approve a swap, you SSH into the sandbox and run `execute_swap.py`. To change a config, you edit a Python file and redeploy.

**Fix**: Authenticated write endpoints.

**Operations we'd allow:**
- Approve / dismiss pending swaps (from Swaps page)
- Close a position early (from Portfolio page)
- Toggle the kill switch (autotrader_disabled.flag)
- Adjust key risk parameters (MAX_BET_AMOUNT, MIN_CONFIDENCE) — with audit log

**Auth:**
- NextAuth with Tailscale-only access initially
- Every write action logs to the event stream
- Sensitive ops (config changes) require confirmation dialog

**Changes:**
- Dashboard API adds POST/PUT endpoints for each write op
- Each write op validates inputs, runs as the `hackathon` user, logs to events
- Dashboard pages add action buttons where appropriate

**Impact**: Operators stop needing SSH for day-to-day actions.

**Estimated effort**: 5-7 days (mostly auth + validation).

---

## Phase 6 — ML Pipeline Revival (Month 2-3)

### 6.1 — LoRA fine-tune on expanded dataset

**Problem**: M5 eval has 8 examples. With 2000+ labeled snapshots from Phase 3, we finally have a real dataset.

**Plan:**
1. `build_training_dataset.py` generates `data/training_dataset.jsonl` with 2000+ examples (80/10/10 train/val/test)
2. Fine-tune `llama3.2:3b` with LoRA adapter on the training split
3. Evaluate on the test split — must beat crowd baseline (current Brier 0.1503)
4. If passes: deploy fine-tuned adapter alongside base Ollama
5. If fails: log why, tune hyperparameters, try again

**Gate**: Promotion only if fine-tuned Brier < crowd Brier on the test split.

**Changes:**
- Revive `finetune.py` (currently in plans, not implemented)
- Add Ollama LoRA adapter loading to `ai_analyzer.py`
- Dashboard Learning Loop page: fine-tune status + eval metrics

**Impact**: AI becomes a source of edge, not a brake. Or we find out it can't, honestly, with data to prove it.

**Estimated effort**: 2-3 weeks (model training is iterative).

---

## Prioritized Rollout Order

This is the "if I could only do one thing at a time, what order?" list:

1. **AI status fix** (Phase 1.1) — unblocks trades immediately
2. **Stat-derived EV fallback** (Phase 1.2) — unblocks EV feedback loop
3. **Analyze-once AI cache** (Phase 1.3) — unblocks AI bandwidth
4. **Legacy metadata backfill** (Phase 1.4) — cleans up current data
5. **Position horizon categorization** (Phase 2.1) — changes how the bot thinks about slot usage
6. **Active repricing** (Phase 2.2) — enables real P&L tracking
7. **Early close + swap logic** (Phase 2.3) — unblocks book turnover
8. **Position Management dashboard page** (Phase 2.4) — operator visibility on 2.1-2.3
9. **Expanded harvest + reconstruction** (Phase 3.1) — unblocks learning
10. **Lower adaptive gates** (Phase 3.2) — lets new data drive weight changes
11. **Pipeline end-to-end run** (Phase 3.3) — first real learning cycle
12. **Real-world data sources** (Phase 4.3) — faster resolution for short-term markets
13. **Polymarket integration** (Phase 4.1) — multiply training data
14. **Event-sourced audit log** (Phase 5.1) — foundation for real-time dashboard
15. **Dashboard API** (Phase 5.2) — real-time updates
16. **Kalshi / Metaculus** (Phase 4.2, 4.4) — quality priors
17. **Dashboard write surface** (Phase 5.3) — operator can act
18. **LoRA fine-tune** (Phase 6.1) — after 3 months of real data accumulation

Phases 1-3 = unblock the bot. Weeks 1-4.
Phases 4-5 = make it actually live and multi-source. Month 2.
Phase 6 = ML on top once data supports it. Month 2-3.

---

## Success Criteria

How we'll know V2 worked:

**End of Week 1 (after Phase 1):**
- AI failures stop vetoing trades
- `estimated_ev` present on every new trade
- AI bandwidth fully utilized (not 10/11 cooled down)

**End of Week 2 (after Phase 2):**
- Book turnover: average position age drops from 10 days to 3 days
- Slot usage dynamic (short-term horizons naturally free up)
- Dashboard shows live unrealised P&L per position

**End of Week 4 (after Phase 3):**
- 2000+ labeled training samples
- Strategy weights materially diverged from 1.0 on real data (not just backtest)
- Category accuracy active on 5+ categories

**End of Month 2 (after Phases 4-5):**
- External data sources feeding at least 20% of decisions
- Real-time dashboard (no 60s lag)
- Operator can act on swap proposals without SSH

**End of Month 3 (after Phase 6):**
- Fine-tuned AI beats crowd baseline on test split (or we have data to prove it doesn't and we abandon M6)

---

## What V2 Does NOT Do

Explicit exclusions to prevent scope creep:

- **Real money trading**: stays paper-only
- **Mobile dashboard**: SSH tunnel from laptop is fine
- **Multi-user auth**: single operator model for now
- **HFT / sub-hourly cycles**: hourly cadence is fine given our pool sizes
- **Full-stack rewrite**: we're building on V1 foundations, not replacing them

---

## Open Questions (to resolve before starting)

1. **How aggressive is "active repricing"?** Every hour for every position, or only when news breaks / market moves?
   - Proposed: every hour, but with rate limiting (max N Manifold API calls per minute)

2. **Where does the event log live?** SQLite in `calibration.db` or separate `events.db`?
   - Proposed: separate `events.db` — easier to archive/rotate, doesn't bloat calibration queries

3. **What's the real-world data budget?** ESPN/Kalshi/Polymarket all have rate limits and some have paid tiers.
   - Proposed: free tiers only in V2. Upgrade later if ROI justifies.

4. **Does the LoRA fine-tune happen on the sandbox or elsewhere?**
   - Proposed: training elsewhere (even 3B on CPU would take days). Inference on sandbox via Ollama LoRA adapter.

5. **How do we handle conflicts between backtest weights and live weights?**
   - Current: `compute_strategy_weights.py` has a merge function. Need to verify it handles 2000 backtest samples + 50 live samples correctly — probably should heavily weight live since it matches current market conditions.

---

*End of V2 plan. Once approved, we start with Phase 1.1 (ai_status fix).*
