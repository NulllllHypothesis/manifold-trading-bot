# Manifold Paper Trading Bot — Hackathon Plan

## Team
| Person | Role |
|--------|------|
| Amir | Backend (FastAPI web server, infra) |
| Aleksi | AI integration (DeepSeek) |
| Frank | Frontend (Web UI, charts) |
| Aki | Telegram bot commands |
| Denis | Data layer (SQLite, auto-trader) |

---

## What Already Exists (Don't Rebuild)
- `manifold_api.py` — Manifold Markets API client
- `paper_trader.py` — Paper trading simulation, P&L, state persistence
- `strategies.py` — Momentum, mean reversion, arbitrage, Kelly criterion
- `dashboard.py` — Plotly charts + text dashboard
- `main.py` — CLI menu interface

## What's Missing
- No AI integration (DeepSeek/Ollama on server but unused)
- No web UI (terminal only)
- No auto-trading loop (fully manual)
- No Telegram bot commands
- Strategies oversimplified (no real historical data)
- Flat JSON persistence (needs SQLite)

---

## Phase 1 — Data & Storage Foundation `(2-3h)` — Denis
*Everything else depends on this.*

**Goals:** SQLite instead of flat JSON, real historical probability data, background price polling.

- [ ] Replace `paper_trading_state.json` with SQLite using `sqlite3`
- [ ] Add `get_market_history(market_id)` to `manifold_api.py` — fetch `/v0/market/{id}/bets`
- [ ] Background polling loop: refresh open market prices every 5 min, save to DB

---

## Phase 2 — AI-Powered Market Analysis `(3-4h)` — Aleksi
*Core differentiator. DeepSeek API or local Ollama (`deepseek-r1:14b`).*

**Goals:** Feed market data into DeepSeek, get YES/NO recommendation with reasoning.

- [ ] Write `ai_analyzer.py` — sends market question + probability + bet history to DeepSeek, returns structured recommendation
- [ ] Integrate AI recommendations into `strategies.py` as `ai_strategy()` method
- [ ] AI vote counts 2x in `analyze_market_for_trading()` confidence scoring

**DeepSeek API:** `https://api.deepseek.com` — model: `deepseek-chat`
**Local Ollama:** `http://localhost:11434` — model: `deepseek-r1:14b`

---

## Phase 3 — Web Dashboard `(3-4h)` — Amir + Frank

### Backend (Amir)
- [ ] `web_server.py` — FastAPI app with endpoints:
  - `GET /api/portfolio` — balance, P&L, metrics
  - `GET /api/trades` — trade history
  - `GET /api/opportunities` — current strategy recommendations
  - `GET /api/positions` — open positions
- [ ] Serve static frontend files from FastAPI
- [ ] Run on port 5000, accessible via SSH tunnel

### Frontend (Frank)
- [ ] Single-page HTML/JS dashboard
- [ ] Chart.js: balance over time, daily P&L bar chart
- [ ] Tables: open positions, recent trades, top opportunities
- [ ] Auto-refresh every 30s

---

## Phase 4 — Telegram Bot Commands `(2-3h)` — Aki
*OpenClaw Telegram bot already connected to the group.*

**Bot token:** `8635961227:AAEVKvnmmawWX6RnB-IP3oW1Nfr87ixAl_g`
**Group ID:** `-5240775171`

- [ ] `telegram_bot.py` using `python-telegram-bot` library
- [ ] Commands:
  - `/portfolio` — show balance + P&L
  - `/scan` — top 5 opportunities
  - `/trade <market_id> YES 50` — place a paper trade
  - `/positions` — list open positions
- [ ] Push notifications: auto-message group when a market resolves

---

## Phase 5 — Auto-Trader `(2-3h)` — Denis (after Phase 1+2)
*Stretch goal: let the bot trade on its own.*

- [ ] `auto_trader.py` — scheduled loop using `schedule` library (every 15 min)
- [ ] Risk controls: max 5 open positions, max 10% balance per trade, skip if confidence < 70%
- [ ] Log all auto-trades to DB with triggering strategy
- [ ] `/autotrader on` and `/autotrader off` Telegram commands (Aki)

---

## Execution Order
```
Phase 1 (Denis)    ──────────────────────────────► unblocks Phase 2 + 5
Phase 2 (Aleksi)   ──── starts after Phase 1 DB schema agreed
Phase 3 (Amir+Frank) ── can start immediately (independent)
Phase 4 (Aki)      ──── can start immediately (independent)
Phase 5 (Denis)    ──────────────────────────────► after Phase 1 + 2 done
```

---

## Server Info
- **Host:** `100.116.161.112`
- **User:** `hackathon` / `hackathon2026`
- **Workspace:** `/home/hackathon/.openclaw/workspace/manifold_bot/`
- **OpenClaw dashboard:** `http://localhost:18789` (via SSH tunnel)
- **Web dashboard (Phase 3):** `http://localhost:5000` (via SSH tunnel)
- **Ollama:** `http://localhost:11434` (local LLM, deepseek-r1:14b)
- **llamafile (free):** `http://172.17.0.1:8080/v1/chat/completions`

## SSH Tunnel (run on your local machine)
```bash
# OpenClaw dashboard
ssh -L 18789:localhost:18789 hackathon@100.116.161.112

# Web dashboard (Phase 3)
ssh -L 5000:localhost:5000 hackathon@100.116.161.112
```
