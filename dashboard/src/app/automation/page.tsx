import { PageHeader } from "@/components/page-header"
import { ComingSoon } from "@/components/coming-soon"

export default function AutomationPage() {
  return (
    <div className="space-y-6">
      <PageHeader
        title="Automation"
        description="Cron schedule, last run time, next run time, duration, success/failure, stale outputs. The page that tells you whether the bot's heart is still beating."
      />
      <ComingSoon
        feature="Cron health board"
        description="Per-job: schedule, last run, next run, last duration, last exit code, output freshness, link to log excerpt. Banner alerts when any job is overdue."
        dataSources={[
          "OS crontab (via API endpoint that runs `crontab -l` on the sandbox)",
          "/tmp/research.log, /tmp/trader.log, /tmp/resolution.log (tail)",
          "data/research_counters.jsonl + data/trader_counters.jsonl (run history)",
        ]}
        buildOrder={7}
      />
    </div>
  )
}
