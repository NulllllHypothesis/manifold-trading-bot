# Technical Overview — Manifold Trading Bot

*Written for someone who has never seen this codebase. No assumed knowledge.*

*Last updated: 2026-04-07 (signal-families + code-quality)* — Signal family architecture: strategies organised into momentum/contrarian/fundamental/filter families with deduplication; `volume_spike_priority` returns float priority boost (not a directional signal); `probability_bias` category-conditional; duplicate-bet guard fixed; 173 tests total.

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
2. **Scores each market** using 6 strategies — picks which ones look like good bets
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
│  - Gets 100+ markets            │
│  - Pre-filter: is_stale_market  │
│  - Scores with 6 strategies     │
│  - Family deduplication         │
│  - Top 3 by composite score     │
│    (confidence + priority_boost │
│     × 0.15)                     │
│  - AI analysis on top 3 ──────► deepseek-r1:14b (local, free)
│    (YES/NO/SKIP + reasoning)    │  ~70-80s per market on CPU
│    calibration table in prompt  │  crowd bias corrections injected
│  - Blends stat + AI confidence  │
│  - Saves market_research.json   │
│    schema_version=2             │
└─────────────────────────────────┘
        │
        │  (:10 every hour)
        ▼
┌─────────────────────────────────┐
│  resolve_positions.py runs      │
│  - Polls Manifold API for each  │
│    open position                │
│  - Calls resolve_market() on    │
│    any that settled             │
│  - Writes bet_outcomes to SQLite│
│    (estimated_ev vs actual_pnl) │
│  - Frees slots for new trades   │
└─────────────────────────────────┘
        │
        │  (:20 every hour)
        ▼
┌─────────────────────────────────┐
│  auto_trader.py runs            │
│  - Reads market_research.json   │
│  - Refuses schema_version < 2   │
│  - Filters by risk rules:       │
│    • confidence ≥ 65%           │
│    • max 10 open positions      │
│    • liquidity ≥ $200           │
│    • no re-entry on OPEN pos    │
│    • AI veto (SKIP) respected   │
│  - Kelly sizing (half-Kelly)    │
│  - Picks top 1-2 trades         │
│  - Stores estimated_ev at trade │
│  - Stores entry_probability +   │
│    entry_confidence per trade   │
│  - Updates paper_trading_state  │
└─────────────────────────────────┘
        │
        │  (:40 every hour)
        ▼
┌─────────────────────────────────┐
│  position_swap_checker.py runs  │
│  - Skips if slots available     │
│  - Skips if pending proposals   │
│    already exist (not expired)  │
│  - Fetches current prob for     │
│    every open position          │
│  - Computes unrealised P&L:     │
│    YES: amount*cur/entry - amt  │
│    NO:  amount*(1-cur)/(1-entry)│
│  - Finds ALL losing positions   │
│    (unrealised_pnl < 0)         │
│  - For each loser, finds best   │
│    unique replacement:          │
│    • confidence ≥ 65%           │
│    • AI not SKIP                │
│    • estimated_ev > 0           │
│    • new_ev > abs(loss)         │
│  - Writes pending_swaps.json    │
│    (numbered list)              │
│  - Sends Telegram per proposal: │
│    "approve swap N" / "dismiss" │
└─────────────────────────────────┘
        │
        │  Human replies in Telegram
        ▼
┌─────────────────────────────────┐
│  execute_swap.py --id N runs    │
│  - Validates proposal not       │
│    expired, status=pending      │
│  - close_position_early():      │
│    simulates selling at current │
│    probability, realises P&L    │
│  - place_paper_bet() for new    │
│    opportunity                  │
│  - Marks proposal approved      │
│  - Sends confirmation to TG     │
└─────────────────────────────────┘
        │
        │  (daily at 19:00 UTC)
        ▼
┌─────────────────────────────────┐
│  daily_summary.py runs          │
│  - Reads state + trade history  │
│  - Calculates P&L, win rate     │
│  - Mondays: appends weekly EV   │
│    accuracy section             │
│  - Formats a Telegram message   │
│  - OpenClaw delivers to group   │
└─────────────────────────────────┘
        │
        │  (Sundays 02:00 UTC)
        ▼
