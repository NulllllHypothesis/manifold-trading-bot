import { z } from "zod"

export const StrategyWeightsSchema = z.object({
  generated_at: z.string().optional(),
  sample_count: z.number().optional(),
  min_samples_threshold: z.number().optional(),
  weights: z.record(z.string(), z.number()),
  per_strategy_samples: z.record(z.string(), z.number()).optional(),
  by_category: z.record(z.string(), z.record(z.string(), z.number())).optional(),
})
export type StrategyWeights = z.infer<typeof StrategyWeightsSchema>
