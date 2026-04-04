#!/usr/bin/env python3
import json
import os
from datetime import datetime
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib import colors
from reportlab.lib.units import inch

# Load data
with open('/home/hackathon/.openclaw/workspace/auto_trades.json', 'r') as f:
    auto_trades = json.load(f)
with open('/home/hackathon/.openclaw/workspace/manifold_bot/paper_trading_state.json', 'r') as f:
    paper_state = json.load(f)
with open('/home/hackathon/.openclaw/workspace/market_research.json', 'r') as f:
    market_research = json.load(f)

# Map market ID to question
market_id_to_question = {}
for rec in market_research.get('latest', {}).get('recommendations', []):
    market_id_to_question[rec['market_id']] = rec['question']
market_id_to_question['mock-market-3'] = 'Mock Market 3 (test)'
market_id_to_question['mock-market-1'] = 'Mock Market 1 (test)'

# Build trade details
trades = []
for trade in auto_trades['trades']:
    market_id = trade['market_id']
    question = market_id_to_question.get(market_id, 'Unknown market')
    position = paper_state['positions'].get(market_id, [{}])[0] if market_id in paper_state['positions'] else {}
    status = position.get('status', 'OPEN')
    profit = position.get('profit', 0)
    actual_outcome = position.get('actual_outcome', '')
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
        'strategies': ', '.join(trade['strategies']),
        'timestamp': trade['timestamp'],
        'balance_after': trade['balance_after'],
        'status': status,
        'result': result,
        'profit': profit,
    })

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
starting_balance = 1000
return_pct = (current_balance - starting_balance) / starting_balance * 100
win_rate = len(win_trades) / (len(win_trades) + len(lose_trades)) * 100 if (len(win_trades) + len(lose_trades)) > 0 else 0

# Create PDF
doc = SimpleDocTemplate("/home/hackathon/.openclaw/workspace/trading_report.pdf",
                        pagesize=letter,
                        rightMargin=0.5*inch,
                        leftMargin=0.5*inch,
                        topMargin=0.5*inch,
                        bottomMargin=0.5*inch)
story = []

styles = getSampleStyleSheet()
title_style = ParagraphStyle('CustomTitle', parent=styles['Heading1'], alignment=TA_CENTER, spaceAfter=12)
heading2_style = ParagraphStyle('Heading2', parent=styles['Heading2'], spaceBefore=12, spaceAfter=6)
normal_style = styles['Normal']

# Title
story.append(Paragraph("Hackathon 2026 Trading Bot Report", title_style))
story.append(Paragraph(f"Generated on {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}", styles['Italic']))
story.append(Spacer(1, 0.2*inch))

# Executive Summary
story.append(Paragraph("Executive Summary", heading2_style))
summary_text = f"""
This report summarizes the trading activity of the Manifold prediction market bot developed during Hackathon 2026.
The bot uses multiple strategies (momentum, mean reversion, volume spike, probability direction) to identify trading opportunities.
All trades are paper trades (simulated) using a starting balance of ${starting_balance}.
"""
story.append(Paragraph(summary_text, normal_style))
story.append(Spacer(1, 0.1*inch))

# Stats table
stats_data = [
    ['Total trades placed:', str(total_trades)],
    ['Open positions:', str(len(open_trades))],
    ['Closed positions:', str(len(closed_trades))],
    ['Wins:', str(len(win_trades))],
    ['Losses:', str(len(lose_trades))],
    ['Cancelled:', str(len(cancelled_trades))],
    ['Total invested:', f'${total_invested:.2f}'],
    ['Total profit/loss:', f'${total_profit:.2f}'],
    ['Current balance:', f'${current_balance:.2f}'],
    ['Return:', f'{return_pct:.1f}%'],
    ['Win rate (resolved):', f'{win_rate:.1f}%' if win_rate > 0 else 'N/A'],
]
stats_table = Table(stats_data, colWidths=[2.5*inch, 1.5*inch])
stats_table.setStyle(TableStyle([
    ('FONTNAME', (0,0), (-1,-1), 'Helvetica'),
    ('FONTSIZE', (0,0), (-1,-1), 10),
    ('ALIGN', (0,0), (0,-1), 'LEFT'),
    ('ALIGN', (1,0), (1,-1), 'RIGHT'),
    ('GRID', (0,0), (-1,-1), 0.5, colors.grey),
    ('BACKGROUND', (0,0), (-1,0), colors.lightgrey),
]))
story.append(stats_table)
story.append(Spacer(1, 0.2*inch))

