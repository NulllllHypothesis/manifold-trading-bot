import { PageHeader } from "@/components/page-header"
import { Card, CardContent } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Mono } from "@/components/format"
import { summarizeRecentCounters, readMarketResearch } from "@/lib/data"
import { FunnelChart, AiFlowChart, RejectionChart } from "@/components/pipeline-charts"
import { cn } from "@/lib/utils"
import { ShieldAlertIcon, AlertTriangleIcon } from "lucide-react"

export const dynamic = "force-dynamic"

export default async function PipelineHealthPage() {
  const [summary, research] = await Promise.all([
    summarizeRecentCounters(undefined, { windowHours: 24 }),
    readMarketResearch(),
  ])

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

      {summary.aiFlow.noResult > 0 &&
      summary.aiFlow.analyzed > 0 &&
      summary.aiFlow.noResult / summary.aiFlow.analyzed > 0.3 ? (
        <Alert variant="destructive">
          <ShieldAlertIcon className="h-4 w-4" />
          <AlertTitle>
            AI no_result is acting as a trade veto ({summary.aiFlow.noResult}/
            {summary.aiFlow.analyzed} = {Math.round(
              (summary.aiFlow.noResult / summary.aiFlow.analyzed) * 100,
            )}
            %)
          </AlertTitle>
          <AlertDescription>
            <Mono>auto_research.py:839</Mono> writes{" "}
            <Mono>ai_returned_skip: true</Mono> for AI timeouts/failures, not
            just real SKIPs. <Mono>auto_trader.py:231</Mono> then treats any{" "}
            <Mono>ai_returned_skip</Mono> as an <Mono>ai_veto</Mono>. Result:
            AI failures suppress trades, wipe <Mono>estimated_ev_exec</Mono>,
            and starve the calibration loop. Fix: add an explicit{" "}
            <Mono>ai_status</Mono> field (not_run | no_result | skip | agree |
            disagree) and only veto on real <Mono>skip</Mono>.
          </AlertDescription>
        </Alert>
      ) : null}

      {(() => {
        const sorted = summary.rejectionBreakdown
        const top = sorted[0]
        const maxPos = sorted.find((r) => r.reason === "max_positions")
        if (maxPos && top && top.reason === "max_positions") {
          const totalRejections = sorted.reduce((s, r) => s + r.count, 0)
          const pct = totalRejections > 0 ? Math.round((maxPos.count / totalRejections) * 100) : 0
          return (
            <Alert>
              <AlertTriangleIcon className="h-4 w-4" />
              <AlertTitle>
                max_positions is the top rejection reason ({maxPos.count} hits, {pct}% of all rejections)
              </AlertTitle>
              <AlertDescription>
                The book is saturated — most recommendations pass filters but cannot
                execute because all slots are full. Prioritize turnover (swap/close
                stale positions) rather than raising max_positions.
              </AlertDescription>
            </Alert>
          )
        }
        return null
      })()}

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
            <div className="mb-2 flex flex-wrap gap-4 text-xs text-muted-foreground">
              <span>
                Success rate: <Mono>{aiSuccessRate}%</Mono>
              </span>
              <span>
                No-result: <Mono>{summary.aiFlow.noResult}</Mono>
              </span>
              <span className={cn(summary.aiFlow.cooled > 0 && "text-warn")}>
                Cooled down: <Mono>{summary.aiFlow.cooled}</Mono>
                {summary.aiFlow.eligible > 0 ? (
                  <span> ({Math.round((summary.aiFlow.cooled / summary.aiFlow.eligible) * 100)}% of eligible)</span>
                ) : null}
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

      <div className="grid gap-6 lg:grid-cols-2">
        {/* Pre-filter breakdown */}
        <Card>
          <CardContent className="p-4">
            <div className="text-[10px] uppercase tracking-wide text-muted-foreground mb-3">
              Market pre-filter (24h — why markets get dropped before strategies)
            </div>
            <div className="space-y-1.5">
              {[
                { label: "Skipped: already resolved", value: summary.preFilter.skippedResolved },
                { label: "Skipped: low liquidity (<$200)", value: summary.preFilter.skippedLowLiquidity },
                { label: "Skipped: stale (no bets >72h)", value: summary.preFilter.skippedStale },
                { label: "Skipped: noise market", value: summary.preFilter.skippedNoise },
              ].map((item) => (
                <div key={item.label} className="flex items-center justify-between text-sm">
                  <span>{item.label}</span>
                  <Mono className="text-muted-foreground">{item.value}</Mono>
                </div>
              ))}
              <div className="border-t border-border/50 pt-1.5 flex items-center justify-between text-sm font-medium">
                <span>Survived to strategy evaluation</span>
                <Mono>
                  {summary.marketsFetched -
                    summary.preFilter.skippedResolved -
                    summary.preFilter.skippedLowLiquidity -
                    summary.preFilter.skippedStale -
                    summary.preFilter.skippedNoise}
                </Mono>
              </div>
            </div>
          </CardContent>
        </Card>

        {/* Strategy interaction stats */}
        <Card>
          <CardContent className="p-4">
            <div className="text-[10px] uppercase tracking-wide text-muted-foreground mb-3">
              Strategy interaction (24h)
            </div>
            <div className="space-y-1.5">
              <div className="flex items-center justify-between text-sm">
                <span>Thin-market confirmations</span>
                <Mono className="text-muted-foreground">{summary.thinMarketConfirmations}</Mono>
              </div>
              <div className="flex items-center justify-between text-sm">
                <span>Tied votes dropped</span>
                <Mono className="text-muted-foreground">{summary.tiedVotesDropped}</Mono>
              </div>
              <div className="flex items-center justify-between text-sm">
                <span>Passed trader filters</span>
                <Mono className="text-muted-foreground">{summary.passedFilter}</Mono>
              </div>
            </div>
            {summary.topEvAtExec.length > 0 ? (
              <>
                <div className="text-[10px] uppercase tracking-wide text-muted-foreground mt-4 mb-2">
                  Top EV at last execution
                </div>
                <div className="space-y-1">
                  {summary.topEvAtExec.slice(0, 5).map(([mid, ev]) => (
                    <div key={mid} className="flex items-center justify-between text-xs">
                      <Mono className="text-muted-foreground truncate max-w-48">{mid}</Mono>
                      <Mono className={cn(ev > 0 ? "text-gain" : "text-loss")}>
                        {ev > 0 ? "+" : ""}{ev.toFixed(3)}
                      </Mono>
                    </div>
                  ))}
                </div>
              </>
            ) : null}
          </CardContent>
        </Card>
      </div>

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

      <div className="grid gap-6 lg:grid-cols-3">
        {/* Family dedup distribution */}
        <Card>
          <CardContent className="p-4">
            <div className="text-[10px] uppercase tracking-wide text-muted-foreground mb-3">
              Family dedup winners (24h)
            </div>
            {Object.keys(summary.dedupWinners).length > 0 ? (
              <div className="space-y-2">
                {Object.entries(summary.dedupWinners)
                  .sort(([, a], [, b]) => b - a)
                  .map(([family, count]) => {
                    const total = Object.values(summary.dedupWinners).reduce((s, n) => s + n, 0)
                    const pct = total > 0 ? (count / total) * 100 : 0
                    return (
                      <div key={family} className="flex items-center gap-3">
                        <div className="w-24 shrink-0 text-sm font-medium">{family}</div>
                        <div className="relative h-5 flex-1 overflow-hidden rounded bg-muted/40">
                          <div
                            className="h-full rounded bg-primary/40"
                            style={{ width: `${pct}%` }}
                          />
                        </div>
                        <Mono className="w-16 shrink-0 text-right text-xs">
                          {count} ({pct.toFixed(0)}%)
                        </Mono>
                      </div>
                    )
                  })}
              </div>
            ) : (
              <div className="text-sm text-muted-foreground">No dedup data.</div>
            )}
          </CardContent>
        </Card>

        {/* Final recs by category */}
        <Card>
          <CardContent className="p-4">
            <div className="text-[10px] uppercase tracking-wide text-muted-foreground mb-3">
              Final recommendations by category (24h)
            </div>
            {Object.keys(summary.finalByCategory).length > 0 ? (
              <div className="space-y-2">
                {Object.entries(summary.finalByCategory)
                  .sort(([, a], [, b]) => b - a)
                  .map(([cat, count]) => {
                    const total = Object.values(summary.finalByCategory).reduce((s, n) => s + n, 0)
                    const pct = total > 0 ? (count / total) * 100 : 0
                    return (
                      <div key={cat} className="flex items-center gap-3">
                        <div className="w-24 shrink-0 text-sm font-medium">{cat}</div>
                        <div className="relative h-5 flex-1 overflow-hidden rounded bg-muted/40">
                          <div
                            className="h-full rounded bg-primary/40"
                            style={{ width: `${pct}%` }}
                          />
                        </div>
                        <Mono className="w-16 shrink-0 text-right text-xs">
                          {count}
                        </Mono>
                      </div>
                    )
                  })}
              </div>
            ) : (
              <div className="text-sm text-muted-foreground">No category data.</div>
            )}
          </CardContent>
        </Card>

        {/* Confidence distribution */}
        <Card>
          <CardContent className="p-4">
            <div className="text-[10px] uppercase tracking-wide text-muted-foreground mb-3">
              Confidence distribution (current snapshot)
            </div>
            {(() => {
              const recs = research?.latest?.recommendations ?? []
              if (recs.length === 0) {
                return <div className="text-sm text-muted-foreground">No recommendations.</div>
              }
              const buckets = [
                { label: "<60%", min: 0, max: 0.6, count: 0 },
                { label: "60-65%", min: 0.6, max: 0.65, count: 0 },
                { label: "65-70%", min: 0.65, max: 0.7, count: 0 },
                { label: "70-80%", min: 0.7, max: 0.8, count: 0 },
                { label: "80-90%", min: 0.8, max: 0.9, count: 0 },
                { label: "90%+", min: 0.9, max: 1.01, count: 0 },
              ]
              for (const r of recs) {
                const c = r.confidence ?? 0
                for (const b of buckets) {
                  if (c >= b.min && c < b.max) { b.count++; break }
                }
              }
              const max = Math.max(...buckets.map((b) => b.count), 1)
              return (
                <div className="space-y-1.5">
                  {buckets.map((b) => (
                    <div key={b.label} className="flex items-center gap-2">
                      <div className="w-14 shrink-0 text-xs text-muted-foreground text-right">{b.label}</div>
                      <div className="relative h-4 flex-1 overflow-hidden rounded bg-muted/40">
                        <div
                          className={cn(
                            "h-full rounded",
                            b.min >= 0.7 ? "bg-gain/50" : b.min >= 0.65 ? "bg-primary/40" : "bg-muted-foreground/30",
                          )}
                          style={{ width: `${(b.count / max) * 100}%` }}
                        />
                      </div>
                      <Mono className="w-6 shrink-0 text-right text-xs">{b.count}</Mono>
                    </div>
                  ))}
                </div>
              )
            })()}
          </CardContent>
        </Card>
      </div>

      <div className="grid gap-4 md:grid-cols-3">
        <Card>
          <CardContent className="p-4 text-center">
            <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
              No active signals
            </div>
            <div className="mt-1 font-mono text-2xl font-semibold tabular-nums">
              {summary.noActiveSignals}
            </div>
            <div className="text-xs text-muted-foreground">
              markets with zero strategy fires (24h total)
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-4 text-center">
            <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
              Strategy mix
            </div>
            <div className="mt-1 flex flex-wrap justify-center gap-1.5">
              {Object.entries(summary.finalByStrategyMix)
                .sort(([, a], [, b]) => b - a)
                .map(([mix, ct]) => (
                  <Badge key={mix} variant="outline" className="font-mono text-xs">
                    {mix}: {ct}
                  </Badge>
                ))}
              {Object.keys(summary.finalByStrategyMix).length === 0 ? (
                <span className="text-sm text-muted-foreground">—</span>
              ) : null}
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-4 text-center">
            <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
              Schema version
            </div>
            <div className="mt-1 font-mono text-2xl font-semibold tabular-nums">
              v{String(research?.latest?.schema_version ?? "?")}
            </div>
            <div className={cn(
              "text-xs",
              research?.latest?.schema_version === 2 ? "text-gain" : "text-warn",
            )}>
              {research?.latest?.schema_version === 2 ? "AI pass ran" : "AI pass may have failed"}
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  )
}
