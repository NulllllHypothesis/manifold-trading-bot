# Technical Overview — Manifold Trading Bot

*Written for someone who has never seen this codebase. No assumed knowledge.*

*Last updated: 2026-04-12 (session 8: Op1–Op7.1 operational track shipped).* Session 8 added: Op2 executable-size swap EV (commit `d7a97b8`), Op3 research + trader observability counters (`6580c7d`) + schema v2 zero-candidate fix (`88b8999`), Op4 `days_before_close` on live snapshots + 4384-row backfill (`28703a7`, `9ee145c`, `c466641`), Op5 per-bucket `probability_bias` lookup + lowered `_MIN_BIAS_TO_TRADE` gate + swap margin buffer (`762c43d`), Op7/Op7.1 noise market filter + Unicode normalization + `_BIAS_EXCLUDED_CATEGORIES` excluding 'other' from probability_bias (`6be2021`, `30c6d97`, `7359b5f`), plus `HEARTBEAT.md` disabled (`fc8777e`) to stop false Telegram alerts. Category v2 (gaming/entertainment/business) shipped in `60c31cf`/`d508b38`/`88b8aa4`. **513 tests passing** across 10+ suites.

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

**Category v2 taxonomy (session 8)** — the classifier now covers **9 categories** with first-match-wins ordering:

```
crypto → ai_tech → gaming → entertainment → politics → sports → science → business → economics → other
```

`gaming`, `entertainment`, and `business` were added in session 8 to shrink the `other` catch-all from 80%+ of markets. Politics is ordered before sports so "Trump win" lands in politics, not sports. Short sports acronyms (`' nba '`, `' nfl '`, `' mma '`) are space-padded to avoid substring collisions with common English words (`inflation`, `unbalanced`, `comma`). One-off backfill for existing labels is `scripts/reclassify_categories.py` — it updated 282/2016 rows in `resolved_markets.category` on the first server run.

**Strategy 1 — Probability Direction** (confidence 0.65)
Bet with momentum if the market leans strongly and has had recent activity (last bet < 48h). Skips the 45–55% coinflip zone.

**Strategy 2 — Mean Reversion** (confidence 0.68)
Bet against extreme probabilities (>80% or <20%), but only when fewer than 100 unique bettors have weighed in. Don't fight genuine deep consensus.

**Strategy 3 — Volume Spike Priority** (priority filter — not a directional signal)
If a market's 24h volume is >2x the batch median, returns a `priority_boost` float (0.0–1.0). This value is added to the composite candidate score (`confidence + priority_boost × 0.15`) to rank which markets go to the AI first. It does NOT vote YES or NO and does NOT count as a signal family member.

**Strategy 4 — Creator Disagreement** (confidence 0.75) — *silent by design*
If the market creator's resolution probability estimate differs from the crowd by ≥15pp, bet toward the creator. Op3 counter evidence shows this strategy almost never fires in production: Manifold's API only populates `resolutionProbability` on ~5% of markets, so the fire rate is structurally capped by field availability. The docstring explicitly warns against widening the 15pp gap threshold — the value comes from ONLY firing on strong creator disagreement.

**Strategy 5 — Thin Market** (confidence 0.65) — *confirmation-only*
When one side of the AMM liquidity pool holds <15% of the total, bet toward that underrepresented side. Wired in `auto_research.py` as confirmation-only: it adds +0.03 boost to an existing contrarian/fundamental signal when it agrees, but never votes independently. So `thin_market` raw_fires in Op3 counters is 0 in most runs — that's expected, because thin_market can only "fire" when another contrarian/fundamental signal matches the same pool-imbalance direction.

**Strategy 6 — Probability Bias** (confidence 0.70) — **Op5 rewired + Op7/Op7.1 tightened (session 8)**
Exploits systematic crowd overconfidence using per-(category, bucket) calibration cells. The strategy now has two lookup paths:

