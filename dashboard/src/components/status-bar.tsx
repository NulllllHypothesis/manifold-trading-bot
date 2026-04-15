import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"
import { STATUS_INDICATORS } from "@/lib/nav"

export type StatusLevel = "ok" | "warn" | "error" | "idle"

export type StatusItem = {
  key: keyof typeof STATUS_INDICATORS
  level: StatusLevel
  /** Short value shown next to the label, e.g. "12m ago" or "8/10" */
  value?: string
  /** Detail tooltip text */
  detail?: string
}

const LEVEL_DOT: Record<StatusLevel, string> = {
  ok: "bg-gain",
  warn: "bg-warn",
  error: "bg-loss",
  idle: "bg-muted-foreground",
}

const LEVEL_RING: Record<StatusLevel, string> = {
  ok: "ring-gain/30",
  warn: "ring-warn/30",
  error: "ring-loss/30",
  idle: "ring-muted-foreground/20",
}

export function StatusBar({ items }: { items: StatusItem[] }) {
  return (
    <div
      data-tabular
      className="flex h-11 items-center gap-0 overflow-x-auto border-b border-border bg-background/60 px-3 backdrop-blur"
    >
      {items.map((item, idx) => {
        const meta = STATUS_INDICATORS[item.key]
        const Icon = meta.icon
        return (
          <Tooltip key={item.key}>
            <TooltipTrigger asChild>
              <button
                type="button"
                className={cn(
                  "group flex items-center gap-2 rounded-md px-2.5 py-1.5 text-sm text-muted-foreground transition-colors hover:bg-accent hover:text-foreground",
                  idx > 0 && "ml-2 border-l border-border/60 pl-4",
                )}
              >
                <span
                  className={cn(
                    "h-2 w-2 shrink-0 rounded-full ring-2 ring-offset-0",
                    LEVEL_DOT[item.level],
                    LEVEL_RING[item.level],
                  )}
                  aria-hidden
                />
                <Icon className="h-4 w-4" />
                <span className="font-medium">{meta.label}</span>
                {item.value ? (
                  <span className="font-mono text-foreground/80">
                    {item.value}
                  </span>
                ) : null}
              </button>
            </TooltipTrigger>
            {item.detail ? (
              <TooltipContent side="bottom">{item.detail}</TooltipContent>
            ) : null}
          </Tooltip>
        )
      })}
    </div>
  )
}
