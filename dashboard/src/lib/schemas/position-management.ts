import { z } from "zod"

/**
 * Shape of `pending_closes.json` entries, written by
 * `scripts/evaluate_positions.py`. Matches Phase 2.3 and stays permissive
 * via `passthrough()` because the producer is still evolving.
 */
export const PendingCloseSchema = z
  .object({
    id: z.number().int(),
    market_id: z.string(),
    trade_id: z.number().int().nullable().optional(),
    n_legs: z.number().int().optional(),
    question: z.string().optional(),
    outcome: z.string().nullable().optional(),
    amount: z.number().nullable().optional(),
    entry_probability: z.number().nullable().optional(),
    current_probability: z.number().nullable().optional(),
    current_unrealised_pnl: z.number().nullable().optional(),
    position_score: z.number().nullable().optional(),
    reason: z.string().optional(),
    proposed_at: z.string(),
    status: z.string(),
    executed_at: z.string().optional(),
    dismissed_at: z.string().optional(),
  })
  .passthrough()
export type PendingClose = z.infer<typeof PendingCloseSchema>

export const PendingClosesFileSchema = z.array(PendingCloseSchema)

/**
 * Shape of `pending_abandons.json` entries, written by
 * `scripts/detect_stale_positions.py` (Phase 2.4). A write-off proposal —
 * realises a terminal loss on approval so it's intentionally propose-only.
 */
export const PendingAbandonSchema = z
  .object({
    id: z.number().int(),
    market_id: z.string(),
    trade_ids: z.array(z.number().int()).optional(),
    question: z.string().optional(),
    outcome: z.string().nullable().optional(),
    total_amount: z.number().nullable().optional(),
    reason: z.string().optional(),
    proposed_at: z.string(),
    status: z.string(),
    executed_at: z.string().optional(),
    dismissed_at: z.string().optional(),
  })
  .passthrough()
export type PendingAbandon = z.infer<typeof PendingAbandonSchema>

export const PendingAbandonsFileSchema = z.array(PendingAbandonSchema)

/**
 * One row from calibration.db:position_snapshots (Phase 2.2).
 * Append-only repricing log. Use for P&L-trajectory visualisation.
 */
export const PositionSnapshotSchema = z.object({
  id: z.number().int(),
  trade_id: z.number().int().nullable().optional(),
  market_id: z.string(),
  snapshot_at: z.string(),
  current_probability: z.number().nullable().optional(),
  entry_probability: z.number().nullable().optional(),
  outcome: z.string().nullable().optional(),
  amount: z.number().nullable().optional(),
  unrealised_pnl: z.number().nullable().optional(),
  days_held: z.number().nullable().optional(),
  is_resolved: z.number().nullable().optional(),
  api_error: z.number().nullable().optional(),
})
export type PositionSnapshot = z.infer<typeof PositionSnapshotSchema>
