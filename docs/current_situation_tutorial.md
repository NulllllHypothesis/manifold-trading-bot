# Current Situation Tutorial

This repository is the Manifold Trading Bot run by Null Hypothesis inside the OpenClaw workspace. The goal of this document is to walk through the dominant code paths that are running today, explain how everything ties together, and point directly at the next work items surfaced in `PLAN.md`.

## 1. Code organization at a glance

- `manifold_bot/`: core Python package.
  - `config.py` reads `MANIFOLD_API_KEY`, exposes constants, and soft-fails with a warning when the key is missing.
  - `manifold_api.py` wraps requests to Manifold (`markets`, `bets`, `me`, etc.) with a shared session; a singleton `api_client` makes the rest of the project code cleaner.
  - `paper_trader.py` simulates bankroll, records trades to `paper_trading_state.json`, auto resolves markets via `ManifoldAPI`, and exports performance metrics/summary helpers.
  - `strategies.py` contains the voting system of probability-direction, mean-reversion, volume-spike, and a Kelly criterion helper plus a reusable `analyze_market_for_trading` summary for dashboards.
  - `dashboard.py` builds performance reports (with Plotly visualizations) on top of the paper trader; it reuses the same strategies to explain why opportunities exist.
  - `main.py` is the interactive CLI entry point—it has demo flows, strategy scanning, and a menu that ties together `PaperTrader`, `TradingDashboard`, and the helper functions from `strategies.py`.

- `automation/`: cron-driven glue.
  - `auto_research.py` runs hourly, pulls 50–100 markets, filters by liquidity, applies the voting strategies, saves the best opportunities to `market_research.json`, and dumps a top-5 summary to stdout.
  - `auto_trader.py` runs 15 minutes later, loads the latest research, checks risk rules (confidence, liquidity, max positions, cooldown), sizes bets, places paper trades, and logs to `auto_trades.json`.
  - `daily_summary.py` runs at 19:00 UTC, reads the paper-trading state plus research/trade logs, computes the day’s win rate/open positions/auto-trading activity, and formats a Telegram-style report.
  - `send_telegram.py` is a placeholder that prints what would be sent and saves it to `last_telegram_message.txt`; the plan is to replace this with a real bot.
  - `setup_cron_jobs.py` can regenerate the three OpenClaw cron jobs (`auto_research`, `auto_trader`, `daily_summary`) and writes `automation/cron_jobs_config.json` for reference.

- `scripts/`: developer helpers such as market ID fetchers and portfolio repair utilities (used off-line for maintenance).
- `docs/`: holds this tutorial and documentation (e.g., `docs/README.md`, `docs/AUTOMATION_README.md`).
- `tests/`: there is a quick sanity runner `tests/test_manifold.py` to verify Manifold API connectivity and `tests/test_automation.py` that shells out to the automation scripts/cron setup.
- `memory/`, `AGENTS.md`, etc.: agent instruction files maintained by the assistant; they describe workflows, branch rules, and onboarding notes you already read.

## 2. Live automation & data flow

1. **Data collection** – `automation/auto_research.py` (cron at `0 * * * *` UTC) pulls markets via `manifold_bot.manifold_api`, scores them with the strategy voting system, and writes a JSON snapshot with the confidence scoring and existing position flag.
2. **Decay/execute trades** – `automation/auto_trader.py` (cron at `15 * * * *` UTC) reads the latest snapshot, enforces risk rules, sizes trades based on confidence, and records outcomes into `manifold_bot/paper_trading_state.json` plus `auto_trades.json`.
3. **Daily reporting** – `automation/daily_summary.py` (cron at `0 19 * * *` UTC) compiles a report from the saved state + research/trade history and (eventually) pushes it through `automation/send_telegram.py`.

All cron jobs are deployed via OpenClaw; `automation/setup_cron_jobs.py` regenerates the `"job"` definitions and prints the exact `openclaw cron add` commands if something needs a rebuild.

## 3. Running & testing today

