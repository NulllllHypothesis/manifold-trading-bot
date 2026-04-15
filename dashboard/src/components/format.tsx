/**
 * Tiny formatting primitives — keep these in one place so the whole app
 * stays consistent (currency, dates, percentages, ages).
 */
import * as React from "react"
import { cn } from "@/lib/utils"

export function Mono({
  children,
  className,
  ...props
}: React.HTMLAttributes<HTMLSpanElement>) {
  return (
    <span className={cn("font-mono", className)} {...props}>
      {children}
    </span>
  )
}

export function formatBytes(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—"
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`
  return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GB`
}

export function formatAge(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—"
  if (seconds < 60) return `${seconds}s ago`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
  if (seconds < 86400) {
    const h = Math.floor(seconds / 3600)
    const m = Math.floor((seconds % 3600) / 60)
    return `${h}h ${m}m ago`
  }
  const d = Math.floor(seconds / 86400)
  return `${d}d ago`
}

export function formatCurrency(
  value: number | null | undefined,
  options: { signed?: boolean } = {},
): string {
  if (value === null || value === undefined) return "—"
  const sign = options.signed && value > 0 ? "+" : ""
  return `${sign}${value < 0 ? "-" : ""}$${Math.abs(value).toFixed(2)}`
}

export function formatPercent(
  value: number | null | undefined,
  digits = 0,
): string {
  if (value === null || value === undefined) return "—"
  return `${(value * 100).toFixed(digits)}%`
}

export function formatTimestamp(iso: string | null | undefined): string {
  if (!iso) return "—"
  try {
    const d = new Date(iso)
    return d.toISOString().replace("T", " ").slice(0, 19) + "Z"
  } catch {
    return iso
  }
}
