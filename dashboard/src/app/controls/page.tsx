import { PageHeader } from "@/components/page-header"
import { Card, CardContent } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Mono, formatAge, formatTimestamp } from "@/components/format"
import { readControlsSnapshot } from "@/lib/data"
import { cn } from "@/lib/utils"
import { LockIcon, ShieldIcon, SettingsIcon, ZapIcon, AlertTriangleIcon } from "lucide-react"

export const dynamic = "force-dynamic"

const GROUP_CONFIG = {
  risk: { label: "Risk Controls", icon: ShieldIcon, color: "text-loss" },
  trading: { label: "Trading Settings", icon: SettingsIcon, color: "text-primary" },
  api: { label: "API Configuration", icon: ZapIcon, color: "text-muted-foreground" },
  features: { label: "Feature Flags", icon: ZapIcon, color: "text-warn" },
} as const

export default async function ControlsPage() {
  const snapshot = await readControlsSnapshot()

  const groups = new Map<string, typeof snapshot.config>()
  for (const entry of snapshot.config) {
    if (!groups.has(entry.group)) groups.set(entry.group, [])
    groups.get(entry.group)!.push(entry)
  }

  return (
    <div className="space-y-6">
      <PageHeader
        title="Controls"
        description="Risk settings, confidence floor, position limits, and feature flags. Read-only reference parsed from config.py."
      />

      <Alert>
        <LockIcon className="h-4 w-4" />
        <AlertTitle>Read-only in v1</AlertTitle>
        <AlertDescription>
          Changing these values requires editing{" "}
          <Mono>manifold_bot/config.py</Mono> and deploying. Write access is
          deferred to v2 (needs auth + validation + audit logging).
          {snapshot.configFileAge !== null ? (
            <span className="ml-1 text-muted-foreground">
              Config file last modified {formatAge(snapshot.configFileAge)}.
            </span>
          ) : null}
        </AlertDescription>
      </Alert>

      {snapshot.config.length === 0 ? (
        <Card>
          <CardContent className="p-8 text-center text-sm text-muted-foreground">
            Could not read manifold_bot/config.py. File may be missing.
          </CardContent>
        </Card>
      ) : (
        [...groups.entries()].map(([group, entries]) => {
          const cfg = GROUP_CONFIG[group as keyof typeof GROUP_CONFIG]
          const Icon = cfg?.icon ?? SettingsIcon
          return (
            <Card key={group}>
              <CardContent className="p-0">
                <div className="flex items-center gap-2 border-b border-border/50 px-4 py-3">
                  <Icon className={cn("h-4 w-4", cfg?.color)} />
                  <span className="text-xs uppercase tracking-wide text-muted-foreground">
                    {cfg?.label ?? group}
                  </span>
                </div>
                <table className="w-full text-sm">
                  <tbody>
                    {entries.map((entry, idx) => (
                      <tr
                        key={entry.key}
                        className={cn(
                          "border-t border-border/50",
                          idx % 2 === 1 ? "bg-muted/10" : undefined,
                        )}
                      >
                        <td className="px-4 py-2.5 w-56">
                          <Mono className="text-xs font-medium">
                            {entry.key}
                          </Mono>
                        </td>
                        <td className="px-4 py-2.5">
                          <Mono className="text-primary text-sm">
                            {entry.value}
                          </Mono>
                        </td>
                        <td className="px-4 py-2.5 text-xs text-muted-foreground">
                          {entry.description}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </CardContent>
            </Card>
          )
        })
      )}

      {snapshot.aiCooldowns.length > 0 ? (
        <Card>
          <CardContent className="p-0">
            <div className="flex items-center gap-2 border-b border-border/50 px-4 py-3">
              <AlertTriangleIcon className="h-4 w-4 text-warn" />
              <span className="text-xs uppercase tracking-wide text-muted-foreground">
                AI Cooldown (markets with repeated AI failures)
              </span>
              <Badge variant="outline" className="ml-auto font-mono text-xs border-warn/40 text-warn">
                {snapshot.aiCooldowns.length} cooled
              </Badge>
            </div>
            <table className="w-full text-sm">
              <thead className="bg-muted/30 text-xs uppercase tracking-wide text-muted-foreground">
                <tr>
                  <th className="px-4 py-2 text-left font-medium">Market ID</th>
                  <th className="px-4 py-2 text-right font-medium">Fails</th>
                  <th className="px-4 py-2 text-left font-medium">Last failure</th>
                </tr>
              </thead>
              <tbody>
                {snapshot.aiCooldowns.map((c, idx) => (
                  <tr
                    key={c.marketId}
                    className={cn(
                      "border-t border-border/50",
                      idx % 2 === 1 ? "bg-muted/10" : undefined,
                    )}
                  >
                    <td className="px-4 py-2.5">
                      <Mono className="text-xs">{c.marketId}</Mono>
                    </td>
                    <td className="px-4 py-2.5 text-right">
                      <Mono className="text-loss">{c.fails}</Mono>
                    </td>
                    <td className="px-4 py-2.5 text-xs text-muted-foreground">
                      {formatTimestamp(c.lastFailAt)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </CardContent>
        </Card>
      ) : null}
    </div>
  )
}
