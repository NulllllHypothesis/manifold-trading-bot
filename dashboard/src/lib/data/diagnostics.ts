import "server-only"
import { DATA_PATHS, BOT_ID, CRON_JOBS } from "@/lib/config"
import { fileStat } from "./_io"

/** Freshness threshold in seconds for a "stale" warning. */
const STALENESS_THRESHOLDS: Record<string, number> = {
  marketResearch: 90 * 60, // research is hourly → stale after 90 min
  paperState: 90 * 60, // updated by trader hourly
  researchCounters: 90 * 60,
  traderCounters: 90 * 60,
  strategyWeights: 8 * 24 * 3600, // weekly job
  calibrationTable: 8 * 24 * 3600,
  m2Audit: 8 * 24 * 3600,
  backtestReport: 8 * 24 * 3600,
  evalReport: 30 * 24 * 3600, // ad-hoc
  newsCache: 6 * 3600, // news rotates often
  aiCooldown: 6 * 3600,
  pendingSwaps: 24 * 3600, // swap proposals expire
  autoTrades: 8 * 24 * 3600,
  calibrationDb: 8 * 24 * 3600,
  snapshotsDb: 8 * 24 * 3600,
}

export type FileFreshness = {
  key: string
  path: string
  exists: boolean
  mtime: string | null
  ageSeconds: number | null
  sizeBytes: number | null
  staleAfterSeconds: number | null
  isStale: boolean
}

export type DiagnosticsSnapshot = {
  generatedAt: string
  workspaceRoot: string
  files: FileFreshness[]
  cronJobs: typeof CRON_JOBS
}

export async function readDiagnostics(
  botId: string = BOT_ID,
): Promise<DiagnosticsSnapshot> {
  const files: FileFreshness[] = []
  for (const [key, getter] of Object.entries(DATA_PATHS)) {
    const path = getter()
    const stat = fileStat(path)
    const threshold = STALENESS_THRESHOLDS[key] ?? null
    const isStale =
      stat.exists &&
      threshold !== null &&
      stat.ageSeconds !== null &&
      stat.ageSeconds > threshold
    files.push({
      key,
      path,
      exists: stat.exists,
      mtime: stat.mtime ? stat.mtime.toISOString() : null,
      ageSeconds: stat.ageSeconds,
      sizeBytes: stat.sizeBytes,
      staleAfterSeconds: threshold,
      isStale,
    })
  }

  // Sort: missing/stale first, then by name
  files.sort((a, b) => {
    const score = (f: FileFreshness) =>
      (!f.exists ? 0 : f.isStale ? 1 : 2)
    const sa = score(a)
    const sb = score(b)
    if (sa !== sb) return sa - sb
    return a.key.localeCompare(b.key)
  })

  return {
    generatedAt: new Date().toISOString(),
    workspaceRoot: DATA_PATHS.marketResearch().replace(
      /\/market_research\.json$/,
      "",
    ),
    files,
    cronJobs: CRON_JOBS,
  }
}
