"""
Paper Trading Bot for Manifold Markets
Simulates trading without using real play money.
"""

import json
import os
import sqlite3
import pandas as pd
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from .manifold_api import api_client
from .config import INITIAL_BALANCE, MIN_BET_AMOUNT, MAX_BET_AMOUNT

# SQLite database — shared with calibration scripts
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DB_PATH = _PROJECT_ROOT / "data" / "calibration.db"


def _init_bet_outcomes_db() -> None:
    """Create bet_outcomes table if it doesn't exist yet."""
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bet_outcomes (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id          TEXT NOT NULL,
            our_recommendation TEXT,    -- YES or NO (what we bet)
            amount             REAL,    -- stake
            probability        REAL,    -- market prob when we placed the bet
            estimated_ev       REAL,    -- EV predicted at trade time (NULL = no AI estimate)
            ai_confidence      REAL,    -- AI confidence score (NULL = no AI)
            strategies         TEXT,    -- JSON list of strategy names that triggered
            market_resolution  TEXT,    -- YES or NO (actual outcome)
            actual_pnl         REAL,    -- realized profit/loss
            ev_error           REAL,    -- estimated_ev - actual_pnl; NULL when estimated_ev is NULL
            resolved_at        TEXT     -- ISO timestamp
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_bet_outcomes_market
        ON bet_outcomes(market_id)
    """)
    conn.commit()
    conn.close()


def _write_bet_outcome(trade: Dict, market_resolution: str, actual_pnl: float) -> None:
    """
    Append one resolved-trade record to bet_outcomes.

    Called by resolve_market() for every position that closes.
    The trade dict is the record originally written by place_paper_bet(),
    so it already carries estimated_ev, ai_confidence, and strategies if
    auto_trader passed them in.

    ev_error = estimated_ev - actual_pnl
      > 0 means AI overestimated the edge (predicted more profit than occurred)
      < 0 means AI underestimated (we made more than predicted)
      NULL when there was no AI estimate (confidence-scaled fallback)
    """
    try:
        estimated_ev = trade.get("estimated_ev")
        ev_error = (estimated_ev - actual_pnl) if estimated_ev is not None else None

        strategies = trade.get("strategies")
        strategies_json = json.dumps(strategies) if strategies is not None else None

        conn = sqlite3.connect(_DB_PATH)
        conn.execute("""
            INSERT INTO bet_outcomes
            (market_id, our_recommendation, amount, probability,
             estimated_ev, ai_confidence, strategies,
             market_resolution, actual_pnl, ev_error, resolved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            trade.get("market_id"),
            trade.get("outcome"),
            trade.get("amount"),
            trade.get("probability"),
            estimated_ev,
            trade.get("ai_confidence"),
            strategies_json,
            market_resolution,
            actual_pnl,
            ev_error,
            datetime.now().isoformat(),
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        # Never let DB errors crash the paper trader
        print(f"[bet_outcomes] warning: could not write outcome for {trade.get('market_id')}: {e}")


class PaperTrader:
    """Paper trading simulation for Manifold Markets"""
    
    def __init__(self, initial_balance: float = INITIAL_BALANCE):
        self.initial_balance = initial_balance
        self.balance = initial_balance
        self.positions = {}  # market_id -> position info
        self.trade_history = []
        self.performance_metrics = {}
        
        # Load/save state
        self.state_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "paper_trading_state.json")
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
                       amount: float, probability: float,
                       estimated_ev: Optional[float] = None,
                       ai_confidence: Optional[float] = None,
                       strategies: Optional[List[str]] = None) -> bool:
        """
        Place a paper trade (simulated bet).

        Args:
            market_id:     Manifold market ID
            outcome:       "YES" or "NO"
            amount:        Bet amount in dollars
            probability:   Current market probability (0–1)
            estimated_ev:  AI-estimated expected value at trade time (stored for calibration)
            ai_confidence: AI confidence score (stored for calibration)
            strategies:    List of strategy names that triggered this trade

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
            'status': 'OPEN',
            # Calibration fields — stored at trade time, read back at resolution time
            'estimated_ev': estimated_ev,
            'ai_confidence': ai_confidence,
            'strategies': strategies,
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
        Resolve a market and calculate P&L.

        Args:
            market_id: Market to resolve
            outcome: Actual outcome ("YES" or "NO")

        Side-effect: appends one row per resolved position to the bet_outcomes
        SQLite table in data/calibration.db for EV calibration tracking.
        """
        if market_id not in self.positions:
            print(f"No positions in market {market_id}")
            return

        _init_bet_outcomes_db()   # no-op if table already exists

        positions = self.positions[market_id]
        total_pnl = 0
        updated_trade_ids = []

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

            # Write outcome to SQLite for EV calibration tracking
            _write_bet_outcome(trade, market_resolution=outcome, actual_pnl=trade['profit'])

            updated_trade_ids.append(trade['trade_id'])
        
        # Also update trade_history to stay consistent
        for trade in self.trade_history:
            if trade['trade_id'] in updated_trade_ids:
                # Find the corresponding position to copy status
                for pos in positions:
                    if pos['trade_id'] == trade['trade_id']:
                        trade['status'] = pos['status']
                        if 'actual_outcome' in pos:
                            trade['actual_outcome'] = pos['actual_outcome']
                        if 'profit' in pos:
                            trade['profit'] = pos['profit']
                        break
        
        # Update balance
        self.balance += total_pnl
        
        print(f"Market {market_id} resolved as {outcome}")
        print(f"Total P&L: ${total_pnl:.2f}")
        print(f"New balance: ${self.balance:.2f}")
        
        self.save_state()
    

    def auto_resolve_markets(self):
        """Auto-check Manifold for resolved markets and update positions"""
        resolved_count = 0
        for market_id in list(self.positions.keys()):
            open_positions = [p for p in self.positions[market_id] if p.get('status') == 'OPEN']
            if not open_positions:
                continue
            try:
                market = api_client.get_market(market_id)
                if market.get('isResolved', False):
                    resolution = market.get('resolution', '')
                    if resolution in ['YES', 'NO']:
                        print(f"Auto-resolving {market.get('question', market_id)[:50]}... as {resolution}")
                        self.resolve_market(market_id, resolution)
                        resolved_count += 1
            except Exception as e:
                print(f"Error checking market {market_id}: {e}")
        if resolved_count:
            print(f"Auto-resolved {resolved_count} market(s)")
        else:
            print("No markets to auto-resolve")
        return resolved_count

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
        
        # Calculate money in open trades
        open_trades = df[df['status'] == 'OPEN']
        money_in_open = open_trades['amount'].sum() if len(open_trades) > 0 else 0
        
        # Calculate potential P&L for open trades (simplified)
        potential_pnl = 0
        for _, trade in open_trades.iterrows():
            if trade['outcome'] == 'YES':
                potential_payout = trade['amount'] / trade['probability'] if trade['probability'] > 0 else 0
                potential_profit = potential_payout - trade['amount']
            else:
                potential_payout = trade['amount'] / (1 - trade['probability']) if trade['probability'] < 1 else 0
                potential_profit = potential_payout - trade['amount']
            potential_pnl += potential_profit
        
        self.performance_metrics = {
            'total_trades': len(df),
            'resolved_trades': len(resolved_trades),
            'open_trades': len(open_trades),
            'win_trades': len(win_trades),
            'lose_trades': len(lose_trades),
            'win_rate': win_rate,
            'realized_pnl': total_pnl,  # Renamed for clarity
            'current_balance': self.balance,
            'initial_balance': self.initial_balance,
            'net_return_pct': ((self.balance - self.initial_balance) / self.initial_balance * 100),
            'money_in_open_trades': money_in_open,
            'potential_pnl_open': potential_pnl,
            'total_exposure': money_in_open + abs(total_pnl),
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
        
        print("\n" + "="*60)
        print("📊 PORTFOLIO SUMMARY")
        print("="*60)
        
        # Balance section
        print("💰 BALANCE:")
        print(f"  Initial: ${self.initial_balance:.2f}")
        print(f"  Current: ${self.balance:.2f}")
        print(f"  Net Return: {metrics.get('net_return_pct', 0):.2f}%")
        
        # Realized P&L section
        print("\n✅ REALIZED P&L (Closed Trades):")
        print(f"  Total: ${metrics.get('realized_pnl', 0):.2f}")
        print(f"  Wins: {metrics.get('win_trades', 0)}")
        print(f"  Losses: {metrics.get('lose_trades', 0)}")
        print(f"  Win Rate: {metrics.get('win_rate', 0)*100:.1f}%")
        
        # Open positions section
        print("\n⏳ OPEN POSITIONS:")
        print(f"  Count: {metrics.get('open_trades', 0)}")
        print(f"  Invested: ${metrics.get('money_in_open_trades', 0):.2f}")
        print(f"  Potential P&L: ${metrics.get('potential_pnl_open', 0):.2f}")
        
        # Risk metrics
        print("\n📈 PERFORMANCE:")
        print(f"  Total Trades: {metrics.get('total_trades', 0)}")
        print(f"  Avg Win: ${metrics.get('avg_win', 0):.2f}")
        print(f"  Avg Loss: ${metrics.get('avg_loss', 0):.2f}")
        print(f"  Profit Factor: {metrics.get('profit_factor', 0):.2f}")
        
        print("="*60)
        
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