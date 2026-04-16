import { PageHeader } from "@/components/page-header"
import { Card, CardContent } from "@/components/ui/card"
import { Mono, formatPercent, formatTimestamp } from "@/components/format"
import { readCalibrationTable } from "@/lib/data"
import { cn } from "@/lib/utils"
import {
  CalibrationCurveChart,
  BiasByBucketChart,
} from "@/components/calibration-charts"

export const dynamic = "force-dynamic"

export default async function CalibrationPage() {
  const cal = await readCalibrationTable()

  const totalSamples =
    cal?.by_category.reduce((s, c) => s + c.sample_size, 0) ?? 0

  const avgBias =
    cal && cal.by_category.length > 0
      ? cal.by_category.reduce(
          (s, c) => s + (c.bias ?? 0) * c.sample_size,
          0,
        ) / totalSamples
      : null

  const reliableCount =
    cal?.by_category.filter((c) => c.reliable).length ?? 0

  return (
    <div className="space-y-6">
      <PageHeader
        title="Calibration & Learning"
        description="EV calibration, crowd bias by category and probability bucket. How well the crowd predicts outcomes — and where the edge lives."
      />

      {cal ? (
        <>
          <Card>
            <CardContent className="p-4 text-xs text-muted-foreground">
              Generated <Mono>{formatTimestamp(cal.generated_at)}</Mono> · min
              cell <Mono>{cal.min_cell_samples ?? "?"}</Mono> · source{" "}
              <Mono>{cal.source_db?.split("/").pop() ?? "?"}</Mono>
            </CardContent>
          </Card>

          <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
            <Card>
              <CardContent className="p-5">
                <div className="text-sm uppercase tracking-wide text-muted-foreground">
                  Total samples
                </div>
                <div className="mt-1.5 font-mono text-3xl font-semibold tabular-nums">
                  {totalSamples.toLocaleString()}
                </div>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="p-5">
                <div className="text-sm uppercase tracking-wide text-muted-foreground">
                  Categories
                </div>
                <div className="mt-1.5 font-mono text-3xl font-semibold tabular-nums">
                  {cal.by_category.length}
                </div>
                <div className="mt-1 font-mono text-sm text-muted-foreground">
                  {reliableCount} reliable
                </div>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="p-5">
                <div className="text-sm uppercase tracking-wide text-muted-foreground">
                  Avg crowd bias
                </div>
                <div
                  className={cn(
                    "mt-1.5 font-mono text-3xl font-semibold tabular-nums",
                    avgBias !== null && avgBias > 0
                      ? "text-warn"
                      : "text-muted-foreground",
                  )}
                >
                  {avgBias !== null
                    ? `${avgBias > 0 ? "+" : ""}${(avgBias * 100).toFixed(1)}%`
                    : "—"}
                </div>
                <div className="mt-1 text-xs text-muted-foreground">
                  {avgBias !== null && avgBias > 0
                    ? "Crowd overestimates YES"
                    : avgBias !== null && avgBias < 0
                      ? "Crowd underestimates YES"
                      : ""}
                </div>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="p-5">
                <div className="text-sm uppercase tracking-wide text-muted-foreground">
                  Prob buckets
                </div>
                <div className="mt-1.5 font-mono text-3xl font-semibold tabular-nums">
                  {cal.buckets?.length ?? 0}
                </div>
              </CardContent>
            </Card>
          </div>

          {cal.buckets && cal.buckets.length > 0 ? (
            <div className="grid gap-6 lg:grid-cols-2">
              <Card>
                <CardContent className="p-4">
                  <div className="text-[10px] uppercase tracking-wide text-muted-foreground mb-3">
                    Calibration Curve (crowd prediction vs actual outcome)
                  </div>
                  <div className="text-xs text-muted-foreground mb-2">
                    Dashed line = perfect calibration. Points above = crowd underestimates. Below = crowd overestimates.
                  </div>
                  <CalibrationCurveChart data={cal.buckets} />
                </CardContent>
              </Card>

              <Card>
                <CardContent className="p-4">
                  <div className="text-[10px] uppercase tracking-wide text-muted-foreground mb-3">
                    Crowd Bias by Probability Bucket
                  </div>
                  <div className="text-xs text-muted-foreground mb-2">
                    Positive = crowd overestimates YES probability. Negative = crowd underestimates.
                  </div>
                  <BiasByBucketChart data={cal.buckets} />
                </CardContent>
              </Card>
            </div>
          ) : null}

          {cal.buckets && cal.buckets.length > 0 ? (
            <Card>
              <CardContent className="p-0">
                <div className="border-b border-border/50 px-4 py-3 text-xs uppercase tracking-wide text-muted-foreground">
                  Probability bucket detail
                </div>
                <table className="w-full text-sm">
                  <thead className="bg-muted/30 text-xs uppercase tracking-wide text-muted-foreground">
                    <tr>
                      <th className="px-4 py-2 text-left font-medium">
                        Bucket
                      </th>
                      <th className="px-4 py-2 text-right font-medium">
                        Crowd midpoint
                      </th>
                      <th className="px-4 py-2 text-right font-medium">
                        Actual YES rate
                      </th>
                      <th className="px-4 py-2 text-right font-medium">
                        Bias
                      </th>
                      <th className="px-4 py-2 text-right font-medium">
                        Samples
                      </th>
                      <th className="px-4 py-2 text-left font-medium">
                        Reliable
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {cal.buckets.map((b, idx) => (
                      <tr
                        key={`${b.bucket_low}-${b.bucket_high}`}
                        className={cn(
                          "border-t border-border/50",
                          idx % 2 === 1 ? "bg-muted/10" : undefined,
                        )}
                      >
                        <td className="px-4 py-2.5 font-medium">
                          {(b.bucket_low * 100).toFixed(0)}–
                          {(b.bucket_high * 100).toFixed(0)}%
                        </td>
                        <td className="px-4 py-2.5 text-right">
                          <Mono className="text-muted-foreground">
                            {formatPercent(
                              b.crowd_midpoint ??
                                (b.bucket_low + b.bucket_high) / 2,
                              1,
                            )}
                          </Mono>
                        </td>
                        <td className="px-4 py-2.5 text-right">
                          <Mono>
                            {formatPercent(b.actual_yes_rate, 1)}
                          </Mono>
                        </td>
                        <td className="px-4 py-2.5 text-right">
                          <Mono
                            className={cn(
                              (b.bias ?? 0) > 0 ? "text-gain" : "text-loss",
                            )}
                          >
                            {b.bias === undefined
                              ? "—"
                              : `${b.bias > 0 ? "+" : ""}${(b.bias * 100).toFixed(1)}%`}
                          </Mono>
                        </td>
                        <td className="px-4 py-2.5 text-right">
                          <Mono>{b.sample_size}</Mono>
                        </td>
                        <td className="px-4 py-2.5 text-xs">
                          {b.reliable ? (
                            <span className="text-gain">yes</span>
                          ) : (
                            <span className="text-muted-foreground">
                              no
                            </span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </CardContent>
            </Card>
          ) : null}

          <Card>
            <CardContent className="p-0">
              <div className="border-b border-border/50 px-4 py-3 text-xs uppercase tracking-wide text-muted-foreground">
                Calibration by category
              </div>
              <table className="w-full text-sm">
                <thead className="bg-muted/30 text-xs uppercase tracking-wide text-muted-foreground">
                  <tr>
                    <th className="px-4 py-2 text-left font-medium">
                      Category
                    </th>
                    <th className="px-4 py-2 text-right font-medium">
                      Samples
                    </th>
                    <th className="px-4 py-2 text-right font-medium">
                      Avg crowd
                    </th>
                    <th className="px-4 py-2 text-right font-medium">
                      Actual YES rate
                    </th>
                    <th className="px-4 py-2 text-right font-medium">Bias</th>
                    <th className="px-4 py-2 text-left font-medium">
                      Reliable
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {cal.by_category.map((c, idx) => (
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
                        <Mono>{c.sample_size}</Mono>
                      </td>
                      <td className="px-4 py-2.5 text-right">
                        <Mono className="text-muted-foreground">
                          {formatPercent(c.avg_crowd_prob, 1)}
                        </Mono>
                      </td>
                      <td className="px-4 py-2.5 text-right">
                        <Mono>
                          {formatPercent(c.actual_yes_rate, 1)}
                        </Mono>
                      </td>
                      <td className="px-4 py-2.5 text-right">
                        <Mono
                          className={cn(
                            (c.bias ?? 0) > 0 ? "text-gain" : "text-loss",
                          )}
                        >
                          {c.bias === undefined
                            ? "—"
                            : `${c.bias > 0 ? "+" : ""}${(c.bias * 100).toFixed(1)}%`}
                        </Mono>
                      </td>
                      <td className="px-4 py-2.5 text-xs">
                        {c.reliable ? (
                          <span className="text-gain">yes</span>
                        ) : (
                          <span className="text-muted-foreground">no</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </CardContent>
          </Card>
        </>
      ) : (
        <Card>
          <CardContent className="p-8 text-center text-sm text-muted-foreground">
            No calibration data found. The Sunday harvest job produces this
            file.
          </CardContent>
        </Card>
      )}
    </div>
  )
}
