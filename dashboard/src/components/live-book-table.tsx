"use client"

import { useState } from "react"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Badge } from "@/components/ui/badge"
import {
  Mono,
  formatCurrency,
  formatPercent,
} from "@/components/format"
import { PositionTrajectoryChart } from "@/components/position-trajectory-chart"
import type { Trade } from "@/lib/schemas/portfolio"
import type { PositionSnapshot } from "@/lib/schemas/position-management"
import { cn } from "@/lib/utils"
import { ChevronDownIcon, ChevronRightIcon } from "lucide-react"

// These two helpers are small and duplicated with page.tsx so we don't
// invent a new shared module just for a row-expansion UI. If either
// grows, extract to components/positions-ui.tsx.
const REPRICE_STALE_HOURS = 2

type OpenRow = Trade & {
  /** Flag from the page — true when an active pending_closes proposal targets this market. */
  hasActiveCloseProposal: boolean
  /** Normalised class (matches what the backend enforces against). */
  normalisedPositionClass: string | undefined
  /** True if the on-disk class is one of the obsolete 4-bucket values. */
  isObsoleteClass: boolean
}

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

function PositionClassBadge({
  normalisedClass,
  rawClass,
  obsolete,
}: {
  normalisedClass: string | undefined
  rawClass: string | undefined
  obsolete: boolean
}) {
  if (!rawClass && !normalisedClass) {
    return (
      <Badge variant="outline" className="text-[10px] text-muted-foreground">
        UNCLASSIFIED
      </Badge>
    )
  }
  const cls = normalisedClass ?? rawClass ?? ""
  const color =
    cls === "short"
      ? "border-gain/40 text-gain"
      : cls === "medium"
      ? "border-primary/50 text-primary"
      : cls === "long_or_uncertain"
      ? "border-loss/40 text-loss"
      : "border-border text-muted-foreground"
  return (
    <span className="flex flex-wrap items-center gap-1">
      <Badge variant="outline" className={cn("font-mono text-[10px]", color)}>
        {cls.toUpperCase()}
      </Badge>
      {obsolete && rawClass && (
        <Badge
          variant="outline"
          className="border-loss/40 font-mono text-[9px] text-loss"
          title={`Persisted as ${rawClass}; backend normalises to ${normalisedClass ?? rawClass}`}
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

const COLUMN_COUNT = 9

export function LiveBookTable({
  rows,
  trajectories,
}: {
  rows: OpenRow[]
  /** market_id → already-aggregated snapshot array (per-run, oldest-first). */
  trajectories: Record<string, PositionSnapshot[]>
}) {
  // One selection at a time — second click on the same row collapses,
  // click on a different row swaps. Same market_id can appear on multiple
  // legs of the same row dataset but close_position_early closes them all
  // together, so the trajectory key is the market_id.
  const [expanded, setExpanded] = useState<string | null>(null)

  const toggle = (marketId: string) =>
    setExpanded((prev) => (prev === marketId ? null : marketId))

  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead className="w-6" aria-label="Expand" />
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
        {rows.length === 0 ? (
          <TableRow>
            <TableCell colSpan={COLUMN_COUNT + 1} className="text-muted-foreground">
              No open positions.
            </TableCell>
          </TableRow>
        ) : (
          rows.map((t) => {
            const entry = t.entry_probability ?? t.probability
            const age = daysSince(t.timestamp)
            const isExpanded = expanded === t.market_id
            const snapshots = trajectories[t.market_id] ?? []
            return (
              <RowFragment
                key={`${t.market_id}-${t.trade_id}`}
                row={t}
                entry={entry ?? null}
                age={age}
                isExpanded={isExpanded}
                snapshots={snapshots}
                onToggle={() => toggle(t.market_id)}
              />
            )
          })
        )}
      </TableBody>
    </Table>
  )
}

function RowFragment({
  row,
  entry,
  age,
  isExpanded,
  snapshots,
  onToggle,
}: {
  row: OpenRow
  entry: number | null
  age: number | null
  isExpanded: boolean
  snapshots: PositionSnapshot[]
  onToggle: () => void
}) {
  const Arrow = isExpanded ? ChevronDownIcon : ChevronRightIcon
  return (
    <>
      <TableRow
        className={cn(
          "cursor-pointer hover:bg-muted/40",
          isExpanded && "bg-muted/30",
        )}
        onClick={onToggle}
        aria-expanded={isExpanded}
        aria-label={`Toggle trajectory for ${row.question ?? row.market_id}`}
      >
        <TableCell className="pr-0">
          <Arrow className="h-3.5 w-3.5 text-muted-foreground" />
        </TableCell>
        <TableCell className="max-w-65 truncate" title={row.question ?? row.market_id}>
          {row.question ?? (
            <Mono className="text-muted-foreground">
              {row.market_id.slice(0, 12)}
            </Mono>
          )}
        </TableCell>
        <TableCell>
          <OutcomeBadge outcome={row.outcome} />
        </TableCell>
        <TableCell>
          <PositionClassBadge
            normalisedClass={row.normalisedPositionClass}
            rawClass={row.position_class}
            obsolete={row.isObsoleteClass}
          />
        </TableCell>
        <TableCell className="text-right">
          <Mono>{formatCurrency(row.amount)}</Mono>
        </TableCell>
        <TableCell className="text-right">
          <Mono className="text-muted-foreground">
            {entry != null ? formatPercent(entry) : "—"}
          </Mono>{" "}
          →{" "}
          <Mono>
            {row.current_probability != null
              ? formatPercent(row.current_probability)
              : "—"}
          </Mono>
        </TableCell>
        <TableCell className="text-right">
          <PnlCell value={row.current_unrealised_pnl} />
        </TableCell>
        <TableCell className="text-right">
          <Mono>{age != null ? `${age.toFixed(1)}d` : "—"}</Mono>
        </TableCell>
        <TableCell>
          <RepriceFreshnessPill lastReprice={row.last_repriced_at} />
        </TableCell>
        <TableCell>
          {row.hasActiveCloseProposal ? (
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
      {isExpanded && (
        <TableRow className="bg-muted/20">
          <TableCell />
          <TableCell colSpan={COLUMN_COUNT}>
            <div className="py-2">
              <div className="mb-2 text-[11px] text-muted-foreground">
                Unrealised P&amp;L trajectory · {snapshots.length} snapshot
                {snapshots.length === 1 ? "" : "s"}
              </div>
              <PositionTrajectoryChart snapshots={snapshots} />
            </div>
          </TableCell>
        </TableRow>
      )}
    </>
  )
}