┌─────────────────────────────────┐
│  harvest_resolved.py runs       │
│  - Fetches 2000+ resolved       │
│    markets from Manifold API    │
│  - Saves to data/calibration.db │
│  - Runs analyze_calibration.py  │
│  - Rebuilds calibration_table   │
│    .json for next AI cycle      │
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
**What it does:** The 6 trading strategies and Kelly sizing. This is where the bot decides whether a market is worth betting on and which way.

**Strategy 1 — Probability Direction** (confidence 0.65)
Bet with momentum if the market leans strongly and has had recent activity (last bet < 48h). Skips the 45–55% coinflip zone.

**Strategy 2 — Mean Reversion** (confidence 0.70)
Bet against extreme probabilities (>80% or <20%), but only when fewer than 100 unique bettors have weighed in. Don't fight genuine deep consensus.

**Strategy 3 — Volume Spike Priority** (priority filter — not a directional signal)
If a market's 24h volume is >2x the batch median, returns a `priority_boost` float (0.0–1.0). This value is added to the composite candidate score (`confidence + priority_boost × 0.15`) to rank which markets go to the AI first. It does NOT vote YES or NO and does NOT count as a signal family member.

**Strategy 4 — Creator Disagreement** (confidence 0.75)
If the market creator's resolution probability estimate differs from the crowd by ≥15pp, bet toward the creator. They often have inside information.

**Strategy 5 — Thin Market** (confidence 0.65)
When one side of the AMM liquidity pool holds <15% of the total, bet toward that underrepresented side — it's underpriced.

**Strategy 6 — Probability Bias** (confidence 0.70)
Exploit systematic crowd overconfidence measured from 1,100+ resolved markets. The 60–70% bucket resolves YES only 47% of the time (+18pp bias) — bet NO. The 30–40% bucket: +11pp bias — also bet NO.
Category-conditional: skips `ai_tech` (only 0.19pp bias) and `economics` (1.51pp bias) — neither clears the 4pp minimum threshold to act on. Fires on `politics` (+6pp), `crypto` (+5pp), `sports`, `science`, `other`.

**Signal families and deduplication** — each strategy belongs to a family:

| Family | Strategies |
|--------|-----------|
| `momentum` | probability_direction |
| `contrarian` | mean_reversion, probability_bias |
| `fundamental` | creator_disagreement, thin_market |
| `filter` | volume_spike_priority |

`auto_research.py` runs all strategies on each market, then deduplicates: **at most 1 signal per directional family** (highest confidence wins within a family). This prevents correlated strategies from stacking as independent evidence and inflating confidence. `thin_market` (filter) is confirmation-only — only included if a `contrarian` or `fundamental` signal agrees. The `filter` family provides `priority_boost` only.

Composite score: `confidence + priority_boost × 0.15`. Top 3 by composite score are sent to AI. Markets that pass the 65% threshold after AI blending are candidates for trading.

---

#### `manifold_bot/ai_analyzer.py`
**What it does:** Sends a market question to a local AI model and asks whether it's mispriced. Also logs every LLM call to `logs/llm_calls.jsonl` for future distillation/training.

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

Crowd calibration (measured over 1,000+ resolved Manifold markets):
  Bucket      Crowd says  Actual YES rate  Bias
    0– 10%     5%          1%            +4pp
   ...
   60– 70%    65%         47%            +18pp ← MOST BIASED
   ...
Use these corrections: if a market is at 65%, history says treat it
as ~47% YES. Adjust your estimated_true_probability accordingly.

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

**How confidence blending works in `auto_research.py`:**
- AI agrees AND `stat_conf < 0.65` → blended result is capped at 0.64 (below trading threshold — AI cannot rescue a weak stat signal)
- AI agrees AND `stat_conf ≥ 0.65` → `confidence = 0.4 × stat + 0.6 × AI`, capped at `stat_conf + 0.10` (AI can boost by at most 10 percentage points above the baseline)
- AI disagrees → `confidence = stat × (0.4 + (1 - ai_conf) × 0.4)` — penalty scales with AI confidence; a very certain AI disagreement keeps only 40% of the stat score
- AI says SKIP → statistical confidence unchanged, no AI boost

