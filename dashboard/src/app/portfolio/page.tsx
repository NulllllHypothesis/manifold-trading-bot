import { PageHeader } from "@/components/page-header"
import { Card, CardContent } from "@/components/ui/card"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Badge } from "@/components/ui/badge"
import { Mono, formatCurrency, formatPercent } from "@/components/format"
import { summarizePortfolio } from "@/lib/data"
import type { Trade } from "@/lib/schemas/portfolio"
import { cn } from "@/lib/utils"

export const dynamic = "force-dynamic"

function daysSince(iso: string | null | undefined): number | null {
  if (!iso) return null
  const ms = Date.now() - Date.parse(iso)
  if (Number.isNaN(ms)) return null
  return Math.max(0, Math.floor(ms / 86_400_000))
}

function OutcomeBadge({ outcome }: { outcome: string }) {
  const isYes = outcome.toUpperCase() === "YES"
  return (
    <Badge
      variant="outline"
      className={cn(
        "font-mono text-[10px]",
        isYes ? "border-gain/40 text-gain" : "border-loss/40 text-loss",
      )}
    >
      {outcome.toUpperCase()}
    </Badge>
  )
}

function ResultBadge({ trade }: { trade: Trade }) {
  const isWin =
    trade.status === "WIN" || (trade.profit !== undefined && (trade.profit ?? 0) > 0)
  const isLose =
    trade.status === "LOSE" ||
    (trade.profit !== undefined && (trade.profit ?? 0) < 0)
  if (isWin) {
    return (
      <Badge variant="outline" className="border-gain/40 text-gain text-[10px]">
        WIN
      </Badge>
    )
  }
  if (isLose) {
    return (
      <Badge variant="outline" className="border-loss/40 text-loss text-[10px]">
        LOSE
      </Badge>
    )
  }
  return (
    <Badge variant="outline" className="text-xs">
      {trade.status}
    </Badge>
  )
}

export default async function PortfolioPage() {
  const portfolio = await summarizePortfolio()

  if (!portfolio) {
    return (
      <div className="space-y-6">
        <PageHeader
          title="Portfolio"
          description="Paper trading state file missing or unreadable."
        />
      </div>
    )
  }

  const openSorted = [...portfolio.openPositions].sort(
    (a, b) => (a.timestamp || "").localeCompare(b.timestamp || "") * -1,
  )

  return (
    <div className="space-y-6">
      <PageHeader
        title="Portfolio"
        description="The current book — open positions and most recent resolutions."
      />

      <Card>
        <CardContent className="p-4 text-sm text-muted-foreground">
          <Mono>{portfolio.openPositionCount}</Mono> open ·{" "}
          <Mono>{formatCurrency(portfolio.openExposure)}</Mono> exposure ·{" "}
          <Mono>{portfolio.totalTrades}</Mono> resolved historically ·{" "}
          realized <Mono>{formatCurrency(portfolio.realizedPnl, { signed: true })}</Mono>
        </CardContent>
      </Card>

      <Card>
        <CardContent className="p-0">
          <div className="px-4 py-3 text-xs uppercase tracking-wide text-muted-foreground border-b border-border">
            Open positions ({openSorted.length})
          </div>
          {openSorted.length === 0 ? (
            <div className="p-6 text-center text-sm text-muted-foreground">
              No open positions.
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Market</TableHead>
                  <TableHead>Category</TableHead>
                  <TableHead>Side</TableHead>
                  <TableHead className="text-right">Stake</TableHead>
                  <TableHead className="text-right">Entry prob</TableHead>
                  <TableHead className="text-right">Profit if win</TableHead>
                  <TableHead className="text-right">Days open</TableHead>
                  <TableHead>Strategies</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {openSorted.map((p) => {
                  const age = daysSince(p.timestamp)
                  return (
                    <TableRow key={`${p.trade_id}-${p.market_id}`}>
                      <TableCell className="max-w-md">
                        <div
                          className="truncate text-base"
                          title={p.question || p.market_id}
                        >
                          {p.question || (
                            <Mono className="text-muted-foreground">
                              {p.market_id}
                            </Mono>
                          )}
                        </div>
                      </TableCell>
                      <TableCell className="text-muted-foreground text-sm">
                        {p.category ?? "—"}
                      </TableCell>
                      <TableCell>
                        <OutcomeBadge outcome={p.outcome} />
                      </TableCell>
                      <TableCell className="text-right">
                        <Mono>{formatCurrency(p.amount)}</Mono>
                      </TableCell>
                      <TableCell className="text-right">
                        <Mono>{formatPercent(p.probability, 0)}</Mono>
                      </TableCell>
                      <TableCell className="text-right">
                        <Mono className="text-gain">
                          {formatCurrency(p.profit_if_win ?? null, { signed: true })}
                        </Mono>
                      </TableCell>
                      <TableCell className="text-right">
                        <Mono className="text-muted-foreground">
                          {age ?? "—"}
                        </Mono>
                      </TableCell>
                      <TableCell>
                        <div className="flex flex-wrap gap-1">
                          {(p.strategies ?? []).map((s) => (
                            <Badge
                              key={s}
                              variant="secondary"
                              className="text-xs font-mono"
                            >
                              {s}
                            </Badge>
                          ))}
                        </div>
                      </TableCell>
                    </TableRow>
                  )
                })}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardContent className="p-0">
          <div className="px-4 py-3 text-xs uppercase tracking-wide text-muted-foreground border-b border-border">
            Recent resolutions ({portfolio.recentResolved.length})
          </div>
          {portfolio.recentResolved.length === 0 ? (
            <div className="p-6 text-center text-sm text-muted-foreground">
              No resolved trades yet.
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Market</TableHead>
                  <TableHead>Side</TableHead>
                  <TableHead className="text-right">Stake</TableHead>
                  <TableHead>Result</TableHead>
                  <TableHead className="text-right">P&L</TableHead>
                  <TableHead>Resolved</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {portfolio.recentResolved.map((t) => (
                  <TableRow key={`${t.trade_id}-${t.market_id}`}>
                    <TableCell className="max-w-md">
                      <div
                        className="truncate text-base"
                        title={t.question || t.market_id}
                      >
                        {t.question || (
                          <Mono className="text-muted-foreground">
                            {t.market_id}
                          </Mono>
                        )}
                      </div>
                    </TableCell>
                    <TableCell>
                      <OutcomeBadge outcome={t.outcome} />
                    </TableCell>
                    <TableCell className="text-right">
                      <Mono>{formatCurrency(t.amount)}</Mono>
                    </TableCell>
                    <TableCell>
                      <ResultBadge trade={t} />
                    </TableCell>
                    <TableCell className="text-right">
                      <Mono
                        className={cn(
                          (t.profit ?? 0) > 0 && "text-gain",
                          (t.profit ?? 0) < 0 && "text-loss",
                        )}
                      >
                        {formatCurrency(t.profit ?? null, { signed: true })}
                      </Mono>
                    </TableCell>
                    <TableCell className="text-sm text-muted-foreground">
                      <Mono>{(t.resolved_at ?? t.timestamp ?? "").slice(0, 10)}</Mono>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
