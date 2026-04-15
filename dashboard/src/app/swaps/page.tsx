import { PageHeader } from "@/components/page-header"
import { Card, CardContent } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import {
  Mono,
  formatCurrency,
  formatPercent,
  formatTimestamp,
} from "@/components/format"
import { readPendingSwaps } from "@/lib/data"
import type { PendingSwap } from "@/lib/schemas/swaps"
import { cn } from "@/lib/utils"

export const dynamic = "force-dynamic"

function StatusBadge({ status }: { status: string }) {
  const s = status.toLowerCase()
  const cls =
    s === "pending"
      ? "border-warn/40 text-warn"
      : s === "approved" || s === "executed"
        ? "border-gain/40 text-gain"
        : s === "expired" || s === "dismissed" || s === "rejected"
          ? "border-muted-foreground/40 text-muted-foreground"
          : ""
  return (
    <Badge variant="outline" className={cn("text-xs font-mono", cls)}>
      {status.toUpperCase()}
    </Badge>
  )
}

function Side({
  label,
  title,
  market,
  lines,
  tone,
}: {
  label: string
  title: string
  market?: string
  lines: Array<{ k: string; v: React.ReactNode }>
  tone: "close" | "open"
}) {
  return (
    <div
      className={cn(
        "rounded-md border p-3 flex-1",
        tone === "close" ? "border-loss/30 bg-loss/5" : "border-gain/30 bg-gain/5",
      )}
    >
      <div className="flex items-center justify-between">
        <span className="text-xs uppercase tracking-wide text-muted-foreground">
          {label}
        </span>
        {market ? (
          <Mono className="text-xs text-muted-foreground">{market}</Mono>
        ) : null}
      </div>
      <div className="mt-1 text-base font-medium line-clamp-2" title={title}>
        {title || <span className="text-muted-foreground">—</span>}
      </div>
      <dl className="mt-2 space-y-1 text-sm">
        {lines.map((l) => (
          <div key={l.k} className="flex justify-between gap-4">
            <dt className="text-muted-foreground">{l.k}</dt>
            <dd>{l.v}</dd>
          </div>
        ))}
      </dl>
    </div>
  )
}

function SwapCard({ swap }: { swap: PendingSwap }) {
  const openRec = swap.open_recommendation
  const openEv =
    openRec?.estimated_ev_exec ?? openRec?.estimated_ev_ref ?? openRec?.estimated_ev
  const swapId = swap.id ?? swap.swap_id ?? "?"
  return (
    <Card>
      <CardContent className="p-4">
        <div className="mb-3 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Mono className="text-sm">#{String(swapId)}</Mono>
            <StatusBadge status={swap.status} />
          </div>
          <div className="text-sm text-muted-foreground">
            Proposed <Mono>{formatTimestamp(swap.proposed_at ?? null)}</Mono>
            {swap.expires_at ? (
              <>
                {" · expires "}
                <Mono>{formatTimestamp(swap.expires_at)}</Mono>
              </>
            ) : null}
          </div>
        </div>
        <div className="flex flex-col gap-3 md:flex-row">
          <Side
            label="Close"
            tone="close"
            title={swap.close_question ?? ""}
            market={swap.close_market_id}
            lines={[
              {
                k: "Side",
                v: <Mono>{swap.close_outcome ?? "—"}</Mono>,
              },
              {
                k: "Entry → current",
                v: (
                  <Mono>
                    {formatPercent(swap.close_entry_probability ?? null, 0)} →{" "}
                    {formatPercent(swap.close_current_probability ?? null, 0)}
                  </Mono>
                ),
              },
              {
                k: "Unrealised P&L",
                v: (
                  <Mono
                    className={cn(
                      (swap.close_unrealised_pnl ?? 0) < 0 && "text-loss",
                      (swap.close_unrealised_pnl ?? 0) > 0 && "text-gain",
                    )}
                  >
                    {formatCurrency(swap.close_unrealised_pnl ?? null, {
                      signed: true,
                    })}
                  </Mono>
                ),
              },
            ]}
          />
          <Side
            label="Open"
            tone="open"
            title={swap.open_question ?? openRec?.question ?? ""}
            market={swap.open_market_id ?? openRec?.market_id}
            lines={[
              {
                k: "Pick",
                v: <Mono>{openRec?.recommendation ?? "—"}</Mono>,
              },
              {
                k: "Confidence",
                v: <Mono>{formatPercent(openRec?.confidence ?? null, 0)}</Mono>,
              },
              {
                k: "EV",
                v: (
                  <Mono
                    className={cn(
                      (openEv ?? 0) > 0 && "text-gain",
                      (openEv ?? 0) < 0 && "text-loss",
                    )}
                  >
                    {openEv != null ? openEv.toFixed(3) : "—"}
                  </Mono>
                ),
              },
              ...(swap.open_strategies && swap.open_strategies.length > 0
                ? [
                    {
                      k: "Strategies",
                      v: (
                        <div className="flex flex-wrap gap-1 justify-end">
                          {swap.open_strategies.map((s) => (
                            <Badge
                              key={s}
                              variant="secondary"
                              className="text-xs font-mono"
                            >
                              {s}
                            </Badge>
                          ))}
                        </div>
                      ),
                    },
                  ]
                : []),
            ]}
          />
        </div>
        {swap.ev_advantage != null ? (
          <div className="mt-3 text-sm text-muted-foreground">
            EV advantage:{" "}
            <Mono className={cn(swap.ev_advantage > 0 && "text-gain")}>
              {swap.ev_advantage.toFixed(3)}
            </Mono>
          </div>
        ) : null}
      </CardContent>
    </Card>
  )
}

export default async function SwapsPage() {
  const swaps = await readPendingSwaps()
  const pending = swaps.filter((s) => s.status === "pending")
  const history = swaps
    .filter((s) => s.status !== "pending")
    .sort((a, b) =>
      (b.proposed_at ?? "").localeCompare(a.proposed_at ?? ""),
    )
    .slice(0, 20)

  return (
    <div className="space-y-6">
      <PageHeader
        title="Swaps"
        description="Position swap proposals — close a losing position to free a slot for a higher-EV opportunity."
      />

      <Card>
        <CardContent className="p-4 text-sm text-muted-foreground">
          <Mono>{pending.length}</Mono> pending · <Mono>{swaps.length}</Mono>{" "}
          total entries in <Mono>pending_swaps.json</Mono>
        </CardContent>
      </Card>

      <section className="space-y-3">
        <div className="text-xs uppercase tracking-wide text-muted-foreground">
          Pending ({pending.length})
        </div>
        {pending.length === 0 ? (
          <Card>
            <CardContent className="p-6 text-center text-base text-muted-foreground">
              No pending swap proposals.
            </CardContent>
          </Card>
        ) : (
          pending.map((s, i) => (
            <SwapCard key={String(s.id ?? s.swap_id ?? i)} swap={s} />
          ))
        )}
      </section>

      {history.length > 0 ? (
        <section className="space-y-3">
          <div className="text-xs uppercase tracking-wide text-muted-foreground">
            Recent history ({history.length})
          </div>
          {history.map((s, i) => (
            <SwapCard key={`h-${String(s.id ?? s.swap_id ?? i)}`} swap={s} />
          ))}
        </section>
      ) : null}
    </div>
  )
}
