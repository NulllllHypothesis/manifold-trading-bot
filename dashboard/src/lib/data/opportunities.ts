import "server-only"
import { BOT_ID } from "@/lib/config"
import type { Recommendation } from "@/lib/schemas/research"
import { readMarketResearch } from "./research"
import { readPaperState } from "./portfolio"

export type AiStatus = "not_run" | "skip_or_no_result" | "agree" | "disagree"

export type BlockedOpportunity = Recommendation & {
  aiStatus: AiStatus
  rejectionReason: string | null
  wouldBeBlocked: boolean
  categorySlotInfo: string | null
}

export type OpportunitySummary = {
  timestamp: string
  totalMarkets: number
  recommendations: BlockedOpportunity[]
  bookFull: boolean
  openCount: number
  maxPositions: number
  categoryCounts: Record<string, number>
  aiVetoedCount: number
  aiTotalCandidates: number
  blockedCount: number
  tradableCount: number
}

function deriveAiStatus(r: Recommendation): AiStatus {
  if (!r.ai_was_candidate) return "not_run"
  if (r.ai_returned_skip) return "skip_or_no_result"
  if (r.ai_recommendation?.toUpperCase() === "SKIP") return "skip_or_no_result"
  if (
    r.ai_recommendation &&
    r.recommendation &&
    r.ai_recommendation.toUpperCase() === r.recommendation.toUpperCase()
  )
    return "agree"
  if (r.ai_recommendation) return "disagree"
  return "skip_or_no_result"
}

import { inferCategory as inferCategoryShared } from "@/lib/category"

function hasCategory(t: { category?: string; question?: string }): boolean {
  return Boolean(t.category || t.question)
}

function inferCategory(t: { category?: string; question?: string }): string {
  if (t.category) return t.category
  return inferCategoryShared(t.question ?? "")
}

export async function summarizeOpportunities(
  botId: string = BOT_ID,
): Promise<OpportunitySummary | null> {
  const [research, state] = await Promise.all([
    readMarketResearch(botId),
    readPaperState(botId),
  ])

  const latest = research?.latest
  if (!latest) return null

  const MAX_POSITIONS = 10
  const MAX_PER_CATEGORY = 3
  const MIN_CONFIDENCE = 0.65
  const MIN_LIQUIDITY = 200

  const openPositions = new Map<string, boolean>()
  const categoryCounts: Record<string, number> = {}
  let openCount = 0

  if (state) {
    for (const trades of Object.values(state.positions)) {
      for (const t of trades) {
        if (t.status === "OPEN") {
          openPositions.set(t.market_id, true)
          openCount++
          if (hasCategory(t)) {
            const cat = inferCategory(t)
            categoryCounts[cat] = (categoryCounts[cat] ?? 0) + 1
          }
        }
      }
    }
  }

  const bookFull = openCount >= MAX_POSITIONS

  let aiVetoedCount = 0
  let aiTotalCandidates = 0

  const recommendations: BlockedOpportunity[] = latest.recommendations.map(
    (r) => {
      const aiStatus = deriveAiStatus(r)

      if (r.ai_was_candidate) {
        aiTotalCandidates++
        if (aiStatus === "skip_or_no_result") aiVetoedCount++
      }

      let rejectionReason: string | null = null
      let categorySlotInfo: string | null = null

      if (aiStatus === "skip_or_no_result") {
        rejectionReason = "ai_veto"
      } else if ((r.confidence ?? 0) < MIN_CONFIDENCE) {
        rejectionReason = "low_confidence"
      } else if (openPositions.has(r.market_id)) {
        rejectionReason = "existing_open"
      } else if (bookFull) {
        rejectionReason = "max_positions"
      } else {
        const cat = r.category ?? "other"
        const effectiveCap = cat === "other" ? MAX_POSITIONS : MAX_PER_CATEGORY
        const catCount = categoryCounts[cat] ?? 0
        if (catCount >= effectiveCap) {
          rejectionReason = "category_cap"
          categorySlotInfo = `${cat}: ${catCount}/${effectiveCap}`
        }
      }

      if (!rejectionReason && (r.liquidity ?? Infinity) < MIN_LIQUIDITY) {
        rejectionReason = "liquidity"
      }

      const cat = r.category ?? "other"
      const catCount = categoryCounts[cat] ?? 0
      if (!categorySlotInfo && catCount > 0) {
        const effectiveCap = cat === "other" ? MAX_POSITIONS : MAX_PER_CATEGORY
        categorySlotInfo = `${cat}: ${catCount}/${effectiveCap}`
      }

      return {
        ...r,
        aiStatus,
        rejectionReason,
        wouldBeBlocked: rejectionReason !== null,
        categorySlotInfo,
      }
    },
  )

  recommendations.sort((a, b) => {
    if (a.wouldBeBlocked !== b.wouldBeBlocked)
      return a.wouldBeBlocked ? 1 : -1
    const ea = a.estimated_ev_exec ?? a.estimated_ev ?? -Infinity
    const eb = b.estimated_ev_exec ?? b.estimated_ev ?? -Infinity
    if (eb !== ea) return eb - ea
    return (b.confidence ?? 0) - (a.confidence ?? 0)
  })

  const blockedCount = recommendations.filter((r) => r.wouldBeBlocked).length
  const tradableCount = recommendations.length - blockedCount

  return {
    timestamp: latest.timestamp,
    totalMarkets: latest.total_markets_analyzed ?? 0,
    recommendations,
    bookFull,
    openCount,
    maxPositions: MAX_POSITIONS,
    categoryCounts,
    aiVetoedCount,
    aiTotalCandidates,
    blockedCount,
    tradableCount,
  }
}
