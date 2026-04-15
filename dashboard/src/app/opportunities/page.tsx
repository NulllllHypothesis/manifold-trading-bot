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
import { Mono, formatPercent, formatTimestamp } from "@/components/format"
import { readMarketResearch } from "@/lib/data"
import type { Recommendation } from "@/lib/schemas/research"
import { cn } from "@/lib/utils"

export const dynamic = "force-dynamic"

function DirectionBadge({ dir }: { dir: string | null | undefined }) {
  if (!dir) return <span className="text-muted-foreground">—</span>
  const up = dir.toUpperCase()
  const cls =
    up === "YES"
      ? "border-gain/40 text-gain"
      : up === "NO"
        ? "border-loss/40 text-loss"
        : ""
  return (
    <Badge variant="outline" className={cn("font-mono text-[10px]", cls)}>
      {up}
    </Badge>
  )
}

function AiVerdict({ r }: { r: Recommendation }) {
  if (r.ai_returned_skip) {
    return (
      <Badge variant="outline" className="text-xs text-muted-foreground">
        SKIP
      </Badge>
    )
  }
  if (r.ai_recommendation) {
    const agree =
      r.recommendation &&
      r.ai_recommendation.toUpperCase() === r.recommendation.toUpperCase()
    return (
      <Badge
        variant="outline"
        className={cn(
          "font-mono text-[10px]",
          agree ? "border-gain/40 text-gain" : "border-warn/40 text-warn",
        )}
      >
        {r.ai_recommendation.toUpperCase()}
        {r.ai_confidence != null ? ` ${Math.round(r.ai_confidence * 100)}%` : ""}
      </Badge>
    )
  }
  if (r.ai_was_candidate) {
    return (
      <Badge variant="outline" className="text-xs text-muted-foreground">
        no_result
      </Badge>
    )
  }
  return <span className="text-muted-foreground text-xs">—</span>
}

export default async function OpportunitiesPage() {
  const research = await readMarketResearch()
  const latest = research?.latest

  if (!latest) {
    return (
      <div className="space-y-6">
        <PageHeader
          title="Opportunities"
          description="market_research.json is missing or empty."
        />
      </div>
    )
  }

  const recs = [...latest.recommendations].sort((a, b) => {
    const ea = a.estimated_ev_exec ?? a.estimated_ev ?? -Infinity
    const eb = b.estimated_ev_exec ?? b.estimated_ev ?? -Infinity
    if (eb !== ea) return eb - ea
    return (b.confidence ?? 0) - (a.confidence ?? 0)
  })

  return (
    <div className="space-y-6">
      <PageHeader
        title="Opportunities"
        description="Live research recommendations, sorted by exec EV then confidence."
      />

      <Card>
        <CardContent className="p-4 text-sm text-muted-foreground">
          Snapshot: <Mono>{formatTimestamp(latest.timestamp)}</Mono> ·{" "}
          <Mono>{latest.recommendations.length}</Mono> recommendations ·{" "}
          schema <Mono>v{String(latest.schema_version ?? "?")}</Mono>
          {latest.total_markets_analyzed != null ? (
            <>
              {" · "}
              <Mono>{latest.total_markets_analyzed}</Mono> markets scanned
            </>
          ) : null}
        </CardContent>
      </Card>

      <Card>
        <CardContent className="p-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Question</TableHead>
                <TableHead>Category</TableHead>
                <TableHead className="text-right">Prob</TableHead>
                <TableHead>Pick</TableHead>
                <TableHead className="text-right">Conf</TableHead>
                <TableHead className="text-right">EV ref</TableHead>
                <TableHead className="text-right">EV exec</TableHead>
                <TableHead>AI</TableHead>
                <TableHead>Strategies</TableHead>
                <TableHead className="text-right">Vol 24h</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {recs.map((r) => (
                <TableRow key={r.market_id}>
                  <TableCell className="max-w-md">
                    <div className="truncate text-base" title={r.question}>
                      {r.existing_position ? (
                        <span className="mr-1.5 text-xs font-mono text-warn">
                          HELD
                        </span>
                      ) : null}
                      {r.question}
                    </div>
                  </TableCell>
                  <TableCell className="text-muted-foreground text-sm">
                    {r.category ?? "—"}
                  </TableCell>
                  <TableCell className="text-right">
                    <Mono>{formatPercent(r.probability, 0)}</Mono>
                  </TableCell>
                  <TableCell>
                    <DirectionBadge dir={r.recommendation} />
                  </TableCell>
                  <TableCell className="text-right">
                    <Mono>{formatPercent(r.confidence, 0)}</Mono>
                  </TableCell>
                  <TableCell className="text-right">
                    <Mono
                      className={cn(
                        (r.estimated_ev_ref ?? 0) > 0 && "text-gain",
                        (r.estimated_ev_ref ?? 0) < 0 && "text-loss",
                      )}
                    >
                      {r.estimated_ev_ref != null
                        ? r.estimated_ev_ref.toFixed(3)
                        : "—"}
                    </Mono>
                  </TableCell>
                  <TableCell className="text-right">
                    <Mono
                      className={cn(
                        (r.estimated_ev_exec ?? 0) > 0 && "text-gain",
                        (r.estimated_ev_exec ?? 0) < 0 && "text-loss",
                      )}
                    >
                      {r.estimated_ev_exec != null
                        ? r.estimated_ev_exec.toFixed(3)
                        : "—"}
                    </Mono>
                  </TableCell>
                  <TableCell>
                    <AiVerdict r={r} />
                  </TableCell>
                  <TableCell>
                    <div className="flex flex-wrap gap-1">
                      {(r.strategies ?? []).map((s) => (
                        <Badge
                          key={s}
                          variant="secondary"
                          className="text-xs font-mono"
                        >
                          {s}
                        </Badge>
                      ))}
                    </div>
                  </TableCell>
                  <TableCell className="text-right">
                    <Mono className="text-muted-foreground">
                      {r.volume24h != null ? Math.round(r.volume24h) : "—"}
                    </Mono>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  )
}
