import "server-only"
import { DATA_PATHS, BOT_ID } from "@/lib/config"
import {
  CalibrationTableSchema,
  type CalibrationTable,
} from "@/lib/schemas/calibration"
import { readJsonFile } from "./_io"

export async function readCalibrationTable(
  _botId: string = BOT_ID,
): Promise<CalibrationTable | null> {
  return readJsonFile(DATA_PATHS.calibrationTable(), CalibrationTableSchema)
}
