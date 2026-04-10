# Technical Overview — Manifold Trading Bot

*Written for someone who has never seen this codebase. No assumed knowledge.*

*Last updated: 2026-04-10 (architecture consistency pass: liquidity alignment, AI cooldown, category inference single-source-of-truth, "other" category uncapping)* — 122 tests in test_calibration.py, 75 in test_strategies.py.

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
│  - Writes snapshot rows ──────► data/market_snapshots.db (M1)
│    (market_id, prob, vol24h,    │  ~100 rows/run; ML training
│     bettors, liquidity, ts)     │  data in 2-3 months
│  - Pre-filter: is_stale_market  │
│  - Scores with 6 strategies     │
│  - Strategy reliability weights │  data/strategy_weights.json
│  - Family deduplication         │
│  - Top 3 by composite score     │
│    (confidence + priority_boost │
│     × 0.15)                     │
│  - AI analysis on top 3 ──────► llama3.2:3b via Ollama (local, free)
│    (YES/NO/SKIP + reasoning)    │  ~35s warm / ~50s cold (CPU)
│    calibration table in prompt  │  crowd bias corrections injected
│  - Blends stat + AI confidence  │
│  - Saves market_research.json   │
│    schema_version=2             │
│  - Calls run_swap_check() ────► position_swap_checker.py
│    (only fires if full+losing)  │  no separate :40 cron needed
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
│    • AI veto (SKIP) first       │
│    • confidence ≥ 65%           │
│    • max 10 open positions      │
│    • liquidity ≥ $200           │
│    • no re-entry on OPEN pos    │
│    • category cap (Phase C):    │  3 default → 1 if accuracy<50%
│      adaptive per own history   │  needs ≥8 resolved trades
│      "other" always uncapped    │  catch-all bucket, positions uncorrelated
│  - Market validation guard:     │  abort if resolved/closed
│    fetches live market first    │
│  - Kelly sizing (Phase B):      │  kelly = 0.5 × strategy_weight
│    weight 0.5→1.2 → 25-60%     │  data/strategy_weights.json
│  - Ranks by estimated EV        │
│  - Picks top 1-2 trades         │
│  - Stores estimated_ev at trade │
│  - Stores entry_probability +   │
│    entry_confidence per trade   │
│  - Updates paper_trading_state  │
└─────────────────────────────────┘
        │
        │  (called by auto_research.py after each run — not a separate cron)
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
│  - Sends directly via Bot API   │
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
        │  (Sundays 02:30 UTC — M2)
        ▼
┌─────────────────────────────────┐
│  audit_resolved_markets.py      │
│  - Inspects all resolved markets│
│    for 3 failure modes:         │
│    1. near-close contamination  │
│       (prob >85% or <15%)       │
│    2. thin markets (<10 bettors)│
│    3. category skew             │
│  - Outputs usable_market_ids    │
│    to data/m2_audit.json        │
│  - Decision gate: YELLOW if     │
│    ≥200 usable (M3 viable)      │
└─────────────────────────────────┘
        │  (Sundays 03:00 UTC — M3)
        ▼
┌─────────────────────────────────┐
│  reconstruct_snapshots.py runs  │
│  - Reads usable IDs from        │
│    data/m2_audit.json           │
│  - For each market, fetches     │
│    full bet history via         │
│    Manifold /v0/bets API        │
│  - Calls prob_at_time() to find │
│    probability at T-7d, T-14d,  │
│    T-30d before market closed   │
│  - Writes source='reconstructed'│
│    rows to market_snapshots.db  │
│  - Skips already-done markets   │
│    (idempotent re-runs)         │
│  - Reports api_error separately │
│    from no_bets (Fix 3)         │
└─────────────────────────────────┘
        │  (Sundays 03:30 UTC — backtest)
        ▼
