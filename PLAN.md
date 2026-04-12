# Manifold Trading Bot — Project Plan

## Quick Start for Devs

```bash
git clone https://github.com/amirghari/manifold-trading-bot && cd manifold-trading-bot
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then open .env and add your Manifold API key
python3 tests/test_manifold.py # verify everything works
```

> Get your Manifold API key at: https://manifold.markets/profile (bottom of the page)

---

## Repo
`https://github.com/amirghari/manifold-trading-bot`

## Team
**Null Hypothesis** — working in the OpenClaw shared workspace on a Linux server.

## Server
- **Host:** See `.env` for `SANDBOX_IP`, `SANDBOX_PASS` (user: `hackathon`)
- **Workspace:** `~/.openclaw/workspace/`
- **Local LLM (free):** `http://localhost:11434` — model `llama3.2:3b` (switched 2026-04-09; 9.3 tok/s vs 0.7 tok/s for old deepseek-r1:14b)
- **DeepSeek API:** `https://api.deepseek.com` — model `deepseek-chat`

## SSH Tunnel (run on your laptop)
```bash
ssh -L 5000:localhost:5000 hackathon@$SANDBOX_IP
```

## Git Workflow
Everyone works on branches — never directly on `main`.
```bash
git checkout main && git pull origin main
git checkout -b feature/your-task-name
# develop, write tests, run tests
git push origin feature/your-task-name
# open PR on GitHub → humans review → merge to main
```
Full rules in `AGENTS.md`.

---

## What Is Already Built

### Core Package (`manifold_bot/`)

| File | What it does |
|------|-------------|
| `manifold_bot/manifold_api.py` | Manifold Markets API client — get markets, search, place bets, get user info |
| `manifold_bot/paper_trader.py` | Paper trading engine — P&L tracking, position management, state persistence to JSON |
| `manifold_bot/strategies.py` | 5 directional strategies + 1 priority filter, organised into signal families (momentum / contrarian / fundamental / filter); Kelly criterion sizing; category-conditional probability bias |
| `manifold_bot/dashboard.py` | Performance charts and text dashboard |
| `manifold_bot/config.py` | API key (from env var `MANIFOLD_API_KEY`), trading constants |
| `manifold_bot/main.py` | Interactive CLI |

### Automation (`automation/`)

| File | What it does | Schedule |
|------|-------------|----------|
| `automation/auto_research.py` | Scans 100+ markets/hour, family deduplication, AI on top 3, saves to `market_research.json`; calls swap check at end | Every hour :00 UTC |
| `automation/auto_trader.py` | Reads research, applies risk rules, Kelly sizing, executes paper trades | Every hour :20 UTC |
| `automation/daily_summary.py` | Calculates daily P&L metrics, sends to Telegram via Bot API | Daily 19:00 UTC |
| `automation/send_telegram.py` | Telegram Bot API sender — retries, Markdown fallback, no library needed | Called by all scripts |
| `automation/telegram_bot.py` | `/portfolio`, `/positions`, `/scan` command handlers | Called by OpenClaw skills / manually |
| `automation/setup_cron_jobs.py` | Utility to recreate OpenClaw cron jobs if needed | Manual |

### Calibration (`scripts/`)
| File | What it does |
|------|-------------|
| `scripts/harvest_resolved.py` | Fetches 1,000+ resolved markets from Manifold API → `data/calibration.db` |
| `scripts/analyze_calibration.py` | Measures crowd bias per probability bucket + category → `data/calibration_table.json` |
| `scripts/weekly_ev_report.py` | Queries `bet_outcomes`, computes EV accuracy ratio by strategy + confidence band |
| `scripts/resolve_positions.py` | Polls Manifold API for resolved positions, frees slots in `paper_trading_state.json` |

### Dev Tools (`scripts/`)
`demo_bot.py`, `show_portfolio.py`, `get_market_ids.py`, `fix_portfolio.py`, `watch_demo.py`

### Tests (`tests/`)
`test_manifold.py` — API connectivity and core
`test_automation.py` — automation script smoke tests

---

## Live Cron Jobs (OpenClaw)

| ID | Name | Schedule |
|---|---|---|
| `359e61eb-...` | Manifold Market Research | `0 * * * *` UTC (hourly :00) — swap check runs inside this |
| `8d2a66ec-...` | Position Resolution | `10 * * * *` UTC (hourly :10) |
| `00695c33-...` | Manifold Auto Trading | `20 * * * *` UTC (hourly :20) |
| `0a77ecb9-...` | Daily Trading Summary | `0 19 * * *` UTC |
| `e9a54afc-...` | Weekly Calibration Harvest | `0 2 * * 0` UTC (Sundays) |
| `d5a01246-...` | Weekly EV Accuracy Report | `0 7 * * 1` UTC (Mondays) |

Note: Position swap check (`f426953c`) was a separate `:40` cron — **disabled**. `run_swap_check()` is now called directly by `auto_research.py` after each research run so it only fires when fresh opportunity data exists.

---

## Current Trading State (as of 2026-04-10)

- **Paper balance:** ~$232 (heavy early losses from duplicate bets; throttled at $5/trade until weights signal real edge)
- **Open positions:** 11/10 (auto_trader.py blocks new trades; awaiting resolution)
- **MAX_BET_AMOUNT:** $5 — throttled while backtest pipeline builds signal. Restore to $25 once `probability_bias` weight clears 1.0 (expected after 2026-04-13 Sunday harvest).
- **Auto-trader confidence threshold:** 65%
- **Max positions per category:** 3 (enforced in `should_trade_market()`; reduces to 1 if category accuracy < 50% with ≥8 samples)
- **Strategy weights:** all 1.0 (6 live resolved trades — below 10-sample gate; `probability_bias` backtest shows 72% accuracy on 25 samples, 5 short of 30-sample gate)
- **Reconstructed snapshots:** 127 rows in `market_snapshots.db` (source='reconstructed') from 267 usable markets
- **Trade logging:** `auto_trades.json` (all trades include `estimated_ev`, `category`, `question`)
- **Active branch:** `main`
- **Agent auto-reviewer:** DISABLED — caused file truncation bugs on 3/4 PRs. Human review only.
- **Telegram bot:** Live — `/portfolio`, `/positions`, `/scan`, `/autotrader on/off`. Address bot: `@hackathon_26_bot /portfolio`
- **Ollama:** `llama3.2:3b` on server (switched 2026-04-09); keep_alive 4h; FIRST_TOKEN_TIMEOUT 90s. If stuck (700%+ CPU): `kill $(pgrep -f "ollama runner") && nohup ollama serve > /tmp/ollama.log 2>&1 &`

---

## What Is NOT Built Yet

Pick whatever interests you. Create a branch, build it, open a PR.

---

### ✅ AI Integration — DONE (branch: feature/ai-analyzer, PR #2)

- [x] `manifold_bot/ai_analyzer.py` — calls local Ollama (`llama3.2:3b`) first, falls back to DeepSeek API
  - Returns: `{ "recommendation": "YES/NO/SKIP", "confidence": 0.0-1.0, "estimated_true_probability": 0.0-1.0, "reasoning": "...", "risk_factors": "..." }`
- [x] Wired into `auto_research.py` — AI runs on top 3 candidates per hourly cycle (`_AI_CANDIDATE_COUNT = 3`)
  - If AI agrees AND stat ≥ 0.65: `0.4 × stat + 0.6 × AI`, capped at `stat + 0.10` (max 10pp boost)
  - If AI agrees AND stat < 0.65: blended result capped at 0.64 — AI cannot rescue a weak stat signal
  - If AI disagrees: confidence scaled down by `0.4 + (1 - ai_conf) × 0.4` — certain AI disagreement keeps only 40%
