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
  readTraderCounters,
  computeAlerts,
} from "@/lib/data"
import { StatusBar, type StatusItem, type StatusLevel } from "./status-bar"

export async function GlobalStatusBar() {
  const [diagnostics, pendingSwaps, researchCounters, traderCounters, alerts] =
    await Promise.all([
      readDiagnostics(),
      readPendingSwaps(),
      readResearchCounters(undefined, { tailLines: 5 }),
      readTraderCounters(undefined, { tailLines: 5 }),
      computeAlerts(),
    ])

  const paperStateFile = diagnostics.files.find((f) => f.key === "paperState")
  const killSwitchActive = existsSync(path.join(WORKSPACE_ROOT, "autotrader_disabled.flag"))

  const lastResearch = researchCounters.at(-1)
  const lastTrader = traderCounters.at(-1)

  const RESEARCH_WARN_SEC = 75 * 60
  const RESEARCH_ERROR_SEC = 3 * 3600

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

  const alertCount = alerts.length
  const alertLevel: StatusLevel =
    alertCount === 0 ? "ok" : alertCount > 5 ? "error" : "warn"

  const pendingCount = pendingSwaps.filter((s) => s.status === "pending").length

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
      level: pendingCount > 0 ? "warn" : "idle",
      value: String(pendingCount),
      detail:
        pendingCount > 0
          ? `${pendingCount} pending swap proposal${pendingCount === 1 ? "" : "s"}`
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
  const [pendingSwaps, alerts] = await Promise.all([
    readPendingSwaps(),
    computeAlerts(),
  ])
  return {
    pendingSwaps: pendingSwaps.filter((s) => s.status === "pending").length,
    alerts: alerts.length,
  }
}
