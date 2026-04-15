import { z } from "zod"

export const RecommendationSchema = z.object({
  market_id: z.string(),
  question: z.string(),
  category: z.string().optional(),
  probability: z.number().nullable().optional(),
  volume: z.number().nullable().optional(),
  volume24h: z.number().nullable().optional(),
  liquidity: z.number().nullable().optional(),
  unique_bettors: z.number().nullable().optional(),
  last_bet_time_ms: z.number().nullable().optional(),
  recommendation: z.string().nullable().optional(),
  confidence: z.number().nullable().optional(),
  strategies: z.array(z.string()).optional(),
  priority_boost: z.number().nullable().optional(),
  existing_position: z.boolean().nullable().optional(),
  analyzed_at: z.string().optional(),
  news_context: z.unknown().optional(),
  ai_was_candidate: z.boolean().optional(),
  ai_returned_skip: z.boolean().optional(),
  ai_recommendation: z.string().nullable().optional(),
  ai_confidence: z.number().nullable().optional(),
  ai_reasoning: z.string().nullable().optional(),
  ai_source: z.string().nullable().optional(),
  estimated_ev_ref: z.number().nullable().optional(),
  estimated_ev_exec: z.number().nullable().optional(),
  estimated_ev: z.number().nullable().optional(),
})
export type Recommendation = z.infer<typeof RecommendationSchema>

export const ResearchSnapshotSchema = z.object({
  schema_version: z.union([z.string(), z.number()]).optional(),
  timestamp: z.string(),
  recommendations: z.array(RecommendationSchema),
  total_markets_analyzed: z.number().optional(),
})
export type ResearchSnapshot = z.infer<typeof ResearchSnapshotSchema>

export const MarketResearchSchema = z.object({
  latest: ResearchSnapshotSchema.optional(),
  history: z.array(ResearchSnapshotSchema).optional(),
})
export type MarketResearch = z.infer<typeof MarketResearchSchema>
