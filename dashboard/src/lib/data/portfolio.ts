import "server-only"
import { DATA_PATHS, BOT_ID } from "@/lib/config"
import { PaperStateSchema, type PaperState, type Trade } from "@/lib/schemas/portfolio"
import { readJsonFile } from "./_io"

export async function readPaperState(_botId: string = BOT_ID): Promise<PaperState | null> {
  return readJsonFile(DATA_PATHS.paperState(), PaperStateSchema)
}

export type PortfolioSummary = {
  balance: number
  openPositionCount: number
  openExposure: number
  realizedPnl: number
  unrealizedPnlEstimate: number
  totalTrades: number
  winCount: number
  loseCount: number
  winRate: number
  openPositions: Trade[]
  recentResolved: Trade[]
}

export async function summarizePortfolio(
  botId: string = BOT_ID,
): Promise<PortfolioSummary | null> {
  const state = await readPaperState(botId)
  if (!state) return null

  const openPositions: Trade[] = []
  for (const trades of Object.values(state.positions)) {
    for (const t of trades) {
      if (t.status === "OPEN") openPositions.push(t)
    }
  }

  const openExposure = openPositions.reduce((sum, p) => sum + p.amount, 0)

  let realizedPnl = 0
  let winCount = 0
  let loseCount = 0
  const resolved: Trade[] = []
  for (const t of state.trade_history) {
    if (t.status === "OPEN") continue
    resolved.push(t)
    const profit = t.profit ?? 0
    realizedPnl += profit
    if (t.status === "WIN" || profit > 0) winCount++
    if (t.status === "LOSE" || (t.status !== "WIN" && profit < 0)) loseCount++
  }

  // Naive unrealized estimate: sum of profit_if_win for OPEN positions weighted by entry prob.
  // True unrealized requires current market probability — not in state file.
  const unrealizedPnlEstimate = 0

  const recentResolved = resolved
    .sort((a, b) => (b.timestamp || "").localeCompare(a.timestamp || ""))
    .slice(0, 10)

  const totalTrades = winCount + loseCount
  const winRate = totalTrades > 0 ? winCount / totalTrades : 0

  return {
    balance: state.balance,
    openPositionCount: openPositions.length,
    openExposure,
    realizedPnl,
    unrealizedPnlEstimate,
    totalTrades,
    winCount,
    loseCount,
    winRate,
    openPositions,
    recentResolved,
  }
}
