import { PageHeader } from "@/components/page-header"
import { Card, CardContent } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Mono, formatAge, formatBytes, formatTimestamp } from "@/components/format"
import { readAutomationStatus } from "@/lib/data"
import { cn } from "@/lib/utils"
import {
  CheckCircle2Icon,
  AlertTriangleIcon,
  XCircleIcon,
  HelpCircleIcon,
  ClockIcon,
} from "lucide-react"

export const dynamic = "force-dynamic"

const HEALTH_CONFIG = {
  healthy: { icon: CheckCircle2Icon, color: "text-gain", label: "Healthy" },
  stale: { icon: AlertTriangleIcon, color: "text-warn", label: "Stale" },
  overdue: { icon: XCircleIcon, color: "text-loss", label: "Overdue" },
  unknown: { icon: HelpCircleIcon, color: "text-muted-foreground", label: "Unknown" },
} as const

export default async function AutomationPage() {
  const jobs = await readAutomationStatus()

  const healthyCt = jobs.filter((j) => j.health === "healthy").length
  const overdueCt = jobs.filter((j) => j.health === "overdue").length

  return (
    <div className="space-y-6">
      <PageHeader
        title="Automation"
        description="Cron schedule, output freshness, and job health. The page that tells you whether the bot's heart is still beating."
      />

      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <Card>
          <CardContent className="p-5">
            <div className="text-sm uppercase tracking-wide text-muted-foreground">
              Total jobs
            </div>
            <div className="mt-1.5 font-mono text-3xl font-semibold tabular-nums">
              {jobs.length}
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-5">
            <div className="text-sm uppercase tracking-wide text-muted-foreground">
              Healthy
            </div>
            <div className="mt-1.5 font-mono text-3xl font-semibold tabular-nums text-gain">
              {healthyCt}
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-5">
            <div className="text-sm uppercase tracking-wide text-muted-foreground">
              Overdue
            </div>
            <div className={cn(
              "mt-1.5 font-mono text-3xl font-semibold tabular-nums",
              overdueCt > 0 ? "text-loss" : "text-muted-foreground",
            )}>
              {overdueCt}
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-5">
            <div className="text-sm uppercase tracking-wide text-muted-foreground">
              Unknown
            </div>
            <div className="mt-1.5 font-mono text-3xl font-semibold tabular-nums text-muted-foreground">
              {jobs.filter((j) => j.health === "unknown").length}
            </div>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardContent className="p-0">
          <div className="border-b border-border/50 px-4 py-3 text-xs uppercase tracking-wide text-muted-foreground">
            Cron job schedule
          </div>
          <table className="w-full text-sm">
            <thead className="bg-muted/30 text-xs uppercase tracking-wide text-muted-foreground">
              <tr>
                <th className="px-4 py-2 text-left font-medium">Status</th>
                <th className="px-4 py-2 text-left font-medium">Job</th>
                <th className="px-4 py-2 text-left font-medium">Schedule</th>
                <th className="px-4 py-2 text-left font-medium">Next run</th>
                <th className="px-4 py-2 text-left font-medium">Output freshness</th>
              </tr>
            </thead>
            <tbody>
              {jobs.map((job, idx) => {
                const cfg = HEALTH_CONFIG[job.health]
                const Icon = cfg.icon
                return (
                  <tr
                    key={job.id}
                    className={cn(
                      "border-t border-border/50",
                      idx % 2 === 1 ? "bg-muted/10" : undefined,
                    )}
                  >
                    <td className="px-4 py-2.5">
                      <div className="flex items-center gap-1.5">
                        <Icon className={cn("h-4 w-4", cfg.color)} />
                        <Badge
                          variant="outline"
                          className={cn(
                            "text-[10px] font-mono",
                            job.health === "healthy" && "border-gain/40 text-gain",
                            job.health === "overdue" && "border-loss/40 text-loss",
                            job.health === "stale" && "border-warn/40 text-warn",
                          )}
                        >
                          {cfg.label}
                        </Badge>
                      </div>
                    </td>
                    <td className="px-4 py-2.5">
                      <div className="font-medium">{job.description}</div>
                      <div className="text-xs text-muted-foreground">
                        <Mono>{job.id}</Mono>
                      </div>
                    </td>
                    <td className="px-4 py-2.5">
                      <Mono className="text-xs">{job.schedule}</Mono>
                      <div className="text-xs text-muted-foreground">
                        {job.intervalDescription}
                      </div>
                    </td>
                    <td className="px-4 py-2.5">
                      {job.nextExpectedRun ? (
                        <Mono className="text-xs">
                          {formatTimestamp(job.nextExpectedRun).slice(11, 19)}
                        </Mono>
                      ) : (
                        <span className="text-muted-foreground">—</span>
                      )}
                    </td>
                    <td className="px-4 py-2.5">
                      {job.outputFiles.length > 0 ? (
                        <div className="space-y-0.5">
                          {job.outputFiles.map((f) => (
                            <div
                              key={f.key}
                              className="flex items-center gap-2 text-xs"
                            >
                              <Mono className="text-muted-foreground">
                                {f.key}
                              </Mono>
                              {f.exists ? (
                                <>
                                  <span className="text-muted-foreground">
                                    {formatAge(f.ageSeconds)}
                                  </span>
                                  <span className="text-muted-foreground">
                                    ({formatBytes(f.sizeBytes)})
                                  </span>
                                </>
                              ) : (
                                <span className="text-loss">missing</span>
                              )}
                            </div>
                          ))}
                        </div>
                      ) : job.lastRunTimestamp ? (
                        <Mono className="text-xs text-muted-foreground">
                          {formatAge(
                            Math.floor(
                              (Date.now() - Date.parse(job.lastRunTimestamp)) /
                                1000,
                            ),
                          )}
                        </Mono>
                      ) : (
                        <span className="text-xs text-muted-foreground">
                          no tracked outputs
                        </span>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </CardContent>
      </Card>

      <Card>
        <CardContent className="p-4">
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <ClockIcon className="h-3.5 w-3.5" />
            All jobs run via OS crontab on the sandbox server. OpenClaw cron is disabled.
            Jobs pull from <Mono>main</Mono> before each run.
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
