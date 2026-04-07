# Automated Trading System - Setup Complete

## 🎯 What We've Built

A complete automated trading system for Manifold Markets with:

### 1. **Hourly Market Research** (`automation/auto_research.py`)
- Analyzes 100+ markets every hour
- Applies momentum and mean reversion strategies
- Saves recommendations to `market_research.json`
- Confidence scoring for each opportunity

### 2. **Hourly Auto Trading** (`automation/auto_trader.py`)
- Executes trades based on research
- Risk management: max 10 positions, 10% per trade, open-position guard prevents re-entry
- Selects trades by estimated EV (highest edge first); confidence is a hard gate (≥65%)
- Logs all auto-trades to `auto_trades.json`

### 3. **Daily Summary Report** (`automation/daily_summary.py`)
- Generates comprehensive daily report
- Calculates performance metrics
- Formats message for Telegram
- Ready for automated delivery

### 4. **Cron Job Configuration** (`automation/setup_cron_jobs.py`)
- 7 scheduled jobs:
  - Research: Every hour at :00 (UTC)
  - Position Resolution: Every hour at :10 (UTC)
  - Trading: Every hour at :20 (UTC)
  - Position Swap Check: Every hour at :40 (UTC)
  - Summary: Daily at 19:00 (UTC)
  - Calibration Harvest: Sundays at 02:00 (UTC)
  - EV Accuracy Report: Mondays at 07:00 (UTC)
- Telegram delivery configured for group chat

## 📊 Current Trading Status
- **Balance**: $1,129.00 (starting from $1,000)
- **Profit**: +$129.00 (+12.9%)
- **Win Rate**: 100% (4/4 closed trades)
- **Open Positions**: 3 markets

## 🚀 Quick Start

### 1. Test the System
```bash
cd /home/hackathon/.openclaw/workspace

# Test research script
python3 automation/auto_research.py

# Test trading script
python3 automation/auto_trader.py

# Test daily summary
python3 automation/daily_summary.py

# Run all tests
python3 tests/test_automation.py
```

### 2. Setup Cron Jobs
```bash
# Create cron jobs from config
openclaw cron add --file automation/cron_jobs_config.json

# Verify jobs
openclaw cron list

# Check status
openclaw cron status
```

### 3. Monitor Execution
```bash
# Watch logs
tail -f ~/.openclaw/logs/gateway.log

# Check research results
cat market_research.json | jq '.latest.recommendations[0]'

# Check auto trades
cat auto_trades.json | jq '.trades[-1]'
```

## ⚙️ Configuration Files

### `automation/cron_jobs_config.json`
Contains 3 pre-configured jobs:
1. `market-research-hourly` - Runs at :00 every hour
2. `auto-trading-hourly` - Runs at :15 every hour
3. `daily-summary` - Runs at 19:00 UTC daily

### Risk Parameters (in `automation/auto_trader.py`)
- `max_positions`: 10 (maximum open positions)
- `max_position_size`: 0.1 (10% of balance per trade, Kelly sizing when AI estimate available)
- `min_confidence`: 0.65 (65% confidence minimum, hard gate)
- Open-position guard: no re-entry on any market already held (use swap to replace)

## 📈 Expected Workflow

1. **:00 every hour** — Research runs, analyzes markets, saves recommendations
2. **:10 every hour** — Position resolution frees closed slots
3. **:20 every hour** — Trading runs, executes up to 2 highest-EV opportunities
4. **:40 every hour** — Swap checker proposes replacements for losing positions
5. **19:00 daily** — Summary generated, sent to Telegram group
6. **Sunday 02:00** — Calibration harvest fetches 2000+ resolved markets
7. **Monday 07:00** — EV accuracy report by strategy and confidence band

## 🗂️ Project Structure

```
manifold-trading-bot/
├── automation/          # Cron-scheduled scripts
│   ├── auto_research.py
│   ├── auto_trader.py
│   ├── daily_summary.py
│   ├── send_telegram.py
│   ├── setup_cron_jobs.py
│   └── cron_jobs_config.json
├── tests/               # Test suite
│   ├── test_manifold.py
│   └── test_automation.py
├── scripts/             # Dev & utility tools
│   ├── demo_bot.py
│   ├── fix_portfolio.py
│   ├── get_market_ids.py
│   ├── show_portfolio.py
│   └── watch_demo.py
├── manifold_bot/        # Core Python package
│   ├── config.py
│   ├── manifold_api.py
│   ├── paper_trader.py
│   ├── strategies.py
│   └── dashboard.py
├── docs/                # Documentation
└── requirements.txt
```

## 🎯 Next Steps for Production

### Immediate:
1. ✅ Test all scripts manually
2. ⬜ Set up cron jobs
3. ⬜ Monitor first few cycles
4. ⬜ Adjust risk parameters as needed

### Phase 2 (Enhancements):
1. **AI Integration** - Add DeepSeek analysis to research
2. **Telegram Bot** - Real message sending (not placeholder)
3. **Web Dashboard** - Real-time monitoring interface
4. **Advanced Strategies** - More sophisticated trading logic

### Phase 3 (Scaling):
1. **SQLite Database** - Replace JSON files (Phase 1 from PLAN.md)
2. **Backtesting** - Historical strategy testing
3. **Multi-account** - Support multiple paper trading accounts
4. **Alert System** - Custom alerts for specific conditions
