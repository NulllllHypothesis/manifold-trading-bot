import { PageHeader } from "@/components/page-header"
import { ComingSoon } from "@/components/coming-soon"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { ScrollTextIcon } from "lucide-react"

export default function AuditPage() {
  return (
    <div className="space-y-6">
      <PageHeader
        title="Audit"
        description="Chronological feed of research saves, trades, resolutions, swap proposals, swap approvals, config changes, Telegram actions."
      />
      <Alert>
        <ScrollTextIcon className="h-4 w-4" />
        <AlertTitle>v1: derived from existing JSONL counters</AlertTitle>
        <AlertDescription>
          {`A true event-sourced audit log requires the bot to start emitting structured events (research_saved, trade_executed, swap_proposed, etc.) to an append-only log. That's a backend project for v2. v1 will derive a "Recent Activity" feed by stitching together existing counter files, trade history, and pending swaps — useful, but lossy.`}
        </AlertDescription>
      </Alert>
      <ComingSoon
        feature={'"Recent Activity" feed (derived)'}
        description="Time-ordered feed merging research-counter timestamps, trade-history entries, swap-proposal timestamps, and resolution events. Filterable by event type."
        dataSources={[
          "data/research_counters.jsonl",
          "data/trader_counters.jsonl",
          "manifold_bot/paper_trading_state.json (trade_history)",
          "pending_swaps.json",
        ]}
        buildOrder={9}
      />
    </div>
  )
}
