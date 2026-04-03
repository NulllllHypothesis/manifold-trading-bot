# Technical Overview — Manifold Trading Bot

*Written for someone who has never seen this codebase. No assumed knowledge.*

> **Last updated: 2026-04-03** — AI integration complete (see Part 3 and Part 4 for changes).

---

## Part 1 — What Is This Thing, In Plain English

### What is Manifold Markets?

Manifold is a prediction market platform. The idea is simple: people ask questions about the future ("Will X happen by Y date?"), and other people bet YES or NO using the platform's fake currency called **mana**.

The market has a live probability — a number between 0% and 100% — that represents what the crowd thinks the chance of YES is. If you think the crowd is wrong, you bet against them. If a market resolves YES and you bet YES, you win. If it resolves NO and you bet NO, you win.

Example:
> "Will GPT-5 be released before June 2026?" — currently at 72%

If you think that's too high (i.e., you think GPT-5 won't ship that fast), you bet NO. If you're right when it resolves, you get paid out.

This is **paper trading** — we're not using real money or real mana. We simulate the bets in a local file. The bot reads real live markets and makes real decisions, but the "money" only exists in a JSON file on the server.

---

### What Does This Bot Do?

Every hour, automatically, without anyone touching anything:

1. **Scans ~100 live Manifold markets** — reads current probabilities, volumes, liquidity
2. **Scores each market** using 3 simple rules — picks which ones look like good bets
3. **Saves the findings** to a file called `market_research.json`
4. **15 minutes later**, reads that file and **places paper bets** on the best opportunities
5. **Once a day at 7pm UTC**, sends a **performance summary** to a Telegram group

The bot runs on a server at a friend's house (Aleksi's homelab). An AI agent framework called **OpenClaw** manages the scheduling, runs the scripts, and forwards results to Telegram.

---

## Part 2 — The Full Cycle, Step by Step

```
Every hour at :00 UTC
        │
        ▼
┌─────────────────────────────────┐
│  auto_research.py runs          │
│  - Calls Manifold API           │
│  - Gets 100 markets             │
│  - Filters: resolved, low liq   │
│  - Scores with 3 strategies     │
│  - Sorts by confidence          │
│  - AI analysis on top 5 ──────► deepseek-r1:14b (local, free)
│    (YES/NO/SKIP + reasoning)    │  ~60s per market on CPU
│  - Blends stat + AI confidence  │
│  - Saves to market_research.json│
└─────────────────────────────────┘
        │
        │  (15 minutes pass)
        ▼
┌─────────────────────────────────┐
│  auto_trader.py runs            │
│  - Reads market_research.json   │
│  - Filters by risk rules:       │
│    • confidence ≥ 65%           │
│    • max 5 open positions       │
│    • liquidity ≥ $200           │
│    • 6h cooldown per market     │
│  - Picks top 1-2 trades         │
│  - Calls PaperTrader to         │
│    simulate the bet             │
│  - Updates paper_trading_state  │
└─────────────────────────────────┘
        │
        │  (daily at 7pm UTC)
        ▼
┌─────────────────────────────────┐
│  daily_summary.py runs          │
│  - Reads state + trade history  │
│  - Calculates P&L, win rate     │
│  - Formats a Telegram message   │
│  - OpenClaw delivers it to      │
│    the group chat               │
└─────────────────────────────────┘
```

---

## Part 3 — Every File Explained

### `manifold_bot/` — The Core Package

This is the brain. The automation scripts call into here.

---

#### `manifold_bot/config.py`
**What it does:** Holds all global constants and settings. Nothing complicated.

```python
MANIFOLD_API_KEY = os.environ.get("MANIFOLD_API_KEY", "")
MANIFOLD_API_BASE = "https://api.manifold.markets"
INITIAL_BALANCE = 1000
MIN_BET_AMOUNT = 1
MAX_BET_AMOUNT = 100
```

The API key is read from an environment variable (not hardcoded). `INITIAL_BALANCE = 1000` is the fake starting money. The min/max bet amounts cap how much we can put on any single trade.

---

#### `manifold_bot/manifold_api.py`
**What it does:** All the HTTP calls to Manifold's API. Everything that touches the internet lives here.

Key methods:
- `get_markets(limit=100)` — fetches a list of live markets
- `get_market(market_id)` — fetches one specific market by ID
- `search_markets(term)` — searches by keyword
- `place_bet(amount, contract_id, outcome)` — **real bet placement** (not used in paper trading)
- `get_user_bets()` — fetch bet history

There's a singleton at the bottom — `api_client = ManifoldAPI()` — that the rest of the code imports. So everywhere you see `api_client.get_market(...)`, it's using this file.

---

#### `manifold_bot/strategies.py`
**What it does:** The 3 trading strategies and a scoring function. This is where the bot decides whether a market is worth betting on and which way.

**Strategy 1 — Probability Direction**
```python
if probability > 0.55:  return "YES"
if probability < 0.45:  return "NO"
return None
```
Simple rule: if the market leans strongly one way, bet with the crowd. Logic: crowds tend to be right, so ride the momentum. Doesn't fire near 50/50 (the threshold is 0.05 away from 50%).

