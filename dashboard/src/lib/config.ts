/**
 * Dashboard config — single source of truth for paths, IDs, and intervals.
 * Designed to be swapped via env vars when we go multi-tenant.
 */

import path from "node:path"

/** The bot this dashboard is showing. Hardcoded for v1; param for multi-tenant. */
export const BOT_ID = process.env.BOT_ID ?? "main"

/** Friendly display name for the bot. */
export const BOT_NAME = process.env.BOT_NAME ?? "Manifold Bot"

/**
 * Filesystem root for the bot's workspace.
 * In Phase 1 (laptop dev): mounted via SCP/rsync sync, or set to the workspace path.
 * In Phase 2 (Vercel deploy): replaced with API base URL.
 */
export const WORKSPACE_ROOT =
  process.env.WORKSPACE_ROOT ?? path.resolve(process.cwd(), "..")

/** Polling interval for client-side data refresh, in milliseconds. */
export const POLL_INTERVAL_MS = Number(
  process.env.POLL_INTERVAL_MS ?? 60_000,
)

/** Paths to known data files, all resolved relative to WORKSPACE_ROOT. */
export const DATA_PATHS = {
  marketResearch: () => path.join(WORKSPACE_ROOT, "market_research.json"),
  paperState: () =>
    path.join(WORKSPACE_ROOT, "manifold_bot/paper_trading_state.json"),
  autoTrades: () => path.join(WORKSPACE_ROOT, "auto_trades.json"),
  pendingSwaps: () => path.join(WORKSPACE_ROOT, "pending_swaps.json"),
  pendingCloses: () => path.join(WORKSPACE_ROOT, "pending_closes.json"),
  pendingAbandons: () => path.join(WORKSPACE_ROOT, "pending_abandons.json"),
  researchCounters: () =>
    path.join(WORKSPACE_ROOT, "data/research_counters.jsonl"),
  traderCounters: () =>
    path.join(WORKSPACE_ROOT, "data/trader_counters.jsonl"),
  strategyWeights: () =>
    path.join(WORKSPACE_ROOT, "data/strategy_weights.json"),
  calibrationTable: () =>
    path.join(WORKSPACE_ROOT, "data/calibration_table.json"),
  categoryAccuracy: () =>
    path.join(WORKSPACE_ROOT, "data/category_accuracy.json"),
  m2Audit: () => path.join(WORKSPACE_ROOT, "data/m2_audit.json"),
  evalReport: () => path.join(WORKSPACE_ROOT, "data/eval_report.json"),
  backtestReport: () =>
    path.join(WORKSPACE_ROOT, "data/backtest_report.json"),
  aiCooldown: () =>
    path.join(WORKSPACE_ROOT, "data/ai_timeout_cooldown.json"),
  newsCache: () => path.join(WORKSPACE_ROOT, "data/news_cache.json"),
  calibrationDb: () => path.join(WORKSPACE_ROOT, "data/calibration.db"),
  snapshotsDb: () =>
    path.join(WORKSPACE_ROOT, "data/market_snapshots.db"),
  liveCrontab: () => path.join(WORKSPACE_ROOT, "data/crontab.txt"),
  openclawJobs: () => path.join(WORKSPACE_ROOT, "data/openclaw_jobs.json"),
} as const

/** Cron schedule reference — kept in sync with CLAUDE.md. */
export const CRON_JOBS = [
  { id: "research", schedule: "0 * * * *", description: "Hourly market research" },
  { id: "reprice", schedule: "5 * * * *", description: "Hourly position repricing (Phase 2.2)" },
  { id: "resolution", schedule: "10 * * * *", description: "Position resolution" },
  { id: "trader", schedule: "20 * * * *", description: "Trade execution" },
  { id: "evaluate", schedule: "30 * * * *", description: "Early-close evaluator (Phase 2.3)" },
  { id: "stale-detection", schedule: "0 13 * * *", description: "Daily stale detection (Phase 2.4)" },
  { id: "daily-summary", schedule: "0 19 * * *", description: "Daily summary" },
  { id: "harvest", schedule: "0 2 * * 0", description: "Sunday calibration harvest" },
  { id: "m2-audit", schedule: "30 2 * * 0", description: "Sunday M2 re-audit" },
  { id: "m3-reconstruct", schedule: "0 3 * * 0", description: "Sunday M3 snapshot reconstruction" },
  { id: "backtest", schedule: "30 3 * * 0", description: "Sunday backtest weights" },
  { id: "ev-report", schedule: "0 7 * * 1", description: "Monday weekly EV report" },
  { id: "live-weights", schedule: "30 7 * * 1", description: "Monday live strategy weights" },
] as const
