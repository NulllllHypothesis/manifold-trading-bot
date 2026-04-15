import { PageHeader } from "@/components/page-header"
import { ComingSoon } from "@/components/coming-soon"
import { Card, CardContent } from "@/components/ui/card"
import { Mono, formatTimestamp } from "@/components/format"
import { readStrategyWeights } from "@/lib/data"
import { cn } from "@/lib/utils"

export const dynamic = "force-dynamic"

export default async function StrategyLabPage() {
  const weights = await readStrategyWeights()

  return (
    <div className="space-y-6">
      <PageHeader
        title="Strategy Lab"
        description="Current strategy weights, live sample counts vs activation gates, and how each strategy is contributing to outcomes. This is where you explain why the bot is trusting or down-weighting a strategy."
      />

      {weights ? (
        <Card>
          <CardContent className="p-0">
            <div className="border-b border-border/50 p-4 text-xs text-muted-foreground">
              Generated <Mono>{formatTimestamp(weights.generated_at)}</Mono> ·
              gate ≥<Mono>{weights.min_samples_threshold ?? 10}</Mono> live
              samples ·<Mono>{weights.sample_count ?? 0}</Mono> total samples
            </div>
            <table className="w-full text-sm">
              <thead className="bg-muted/30 text-xs uppercase tracking-wide text-muted-foreground">
                <tr>
                  <th className="px-4 py-2 text-left font-medium">Strategy</th>
                  <th className="px-4 py-2 text-right font-medium">Weight</th>
                  <th className="px-4 py-2 text-right font-medium">
                    Live samples
                  </th>
                  <th className="px-4 py-2 text-left font-medium">State</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(weights.weights).map(([strat, w], idx) => {
                  const samples = weights.per_strategy_samples?.[strat] ?? 0
                  const diverged = Math.abs(w - 1.0) > 0.001
                  const state = diverged
                    ? "diverged from default"
                    : samples >= (weights.min_samples_threshold ?? 10)
                      ? "at default (gate met)"
                      : "at default (insufficient samples)"
                  return (
                    <tr
                      key={strat}
                      className={cn(
                        "border-t border-border/50",
                        idx % 2 === 1 ? "bg-muted/10" : undefined,
                      )}
                    >
                      <td className="px-4 py-2.5 font-medium">{strat}</td>
                      <td className="px-4 py-2.5 text-right">
                        <Mono
                          className={cn(
                            diverged && "text-primary",
                          )}
                        >
                          {w.toFixed(4)}
                        </Mono>
                      </td>
                      <td className="px-4 py-2.5 text-right">
                        <Mono>{samples}</Mono>
                      </td>
                      <td
                        className={cn(
                          "px-4 py-2.5 text-xs",
                          diverged
                            ? "text-primary"
                            : "text-muted-foreground",
                        )}
                      >
                        {state}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </CardContent>
        </Card>
      ) : null}

      <ComingSoon
        feature="Strategy contribution analysis"
        description="Charts showing per-strategy fire rate over time, accuracy on resolved trades, EV contribution, family dedup wins (momentum/contrarian/fundamental). Per-category strategy weights heatmap."
        dataSources={[
          "data/strategy_weights.json",
          "data/category_accuracy.json",
          "manifold_bot/paper_trading_state.json (resolved trades)",
        ]}
        buildOrder={8}
      />
    </div>
  )
}
