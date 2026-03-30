#!/usr/bin/env python3
"""
Get Market IDs for trading
"""

import os
import sys
# Add project root to path so manifold_bot package is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from manifold_bot.manifold_api import api_client

print("="*60)
print("📋 MARKET ID LOOKUP TOOL")
print("="*60)

# Get markets
markets = api_client.get_markets(limit=10)

print(f"\nFound {len(markets)} markets:")
print("-"*60)

for i, market in enumerate(markets, 1):
    question = market.get('question', 'Unknown')
    if len(question) > 50:
        question = question[:47] + "..."
    
    market_id = market.get('id', 'Unknown')
    prob = market.get('probability', 0) * 100
    volume = market.get('volume', 0)
    
    print(f"{i}. {question}")
    print(f"   📍 ID: {market_id}")
    print(f"   📊 {prob:.1f}% YES | 💰 ${volume:.0f}")
    print()

print("="*60)
print("💡 HOW TO USE MARKET IDs:")
print("1. Copy the ID from above")
print("2. In interactive mode, select 'Place paper trade'")
print("3. Paste the ID when prompted")
print("4. Enter YES/NO and amount")
print("="*60)

# Also show your current portfolio market IDs
print("\n📊 YOUR CURRENT PORTFOLIO MARKET IDs:")
print("-"*60)

from manifold_bot.paper_trader import PaperTrader
trader = PaperTrader()

open_trades = [t for t in trader.trade_history if t.get('status') == 'OPEN']
if open_trades:
    for i, trade in enumerate(open_trades, 1):
        market_id = trade.get('market_id', 'Unknown')
        try:
            market = api_client.get_market(market_id)
            question = market.get('question', 'Unknown')
            if len(question) > 40:
                question = question[:37] + "..."
            print(f"{i}. {question}")
            print(f"   ID: {market_id}")
        except:
            print(f"{i}. Market ID: {market_id}")
else:
    print("No open positions")

print("="*60)