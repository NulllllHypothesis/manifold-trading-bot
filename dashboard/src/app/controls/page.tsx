import { PageHeader } from "@/components/page-header"
import { ComingSoon } from "@/components/coming-soon"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { LockIcon } from "lucide-react"

export default function ControlsPage() {
  return (
    <div className="space-y-6">
      <PageHeader
        title="Controls"
        description="Risk settings, autotrader enable/disable, max positions, confidence floor, max bet size, liquidity floor, category caps, model/backend status."
      />
      <Alert>
        <LockIcon className="h-4 w-4" />
        <AlertTitle>Read-only in v1</AlertTitle>
        <AlertDescription>
          Mutating bot config from the dashboard requires a config service, auth,
          validation, and audit logging — deliberately deferred to v2. For now,
          this page will display the current values from{" "}
          <code className="font-mono text-xs">manifold_bot/config.py</code> as a
          read-only reference.
        </AlertDescription>
      </Alert>
      <ComingSoon
        feature="Configuration display (read-only)"
        description="Show current values: MAX_POSITIONS, MAX_BET_AMOUNT, MIN_CONFIDENCE, MAX_PER_CATEGORY, MIN_LIQUIDITY, AI cooldown threshold, news fetch on/off, and which AI backend is in use. Toggling these requires a code commit + deploy in v1."
        dataSources={[
          "manifold_bot/config.py (parsed via API endpoint)",
          "data/ai_timeout_cooldown.json (live AI backend health)",
        ]}
        buildOrder={10}
      />
    </div>
  )
}
