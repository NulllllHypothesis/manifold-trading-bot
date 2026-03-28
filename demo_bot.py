#!/usr/bin/env python3
"""
End-to-end demo of the Manifold Paper Trading Bot
"""

import sys
import time
from datetime import datetime

print("="*60)
print("MANIFOLD PAPER TRADING BOT - END-TO-END DEMO")
print("="*60)
print()

# Step 1: Test API Connection
print("Step 1: Testing API Connection...")
try:
    from manifold_bot.manifold_api import api_client
    user = api_client.get_user_info()
    print(f"✅ Connected as: {user.get('name', 'Unknown')}")
    print(f"   Username: {user.get('username', 'Unknown')}")
    print(f"   Balance: ${user.get('balance', 0):.2f}")
except Exception as e:
    print(f"❌ Connection failed: {e}")
    sys.exit(1)

print()

# Step 2: Explore Markets
print("Step 2: Exploring Available Markets...")
try:
    markets = api_client.get_markets(limit=5)
    print(f"✅ Found {len(markets)} markets")
    print()
    
    print("Top 5 markets:")
    for i, market in enumerate(markets, 1):
        question = market.get('question', 'Unknown')
        if len(question) > 60:
            question = question[:57] + "..."
        prob = market.get('probability', 0) * 100
        volume = market.get('volume', 0)
        liquidity = market.get('liquidity', 0)
        
        print(f"{i}. {question}")
        print(f"   Probability: {prob:.1f}% YES | Volume: ${volume:.0f} | Liquidity: ${liquidity:.0f}")
        
        # Store first market for demo trading
        if i == 1:
            demo_market = market
        print()
        
except Exception as e:
    print(f"❌ Error fetching markets: {e}")
    demo_market = None

print()

# Step 3: Initialize Paper Trader
print("Step 3: Initializing Paper Trader...")
try:
    from manifold_bot.paper_trader import PaperTrader
    trader = PaperTrader(initial_balance=1000)
    print(f"✅ Paper trader created with ${trader.initial_balance:.2f} virtual balance")
    print(f"   Current balance: ${trader.balance:.2f}")
except Exception as e:
    print(f"❌ Error creating paper trader: {e}")
    sys.exit(1)

print()

# Step 4: Demo Paper Trading
if demo_market:
    print("Step 4: Demo Paper Trading...")
    market_id = demo_market.get('id')
    question = demo_market.get('question', 'Unknown')
    if len(question) > 50:
        question = question[:47] + "..."
    probability = demo_market.get('probability', 0.5)
    
    print(f"Trading on: {question}")
    print(f"Market ID: {market_id}")
    print(f"Current probability: {probability*100:.1f}% YES")
    print()
    
    # Place a YES bet
    print("Placing paper trade: YES $50...")
    if trader.place_paper_bet(market_id, "YES", 50, probability):
        print("✅ Trade placed successfully!")
    else:
        print("❌ Trade failed")
    
    print()
    
    # Place a NO bet on a different market
    print("Finding another market for NO bet...")
    try:
        # Get a different market
        more_markets = api_client.get_markets(limit=10)
        for market in more_markets:
            if market.get('id') != market_id and not market.get('isResolved', False):
                market2_id = market.get('id')
                market2_question = market.get('question', 'Unknown')
                if len(market2_question) > 50:
                    market2_question = market2_question[:47] + "..."
                market2_prob = market.get('probability', 0.5)
                
                print(f"Trading on: {market2_question}")
                print(f"Current probability: {market2_prob*100:.1f}% YES")
                print("Placing paper trade: NO $30...")
                
                if trader.place_paper_bet(market2_id, "NO", 30, market2_prob):
                    print("✅ Trade placed successfully!")
                else:
                    print("❌ Trade failed")
                break
    except Exception as e:
        print(f"⚠️ Could not place second trade: {e}")

print()

# Step 5: Show Portfolio
print("Step 5: Portfolio Summary...")
trader.print_portfolio_summary()

print()

# Step 6: Demo Strategy Scanner
print("Step 6: Running Strategy Scanner...")
try:
    from manifold_bot.dashboard import scan_markets_for_opportunities
    opportunities = scan_markets_for_opportunities(limit=20)
    
    if opportunities:
        print(f"✅ Found {len(opportunities)} trading opportunities")
        print()
        print("Top 3 opportunities:")
        for i, opp in enumerate(opportunities[:3], 1):
            question = opp.get('question', 'Unknown')
            if len(question) > 50:
                question = question[:47] + "..."
            prob = opp.get('current_probability', 0) * 100
            action = opp.get('recommended_action')
            confidence = opp.get('confidence', 0) * 100
            
            print(f"{i}. {question}")
            print(f"   Current: {prob:.1f}% YES")
            print(f"   Action: {action} (Confidence: {confidence:.0f}%)")
            print()
    else:
        print("⚠️ No trading opportunities found")
        
except Exception as e:
    print(f"❌ Error running strategy scanner: {e}")

print()

# Step 7: Demo Market Resolution
print("Step 7: Demo Market Resolution...")
if demo_market:
    print(f"Resolving market as YES...")
    trader.resolve_market(market_id, "YES")
    print()
    print("Updated Portfolio:")
    trader.print_portfolio_summary()

print()

# Step 8: Performance Metrics
print("Step 8: Performance Metrics...")
try:
    from manifold_bot.dashboard import TradingDashboard
    dashboard = TradingDashboard(trader)
    
    print("Key Metrics:")
    metrics = trader.get_performance_metrics()
    
    print(f"   Total Trades: {metrics.get('total_trades', 0)}")
    print(f"   Win Rate: {metrics.get('win_rate', 0)*100:.1f}%")
    print(f"   Total P&L: ${metrics.get('total_pnl', 0):.2f}")
    print(f"   Current Balance: ${metrics.get('current_balance', 0):.2f}")
    print(f"   Return: {metrics.get('return_pct', 0):.2f}%")
    
except Exception as e:
    print(f"❌ Error calculating metrics: {e}")

print()

# Step 9: Export Data
print("Step 9: Data Export Demo...")
try:
    import json
    filename = f"trading_demo_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    
    data = {
        'demo_run_at': datetime.now().isoformat(),
        'initial_balance': trader.initial_balance,
        'current_balance': trader.balance,
        'performance_metrics': trader.get_performance_metrics(),
        'trade_count': len(trader.trade_history),
        'open_positions': len([p for p in trader.trade_history if p.get('status') == 'OPEN'])
    }
    
    with open(filename, 'w') as f:
        json.dump(data, f, indent=2, default=str)
    
    print(f"✅ Data exported to: {filename}")
    print(f"   File size: {len(json.dumps(data))} bytes")
    
except Exception as e:
    print(f"⚠️ Could not export data: {e}")

print()
print("="*60)
print("DEMO COMPLETE! 🎯")
print("="*60)
print()
print("Next steps you can try:")
print("1. Run interactive mode: python3 -m manifold_bot.main")
print("2. Try different strategies in strategies.py")
print("3. Extend the bot with your own features")
print("4. Track performance over time")
print()
print("Happy trading! 📈")