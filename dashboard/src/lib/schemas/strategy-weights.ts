import { z } from "zod"

const CategoryStrategyEntry = z.union([
  z.number(),
  z.object({
    weight: z.number(),
    accuracy: z.number().optional(),
    n: z.number().optional(),
  }),
])

export const StrategyWeightsSchema = z.object({
  generated_at: z.string().optional(),
  sample_count: z.number().optional(),
  min_samples_threshold: z.number().optional(),
  weights: z.record(z.string(), z.number()),
  per_strategy_samples: z.record(z.string(), z.number()).optional(),
  by_category: z.record(z.string(), z.record(z.string(), CategoryStrategyEntry)).optional(),
})
export type StrategyWeights = z.infer<typeof StrategyWeightsSchema>

export type CategoryStrategyValue = z.infer<typeof CategoryStrategyEntry>

export function getCategoryWeight(val: CategoryStrategyValue): number {
  if (typeof val === "number") return val
  return val.weight
}

export function getCategoryAccuracy(val: CategoryStrategyValue): number | null {
  if (typeof val === "number") return null
  return val.accuracy ?? null
}

export function getCategorySamples(val: CategoryStrategyValue): number | null {
  if (typeof val === "number") return null
  return val.n ?? null
}
