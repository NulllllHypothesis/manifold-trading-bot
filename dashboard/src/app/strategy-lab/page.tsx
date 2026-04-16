import { PageHeader } from "@/components/page-header"
import { Card, CardContent } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Mono, formatTimestamp, formatPercent } from "@/components/format"
import { readStrategyWeights, summarizePerformance, readResearchCounters } from "@/lib/data"
import { cn } from "@/lib/utils"

export const dynamic = "force-dynamic"

export default async function StrategyLabPage() {
  const [weights, performance, researchCounters] = await Promise.all([
    readStrategyWeights(),
    summarizePerformance(),
    readResearchCounters(undefined, { tailLines: 50 }),
  ])

  const strategyAccuracy = new Map<
    string,
    { wins: number; losses: number; pnl: number; trades: number }
  >()

  if (performance) {
    for (const s of performance.byStrategy) {
      const winRate = s.trades > 0 ? s.wins / s.trades : 0
      strategyAccuracy.set(s.key, {
        wins: s.wins,
        losses: s.trades - s.wins,
        pnl: s.pnl,
        trades: s.trades,
      })
    }
  }

  const strategyFireRate = new Map<string, number>()
  const totalRuns = researchCounters.length
  for (const r of researchCounters) {
    if (r.raw_fires) {
      for (const [strat, count] of Object.entries(r.raw_fires)) {
        strategyFireRate.set(strat, (strategyFireRate.get(strat) ?? 0) + count)
      }
    }
  }

  const allStrategies = new Set<string>()
  if (weights) {
    for (const s of Object.keys(weights.weights)) allStrategies.add(s)
  }
  for (const s of strategyAccuracy.keys()) allStrategies.add(s)
  for (const s of strategyFireRate.keys()) allStrategies.add(s)

  return (
    <div className="space-y-6">
      <PageHeader
        title="Strategy Lab"
        description="Current strategy weights, fire rates, and how each strategy is contributing to outcomes. This is where you understand why the bot trusts or down-weights a strategy."
      />

      {weights ? (
        <Card>
          <CardContent className="p-4 text-xs text-muted-foreground">
            Weights generated{" "}
            <Mono>{formatTimestamp(weights.generated_at)}</Mono> · gate:{" "}
            <Mono>≥{weights.min_samples_threshold ?? 10}</Mono> live samples ·{" "}
            <Mono>{weights.sample_count ?? 0}</Mono> total samples
          </CardContent>
        </Card>
      ) : null}

      <Card>
        <CardContent className="p-0">
          <div className="border-b border-border/50 px-4 py-3 text-xs uppercase tracking-wide text-muted-foreground">
            Strategy performance & weights
          </div>
          <table className="w-full text-sm">
            <thead className="bg-muted/30 text-xs uppercase tracking-wide text-muted-foreground">
              <tr>
                <th className="px-4 py-2 text-left font-medium">Strategy</th>
                <th className="px-4 py-2 text-right font-medium">Weight</th>
                <th className="px-4 py-2 text-right font-medium">Samples</th>
                <th className="px-4 py-2 text-right font-medium">Fires</th>
                <th className="px-4 py-2 text-right font-medium">Trades</th>
                <th className="px-4 py-2 text-right font-medium">Win rate</th>
                <th className="px-4 py-2 text-right font-medium">P&L</th>
                <th className="px-4 py-2 text-left font-medium">State</th>
              </tr>
            </thead>
            <tbody>
              {[...allStrategies].map((strat, idx) => {
                const w = weights?.weights[strat] ?? null
                const samples = weights?.per_strategy_samples?.[strat] ?? 0
                const fires = strategyFireRate.get(strat) ?? 0
                const acc = strategyAccuracy.get(strat)
                const diverged = w !== null && Math.abs(w - 1.0) > 0.001
                const winRate =
                  acc && acc.trades > 0 ? acc.wins / acc.trades : null
                const state = diverged
                  ? "diverged"
                  : w !== null && samples >= (weights?.min_samples_threshold ?? 10)
                    ? "gate met"
                    : w !== null
                      ? "insufficient"
                      : "unweighted"

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
                      {w !== null ? (
                        <Mono className={cn(diverged && "text-primary")}>
                          {w.toFixed(4)}
                        </Mono>
                      ) : (
                        <span className="text-muted-foreground">—</span>
                      )}
                    </td>
                    <td className="px-4 py-2.5 text-right">
                      <Mono>{samples}</Mono>
                    </td>
                    <td className="px-4 py-2.5 text-right">
                      <Mono className="text-muted-foreground">{fires}</Mono>
                      {totalRuns > 0 ? (
                        <div className="text-[10px] text-muted-foreground">
                          {((fires / totalRuns)).toFixed(1)}/run
                        </div>
                      ) : null}
                    </td>
                    <td className="px-4 py-2.5 text-right">
                      {acc ? (
                        <Mono>
                          {acc.wins}W/{acc.losses}L
                        </Mono>
                      ) : (
                        <span className="text-muted-foreground">—</span>
                      )}
                    </td>
                    <td className="px-4 py-2.5 text-right">
                      {winRate !== null ? (
                        <Mono
                          className={cn(
                            winRate >= 0.5 ? "text-gain" : "text-loss",
                          )}
                        >
                          {formatPercent(winRate)}
                        </Mono>
                      ) : (
                        <span className="text-muted-foreground">—</span>
                      )}
                    </td>
                    <td className="px-4 py-2.5 text-right">
                      {acc ? (
                        <Mono
                          className={cn(
                            acc.pnl >= 0 ? "text-gain" : "text-loss",
                          )}
                        >
                          {acc.pnl >= 0 ? "+" : ""}${acc.pnl.toFixed(2)}
                        </Mono>
                      ) : (
                        <span className="text-muted-foreground">—</span>
                      )}
                    </td>
                    <td className="px-4 py-2.5">
                      <Badge
                        variant="outline"
                        className={cn(
                          "text-[10px] font-mono",
                          diverged && "border-primary/40 text-primary",
                          state === "gate met" && "border-gain/40 text-gain",
                          state === "insufficient" &&
                            "border-border text-muted-foreground",
                        )}
                      >
                        {state}
                      </Badge>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </CardContent>
      </Card>

      {weights?.by_category &&
      Object.keys(weights.by_category).length > 0 ? (
        <Card>
          <CardContent className="p-0">
            <div className="border-b border-border/50 px-4 py-3 text-xs uppercase tracking-wide text-muted-foreground">
              Per-category strategy weights
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="bg-muted/30 text-xs uppercase tracking-wide text-muted-foreground">
                  <tr>
                    <th className="px-4 py-2 text-left font-medium">
                      Category
                    </th>
                    {[...allStrategies].map((s) => (
                      <th
                        key={s}
                        className="px-3 py-2 text-right font-medium"
                      >
                        {s.slice(0, 12)}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(weights.by_category).map(
                    ([cat, catWeights], idx) => (
                      <tr
                        key={cat}
                        className={cn(
                          "border-t border-border/50",
                          idx % 2 === 1 ? "bg-muted/10" : undefined,
                        )}
                      >
                        <td className="px-4 py-2.5 font-medium">{cat}</td>
                        {[...allStrategies].map((s) => {
                          const v = catWeights[s]
                          const diff =
                            v !== undefined ? Math.abs(v - 1.0) > 0.001 : false
                          return (
                            <td
                              key={s}
                              className="px-3 py-2.5 text-right"
                            >
                              {v !== undefined ? (
                                <Mono
                                  className={cn(
                                    diff && "text-primary",
                                    !diff && "text-muted-foreground",
                                  )}
                                >
                                  {v.toFixed(3)}
                                </Mono>
                              ) : (
                                <span className="text-muted-foreground">
                                  —
                                </span>
                              )}
                            </td>
                          )
                        })}
                      </tr>
                    ),
                  )}
                </tbody>
              </table>
            </div>
          </CardContent>
        </Card>
      ) : null}
    </div>
  )
}