┌─────────────────────────────────┐
│  backtest_from_snapshots.py     │
│  - Loads reconstructed rows     │
│    (source='reconstructed') +   │
│    joins category from          │
│    resolved_markets             │
│  - Runs mean_reversion and      │
│    probability_bias on each     │
│    snapshot where outcome known │
│    (probability_direction skipped│
│    — volume24h NULL for history)│
│  - Computes accuracy → weight   │
│    (same formula as live):      │
│    acc≥0.60 → 1.0+(acc-0.5)×2  │
│    acc<0.50 → max(0.5,acc/0.5)  │
│  - Merge rule per strategy:     │
│    live ≥10 trades → use live   │
│    backtest ≥30 → fill gap      │
│    else → default 1.0           │
│  - Updates strategy_weights.json│
│    (backtest source only)       │
└─────────────────────────────────┘
        │  (Mondays 07:30 UTC — live weights)
        ▼
┌─────────────────────────────────┐
│  compute_strategy_weights.py    │
│  - Reads bet_outcomes (live     │
│    resolved paper trades)       │
│  - Computes per-strategy        │
│    direction accuracy           │
│  - Writes weights +             │
│    per_strategy_samples to      │
│    strategy_weights.json        │
│  - per_strategy_samples is the  │
│    handoff signal: once ≥10     │
│    live trades exist for a      │
│    strategy, live weight wins   │
│    over backtest on next Sunday │
│  - Git commits the file         │
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
MAX_BET_AMOUNT = 5   # throttled — see comment in file
MIN_CONFIDENCE = 0.65
MAX_POSITIONS_PER_CATEGORY = 3
```

The API key is read from an environment variable (not hardcoded). `INITIAL_BALANCE = 1000` is the fake starting money. `MAX_POSITIONS_PER_CATEGORY = 3` caps how many open bets can share a topic category (crypto/politics/sports/etc) — prevents the bot going all-in on one topic during a volatile week.

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
**What it does:** The 6 trading strategies, Kelly sizing, and **category inference**. This is where the bot decides whether a market is worth betting on and which way.

`_infer_market_category(question)` is the **single source of truth** for category classification. `scripts/harvest_resolved.py` imports it directly so calibration DB labels always match live-trading labels. The function pads the search string with spaces (`' ' + q + ' '`) so space-bounded keywords like `' win '` or `' ai '` work correctly at sentence boundaries and don't false-match substrings (e.g. `' game '` doesn't match "GameStop", `' team '` doesn't match "steam", `' ai '` doesn't match "raise").

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
1. **Ollama** at `localhost:11434` — runs `llama3.2:3b` locally on the server. Free, no API cost, ~5–10 seconds per market on CPU.
2. **DeepSeek API** — fallback only if Ollama is unreachable. Uses `DEEPSEEK_API_KEY` already on the server.

What it sends to the AI (simplified):
```
Question: "Will X happen by 2027?"
Current probability: 72%
Volume: $5,200
Liquidity: $1,400
Close date: 2027-01-01

Historical prior for this market (category: politics, 70–80% bucket):
  Past Manifold markets like this: 48 resolved
  Crowd said ~75%, actually resolved YES 68.2% → crowd overestimates YES by +6.8pp
  Suggested adjustment: treat the current 72% as closer to 65.2%.
  Adjust your estimated_true_probability accordingly before deciding YES/NO/SKIP.

