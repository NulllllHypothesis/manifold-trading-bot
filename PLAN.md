# Manifold Paper Trading Bot — Project Plan

## Repo
`https://github.com/amirghari/manifold-trading-bot`

## Server
- **Host:** `100.116.161.112` — user: `hackathon` / pass: `hackathon2026`
- **Workspace:** `~/.openclaw/workspace/`
- **OpenClaw dashboard:** SSH tunnel → `http://localhost:18789`
- **Web dashboard (Phase 3):** SSH tunnel → `http://localhost:5000`
- **Local LLM (free):** `http://localhost:11434` — model `deepseek-r1:14b`
- **DeepSeek API:** `https://api.deepseek.com` — model `deepseek-chat`

## SSH Tunnel (run on your laptop)
```bash
ssh -L 18789:localhost:18789 hackathon@100.116.161.112
# for web dashboard
ssh -L 5000:localhost:5000 hackathon@100.116.161.112
```

## Git Workflow
```bash
git clone https://github.com/amirghari/manifold-trading-bot.git
git checkout -b feature/your-task-name   # create your branch
git push origin feature/your-task-name  # push when ready
# open a Pull Request on GitHub → merge to main
```

---

## What's Already Built

| File | What it does |
|------|-------------|
| `manifold_bot/manifold_api.py` | Manifold Markets API client |
| `manifold_bot/paper_trader.py` | Paper trading simulation, P&L, state persistence |
| `manifold_bot/strategies.py` | Momentum, mean reversion, arbitrage, Kelly criterion |
| `manifold_bot/dashboard.py` | Performance charts + text dashboard |
| `manifold_bot/main.py` | CLI menu |
| `fix_portfolio.py` | Portfolio state consistency fixer |

---

## Remaining Tasks

Pick whatever interests you. Create a branch, build it, open a PR.

---

### Data & Storage
- [ ] Replace flat `paper_trading_state.json` with **SQLite** — one DB file, proper tables for trades, positions, markets
- [ ] Add `get_market_history(market_id)` to `manifold_api.py` — fetch historical bets from `/v0/market/{id}/bets` to get probability over time
- [ ] Background polling loop — refresh open market prices every 5 min, save snapshots to DB

---

### AI Integration
- [ ] Write `ai_analyzer.py` — send market question + current probability + recent bets to **DeepSeek** (`deepseek-chat`), get back a structured YES/NO recommendation with reasoning
- [ ] Add `ai_strategy()` to `strategies.py` using the above analyzer
- [ ] Weight AI signal higher in the `analyze_market_for_trading()` confidence vote

> DeepSeek API key is in `manifold_bot/config.py` or ask Aleksi.
> Local alternative: Ollama at `http://localhost:11434`, model `deepseek-r1:14b`

---

### Web Dashboard
- [ ] **Backend** — `web_server.py` using FastAPI, endpoints:
  - `GET /api/portfolio` — balance, P&L, metrics
  - `GET /api/trades` — full trade history
  - `GET /api/positions` — open positions
  - `GET /api/opportunities` — current strategy recommendations
- [ ] **Frontend** — `static/index.html`, single page with:
  - Balance over time (Chart.js line chart)
  - Daily P&L (bar chart)
  - Open positions table
  - Top opportunities list
  - Auto-refresh every 30s
- [ ] Run on port `5000`, serve static files from FastAPI

---

### Telegram Bot
- [ ] `telegram_bot.py` using `python-telegram-bot` library
- [ ] Commands:
  - `/portfolio` — current balance + P&L
  - `/scan` — top 5 trading opportunities right now
  - `/trade <market_id> YES 50` — place a paper trade
  - `/positions` — list open positions
- [ ] Auto-notify the group when a market resolves

> Bot token: already in OpenClaw config on server (`~/.openclaw/openclaw.json`)
> Group ID: `-5240775171`

---

### Auto-Trader
- [ ] `auto_trader.py` — runs every 15 min using `schedule` library
  - Scans markets, runs all strategies + AI
  - Places top-confidence trades automatically
  - Risk controls: max 5 open positions, max 10% balance per trade, min 70% confidence to trade
  - Logs every auto-trade to DB with which strategy triggered it
- [ ] `/autotrader on` and `/autotrader off` Telegram commands

---

## Current Phase Status

| Area | Progress |
|------|----------|
| Core trading engine | ✅ Done |
| API integration | ✅ Done |
| Basic strategies | ✅ Done |
| CLI interface | ✅ Done |
| Portfolio metrics | ✅ Done (improved) |
| SQLite storage | ❌ Not started |
| AI integration | ❌ Not started |
| Web dashboard | ❌ Not started |
| Telegram commands | ❌ Not started |
| Auto-trader | ❌ Not started |
