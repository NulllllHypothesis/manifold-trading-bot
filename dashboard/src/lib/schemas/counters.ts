import { z } from "zod"

export const RawFiresSchema = z.record(z.string(), z.number())

export const ResearchCounterSchema = z.object({
  timestamp: z.string(),
  markets_fetched: z.number().optional(),
  skipped_resolved: z.number().optional(),
  skipped_low_liquidity: z.number().optional(),
  skipped_stale: z.number().optional(),
  skipped_noise: z.number().optional(),
  raw_fires: RawFiresSchema.optional(),
  dedup_winners: z.record(z.string(), z.number()).optional(),
  thin_market_confirmations: z.number().optional(),
  no_active_signals: z.number().optional(),
  tied_votes_dropped: z.number().optional(),
  ai_eligible: z.number().optional(),
  ai_cooled_down: z.number().optional(),
  ai_analyzed: z.number().optional(),
  ai_agree: z.number().optional(),
  ai_disagree: z.number().optional(),
  ai_skip: z.number().optional(),
  ai_no_result: z.number().optional(),
  final_recommendations: z.number().optional(),
  final_by_strategy_mix: z.record(z.string(), z.number()).optional(),
  final_by_category: z.record(z.string(), z.number()).optional(),
})
export type ResearchCounter = z.infer<typeof ResearchCounterSchema>

export const TraderRejectionsSchema = z.object({
  ai_veto: z.number().optional(),
  low_confidence: z.number().optional(),
  existing_open: z.number().optional(),
  max_positions: z.number().optional(),
  category_cap: z.number().optional(),
  liquidity: z.number().optional(),
  kelly_no_edge: z.number().optional(),
  size_too_small: z.number().optional(),
  market_unverifiable: z.number().optional(),
}).catchall(z.number())
export type TraderRejections = z.infer<typeof TraderRejectionsSchema>

export const TraderCounterSchema = z.object({
  timestamp: z.string(),
  recommendations_loaded: z.number().optional(),
  rejected: TraderRejectionsSchema.optional(),
  passed_filter: z.number().optional(),
  top_ev_at_exec: z.array(z.unknown()).optional(),
  trades_executed: z.number().optional(),
})
export type TraderCounter = z.infer<typeof TraderCounterSchema>
