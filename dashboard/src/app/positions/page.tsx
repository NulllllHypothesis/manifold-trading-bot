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
import {
  Mono,
  formatCurrency,
  formatPercent,
  formatTimestamp,
} from "@/components/format"
import { PositionTrajectoryChart } from "@/components/position-trajectory-chart"
import {
  readPaperState,
  readPendingCloses,
  readPendingAbandons,
  readMarketTrajectory,
  isObsoletePositionClass,
  normalizePositionClass,
} from "@/lib/data"
import type { Trade } from "@/lib/schemas/portfolio"
import type {
  PendingClose,
  PendingAbandon,
} from "@/lib/schemas/position-management"
import { cn } from "@/lib/utils"
import {
  AlertTriangleIcon,
  ArchiveIcon,
  ClockAlertIcon,
  SkullIcon,
  TrendingDownIcon,
} from "lucide-react"

export const dynamic = "force-dynamic"

// Phase 2.4 detector thresholds, kept in sync with manifold_bot/config.py.
// Staleness pill uses Phase 2.3's REPRICE_STALE_HOURS (2h).
const REPRICE_STALE_HOURS = 2

function hoursSince(iso: string | null | undefined): number | null {
  if (!iso) return null
  const ms = Date.now() - Date.parse(iso)
  if (Number.isNaN(ms)) return null
  return Math.max(0, ms / 3_600_000)
}

function daysSince(iso: string | null | undefined): number | null {
  const h = hoursSince(iso)
  return h == null ? null : h / 24
}

function PositionClassBadge({ cls }: { cls: string | undefined }) {
  if (!cls) {
    return (
      <Badge variant="outline" className="text-[10px] text-muted-foreground">
        UNCLASSIFIED
      </Badge>
    )
  }
  // Normalise obsolete 4-bucket values to the current 3-bucket model so the
  // badge colour reflects what the backend actually enforces against, not
  // the stale on-disk label. We also flag the row as OBSOLETE so the
  // operator knows the on-disk value needs a rewrite — it's currently
  // being normalised at read time on every slot check.
  const normalised = normalizePositionClass(cls) ?? cls
  const obsolete = isObsoletePositionClass(cls)
  const color =
    normalised === "short"
      ? "border-gain/40 text-gain"
      : normalised === "medium"
      ? "border-primary/50 text-primary"
      : normalised === "long_or_uncertain"
      ? "border-loss/40 text-loss"
      : "border-border text-muted-foreground"
  return (
    <span className="flex flex-wrap items-center gap-1">
      <Badge variant="outline" className={cn("font-mono text-[10px]", color)}>
        {normalised.toUpperCase()}
      </Badge>
      {obsolete && (
        <Badge
          variant="outline"
          className="border-loss/40 font-mono text-[9px] text-loss"
          title={`Persisted as ${cls}; backend normalises to ${normalised}`}
        >
          OBSOLETE
        </Badge>
      )}
    </span>
  )
}

