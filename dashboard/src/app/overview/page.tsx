import { PageHeader } from "@/components/page-header"
import { Card, CardContent } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import {
  Mono,
  formatCurrency,
  formatPercent,
  formatTimestamp,
} from "@/components/format"
import {
  summarizePortfolio,
  summarizeRecentCounters,
  computeAlerts,
} from "@/lib/data"
import type { Trade } from "@/lib/schemas/portfolio"
import { cn } from "@/lib/utils"
import {
  AlertTriangleIcon,
  CheckCircle2Icon,
} from "lucide-react"

export const dynamic = "force-dynamic"

export default async function OverviewPage() {
  const [portfolio, alerts, conversionWindow] = await Promise.all([
    summarizePortfolio(),
    computeAlerts(),
    summarizeRecentCounters(undefined, { windowHours: 24 }),
  ])

  const totalMarketsFetched = conversionWindow.marketsFetched
  const totalTradesExecuted = conversionWindow.tradesExecuted
  const conversionRate =
    totalMarketsFetched > 0
      ? (totalTradesExecuted / totalMarketsFetched) * 100
      : null

  const recentActivity: Trade[] = portfolio?.recentResolved ?? []

  return (
    <div className="space-y-6">
      <PageHeader
        title="Overview"
        description="Balance, pipeline health, and the most critical alerts."
      />

      <div className="grid grid-cols-2 gap-4 md:grid-cols-6">
        <KpiCard
          label="Balance"
          value={portfolio ? formatCurrency(portfolio.balance) : "—"}
        />
        <KpiCard
          label="Total P&L"
          value={
            portfolio
              ? formatCurrency(portfolio.realizedPnl, { signed: true })
              : "—"
          }
          tone={
            portfolio
              ? portfolio.realizedPnl >= 0
                ? "gain"
                : "loss"
              : "muted"
          }
        />
        <KpiCard
          label="Today P&L"
          value={
            portfolio
              ? formatCurrency(portfolio.todayPnl, { signed: true })
              : "—"
          }
          tone={
            portfolio
              ? portfolio.todayPnl > 0
                ? "gain"
                : portfolio.todayPnl < 0
                  ? "loss"
                  : "muted"
              : "muted"
          }
        />
        <KpiCard
          label="Open / Max"
          value={
            portfolio
              ? `${portfolio.openPositionCount} / ${portfolio.bookHealth.maxPositions}`
              : "—"
          }
          tone={portfolio?.bookHealth.bookFull ? "loss" : "default"}
          subtitle={
            portfolio?.bookHealth.bookFull ? "BOOK FULL" : undefined
          }
        />
        <KpiCard
          label="Win rate"
          value={portfolio ? formatPercent(portfolio.winRate) : "—"}
          subtitle={
            portfolio
              ? `${portfolio.winCount}W / ${portfolio.loseCount}L${portfolio.totalTrades - portfolio.winCount - portfolio.loseCount > 0 ? ` / ${portfolio.totalTrades - portfolio.winCount - portfolio.loseCount}N` : ""} of ${portfolio.totalTrades}`
              : undefined
          }
        />
        <KpiCard
          label="Conversion"
          value={conversionRate !== null ? `${conversionRate.toFixed(1)}%` : "—"}
          subtitle={`${totalTradesExecuted} trades / ${totalMarketsFetched} mkts`}
          tone={
            conversionRate !== null && conversionRate < 1 ? "loss" : "default"
          }
        />
      </div>

      <Card>
        <CardContent className="p-0">
          <div className="px-4 py-3 text-xs uppercase tracking-wide text-muted-foreground border-b border-border">
            Alerts ({alerts.length})
          </div>
          {alerts.length === 0 ? (
            <div className="flex items-center gap-2 p-4 text-sm text-muted-foreground">
              <CheckCircle2Icon className="h-4 w-4 text-gain" />
              All systems healthy, pipeline converting, book has capacity.
            </div>
          ) : (
            <ul className="divide-y divide-border">
              {alerts.map((a, i) => (
                <li
                  key={`${a.level}-${a.title}-${i}`}
                  className="flex items-start gap-3 px-4 py-3"
                >
                  {a.level === "error" ? (
                    <AlertTriangleIcon className="mt-0.5 h-4 w-4 shrink-0 text-loss" />
                  ) : a.level === "warn" ? (
                    <AlertTriangleIcon className="mt-0.5 h-4 w-4 shrink-0 text-warn" />
                  ) : (
                    <AlertTriangleIcon className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
                  )}
                  <div className="min-w-0 flex-1">
                    <div className="text-base font-medium">{a.title}</div>
                    {a.detail ? (
                      <div className="text-sm text-muted-foreground">
                        {a.detail}
                      </div>
                    ) : null}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardContent className="p-0">
          <div className="px-4 py-3 text-xs uppercase tracking-wide text-muted-foreground border-b border-border">
            Recent activity ({recentActivity.length})
          </div>
          {recentActivity.length === 0 ? (
            <div className="p-4 text-sm text-muted-foreground">
              No resolved trades yet.
            </div>
          ) : (
            <ul className="divide-y divide-border">
              {recentActivity.map((t) => {
                const isWin =
                  t.status === "WIN" || (t.profit ?? 0) > 0
                const isLose =
                  t.status === "LOSE" || (t.profit ?? 0) < 0
                return (
                  <li
                    key={`${t.trade_id}-${t.market_id}`}
                    className="flex items-center gap-3 px-4 py-2.5"
                  >
                    <Badge
                      variant="outline"
                      className={cn(
                        "text-xs font-mono shrink-0",
                        isWin && "border-gain/40 text-gain",
                        isLose && "border-loss/40 text-loss",
                      )}
                    >
                      {isWin ? "WIN" : isLose ? "LOSE" : t.status}
                    </Badge>
                    <div className="min-w-0 flex-1 truncate text-base">
                      {t.question || (
                        <Mono className="text-muted-foreground">
                          {t.market_id}
                        </Mono>
                      )}
                    </div>
                    <Mono
                      className={cn(
                        "shrink-0",
                        (t.profit ?? 0) > 0 && "text-gain",
                        (t.profit ?? 0) < 0 && "text-loss",
                      )}
                    >
                      {formatCurrency(t.profit ?? null, { signed: true })}
                    </Mono>
                    <Mono className="shrink-0 text-sm text-muted-foreground">
                      {formatTimestamp(t.resolved_at ?? t.timestamp).slice(
                        0,
                        10,
                      )}
                    </Mono>
                  </li>
                )
              })}
            </ul>
          )}
        </CardContent>
      </Card>
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
        <div className="text-sm uppercase tracking-wide text-muted-foreground">
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
