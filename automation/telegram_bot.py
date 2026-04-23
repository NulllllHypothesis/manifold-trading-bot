#!/usr/bin/env python3
"""
Telegram bot command handlers for the Manifold paper trading bot.

Each handler reads live state from JSON files and formats a concise
Telegram-ready response. Handlers are designed to be called by OpenClaw
when a user sends a slash command in the Telegram group.

Usage (OpenClaw cron/skill prompt):
    python3 automation/telegram_bot.py portfolio
    python3 automation/telegram_bot.py positions
    python3 automation/telegram_bot.py scan

The script prints the formatted response to stdout (OpenClaw reads stdout
and forwards it to Telegram) AND sends it directly via the Bot API so it
arrives even if the LLM intermediary is slow.

Commands:
    portfolio       — balance, P&L, win rate summary
    positions       — list all open positions with entry vs current price
    scan            — top 5 opportunities from latest market_research.json
    autotrader-on   — remove flag file, resume automated trading
    autotrader-off  — create flag file, pause automated trading
"""

import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from manifold_bot.config import INITIAL_BALANCE
from manifold_bot.manifold_api import api_client
from manifold_bot.paper_trader import PaperTrader
from automation.send_telegram import send_message

# ── File paths ─────────────────────────────────────────────────────────────────
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STATE_FILE    = os.path.join(_ROOT, "manifold_bot", "paper_trading_state.json")
_RESEARCH_FILE = os.path.join(_ROOT, "market_research.json")
_FLAG_FILE     = os.path.join(_ROOT, "autotrader_disabled.flag")


def _load_state() -> dict:
    if not os.path.exists(_STATE_FILE):
        return {}
    with open(_STATE_FILE) as f:
        return json.load(f)


def _load_research() -> dict:
    if not os.path.exists(_RESEARCH_FILE):
        return {}
    with open(_RESEARCH_FILE) as f:
        return json.load(f)


# ── /portfolio ─────────────────────────────────────────────────────────────────

def cmd_portfolio() -> str:
    state = _load_state()
    if not state:
        return "No trading state found yet — bot hasn't run."

    balance = state.get("balance", 0)
    # Honour capital_epochs so P&L reads against the latest deliberate
    # top-up baseline, not the hard-coded INITIAL_BALANCE. Shared helper
    # with daily_summary so both surfaces agree.
    baseline = PaperTrader.baseline_from_state(state, default=INITIAL_BALANCE)
    total_pnl = balance - baseline
    pnl_pct = (total_pnl / baseline) * 100 if baseline else 0.0

    history = state.get("trade_history", [])
    closed = [t for t in history if t.get("status") in ("WIN", "LOSE", "CLOSED_EARLY")]
    wins   = [t for t in closed if t.get("status") == "WIN"]
    win_rate = len(wins) / len(closed) * 100 if closed else 0

    positions = state.get("positions", {})
    open_count = sum(
        1 for trades in positions.values()
        for t in trades if t.get("status") == "OPEN"
    )
    total_invested = sum(
        t.get("amount", 0)
        for trades in positions.values()
        for t in trades if t.get("status") == "OPEN"
    )

    pnl_emoji = "📈" if total_pnl >= 0 else "📉"
    msg  = "💰 *PORTFOLIO*\n"
    msg += f"Balance:   ${balance:,.2f}\n"
    msg += f"P&L:       {pnl_emoji} ${total_pnl:+,.2f} ({pnl_pct:+.1f}%)\n"
    msg += f"Invested:  ${total_invested:,.2f}\n\n"
    msg += f"Positions: {open_count}/10 slots used\n"
    if closed:
        msg += f"Win rate:  {win_rate:.0f}% ({len(wins)}W / {len(closed) - len(wins)}L from {len(closed)} closed)\n"
    msg += f"\n_Updated {datetime.now().strftime('%H:%M UTC')}_"
    return msg


# ── /positions ─────────────────────────────────────────────────────────────────

