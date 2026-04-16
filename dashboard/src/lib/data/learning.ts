import "server-only"
import { DATA_PATHS } from "@/lib/config"
import { readJsonFile, fileStat } from "./_io"
import { StrategyWeightsSchema } from "@/lib/schemas/strategy-weights"
import { CalibrationTableSchema } from "@/lib/schemas/calibration"
import { z } from "zod"

const CategoryAccuracySchema = z.object({
  generated_at: z.string().optional(),
  sample_count: z.number().optional(),
  min_samples_per_category: z.number().optional(),
  categories: z.record(
    z.string(),
    z.object({ accuracy: z.number(), sample_count: z.number() }),
  ).optional(),
})

const EvalReportSchema = z.object({
  evaluated_at: z.string().optional(),
  n_examples: z.number().optional(),
  results: z
    .record(z.string(), z.unknown())
    .optional(),
  promotion: z
    .object({
      eligible: z.boolean().optional(),
      reason: z.string().optional(),
    })
    .optional(),
}).passthrough()

const BacktestReportSchema = z.object({
  generated_at: z.string().optional(),
  snapshots_used: z.number().optional(),
  dry_run: z.boolean().optional(),
}).passthrough()

const M2AuditSchema = z.object({
  generated_at: z.string().optional(),
  total_markets: z.number().optional(),
  usable_for_m3: z.number().optional(),
}).passthrough()

export type LoopStage = {
  label: string
  value: number | string
  detail: string
  healthy: boolean
}

export type WeightEntry = {
  strategy: string
  weight: number
  samples: number
  diverged: boolean
  gateThreshold: number
}

export type LearningAlert = {
  level: "error" | "warn" | "info"
  title: string
  detail: string
}

export type LearningLoopSummary = {
  loopFunnel: LoopStage[]
  weights: WeightEntry[]
  alerts: LearningAlert[]
  categoryAccuracy: Array<{ category: string; accuracy: number; samples: number; gateMet: boolean }>
  timestamps: {
    lastHarvest: string | null
    lastCalibration: string | null
    lastWeightUpdate: string | null
    lastBacktest: string | null
    lastEval: string | null
    lastM2Audit: string | null
  }
  evalReport: {
    nExamples: number | null
    brierScore: number | null
    directionalAccuracy: number | null
    promotionEligible: boolean
    promotionReason: string | null
  } | null
  backtestReport: {
    snapshotsUsed: number | null
    dryRun: boolean
  } | null
}