1. **Per-bucket cell** (preferred): reads `by_category_bucket` rows from `data/calibration_table.json` where `sample_size ≥ 15` AND `reliable=True`. If a match exists for the market's `(category, 10%-bucket)`, use its bias directly.
2. **Category aggregate** (fallback): if no reliable per-bucket cell exists, fall back to the category-level mean bias from `by_category` rows in the same file.

The strategy fires when `abs(bias) ≥ _MIN_BIAS_TO_TRADE` (0.025 — lowered from 0.04 in Op5 because category v2 reclassify diluted every bias value). Direction is set by the sign of the bias:

- Positive bias (crowd overestimates YES) → bet **NO**
- Negative bias (crowd underestimates YES) → bet **YES** (new in Op5; previously only positive-bias cells were handled)

Op5 also removed the hardcoded "fires only in 60-70% and 30-40% buckets" filter — any bucket with reliable calibration data now qualifies.

**`_BIAS_EXCLUDED_CATEGORIES` (Op7.1):** The `other` category is fully excluded from probability_bias. Its aggregate bias (0.0259) barely cleared the 0.025 threshold, but `other` is a catch-all averaging unrelated sub-populations (politics/celebrity/coinflip/research) — applying it to any individual market is semantically wrong. The exclusion is implemented as a set check at the top of the strategy function.

**Noise market filter (Op7 + Op7.1):** Before strategy evaluation, `_is_noise_market(question)` checks if a market is structural noise (coinflip, free lottery, d20/d6 roll). The filter uses **NFKD Unicode normalization + combining mark stripping** so accented variants like "Dailÿ Coin Flip" cannot bypass ASCII-only patterns. Patterns were tightened in Op7.1 to avoid false positives: bare substrings like 'random', 'dice', 'lottery' were removed (they matched "random drug testing", "Dice Dreams", etc.); only specific phrases are matched: coinflip variants, 'free lottery', 'd20 roll'/'d6 roll'.

**Op5 production impact (2026-04-11 15:55 UTC, first post-deploy run):** `probability_bias` raw fires went from **0 to 18** in a single run; `contrarian` dedup winners went from 3 to 18; the final recommendation mix expanded from 1 shape (`probability_direction` only) to 3 shapes. Ollama vetoed all 3 bias recs it analysed that run — this over-firing on `other` category markets motivated Op7.

**Op7/Op7.1 production impact (2026-04-12):** 24h of counter data showed 363 probability_bias fires, 96% on 'other', 18% structural noise, AI vetoed 76%. After Op7.1: `other` excluded entirely, noise markets filtered out, false-positive-safe patterns only.

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
2. Writes all non-resolved markets to `data/market_snapshots.db` with `source='live'` and `days_before_close` computed from `closeTime` at snapshot time (Op4 — used by the M4 training pipeline)
3. Filters out: resolved markets, stale markets (last bet > 72h AND volume24h < 10), liquidity under `MIN_LIQUIDITY` (200)
4. Runs all 6 strategies on each remaining market
5. Family deduplication: keeps strongest signal per directional family (momentum/contrarian/fundamental); applies `thin_market` confirmation filter
6. Computes `priority_boost` via `volume_spike_priority`, adds composite score (`confidence + priority_boost × 0.15`)
7. Sorts by composite score (highest first); sends top 3 to AI (`_AI_CANDIDATE_COUNT = 3`)
8. Blends stat + AI confidence
9. Computes two EV figures per recommendation: `estimated_ev_ref` (at $25 reference stake, for cross-market ranking) and `estimated_ev_exec` (at real `MAX_BET_AMOUNT=$5` stake, used by the swap checker). `estimated_ev` remains as a legacy alias for `_ref`. **Op2 fix (session 8):** swap decisions now compare real unrealised losses against `estimated_ev_exec`, not `_ref` — a $25-scale $4 EV is only a $0.80 EV at $5 executable stake.
10. **AI metadata defaults (session 8):** every rec is initialised with `ai_was_candidate=False`, `ai_returned_skip=False`, `ai_recommendation=None` BEFORE the AI candidate pass runs. This ensures `save_research()` always writes `schema_version=2` when the pipeline runs to completion, even when the AI pass doesn't run (e.g. because every above-floor rec was on timeout cooldown). Previously a zero-candidate run wrote schema v1 and halted the trader.
11. Saves to `market_research.json` — keeps the latest result plus a 24-hour history
12. Calls `run_swap_check()` at the end (not a separate cron)
13. **Op3 counters (session 8):** emits a per-run JSON object to `data/research_counters.jsonl` and prints a human-readable summary block to the cron log

