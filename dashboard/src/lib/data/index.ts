/**
 * Data layer entry — server-only.
 *
 * Every adapter in this directory is responsible for:
 *   1. Reading a known file (or DB) from the bot's workspace.
 *   2. Validating it via Zod.
 *   3. Returning typed data, or `null` if the source doesn't exist.
 *
 * Strict contract:
 *   - All reads, never writes.
 *   - All adapters take an optional `botId` parameter (default = current bot).
 *     Hardcoded today; future multi-tenant work will route by botId.
 *   - All file/DB I/O lives here, never in components or pages.
 *
 * When we move to a hosted API (Phase 2/3), these functions become
 * `await fetch(...)` calls — components and pages don't change.
 */
import "server-only"

export { readPaperState, summarizePortfolio } from "./portfolio"
export { summarizePerformance } from "./performance"
export { readMarketResearch } from "./research"
export {
  readResearchCounters,
  readTraderCounters,
  summarizeRecentCounters,
} from "./counters"
export { readStrategyWeights } from "./strategy-weights"
export { readCalibrationTable } from "./calibration"
export { readPendingSwaps } from "./swaps"
export { readDiagnostics } from "./diagnostics"
export { readAutomationStatus } from "./automation"
export { readAuditEvents } from "./audit"
export { readControlsSnapshot } from "./controls"
export { summarizeOpportunities } from "./opportunities"
export { summarizeNewsImpact } from "./news"
