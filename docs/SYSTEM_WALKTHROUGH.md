# SYSTEM_WALKTHROUGH — Operator's Brain

> **Purpose**: this is not a reference manual. It's the doc you read *once*, running commands on the sandbox alongside, so you build a mental model of how the bot actually decides things.
>
> **How to use**: start at §0 (safety + drift check), then §1, work down. Every section ends with a `now run this` block — actually run it on the sandbox. Don't skip the runs, they're what make it stick.
>
> **Estimated time**: 90–120 minutes, ideally in one sitting.
>
> **Doc version**: written 2026-04-21. Specific sandbox-state numbers in this doc are as-observed on that date and will drift — follow the `now run this` blocks to see current values, don't trust the numbers in prose.

---

## 0. Before You Start

### 0.1 Safe vs unsafe commands

This doc asks you to run commands against the live sandbox. The colour/prefix convention in every `now run this` block:

| Prefix | Meaning | Examples |
|---|---|---|
| `🟢 READ` | Pure read. No state mutation anywhere. Safe to run any number of times. | `crontab -l`, `sqlite3 ... SELECT ...`, `cat market_research.json`, `python3 -c 'import json; print(...)'` |
| `🟡 OBSERVE` | Reads + runs a script in its natural read phase. Safe but may append to log files (`/tmp/*.log`, `data/*_counters.jsonl`). | `python3 scripts/detect_stale_positions.py` (no `--apply`) |
| `🔴 MUTATE` | Changes state (`paper_trading_state.json`, `bet_outcomes`, balance). Only run when you mean to. | `execute_close.py --id N`, `execute_abandon.py --id N`, `close_position_early(...)` direct calls, `place_paper_bet(...)` |

**Every `now run this` block in this doc is `🟢 READ`.** If you see 🔴 elsewhere, stop and confirm intent before running.

While studying: **never run a `🔴 MUTATE` command.** If you need to trace what a mutation would do, read the code, don't execute it.

### 0.2 Version drift checklist

The code changes frequently. Before trusting specific numbers in this doc, confirm you're in sync:

```bash
🟢 READ
# 1. Local main commit
git -C /Users/amirghari/Desktop/Hackathon/workspace log --oneline -1

# 2. Sandbox main commit (should match local)
ssh hackathon-server 'cd /home/hackathon/.openclaw/workspace && git log --oneline -1'

# 3. Live crontab (should match §1 map below)
ssh hackathon-server 'crontab -l' | grep -E "^[0-9]" | sort

# 4. Cron generator in the repo (should match live crontab)
grep "^[0-9].*cd \$WORKSPACE" /Users/amirghari/Desktop/Hackathon/workspace/automation/setup_cron_jobs.py
```

If any of (1)–(4) disagree with each other or with §1, **resolve the drift first**. A walkthrough against a stale doc teaches the wrong model.

---

## 1. The One-Page System Map

Keep this in your head. Everything else hangs off it.

```
            Manifold API
                 │
                 ▼
    :00  automation/auto_research.py
                 │ writes
                 ▼
         market_research.json          ←── shortlist of candidates + AI votes
                 │
                 ▼
    :20  automation/auto_trader.py    ←── applies gates, places bet
                 │ writes
                 ▼
    manifold_bot/paper_trading_state.json
    (balance, positions[market_id][legs])
                 │
                 ▼
    :05  scripts/reprice_positions.py       (every hour)
                 │ writes
                 ▼
    data/calibration.db: position_snapshots ←── P&L trajectory over time
                 │
                 ▼
    :30  scripts/evaluate_positions.py      ←── propose CLOSE (if score < -0.50)
                 │ writes
                 ▼
         pending_closes.json          ←── human approves via execute_close.py
                 │
                 ▼
    :10  scripts/resolve_positions.py       (every hour)
  13:00  scripts/detect_stale_positions.py  (daily — STRANDED/ABANDONED)
                 │ writes
                 ▼
    data/calibration.db: bet_outcomes       ←── the learning data
                 │
                 ▼
    Monday 07:00  scripts/weekly_ev_report.py        (EV accuracy → Telegram)
    Monday 07:30  scripts/compute_strategy_weights.py (weights update, live gate=10 samples)
                 │ writes
                 ▼
    data/strategy_weights.json       ←── the learning output, feeds back into :00 research
```

**Three things to notice:**

1. **Research and trading are separate crons.** Research picks candidates (`:00`); the trader picks which of those to actually bet on (`:20`). They talk only via `market_research.json`.
2. **Repricing and resolution are separate too.** Repricing (`:05`) updates live P&L; resolution (`:10`) turns a resolved market into realized P&L. Evaluator (`:30`) only decides *early* closes.
3. **Weekly cron is the only thing that moves `strategy_weights.json`.** Everything else accumulates data. Learning happens on Mondays.

### Now run this

Confirm the cron schedule is what this doc claims:

```bash
ssh hackathon-server 'crontab -l' | grep -E "^[0-9]"
```

As of 2026-04-21 you should see `:00 research`, `:05 reprice`, `:10 resolve`, `:20 trade`, `:30 evaluate`, `13:00 stale detection`, plus the weekly Sunday/Monday jobs. If your output differs, go back to §0.2 and resolve the drift.

---

## 2. Data Contracts

What each file / table actually means. **These are the nouns in the system. If you know what they are and who reads/writes them, you know the bot.**

### 2.1 `market_research.json` (written by research, read by trader)

| Key | What it is |
|---|---|
| `latest.recommendations[]` | One entry per candidate the research shortlisted this hour. Includes `recommendation` (YES/NO), `confidence`, `strategies[]`, `estimated_ev`, `ai_status`, `position_class`, etc. |
| `latest.counters` | How many markets were fetched, skipped by each filter, analyzed by AI, etc. Same shape as `data/research_counters.jsonl`. |
| `history[]` | Previous hours' runs. Used by the dashboard. |

**Mental model**: this is the trader's *menu*. The trader only considers markets on this menu — never queries Manifold directly to pick a trade.

