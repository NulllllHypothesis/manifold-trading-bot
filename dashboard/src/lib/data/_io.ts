/**
 * Internal shared I/O helpers for data adapters.
 * Server-only; never imported from components.
 */
import "server-only"
import fs from "node:fs/promises"
import { createReadStream, existsSync, statSync } from "node:fs"
import { createInterface } from "node:readline"
import type { ZodSchema } from "zod"

/**
 * Read a JSON file from disk, validate against schema, return parsed value or null
 * if the file doesn't exist. Throws on parse/validation failure (loud is correct
 * here — silent fallbacks hide bugs).
 */
export async function readJsonFile<T>(
  filePath: string,
  schema: ZodSchema<T>,
): Promise<T | null> {
  if (!existsSync(filePath)) return null
  const raw = await fs.readFile(filePath, "utf-8")
  const parsed = JSON.parse(raw) as unknown
  return schema.parse(parsed)
}

/**
 * Read a JSONL file. Returns one validated object per non-empty line.
 * Skips lines that fail validation rather than aborting the whole read —
 * counter logs grow long and we don't want one bad line to break a whole page.
 */
export async function readJsonLinesFile<T>(
  filePath: string,
  schema: ZodSchema<T>,
  options: { tailLines?: number } = {},
): Promise<T[]> {
  if (!existsSync(filePath)) return []

  const stream = createReadStream(filePath, { encoding: "utf-8" })
  const rl = createInterface({ input: stream, crlfDelay: Infinity })
  const out: T[] = []
  for await (const line of rl) {
    const trimmed = line.trim()
    if (!trimmed) continue
    try {
      out.push(schema.parse(JSON.parse(trimmed)))
    } catch {
      // skip malformed line
    }
  }
  if (options.tailLines && out.length > options.tailLines) {
    return out.slice(out.length - options.tailLines)
  }
  return out
}

/** Get file mtime + size, or null if missing. Used by diagnostics. */
export function fileStat(filePath: string): {
  exists: boolean
  mtime: Date | null
  sizeBytes: number | null
  ageSeconds: number | null
} {
  if (!existsSync(filePath)) {
    return { exists: false, mtime: null, sizeBytes: null, ageSeconds: null }
  }
  const s = statSync(filePath)
  const mtime = s.mtime
  return {
    exists: true,
    mtime,
    sizeBytes: s.size,
    ageSeconds: Math.floor((Date.now() - mtime.getTime()) / 1000),
  }
}
