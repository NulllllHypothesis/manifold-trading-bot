import "server-only"
import { DATA_PATHS, BOT_ID } from "@/lib/config"
import {
  StrategyWeightsSchema,
  type StrategyWeights,
} from "@/lib/schemas/strategy-weights"
import { readJsonFile } from "./_io"

export async function readStrategyWeights(
  _botId: string = BOT_ID,
): Promise<StrategyWeights | null> {
  return readJsonFile(DATA_PATHS.strategyWeights(), StrategyWeightsSchema)
}
