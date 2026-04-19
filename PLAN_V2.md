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

## Design Principles (from reviewer, applied throughout)

1. **Honest accounting**: no fake `P&L = 0` closes. Either mark-to-market at real AMM price, or explicit `STRANDED` / `ABANDONED` status. Never pretend we know a sale price we don't.
2. **Live-trade gates stay conservative**: min samples ≥ 10 for live weight changes. Historical backtest gets a SEPARATE promotion path with its own higher sample requirement and a dampening factor — we don't mix the two.
3. **EV must work without AI**: `estimated_ev` requires a probability estimate. If AI doesn't provide one, use a stat-derived fallback. Otherwise calibration stays dark.
4. **Balance reset is an experiment, not a fix**: more paper cash doesn't teach the bot anything. If we reset, we mark it as a new "Phase 2 capital" epoch and don't pretend it's continuous performance.
5. **Make it smarter BEFORE giving it more room**: ship Phase 1 (AI fixes, EV fallback, analyze-once cache) first. THEN drop stale positions. THEN consider balance increase. Order matters because otherwise we let the bot make the same mistakes with more capital.

---

## Phase 1 — Unblock the Pipeline (Week 1) — **✅ SHIPPED (PR #12)**

**Status**: All three core fixes merged into `feature/v2-ai-smarter`, along with a Phase 1 extension (skip AI on held markets) and all of Phase 2.1 including 6 rounds of reviewer-driven fixes (persistence, rename, 3-class collapse, obsolete-value migration on positions, obsolete-value migration on recommendations). **563 tests pass** (was 513, +50 net). Phase 1.4 (legacy backfill) deferred to Phase 2 batch since it depends on stale detection infrastructure.

