---
name: positions
description: "List all open paper trading positions — market question, direction (YES/NO), amount invested, entry probability. Use when: user says /positions, asks what markets we're in, what trades are open, show positions, or what's currently bet on."
---

# Positions Command

List all currently open paper trading positions.

## Command

```bash
cd /home/hackathon/.openclaw/workspace && python3 automation/telegram_bot.py positions
```

Run the command, then relay the output to the user. Do not add commentary — the output is already formatted for Telegram.
