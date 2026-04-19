# Manifold Trading Bot

An automated trading system for [Manifold Markets](https://manifold.markets) — a prediction market platform. The bot researches live markets every hour, applies statistical trading strategies, executes paper trades with risk management, and reports daily performance to a Telegram group.

## What It Does

| Layer | Script | Schedule |
|---|---|---|
| Market research | `automation/auto_research.py` | Every hour at :00 UTC |
| Position repricing | `scripts/reprice_positions.py` | Every hour at :05 UTC |
| Position resolution | `scripts/resolve_positions.py` | Every hour at :10 UTC |
| Trade execution | `automation/auto_trader.py` | Every hour at :20 UTC |
| Daily report | `automation/daily_summary.py` | Daily at 19:00 UTC |
| Calibration harvest | `scripts/harvest_resolved.py` + `analyze_calibration.py` | Sundays 02:00 UTC |
| EV accuracy report | `scripts/weekly_ev_report.py` | Mondays 07:00 UTC |
| Strategy weight update | `scripts/compute_strategy_weights.py` | Mondays 07:30 UTC |

The research engine scans 100+ live markets and scores each one using six strategies across four signal families: **momentum**, **contrarian**, **fundamental**, and a **priority filter** for volume spikes. Each top candidate is sent to a local AI model (Ollama `llama3.2:3b`, DeepSeek API fallback) which estimates the true probability and votes YES/NO/SKIP. Statistical and AI confidence are blended before trading. The auto-trader applies risk rules — maximum 10 open positions, 10% of balance per trade, 65% minimum confidence, category exposure caps, AI veto respected.

All trades run in **paper trading mode** against a simulated $1,000 balance, using real market data from the Manifold API.

## Project Structure

```
manifold-trading-bot/
├── automation/                  # Cron-scheduled scripts
│   ├── auto_research.py         # Hourly market scanner and scorer
│   ├── auto_trader.py           # Automated trade executor with risk controls
│   ├── daily_summary.py         # Daily P&L report generator
│   ├── send_telegram.py         # Telegram delivery
│   └── setup_cron_jobs.py       # Prints OS crontab block — paste into crontab -e on the server
│
├── manifold_bot/                # Core Python package
│   ├── manifold_api.py          # Manifold Markets API client
│   ├── paper_trader.py          # Paper trading engine, P&L, state persistence
│   ├── strategies.py            # Momentum, mean reversion, volume spike, Kelly
│   ├── dashboard.py             # Performance charts and console output
│   ├── config.py                # API keys and trading parameters
│   └── main.py                  # Interactive CLI
│
├── tests/                       # Test suite (296 tests)
│   ├── test_strategies.py       # 81 unit tests (strategies, trader, AI, phases B/C)
│   ├── test_calibration.py      # 136 unit tests (calibration pipeline, weights, by_category)
│   └── test_swap.py             # 66 unit tests (position swap, EV regression)
│
├── scripts/                     # Maintenance and pipeline scripts
│   ├── resolve_positions.py     # Hourly position resolution (polls Manifold API, frees slots)
│   ├── harvest_resolved.py      # Weekly — fetches 2000+ resolved markets into calibration.db
│   ├── analyze_calibration.py   # Builds calibration_table.json from calibration.db
│   ├── weekly_ev_report.py      # Monday EV accuracy report sent to Telegram
│   ├── compute_strategy_weights.py # Updates data/strategy_weights.json from bet_outcomes
│   ├── position_swap_checker.py # Proposes swapping losing positions for better opportunities
│   ├── execute_swap.py          # Executes or dismisses a pending swap proposal
│   └── show_portfolio.py        # Portfolio viewer (manual use)
│
├── data/                        # Persistent data (not committed except weights)
│   ├── calibration.db           # bet_outcomes table; EV and P&L records
│   ├── market_snapshots.db      # Hourly market state snapshots (ML training data)
│   ├── strategy_weights.json    # Per-strategy reliability scalars (updated weekly)
│   └── category_accuracy.json   # Per-category direction accuracy (updated weekly)
│
├── docs/                        # Documentation
│   ├── AUTOMATION_README.md     # Automation system setup guide
│   └── technical_overview.md    # Deep-dive architecture reference
│
├── memory/                      # Session logs — read by OpenClaw on startup
│   └── YYYY-MM-DD.md            # Daily notes: what changed, current state, what's next
│
├── requirements.txt             # Python dependencies
└── LICENSE                      # MIT License
```

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure API key

Set your Manifold API key in `manifold_bot/config.py`:

```python
MANIFOLD_API_KEY = "your-key-here"
```

### 3. Verify connection

```bash
python3 tests/test_manifold.py
```

### 4. Run a research cycle manually

```bash
python3 automation/auto_research.py
```

### 5. Run a trading cycle manually

```bash
python3 automation/auto_trader.py
```

## Automated Trading (OS Cron)

The system runs autonomously on a Linux server. All scheduling uses the OS-level cron daemon — scripts execute directly with no LLM agent in the loop. The only Telegram messages sent are the ones the scripts themselves choose to send (trade placed, market resolved, daily summary, weekly report).

**Crontab (active on server):**

```
# Market research — hourly :00
0 * * * *  cd $WORKSPACE && git pull origin main -q && python3 automation/auto_research.py

# Position repricing — hourly :05 (Phase 2.2, measure-only)
5 * * * *  cd $WORKSPACE && git pull origin main -q && python3 scripts/reprice_positions.py

# Position resolution — hourly :10
10 * * * *  cd $WORKSPACE && git pull origin main -q && python3 scripts/resolve_positions.py

# Auto trading — hourly :20
20 * * * *  cd $WORKSPACE && git pull origin main -q && python3 automation/auto_trader.py

# Daily summary — 19:00 UTC
0 19 * * *  cd $WORKSPACE && git pull origin main -q && python3 automation/daily_summary.py

# Weekly calibration harvest — Sunday 02:00 UTC
0 2 * * 0  cd $WORKSPACE && git pull origin main -q && python3 scripts/harvest_resolved.py --limit 2000 && python3 scripts/analyze_calibration.py

# Weekly M2 re-audit — Sunday 02:30 UTC
30 2 * * 0  cd $WORKSPACE && python3 scripts/audit_resolved_markets.py --quiet

# Weekly M3 reconstruction — Sunday 03:00 UTC
0 3 * * 0  cd $WORKSPACE && python3 scripts/reconstruct_snapshots.py

# Weekly backtest → strategy weights — Sunday 03:30 UTC
30 3 * * 0  cd $WORKSPACE && python3 scripts/backtest_from_snapshots.py

# Weekly EV report — Monday 07:00 UTC
0 7 * * 1  cd $WORKSPACE && git pull origin main -q && python3 scripts/weekly_ev_report.py --telegram

# Weekly strategy weights — Monday 07:30 UTC
30 7 * * 1  cd $WORKSPACE && git pull origin main -q && python3 scripts/compute_strategy_weights.py && git add data/strategy_weights.json && git diff --cached --quiet || git commit -m "chore: update strategy weights [skip ci]"
```

To view or edit: `crontab -e` on the server. Logs: `/tmp/research.log`, `/tmp/trader.log`, `/tmp/resolution.log`, etc.

See [docs/AUTOMATION_README.md](docs/AUTOMATION_README.md) for the full setup guide.

**Risk parameters** (in `manifold_bot/config.py` and `automation/auto_trader.py`):

| Parameter | Value | Description |
|---|---|---|
| `MAX_POSITIONS` | 10 | Maximum simultaneous open positions |
| `MAX_POSITIONS_PER_CATEGORY` | 3 | Max positions in any one topic category |
| `max_position_size` | 10% | Max balance per single trade |
| `MIN_CONFIDENCE` | 65% | Minimum strategy confidence to trade |
| `MIN_BET_AMOUNT` | $1 | Minimum bet size |
| `MAX_BET_AMOUNT` | $5 (throttled — restore to $25 once strategy weights diverge from 1.0) | Maximum bet size |

## Trading Strategies

Six strategies across four signal families. At most one directional signal per family is allowed into a trade (highest confidence wins within a family), preventing correlated strategies from stacking as independent evidence.

| Strategy | Family | What it does |
|---|---|---|
| Probability Direction | momentum | Bet with sustained drift; skips the 45-55% noise zone |
| Mean Reversion | contrarian | Bet against extremes (>80% or <20%) when fewer than 100 bettors have weighed in |
| Probability Bias | contrarian | Exploits measured crowd overconfidence from 1,100+ resolved markets (e.g. 60-70% bucket resolves YES only 47% of the time) |
| Creator Disagreement | fundamental | Bet toward the market creator when their estimate differs from the crowd by ≥15pp |
| Thin Market | fundamental | Bet toward the underrepresented side of the AMM liquidity pool (<15% of pool) |
| Volume Spike Priority | filter | Not directional — boosts candidate ranking score when 24h volume is >2× the batch median |

After statistical scoring, the top candidates are sent to a local AI (`llama3.2:3b` via Ollama, DeepSeek API as fallback). The AI reads the actual market question and estimates the true probability. Statistical and AI confidence are blended — AI can boost by up to 10pp or penalise down to 40% of the stat score.

**Kelly Criterion** — position sizing scales with the AI's estimated edge. Half-Kelly used to reduce variance, further scaled by a per-strategy reliability weight (0.5–1.2) updated weekly from resolved trade outcomes. Weights are looked up per `(strategy, category)` first, falling back to the global per-strategy weight, then to 1.0. Falls back to confidence-scaled sizing when no AI estimate is available.

## Development Workflow

All development follows the branch rules in `AGENTS.md`:

- `main` is protected — cron jobs run from it, no direct pushes
- New features go on `feature/name` branches
- Every feature requires new tests in `tests/`
- Full test suite must pass before opening a PR to `main`

```bash
git checkout main && git pull origin main
git checkout -b feature/your-feature
# ... develop ...
python3 tests/test_manifold.py && python3 tests/test_automation.py
git push origin feature/your-feature
# open PR on GitHub
```

## Running Tests

```bash
python3 tests/test_strategies.py    # strategies, trader, AI, phases B/C (81 tests)
python3 tests/test_calibration.py   # calibration pipeline, weights, by_category (136 tests)
python3 tests/test_swap.py          # position swap, EV regression (66 tests)
```

The pre-push git hook runs these automatically before every `git push` (when dependencies are installed).

## Session Memory

The `memory/` directory contains dated session logs (`YYYY-MM-DD.md`). OpenClaw reads today's and yesterday's file automatically on every startup — this is how it stays aware of recent changes, current project state, and what to work on next without anyone having to brief it each time.

New team members should also read the latest file in `memory/` after pulling — it covers what was built, what changed, live cron job IDs, and what's remaining.

## License

MIT License — Copyright (c) 2026 Null Hypothesis. See [LICENSE](LICENSE).
