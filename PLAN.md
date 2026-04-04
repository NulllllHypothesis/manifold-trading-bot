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
| `manifold_bot/strategies.py` | Momentum, mean reversion, volume spike, Kelly criterion sizing |
| `manifold_bot/dashboard.py` | Performance charts and text dashboard |
| `manifold_bot/config.py` | API key (from env var `MANIFOLD_API_KEY`), trading constants |
| `manifold_bot/main.py` | Interactive CLI |

### Automation (`automation/`)

| File | What it does | Schedule |
|------|-------------|----------|
| `automation/auto_research.py` | Scans 100+ markets/hour, scores by strategy confidence, saves to `market_research.json` | Every hour :00 UTC |
| `automation/auto_trader.py` | Reads research, applies risk rules, executes paper trades, logs to `auto_trades.json` | Every hour :15 UTC |
| `automation/daily_summary.py` | Calculates daily P&L metrics, formats Telegram message | Daily 19:00 UTC |
| `automation/send_telegram.py` | Telegram sender (currently placeholder — prints to console) | Called by daily_summary |
| `automation/setup_cron_jobs.py` | Utility to recreate OpenClaw cron jobs if needed | Manual |

### Dev Tools (`scripts/`)
`demo_bot.py`, `show_portfolio.py`, `get_market_ids.py`, `fix_portfolio.py`, `watch_demo.py`

### Tests (`tests/`)
`test_manifold.py` — API connectivity and core
`test_automation.py` — automation script smoke tests

---

## Live Cron Jobs (OpenClaw)

| ID | Name | Schedule |
|---|---|---|
| `359e61eb-...` | Manifold Market Research | `0 * * * *` UTC |
| `57ce0ebc-...` | Manifold Auto Trading | `15 * * * *` UTC |
| `0a77ecb9-...` | Daily Trading Summary | `0 19 * * *` UTC |

---

## Current Trading State (as of 2026-04-04)

- **Paper balance:** $530 (started $1,000)
- **Open positions:** 6 (exceeds max_positions=5 — bot is fully blocked from new trades)
- **All positions:** NO at 50% probability, placed before AI integration
- **Last trade:** 2026-04-03
- **Auto-trader confidence threshold:** 65%
- **Trade logging:** `auto_trades.json` (6 trades total)
- **🔴 Immediate action needed:** Close positions to unblock the bot (see "Close Mock Positions" below)

---

## What Is NOT Built Yet

Pick whatever interests you. Create a branch, build it, open a PR.

---

### ✅ AI Integration — DONE (branch: feature/ai-analyzer, PR #2)

- [x] `manifold_bot/ai_analyzer.py` — calls local Ollama (`deepseek-r1:14b`) first, falls back to DeepSeek API
  - Returns: `{ "recommendation": "YES/NO/SKIP", "confidence": 0.0-1.0, "estimated_true_probability": 0.0-1.0, "reasoning": "...", "risk_factors": "..." }`
- [x] Wired into `auto_research.py` — AI runs on top 5 candidates per hourly cycle
  - If AI agrees AND stat ≥ 0.65: `0.4 × stat + 0.6 × AI`, capped at `stat + 0.10` (max 10pp boost)
  - If AI agrees AND stat < 0.65: blended result capped at 0.64 — AI cannot rescue a weak stat signal
  - If AI disagrees: confidence scaled down by `0.4 + (1 - ai_conf) × 0.4` — certain AI disagreement keeps only 40%
- [x] Fixed volume spike `avg_volume` — now uses **median** of fetched markets (mean was skewed by outliers)
- [x] 24 tests in `tests/test_ai.py` — all passing (includes full blending logic coverage)
- [x] Ollama confirmed running on server: `deepseek-r1:14b` loaded, ~60s/response on CPU (capped to 5 markets to stay within hourly window)
- [x] `MIN_CONFIDENCE` moved to `config.py` — single source of truth for trading threshold across trader + researcher
- [x] DeepSeek API fallback: explicit `delay=2` at call site, `per_market_timeout=90` to prevent hung calls
- [x] Schema version written dynamically — v2 only if AI pass completed; v1 if aborted (blocks trading)
- [x] Schema version guard in `auto_trader.py` — handles both flat and nested file layouts, refuses to trade on v1
- [x] Auto-fixing review agent in `.github/scripts/claude-review.js` — reviews PRs and commits fixes directly
- [x] PR #2 merged to main

