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
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Mono, formatCurrency, formatPercent } from "@/components/format"
import { summarizePortfolio } from "@/lib/data"
import type { Trade } from "@/lib/schemas/portfolio"
import { cn } from "@/lib/utils"
import { AlertTriangleIcon, LockIcon } from "lucide-react"

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
    trade.status === "WIN" ||
    (trade.profit !== undefined && (trade.profit ?? 0) > 0)
  const isLose =
    trade.status === "LOSE" ||
    (trade.profit !== undefined && (trade.profit ?? 0) < 0)
  if (isWin) {
    return (
      <Badge
        variant="outline"
        className="border-gain/40 text-gain text-[10px]"
      >
        WIN
      </Badge>
    )
  }
  if (isLose) {
    return (
      <Badge
        variant="outline"
        className="border-loss/40 text-loss text-[10px]"
      >
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

  const { bookHealth } = portfolio

  const openSorted = [...portfolio.openPositions].sort(
    (a, b) => (a.timestamp || "").localeCompare(b.timestamp || ""),
  )

  return (
    <div className="space-y-6">
      <PageHeader
        title="Portfolio"
        description="The current book — open positions, slot usage, and turnover health."
      />

      {bookHealth.bookFull ? (
        <Alert variant="destructive">
          <LockIcon className="h-4 w-4" />
          <AlertTitle>
            Book full: {portfolio.openPositionCount}/{bookHealth.maxPositions}
          </AlertTitle>
          <AlertDescription>
            All position slots occupied. New trades are blocked until positions
            resolve or are swapped. Oldest position is{" "}
            {bookHealth.oldestOpenDays ?? "?"} days old.
          </AlertDescription>
        </Alert>
      ) : null}

      {bookHealth.stalePositionCount > 0 ? (
        <Alert>
          <AlertTriangleIcon className="h-4 w-4" />
          <AlertTitle>
            {bookHealth.stalePositionCount} stale position
            {bookHealth.stalePositionCount === 1 ? "" : "s"} (&gt;7 days)
          </AlertTitle>
          <AlertDescription>
            Stale positions block turnover and starve the learning loop. Consider
            closing via swap or manual resolution review.
          </AlertDescription>
        </Alert>
      ) : null}

      <Card>
        <CardContent className="p-4 text-sm text-muted-foreground">
          <Mono>{portfolio.openPositionCount}</Mono> open ·{" "}
          <Mono>{formatCurrency(portfolio.openExposure)}</Mono> exposure ·{" "}
          <Mono>{formatCurrency(portfolio.balance)}</Mono> cash (
          {Math.round(bookHealth.cashRatio * 100)}% of initial) ·{" "}
          <Mono>{portfolio.totalTrades}</Mono> resolved ·{" "}
          P&L{" "}
          <Mono>
            {formatCurrency(portfolio.realizedPnl, { signed: true })}
          </Mono>
        </CardContent>
      </Card>

      <div className="grid gap-4 md:grid-cols-2">
        <Card>
          <CardContent className="p-0">
            <div className="px-4 py-3 text-xs uppercase tracking-wide text-muted-foreground border-b border-border">
              Category slot usage
            </div>
            {bookHealth.categorySlots.length === 0 ? (
              <div className="p-4 text-sm text-muted-foreground">
                No open positions.
              </div>
            ) : (
              <table className="w-full text-sm">
                <thead className="bg-muted/30 text-xs uppercase tracking-wide text-muted-foreground">
                  <tr>
                    <th className="px-4 py-2 text-left font-medium">
                      Category
                    </th>
                    <th className="px-4 py-2 text-right font-medium">
                      Open
                    </th>
                    <th className="px-4 py-2 text-right font-medium">Cap</th>
                    <th className="px-4 py-2 text-left font-medium">
                      Status
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {bookHealth.categorySlots.map((slot, idx) => (
                    <tr
                      key={slot.category}
                      className={cn(
                        "border-t border-border/50",
                        idx % 2 === 1 ? "bg-muted/10" : undefined,
                      )}
                    >
                      <td className="px-4 py-2 font-medium">
                        {slot.category}
                      </td>
                      <td className="px-4 py-2 text-right">
                        <Mono>{slot.open}</Mono>
                      </td>
                      <td className="px-4 py-2 text-right">
                        <Mono className="text-muted-foreground">
                          {slot.cap}
                        </Mono>
                      </td>
                      <td className="px-4 py-2">
                        {slot.full ? (
                          <Badge
                            variant="outline"
                            className="text-[10px] font-mono border-loss/40 text-loss"
                          >
                            FULL
                          </Badge>
                        ) : (
                          <span className="text-xs text-gain">
                            {slot.cap - slot.open} free
                          </span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardContent className="p-0">
            <div className="px-4 py-3 text-xs uppercase tracking-wide text-muted-foreground border-b border-border">
              Book health
            </div>
            <table className="w-full text-sm">
              <tbody>
                <tr className="border-t border-border/50">
                  <td className="px-4 py-2 text-muted-foreground">
                    Slots used
                  </td>
                  <td className="px-4 py-2 text-right">
                    <Mono
                      className={cn(bookHealth.bookFull && "text-loss")}
                    >
                      {portfolio.openPositionCount}/{bookHealth.maxPositions}
                    </Mono>
                  </td>
                </tr>
                <tr className="border-t border-border/50 bg-muted/10">
                  <td className="px-4 py-2 text-muted-foreground">
                    Cash remaining
                  </td>
                  <td className="px-4 py-2 text-right">
                    <Mono>{formatCurrency(portfolio.balance)}</Mono>
                    <span className="ml-1 text-xs text-muted-foreground">
                      ({Math.round(bookHealth.cashRatio * 100)}%)
                    </span>
                  </td>
                </tr>
                <tr className="border-t border-border/50">
                  <td className="px-4 py-2 text-muted-foreground">
                    Oldest position
                  </td>
                  <td className="px-4 py-2 text-right">
                    <Mono
                      className={cn(
                        bookHealth.oldestOpenDays !== null &&
                          bookHealth.oldestOpenDays >= 7 &&
                          "text-warn",
                      )}
                    >
                      {bookHealth.oldestOpenDays ?? "—"} days
                    </Mono>
                  </td>
                </tr>
                <tr className="border-t border-border/50 bg-muted/10">
                  <td className="px-4 py-2 text-muted-foreground">
                    Stale (&gt;7d)
                  </td>
                  <td className="px-4 py-2 text-right">
                    <Mono
                      className={cn(
                        bookHealth.stalePositionCount > 0 && "text-warn",
                      )}
                    >
                      {bookHealth.stalePositionCount}
                    </Mono>
                  </td>
                </tr>
                <tr className="border-t border-border/50">
                  <td className="px-4 py-2 text-muted-foreground">
                    Last trade
                  </td>
                  <td className="px-4 py-2 text-right">
                    <Mono className="text-xs">
                      {bookHealth.lastTradeTimestamp
                        ? `${bookHealth.daysSinceLastTrade}d ago`
                        : "—"}
                    </Mono>
                  </td>
                </tr>
              </tbody>
            </table>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardContent className="p-0">
          <div className="px-4 py-3 text-xs uppercase tracking-wide text-muted-foreground border-b border-border">
            Open positions ({openSorted.length}) — oldest first
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
                  const isStale = age !== null && age >= 7
                  return (
                    <TableRow
                      key={`${p.trade_id}-${p.market_id}`}
                      className={cn(isStale && "bg-warn/5")}
                    >
                      <TableCell className="max-w-md">
                        <div
                          className="truncate text-base"
                          title={p.question || p.market_id}
                        >
                          {isStale ? (
                            <span className="mr-1.5 text-xs font-mono text-warn">
                              STALE
                            </span>
                          ) : null}
                          {p.question || (
                            <Mono className="text-muted-foreground">
                              {p.market_id}
                            </Mono>
                          )}
                        </div>
                      </TableCell>
                      <TableCell className="text-muted-foreground text-sm">
                        {p.category ?? "unknown"}
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
                          {formatCurrency(p.profit_if_win ?? null, {
                            signed: true,
                          })}
                        </Mono>
                      </TableCell>
                      <TableCell className="text-right">
                        <Mono
                          className={cn(
                            isStale ? "text-warn font-semibold" : "text-muted-foreground",
                          )}
                        >
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
                  <TableHead>Strategies</TableHead>
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
                    <TableCell>
                      <div className="flex flex-wrap gap-1">
                        {(t.strategies ?? []).map((s) => (
                          <Badge
                            key={s}
                            variant="secondary"
                            className="text-xs font-mono"
                          >
                            {s}
                          </Badge>
                        ))}
                        {(!t.strategies || t.strategies.length === 0) ? (
                          <span className="text-xs text-muted-foreground">—</span>
                        ) : null}
                      </div>
                    </TableCell>
                    <TableCell className="text-sm text-muted-foreground">
                      <Mono>
                        {(t.resolved_at ?? t.timestamp ?? "").slice(0, 10)}
                      </Mono>
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
