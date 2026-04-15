import { PageHeader } from "@/components/page-header"
import { ComingSoon } from "@/components/coming-soon"
import { Card, CardContent } from "@/components/ui/card"
import { Mono } from "@/components/format"
import { readPendingSwaps } from "@/lib/data"

export const dynamic = "force-dynamic"

export default async function SwapsPage() {
  const swaps = await readPendingSwaps()
  const pending = swaps.filter((s) => s.status === "pending")

  return (
    <div className="space-y-6">
      <PageHeader
        title="Swaps"
        description="Position swap proposals — close a losing position to free a slot for a higher-EV opportunity. Approve/dismiss inline."
      />
      <Card>
        <CardContent className="p-4 text-xs text-muted-foreground">
          <Mono>{pending.length}</Mono> pending · <Mono>{swaps.length}</Mono>{" "}
          total entries in <Mono>pending_swaps.json</Mono>
        </CardContent>
      </Card>
      <ComingSoon
        feature="Swap proposals board"
        description="Side-by-side cards: losing position (current loss, market context) vs proposed replacement (open-side EV, strategies, confidence). One-click approve (calls execute_swap.py via API) or dismiss. Plus a swap history log: approved / dismissed / expired."
        dataSources={[
          "pending_swaps.json",
          "scripts/position_swap_checker.py (via subprocess for actions)",
        ]}
        buildOrder={5}
      />
    </div>
  )
}
