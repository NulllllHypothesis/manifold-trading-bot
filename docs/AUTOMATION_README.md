# Automated Trading System

## What We've Built

A fully automated paper trading system for Manifold Markets running on a Linux server. All scheduling uses the OS-level cron daemon — no LLM agent in the trading loop.

### 1. Hourly Market Research (`automation/auto_research.py`)
- Scans 100+ markets every hour, applies six statistical strategies across four signal families
- Top candidates sent to local AI (`llama3.2:3b` via Ollama, DeepSeek API fallback) for probability estimation
- Writes `market_research.json` with ranked recommendations
- Writes hourly snapshots to `data/market_snapshots.db` (training data for future ML pipeline)

### 2. Hourly Auto Trading (`automation/auto_trader.py`)
- Executes paper trades based on research output
- Risk management: max 10 positions, max 3 per category (≤1 if accuracy < 50%; "other" uncapped), 10% per trade, 65% min confidence, AI veto respected, max bet $5 (throttled)
- **Phase B** — Kelly fraction scales with strategy reliability weight (`kelly_fraction = 0.5 × weight`)
- **Phase C** — Category exposure cap tightens to 1 when per-category direction accuracy < 50% (≥8 samples)
- Selects trades by estimated EV (highest edge first); confidence is a hard gate (≥65%)
- Logs all auto-trades to `auto_trades.json`

### 3. Hourly Position Resolution (`scripts/resolve_positions.py`)
- Polls Manifold API for market resolutions
- Closes out resolved positions, records actual P&L to `data/calibration.db`
- Frees position slots for new trades

### 4. Daily Summary Report (`automation/daily_summary.py`)
- Generates comprehensive daily report with P&L and open positions
- Sends to Telegram group at 19:00 UTC

### 5. Weekly Calibration Pipeline (Sundays → Mondays)
- **Sunday 02:00** — harvest 2000+ resolved markets into `data/calibration.db`
- **Monday 07:00** — EV accuracy report (per-strategy, per-confidence-band) sent to Telegram
- **Monday 07:30** — recompute strategy reliability weights (`data/strategy_weights.json`) and category accuracy (`data/category_accuracy.json`)

## Scheduling (OS crontab)

All jobs run via the OS-level cron daemon. To view: `crontab -l` on the server. To edit: `crontab -e`.

| Job | Schedule | Script |
|---|---|---|
| Market research | Every hour :00 UTC | `automation/auto_research.py` |
| Position resolution | Every hour :10 UTC | `scripts/resolve_positions.py` |
| Auto trading | Every hour :20 UTC | `automation/auto_trader.py` |
| Daily summary | 19:00 UTC daily | `automation/daily_summary.py` |
| Calibration harvest | Sundays 02:00 UTC | `scripts/harvest_resolved.py` + `analyze_calibration.py` |
| EV accuracy report | Mondays 07:00 UTC | `scripts/weekly_ev_report.py --telegram` |
| Strategy weight update | Mondays 07:30 UTC | `scripts/compute_strategy_weights.py` |

Logs: `/tmp/research.log`, `/tmp/trader.log`, `/tmp/resolution.log`, etc.

Telegram notifications are sent directly by scripts via Bot API — not via OpenClaw. Notifications fire only when something happens: trade placed, market resolved, daily summary, weekly EV report.

## Current Trading Status

Check `manifold_bot/paper_trading_state.json` for live balance and open positions.

```bash
cat manifold_bot/paper_trading_state.json | python3 -c "import sys,json; s=json.load(sys.stdin); print(f'Balance: \${s[\"balance\"]:.2f}  Positions: {len(s[\"positions\"])}')"
```

## Quick Start

### 1. Test the System

```bash
cd /home/hackathon/.openclaw/workspace

# Run full test suite
python3 tests/test_strategies.py
python3 tests/test_calibration.py

# Test individual scripts
python3 automation/auto_research.py
python3 automation/auto_trader.py
python3 automation/daily_summary.py
```

### 2. Check Cron Status

```bash
# View active crontab
crontab -l

# Check recent log output
tail -50 /tmp/research.log
tail -50 /tmp/trader.log
```

### 3. Monitor Execution

