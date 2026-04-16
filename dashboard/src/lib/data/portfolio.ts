import "server-only"
import { DATA_PATHS, BOT_ID } from "@/lib/config"
import { PaperStateSchema, type PaperState, type Trade } from "@/lib/schemas/portfolio"
import { readJsonFile } from "./_io"
import { readMarketResearch } from "./research"

import { inferCategory as inferCategoryFromQuestion } from "@/lib/category"

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

export type CategorySlot = {
  category: string
  open: number
  cap: number
  full: boolean
}

export type BookHealth = {
  maxPositions: number
  bookFull: boolean
  cashRatio: number
  oldestOpenDays: number | null
  stalePositionCount: number
  categorySlots: CategorySlot[]
  lastTradeTimestamp: string | null
  daysSinceLastTrade: number | null
}

export type PortfolioSummary = {
  balance: number
  todayPnl: number
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
  bookHealth: BookHealth
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

  const unrealizedPnlEstimate = 0

  const recentResolved = resolved
    .sort((a, b) =>
      (b.resolved_at || b.timestamp || "").localeCompare(
        a.resolved_at || a.timestamp || "",
      ),
    )
    .slice(0, 10)

  const totalTrades = resolved.length
  const winRate = totalTrades > 0 ? winCount / totalTrades : 0

  const MAX_POSITIONS = 10
  const MAX_PER_CATEGORY = 3
  const STALE_DAYS = 7

  const categoryCounts = new Map<string, number>()
  let legacyCount = 0
  let oldestMs = Infinity
  let staleCount = 0
  for (const p of openPositions) {
    const ts = Date.parse(p.timestamp)
    if (!Number.isNaN(ts) && ts < oldestMs) oldestMs = ts
    const ageDays = Number.isNaN(ts) ? 0 : (Date.now() - ts) / 86_400_000
    if (ageDays >= STALE_DAYS) staleCount++

    if (!p.category && !p.question) {
      legacyCount++
      continue
    }
    const cat = p.category ?? inferCategoryFromQuestion(p.question ?? "")
    categoryCounts.set(cat, (categoryCounts.get(cat) ?? 0) + 1)
  }

  const allCategories = new Set<string>()
  for (const cat of categoryCounts.keys()) allCategories.add(cat)

  const categorySlots: CategorySlot[] = [...allCategories]
    .sort((a, b) => (categoryCounts.get(b) ?? 0) - (categoryCounts.get(a) ?? 0))
    .map((cat) => {
      const open = categoryCounts.get(cat) ?? 0
      const effectiveCap = cat === "other" ? MAX_POSITIONS : MAX_PER_CATEGORY
      return { category: cat, open, cap: effectiveCap, full: open >= effectiveCap }
    })
  if (legacyCount > 0) {
    categorySlots.push({
      category: "legacy (uncategorized)",
      open: legacyCount,
      cap: MAX_POSITIONS,
      full: false,
    })
  }

  const oldestDays =
    oldestMs < Infinity
      ? Math.floor((Date.now() - oldestMs) / 86_400_000)
      : null

  const allTimestamps = [
    ...openPositions.map((p) => p.timestamp),
    ...resolved.map((t) => t.resolved_at ?? t.timestamp),
  ].filter(Boolean).sort()
  const lastTradeTimestamp = allTimestamps.at(-1) ?? null
  const daysSinceLastTrade = lastTradeTimestamp
    ? Math.floor((Date.now() - Date.parse(lastTradeTimestamp)) / 86_400_000)
    : null

  const initialBalance = state.initial_balance ?? 1000
  const cashRatio = initialBalance > 0 ? state.balance / initialBalance : 0

  const bookHealth: BookHealth = {
    maxPositions: MAX_POSITIONS,
    bookFull: openPositions.length >= MAX_POSITIONS,
    cashRatio,
    oldestOpenDays: oldestDays,
    stalePositionCount: staleCount,
    categorySlots,
    lastTradeTimestamp,
    daysSinceLastTrade,
  }

  const today = new Date().toISOString().slice(0, 10)
  const todayPnl = resolved
    .filter((t) => (t.resolved_at ?? t.timestamp ?? "").slice(0, 10) === today)
    .reduce((sum, t) => sum + (t.profit ?? 0), 0)

  return {
    balance: state.balance,
    todayPnl,
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
    bookHealth,
  }
}
