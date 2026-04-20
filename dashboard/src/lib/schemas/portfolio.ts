import { z } from "zod"

/** A single trade row inside paper_trading_state.json. */
export const TradeSchema = z.object({
  trade_id: z.number().int(),
  timestamp: z.string(),
  market_id: z.string(),
  outcome: z.string(),
  amount: z.number(),
  probability: z.number(),
  payout_if_win: z.number().nullable().optional(),
  profit_if_win: z.number().nullable().optional(),
  profit_if_lose: z.number().nullable().optional(),
  profit: z.number().nullable().optional(),
  status: z.string(),
  /** Some trades carry strategy/AI metadata; tolerated as passthrough. */
  question: z.string().optional(),
  category: z.string().optional(),
  strategies: z.array(z.string()).optional(),
  resolved_at: z.string().optional(),
  // Phase 2.1 classification fields
  entry_probability: z.number().nullable().optional(),
  term: z.string().optional(),
  resolvability: z.string().optional(),
  position_class: z.string().optional(),
  close_time_ms: z.number().nullable().optional(),
  // Phase 2.2 repricing fields (in-place on each OPEN leg)
  current_probability: z.number().nullable().optional(),
  current_unrealised_pnl: z.number().nullable().optional(),
  last_repriced_at: z.string().optional(),
  is_resolved_on_api: z.boolean().optional(),
  // Phase 2.3 early-close
  actual_outcome: z.string().optional(),
  // Phase 2.4 STRANDED/ABANDONED metadata (only present when flipped)
  stranded_at: z.string().optional(),
  stranded_reason: z.string().optional(),
  abandoned_at: z.string().optional(),
  abandoned_reason: z.string().optional(),
  // Phase 1.4 backfilled fields
  estimated_ev: z.number().nullable().optional(),
  ai_confidence: z.number().nullable().optional(),
  confidence: z.number().nullable().optional(),
})
export type Trade = z.infer<typeof TradeSchema>

export const PaperStateSchema = z.object({
  balance: z.number(),
  positions: z.record(z.string(), z.array(TradeSchema)),
  trade_history: z.array(TradeSchema),
  saved_at: z.string().optional(),
  initial_balance: z.number().optional(),
})
export type PaperState = z.infer<typeof PaperStateSchema>
