/**
 * Server component that wires the status bar to live data.
 * Reads file freshness, counter recency, swap counts → computes status levels.
 */
import { existsSync } from "node:fs"
import path from "node:path"
import { WORKSPACE_ROOT } from "@/lib/config"
import {
  readDiagnostics,
  readPendingSwaps,
  readResearchCounters,
  readStrategyWeights,
  readTraderCounters,
  summarizePortfolio,
} from "@/lib/data"
import { StatusBar, type StatusItem, type StatusLevel } from "./status-bar"

export async function GlobalStatusBar() {
  const [diagnostics, pendingSwaps, researchCounters, traderCounters, portfolio, strategyWeights] =
    await Promise.all([
      readDiagnostics(),
      readPendingSwaps(),
      readResearchCounters(undefined, { tailLines: 5 }),
      readTraderCounters(undefined, { tailLines: 5 }),
      summarizePortfolio(),
      readStrategyWeights(),
    ])

  const paperStateFile = diagnostics.files.find((f) => f.key === "paperState")
  const killSwitchActive = existsSync(path.join(WORKSPACE_ROOT, "autotrader_disabled.flag"))

  const lastResearch = researchCounters.at(-1)
  const lastTrader = traderCounters.at(-1)

  // Age/staleness is computed client-side in StatusBar to keep SSR stable.
  // Server only passes absolute timestamps + thresholds.
  const RESEARCH_WARN_SEC = 75 * 60
  const RESEARCH_ERROR_SEC = 3 * 3600

  // AI health: derived from recent ai_no_result rate
  const recentAi = researchCounters.slice(-5)
  const aiAnalyzed = recentAi.reduce((s, r) => s + (r.ai_analyzed ?? 0), 0)
  const aiNoResult = recentAi.reduce((s, r) => s + (r.ai_no_result ?? 0), 0)
  const aiNoResultRate = aiAnalyzed > 0 ? aiNoResult / aiAnalyzed : null
  const aiLevel: StatusLevel =
    aiNoResultRate === null
      ? "idle"
      : aiNoResultRate > 0.7
        ? "error"
      : aiNoResultRate > 0.4
          ? "warn"
          : "ok"

  const staleFiles = diagnostics.files.filter((f) => f.exists && f.isStale)
  const missingFiles = diagnostics.files.filter((f) => !f.exists)
  let alertCount = staleFiles.length + missingFiles.length
  if (killSwitchActive) alertCount++
  if (portfolio?.bookHealth.bookFull) alertCount++
  if (portfolio && portfolio.bookHealth.cashRatio < 0.25) alertCount++
  if (portfolio && portfolio.bookHealth.stalePositionCount > 0) alertCount++
  if (portfolio && portfolio.bookHealth.daysSinceLastTrade !== null && portfolio.bookHealth.daysSinceLastTrade >= 1) alertCount++
  if (aiNoResultRate !== null && aiNoResultRate > 0.4) alertCount++
  if (strategyWeights && Object.values(strategyWeights.weights).every((w) => Math.abs(w - 1.0) < 0.001)) alertCount++
  const recentResolved = portfolio?.recentResolved.filter((t) => {
    const ts = t.resolved_at ?? t.timestamp
    if (!ts) return false
    const age = (new Date(diagnostics.generatedAt).getTime() - Date.parse(ts)) / 86_400_000
    return age < 3
  })
  if (recentResolved && recentResolved.length === 0 && (portfolio?.openPositionCount ?? 0) > 0) alertCount++
  const alertLevel: StatusLevel =
    alertCount === 0 ? "ok" : alertCount > 5 ? "error" : "warn"

  const items: StatusItem[] = [
    {
      key: "botEnabled",
      level: killSwitchActive ? "error" : paperStateFile?.exists ? "ok" : "error",
      value: killSwitchActive ? "KILL" : paperStateFile?.exists ? "ON" : "OFF",
      detail: killSwitchActive
        ? "autotrader_disabled.flag is present — no trades will execute"
        : paperStateFile?.exists
          ? "Paper trading state file present"
          : "Paper trading state file missing",
    },
    {
      key: "research",
      level: lastResearch ? "idle" : "error",
      value: lastResearch ? "—" : "n/a",
      timestamp: lastResearch?.timestamp,
      warnAfterSec: RESEARCH_WARN_SEC,
      errorAfterSec: RESEARCH_ERROR_SEC,
      detail: lastResearch
        ? `Last research run: ${lastResearch.timestamp}`
        : "No research runs found",
    },
    {
      key: "trader",
      level: lastTrader ? "idle" : "error",
      value: lastTrader ? "—" : "n/a",
      timestamp: lastTrader?.timestamp,
      warnAfterSec: RESEARCH_WARN_SEC,
      errorAfterSec: RESEARCH_ERROR_SEC,
      detail: lastTrader
        ? `Last trader run: ${lastTrader.timestamp}`
        : "No trader runs found",
    },
    {
      key: "swaps",
      level: pendingSwaps.filter((s) => s.status === "pending").length > 0 ? "warn" : "idle",
      value: String(pendingSwaps.filter((s) => s.status === "pending").length),
      detail:
        pendingSwaps.filter((s) => s.status === "pending").length > 0
          ? `${pendingSwaps.filter((s) => s.status === "pending").length} pending swap proposal${pendingSwaps.filter((s) => s.status === "pending").length === 1 ? "" : "s"}`
          : `No pending swaps (${pendingSwaps.length} total: ${pendingSwaps.map((s) => s.status).join(", ") || "none"})`,
    },
    {
      key: "ai",
      level: aiLevel,
      value:
        aiNoResultRate === null
          ? "—"
          : `${Math.round((1 - aiNoResultRate) * 100)}%`,
      detail:
        aiNoResultRate === null
          ? "No AI calls in last 5 research runs"
          : `${Math.round(aiNoResultRate * 100)}% no_result over last 5 runs (${aiAnalyzed} analyzed)`,
    },
    {
      key: "alerts",
      level: alertLevel,
      value: String(alertCount),
      detail:
        alertCount === 0
          ? "All systems healthy"
          : `${alertCount} issues — see Overview for details`,
    },
  ]

  return <StatusBar items={items} />
}