### 2.2 `manifold_bot/paper_trading_state.json` (written by trader + every closer, read by everything)

```json
{
  "balance": 100.00,
  "positions": {
    "<market_id>": [ {leg1}, {leg2}, ... ],
    ...
  },
  "trade_history": [ ... ],
  "phase3_capital_epoch_started": "2026-04-20"   // audit marker for reset
}
```

Each leg has: `trade_id`, `status` (`OPEN` / `WIN` / `LOSE` / `CLOSED_EARLY` / `STRANDED` / `ABANDONED`), `outcome` (our YES/NO bet), `amount`, `entry_probability`, and post-Phase-2 fields like `current_unrealised_pnl`, `last_repriced_at`, `position_class`, `close_time_ms`.

**Who writes this**: `paper_trader.py` (`place_paper_bet`, `resolve_market*`, `close_position_early`, `mark_market_abandoned`) and `reprice_positions.py` (in-place updates of `current_*` fields).

**Who reads it**: almost everything — trader checks caps, evaluator scores, detector flags stale, resolver looks for markets to reconcile.

### 2.3 `pending_closes.json` / `pending_abandons.json` (evaluator writes, `execute_*.py` reads)

Lists of proposals awaiting human approval. Each entry: `id`, `market_id`, `reason`, `proposed_at`, `status` (`pending` / `approved` / `dismissed` / `expired`), `position_score`, etc. The evaluator writes new ones hourly (`:30`); you approve via:

```bash
python3 scripts/execute_close.py --id 7
python3 scripts/execute_abandon.py --id 3
```

4h TTL on closes, 72h on abandons (because write-offs deserve more consideration).

### 2.4 `data/calibration.db:position_snapshots` (reprice writes, evaluator + dashboard read)

Append-only log. One row per (position, repricing run). Columns: `snapshot_at`, `market_id`, `trade_id`, `current_probability`, `unrealised_pnl`, `days_held`, `is_resolved`, `api_error`.

**Mental model**: this is the chart data. The `/positions` dashboard uses this to draw the P&L-over-time line when you click a row.

### 2.5 `data/calibration.db:bet_outcomes` (terminal writes, weekly jobs read)

One row per trade that *terminated* (WIN / LOSS / MKT / CANCEL / CLOSED_EARLY / ABANDONED). Columns include `estimated_ev`, `ai_confidence`, `ai_estimated_probability`, `strategies`, `actual_pnl`, `ev_error`, `era`, `resolved_at`, `category`.

**`era` is the important field for learning**:

