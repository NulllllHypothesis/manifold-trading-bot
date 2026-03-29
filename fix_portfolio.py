#!/usr/bin/env python3
"""
Fix portfolio balance and consistency
"""

import json
from datetime import datetime

# Load current state
with open('paper_trading_state.json', 'r') as f:
    state = json.load(f)

print("BEFORE FIX:")
print(f"Balance: ${state['balance']}")
print(f"Total trades: {len(state['trade_history'])}")

# Count open vs resolved
open_trades = [t for t in state['trade_history'] if t.get('status') == 'OPEN']
resolved_trades = [t for t in state['trade_history'] if t.get('status') in ['WIN', 'LOSE']]

print(f"Open trades: {len(open_trades)}")
print(f"Resolved trades: {len(resolved_trades)}")

# Check for inconsistencies
print("\nCHECKING FOR INCONSISTENCIES:")
inconsistencies = []

# Check each market in positions
for market_id, positions in state['positions'].items():
    for pos in positions:
        # Find corresponding trade in history
        trade = next((t for t in state['trade_history'] if t['trade_id'] == pos['trade_id']), None)
        if trade and trade['status'] != pos.get('status', 'OPEN'):
            inconsistencies.append({
                'trade_id': pos['trade_id'],
                'position_status': pos.get('status'),
                'history_status': trade['status'],
                'market_id': market_id
            })

if inconsistencies:
    print(f"Found {len(inconsistencies)} inconsistencies:")
    for inc in inconsistencies:
        print(f"  Trade {inc['trade_id']}: Position={inc['position_status']}, History={inc['history_status']}")
    
    # Fix them
    print("\nFIXING INCONSISTENCIES...")
    for inc in inconsistencies:
        # Update trade history to match position status
        for trade in state['trade_history']:
            if trade['trade_id'] == inc['trade_id']:
                trade['status'] = inc['position_status']
                if inc['position_status'] == 'WIN' and 'profit' not in trade:
                    # Add profit if missing
                    trade['profit'] = trade.get('profit_if_win', 0)
                print(f"  Fixed trade {inc['trade_id']}: {inc['history_status']} -> {inc['position_status']}")
else:
    print("No inconsistencies found")

# Recalculate balance
print("\nRECALCULATING BALANCE...")
initial_balance = 1000
calculated_balance = initial_balance

for trade in state['trade_history']:
    amount = trade['amount']
    
    if trade['status'] == 'OPEN':
        calculated_balance -= amount
    elif trade['status'] == 'WIN':
        profit = trade.get('profit', trade.get('profit_if_win', 0))
        calculated_balance += profit
    elif trade['status'] == 'LOSE':
        # Already deducted when placed
        pass

print(f"Initial balance: ${initial_balance}")
print(f"Calculated balance: ${calculated_balance}")
print(f"Stored balance: ${state['balance']}")
print(f"Difference: ${calculated_balance - state['balance']}")

# Update balance
state['balance'] = calculated_balance
state['saved_at'] = datetime.now().isoformat()

# Save fixed state
with open('paper_trading_state_fixed.json', 'w') as f:
    json.dump(state, f, indent=2)

print(f"\n✅ Fixed state saved to paper_trading_state_fixed.json")
print(f"New balance: ${state['balance']}")

# Also create a clean backup
clean_state = {
    'balance': 1000,  # Start fresh
    'positions': {},
    'trade_history': [],
    'saved_at': datetime.now().isoformat()
}

with open('paper_trading_state_clean.json', 'w') as f:
    json.dump(clean_state, f, indent=2)

print(f"✅ Clean slate saved to paper_trading_state_clean.json (balance: $1000)")