#!/usr/bin/env python3
"""
Quick test script for Manifold API setup
"""

import sys
sys.path.insert(0, '/home/hackathon/.openclaw/workspace')

from manifold_bot.manifold_api import api_client

print("Testing Manifold API setup...")
print("="*50)

try:
    # Test 1: Get user info
    print("1. Testing API connection...")
    user_info = api_client.get_user_info()
    print(f"   ✅ Success! Connected as: {user_info.get('name', 'Unknown')}")
    print(f"   Username: {user_info.get('username', 'Unknown')}")
    print(f"   Balance: ${user_info.get('balance', 0):.2f}")
    
    # Test 2: Get markets
    print("\n2. Fetching markets...")
    markets = api_client.get_markets(limit=3)
    print(f"   ✅ Found {len(markets)} markets")
    
    for i, market in enumerate(markets[:3], 1):
        question = market.get('question', 'Unknown')[:60] + '...'
        prob = market.get('probability', 0) * 100
        print(f"   {i}. {question}")
        print(f"      Probability: {prob:.1f}% YES")
    
    # Test 3: Search markets
    print("\n3. Testing search...")
    search_results = api_client.search_markets("bitcoin", limit=2)
    if search_results:
        print(f"   ✅ Found {len(search_results)} Bitcoin markets")
        for market in search_results[:2]:
            print(f"      - {market.get('question', 'Unknown')[:50]}...")
    else:
        print("   ⚠️  No Bitcoin markets found (that's okay)")
    
    print("\n" + "="*50)
    print("✅ All tests passed! Your Manifold API is working correctly.")
    print("\nNext steps:")
    print("1. Run the main bot: python -m manifold_bot.main")
    print("2. Try demo mode: python -c 'from manifold_bot.main import demo_paper_trading; demo_paper_trading()'")
    print("3. Explore interactive mode for full features")
    
except Exception as e:
    print(f"\n❌ Error: {e}")
    print("\nTroubleshooting:")
    print("1. Check your API key in manifold_bot/config.py")
    print("2. Verify internet connection")
    print("3. Make sure requests library is installed: pip install requests")
    import traceback
    traceback.print_exc()