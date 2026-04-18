import { z } from "zod"

export const CategoryCalibrationSchema = z.object({
  category: z.string(),
  sample_size: z.number(),
  avg_crowd_prob: z.number().optional(),
  actual_yes_rate: z.number().optional(),
  bias: z.number().optional(),
  reliable: z.boolean().optional(),
})
export type CategoryCalibration = z.infer<typeof CategoryCalibrationSchema>

export const BucketCalibrationSchema = z.object({
  category: z.string(),
  bucket: z.union([z.string(), z.number()]).optional(),
  bucket_low: z.number().optional(),
  bucket_high: z.number().optional(),
  sample_size: z.number(),
  avg_crowd_prob: z.number().optional(),
  actual_yes_rate: z.number().optional(),
  bias: z.number().optional(),
  reliable: z.boolean().optional(),
})
export type BucketCalibration = z.infer<typeof BucketCalibrationSchema>

export const CalibrationTableSchema = z.object({
  generated_at: z.string().optional(),
  generated_at_unix: z.number().optional(),
  source_db: z.string().optional(),
  min_cell_samples: z.number().optional(),
  description: z.string().optional(),
  buckets: z.array(z.object({
    bucket_low: z.number(),
    bucket_high: z.number(),
    crowd_midpoint: z.number().optional(),
    actual_yes_rate: z.number(),
    sample_size: z.number(),
    bias: z.number().optional(),
    reliable: z.boolean().optional(),
  })).optional(),
  by_category: z.array(CategoryCalibrationSchema),
  by_category_bucket: z.array(BucketCalibrationSchema).optional(),
})
export type CalibrationTable = z.infer<typeof CalibrationTableSchema>
