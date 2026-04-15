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
  readDiagnostics,
  readPendingSwaps,
  readResearchCounters,
  summarizePortfolio,
} from "@/lib/data"
import type { Trade } from "@/lib/schemas/portfolio"
import { cn } from "@/lib/utils"
import { AlertTriangleIcon, CheckCircle2Icon } from "lucide-react"

export const dynamic = "force-dynamic"

type Alert = {
  level: "warn" | "error" | "info"
  title: string
  detail?: string
}

function buildAlerts(args: {
  staleFiles: Array<{ key: string }>
  missingFiles: Array<{ key: string }>
  pendingSwapCount: number
  aiNoResultRate: number | null
  aiAnalyzed: number
}): Alert[] {
  const out: Alert[] = []
  for (const f of args.missingFiles) {
    out.push({
      level: "error",
      title: `Missing: ${f.key}`,
      detail: "Expected data file is not on disk.",
    })
  }
  for (const f of args.staleFiles) {
    out.push({
      level: "warn",
      title: `Stale: ${f.key}`,
      detail: "File exists but has not been updated within its freshness window.",
    })
  }
  if (args.pendingSwapCount > 0) {
    out.push({
      level: "info",
      title: `${args.pendingSwapCount} pending swap proposal${
        args.pendingSwapCount === 1 ? "" : "s"
      }`,
      detail: "Review on the Swaps page.",
    })
  }
  if (args.aiNoResultRate !== null && args.aiNoResultRate > 0.4) {
    out.push({
      level: args.aiNoResultRate > 0.7 ? "error" : "warn",
      title: `AI no_result rate ${Math.round(args.aiNoResultRate * 100)}%`,
      detail: `${args.aiAnalyzed} analyzed across last 5 research runs.`,
    })
  }
  return out
}

export default async function OverviewPage() {
  const [portfolio, diagnostics, pendingSwaps, researchCounters] =
    await Promise.all([
      summarizePortfolio(),
      readDiagnostics(),
      readPendingSwaps(),
      readResearchCounters(undefined, { tailLines: 5 }),
    ])

  const staleFiles = diagnostics.files.filter((f) => f.exists && f.isStale)
  const missingFiles = diagnostics.files.filter((f) => !f.exists)

  const recent = researchCounters.slice(-5)
  const aiAnalyzed = recent.reduce((s, r) => s + (r.ai_analyzed ?? 0), 0)
  const aiNoResult = recent.reduce((s, r) => s + (r.ai_no_result ?? 0), 0)
  const aiNoResultRate = aiAnalyzed > 0 ? aiNoResult / aiAnalyzed : null

  const alerts = buildAlerts({
    staleFiles,
    missingFiles,
    pendingSwapCount: pendingSwaps.filter((s) => s.status === "pending").length,
    aiNoResultRate,
    aiAnalyzed,
  })

  const recentActivity: Trade[] = portfolio?.recentResolved ?? []

  return (
    <div className="space-y-6">
      <PageHeader
        title="Overview"
        description="Balance, current book, alerts, and the most recent resolutions."
      />

      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <KpiCard
          label="Balance"
          value={portfolio ? formatCurrency(portfolio.balance) : "—"}
        />
        <KpiCard
          label="Realized P&L"
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

      <Card>
        <CardContent className="p-0">
          <div className="px-4 py-3 text-xs uppercase tracking-wide text-muted-foreground border-b border-border">
            Alerts ({alerts.length})
          </div>
          {alerts.length === 0 ? (
            <div className="flex items-center gap-2 p-4 text-sm text-muted-foreground">
              <CheckCircle2Icon className="h-4 w-4 text-gain" />
              All data files fresh, AI healthy, no pending actions.
            </div>
          ) : (
            <ul className="divide-y divide-border">
              {alerts.map((a, i) => (
                <li
                  key={`${a.level}-${a.title}-${i}`}
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
                      {formatTimestamp(t.resolved_at ?? t.timestamp).slice(0, 10)}
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
