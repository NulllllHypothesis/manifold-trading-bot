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
  dedupWinners: Record<string, number>
  noActiveSignals: number
  finalByCategory: Record<string, number>
  finalByStrategyMix: Record<string, number>
  preFilter: {
    skippedResolved: number
    skippedLowLiquidity: number
    skippedStale: number
    skippedNoise: number
  }
  thinMarketConfirmations: number
  tiedVotesDropped: number
  topEvAtExec: Array<[string, number]>
  passedFilter: number
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

  let skippedResolved = 0
  let skippedLowLiquidity = 0
  let skippedStale = 0
  let skippedNoise = 0
  let thinMarketConfirmations = 0
  let tiedVotesDropped = 0

  const dedupAcc: Record<string, number> = {}
  const catAcc: Record<string, number> = {}
  const mixAcc: Record<string, number> = {}
  let noActiveSignals = 0
  for (const r of recentR) {
    for (const [family, n] of Object.entries(r.dedup_winners ?? {})) {
      dedupAcc[family] = (dedupAcc[family] ?? 0) + n
    }
    for (const [cat, n] of Object.entries(r.final_by_category ?? {})) {
      catAcc[cat] = (catAcc[cat] ?? 0) + n
    }
    for (const [mix, n] of Object.entries(r.final_by_strategy_mix ?? {})) {
      mixAcc[mix] = (mixAcc[mix] ?? 0) + n
    }
    noActiveSignals += r.no_active_signals ?? 0
    skippedResolved += r.skipped_resolved ?? 0
    skippedLowLiquidity += r.skipped_low_liquidity ?? 0
    skippedStale += r.skipped_stale ?? 0
    skippedNoise += r.skipped_noise ?? 0
    thinMarketConfirmations += r.thin_market_confirmations ?? 0
    tiedVotesDropped += r.tied_votes_dropped ?? 0
  }

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
    dedupWinners: dedupAcc,
    noActiveSignals,
    finalByCategory: catAcc,
    finalByStrategyMix: mixAcc,
    preFilter: {
      skippedResolved,
      skippedLowLiquidity,
      skippedStale,
      skippedNoise,
    },
    thinMarketConfirmations,
    tiedVotesDropped,
    topEvAtExec: (recentT.at(-1)?.top_ev_at_exec ?? []) as Array<[string, number]>,
    passedFilter: recentT.reduce((s, t) => s + (t.passed_filter ?? 0), 0),
  }
}
