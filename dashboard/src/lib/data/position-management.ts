import "server-only"
import { existsSync } from "node:fs"
import Database from "better-sqlite3"
import { DATA_PATHS, BOT_ID } from "@/lib/config"
import {
  PendingClosesFileSchema,
  PendingAbandonsFileSchema,
  PositionSnapshotSchema,
  type PendingClose,
  type PendingAbandon,
  type PositionSnapshot,
} from "@/lib/schemas/position-management"
import { readJsonFile } from "./_io"

/** Read pending_closes.json (Phase 2.3). Empty list if file doesn't exist yet. */
export async function readPendingCloses(
  _botId: string = BOT_ID,
): Promise<PendingClose[]> {
  const data = await readJsonFile(DATA_PATHS.pendingCloses(), PendingClosesFileSchema)
  return data ?? []
}

/** Read pending_abandons.json (Phase 2.4). Empty list if file doesn't exist. */
export async function readPendingAbandons(
  _botId: string = BOT_ID,
): Promise<PendingAbandon[]> {
  const data = await readJsonFile(DATA_PATHS.pendingAbandons(), PendingAbandonsFileSchema)
  return data ?? []
}

/**
 * Read the latest-N position_snapshots rows from calibration.db (Phase 2.2).
 *
 * Open/read-only, no WAL writes, closed after every call — we're on a
 * request-scoped data path and the DB file is also being appended to by
 * the Python reprice script. Keeping the handle's lifetime tiny avoids
 * locking surprises.
 *
 * If the DB or table is missing (fresh install, or Phase 2.2 hasn't run
 * yet) returns an empty array rather than throwing — matches the
 * "null-soft" contract of the rest of the data layer.
 */
export function readPositionSnapshots(
  options: { limit?: number; marketIds?: string[] } = {},
  _botId: string = BOT_ID,
): PositionSnapshot[] {
  const dbPath = DATA_PATHS.calibrationDb()
  if (!existsSync(dbPath)) return []

  const db = new Database(dbPath, { readonly: true, fileMustExist: true })
  try {
    // If the table doesn't exist, sqlite_master lookup beats a thrown
    // query error — clearer intent and faster than a try/catch around SELECT.
    const table = db
      .prepare(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='position_snapshots'",
      )
      .get() as { name?: string } | undefined
    if (!table?.name) return []

    const limit = options.limit ?? 2000
    const params: Record<string, unknown> = { limit }
    let where = ""
    if (options.marketIds && options.marketIds.length > 0) {
      // Build a named-param IN (...) clause so better-sqlite3 can prepare it.
      const placeholders = options.marketIds
        .map((_, i) => `@mid${i}`)
        .join(", ")
      where = `WHERE market_id IN (${placeholders})`
      options.marketIds.forEach((id, i) => {
        params[`mid${i}`] = id
      })
    }

    const rows = db
      .prepare(
        `SELECT id, trade_id, market_id, snapshot_at, current_probability,
                entry_probability, outcome, amount, unrealised_pnl,
                days_held, is_resolved, api_error
         FROM position_snapshots
         ${where}
         ORDER BY snapshot_at DESC
         LIMIT @limit`,
      )
      .all(params) as unknown[]

    const parsed: PositionSnapshot[] = []
    for (const raw of rows) {
      const r = PositionSnapshotSchema.safeParse(raw)
      if (r.success) parsed.push(r.data)
    }
    // Return oldest-first so line charts read left-to-right.
    return parsed.reverse()
  } finally {
    db.close()
  }
}
