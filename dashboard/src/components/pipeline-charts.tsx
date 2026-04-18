"use client"

import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts"
import type { CounterSummary } from "@/lib/data/counters"

const BORDER = "var(--color-border)"
const MUTED = "var(--color-muted-foreground)"
const PRIMARY = "var(--color-primary)"
const GAIN = "var(--color-gain)"
const LOSS = "var(--color-loss)"
const WARN = "var(--color-warn)"

const AXIS_STYLE = {
  fontSize: 11,
  fill: "var(--color-muted-foreground)",
}

function ChartTooltip({
  active,
  payload,
  label,
}: {
  active?: boolean
  payload?: Array<{ value: number; name: string; color: string }>
  label?: string
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
            {p.value}
          </span>
        </div>
      ))}
    </div>
  )
}

const FUNNEL_COLORS = [
  "var(--color-primary)",
  "hsl(200 60% 50%)",
  "hsl(170 60% 45%)",
  "hsl(140 60% 45%)",
  "var(--color-gain)",
]

export function FunnelChart({ data }: { data: CounterSummary["funnel"] }) {
  if (data.length === 0) {
    return (
      <div className="flex h-48 items-center justify-center text-sm text-muted-foreground">
        No funnel data.
      </div>
    )
  }

  return (
    <div className="h-64 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart
          data={data}
          layout="vertical"
          margin={{ top: 4, right: 32, left: 0, bottom: 4 }}
        >
          <CartesianGrid stroke={BORDER} strokeDasharray="3 3" horizontal={false} />
          <XAxis
            type="number"
            tick={AXIS_STYLE}
            tickLine={false}
            axisLine={{ stroke: BORDER }}
          />
          <YAxis
            type="category"
            dataKey="label"
            width={140}
            tick={AXIS_STYLE}
            tickLine={false}
            axisLine={{ stroke: BORDER }}
          />
          <Tooltip
            content={<ChartTooltip />}
            cursor={{ fill: "var(--color-muted)", opacity: 0.2 }}
          />
          <Bar dataKey="value" name="Count" radius={[0, 4, 4, 0]}>
            {data.map((_, i) => (
              <Cell key={i} fill={FUNNEL_COLORS[i % FUNNEL_COLORS.length]} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}

type AiFlowData = CounterSummary["aiFlow"]

export function AiFlowChart({ data }: { data: AiFlowData }) {
  const chartData = [
    { label: "Eligible", value: data.eligible, color: MUTED },
    { label: "Cooled down", value: data.cooled, color: WARN },
    { label: "Analyzed", value: data.analyzed, color: PRIMARY },
    { label: "Agree", value: data.agree, color: GAIN },
    { label: "Disagree", value: data.disagree, color: LOSS },
    { label: "Skip", value: data.skip, color: MUTED },
    { label: "No result", value: data.noResult, color: LOSS },
  ]

  const total = data.eligible || 1
  const hasData = data.eligible > 0

  if (!hasData) {
    return (
      <div className="flex h-32 items-center justify-center text-sm text-muted-foreground">
        No AI calls in this window.
      </div>
    )
  }

  return (
    <div className="space-y-2">
      {chartData.map((item) => {
        const pct = (item.value / total) * 100
        return (
          <div key={item.label} className="flex items-center gap-3">
            <div className="w-24 shrink-0 text-sm text-muted-foreground">
              {item.label}
            </div>
            <div className="relative h-5 flex-1 overflow-hidden rounded bg-muted/40">
              <div
                className="h-full rounded transition-all"
                style={{ width: `${Math.max(pct, pct > 0 ? 2 : 0)}%`, background: item.color }}
              />
            </div>
            <div className="w-16 shrink-0 text-right font-mono text-xs tabular-nums">
              {item.value} <span className="text-muted-foreground">({pct.toFixed(0)}%)</span>
            </div>
          </div>
        )
      })}
    </div>
  )
}

export function RejectionChart({
  data,
}: {
  data: Array<{ reason: string; count: number }>
}) {
  if (data.length === 0) {
    return (
      <div className="flex h-24 items-center justify-center text-sm text-muted-foreground">
        No rejections in this window.
      </div>
    )
  }

  const max = Math.max(...data.map((d) => d.count), 1)

  return (
    <div className="space-y-1.5">
      {data.map((r) => {
        const pct = (r.count / max) * 100
        return (
          <div key={r.reason} className="flex items-center gap-3">
            <div className="w-36 shrink-0 truncate text-sm font-medium">
              {r.reason.replace(/_/g, " ")}
            </div>
            <div className="relative h-4 flex-1 overflow-hidden rounded bg-muted/40">
              <div
                className="h-full rounded bg-loss/40"
                style={{ width: `${pct}%` }}
              />
            </div>
            <div className="w-12 shrink-0 text-right font-mono text-xs">
              {r.count}
            </div>
          </div>
        )
      })}
    </div>
  )
}