Is this mispriced? Should we bet YES, NO, or skip?
```

Strategy weights are **not** injected into the AI prompt. They belong only in the stat-scoring layer (`auto_research.py`) and the sizing layer (`auto_trader.py`). Injecting them would count the same historical reliability signal a third time — the AI should reason independently about whether the market is mispriced.

The AI now receives a **query-specific calibration prior** (`_build_query_calibration_note()`) instead of the old global 10-row table. Lookup order: `(category, probability_bucket)` cell with `n ≥ 15` → global bucket → no note. This eliminates the old calibration double-count: the AI sees only what history says about *this kind* of market, not a broad table that mostly describes unrelated markets. The `probability_bias_strategy` in `strategies.py` still reads the same calibration data independently — those are two separate uses of the same source, not double-counting.

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
- `resolve_market(market_id, outcome, question="")` — settles a bet, updates balance, writes a row to `bet_outcomes`, and calls `_notify_resolution()` to send a Telegram message (✅/❌ emoji, P&L, new balance). Never raises — Telegram being down cannot prevent resolution.
- `_notify_resolution(market_id, question, outcome, our_bet, total_pnl, new_balance)` — module-level function, wraps all Telegram calls in `try/except`. Sends: win/loss emoji, question (truncated to 80 chars), what resolved vs what we bet, P&L, current balance.
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

**Risk management rules (the filter gauntlet, checked in order):**

| Rule | Value | Meaning |
|------|-------|---------|
| AI veto | SKIP | If AI returned SKIP, blocked regardless of confidence — checked first |
| Min confidence | 65% | Only trade if the strategy signal is strong enough |
| Max positions | 10 | Never hold more than 10 open bets at once |
| Max per category | 3 | No more than 3 open bets in the same topic (crypto/politics/sports/etc). Exception: "other" is uncapped at `MAX_POSITIONS` because positions in the catch-all bucket are uncorrelated by construction. |
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

**Autotrader kill switch:** At startup, `auto_trader.py` checks for `autotrader_disabled.flag` in the workspace root. If found, it exits immediately with a clear message — no trades placed, no noise. Create the flag via `@hackathon_26_bot /autotrader off` or delete it via `/autotrader on`. The flag file approach is safe across cron runs: any in-flight run finishes normally; only the next scheduled run is suppressed.

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
**What it does:** Called at the end of every `auto_research.py` run (not a separate cron). Proposes swapping a losing position for a better opportunity when all slots are full.

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
- `bet_outcomes` — one row per resolved position: `estimated_ev`, `actual_pnl`, `ev_error`, `strategies`, `category`, `era`

#### `data/market_snapshots.db` (gitignored)
SQLite database with a single `market_snapshots` table. Two row types:
- `source='live'` — written by `auto_research.py` every hour (~100 rows/run). M1 pipeline. Fields: `market_id`, `question`, `probability`, `volume24h`, `bettors`, `liquidity`, `snapshot_at`.
- `source='reconstructed'` — written by `reconstruct_snapshots.py` (M3). Fields include `days_before_close` (7/14/30) and `outcome` (YES/NO). These are synthetic snapshots computed from bet history: they represent what the market *looked like* at decision time, not at close.

#### `data/m2_audit.json` (gitignored)
Output of `audit_resolved_markets.py` (M2). Contains:
- Quality stats: near-close %, thin market %, category distribution
- `usable_market_ids` list — the IDs that passed all filters and are worth M3 reconstruction
- `decision_gate`: GREEN / YELLOW / RED

#### `data/strategy_weights.json` (committed)
Written by both `compute_strategy_weights.py` (Monday live run) and `backtest_from_snapshots.py` (Sunday backtest run). Schema:
```json
{
  "weights": {"mean_reversion": 1.0, "probability_bias": 1.0, ...},
  "per_strategy_samples": {"mean_reversion": 6, ...},
  "generated_at": "...",
  "sample_count": 6
}
```
`per_strategy_samples` is the handoff field: once a strategy clears 10 live trades, the Monday run promotes it from `source='backtest'` to `source='live'` automatically.

#### `data/backtest_report.json` (gitignored)
Full output of the most recent `backtest_from_snapshots.py` run. Contains per-strategy accuracy stats, weights computed, merge decisions, and number of snapshots used.

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

### ✅ Fixed — Strategy Weights Loop Broken (feedback loop repair, 2026-04-09/10)

**The problem:** Live paper trades take months to resolve. With only 6 resolved trades total (none per strategy), `compute_strategy_weights.py` returned 1.0 for every strategy forever. The bot was flying blind with no signal on what actually worked.

**Three bugs fixed:**

**Fix 1 — DB schema crash on sandbox**: `_fetch_outcomes()` in `weekly_ev_report.py` queried the `category` column without running a migration first. Sandbox DBs created before the column was added crashed with `OperationalError: no such column: category`. Fixed: `_ensure_bet_outcomes_schema()` now runs before any SELECT, adding missing columns silently.

**Fix 2 — M3 not feeding weight loop (architectural gap)**: The M3 reconstruction script implied it would update strategy weights, but `compute_strategy_weights.py` only reads `bet_outcomes` (live trades). Reconstructed snapshots never touched weights. Fixed: `scripts/backtest_from_snapshots.py` is the bridge — it reads `source='reconstructed'` rows from `market_snapshots.db`, runs strategies against them (outcomes known), computes accuracy → weights, and merges with live data using the priority rule above.

**Fix 3 — API failures swallowed as `no_bets`**: `fetch_bets_for_market()` wrapped all HTTP calls in a try/except that silently converted network errors into empty bet lists. A market that returned a 500 error looked identical to a market with no bet history. Fixed: exceptions now propagate; `reconstruct_market()` catches them as `reason="api_error"` so the summary shows separate counters for `api_errors` vs `no_bets`.

**Fix 4 — `per_strategy_samples` never written**: `_compute_weights()` computed per-strategy sample counts internally but only returned the weight scalars. `merge_weights()` in `backtest_from_snapshots.py` read `per_strategy_samples` from `strategy_weights.json` to decide when to promote live over backtest — but since the field was never written, live data could never win. Fixed: `_compute_weights()` now returns `(weights, per_strategy_samples)` and `main()` writes both fields to `strategy_weights.json`.

### ✅ Fixed — M2/M3 Pipeline (2026-04-09/10)

**M2 audit result (first run):**
- 1,121 resolved markets in corpus
- 55% near-close (>85% or <15%) — excluded as training signal was already obvious
- 41% thin markets (<10 bettors) — excluded as crowd too small to trust
- **267 usable markets** → YELLOW gate (viable, proceed with M3)

**M3 reconstruction result (first run, 267 markets):**
- 127 snapshot rows written to `market_snapshots.db` (source='reconstructed')
- 674 windows skipped (`all_windows_empty` — market opened and closed within 30 days, no bet history at T-30/T-14)
- 0 API errors

**Backtest result:**
- `mean_reversion`: 0 signals — M2's quality filter removes near-extreme-probability markets which are exactly the ones `mean_reversion` needs (>85% or <15%). Structural incompatibility; can only be evaluated from live `bet_outcomes`.
- `probability_bias`: 25 samples, **72% accuracy** (18/25 correct). 5 samples short of the 30-sample gate. Weight would be 1.20 (maximum boost) if it cleared. Will clear after next Sunday harvest adds new resolved markets.

**Automated from next Sunday 2026-04-13:**
M2→M3→backtest runs automatically every Sunday night after the harvest (02:30/03:00/03:30 UTC). No manual intervention needed.

---

## Part 5 — What's Next

**Done:**
- ✅ AI integration (local Ollama `llama3.2:3b`, DeepSeek API fallback)
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

- ✅ **Real Telegram bot** (2026-04-08):
  - `automation/send_telegram.py` — real Bot API HTTP calls; retries 3×; Markdown fallback; no library needed
  - `automation/telegram_bot.py` — `/portfolio`, `/positions`, `/scan`, `/autotrader on`, `/autotrader off` command handlers; prints stdout + sends via API
  - `manifold_bot/config.py` — `TELEGRAM_BOT_TOKEN` + `TELEGRAM_GROUP_ID` from `.env`
  - `skills/portfolio/`, `skills/positions/`, `skills/scan/`, `skills/autotrader-on/`, `skills/autotrader-off/` — OpenClaw workspace skills; all `✓ ready`
  - Usage: `@hackathon_26_bot /portfolio` in group, or DM the bot directly
  - `automation/daily_summary.py` — now sends directly to Telegram instead of writing to file

- ✅ **Auto-notify on resolution + /autotrader on/off** (2026-04-08):
  - `manifold_bot/paper_trader.py` — `_notify_resolution()` fires on every `resolve_market()` call; ✅/❌ emoji, P&L, new balance; never raises
  - `automation/auto_trader.py` — `autotrader_disabled.flag` check at startup; exits cleanly if present
  - Two new OpenClaw skills: `skills/autotrader-on/` and `skills/autotrader-off/`

- ✅ **Swap check moved into research** (2026-04-08):
  - Removed standalone `:40` cron job (`f426953c` disabled on server)
  - `run_swap_check()` called at end of `auto_research.py` main() — fires only when fresh data exists

- ✅ **Sandbox reproducibility restored** (2026-04-08):
  - Research cron was calling untracked `auto_research_fast.py` (skips AI); restored to `automation/auto_research.py`
  - Removed experiment files: `auto_research_fast.py`, `market_research_fast.json`, `run_research.sh`, `test_research.py`, `automation/auto_research_backup.py`
  - Removed dead disabled trading job (`57ce0ebc`); removed stale `logs/trading.log` + `logs/research.log`
  - Closed 2 duplicate `AsUEPpRNc5` positions (trade\_ids 12, 14) — now 8/10 slots used
  - Restarted stuck Ollama runner (was consuming 784% CPU, producing no output); AI analysis confirmed working

- ✅ **Signal family architecture + code quality** (2026-04-07):
  - `strategies.py` — `SIGNAL_FAMILIES` dict; `volume_spike_priority` returns float 0.0–1.0; `probability_bias` category-conditional; `mean_reversion` close-time guard; `probability_direction` volume guard
  - `auto_research.py` — family deduplication, composite ranking, `_AI_CANDIDATE_COUNT = 3`, research always runs
  - `test_manifold.py` — 7 fully mocked unit tests (no network); `--live` flag for manual smoke test
  - `test_strategies.py` — 48 tests including regression for duplicate-bet (2czul2Rync) bug
  - **173 tests total across all suites**

**Completed (2026-04-09):**

- ✅ **OS cron migration** — all 7 scheduled jobs moved from OpenClaw `agentTurn` cron to Linux OS crontab. Scripts run directly (no LLM agent, no narration). Telegram messages come only from the scripts themselves. OpenClaw cron jobs disabled.
- ✅ **Market validation guard** — `auto_trader.py:execute_trade()` now aborts if the market doesn't exist on Manifold, is already resolved, or is closed. Previously a fetch failure silently continued and placed a bet on a phantom market.
- ✅ **Test suite green** — `volume_spike` → `volume_spike_priority` rename reflected in `tests/test_strategies.py`; 48/48 passing.
- ✅ **Weekly strategy weights cron** — registered (Mondays 07:30 UTC); runs `compute_strategy_weights.py`, auto-commits updated `data/strategy_weights.json`.
- ✅ **Trade + resolution Telegram notifications** — `auto_trader.py` sends a message per executed trade; `resolve_positions.py` sends a message when markets resolve. Silent otherwise.
- ✅ **Stale root files deleted** — 14 stale `.py` and 4 stale `.json` files removed from workspace root.
- ✅ **.gitignore updated** — `pending_swaps.json`, `auto_trades.json`, `market_research.json`, `.clawhub/` excluded.

**Completed this session (2026-04-08):**

- ✅ **Category exposure caps** — `MAX_POSITIONS_PER_CATEGORY=3` in `config.py`; enforced in `should_trade_market()`; `category` stored on every trade record; `🗂 BY CATEGORY` breakdown in daily summary
- ✅ **Weighted scoring Phase A** — `compute_strategy_weights.py` reads `bet_outcomes` (post_ev_fix era), computes direction accuracy per strategy, writes `data/strategy_weights.json`; weights (0.5–1.2) applied to raw signal confidence before family dedup in `auto_research.py`. Bug fixed: strategies stored as JSON array — now parsed correctly with `_parse_strategies()`. All 6 active strategies covered.
- ~~**Weighted scoring Phase D**~~ — `_build_performance_note()` was removed (session 4). Injecting strategy weights into the AI prompt was double-counting: the weights already scale stat confidence in `auto_research.py` and Kelly fraction in `auto_trader.py`. Removed to fix the triple-count.
- ✅ **Telegram dedup fix** — `telegram_bot.py:main()` no longer prints to stdout (OpenClaw was also forwarding stdout, causing every reply to arrive twice)
- ✅ **Positions show real titles** — `/positions` does 3-tier lookup: stored `question` field → research data → Manifold API fetch for old trades

**Completed (2026-04-09 session 2):**

- ✅ **Phase B — Dynamic Kelly from strategy weights** — `auto_trader.py` loads `data/strategy_weights.json` at startup; Kelly multiplier = `0.5 × weight` (range 25–60%); `_get_strategy_weight()` takes max weight across all strategies in a rec; `strategy_weight_at_trade_time` logged in `auto_trades.json`.
- ✅ **Phase C — Adaptive category caps** — `bet_outcomes` schema: `ADD COLUMN category TEXT`; `_write_bet_outcome` stores category; `compute_strategy_weights.py` adds `_compute_category_accuracy()` and writes `data/category_accuracy.json`; `auto_trader._effective_category_cap()` reduces cap 3→1 when accuracy < 50% with ≥8 samples. Bug fixed: `_fetch_outcomes()` was missing `category` from SELECT — every row fell back to `"other"`, silently broken.
- ✅ **M1 — Hourly snapshot logger** — `_write_market_snapshots()` in `auto_research.py`; writes one row per non-resolved market to `data/market_snapshots.db` after every fetch (~100 rows/run); best-effort (wrapped in try/except so snapshot failures don't abort research).
- ✅ **Ollama timeout fix** — `FIRST_TOKEN_TIMEOUT` raised 40s→90s (cold model load on this hardware takes ~40s); `keep_alive` 2h→4h.
- ✅ **Test suite: 114 tests** — added `TestAdaptiveCategoryCapPhaseC` (8 tests), `TestSnapshotLogger` (2 tests), `TestComputeCategoryAccuracy` (4 tests including end-to-end category column check).

**Completed (2026-04-10 session 3 — architecture consistency):**

- ✅ **Liquidity threshold alignment** — `MIN_LIQUIDITY = 200` added to `config.py` as single source of truth; all three consumers (`auto_research.py`, `auto_trader.py`, `position_swap_checker.py`) import it. Previously research filtered at 100 and trader filtered at 200 — AI slots were spent on markets that would be rejected at trade time.
- ✅ **AI timeout cooldown** — markets that trigger repeated Ollama timeouts get a 24h cooldown (`data/ai_timeout_cooldown.json`, gitignored). Prevents the same market from consuming AI budget every hour during model loading failures.
- ✅ **Category inference — single source of truth** — `_infer_market_category()` in `strategies.py` is now the canonical implementation. `harvest_resolved.py` imports it directly (removed 30-line duplicate). Calibration DB labels now always match live-trading labels. Keyword list expanded (war/conflict → politics, generic sports terms with word-boundary matching, ai_tech, economics, science). Word-boundary fix: search string padded with spaces so `' game '` doesn't match "GameStop", `' team '` doesn't match "steam", `' ai '` doesn't match "raise".
- ✅ **"other" category uncapped** — `_effective_category_cap("other")` now returns `self.max_positions` instead of `MAX_POSITIONS_PER_CATEGORY`. "other" is a catch-all; positions in it are uncorrelated by construction so the concentration limit was wrong. Specific categories still enforce the 3-position cap.
- ✅ **Stale `"volume_spike"` key removed** — `_KNOWN_STRATEGIES` in `compute_strategy_weights.py` and committed `data/strategy_weights.json` cleaned up (key was renamed to `volume_spike_priority` in session 1 but the stale entry persisted).
- ✅ **`data/category_accuracy.json` seeded** — file was never generated locally; `_load_category_accuracy_file()` silently returned `{}`, keeping Phase C adaptive caps permanently dormant. Seeded with valid empty structure.
- ✅ **`automation/cron_jobs_config.json` gitignored** — OpenClaw background sync regenerates this file on every sync, stomping manually-added entries. Permanently fixed by removing from git tracking.
- ✅ **Test suite: 210 tests** — 17 new tests across `TestLiquidityThresholdAlignment`, `TestAiTimeoutCooldown`, `TestSwapCheckerLiquidityGate`, new keyword coverage in `TestInferCategory`, and `TestAdaptiveCategoryCapPhaseC` extended with "other" uncap behavior.

**Architectural improvements shipped (sessions 4–5):**

- ✅ Strategy weights removed from AI prompt — weights only in stat-scoring (auto_research.py) and sizing (auto_trader.py) layers.
- ✅ Crowd calibration now query-specific — `_build_query_calibration_note()` replaces the global 10-row table; AI sees the `(category, bucket)` prior for this exact market.
- ✅ `by_category` strategy weights — `weight(strategy, category)` → `weight(strategy)` → `1.0` fallback; emitted by `compute_strategy_weights.py`, consumed in both `auto_research.py` stat-scoring and `auto_trader.py` Kelly sizing.

**P3 — News fetcher** — structured news API integration as a `fundamental` family signal (`manifold_bot/news_fetcher.py`); keywords extracted from market question, queries NewsAPI.org free tier.

**P4 — Whale tracking** — track large-bet accounts via `/v0/bets` endpoint; bettor-accuracy cache in `data/calibration.db`.

**M2 — Audit resolved_markets** — one script, one hour: category distribution, snapshot timing, bettor count distribution; output a report before building M3+.

**M3 — Reconstruct earlier snapshots** — fetch bet history for the 1,121 resolved markets, find probability at T-7d/T-14d/T-30d before close; transforms near-close data into training data matching live conditions.

---

## Part 6 — Infrastructure Summary

The bot doesn't run on your laptop. It runs on a server (Aleksi's homelab) inside a Docker container. Here's how it's wired:

```
Docker Container (Linux)
    ├── OS cron daemon (crontab — scripts run directly, no LLM in the loop)
    │       ├── hourly   :00 UTC  → python3 automation/auto_research.py
    │       ├── hourly   :10 UTC  → python3 scripts/resolve_positions.py
    │       ├── hourly   :20 UTC  → python3 automation/auto_trader.py
    │       ├── daily 19:00 UTC   → python3 automation/daily_summary.py
    │       ├── Sundays  02:00    → python3 scripts/harvest_resolved.py + analyze_calibration.py
    │       ├── Mondays  07:00    → python3 scripts/weekly_ev_report.py --telegram
    │       └── Mondays  07:30    → python3 scripts/compute_strategy_weights.py + git commit
    └── OpenClaw (Node.js AI agent gateway — handles Telegram bot, DM commands, skills)
            ├── Telegram bot (@hackathon_26_bot)
            │       ├── group -5240775171: /portfolio /positions /scan /autotrader on/off
            │       └── Notifications sent directly by scripts via Bot API (not via OpenClaw)
            └── Git workspace (~/.openclaw/workspace/)
                    ├── This repo (main branch) — each cron run does git pull first
                    └── data/calibration.db (SQLite, gitignored)
                        ├── resolved_markets (1,121+ rows)
                        └── bet_outcomes (written on each resolution)
```

Scripts execute directly via OS cron — no LLM agent involved in scheduling. Each script sends its own Telegram notification when something meaningful happens (trade placed, market resolved, daily/weekly reports). The OpenClaw gateway handles interactive bot commands only.

**Environment variables:** `MANIFOLD_API_KEY` is set in both `/etc/profile.d/hackathon.sh` (sourced by login shells) and in `openclaw.json` (injected into cron session environments). `config.py` also loads from the workspace `.env` file via `load_dotenv`. Key priority: OS environment (from `openclaw.json`) wins over `.env` since `load_dotenv` does not override existing vars.

**Ollama:** `llama3.2:3b` (2.0 GB) runs as a persistent `ollama serve` + runner process pair. The runner spawns on first generation request and stays alive. `keep_alive: "2h"` in API requests keeps the model resident between hourly cron runs (warm start ~2s vs cold ~20s). If it gets stuck in a spin loop (symptom: 700%+ CPU, no output), restart with `kill $(pgrep -f "ollama runner") && nohup ollama serve > /tmp/ollama.log 2>&1 &` — the model reloads on next call.

---

*Last updated: 2026-04-09 (OS cron migration, llama3.2:3b, market validation guard, test suite green, weekly weights cron)*
