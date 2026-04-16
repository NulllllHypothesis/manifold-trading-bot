"use client"

import {
  Line,
  CartesianGrid,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  ComposedChart,
  Bar,
  BarChart,
  Cell,
} from "recharts"

const BORDER = "var(--color-border)"
const MUTED = "var(--color-muted-foreground)"
const PRIMARY = "var(--color-primary)"
const GAIN = "var(--color-gain)"
const LOSS = "var(--color-loss)"

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
            {(p.value * 100).toFixed(1)}%
          </span>
        </div>
      ))}
    </div>
  )
}

type BucketData = {
  bucket_low: number
  bucket_high: number
  crowd_midpoint?: number
  actual_yes_rate: number
  sample_size: number
  bias?: number
  reliable?: boolean
}

export function CalibrationCurveChart({ data }: { data: BucketData[] }) {
  if (data.length === 0) {
    return (
      <div className="flex h-64 items-center justify-center text-sm text-muted-foreground">
        No calibration bucket data.
      </div>
    )
  }

  const chartData = data.map((b) => ({
    bucket: `${(b.bucket_low * 100).toFixed(0)}-${(b.bucket_high * 100).toFixed(0)}%`,
    crowdProb: b.crowd_midpoint ?? (b.bucket_low + b.bucket_high) / 2,
    actualRate: b.actual_yes_rate,
    samples: b.sample_size,
  }))

  return (
    <div className="h-72 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={chartData} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
          <CartesianGrid stroke={BORDER} strokeDasharray="3 3" />
          <XAxis
            dataKey="crowdProb"
            tick={AXIS_STYLE}
            tickLine={false}
            axisLine={{ stroke: BORDER }}
            tickFormatter={(v) => `${(v * 100).toFixed(0)}%`}
            label={{ value: "Crowd Prediction", position: "bottom", offset: -2, style: { fontSize: 11, fill: MUTED } }}
          />
          <YAxis
            tick={AXIS_STYLE}
            tickLine={false}
            axisLine={{ stroke: BORDER }}
            tickFormatter={(v) => `${(v * 100).toFixed(0)}%`}
            domain={[0, 1]}
            label={{ value: "Actual YES Rate", angle: -90, position: "insideLeft", offset: 10, style: { fontSize: 11, fill: MUTED } }}
          />
          <ReferenceLine
            segment={[{ x: 0, y: 0 }, { x: 1, y: 1 }]}
            stroke={MUTED}
            strokeDasharray="4 4"
            strokeWidth={1}
          />
          <Tooltip content={<ChartTooltip />} />
          <Line
            type="monotone"
            dataKey="actualRate"
            name="Actual YES rate"
            stroke={PRIMARY}
            strokeWidth={2}
            dot={{ fill: PRIMARY, r: 4 }}
            activeDot={{ r: 6 }}
          />
          <Line
            type="monotone"
            dataKey="crowdProb"
            name="Perfect calibration"
            stroke={MUTED}
            strokeWidth={1}
            strokeDasharray="4 4"
            dot={false}
          />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  )
}

export function BiasByBucketChart({ data }: { data: BucketData[] }) {
  if (data.length === 0) {
    return (
      <div className="flex h-48 items-center justify-center text-sm text-muted-foreground">
        No bias data.
      </div>
    )
  }

  const chartData = data.map((b) => ({
    bucket: `${(b.bucket_low * 100).toFixed(0)}-${(b.bucket_high * 100).toFixed(0)}%`,
    bias: b.bias ?? 0,
    samples: b.sample_size,
  }))

  return (
    <div className="h-56 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={chartData} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
          <CartesianGrid stroke={BORDER} strokeDasharray="3 3" vertical={false} />
          <XAxis
            dataKey="bucket"
            tick={AXIS_STYLE}
            tickLine={false}
            axisLine={{ stroke: BORDER }}
          />
          <YAxis
            tick={AXIS_STYLE}
            tickLine={false}
            axisLine={{ stroke: BORDER }}
            tickFormatter={(v) => `${(v * 100).toFixed(0)}%`}
            width={50}
          />
          <ReferenceLine y={0} stroke={MUTED} />
          <Tooltip
            content={({ active, payload, label }) => {
              if (!active || !payload || payload.length === 0) return null
              const bias = payload[0]!.value as number
              return (
                <div className="rounded-md border border-border bg-popover px-3 py-2 text-sm shadow-md">
                  <div className="mb-1 font-medium text-foreground">{label}</div>
                  <div className="font-mono tabular-nums">
                    Bias: {bias > 0 ? "+" : ""}
                    {(bias * 100).toFixed(1)}%
                  </div>
                </div>
              )
            }}
            cursor={{ fill: "var(--color-muted)", opacity: 0.2 }}
          />
          <Bar dataKey="bias" name="Crowd Bias" radius={[3, 3, 0, 0]}>
            {chartData.map((d, i) => (
              <Cell key={i} fill={d.bias >= 0 ? GAIN : LOSS} opacity={0.7} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}
