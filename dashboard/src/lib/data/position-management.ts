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

/**
 * Mirrors `automation/auto_trader.py:_OBSOLETE_POSITION_CLASS_MIGRATIONS`.
 *
 * Positions persisted during intermediate 4-bucket revisions of Phase 2.1
 * still carry `long_reliable` / `long_risky` (old rename) or `long_high` /
 * `long_mid_low` (even older rename) values. The backend normalises these
 * at read-time so slot accounting stays honest; the dashboard must do the
 * same or it will (a) paint raw legacy labels on the UI and (b) miss them
 * in the "classification gap" alert while the bot silently treats them as
 * long_or_uncertain. Keep this map in sync with the Python source; single
 * source of truth is the `.py` file since that's where new obsolete values
 * would be introduced first.
 */
const OBSOLETE_POSITION_CLASS_MIGRATIONS: Record<string, string> = {
  long_reliable: "long_or_uncertain",
  long_risky: "long_or_uncertain",
  long_high: "long_or_uncertain",
  long_mid_low: "long_or_uncertain",
}

export function normalizePositionClass(
  pclass: string | null | undefined,
): string | null | undefined {
  if (!pclass) return pclass
  return OBSOLETE_POSITION_CLASS_MIGRATIONS[pclass] ?? pclass
}

/** True if the value is one of the obsolete 4-bucket names. */
export function isObsoletePositionClass(
  pclass: string | null | undefined,
): boolean {
  return !!pclass && pclass in OBSOLETE_POSITION_CLASS_MIGRATIONS
}

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

/**
 * Aggregate per-leg snapshots into one row per (market_id, snapshot_at) run.
 *
 * Phase 2.2 writes one row per OPEN leg per repricing tick, so a market
 * with two legs produces two rows at the same `snapshot_at`. The trader's
 * close decision in Phase 2.3 sums across legs — if the chart plots
 * per-leg rows, it renders a zig-zag that looks like market-level P&L is
 * swinging when it's really two separate legs at the same instant.
 *
 * This aggregator matches the scoring semantics: sum `unrealised_pnl` per
 * run, skip rows flagged `api_error=1` so they don't count as $0 samples.
 */
export function aggregateSnapshotsByRun(
  snapshots: PositionSnapshot[],
): PositionSnapshot[] {
  const byRun = new Map<string, PositionSnapshot[]>()
  for (const s of snapshots) {
    if (s.api_error) continue
    const existing = byRun.get(s.snapshot_at)
    if (existing) existing.push(s)
    else byRun.set(s.snapshot_at, [s])
  }

  const out: PositionSnapshot[] = []
  for (const [runTs, legs] of byRun) {
    const first = legs[0]
    let pnlSum = 0
    let pnlCount = 0
    let amountSum = 0
    let anyResolved = 0
    for (const l of legs) {
      if (typeof l.unrealised_pnl === "number") {
        pnlSum += l.unrealised_pnl
        pnlCount += 1
      }
      if (typeof l.amount === "number") amountSum += l.amount
      if (l.is_resolved) anyResolved = 1
    }
    out.push({
      id: first.id,
      trade_id: null, // aggregate — individual leg ids discarded
      market_id: first.market_id,
      snapshot_at: runTs,
      current_probability: first.current_probability ?? null,
      entry_probability: null,
      outcome: legs.length === 1 ? first.outcome ?? null : null,
      amount: amountSum || null,
      unrealised_pnl: pnlCount > 0 ? pnlSum : null,
      days_held: first.days_held ?? null,
      is_resolved: anyResolved,
      api_error: 0,
    })
  }

  // Chronological for line charts.
  out.sort((a, b) => a.snapshot_at.localeCompare(b.snapshot_at))
  return out
}

/**
 * Convenience: read snapshots for one market and collapse to per-run rows
 * suitable for plotting a single line. Callers that need the raw per-leg
 * rows can still use `readPositionSnapshots` directly.
 */
export function readMarketTrajectory(
  marketId: string,
  options: { limit?: number } = {},
  botId: string = BOT_ID,
): PositionSnapshot[] {
  const raw = readPositionSnapshots(
    { marketIds: [marketId], limit: options.limit ?? 500 },
    botId,
  )
  return aggregateSnapshotsByRun(raw)
}

/**
 * Batch-load market-aggregated trajectories for multiple markets in a
 * single DB read. Returns `{ [market_id]: PositionSnapshot[] }` (each
 * value already grouped per-run by aggregateSnapshotsByRun).
 *
 * Used by the /positions page to pre-hydrate every OPEN market so the
 * client-side row-expand shows the chart instantly, without a fetch
 * round-trip per click.
 *
 * The SQL does one IN (...) + ORDER BY snapshot_at DESC per market,
 * which better-sqlite3 handles cheaply — 10 markets × 200 points is a
 * ~2k-row read well inside a few ms.
 */
export function readMarketTrajectoriesForMarkets(
  marketIds: string[],
  options: { perMarketLimit?: number } = {},
  _botId: string = BOT_ID,
): Record<string, PositionSnapshot[]> {
  if (marketIds.length === 0) return {}

  // Fetch with a generous overall limit so we don't truncate any market
  // before aggregation. 200 points × N markets is a safe upper bound.
  const perMarketLimit = options.perMarketLimit ?? 200
  const raw = readPositionSnapshots({
    marketIds,
    limit: perMarketLimit * marketIds.length,
  })

  // Bucket by market_id, then aggregate each bucket independently.
  const buckets = new Map<string, PositionSnapshot[]>()
  for (const s of raw) {
    const arr = buckets.get(s.market_id)
    if (arr) arr.push(s)
    else buckets.set(s.market_id, [s])
  }
  const out: Record<string, PositionSnapshot[]> = {}
  for (const [mid, rows] of buckets) {
    out[mid] = aggregateSnapshotsByRun(rows).slice(-perMarketLimit)
  }
  return out
}