# Trade Details
story.append(Paragraph("Trade Details", heading2_style))
story.append(Paragraph("All trades in chronological order:", normal_style))
story.append(Spacer(1, 0.1*inch))

# Prepare table data
table_data = [['Date', 'Market Question', 'Outcome', 'Amount', 'Prob.', 'Conf.', 'Strategies', 'Result', 'Profit']]
for t in trades:
    date = datetime.fromisoformat(t['timestamp'].replace('Z', '+00:00')).strftime('%Y-%m-%d %H:%M')
    question = t['question']
    if len(question) > 60:
        question = question[:57] + '...'
    outcome = t['outcome']
    amount = f"${t['amount']:.2f}"
    prob = f"{t['probability']:.2f}"
    conf = f"{t['confidence']:.2f}"
    strategies = t['strategies']
    result = t['result']
    profit = f"${t['profit']:.2f}" if t['profit'] != 0 else '--'
    table_data.append([date, question, outcome, amount, prob, conf, strategies, result, profit])

# Create table with column widths
col_widths = [0.8*inch, 2.2*inch, 0.5*inch, 0.6*inch, 0.5*inch, 0.5*inch, 1.0*inch, 0.6*inch, 0.6*inch]
trade_table = Table(table_data, colWidths=col_widths, repeatRows=1)
trade_table.setStyle(TableStyle([
    ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
    ('FONTSIZE', (0,0), (-1,0), 9),
    ('BACKGROUND', (0,0), (-1,0), colors.lightgrey),
    ('ALIGN', (0,0), (-1,0), 'CENTER'),
    ('FONTNAME', (0,1), (-1,-1), 'Helvetica'),
    ('FONTSIZE', (0,1), (-1,-1), 8),
    ('ALIGN', (3,1), (8,-1), 'RIGHT'),
    ('ALIGN', (2,1), (2,-1), 'CENTER'),
    ('GRID', (0,0), (-1,-1), 0.5, colors.grey),
    ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, colors.whitesmoke]),
    ('VALIGN', (0,0), (-1,-1), 'TOP'),
]))
story.append(trade_table)
story.append(Spacer(1, 0.2*inch))

# Strategy Explanation
story.append(Paragraph("Strategy Explanation", heading2_style))
strategy_text = """
• Momentum: Bet on the direction of recent price movement.
• Mean reversion: Bet that extreme probabilities will revert towards 50%.
• Volume spike: Bet on markets with unusually high trading volume.
• Probability direction: Bet on markets where probability is moving in a consistent direction.
"""
story.append(Paragraph(strategy_text, normal_style))
story.append(Spacer(1, 0.1*inch))

# Performance Analysis
story.append(Paragraph("Performance Analysis", heading2_style))
analysis_text = f"""
The bot started with ${starting_balance} and currently has a balance of ${current_balance:.2f}. 
The overall return is {return_pct:.1f}%. The win rate among resolved trades is {win_rate:.1f}%.
"""
story.append(Paragraph(analysis_text, normal_style))
story.append(Spacer(1, 0.1*inch))

# Recommendations
story.append(Paragraph("Recommendations", heading2_style))
rec_text = """
• Consider adjusting confidence thresholds to reduce exposure in low‑confidence trades.
• Diversify across more markets to reduce risk.
• Implement stop‑loss mechanisms for open positions.
• Regularly review and update trading strategies based on market conditions.
"""
story.append(Paragraph(rec_text, normal_style))

# Build PDF
doc.build(story)
print("PDF generated: trading_report.pdf")