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
  group: "risk" | "trading" | "research" | "ai" | "swap" | "api" | "features"
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
  autotraderDisabled: boolean
}

const CONFIG_PY_PATTERNS: Array<{
  pattern: RegExp
  key: string
  description: string
  group: ConfigEntry["group"]
}> = [
  { pattern: /^MAX_BET_AMOUNT\s*=\s*(.+)/, key: "MAX_BET_AMOUNT", description: "Maximum bet size per trade", group: "risk" },
  { pattern: /^MIN_BET_AMOUNT\s*=\s*(.+)/, key: "MIN_BET_AMOUNT", description: "Minimum bet size", group: "risk" },
  { pattern: /^MIN_CONFIDENCE\s*=\s*(.+)/, key: "MIN_CONFIDENCE", description: "Minimum confidence threshold to trade", group: "risk" },
  { pattern: /^MAX_POSITIONS\s*=\s*(.+)/, key: "MAX_POSITIONS", description: "Maximum total open positions", group: "risk" },
  { pattern: /^MAX_POSITIONS_PER_CATEGORY\s*=\s*(.+)/, key: "MAX_POSITIONS_PER_CATEGORY", description: "Per-category concentration limit", group: "risk" },
  { pattern: /^MIN_LIQUIDITY\s*=\s*(.+)/, key: "MIN_LIQUIDITY", description: "Minimum market liquidity to trade", group: "risk" },
  { pattern: /^INITIAL_BALANCE\s*=\s*(.+)/, key: "INITIAL_BALANCE", description: "Starting paper trading balance", group: "trading" },
  { pattern: /^MANIFOLD_API_BASE\s*=\s*(.+)/, key: "MANIFOLD_API_BASE", description: "Manifold Markets API base URL", group: "api" },
]

type ScriptKnob = {
  file: string
  pattern: RegExp
  key: string
  description: string
  group: ConfigEntry["group"]
}

const SCRIPT_KNOBS: ScriptKnob[] = [
  { file: "automation/auto_research.py", pattern: /_AI_TIMEOUT_COOLDOWN_HOURS\s*=\s*(\d+)/, key: "AI_COOLDOWN_HOURS", description: "How long a market stays on AI cooldown after failures", group: "ai" },
  { file: "automation/auto_research.py", pattern: /_AI_TIMEOUT_MAX_FAILS\s*=\s*(\d+)/, key: "AI_COOLDOWN_MAX_FAILS", description: "AI failures before market is excluded", group: "ai" },
  { file: "automation/auto_research.py", pattern: /_WEAK_STAT_CAP\s*=\s*([0-9.]+)/, key: "WEAK_STAT_CAP", description: "Confidence cap for low-signal markets (MIN_CONFIDENCE - 0.01)", group: "research" },
  { file: "automation/auto_research.py", pattern: /_STAT_BOOST_FLOOR\s*=\s*([0-9.]+)/, key: "STAT_BOOST_FLOOR", description: "Markets below this floor are capped at WEAK_STAT_CAP", group: "research" },
  { file: "automation/auto_research.py", pattern: /_AI_BOOST_MARGIN\s*=\s*([0-9.]+)/, key: "AI_BOOST_MARGIN", description: "Max AI confidence boost over stat signal (+0.10)", group: "ai" },
  { file: "automation/auto_research.py", pattern: /_NEWS_CANDIDATES\s*=\s*(\d+)/, key: "NEWS_CANDIDATES", description: "Top N recommendations enriched with news headlines", group: "research" },
  { file: "automation/auto_research.py", pattern: /VOLUME_SPIKE_MULTIPLIER\s*=\s*([0-9.]+)/, key: "VOLUME_SPIKE_MULTIPLIER", description: "24h volume must exceed this × 7d avg for priority boost", group: "research" },
  { file: "automation/auto_trader.py", pattern: /max_trades_per_cycle\s*=\s*(\d+)/, key: "MAX_TRADES_PER_CYCLE", description: "Maximum new trades per hourly run", group: "trading" },
  { file: "automation/auto_trader.py", pattern: /self\.max_position_size\s*=\s*([0-9.]+)/, key: "MAX_POSITION_SIZE_FRACTION", description: "Fraction of balance per trade (fallback sizing)", group: "trading" },
  { file: "manifold_bot/ai_analyzer.py", pattern: /FIRST_TOKEN_TIMEOUT\s*=\s*(\d+)/, key: "AI_FIRST_TOKEN_TIMEOUT", description: "Ollama first-token timeout (cold model load)", group: "ai" },
  { file: "manifold_bot/ai_analyzer.py", pattern: /PER_CHUNK_TIMEOUT\s*=\s*(\d+)/, key: "AI_PER_CHUNK_TIMEOUT", description: "Ollama per-chunk timeout during generation", group: "ai" },
  { file: "manifold_bot/ai_analyzer.py", pattern: /OLLAMA_MODEL\s*=\s*"([^"]+)"/, key: "OLLAMA_MODEL", description: "Local AI model for market analysis", group: "ai" },
  { file: "manifold_bot/ai_analyzer.py", pattern: /DEEPSEEK_MODEL\s*=\s*"([^"]+)"/, key: "DEEPSEEK_MODEL", description: "Fallback API model when Ollama fails", group: "ai" },
  { file: "scripts/position_swap_checker.py", pattern: /SWAP_EXPIRY_HOURS\s*=\s*(\d+)/, key: "SWAP_EXPIRY_HOURS", description: "Hours before pending swap proposals expire", group: "swap" },
  { file: "scripts/position_swap_checker.py", pattern: /SWAP_MIN_MARGIN\s*=\s*([0-9.]+)/, key: "SWAP_MIN_MARGIN", description: "Minimum edge (new EV > |loss| + margin) to propose swap", group: "swap" },
  { file: "automation/auto_trader.py", pattern: /_MIN_SAMPLES_PER_CATEGORY\s*=\s*(\d+)/, key: "MIN_SAMPLES_PER_CATEGORY", description: "Samples needed before adaptive category caps activate", group: "risk" },
  { file: "automation/auto_trader.py", pattern: /_MIN_SAMPLES_PER_STRATEGY\s*=\s*(\d+)/, key: "MIN_SAMPLES_PER_STRATEGY", description: "Samples needed before strategy weight adaptation", group: "risk" },
  { file: "manifold_bot/strategies.py", pattern: /_MIN_BUCKET_SAMPLES\s*=\s*(\d+)/, key: "MIN_BUCKET_SAMPLES", description: "Minimum samples per calibration bucket for bias strategy", group: "research" },
  { file: "manifold_bot/strategies.py", pattern: /MIN_BIAS_TO_TRADE\s*=\s*([0-9.]+)/, key: "MIN_BIAS_TO_TRADE", description: "Minimum crowd bias magnitude for probability_bias strategy", group: "research" },
]

