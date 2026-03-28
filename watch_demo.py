#!/usr/bin/env python3
"""
Watch demo - shows multiple bot features in action
"""

import time
import sys

def print_step(step, description):
    print(f"\n{'='*60}")
    print(f"STEP {step}: {description}")
    print('='*60)
    time.sleep(1)

def run_demo():
    print("🚀 MANIFOLD BOT LIVE DEMO - WATCH THE BOT IN ACTION")
    print("="*60)
    time.sleep(2)
    
    # Step 1: API Connection
    print_step(1, "API CONNECTION TEST")
    from manifold_bot.manifold_api import api_client
    user = api_client.get_user_info()
    print(f"✅ Connected to Manifold as: {user['name']}")
    print(f"   Real balance: ${user['balance']:.2f}")
    time.sleep(2)
    
    # Step 2: Market Exploration
    print_step(2, "EXPLORING LIVE MARKETS")
    markets = api_client.get_markets(limit=5)
    # Filter markets with probability data
    valid_markets = [m for m in markets if m.get('probability') is not None]
    print(f"✅ Found {len(valid_markets)} tradable markets (with probability data)")
    for i, market in enumerate(valid_markets[:3], 1):
        question = market.get('question', 'Unknown')[:50] + "..." if len(market.get('question', '')) > 50 else market.get('question', 'Unknown')
        prob = market.get('probability', 0) * 100
        print(f"{i}. {question}")
        print(f"   📊 {prob:.1f}% YES | 💰 ${market.get('volume', 0):.0f} volume")
    time.sleep(3)
    
    # Step 3: Paper Trading Setup
    print_step(3, "PAPER TRADING SETUP")
    from manifold_bot.paper_trader import PaperTrader
    trader = PaperTrader(initial_balance=1000)
    print(f"✅ Paper trader created with ${trader.initial_balance:.2f} virtual balance")
    print(f"   Current paper balance: ${trader.balance:.2f}")
    time.sleep(2)
    
    # Step 4: Place Some Trades
    print_step(4, "PLACING PAPER TRADES")
    if valid_markets:
        market1 = valid_markets[0]
        question1 = market1.get('question', 'Unknown')[:60] + "..." if len(market1.get('question', '')) > 60 else market1.get('question', 'Unknown')
        print(f"📈 Trading on: {question1}")
        print(f"   Current probability: {market1.get('probability', 0)*100:.1f}% YES")
        
        # Place a YES bet
        if trader.place_paper_bet(market1['id'], "YES", 50, market1.get('probability', 0.5)):
            print("✅ Placed: YES $50")
            print(f"   New paper balance: ${trader.balance:.2f}")
        
        # Place a NO bet on different market
        if len(valid_markets) > 1:
            market2 = valid_markets[1]
            question2 = market2.get('question', 'Unknown')[:60] + "..." if len(market2.get('question', '')) > 60 else market2.get('question', 'Unknown')
            print(f"\n📉 Trading on: {question2}")
            print(f"   Current probability: {market2.get('probability', 0)*100:.1f}% YES")
            
            if trader.place_paper_bet(market2['id'], "NO", 30, market2.get('probability', 0.5)):
                print("✅ Placed: NO $30")
                print(f"   New paper balance: ${trader.balance:.2f}")
    time.sleep(3)
    
    # Step 5: Show Portfolio
    print_step(5, "PORTFOLIO OVERVIEW")
    trader.print_portfolio_summary()
    time.sleep(3)
    
    # Step 6: Strategy Scanner
    print_step(6, "STRATEGY SCANNER")
    from manifold_bot.dashboard import scan_markets_for_opportunities
    opportunities = scan_markets_for_opportunities(limit=10)
    
    if opportunities:
        print(f"✅ Found {len(opportunities)} trading opportunities")
        print("\nTop opportunity:")
        opp = opportunities[0]
        question = opp['question'][:60] + "..." if len(opp['question']) > 60 else opp['question']
        print(f"📋 {question}")
        print(f"🎯 Action: {opp['recommended_action']}")
        print(f"📊 Confidence: {opp['confidence']*100:.0f}%")
        print(f"💰 Current: {opp['current_probability']*100:.1f}% YES")
    else:
        print("⚠️ No strong opportunities found right now")
    time.sleep(3)
    
    # Step 7: Performance Metrics
    print_step(7, "PERFORMANCE METRICS")
    from manifold_bot.dashboard import TradingDashboard
    dashboard = TradingDashboard(trader)
    
    print("📈 Key Performance Indicators:")
    metrics = trader.get_performance_metrics()
    print(f"   📊 Total Trades: {metrics.get('total_trades', 0)}")
    print(f"   ✅ Win Rate: {metrics.get('win_rate', 0)*100:.1f}%")
    print(f"   💰 Total P&L: ${metrics.get('total_pnl', 0):.2f}")
    print(f"   🏦 Current Balance: ${metrics.get('current_balance', 0):.2f}")
    print(f"   📉 Return: {metrics.get('return_pct', 0):.2f}%")
    time.sleep(3)
    
    # Step 8: Resolve a Market
    print_step(8, "MARKET RESOLUTION")
    if valid_markets:
        print(f"🔮 Resolving market as YES...")
        trader.resolve_market(valid_markets[0]['id'], "YES")
        print("\n📊 Updated Portfolio:")
        trader.print_portfolio_summary()
    time.sleep(3)
    
    # Step 9: Export Data
    print_step(9, "DATA EXPORT")
    import json
    from datetime import datetime
    
    data = {
        'demo_completed_at': datetime.now().isoformat(),
        'final_balance': trader.balance,
        'total_trades': len(trader.trade_history),
        'performance_summary': trader.get_performance_metrics()
    }
    
    filename = f"watch_demo_{datetime.now().strftime('%H%M%S')}.json"
    with open(filename, 'w') as f:
        json.dump(data, f, indent=2)
    
    print(f"✅ Data exported to: {filename}")
    print(f"   📁 File ready for analysis")
    time.sleep(2)
    
    # Final Summary
    print("\n" + "="*60)
    print("🎉 DEMO COMPLETE - BOT IS FULLY OPERATIONAL!")
    print("="*60)
    print("\nWhat you just saw:")
    print("1. ✅ Real API connection to Manifold Markets")
    print("2. ✅ Live market data streaming")
    print("3. ✅ Paper trading with virtual money")
    print("4. ✅ Portfolio management")
    print("5. ✅ Strategy scanning")
    print("6. ✅ Performance tracking")
    print("7. ✅ Market resolution")
    print("8. ✅ Data export")
    print("\n🚀 Ready for your hackathon project!")
    print("Run: python3 -m manifold_bot.main  for interactive mode")

if __name__ == "__main__":
    try:
        run_demo()
    except KeyboardInterrupt:
        print("\n\nDemo interrupted by user")
    except Exception as e:
        print(f"\nError during demo: {e}")
        import traceback
        traceback.print_exc()