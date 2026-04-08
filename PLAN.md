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
- **Local LLM (free):** `http://localhost:11434` — model `deepseek-r1:14b`
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

## Current Trading State (as of 2026-04-08)

- **Paper balance:** ~$139.59
- **Open positions:** 8/10 (2 duplicate AsUEPpRNc5 positions closed; 2 slots available)
- **Auto-trader confidence threshold:** 65%
- **Trade logging:** `auto_trades.json` (all trades include `estimated_ev`)
- **Active branch:** `main`
- **Agent auto-reviewer:** DISABLED — caused file truncation bugs on 3/4 PRs. Human review only.
- **Telegram bot:** Live — `/portfolio`, `/positions`, `/scan`, `/autotrader on/off` via `automation/telegram_bot.py`. Address bot directly in group: `@hackathon_26_bot /portfolio`
- **Ollama:** `deepseek-r1:14b` on server; runner process restartable via `kill $(pgrep -f "ollama runner") && ollama serve` if it gets stuck in a spin loop (seen 2026-04-08: 784% CPU, no output)

---

## What Is NOT Built Yet

Pick whatever interests you. Create a branch, build it, open a PR.

---

### ✅ AI Integration — DONE (branch: feature/ai-analyzer, PR #2)

- [x] `manifold_bot/ai_analyzer.py` — calls local Ollama (`deepseek-r1:14b`) first, falls back to DeepSeek API
  - Returns: `{ "recommendation": "YES/NO/SKIP", "confidence": 0.0-1.0, "estimated_true_probability": 0.0-1.0, "reasoning": "...", "risk_factors": "..." }`
- [x] Wired into `auto_research.py` — AI runs on top 3 candidates per hourly cycle (`_AI_CANDIDATE_COUNT = 3`)
  - If AI agrees AND stat ≥ 0.65: `0.4 × stat + 0.6 × AI`, capped at `stat + 0.10` (max 10pp boost)
  - If AI agrees AND stat < 0.65: blended result capped at 0.64 — AI cannot rescue a weak stat signal
  - If AI disagrees: confidence scaled down by `0.4 + (1 - ai_conf) × 0.4` — certain AI disagreement keeps only 40%
- [x] Fixed volume spike `avg_volume` — now uses **median** of fetched markets (mean was skewed by outliers)
- [x] 24 tests in `tests/test_ai.py` — all passing (includes full blending logic coverage)
- [x] Ollama confirmed running on server: `deepseek-r1:14b` loaded, ~60s/response on CPU (capped to 3 markets — worst case 270s < 300s cron limit)
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

- [x] `automation/send_telegram.py` — real Bot API sender; 3 retries with backoff; falls back to plain text on Markdown parse errors; truncates at 4096 chars; `send_telegram_message()` kept for backward compat
- [x] `automation/telegram_bot.py` — five command handlers:
  - `portfolio` — balance, P&L, win rate, open slot count
  - `positions` — all open trades with direction, amount, entry probability
  - `scan` — top 5 opportunities from latest research with AI score + EV
  - `autotrader-on` — removes `autotrader_disabled.flag`, resumes trading next cycle
  - `autotrader-off` — creates `autotrader_disabled.flag`, trading paused until re-enabled
  - Prints to stdout (OpenClaw capture) AND sends directly via Bot API
  - Run: `python3 automation/telegram_bot.py portfolio|positions|scan|autotrader-on|autotrader-off`
- [x] `manifold_bot/config.py` — `TELEGRAM_BOT_TOKEN` and `TELEGRAM_GROUP_ID` read from `.env`
- [x] `automation/daily_summary.py` — `send_to_telegram()` now calls real Bot API instead of placeholder
- [x] OpenClaw skills registered: `skills/portfolio/`, `skills/positions/`, `skills/scan/`, `skills/autotrader-on/`, `skills/autotrader-off/` — all `✓ ready`
- [x] `manifold_bot/paper_trader.py` — `_notify_resolution()` sends Telegram when a position closes; `resolve_market()` accepts optional `question` param and calls it. Never raises — Telegram down cannot block resolution.
- [x] `automation/auto_trader.py` — checks for `autotrader_disabled.flag` at startup; exits cleanly with a message if present (cron log shows clear status, no noise)