const AiCooldownSchema = z.record(
  z.string(),
  z.object({ fails: z.number(), last_fail_at: z.string() }),
)

function extractValue(content: string, pattern: RegExp): string | null {
  const match = content.match(pattern)
  if (!match) return null
  let value = match[1]!.trim()
  if (value.includes("#")) value = value.split("#")[0]!.trim()
  return value.replace(/^["']|["']$/g, "")
}

export async function readControlsSnapshot(): Promise<ControlsSnapshot> {
  const configPath = path.join(WORKSPACE_ROOT, "manifold_bot/config.py")
  const config: ConfigEntry[] = []
  let configFileAge: number | null = null

  if (existsSync(configPath)) {
    const stat = await fs.stat(configPath)
    configFileAge = Math.floor((Date.now() - stat.mtime.getTime()) / 1000)

    const content = await fs.readFile(configPath, "utf-8")
    for (const { pattern, key, description, group } of CONFIG_PY_PATTERNS) {
      const value = extractValue(content, pattern)
      if (value) config.push({ key, value, description, group })
    }

    const hasNewsKey = content.includes("NEWS_API_KEY")
    config.push({
      key: "NEWS_FETCHER",
      value: hasNewsKey ? "enabled (if key set)" : "not configured",
      description: "P3 news strategy integration",
      group: "features",
    })
  }

  for (const knob of SCRIPT_KNOBS) {
    const filePath = path.join(WORKSPACE_ROOT, knob.file)
    if (!existsSync(filePath)) continue
    try {
      const content = await fs.readFile(filePath, "utf-8")
      const value = extractValue(content, knob.pattern)
      if (value) {
        config.push({
          key: knob.key,
          value,
          description: knob.description,
          group: knob.group,
        })
      }
    } catch {
      // skip unreadable files
    }
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

  const flagPath = path.join(WORKSPACE_ROOT, "autotrader_disabled.flag")
  const autotraderDisabled = existsSync(flagPath)

  return { config, aiCooldowns, configFileAge, autotraderDisabled }
}