> No new API keys needed. Ollama is free/local. DeepSeek API key already on server.

---

### 🟡 Medium Priority — Calibration & Feedback Loop

The bot currently has no memory of what happened. Markets resolve, bets settle, and nothing changes about how future markets are scored. This closes that loop.

**Step 1 — Harvest resolved markets (foundation)**
- [ ] Script `scripts/harvest_resolved.py` — calls `GET /v0/markets?isResolved=true`, pages through results, saves to SQLite
  - Fields to store: `market_id`, `question`, `probability_at_close`, `outcome` (YES/NO), `close_date`, `category` (inferred)
  - Aim for 1,000+ resolved markets as a starting corpus
- [ ] Schedule as a weekly cron job (Manifold resolves ~100 markets/day)

**Step 2 — Measure where the crowd is wrong**
- [ ] Script `scripts/analyze_calibration.py` — groups resolved markets by probability bucket (0-10%, 10-20%, ..., 90-100%) and measures actual resolution rate per bucket
  - If markets at 70% only resolve YES 55% of the time → crowd is overconfident at high probabilities
  - Output: calibration table by bucket + by question category

**Step 3 — Feed calibration into AI prompts**
- [ ] Add a calibration context block to `ANALYSIS_PROMPT` in `ai_analyzer.py`:
  ```
  Historical calibration note: on this platform, markets at ~70% probability
  resolve YES only 58% of the time (crowd tends to overprice high-probability events).
  ```
  No retraining needed — just prompt grounding with real data.

**Step 4 — Close the loop: outcome tracking**
- [ ] When `paper_trader.py` resolves a market, write to a `bet_outcomes` table: `(market_id, our_recommendation, our_confidence, ai_source, outcome, pnl)`
- [ ] Weekly summary: accuracy by AI confidence band, by strategy, by question category
  - "When AI confidence > 80%, we were right X% of the time"
  - Use this to tune `MIN_CONFIDENCE` and blending weights over time

**Why this matters:**
The current bot has genuine zero edge on statistics alone — all signals are generic. Calibration gives it something no one else has: a learned correction for this specific platform's crowd biases. A market at 72% that historically resolves YES only 56% of the time is a NO bet regardless of what the question says.

> Prerequisite: SQLite storage (see below). Harvest script can use flat JSON as a stopgap.

---

### 🟡 Medium Priority — Real Telegram Bot

`automation/send_telegram.py` is currently a placeholder — it just prints to console.

- [ ] Replace with real `python-telegram-bot` integration
- [ ] Add `telegram_bot.py` with interactive commands:
  - `/portfolio` — current balance + P&L
  - `/scan` — top 5 trading opportunities right now
  - `/trade <market_id> YES 50` — place a paper trade
  - `/positions` — list open positions
- [ ] Auto-notify the group when a market resolves
- [ ] `/autotrader on` and `/autotrader off` commands

> Bot token: in OpenClaw config on server (`~/.openclaw/openclaw.json`)
> Group ID: `-5240775171`

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
| Fix `per_market_timeout` bug | ✅ Done | `batch_analyze()` param added — AI pass no longer silently fails |
| Close open positions | 🔄 In Progress | Positions 5+6 (`6pAcuEd22A`, `yEcN9AzZ05`) still open on server — must be closed manually or via auto-close before slot is free |
| Calibration & feedback loop | ❌ Not started | Harvest resolved markets → measure crowd bias → prompt grounding |
| Telegram bot commands | ❌ Not started | Placeholder only right now |
| Web dashboard | ❌ Not started | |
| SQLite storage | ❌ Not started | |