**Strategy 2 — Mean Reversion**
```python
if probability > 0.8:  return "NO"   # too high, bet it comes down
if probability < 0.2:  return "YES"  # too low, bet it comes up
return None
```
Opposite logic: if something is at 85% probability, it's probably overpriced. Bet against extreme consensus. Classic contrarian strategy.

**Strategy 3 — Volume Spike**
```python
if current_volume > avg_volume * 2:
    return "YES" if probability > 0.5 else "NO"
```
If a market suddenly has a lot of activity (2x the average of all fetched markets), something is happening — bet in the direction it's already leaning. `avg_volume` is now calculated from the actual batch of markets fetched each run (was hardcoded to 100 before, which caused it to fire on nearly everything).

**The voting system** — `analyze_market_for_trading()` runs all 3 strategies on a market, counts the YES votes vs NO votes, and sets `confidence = votes_for_winner / total_votes`. So if 2 strategies say NO and 1 says YES, confidence is 0.67 (67%).

**The honest problem with this:** The confidence scores cluster around 0.65 because almost everything that passes the volume spike threshold gets a 65% score (only 1 of 1 strategies fired, so 1/1 = 100%... wait, actually the issue is different — see Part 4 for the real breakdown).

---

#### `manifold_bot/ai_analyzer.py` ← new
**What it does:** Sends a market question to a local AI model and asks whether it's mispriced.

Two backends, tried in order:
1. **Ollama** at `localhost:11434` — runs `deepseek-r1:14b` locally on the server. Free, no API cost, ~60 seconds per market on CPU.
2. **DeepSeek API** — fallback only if Ollama is unreachable. Uses `DEEPSEEK_API_KEY` already on the server.

What it sends to the AI (simplified):
```
Question: "Will X happen by 2027?"
Current probability: 72%
Volume: $5,200
Liquidity: $1,400
Close date: 2027-01-01

Is this mispriced? Should we bet YES, NO, or skip?
```

What it gets back:
```json
{
  "recommendation": "NO",
  "confidence": 0.78,
  "estimated_true_probability": 0.45,
  "reasoning": "72% seems high — similar events have resolved NO 60% of the time.",
  "risk_factors": "Could change if new legislation passes."
}
```

**How confidence blending works in auto_research.py:**
- AI agrees with statistical signal → `confidence = 0.4 × stat + 0.6 × AI`
- AI disagrees → `confidence = stat × 0.4` (penalised, very unlikely to reach 65% threshold)
- AI says SKIP → statistical confidence unchanged, no AI boost

---

#### `manifold_bot/paper_trader.py`
**What it does:** Simulates placing and tracking bets. Reads/writes `manifold_bot/paper_trading_state.json`.

**Current state of the portfolio:**
- Balance: $655
- 4 open positions (all bet NO at 50% probability)
- Max positions: 5 (1 slot free)

Key methods:
- `place_paper_bet(market_id, outcome, amount, probability)` — deducts from balance, saves to state
- `resolve_market(market_id, outcome)` — settles a bet when a market closes, adds/removes from balance
- `auto_resolve_markets()` — checks Manifold API for any markets that have resolved, settles them automatically
- `get_performance_metrics()` — calculates win rate, P&L, profit factor, etc.

**How payout works (important):**
```python
# If you bet YES at 30% probability with $10:
payout = $10 / 0.30 = $33.33
profit_if_win = $33.33 - $10 = $23.33
profit_if_lose = -$10
```
Lower probability = higher payout if you're right. This is how prediction markets work — they're basically odds-based.

**State file:** `manifold_bot/paper_trading_state.json` is the single source of truth. Every bet is saved here immediately after placement.

---

### `automation/` — The Cron Scripts

These three files are what actually run on the server every hour. They're standalone scripts that import from `manifold_bot/`.

---

#### `automation/auto_research.py`
**What it does:** The hourly market scanner.

Flow:
1. Checks if we're already at max positions — if yes, skips entirely (no point researching if we can't trade)
2. Calls `api_client.get_markets(limit=100)` — fetches 100 live markets from Manifold
3. Filters out: resolved markets, markets with liquidity under $100
4. Runs all 3 strategies on each remaining market
5. Calculates overall recommendation + confidence score per market
6. Sorts by confidence (highest first)
7. Saves to `market_research.json` — keeps the latest result plus a 24-hour history

Output file structure (`market_research.json`):
```json
{
  "latest": {
    "timestamp": "2026-04-03T06:00:00",
    "recommendations": [
      {
        "market_id": "abc123",
        "question": "Will X happen?",
        "probability": 0.72,
        "recommendation": "NO",
        "confidence": 0.65,
        "strategies": ["volume_spike"]
      }
    ]
  },
  "history": [ ...last 23 runs... ]
}
```

---

#### `automation/auto_trader.py`
**What it does:** Reads the research file and decides whether to actually place trades.

**Risk management rules (the filter gauntlet):**

