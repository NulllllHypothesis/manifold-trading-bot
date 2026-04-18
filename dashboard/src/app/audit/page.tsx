import { PageHeader } from "@/components/page-header"
import { Card, CardContent } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Mono, formatTimestamp } from "@/components/format"
import { readAuditEvents } from "@/lib/data"
import { cn } from "@/lib/utils"
import {
  SearchIcon,
  BarChart3Icon,
  ArrowRightLeftIcon,
  CheckCircle2Icon,
  XCircleIcon,
  RepeatIcon,
  AlertTriangleIcon,
} from "lucide-react"
import type { AuditEventType } from "@/lib/data/audit"

export const dynamic = "force-dynamic"

const EVENT_CONFIG: Record<
  AuditEventType,
  {
    icon: typeof SearchIcon
    color: string
    badgeColor: string
    label: string
  }
> = {
  research_run: {
    icon: SearchIcon,
    color: "text-primary",
    badgeColor: "border-primary/40 text-primary",
    label: "Research",
  },
  trader_run: {
    icon: BarChart3Icon,
    color: "text-primary",
    badgeColor: "border-primary/40 text-primary",
    label: "Trader",
  },
  trade_executed: {
    icon: ArrowRightLeftIcon,
    color: "text-warn",
    badgeColor: "border-warn/40 text-warn",
    label: "Trade",
  },
  trade_resolved: {
    icon: CheckCircle2Icon,
    color: "text-gain",
    badgeColor: "border-gain/40 text-gain",
    label: "Resolved",
  },
  swap_proposed: {
    icon: RepeatIcon,
    color: "text-warn",
    badgeColor: "border-warn/40 text-warn",
    label: "Swap",
  },
  swap_executed: {
    icon: RepeatIcon,
    color: "text-gain",
    badgeColor: "border-gain/40 text-gain",
    label: "Swap OK",
  },
  swap_expired: {
    icon: XCircleIcon,
    color: "text-muted-foreground",
    badgeColor: "border-border text-muted-foreground",
    label: "Expired",
  },
}

export default async function AuditPage() {
  const events = await readAuditEvents(undefined, { limit: 100 })

  const typeCounts: Record<string, number> = {}
  for (const e of events) {
    typeCounts[e.type] = (typeCounts[e.type] ?? 0) + 1
  }

  const dateGroups = new Map<string, typeof events>()
  for (const e of events) {
    const date = e.timestamp.slice(0, 10) || "unknown"
    if (!dateGroups.has(date)) dateGroups.set(date, [])
    dateGroups.get(date)!.push(e)
  }

  return (
    <div className="space-y-6">
      <PageHeader
        title="Audit"
        description="Chronological feed of bot actions — research runs, trades, resolutions, swaps. Derived from existing data files (v1)."
      />

      <div className="flex flex-wrap gap-2">
        {Object.entries(typeCounts).map(([type, count]) => {
          const cfg = EVENT_CONFIG[type as AuditEventType]
          return (
            <Badge
              key={type}
              variant="outline"
              className={cn("font-mono text-xs", cfg?.badgeColor)}
            >
              {cfg?.label ?? type}: {count}
            </Badge>
          )
        })}
        <Badge variant="outline" className="font-mono text-xs">
          Total: {events.length}
        </Badge>
      </div>

      {events.length === 0 ? (
        <Card>
          <CardContent className="p-8 text-center text-sm text-muted-foreground">
            No events found. Data files may be missing.
          </CardContent>
        </Card>
      ) : (
        [...dateGroups.entries()].map(([date, dayEvents]) => (
          <Card key={date}>
            <CardContent className="p-0">
              <div className="border-b border-border/50 px-4 py-3 text-xs uppercase tracking-wide text-muted-foreground">
                {date} ({dayEvents.length} events)
              </div>
              <ul className="divide-y divide-border/50">
                {dayEvents.map((e, i) => {
                  const cfg = EVENT_CONFIG[e.type]
                  const Icon = cfg?.icon ?? AlertTriangleIcon
                  return (
                    <li
                      key={`${e.type}-${e.timestamp}-${i}`}
                      className="flex items-start gap-3 px-4 py-3"
                    >
                      <Icon
                        className={cn(
                          "mt-0.5 h-4 w-4 shrink-0",
                          cfg?.color ?? "text-muted-foreground",
                        )}
                      />
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2">
                          <Badge
                            variant="outline"
                            className={cn(
                              "text-[10px] font-mono shrink-0",
                              cfg?.badgeColor,
                            )}
                          >
                            {cfg?.label ?? e.type}
                          </Badge>
                          <span className="text-sm font-medium truncate">
                            {e.title}
                          </span>
                        </div>
                        {e.detail ? (
                          <div className="mt-0.5 text-xs text-muted-foreground truncate">
                            {e.detail}
                          </div>
                        ) : null}
                      </div>
                      <Mono className="shrink-0 text-xs text-muted-foreground">
                        {formatTimestamp(e.timestamp).slice(11, 19)}
                      </Mono>
                    </li>
                  )
                })}
              </ul>
            </CardContent>
          </Card>
        ))
      )}
    </div>
  )
}
