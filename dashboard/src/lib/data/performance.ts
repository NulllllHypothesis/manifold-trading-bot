import "server-only"
import { BOT_ID } from "@/lib/config"
import type { Trade } from "@/lib/schemas/portfolio"
import { readPaperState } from "./portfolio"

export type EquityPoint = { date: string; cumPnl: number }
export type DailyPnl = {
  date: string
  profit: number
  trades: number
  wins: number
  losses: number
}
export type SliceStat = {
  key: string
  pnl: number
  trades: number
  wins: number
  winRate: number
}

export type PerformanceStats = {
  totalResolved: number
  wins: number
  losses: number
  winRate: number
  totalPnl: number
  bestTrade: number
  worstTrade: number
  avgTrade: number
  profitFactor: number | null
  maxDrawdown: number
}

export type PerformanceSummary = {
  equityCurve: EquityPoint[]
  dailyPnl: DailyPnl[]
  byCategory: SliceStat[]
  byStrategy: SliceStat[]
  stats: PerformanceStats
  windowFrom: string | null
  windowTo: string | null
}

function resolvedDate(t: Trade): string {
  const raw = t.resolved_at || t.timestamp || ""
  return raw.slice(0, 10)
}

function isWin(t: Trade): boolean {
  return t.status === "WIN" || (t.profit ?? 0) > 0
}

function isLose(t: Trade): boolean {
  return t.status === "LOSE" || (t.profit ?? 0) < 0
}

export async function summarizePerformance(
  botId: string = BOT_ID,
): Promise<PerformanceSummary | null> {
  const state = await readPaperState(botId)
  if (!state) return null

  const resolved = state.trade_history
    .filter((t) => t.status !== "OPEN")
    .sort((a, b) =>
      (a.resolved_at || a.timestamp || "").localeCompare(
        b.resolved_at || b.timestamp || "",
      ),
    )

  const equityCurve: EquityPoint[] = []
  const dailyMap = new Map<string, DailyPnl>()
  const categoryMap = new Map<string, SliceStat>()
  const strategyMap = new Map<string, SliceStat>()

  let cum = 0
  let wins = 0
  let losses = 0
  let grossWin = 0
  let grossLoss = 0
  let best = 0
  let worst = 0
  let peak = 0
  let maxDd = 0

  for (const t of resolved) {
    const profit = t.profit ?? 0
    cum += profit
    const date = resolvedDate(t)
    equityCurve.push({ date, cumPnl: Number(cum.toFixed(2)) })

    if (cum > peak) peak = cum
    const dd = peak - cum
    if (dd > maxDd) maxDd = dd

    if (profit > 0) {
      wins++
      grossWin += profit
      if (profit > best) best = profit
    }
    if (profit < 0) {
      losses++
      grossLoss += -profit
      if (profit < worst) worst = profit
    }

    const day = dailyMap.get(date) ?? {
      date,
      profit: 0,
      trades: 0,
      wins: 0,
      losses: 0,
    }
    day.profit += profit
    day.trades++
    if (isWin(t)) day.wins++
    if (isLose(t)) day.losses++
    dailyMap.set(date, day)

    const category = t.category ?? "unknown"
    const c = categoryMap.get(category) ?? {
      key: category,
      pnl: 0,
      trades: 0,
      wins: 0,
      winRate: 0,
    }
    c.pnl += profit
    c.trades++
    if (isWin(t)) c.wins++
    categoryMap.set(category, c)

    for (const s of t.strategies ?? []) {
      const entry = strategyMap.get(s) ?? {
        key: s,
        pnl: 0,
        trades: 0,
        wins: 0,
        winRate: 0,
      }
      entry.pnl += profit
      entry.trades++
      if (isWin(t)) entry.wins++
      strategyMap.set(s, entry)
    }
  }

  for (const v of dailyMap.values()) v.profit = Number(v.profit.toFixed(2))
  for (const v of categoryMap.values()) {
    v.pnl = Number(v.pnl.toFixed(2))
    v.winRate = v.trades > 0 ? v.wins / v.trades : 0
  }
  for (const v of strategyMap.values()) {
    v.pnl = Number(v.pnl.toFixed(2))
    v.winRate = v.trades > 0 ? v.wins / v.trades : 0
  }

  const dailyPnl = [...dailyMap.values()].sort((a, b) =>
    a.date.localeCompare(b.date),
  )
  const byCategory = [...categoryMap.values()].sort((a, b) => b.pnl - a.pnl)
  const byStrategy = [...strategyMap.values()].sort((a, b) => b.pnl - a.pnl)

  const totalResolved = wins + losses
  const totalPnl = grossWin - grossLoss

  const stats: PerformanceStats = {
    totalResolved,
    wins,
    losses,
    winRate: totalResolved > 0 ? wins / totalResolved : 0,
    totalPnl: Number(totalPnl.toFixed(2)),
    bestTrade: Number(best.toFixed(2)),
    worstTrade: Number(worst.toFixed(2)),
    avgTrade:
      totalResolved > 0 ? Number((totalPnl / totalResolved).toFixed(2)) : 0,
    profitFactor:
      grossLoss > 0 ? Number((grossWin / grossLoss).toFixed(2)) : null,
    maxDrawdown: Number(maxDd.toFixed(2)),
  }

  return {
    equityCurve,
    dailyPnl,
    byCategory,
    byStrategy,
    stats,
    windowFrom: resolved[0] ? resolvedDate(resolved[0]) : null,
    windowTo: resolved.at(-1) ? resolvedDate(resolved.at(-1)!) : null,
  }
}
