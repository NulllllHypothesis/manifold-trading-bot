# Manifold Paper Trading Bot

A paper trading bot for Manifold Markets that allows you to practice prediction market trading without risking real play money.

## Features

- **Paper Trading**: Simulate trades with virtual money
- **Real Market Data**: Connect to real Manifold Markets API
- **Performance Tracking**: Monitor P&L, win rate, Sharpe ratio, etc.
- **Trading Strategies**: Momentum, mean reversion, arbitrage detection
- **Interactive Dashboard**: Visualize performance and opportunities
- **Data Export**: Save trading history for analysis

## Setup

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure API Key
Your Manifold API key is already configured in `config.py`:
```python
MANIFOLD_API_KEY = "22a41d7d-10e8-44bf-93d6-5dff623f27a9"
```

### 3. Test Connection
```bash
python -m manifold_bot.main
```

## Usage

### Quick Start
```python
from manifold_bot.main import demo_paper_trading
demo_paper_trading()
```

### Interactive Mode
Run the interactive trading interface:
```bash
python -m manifold_bot.main
```

### API Examples
```python
from manifold_bot.manifold_api import api_client

# Get user info
user = api_client.get_user_info()
print(f"Hello {user['name']}!")

# Get markets
markets = api_client.get_markets(limit=10)
for market in markets:
    print(f"{market['question'][:50]}... - {market['probability']*100:.1f}%")

# Search markets
results = api_client.search_markets("bitcoin", limit=5)
```

### Paper Trading
```python
from manifold_bot.paper_trader import PaperTrader

# Create trader with $1000 virtual balance
trader = PaperTrader(initial_balance=1000)

# Place paper trade
trader.place_paper_bet(
    market_id="abc123...",
    outcome="YES",
    amount=50,
    probability=0.65  # Current market probability
)

# Resolve market
trader.resolve_market(market_id="abc123...", outcome="YES")

# Get performance metrics
metrics = trader.get_performance_metrics()
print(f"Win Rate: {metrics['win_rate']*100:.1f}%")
```

## Project Structure

```
manifold_bot/
├── config.py              # API configuration
├── manifold_api.py        # Manifold API client
├── paper_trader.py        # Paper trading simulation
├── strategies.py          # Trading strategies
├── dashboard.py           # Performance dashboard
├── main.py               # Entry point & interactive mode
├── requirements.txt      # Python dependencies
└── README.md            # This file
```

## Trading Strategies

### 1. Momentum Strategy
- Bets in direction of recent price movement
- Uses probability thresholds to avoid noise

### 2. Mean Reversion Strategy
- Bets against extreme probabilities (>80% or <20%)
- Assumes probabilities tend to revert to mean

### 3. Arbitrage Detection
- Finds price discrepancies between related markets
- Groups markets by keywords/tags

### 4. Kelly Criterion
- Calculates optimal bet size based on edge
- Manages risk through position sizing

## Performance Metrics

The bot tracks:
- **Win Rate**: Percentage of profitable trades
- **Profit Factor**: Gross profit / gross loss
- **Sharpe Ratio**: Risk-adjusted returns
- **Maximum Drawdown**: Largest peak-to-trough decline
- **Daily P&L**: Profit/loss by day
- **Market Performance**: P&L by market

## Dashboard

The dashboard provides:
- Balance over time chart
- Daily P&L bar chart
- Win/loss distribution pie chart
- Top markets performance
- Text-based console output

```python
from manifold_bot.dashboard import TradingDashboard

trader = PaperTrader()
dashboard = TradingDashboard(trader)
dashboard.print_dashboard()

# Generate visualizations (requires plotly)
fig = dashboard.create_visualizations()
fig.show()
```

## Learning Resources

### Manifold Markets
- [API Documentation](https://docs.manifold.markets/api)
- [Python Library](https://pypi.org/project/manifoldpy/)
- [Community Discord](https://discord.com/invite/eHQBNBqXuh)

### Prediction Markets
- [How Prediction Markets Work](https://manifold.markets/help)
- [Trading Strategies](https://www.predictit.org/help/trading-strategies)
- [Probability Calibration](https://arxiv.org/abs/1802.02538)

## Safety Notes

1. **Paper Trading Only**: This bot uses virtual money for learning
2. **API Rate Limits**: Respect Manifold's API rate limits
3. **No Financial Advice**: This is for educational purposes only
4. **Data Privacy**: Your API key is stored locally in `config.py`

## Next Steps

1. **Start Simple**: Use the demo mode to understand paper trading
2. **Test Strategies**: Try different strategies in interactive mode
3. **Track Performance**: Use the dashboard to monitor results
4. **Extend Functionality**: Add your own strategies or features

## Troubleshooting

### API Connection Issues
- Verify your API key in `config.py`
- Check internet connection
- Ensure `requests` library is installed

### Import Errors
```bash
# Install from project root
pip install -e .
```

### Plotly Not Showing Charts
```bash
# Install Jupyter or use offline mode
pip install notebook
```

## Contributing

Feel free to:
- Add new trading strategies
- Improve the dashboard
- Fix bugs or add features
- Create better documentation

## License

Educational use only. Not for real trading.