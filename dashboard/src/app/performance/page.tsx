import { PageHeader } from "@/components/page-header"
import { ComingSoon } from "@/components/coming-soon"

export default function PerformancePage() {
  return (
    <div className="space-y-6">
      <PageHeader
        title="Performance"
        description="Equity curve, daily P&L, win rate, profit factor, best/worst markets, performance by category and by strategy. Realized vs unrealized returns."
      />
      <ComingSoon
        feature="Performance dashboard"
        description="Equity curve over time, daily P&L bars, win/loss distribution, top/bottom markets, slice-by-category and slice-by-strategy tables. Sharpe and max drawdown if we have enough trade history."
        dataSources={[
          "manifold_bot/paper_trading_state.json (trade_history)",
          "auto_trades.json",
          "Computed equity from trade history + initial balance",
        ]}
        buildOrder={6}
      />
    </div>
  )
}
