import "server-only"
import { DATA_PATHS, BOT_ID } from "@/lib/config"
import { NewsCacheSchema, type CachedArticle } from "@/lib/schemas/news"
import { readJsonFile } from "./_io"
import { readMarketResearch } from "./research"
import { readPaperState } from "./portfolio"

export type NewsMarketEntry = {
  marketId: string
  question: string | null
  category: string | null
  fetchDates: string[]
  articles: CachedArticle[]
  totalArticles: number
  emptyFetches: number
  usedInNewsContext: boolean
  newsContextHeadlines: string[]
  confidence: number | null
  wasAiCandidate: boolean
  becameTrade: boolean
  tradeOutcome: string | null
  tradeProfit: number | null
}

export type NewsDirectionSignal = {
  marketId: string
  question: string | null
  headlines: string[]
  yesLean: number
  noLean: number
  direction: "YES" | "NO" | null
  recDirection: string | null
  agrees: boolean | null
  confidence: number | null
}

export type NewsFetchHealth = {
  totalCacheEntries: number
  entriesWithArticles: number
  emptyEntries: number
  uniqueMarkets: number
  marketsWithArticles: number
  testEntries: number
  dateRange: { earliest: string | null; latest: string | null }
  newsApiEnabled: boolean
}

export type NewsImpactSummary = {
  markets: NewsMarketEntry[]
  signals: NewsDirectionSignal[]
  fetchHealth: NewsFetchHealth
  recsWithNews: number
  recsWithoutNews: number
  tradesWithNews: number
  tradesWithoutNews: number
}

const YES_WORDS = new Set([
  "confirmed", "approved", "passed", "signed", "enacted", "launched",
  "succeeded", "won", "wins", "victory", "breakthrough", "surges",
  "soars", "rises", "jumps", "gains", "bullish", "upgrade", "deal",
  "agreement", "supports", "backs", "endorses", "accepts", "adopts",
])

const NO_WORDS = new Set([
  "denied", "rejected", "failed", "blocked", "vetoed", "cancelled",
  "canceled", "postponed", "delayed", "crashed", "plunges", "falls",
  "drops", "loses", "loss", "defeat", "bearish", "downgrade", "opposes",
  "refuses", "withdraws", "collapses", "scandal", "investigation",
])

function computeDirection(
  articles: CachedArticle[],
): { yesLean: number; noLean: number; direction: "YES" | "NO" | null } {
  let yesLean = 0
  let noLean = 0
  for (const a of articles) {
    const words = new Set(a.title.toLowerCase().split(/\s+/))
    let yesHits = 0
    let noHits = 0
    for (const w of words) {
      if (YES_WORDS.has(w)) yesHits++
      if (NO_WORDS.has(w)) noHits++
    }
    if (yesHits > noHits) yesLean++
    else if (noHits > yesHits) noLean++
  }
  const direction = yesLean > noLean ? "YES" as const : noLean > yesLean ? "NO" as const : null
  return { yesLean, noLean, direction }
}

function computeNewsConfidence(articles: CachedArticle[]): number {
  if (articles.length === 0) return 0.6
  const minAge = Math.min(...articles.map((a) => a.age_hours ?? Infinity))
  if (minAge < 24) return 0.8
  if (minAge < 72) return 0.7
  return 0.6
}

const TITLE_CACHE = new Map<string, { title: string | null; ts: number }>()
const TITLE_TTL_MS = 60 * 60 * 1000

async function fetchManifoldTitle(marketId: string): Promise<string | null> {
  const cached = TITLE_CACHE.get(marketId)
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
    TITLE_CACHE.set(marketId, { title: value, ts: Date.now() })
    return value
  } catch {
    TITLE_CACHE.set(marketId, { title: null, ts: Date.now() })
    return null
  }
}

