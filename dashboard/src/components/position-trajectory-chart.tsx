"use client"

import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts"
import type { PositionSnapshot } from "@/lib/schemas/position-management"
import { formatCurrency } from "@/components/format"

const GAIN = "var(--color-gain)"
const LOSS = "var(--color-loss)"
const PRIMARY = "var(--color-primary)"
const BORDER = "var(--color-border)"
const MUTED = "var(--color-muted-foreground)"

const AXIS_STYLE = { fontSize: 11, fill: MUTED }

/**
 * Unrealised-P&L-over-time chart, one point per repricing run.
 *
 * Input: the latest N position_snapshots rows for a specific market,
 * already sorted oldest-first by the loader. Zero and below-zero states
 * are colour-coded (gain/loss) via ReferenceLine at 0 so you can see at
 * a glance when a position crossed the waterline.
 */
export function PositionTrajectoryChart({
  snapshots,
}: {
  snapshots: PositionSnapshot[]
}) {
  if (snapshots.length < 2) {
    return (
      <div className="flex h-40 items-center justify-center text-xs text-muted-foreground">
        Not enough repricing history yet (need ≥2 snapshots).
      </div>
    )
  }

  const data = snapshots.map((s) => ({
    // "HH:mm MMM d" kept compact for dense X axis
    t: formatShortTime(s.snapshot_at),
    pnl: typeof s.unrealised_pnl === "number" ? s.unrealised_pnl : null,
  }))

  return (
    <ResponsiveContainer width="100%" height={180}>
      <LineChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
        <CartesianGrid stroke={BORDER} strokeDasharray="2 4" />
        <XAxis
          dataKey="t"
          tick={AXIS_STYLE}
          axisLine={{ stroke: BORDER }}
          tickLine={false}
          interval="preserveStartEnd"
          minTickGap={40}
        />
        <YAxis
          tick={AXIS_STYLE}
          axisLine={{ stroke: BORDER }}
          tickLine={false}
          tickFormatter={(v: number) => formatCurrency(v)}
          width={60}
        />
        <ReferenceLine y={0} stroke={MUTED} strokeDasharray="3 3" />
        <Tooltip
          content={({ active, payload, label }) => {
            if (!active || !payload || payload.length === 0) return null
            const v = payload[0]?.value as number | undefined
            const isGain = (v ?? 0) > 0
            return (
              <div className="rounded-md border border-border bg-popover px-3 py-2 text-xs shadow-md">
                <div className="mb-0.5 text-muted-foreground">{String(label)}</div>
                <div
                  className="font-mono tabular-nums"
                  style={{ color: v == null ? MUTED : isGain ? GAIN : LOSS }}
                >
                  {v == null ? "—" : formatCurrency(v)}
                </div>
              </div>
            )
          }}
        />
        <Line
          type="monotone"
          dataKey="pnl"
          stroke={PRIMARY}
          strokeWidth={1.5}
          dot={false}
          connectNulls={false}
          isAnimationActive={false}
        />
      </LineChart>
    </ResponsiveContainer>
  )
}

function formatShortTime(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const mm = String(d.getUTCMonth() + 1).padStart(2, "0")
  const dd = String(d.getUTCDate()).padStart(2, "0")
  const hh = String(d.getUTCHours()).padStart(2, "0")
  return `${mm}-${dd} ${hh}:00`
}