- [x] Fixed volume spike `avg_volume` — now uses **median** of fetched markets (mean was skewed by outliers)
- [x] 24 tests in `tests/test_ai.py` — all passing (includes full blending logic coverage)
- [x] Ollama confirmed running on server: `llama3.2:3b` loaded, ~5-10s/response on CPU (capped to 3 markets — worst case ~30s well within 300s cron limit)
- [x] `MIN_CONFIDENCE` moved to `config.py` — single source of truth for trading threshold across trader + researcher
- [x] DeepSeek API fallback: explicit `delay=2` at call site, `per_market_timeout=90` to prevent hung calls
- [x] Schema version written dynamically — v2 only if AI pass completed; v1 if aborted (blocks trading)
- [x] Schema version guard in `auto_trader.py` — handles both flat and nested file layouts, refuses to trade on v1
- [x] Auto-fixing review agent in `.github/scripts/claude-review.js` — reviews PRs and commits fixes directly
- [x] PR #2 merged to main
- [x] **Distillation logging** — every LLM call logged to `logs/llm_calls.jsonl` (gitignored): timestamp, model, market_id, system_prompt_sha256, parsed_output. Log rotates at 100MB, 3 backups. Raw responses, full prompts, and chain-of-thought intentionally excluded (compliance). PR #5 merged 2026-04-06.
- [x] PR #3 (addDistillation) closed — all changes included in PR #5

> No new API keys needed. Ollama is free/local. DeepSeek API key already on server.

> **⚠️ Known regression:** `MIN_CONFIDENCE` was changed from 0.65 → 0.60 by the risk-parameters PR. `_WEAK_STAT_CAP` is derived as `MIN_CONFIDENCE - 0.01`, so it's now 0.59 instead of 0.64, causing 4 test failures in `tests/test_ai.py`. `_STAT_BOOST_FLOOR` was hardcoded to 0.65 as a workaround (PR #6 auto-fix) so the AI boost logic is decoupled from the regression, but `_WEAK_STAT_CAP` still tracks `MIN_CONFIDENCE`. Recommended: revert `MIN_CONFIDENCE` to 0.65 in `config.py`.

---

### ✅ Strategy Improvements — DONE (PR #6, merged 2026-04-06)

