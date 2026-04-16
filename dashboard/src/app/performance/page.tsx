import { PageHeader } from "@/components/page-header"
import { Card, CardContent } from "@/components/ui/card"
import { Mono, formatCurrency, formatPercent } from "@/components/format"
import { summarizePerformance } from "@/lib/data"
import {
  DailyPnlChart,
  EquityCurveChart,
  SliceBarChart,
} from "@/components/performance-charts"
import { cn } from "@/lib/utils"

export const dynamic = "force-dynamic"

function KpiCard({
  label,
  value,
  tone = "default",
  subtitle,
}: {
  label: string
  value: string
  tone?: "default" | "gain" | "loss" | "muted"
  subtitle?: string
}) {
  const valueColor =
    tone === "gain"
      ? "text-gain"
      : tone === "loss"
        ? "text-loss"
        : tone === "muted"
          ? "text-muted-foreground"
          : "text-foreground"
  return (
    <Card>
      <CardContent className="p-4">
        <div className="text-sm uppercase tracking-wide text-muted-foreground">
          {label}
        </div>
        <div
          className={cn(
            "mt-1.5 font-mono text-2xl font-semibold tabular-nums",
            valueColor,
          )}
        >
          {value}
        </div>
        {subtitle ? (
          <div className="mt-0.5 text-sm text-muted-foreground">{subtitle}</div>
        ) : null}
      </CardContent>
    </Card>
  )
}

export default async function PerformancePage() {
  const perf = await summarizePerformance()

  if (!perf) {
    return (
      <div className="space-y-6">
        <PageHeader
          title="Performance"
          description="Paper trading state file missing or unreadable."
        />
      </div>
    )
  }

  const { stats, equityCurve, dailyPnl, byCategory, byStrategy } = perf

  return (
    <div className="space-y-6">
      <PageHeader
        title="Performance"
        description="Equity curve, daily P&L, and slices by category and strategy — from resolved trades."
      />

      <Card>
        <CardContent className="p-4 text-sm text-muted-foreground">
          Window:{" "}
          <Mono>{perf.windowFrom ?? "—"}</Mono> → <Mono>{perf.windowTo ?? "—"}</Mono>{" "}
          · <Mono>{stats.totalResolved}</Mono> resolved trades ·{" "}
          <Mono>{stats.wins}W</Mono> / <Mono>{stats.losses}L</Mono>{stats.totalResolved - stats.wins - stats.losses > 0 ? <>{" "}/ <Mono>{stats.totalResolved - stats.wins - stats.losses}N</Mono></> : null}
        </CardContent>
      </Card>

      <div className="grid grid-cols-2 gap-4 md:grid-cols-3 lg:grid-cols-6">
        <KpiCard
          label="Total P&L"
          value={formatCurrency(stats.totalPnl, { signed: true })}
          tone={stats.totalPnl >= 0 ? "gain" : "loss"}
        />
        <KpiCard
          label="Win rate"
          value={formatPercent(stats.winRate, 0)}
          subtitle={`${stats.wins}W / ${stats.losses}L${stats.totalResolved - stats.wins - stats.losses > 0 ? ` / ${stats.totalResolved - stats.wins - stats.losses}N` : ""} of ${stats.totalResolved}`}
        />
        <KpiCard
          label="Profit factor"
          value={stats.profitFactor !== null ? stats.profitFactor.toFixed(2) : "—"}
          tone={
            stats.profitFactor === null
              ? "muted"
              : stats.profitFactor >= 1
                ? "gain"
                : "loss"
          }
        />
        <KpiCard
          label="Avg trade"
          value={formatCurrency(stats.avgTrade, { signed: true })}
          tone={stats.avgTrade >= 0 ? "gain" : "loss"}
        />
        <KpiCard
          label="Max drawdown"
          value={formatCurrency(-stats.maxDrawdown, { signed: true })}
          tone={stats.maxDrawdown > 0 ? "loss" : "muted"}
        />
        <KpiCard
          label="Best / worst"
          value={`${formatCurrency(stats.bestTrade, { signed: true })}`}
          tone="gain"
          subtitle={`worst ${formatCurrency(stats.worstTrade, { signed: true })}`}
        />
      </div>

      <Card>
        <CardContent className="p-4">
          <div className="mb-3 text-xs uppercase tracking-wide text-muted-foreground">
            Cumulative P&L
          </div>
          <EquityCurveChart data={equityCurve} />
        </CardContent>
      </Card>

      <Card>
        <CardContent className="p-4">
          <div className="mb-3 text-xs uppercase tracking-wide text-muted-foreground">
            Daily P&L
          </div>
          <DailyPnlChart data={dailyPnl} />
        </CardContent>
      </Card>

      <div className="grid gap-4 md:grid-cols-2">
        <Card>
          <CardContent className="p-0">
            <div className="px-4 py-3 text-xs uppercase tracking-wide text-muted-foreground border-b border-border">
              By category ({byCategory.length})
            </div>
            <SliceBarChart
              data={byCategory}
              emptyLabel="No category data."
            />
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-0">
            <div className="px-4 py-3 text-xs uppercase tracking-wide text-muted-foreground border-b border-border">
              By strategy ({byStrategy.length})
            </div>
            <SliceBarChart
              data={byStrategy}
              emptyLabel="No strategy tags on resolved trades."
            />
          </CardContent>
        </Card>
      </div>
    </div>
  )
}