function OutcomeBadge({ outcome }: { outcome: string | undefined }) {
  if (!outcome) return <span className="text-muted-foreground">—</span>
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

function RepriceFreshnessPill({ lastReprice }: { lastReprice: string | undefined }) {
  const hrs = hoursSince(lastReprice)
  if (hrs == null) {
    return (
      <Badge variant="outline" className="text-[10px] text-muted-foreground">
        never repriced
      </Badge>
    )
  }
  const stale = hrs > REPRICE_STALE_HOURS
  return (
    <Badge
      variant="outline"
      className={cn(
        "text-[10px] font-mono",
        stale ? "border-loss/40 text-loss" : "border-gain/40 text-gain",
      )}
    >
      {stale ? "STALE" : "FRESH"} ({hrs < 1 ? `${Math.round(hrs * 60)}m` : `${hrs.toFixed(1)}h`})
    </Badge>
  )
}

function PnlCell({ value }: { value: number | null | undefined }) {
  if (value == null) return <span className="text-muted-foreground">—</span>
  const c = value > 0 ? "text-gain" : value < 0 ? "text-loss" : "text-muted-foreground"
  return <Mono className={c}>{formatCurrency(value)}</Mono>
}

export default async function PositionsPage() {
  const [state, pendingCloses, pendingAbandons] = await Promise.all([
    readPaperState(),
    readPendingCloses(),
    readPendingAbandons(),
  ])

  if (!state) {
    return (
      <div className="space-y-6">
        <PageHeader
          title="Position Management"
          description="Paper trading state file missing or unreadable."
        />
      </div>
    )
  }

  // Flatten all legs and bucket by status
  const allLegs: Trade[] = Object.values(state.positions).flat()
  const open = allLegs.filter((t) => t.status === "OPEN")
  const stranded = allLegs.filter((t) => t.status === "STRANDED")
  const abandoned = allLegs.filter((t) => t.status === "ABANDONED")
  const closedEarly = allLegs.filter((t) => t.status === "CLOSED_EARLY")

  // Active proposals only — same semantics as the swap-checker view.
  const activeCloses = pendingCloses.filter((p) => p.status === "pending")
  const activeAbandons = pendingAbandons.filter((p) => p.status === "pending")

  // Proposal set keyed by market_id for HOLD/CLOSE badge lookup on the
  // Live Book rows. A market with an active proposal is flagged CLOSE.
  const closeProposedMarkets = new Set(activeCloses.map((p) => p.market_id))

  // Load the latest market-aggregated trajectory (per-leg rows collapsed by
  // snapshot_at and summed — see aggregateSnapshotsByRun). For a market
  // with multiple OPEN legs this is the honest line; the raw per-leg rows
  // would render as a zig-zag of overlapping points at each timestamp.
  const chartCandidate = pickChartCandidate(open)
  const snapshots = chartCandidate
    ? readMarketTrajectory(chartCandidate.market_id, { limit: 500 })
    : []

  const strandedAmount = stranded.reduce((acc, t) => acc + (t.amount ?? 0), 0)
  const abandonedAmount = abandoned.reduce((acc, t) => acc + (t.amount ?? 0), 0)
  // "Gap" = either truly missing class OR carrying an obsolete 4-bucket
  // value (long_reliable / long_risky / long_high / long_mid_low). Both
  // cases mean the persisted label doesn't match what the backend reads.
  const gapCount = open.filter(
    (t) => !t.position_class || isObsoletePositionClass(t.position_class),
  ).length
  const obsoleteCount = open.filter((t) =>
    isObsoletePositionClass(t.position_class),
  ).length

  return (
    <div className="space-y-6">
      <PageHeader
        title="Position Management"
        description="Phase 2.5 — live book with classification, plus pending close / abandon proposals and write-off history."
      />

      {gapCount > 0 && (
        <Alert variant="destructive">
          <AlertTriangleIcon className="h-4 w-4" />
          <AlertTitle>
            {gapCount} open position(s) with stale classification
            {obsoleteCount > 0 &&
              ` (${obsoleteCount} carry obsolete 4-bucket values)`}
          </AlertTitle>
          <AlertDescription>
            These either lack <code className="text-xs">position_class</code>{" "}
            entirely, or carry legacy values (
            <code className="text-xs">long_reliable</code>,{" "}
            <code className="text-xs">long_risky</code>,{" "}
            <code className="text-xs">long_high</code>,{" "}
            <code className="text-xs">long_mid_low</code>) that the backend
            normalises on read. The data is still handled correctly, but
            rewriting on disk removes the migration dependency. Run{" "}
            <code className="text-xs">
              scripts/backfill_position_metadata.py --apply
            </code>
            .
          </AlertDescription>
        </Alert>
      )}

      {/* ── Live Book ───────────────────────────────────────── */}
      <section className="space-y-3">
        <h2 className="text-sm font-semibold text-foreground">
          Live Book — {open.length} open
        </h2>
        <Card>
          <CardContent className="p-0">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Market</TableHead>
                  <TableHead>Side</TableHead>
                  <TableHead>Class</TableHead>
                  <TableHead className="text-right">Amount</TableHead>
                  <TableHead className="text-right">Entry → Now</TableHead>
                  <TableHead className="text-right">Unrealised</TableHead>
                  <TableHead className="text-right">Age</TableHead>
                  <TableHead>Reprice</TableHead>
                  <TableHead>Action</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {open.length === 0 ? (
                  <TableRow>
                    <TableCell colSpan={9} className="text-muted-foreground">
                      No open positions.
                    </TableCell>
                  </TableRow>
                ) : (
                  open.map((t) => {
                    const entry = t.entry_probability ?? t.probability
                    const age = daysSince(t.timestamp)
                    const isClose = closeProposedMarkets.has(t.market_id)
                    return (
                      <TableRow key={`${t.market_id}-${t.trade_id}`}>
                        <TableCell
                          className="max-w-65 truncate"
                          title={t.question ?? t.market_id}
                        >
                          {t.question ?? (
                            <Mono className="text-muted-foreground">
                              {t.market_id.slice(0, 12)}
                            </Mono>
                          )}
                        </TableCell>
                        <TableCell>
                          <OutcomeBadge outcome={t.outcome} />
                        </TableCell>
                        <TableCell>
                          <PositionClassBadge cls={t.position_class} />
                        </TableCell>
                        <TableCell className="text-right">
                          <Mono>{formatCurrency(t.amount)}</Mono>
                        </TableCell>
                        <TableCell className="text-right">
                          <Mono className="text-muted-foreground">
                            {entry != null ? formatPercent(entry) : "—"}
                          </Mono>{" "}
                          →{" "}
                          <Mono>
                            {t.current_probability != null
                              ? formatPercent(t.current_probability)
                              : "—"}
                          </Mono>
                        </TableCell>
                        <TableCell className="text-right">
                          <PnlCell value={t.current_unrealised_pnl} />
                        </TableCell>
                        <TableCell className="text-right">
                          <Mono>{age != null ? `${age.toFixed(1)}d` : "—"}</Mono>
                        </TableCell>
                        <TableCell>
                          <RepriceFreshnessPill lastReprice={t.last_repriced_at} />
                        </TableCell>
                        <TableCell>
                          {isClose ? (
                            <Badge
                              variant="outline"
                              className="border-loss/40 text-loss text-[10px]"
                            >
                              CLOSE
                            </Badge>
                          ) : (
                            <Badge
                              variant="outline"
                              className="border-border text-muted-foreground text-[10px]"
                            >
                              HOLD
                            </Badge>
                          )}
                        </TableCell>
                      </TableRow>
                    )
                  })
                )}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      </section>

      {/* ── P&L trajectory (one illustrative market) ───────── */}
      {chartCandidate && (
        <section className="space-y-3">
          <h2 className="text-sm font-semibold text-foreground">
            Unrealised P&amp;L trajectory —{" "}
            <span className="text-muted-foreground">
              {chartCandidate.question ?? chartCandidate.market_id}
            </span>
          </h2>
          <Card>
            <CardContent className="pt-4">
              <PositionTrajectoryChart snapshots={snapshots} />
            </CardContent>
          </Card>
        </section>
      )}

      {/* ── Pending CLOSE proposals (Phase 2.3) ────────────── */}
      <section className="space-y-3">
        <h2 className="flex items-center gap-2 text-sm font-semibold">
          <TrendingDownIcon className="h-4 w-4 text-loss" />
          Pending Close Proposals — {activeCloses.length}
        </h2>
        <Card>
          <CardContent className="p-0">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>#</TableHead>
                  <TableHead>Market</TableHead>
                  <TableHead>Side</TableHead>
                  <TableHead className="text-right">Score</TableHead>
                  <TableHead className="text-right">Unrealised</TableHead>
                  <TableHead>Reason</TableHead>
                  <TableHead>Proposed</TableHead>
                  <TableHead>Approve</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {activeCloses.length === 0 ? (
                  <TableRow>
                    <TableCell colSpan={8} className="text-muted-foreground">
                      No pending close proposals.
                    </TableCell>
                  </TableRow>
                ) : (
                  activeCloses.map((p) => (
                    <ClosesRow key={p.id} proposal={p} />
                  ))
                )}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      </section>

      {/* ── Pending ABANDON proposals (Phase 2.4) ──────────── */}
      <section className="space-y-3">
        <h2 className="flex items-center gap-2 text-sm font-semibold">
          <SkullIcon className="h-4 w-4 text-loss" />
          Pending Abandon Proposals — {activeAbandons.length}
        </h2>
        <Card>
          <CardContent className="p-0">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>#</TableHead>
                  <TableHead>Market</TableHead>
                  <TableHead className="text-right">Write-off</TableHead>
                  <TableHead>Reason</TableHead>
                  <TableHead>Proposed</TableHead>
                  <TableHead>Approve</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {activeAbandons.length === 0 ? (
                  <TableRow>
                    <TableCell colSpan={6} className="text-muted-foreground">
                      No pending abandon proposals.
                    </TableCell>
                  </TableRow>
                ) : (
                  activeAbandons.map((p) => (
                    <AbandonsRow key={p.id} proposal={p} />
                  ))
                )}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      </section>

      {/* ── STRANDED section ───────────────────────────────── */}
      <section className="space-y-3">
        <h2 className="flex items-center gap-2 text-sm font-semibold">
          <ClockAlertIcon className="h-4 w-4 text-primary" />
          Stranded Positions — {stranded.length} (${strandedAmount.toFixed(2)} locked)
        </h2>
        <p className="text-xs text-muted-foreground">
          Past-close markets waiting on creator resolution. Not counted in slot
          accounting and not marked as WIN/LOSE — resolve_positions.py will
          reconcile when / if the creator resolves.
        </p>
        <Card>
          <CardContent className="p-0">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Market</TableHead>
                  <TableHead>Side</TableHead>
                  <TableHead className="text-right">Amount</TableHead>
                  <TableHead>Stranded at</TableHead>
                  <TableHead>Reason</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {stranded.length === 0 ? (
                  <TableRow>
                    <TableCell colSpan={5} className="text-muted-foreground">
                      No stranded positions.
                    </TableCell>
                  </TableRow>
                ) : (
                  stranded.map((t) => (
                    <TableRow key={`${t.market_id}-${t.trade_id}`}>
                      <TableCell
                        className="max-w-65 truncate"
                        title={t.question ?? t.market_id}
                      >
                        {t.question ?? (
                          <Mono className="text-muted-foreground">
                            {t.market_id.slice(0, 12)}
                          </Mono>
                        )}
                      </TableCell>
                      <TableCell>
                        <OutcomeBadge outcome={t.outcome} />
                      </TableCell>
                      <TableCell className="text-right">
                        <Mono>{formatCurrency(t.amount)}</Mono>
                      </TableCell>
                      <TableCell>
                        <Mono className="text-muted-foreground">
                          {formatTimestamp(t.stranded_at)}
                        </Mono>
                      </TableCell>
                      <TableCell className="text-xs text-muted-foreground">
                        {t.stranded_reason ?? "—"}
                      </TableCell>
                    </TableRow>
                  ))
                )}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      </section>

      {/* ── ABANDONED / CLOSED_EARLY history ───────────────── */}
      <section className="space-y-3">
        <h2 className="flex items-center gap-2 text-sm font-semibold">
          <ArchiveIcon className="h-4 w-4 text-muted-foreground" />
          Terminal history — {abandoned.length} abandoned, {closedEarly.length} closed early
        </h2>
        <Card>
          <CardContent className="p-0">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Status</TableHead>
                  <TableHead>Market</TableHead>
                  <TableHead>Side</TableHead>
                  <TableHead className="text-right">Amount</TableHead>
                  <TableHead className="text-right">Realised</TableHead>
                  <TableHead>When</TableHead>
                  <TableHead>Reason</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {abandoned.length + closedEarly.length === 0 ? (
                  <TableRow>
                    <TableCell colSpan={7} className="text-muted-foreground">
                      No terminal closes yet.
                    </TableCell>
                  </TableRow>
                ) : (
                  [...closedEarly, ...abandoned].map((t) => (
                    <TableRow key={`${t.market_id}-${t.trade_id}`}>
                      <TableCell>
                        <Badge
                          variant="outline"
                          className={cn(
                            "text-[10px] font-mono",
                            t.status === "ABANDONED"
                              ? "border-loss/40 text-loss"
                              : "border-primary/50 text-primary",
                          )}
                        >
                          {t.status}
                        </Badge>
                      </TableCell>
                      <TableCell
                        className="max-w-65 truncate"
                        title={t.question ?? t.market_id}
                      >
                        {t.question ?? (
                          <Mono className="text-muted-foreground">
                            {t.market_id.slice(0, 12)}
                          </Mono>
                        )}
                      </TableCell>
                      <TableCell>
                        <OutcomeBadge outcome={t.outcome} />
                      </TableCell>
                      <TableCell className="text-right">
                        <Mono>{formatCurrency(t.amount)}</Mono>
                      </TableCell>
                      <TableCell className="text-right">
                        <PnlCell value={t.profit} />
                      </TableCell>
                      <TableCell>
                        <Mono className="text-muted-foreground">
                          {formatTimestamp(
                            t.abandoned_at ?? t.resolved_at ?? null,
                          )}
                        </Mono>
                      </TableCell>
                      <TableCell className="text-xs text-muted-foreground">
                        {t.abandoned_reason ?? "—"}
                      </TableCell>
                    </TableRow>
                  ))
                )}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      </section>

      <p className="text-[11px] text-muted-foreground">
        Abandon total already realised as loss: <Mono>${abandonedAmount.toFixed(2)}</Mono>.
        Approvals still flow via SSH (<code>scripts/execute_close.py</code> /{" "}
        <code>scripts/execute_abandon.py</code>) — a browser write surface
        lands in Phase 5.3.
      </p>
    </div>
  )
}

function ClosesRow({ proposal }: { proposal: PendingClose }) {
  return (
    <TableRow>
      <TableCell>
        <Mono>#{proposal.id}</Mono>
      </TableCell>
      <TableCell
        className="max-w-60 truncate"
        title={proposal.question ?? proposal.market_id}
      >
        {proposal.question ?? (
          <Mono className="text-muted-foreground">
            {proposal.market_id.slice(0, 12)}
          </Mono>
        )}
      </TableCell>
      <TableCell>
        <OutcomeBadge outcome={proposal.outcome ?? undefined} />
      </TableCell>
      <TableCell className="text-right">
        <PnlCell value={proposal.position_score ?? undefined} />
      </TableCell>
      <TableCell className="text-right">
        <PnlCell value={proposal.current_unrealised_pnl ?? undefined} />
      </TableCell>
      <TableCell className="text-xs text-muted-foreground">
        {proposal.reason ?? "—"}
      </TableCell>
      <TableCell>
        <Mono className="text-muted-foreground">
          {formatTimestamp(proposal.proposed_at)}
        </Mono>
      </TableCell>
      <TableCell>
        <code className="rounded bg-muted px-1.5 py-0.5 text-[10px]">
          approve close {proposal.id}
        </code>
      </TableCell>
    </TableRow>
  )
}

function AbandonsRow({ proposal }: { proposal: PendingAbandon }) {
  return (
    <TableRow>
      <TableCell>
        <Mono>#{proposal.id}</Mono>
      </TableCell>
      <TableCell
        className="max-w-60 truncate"
        title={proposal.question ?? proposal.market_id}
      >
        {proposal.question ?? (
          <Mono className="text-muted-foreground">
            {proposal.market_id.slice(0, 12)}
          </Mono>
        )}
      </TableCell>
      <TableCell className="text-right">
        <Mono className="text-loss">
          {formatCurrency(-(proposal.total_amount ?? 0))}
        </Mono>
      </TableCell>
      <TableCell className="text-xs text-muted-foreground">
        {proposal.reason ?? "—"}
      </TableCell>
      <TableCell>
        <Mono className="text-muted-foreground">
          {formatTimestamp(proposal.proposed_at)}
        </Mono>
      </TableCell>
      <TableCell>
        <code className="rounded bg-muted px-1.5 py-0.5 text-[10px]">
          approve abandon {proposal.id}
        </code>
      </TableCell>
    </TableRow>
  )
}

/**
 * Choose one market to chart: the OPEN position with the most-negative
 * unrealised P&L (most interesting signal). Falls back to the oldest
 * position if nothing is losing. Returns null when the book is empty.
 */
function pickChartCandidate(open: Trade[]): Trade | null {
  if (open.length === 0) return null
  const losing = open
    .filter((t) => typeof t.current_unrealised_pnl === "number")
    .sort(
      (a, b) =>
        (a.current_unrealised_pnl ?? 0) - (b.current_unrealised_pnl ?? 0),
    )
  if (losing.length > 0 && (losing[0].current_unrealised_pnl ?? 0) < 0) {
    return losing[0]
  }
  const aged = [...open].sort(
    (a, b) => Date.parse(a.timestamp) - Date.parse(b.timestamp),
  )
  return aged[0] ?? null
}