- [x] New pre-filter `is_stale_market`: skips markets with last bet > 72h AND volume24h < 10
- [x] `probability_direction_strategy`: now skips 45–55% coinflip zone; requires `lastBetTime` < 48h
- [x] `mean_reversion_strategy`: skips when `uniqueBettorCount > 100` (don't fight genuine consensus)
- [x] `volume_spike_strategy`: uses `volume24Hours` vs batch **median** (not all-time volume); parameterised `spike_multiplier=2.0`
- [x] NEW `creator_disagreement_strategy` (confidence 0.75): fires when `abs(resolutionProbability − probability) ≥ 0.15`; bets toward creator's estimate
- [x] NEW `thin_market_strategy` (confidence 0.65): fires when AMM pool imbalance > 0.70; bets toward thin side
- [x] **Kelly Criterion wired into `auto_trader.py`**: `calculate_position_size` now accepts `(recommendation, outcome)` and uses `ai_estimated_probability` for proper Kelly sizing; falls back to confidence-scaled if no AI estimate; returns 0.0 on negative edge (trade skipped)
- [x] **Kelly bug fixed**: old guard `payout_ratio <= 1` silently returned 0 for YES bets on markets above 50%. Fixed to `payout_ratio <= 0`.
- [x] AI candidate selection uses recency boost (sorted by `lastBetTime`) instead of pure confidence rank
- [x] `_STAT_BOOST_FLOOR` hardcoded to 0.65 (decoupled from MIN_CONFIDENCE regression)
- [x] Agent auto-fix (commit 3c98ce3) truncated 3 files — restored in follow-up commit bb0c6b5

---

### ✅ Signal Family Architecture + Code Quality — DONE (2026-04-07)

Prevents correlated strategies from stacking as independent evidence, and fixes ranking/guard bugs found in code review.

**Signal families:**
- [x] `SIGNAL_FAMILIES` dict in `strategies.py` — each strategy labelled `momentum`, `contrarian`, `fundamental`, or `filter`
- [x] Family-aware aggregation in `auto_research.py` — at most 1 signal per directional family (highest confidence wins within a family); `thin_market` (filter) is confirmation-only, only kept when a `contrarian` or `fundamental` signal agrees
- [x] `volume_spike_priority` renamed from `volume_spike_strategy` — returns `float 0.0–1.0` (priority boost for candidate ranking), **not** a directional YES/NO signal. Stored as `priority_boost` on each recommendation dict
- [x] Composite candidate ranking: `confidence + priority_boost × 0.15` — priority_boost actually used in sort key (was computed but discarded before this fix)
- [x] `probability_direction_strategy` — added `volume24h >= 5` guard (was firing on zero-volume markets)
- [x] `mean_reversion_strategy` — threshold tightened 0.80 → 0.85; added close-time guard (skips markets resolving in < 7 days — not enough time to revert)
- [x] `probability_bias_strategy` — category-conditional: skips `ai_tech` (0.19pp bias, < 4pp threshold) and `economics` (1.51pp, < 4pp threshold); fires on `politics` (+6pp), `crypto` (+5pp), and others above threshold

**Code quality:**
- [x] `_AI_CANDIDATE_COUNT = 3` constant enforced — selects exactly 3 markets for AI, marks exactly 3 as `ai_was_candidate=True` in research output
- [x] `test_manifold.py` rewritten as 7 fully mocked unit tests (no network required); `--live` flag for manual smoke testing against real API
- [x] `auto_research.py` reporting hygiene — research always runs (removed the early-exit when at max positions); reporting no longer inflates AI hit counts

**Bug fixes:**
- [x] Zero/negative Kelly in `execute_swap.py` pre-validated BEFORE closing position — previously a zero-Kelly new trade would close the existing position but then abort, leaving the portfolio in a half-modified state
- [x] Duplicate-bet guard (`should_trade_market`) queries live `self.trader.positions` — ignores stale `existing_position=False` flag from research JSON (the 2czul2Rync bug: repeated bets on same market)
- [x] Regression tests for duplicate-bet bug added to `test_strategies.py`
- [x] `datetime.utcfromtimestamp()` (deprecated) → `datetime.fromtimestamp(ts/1000, tz=timezone.utc)` in `harvest_resolved.py`

**Tests:** 173 total across all suites (up from 137)

---

### ✅ Position Resolution Loop — DONE (feature/position-resolution)

The bot was permanently frozen at max positions with no mechanism to detect when markets resolved on Manifold.

- [x] `scripts/resolve_positions.py` — polls Manifold API for each open position, calls `PaperTrader.resolve_market()` on any that have settled, prints before/after slot count
- [x] `PaperTrader.auto_resolve_markets()` was already implemented in `paper_trader.py` — script is a thin wrapper
- [x] New cron job `position-resolution` at `:10` every hour (between research `:00` and trading `:20`)
- [x] Added to `test_automation.py` test suite

---

### ✅ Calibration & Feedback Loop — DONE (branch: feature/calibration, 2026-04-07)

**Step 1 — Harvest resolved markets**
- [x] `scripts/harvest_resolved.py` — fetches resolved binary markets from Manifold API, saves to SQLite
  - Fields: `market_id`, `question`, `probability_close`, `outcome` (YES/NO), `close_date`, `unique_bettors`, `category` (keyword-inferred)
  - 1,121 resolved markets harvested as starting corpus; `data/calibration.db` created on server
- [x] Weekly cron registered on server: `weekly-harvest` — Sundays 02:00 UTC (ID: `e9a54afc-...`)
  - Runs `harvest_resolved.py --limit 2000 && analyze_calibration.py` to keep table fresh

**Step 2 — Measure where the crowd is wrong**
- [x] `scripts/analyze_calibration.py` — groups markets by 10pp probability bucket, measures actual YES rate
  - Output: `data/calibration_table.json` — committed to git (small JSON, versioned over time)
  - Key finding: **+18pp overconfidence at 60-70%** (crowd says 65%, actual YES rate 47%)
  - Also breaks down by category (sports/crypto/politics/ai_tech/economics/science/other)
  - Sports and economics: well-calibrated. Politics: +6pp overestimate. Crypto: +5pp.

**Step 3 — Feed calibration into AI prompts**
- [x] `_CALIBRATION_NOTE` built at module import from `data/calibration_table.json` and injected into `ANALYSIS_PROMPT` in `ai_analyzer.py`
  - AI now sees a 10-row correction table in every prompt; worst bucket flagged with `← MOST BIASED`
  - Degrades gracefully: if file is absent, prompt is unchanged (empty string)
  - Instructs AI: "if a market is at 65%, history says treat it as ~47% YES — adjust estimated_true_probability"

**Step 4 — Close the loop: EV-based outcome tracking**
- [x] `estimated_ev` stored at trade time in `auto_trades.json` and in the position record in `paper_trading_state.json`
  - `auto_trader.py` computes EV before calling `place_paper_bet()` so it flows into the position record (fixed — was previously passed as None)
  - `auto_research.py` computes `estimated_ev` on every recommendation using a $25 reference bet so `position_swap_checker` can rank opportunities
  - `place_paper_bet()` accepts `estimated_ev`, `ai_confidence`, `strategies` as optional params
- [x] `paper_trader.py` writes to `bet_outcomes` SQLite table on every `resolve_market()` call:
  - Schema: `market_id, our_recommendation, amount, probability, estimated_ev, ai_confidence, strategies (JSON), market_resolution, actual_pnl, ev_error, resolved_at`
  - `ev_error = estimated_ev − actual_pnl` (model error; NULL when no AI estimate)
- [x] `scripts/weekly_ev_report.py` — queries `bet_outcomes`, computes EV accuracy ratio, breakdowns by strategy and confidence band; outputs to console + `telegram_weekly_ev.txt`
  - Interpretation: ratio 1.0 = well-calibrated, ratio 0.3 = AI overestimates edge by 3×
- [x] `automation/daily_summary.py` appends weekly EV section on Mondays
- [x] `weekly-ev-report` cron registered on server — Mondays 07:00 UTC (ID: `d5a01246-...`)

**Why EV and not just confidence:**
Confidence measures how sure the AI is. EV measures how much money we expect to make. Two trades at identical confidence can have completely different EV depending on the market's current price (which determines the payout). Tracking win-rate by confidence band misses this — tracking EV vs actual P&L directly measures whether the model's probability estimates are accurate in dollar terms.

---

### ✅ Smart Position Swap — DONE (branch: feature/smart-swap)

When all positions are full the bot was frozen. This feature turns `max_positions` from a hard block into a dynamic portfolio manager with human approval via Telegram.

**How it works:**
1. Cron job at `:40` runs `scripts/position_swap_checker.py`
2. Scans ALL open positions, fetches current probability from Manifold API, computes unrealised P&L for each
3. Finds all losing positions (unrealised_pnl < 0), sorted worst-first
4. For each loser, finds the best unique replacement from `market_research.json` (confidence ≥ 65%, AI not SKIP, positive EV)
5. Proposes a swap only if `new_opportunity_ev > abs(unrealised_loss)` — crystallising a loss is only worth it if the new trade is clearly better
6. Writes all qualifying proposals to `pending_swaps.json` as a numbered list
7. Sends one Telegram message per proposal — each clearly numbered for selective approval

**Human interaction (Telegram):**
- Bot posts: `♻️ Swap Proposal #1` ... `Reply "approve swap 1" to execute. Reply "dismiss swap 1" to skip.`
- Multiple proposals in one run → multiple numbered messages
- Each proposal independent — approve one, dismiss another
- OpenClaw executes: `python3 scripts/execute_swap.py --id 1` or `--id 2 --dismiss`
- Proposals expire after 4h; checker re-evaluates next `:40`

**Files built:**
- [x] `manifold_bot/paper_trader.py` — added `entry_probability`, `entry_confidence` to every trade record; added `close_position_early(market_id, current_prob)` method
- [x] `scripts/position_swap_checker.py` — finds all weak positions, pairs each with unique opportunity, writes `pending_swaps.json`, sends Telegram per proposal
- [x] `scripts/execute_swap.py` — `--id N` approves, `--id N --dismiss` dismisses; sends confirmation to Telegram
- [x] `automation/setup_cron_jobs.py` — swap check is NOT a separate cron; `run_swap_check()` is called at the end of `auto_research.py` after every research run
- [x] `tests/test_swap.py` — 61 tests covering all paths (maths, filtering, flag-file logic, integration, zero-Kelly pre-validation, EV pipeline regression)

**Cron schedule (current):**
```
:00  Market Research  (swap check runs inside this at end)
:10  Position Resolution
:20  Auto Trading
19:00 daily  Daily Summary
Sun 02:00    Weekly Harvest
Mon 07:00    Weekly EV Report
```

---

### ✅ Real Telegram Bot — DONE (2026-04-08)

No external library needed — uses plain HTTP to the Telegram Bot API.

- [x] `automation/send_telegram.py` — real Bot API sender; 3 retries with backoff; falls back to plain text on Markdown parse errors; truncates at 4096 chars
- [x] `automation/telegram_bot.py` — five command handlers: `portfolio`, `positions`, `scan`, `autotrader-on`, `autotrader-off`
  - `main()` calls `send_message()` only — no `print(response)`. OpenClaw skill runner also captures stdout, so printing caused every reply to send twice. Fixed.
  - `/positions` shows real market titles: 3-tier fallback — stored `question` field → research data lookup → Manifold API fetch for old trades that predate the field
  - Run: `python3 automation/telegram_bot.py portfolio|positions|scan|autotrader-on|autotrader-off`
- [x] `manifold_bot/config.py` — `TELEGRAM_BOT_TOKEN` and `TELEGRAM_GROUP_ID` read from `.env`
- [x] `automation/daily_summary.py` — sends directly to Telegram via Bot API
- [x] OpenClaw skills registered: `skills/portfolio/`, `skills/positions/`, `skills/scan/`, `skills/autotrader-on/`, `skills/autotrader-off/`
- [x] `manifold_bot/paper_trader.py` — `_notify_resolution()` sends Telegram on every resolution; never raises
- [x] `automation/auto_trader.py` — checks `autotrader_disabled.flag` at startup; exits cleanly if present

**How to use in Telegram:**
```
@hackathon_26_bot /portfolio       ← balance + P&L
@hackathon_26_bot /positions       ← all open trades with real titles
@hackathon_26_bot /scan            ← top opportunities
@hackathon_26_bot /autotrader off  ← pause trading
@hackathon_26_bot /autotrader on   ← resume trading
```

---

### ✅ Category Exposure Caps — DONE (2026-04-08)

- [x] `manifold_bot/config.py` — `MAX_POSITIONS_PER_CATEGORY = 3`
- [x] `automation/auto_research.py` — `_infer_market_category()` called per recommendation; `category` stored in rec dict
- [x] `manifold_bot/paper_trader.py` — `place_paper_bet()` accepts `category` and `question`; stored in trade record
- [x] `automation/auto_trader.py` — `should_trade_market()` counts open positions per category, rejects if at cap; passes `category`/`question` through to `place_paper_bet()`
- [x] `automation/daily_summary.py` — `calculate_daily_metrics()` builds `category_breakdown` dict; displayed as `🗂 BY CATEGORY` bar chart in daily report

---

### ✅ Weighted Scoring Phase A + D — DONE (2026-04-08)

**Phase A — Strategy reliability weights**

- [x] `scripts/compute_strategy_weights.py` — reads `bet_outcomes` (post_ev_fix era); computes direction accuracy per strategy; weights range 0.5–1.2; min-sample gate (10 per strategy); writes `data/strategy_weights.json`
  - **Bug fixed (2026-04-08):** strategies stored as JSON array by `paper_trader.py` (`json.dumps`); script was splitting on comma giving malformed tokens. Fixed with `_parse_strategies()` — JSON-first, CSV fallback.
  - **Coverage fixed (2026-04-08):** `_KNOWN_STRATEGIES` now covers all 6 active strategies (`probability_direction`, `mean_reversion`, `volume_spike`, `probability_bias`, `creator_disagreement`, `ai_analysis`). `thin_market` excluded intentionally — confirmation-only, never sole trigger in a `bet_outcomes` row.
- [x] `data/strategy_weights.json` — committed; all 1.0 right now (0 post-fix resolutions yet); auto-updates once trades accumulate
- [x] `automation/auto_research.py` — loads weights once per run; applies to each raw signal's base confidence before family dedup; clamped to [0.0, 1.0]

**Phase D — Self-performance note in AI prompt — REMOVED (2026-04-10)**

- [x] ~~`_build_performance_note()`~~ removed from `ai_analyzer.py`. Caused triple-counting: strategy weights already applied in stat confidence scaling (`auto_research.py`) and Kelly sizing (`auto_trader.py`). Telling the AI "mean_reversion has 56% accuracy" adds no independent signal — it just biases the same prior the stat layer already acted on.
- Replaced by: `_build_query_calibration_note(question, probability)` — infers category from the question text, looks up the `(category, bucket)` cell in `calibration_table.json`, returns a single-sentence prior: "Politics markets in the 60-70% bucket resolved YES 54% of the time (n=142, bias=+6.2pp)." Tells the AI what to expect from THIS type of market — directly actionable, not historical noise.

> Cron registered: `compute_strategy_weights.py` — Mondays 07:30 UTC (live weights). `backtest_from_snapshots.py` — Sundays 03:30 UTC (backtest fills gaps). Both write to `data/strategy_weights.json`.

---

## Execution Plan — Status as of 2026-04-10

**Context:** Live infra is strong. The adaptation pieces (strategy weights, category accuracy) are scaffolding fed by sparse data — no resolved live trades yet. The bot is losing because the feedback loop is too weak to tell us what actually works. Expanding live complexity before fixing the measurement system is the wrong move.

### ✅ Session 1 (2026-04-09): Risk throttle + M2 audit + M3 reconstruction — DONE

1. **Risk throttle** — `MAX_BET_AMOUNT` $100 → $5. Bot keeps running (M1 snapshots accumulate), but can't blow remaining balance. ✅
2. **M2 audit** — 267/1121 usable markets (YELLOW gate). ✅
3. **M3 reconstruction** — 127 reconstructed snapshots. 3 code review bugs fixed. ✅

### ✅ Session 2 (2026-04-10): Backtest pipeline + automation — DONE

- Built `backtest_from_snapshots.py` — bridges M3 snapshots into strategy weights.
- Fixed `per_strategy_samples` gap — live weights can now correctly override backtest once gate clears.
- `probability_bias`: 72% accuracy / 25 samples. 5 short of 30-sample gate.
- `mean_reversion`: 0 signals — structurally incompatible with M2 quality filter.
- **Decision gate result:** Weights did NOT shift from 1.0 this run (5 samples short). Re-evaluate after 2026-04-13 harvest.
- Automated the Sunday M2→M3→backtest chain (02:30/03:00/03:30 UTC crontab).

### ✅ Session 3 (2026-04-10): Phase B+C + Category Architecture — DONE

- Phase B: dynamic Kelly scaling from strategy weights (`kelly_fraction = 0.5 × weight`)
- Phase C: per-category accuracy → adaptive caps (cap → 1 when accuracy < 50% with ≥8 samples)
- Category single-source-of-truth: `_infer_market_category()` in `strategies.py` canonical; `harvest_resolved.py` imports alias
- "other" category uncapped: `_effective_category_cap("other")` returns `max_positions`

### ✅ Session 4 (2026-04-10): Category Fixes + Double-Counting Documented — DONE

- Fixed category data flow in Phase C (snapshot isolation, correct category propagation)
- Word-boundary matching in `_infer_market_category()` to avoid false matches ("Windows" ≠ "win")
- Documented double-counting of strategy weights across stat/AI/Kelly layers
- 210 tests passing

### ✅ Session 5 (2026-04-10): Double-Counting Fix + by_category Weights — DONE

- Removed `_build_performance_note()` from AI prompt — weights belong in stat/sizing layers only
- Added `_build_query_calibration_note(question, probability)`: query-specific prior looks up `(category, bucket)` cell, falls back to global
- `compute_strategy_weights.py` now emits `by_category` weights; fallback chain: `by_category[cat][strat]` → `global[strat]` → `1.0`
- `_get_strategy_weight(strategies, category)` rewritten with `max()` generator (fixes silent suppression of penalty weights < 1.0)
- Trade log records category-inferred weight at trade time (accurate audit)
- Fixed bias direction wording: positive → "overestimates", negative → "underestimates", zero → "well calibrated"
- Fixed `test_swap.py` 8 pre-existing failures: added `liquidity` to helper, amounts fixed to `MAX_BET_AMOUNT`
- 296 tests total (81 test_strategies + 136 test_calibration + 66 test_swap + 7 test_manifold + 6 test_automation)

---

## Roadmap — Priority Order

Two parallel tracks. Neither blocks the other.

---

### Track 1 — Trading Quality (improves the live bot)

#### ✅ P1 — Weighted Scoring Phase B + C — DONE (2026-04-09)

Phase A is live. Phase B+C closes the Kelly sizing loop using data already in `bet_outcomes`.

**Phase B — Dynamic Kelly scaling from strategy weights**

- [x] `auto_trader.py` — reads `data/strategy_weights.json` at startup; scales Kelly fraction: `kelly_fraction = 0.5 × weight`. Weight 0.5 → 25% Kelly, weight 1.0 → 50%, weight 1.2 → 60%.
- [x] `_get_strategy_weight()` — takes max weight across all strategies in a rec (best signal drives size)
- [x] `strategy_weight_at_trade_time` added to trade log in `auto_trades.json`
- [x] Guard implicit: strategies below min-sample threshold are stored as weight=1.0 → standard half-Kelly

**Phase C — Category-level accuracy → adaptive caps**

- [x] `bet_outcomes` schema migration: `ADD COLUMN category TEXT`; `_write_bet_outcome` stores category from trade record
- [x] `scripts/compute_strategy_weights.py` — `_compute_category_accuracy()` added; writes `data/category_accuracy.json`
- [x] `auto_trader.py` — `_effective_category_cap()` reduces cap 3 → 1 when category accuracy < 50% with ≥8 samples
- [x] Complements the static calibration table: crowd bias corrects probability estimates; own trade history corrects exposure per topic

#### ✅ P2 — Register weekly-strategy-weights cron — DONE (2026-04-09)

- [x] OS crontab: Mondays 07:30 UTC — runs `compute_strategy_weights.py`, commits `data/strategy_weights.json` + `data/category_accuracy.json`

#### ✅ P3 — News Fetcher (done 2026-04-10)

Adds real current-event awareness to the fundamental family. Addresses the knowledge cutoff problem — markets about near-future events need fresh context, not a frozen training corpus.

- [x] `manifold_bot/news_fetcher.py` — extracts keywords from market question, queries NewsAPI.org (free tier, no cost)
- [x] Returns: recent headlines with publication date
- [x] `news_strategy` in `strategies.py` — `family: "fundamental"`, confidence 0.60–0.80 based on headline recency and relevance
- [x] Wire into `auto_research.py` — top 3 candidates per cycle (72 max req/day on free tier); recency-scaled confidence modifier

#### ✅ Op1 — Operational unblock (done 2026-04-11)

The bot was jammed: `11/10` positions, one pending swap blocking all further swap proposals. Dismissed manually; swap loop unblocked. The 11th ghost position (`ZLCUqRRE85`, no question text) remains by user instruction.

- [x] Dismiss stale pending swap
- [x] Verify swap loop resumes on next `:20` cron cycle

#### ✅ Op2 — Swap EV correctness (done 2026-04-11, commit `d7a97b8`)

**Bug that was shipped:** [auto_research.py:685](automation/auto_research.py#L685) computed `estimated_ev` at a fixed `_EV_REF = $25` reference size for cross-market ranking. [position_swap_checker.py:313-321](scripts/position_swap_checker.py#L313) then compared a **real unrealised loss** against this **$25 reference EV**, overstating swap attractiveness by `$25 / MAX_BET_AMOUNT = 5×` under the throttle. The WTI pending swap was the evidence: loss `-$1.99`, ref-EV `+$4.09`, exec-EV `+$0.82` — it only looked good because we were measuring against the wrong number.

- [x] `auto_research.py` — writes BOTH `estimated_ev_ref` ($25) AND `estimated_ev_exec` (`MAX_BET_AMOUNT`) on every recommendation; legacy `estimated_ev` kept as alias for ref
- [x] `position_swap_checker.py` — new `_swap_ev()` helper prefers exec and falls back to legacy field; `find_best_opportunity` ranks by exec EV; `ev > loss` gate uses exec EV
- [x] `tests/test_swap.py` — 8 new tests in `TestSwapEvCorrectness`, including explicit WTI regression guard
- [x] **Verified in production 2026-04-11 11:40 UTC:** rejected a `USpnpA6Idq` swap with message "best exec EV $0.51 does not justify $2.63 loss"

#### ✅ Op3 — Strategy observability (done 2026-04-11, commit `6580c7d`)

Per-run counters across research + trader. Evidence-first operation before Op5 retune.

**Research counters** ([automation/auto_research.py](automation/auto_research.py) → `data/research_counters.jsonl`)
- [x] markets fetched / skipped by reason (resolved, low_liquidity, stale)
- [x] raw fires by strategy (5 directional strategies)
- [x] family dedup winners (momentum / contrarian / fundamental)
- [x] thin-market confirmations, no-signal drops, tied-vote drops
- [x] AI flow (eligible / cooled_down / analyzed / agree / disagree / skip / no_result)
- [x] final mix by strategy-set and by category

**Trader counters** ([automation/auto_trader.py](automation/auto_trader.py) → `data/trader_counters.jsonl`)
- [x] recs loaded, rejections by reason (9 keys), passed filter, top-5 by exec EV, trades executed
- [x] `_trade_rejection_reason()` returns string key; `should_trade_market()` kept as bool wrapper

**Tests** — [tests/test_observability.py](tests/test_observability.py) 31 tests, including the schema v2 invariant (see below).

#### ✅ Op3.1 — Schema v2 zero-candidate fix (done 2026-04-11, commit `88b8999`)

**Production bug caught by reviewer:** when every above-floor rec was on AI-timeout cooldown, `candidate_markets` became empty, the inner `if candidate_markets:` block never ran, recs were missing the `ai_was_candidate` field, and `save_research()` fell back to `schema_version=1`. That halted the trader for the rest of the cycle.

- [x] Initialize AI metadata defaults on every rec BEFORE the candidate pass
- [x] 6 new tests covering `TestSchemaV2Invariant` + `TestAnalyzeMarketsZeroCandidatePath`

#### ✅ Op4 — Live snapshot quality (`days_before_close`) (done 2026-04-11)

Forward path + historical backfill for the live snapshot training pipeline.

- [x] `auto_research.py::_write_market_snapshots()` — computes `days_before_close` from each market's `closeTime` at snapshot time; clamps already-closed observations to 0; `None` when `closeTime` missing or malformed
- [x] `scripts/backfill_days_before_close.py` — one-off backfill for existing NULL live rows; fast local lookup via `resolved_markets.close_date`, optional `--no-api` mode, idempotent
- [x] `scripts/build_training_dataset.py::_snap_live_to_window()` — bucket to nearest window `{7, 14, 30}` within ±1 day tolerance, earliest snapshot per window wins
- [x] `tests/test_days_before_close.py` — 30 tests
- [x] **Server backfill: 4384 of 4605 rows populated (95.2%)** in ~72s
- [x] Training dataset rebuilt: still 127 rows because **0 of 16 qualifying live markets have resolved yet** — Op4 is pre-positioning data

**Expected future effect:** as the 4500 backfilled live rows slide through the T-7/14/30 windows and their markets resolve, the dataset grows week-over-week without code changes.

#### ✅ Op5 — Strategy guard retune (done 2026-04-11, commit `762c43d`)

**Not** a base-confidence retune. Op3 counter evidence showed three strategies were silent at the guard level, so retuning confidences would have been pointless.

**probability_bias — per-bucket lookup + lowered threshold**
- [x] `_load_calibration_data()` loads both category aggregates AND per-(category, bucket) cells from `calibration_table.json` at import time
- [x] New `_bucket_bias_for(category, probability)` helper returns per-bucket bias when cell has `sample_size ≥ _MIN_BUCKET_SAMPLES (15)` and `reliable=True`
- [x] `probability_bias_strategy()` tries per-bucket first, falls back to category aggregate
- [x] Removed the hardcoded 60-70% / 30-40% bucket filter — any bucket with reliable calibration data now qualifies
- [x] `_MIN_BIAS_TO_TRADE` lowered from `0.04` → `0.025` based on post-v2 bias distribution
- [x] Negative-bias cells now produce `YES` (crowd underestimates) instead of None
- [x] Missing `probability` field → None (was: defaulted to 0.5)

**Documented silent-by-design**
- [x] `creator_disagreement`: docstring now explains that `resolutionProbability` only populated on ~5% of markets — 0 fires is structural
- [x] `thin_market`: docstring now explains it's confirmation-only, can only "fire" when matching another contrarian/fundamental signal

**Swap margin buffer**
- [x] `scripts/position_swap_checker.py::SWAP_MIN_MARGIN = $1.00`
- [x] Gate in `run_swap_check()` now requires `best_ev ≥ loss + SWAP_MIN_MARGIN`
- [x] Rejection message prints both numbers for auditability

**Tests** — Reworked `TestProbabilityBiasStrategy` with deterministic `_CalibrationPatch` context manager (12 tests) + new `TestSwapMinMarginBuffer` (5) + `TestRunSwapCheckWithMinMargin` (2). All 266 tests passing.

**Diagnostic:** [`scripts/analyze_strategy_coverage.py`](scripts/analyze_strategy_coverage.py) — read-only, reports 24h fire rates + which categories/cells unlock at a proposed threshold. Used to pick the `0.025` threshold before coding.

**Production verification** (2026-04-11 15:55 UTC, first run post-Op5):

| Metric | Pre-Op5 (11:39) | Post-Op5 (15:55) |
|---|---:|---:|
| `probability_bias` raw fires | 0 | **18** |
| `probability_direction` raw fires | 10 | 11 |
| `mean_reversion` raw fires | 3 | 3 |
| `contrarian` dedup winners | 3 | 18 |
| `no_active_signals` | 29 | **18** |
| `final_recommendations` | 7 | 19 |
| `ai_eligible` | 7 | 19 |
| Strategy mix shapes | 1 | **3** |

**Op5 follow-up observation (for Op7):** in the post-Op5 run, Ollama `ai_skip=3/3` — every bias rec the AI analysed was vetoed. This suggests per-(category, bucket) bias applied to `other` markets near the 50-60% bucket may be over-firing on markets where the underlying "other" historical bias doesn't apply (e.g. Daily Coinflip markets at exactly 50% are 50/50 by construction). The AI veto is correctly catching these. **Not an Op5 regression** — the strategy mix is finally diverse, it just means category-level `other` aggregate is too heterogeneous to be a useful prior. Op7 candidate: filter out markets whose question text flags them as structurally uncorrelated with category bias (coinflip, roll, random, etc.) OR exclude `other` from the per-bucket lookup entirely.

#### ✅ Op5.1 — MKT/CANCEL resolution + position overflow fix (done 2026-04-12)

Three positions were permanently stuck as OPEN because `auto_resolve_markets()` only handled YES/NO. Markets resolved as MKT (proportional payout) or CANCEL (full refund) were silently skipped, consuming slots forever.

- [x] `paper_trader.py::resolve_market_mkt()` — proportional payout at `resolutionProbability`
- [x] `paper_trader.py::resolve_market_cancel()` — full refund of bet amount
- [x] `auto_resolve_markets()` dispatches: YES/NO → existing, MKT → new, CANCEL → new
- [x] Position overflow fix: `run_trading_cycle()` re-checks `_trade_rejection_reason()` before EACH trade (prevents 11/10 when starting at 9 and placing 2)
- [x] `tests/test_resolution_types.py` — 18 tests covering MKT payout math, CANCEL refund, dispatch, skipping non-OPEN
- [x] **Server result:** 3 positions resolved (Moon MKT -$0.04, Light-year MKT +$0.00, Mythos CANCEL +$10 refund), balance $55→$65, book 11→8→10 (2 new trades placed in freed slots)

#### ✅ Op5.2 — Backtest weight preservation (done 2026-04-12, commit `bd114cc`)

`compute_strategy_weights.py` (Monday 07:30) was overwriting `strategy_weights.json` from scratch, losing the `probability_bias = 1.2` that `backtest_from_snapshots.py` (Sunday 03:30) had computed from 186 historical evaluations.

- [x] Monday run now reads existing file and preserves weights for strategies where live samples < MIN_SAMPLES_PER_STRATEGY (10)
- [x] Backtest signal persists until enough live data exists to override it

#### 🟢 Op6 — M6 training run

Deferred until dataset grows via Op4's live accumulation path. The M6 scaffolding ([scripts/finetune.py](scripts/finetune.py)) and the promotion gate (crowd Brier 0.1503 / DirAcc 75.0% on the held-out test split) are already in place. Reasons to wait:

- the dataset is still 127 rows after reclassify + Op4 (weekly growth expected)
- test split is only 8 examples → a "passing" run would be hard to trust
- Op5 has finally unblocked the signal layer; if the signal mix produces enough tradable opportunities, a fine-tuned veto may turn out to be unnecessary

#### 🟢 Op7 — Bias over-firing on heterogeneous `other` markets

Follow-up to Op5 identified in the 15:55 post-Op5 run. The `other` category is a catch-all — its aggregate bias is an average over politics/celebrity/coinflip/research markets that share nothing except "failed to match any keyword". Applying that aggregate to a random coinflip market is semantically wrong.

Candidates to consider:
- Exclude `other` from the per-bucket lookup (keep `other` for aggregate fallback only)
- Add a "structural noise" filter on question text (coinflip, random, roll, d20, etc.)
- Require per-bucket bias cells to have BOTH high `sample_size` AND reasonable homogeneity (standard deviation of bias across sub-populations)

Op7 should follow 24-48h of Op3 counter data showing AI veto rates by category.

#### 🟡 P4 — Whale Tracking

Strong signal when it fires. High-accuracy large bettors on Manifold have asymmetric information. Confidence scales with bettor track record and bet size.

- [ ] `manifold_bot/whale_tracker.py` — fetches recent large bets via `/v0/bets`, filters by amount and bettor profit history
- [ ] `whale_strategy` in `strategies.py` — `family: "fundamental"`, confidence scales with whale accuracy × bet size
- [ ] Bettor-accuracy cache in SQLite (`data/calibration.db` — reuse existing DB)

#### 🟢 P5 — Internal Operator Dashboard

**Purpose:** an operator console for the team — not a public product. The system now has enough moving parts (8 analysis layers, 4 learning loops, Op3 counters, swap proposals) that debugging from raw logs is getting impractical. The dashboard makes the bot's behaviour legible so the right next improvements become visible from patterns over time, not one-off log reads.

**Scope:** internal only. Runs on the server (port 5000), accessed via SSH tunnel or Tailscale. No auth needed (private network). No public exposure until core performance story is solid.

**Tech:** FastAPI backend reading existing JSON/SQLite files. Single-page HTML + Chart.js frontend. Zero writes to trading state — pure read-only observer.

**Panels (ordered by operator value):**

**Panel 1 — Portfolio (the thing you check 10x a day)**
- [ ] Balance + total P&L + today's P&L
- [ ] Open positions table: market question, category, bet direction, $amount, entry price, current price, unrealised P&L, age
- [ ] Recent resolved trades: outcome (WIN/LOSE/MKT/CANCEL), actual P&L, which strategies triggered
- [ ] Position count vs MAX_POSITIONS (visual indicator when full)

**Panel 2 — Research Feed (what the bot is seeing right now)**
- [ ] Latest recommendations from `market_research.json`: question, confidence, strategies, EV (exec), AI recommendation
- [ ] Which markets went to AI and what AI returned (agree/disagree/SKIP + reasoning snippet)
- [ ] News headlines that were fetched (from `rec['news_context']`)
- [ ] Schema version indicator (v2 = healthy, v1 = AI pass failed)

**Panel 3 — Strategy Health (Op3 counters visualised)**
- [ ] Raw fire rates per strategy over time (line chart from `research_counters.jsonl`)
- [ ] `no_active_signals` count over time (are we covering more markets?)
- [ ] Family dedup winner distribution (momentum vs contrarian vs fundamental)
- [ ] AI flow: eligible → cooled → analyzed → agree/disagree/skip/no-result (funnel chart)
- [ ] Trader rejection reasons breakdown (from `trader_counters.jsonl`): which gate is the bottleneck?
- [ ] Confidence distribution of final recommendations (histogram)

**Panel 4 — Learning Health (feedback loops)**
- [ ] Strategy weights over time (are they moving from 1.0? which direction?)
- [ ] `per_strategy_samples` progress toward the ≥ 10 live activation gate
- [ ] Calibration bias by category (bar chart from `calibration_table.json`)
- [ ] Per-bucket bias heatmap: category × probability bucket, colour by bias magnitude
- [ ] Category accuracy: samples accumulated vs the ≥ 8 activation gate
- [ ] Dataset growth: training_dataset.jsonl row count over time (reconstructed vs live)

**Panel 5 — Swap Proposals (replace Telegram flow)**
- [ ] Active pending swaps with close-side loss vs open-side exec EV
- [ ] One-click approve/dismiss (calls `execute_swap.py` via subprocess)
- [ ] Swap history: approved/dismissed/expired with timestamps

**Not in v1:**
- Public access or auth (internal only for now)
- Real-time WebSocket updates (polling every 60s is fine)
- Trade execution from the dashboard (trades are placed by the cron, not by humans)
- Mobile layout (SSH tunnel from a laptop is the access pattern)

**Implementation order:**
1. FastAPI backend + `/api/portfolio`, `/api/research`, `/api/counters`, `/api/learning` endpoints
2. Static HTML shell with Chart.js loaded
3. Panel 1 (portfolio) — highest immediate value
4. Panel 3 (strategy health) — second highest, directly informs Op7 decisions
5. Panels 2 + 4 + 5 in that order

#### 🟢 P6 — SQLite storage (housekeeping)

- [ ] Replace flat JSON state files with SQLite tables
- [ ] `get_market_history(market_id)` — `/v0/market/{id}/bets` for historical bets

---

### Track 2 — ML Pipeline (fine-tuning path)

Goal: train a local model that estimates true market probability better than the crowd or generic LLM prompts.
Constraint: CPU-only server (i5-1340P, 62 GB RAM). Max viable model: ~7B params. Fine-tuning viable with LoRA.

#### ✅ M1 — Hourly snapshot logger — DONE (2026-04-09)

The single highest-leverage infrastructure investment. Without it, any training dataset will only have near-close snapshots — teaching the model late-stage market behavior instead of decision-time behavior.

- [x] `_write_market_snapshots()` added to `auto_research.py` — called after every hourly market fetch
- [x] Writes `(market_id, question, probability, volume24h, bettors, liquidity, snapshot_at)` to `data/market_snapshots.db` (SQLite)
- [x] One row per market per hour — ~100 rows/run; indexed by `market_id` and `snapshot_at`
- [x] After 2–3 months: join against `resolved_markets` for ML training pairs
- [ ] After 2–3 months: join against `resolved_markets` to get `(snapshot_at_T, actual_resolution)` pairs for training

#### ✅ M2 — Audit the 1121 resolved_markets rows — DONE (2026-04-09)

- [x] `scripts/audit_resolved_markets.py` — three quality filters: near-close contamination (>85%/<15%), thin markets (<10 bettors), category skew
- [x] Result: 267/1121 usable markets (55% near-close excluded, 41% thin excluded) → YELLOW gate
- [x] Outputs `data/m2_audit.json` with `usable_market_ids` list consumed by M3
- [x] Automated: runs every Sunday 02:30 UTC after harvest

#### ✅ M3 — Reconstruct earlier snapshots from bet history — DONE (2026-04-09)

- [x] `scripts/reconstruct_snapshots.py` — fetches full bet history per market via `/v0/bets`, calls `prob_at_time()` to find probability at T-7d/T-14d/T-30d before close
- [x] Writes `source='reconstructed'` rows to `market_snapshots.db` with `days_before_close` and `outcome` columns
- [x] Idempotent — skips already-reconstructed markets; re-runs accumulate new data only
- [x] Fix 3 (2026-04-09): API errors now reported as `api_error`, not silently collapsed into `no_bets`
- [x] First run: 267 markets → 127 snapshot rows written, 674 windows skipped (markets too short-lived)
- [x] Automated: runs every Sunday 03:00 UTC after M2

#### ✅ Offline Backtest Pipeline — DONE (2026-04-09/10)

Bridges reconstructed snapshots → strategy weights. Without this, M3 data never reached `strategy_weights.json` (which only reads live `bet_outcomes`).

- [x] `scripts/backtest_from_snapshots.py` — loads `source='reconstructed'` rows, runs `mean_reversion` and `probability_bias` (skips `probability_direction` — `volume24h` is NULL for historical data), computes accuracy → weight using same formula as live system
- [x] Merge rule: live wins when `per_strategy_samples ≥ 10`; backtest fills gap when `≥ 30` samples; else default 1.0
- [x] Fix 4 (2026-04-10): `compute_strategy_weights.py` now emits `per_strategy_samples` in `strategy_weights.json` so the live-over-backtest handoff actually fires
- [x] Fix 1 (2026-04-09): `_ensure_bet_outcomes_schema()` prevents `OperationalError: no such column: category` on older sandbox DBs
- [x] First backtest result: `probability_bias` 72% accuracy / 25 samples (5 short of gate — will clear after 2026-04-13 harvest)
- [x] Automated: runs every Sunday 03:30 UTC after M3

#### ✅ M4 — Dataset formatter (done 2026-04-10)

- [x] `scripts/build_training_dataset.py` — joins M3 reconstructed snapshots + resolved_markets → training JSONL
- [x] Input fields: question, category, crowd_prob_at_snapshot, days_to_close, unique_bettors, liquidity
- [x] Target: `true_probability` (1.0/0.0) + `prompt`/`completion` for M6 LoRA
- [x] 127 examples (T-7/14/30 decision-time only, no near-close leakage); 95 train / 24 val / 8 test; output: `data/training_dataset.jsonl`

#### ✅ M5 — Eval harness (done 2026-04-10)

- [x] `scripts/run_eval_harness.py` — evaluates any model against 4 baselines on training_dataset.jsonl
- [x] Baselines on held-out test split (8 examples): crowd **Brier 0.1503 / DirAcc 75.0%**, always_0.5, always_0.8, random
- [x] Metrics: Brier score, log loss, directional accuracy, calibration curve per bucket + per category
- [x] Promotion rule: finetuned Brier < crowd Brier AND < deepseek Brier AND DirAcc >= crowd DirAcc
- [x] Report written to data/eval_report.json
- [x] Note: earlier `--split all` run gave crowd Brier 0.2218 / DirAcc 64.6% — not a valid held-out gate; superseded by test-split numbers above

#### 🟢 M6 — LoRA fine-tune llama3.2:3b

- [x] `requirements-train.txt` — `transformers`, `peft`, `datasets`, `accelerate`, `torch`
- [x] `scripts/finetune.py` — completion-only loss masking; LoRA rank=8; adapter saved to `data/lora_adapter/`; writes `data/finetuned_preds.jsonl` keyed by `example_id`
- [ ] Run training on server: `pip install -r requirements-train.txt && python3 scripts/finetune.py`
- [ ] Evaluate: `python3 scripts/run_eval_harness.py --finetuned-predictions data/finetuned_preds.jsonl --split test`
- [ ] Promote to production (GGUF → Ollama) only if Brier < 0.1503 AND DirAcc >= 75.0%

---

### What's retired / deprioritized

| Item | Status | Reason |
|---|---|---|
| `deepseek-r1:14b` on Ollama | Retired | 0.7 tok/s on CPU; replaced by `llama3.2:3b` (9.3 tok/s) |
| Agent auto-reviewer | Disabled | Truncated files and introduced bugs on 3/4 PRs — human review only |
| Close open positions manually | Dropped | Auto-resolver handles this passively |

---

### ✅ Fix `per_market_timeout` Bug — DONE

`auto_research.py` passes `per_market_timeout=90` to `batch_analyze()` — parameter added to signature in `manifold_bot/ai_analyzer.py`. AI pass no longer raises `TypeError`.

### ✅ Fix Cron Timeout — DONE

`max_markets` reduced from 5 → 3. Worst-case AI pass: 3×90s = 270s, within the 300s cron limit. Previously 5×90s = 450s caused the research job to be killed mid-run every cycle, producing no output. `per_market_timeout` kept at 90s — reducing it would cause all markets to time out if Ollama inference takes ~70-80s.
Remaining server action: `openclaw cron edit 359e61eb-... --timeout 600` to add buffer.

### ✅ API Key Auto-loading — DONE

`manifold_bot/config.py` now parses `.env` from the project root at import time via `os.environ.setdefault`. Previously the key was only read from the OS environment, so scripts run outside a login shell (cron jobs, tests without export) always got a 401. No extra dependencies — plain file read.

### ✅ EV Stored at Trade Time — DONE

`auto_trader.py` now computes `estimated_ev = P(win) × payout_if_win − stake` using `ai_estimated_probability` and `current_prob` at the moment a trade executes. Stored as `estimated_ev` in every `auto_trades.json` record. `null` when no AI estimate is available. Unlocks calibration step 4 and smart position swap EV comparison.

---

## Phase Status

| Area | Status | Notes |
|------|--------|-------|
| Core trading engine | ✅ Done | |
| API integration | ✅ Done | |
| Basic strategies | ✅ Done | Momentum, mean reversion, volume spike, Kelly |
| CLI interface | ✅ Done | |
| Portfolio metrics | ✅ Done | |
| Auto-trader | ✅ Done | Running via cron, 65% confidence threshold |
| Market research | ✅ Done | Hourly, saves to JSON |
| Daily summary | ✅ Done | Cron at 19:00 UTC |
| Project structure | ✅ Done | automation/, tests/, scripts/, docs/, memory/ |
| Git workflow rules | ✅ Done | AGENTS.md enforces branch rules |
| AI integration | ✅ Done | `ai_analyzer.py` + wired into research, merged |
| Auto-fixing review agent | ✅ Done | Commits fixes directly to PR branch |
| Fix `per_market_timeout` bug | ✅ Done | `concurrent.futures` timeout enforcement in `batch_analyze()` |
| Distillation logging | ✅ Done | `logs/llm_calls.jsonl` with CoT extraction + log rotation, PR #5 merged 2026-04-06 |
| Strategy improvements | ✅ Done | 5 strategies with richer API fields, Kelly wiring, recency boost, PR #6 merged 2026-04-06 |
| Revert MIN_CONFIDENCE to 0.65 | ✅ Done | Reverted in commit dfdec11 |
| Position resolution loop | ✅ Done | `scripts/resolve_positions.py` + cron at :10, unblocks frozen bot |
| EV stored at trade time | ✅ Done | `estimated_ev` field in `auto_trades.json`, unlocks calibration step 4 |
| API key auto-loading | ✅ Done | `config.py` parses `.env` at import — no more 401s in cron/tests |
| Cron timeout fix | ✅ Done | `max_markets` 5→3, worst case 270s < 300s limit |
| Close open positions | 🔄 In Progress | Bot at 10/10 positions — auto-resolver will free slots as markets settle |
| Calibration & feedback loop | ✅ Done | Harvest 1,121 markets → bias table → AI prompt injection → bet_outcomes → weekly EV report |
| Smart position swap | ✅ Done | EV-based swap with Telegram approval — merged PR #9 |
| Signal family architecture | ✅ Done | momentum/contrarian/fundamental/filter; deduplication; volume_spike_priority as float filter |
| Code quality (candidate count, priority_boost, test_manifold, duplicate-bet guard) | ✅ Done | 136 test_calibration + 81 test_strategies + 66 test_swap = 283 tests |
| Telegram bot commands | ✅ Done | real Bot API; /portfolio /positions /scan /autotrader on/off; no duplicate sends; positions show real titles |
| Auto-notify on resolution | ✅ Done | `_notify_resolution()` in `paper_trader.py`; fires on every resolution; never blocks |
| /autotrader on/off | ✅ Done | flag file in `auto_trader.py`; telegram handlers; two OpenClaw skills |
| Sandbox reproducibility | ✅ Fixed | `auto_research_fast.py` removed; experiment files cleaned; duplicate trading job removed |
| Category exposure caps | ✅ Done | `MAX_POSITIONS_PER_CATEGORY=3`; enforced in `should_trade_market()`; category stored on trade record; breakdown in daily summary |
| Weighted scoring Phase A + D | ✅ Done | `compute_strategy_weights.py` (JSON parse fixed, all 6 strategies, per_strategy_samples emitted); weights applied to signals in `auto_research.py`; performance note in AI prompt |
| Local model switch (llama3.2:3b) | ✅ Done | FIRST_TOKEN_TIMEOUT 40→90s; keep_alive 2h→4h (2026-04-09) |
| AI call tracking | ✅ Done | source, latency_ms, ollama_timed_out in llm_calls.jsonl |
| Code quality (13 issues) | ✅ Done | crash fix, dead code removed, pandas removed, paths absolute, imports cleaned |
| Weighted scoring Phase B + C | ✅ Done | dynamic Kelly scaling from weights; per-category accuracy → adaptive caps (2026-04-09) |
| Register weekly-strategy-weights cron | ✅ Done | OS crontab Mondays 07:30 UTC (2026-04-09) |
| Hourly snapshot logger (M1) | ✅ Done | `_write_market_snapshots()` in auto_research.py → market_snapshots.db (2026-04-09) |
| bet_outcomes schema migration fix | ✅ Done | `_ensure_bet_outcomes_schema()` prevents OperationalError on older DBs (2026-04-09) |
| M2 — Audit resolved_markets corpus | ✅ Done | 267/1121 usable; YELLOW gate; data/m2_audit.json (2026-04-09) |
| M3 — Reconstruct decision-time snapshots | ✅ Done | 127 rows in market_snapshots.db; api_error vs no_bets fix (2026-04-09) |
| Offline backtest pipeline | ✅ Done | backtest_from_snapshots.py; probability_bias 72%/25 samples; automated Sunday chain (2026-04-10) |
| per_strategy_samples in strategy_weights.json | ✅ Done | live-over-backtest handoff wired (2026-04-10) |
| Phase D removal (weights from AI prompt) | ✅ Done | `_build_performance_note()` removed; eliminates triple-counting (2026-04-10) |
| Query-specific calibration prior | ✅ Done | `_build_query_calibration_note()` replaces global 10-row table in AI prompt (2026-04-10) |
| by_category strategy weights | ✅ Done | `compute_strategy_weights.py` emits `by_category`; fallback chain in `auto_research.py` + `auto_trader.py` (2026-04-10) |
| test_swap.py pre-existing failures | ✅ Done | liquidity gate + MAX_BET_AMOUNT fix; all 66 passing (2026-04-10) |
| Bias direction wording in AI prompt | ✅ Done | positive/negative/zero branches in `_build_query_calibration_note()` (2026-04-10) |
| Category-aware trade log | ✅ Done | `strategy_weight_at_trade_time` now uses category-inferred weight (2026-04-10) |
| News fetcher | ✅ Done | top-3 post-loop enrichment; recency-scaled modifier; 36 tests; `NEWS_API_KEY` in `.env` (2026-04-10) |
| Whale tracking | 🟡 P4 | bettor-accuracy cache in SQLite; fundamental family |
| Web dashboard | 🟢 P5 | convenience only |
| SQLite storage | 🟢 P6 | housekeeping |
| Dataset formatter | ✅ M4 | 127 rows (T-7/14/30), example_id key, 80/10/10 split; data/training_dataset.jsonl (2026-04-10) |
| Eval harness | ✅ M5 | 4 baselines; crowd Brier 0.1503 / DirAcc 75.0% on test split; data/eval_report.json (2026-04-10) |
| LoRA fine-tune llama3.2:3b | 🟢 M6 | offline first: train → finetuned_preds.jsonl → M5 gate; deploy only if clears |
