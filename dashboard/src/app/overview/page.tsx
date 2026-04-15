import { PageHeader } from "@/components/page-header"
import { Card, CardContent } from "@/components/ui/card"
import { ComingSoon } from "@/components/coming-soon"
import {
  Mono,
  formatCurrency,
  formatPercent,
} from "@/components/format"
import { summarizePortfolio } from "@/lib/data"
import { cn } from "@/lib/utils"

export const dynamic = "force-dynamic"

export default async function OverviewPage() {
  const portfolio = await summarizePortfolio()

  return (
    <div className="space-y-6">
      <PageHeader
        title="Overview"
        description="Balance, current book, and the few things you should care about right now."
      />

      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <KpiCard
          label="Balance"
          value={portfolio ? formatCurrency(portfolio.balance) : "—"}
        />
        <KpiCard
          label="Realized P&L"
          value={portfolio ? formatCurrency(portfolio.realizedPnl, { signed: true }) : "—"}
          tone={portfolio ? (portfolio.realizedPnl >= 0 ? "gain" : "loss") : "muted"}
        />
        <KpiCard
          label="Open positions"
          value={portfolio ? `${portfolio.openPositionCount} / 10` : "—"}
        />
        <KpiCard
          label="Win rate"
          value={portfolio ? formatPercent(portfolio.winRate) : "—"}
          subtitle={
            portfolio
              ? `${portfolio.winCount}W / ${portfolio.loseCount}L`
              : undefined
          }
        />
      </div>

      <ComingSoon
        feature="Operator alert rail + recent activity"
        description="Stale-data warnings, AI degradation alerts, pending swap CTAs, and a chronological strip of recent runs/trades will live here."
        dataSources={[
          "data/research_counters.jsonl",
          "data/trader_counters.jsonl",
          "pending_swaps.json",
        ]}
        buildOrder={1}
      />

      {portfolio ? (
        <Card>
          <CardContent className="p-4 text-xs text-muted-foreground">
            Read from <Mono>manifold_bot/paper_trading_state.json</Mono> ·{" "}
            <Mono>{portfolio.openPositions.length}</Mono> open ·{" "}
            <Mono>{portfolio.totalTrades}</Mono> resolved historically · exposure{" "}
            <Mono>{formatCurrency(portfolio.openExposure)}</Mono>
          </CardContent>
        </Card>
      ) : null}
    </div>
  )
}

function KpiCard({
  label,
  value,
  subtitle,
  tone = "default",
}: {
  label: string
  value: string
  subtitle?: string
  tone?: "default" | "gain" | "loss" | "muted"
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
      <CardContent className="p-5">
        <div className="text-xs uppercase tracking-wide text-muted-foreground">
          {label}
        </div>
        <div
          className={cn(
            "mt-1.5 font-mono text-3xl font-semibold tabular-nums",
            valueColor,
          )}
        >
          {value}
        </div>
        {subtitle ? (
          <div className="mt-1 font-mono text-sm text-muted-foreground tabular-nums">
            {subtitle}
          </div>
        ) : null}
      </CardContent>
    </Card>
  )
}
