import { z } from "zod"

/**
 * Matches the shape written by scripts/position_swap_checker.py.
 * Fields are optional/passthrough because the producer evolves.
 */
export const PendingSwapSchema = z
  .object({
    id: z.union([z.string(), z.number()]).optional(),
    swap_id: z.union([z.string(), z.number()]).optional(),
    status: z.string(),
    proposed_at: z.string().optional(),
    expires_at: z.string().optional(),

    close_market_id: z.string().optional(),
    close_question: z.string().optional(),
    close_outcome: z.string().optional(),
    close_entry_probability: z.number().nullable().optional(),
    close_current_probability: z.number().nullable().optional(),
    close_unrealised_pnl: z.number().nullable().optional(),

    open_market_id: z.string().optional(),
    open_question: z.string().optional(),
    open_recommendation: z
      .object({
        market_id: z.string().optional(),
        question: z.string().optional(),
        recommendation: z.string().nullable().optional(),
        confidence: z.number().nullable().optional(),
        probability: z.number().nullable().optional(),
        estimated_ev: z.number().nullable().optional(),
        estimated_ev_ref: z.number().nullable().optional(),
        estimated_ev_exec: z.number().nullable().optional(),
      })
      .passthrough()
      .optional(),
    open_strategies: z.array(z.string()).optional(),

    ev_advantage: z.number().nullable().optional(),
  })
  .passthrough()
export type PendingSwap = z.infer<typeof PendingSwapSchema>

export const PendingSwapsFileSchema = z.array(PendingSwapSchema)
export type PendingSwapsFile = z.infer<typeof PendingSwapsFileSchema>