Output file structure (`market_research.json`):
```json
{
  "latest": {
    "timestamp": "2026-04-11T15:55:58",
    "schema_version": 2,
    "recommendations": [
      {
        "market_id": "abc123",
        "question": "Will X happen?",
        "probability": 0.72,
        "recommendation": "NO",
        "confidence": 0.65,
        "strategies": ["probability_bias"],
        "priority_boost": 0.42,
        "ai_was_candidate": true,
        "ai_returned_skip": false,
        "ai_recommendation": "NO",
        "estimated_ev_ref": 4.20,
        "estimated_ev_exec": 0.84,
        "estimated_ev": 4.20
      }
    ]
  },
  "history": [ ...last 23 runs... ]
}
```

**Op3 research counters** (`data/research_counters.jsonl`, one line per run):
```json
{
  "timestamp": "2026-04-11T15:55:58+00:00",
  "markets_fetched": 100,
  "skipped_resolved": 7, "skipped_low_liquidity": 55, "skipped_stale": 0,
  "raw_fires": {
    "probability_direction": 11, "mean_reversion": 3,
    "probability_bias": 18, "creator_disagreement": 0, "thin_market": 0
  },
  "dedup_winners": {"momentum": 11, "contrarian": 18, "fundamental": 0},
  "thin_market_confirmations": 0, "no_active_signals": 18, "tied_votes_dropped": 1,
  "ai_eligible": 19, "ai_cooled_down": 9, "ai_analyzed": 3,
  "ai_agree": 0, "ai_disagree": 0, "ai_skip": 3, "ai_no_result": 3,
  "final_recommendations": 19,
  "final_by_strategy_mix": {
    "probability_bias": 9,
    "probability_bias,probability_direction": 8,
    "probability_direction": 2
  },
  "final_by_category": {"other": 16, "politics": 3}
}
```

Read-only diagnostic: `python3 scripts/analyze_strategy_coverage.py` loads this file plus `calibration_table.json` and reports 24h fire rates, which categories/cells unlock at a proposed threshold, and the current schema state.

---

#### `automation/auto_trader.py`
**What it does:** Reads the research file and decides whether to actually place trades.

**Schema guard** — refuses to trade on pre-AI research. Supports both file layouts written by `auto_research.py`:
```python
# Nested layout (normal): { "latest": { "schema_version": 2, ... }, "history": [...] }
# Flat layout (edge case): { "schema_version": 2, ... }
```
If `schema_version < 2` (i.e. AI pass was skipped or aborted), prints a Telegram-visible halt message and returns None. Session 8's Op3.1 fix guarantees schema v2 is always written when the research pipeline runs to completion.

**Risk management rules (the filter gauntlet, checked in order):**

Session 8 refactor: `should_trade_market()` is now a thin bool wrapper around `_trade_rejection_reason()`, which returns the exact rejection-reason string. The string keys match the Op3 trader counter buckets so every rejection is tallied.