| `era` | What it means | Counts toward live weights? |
|---|---|---|
| `post_ev_fix` | Real resolution on a trade placed after EV pipeline was working (2026-04-07+) | ✅ yes |
| `early_close` | Closed via `execute_close.py` at AMM price before resolution | ❌ no (audit only) |
| `write_off` | `ABANDONED` — 90 days past close with no resolution | ❌ no (audit only) |
| `pre_ev_fix` | Legacy trades, missing EV data | ❌ no |
| `stranded` | (not currently written — STRANDED legs stay in state, don't book yet) | n/a |

> **Phase 3 gotcha**: only `post_ev_fix` drives `compute_strategy_weights.py`. Reviewer caught this during Phase 3 prep — don't let the total row count fool you.

### 2.6 `data/strategy_weights.json` (Monday cron writes, research reads)

```json
{
  "probability_direction": 1.00,
  "mean_reversion": 1.00,
  "probability_bias": 1.00,
  "creator_disagreement": 1.00,
  "thin_market": 1.00,
  "by_category": { "sports": { "probability_direction": 1.05 }, ... }
}
```

When a strategy has ≥10 `post_ev_fix` outcomes with reliable EV, its weight drifts off 1.00 to reflect real accuracy. Research multiplies raw confidence by this weight when picking AI candidates.

**Currently all 1.00.** That's the learning-loop output we're trying to unlock on Monday 2026-04-27.

### Now run this

```bash
ssh hackathon-server '
cd /home/hackathon/.openclaw/workspace
echo "=== Rows per era ==="
sqlite3 data/calibration.db "SELECT era, COUNT(*) FROM bet_outcomes GROUP BY era"
echo "=== Current weights ==="
cat data/strategy_weights.json | python3 -m json.tool | head -12
'
```

As of 2026-04-21 we saw: `post_ev_fix: 10`, `early_close: 6`, `pre_ev_fix: 3`, every strategy weight = 1.0. Your run may differ — that's "pipeline works but no signal moved yet," exactly the state we want to flip on Monday.

---

## 2.5 Decision Gates — Cheat Sheet

When a trade shows up somewhere unexpected (or doesn't show up where you expected), one of these gates is why. Keep this table open as a reference.

### Research-time filters (`:00` cron, `automation/auto_research.py`)

Applied **before** any strategy runs. Each counts into the corresponding `skipped_*` counter in `data/research_counters.jsonl`.

| Filter | Rejects if... | Code |
|---|---|---|
| `skipped_resolved` | `isResolved=True` on the API payload | [auto_research.py](/Users/amirghari/Desktop/Hackathon/workspace/automation/auto_research.py:541) |
| `skipped_low_liquidity` | `totalLiquidity < MIN_LIQUIDITY` (200) | same |
| `skipped_stale` | Last bet > 72h ago AND near-zero 24h volume | `TradingStrategies.is_stale_market` |
| `skipped_noise` | Structurally random (coinflip, lottery, etc.) | `_is_noise_market(question)` |
| `skipped_unverifiable` | Question starts with "Will I… / Will my… / Do I…" etc. | `is_unverifiable_market` (PR #19) |
| `skipped_thin_other_momentum` | category=other (or inferred-other) + only `probability_direction` fired + <3 unique bettors | `is_thin_other_momentum_market` (PR #19/20) |

### Trader rejection reasons (`:20` cron, `automation/auto_trader.py:_trade_rejection_reason`)

Applied **per recommendation** after research has already shortlisted it. Each writes to `data/trader_counters.jsonl` under `rejected.<reason>`.

| Reason | Rejects if... |
|---|---|
| `ai_veto` | `ai_status=='skip'` (real AI SKIP, not just timeout) |
| `market_unverifiable` | Backstop — `is_unverifiable_market()` or `is_thin_other_momentum_market()` returns True even though research should have caught it |
| `solo_momentum_low_res` | (PR #25) Voting strategies = `['probability_direction']` only AND `resolvability == 'low'`. `thin_market` is excluded from the voter count. Multi-signal trades and high/medium-resolvability markets pass. |
| `negative_ev` | (PR #22, refined PR #26) `estimated_ev_exec` (or legacy `estimated_ev`) ≤ `MIN_ESTIMATED_EV_FLOOR` (0.0). Pre-flight uses the rec's stale EV; live recompute in `execute_trade` uses fresh market price. After PR #26 the EV at this gate is honest current-price-anchored math, not the old `confidence`-as-`P(YES)` shortcut. |
| `low_confidence` | `confidence < MIN_CONFIDENCE` (0.65) |
| `existing_open` | We already hold an OPEN leg on this market (use swap, don't stack) |
| `max_positions` | Global book at 10/10 |
| `position_class_full` | short=5 / medium=3 / long_or_uncertain=2 bucket is at cap |
| `category_cap` | A category already has `MAX_POSITIONS_PER_CATEGORY=3` open legs |
| `liquidity` | Live liquidity dropped below floor between research and trade-time |
| `kelly_no_edge` | Kelly sizing returned ≤0 bet |
| `size_too_small` | Bet would be < `MIN_BET_AMOUNT` |

**EV math change (PR #26):** stat-only EV used to be `P(YES) = confidence` (or `1 − confidence` for NO), which conflated signal reliability with outcome probability and produced phantom +$4 EVs on coin-flip markets. The fix anchors `P(YES)` to `current_prob` and applies a bounded edge: `edge = (confidence − 0.5) × STAT_PROB_EDGE_SHRINKAGE` (default 0.3). Both `auto_trader.execute_trade` and the new `auto_research._compute_rec_ev` helper use the same `_stat_derived_probability` function and validate `probability` identically (None / non-numeric / out-of-range → None). Tune `STAT_PROB_EDGE_SHRINKAGE` in `manifold_bot/config.py` if Phase 3 `ev_error` data shows the model is systematically over- or under-estimating.

**Dismiss cooldown (PR #24):** A dismissed close or swap proposal silences that market for `DISMISS_COOLDOWN_HOURS=48`. `evaluate_positions._existing_pending_ids` and `position_swap_checker.recently_dismissed_close_ids` both honor this; expiry releases the suppression.

**AI reliability stack (PR #29 + PR #30):** the bot's AI calls go local-first (Ollama `llama3.2:3b`), then API-fallback (DeepSeek `deepseek-v4-flash`). Two layers of reliability work matter for understanding logs:

- **PR #29 (parse-failure fallback):** if Ollama returns text that `_parse_response` can't extract JSON from, we now retry on DeepSeek. Pre-fix audit: 78% of Ollama responses gave up silently. Post-fix the rescue rate is logged via flags below.
- **PR #30 (constrained decoding) — pending merge:** Ollama's `format=ANALYSIS_SCHEMA` parameter forces FSM-decoded JSON output that conforms to the schema mathematically. DeepSeek leg gets `response_format={"type":"json_object"}` (syntax-only; DeepSeek doesn't expose strict-schema mode).

`logs/llm_calls.jsonl` carries one record per call with these fields (introduced in PR #29):

| Flag | Meaning |
|---|---|
| `parsed_output` | The structured fields, or `null` if both backends failed |
| `source` | `"ollama"` or `"deepseek_api"` — which one produced the result |
| `ollama_returned_none` | Transport-level failure (timeout, HTTP non-200, connection error) |
| `ollama_parse_failed` | Ollama returned text but couldn't be parsed (silent-failure mode pre-PR #29) |
| `deepseek_attempted` | We entered the DeepSeek fallback branch (Ollama failed for any reason) |
| `deepseek_returned_value` | `_call_deepseek_api` returned non-None |
| `deepseek_saved_a_parse_failure` | Headline metric: DeepSeek produced parseable output specifically after Ollama parse-failed |

Post-merge audit pattern (run 24h after a reliability PR ships):

```bash
ssh hackathon-server "cd /home/hackathon/.openclaw/workspace && python3 -c '
import json, datetime
total = parsed_ok = saved = 0
with open(\"logs/llm_calls.jsonl\") as f:
    for line in f:
        r = json.loads(line)
        ts = datetime.datetime.fromisoformat(r[\"timestamp\"])
        if (datetime.datetime.now(datetime.timezone.utc) - ts).total_seconds() > 86400: continue
        total += 1
        parsed_ok += int(r.get(\"parsed_output\") is not None)
        saved += int(r.get(\"deepseek_saved_a_parse_failure\") or False)
print(f\"24h: {parsed_ok}/{total} parsed ({parsed_ok/total*100:.1f}%), DeepSeek rescues: {saved}\")
'"
```

### Evaluator HOLD/CLOSE reasons (`:30` cron, `scripts/evaluate_positions.py:classify_position`)

Applied **per OPEN market aggregate**. Reason string lands in the Telegram CTA text.

| Reason | Meaning | Action |
|---|---|---|
| `awaiting_resolve` | `is_resolved_on_api=True` — resolve_positions handles it next | HOLD |
| `no_reprice` | No `current_unrealised_pnl` stamped yet (Phase 2.2 hasn't touched it) | HOLD |
| `stale_reprice` | Last reprice > `REPRICE_STALE_HOURS` (2h) — old data, don't score | HOLD |
| `too_new` | Youngest leg < `MIN_HOLD_HOURS` (2h) — let it settle first (PR #21) | HOLD |
| `score_above_threshold` | position_score ≥ -0.50 | HOLD |
| `pnl=..., age=..., [long_horizon_penalty]` | position_score < -0.50 | **CLOSE** |

### Stale-detector transitions (`13:00` daily, `scripts/detect_stale_positions.py:classify_stale`)

Applied **per OPEN market**. Thresholds measured from `closeTime`.

| Transition | When | Terminal? |
|---|---|---|
| `SKIP_NOT_CLOSED` | `now < closeTime` | — (normal OPEN) |
| `SKIP_GRACE` | `0 ≤ now-closeTime < 48h` | — (creators often late) |
| `STRANDED` (auto) | `48h ≤ now-closeTime < 90d`, not resolved | ✅ reversible if market resolves later |
| `ABANDON` (propose) | `now-closeTime ≥ 90d`, still not resolved | → `ABANDONED` terminal write-off on approval |
| `SKIP_RESOLVED` | `isResolved=True` | → let resolve_positions handle |

### `era` field on `bet_outcomes` — what drives weights?

| `era` | Written by | Counts toward live weights? |
|---|---|---|
| `post_ev_fix` | `resolve_market*` on post-2026-04-07 trades | ✅ yes (min_samples=10) |
| `early_close` | `close_position_early` (Phase 2.3 approvals) | ❌ audit only |
| `write_off` | `mark_market_abandoned` (Phase 2.4 approvals) | ❌ audit only |
| `pre_ev_fix` | Backfilled on legacy rows with NULL `estimated_ev` | ❌ excluded |

---

## 3. Walkthrough — Four Trades

One accepted. One rejected (pattern). One closed early. One fully resolved. For each, the "why did this happen?" drill.

---

### 3.1 Trade 1 — Accepted: the Cameroon VP trade (`llt8uhPPRA`, #37)

**The trade** (pulled from live state):

```
market_id:  llt8uhPPRA
trade_id:   37
ts:         2026-04-21T02:20:02
question:   "Will President Paul Biya of Cameroon appoint a Vice President before Apr 30?"
outcome:    NO @ 0.33968
class:      medium
strategies: ['probability_direction']
est_ev:    -0.078     ← NEGATIVE
ai_conf:    0.0       ← AI didn't run
```

This is a really interesting one to trace. Let me walk you through what happened, then drill the "why?" at each step.

#### The path this trade took

1. **`:00` — research**: Manifold API returned ~100 live markets. `auto_research.py` filtered them through:
   - `skipped_resolved` (already settled)
   - `skipped_low_liquidity` (< MIN_LIQUIDITY=$200)
   - `skipped_stale` (>72h since last bet)
   - `skipped_noise` (structurally random markets)
   - **`skipped_unverifiable`** (Will I... / Will my... — the filter we shipped in PR #19)
   - **`skipped_thin_other_momentum`** (category=other + only probability_direction + <3 bettors, PR #19+20)

   Cameroon survived. ✓

2. **Strategies scored it**. Only `probability_direction` fired (momentum — the market had been moving). Mean reversion didn't, probability_bias didn't, creator_disagreement didn't, thin_market didn't. That's the minimum bar: 1 signal.

3. **AI candidate selection**: top-3 by confidence went to Ollama/DeepSeek for deeper analysis. Cameroon wasn't in the top 3 this hour, so `ai_status=not_run`, `ai_confidence=0.0`. Recommendation written with stat confidence only.

4. **Recommendation built**: `{recommendation: "NO", confidence: 0.74 (say), strategies: ['probability_direction'], estimated_ev: computed-by-stat-fallback, ai_status: 'not_run', position_class: 'medium'}`. Written to `market_research.json`.

5. **`:20` — trader**: `auto_trader.py` loads `market_research.json`, walks recommendations. For each:
   - `_trade_rejection_reason(market_id, rec)` — run all gates
   - Cameroon passed: no AI veto, confidence ≥ 0.65, no existing open, global cap OK (6/10), **medium cap OK** (was 2/3, this would make 3/3), liquidity OK, unverifiable filter OK.
   - Kelly sizing at $5 (MAX_BET_AMOUNT throttle).
   - `place_paper_bet` → state file updated.

#### Why did this happen? (the drill)

| Question | Answer |
|---|---|
| **Why did research consider this market?** | It was on Manifold's recent list, had ≥$200 liquidity, ≥1 bet in last 72h, and wasn't flagged as noise/unverifiable. |
| **Which filters did it survive?** | All six pre-strategy filters. |
| **Which strategy signal fired?** | Only `probability_direction`. Market had momentum in one direction and we rode the follow-through — our NO bet at 33.97% implies the market had been *drifting down*. |
| **Did AI approve, skip, timeout, or cache-hit?** | Not run. Wasn't in the top-3 AI candidates this hour — so no AI signal at all. `ai_status='not_run'`. |
| **Why did the trader accept it?** | Confidence cleared 0.65 floor, no higher-ranked rec blocked it, caps weren't full, no veto. |
| **What position class did it get?** | `medium` — closeTime is Apr 30, ~9 days away, so term=medium. Combined with resolvability inferred from category → `medium` position_class. |
| **What would make us close it?** | The Phase 2.3 evaluator will score it hourly: `score = unrealised_pnl − days_held × 0.05 − (medium → no long penalty)`. If it drops below -$0.50 for more than 2h (MIN_HOLD_HOURS, PR #21), we get a CLOSE proposal. |
| **When does it become learning data?** | Either at market resolution (Apr 30, goes into `bet_outcomes` with `era='post_ev_fix'` and counts toward weight updates on Monday 2026-05-04) or if closed early (`era='early_close'`, audit-only, doesn't drive weights). |

#### The uncomfortable fact (historical — fixed by PR #22 and PR #26)

**`estimated_ev = -0.078`** at the time this trade was recorded. The bot placed a trade with *negative expected value*.

**Why it was allowed back then:** the trader's gate was `confidence ≥ 0.65`, not `estimated_ev > 0`. The stat-derived EV fallback (Phase 1.2) just stamped an EV number for later auditing — it didn't block the trade.

**What changed since:**
- **PR #22** added `MIN_ESTIMATED_EV_FLOOR=0.0`. The trader now rejects any rec whose `estimated_ev_exec` (or legacy `estimated_ev`) is ≤ 0 at both pre-flight and live-recompute stages. A trade like this one would now fire `rejected.negative_ev` and never reach `place_paper_bet`.
- **PR #26** rewrote the stat-derived probability math. The old fallback set `P(YES) = confidence`, which inflated edge — a `confidence=0.65 NO` call on a 50% market produced `P(YES)=0.35` and a positive-looking EV that wasn't real. The new math anchors P(YES) to `current_prob` with a bounded edge, so the EV the gate sees is honest.

**Net:** the leg shown above is the type of trade the bot used to take and no longer takes. Phase 3's weekly EV report will, once it has data, tell us whether the new gate is too tight or too loose.

#### Now run this

```bash
ssh hackathon-server '
cd /home/hackathon/.openclaw/workspace
python3 -c "
import json
s = json.load(open(\"manifold_bot/paper_trading_state.json\"))
for mid, legs in s[\"positions\"].items():
    for l in legs:
        if l.get(\"trade_id\") == 37:
            for k in sorted(l.keys()):
                print(f\"  {k}: {l[k]}\")
"'
```

Scan the output. Match each field to a stage in the flow above. This is the leg as it sits right now — entry data from the `:20` trade, plus whatever repricings have happened since.

---

### 3.2 Trade 2 — The Rejection Pattern

Rejections are usually more interesting than acceptances. They tell you what the bot *almost* did and why it refused.

Last three `:20` runs on the sandbox (from `data/trader_counters.jsonl`):

```
06:20  position_class_full: 4   existing_open: 3   low_confidence: 2
07:20  position_class_full: 4   existing_open: 3   low_confidence: 2
08:20  position_class_full: 4   low_confidence: 2   existing_open: 2
```

**`position_class_full`** is the most common rejection right now. Let's drill it.

#### What's happening

The book currently holds:
- `short`: 1/5 (4 slots open)
- `medium`: 3/3 (FULL)
- `long_or_uncertain`: 2/2 (FULL)

Every `:20`, ~10–15 recommendations make the menu. If they're mostly `medium` or `long_or_uncertain`, the trader rejects them against the closed slot.

#### Why did this happen? (the drill)

| Question | Answer |
|---|---|
| **Why did research consider these markets?** | Same reasons as Cameroon — they're valid, liquid, tradable. |
| **Which filters did they survive at research stage?** | All of them — they made the menu. Rejection is a *trader*-stage event, not research. |
| **Which strategy signal fired?** | Various. Irrelevant to the rejection — the trader doesn't look at strategies when gating class. |
| **Did AI approve, skip, timeout, or cache-hit?** | Mostly cache-hit (these markets have been seen before). Again, irrelevant to class rejection. |
| **Why did trader reject?** | Their `position_class` was `medium` or `long_or_uncertain`, and that bucket was at cap. Trader returned reason `position_class_full`. |
| **What position class did they get?** | Whatever `classify_position` said — based on `close_time_ms` and category. |
| **What would unblock them?** | A medium/long position has to resolve, be closed early (Phase 2.3), or be written off (Phase 2.4). Then the slot frees. |
| **When does this become learning data?** | It doesn't — rejections aren't trades. But the counter IS logged in `data/trader_counters.jsonl` and surfaces on the `/pipeline` dashboard, so you can see *how much signal the bot is leaving on the table* due to slot constraints. |

#### Two distinct rejection types worth understanding

- **`position_class_full`** (current pattern): the book is structurally lopsided. Phase 3 capital epoch was supposed to fix this by closing 3 stale longs, but the `:20` trader has already rebuilt medium+long back to cap on new trades.
- **`existing_open`** (also common): the recommendation is for a market we already have an open leg on. Trader refuses to stack — uses the swap mechanism instead (`position_swap_checker.py` runs inline after research).

#### Now run this

```bash
ssh hackathon-server '
cd /home/hackathon/.openclaw/workspace
echo "=== Last 5 trader runs: rejection breakdown ==="
tail -5 data/trader_counters.jsonl | python3 -c "
import sys, json
for line in sys.stdin:
    d = json.loads(line)
    print(d.get(\"timestamp\"))
    for r, n in sorted(d.get(\"rejected\", {}).items(), key=lambda x: -x[1]):
        if n > 0:
            print(f\"  {r:20s} {n}\")
    print(f\"  passed_filter        {d.get(\"passed_filter\", 0)}\")
    print(f\"  trades_executed      {d.get(\"trades_executed\", 0)}\")
    print()
"'
```

As of 2026-04-21 the pattern was ~4 rejections/run, 0–1 trades/run — the bot straining against its own slot structure after the Phase 3 capital epoch. Your values may differ as the book churns; compare to today's `data/trader_counters.jsonl` tail.

---

### 3.3 Trade 3 — Closed: `CEIUnpQL26`, the vanity trade

This one's pedagogically rich because it hits Phase 2.3 (evaluator), Phase 2.4 (stale classifier — though it didn't fire because of closure), the unverifiable filter (PR #19), and the re-entry loop (the filter needed to be deployed before it stopped re-opening).

**The trade** (two legs, two closes):

```
leg #33: placed 2026-04-20T09:20, YES @ 0.7034, $5, confidence=0.74
         AI reasoning: "pet content for pets gaining popularity… underestimation of my ability to complete pressing tasks"
         ← hallucinated nonsense
         question: "Will I complete all my pressing tasks this week?"

leg #34: placed 2026-04-20T10:20, YES @ 0.7034, $5, confidence=0.65, estimated_ev=-0.38
         ← negative EV!
         bot re-entered because filter wasn't deployed yet
```

#### The sequence of events

1. **Day 1 :20 cron**: bot picks `CEIUnpQL26` from research menu. Passes confidence, class cap, liquidity. AI analyzed → confidently hallucinated. Trade placed.
2. **Operator spots it**, says "this is garbage." Closes manually via `close_position_early(market_id, live_prob)`. P&L = 0.00 (entry ≈ current). Row written to `bet_outcomes` with `era='early_close'`.
3. **Next :20 cron**: bot sees the same market on the research menu again, same filters still pass, same logic → opens trade #34. This time AI didn't run → `ai_confidence=0`, `estimated_ev=-0.38` (negative).
4. **Operator closes again**. Same flat P&L.
5. **Root cause fixed**: PR #19 ships the unverifiable-market filter — `"Will I..."` now hard-blocks at research layer. PR #20 adds the punctuation + inference-fallback edge cases.
6. **After merge + sandbox deploy**: bot's research run skips the market, trader never sees it. Loop broken.

#### Why did this happen? (the drill)

| Question | Answer |
|---|---|
| **Why did research consider this market?** | Category=`other`, but passed the pre-filter chain before PR #19 landed. The `resolvability=low` classification happened, but resolvability is a *soft* label — it doesn't block trades, just biases class. |
| **Which filters did it survive?** | All of them pre-PR #19. Nothing was watching for "Will I..." pattern. |
| **Which strategy signal fired?** | `probability_direction`. The market had 2 bettors and was drifting — technically momentum. |
| **Did AI approve, skip, timeout, or cache-hit?** | Approved with hallucinated reasoning on leg #33. Didn't run at all on leg #34 (confidence too low to make top-3). |
| **Why did the trader accept it?** | All gates cleared. The `market_unverifiable` counter existed in code but was never wired to a check. |
| **What position class did it get?** | `short` — resolution deadline was "this week", so closeTime ~7 days away. |
| **What would make us close it?** | Evaluator would have scored it: `0 − 0 × 0.05 − 0 (short) = 0.00` → above -0.50 threshold → HOLD. *The Phase 2.3 evaluator would never have closed this on its own*. Only operator intervention + filter deployment killed the loop. |
| **When does it become learning data?** | Both closes went into `bet_outcomes` with `era='early_close'`. Since `early_close` is audit-only (doesn't drive weights), these trades **don't teach the learning loop anything**. The filter catches it upstream now — which is the right place to catch nonsense. |

#### The lesson

The evaluator (Phase 2.3) is not the first line of defense against bad trades. It's for recycling *legitimate* trades that aren't paying off. For *structurally wrong* trades, the fix is always upstream — at research or trader pre-filter. When you see a trade that makes you say "how did this ever get opened," the question to ask is not "why didn't the evaluator close it?" but "which pre-filter should have rejected it?"

#### Now run this

```bash
ssh hackathon-server '
cd /home/hackathon/.openclaw/workspace
echo "=== Both closes in bet_outcomes ==="
sqlite3 data/calibration.db "
  SELECT market_resolution, actual_pnl, era, strategies, resolved_at
  FROM bet_outcomes
  WHERE market_id = \"CEIUnpQL26\"
  ORDER BY resolved_at
"
echo ""
echo "=== Verify filter is live ==="
python3 -c "
from manifold_bot.strategies import is_unverifiable_market
q = \"Will I complete all my pressing tasks this week?\"
print(\"is_unverifiable_market:\", is_unverifiable_market({\"question\": q}))
"'
```

Both rows should show `era=early_close` with pnl=0.00, and the filter check should return `True`. That's the full lifecycle — opened, closed, filtered going forward.

---

### 3.4 Trade 4 — Resolved: `Ic9p2lCd05`, the boring sports loss

The previous three trades are all unusual: structurally bad (CEIUnpQL26), rejected-by-pattern, or still open (Cameroon). You need at least one *boring* example: a trade that was placed normally, resolved normally, and landed in `bet_outcomes` as a single clean row. This is what 95% of the bot's output looks like when the system is working.

**The trade** (from `bet_outcomes`, resolved 2026-04-19 20:10):

```
market_id:       Ic9p2lCd05
recommendation:  NO @ 48%
amount:          $5
category:        sports
strategies:      ['probability_bias']
estimated_ev:    +$3.08     ← EV said "good bet"
ai_confidence:   0.0        ← AI didn't vote
market_resolution: YES      ← market resolved YES, our NO LOSES
actual_pnl:      -$5.00
era:             post_ev_fix   ← counts toward Monday's learning loop
```

#### The path this trade took

1. **`:00` research**: sports market, had ≥200 liquidity, passed all filters. `probability_bias` fired (market's probability was ~48% and the strategy identified a bias). Written to `market_research.json` with `recommendation: NO, confidence: ≥0.65, estimated_ev: +3.08`.
2. **`:20` trader**: `_trade_rejection_reason` returned `None`. No AI veto (AI didn't vote), confidence cleared floor, no existing open, class + category caps OK, Kelly sized at $5 (MAX_BET_AMOUNT). Placed via `place_paper_bet`.
3. **Lifecycle**: `:05` reprice each hour updated `current_unrealised_pnl` as the market drifted. **Evaluator left it alone** — sports is `medium` class (no long-horizon penalty), and a small unrealised loss wouldn't trip `-0.50` threshold.
4. **`:10` resolve**: at some point Manifold's API flagged `isResolved=True, resolution=YES`. `auto_resolve_markets` picked it up, called `resolve_market('Ic9p2lCd05', 'YES')`. Our NO bet lost. `status → LOSE`, `profit → -5.0`.
5. **`_write_bet_outcome`**: wrote the row with `era='post_ev_fix'`, `ev_error = estimated_ev - actual_pnl = 3.08 - (-5.0) = +$8.08` (huge overestimate — said "I expected +3, got -5").

#### Why did this happen? (the drill)

| Question | Answer |
|---|---|
| **Why did research consider this?** | Sports market, active, liquid, fits the `probability_bias` strategy shape. |
| **Which filters did it survive?** | All research filters. |
| **Which strategy signal fired?** | Only `probability_bias` — stat thought the market was mispricing, and biased toward NO at 48%. |
| **Did AI approve?** | AI didn't vote (`ai_confidence=0.0`, `ai_status` not stamped — predates that field). No AI contribution either way. |
| **Why did the trader accept?** | All gates cleared. `estimated_ev=+3.08` meant the stat-derived expected value was positive too. On paper, a good bet. |
| **What position class did it get?** | `medium` (sports market, closeTime ~days to weeks away). No long-horizon penalty to worry about. |
| **What would make us close it?** | The evaluator ran `:30` every hour. Given small unrealised fluctuations, score stayed above -0.50 → HOLD every time. This is exactly the case where the evaluator should stay out of the way. |
| **When did it become learning data?** | The moment `resolve_market` wrote the `bet_outcomes` row with `era='post_ev_fix'`. From then on, it's counted in the weekly EV audit (next Monday 2026-04-27) and in strategy weight computation (if `probability_bias` has ≥10 similar rows). |

#### The important teaching moment: `ev_error = +$8.08`

The stat-derived EV said we'd gain ~$3 on this bet. We actually lost $5. That's an `ev_error` of +$8.08 — EV was too optimistic by about 2.5× a whole bet size.

**What this means for Phase 3**:

- If *most* `probability_bias` trades have strongly-positive `ev_error` (EV systematically overpredicts), then `compute_strategy_weights.py` will penalise that strategy's weight on Monday — it'll drop toward 0.5 or so, which reduces its confidence in future research runs.
- Conversely, if `ev_error` is *consistently zero-ish* across 10+ trades of a strategy, its weight stays near 1.0 (it's accurate) or moves above 1.0 (it's under-confident).

**The system learns the gap between expected and realised EV per strategy.** That's the whole point of `bet_outcomes` and `era='post_ev_fix'`. This one trade, by itself, tells us nothing — but 10 of them across `probability_bias` is signal.

#### Now run this

```bash
🟢 READ
ssh hackathon-server "
cd /home/hackathon/.openclaw/workspace
echo '=== The row in bet_outcomes ==='
sqlite3 data/calibration.db 'SELECT market_id, our_recommendation, market_resolution, estimated_ev, actual_pnl, ev_error, era, strategies FROM bet_outcomes WHERE market_id = \"Ic9p2lCd05\"'

echo
echo '=== All post_ev_fix rows, grouped by strategy ==='
sqlite3 data/calibration.db 'SELECT strategies, COUNT(*), ROUND(AVG(ev_error), 2) as avg_err FROM bet_outcomes WHERE era = \"post_ev_fix\" AND ev_error IS NOT NULL GROUP BY strategies ORDER BY COUNT(*) DESC'
"
```

The second query is what `compute_strategy_weights.py` does on Monday — groups by strategy, averages `ev_error`, and updates weights if the sample size ≥ 10. As of 2026-04-21 most strategies have <10 samples so weights stay at 1.0. That's the bottleneck — not the code, just data volume.

---

## 4. What Happens Monday — Phase 3 First Activation

Sunday cron pipeline (already running each week, just nothing interesting happened yet):

```
Sun 02:00  harvest_resolved.py        → adds resolved markets to calibration.db
Sun 02:30  audit_resolved_markets.py  → cross-checks quality
Sun 03:00  reconstruct_snapshots.py   → replays prob at T-7/14/30 for resolved markets
Sun 03:30  backtest_from_snapshots.py → simulates strategies against the corpus

Mon 07:00  weekly_ev_report.py         → EV accuracy → Telegram
Mon 07:30  compute_strategy_weights.py → update data/strategy_weights.json
```

### What to expect this coming Monday (2026-04-27)

- **`bet_outcomes` row count**: probably 12–15 useful `post_ev_fix` rows, vs. the current 10. Short-term trades opened this week resolve inside the week.
- **Strategy weights**: likely move slightly off 1.00 for the 1–2 strategies that hit min_samples=10. Rest stay at 1.00 (gated out).
- **EV report in Telegram**: first real "how accurate were our EV estimates" number. Could be any sign.

### What to read *on Monday* to diagnose

1. The Telegram EV report — absolute accuracy number, per-strategy breakdown.
2. `data/strategy_weights.json` diff against previous week — did anything move?
3. `/positions` dashboard — did the book composition change as trades resolved?
4. `data/trader_counters.jsonl` — did the rejection pattern change?

### What would tell you learning is "working"

- At least one strategy weight < 0.95 or > 1.05 (the bot learned something)
- EV accuracy bounded roughly in (−$0.50, +$0.50) per trade (means EV estimates aren't wildly wrong)
- `by_category` accuracy data populated for ≥2 categories (crypto/politics/etc.)

### What would tell you Phase 3 needs more iteration

- All weights still 1.00 (min_samples gate never met)
- EV accuracy drifting strongly negative (bot's EV estimates are too optimistic — overpredicts edge)
- Rejection pattern unchanged (bot's decisions don't incorporate new signal)

Either outcome is OK for a first activation — you're proving the pipeline runs. Rich signal comes from accumulation.

---

## 5. Operator-Brain — Terse Reference

Once you've done the walkthrough, this is the sheet to keep open.

### Daily flow (when paying attention)

```
:00 → look at /pipeline for research counters
:20 → look at /overview for whether new trades were placed
:30 → check Telegram for CLOSE proposals; approve or dismiss
13:00 (daily) → check Telegram for ABANDON proposals
19:00 → daily summary — spot-check balance vs. yesterday
```

### Deciding on a close proposal (Phase 2.3)

```
Reply to the Telegram message:
  approve close N  → realises P&L at live AMM price
  dismiss close N  → lets it run

Rules of thumb:
  - pnl > +$0.20 and long_or_uncertain     → approve (realise + free slot)
  - pnl < -$1.00 and old (>10d)            → approve (stop the bleed)
  - pnl ≈ 0.00 and <2h old                 → dismiss (post-PR #21 this won't fire)
  - pnl > 0, short/medium, score > -0.50   → evaluator won't propose these anyway
```

### Deciding on an abandon proposal (Phase 2.4)

```
These fire at 90d past closeTime, no resolution. Only option is:
  approve abandon N  → books full loss, moves to era=write_off
  dismiss abandon N  → wait another 72h for next proposal

Rule: approve. By 90d past close, the capital is gone. Booking the loss
is honest accounting.
```

### When you see a weird trade

1. **State file first**: `ssh hackathon-server "cd workspace && python3 -c 'import json; ...'"` — print the leg.
2. **Research menu**: `cat market_research.json | jq '.latest.recommendations[] | select(.market_id==\"...\")'` — was it even on the menu? With what signals?
3. **Trader log**: `tail -100 /tmp/trader.log` — what did the trader say at decision time?
4. **bet_outcomes**: if closed/resolved, `sqlite3 data/calibration.db "SELECT * FROM bet_outcomes WHERE market_id='...'"`

### Known structural gaps (as of 2026-04-21)

- Only `post_ev_fix` era drives live weights (PR? not filed — by design)
- `_infer_market_category` keyword heuristic misses Fed / MLB teams / etc. — upstream taxonomy
- Trader accepts negative-EV trades if confidence ≥ 0.65 (open question — real or not?)
- Class-cap distribution hand-tuned (short=5, medium=3, long_or_uncertain=2) — not data-driven yet
- AI prompt has no refusal instruction — defense-in-depth improvement, not shipped

---

## 6. What You Now Know

If you did the runs: you've seen the research counters update, walked a real trade's data through the state file, watched the rejection pattern in the trader log, traced a genuine resolution into `bet_outcomes`, and confirmed the filter deployment on sandbox. That's the whole pipeline, end-to-end, with your own eyes.

You don't need to memorize every function name. You need to know:
- **Where data flows** (the system map)
- **What each artifact means** (data contracts)
- **Why the bot made each decision** (the drill)

Everything else is reference — lookable when you need it, not load-bearing in your head.

---

## 7. Debug Worksheet — "why did this trade happen?"

When you spot something weird in the book, fill this in before diving into code. Most of the time the worksheet itself points at the answer.

Copy this into a scratch file or a chat and fill it in against the specific market.

```markdown
## Debug Worksheet — <market_id>

### 1. Research record (if still on menu)
🟢 READ
$ cat market_research.json | python3 -c "
import json,sys
d=json.load(sys.stdin).get('latest',{})
m=[r for r in d.get('recommendations',[]) if r.get('market_id')=='<market_id>']
print(json.dumps(m[0] if m else {}, indent=2, default=str))
"

- [ ] Was it on the menu? Y/N
- [ ] Which strategies fired?
- [ ] What was the recommendation + confidence?
- [ ] What was estimated_ev?
- [ ] What was ai_status?
- [ ] What position_class was stamped?

### 2. Trader gate result
🟢 READ
Look at /tmp/trader.log around the relevant :20 run:
$ ssh hackathon-server "tail -200 /tmp/trader.log | grep -A3 '<market_id>'"

- [ ] Was it passed or rejected?
- [ ] If rejected, which reason string? (see §2.5 trader table)
- [ ] If passed, what was the Kelly sizing?

### 3. State leg (if accepted)
🟢 READ
$ ssh hackathon-server "cd /home/hackathon/.openclaw/workspace && python3 -c '
import json
s=json.load(open(\"manifold_bot/paper_trading_state.json\"))
for mid,lgs in s[\"positions\"].items():
    for l in lgs:
        if l.get(\"market_id\")==\"<market_id>\":
            print(json.dumps(l, indent=2, default=str))
'"

- [ ] What's the current status? (OPEN / WIN / LOSE / CLOSED_EARLY / STRANDED / ABANDONED)
- [ ] entry_probability vs current_probability?
- [ ] current_unrealised_pnl?
- [ ] last_repriced_at — how fresh?
- [ ] position_class — current, or obsolete 4-bucket value?

### 4. Snapshot rows (P&L trajectory)
🟢 READ
$ ssh hackathon-server "cd /home/hackathon/.openclaw/workspace && sqlite3 data/calibration.db \
  'SELECT snapshot_at, current_probability, unrealised_pnl, api_error FROM position_snapshots WHERE market_id=\"<market_id>\" ORDER BY snapshot_at'"

- [ ] How many snapshots?
- [ ] P&L trend (up/down/flat)?
- [ ] Any api_error=1 rows? (Phase 2.2 fetch failures)

### 5. Pending proposals (if any)
🟢 READ
$ ssh hackathon-server "cd /home/hackathon/.openclaw/workspace && cat pending_closes.json pending_abandons.json pending_swaps.json 2>/dev/null | python3 -c '
import json,sys
for line in sys.stdin:
    pass  # simple scan; better: jq the files individually
'"
Better:
$ ssh hackathon-server "cd /home/hackathon/.openclaw/workspace && \
  python3 -c 'import json; [print(p) for p in json.load(open(\"pending_closes.json\")) if p.get(\"market_id\") == \"<market_id>\"]' 2>/dev/null"

- [ ] Any proposal live?
- [ ] If expired/dismissed: why?

### 6. Outcome row (if resolved/closed)
🟢 READ
$ ssh hackathon-server "cd /home/hackathon/.openclaw/workspace && sqlite3 data/calibration.db \
  'SELECT * FROM bet_outcomes WHERE market_id=\"<market_id>\" ORDER BY resolved_at'"

- [ ] market_resolution?
- [ ] actual_pnl vs estimated_ev → ev_error?
- [ ] era (post_ev_fix / early_close / write_off / pre_ev_fix)?

### 7. Conclusion
- [ ] Which gate made this happen (or not happen)?
- [ ] Is the observed behaviour correct per §2.5 of this doc?
- [ ] If incorrect: is it a config issue, a classifier issue, or a real bug?
```

**When to use this**: any trade you don't immediately understand. Don't look at code first. The data in 1–6 usually tells the whole story; code only helps when the data contradicts §2.5.

---

*Written 2026-04-21. If the pipeline changes, this doc rots. Run §0.2 before each session to confirm you're in sync. If the decision-gate cheat sheet in §2.5 or the trade-walk details drift from current code, patch them — they're the highest-value part for a working operator.*
