"use client"

import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts"
import { cn } from "@/lib/utils"
import type {
  DailyPnl,
  EquityPoint,
  SliceStat,
} from "@/lib/data/performance"

const GAIN = "var(--color-gain)"
const LOSS = "var(--color-loss)"
const MUTED = "var(--color-muted-foreground)"
const PRIMARY = "var(--color-primary)"
const BORDER = "var(--color-border)"

const AXIS_STYLE = {
  fontSize: 11,
  fill: "var(--color-muted-foreground)",
}

function ChartTooltip({
  active,
  payload,
  label,
  formatValue,
}: {
  active?: boolean
  payload?: Array<{ value: number; name: string; color: string }>
  label?: string
  formatValue?: (v: number) => string
}) {
  if (!active || !payload || payload.length === 0) return null
  return (
    <div className="rounded-md border border-border bg-popover px-3 py-2 text-sm shadow-md">
      <div className="mb-1 font-medium text-foreground">{label}</div>
      {payload.map((p, i) => (
        <div key={i} className="flex items-center justify-between gap-3">
          <span className="flex items-center gap-1.5 text-muted-foreground">
            <span
              className="h-2 w-2 rounded-full"
              style={{ background: p.color }}
            />
            {p.name}
          </span>
          <span className="font-mono tabular-nums text-foreground">
            {formatValue ? formatValue(p.value) : p.value}
          </span>
        </div>
      ))}
    </div>
  )
}

const fmtUsd = (v: number) =>
  `${v < 0 ? "-" : ""}$${Math.abs(v).toFixed(2)}`

export function EquityCurveChart({ data }: { data: EquityPoint[] }) {
  if (data.length === 0) {
    return (
      <div className="flex h-72 items-center justify-center text-sm text-muted-foreground">
        No resolved trades yet.
      </div>
    )
  }
  return (
    <div className="h-72 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
          <CartesianGrid stroke={BORDER} strokeDasharray="3 3" vertical={false} />
          <XAxis
            dataKey="date"
            tick={AXIS_STYLE}
            tickLine={false}
            axisLine={{ stroke: BORDER }}
            minTickGap={24}
          />
          <YAxis
            tick={AXIS_STYLE}
            tickLine={false}
            axisLine={{ stroke: BORDER }}
            tickFormatter={(v) => `$${v}`}
            width={60}
          />
          <ReferenceLine y={0} stroke={MUTED} strokeDasharray="2 4" />
          <Tooltip
            content={<ChartTooltip formatValue={fmtUsd} />}
            cursor={{ stroke: MUTED, strokeDasharray: "2 4" }}
          />
          <Line
            type="monotone"
            dataKey="cumPnl"
            name="Cumulative P&L"
            stroke={PRIMARY}
            strokeWidth={2}
            dot={false}
            activeDot={{ r: 4 }}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}

export function DailyPnlChart({ data }: { data: DailyPnl[] }) {
  if (data.length === 0) {
    return (
      <div className="flex h-64 items-center justify-center text-sm text-muted-foreground">
        No daily data yet.
      </div>
    )
  }
  return (
    <div className="h-64 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
          <CartesianGrid stroke={BORDER} strokeDasharray="3 3" vertical={false} />
          <XAxis
            dataKey="date"
            tick={AXIS_STYLE}
            tickLine={false}
            axisLine={{ stroke: BORDER }}
            minTickGap={24}
          />
          <YAxis
            tick={AXIS_STYLE}
            tickLine={false}
            axisLine={{ stroke: BORDER }}
            tickFormatter={(v) => `$${v}`}
            width={60}
          />
          <ReferenceLine y={0} stroke={MUTED} />
          <Tooltip
            content={<ChartTooltip formatValue={fmtUsd} />}
            cursor={{ fill: "var(--color-muted)", opacity: 0.3 }}
          />
          <Bar dataKey="profit" name="Daily P&L" radius={[3, 3, 0, 0]}>
            {data.map((d, i) => (
              <Cell key={i} fill={d.profit >= 0 ? GAIN : LOSS} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}

export function SliceBarChart({
  data,
  emptyLabel = "No data.",
}: {
  data: SliceStat[]
  emptyLabel?: string
}) {
  if (data.length === 0) {
    return (
      <div className="flex h-48 items-center justify-center text-sm text-muted-foreground">
        {emptyLabel}
      </div>
    )
  }
  const max = Math.max(...data.map((d) => Math.abs(d.pnl)), 1)
  return (
    <ul className="divide-y divide-border">
      {data.map((d) => {
        const pct = (Math.abs(d.pnl) / max) * 100
        const positive = d.pnl >= 0
        return (
          <li
            key={d.key}
            className="flex items-center gap-3 px-4 py-2.5 text-sm"
          >
            <div className="w-32 shrink-0 truncate" title={d.key}>
              {d.key}
            </div>
            <div className="relative h-4 flex-1 overflow-hidden rounded bg-muted/40">
              <div
                className={cn(
                  "absolute top-0 h-full rounded",
                  positive ? "bg-gain/60" : "bg-loss/60",
                )}
                style={{
                  width: `${pct}%`,
                  left: positive ? "50%" : `${50 - pct / 2}%`,
                  marginLeft: positive ? 0 : undefined,
                }}
              />
              <div className="absolute left-1/2 top-0 h-full w-px bg-muted-foreground/30" />
            </div>
            <div
              className={cn(
                "w-20 shrink-0 text-right font-mono tabular-nums",
                positive ? "text-gain" : "text-loss",
              )}
            >
              {fmtUsd(d.pnl)}
            </div>
            <div className="w-20 shrink-0 text-right text-xs text-muted-foreground font-mono">
              {d.trades}t · {Math.round(d.winRate * 100)}%
            </div>
          </li>
        )
      })}
    </ul>
  )
}
