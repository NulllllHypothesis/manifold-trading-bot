import { z } from "zod"

export const PendingSwapSchema = z.object({
  swap_id: z.union([z.string(), z.number()]).optional(),
  status: z.string(),
  proposed_at: z.string().optional(),
  expires_at: z.string().optional(),
  close_position: z.unknown().optional(),
  open_recommendation: z.unknown().optional(),
  ev_advantage: z.number().nullable().optional(),
}).passthrough()
export type PendingSwap = z.infer<typeof PendingSwapSchema>

export const PendingSwapsFileSchema = z.array(PendingSwapSchema)
export type PendingSwapsFile = z.infer<typeof PendingSwapsFileSchema>