def cmd_positions() -> str:
    state = _load_state()
    if not state:
        return "No trading state found yet."

    positions = state.get("positions", {})

    # Build a market_id → question title lookup.
    # 1. Seed from latest research (fast, no API call needed for recent markets).
    research = _load_research()
    latest = research.get("latest", research)
    _title_lookup = {
        r.get("market_id"): r.get("question", "")
        for r in latest.get("recommendations", [])
        if r.get("market_id")
    }
    # 2. For positions that still lack a title, fetch from Manifold API.
    missing_ids = [
        mid for mid, trades in positions.items()
        if any(t.get("status") == "OPEN" for t in trades)
        and not _title_lookup.get(mid)
        and not any(t.get("question") for t in trades if t.get("status") == "OPEN")
    ]
    for mid in missing_ids:
        try:
            market = api_client.get_market(mid)
            _title_lookup[mid] = market.get("question", mid)
        except Exception:
            pass
    open_trades = []
    for market_id, trades in positions.items():
        for t in trades:
            if t.get("status") == "OPEN":
                open_trades.append((market_id, t))

    if not open_trades:
        return "No open positions right now."

    msg = f"📋 *OPEN POSITIONS* ({len(open_trades)}/10)\n\n"
    for i, (market_id, t) in enumerate(open_trades, 1):
        direction = t.get("outcome", "?")
        amount    = t.get("amount", 0)
        entry_p   = t.get("entry_probability") or t.get("probability", 0)
        # Blended stat+AI confidence is what the gate actually saw. Fall back to
        # the legacy entry_confidence (which is ai_confidence and is 0 when AI
        # didn't vote) only for rows written before the confidence field existed.
        conf = t.get("confidence")
        if conf is None:
            conf = t.get("entry_confidence") or 0

        # ai_status at entry — lets the reader tell "AI didn't vote" apart from
        # "AI voted with 0% confidence". Older legs lack this field.
        ai_status = t.get("ai_status")

        # Prefer stored question, fall back to research lookup, then market ID
        raw_title = t.get("question") or _title_lookup.get(market_id) or market_id
        question  = raw_title[:60]

        direction_symbol = "🟢" if direction == "YES" else "🔴"
        msg += f"{i}. {direction_symbol} *{direction}* — {question}\n"
        line = f"   ${amount:.0f} at {entry_p*100:.0f}% | conf {conf*100:.0f}%"
        if ai_status:
            line += f" · AI {ai_status}"
        msg += line + "\n"

    msg += f"\n_Updated {datetime.now().strftime('%H:%M UTC')}_"
    return msg


# ── /scan ──────────────────────────────────────────────────────────────────────

def cmd_scan() -> str:
    research = _load_research()
    if not research:
        return "No research data yet — waiting for next hourly scan."

    latest = research.get("latest", research)
    recs   = latest.get("recommendations", [])
    ts     = latest.get("timestamp", "")

    if not recs:
        return "Latest research found no opportunities."

    # Show top 5 by confidence, skipping markets already held
    top = [r for r in recs if not r.get("existing_position")][:5]
    if not top:
        top = recs[:5]

    try:
        scan_time = datetime.fromisoformat(ts).strftime("%H:%M UTC") if ts else "unknown"
    except Exception:
        scan_time = ts[:16] if ts else "unknown"

    msg = f"🔍 *TOP OPPORTUNITIES* (scan at {scan_time})\n\n"
    for i, r in enumerate(top, 1):
        direction = r.get("recommendation", "?")
        conf      = r.get("confidence", 0)
        prob      = r.get("probability", 0)
        question  = r.get("question", r.get("market_id", "?"))[:55]
        strats    = ", ".join(r.get("strategies", []))
        ev        = r.get("estimated_ev")

        ai_rec  = r.get("ai_recommendation")
        ai_conf = r.get("ai_confidence", 0)
        ai_note = ""
        if ai_rec and ai_rec != "SKIP" and r.get("ai_was_candidate"):
            ai_note = f" | AI: {ai_rec} {ai_conf*100:.0f}%"
        elif r.get("ai_returned_skip"):
            ai_note = " | AI: SKIP"

        ev_note = f" | EV ${ev:+.2f}" if ev is not None else ""
        direction_symbol = "🟢" if direction == "YES" else "🔴"

        msg += f"{i}. {direction_symbol} *{direction}* {conf*100:.0f}% — {question}...\n"
        msg += f"   Mkt: {prob*100:.0f}%{ai_note}{ev_note}\n"
        msg += f"   _{strats}_\n\n"

    schema = latest.get("schema_version", 1)
    if schema < 2:
        msg += "⚠️ _AI analysis unavailable for this scan (schema v1)_\n"

    return msg.rstrip()


# ── /autotrader on | off ───────────────────────────────────────────────────────

def cmd_autotrader_off() -> str:
    with open(_FLAG_FILE, "w") as f:
        f.write(f"Disabled at {datetime.now(timezone.utc).isoformat()}\n")
    return "🚫 *Auto-trader DISABLED*\nNo new trades will be placed until re-enabled.\nUse `/autotrader on` to resume."


def cmd_autotrader_on() -> str:
    if os.path.exists(_FLAG_FILE):
        os.remove(_FLAG_FILE)
        return "✅ *Auto-trader ENABLED*\nTrading will resume on the next hourly cycle."
    return "✅ *Auto-trader is already ENABLED* (flag file was not present)."


# ── CLI entry point ────────────────────────────────────────────────────────────

_COMMANDS = {
    "portfolio":      cmd_portfolio,
    "positions":      cmd_positions,
    "scan":           cmd_scan,
    "autotrader-on":  cmd_autotrader_on,
    "autotrader-off": cmd_autotrader_off,
}

def main():
    if len(sys.argv) < 2 or sys.argv[1] not in _COMMANDS:
        cmds = " | ".join(_COMMANDS)
        print(f"Usage: python3 automation/telegram_bot.py [{cmds}]")
        return 1

    cmd = sys.argv[1]
    response = _COMMANDS[cmd]()

    # Send directly via Bot API — the skill runner also captures stdout and
    # would forward it, causing a duplicate. Suppress stdout here.
    send_message(response)
    return 0


if __name__ == "__main__":
    sys.exit(main())