export async function summarizeNewsImpact(
  _botId: string = BOT_ID,
): Promise<NewsImpactSummary | null> {
  const [cache, research, state] = await Promise.all([
    readJsonFile(DATA_PATHS.newsCache(), NewsCacheSchema),
    readMarketResearch(),
    readPaperState(),
  ])

  if (!cache) return null

  const recs = research?.latest?.recommendations ?? []
  const recsByMarket = new Map(recs.map((r) => [r.market_id, r]))

  const titles = new Map<string, string>()
  for (const r of recs) if (r.question) titles.set(r.market_id, r.question)
  for (const snap of research?.history ?? []) {
    for (const r of snap.recommendations) {
      if (r.question && !titles.has(r.market_id)) titles.set(r.market_id, r.question)
    }
  }
  if (state) {
    for (const t of state.trade_history) {
      if (t.question && !titles.has(t.market_id)) titles.set(t.market_id, t.question)
    }
    for (const trades of Object.values(state.positions)) {
      for (const t of trades) {
        if (t.question && !titles.has(t.market_id)) titles.set(t.market_id, t.question)
      }
    }
  }

  const tradesByMarket = new Map<
    string,
    { status: string; profit: number | null }
  >()
  if (state) {
    for (const t of state.trade_history) {
      tradesByMarket.set(t.market_id, {
        status: t.status,
        profit: t.profit ?? null,
      })
    }
    for (const trades of Object.values(state.positions)) {
      for (const t of trades) {
        if (t.status === "OPEN" && !tradesByMarket.has(t.market_id)) {
          tradesByMarket.set(t.market_id, { status: "OPEN", profit: null })
        }
      }
    }
  }

  const byMarket = new Map<
    string,
    { dates: string[]; articles: CachedArticle[]; emptyFetches: number }
  >()
  let testEntries = 0
  let emptyEntries = 0
  let entriesWithArticles = 0
  let earliestDate: string | null = null
  let latestDate: string | null = null

  for (const [key, articles] of Object.entries(cache)) {
    const [mid, date] = key.includes(":")
      ? [key.split(":")[0]!, key.split(":")[1]!]
      : [key, ""]

    if (mid.includes("test") || mid === "test") {
      testEntries++
      continue
    }

    if (date) {
      if (!earliestDate || date < earliestDate) earliestDate = date
      if (!latestDate || date > latestDate) latestDate = date
    }

    if (articles.length === 0) {
      emptyEntries++
    } else {
      entriesWithArticles++
    }

    if (!byMarket.has(mid)) {
      byMarket.set(mid, { dates: [], articles: [], emptyFetches: 0 })
    }
    const entry = byMarket.get(mid)!
    if (date) entry.dates.push(date)
    entry.articles.push(...articles)
    if (articles.length === 0) entry.emptyFetches++
  }

  const missingTitles = [...byMarket.keys()].filter((mid) => !titles.has(mid))
  if (missingTitles.length > 0) {
    const fetched = await Promise.all(
      missingTitles.slice(0, 30).map(async (id) =>
        [id, await fetchManifoldTitle(id)] as const,
      ),
    )
    for (const [id, title] of fetched) {
      if (title) titles.set(id, title)
    }
  }

  const markets: NewsMarketEntry[] = []
  const signals: NewsDirectionSignal[] = []
  let recsWithNews = 0
  let recsWithoutNews = 0
  let tradesWithNews = 0
  let tradesWithoutNews = 0

  for (const [mid, data] of byMarket) {
    const rec = recsByMarket.get(mid)
    const trade = tradesByMarket.get(mid)
    const newsContextRaw = rec?.news_context
    const newsContextHeadlines: string[] = Array.isArray(newsContextRaw)
      ? newsContextRaw.filter((h): h is string => typeof h === "string")
      : []
    const usedInNewsContext = newsContextHeadlines.length > 0
    const question = rec?.question ?? titles.get(mid) ?? null

    markets.push({
      marketId: mid,
      question,
      category: rec?.category ?? null,
      fetchDates: data.dates.sort(),
      articles: data.articles,
      totalArticles: data.articles.length,
      emptyFetches: data.emptyFetches,
      usedInNewsContext,
      newsContextHeadlines,
      confidence: rec?.confidence ?? null,
      wasAiCandidate: rec?.ai_was_candidate ?? false,
      becameTrade: trade !== undefined,
      tradeOutcome: trade?.status ?? null,
      tradeProfit: trade?.profit ?? null,
    })

    if (data.articles.length > 0) {
      const { yesLean, noLean, direction } = computeDirection(data.articles)
      signals.push({
        marketId: mid,
        question,
        headlines: data.articles.map((a) => a.title),
        yesLean,
        noLean,
        direction,
        recDirection: rec?.recommendation ?? null,
        agrees:
          direction && rec?.recommendation
            ? direction === rec.recommendation.toUpperCase()
            : null,
        confidence: computeNewsConfidence(data.articles),
      })
    }
  }

  for (const r of recs) {
    const hasNews = byMarket.has(r.market_id) && (byMarket.get(r.market_id)!.articles.length > 0)
    if (hasNews) recsWithNews++
    else recsWithoutNews++
  }

  for (const [mid] of tradesByMarket) {
    const hasNews = byMarket.has(mid) && (byMarket.get(mid)!.articles.length > 0)
    if (hasNews) tradesWithNews++
    else tradesWithoutNews++
  }

  markets.sort((a, b) => b.totalArticles - a.totalArticles)
  signals.sort((a, b) => (b.confidence ?? 0) - (a.confidence ?? 0))

  const hasRecentFetches = latestDate !== null &&
    (new Date().getTime() - new Date(latestDate).getTime()) < 3 * 86_400_000

  return {
    markets,
    signals,
    fetchHealth: {
      totalCacheEntries: Object.keys(cache).length - testEntries,
      entriesWithArticles,
      emptyEntries,
      uniqueMarkets: byMarket.size,
      marketsWithArticles: [...byMarket.values()].filter(
        (m) => m.articles.length > 0,
      ).length,
      testEntries,
      dateRange: { earliest: earliestDate, latest: latestDate },
      newsApiEnabled: hasRecentFetches,
    },
    recsWithNews,
    recsWithoutNews,
    tradesWithNews,
    tradesWithoutNews,
  }
}
