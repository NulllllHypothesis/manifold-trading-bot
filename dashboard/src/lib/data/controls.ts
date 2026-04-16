import "server-only"
import fs from "node:fs/promises"
import { existsSync } from "node:fs"
import path from "node:path"
import { WORKSPACE_ROOT, DATA_PATHS } from "@/lib/config"
import { readJsonFile } from "./_io"
import { z } from "zod"

export type ConfigEntry = {
  key: string
  value: string
  description: string
  group: "risk" | "trading" | "api" | "features"
}

export type AiCooldownEntry = {
  marketId: string
  fails: number
  lastFailAt: string
}

export type ControlsSnapshot = {
  config: ConfigEntry[]
  aiCooldowns: AiCooldownEntry[]
  configFileAge: number | null
}

const CONFIG_PATTERNS: Array<{
  pattern: RegExp
  key: string
  description: string
  group: ConfigEntry["group"]
}> = [
  { pattern: /^MAX_BET_AMOUNT\s*=\s*(.+)/, key: "MAX_BET_AMOUNT", description: "Maximum bet size per trade", group: "risk" },
  { pattern: /^MIN_BET_AMOUNT\s*=\s*(.+)/, key: "MIN_BET_AMOUNT", description: "Minimum bet size", group: "risk" },
  { pattern: /^MIN_CONFIDENCE\s*=\s*(.+)/, key: "MIN_CONFIDENCE", description: "Minimum confidence threshold to trade", group: "risk" },
  { pattern: /^MAX_POSITIONS\s*=\s*(.+)/, key: "MAX_POSITIONS", description: "Maximum total open positions", group: "risk" },
  { pattern: /^MAX_POSITIONS_PER_CATEGORY\s*=\s*(.+)/, key: "MAX_POSITIONS_PER_CATEGORY", description: "Maximum positions per category", group: "risk" },
  { pattern: /^MIN_LIQUIDITY\s*=\s*(.+)/, key: "MIN_LIQUIDITY", description: "Minimum market liquidity to trade", group: "risk" },
  { pattern: /^INITIAL_BALANCE\s*=\s*(.+)/, key: "INITIAL_BALANCE", description: "Starting paper trading balance", group: "trading" },
  { pattern: /^MANIFOLD_API_BASE\s*=\s*(.+)/, key: "MANIFOLD_API_BASE", description: "Manifold Markets API base URL", group: "api" },
]

const AiCooldownSchema = z.record(
  z.string(),
  z.object({ fails: z.number(), last_fail_at: z.string() }),
)

export async function readControlsSnapshot(): Promise<ControlsSnapshot> {
  const configPath = path.join(WORKSPACE_ROOT, "manifold_bot/config.py")
  const config: ConfigEntry[] = []
  let configFileAge: number | null = null

  if (existsSync(configPath)) {
    const stat = await fs.stat(configPath)
    configFileAge = Math.floor((Date.now() - stat.mtime.getTime()) / 1000)

    const content = await fs.readFile(configPath, "utf-8")
    const lines = content.split("\n")

    for (const line of lines) {
      const trimmed = line.trim()
      for (const { pattern, key, description, group } of CONFIG_PATTERNS) {
        const match = trimmed.match(pattern)
        if (match) {
          let value = match[1]!.trim()
          if (value.includes("#")) value = value.split("#")[0]!.trim()
          value = value.replace(/^["']|["']$/g, "")
          config.push({ key, value, description, group })
        }
      }
    }

    const hasNewsKey = content.includes("NEWS_API_KEY")
    config.push({
      key: "NEWS_FETCHER",
      value: hasNewsKey ? "enabled (if key set)" : "not configured",
      description: "P3 news strategy integration",
      group: "features",
    })
  }

  let aiCooldowns: AiCooldownEntry[] = []
  const cooldownPath = DATA_PATHS.aiCooldown()
  if (existsSync(cooldownPath)) {
    const raw = await readJsonFile(cooldownPath, AiCooldownSchema)
    if (raw) {
      aiCooldowns = Object.entries(raw).map(([marketId, v]) => ({
        marketId,
        fails: v.fails,
        lastFailAt: v.last_fail_at,
      }))
    }
  }

  return { config, aiCooldowns, configFileAge }
}
