#!/usr/bin/env python3
"""
Telegram message sender — real Bot API implementation.

Sends messages to the Hackathon trading group via the Telegram Bot API.
No third-party library required; uses plain HTTP via requests.

Usage (as module):
    from automation.send_telegram import send_message, send_telegram_message
    send_message("Hello group")

Usage (as script — sends last daily summary):
    python3 automation/send_telegram.py
    python3 automation/send_telegram.py --text "Custom message"

Requires TELEGRAM_BOT_TOKEN in .env or environment.
TELEGRAM_GROUP_ID defaults to -5240775171 but can be overridden in .env.
"""

import os
import sys
import time
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from manifold_bot.config import TELEGRAM_BOT_TOKEN, TELEGRAM_GROUP_ID

# Telegram's hard limit per message
_MAX_LEN = 4096


def send_message(text: str, chat_id: str = None, parse_mode: str = "Markdown") -> bool:
    """
    Send a message to a Telegram chat via the Bot API.

    Args:
        text:       Message body. Supports Telegram Markdown v1 formatting.
        chat_id:    Target chat/group ID. Defaults to TELEGRAM_GROUP_ID.
        parse_mode: "Markdown" (default) or "HTML". Pass "" to send plain text.

    Returns:
        True on success, False on failure.
    """
    if chat_id is None:
        chat_id = TELEGRAM_GROUP_ID

    if not TELEGRAM_BOT_TOKEN:
        print("[send_telegram] TELEGRAM_BOT_TOKEN not set — printing instead of sending.")
        print(text)
        return False

    # Truncate gracefully — Telegram rejects messages over 4096 chars
    if len(text) > _MAX_LEN:
        cutoff = _MAX_LEN - 60
        text = text[:cutoff] + "\n\n_(message truncated — too long)_"

    try:
        import requests
    except ImportError:
        print("[send_telegram] requests library not available — cannot send.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": str(chat_id), "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode

    last_error = None
    for attempt in range(3):
        try:
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code == 200:
                return True

            err = resp.json() if resp.content else {}
            # Telegram returns 400 when Markdown is malformed.
            # Retry once as plain text so the message still gets through.
            if resp.status_code == 400 and "parse_mode" in payload:
                print(f"[send_telegram] Markdown parse error — retrying as plain text.")
                payload.pop("parse_mode")
                continue

            last_error = f"HTTP {resp.status_code}: {err.get('description', resp.text[:120])}"
            break

        except requests.exceptions.Timeout:
            last_error = "request timed out"
            if attempt < 2:
                time.sleep(2 ** attempt)
        except Exception as e:
            last_error = str(e)
            if attempt < 2:
                time.sleep(2 ** attempt)

    print(f"[send_telegram] Failed to send message: {last_error}")
    return False


def send_telegram_message(message: str) -> bool:
    """Backward-compatible wrapper used by daily_summary, swap scripts, etc."""
    return send_message(message)


def main():
    parser = argparse.ArgumentParser(description="Send a message to the Telegram trading group")
    parser.add_argument("--text", help="Message text to send directly")
    parser.add_argument("--file", default="telegram_daily_summary.txt",
                        help="File containing the message (default: telegram_daily_summary.txt)")
    args = parser.parse_args()

    if args.text:
        message = args.text
    elif os.path.exists(args.file):
        with open(args.file) as f:
            message = f.read().strip()
        if not message:
            print(f"[send_telegram] {args.file} is empty.")
            return 1
    else:
        print(f"[send_telegram] {args.file} not found. Run daily_summary.py first, or use --text.")
        return 1

    success = send_message(message)
    if success:
        print("[send_telegram] Message sent successfully.")
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
