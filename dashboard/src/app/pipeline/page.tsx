import { PageHeader } from "@/components/page-header"
import { ComingSoon } from "@/components/coming-soon"
import { Card, CardContent } from "@/components/ui/card"
import { Mono } from "@/components/format"
import { summarizeRecentCounters } from "@/lib/data"
import { cn } from "@/lib/utils"

export const dynamic = "force-dynamic"

export default async function PipelineHealthPage() {
  const summary = await summarizeRecentCounters(undefined, { windowHours: 24 })

  return (
    <div className="space-y-6">
      <PageHeader
        title="Pipeline Health"
        description="The research-to-execution funnel. Where decisions get filtered out matters more than how many made it through."
      />

      <Card>
        <CardContent className="p-4 text-xs text-muted-foreground">
          Window: last <Mono>{summary.windowHours}h</Mono> ·{" "}
          <Mono>{summary.researchRuns}</Mono> research runs ·{" "}
          <Mono>{summary.traderRuns}</Mono> trader runs
        </CardContent>
      </Card>

      {/* Funnel preview as a stacked list — full chart later */}
      <Card>
        <CardContent className="p-4">
          <div className="text-[10px] uppercase tracking-wide text-muted-foreground mb-3">
            Funnel (24h totals — preview, full chart coming)
          </div>
          <div className="space-y-2">
            {summary.funnel.map((stage) => {
              const max = Math.max(...summary.funnel.map((s) => s.value), 1)
              const pct = (stage.value / max) * 100
              return (
                <div key={stage.label} className="flex items-center gap-3">
                  <div className="w-44 shrink-0 text-sm">{stage.label}</div>
                  <div className="relative h-6 flex-1 overflow-hidden rounded bg-muted/40">
                    <div
                      className={cn(
                        "h-full rounded transition-all",
                        "bg-primary/30",
                      )}
                      style={{ width: `${pct}%` }}
                    />
                    <div className="absolute inset-0 flex items-center justify-end pr-2">
                      <Mono className="text-xs">{stage.value}</Mono>
                    </div>
                  </div>
                </div>
              )
            })}
          </div>
        </CardContent>
      </Card>

      {summary.rejectionBreakdown.length > 0 ? (
        <Card>
          <CardContent className="p-4">
            <div className="text-[10px] uppercase tracking-wide text-muted-foreground mb-3">
              Trader rejection reasons (24h)
            </div>
            <div className="space-y-1">
              {summary.rejectionBreakdown.map((r) => (
                <div
                  key={r.reason}
                  className="flex items-center justify-between text-sm"
                >
                  <span className="font-medium">{r.reason}</span>
                  <Mono className="text-muted-foreground">{r.count}</Mono>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      ) : null}

      <ComingSoon
        feature="Full pipeline visualization"
        description="Sankey-style funnel chart, trend lines per stage over hours/days, drill-into-cause for each rejection reason, AI flow funnel (eligible → cooled → analyzed → agree/disagree/skip/no-result)."
        dataSources={[
          "data/research_counters.jsonl",
          "data/trader_counters.jsonl",
        ]}
        buildOrder={2}
      />
    </div>
  )
}
