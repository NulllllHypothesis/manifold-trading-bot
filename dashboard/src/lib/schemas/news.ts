import { z } from "zod"

export const CachedArticleSchema = z.object({
  title: z.string(),
  source: z.string(),
  published_at: z.string(),
  age_hours: z.number().optional(),
})
export type CachedArticle = z.infer<typeof CachedArticleSchema>

export const NewsCacheSchema = z.record(
  z.string(),
  z.array(CachedArticleSchema),
)
export type NewsCache = z.infer<typeof NewsCacheSchema>