| Rule | Value | Meaning |
|------|-------|---------|
| Min confidence | 65% | Only trade if the strategy signal is strong enough |
| Max positions | 5 | Never hold more than 5 open bets at once |
| Max position size | 10% of balance | Don't bet more than 10% of total cash on one trade |
| Min liquidity | $200 | Don't trade illiquid markets (hard to exit) |
| Cooldown | 6 hours | Don't double-up on same market within 6 hours |
| Max trades per run | 2 | Only place 2 trades max per hourly cycle |

**Position sizing formula:**
```python
base_size = balance * 0.10          # 10% of balance
multiplier = confidence / 0.65      # scales with confidence
position_size = base_size * multiplier
# then capped at $100 max, $1 min
```

So with $684 balance and 65% confidence: `$68.4 * 1.0 = $68.4`, rounded to nearest $5 = `$70`.

**Why trades aren't executing right now:** 4 positions are open, max is 5, so 1 slot is available. But all recommendations are coming in at exactly 60-62% confidence, which is below the 65% threshold. Nothing passes the filter.

---

#### `automation/daily_summary.py`
**What it does:** Generates and sends a daily performance report at 7pm UTC.

Reads from:
- `manifold_bot/paper_trading_state.json` — balance, positions, trade history
- `market_research.json` — how many research sessions ran
- `auto_trades.json` — how many trades executed

Calculates: today's trades, today's P&L, win rate, open positions, total invested.

Formats a Telegram-ready message. The message gets saved to `telegram_daily_summary.txt` and OpenClaw delivers it to the group.

---

### Supporting Files

#### `manifold_bot/paper_trading_state.json`
The live portfolio. Every bet is here. Current state: 4 open positions, all betting NO at 50% probability on different markets, $655 balance remaining from $1000 start.

#### `market_research.json`
~426 KB file. Contains the last 24 hours of hourly research runs. Each run has ~50-100 market recommendations. This is the bridge between research and trading.

#### `auto_trades.json`
Log of every trade the bot has actually executed. 8 trades total so far.

---

## Part 4 — Algorithm Problems: What Was Wrong, What's Fixed

### ✅ Fixed — Volume Spike Hardcoded Threshold

`avg_volume` was hardcoded to `100`, which caused the volume spike strategy to fire on almost every market with any activity. It now uses the real average volume of the 100 markets fetched each run, so only genuinely unusual volume triggers it.

### ✅ Fixed — No Real Signal (AI integration added)

The old strategies only looked at the current probability number — they had no idea what the market question actually said. `ai_analyzer.py` now reads the question and reasons about whether the probability makes sense. The top 5 candidates each run get AI-scored, and confidence is blended:
- AI agrees → boosted confidence (harder to game with weak stats)
- AI disagrees → penalised to 40% (very unlikely to pass the 65% trading threshold)

### Still present — Mean Reversion and Probability Direction Conflict

These two strategies fundamentally disagree at the same probability levels. If a market is at 75%:
- Probability Direction says: "Bet YES — momentum is up"
- Mean Reversion says: "Bet NO — it's too high, it'll come down"

When both fire they cancel out (tie = skip). The AI layer above them partially compensates for this — if the stats cancel out but AI has conviction, AI can still push the score.

### Still present — All Current Bets Are NO at 50%

Every open position is a NO bet at exactly 50% probability — payout of 2x, barely better than a coin flip. These were placed before the AI integration. Future trades will require AI agreement, which should avoid this.

---

## Part 5 — What's Next

**Done:**
- ✅ AI integration (DeepSeek local model)
- ✅ Volume spike avg fixed

**Still todo, in priority order:**

**1. Avoid 50% markets** — Add a pre-filter: skip any market where probability is between 45-55%. No signal there.

**2. Real Telegram bot commands** — `/portfolio`, `/scan`, `/positions` — currently a placeholder that just prints to console.

**3. Web dashboard** — FastAPI backend + Chart.js frontend on port 5000, SSH tunnel to view locally.

**4. SQLite storage** — Replace the flat JSON state files with a proper database.

---

## Part 6 — Infrastructure Summary

The bot doesn't run on your laptop. It runs on a server (Aleksi's homelab) inside a Docker container. Here's how it's wired:

```
Docker Container (Linux)
    └── OpenClaw (Node.js AI agent gateway)
            ├── Cron scheduler (built-in, not OS cron)
            │       ├── :00 UTC → runs auto_research.py
            │       ├── :15 UTC → runs auto_trader.py
            │       └── 19:00 UTC → runs daily_summary.py
            ├── Telegram bot (@hackathon_26_bot)
            │       └── sends results to group -5240775171
            └── Git workspace (~/.openclaw/workspace/)
                    └── This repo (checked out, pulled every run)
```

The scripts run inside the container's shell. OpenClaw executes them as shell commands (via its `coding` tools profile), reads stdout, and forwards summaries to Telegram.

**Environment variables** are loaded from `/etc/profile.d/hackathon.sh` before each cron command runs (we fixed this today). The API key is `MANIFOLD_API_KEY`.

---

*Last updated: 2026-04-03*
