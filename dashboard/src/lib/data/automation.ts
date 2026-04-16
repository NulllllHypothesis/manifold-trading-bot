import "server-only"
import { CRON_JOBS, DATA_PATHS } from "@/lib/config"
import { fileStat } from "./_io"
import { readResearchCounters, readTraderCounters } from "./counters"

type CronOutputMapping = {
  fileKeys: string[]
  counterSource?: "research" | "trader"
}

const CRON_OUTPUT_MAP: Record<string, CronOutputMapping> = {
  research: { fileKeys: ["marketResearch", "researchCounters"], counterSource: "research" },
  resolution: { fileKeys: ["paperState"], counterSource: undefined },
  trader: { fileKeys: ["paperState", "traderCounters"], counterSource: "trader" },
  "daily-summary": { fileKeys: [], counterSource: undefined },
  harvest: { fileKeys: ["calibrationDb", "calibrationTable"], counterSource: undefined },
  "m2-audit": { fileKeys: ["m2Audit"], counterSource: undefined },
  "m3-reconstruct": { fileKeys: ["snapshotsDb"], counterSource: undefined },
  backtest: { fileKeys: ["strategyWeights"], counterSource: undefined },
  "ev-report": { fileKeys: [], counterSource: undefined },
  "live-weights": { fileKeys: ["strategyWeights"], counterSource: undefined },
}

export type CronJobStatus = {
  id: string
  schedule: string
  description: string
  intervalDescription: string
  outputFiles: Array<{
    key: string
    path: string
    exists: boolean
    ageSeconds: number | null
    sizeBytes: number | null
  }>
  lastRunTimestamp: string | null
  health: "healthy" | "stale" | "overdue" | "unknown"
  nextExpectedRun: string | null
}

function describeSchedule(cron: string): string {
  const [min, hour, _dom, _mon, dow] = cron.split(" ")
  if (dow === "0") return `Sundays ${hour!.padStart(2, "0")}:${min!.padStart(2, "0")} UTC`
  if (dow === "1") return `Mondays ${hour!.padStart(2, "0")}:${min!.padStart(2, "0")} UTC`
  if (hour === "*") return `Hourly at :${min!.padStart(2, "0")} UTC`
  if (dow === "*") return `Daily ${hour!.padStart(2, "0")}:${min!.padStart(2, "0")} UTC`
  return cron
}

function computeNextRun(cron: string, now: Date): Date {
  const [minStr, hourStr, , , dowStr] = cron.split(" ")
  const min = Number(minStr)
  const next = new Date(now)
  next.setUTCSeconds(0, 0)

  if (hourStr === "*") {
    next.setUTCMinutes(min)
    if (next <= now) next.setUTCHours(next.getUTCHours() + 1)
    return next
  }

  const hour = Number(hourStr)

  if (dowStr === "*") {
    next.setUTCHours(hour, min, 0, 0)
    if (next <= now) next.setUTCDate(next.getUTCDate() + 1)
    return next
  }

  const targetDow = Number(dowStr)
  next.setUTCHours(hour, min, 0, 0)
  const currentDow = next.getUTCDay()
  let daysAhead = targetDow - currentDow
  if (daysAhead < 0) daysAhead += 7
  if (daysAhead === 0 && next <= now) daysAhead = 7
  next.setUTCDate(next.getUTCDate() + daysAhead)
  return next
}

function computeInterval(cron: string): number {
  const [, hourStr, , , dowStr] = cron.split(" ")
  if (hourStr === "*") return 3600
  if (dowStr === "*") return 86400
  return 7 * 86400
}

export async function readAutomationStatus(): Promise<CronJobStatus[]> {
  const [researchCounters, traderCounters] = await Promise.all([
    readResearchCounters(undefined, { tailLines: 5 }),
    readTraderCounters(undefined, { tailLines: 5 }),
  ])

  const lastResearchTs = researchCounters.at(-1)?.timestamp ?? null
  const lastTraderTs = traderCounters.at(-1)?.timestamp ?? null

  const now = new Date()

  return CRON_JOBS.map((job) => {
    const mapping = CRON_OUTPUT_MAP[job.id]
    const outputFiles = (mapping?.fileKeys ?? []).map((key) => {
      const pathFn = DATA_PATHS[key as keyof typeof DATA_PATHS]
      const p = pathFn()
      const stat = fileStat(p)
      return {
        key,
        path: p,
        exists: stat.exists,
        ageSeconds: stat.ageSeconds,
        sizeBytes: stat.sizeBytes,
      }
    })

    let lastRunTimestamp: string | null = null
    if (mapping?.counterSource === "research") lastRunTimestamp = lastResearchTs
    else if (mapping?.counterSource === "trader") lastRunTimestamp = lastTraderTs

    const intervalSec = computeInterval(job.schedule)
    const staleThreshold = intervalSec * 1.5

    let health: CronJobStatus["health"] = "unknown"
    if (outputFiles.length > 0) {
      const primaryFile = outputFiles[0]!
      if (!primaryFile.exists) {
        health = "unknown"
      } else if (primaryFile.ageSeconds !== null && primaryFile.ageSeconds > staleThreshold) {
        health = "overdue"
      } else {
        health = "healthy"
      }
    } else if (lastRunTimestamp) {
      const age = (Date.now() - Date.parse(lastRunTimestamp)) / 1000
      health = age > staleThreshold ? "overdue" : "healthy"
    }

    const nextRun = computeNextRun(job.schedule, now)

    return {
      id: job.id,
      schedule: job.schedule,
      description: job.description,
      intervalDescription: describeSchedule(job.schedule),
      outputFiles,
      lastRunTimestamp,
      health,
      nextExpectedRun: nextRun.toISOString(),
    }
  })
}
