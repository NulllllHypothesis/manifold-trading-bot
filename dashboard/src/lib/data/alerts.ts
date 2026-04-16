import "server-only"
import { existsSync } from "node:fs"
import path from "node:path"
import { WORKSPACE_ROOT } from "@/lib/config"
import { readDiagnostics } from "./diagnostics"
import { summarizePortfolio } from "./portfolio"
import { readResearchCounters } from "./counters"
import { readStrategyWeights } from "./strategy-weights"
import { readPendingSwaps } from "./swaps"
import { summarizeRecentCounters } from "./counters"

export type AlertLevel = "error" | "warn" | "info"

export type AlertItem = {
  level: AlertLevel
  title: string
  detail?: string
}

export async function computeAlerts(): Promise<AlertItem[]> {
  const [diagnostics, portfolio, researchCounters, strategyWeights, pendingSwaps, conversionWindow] =
    await Promise.all([
      readDiagnostics(),
      summarizePortfolio(),
      readResearchCounters(undefined, { tailLines: 20 }),
      readStrategyWeights(),
      readPendingSwaps(),
      summarizeRecentCounters(undefined, { windowHours: 24 }),
    ])

  const out: AlertItem[] = []

  const killSwitchActive = existsSync(
    path.join(WORKSPACE_ROOT, "autotrader_disabled.flag"),
  )
  if (killSwitchActive) {
    out.push({
      level: "error",
      title: "Auto-trader DISABLED (kill switch)",
      detail: "autotrader_disabled.flag exists. No trades will execute.",
    })
  }

  if (portfolio?.bookHealth.bookFull) {
    out.push({
      level: "error",
      title: `Book full: ${portfolio.openPositionCount}/${portfolio.bookHealth.maxPositions} positions`,
      detail: "No new trades can execute. Free slots by resolving or swapping stale positions.",
    })
  }

  if (portfolio && portfolio.bookHealth.cashRatio < 0.25) {
    out.push({
      level: portfolio.bookHealth.cashRatio < 0.10 ? "error" : "warn",
      title: `Cash critically low: $${portfolio.balance.toFixed(2)} (${Math.round(portfolio.bookHealth.cashRatio * 100)}% of initial)`,
      detail: "Most capital is locked in open positions.",
    })
  }

  if (
    portfolio &&
    portfolio.bookHealth.daysSinceLastTrade !== null &&
    portfolio.bookHealth.daysSinceLastTrade >= 1
  ) {
    out.push({
      level: portfolio.bookHealth.daysSinceLastTrade >= 3 ? "error" : "warn",
      title: `${portfolio.bookHealth.daysSinceLastTrade} day${portfolio.bookHealth.daysSinceLastTrade === 1 ? "" : "s"} since last trade`,
      detail: "The pipeline is not converting.",
    })
  }

  if (portfolio && portfolio.bookHealth.stalePositionCount > 0) {
    out.push({
      level: portfolio.bookHealth.stalePositionCount >= 5 ? "error" : "warn",
      title: `${portfolio.bookHealth.stalePositionCount} stale position${portfolio.bookHealth.stalePositionCount === 1 ? "" : "s"} (>7 days old)`,
      detail: portfolio.bookHealth.oldestOpenDays
        ? `Oldest is ${portfolio.bookHealth.oldestOpenDays} days old.`
        : undefined,
    })
  }

  const recentResolved = portfolio?.recentResolved.filter((t) => {
    const ts = t.resolved_at ?? t.timestamp
    if (!ts) return false
    return (Date.now() - Date.parse(ts)) / 86_400_000 < 3
  })
  if (
    recentResolved &&
    recentResolved.length === 0 &&
    (portfolio?.openPositionCount ?? 0) > 0
  ) {
    out.push({
      level: "warn",
      title: "No trades resolved in last 3 days",
      detail: "The book is stuck.",
    })
  }

  const recent5 = researchCounters.slice(-5)
  const aiAnalyzed = recent5.reduce((s, r) => s + (r.ai_analyzed ?? 0), 0)
  const aiNoResult = recent5.reduce((s, r) => s + (r.ai_no_result ?? 0), 0)
  const aiNoResultRate = aiAnalyzed > 0 ? aiNoResult / aiAnalyzed : null
  if (aiNoResultRate !== null && aiNoResultRate > 0.4) {
    out.push({
      level: aiNoResultRate > 0.7 ? "error" : "warn",
      title: `AI no_result rate ${Math.round(aiNoResultRate * 100)}%`,
      detail: `${aiAnalyzed} analyzed across last 5 runs.`,
    })
  }

  const conversionRate =
    conversionWindow.marketsFetched > 0
      ? (conversionWindow.tradesExecuted / conversionWindow.marketsFetched) * 100
      : null
  if (conversionRate !== null && conversionRate < 1) {
    out.push({
      level: "warn",
      title: `Pipeline conversion ${conversionRate.toFixed(1)}% (24h)`,
    })
  }

  if (
    strategyWeights &&
    Object.values(strategyWeights.weights).every(
      (w) => Math.abs(w - 1.0) < 0.001,
    )
  ) {
    out.push({
      level: "warn",
      title: `Learning loop not active: all weights at 1.0 (${strategyWeights.sample_count ?? 0} samples)`,
    })
  }

  const staleFiles = diagnostics.files.filter((f) => f.exists && f.isStale)
  const missingFiles = diagnostics.files.filter((f) => !f.exists)
  for (const f of missingFiles) {
    out.push({ level: "error", title: `Missing: ${f.key}` })
  }
  for (const f of staleFiles) {
    out.push({ level: "warn", title: `Stale: ${f.key}` })
  }

  const pendingSwapCount = pendingSwaps.filter(
    (s) => s.status === "pending",
  ).length
  if (pendingSwapCount > 0) {
    out.push({
      level: "info",
      title: `${pendingSwapCount} pending swap${pendingSwapCount === 1 ? "" : "s"}`,
    })
  }

  return out
}
