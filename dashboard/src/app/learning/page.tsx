import { PageHeader } from "@/components/page-header"
import { Card, CardContent } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Mono, formatTimestamp } from "@/components/format"
import { summarizeLearningLoop } from "@/lib/data/learning"
import { cn } from "@/lib/utils"
import {
  AlertTriangleIcon,
  CheckCircle2Icon,
  ArrowRightIcon,
} from "lucide-react"

export const dynamic = "force-dynamic"

export default async function LearningPage() {
  const data = await summarizeLearningLoop()

  const divergedCount = data.weights.filter((w) => w.diverged).length
  const totalWeights = data.weights.length

  return (
    <div className="space-y-6">
      <PageHeader
        title="Learning Loop"
        description="Is the feedback loop alive? Past markets → bot behavior → resolution → calibration → weight updates → changed trading. This page answers whether the bot is actually adapting."
      />

      {/* Loop health alerts */}
      {data.alerts.length > 0 ? (
        <Card>
          <CardContent className="p-0">
            <div className="px-4 py-3 text-xs uppercase tracking-wide text-muted-foreground border-b border-border">
              Learning health ({data.alerts.length} issue{data.alerts.length === 1 ? "" : "s"})
            </div>
            <ul className="divide-y divide-border">
              {data.alerts.map((a, i) => (
                <li
                  key={`${a.level}-${i}`}
                  className="flex items-start gap-3 px-4 py-3"
                >
                  <AlertTriangleIcon
                    className={cn(
                      "mt-0.5 h-4 w-4 shrink-0",
                      a.level === "error" && "text-loss",
                      a.level === "warn" && "text-warn",
                      a.level === "info" && "text-muted-foreground",
                    )}
                  />
                  <div className="min-w-0 flex-1">
                    <div className="text-sm font-medium">{a.title}</div>
                    <div className="text-xs text-muted-foreground">
                      {a.detail}
                    </div>
                  </div>
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      ) : (
        <Card>
          <CardContent className="p-4 flex items-center gap-2 text-sm text-muted-foreground">
            <CheckCircle2Icon className="h-4 w-4 text-gain" />
            Learning loop is healthy — weights are diverging, jobs are running.
          </CardContent>
        </Card>
      )}

      {/* Loop funnel */}
      <Card>
        <CardContent className="p-0">
          <div className="px-4 py-3 text-xs uppercase tracking-wide text-muted-foreground border-b border-border">
            Feedback loop pipeline
          </div>
          <div className="px-4 py-4">
            <div className="flex flex-wrap items-center gap-2">
              {data.loopFunnel.map((stage, i) => (
                <div key={stage.label} className="flex items-center gap-2">
                  {i > 0 ? (
                    <ArrowRightIcon className="h-3 w-3 text-muted-foreground shrink-0" />
                  ) : null}
                  <div
                    className={cn(
                      "rounded-lg border px-3 py-2 text-center",
                      stage.healthy
                        ? "border-gain/30 bg-gain/5"
                        : "border-warn/30 bg-warn/5",
                    )}
                  >
                    <div className="font-mono text-lg font-semibold tabular-nums">
                      {stage.value}
                    </div>
                    <div className="text-[10px] text-muted-foreground whitespace-nowrap">
                      {stage.label}
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Timestamps */}
      <Card>
        <CardContent className="p-0">
          <div className="px-4 py-3 text-xs uppercase tracking-wide text-muted-foreground border-b border-border">
            Last run timestamps
          </div>
          <table className="w-full text-sm">
            <tbody>
              {[
                { label: "Calibration harvest", ts: data.timestamps.lastHarvest, schedule: "Sundays 02:00 UTC" },
                { label: "M2 audit", ts: data.timestamps.lastM2Audit, schedule: "Sundays 02:30 UTC" },
                { label: "Calibration table", ts: data.timestamps.lastCalibration, schedule: "Sundays 02:00 UTC" },
                { label: "Backtest", ts: data.timestamps.lastBacktest, schedule: "Sundays 03:30 UTC" },
                { label: "Strategy weights", ts: data.timestamps.lastWeightUpdate, schedule: "Mondays 07:30 UTC" },
                { label: "Eval report", ts: data.timestamps.lastEval, schedule: "ad-hoc" },
              ].map((row, idx) => (
                <tr
                  key={row.label}
                  className={cn(
                    "border-t border-border/50",
                    idx % 2 === 1 ? "bg-muted/10" : undefined,
                  )}
                >
                  <td className="px-4 py-2 font-medium">{row.label}</td>
                  <td className="px-4 py-2">
                    {row.ts ? (
                      <Mono className="text-xs">
                        {formatTimestamp(row.ts)}
                      </Mono>
                    ) : (
                      <span className="text-xs text-muted-foreground">
                        never
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-2 text-xs text-muted-foreground">
                    {row.schedule}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </CardContent>
      </Card>

      <div className="grid gap-6 lg:grid-cols-2">
        {/* Strategy weights */}
        <Card>
          <CardContent className="p-0">
            <div className="px-4 py-3 text-xs uppercase tracking-wide text-muted-foreground border-b border-border">
              Strategy weights ({divergedCount}/{totalWeights} diverged from 1.0)
            </div>
            <table className="w-full text-sm">
              <thead className="bg-muted/30 text-xs uppercase tracking-wide text-muted-foreground">
                <tr>
                  <th className="px-4 py-2 text-left font-medium">
                    Strategy
                  </th>
                  <th className="px-4 py-2 text-right font-medium">
                    Weight
                  </th>
                  <th className="px-4 py-2 font-medium">
                    Samples → gate
                  </th>
                  <th className="px-4 py-2 text-left font-medium">Status</th>
                </tr>
              </thead>
              <tbody>
                {data.weights.map((w, idx) => (
                  <tr
                    key={w.strategy}
                    className={cn(
                      "border-t border-border/50",
                      idx % 2 === 1 ? "bg-muted/10" : undefined,
                    )}
                  >
                    <td className="px-4 py-2.5 font-medium">{w.strategy}</td>
                    <td className="px-4 py-2.5 text-right">
                      <Mono
                        className={cn(w.diverged && "text-primary font-semibold")}
                      >
                        {w.weight.toFixed(4)}
                      </Mono>
                    </td>
                    <td className="px-4 py-2.5">
                      <div className="flex items-center gap-2">
                        <div className="relative h-3 w-20 overflow-hidden rounded bg-muted/40">
                          <div
                            className={cn(
                              "h-full rounded",
                              w.samples >= w.gateThreshold
                                ? "bg-gain/60"
                                : "bg-primary/40",
                            )}
                            style={{
                              width: `${Math.min(100, (w.samples / w.gateThreshold) * 100)}%`,
                            }}
                          />
                        </div>
                        <Mono className="text-xs">
                          {w.samples}/{w.gateThreshold}
                        </Mono>
                      </div>
                    </td>
                    <td className="px-4 py-2.5">
                      <Badge
                        variant="outline"
                        className={cn(
                          "text-[10px] font-mono",
                          w.diverged
                            ? "border-primary/40 text-primary"
                            : w.samples >= w.gateThreshold
                              ? "border-gain/40 text-gain"
                              : "border-border text-muted-foreground",
                        )}
                      >
                        {w.diverged
                          ? "adapted"
                          : w.samples >= w.gateThreshold
                            ? "gate met"
                            : "collecting"}
                      </Badge>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </CardContent>
        </Card>

        {/* Category accuracy */}
        <Card>
          <CardContent className="p-0">
            <div className="px-4 py-3 text-xs uppercase tracking-wide text-muted-foreground border-b border-border">
              Category accuracy (adaptive cap gate: ≥{data.categoryAccuracy[0]
                ? 8
                : 8}{" "}
              samples)
            </div>
            {data.categoryAccuracy.length === 0 ? (
              <div className="p-4 text-sm text-muted-foreground">
                No category accuracy data yet. Need resolved trades with
                category metadata.
              </div>
            ) : (
              <table className="w-full text-sm">
                <thead className="bg-muted/30 text-xs uppercase tracking-wide text-muted-foreground">
                  <tr>
                    <th className="px-4 py-2 text-left font-medium">
                      Category
                    </th>
                    <th className="px-4 py-2 text-right font-medium">
                      Accuracy
                    </th>
                    <th className="px-4 py-2 text-right font-medium">
                      Samples
                    </th>
                    <th className="px-4 py-2 text-left font-medium">
                      Cap effect
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {data.categoryAccuracy.map((c, idx) => {
                    const capEffect = !c.gateMet
                      ? "default (3)"
                      : c.accuracy < 0.5
                        ? "restricted (1)"
                        : "default (3)"
                    return (
                      <tr
                        key={c.category}
                        className={cn(
                          "border-t border-border/50",
                          idx % 2 === 1 ? "bg-muted/10" : undefined,
                        )}
                      >
                        <td className="px-4 py-2.5 font-medium">
                          {c.category}
                        </td>
                        <td className="px-4 py-2.5 text-right">
                          <Mono
                            className={cn(
                              c.accuracy >= 0.5 ? "text-gain" : "text-loss",
                            )}
                          >
                            {(c.accuracy * 100).toFixed(0)}%
                          </Mono>
                        </td>
                        <td className="px-4 py-2.5 text-right">
                          <Mono>{c.samples}</Mono>
                        </td>
                        <td className="px-4 py-2.5">
                          <Badge
                            variant="outline"
                            className={cn(
                              "text-[10px] font-mono",
                              !c.gateMet
                                ? "border-border text-muted-foreground"
                                : c.accuracy < 0.5
                                  ? "border-loss/40 text-loss"
                                  : "border-gain/40 text-gain",
                            )}
                          >
                            {capEffect}
                          </Badge>
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            )}
          </CardContent>
        </Card>
      </div>

      {/* Eval report */}
      {data.evalReport ? (
        <Card>
          <CardContent className="p-0">
            <div className="px-4 py-3 text-xs uppercase tracking-wide text-muted-foreground border-b border-border">
              Latest evaluation report
            </div>
            <div className="grid grid-cols-2 gap-4 p-4 md:grid-cols-4">
              <div>
                <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
                  Test examples
                </div>
                <div className="mt-1 font-mono text-xl font-semibold">
                  {data.evalReport.nExamples ?? "—"}
                </div>
              </div>
              <div>
                <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
                  Brier score
                </div>
                <div className="mt-1 font-mono text-xl font-semibold">
                  {data.evalReport.brierScore?.toFixed(4) ?? "—"}
                </div>
                <div className="text-xs text-muted-foreground">
                  lower is better
                </div>
              </div>
              <div>
                <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
                  Directional accuracy
                </div>
                <div className="mt-1 font-mono text-xl font-semibold">
                  {data.evalReport.directionalAccuracy != null
                    ? `${(data.evalReport.directionalAccuracy * 100).toFixed(0)}%`
                    : "—"}
                </div>
              </div>
              <div>
                <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
                  Promotion eligible
                </div>
                <div className="mt-1">
                  <Badge
                    variant="outline"
                    className={cn(
                      "font-mono text-xs",
                      data.evalReport.promotionEligible
                        ? "border-gain/40 text-gain"
                        : "border-border text-muted-foreground",
                    )}
                  >
                    {data.evalReport.promotionEligible ? "YES" : "NO"}
                  </Badge>
                </div>
                {data.evalReport.promotionReason ? (
                  <div className="mt-1 text-xs text-muted-foreground">
                    {data.evalReport.promotionReason}
                  </div>
                ) : null}
              </div>
            </div>
          </CardContent>
        </Card>
      ) : null}

      {/* Backtest report */}
      {data.backtestReport ? (
        <Card>
          <CardContent className="p-4 text-xs text-muted-foreground">
            Last backtest used{" "}
            <Mono>{data.backtestReport.snapshotsUsed ?? "?"}</Mono> snapshots
            {data.backtestReport.dryRun ? (
              <Badge
                variant="outline"
                className="ml-2 text-[10px] font-mono border-warn/40 text-warn"
              >
                DRY RUN
              </Badge>
            ) : null}
          </CardContent>
        </Card>
      ) : null}
    </div>
  )
}