export async function summarizeLearningLoop(): Promise<LearningLoopSummary> {
  const [sw, cal, catAcc, evalRaw, btRaw, m2Raw] = await Promise.all([
    readJsonFile(DATA_PATHS.strategyWeights(), StrategyWeightsSchema),
    readJsonFile(DATA_PATHS.calibrationTable(), CalibrationTableSchema),
    readJsonFile(DATA_PATHS.categoryAccuracy(), CategoryAccuracySchema),
    readJsonFile(DATA_PATHS.evalReport(), EvalReportSchema),
    readJsonFile(DATA_PATHS.backtestReport(), BacktestReportSchema),
    readJsonFile(DATA_PATHS.m2Audit(), M2AuditSchema),
  ])

  const calDbStat = fileStat(DATA_PATHS.calibrationDb())

  const resolvedMarkets = m2Raw?.total_markets ?? (calDbStat.exists ? "present" : 0)
  const usableForM3 = m2Raw?.usable_for_m3 ?? 0
  const snapshotsUsed = btRaw?.snapshots_used ?? 0
  const calCategories = cal?.by_category.length ?? 0
  const calBuckets = cal?.buckets?.length ?? 0
  const weightSamples = sw?.sample_count ?? 0
  const catAccSamples = catAcc?.sample_count ?? 0

  const weightsActive = sw
    ? Object.values(sw.weights).some((w) => Math.abs(w - 1.0) > 0.001)
    : false

  const loopFunnel: LoopStage[] = [
    {
      label: "Resolved markets",
      value: resolvedMarkets,
      detail: "Markets in calibration.db:resolved_markets (harvested from API)",
      healthy: typeof resolvedMarkets === "number" ? resolvedMarkets > 100 : calDbStat.exists,
    },
    {
      label: "Usable for M3",
      value: usableForM3,
      detail: "Markets passing M2 audit quality gate",
      healthy: usableForM3 > 50,
    },
    {
      label: "Backtest snapshots",
      value: snapshotsUsed,
      detail: "Snapshots used for strategy backtesting",
      healthy: snapshotsUsed > 0,
    },
    {
      label: "Calibration categories",
      value: calCategories,
      detail: "Categories with crowd-bias calibration data",
      healthy: calCategories >= 5,
    },
    {
      label: "Calibration buckets",
      value: calBuckets,
      detail: "Probability buckets with bias data (10% intervals)",
      healthy: calBuckets >= 8,
    },
    {
      label: "Bet outcomes",
      value: catAccSamples,
      detail: "Resolved trades in calibration.db:bet_outcomes",
      healthy: catAccSamples >= 10,
    },
    {
      label: "Weight samples",
      value: weightSamples,
      detail: "Samples feeding into strategy weight computation",
      healthy: weightSamples >= 10,
    },
    {
      label: "Weights diverged",
      value: weightsActive ? "YES" : "NO",
      detail: weightsActive
        ? "At least one strategy weight has moved from 1.0"
        : "All weights still at 1.0 default",
      healthy: weightsActive,
    },
  ]

  const weights: WeightEntry[] = sw
    ? Object.entries(sw.weights).map(([strategy, weight]) => ({
        strategy,
        weight,
        samples: sw.per_strategy_samples?.[strategy] ?? 0,
        diverged: Math.abs(weight - 1.0) > 0.001,
        gateThreshold: sw.min_samples_threshold ?? 10,
      }))
    : []

  const categoryAccuracy = catAcc?.categories
    ? Object.entries(catAcc.categories).map(([category, info]) => ({
        category,
        accuracy: info.accuracy,
        samples: info.sample_count,
        gateMet: info.sample_count >= (catAcc.min_samples_per_category ?? 8),
      }))
    : []

  const alerts: LearningAlert[] = []

  if (!weightsActive) {
    alerts.push({
      level: "warn",
      title: "Strategy weights still at defaults",
      detail: `All weights are 1.0. Need ≥${sw?.min_samples_threshold ?? 10} samples per strategy for adaptation. Currently ${weightSamples} total.`,
    })
  }

  if (catAccSamples < 10) {
    alerts.push({
      level: "warn",
      title: `Only ${catAccSamples} bet outcomes recorded`,
      detail: "Need more resolved trades with strategy metadata for meaningful weight computation.",
    })
  }

  const catAccGateThreshold = catAcc?.min_samples_per_category ?? 8
  const catsBelowGate = categoryAccuracy.filter((c) => !c.gateMet)
  if (catsBelowGate.length > 0) {
    alerts.push({
      level: "info",
      title: `${catsBelowGate.length} categories below ${catAccGateThreshold}-sample gate`,
      detail: `Categories ${catsBelowGate.map((c) => c.category).join(", ")} do not yet have enough samples for adaptive caps.`,
    })
  }

  const calAge = fileStat(DATA_PATHS.calibrationTable())
  if (calAge.ageSeconds !== null && calAge.ageSeconds > 8 * 86400) {
    alerts.push({
      level: "warn",
      title: "Calibration table is stale",
      detail: `Last generated ${Math.floor(calAge.ageSeconds / 86400)} days ago. Sunday harvest job may not be running.`,
    })
  }

  const swAge = fileStat(DATA_PATHS.strategyWeights())
  if (swAge.ageSeconds !== null && swAge.ageSeconds > 8 * 86400) {
    alerts.push({
      level: "warn",
      title: "Strategy weights file is stale",
      detail: `Last updated ${Math.floor(swAge.ageSeconds / 86400)} days ago. Monday weight computation may not be running.`,
    })
  }

  if (btRaw?.dry_run === true) {
    alerts.push({
      level: "info",
      title: "Last backtest was a dry run",
      detail: "Backtest produced candidate weights but did not merge them into live weights.",
    })
  }

  let evalReport: LearningLoopSummary["evalReport"] = null
  if (evalRaw) {
    const crowd = (evalRaw.results as Record<string, Record<string, unknown>> | undefined)?.["crowd"]
    evalReport = {
      nExamples: evalRaw.n_examples ?? null,
      brierScore: typeof crowd?.brier_score === "number" ? crowd.brier_score : null,
      directionalAccuracy: typeof crowd?.directional_accuracy === "number" ? crowd.directional_accuracy : null,
      promotionEligible: evalRaw.promotion?.eligible ?? false,
      promotionReason: evalRaw.promotion?.reason ?? null,
    }
  }

  return {
    loopFunnel,
    weights,
    alerts,
    categoryAccuracy,
    timestamps: {
      lastHarvest: calDbStat.mtime?.toISOString() ?? null,
      lastCalibration: cal?.generated_at ?? null,
      lastWeightUpdate: sw?.generated_at ?? null,
      lastBacktest: btRaw?.generated_at ?? null,
      lastEval: evalRaw?.evaluated_at ?? null,
      lastM2Audit: m2Raw?.generated_at ?? null,
    },
    evalReport,
    backtestReport: btRaw
      ? { snapshotsUsed: btRaw.snapshots_used ?? null, dryRun: btRaw.dry_run ?? false }
      : null,
  }
}
