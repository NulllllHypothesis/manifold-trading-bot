#!/usr/bin/env python3
"""
Show detailed portfolio with market information
"""

from manifold_bot.paper_trader import PaperTrader
from manifold_bot.manifold_api import api_client

print("="*60)
print("📊 DETAILED PORTFOLIO VIEW")
print("="*60)

# Load trader
trader = PaperTrader()

# Get performance metrics
metrics = trader.get_performance_metrics()

print(f"\n💰 BALANCE: ${trader.balance:.2f} (Started: ${trader.initial_balance:.2f})")
print(f"📈 P&L: ${metrics.get('total_pnl', 0):.2f}")
print(f"🎯 Win Rate: {metrics.get('win_rate', 0)*100:.1f}%")
print(f"📊 Total Trades: {metrics.get('total_trades', 0)}")
print()

# Show all trades
print("📋 ALL TRADES:")
print("-"*60)

for i, trade in enumerate(trader.trade_history, 1):
    market_id = trade.get('market_id', 'Unknown')
    outcome = trade.get('outcome', '?')
    amount = trade.get('amount', 0)
    probability = trade.get('probability', 0)
    status = trade.get('status', 'UNKNOWN')
    profit = trade.get('profit', 0)
    
    # Try to get market info
    market_info = "Unknown Market"
    try:
        market = api_client.get_market(market_id)
        market_info = market.get('question', 'Unknown Market')
        if len(market_info) > 60:
            market_info = market_info[:57] + "..."
    except:
        market_info = f"Market {market_id[:8]}..."
    
    # Format trade info
    status_icon = "✅" if status == 'WIN' else "❌" if status == 'LOSE' else "⏳"
    profit_str = f" (+${profit:.2f})" if profit > 0 else f" (${profit:.2f})" if profit < 0 else ""
    
    print(f"{i}. {status_icon} {market_info}")
    print(f"   {outcome} ${amount:.2f} @ {probability:.1%} | Status: {status}{profit_str}")
    print()

# Show open positions separately
open_trades = [t for t in trader.trade_history if t.get('status') == 'OPEN']
if open_trades:
    print("⏳ OPEN POSITIONS:")
    print("-"*60)
    
    for i, trade in enumerate(open_trades, 1):
        market_id = trade.get('market_id', 'Unknown')
        
        # Get detailed market info
        try:
            market = api_client.get_market(market_id)
            question = market.get('question', 'Unknown Market')
            current_prob = market.get('probability', 0) * 100
            volume = market.get('volume', 0)
            liquidity = market.get('liquidity', 0)
            
            if len(question) > 50:
                question = question[:47] + "..."
            
            print(f"{i}. {question}")
            print(f"   ID: {market_id}")
            print(f"   Your bet: {trade['outcome']} ${trade['amount']} @ {trade['probability']:.1%}")
            print(f"   Current: {current_prob:.1f}% YES | Volume: ${volume:.0f}")
            
            # Calculate potential P&L
            if trade['outcome'] == 'YES':
                potential_payout = trade['amount'] / trade['probability'] if trade['probability'] > 0 else 0
                potential_profit = potential_payout - trade['amount']
            else:  # NO
                potential_payout = trade['amount'] / (1 - trade['probability']) if trade['probability'] < 1 else 0
                potential_profit = potential_payout - trade['amount']
            
            print(f"   Potential: +${potential_profit:.2f} if {trade['outcome']}")
            print()
            
        except Exception as e:
            print(f"{i}. Market {market_id[:8]}... - Error fetching details: {e}")
            print()

# Show resolved trades
resolved_trades = [t for t in trader.trade_history if t.get('status') in ['WIN', 'LOSE']]
if resolved_trades:
    print("📊 RESOLVED TRADES:")
    print("-"*60)
    
    wins = [t for t in resolved_trades if t.get('status') == 'WIN']
    losses = [t for t in resolved_trades if t.get('status') == 'LOSE']
    
    total_profit = sum(t.get('profit', 0) for t in resolved_trades)
    
    print(f"Wins: {len(wins)} | Losses: {len(losses)}")
    print(f"Total P&L from resolved: ${total_profit:.2f}")
    print()

print("="*60)
print("💡 Tip: Run 'python3 -m manifold_bot.main' for interactive trading")
print("="*60)