All V2 work stays on **one branch, one PR** (`feature/v2-ai-smarter` → PR #12) per user preference. Currently at commit `875f88f`.

The pipeline is built correctly. Specific bugs and wrong defaults are blocking it. Fix those first.

### 1.1 — Separate AI states (`ai_status` field) ✅

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

### 1.2 — Stat-derived probability fallback for EV ✅

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

### 1.3 — Analyze-once AI cache ✅

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

### 1.4 — Legacy position metadata backfill — ✅ **SHIPPED (PR #17)**

**Shipped**:
- `scripts/backfill_position_metadata.py` — one-shot, `--apply` flag required to persist (dry-run by default). Prioritises Manifold API (authoritative for `question`/`category`/`close_time_ms`) over `auto_trades.json` (recovers `strategies`/`estimated_ev`/`confidence`).
- Per-leg timestamp matching on entry-level fields so multi-leg markets keep their distinct attribution (reviewer-caught bug).
- Market-level fields (`question` fallback) search all records, not just the oldest (reviewer-caught edge case).
- 29 tests including the multi-leg + old-format-records regression cases.
- **Blocked on sandbox apply**: sandbox host offline since ~09:00 UTC 2026-04-19. Dry-run pre-outage showed 46 fields would be written across 10 legs. Apply when host returns.

**Original problem** (for reference): 9/9 open positions were missing `close_time_ms`/`term`/`resolvability`/`position_class`, which silently defeated Phase 2.1 slot-cap accounting. After apply, the book will show 9 `long_or_uncertain` + 1 `medium` — `long_or_uncertain=2` cap was 7 over.

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

## Phase 2 — Active Portfolio Management (Week 2) — **partially shipped**

**Status**:
- § 2.1 Position classification → ✅ SHIPPED (see below)
- § 2.2 Active repricing → next
- § 2.3 Early close + swap at real price → pending
- § 2.4 STRANDED state → pending
- § 2.5 Dashboard Position Management page → pending

The bot currently operates like a vault: trade goes in, nothing comes out until Manifold resolves it. V2 turns this into a managed book.

### 2.1 — Position classification: term × resolvability ✅ SHIPPED

**Final shape** (after rename + reviewer-driven simplification):

Three labels on every recommendation and position. Only one drives hard caps; the other two are soft labels for dashboard, sorting, and future ranking.

**term** (how soon the market resolves — primary from Manifold `closeTime`):
| term | definition |
|------|-----------|
| `short` | closeTime ≤ 7 days away |
| `medium` | 8-30 days away |
| `long` | >30 days away |
| `unknown` | no closeTime / invalid |

**resolvability** (how likely the market is to actually resolve — soft label):
| resolvability | category baseline |
|---------------|-------------------|
| `high` | sports, economics |
| `medium` | politics, ai_tech, entertainment, gaming, science, business, crypto |
| `low` | other; "will X ever happen" question patterns; demoted for low liquidity/bettors |

**position_class** (the ONLY cap-enforcement bucket — derived from term):

| class | cap | who lands here |
|-------|-----|----------------|
| `short` | 5 slots | any short-term market |
| `medium` | 3 slots | any medium-term market |
| `long_or_uncertain` | 2 slots | any long-term or unknown-term market |
| **Total** | **10** | |

**Why 3 classes, not 4**: a 10-slot book is small. Splitting long-term into `long_reliable` vs `long_risky` (1 slot each) made single misclassifications costly. Collapsing them into `long_or_uncertain` (2 slots) keeps almost all the signal with less fragility. `resolvability` is still persisted for soft ranking — just doesn't drive a separate hard cap.

**Why this favors learning**: short-term high-resolvability markets produce outcomes in days. The bot learns from resolved trades, so biasing toward fast-resolving markets maximizes learning rate per slot-day.

**Honest legacy handling**: positions with NO `position_class`, NO `close_time_ms`, NO `question`, NO `category` are skipped from class-cap accounting. They still count against `MAX_POSITIONS` (global cap), so the book never exceeds size. This prevents the 8 current metadata-poor legacy positions from eating all 2 `long_or_uncertain` slots during transition.

**New rejection reason**: `position_class_full` (distinct from `max_positions` so we can diagnose which bucket is bottlenecking).

**What NOT in this phase** (deferred from original plan):
- Creator `lastActive` API lookup — first-pass uses category + question heuristics only; can be added later
- Active creator-resolution-rate tracking — same

**Shipped in** (on `feature/v2-ai-smarter`, all in PR #12):

| Commit | What it did |
|--------|-------------|
| `b213e23` | Initial classification + slot caps |
| `08782b6` | Persistence fix — plumb position_class through place_paper_bet |
| `e44b910` | Rename: horizon → term, reliability → resolvability, slot_bucket → position_class |
| `0bb4a4e` | 3-class simplification (merged long_reliable + long_risky → long_or_uncertain) + honest metadata-poor handling |
| `9e704be` | Normalize obsolete values on position read |
| `875f88f` | Normalize obsolete values + legacy field name on recommendation read too |

Current at `875f88f`. 563 tests pass. 6 reviewer rounds, all findings resolved.

---

### 2.2 — Active repricing using current market probability (measure-only)

**Scope for this phase is strictly observation.** We collect real mark-to-market data hourly; we do **not** act on it. Close decisions live in § 2.3, stranded detection in § 2.4, and the live-P&L dashboard is a follow-on to § 2.5. Splitting measure from act keeps the diff auditable and lets us verify the AMM math against weeks of snapshots before any close decision depends on it.

**Problem**: The bot places a bet and forgets it. We never check if the market has moved against us. A $5 YES bet at 65% probability might be worth $1.50 now — we have no way to know.

**Fix**: Hourly repricing loop — measure only.

**Every cycle (hourly, :05 UTC, before resolve_positions.py at :10):**
1. For each OPEN position, fetch current probability from Manifold API (`/v0/market/{marketId}`)
2. Compute unrealised P&L using the AMM pricing formula
3. Append a snapshot row to `calibration.db:position_snapshots`
4. Update the live state file in place: `current_probability`, `current_unrealised_pnl`, `last_repriced_at`, `is_resolved_on_api`

**Out of scope for 2.2** (deliberately deferred):
- No close/swap decisions driven by unrealised P&L — that's § 2.3.
- No STRANDED/ABANDONED handling — that's § 2.4.
- No dashboard panel consuming the new fields — follows § 2.5.

**Unrealised P&L formula** (for YES position):
```
current_value = amount × (current_prob / entry_prob)
unrealised_pnl = current_value - amount
```
(For NO: swap the probabilities.)

**Changes:**
- New script: `scripts/reprice_positions.py` (hourly cron, :05)
- New table in `calibration.db`: `position_snapshots` (one row per position per hour; `api_error=1` rows for tolerated fetch failures so dashboards see chronic failures)
- `paper_trading_state.json` positions gain `current_probability`, `current_unrealised_pnl`, `last_repriced_at`, `is_resolved_on_api` fields
- Crontab generator (`automation/setup_cron_jobs.py`) + README updated with the :05 entry

**Impact**: We actually know what our positions are worth right now — recorded, not just computed. Enables § 2.3 (close/swap policy) and § 2.4 (STRANDED detection) to run on real data instead of guesses.

**Estimated effort**: 2 days.

---

### 2.3 — Early close using real market prices (propose-only) — ✅ **SHIPPED (PR #15)**, 4 reviewer rounds applied

**Reviewer round 1 fixes** (all green, 624 tests):

- **High** — `execute_close.py` now refetches the market right before closing and raises `MarketResolvedError` if `isResolved=True`. Previously a market that resolved between the :30 proposal and human approval would be closed at current `probability`, mislabelling true resolutions as `CLOSED_EARLY` in `bet_outcomes` and realising wrong economics on MKT/CANCEL/NO.
- **Medium** — `classify_position` now checks `last_repriced_at` freshness (`REPRICE_STALE_HOURS=2`) and emits reason `stale_reprice` when a Phase 2.2 fetch failure has left yesterday's price on state. Without this gate, a transient API outage could fire CLOSE proposals against day-old prices.
- **Medium** — `run_evaluator` now dedupes intra-run on `market_id`. A market with multiple OPEN trades (legacy averaging-in) produced two proposals per run; `close_position_early` closes ALL open trades at once so the second Telegram CTA would've been a dead end.

**Shipped in this phase** (propose-only, never auto-executes):
- `position_score()` pure function: `unrealised_pnl − days_held × DAILY_DECAY_COST − (long_or_uncertain ? LONG_HORIZON_PENALTY : 0)`
- `scripts/evaluate_positions.py` — scores every OPEN position hourly at :30, classifies as HOLD or CLOSE, emits proposals to `pending_closes.json`, sends Telegram CTAs ("approve close N" / "dismiss close N")
- `scripts/execute_close.py` — human-approved standalone close at current AMM price, free the slot
- `PaperTrader.close_position_early` already closes at real AMM price; now records `bet_outcomes` with `era='early_close'` so the weekly EV audit can segment close-at-market outcomes from true-resolution outcomes
- Config constants: `DAILY_DECAY_COST=0.05`, `LONG_HORIZON_PENALTY=0.50`, `CLOSE_SCORE_THRESHOLD=-0.50`

**Deliberately deferred**:
- **Auto-execution**: the evaluator proposes, a human approves. Thresholds are starting guesses; we tune against real data from the Phase 2.2 snapshot trail before letting the machine act.
- **SWAP proposals via position_score**: `position_swap_checker.py` continues to handle swap proposals when the book is full. Migrating its internal logic to `position_score` is a 2.3b follow-on — doing it in this PR would conflate two already-working surfaces.
- **Dashboard badges** (HOLD / CLOSE / SWAP per position): follow-on to § 2.5.
- **STRANDED / ABANDONED state**: § 2.4.

**Problem** (unchanged): `position_swap_checker.py` already proposes swaps when the book is full, but it uses ad-hoc "losing position" logic. Now that we have live unrealised P&L (§ 2.2), we have a proper close-decision score that doesn't need the book to be full to fire.

**Close decision policy**:

```
position_score = current_unrealised_pnl
               − days_held × DAILY_DECAY_COST
               − (position_class == 'long_or_uncertain' ? LONG_HORIZON_PENALTY : 0)

if score < CLOSE_SCORE_THRESHOLD (-0.50):  propose CLOSE (Telegram, human-approved)
else:                                       HOLD
```

Safety guards:
- `is_resolved_on_api=True` → HOLD (resolve_positions.py at :10 handles finalization).
- No repricing data yet → HOLD with reason `no_reprice` (next :05 will fill it in).
- A market with a non-expired pending proposal → do not re-propose. Expired proposals auto-roll so stale pending entries don't block new ones.

**Close mechanics**:
- `execute_close.py --id N` fetches live probability (falls back to snapshot price on API failure) and invokes `PaperTrader.close_position_early` which:
  - Computes sale value via AMM (YES: `amount × current_prob/entry_prob`; NO: swap)
  - Marks the trade `status='CLOSED_EARLY'`, records real P&L (can be negative)
  - Writes `bet_outcomes` row with `market_resolution='CLOSED_EARLY'`, `era='early_close'`
  - Adds realised P&L to balance, frees the slot

**Impact**: Bot can now honestly realise losses instead of holding forever. No fake P&L=0. The learning loop gets fed by `early_close` rows alongside `post_ev_fix` resolution rows. Operator stays in the loop on every close for the first tuning cycle.

**Estimated effort**: 3-4 days. **Actual: 1 day for propose-only scope**.

---

### 2.4 — Stale position detection + STRANDED / ABANDONED state — ✅ **SHIPPED (PR #16)**

**Shipped in this phase**:

- **STRANDED (auto)** — past `closeTime + 48h` grace, still unresolved → flip status to `STRANDED`, stamped with `stranded_at` + `stranded_reason`. No P&L recorded. Removed from slot accounting (all downstream filters read `status == 'OPEN'`). Reversible: `paper_trader.resolve_market*` now also picks up `STRANDED` legs so eventual resolutions reconcile to WIN/LOSS with real P&L.
- **ABANDONED (propose-only)** — past `closeTime + 90d`, still unresolved → emit proposal to `pending_abandons.json`, Telegram CTA (`approve abandon N` / `dismiss abandon N`), 72h expiry. Approval via `scripts/execute_abandon.py` calls `PaperTrader.mark_market_abandoned`: every OPEN/STRANDED leg flips to `ABANDONED` with `profit = -amount`, balance reduced, one row per leg in `bet_outcomes` with `market_resolution='ABANDONED'` and `era='write_off'` so the weekly EV audit segments terminal write-offs from real resolutions.
- **Resolved-market guard on approval** — `execute_abandon` refetches before writing the loss and raises `MarketResolvedError` if the market resolved after proposal creation. Same honest-accounting pattern as `execute_close` in Phase 2.3.
- **Config**: `STALE_GRACE_HOURS=48`, `STALE_ABANDON_DAYS=90`, `ABANDON_EXPIRY_HOURS=72`.
- **Cron**: daily at 13:00 UTC. Slow-moving signal — no reason to thrash hourly.

**Deliberately deferred**:
- **Auto ABANDONED** — propose-only first. Observe which legacy positions actually cross the 90d line, then flip to auto.
- **Still-tradable "old + losing"** — already owned by Phase 2.3's hourly `evaluate_positions` via `position_score`. Not duplicated here.
- **Creator-activity signal** (`lastActive > 30d`) — secondary, defer.
- **Dashboard STRANDED / ABANDONED sections** — Phase 2.5.

---

**Original spec (for reference)**:

**Problem**: Dead positions eat slots. But "closing them at fake $0" is lying to ourselves about our performance. We need two different outcomes depending on whether the market is still tradable.

**Two states, not one close-all-the-same approach**:

1. **Market still open + tradable** → Close early at **current AMM mark-to-market price** (real sale value, could be profit or loss)
2. **Market closed but unresolved** (past `closeTime`, creator hasn't resolved) → Move to **STRANDED** state. Removed from active book (no longer consuming slots) but NOT marked as WIN/LOSE/CLOSED. Kept in a separate column in dashboard, awaiting eventual resolution or long-timeout write-off.

This matches reality: a past-close unresolved market has no current trade price because nobody is betting. We can't sell it. But we can stop pretending it's blocking a slot.

**Detection signals** (each independently scored):

| Signal | Threshold | Action |
|--------|-----------|--------|
| Market still open, position age >14d | age > 14 AND horizon ≠ long | Re-score via § 2.3, decide hold/close/swap |
| Market closed, unresolved, grace period passed | closeTime < now - 48h AND NOT isResolved | Move to STRANDED |
| Market closed, unresolved, long timeout | closeTime < now - 90d AND NOT isResolved | Administrative write-off: mark `ABANDONED` (explicit status, NOT fake CLOSED) |
| No bet activity on market | lastBetTime > 14d ago AND market is open | Flag as low-reliability, apply close-early via § 2.3 |
| Creator inactive | Creator lastActive > 30d ago | Flag as low-reliability, avoid re-adding to book |

**Three possible outcomes per stale position** (not one):

```
evaluate_stale(position):
    market = fetch_market(position.market_id)

    if market.isResolved:
        return  # resolve_positions.py will handle it

    if market.closeTime > now():
        # Still tradable
        if position_score(position) < threshold:
            close_early_at_mark_to_market(position, market.probability)
            # Record real P&L based on AMM price
        else:
            hold()  # still has value, let it run
        return

    # Market closed, unresolved
    grace_period_end = market.closeTime + 48h

    if now() < grace_period_end:
        hold()  # creator might still resolve
        return

    if now() < market.closeTime + 90d:
        move_to_stranded(position)  # out of active slot accounting
        return

    # Long-timeout write-off
    mark_abandoned(position)  # explicit ABANDONED status
```

**STRANDED state specifics**:
- New `status` value in `paper_trading_state.json`: `"STRANDED"`
- Stored amount remains on record (we didn't recover the capital)
- NOT counted in `openPositionCount` for slot purposes
- NOT counted in realized P&L (no exit event yet)
- Shown in dashboard Portfolio page in a separate "Stranded" section
- If market eventually resolves later, a separate reconciliation script can convert STRANDED → WIN/LOSE using `resolve_positions.py`

**ABANDONED state specifics**:
- Only applied after 90 days past close with no resolution
- `status = "ABANDONED"`, `profit = -amount` (full write-off)
- Counted as realized LOSS (honest)
- Reason: at this point the money is gone, we should stop pretending otherwise
- Shown separately on dashboard — distinct from normal CLOSED trades

**Close-early mechanics** (for markets still open):
- Fetch current market probability from Manifold API
- Use AMM formula for sale value:
  - YES position: `sale_value = amount × (current_prob / entry_prob)`
  - NO position: `sale_value = amount × ((1-current_prob) / (1-entry_prob))`
- `profit = sale_value - amount` (real, could be negative)
- `status = "CLOSED_EARLY"`, `stale_reason` recorded

**What the script does**:

`scripts/detect_stale_positions.py`:
1. Load `paper_trading_state.json`
2. For each OPEN position, fetch market metadata from Manifold API:
   - `closeTime`, `isResolved`, `lastBetTime`, current `probability`
   - Creator's `lastActive` (from `/v0/user/{creatorId}`)
3. Classify into: still_tradable / past_close_grace / past_close_long / already_resolved
4. Apply the decision flow above per position
5. Write status updates atomically to `paper_trading_state.json`
6. Record outcomes in `calibration.db:bet_outcomes`:
   - `CLOSED_EARLY` with real P&L → era = `early_close`
   - `STRANDED` → era = `stranded` (no profit recorded, pending)
   - `ABANDONED` → era = `write_off` with full loss
7. Log action summary + Telegram notification

**Schedule**: Daily at 13:00 UTC (not hourly — this is slow to change, no need to thrash).

**Dashboard integration**:
- Portfolio page: add "Stranded positions" section (separate from Open)
- Add "Abandoned (write-offs)" section in Performance page
- Stale signals column with badges showing triggered conditions
- Daily summary Telegram message includes: X closed early, Y stranded, Z written off

**Impact on current state** (8 legacy positions on locally stored state):
- Most have closeTime in the past but aren't formally resolved
- Expected outcome on first run: 3-5 moved to STRANDED, 1-2 closed early at real mark-to-market, 0-2 held (still tradable and scoring well)
- Frees 4-7 slots without fake accounting
- Preserves performance record honesty

**What this is NOT**:
- Not "close everything old at fake $0" — that was the wrong earlier proposal
- Not applied to short-term positions that still have time to resolve
- Not a way to manipulate P&L — STRANDED is explicitly not counted until real resolution

**Estimated effort**: 1.5-2 days (creator activity lookup + STRANDED status wiring + dashboard updates).

---

### 2.5 — Dashboard: Position Management page

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

**Problem** (verified on local workspace 2026-04-18):
- `harvest_resolved.py` has pulled **1121 resolved markets** in `calibration.db:resolved_markets`
- `market_snapshots.db` has **7950 live rows, 0 reconstructed rows**
- `bet_outcomes` has **3 rows** (not 12)
- Live snapshots and resolved corpus don't overlap — resolved corpus ends before live snapshots started

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

### 3.2 — Separate backtest promotion from live activation gates

**Problem**: Currently we have ONE gate (`min_samples_threshold = 10`) deciding whether a weight is adopted, regardless of whether the samples came from live trades or historical backtest. That's wrong — backtest and live are fundamentally different signal sources.

**Fix**: TWO independent promotion paths, each with its own gate.

**Path A: Live-trade gate (stays conservative)**
- `MIN_SAMPLES_PER_STRATEGY_LIVE = 10` — unchanged
- A weight only activates from live data when we have 10+ real bet outcomes for that strategy
- Why conservative: real trades have real money consequences, we want strong evidence before adapting
- This is the only path that has been available so far. With current 3 bet_outcomes, we're nowhere near 10.

**Path B: Backtest promotion (new, with its own rules)**
- `MIN_BACKTEST_SAMPLES_PER_STRATEGY = 50` — higher than live because backtest is noisier (reconstruction errors)
- `MIN_BACKTEST_ACCURACY_THRESHOLD = 0.55` — only promote if accuracy is meaningfully above 50%
- `BACKTEST_WEIGHT_DAMPENING = 0.7` — a weight derived from backtest-only gets applied at 70% strength vs the full 100% it would get from live data
- Why dampened: backtest was run on reconstructed historical state which has inherent error vs what the bot actually saw in real-time

**Merge rule** (when both paths have signal):
- Live samples ≥ live gate → use live-only weight at full strength
- Live samples < live gate but backtest qualifies → use backtest weight with dampening
- Neither → weight stays at 1.0 (default, no adaptation)

**Changes:**
- `compute_strategy_weights.py`: separate live weight computation from backtest weight computation
- `strategy_weights.json` shape: add `source: "live" | "backtest" | "live+backtest"` and `confidence: 0.0-1.0` per strategy
- Dashboard Learning Loop page: show weight source per strategy with confidence indicator

**Why this matters**: the reviewer correctly pointed out that blindly lowering live gates from 10→5 would cause the bot to adapt on noise. Keeping live gates at 10 + adding a separate backtest promotion path means we can benefit from historical data NOW without taking on live-trading noise risk.

**Estimated effort**: 1 day.

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

1. ✅ **AI status fix** (Phase 1.1) — SHIPPED
2. ✅ **Stat-derived EV fallback** (Phase 1.2) — SHIPPED
3. ✅ **Analyze-once AI cache** (Phase 1.3) — SHIPPED
4. ✅ **Skip AI on held markets** (Phase 1 extension) — SHIPPED
5. ✅ **Position classification + 3-class caps** (Phase 2.1) — SHIPPED
6. ✅ **Active repricing** (Phase 2.2) — SHIPPED (PR #14)
7. ✅ **Early close evaluator + approval flow** (Phase 2.3, propose-only) — SHIPPED (PR #15)
8. ✅ **Stale detection + STRANDED/ABANDONED** (Phase 2.4) — SHIPPED (PR #16)
9. ✅ **Legacy metadata backfill** (Phase 1.4) — SHIPPED (PR #17), sandbox apply pending host return
10. **Position Management dashboard page** (Phase 2.5) — operator visibility on 2.1-2.4 **← next**
11. **Swap checker migration to position_score** (Phase 2.3b) — after ~1 week observing 2.3/2.4
12. **Expanded harvest + reconstruction** (Phase 3.1) — unblocks learning
13. **Lower adaptive gates** (Phase 3.2) — lets new data drive weight changes
14. **Pipeline end-to-end run** (Phase 3.3) — first real learning cycle
15. **Real-world data sources** (Phase 4.3) — faster resolution for short-term markets
16. **Polymarket integration** (Phase 4.1) — multiply training data
17. **Event-sourced audit log** (Phase 5.1) — foundation for real-time dashboard
18. **Dashboard API** (Phase 5.2) — real-time updates
19. **Kalshi / Metaculus** (Phase 4.2, 4.4) — quality priors
20. **Dashboard write surface** (Phase 5.3) — operator can act
21. **LoRA fine-tune** (Phase 6.1) — after 3 months of real data accumulation

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
