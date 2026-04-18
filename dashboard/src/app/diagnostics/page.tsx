import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Separator } from "@/components/ui/separator"
import { PageHeader } from "@/components/page-header"
import {
  Mono,
  formatAge,
  formatBytes,
  formatTimestamp,
} from "@/components/format"
import { readDiagnostics } from "@/lib/data"
import { cn } from "@/lib/utils"

export const dynamic = "force-dynamic"

export default async function DiagnosticsPage() {
  const diag = await readDiagnostics()

  const counts = {
    total: diag.files.length,
    missing: diag.files.filter((f) => !f.exists).length,
    stale: diag.files.filter((f) => f.exists && f.isStale).length,
    fresh: diag.files.filter((f) => f.exists && !f.isStale).length,
  }

  return (
    <div className="space-y-6">
      <PageHeader
        title="Diagnostics"
        description="File freshness, schema versions, and the operational plumbing the rest of the dashboard depends on. If something looks off here, every other page lies to you."
      />

      {/* Summary cards */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <SummaryCard label="Total files" value={counts.total} tone="muted" />
        <SummaryCard
          label="Fresh"
          value={counts.fresh}
          tone={counts.fresh > 0 ? "ok" : "muted"}
        />
        <SummaryCard
          label="Stale"
          value={counts.stale}
          tone={counts.stale > 0 ? "warn" : "muted"}
        />
        <SummaryCard
          label="Missing"
          value={counts.missing}
          tone={counts.missing > 0 ? "error" : "muted"}
        />
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Data file freshness</CardTitle>
          <CardDescription>
            Workspace root: <Mono>{diag.workspaceRoot}</Mono> · Generated{" "}
            <Mono>{formatTimestamp(diag.generatedAt)}</Mono>
          </CardDescription>
        </CardHeader>
        <CardContent className="p-0">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-muted/30 text-xs uppercase tracking-wide text-muted-foreground">
                <tr>
                  <th className="px-4 py-2 text-left font-medium">Status</th>
                  <th className="px-4 py-2 text-left font-medium">Key</th>
                  <th className="px-4 py-2 text-left font-medium">Path</th>
                  <th className="px-4 py-2 text-right font-medium">Size</th>
                  <th className="px-4 py-2 text-right font-medium">Age</th>
                  <th className="px-4 py-2 text-left font-medium">Modified</th>
                </tr>
              </thead>
              <tbody>
                {diag.files.map((f, idx) => {
                  const status = !f.exists
                    ? { label: "missing", variant: "destructive" as const }
                    : f.isStale
                      ? { label: "stale", variant: "outline" as const }
                      : { label: "fresh", variant: "secondary" as const }
                  return (
                    <tr
                      key={f.key}
                      className={cn(
                        "border-t border-border/50 transition-colors",
                        idx % 2 === 1 ? "bg-muted/10" : undefined,
                      )}
                    >
                      <td className="px-4 py-2.5">
                        <Badge
                          variant={status.variant}
                          className={cn(
                            "uppercase",
                            status.label === "fresh" &&
                              "border-gain/30 bg-gain/15 text-gain",
                            status.label === "stale" &&
                              "border-warn/40 bg-warn/15 text-warn",
                          )}
                        >
                          {status.label}
                        </Badge>
                      </td>
                      <td className="px-4 py-2.5 font-medium">{f.key}</td>
                      <td className="px-4 py-2.5">
                        <Mono className="text-xs text-muted-foreground">
                          {f.path.replace(diag.workspaceRoot, ".")}
                        </Mono>
                      </td>
                      <td className="px-4 py-2.5 text-right">
                        <Mono className="text-xs">
                          {formatBytes(f.sizeBytes)}
                        </Mono>
                      </td>
                      <td className="px-4 py-2.5 text-right">
                        <Mono
                          className={cn(
                            "text-xs",
                            f.isStale && "text-warn",
                            !f.exists && "text-loss",
                          )}
                        >
                          {formatAge(f.ageSeconds)}
                        </Mono>
                      </td>
                      <td className="px-4 py-2.5">
                        <Mono className="text-xs text-muted-foreground">
                          {formatTimestamp(f.mtime)}
                        </Mono>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Cron schedule</CardTitle>
          <CardDescription>
            Source: <Mono>CLAUDE.md</Mono> — the OS crontab is authoritative on the
            sandbox, this is the documented expected schedule.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-muted/30 text-xs uppercase tracking-wide text-muted-foreground">
                <tr>
                  <th className="px-4 py-2 text-left font-medium">Job</th>
                  <th className="px-4 py-2 text-left font-medium">Schedule</th>
                  <th className="px-4 py-2 text-left font-medium">Description</th>
                </tr>
              </thead>
              <tbody>
                {diag.cronJobs.map((job, idx) => (
                  <tr
                    key={job.id}
                    className={cn(
                      "border-t border-border/50",
                      idx % 2 === 1 ? "bg-muted/10" : undefined,
                    )}
                  >
                    <td className="px-4 py-2.5 font-medium">{job.id}</td>
                    <td className="px-4 py-2.5">
                      <Mono className="text-xs">{job.schedule}</Mono>
                    </td>
                    <td className="px-4 py-2.5 text-muted-foreground">
                      {job.description}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </CardContent>
      </Card>

      <Separator />

      <p className="text-xs text-muted-foreground">
        This page is the proof-of-life for the data adapter layer. If a file is
        missing or stale here, the corresponding tab elsewhere in the dashboard
        will show empty or stale data — start your debugging here.
      </p>
    </div>
  )
}

function SummaryCard({
  label,
  value,
  tone,
}: {
  label: string
  value: number
  tone: "ok" | "warn" | "error" | "muted"
}) {
  const color =
    tone === "ok"
      ? "text-gain"
      : tone === "warn"
        ? "text-warn"
        : tone === "error"
          ? "text-loss"
          : "text-foreground"
  return (
    <Card>
      <CardContent className="p-4">
        <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
          {label}
        </div>
        <div
          className={cn(
            "mt-1 font-mono text-2xl font-semibold tabular-nums",
            color,
          )}
        >
          {value}
        </div>
      </CardContent>
    </Card>
  )
}
