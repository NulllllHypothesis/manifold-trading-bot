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

## Current Trading State (as of 2026-03-30)

- **Paper balance:** ~$525 (started $1,000)
- **Open positions:** 6 (2 are in non-existent mock markets — need closing)
- **Auto-trader confidence threshold:** 65% (plan originally said 70% — kept at 65% for now)
- **Trade logging:** `auto_trades.json` (SQLite planned for later)

---

## What Is NOT Built Yet

Pick whatever interests you. Create a branch, build it, open a PR.

---

### ✅ AI Integration — DONE (branch: feature/ai-analyzer)

- [x] `manifold_bot/ai_analyzer.py` — calls local Ollama (`deepseek-r1:14b`) first, falls back to DeepSeek API
  - Returns: `{ "recommendation": "YES/NO/SKIP", "confidence": 0.0-1.0, "estimated_true_probability": 0.0-1.0, "reasoning": "...", "risk_factors": "..." }`
- [x] Wired into `auto_research.py` — AI runs on top 5 candidates per hourly cycle
  - If AI agrees with stats: confidence boosted (60% AI / 40% stat blend)
  - If AI disagrees: confidence penalised to 40% of original (effectively blocked from trading)
- [x] Fixed volume spike `avg_volume` — now uses **median** of fetched markets (mean was skewed by outliers)
- [x] 23 tests in `tests/test_ai.py` — all passing (includes full blending logic coverage)
- [x] Ollama confirmed running on server: `deepseek-r1:14b` loaded, ~60s/response on CPU (capped to 5 markets to stay within hourly window)
- [x] `MIN_CONFIDENCE` moved to `config.py` — single source of truth for trading threshold across trader + researcher
- [x] DeepSeek API fallback: 2s rate-limit delay enforced regardless of caller, warning logged when used
- [x] Schema version guard in `auto_trader.py` — refuses to trade on pre-AI (v1) research files
- [ ] PR to main — pending agent review approval

> No new API keys needed. Ollama is free/local. DeepSeek API key already on server.

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

### 🟢 Lower Priority — Close Mock Positions

Two open positions are in non-existent mock markets and are stuck. Need a script or manual step to close/remove them from `paper_trading_state.json` to free up capacity for real trades.

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
| AI integration | ✅ Done | `ai_analyzer.py` + wired into research, PR pending |
| Telegram bot commands | ❌ Not started | Placeholder only right now |
| Web dashboard | ❌ Not started | |
| SQLite storage | ❌ Not started | |
| Close mock positions | ❌ Not started | Blocking 2 position slots |
