import "server-only"
import fs from "node:fs/promises"
import { existsSync } from "node:fs"
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
  trader: { fileKeys: ["traderCounters"], counterSource: "trader" },
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
  source: "os-crontab" | "config-static"
  outputFiles: Array<{
    key: string
    path: string
    exists: boolean
    ageSeconds: number | null
    sizeBytes: number | null
  }>
  lastRunTimestamp: string | null
  lastRunAgeSeconds: number | null
  health: "healthy" | "stale" | "overdue" | "unknown"
  nextExpectedRun: string | null
}

export type OpenClawJob = {
  id: string
  name: string
  schedule: string
  enabled: boolean
  message: string
}

export type AutomationSnapshot = {
  osCronJobs: CronJobStatus[]
  openclawJobs: OpenClawJob[]
  liveCrontabAvailable: boolean
  openclawJobsAvailable: boolean
}

function describeSchedule(cron: string): string {
  const parts = cron.trim().split(/\s+/)
  if (parts.length < 5) return cron
  const [min, hour, , , dow] = parts
  if (dow === "0") return `Sundays ${hour!.padStart(2, "0")}:${min!.padStart(2, "0")} UTC`
  if (dow === "1") return `Mondays ${hour!.padStart(2, "0")}:${min!.padStart(2, "0")} UTC`
  if (hour === "*") return `Hourly at :${min!.padStart(2, "0")} UTC`
  if (dow === "*") return `Daily ${hour!.padStart(2, "0")}:${min!.padStart(2, "0")} UTC`
  return cron
}

function computeNextRun(cron: string, now: Date): Date {
  const parts = cron.trim().split(/\s+/)
  if (parts.length < 5) return new Date(now.getTime() + 3600_000)
  const [minStr, hourStr, , , dowStr] = parts
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
  const parts = cron.trim().split(/\s+/)
  if (parts.length < 5) return 3600
  const [, hourStr, , , dowStr] = parts
  if (hourStr === "*") return 3600
  if (dowStr === "*") return 86400
  return 7 * 86400
}

