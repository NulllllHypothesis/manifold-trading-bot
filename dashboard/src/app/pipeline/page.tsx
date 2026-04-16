import { PageHeader } from "@/components/page-header"
import { Card, CardContent } from "@/components/ui/card"
import { Mono } from "@/components/format"
import { summarizeRecentCounters } from "@/lib/data"
import { FunnelChart, AiFlowChart, RejectionChart } from "@/components/pipeline-charts"

export const dynamic = "force-dynamic"

export default async function PipelineHealthPage() {
  const summary = await summarizeRecentCounters(undefined, { windowHours: 24 })

  const conversionRate =
    summary.marketsFetched > 0
      ? ((summary.tradesExecuted / summary.marketsFetched) * 100).toFixed(2)
      : "0"

  const aiSuccessRate =
    summary.aiFlow.analyzed > 0
      ? (
          ((summary.aiFlow.agree + summary.aiFlow.disagree) /
            summary.aiFlow.analyzed) *
          100
        ).toFixed(0)
      : "—"

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
          <Mono>{summary.traderRuns}</Mono> trader runs ·{" "}
          Conversion: <Mono>{conversionRate}%</Mono> (markets → trades)
        </CardContent>
      </Card>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <CardContent className="p-4">
            <div className="text-[10px] uppercase tracking-wide text-muted-foreground mb-3">
              Research → Execution Funnel (24h)
            </div>
            <FunnelChart data={summary.funnel} />
          </CardContent>
        </Card>

        <Card>
          <CardContent className="p-4">
            <div className="text-[10px] uppercase tracking-wide text-muted-foreground mb-3">
              AI Analysis Flow (24h)
            </div>
            <div className="mb-2 flex gap-4 text-xs text-muted-foreground">
              <span>
                Success rate: <Mono>{aiSuccessRate}%</Mono>
              </span>
              <span>
                No-result: <Mono>{summary.aiFlow.noResult}</Mono>
              </span>
            </div>
            <AiFlowChart data={summary.aiFlow} />
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardContent className="p-4">
          <div className="text-[10px] uppercase tracking-wide text-muted-foreground mb-3">
            Trader Rejection Reasons (24h)
          </div>
          <RejectionChart data={summary.rejectionBreakdown} />
        </CardContent>
      </Card>

      <div className="grid gap-4 md:grid-cols-5">
        {summary.funnel.map((stage) => (
          <Card key={stage.label}>
            <CardContent className="p-4 text-center">
              <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
                {stage.label}
              </div>
              <div className="mt-1 font-mono text-2xl font-semibold tabular-nums">
                {stage.value}
              </div>
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  )
}
