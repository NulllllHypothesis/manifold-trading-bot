# Manifold Bot Operator Dashboard

A Next.js dashboard for monitoring and debugging the Manifold prediction market paper trading bot. This is not a toy metrics page -- it reads every data file the bot produces and surfaces the exact operational state: what the bot is doing, why it's stuck, and whether it's learning.

## Quick Start

```bash
cd dashboard
pnpm install

# Option 1: Run against local workspace data
pnpm dev
# Dashboard at http://localhost:3000

# Option 2: Run against live sandbox data (recommended)
pnpm dev:sandbox
# Syncs 18 files from the sandbox server, then starts the dashboard
```

### Prerequisites

- **Node.js 20+** and **pnpm** installed
- For sandbox mode: SSH access to the sandbox server. Set up one of:
  - SSH key: `ssh-copy-id hackathon-server` (one-time, recommended)
  - Password: add `SANDBOX_PASS=yourpassword` and `SANDBOX_IP=x.x.x.x` to the workspace `.env`

### Keeping Data Fresh

```bash
# One-shot sync (pull latest from sandbox without starting dashboard)
pnpm sync

# Auto-sync every 60 seconds in a separate terminal
pnpm sync:watch

# Check if sandbox connection works
pnpm check
```

---

## Architecture

```
Browser  <-->  Next.js App Router (Server Components)
                    |
              lib/data/ adapters (server-only)
                    |
              Bot workspace files (JSON, JSONL, SQLite, .py configs)
                    |
              Either: local workspace (../) or sandbox snapshot (/tmp/manifold-sandbox-snapshot)
```

**Key design decisions:**

- **Server Components only** -- all data reads happen server-side. No API layer needed for v1.
- **Zod-validated schemas** -- every file shape is validated at read time. Malformed data fails loudly.
- **`WORKSPACE_ROOT` env var** -- the single lever that switches between local and sandbox data. Unset = repo root. Set = snapshot directory.
- **`botId` parameter** -- every adapter takes `botId` (hardcoded `'main'` for v1). Designed for multi-tenant expansion.
- **Read-only** -- the dashboard never writes to bot files. Trade execution stays in the cron jobs.

### Tech Stack

- Next.js 16 (Turbopack) + React 19 + TypeScript
- shadcn/ui + Tailwind v4 (dark mode default)
- Recharts for charts (equity curve, funnel, calibration)
- better-sqlite3 for SQLite reads
- Zod for schema validation

---

## Pages

