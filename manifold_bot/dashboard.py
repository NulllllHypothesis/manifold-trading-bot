"""
Dashboard for monitoring paper trading performance
"""

import json
import pandas as pd
from datetime import datetime, timedelta
from typing import Dict, List
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from .paper_trader import PaperTrader
from .manifold_api import api_client
from .strategies import analyze_market_for_trading


class TradingDashboard:
    """Interactive dashboard for paper trading"""
    
    def __init__(self, trader: PaperTrader):
        self.trader = trader
    
    def generate_performance_report(self) -> Dict:
        """Generate comprehensive performance report"""
        metrics = self.trader.get_performance_metrics()
        
        report = {
            'summary': {
                'current_balance': metrics.get('current_balance', 0),
                'total_pnl': metrics.get('total_pnl', 0),
                'return_pct': metrics.get('return_pct', 0),
                'win_rate': metrics.get('win_rate', 0),
                'profit_factor': metrics.get('profit_factor', 0),
                'sharpe_ratio': self._calculate_sharpe_ratio(),
                'max_drawdown': self._calculate_max_drawdown()
            },
            'recent_trades': self._get_recent_trades(10),
            'top_performing_markets': self._get_top_markets(),
            'worst_performing_markets': self._get_worst_markets(),
            'daily_performance': self._get_daily_performance(),
            'generated_at': datetime.now().isoformat()
        }
        
        return report
    
    def _calculate_sharpe_ratio(self, risk_free_rate: float = 0.02) -> float:
        """Calculate Sharpe ratio (simplified)"""
        trades = [t for t in self.trader.trade_history if t.get('profit') is not None]
        if len(trades) < 2:
            return 0
        
        returns = [t['profit'] / t['amount'] for t in trades if t['amount'] > 0]
        if not returns:
            return 0
        
        avg_return = sum(returns) / len(returns)
        std_return = pd.Series(returns).std()
        
        if std_return == 0:
            return 0
        
        return (avg_return - risk_free_rate/365) / std_return
    
    def _calculate_max_drawdown(self) -> float:
        """Calculate maximum drawdown"""
        if not self.trader.trade_history:
            return 0
        
        # Simulate balance over time
        balance = self.trader.initial_balance
        balances = [balance]
        
        for trade in sorted(self.trader.trade_history, key=lambda x: x.get('timestamp', '')):
            if trade.get('profit') is not None:
                balance += trade['profit']
                balances.append(balance)
        
        if len(balances) < 2:
            return 0
        
        # Calculate drawdown
        peak = balances[0]
        max_dd = 0
        
        for balance in balances:
            if balance > peak:
                peak = balance
            dd = (peak - balance) / peak
            max_dd = max(max_dd, dd)
        
        return max_dd * 100  # Return as percentage
    
    def _get_recent_trades(self, limit: int = 10) -> List[Dict]:
        """Get most recent trades"""
        trades = sorted(self.trader.trade_history, 
                       key=lambda x: x.get('timestamp', ''), 
                       reverse=True)[:limit]
        
        # Add market info
        for trade in trades:
            market_id = trade.get('market_id')
            if market_id:
                try:
                    market = api_client.get_market(market_id)
                    trade['market_question'] = market.get('question', 'Unknown')[:50] + '...'
                except:
                    trade['market_question'] = 'Unknown'
        
        return trades
    
    def _get_top_markets(self, limit: int = 5) -> List[Dict]:
        """Get top performing markets by P&L"""
        # Group trades by market
        market_pnl = {}
        
        for trade in self.trader.trade_history:
            if trade.get('profit') is None:
                continue
            
            market_id = trade.get('market_id')
            if market_id not in market_pnl:
                market_pnl[market_id] = {'total_pnl': 0, 'trades': 0}
            
            market_pnl[market_id]['total_pnl'] += trade['profit']
            market_pnl[market_id]['trades'] += 1
        
        # Sort by P&L
        sorted_markets = sorted(market_pnl.items(), 
                               key=lambda x: x[1]['total_pnl'], 
                               reverse=True)[:limit]
        
        result = []
        for market_id, data in sorted_markets:
            try:
                market = api_client.get_market(market_id)
                result.append({
                    'market_id': market_id,
                    'question': market.get('question', 'Unknown')[:60] + '...',
                    'total_pnl': data['total_pnl'],
                    'trades': data['trades'],
                    'avg_pnl_per_trade': data['total_pnl'] / data['trades'] if data['trades'] > 0 else 0
                })
            except:
                continue
        
        return result
    
    def _get_worst_markets(self, limit: int = 5) -> List[Dict]:
        """Get worst performing markets by P&L"""
        markets = self._get_top_markets(limit * 2)  # Get more to filter
        return sorted(markets, key=lambda x: x['total_pnl'])[:limit]
    
    def _get_daily_performance(self) -> List[Dict]:
        """Get daily P&L performance"""
        if not self.trader.trade_history:
            return []
        
        # Group trades by day
        daily_pnl = {}
        
        for trade in self.trader.trade_history:
            if trade.get('profit') is None:
                continue
            
            timestamp = trade.get('timestamp', '')
            if timestamp:
                try:
                    date = datetime.fromisoformat(timestamp.replace('Z', '+00:00')).date()
                    date_str = date.isoformat()
                    
                    if date_str not in daily_pnl:
                        daily_pnl[date_str] = 0
                    
                    daily_pnl[date_str] += trade['profit']
                except:
                    continue
        
        # Convert to list
        return [{'date': date, 'pnl': pnl} for date, pnl in sorted(daily_pnl.items())]
    
    def create_visualizations(self):
        """Create Plotly visualizations"""
        metrics = self.trader.get_performance_metrics()
        
        # Create subplots
        fig = make_subplots(
            rows=2, cols=2,
            subplot_titles=('Balance Over Time', 'Daily P&L', 
                          'Win/Loss Distribution', 'Market Performance'),
            specs=[[{'type': 'scatter'}, {'type': 'bar'}],
                   [{'type': 'pie'}, {'type': 'bar'}]]
        )
        
        # 1. Balance over time (simplified)
        if self.trader.trade_history:
            balances = [self.trader.initial_balance]
            times = [datetime.now() - timedelta(days=7)]  # Start 7 days ago
            
            # Simulate balance changes
            current_balance = self.trader.initial_balance
            for trade in sorted(self.trader.trade_history, key=lambda x: x.get('timestamp', '')):
                if trade.get('profit') is not None:
                    current_balance += trade['profit']
                    balances.append(current_balance)
                    
                    try:
                        time = datetime.fromisoformat(trade['timestamp'].replace('Z', '+00:00'))
                        times.append(time)
                    except:
                        times.append(datetime.now())
            
            fig.add_trace(
                go.Scatter(x=times, y=balances, mode='lines+markers', name='Balance'),
                row=1, col=1
            )
        
        # 2. Daily P&L
        daily_data = self._get_daily_performance()
        if daily_data:
            dates = [d['date'] for d in daily_data]
            pnls = [d['pnl'] for d in daily_data]
            
            colors = ['green' if pnl >= 0 else 'red' for pnl in pnls]
            
            fig.add_trace(
                go.Bar(x=dates, y=pnls, marker_color=colors, name='Daily P&L'),
                row=1, col=2
            )
        
        # 3. Win/Loss distribution
        if metrics.get('win_trades', 0) > 0 or metrics.get('lose_trades', 0) > 0:
            fig.add_trace(
                go.Pie(
                    labels=['Wins', 'Losses'],
                    values=[metrics.get('win_trades', 0), metrics.get('lose_trades', 0)],
                    hole=0.3
                ),
                row=2, col=1
            )
        
        # 4. Top markets performance
        top_markets = self._get_top_markets(5)
        if top_markets:
            market_names = [m['question'][:30] + '...' for m in top_markets]
            market_pnls = [m['total_pnl'] for m in top_markets]
            
            colors = ['green' if pnl >= 0 else 'red' for pnl in market_pnls]
            
            fig.add_trace(
                go.Bar(x=market_names, y=market_pnls, marker_color=colors, name='Market P&L'),
                row=2, col=2
            )
        
        # Update layout
        fig.update_layout(
            height=800,
            showlegend=True,
            title_text="Paper Trading Dashboard"
        )
        
        return fig
    
    def print_dashboard(self):
        """Print text-based dashboard to console"""
        report = self.generate_performance_report()
        summary = report['summary']
        
        print("\n" + "="*60)
        print("MANIFOLD PAPER TRADING DASHBOARD")
        print("="*60)
        
        print(f"\n📊 PERFORMANCE SUMMARY")
        print(f"   Current Balance: ${summary['current_balance']:.2f}")
        print(f"   Total P&L: ${summary['total_pnl']:.2f}")
        print(f"   Return: {summary['return_pct']:.2f}%")
        print(f"   Win Rate: {summary['win_rate']*100:.1f}%")
        print(f"   Profit Factor: {summary['profit_factor']:.2f}")
        print(f"   Sharpe Ratio: {summary['sharpe_ratio']:.3f}")
        print(f"   Max Drawdown: {summary['max_drawdown']:.2f}%")
        
        print(f"\n📈 RECENT TRADES (Last 5)")
        recent_trades = report['recent_trades'][:5]
        for trade in recent_trades:
            status = trade.get('status', 'OPEN')
            profit = trade.get('profit', 0)
            profit_str = f"+${profit:.2f}" if profit >= 0 else f"-${abs(profit):.2f}"
            
            print(f"   {status}: {trade.get('outcome', '?')} ${trade.get('amount', 0):.2f} "
                  f"→ {profit_str}")
        
        print(f"\n🏆 TOP MARKETS")
        for market in report['top_performing_markets'][:3]:
            print(f"   ${market['total_pnl']:.2f}: {market['question']}")
        
        print(f"\n⚠️  WORST MARKETS")
        for market in report['worst_performing_markets'][:3]:
            print(f"   ${market['total_pnl']:.2f}: {market['question']}")
        
        print(f"\n📅 DAILY PERFORMANCE")
        for day in report['daily_performance'][-3:]:  # Last 3 days
            pnl = day['pnl']
            pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
            print(f"   {day['date']}: {pnl_str}")
        
        print("\n" + "="*60)


def scan_markets_for_opportunities(limit: int = 20) -> List[Dict]:
    """Scan markets for trading opportunities"""
    try:
        markets = api_client.get_markets(limit=limit)
        opportunities = []
        
        for market in markets:
            if market.get('isResolved', False):
                continue
            
            analysis = analyze_market_for_trading(market)
            if analysis['recommended_action']:
                opportunities.append(analysis)
        
        # Sort by confidence
        opportunities.sort(key=lambda x: x['confidence'], reverse=True)
        return opportunities[:10]  # Return top 10
        
    except Exception as e:
        print(f"Error scanning markets: {e}")
        return []