/**
 * Server component that wires the status bar to live data.
 * Reads file freshness, counter recency, swap counts → computes status levels.
 */
import {
  readDiagnostics,
  readPendingSwaps,
  readResearchCounters,
  readTraderCounters,
} from "@/lib/data"
import { StatusBar, type StatusItem, type StatusLevel } from "./status-bar"

function formatAge(ageSeconds: number | null): string {
  if (ageSeconds === null) return "—"
  if (ageSeconds < 60) return `${ageSeconds}s`
  if (ageSeconds < 3600) return `${Math.floor(ageSeconds / 60)}m`
  if (ageSeconds < 86400) return `${Math.floor(ageSeconds / 3600)}h`
  return `${Math.floor(ageSeconds / 86400)}d`
}

function levelForAge(
  ageSeconds: number | null,
  warnAfter: number,
  errorAfter: number,
): StatusLevel {
  if (ageSeconds === null) return "idle"
  if (ageSeconds > errorAfter) return "error"
  if (ageSeconds > warnAfter) return "warn"
  return "ok"
}

export async function GlobalStatusBar() {
  const [diagnostics, pendingSwaps, researchCounters, traderCounters] =
    await Promise.all([
      readDiagnostics(),
      readPendingSwaps(),
      readResearchCounters(undefined, { tailLines: 5 }),
      readTraderCounters(undefined, { tailLines: 5 }),
    ])

  const paperStateFile = diagnostics.files.find((f) => f.key === "paperState")
  const researchFile = diagnostics.files.find((f) => f.key === "researchCounters")
  const aiCooldownFile = diagnostics.files.find((f) => f.key === "aiCooldown")

  const lastResearch = researchCounters.at(-1)
  const lastTrader = traderCounters.at(-1)

  // Snapshot "now" once so the React Compiler sees a pure render path.
  const nowMs = new Date().getTime()
  const lastResearchAge = lastResearch
    ? Math.floor((nowMs - Date.parse(lastResearch.timestamp)) / 1000)
    : null
  const lastTraderAge = lastTrader
    ? Math.floor((nowMs - Date.parse(lastTrader.timestamp)) / 1000)
    : null

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
  const alertLevel: StatusLevel =
    staleFiles.length + missingFiles.length === 0
      ? "ok"
      : staleFiles.length + missingFiles.length > 3
        ? "error"
        : "warn"
  const alertCount = staleFiles.length + missingFiles.length

  const items: StatusItem[] = [
    {
      key: "botEnabled",
      level: paperStateFile?.exists ? "ok" : "error",
      value: paperStateFile?.exists ? "ON" : "OFF",
      detail: paperStateFile?.exists
        ? `Paper state updated ${formatAge(paperStateFile.ageSeconds)} ago`
        : "Paper trading state file missing",
    },
    {
      key: "research",
      level: levelForAge(lastResearchAge, 75 * 60, 3 * 3600),
      value: formatAge(lastResearchAge),
      detail: lastResearch
        ? `Last research run: ${lastResearch.timestamp}`
        : "No research runs found",
    },
    {
      key: "trader",
      level: levelForAge(lastTraderAge, 75 * 60, 3 * 3600),
      value: formatAge(lastTraderAge),
      detail: lastTrader
        ? `Last trader run: ${lastTrader.timestamp}`
        : "No trader runs found",
    },
    {
      key: "swaps",
      level: pendingSwaps.length > 0 ? "warn" : "idle",
      value: String(pendingSwaps.length),
      detail:
        pendingSwaps.length > 0
          ? `${pendingSwaps.length} pending swap proposal${pendingSwaps.length === 1 ? "" : "s"}`
          : "No pending swap proposals",
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
          ? "All known data files present and fresh"
          : `${missingFiles.length} missing, ${staleFiles.length} stale`,
    },
  ]

  return <StatusBar items={items} />
}

/** Light helper used by sidebar to surface badge counts. */
export async function getSidebarBadges(): Promise<{
  pendingSwaps: number
  alerts: number
}> {
  const [pendingSwaps, diagnostics] = await Promise.all([
    readPendingSwaps(),
    readDiagnostics(),
  ])
  const alerts = diagnostics.files.filter((f) => !f.exists || f.isStale).length
  return {
    pendingSwaps: pendingSwaps.filter((s) => s.status === "pending").length,
    alerts,
  }
}
