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
import { Mono, formatPercent, formatTimestamp } from "@/components/format"
import { summarizeOpportunities } from "@/lib/data"
import type { AiStatus } from "@/lib/data/opportunities"
import { cn } from "@/lib/utils"
import {
  LockIcon,
  ShieldAlertIcon,
} from "lucide-react"

export const dynamic = "force-dynamic"

function DirectionBadge({ dir }: { dir: string | null | undefined }) {
  if (!dir) return <span className="text-muted-foreground">—</span>
  const up = dir.toUpperCase()
  const cls =
    up === "YES"
      ? "border-gain/40 text-gain"
      : up === "NO"
        ? "border-loss/40 text-loss"
        : ""
  return (
    <Badge variant="outline" className={cn("font-mono text-[10px]", cls)}>
      {up}
    </Badge>
  )
}

const AI_STATUS_CONFIG: Record<
  AiStatus,
  { label: string; cls: string; detail: string }
> = {
  not_run: {
    label: "—",
    cls: "text-muted-foreground",
    detail: "Not sent to AI",
  },
  skip_or_no_result: {
    label: "SKIP / NO RESULT",
    cls: "border-warn/40 text-warn",
    detail: "AI returned SKIP or timed out — indistinguishable in current data (both write ai_returned_skip=true). Treated as veto by trader.",
  },
  agree: {
    label: "AGREE",
    cls: "border-gain/40 text-gain",
    detail: "AI confirms stat signal",
  },
  disagree: {
    label: "DISAGREE",
    cls: "border-warn/40 text-warn",
    detail: "AI disagrees with stat signal",
  },
}

function AiStatusBadge({ status }: { status: AiStatus }) {
  const cfg = AI_STATUS_CONFIG[status]
  if (status === "not_run") {
    return <span className="text-muted-foreground text-xs">—</span>
  }
  return (
    <Badge variant="outline" className={cn("font-mono text-[10px]", cfg.cls)} title={cfg.detail}>
      {cfg.label}
    </Badge>
  )
}

const REJECTION_LABELS: Record<string, { label: string; color: string }> = {
  ai_veto: { label: "AI veto (skip or timeout)", color: "text-warn" },
  low_confidence: { label: "Low conf", color: "text-muted-foreground" },
  existing_open: { label: "Already held", color: "text-warn" },
  max_positions: { label: "Book full", color: "text-loss" },
  category_cap: { label: "Cat cap", color: "text-warn" },
  liquidity: { label: "Low liq", color: "text-muted-foreground" },
}

function RejectionBadge({ reason }: { reason: string | null }) {
  if (!reason) {
    return (
      <Badge
        variant="outline"
        className="font-mono text-[10px] border-gain/40 text-gain"
      >
        TRADABLE
      </Badge>
    )
  }
  const cfg = REJECTION_LABELS[reason] ?? {
    label: reason,
    color: "text-muted-foreground",
  }
  return (
    <Badge
      variant="outline"
      className={cn("font-mono text-[10px]", cfg.color)}
    >
      {cfg.label}
    </Badge>
  )
}

