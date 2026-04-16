import "server-only"
import { BOT_ID } from "@/lib/config"
import { readPaperState } from "./portfolio"
import { readResearchCounters, readTraderCounters } from "./counters"
import { readPendingSwaps } from "./swaps"

export type AuditEventType =
  | "research_run"
  | "trader_run"
  | "trade_executed"
  | "trade_resolved"
  | "swap_proposed"
  | "swap_executed"
  | "swap_expired"

export type AuditEvent = {
  type: AuditEventType
  timestamp: string
  title: string
  detail?: string
  metadata?: Record<string, unknown>
}

export async function readAuditEvents(
  botId: string = BOT_ID,
  options: { limit?: number } = {},
): Promise<AuditEvent[]> {
  const limit = options.limit ?? 100

  const [state, researchCounters, traderCounters, swaps] = await Promise.all([
    readPaperState(botId),
    readResearchCounters(botId, { tailLines: 50 }),
    readTraderCounters(botId, { tailLines: 50 }),
    readPendingSwaps(botId),
  ])

  const events: AuditEvent[] = []

  for (const r of researchCounters) {
    events.push({
      type: "research_run",
      timestamp: r.timestamp,
      title: "Research scan completed",
      detail: `${r.markets_fetched ?? 0} markets fetched, ${r.final_recommendations ?? 0} recommendations, ${r.ai_analyzed ?? 0} AI analyzed`,
      metadata: {
        marketsFetched: r.markets_fetched,
        finalRecs: r.final_recommendations,
        aiAnalyzed: r.ai_analyzed,
        aiAgree: r.ai_agree,
        aiDisagree: r.ai_disagree,
        aiNoResult: r.ai_no_result,
      },
    })
  }

  for (const t of traderCounters) {
    events.push({
      type: "trader_run",
      timestamp: t.timestamp,
      title: "Trader run completed",
      detail: `${t.recommendations_loaded ?? 0} loaded, ${t.passed_filter ?? 0} passed filter, ${t.trades_executed ?? 0} executed`,
      metadata: {
        loaded: t.recommendations_loaded,
        passed: t.passed_filter,
        executed: t.trades_executed,
        rejected: t.rejected,
      },
    })
  }

  if (state) {
    for (const t of state.trade_history) {
      if (t.status === "OPEN") {
        events.push({
          type: "trade_executed",
          timestamp: t.timestamp,
          title: `Opened ${t.outcome} position`,
          detail: `$${t.amount.toFixed(2)} on "${t.question || t.market_id}" at ${(t.probability * 100).toFixed(0)}%`,
          metadata: {
            tradeId: t.trade_id,
            marketId: t.market_id,
            amount: t.amount,
            outcome: t.outcome,
            strategies: t.strategies,
          },
        })
      } else {
        const profit = t.profit ?? 0
        events.push({
          type: "trade_resolved",
          timestamp: t.resolved_at ?? t.timestamp,
          title: `Position resolved: ${t.status}`,
          detail: `${profit >= 0 ? "+" : ""}$${profit.toFixed(2)} on "${t.question || t.market_id}"`,
          metadata: {
            tradeId: t.trade_id,
            marketId: t.market_id,
            profit,
            status: t.status,
          },
        })
      }
    }

    for (const trades of Object.values(state.positions)) {
      for (const t of trades) {
        if (t.status === "OPEN") {
          events.push({
            type: "trade_executed",
            timestamp: t.timestamp,
            title: `Opened ${t.outcome} position`,
            detail: `$${t.amount.toFixed(2)} on "${t.question || t.market_id}" at ${(t.probability * 100).toFixed(0)}%`,
            metadata: {
              tradeId: t.trade_id,
              marketId: t.market_id,
              amount: t.amount,
              outcome: t.outcome,
              strategies: t.strategies,
            },
          })
        }
      }
    }
  }

  for (const s of swaps) {
    const eventType: AuditEventType =
      s.status === "executed" ? "swap_executed" :
      s.status === "expired" ? "swap_expired" :
      "swap_proposed"
    events.push({
      type: eventType,
      timestamp: s.proposed_at ?? "",
      title: `Swap ${s.status}: close ${s.close_market_id ?? "?"} → open ${s.open_market_id ?? "?"}`,
      detail: undefined,
      metadata: { swapId: s.swap_id, status: s.status },
    })
  }

  events.sort((a, b) => b.timestamp.localeCompare(a.timestamp))
  return events.slice(0, limit)
}