**How to use in Telegram:**
```
@hackathon_26_bot /portfolio       ← balance + P&L
@hackathon_26_bot /positions       ← all open trades
@hackathon_26_bot /scan            ← top opportunities
@hackathon_26_bot /autotrader off  ← pause trading
@hackathon_26_bot /autotrader on   ← resume trading
```
Address the bot directly with `@hackathon_26_bot` in the group, or DM it directly.

---

### 🟡 Next — Category Exposure Caps

Each market `category` (crypto/politics/ai_tech/sports/economics/science/other) is inferred by keyword at research time. Currently there is no limit on how many open positions can belong to the same category — the bot could go 8/10 slots into crypto during a volatile week.

- [ ] Add `max_positions_per_category` config (suggested: 3 out of 10 total)
- [ ] In `auto_trader.py` `should_trade_market()`: count open positions per category, block if at cap
- [ ] Surface category breakdown in `daily_summary.py`

---

### 🟡 Next — Weighted Scoring + Adaptive Learning

The model weights in Ollama/DeepSeek do not update after a trade — the model itself is frozen. What *can* learn is the **policy layer**: which strategies to trust, how much to size, and what context to give the AI. The goal is to turn `bet_outcomes` data into automated behavior changes, not just reports you have to read manually.

**Phase A — Strategy reliability weights** (prerequisite for weighted scoring)

- [ ] `scripts/compute_strategy_weights.py` — queries `bet_outcomes`, computes per-strategy EV accuracy ratio (realized EV / estimated EV) over last 90 days; writes `data/strategy_weights.json`
- [ ] `data/strategy_weights.json` — committed to git; updated weekly by cron; initial value 1.0 for all strategies
- [ ] `strategies.py` — each strategy returns `{"direction", "strength", "reliability_weight", "family"}`; `reliability_weight` loaded from `strategy_weights.json` at import
- [ ] `auto_research.py` — composite score: `direction_sign × strength × reliability_weight`; replaces flat confidence as primary ranking key
- [ ] Keep `confidence` field in output for backward compatibility (map score → 0.0–1.0)

**Phase B — Dynamic risk scaling from EV accuracy**

- [ ] `auto_trader.py` — read `data/strategy_weights.json` at startup; if a strategy's EV ratio < 0.5 (overestimating edge by 2×): raise min confidence threshold for that strategy by 5pp; if EV ratio < 0.3: skip that strategy entirely for this cycle
- [ ] `auto_trader.py` — Kelly fraction scales with EV ratio: `kelly_fraction = 0.5 × min(ev_ratio, 1.0)`; a strategy at EV ratio 0.6 gets half-Kelly × 0.6 instead of full half-Kelly
- [ ] Add `ev_ratio_at_trade_time` to each trade record in `auto_trades.json` for audit trail

**Phase C — Category-level learning from own trade history**

- [ ] `scripts/compute_strategy_weights.py` — extend to also compute per-category EV accuracy (politics/crypto/sports/etc) from `bet_outcomes`; write to `data/category_accuracy.json`
- [ ] `auto_trader.py` — category accuracy feeds the category exposure caps: if a category's EV ratio < 0.5, reduce its cap from 3 to 1 automatically
- [ ] This complements the static calibration table — the public crowd bias corrects probability estimates, our own trade history corrects position sizing and exposure

**Phase D — Self-performance note in AI prompt**

- [ ] `ai_analyzer.py` — load `data/strategy_weights.json` at module import; build a compact 3-5 line note injected alongside the calibration table:
  ```
  Recent strategy performance (last 90 resolved trades):
    creator_disagreement: EV ratio 1.2 (strong — trust this signal)
    mean_reversion: EV ratio 0.4 (weak — apply extra skepticism)
    momentum: EV ratio 0.8 (solid)
  ```
- [ ] This makes the AI reason with our actual system history, not just public crowd bias
- [ ] Degrades gracefully: if `strategy_weights.json` absent, note is empty string

**What this is NOT:**
- Not fine-tuning the model weights — that's a much later step requiring clean labeled data and a stable signal stack
- Not immediate per-trade learning — weights update weekly from resolved outcomes
- The "intelligence" increase is: better context in prompts + better policy (sizing + thresholds) driven by actual P&L, not just confidence scores