1. **Environment** – ensure `MANIFOLD_API_KEY` is set in your shell before running any bot. The `.env` example in `PLAN.md` shows the format but actual values live in the server’s env or local secrets.
2. **Manual runs** – `python3 -m manifold_bot.main` kicks off the interactive CLI; `demo_paper_trading` and `run_strategy_scanner` are handy for manual verification, and both print portfolio snapshots (including auto resolution and open positions).
3. **Automation sanity** – before touching cron, run `python3 automation/auto_research.py`, `python3 automation/auto_trader.py`, and `python3 automation/daily_summary.py` from the repo root to exercise every script. `tests/test_automation.py` already shells these out and is part of the required test suite.
4. **Core API check** – run `python3 tests/test_manifold.py` to confirm `MANIFOLD_API_KEY` is valid and the Manifold API is reachable.

## 4. The current situation (from `PLAN.md`)

- **Paper balance:** ~$525 (down from $1,000 seed capital).  
- **Open positions:** 6 total, but 2 exist inside mock markets that no longer resolve—they are stuck and should be pruned.  
- **Auto-trader confidence threshold:** 65% today (lowered slightly from 70% to keep the bot active).  
- **Storage:** state is still JSON; the migration to SQLite is pending.
- **Telegram:** messaging is still the placeholder in `automation/send_telegram.py`.
- **AI:** there is no DeepSeek/LLM integration yet; the strategies rely entirely on probability heuristics.

## 5. How to move forward (follow `PLAN.md`)

These are the prioritized work streams spelled out in `PLAN.md`. Treat each as its own branch; every change must include new tests and pass `python3 tests/test_manifold.py && python3 tests/test_automation.py`.

1. **AI integration (High priority)**  
   - Build `manifold_bot/ai_analyzer.py` that calls the DeepSeek API (`deepseek-chat` or the local `deepseek-r1:14b` Ollama) and returns `{recommendation, confidence, reasoning}`.  
   - Hook it into `manifold_bot/strategies.py` via a new `ai_strategy()` and weight its signal more heavily inside `automation/auto_research.py`.  
   - Surface the AI fields in the dashboards/reports and add `tests/test_ai.py` that mocks the DeepSeek client.

2. **Telegram bot (Medium priority)**  
   - Replace the placeholder logic in `automation/send_telegram.py` with `python-telegram-bot` integration, honoring the token stored in OpenClaw config and the group ID `-5240775171`.  
   - Add command handlers (`/portfolio`, `/scan`, `/trade`, `/positions`, `/autotrader on/off`) and wire daily summaries to send actual messages.  
   - Extend `tests/` with command simulation as needed.

3. **Web dashboard (Medium priority)**  
   - Stand up `web_server.py` (FastAPI) and expose endpoints for portfolio, trades, positions, and opportunities.  
   - Add a `static/index.html` that consumes these endpoints (Chart.js + tables) and refreshes every 30s over the SSH tunnel (port 5000).  
   - Document how to start the dashboard and expose any required setup steps in `docs/` (maybe extend this tutorial).

4. **SQLite storage (Lower priority)**  
   - Migrate `paper_trading_state.json`, `market_research.json`, and `auto_trades.json` to a single SQLite file with tables for trades, positions, research snapshots, and strategy signals.  
   - Expose helper functions in `manifold_bot/paper_trader.py` and `automation/` scripts to write/read from the DB, keeping the JSON files as an export option.

5. **Close mock positions (Lower priority but blocking)**  
   - Identify the two dead markets inside `manifold_bot/paper_trading_state.json` and either call `paper_trader.resolve_market(...)` with their last known outcome or delete them manually with a small maintenance script so the bot can trade again.

## 6. Recommended next actions

1. Confirm `amerilain` (once an admin) has accepted the collaborator invite.  
2. Make sure all cron jobs listed in `PLAN.md` are still running via `openclaw cron list`.  
3. Choose the next high-value story from section 5, create a branch (`feature/<name>`), add the necessary tests, run the full suite, and open a PR.  
4. Update this tutorial file if the architecture changes or if new automation flows land so new teammates can onboard quickly.

Feel free to ping me for the next audit or to help write scripts for the AI/Telegram/dashboard work above.
