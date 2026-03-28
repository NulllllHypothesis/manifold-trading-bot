"""
Paper Trading Bot for Manifold Markets
Simulates trading without using real play money.
"""

import json
import pandas as pd
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from .manifold_api import api_client
from .config import INITIAL_BALANCE, MIN_BET_AMOUNT, MAX_BET_AMOUNT


class PaperTrader:
    """Paper trading simulation for Manifold Markets"""
    
    def __init__(self, initial_balance: float = INITIAL_BALANCE):
        self.initial_balance = initial_balance
        self.balance = initial_balance
        self.positions = {}  # market_id -> position info
        self.trade_history = []
        self.performance_metrics = {}
        
        # Load/save state
        self.state_file = "paper_trading_state.json"
        self.load_state()
    
    def load_state(self):
        """Load trading state from file"""
        try:
            with open(self.state_file, 'r') as f:
                state = json.load(f)
                self.balance = state.get('balance', self.initial_balance)
                self.positions = state.get('positions', {})
                self.trade_history = state.get('trade_history', [])
                print(f"Loaded state: Balance=${self.balance:.2f}, {len(self.positions)} positions")
        except FileNotFoundError:
            print("No saved state found, starting fresh")
    
    def save_state(self):
        """Save trading state to file"""
        state = {
            'balance': self.balance,
            'positions': self.positions,
            'trade_history': self.trade_history,
            'saved_at': datetime.now().isoformat()
        }
        with open(self.state_file, 'w') as f:
            json.dump(state, f, indent=2)
    
    def place_paper_bet(self, market_id: str, outcome: str, 
                       amount: float, probability: float) -> bool:
        """
        Place a paper trade (simulated bet)
        
        Args:
            market_id: Manifold market ID
            outcome: "YES" or "NO"
            amount: Bet amount
            probability: Current market probability (0-1)
        
        Returns:
            bool: True if trade successful
        """
        # Validate inputs
        if amount < MIN_BET_AMOUNT:
            print(f"Amount ${amount} below minimum ${MIN_BET_AMOUNT}")
            return False
        
        if amount > MAX_BET_AMOUNT:
            print(f"Amount ${amount} above maximum ${MAX_BET_AMOUNT}")
            return False
        
        if amount > self.balance:
            print(f"Insufficient balance: ${self.balance:.2f} < ${amount}")
            return False
        
        if outcome not in ["YES", "NO"]:
            print(f"Invalid outcome: {outcome}. Must be 'YES' or 'NO'")
            return False
        
        # Calculate expected value
        if outcome == "YES":
            payout = amount / probability if probability > 0 else 0
            profit_if_win = payout - amount
            profit_if_lose = -amount
        else:  # NO
            payout = amount / (1 - probability) if probability < 1 else 0
            profit_if_win = payout - amount
            profit_if_lose = -amount
        
        # Record trade
        trade = {
            'trade_id': len(self.trade_history) + 1,
            'timestamp': datetime.now().isoformat(),
            'market_id': market_id,
            'outcome': outcome,
            'amount': amount,
            'probability': probability,
            'payout_if_win': payout,
            'profit_if_win': profit_if_win,
            'profit_if_lose': profit_if_lose,
            'status': 'OPEN'
        }
        
        # Update balance and positions
        self.balance -= amount
        
        if market_id not in self.positions:
            self.positions[market_id] = []
        
        self.positions[market_id].append(trade)
        self.trade_history.append(trade)
        
        print(f"Paper trade placed: {outcome} ${amount} @ {probability:.1%}")
        print(f"New balance: ${self.balance:.2f}")
        
        self.save_state()
        return True
    
    def resolve_market(self, market_id: str, outcome: str):
        """
        Resolve a market and calculate P&L
        
        Args:
            market_id: Market to resolve
            outcome: Actual outcome ("YES" or "NO")
        """
        if market_id not in self.positions:
            print(f"No positions in market {market_id}")
            return
        
        positions = self.positions[market_id]
        total_pnl = 0
        
        for trade in positions:
            if trade['status'] != 'OPEN':
                continue
            
            if trade['outcome'] == outcome:
                # Winning trade
                profit = trade['profit_if_win']
                trade['status'] = 'WIN'
                trade['actual_outcome'] = outcome
                trade['profit'] = profit
                total_pnl += profit
                print(f"Trade {trade['trade_id']}: WIN +${profit:.2f}")
            else:
                # Losing trade
                profit = trade['profit_if_lose']
                trade['status'] = 'LOSE'
                trade['actual_outcome'] = outcome
                trade['profit'] = profit
                total_pnl += profit
                print(f"Trade {trade['trade_id']}: LOSE ${profit:.2f}")
        
        # Update balance
        self.balance += total_pnl
        
        print(f"Market {market_id} resolved as {outcome}")
        print(f"Total P&L: ${total_pnl:.2f}")
        print(f"New balance: ${self.balance:.2f}")
        
        self.save_state()
    
    def get_performance_metrics(self) -> Dict:
        """Calculate performance metrics"""
        if not self.trade_history:
            return {}
        
        df = pd.DataFrame(self.trade_history)
        
        # Filter resolved trades
        resolved_trades = df[df['status'].isin(['WIN', 'LOSE'])]
        
        if len(resolved_trades) == 0:
            return {
                'total_trades': len(df),
                'open_trades': len(df[df['status'] == 'OPEN']),
                'current_balance': self.balance,
                'total_pnl': 0,
                'win_rate': 0
            }
        
        # Calculate metrics
        win_trades = resolved_trades[resolved_trades['status'] == 'WIN']
        lose_trades = resolved_trades[resolved_trades['status'] == 'LOSE']
        
        total_pnl = resolved_trades['profit'].sum()
        win_rate = len(win_trades) / len(resolved_trades) if len(resolved_trades) > 0 else 0
        
        avg_win = win_trades['profit'].mean() if len(win_trades) > 0 else 0
        avg_loss = lose_trades['profit'].mean() if len(lose_trades) > 0 else 0
        profit_factor = abs(win_trades['profit'].sum() / lose_trades['profit'].sum()) if len(lose_trades) > 0 and lose_trades['profit'].sum() != 0 else float('inf')
        
        self.performance_metrics = {
            'total_trades': len(df),
            'resolved_trades': len(resolved_trades),
            'open_trades': len(df) - len(resolved_trades),
            'win_trades': len(win_trades),
            'lose_trades': len(lose_trades),
            'win_rate': win_rate,
            'total_pnl': total_pnl,
            'current_balance': self.balance,
            'return_pct': ((self.balance - self.initial_balance) / self.initial_balance * 100),
            'avg_win': avg_win,
            'avg_loss': avg_loss,
            'profit_factor': profit_factor,
            'largest_win': win_trades['profit'].max() if len(win_trades) > 0 else 0,
            'largest_loss': lose_trades['profit'].min() if len(lose_trades) > 0 else 0
        }
        
        return self.performance_metrics
    
    def print_portfolio_summary(self):
        """Print portfolio summary"""
        metrics = self.get_performance_metrics()
        
        print("\n" + "="*50)
        print("PORTFOLIO SUMMARY")
        print("="*50)
        print(f"Initial Balance: ${self.initial_balance:.2f}")
        print(f"Current Balance: ${self.balance:.2f}")
        print(f"Total P&L: ${metrics.get('total_pnl', 0):.2f}")
        print(f"Return: {metrics.get('return_pct', 0):.2f}%")
        print(f"Total Trades: {metrics.get('total_trades', 0)}")
        print(f"Win Rate: {metrics.get('win_rate', 0)*100:.1f}%")
        print(f"Profit Factor: {metrics.get('profit_factor', 0):.2f}")
        print("="*50)
        
        # Show open positions
        open_positions = [p for p in self.trade_history if p.get('status') == 'OPEN']
        if open_positions:
            print(f"\nOpen Positions: {len(open_positions)}")
            for pos in open_positions[-5:]:  # Show last 5
                market_id = pos['market_id']
                try:
                    # Try to get market info
                    market = api_client.get_market(market_id)
                    question = market.get('question', 'Unknown Market')
                    if len(question) > 50:
                        question = question[:47] + "..."
                    print(f"  📊 {question}")
                    print(f"    ID: {market_id[:8]}... | {pos['outcome']} ${pos['amount']} @ {pos['probability']:.1%}")
                except:
                    # Fallback to ID if API fails
                    print(f"  Market {market_id[:8]}...: {pos['outcome']} ${pos['amount']} @ {pos['probability']:.1%}")
    
    def get_market_analysis(self, market_id: str) -> Dict:
        """Get analysis for a specific market"""
        try:
            market = api_client.get_market(market_id)
            
            analysis = {
                'id': market.get('id'),
                'question': market.get('question'),
                'probability': market.get('probability'),
                'volume': market.get('volume'),
                'liquidity': market.get('liquidity'),
                'created_time': market.get('createdTime'),
                'close_time': market.get('closeTime'),
                'is_resolved': market.get('isResolved', False),
                'resolution': market.get('resolution'),
                'tags': market.get('tags', [])
            }
            
            return analysis
        except Exception as e:
            print(f"Error analyzing market {market_id}: {e}")
            return {}