> Suggested cron addition: `weekly-strategy-weights` — Sundays 03:00 UTC (after harvest, before EV report)
> Runs `compute_strategy_weights.py`, commits updated `data/strategy_weights.json` to main

---

### 🟡 Medium Priority — News Fetcher

Add a real news signal to the fundamental family. The key design point: use a structured news API (not DDG/Google scraping) to get recent headlines per market topic.

- [ ] `manifold_bot/news_fetcher.py` — takes a market question, extracts keywords, queries news API (e.g. NewsAPI.org free tier)
- [ ] Returns: list of recent headlines with publication date and sentiment
- [ ] `news_strategy` in `strategies.py` — `family: "fundamental"`, confidence 0.60-0.80 based on headline count and recency
- [ ] Wire into `auto_research.py` — runs in parallel with stat strategies (not on every market — only top 20 candidates to stay within time budget)

---

### 🟡 Medium Priority — Whale Tracking

Track large individual bettors on Manifold as a signal. When a known high-accuracy bettor places a big bet, that's strong evidence about the true probability.

- [ ] `manifold_bot/whale_tracker.py` — fetches recent large bets via `/v0/bets` endpoint, filters by amount threshold (e.g. > M$500) and bettor profit history
- [ ] `whale_strategy` in `strategies.py` — `family: "fundamental"`, confidence scales with whale's historical accuracy and bet size
- [ ] Wire into `auto_research.py` as a supplemental fundamental signal
- [ ] Requires: building a bettor-accuracy cache in SQLite (can reuse `data/calibration.db`)

---

### 🟡 Medium Priority — Web Dashboard

- [ ] `web_server.py` using FastAPI, endpoints:
  - `GET /api/portfolio` — balance, P&L, metrics
  - `GET /api/trades` — full trade history
  - `GET /api/positions` — open positions
  - `GET /api/opportunities` — current research recommendations
- [ ] `static/index.html` — single page with:
  - Balance over time (Chart.js line chart)
  - Daily P&L (bar chart)
  - Open positions table
  - Top opportunities list
  - Auto-refresh every 30s
- [ ] Run on port `5000`, SSH tunnel to view locally

---

### 🟢 Lower Priority — Data & Storage

- [ ] Replace flat JSON state files with **SQLite** — one DB file, tables for trades, positions, markets
- [ ] Add `get_market_history(market_id)` to `manifold_api.py` — fetch historical bets from `/v0/market/{id}/bets`
- [ ] Background polling loop — refresh open market prices every 5 min, save snapshots

---

### 🔄 Close Open Positions — IN PROGRESS

Initiated closure of positions 5 (`6pAcuEd22A`, $65) and 6 (`yEcN9AzZ05`, $60) — pre-AI NO bets at 50%, no signal.
- ⚠️ Positions 5+6 (`6pAcuEd22A`, `yEcN9AzZ05`) still open on server — close manually or via auto-close feature before treating slot as free.
- Bot currently at 6/5 open positions and remains blocked from new trades until both positions are confirmed closed.

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
| Code quality (candidate count, priority_boost, test_manifold, duplicate-bet guard) | ✅ Done | 173 tests total |
| Telegram bot commands | ✅ Done | `send_telegram.py` real Bot API; `telegram_bot.py` with /portfolio /positions /scan /autotrader on/off; OpenClaw skills registered |
| Auto-notify on resolution | ✅ Done | `_notify_resolution()` in `paper_trader.py`; fires on every `resolve_market()` call; never blocks resolution |
| /autotrader on/off | ✅ Done | flag file mechanism in `auto_trader.py`; `telegram_bot.py` handlers; two OpenClaw skills |
| Sandbox reproducibility | ✅ Fixed | `auto_research_fast.py` was live; restored to `automation/auto_research.py`; experiment files cleaned; duplicate trading job removed |
| Category exposure caps | ❌ Not started | |
| Weighted scoring + adaptive learning | ❌ Not started | strategy weights → dynamic sizing → category accuracy → self-performance prompt note |
| News fetcher | ❌ Not started | |
| Whale tracking | ❌ Not started | |
| Web dashboard | ❌ Not started | |
| SQLite storage | ❌ Not started | |
