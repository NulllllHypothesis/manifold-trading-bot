import { PageHeader } from "@/components/page-header"
import { ComingSoon } from "@/components/coming-soon"
import { Card, CardContent } from "@/components/ui/card"
import { Mono, formatCurrency } from "@/components/format"
import { summarizePortfolio } from "@/lib/data"

export const dynamic = "force-dynamic"

export default async function PortfolioPage() {
  const portfolio = await summarizePortfolio()

  return (
    <div className="space-y-6">
      <PageHeader
        title="Portfolio"
        description="The current book — open positions grouped by category, sortable by risk, with entry vs current probability, unrealized P&L, days open, strategy tags, and likely resolution timing."
      />
      {portfolio ? (
        <Card>
          <CardContent className="p-4 text-xs text-muted-foreground">
            <Mono>{portfolio.openPositionCount}</Mono> open ·{" "}
            <Mono>{formatCurrency(portfolio.openExposure)}</Mono> exposure ·{" "}
            <Mono>{portfolio.recentResolved.length}</Mono> recent resolutions
          </CardContent>
        </Card>
      ) : null}
      <ComingSoon
        feature="Open positions table"
        description="Per-position view with entry price vs current market price, unrealized P&L, days open, stake size, strategy tags, AI confidence, expected resolution window. Click a row to open the Market Detail Drawer (full lifecycle, AI trace, comparable resolved markets)."
        dataSources={[
          "manifold_bot/paper_trading_state.json",
          "Live Manifold market data (via API or sync)",
        ]}
        buildOrder={4}
      />
    </div>
  )
}
