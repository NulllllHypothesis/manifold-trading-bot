import "server-only"
import { DATA_PATHS, BOT_ID } from "@/lib/config"
import { PendingSwapsFileSchema, type PendingSwap } from "@/lib/schemas/swaps"
import { readJsonFile } from "./_io"

export async function readPendingSwaps(
  _botId: string = BOT_ID,
): Promise<PendingSwap[]> {
  const data = await readJsonFile(DATA_PATHS.pendingSwaps(), PendingSwapsFileSchema)
  return data ?? []
}
