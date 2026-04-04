#!/usr/bin/env python3
import json
import os
from datetime import datetime

# Load auto_trades.json
with open('/home/hackathon/.openclaw/workspace/auto_trades.json', 'r') as f:
    auto_trades = json.load(f)

# Load paper_trading_state.json
with open('/home/hackathon/.openclaw/workspace/manifold_bot/paper_trading_state.json', 'r') as f:
    paper_state = json.load(f)

# Load market_research.json
with open('/home/hackathon/.openclaw/workspace/market_research.json', 'r') as f:
    market_research = json.load(f)

# Map market ID to question from market_research
market_id_to_question = {}
for rec in market_research.get('latest', {}).get('recommendations', []):
    market_id_to_question[rec['market_id']] = rec['question']

# Add mock markets (these are test markets)
market_id_to_question['mock-market-3'] = 'Mock Market 3 (test)'
market_id_to_question['mock-market-1'] = 'Mock Market 1 (test)'

# For real IDs not in research, try to fetch from somewhere else? We'll keep as unknown for now.

# Build trade details
trades = []
for trade in auto_trades['trades']:
    market_id = trade['market_id']
    question = market_id_to_question.get(market_id, 'Unknown market')
    # Find position details from paper_state
    position = paper_state['positions'].get(market_id, [{}])[0] if market_id in paper_state['positions'] else {}
    status = position.get('status', 'OPEN')
    profit = position.get('profit', 0)
    actual_outcome = position.get('actual_outcome', '')
    # Determine win/lose/cancelled/open
    if status == 'WIN':
        result = 'WIN'
    elif status == 'LOSE':
        result = 'LOSE'
    elif status == 'CLOSED' and actual_outcome == 'CANCELLED':
        result = 'CANCELLED'
    else:
        result = 'OPEN'
    
    trades.append({
        'market_id': market_id,
        'question': question,
        'outcome': trade['outcome'],
        'amount': trade['amount'],
        'probability': trade['probability'],
        'confidence': trade['confidence'],
        'strategies': trade['strategies'],
        'timestamp': trade['timestamp'],
        'balance_after': trade['balance_after'],
        'status': status,
        'result': result,
        'profit': profit,
    })

# Sort by timestamp
trades.sort(key=lambda x: x['timestamp'])

# Summary stats
total_trades = len(trades)
closed_trades = [t for t in trades if t['result'] in ('WIN', 'LOSE', 'CANCELLED')]
open_trades = [t for t in trades if t['result'] == 'OPEN']
win_trades = [t for t in trades if t['result'] == 'WIN']
lose_trades = [t for t in trades if t['result'] == 'LOSE']
cancelled_trades = [t for t in trades if t['result'] == 'CANCELLED']
total_invested = sum(t['amount'] for t in trades)
total_profit = sum(t['profit'] for t in trades)
current_balance = paper_state.get('balance', 0)

# Generate LaTeX
latex = '''\\documentclass[12pt]{article}
\\usepackage[margin=1in]{geometry}
\\usepackage{booktabs}
\\usepackage{longtable}
\\usepackage{graphicx}
\\usepackage{hyperref}
\\title{Hackathon Trading Bot Report}
\\author{Automated Trading Assistant}
\\date{''' + datetime.now().strftime('%Y-%m-%d %H:%M UTC') + '''}
\\begin{document}
\\maketitle

\\section*{Executive Summary}
This report summarizes the trading activity of the Manifold prediction market bot developed during Hackathon 2026. The bot uses multiple strategies (momentum, mean reversion, volume spike, probability direction) to identify trading opportunities. All trades are paper trades (simulated) using a starting balance of \\$1000.

\\begin{itemize}
    \\item Total trades placed: ''' + str(total_trades) + '''
    \\item Open positions: ''' + str(len(open_trades)) + '''
    \\item Closed positions: ''' + str(len(closed_trades)) + ''' (''' + str(len(win_trades)) + ''' wins, ''' + str(len(lose_trades)) + ''' losses, ''' + str(len(cancelled_trades)) + ''' cancelled)
    \\item Total invested: \\$''' + f"{total_invested:.2f}" + '''
    \\item Total profit/loss: \\$''' + f"{total_profit:.2f}" + '''
    \\item Current balance: \\$''' + f"{current_balance:.2f}" + '''
\\end{itemize}

\\section{Trade Details}
All trades are listed in chronological order.

\\begin{longtable}{p{0.15\\textwidth}p{0.4\\textwidth}cccccc}
\\toprule
\\textbf{Date} & \\textbf{Market Question} & \\textbf{Outcome} & \\textbf{Amount} & \\textbf{Prob.} & \\textbf{Conf.} & \\textbf{Result} & \\textbf{Profit} \\\\
\\midrule
\\endhead
'''

for t in trades:
    date = datetime.fromisoformat(t['timestamp'].replace('Z', '+00:00')).strftime('%Y-%m-%d %H:%M')
    # Escape LaTeX special characters in question
    question = t['question'].replace('&', '\\&').replace('%', '\\%').replace('$', '\\$').replace('#', '\\#').replace('_', '\\_').replace('{', '\\{').replace('}', '\\}')
    # Shorten if too long
    if len(question) > 80:
        question = question[:77] + '...'
    outcome = t['outcome']
    amount = f"\\${t['amount']:.2f}"
    prob = f"{t['probability']:.2f}"
    conf = f"{t['confidence']:.2f}"
    result = t['result']
    profit = f"\\${t['profit']:.2f}" if t['profit'] != 0 else '--'
    strategies = ', '.join(t['strategies'])
    # Add strategies as a note in parentheses after question
    question_with_strat = question + ' \\scriptsize{(' + strategies + ')}'
    latex += f"{date} & {question_with_strat} & {outcome} & {amount} & {prob} & {conf} & {result} & {profit} \\\\\n"

latex += '''\\bottomrule
\\end{longtable}

\\section{Strategy Explanation}
\\begin{description}
    \\item[momentum] Bet on the direction of recent price movement.
    \\item[mean\\_reversion] Bet that extreme probabilities will revert towards 50\\%.
    \\item[volume\\_spike] Bet on markets with unusually high trading volume.
    \\item[probability\\_direction] Bet on markets where probability is moving in a consistent direction.
\\end{description}

\\section{Performance Analysis}
The bot started with \\$1000 and currently has a balance of \\$''' + f"{current_balance:.2f}" + '''. The overall return is ''' + f"{(current_balance - 1000) / 10:.1f}" + '''\\%. The win rate among resolved trades is ''' + (f"{(len(win_trades) / (len(win_trades) + len(lose_trades)) * 100):.1f}\\%" if (len(win_trades) + len(lose_trades)) > 0 else "N/A") + '''.

\\section{Recommendations}
\\begin{itemize}
    \\item Consider adjusting confidence thresholds to reduce exposure in low‑confidence trades.
    \\item Diversify across more markets to reduce risk.
    \\item Implement stop‑loss mechanisms for open positions.
\\end{itemize}

\\end{document}
'''

# Write LaTeX file
with open('/home/hackathon/.openclaw/workspace/report.tex', 'w') as f:
    f.write(latex)

print('Generated report.tex')