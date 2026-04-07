#!/usr/bin/env python3
"""
Daily summary report for Manifold Paper Trading Bot.
Generates a comprehensive report and sends it to Telegram.
"""

import json
import sys
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import statistics

# Add project root to path so manifold_bot package is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from manifold_bot.paper_trader import PaperTrader

try:
    from scripts.weekly_ev_report import generate_report as _ev_report
except Exception:
    _ev_report = None

class DailySummary:
    """Generate daily trading summary"""

    def __init__(self):
        self.state_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "manifold_bot", "paper_trading_state.json")
        self.research_file = "market_research.json"
        self.trade_log_file = "auto_trades.json"

    def load_data(self) -> Optional[Dict]:
        """Load all necessary data"""
        data = {}

        # Load trading state
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r') as f:
                    data['state'] = json.load(f)
            except Exception as e:
                print(f"Error loading state: {e}")
                return None
        else:
            print(f"State file not found: {self.state_file}")
            return None

        # Load research history
        if os.path.exists(self.research_file):
            try:
                with open(self.research_file, 'r') as f:
                    data['research'] = json.load(f)
            except:
                data['research'] = {}

        # Load auto trades
        if os.path.exists(self.trade_log_file):
            try:
                with open(self.trade_log_file, 'r') as f:
                    data['auto_trades'] = json.load(f)
            except:
                data['auto_trades'] = {}

        return data

    def calculate_daily_metrics(self, data: Dict) -> Dict:
        """Calculate daily performance metrics"""
        state = data.get('state', {})
        trade_history = state.get('trade_history', [])

        # Get today's date
        today = datetime.now().date()
        today_str = today.isoformat()

        # Filter today's trades
        today_trades = []
        for trade in trade_history:
            trade_time = trade.get('timestamp', '')
            if trade_time.startswith(today_str):
                today_trades.append(trade)

        # Calculate metrics
        metrics = {
            'date': today_str,
            'total_trades_today': len(today_trades),
            'balance': state.get('balance', 0),
            'initial_balance': 1000,
            'total_profit': state.get('balance', 0) - 1000
        }

        # Today's profit
        today_profit = sum(t.get('profit', 0) for t in today_trades if t.get('profit') is not None)
        metrics['today_profit'] = today_profit

        # Today's win rate
        today_closed = [t for t in today_trades if t.get('status') in ['WIN', 'LOSE']]
        if today_closed:
            today_wins = sum(1 for t in today_closed if t.get('status') == 'WIN')
            metrics['today_win_rate'] = today_wins / len(today_closed) * 100
            metrics['today_wins'] = today_wins
            metrics['today_losses'] = len(today_closed) - today_wins
        else:
            metrics['today_win_rate'] = 0
            metrics['today_wins'] = 0
            metrics['today_losses'] = 0

        # Open positions
        positions = state.get('positions', {})
        open_positions = []
        for market_id, trades in positions.items():
            open_trades = [t for t in trades if t.get('status') == 'OPEN']
            if open_trades:
                total_invested = sum(t.get('amount', 0) for t in open_trades)
                open_positions.append({
                    'market_id': market_id,
                    'trades': len(open_trades),
                    'total_invested': total_invested
                })

        metrics['open_positions'] = open_positions
        metrics['total_open_positions'] = len(open_positions)
        metrics['total_invested'] = sum(p['total_invested'] for p in open_positions)

        # Research activity
        research = data.get('research', {})
        research_history = research.get('history', [])

        # Count research sessions in last 24 hours
        last_24h = datetime.now() - timedelta(hours=24)
        recent_research = []
        for session in research_history:
            session_time = session.get('timestamp', '')
            if session_time:
                try:
                    session_dt = datetime.fromisoformat(session_time.replace('Z', '+00:00'))
                    if session_dt > last_24h:
                        recent_research.append(session)
                except:
                    continue

        metrics['research_sessions_24h'] = len(recent_research)
        if recent_research:
            avg_opportunities = sum(s.get('total_markets_analyzed', 0) for s in recent_research) / len(recent_research)
            metrics['avg_opportunities_per_research'] = round(avg_opportunities, 1)

        # Auto trading activity
        auto_trades = data.get('auto_trades', {})
        auto_trade_list = auto_trades.get('trades', [])

        # Auto trades in last 24 hours
        recent_auto_trades = []
        for trade in auto_trade_list:
            trade_time = trade.get('timestamp', '')
            if trade_time:
                try:
                    trade_dt = datetime.fromisoformat(trade_time.replace('Z', '+00:00'))
                    if trade_dt > last_24h:
                        recent_auto_trades.append(trade)
                except:
                    continue

        metrics['auto_trades_24h'] = len(recent_auto_trades)

        return metrics

    def generate_summary_text(self, metrics: Dict) -> str:
        """Generate formatted summary text for Telegram"""
        # Emoji headers
        summary = "📊 *DAILY TRADING SUMMARY*\n"
        summary += f"📅 {metrics['date']}\n"
        summary += "═" * 30 + "\n\n"

        # Performance
        summary += "💰 *PERFORMANCE*\n"
        summary += f"Balance: ${metrics['balance']:.2f}\n"
        summary += f"Total Profit: ${metrics['total_profit']:+.2f} ({metrics['total_profit']/10:+.1f}%)\n"
        summary += f"Today's Profit: ${metrics['today_profit']:+.2f}\n"

        if metrics['today_wins'] + metrics['today_losses'] > 0:
            summary += f"Today's Win Rate: {metrics['today_win_rate']:.1f}% "
            summary += f"({metrics['today_wins']}W/{metrics['today_losses']}L)\n"

        summary += "\n"

        # Trading Activity
        summary += "📈 *TRADING ACTIVITY*\n"
        summary += f"Trades Today: {metrics['total_trades_today']}\n"
        summary += f"Open Positions: {metrics['total_open_positions']}\n"
        summary += f"Total Invested: ${metrics['total_invested']:.2f}\n"

        if metrics['open_positions']:
            summary += "\n📋 *TOP OPEN POSITIONS*\n"
            for i, pos in enumerate(metrics['open_positions'][:3], 1):
                summary += f"{i}. Market {pos['market_id'][:8]}...\n"
                summary += f"   ${pos['total_invested']:.2f} ({pos['trades']} trades)\n"

        summary += "\n"

        # Research & Automation
        summary += "🤖 *AUTOMATION STATUS*\n"
        summary += f"Research Sessions (24h): {metrics['research_sessions_24h']}\n"

        if metrics.get('avg_opportunities_per_research'):
            summary += f"Avg Opportunities: {metrics['avg_opportunities_per_research']:.1f}\n"

        summary += f"Auto Trades (24h): {metrics['auto_trades_24h']}\n"

        summary += "\n"

        # Recommendations
        profit = metrics['total_profit']
        if profit > 100:
            summary += "🎉 *EXCELLENT DAY!* Keep up the great work!\n"
        elif profit > 0:
            summary += "👍 *GOOD DAY!* Positive returns achieved.\n"
        elif profit == 0:
            summary += "➖ *BREAK EVEN* - No gains or losses today.\n"
        else:
            summary += "⚠️ *CHALLENGING DAY* - Review strategies.\n"

        summary += "\n"

        # On Mondays, append the weekly EV accuracy section
        if datetime.now().weekday() == 0:  # 0 = Monday
            summary += "📐 *WEEKLY EV ACCURACY*\n"
            # Attempt a fresh local import in case the module became available
            # after the top-level try/except; fall back to the cached reference.
            ev_report_fn = _ev_report
            if ev_report_fn is None:
                try:
                    from scripts.weekly_ev_report import generate_report as ev_report_fn
                except Exception:
                    ev_report_fn = None

            if ev_report_fn is None:
                ev_section = "_(EV report unavailable: module could not be imported)_\n"
            else:
                try:
                    ev_section = ev_report_fn()
                except Exception as e:
                    ev_section = f"EV report unavailable: {e}"
            summary += ev_section
            summary += "\n"

        summary += "Next report: Tomorrow at 19:00 UTC\n"
        summary += "#Hackathon2026 #TradingBot"

        return summary

    def send_to_telegram(self, message: str):
        """Send message to Telegram group"""
        # This would integrate with python-telegram-bot
        # For now, we'll print it and the cron job can handle delivery
        print("\n" + "="*60)
        print("TELEGRAM MESSAGE READY:")
        print("="*60)
        print(message)
        print("="*60)

        # In a real implementation, you would:
        # 1. Import python-telegram-bot
        # 2. Use bot.send_message(chat_id=GROUP_ID, text=message, parse_mode='Markdown')
        # 3. Handle errors and retries

        # For now, we'll create a file that can be sent by another script
        telegram_file = "telegram_daily_summary.txt"
        with open(telegram_file, 'w') as f:
            f.write(message)

        print(f"\nMessage saved to {telegram_file}")
        print("To send manually: python3 automation/send_telegram.py")

    def generate_report(self):
        """Generate and deliver daily report"""
        print(f"\n{'='*60}")
        print(f"DAILY SUMMARY REPORT - {datetime.now()}")
        print(f"{'='*60}")

        # Load data
        data = self.load_data()
        if not data:
            print("No state file found — nothing to summarize yet.")
            return True  # Not an error; bot just hasn't run yet

        # Calculate metrics
        metrics = self.calculate_daily_metrics(data)

        # Print console report
        print(f"\n📊 DAILY METRICS:")
        print(f"   Date: {metrics['date']}")
        print(f"   Balance: ${metrics['balance']:.2f}")
        print(f"   Total Profit: ${metrics['total_profit']:+.2f}")
        print(f"   Today's Profit: ${metrics['today_profit']:+.2f}")
        print(f"   Trades Today: {metrics['total_trades_today']}")
        print(f"   Open Positions: {metrics['total_open_positions']}")

        if metrics['today_wins'] + metrics['today_losses'] > 0:
            print(f"   Today's Win Rate: {metrics['today_win_rate']:.1f}%")

        print(f"   Research Sessions: {metrics['research_sessions_24h']}")
        print(f"   Auto Trades: {metrics['auto_trades_24h']}")

        # Generate Telegram message
        telegram_msg = self.generate_summary_text(metrics)

        # Send to Telegram
        self.send_to_telegram(telegram_msg)

        print(f"\n✅ Daily summary generated successfully")
        return True

def main():
    """Main function"""
    summary = DailySummary()
    success = summary.generate_report()

    return 0 if success else 1

if __name__ == "__main__":
    try:
        exit_code = main()
        sys.exit(exit_code)
    except Exception as e:
        print(f"Fatal error: {e}")
        sys.exit(1)
