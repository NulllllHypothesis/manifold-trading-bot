import { PageHeader } from "@/components/page-header"
import { ComingSoon } from "@/components/coming-soon"
import { Card, CardContent } from "@/components/ui/card"
import { Mono, formatPercent, formatTimestamp } from "@/components/format"
import { readCalibrationTable } from "@/lib/data"
import { cn } from "@/lib/utils"

export const dynamic = "force-dynamic"

export default async function CalibrationPage() {
  const cal = await readCalibrationTable()

  return (
    <div className="space-y-6">
      <PageHeader
        title="Calibration & Learning"
        description={'EV calibration, crowd bias by category and bucket, dataset growth, model improvement signals. The "is the bot getting smarter?" page — and an honest accounting of why it currently isn\'t.'}
      />

      {cal ? (
        <Card>
          <CardContent className="p-0">
            <div className="border-b border-border/50 p-4 text-xs text-muted-foreground">
              Generated <Mono>{formatTimestamp(cal.generated_at)}</Mono> · min
              cell <Mono>{cal.min_cell_samples ?? "?"}</Mono> · source{" "}
              <Mono>{cal.source_db ?? "?"}</Mono>
            </div>
            <table className="w-full text-sm">
              <thead className="bg-muted/30 text-xs uppercase tracking-wide text-muted-foreground">
                <tr>
                  <th className="px-4 py-2 text-left font-medium">Category</th>
                  <th className="px-4 py-2 text-right font-medium">Samples</th>
                  <th className="px-4 py-2 text-right font-medium">
                    Avg crowd
                  </th>
                  <th className="px-4 py-2 text-right font-medium">
                    Actual YES rate
                  </th>
                  <th className="px-4 py-2 text-right font-medium">Bias</th>
                  <th className="px-4 py-2 text-left font-medium">Reliable</th>
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
                    <td className="px-4 py-2.5 font-medium">{c.category}</td>
                    <td className="px-4 py-2.5 text-right">
                      <Mono>{c.sample_size}</Mono>
                    </td>
                    <td className="px-4 py-2.5 text-right">
                      <Mono className="text-muted-foreground">
                        {formatPercent(c.avg_crowd_prob, 1)}
                      </Mono>
                    </td>
                    <td className="px-4 py-2.5 text-right">
                      <Mono>{formatPercent(c.actual_yes_rate, 1)}</Mono>
                    </td>
                    <td className="px-4 py-2.5 text-right">
                      <Mono
                        className={cn(
                          (c.bias ?? 0) > 0 ? "text-gain" : "text-loss",
                        )}
                      >
                        {c.bias === undefined
                          ? "—"
                          : `${c.bias > 0 ? "+" : ""}${c.bias.toFixed(4)}`}
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
      ) : null}

      <ComingSoon
        feature="Learning loop visualization"
        description="Calibration drift over time (animated week-by-week), per-bucket bias heatmap (category × probability bucket), category accuracy with activation-gate progress, dataset growth chart. AI-improvement panel that honestly shows why we're stuck (M6 not run, dataset 127 rows, 75% Ollama timeout)."
        dataSources={[
          "data/calibration_table.json",
          "data/calibration.db (via SQL)",
          "data/training_dataset.jsonl (row count)",
          "data/eval_report.json",
        ]}
        buildOrder={9}
      />
    </div>
  )
}
