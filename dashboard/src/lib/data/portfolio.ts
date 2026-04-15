import "server-only"
import { DATA_PATHS, BOT_ID } from "@/lib/config"
import { PaperStateSchema, type PaperState, type Trade } from "@/lib/schemas/portfolio"
import { readJsonFile } from "./_io"
import { readMarketResearch } from "./research"

export async function readPaperState(_botId: string = BOT_ID): Promise<PaperState | null> {
  return readJsonFile(DATA_PATHS.paperState(), PaperStateSchema)
}

/**
 * Module-level cache for Manifold API title lookups.
 * Null values cache "not found" to avoid re-hitting the API for dead IDs.
 */
const MANIFOLD_TITLE_CACHE = new Map<string, { title: string | null; ts: number }>()
const TITLE_TTL_MS = 60 * 60 * 1000

async function fetchManifoldTitle(marketId: string): Promise<string | null> {
  const cached = MANIFOLD_TITLE_CACHE.get(marketId)
  if (cached && Date.now() - cached.ts < TITLE_TTL_MS) return cached.title
  try {
    const res = await fetch(
      `https://api.manifold.markets/v0/market/${marketId}`,
      { signal: AbortSignal.timeout(3000) },
    )
    const title = res.ok
      ? (((await res.json()) as { question?: unknown }).question ?? null)
      : null
    const value = typeof title === "string" ? title : null
    MANIFOLD_TITLE_CACHE.set(marketId, { title: value, ts: Date.now() })
    return value
  } catch {
    MANIFOLD_TITLE_CACHE.set(marketId, { title: null, ts: Date.now() })
    return null
  }
}

/**
 * Builds a market_id → question map from every source we can reach locally:
 * research.latest, research.history, and any trade/position that already
 * carries a question. Missing IDs (typically legacy trades) are filled in
 * from the Manifold public API with a 1h in-memory cache.
 */
async function buildTitleLookup(state: PaperState): Promise<Map<string, string>> {
  const titles = new Map<string, string>()
  const add = (mid: string | undefined, q: string | undefined) => {
    if (mid && q && !titles.has(mid)) titles.set(mid, q)
  }

  for (const trades of Object.values(state.positions)) {
    for (const t of trades) add(t.market_id, t.question)
  }
  for (const t of state.trade_history) add(t.market_id, t.question)

  const research = await readMarketResearch()
  if (research?.latest) {
    for (const r of research.latest.recommendations) add(r.market_id, r.question)
  }
  for (const snap of research?.history ?? []) {
    for (const r of snap.recommendations) add(r.market_id, r.question)
  }

  const missing = new Set<string>()
  for (const trades of Object.values(state.positions)) {
    for (const t of trades) {
      if (!t.question && !titles.has(t.market_id)) missing.add(t.market_id)
    }
  }
  for (const t of state.trade_history) {
    if (!t.question && !titles.has(t.market_id)) missing.add(t.market_id)
  }

  if (missing.size > 0) {
    const fetched = await Promise.all(
      [...missing].map(async (id) => [id, await fetchManifoldTitle(id)] as const),
    )
    for (const [id, title] of fetched) if (title) titles.set(id, title)
  }

  return titles
}

function withTitle(t: Trade, titles: Map<string, string>): Trade {
  if (t.question) return t
  const q = titles.get(t.market_id)
  return q ? { ...t, question: q } : t
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

  const titles = await buildTitleLookup(state)

  const openPositions: Trade[] = []
  for (const trades of Object.values(state.positions)) {
    for (const t of trades) {
      if (t.status === "OPEN") openPositions.push(withTitle(t, titles))
    }
  }

  const openExposure = openPositions.reduce((sum, p) => sum + p.amount, 0)

  let realizedPnl = 0
  let winCount = 0
  let loseCount = 0
  const resolved: Trade[] = []
  for (const raw of state.trade_history) {
    if (raw.status === "OPEN") continue
    const t = withTitle(raw, titles)
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