| Rule | Value | Rejection key | Meaning |
|------|-------|--------------|---------|
| AI veto | SKIP | `ai_veto` | If AI returned SKIP, blocked regardless of confidence — checked first |
| Min confidence | 65% | `low_confidence` | Only trade if the strategy signal is strong enough |
| Existing OPEN | — | `existing_open` | Blocks new bet on a market with an existing OPEN position (live check, not research flag) |
| Max positions | 10 | `max_positions` | Never hold more than 10 open bets at once |
| Max per category | 3 | `category_cap` | No more than 3 open bets in the same topic. Exception: "other" is uncapped at `MAX_POSITIONS` because positions in the catch-all bucket are uncorrelated by construction. |
| Min liquidity | $200 | `liquidity` | Don't trade illiquid markets (hard to exit) |
| Kelly no-edge | — | `kelly_no_edge` | `calculate_position_size()` returned 0.0 because Kelly fraction ≤ 0 |
| Min bet size | $1 | `size_too_small` | Post-sizing, bet size below `MIN_BET_AMOUNT` |
| Market unverifiable | — | `market_unverifiable` | Market doesn't exist on Manifold or is already resolved/closed at trade time |
| Max trades per run | 2 | — | Only place 2 trades max per hourly cycle (applied after gauntlet) |

**Op3 trader counters** (`data/trader_counters.jsonl`) record exactly these rejection reasons plus `recommendations_loaded`, `passed_filter`, `trades_executed`, and `top_ev_at_exec` (top-5 by exec EV so you can see what would have traded if the book had free slots).

Ranking: tradable recs are sorted by `estimated_ev_exec` (Op2-correct exec EV) descending. Recs without the exec field fall back to legacy `estimated_ev` for backwards compatibility.

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
   - Ranked by `_swap_ev()` which prefers `estimated_ev_exec` over legacy `estimated_ev`
7. **Op2 exec-EV gate + Op5 margin buffer (session 8):** requires `best_exec_ev ≥ abs(unrealised_loss) + SWAP_MIN_MARGIN` (1.00). The exec-EV prefix prevents the old bug where $25-ref EVs were compared to real $5-stake losses, inflating proposals by 5×. The margin buffer prevents tiny-margin swaps like the 2026-04-11 11:40 WTI proposal (loss $0.30, exec EV $0.51, net $0.21 of edge) from going through.
8. Writes all qualifying pairs to `pending_swaps.json` as a numbered list
9. Sends one Telegram message per proposal

**`_swap_ev(rec)` helper** — new in session 8 (Op2). Single source of truth for "which EV field do swaps compare against". Order: `estimated_ev_exec` → legacy `estimated_ev` fallback → None. Both `find_best_opportunity()` ranking and the `ev > loss + margin` gate use this helper.