export default async function OpportunitiesPage() {
  const data = await summarizeOpportunities()

  if (!data) {
    return (
      <div className="space-y-6">
        <PageHeader
          title="Opportunities"
          description="market_research.json is missing or empty."
        />
      </div>
    )
  }

  const rejectionCounts: Record<string, number> = {}
  for (const r of data.recommendations) {
    if (r.rejectionReason) {
      rejectionCounts[r.rejectionReason] =
        (rejectionCounts[r.rejectionReason] ?? 0) + 1
    }
  }

  return (
    <div className="space-y-6">
      <PageHeader
        title="Opportunities"
        description="Blocked opportunity debugger — why the bot can't trade these recommendations."
      />

      {data.bookFull ? (
        <Alert variant="destructive">
          <LockIcon className="h-4 w-4" />
          <AlertTitle>
            Book is full: {data.openCount}/{data.maxPositions} positions
          </AlertTitle>
          <AlertDescription>
            All {data.maxPositions} position slots are occupied. No new trades
            can execute until positions resolve or are closed via swap. The
            trader rejected {rejectionCounts["max_positions"] ?? 0}{" "}
            recommendations this snapshot for this reason alone.
          </AlertDescription>
        </Alert>
      ) : null}

      {data.aiVetoedCount > 0 ? (
        <Alert>
          <ShieldAlertIcon className="h-4 w-4" />
          <AlertTitle>
            AI vetoed {data.aiVetoedCount}/{data.aiTotalCandidates} candidates
            (skip or timeout — indistinguishable)
          </AlertTitle>
          <AlertDescription>
            auto_research.py writes the same{" "}
            <Mono>ai_returned_skip: true</Mono> for both real AI SKIPs and
            timeouts/failures, so the dashboard cannot tell them apart. The
            trader vetoes both equally. Fix: add an explicit{" "}
            <Mono>ai_status</Mono> field in the producer so the dashboard can
            distinguish real skips from failures.
          </AlertDescription>
        </Alert>
      ) : null}

      <Card>
        <CardContent className="p-4 text-sm text-muted-foreground">
          Snapshot: <Mono>{formatTimestamp(data.timestamp)}</Mono> ·{" "}
          <Mono>{data.totalMarkets}</Mono> markets scanned ·{" "}
          <Mono>{data.recommendations.length}</Mono> recommendations
        </CardContent>
      </Card>

      <div className="grid grid-cols-2 gap-4 md:grid-cols-5">
        <Card>
          <CardContent className="p-4 text-center">
            <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
              Tradable
            </div>
            <div className={cn("mt-1 font-mono text-2xl font-semibold", data.tradableCount > 0 ? "text-gain" : "text-loss")}>
              {data.tradableCount}
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-4 text-center">
            <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
              Blocked
            </div>
            <div className="mt-1 font-mono text-2xl font-semibold text-loss">
              {data.blockedCount}
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-4 text-center">
            <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
              Book
            </div>
            <div className={cn("mt-1 font-mono text-2xl font-semibold", data.bookFull ? "text-loss" : "text-gain")}>
              {data.openCount}/{data.maxPositions}
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-4 text-center">
            <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
              AI vetoed
            </div>
            <div className={cn("mt-1 font-mono text-2xl font-semibold", data.aiVetoedCount > 0 ? "text-warn" : "text-muted-foreground")}>
              {data.aiVetoedCount}/{data.aiTotalCandidates}
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-4 text-center">
            <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
              Category slots
            </div>
            <div className="mt-1 text-xs font-mono space-y-0.5">
              {Object.entries(data.categoryCounts).map(([cat, n]) => {
                const cap = cat === "other" ? 10 : 3
                return (
                  <div key={cat} className={cn(n >= cap && "text-loss")}>
                    {cat}: {n}/{cap}
                  </div>
                )
              })}
              {Object.keys(data.categoryCounts).length === 0 ? (
                <span className="text-muted-foreground">empty</span>
              ) : null}
            </div>
          </CardContent>
        </Card>
      </div>

      {Object.keys(rejectionCounts).length > 0 ? (
        <Card>
          <CardContent className="p-4">
            <div className="text-[10px] uppercase tracking-wide text-muted-foreground mb-3">
              Rejection breakdown (this snapshot)
            </div>
            <div className="flex flex-wrap gap-2">
              {Object.entries(rejectionCounts)
                .sort(([, a], [, b]) => b - a)
                .map(([reason, count]) => {
                  const cfg = REJECTION_LABELS[reason]
                  return (
                    <Badge
                      key={reason}
                      variant="outline"
                      className={cn("font-mono text-xs", cfg?.color)}
                    >
                      {cfg?.label ?? reason}: {count}
                    </Badge>
                  )
                })}
            </div>
          </CardContent>
        </Card>
      ) : null}

      <Card>
        <CardContent className="p-0">
          <div className="px-4 py-3 text-xs uppercase tracking-wide text-muted-foreground border-b border-border">
            Recommendations (tradable first, then blocked)
          </div>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Question</TableHead>
                <TableHead>Cat</TableHead>
                <TableHead className="text-right">Prob</TableHead>
                <TableHead>Pick</TableHead>
                <TableHead className="text-right">Conf</TableHead>
                <TableHead className="text-right">EV exec</TableHead>
                <TableHead className="text-right">Boost</TableHead>
                <TableHead>AI status</TableHead>
                <TableHead>AI src</TableHead>
                <TableHead>AI reasoning</TableHead>
                <TableHead className="text-right">Liq</TableHead>
                <TableHead className="text-right">Bettors</TableHead>
                <TableHead>Would-be rejection</TableHead>
                <TableHead>Strategies</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.recommendations.map((r) => (
                <TableRow
                  key={r.market_id}
                  className={cn(r.wouldBeBlocked && "opacity-60")}
                >
                  <TableCell className="max-w-sm">
                    <div className="truncate text-base" title={r.question}>
                      {r.existing_position ? (
                        <span className="mr-1.5 text-xs font-mono text-warn">
                          HELD
                        </span>
                      ) : null}
                      {r.question}
                    </div>
                  </TableCell>
                  <TableCell className="text-muted-foreground text-xs">
                    {r.category ?? "—"}
                    {r.categorySlotInfo ? (
                      <div className="text-[10px]">{r.categorySlotInfo}</div>
                    ) : null}
                  </TableCell>
                  <TableCell className="text-right">
                    <Mono>{formatPercent(r.probability, 0)}</Mono>
                  </TableCell>
                  <TableCell>
                    <DirectionBadge dir={r.recommendation} />
                  </TableCell>
                  <TableCell className="text-right">
                    <Mono>{formatPercent(r.confidence, 0)}</Mono>
                  </TableCell>
                  <TableCell className="text-right">
                    <Mono
                      className={cn(
                        (r.estimated_ev_exec ?? 0) > 0 && "text-gain",
                        (r.estimated_ev_exec ?? 0) < 0 && "text-loss",
                        r.estimated_ev_exec == null && "text-muted-foreground",
                      )}
                    >
                      {r.estimated_ev_exec != null
                        ? r.estimated_ev_exec.toFixed(3)
                        : "null"}
                    </Mono>
                  </TableCell>
                  <TableCell className="text-right">
                    {r.priority_boost ? (
                      <Mono className="text-xs text-warn">
                        +{r.priority_boost.toFixed(2)}
                      </Mono>
                    ) : (
                      <span className="text-xs text-muted-foreground">—</span>
                    )}
                  </TableCell>
                  <TableCell>
                    <AiStatusBadge status={r.aiStatus} />
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {r.ai_source ?? "—"}
                  </TableCell>
                  <TableCell className="max-w-50">
                    {r.ai_reasoning ? (
                      <span className="text-xs text-muted-foreground truncate block" title={r.ai_reasoning}>
                        {r.ai_reasoning.slice(0, 80)}{r.ai_reasoning.length > 80 ? "…" : ""}
                      </span>
                    ) : (
                      <span className="text-xs text-muted-foreground">—</span>
                    )}
                  </TableCell>
                  <TableCell className="text-right">
                    <Mono className="text-xs text-muted-foreground">
                      {r.liquidity != null ? `$${Math.round(r.liquidity)}` : "—"}
                    </Mono>
                  </TableCell>
                  <TableCell className="text-right">
                    <Mono className="text-xs text-muted-foreground">
                      {r.unique_bettors ?? "—"}
                    </Mono>
                  </TableCell>
                  <TableCell>
                    <RejectionBadge reason={r.rejectionReason} />
                  </TableCell>
                  <TableCell>
                    <div className="flex flex-wrap gap-1">
                      {(r.strategies ?? []).map((s) => (
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
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  )
}
