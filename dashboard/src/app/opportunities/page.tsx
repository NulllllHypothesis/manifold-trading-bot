import { PageHeader } from "@/components/page-header"
import { ComingSoon } from "@/components/coming-soon"
import { Card, CardContent } from "@/components/ui/card"
import { Mono, formatTimestamp } from "@/components/format"
import { readMarketResearch } from "@/lib/data"

export const dynamic = "force-dynamic"

export default async function OpportunitiesPage() {
  const research = await readMarketResearch()
  const latest = research?.latest

  return (
    <div className="space-y-6">
      <PageHeader
        title="Opportunities"
        description="Live research recommendations: what the bot is seeing right now, with strategy mix, AI verdict, EV, and rejection reasons."
      />
      {latest ? (
        <Card>
          <CardContent className="p-4 text-xs text-muted-foreground">
            Latest snapshot: <Mono>{formatTimestamp(latest.timestamp)}</Mono> ·{" "}
            <Mono>{latest.recommendations.length}</Mono> recommendations · schema{" "}
            <Mono>v{String(latest.schema_version ?? "?")}</Mono>
          </CardContent>
        </Card>
      ) : null}
      <ComingSoon
        feature="Live research board"
        description="Sortable table of recommendations with question, category, current probability, recommendation direction, stat confidence, AI verdict, EV (ref/exec), strategies fired, news context, and whether the trader is blocking it."
        dataSources={[
          "market_research.json (latest snapshot)",
          "manifold_bot/paper_trading_state.json (existing positions)",
          "data/trader_counters.jsonl (rejection reasons)",
        ]}
        buildOrder={3}
      />
    </div>
  )
}