**Telegram message format:**
```
♻️ Swap Proposal #1
Close: NO on "Will Biden give a speech in 2026?"
  Entered 35%, now 58%, unrealised $-14.30

Open: YES on "Will OpenAI release GPT-5 before June 2026?"
  AI confidence 78%, exec EV +$4.20

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

### ✅ Fixed — Session 8 (2026-04-11): Op1–Op5 operational track

**Op1 — Operational unblock.** The bot had 11/10 positions and a stuck pending swap blocking the swap loop (`has_active_pending_swaps()` gate). Dismissed manually; swap loop unblocked. The 11th ghost position (`ZLCUqRRE85`, no stored question text) was left in place per user instruction.

**Op2 — Swap EV correctness (commit `d7a97b8`).** `auto_research.py` computed `estimated_ev` at a fixed $25 reference stake for cross-market ranking. `position_swap_checker.py` then compared that number to **real unrealised losses**, overstating swap attractiveness by `$25 / MAX_BET_AMOUNT = 5×` under the current throttle. The WTI pending swap was the evidence: loss -$1.99, ref-EV +$4.09, exec-EV +$0.82 — only looked good because we were measuring against the wrong number. Fix: write BOTH `estimated_ev_ref` ($25) and `estimated_ev_exec` (`MAX_BET_AMOUNT`) on every rec; new `_swap_ev()` helper prefers exec; both ranking and the gate use it. `tests/test_swap.py::TestSwapEvCorrectness` locks this in with an explicit WTI regression guard. **Verified in production 2026-04-11 11:40 UTC:** rejected a $2.63-loss/$0.51-exec-EV swap that the old code would have approved.

**Op3 — Strategy observability (commit `6580c7d`).** Before retuning anything, we needed counter data to decide WHY the live bot was mono-strategy. New `data/research_counters.jsonl` + `data/trader_counters.jsonl` record per-run: markets fetched, skips by reason, raw fires per strategy, family dedup winners, thin-market confirmations, no-signal drops, AI flow (eligible/cooled/analyzed/agree/disagree/skip/no-result), final recommendation mix by strategy-set AND category; on the trader side: recs loaded, rejections by reason (9 keys), passed_filter, top-5 by exec EV, trades executed. `should_trade_market()` was refactored so every rejection returns a string key that matches a counter bucket. `tests/test_observability.py` has 25 tests for counter shape + rejection key mapping.

**Op3.1 — Schema v2 zero-candidate fix (commit `88b8999`).** Production bug caught mid-session: when every above-floor rec was on AI-timeout cooldown, `candidate_markets` ended up empty, the `if candidate_markets:` block never ran, recs were missing `ai_was_candidate` field, and `save_research()` fell back to `schema_version=1`. That halted the trader for the rest of the cycle. Fix: initialize AI metadata defaults on every rec BEFORE the candidate pass. 6 new tests including `TestAnalyzeMarketsZeroCandidatePath` end-to-end regression guard.

**Op4 — Live snapshot `days_before_close` (commits `28703a7`, `9ee145c`, `c466641`).** Before Op4, live snapshots were written with `days_before_close=NULL` so `build_training_dataset.py` skipped them. ~4600 live rows were accumulating as runtime artifacts with no training-track value. Fix: `_write_market_snapshots()` computes `days_before_close` from `closeTime` at snapshot time, clamped to 0 for already-closed markets. New `scripts/backfill_days_before_close.py` one-off backfill uses `resolved_markets.close_date` as a fast local lookup with a Manifold API fallback for unresolved markets. `build_training_dataset.py::_snap_live_to_window()` buckets each live row to the nearest canonical window `{7, 14, 30}` within ±1 day tolerance. **Server backfill: 4384/4605 rows populated (95.2%)** in ~72s. 30 tests in `tests/test_days_before_close.py`.

*Dataset impact today is zero* — 0 of the 16 qualifying live markets have resolved yet. Op4 pre-positions the data so the dataset grows week-over-week as those markets resolve, without further code changes.

**Op5 — Strategy guard retune (commit `762c43d`).** First real Op3 counter run (2026-04-11 11:39 UTC) showed `probability_bias=0`, `creator_disagreement=0`, `thin_market=0` in raw fires — three strategies never fired at all, so tuning their base confidences (0.65/0.68/0.70) would have been pointless. The guards were the problem, not the confidences. The diagnostic `scripts/analyze_strategy_coverage.py` showed that after category v2 reclassify, every category's aggregate bias had dropped (politics: 0.0623 → 0.0068, crypto: 0.0534 → 0.0395, etc.) — the 0.04 gate was unreachable. Fixes:

1. **probability_bias** — rewired to use per-`(category, bucket)` cells from `calibration_table.json` directly (`sample_size ≥ 15`, `reliable=True`); category aggregate is the fallback. Removed hardcoded 60-70% / 30-40% bucket filter. Lowered `_MIN_BIAS_TO_TRADE` from 0.04 to 0.025. Negative-bias cells now produce YES (crowd underestimates) instead of None.
2. **creator_disagreement** + **thin_market** — documented silent-by-design in docstrings so future operators don't waste time trying to "fix" the 0 counters. `creator_disagreement` is structurally capped by Manifold's ~5% API field availability; `thin_market` only fires as a +0.03 boost on matching contrarian/fundamental signals.
3. **Swap margin buffer** — new `SWAP_MIN_MARGIN = $1.00` constant. Swap gate now requires `best_ev ≥ loss + SWAP_MIN_MARGIN`. Prevents tiny-margin swaps like the 2026-04-11 11:40 proposal (loss $0.30, exec EV $0.51, net $0.21 of edge for crystallising a loss).
4. **Tests** — `TestProbabilityBiasStrategy` rewritten with a deterministic `_CalibrationPatch` context manager so tests don't depend on whatever `calibration_table.json` is on disk; 12 new tests. New `TestSwapMinMarginBuffer` (5) + `TestRunSwapCheckWithMinMargin` (2).

**Op5 production impact** (first post-deploy run, 2026-04-11 15:55 UTC):

| Metric | Pre-Op5 | Post-Op5 |
|---|---:|---:|
| `probability_bias` raw fires | 0 | **18** |
| `contrarian` dedup winners | 3 | 18 |
| `no_active_signals` | 29 | 18 |
| `final_recommendations` | 7 | 19 |
| Strategy mix shapes | 1 | 3 |

**Follow-up observation → fixed by Op7/Op7.1:** In the post-Op5 run Ollama vetoed all 3 `probability_bias` recs it analysed (`ai_skip=3/3`). 24h of counter data confirmed: 363 probability_bias fires, 96% on 'other', 18% structural noise, AI vetoed 76%. Fixed in Op7/Op7.1 — see below.

**HEARTBEAT.md disabled (commit `fc8777e`).** OpenClaw's heartbeat loop was interpreting `HEARTBEAT.md`'s template header as a task and running a mini status-check turn every hour. That turn was re-reading stale session history from 10:10 UTC (before the session 8 fixes landed) and firing false-positive Telegram alerts claiming Ollama was down, research was missing, balance was $5.28, etc. None of which was true. Fixed by replacing the template text with comment-only content so the heartbeat loop replies HEARTBEAT_OK and stays silent. Real observability now comes from Op3 counters + cron logs + daily summary only.

---

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

### Current operational backlog (as of session 8, 2026-04-12)

**Op6 — M6 LoRA fine-tune run.** Deferred. Scaffolding (`scripts/finetune.py`), promotion gate (crowd Brier 0.1503 / DirAcc 75.0% on held-out test split), and all tests are in place. Blocked on dataset growth via Op4's live accumulation path — current dataset is still 127 rows and the 8-example test split can't distinguish signal from noise. Also contingent on whether Op5's newly-unblocked signal layer produces enough tradable opportunities that a fine-tuned veto turns out to be unnecessary.

**Op7/Op7.1 — Bias over-firing on heterogeneous `other` markets (done 2026-04-12, commits `6be2021`, `30c6d97`, `7359b5f`).** 24h of Op3 counter data after Op5 showed probability_bias over-firing: 363 fires, 96% on 'other', 18% structural noise (coinflip/lottery), AI vetoed 76%. Three-commit fix:

1. **Op7 (`6be2021`):** Noise market filter (`_is_noise_market`) on question text + exclude 'other' from per-bucket lookup
2. **Op7.1 (`30c6d97`):** Unicode bypass fix (NFKD normalization + combining mark stripping) + exclude 'other' from probability_bias entirely via `_BIAS_EXCLUDED_CATEGORIES` (aggregate 0.0259 still cleared 0.025 threshold)
3. **Op7.1 tightening (`7359b5f`):** Removed overly broad noise patterns ('random', 'dice', 'lottery' matched false positives like "random drug testing", "Dice Dreams"). Kept only: coinflip variants, 'free lottery', 'd20 roll'/'d6 roll'. Added 4 false-positive guard tests.

**513 tests passing.** Server deployed at `7359b5f`.

**Tracks not in the Op1-Op7 queue:**
- P4 — Whale tracking (fundamental family, new `whale_strategy`)
- P5 — FastAPI web dashboard (convenience)
- P6 — SQLite storage for state files (housekeeping)

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

*Last updated: 2026-04-12 (session 8 — Op1–Op7.1 operational track shipped: dual-field swap EV, research/trader observability counters, schema v2 zero-candidate fix, live snapshot `days_before_close` + backfill, per-bucket `probability_bias` lookup with lowered threshold, swap margin buffer, category v2 taxonomy, HEARTBEAT.md disabled, Op7/Op7.1 noise market filter + Unicode normalization + `_BIAS_EXCLUDED_CATEGORIES` excluding 'other' from probability_bias. 513 tests passing.)*
