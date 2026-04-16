import { PageHeader } from "@/components/page-header"
import { Card, CardContent } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Mono, formatTimestamp, formatPercent } from "@/components/format"
import { readStrategyWeights, summarizePerformance, readResearchCounters, readPaperState } from "@/lib/data"
import {
  getCategoryWeight,
  getCategoryAccuracy,
  getCategorySamples,
} from "@/lib/schemas/strategy-weights"
import { cn } from "@/lib/utils"
import { AlertTriangleIcon } from "lucide-react"

export const dynamic = "force-dynamic"

export default async function StrategyLabPage() {
  const [weights, performance, researchCounters, paperState] = await Promise.all([
    readStrategyWeights(),
    summarizePerformance(),
    readResearchCounters(undefined, { tailLines: 50 }),
    readPaperState(),
  ])

  const strategyAccuracy = new Map<
    string,
    { wins: number; losses: number; pnl: number; trades: number }
  >()

  if (performance) {
    for (const s of performance.byStrategy) {
      strategyAccuracy.set(s.key, {
        wins: s.wins,
        losses: s.trades - s.wins,
        pnl: s.pnl,
        trades: s.trades,
      })
    }
  }

  let resolvedTotal = 0
  let resolvedWithStrategies = 0
  if (paperState) {
    for (const t of paperState.trade_history) {
      if (t.status === "OPEN") continue
      resolvedTotal++
      if (t.strategies && t.strategies.length > 0) resolvedWithStrategies++
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

  const metadataCoverage =
    resolvedTotal > 0 ? resolvedWithStrategies / resolvedTotal : 1

  return (
    <div className="space-y-6">
      <PageHeader
        title="Strategy Lab"
        description="Current strategy weights, fire rates, and how each strategy is contributing to outcomes. This is where you understand why the bot trusts or down-weights a strategy."
      />

      {metadataCoverage < 0.5 && resolvedTotal > 0 ? (
        <Alert>
          <AlertTriangleIcon className="h-4 w-4" />
          <AlertTitle>
            Strategy metadata missing on {resolvedTotal - resolvedWithStrategies}/
            {resolvedTotal} resolved trades
          </AlertTitle>
          <AlertDescription>
            Only {Math.round(metadataCoverage * 100)}% of resolved trades have{" "}
            <Mono>strategies</Mono> populated. Win/loss and P&L columns below
            are undercounting — a strategy may have real history that is not
            reflected here because older trades lack the field.
          </AlertDescription>
        </Alert>
      ) : null}

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
                const wr =
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
                    <td className="px-4 py-2.5">
                      <div className="flex items-center gap-2">
                        <div className="relative h-3 w-16 overflow-hidden rounded bg-muted/40">
                          <div
                            className={cn(
                              "h-full rounded",
                              samples >= (weights?.min_samples_threshold ?? 10)
                                ? "bg-gain/60"
                                : "bg-primary/40",
                            )}
                            style={{
                              width: `${Math.min(100, (samples / (weights?.min_samples_threshold ?? 10)) * 100)}%`,
                            }}
                          />
                        </div>
                        <Mono className="text-xs">
                          {samples}/{weights?.min_samples_threshold ?? 10}
                        </Mono>
                      </div>
                    </td>
                    <td className="px-4 py-2.5 text-right">
                      <Mono className="text-muted-foreground">{fires}</Mono>
                      {totalRuns > 0 ? (
                        <div className="text-[10px] text-muted-foreground">
                          {(fires / totalRuns).toFixed(1)}/run
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
                      {wr !== null ? (
                        <Mono
                          className={cn(
                            wr >= 0.5 ? "text-gain" : "text-loss",
                          )}
                        >
                          {formatPercent(wr)}
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
                          const raw = catWeights[s]
                          if (raw === undefined) {
                            return (
                              <td
                                key={s}
                                className="px-3 py-2.5 text-right text-muted-foreground"
                              >
                                —
                              </td>
                            )
                          }
                          const w = getCategoryWeight(raw)
                          const acc = getCategoryAccuracy(raw)
                          const n = getCategorySamples(raw)
                          const diff = Math.abs(w - 1.0) > 0.001
                          return (
                            <td
                              key={s}
                              className="px-3 py-2.5 text-right"
                            >
                              <Mono
                                className={cn(
                                  diff ? "text-primary" : "text-muted-foreground",
                                )}
                              >
                                {w.toFixed(3)}
                              </Mono>
                              {acc !== null || n !== null ? (
                                <div className="text-[10px] text-muted-foreground">
                                  {acc !== null
                                    ? `${(acc * 100).toFixed(0)}%`
                                    : ""}
                                  {n !== null ? ` n=${n}` : ""}
                                </div>
                              ) : null}
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