/** Light helper used by sidebar to surface badge counts. */
export async function getSidebarBadges(): Promise<{
  pendingSwaps: number
  alerts: number
}> {
  const [pendingSwaps, diagnostics, portfolio, sw, rc] = await Promise.all([
    readPendingSwaps(),
    readDiagnostics(),
    summarizePortfolio(),
    readStrategyWeights(),
    readResearchCounters(undefined, { tailLines: 5 }),
  ])
  let alerts = diagnostics.files.filter((f) => !f.exists || f.isStale).length
  if (portfolio?.bookHealth.bookFull) alerts++
  if (portfolio && portfolio.bookHealth.cashRatio < 0.25) alerts++
  if (portfolio && portfolio.bookHealth.stalePositionCount > 0) alerts++
  if (portfolio && portfolio.bookHealth.daysSinceLastTrade !== null && portfolio.bookHealth.daysSinceLastTrade >= 1) alerts++
  const aiAnalyzed = rc.slice(-5).reduce((s, r) => s + (r.ai_analyzed ?? 0), 0)
  const aiNoResult = rc.slice(-5).reduce((s, r) => s + (r.ai_no_result ?? 0), 0)
  if (aiAnalyzed > 0 && aiNoResult / aiAnalyzed > 0.4) alerts++
  if (sw && Object.values(sw.weights).every((w) => Math.abs(w - 1.0) < 0.001)) alerts++
  if (existsSync(path.join(WORKSPACE_ROOT, "autotrader_disabled.flag"))) alerts++
  const recentRes = portfolio?.recentResolved.filter((t) => {
    const ts = t.resolved_at ?? t.timestamp
    if (!ts) return false
    return (Date.now() - Date.parse(ts)) / 86_400_000 < 3
  })
  if (recentRes && recentRes.length === 0 && (portfolio?.openPositionCount ?? 0) > 0) alerts++
  return {
    pendingSwaps: pendingSwaps.filter((s) => s.status === "pending").length,
    alerts,
  }
}
