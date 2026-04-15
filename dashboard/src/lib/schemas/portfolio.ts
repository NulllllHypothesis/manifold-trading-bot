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
