# Manifold Trading Bot

An automated trading system for [Manifold Markets](https://manifold.markets) — a prediction market platform. The bot researches live markets every hour, applies statistical trading strategies, executes paper trades with risk management, and reports daily performance to a Telegram group.

## What It Does

| Layer | Script | Schedule |
|---|---|---|
| Market research | `automation/auto_research.py` | Every hour at :00 UTC |
| Trade execution | `automation/auto_trader.py` | Every hour at :15 UTC |
| Daily report | `automation/daily_summary.py` | Daily at 19:00 UTC |

The research engine scans 100+ live markets and scores each one using three strategies: **momentum** (follow recent probability movement), **mean reversion** (bet against extremes), and **volume spike** (follow unusual activity). The auto-trader applies risk rules before executing — maximum 5 open positions, 10% of balance per trade, 65% minimum confidence, 6-hour cooldown per market.

All trades run in **paper trading mode** against a simulated $1,000 balance, using real market data from the Manifold API.

## Project Structure

```
manifold-trading-bot/
├── automation/                  # Cron-scheduled scripts
│   ├── auto_research.py         # Hourly market scanner and scorer
│   ├── auto_trader.py           # Automated trade executor with risk controls
│   ├── daily_summary.py         # Daily P&L report generator
│   ├── send_telegram.py         # Telegram delivery
│   ├── setup_cron_jobs.py       # Utility to (re)create OpenClaw cron jobs
│   └── cron_jobs_config.json    # Cron job definitions
│
├── manifold_bot/                # Core Python package
│   ├── manifold_api.py          # Manifold Markets API client
│   ├── paper_trader.py          # Paper trading engine, P&L, state persistence
│   ├── strategies.py            # Momentum, mean reversion, volume spike, Kelly
│   ├── dashboard.py             # Performance charts and console output
│   ├── config.py                # API keys and trading parameters
│   └── main.py                  # Interactive CLI
│
├── tests/                       # Test suite
│   ├── test_manifold.py         # API connectivity and core tests
│   └── test_automation.py       # Automation script smoke tests
│
├── scripts/                     # Dev and maintenance tools
│   ├── demo_bot.py              # End-to-end demo run
│   ├── show_portfolio.py        # Detailed portfolio viewer
│   ├── get_market_ids.py        # Market ID lookup tool
│   ├── fix_portfolio.py         # Portfolio state repair utility
│   └── watch_demo.py            # Live feature demo
│
├── docs/                        # Documentation
│   ├── AUTOMATION_README.md     # Automation system setup guide
│   └── README.md                # Legacy detailed reference
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

## Automated Trading (OpenClaw / Cron)

The system runs autonomously on a Linux server via [OpenClaw](https://openclaw.dev) scheduled jobs.

To set up or reset the cron jobs:

```bash
python3 automation/setup_cron_jobs.py
openclaw cron add --name "Manifold Market Research" --cron "0 * * * *" ...
openclaw cron list
```

See [docs/AUTOMATION_README.md](docs/AUTOMATION_README.md) for the full setup guide.

**Risk parameters** (configurable in `automation/auto_trader.py`):

| Parameter | Value | Description |
|---|---|---|
| `max_positions` | 5 | Maximum simultaneous open positions |
| `max_position_size` | 10% | Max balance per single trade |
| `min_confidence` | 65% | Minimum strategy confidence to trade |
| `cooldown_hours` | 6 | Hours before re-entering the same market |

## Trading Strategies

**Momentum** — if a market probability has been drifting in one direction, continue betting that way. Uses a threshold to avoid noise near 50%.

**Mean Reversion** — if probability is extreme (>80% YES or <20% YES), bet it will pull back toward the middle.

**Volume Spike** — if trading volume is 2× above the market average, follow the direction of the current probability. Unusual volume signals new information.

**Kelly Criterion** — position sizing formula that maximises long-run growth given your estimated edge and payout ratio. Capped at 25% of balance.

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
python3 tests/test_manifold.py      # API and core
python3 tests/test_automation.py    # Automation scripts
```

The pre-push git hook runs these automatically before every `git push` (when dependencies are installed).

## License

MIT License — see [LICENSE](LICENSE).
