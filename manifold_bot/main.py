"""
Main entry point for Manifold Paper Trading Bot
"""

import sys
import json
from datetime import datetime
from .manifold_api import api_client
from .paper_trader import PaperTrader
from .dashboard import TradingDashboard, scan_markets_for_opportunities
from .strategies import TradingStrategies


def test_api_connection():
    """Test connection to Manifold API"""
    print("Testing Manifold API connection...")
    try:
        user_info = api_client.get_user_info()
        print(f"✅ Connected successfully!")
        print(f"   User: {user_info.get('name', 'Unknown')}")
        print(f"   Username: {user_info.get('username', 'Unknown')}")
        print(f"   Balance: ${user_info.get('balance', 0):.2f}")
        return True
    except Exception as e:
        print(f"❌ Connection failed: {e}")
        return False


def explore_markets():
    """Explore available markets"""
    print("\nExploring markets...")
    try:
        markets = api_client.get_markets(limit=10)
        print(f"Found {len(markets)} markets")
        
        for i, market in enumerate(markets[:5], 1):
            question = market.get('question', 'Unknown')[:60] + '...'
            prob = market.get('probability', 0) * 100
            volume = market.get('volume', 0)
            
            print(f"{i}. {question}")
            print(f"   Probability: {prob:.1f}% | Volume: ${volume:.0f}")
        
        return markets
    except Exception as e:
        print(f"Error fetching markets: {e}")
        return []


def demo_paper_trading():
    """Demo paper trading functionality"""
    print("\n" + "="*50)
    print("DEMO: PAPER TRADING")
    print("="*50)
    
    # Create paper trader
    trader = PaperTrader(initial_balance=1000)
    
    # Get some markets
    markets = api_client.get_markets(limit=5)
    
    print("\nAvailable markets for demo:")
    demo_markets = []
    for i, market in enumerate(markets[:3], 1):
        if market.get('isResolved', False):
            continue
        
        demo_markets.append(market)
        question = market.get('question', 'Unknown')[:50] + '...'
        prob = market.get('probability', 0) * 100
        
        print(f"{i}. {question}")
        print(f"   ID: {market.get('id')}")
        print(f"   Current: {prob:.1f}% YES")
    
    if not demo_markets:
        print("No open markets found for demo")
        return
    
    # Demo trades
    print("\nPlacing demo paper trades...")
    
    # Trade 1: Bet YES on first market
    market1 = demo_markets[0]
    amount1 = 50
    prob1 = market1.get('probability', 0.5)
    
    if trader.place_paper_bet(market1['id'], "YES", amount1, prob1):
        print(f"✅ Placed: YES ${amount1} on market 1")
    
    # Trade 2: Bet NO on second market
    if len(demo_markets) > 1:
        market2 = demo_markets[1]
        amount2 = 30
        prob2 = market2.get('probability', 0.5)
        
        if trader.place_paper_bet(market2['id'], "NO", amount2, prob2):
            print(f"✅ Placed: NO ${amount2} on market 2")
    
    # Show portfolio
    trader.print_portfolio_summary()
    
    # Demo resolving a market
    print("\nDemo: Resolving market 1 as YES...")
    trader.resolve_market(market1['id'], "YES")
    
    # Show updated portfolio
    trader.print_portfolio_summary()
    
    return trader


def run_strategy_scanner():
    """Run strategy scanner to find trading opportunities"""
    print("\n" + "="*50)
    print("STRATEGY SCANNER")
    print("="*50)
    
    opportunities = scan_markets_for_opportunities(limit=30)
    
    if not opportunities:
        print("No trading opportunities found")
        return
    
    print(f"Found {len(opportunities)} opportunities:")
    
    for i, opp in enumerate(opportunities[:5], 1):
        question = opp.get('question', 'Unknown')[:60] + '...'
        prob = opp.get('current_probability', 0) * 100
        action = opp.get('recommended_action')
        confidence = opp.get('confidence', 0) * 100
        
        print(f"\n{i}. {question}")
        print(f"   Current: {prob:.1f}% YES")
        print(f"   Action: {action} (Confidence: {confidence:.0f}%)")
        
        signals = opp.get('signals', {})
        if signals:
            print(f"   Signals: {', '.join(f'{k}: {v}' for k, v in signals.items())}")


def interactive_mode():
    """Interactive mode for manual trading"""
    print("\n" + "="*50)
    print("INTERACTIVE TRADING MODE")
    print("="*50)
    
    trader = PaperTrader()
    dashboard = TradingDashboard(trader)
    
    while True:
        print("\nOptions:")
        print("  1. View portfolio")
        print("  2. Scan for opportunities")
        print("  3. Place paper trade")
        print("  4. Resolve market")
        print("  5. Show dashboard")
        print("  6. Export data")
        print("  7. Exit")
        
        choice = input("\nSelect option (1-7): ").strip()
        
        if choice == "1":
            trader.print_portfolio_summary()
        
        elif choice == "2":
            run_strategy_scanner()
        
        elif choice == "3":
            print("\nPlace Paper Trade:")
            market_id = input("Market ID: ").strip()
            outcome = input("Outcome (YES/NO): ").strip().upper()
            amount = float(input("Amount: ").strip())
            
            # Get current probability
            try:
                market = api_client.get_market(market_id)
                prob = market.get('probability', 0.5)
                
                if trader.place_paper_bet(market_id, outcome, amount, prob):
                    print("✅ Trade placed successfully!")
                else:
                    print("❌ Trade failed")
            except Exception as e:
                print(f"Error: {e}")
        
        elif choice == "4":
            print("\nResolve Market:")
            market_id = input("Market ID: ").strip()
            outcome = input("Actual outcome (YES/NO): ").strip().upper()
            
            trader.resolve_market(market_id, outcome)
        
        elif choice == "5":
            dashboard.print_dashboard()
        
        elif choice == "6":
            print("\nExporting data...")
            metrics = trader.get_performance_metrics()
            
            filename = f"trading_data_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            with open(filename, 'w') as f:
                json.dump({
                    'performance_metrics': metrics,
                    'trade_history': trader.trade_history,
                    'positions': trader.positions
                }, f, indent=2, default=str)
            
            print(f"✅ Data exported to {filename}")
        
        elif choice == "7":
            print("Exiting interactive mode")
            break
        
        else:
            print("Invalid choice")


def main():
    """Main function"""
    print("="*60)
    print("MANIFOLD PAPER TRADING BOT")
    print("="*60)
    
    # Test API connection
    if not test_api_connection():
        print("\n❌ Cannot proceed without API connection")
        return
    
    while True:
        print("\nMain Menu:")
        print("  1. Test API Connection")
        print("  2. Explore Markets")
        print("  3. Demo Paper Trading")
        print("  4. Run Strategy Scanner")
        print("  5. Interactive Mode")
        print("  6. Exit")
        
        choice = input("\nSelect option (1-6): ").strip()
        
        if choice == "1":
            test_api_connection()
        
        elif choice == "2":
            explore_markets()
        
        elif choice == "3":
            demo_paper_trading()
        
        elif choice == "4":
            run_strategy_scanner()
        
        elif choice == "5":
            interactive_mode()
        
        elif choice == "6":
            print("\nGoodbye! Happy trading! 🎯")
            break
        
        else:
            print("Invalid choice")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
        sys.exit(0)
    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)