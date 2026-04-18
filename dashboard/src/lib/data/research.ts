import "server-only"
import { DATA_PATHS, BOT_ID } from "@/lib/config"
import { MarketResearchSchema, type MarketResearch } from "@/lib/schemas/research"
import { readJsonFile } from "./_io"

export async function readMarketResearch(
  _botId: string = BOT_ID,
): Promise<MarketResearch | null> {
  return readJsonFile(DATA_PATHS.marketResearch(), MarketResearchSchema)
}