`MIN_CONFIDENCE` lives in `config.py` and is imported by both `auto_trader.py` and `auto_research.py` — one place to change. **Current value is 0.65.**

**Distillation logging** (merged in PR #5):
Every LLM call is appended to `logs/llm_calls.jsonl` (gitignored). Each record stores:
- `timestamp`, `model`, `market_id`
- `system_prompt_sha256` — hash of the static prompt so you can verify it hasn't changed between runs without bloating every record
- `parsed_output` — the structured fields (recommendation, confidence, etc.)

Raw responses, full prompts, and chain-of-thought are intentionally NOT logged — the model may echo market data back, raising compliance concerns. The log rotates at 100MB, keeping 3 backups.

**Robustness guards:**
- `batch_analyze` is called with `delay=2` and `per_market_timeout=90` — prevents a hung Ollama call from stalling the hourly cron job. Each market runs in a `ThreadPoolExecutor(max_workers=1)` with a timeout.
- If the Manifold API returns markets that don't match by `id` field, the AI pass aborts with a hard error and `schema_version=1` is written, causing `auto_trader.py` to block trading rather than proceed without AI validation.
- `schema_version` is written dynamically (2 if AI fields present, 1 if AI pass was skipped or aborted).

---

#### `manifold_bot/paper_trader.py`
**What it does:** Simulates placing and tracking bets. Reads/writes `manifold_bot/paper_trading_state.json`.

Key methods:
- `place_paper_bet(market_id, outcome, amount, probability, estimated_ev, ai_confidence, strategies)` — deducts from balance, saves to state. Stores `entry_probability` and `entry_confidence` alongside each trade record for later drift tracking.
- `resolve_market(market_id, outcome)` — settles a bet, updates balance, and **writes a row to the `bet_outcomes` SQLite table** (`data/calibration.db`) with `estimated_ev`, `ai_confidence`, `actual_pnl`, and `ev_error = estimated_ev − actual_pnl`
- `close_position_early(market_id, current_prob)` — simulates selling a position before resolution at the current market probability. Used by `execute_swap.py`. P&L formula: `YES: amount * current_prob / entry_prob − amount`, `NO: amount * (1−current_prob) / (1−entry_prob) − amount`. Writes a `CLOSED_EARLY` row to `bet_outcomes`.
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
**What it does:** The hourly market scanner. Always runs — even when all positions are full, so `position_swap_checker.py` has fresh data.

Flow:
1. Calls `api_client.get_markets(limit=100)` — fetches 100 live markets from Manifold
2. Filters out: resolved markets, stale markets (last bet > 72h AND volume24h < 10), liquidity under $100
3. Runs all 6 strategies on each remaining market
4. Family deduplication: keeps strongest signal per directional family (momentum/contrarian/fundamental); applies `thin_market` confirmation filter
5. Computes `priority_boost` via `volume_spike_priority`, adds composite score (`confidence + priority_boost × 0.15`)
6. Sorts by composite score (highest first); sends top 3 to AI (`_AI_CANDIDATE_COUNT = 3`)
7. Blends stat + AI confidence; computes `estimated_ev` per recommendation (reference $25 bet)
8. Saves to `market_research.json` — keeps the latest result plus a 24-hour history

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
        "strategies": ["contrarian:mean_reversion"],
        "priority_boost": 0.42,
        "ai_was_candidate": true,
        "estimated_ev": 4.20
      }
    ]
  },
  "history": [ ...last 23 runs... ]
}
```

---

#### `automation/auto_trader.py`
**What it does:** Reads the research file and decides whether to actually place trades.

**Schema guard** — refuses to trade on pre-AI research. Supports both file layouts written by `auto_research.py`:
```python
# Nested layout (normal): { "latest": { "schema_version": 2, ... }, "history": [...] }
# Flat layout (edge case): { "schema_version": 2, ... }
```
If `schema_version < 2` (i.e. AI pass was skipped or aborted), prints a Telegram-visible halt message and returns None.

**Risk management rules (the filter gauntlet):**

| Rule | Value | Meaning |
|------|-------|---------|
| Min confidence | 65% | Only trade if the strategy signal is strong enough |
| Max positions | 10 | Never hold more than 10 open bets at once |
| Max position size | 10% of balance | Don't bet more than 10% of total cash on one trade |
| Min liquidity | $200 | Don't trade illiquid markets (hard to exit) |
| No re-entry | — | Blocks any new bet on a market with an existing OPEN position (live check, not research flag) |
| Max trades per run | 2 | Only place 2 trades max per hourly cycle |

**Position sizing formula:**
```python
base_size = balance * 0.10          # 10% of balance
multiplier = confidence / 0.65      # scales with confidence
position_size = base_size * multiplier
# then capped at $100 max, $1 min
```

So with $684 balance and 65% confidence: `$68.4 * 1.0 = $68.4`, rounded to nearest $5 = `$70`.

**Why trades aren't executing:** If all 10 position slots are full, `should_trade_market()` rejects everything at the max positions check. Use the smart-swap mechanism to replace underperforming positions.

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

---

#### `scripts/position_swap_checker.py`
**What it does:** Runs at `:40` every hour. Proposes swapping a losing position for a better opportunity when all slots are full.

Logic:
1. If `open_positions < MAX_POSITIONS` → exits immediately (slots free, normal trading will handle it)
2. If `pending_swaps.json` has any unexpired pending entries → exits (don't spam while awaiting human reply)
3. Fetches current probability for every open position from the Manifold API
4. Computes `unrealised_pnl` for each — measures how far the market has moved against us
5. Collects all losing positions (pnl < 0), sorts worst-first
6. For each loser, finds the best unique replacement from `market_research.json`:
   - confidence ≥ 65%, AI not SKIP, positive EV, not already held or already proposed
7. Only pairs them if `new_opportunity_ev > abs(unrealised_loss)` (worth crystallising the loss)
8. Writes all qualifying pairs to `pending_swaps.json` as a numbered list
9. Sends one Telegram message per proposal

**Telegram message format:**
```
♻️ Swap Proposal #1
Close: NO on "Will Biden give a speech in 2026?"
  Entered 35%, now 58%, unrealised $-14.30