```bash
# Check latest research
cat market_research.json | python3 -c "import sys,json; r=json.load(sys.stdin); recs=r.get('recommendations',[]); print(f'{len(recs)} recommendations'); print(recs[0] if recs else 'none')"

# Check recent trades
cat auto_trades.json | python3 -c "import sys,json; t=json.load(sys.stdin); trades=t.get('trades',[]); print(f'{len(trades)} trades total'); [print(x) for x in trades[-3:]]"
```

## Configuration

### Risk Parameters (in `manifold_bot/config.py` and `automation/auto_trader.py`)

| Parameter | Value | Description |
|---|---|---|
| `MAX_POSITIONS` | 10 | Maximum simultaneous open positions |
| `MAX_POSITIONS_PER_CATEGORY` | 3 | Max positions in any one topic category (reduced to 1 if accuracy < 50%) |
| `max_position_size` | 10% | Max balance per single trade |
| `MIN_CONFIDENCE` | 65% | Minimum strategy confidence to trade |
| `MIN_BET_AMOUNT` | $1 | Minimum bet size |
| `MAX_BET_AMOUNT` | $5 (throttled — restore to $25 once strategy weights diverge from 1.0) | Maximum bet size |

### Strategy Weights (`data/strategy_weights.json`)

Per-strategy reliability scalars (0.5–1.2) updated weekly from `bet_outcomes`. Multiply the base Kelly fraction:
- `weight = 1.0` → 50% Kelly (default until min-sample gate cleared: 10 trades per strategy)
- `weight = 0.5` → 25% Kelly (unreliable strategy)
- `weight = 1.2` → 60% Kelly (consistently accurate strategy)

### Category Accuracy (`data/category_accuracy.json`)

Per-category direction accuracy from resolved trades (min 8 samples). When accuracy < 50%, the category position cap tightens to 1. Updated weekly alongside strategy weights.

## Expected Workflow

1. **:00 every hour** — Research runs, scans markets, writes `market_research.json`, appends to `market_snapshots.db`
2. **:10 every hour** — Position resolution frees closed slots, records P&L to `calibration.db`
3. **:20 every hour** — Trading runs, executes up to 2 highest-EV opportunities
4. **19:00 daily** — Summary report sent to Telegram group
5. **Sunday 02:00** — Calibration harvest fetches 2000+ resolved markets
6. **Monday 07:00** — EV accuracy report by strategy and confidence band
7. **Monday 07:30** — Strategy weights and category accuracy recomputed and committed

## Project Structure

```
manifold-trading-bot/
├── automation/          # Cron-scheduled scripts
│   ├── auto_research.py
│   ├── auto_trader.py
│   ├── daily_summary.py
│   ├── send_telegram.py
│   └── setup_cron_jobs.py
│
├── manifold_bot/        # Core Python package
│   ├── manifold_api.py
│   ├── paper_trader.py          # paper trading engine + bet_outcomes writer
│   ├── strategies.py
│   ├── ai_analyzer.py           # Ollama + DeepSeek AI integration
│   ├── config.py
│   └── paper_trading_state.json # live state: balance + open positions
│
├── scripts/             # Maintenance and pipeline scripts
│   ├── resolve_positions.py
│   ├── harvest_resolved.py
│   ├── analyze_calibration.py
│   ├── weekly_ev_report.py
│   ├── compute_strategy_weights.py
│   └── show_portfolio.py
│
├── data/                # Persistent data files (not committed except weights)
│   ├── calibration.db           # bet_outcomes, calibration tables
│   ├── market_snapshots.db      # hourly market state snapshots (M1)
│   ├── strategy_weights.json    # per-strategy reliability scalars
│   └── category_accuracy.json   # per-category direction accuracy
│
├── tests/
│   ├── test_strategies.py       # 81 unit tests (strategies, trader, AI, phases B/C)
│   ├── test_calibration.py      # 136 unit tests (calibration pipeline, weights, by_category)
│   └── test_swap.py             # 66 unit tests (position swap, EV regression)
│
├── docs/
│   ├── AUTOMATION_README.md     # this file
│   └── technical_overview.md    # deep-dive architecture doc
│
└── meta/memory/         # Daily session notes (moved into meta/ during 2026-05 portfolio freeze)
```
