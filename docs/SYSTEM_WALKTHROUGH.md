# SYSTEM_WALKTHROUGH — Operator's Brain

> **Purpose**: this is not a reference manual. It's the doc you read *once*, running commands on the sandbox alongside, so you build a mental model of how the bot actually decides things.
>
> **How to use**: start at §1, work down. Every section ends with a `now run this` block — actually run it on the sandbox. Don't skip the runs, they're what make it stick.
>
> **Estimated time**: 90–120 minutes, ideally in one sitting.

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

You should see `:00 research`, `:05 reprice`, `:10 resolve`, `:20 trade`, `:30 evaluate`, `13:00 stale detection`, plus the weekly Sunday/Monday jobs.

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

You should see `post_ev_fix: 10` rows (right at the min-sample gate) and every strategy weight = 1.0. That's "pipeline works but no signal moved yet" — the state we want to flip on Monday.

---

## 3. Walkthrough — Three Trades

One accepted. One rejected (pattern). One closed. For each, the "why did this happen?" drill.

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

#### The uncomfortable fact

**`estimated_ev = -0.078`.** The bot placed a trade with *negative expected value*. Why?

Because the trader's gate is `confidence ≥ 0.65`, not `estimated_ev > 0`. The stat-derived EV fallback (Phase 1.2) just stamps an EV number for later auditing — it doesn't block the trade. So a high-confidence + negative-EV trade still fires.

**Is this a bug?** Not really — it's a deliberate choice. Over many trades, if the confidence signal is meaningful, slightly-negative-EV trades average out to positive EV. But it IS something you should file as a real question for Phase 3: **does confidence actually track EV in our data?** If the weekly report shows confidence > 0.65 trades are systematically negative-EV, we have a calibration problem.

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

You'll see the same pattern: 4-ish rejections per run, maybe 0-1 trades executed. That's the bot straining against its own slot structure.

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

If you did the runs: you've seen the research counters update, walked a real trade's data through the state file, watched the rejection pattern in the trader log, and confirmed the filter deployment on sandbox. That's the whole pipeline, end-to-end, with your own eyes.

You don't need to memorize every function name. You need to know:
- **Where data flows** (the system map)
- **What each artifact means** (data contracts)
- **Why the bot made each decision** (the drill)

Everything else is reference — lookable when you need it, not load-bearing in your head.

---

*Written 2026-04-21. If the pipeline changes, this doc rots — check the last-modified date and compare to the actual cron output before trusting it.*