function parseLiveCrontab(text: string): Array<{ schedule: string; command: string; description: string }> {
  const lines = text.split("\n")
  const entries: Array<{ schedule: string; command: string; description: string }> = []
  let lastComment = ""

  for (const line of lines) {
    const trimmed = line.trim()
    if (trimmed.startsWith("#")) {
      lastComment = trimmed.replace(/^#+\s*/, "")
      continue
    }
    if (!trimmed || trimmed.startsWith("SHELL") || trimmed.startsWith("PATH") || trimmed.startsWith("MAILTO")) {
      continue
    }
    const match = trimmed.match(/^(\S+\s+\S+\s+\S+\s+\S+\s+\S+)\s+(.+)$/)
    if (match) {
      entries.push({
        schedule: match[1]!,
        command: match[2]!,
        description: lastComment || (match[2]!.split("&&").pop()?.trim() ?? ""),
      })
      lastComment = ""
    }
  }
  return entries
}

function inferJobId(command: string): string | null {
  if (command.includes("auto_research")) return "research"
  if (command.includes("resolve_positions")) return "resolution"
  if (command.includes("auto_trader")) return "trader"
  if (command.includes("daily_summary")) return "daily-summary"
  if (command.includes("harvest_resolved") || command.includes("analyze_calibration")) return "harvest"
  if (command.includes("audit_resolved")) return "m2-audit"
  if (command.includes("reconstruct_snapshots")) return "m3-reconstruct"
  if (command.includes("backtest_from_snapshots")) return "backtest"
  if (command.includes("weekly_ev_report")) return "ev-report"
  if (command.includes("compute_strategy_weights")) return "live-weights"
  return null
}

export async function readAutomationStatus(
  nowMs?: number,
): Promise<AutomationSnapshot> {
  const [researchCounters, traderCounters] = await Promise.all([
    readResearchCounters(undefined, { tailLines: 5 }),
    readTraderCounters(undefined, { tailLines: 5 }),
  ])

  const lastResearchTs = researchCounters.at(-1)?.timestamp ?? null
  const lastTraderTs = traderCounters.at(-1)?.timestamp ?? null

  const now = nowMs ?? Date.now()
  const nowDate = new Date(now)

  const liveCrontabPath = DATA_PATHS.liveCrontab()
  const liveCrontabAvailable = existsSync(liveCrontabPath)

  let jobSource: "os-crontab" | "config-static" = "config-static"
  let jobEntries: Array<{ id: string; schedule: string; description: string }>

  if (liveCrontabAvailable) {
    const crontabText = await fs.readFile(liveCrontabPath, "utf-8")
    const parsed = parseLiveCrontab(crontabText)
    jobEntries = parsed.map((entry) => ({
      id: inferJobId(entry.command) ?? entry.command.slice(0, 30),
      schedule: entry.schedule,
      description: entry.description,
    }))
    jobSource = "os-crontab"
  } else {
    jobEntries = CRON_JOBS.map((j) => ({
      id: j.id,
      schedule: j.schedule,
      description: j.description,
    }))
  }

  const osCronJobs: CronJobStatus[] = jobEntries.map((job) => {
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

    let lastRunAgeSeconds: number | null = null
    if (lastRunTimestamp) {
      lastRunAgeSeconds = Math.floor((now - Date.parse(lastRunTimestamp)) / 1000)
    }

    let health: CronJobStatus["health"] = "unknown"

    if (lastRunTimestamp) {
      health = lastRunAgeSeconds !== null && lastRunAgeSeconds > staleThreshold
        ? "overdue"
        : "healthy"
    } else if (outputFiles.length > 0) {
      const freshest = outputFiles
        .filter((f) => f.exists && f.ageSeconds !== null)
        .sort((a, b) => (a.ageSeconds ?? Infinity) - (b.ageSeconds ?? Infinity))[0]
      if (freshest) {
        health = freshest.ageSeconds! > staleThreshold ? "overdue" : "healthy"
      }
    }

    const nextRun = computeNextRun(job.schedule, nowDate)

    return {
      id: job.id,
      schedule: job.schedule,
      description: job.description,
      intervalDescription: describeSchedule(job.schedule),
      source: jobSource,
      outputFiles,
      lastRunTimestamp,
      lastRunAgeSeconds,
      health,
      nextExpectedRun: nextRun.toISOString(),
    }
  })

  let openclawJobs: OpenClawJob[] = []
  const openclawPath = DATA_PATHS.openclawJobs()
  const openclawJobsAvailable = existsSync(openclawPath)

  if (openclawJobsAvailable) {
    try {
      const raw = JSON.parse(await fs.readFile(openclawPath, "utf-8")) as unknown
      const jobsArr: unknown[] = Array.isArray(raw)
        ? raw
        : (raw as Record<string, unknown>)?.jobs != null && Array.isArray((raw as Record<string, unknown>).jobs)
          ? (raw as Record<string, unknown>).jobs as unknown[]
          : []
      openclawJobs = jobsArr.map((j: unknown) => {
        const job = j as Record<string, unknown>
        const sched = job.schedule as Record<string, unknown> | string | undefined
        const schedExpr = typeof sched === "string"
          ? sched
          : typeof sched === "object" && sched !== null
            ? String((sched as Record<string, unknown>).expr ?? "")
            : ""
        const payload = (job.payload ?? {}) as Record<string, unknown>
        return {
          id: String(job.id ?? ""),
          name: String(job.name ?? job.description ?? ""),
          schedule: schedExpr,
          enabled: job.enabled === true,
          message: String(payload.message ?? job.message ?? "").slice(0, 120),
        }
      })
    } catch {
      // malformed JSON — leave empty
    }
  }

  return {
    osCronJobs,
    openclawJobs,
    liveCrontabAvailable,
    openclawJobsAvailable,
  }
}
