# Technical Overview

This document amplifies the previous tutorial with a more technical walkthrough of the live system, end-to-end data flows, and clear references to the files that implement each piece. Read it if you want to debug cron runs, understand where state lives, or decide what to build next.

## 1. Core packages and state handling

### `manifold_bot`
- `config.py` wires `MANIFOLD_API_KEY`, base URLs, and trading constants (initial balance, min/max bet sizes). All other modules import values from here, so keeping the env var populated is critical for every run.
- `manifold_api.py` is the HTTP client. It builds a persistent `requests.Session` with the `Authorization: Key` header, defines endpoints, and provides helpers (`get_markets`, `search_markets`, `get_market`, `get_user_info`, `place_bet`, etc.). The shared singleton `api_client` is imported across modules to avoid reinitializing sessions.
- `paper_trader.py` manages the simulated bankroll:
  - Loads/saves JSON state at `manifold_bot/paper_trading_state.json`.
  - `place_paper_bet` enforces the min/max bet rules, updates `balance`, records trades (with payout math), and persists the state every time.
  - `resolve_market` and `auto_resolve_markets` fetch Manifold market details, calculate profit/loss, tag statuses (`OPEN`, `WIN`, `LOSE`), and update both `positions` and `trade_history`.
  - `get_performance_metrics`, `print_portfolio_summary`, and `get_market_analysis` wrap pandas computations/printouts used by dashboards, automation logs, and CLI debugging (see `dashboard.py`/`automation` scripts).
- `strategies.py` encodes the voting heuristics:
  - `probability_direction_strategy`, `mean_reversion_strategy`, `volume_spike_strategy`, `find_arbitrage_opportunities`, and `calculate_kelly_criterion`.
  - `analyze_market_for_trading` applies the voting logic, computes a `recommended_action`, and attaches signal metadata that automation prints and dashboards consume.
- `dashboard.py` builds performance reports (Plotly/structured dicts) using the metrics above and the same strategy scan. It is invoked indirectly through `main.py` and can be extended later by the web dashboard.
- `main.py` exposes demos + interactive CLI paths:
  - `test_api_connection`, `explore_markets`, `demo_paper_trading`, `run_strategy_scanner`, `interactive_mode`. Each prints structured state, showing how modules interact.

## 2. Automation pipeline (cron-driven)

All automation lives under `automation/` and is orchestrated via OpenClaw cron jobs defined at `automation/setup_cron_jobs.py`.

### `automation/auto_research.py` (cron at `0 * * * *` UTC)
1. Instantiate `MarketResearcher`, which loads `manifold_bot.paper_trader.PaperTrader` to know current positions.
2. Fetch up to 100 markets via `api_client.get_markets`.
3. Filter out resolved markets and low-liquidity markets (<$100 liquidity).
4. Run each strategy (`probability_direction`, `mean_reversion`, `volume_spike`), collect votes with confidence, compute `recommendation`, `confidence`, and flag existing positions.
5. Sort the recommendations, write `market_research.json` (latest + 24-hour history), and print the top 5 for logging.

### `automation/auto_trader.py` (cron at `15 * * * *` UTC)
1. Load the latest research snapshot from `market_research.json`.
2. For each recommendation, run `should_trade_market`:
   - Enforce confidence threshold (≥65% currently)
   - Cooldown before adding to existing positions (6h)
   - Limit open positions (`max_positions=5`) and liquidity (≥$200)
3. Calculate bet size with `calculate_position_size` (10% of balance scaled by confidence, ± bounds, rounded to nearest $5).
4. Place paper trades via `PaperTrader.place_paper_bet`, log to `auto_trades.json`, and persist the updated state.

### `automation/daily_summary.py` (cron at `0 19 * * *` UTC)
1. Reads `manifold_bot/paper_trading_state.json`, `market_research.json`, and `auto_trades.json`.
2. Computes day’s trade counts, today’s win rate, open positions, research frequency, and automation volume.
3. Formats a Telegram-style message (with emoji headers) and writes it to `telegram_daily_summary.txt`.
4. (Planned) Passes the message to `automation/send_telegram.py`, which currently only prints and saves to `last_telegram_message.txt`.

### `automation/setup_cron_jobs.py`
- Generates cron job definitions for the three scripts above.
- Outputs `automation/cron_jobs_config.json`.
- Prints exact `openclaw cron add` commands for manual redeployment.
- These definitions are the single source of truth for the live cron schedules.

## 3. Data files & logs

- `manifold_bot/paper_trading_state.json`: persistent paper-trading ledger (balance, positions, trade history).
- `market_research.json`: latest opportunity set + rolling history (maintained by `auto_research.py`).
- `auto_trades.json`: last 100 auto-trade executions (maintained by `auto_trader.py`).
- `telegram_daily_summary.txt` / `last_telegram_message.txt`: hold the formatted Telegram messages for manual verification while the real bot is missing.
- Cron logs are monitored via `tail -f ~/.openclaw/logs/gateway.log` and the OpenClaw dashboard.

## 4. End-to-end flow recap

1. **Research phase (minute :00)** – gather markets, filter/rescore with `strategies.py`, produce `market_research.json`, and flag markets already present in `paper_trading_state.json`.
2. **Trading phase (minute :15)** – load the latest research, apply risk checks, place paper bets, update `paper_trading_state.json`/`auto_trades.json`, and log output for monitoring.
3. **Summary phase (daily at 19:00 UTC)** – compute metrics, format the report, and (optionally) notify the team via Telegram once the bot there is completed.
4. **Manual CLI operations** – `manifold_bot.main` allows you to run demos, scan markets interactively, and inspect the portfolio using the same building blocks.

This pipeline keeps the bot running, ensures state is preserved, and prints enough context to troubleshoot issues when cron logs are inspected.

## 5. Next steps

Use this prioritized roadmap from `PLAN.md`. Each story should ship with tests (`tests/test_manifold.py && tests/test_automation.py`) and be developed off `main` on `feature/...` branches.

1. **AI integration (High)** – add `manifold_bot/ai_analyzer.py`, integrate DeepSeek/`deepseek-r1:14b`, weight AI signals in `auto_research.py`, and write `tests/test_ai.py`.
2. **Telegram bot (Medium)** – replace `automation/send_telegram.py` with a `python-telegram-bot` implementation (token stored in OpenClaw config, group ID `-5240775171`), wire `/portfolio`, `/scan`, `/trade`, `/positions`, and `/autotrader` controls, and connect the daily summary.
3. **Web dashboard (Medium)** – build a FastAPI server (`web_server.py`), add Chart.js-based `static/index.html`, expose portfolio/trade/opportunity endpoints, and document how to tunnel it via SSH (port 5000).
4. **Data persistence (Lower)** – migrate all JSON stores to a single SQLite database with tables for trades, research snapshots, and open positions; update the `PaperTrader` and automation scripts accordingly.
5. **Clean stuck mock positions (Lower, blocking)** – identify the two unresolved mock markets inside `manifold_bot/paper_trading_state.json` and either resolve or remove them to free up slots for real trades.

You can reference `docs/current_situation_tutorial.md` for higher-level context, but this file shows exactly which modules to dive into first. Pick a story, create a branch (`feature/<story>`), add tests, run the full suite, and open a PR for review. Keep me in the loop if you need help wiring the AI, messaging, or dashboard layers.
