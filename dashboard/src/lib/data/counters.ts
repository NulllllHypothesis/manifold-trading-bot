import "server-only"
import { DATA_PATHS, BOT_ID } from "@/lib/config"
import {
  ResearchCounterSchema,
  TraderCounterSchema,
  type ResearchCounter,
  type TraderCounter,
} from "@/lib/schemas/counters"
import { readJsonLinesFile } from "./_io"

export async function readResearchCounters(
  _botId: string = BOT_ID,
  options: { tailLines?: number } = {},
): Promise<ResearchCounter[]> {
  return readJsonLinesFile(
    DATA_PATHS.researchCounters(),
    ResearchCounterSchema,
    options,
  )
}

export async function readTraderCounters(
  _botId: string = BOT_ID,
  options: { tailLines?: number } = {},
): Promise<TraderCounter[]> {
  return readJsonLinesFile(
    DATA_PATHS.traderCounters(),
    TraderCounterSchema,
    options,
  )
}

export type FunnelStage = {
  label: string
  value: number
  /** Optional drill-down breakdown */
  breakdown?: Array<{ label: string; value: number }>
}

export type CounterSummary = {
  windowHours: number
  researchRuns: number
  traderRuns: number
  marketsFetched: number
  finalRecommendations: number
  tradesExecuted: number
  rejectionBreakdown: Array<{ reason: string; count: number }>
  aiFlow: {
    eligible: number
    cooled: number
    analyzed: number
    agree: number
    disagree: number
    skip: number
    noResult: number
  }
  /** Stages for a research-to-execution funnel chart. */
  funnel: FunnelStage[]
}

export async function summarizeRecentCounters(
  botId: string = BOT_ID,
  options: { windowHours?: number } = {},
): Promise<CounterSummary> {
  const windowHours = options.windowHours ?? 24
  const cutoff = Date.now() - windowHours * 3600_000

  const [research, trader] = await Promise.all([
    readResearchCounters(botId),
    readTraderCounters(botId),
  ])

  const recentR = research.filter((r) => Date.parse(r.timestamp) >= cutoff)
  const recentT = trader.filter((r) => Date.parse(r.timestamp) >= cutoff)

  const sumR = (key: keyof ResearchCounter) =>
    recentR.reduce((acc, r) => acc + ((r[key] as number | undefined) ?? 0), 0)

  const marketsFetched = sumR("markets_fetched")
  const finalRecommendations = sumR("final_recommendations")
  const aiEligible = sumR("ai_eligible")
  const aiCooled = sumR("ai_cooled_down")
  const aiAnalyzed = sumR("ai_analyzed")
  const aiAgree = sumR("ai_agree")
  const aiDisagree = sumR("ai_disagree")
  const aiSkip = sumR("ai_skip")
  const aiNoResult = sumR("ai_no_result")

  const tradesExecuted = recentT.reduce(
    (acc, r) => acc + (r.trades_executed ?? 0),
    0,
  )
  const rejectionsAcc: Record<string, number> = {}
  for (const t of recentT) {
    for (const [reason, n] of Object.entries(t.rejected ?? {})) {
      if (!n) continue
      rejectionsAcc[reason] = (rejectionsAcc[reason] ?? 0) + n
    }
  }
  const rejectionBreakdown = Object.entries(rejectionsAcc)
    .map(([reason, count]) => ({ reason, count }))
    .sort((a, b) => b.count - a.count)

  const funnel: FunnelStage[] = [
    { label: "Markets fetched", value: marketsFetched },
    { label: "Final recommendations", value: finalRecommendations },
    { label: "AI eligible", value: aiEligible },
    { label: "AI analyzed", value: aiAnalyzed },
    { label: "Trades executed", value: tradesExecuted },
  ]

  return {
    windowHours,
    researchRuns: recentR.length,
    traderRuns: recentT.length,
    marketsFetched,
    finalRecommendations,
    tradesExecuted,
    rejectionBreakdown,
    aiFlow: {
      eligible: aiEligible,
      cooled: aiCooled,
      analyzed: aiAnalyzed,
      agree: aiAgree,
      disagree: aiDisagree,
      skip: aiSkip,
      noResult: aiNoResult,
    },
    funnel,
  }
}