The dashboard has **15 pages** organized into two sections: **Command Center** (what's happening right now) and **Management Console** (is the system healthy and improving).

### Command Center

#### Overview (`/overview`)

The home page. Shows at a glance whether the bot is healthy or in trouble.

- **6 KPI cards**: Balance, Total P&L, Today's P&L, Open/Max positions (red when full), Win rate (with W/L/N breakdown), Pipeline conversion rate (24h)
- **Alerts list**: Every operational issue in one place. Fires for: kill switch active, book full, cash critically low, days since last trade, stale positions (>7 days), no resolutions in 3 days, AI no_result rate >40%, pipeline conversion <1%, strategy weights all at defaults, missing/stale data files, pending swap proposals
- **Recent activity**: Last 10 resolved trades with WIN/LOSE badges and P&L

The alert count shown here is the **single source of truth** -- the same `computeAlerts()` function drives the status bar, sidebar badge, and this list.

#### Opportunities (`/opportunities`)

The blocked opportunity debugger. Answers: "the bot found ideas -- why can't it trade them?"

- **Alert banners**: Book full warning (with rejection count), AI veto warning (with skip/timeout count)
- **5 KPI cards**: Tradable vs Blocked count, Book occupancy, AI vetoed count, Category slot breakdown
- **Rejection breakdown**: Badge summary of why recommendations are blocked (ai_veto, max_positions, category_cap, low_confidence, existing_open, liquidity)
- **Recommendations table** (sorted tradable-first):
  - Question, Category (with slot info like `politics: 1/3`), Probability, Pick (YES/NO), Confidence, EV exec, Priority boost (from volume spike), AI status (SKIP/NO RESULT/AGREE/DISAGREE), AI source (ollama/deepseek), AI reasoning snippet, Liquidity ($), Unique bettors, Would-be rejection reason, Strategies

Every recommendation shows what would happen if the trader tried to execute it, using the same risk gates as the real trader (including adaptive category caps from `category_accuracy.json`).

#### Portfolio (`/portfolio`)

The current book. Shows what the bot owns and whether positions are getting stuck.

- **Alert banners**: Book full, stale positions (>7 days)
- **Summary strip**: Open count, exposure, cash (% of initial), resolved count, realized P&L
- **Category slot usage table**: Per-category open count vs effective cap (adaptive -- reads `category_accuracy.json`). "other" is uncapped (cap=10). Legacy positions without category metadata shown separately.
- **Book health panel**: Slots used, cash remaining, oldest position age, stale count, last trade timestamp
- **Open positions table** (oldest first): Market question, Category, Side (YES/NO), Stake ($), Entry probability, Profit if win, Days open (amber highlight + STALE tag at 7+), Strategies
- **Recent resolutions table**: Market, Side, Stake, Result (WIN/LOSE/N), P&L, Strategies, Resolved date

#### Swaps (`/swaps`)

Position swap proposals -- replacing losers with better opportunities.

- **Pending swaps**: Close-side position (entry prob, current prob, unrealised P&L) paired with open-side opportunity (recommendation, confidence, EV, strategies)
- **Swap history**: Past proposals with status (pending/approved/executed/expired/dismissed), timestamps

#### Performance (`/performance`)

Historical trading analytics.

- **Summary strip**: Date window, resolved trade count with W/L/N breakdown
- **6 KPI cards**: Total P&L, Win rate, Profit factor, Avg trade, Max drawdown, Best/worst trade
- **Equity curve**: Line chart of cumulative P&L over time
- **Daily P&L**: Bar chart color-coded by gain/loss
- **By category**: Horizontal bar chart centered on zero, with trade count and win rate
- **By strategy**: Same format, showing which strategies are making/losing money

### Management Console

#### Pipeline Health (`/pipeline`)

The research-to-execution funnel. Where decisions get filtered out.

- **Alert banners**: AI no_result acting as veto (with %, code references), max_positions as dominant rejection (only when actually #1)
- **Summary strip**: Window (24h), research/trader run counts, conversion rate
- **Funnel chart**: Horizontal bar -- Markets fetched -> Final recommendations -> AI eligible -> AI analyzed -> Trades executed
- **AI analysis flow**: Per-stage bar -- Eligible, Cooled down (amber when high), Analyzed, Agree, Disagree, Skip, No result. Shows success rate and cooldown percentage.
- **Trader rejection reasons**: Bar chart of why recommendations fail risk gates
- **Market pre-filter**: Skipped counts (already resolved, low liquidity, stale, noise patterns) + "survived to strategy evaluation" total
- **Strategy interaction**: Thin-market confirmations, tied votes dropped, passed filter count, top EV at last execution (market IDs + EV values)
- **Family dedup distribution**: Momentum vs contrarian vs fundamental winner counts
- **Final recs by category**: Category distribution of output recommendations
- **Confidence distribution**: Histogram of current recommendation confidence values
- **Strategy mix**: Badge summary of strategy combinations in final output
- **Schema version**: v2 (AI pass ran) or v1 (AI pass may have failed), with health indicator
- **No active signals**: Markets where zero strategies fired (coverage gap metric)

#### Automation (`/automation`)

Cron job health. Is the bot's heart still beating?

- **4 KPI cards**: Total jobs, Healthy, Overdue, Unknown
- **OS crontab table** (live from server when synced, static fallback otherwise): Per-job status (healthy/overdue/unknown), description, cron schedule + human-readable interval, next run time, output file freshness (age + size per tracked file). Badge shows "live from server" or "static fallback".
- **OpenClaw cron table**: All OpenClaw jobs with name, schedule, enabled/disabled status, message snippet. Shows which jobs are still enabled (weekly news summary, monthly prune) vs disabled (migrated trading jobs). Badge shows "live from server" or "not synced".

Both tables are populated from files synced via `pnpm sync` (`crontab -l` output and `~/.openclaw/cron/jobs.json`).

#### Strategy Lab (`/strategy-lab`)

Strategy weights, fire rates, and whether each strategy is contributing.

- **Metadata warning**: Fires when <50% of resolved trades have strategy metadata (honest about data gaps)
- **Weights metadata**: Generation timestamp, sample gate threshold, total samples
- **Strategy table**: Strategy name, Current weight (highlighted when diverged from 1.0), Samples progress bar toward activation gate (visual fill + N/threshold), Fire count + per-run average, Trade record (W/L), Win rate (color-coded), P&L, Status badge (adapted/gate met/collecting/unweighted)
- **Per-category weights** (when available): Heatmap-style table showing weight + accuracy + sample count per (category, strategy) cell

#### Calibration (`/calibration`)

EV calibration and crowd bias. How well the crowd predicts outcomes.

- **Metadata strip**: Generation timestamp, min cell samples, source database
- **4 KPI cards**: Total samples, Categories (with reliable count), Average crowd bias (with direction), Probability bucket count
- **Calibration curve chart**: Line plot of crowd prediction vs actual YES rate. Dashed diagonal = perfect calibration. Points above = crowd underestimates.
- **Bias by bucket chart**: Bar chart of crowd bias per 10% probability bucket. Green = overestimates YES, red = underestimates.
- **Probability bucket detail table**: Bucket range, crowd midpoint, actual YES rate, bias, samples, reliable flag
- **Calibration by category table**: Category, samples, avg crowd probability, actual YES rate, bias, reliable flag

#### Learning Loop (`/learning`)

Is the feedback loop alive? The end-to-end chain from past markets to changed trading behavior.

- **Health alerts**: Weights still at defaults, too few bet outcomes, categories below sample gate, calibration/weights files stale, last backtest was dry run
- **Feedback loop pipeline**: Visual funnel showing the full chain: Resolved markets (2016) -> Usable for M3 (416) -> Backtest snapshots (267) -> Calibration categories (10) -> Calibration buckets (10) -> Bet outcomes (6) -> Weight samples (6) -> Weights diverged (YES/NO). Each stage green when healthy, amber when below threshold.
- **Last run timestamps**: Harvest, M2 audit, calibration table, backtest, strategy weights, eval report -- with expected schedule
- **Strategy weights table**: Per-strategy weight, sample progress bar toward gate, status badge (adapted/gate met/collecting)
- **Category accuracy table**: Per-category accuracy, sample count, gate status, effective cap impact (default 3 / restricted 1)
- **Eval report**: Test examples, Brier score, directional accuracy, promotion eligibility + reason
- **Backtest report**: Snapshots used, dry run flag

#### News Impact (`/news-impact`)

How news headlines affect recommendations and trade outcomes.

- **5 KPI cards**: Markets with news, Current recs with news, News agrees, News disagrees, Trades overlapping news
- **Signal direction table**: Per-market -- news direction (YES/NO/none with lean counts), recommendation direction, agreement badge, recency confidence (60-80%), headline count, trade exists badge (with outcome)
- **Recent headlines by market**: Per-market expandable list with "Sent to AI as news_context" sub-section (the exact headlines injected into the AI prompt), cached article titles/sources, "in AI prompt" badge, trade outcome badge
- **Fetch health table**: News API status (active within 3 days), cache entries (with/empty), unique markets fetched (with hit rate), date range, test entries excluded
- **Phase 1 limitations note**: Honest about what's inferred vs what would need persisted attribution payloads

#### Controls (`/controls`)

All runtime parameters across the entire codebase. Read-only reference.

- **Kill switch banner**: Red alert when `autotrader_disabled.flag` exists
- **7 parameter groups** (parsed from actual source files, not hardcoded):
  - **Risk Controls**: MAX_BET_AMOUNT, MIN_BET_AMOUNT, MIN_CONFIDENCE, MAX_POSITIONS, MAX_POSITIONS_PER_CATEGORY, MIN_LIQUIDITY, MIN_SAMPLES_PER_CATEGORY, MIN_SAMPLES_PER_STRATEGY
  - **Trading Settings**: INITIAL_BALANCE, MAX_TRADES_PER_CYCLE, MAX_POSITION_SIZE_FRACTION
  - **Research Pipeline**: WEAK_STAT_CAP, STAT_BOOST_FLOOR, NEWS_CANDIDATES, VOLUME_SPIKE_MULTIPLIER, MIN_BUCKET_SAMPLES, MIN_BIAS_TO_TRADE
  - **AI Analysis**: AI_COOLDOWN_HOURS, AI_COOLDOWN_MAX_FAILS, AI_BOOST_MARGIN, FIRST_TOKEN_TIMEOUT, PER_CHUNK_TIMEOUT, OLLAMA_MODEL, DEEPSEEK_MODEL
  - **Swap Checker**: SWAP_EXPIRY_HOURS, SWAP_MIN_MARGIN
  - **API Configuration**: MANIFOLD_API_BASE
  - **Feature Flags**: NEWS_FETCHER status
- **AI cooldown table**: Markets currently on timeout cooldown (market ID, fail count, last failure timestamp)

#### Audit (`/audit`)

Chronological feed of all bot actions.

- **Event type badges**: Research run, Trader run, Trade executed, Trade resolved (approximate date flagged), Swap proposed/executed/expired
- **Per-day grouping**: Events grouped by date with count headers
- **Event details**: Timestamp, title, detail text (markets fetched, recommendations, trades executed, P&L amounts)

Derived from existing data files (trade history, research/trader counters, swap proposals). Events are deduplicated by trade ID.

#### Diagnostics (`/diagnostics`)

Data file health check.

- **File freshness table**: All 16+ tracked data files with key, path, exists/missing, size, age, staleness threshold, fresh/stale/missing badge
- **Cron schedule reference**: Static schedule table from CLAUDE.md

### Global UI Elements

#### Status Bar (top of every page)

6 live indicators updated on every page load:

- **Bot**: ON (green) / OFF (red) / KILL (red, when `autotrader_disabled.flag` exists)
- **Research**: Time since last research run (green <75min, amber >75min, red >3h)
- **Trader**: Time since last trader run (same thresholds)
- **Swaps**: Pending swap count (amber when >0, shows only truly pending, not expired)
- **AI**: Success rate (green >60%, amber 40-60%, red <40%)
- **Alerts**: Total issue count from `computeAlerts()` -- same number as Overview page

#### Sidebar

Collapsible navigation with two sections (Command Center + Management Console). Badges on Swaps (pending count) and Alerts page link.

---

## Data Sources

The dashboard reads these files from the bot workspace:

| File | Updated by | Frequency |
|------|-----------|-----------|
| `market_research.json` | auto_research.py | Hourly |
| `manifold_bot/paper_trading_state.json` | auto_trader.py, resolve_positions.py | Hourly |
| `data/research_counters.jsonl` | auto_research.py | Hourly (append) |
| `data/trader_counters.jsonl` | auto_trader.py | Hourly (append) |
| `data/news_cache.json` | news_fetcher.py | Hourly |
| `data/ai_timeout_cooldown.json` | auto_research.py | Hourly |
| `auto_trades.json` | auto_trader.py | On trade |
| `pending_swaps.json` | position_swap_checker.py | On swap check |
| `data/strategy_weights.json` | compute_strategy_weights.py | Weekly (Monday) |
| `data/calibration_table.json` | analyze_calibration.py | Weekly (Sunday) |
| `data/category_accuracy.json` | compute_strategy_weights.py | Weekly (Monday) |
| `data/calibration.db` | harvest_resolved.py | Weekly (Sunday) |
| `data/market_snapshots.db` | auto_research.py, reconstruct_snapshots.py | Hourly + Weekly |
| `data/m2_audit.json` | audit_resolved_markets.py | Weekly (Sunday) |
| `data/eval_report.json` | (ad-hoc) | Manual |
| `data/backtest_report.json` | backtest_from_snapshots.py | Weekly (Sunday) |
| `manifold_bot/config.py` | Manual edit | On deploy |
| `automation/auto_research.py` | Manual edit | On deploy |
| `automation/auto_trader.py` | Manual edit | On deploy |
| `manifold_bot/ai_analyzer.py` | Manual edit | On deploy |
| `scripts/position_swap_checker.py` | Manual edit | On deploy |
| `manifold_bot/strategies.py` | Manual edit | On deploy |
| `autotrader_disabled.flag` | Manual create/delete | On demand |
| `data/crontab.txt` | Synced via `pnpm sync` | On sync |
| `data/openclaw_jobs.json` | Synced via `pnpm sync` | On sync |

---

## Development

```bash
pnpm dev          # Start dev server (local workspace data)
pnpm dev:sandbox  # Sync from sandbox + start (live data)
pnpm build        # Production build
pnpm lint         # ESLint check
pnpm sync         # One-shot sync from sandbox
pnpm sync:watch   # Continuous sync every 60s
pnpm check        # Test sandbox SSH connection
```

### Adding a New Page

1. Create data adapter in `src/lib/data/yourpage.ts` (import `"server-only"`)
2. Add Zod schema in `src/lib/schemas/` if needed
3. Export from `src/lib/data/index.ts`
4. Create page in `src/app/yourpage/page.tsx` with `export const dynamic = "force-dynamic"`
5. Add nav entry in `src/lib/nav.ts`
6. Add any new data file paths to `src/lib/config.ts` DATA_PATHS and `scripts/sync-from-sandbox.sh` FILES array

### File Structure

```
dashboard/
  src/
    app/                    # 15 page routes
      overview/
      opportunities/
      portfolio/
      swaps/
      performance/
      pipeline/
      automation/
      strategy-lab/
      calibration/
      learning/
      news-impact/
      controls/
      audit/
      diagnostics/
      layout.tsx            # Root layout with sidebar + status bar
    components/             # UI components
      app-sidebar.tsx       # Collapsible nav sidebar
      global-status-bar.tsx # Server component wiring status data
      status-bar.tsx        # Client component rendering status indicators
      performance-charts.tsx # Recharts: equity curve, daily P&L, slice bars
      pipeline-charts.tsx   # Recharts: funnel, AI flow, rejection bars
      calibration-charts.tsx # Recharts: calibration curve, bias bars
      page-header.tsx       # Page title + description
      format.tsx            # Formatting utilities (currency, percent, age, bytes)
      coming-soon.tsx       # Placeholder for future features
    lib/
      config.ts             # BOT_ID, WORKSPACE_ROOT, DATA_PATHS, CRON_JOBS
      nav.ts                # Sidebar navigation structure
      category.ts           # Category inference (mirrors strategies.py keywords)
      data/                 # Server-only data adapters
        index.ts            # Barrel exports
        _io.ts              # File I/O primitives (readJsonFile, readJsonLinesFile, fileStat)
        alerts.ts           # Single source of truth for all alert conditions
        portfolio.ts        # Paper state, book health, category slots
        performance.ts      # Equity curve, daily P&L, per-category/strategy stats
        research.ts         # market_research.json reader
        counters.ts         # Research + trader counter aggregation
        opportunities.ts    # Blocked opportunity simulation with trader-parity risk gates
        swaps.ts            # Pending swap proposals
        diagnostics.ts      # File freshness checks
        automation.ts       # Cron job health (live crontab + OpenClaw parser)
        audit.ts            # Unified event timeline
        controls.ts         # Config parser (reads 5 source files for runtime knobs)
        news.ts             # News cache + context + trade overlap
        learning.ts         # Feedback loop chain (harvest -> calibration -> weights)
        strategy-weights.ts # Strategy weight reader
        calibration.ts      # Calibration table reader
      schemas/              # Zod type definitions
        portfolio.ts
        research.ts
        counters.ts
        swaps.ts
        strategy-weights.ts
        calibration.ts
        news.ts
  scripts/
    sync-from-sandbox.sh    # Rsync + SSH commands to pull live data
    check-connection.sh     # SSH connectivity test
  public/                   # Static assets
```