Open: YES on "Will OpenAI release GPT-5 before June 2026?"
  AI confidence 78%, EV +$22.50

Reply "approve swap 1" to execute.
Reply "dismiss swap 1" to skip.
(Expires in 4h)
```

---

#### `scripts/execute_swap.py`
**What it does:** Executes or dismisses a specific pending swap by ID. Called by OpenClaw when the human replies in Telegram.

```bash
python3 scripts/execute_swap.py --id 1           # approve
python3 scripts/execute_swap.py --id 2 --dismiss # dismiss
```

On approve:
1. Validates proposal is `pending` and not expired (> 4h old)
2. Fetches fresh current probability for both markets
3. Calls `close_position_early()` on the losing position
4. Calls `place_paper_bet()` for the new opportunity (confidence-scaled sizing)
5. Marks proposal `approved` in `pending_swaps.json`
6. Sends confirmation to Telegram

On dismiss: marks `dismissed`, sends a brief Telegram acknowledgement.

---

### Supporting Files

#### `manifold_bot/paper_trading_state.json`
The live portfolio. Gitignored (runtime data). Supports up to 10 open positions. Balance and positions are the current live state on the server.

#### `market_research.json`
Contains the last 24 hours of hourly research runs (schema_version 2). Each run has ~50-100 market recommendations with AI scores on the top 3 (`ai_was_candidate=True`). Bridge between research and trading.

#### `auto_trades.json`
Log of every trade the bot has executed. Every record includes `estimated_ev` (the AI's EV prediction at trade time), `ai_confidence`, and `strategies`. Used for the weekly EV accuracy report.

#### `data/calibration.db` (gitignored)
SQLite database with two tables:
- `resolved_markets` — 1,121+ historical Manifold markets with `probability_close`, `outcome`, `category`
- `bet_outcomes` — one row per resolved position: `estimated_ev`, `actual_pnl`, `ev_error`, `strategies`

#### `data/calibration_table.json` (committed)
Crowd bias correction table generated weekly by `analyze_calibration.py`. Loaded by `ai_analyzer.py` at import time and injected into every AI prompt. Key finding: **+18pp overconfidence in the 60-70% probability bucket**.

#### `generate_report.py` / `generate_pdf.py`
One-off scripts (hardcoded to server paths) that generate a human-readable PDF trading report using `reportlab`. Not part of the automated pipeline — run manually on the server for ad-hoc reporting.

---

## Part 4 — Algorithm Problems: What Was Wrong, What's Fixed

### ✅ Fixed — Volume Spike Hardcoded Threshold

`avg_volume` was hardcoded to `100`, which caused the volume spike strategy to fire on almost every market with any activity. It now uses the real average volume of the 100 markets fetched each run, so only genuinely unusual volume triggers it.

### ✅ Fixed — No Real Signal (AI integration added)

The old strategies only looked at the current probability number — they had no idea what the market question actually said. `ai_analyzer.py` now reads the question and reasons about whether the probability makes sense. The top 5 candidates each run get AI-scored, and confidence is blended:
- AI agrees → boosted confidence (harder to game with weak stats)
- AI disagrees → penalised to 40% (very unlikely to pass the 65% trading threshold)

### ✅ Fixed — Mean Reversion and Probability Direction Conflict

These two strategies disagree at the same probability levels:
- Probability Direction (`momentum` family) says: "Bet YES — momentum is up"
- Mean Reversion (`contrarian` family) says: "Bet NO — it's too high"

This is now handled correctly by family deduplication. They belong to **different families**, so both can contribute their signals — but they do not cancel each other out or average together. The family with the stronger signal at 65%+ threshold wins. If both families fire in opposite directions, the AI blending layer above has the final say.

### Current state — Bot at Max Capacity

The bot runs up to 10 open positions. All new trades require AI agreement (schema_version=2). Re-entry on a market with an existing OPEN position is blocked by a live position check in `should_trade_market()` — this guard reads the live portfolio state, not the potentially-stale `existing_position` flag in `market_research.json` (the 2czul2Rync duplicate-bet bug is fixed and regression-tested). Use the smart-swap mechanism to replace underperforming positions when all slots are full.

---

## Part 5 — What's Next

**Done:**
- ✅ AI integration (local Ollama `deepseek-r1:14b`, DeepSeek API fallback)
- ✅ Volume spike avg fixed (median of fetched markets)
- ✅ Confidence blending with floor/cap system
- ✅ Schema versioning + guard (`auto_trader.py` refuses pre-AI research)
- ✅ Auto-fixing review agent (commits fixes directly to PR branch)
- ✅ `per_market_timeout` enforcement via `concurrent.futures` (prevents hung Ollama stalling cron)
- ✅ Distillation logging — every LLM call appended to `logs/llm_calls.jsonl` with CoT extraction and log rotation (PR #5, merged 2026-04-06)
- ✅ 5 strategies with Kelly sizing, AI veto, recency boost, stale market pre-filter (PR #6)
- ✅ Position resolution loop — `scripts/resolve_positions.py` + cron at :10 frees frozen slots
- ✅ EV stored at trade time — `estimated_ev` in every `auto_trades.json` record and position record
- ✅ **Calibration & feedback loop** (feature/calibration, 2026-04-07):
  - `scripts/harvest_resolved.py` → 1,121 markets in `data/calibration.db`
  - `scripts/analyze_calibration.py` → `data/calibration_table.json` with bucket + category breakdown
  - `ai_analyzer.py` — calibration table injected into every AI prompt
  - `paper_trader.py` — `bet_outcomes` table written on every market resolution
  - `scripts/weekly_ev_report.py` — EV accuracy ratio by strategy + confidence band
  - 2 new weekly crons on server (harvest Sundays, EV report Mondays)
- ✅ **Smart Position Swap** (feature/smart-swap, 2026-04-07):
  - `paper_trader.py` — `entry_probability`, `entry_confidence` on every trade; `close_position_early()` added
  - `scripts/position_swap_checker.py` — finds all losing positions, pairs each with unique replacement, multi-proposal Telegram flow
  - `scripts/execute_swap.py` — `--id N` approves, `--id N --dismiss` dismisses; Telegram confirmation; zero/negative Kelly pre-validated BEFORE closing position
  - New `:40` cron `position-swap-check` in `setup_cron_jobs.py`
  - 61 tests in `tests/test_swap.py`

- ✅ **Signal family architecture + code quality** (2026-04-07):
  - `strategies.py` — `SIGNAL_FAMILIES` dict; `volume_spike_priority` returns float 0.0–1.0; `probability_bias` category-conditional; `mean_reversion` close-time guard; `probability_direction` volume guard
  - `auto_research.py` — family deduplication, composite ranking, `_AI_CANDIDATE_COUNT = 3`, research always runs
  - `test_manifold.py` — 7 fully mocked unit tests (no network); `--live` flag for manual smoke test
  - `test_strategies.py` — 48 tests including regression for duplicate-bet (2czul2Rync) bug
  - **173 tests total across all suites**

**Next steps, in priority order:**

**1. Real Telegram Bot** — `send_telegram.py` is a placeholder that prints to console. Replace with real `python-telegram-bot` integration so swap proposals and confirmations actually reach Telegram.

**2. Category exposure caps** — add `max_positions_per_category` (suggest: 3/10) to prevent over-concentration in one topic during volatile periods.

**3. Weighted scoring function** — replace flat confidence with `direction_sign × strength × reliability_weight`. Reliability weights start equal and update from `bet_outcomes` table over time.

**4. News fetcher** — structured news API integration as a `fundamental` family signal. Use keywords extracted from the market question.

**5. Whale tracking** — track large-bet accounts via `/v0/bets` endpoint; build bettor-accuracy cache in SQLite.

**6. Web dashboard** — FastAPI backend + Chart.js frontend on port 5000, SSH tunnel to view locally.

**7. SQLite storage** — Replace flat JSON state files with a proper database.

---

## Part 6 — Infrastructure Summary

The bot doesn't run on your laptop. It runs on a server (Aleksi's homelab) inside a Docker container. Here's how it's wired:

```
Docker Container (Linux)
    └── OpenClaw (Node.js AI agent gateway)
            ├── Cron scheduler (built-in, not OS cron)
            │       ├── hourly   :00 UTC  → runs auto_research.py
            │       ├── hourly   :10 UTC  → runs resolve_positions.py
            │       ├── hourly   :20 UTC  → runs auto_trader.py
            │       ├── hourly   :40 UTC  → runs position_swap_checker.py
            │       ├── daily 19:00 UTC   → runs daily_summary.py
            │       ├── Sundays  02:00    → runs harvest_resolved.py + analyze_calibration.py
            │       └── Mondays  07:00    → runs weekly_ev_report.py
            ├── Telegram bot (@hackathon_26_bot)
            │       └── sends results to group -5240775171
            └── Git workspace (~/.openclaw/workspace/)
                    ├── This repo (feature/calibration branch)
                    └── data/calibration.db (SQLite, gitignored)
                        ├── resolved_markets (1,121+ rows)
                        └── bet_outcomes (written on each resolution)
```

The scripts run inside the container's shell. OpenClaw executes them as shell commands (via its `coding` tools profile), reads stdout, and forwards summaries to Telegram.

**Environment variables** are set in `/etc/profile.d/hackathon.sh`. Note: the OpenClaw gateway process is started at container boot by `runuser` and does NOT source this file. Cron sessions inherit the gateway's environment, so `MANIFOLD_API_KEY` is technically unavailable — but this only causes a warning; paper trading doesn't need it (PaperTrader works offline). To fix properly: add the key to `openclaw.json` or prefix cron commands with `source /etc/profile.d/hackathon.sh &&`.

---

*Last updated: 2026-04-07 (smart-swap)*
