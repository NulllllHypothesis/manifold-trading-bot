import { PageHeader } from "@/components/page-header"
import { Card, CardContent } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Mono } from "@/components/format"
import { summarizeNewsImpact } from "@/lib/data"
import { cn } from "@/lib/utils"
import {
  NewspaperIcon,
  AlertTriangleIcon,
} from "lucide-react"

export const dynamic = "force-dynamic"

function DirectionBadge({
  dir,
  size = "default",
}: {
  dir: string | null
  size?: "default" | "sm"
}) {
  if (!dir) {
    return (
      <span className="text-muted-foreground text-xs">no signal</span>
    )
  }
  const cls =
    dir === "YES"
      ? "border-gain/40 text-gain"
      : dir === "NO"
        ? "border-loss/40 text-loss"
        : ""
  return (
    <Badge
      variant="outline"
      className={cn(
        "font-mono",
        size === "sm" ? "text-[10px]" : "text-xs",
        cls,
      )}
    >
      {dir}
    </Badge>
  )
}

function AgreementBadge({ agrees }: { agrees: boolean | null }) {
  if (agrees === null) {
    return (
      <span className="text-muted-foreground text-xs">—</span>
    )
  }
  return agrees ? (
    <Badge
      variant="outline"
      className="text-[10px] font-mono border-gain/40 text-gain"
    >
      agrees
    </Badge>
  ) : (
    <Badge
      variant="outline"
      className="text-[10px] font-mono border-loss/40 text-loss"
    >
      disagrees
    </Badge>
  )
}

export default async function NewsImpactPage() {
  const data = await summarizeNewsImpact()

  if (!data) {
    return (
      <div className="space-y-6">
        <PageHeader
          title="News Impact"
          description="data/news_cache.json is missing. News fetching may not be configured."
        />
        <Alert>
          <AlertTriangleIcon className="h-4 w-4" />
          <AlertTitle>No news data</AlertTitle>
          <AlertDescription>
            Set <Mono>NEWS_API_KEY</Mono> in <Mono>.env</Mono> to enable news
            fetching. The bot fetches headlines for the top 3 recommendation
            candidates each hour.
          </AlertDescription>
        </Alert>
      </div>
    )
  }

  const { markets, signals, fetchHealth } = data

  const agreeing = signals.filter((s) => s.agrees === true)
  const disagreeing = signals.filter((s) => s.agrees === false)

  return (
    <div className="space-y-6">
      <PageHeader
        title="News Impact"
        description="How news headlines are affecting recommendations, signal direction, and trade outcomes."
      />

      <div className="grid grid-cols-2 gap-4 md:grid-cols-5">
        <Card>
          <CardContent className="p-4 text-center">
            <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
              Markets with news
            </div>
            <div className="mt-1 font-mono text-2xl font-semibold">
              {fetchHealth.marketsWithArticles}
            </div>
            <div className="text-xs text-muted-foreground">
              of {fetchHealth.uniqueMarkets} fetched
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-4 text-center">
            <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
              Current recs with news
            </div>
            <div className="mt-1 font-mono text-2xl font-semibold">
              {data.recsWithNews}
            </div>
            <div className="text-xs text-muted-foreground">
              of {data.recsWithNews + data.recsWithoutNews}
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-4 text-center">
            <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
              News agrees
            </div>
            <div className="mt-1 font-mono text-2xl font-semibold text-gain">
              {agreeing.length}
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-4 text-center">
            <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
              News disagrees
            </div>
            <div className="mt-1 font-mono text-2xl font-semibold text-loss">
              {disagreeing.length}
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-4 text-center">
            <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
              Trades overlapping news
            </div>
            <div className="mt-1 font-mono text-2xl font-semibold">
              {data.tradesWithNews}
            </div>
            <div className="text-xs text-muted-foreground">
              of {data.tradesWithNews + data.tradesWithoutNews}
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Section 2: Signal Direction & Agreement */}
      {signals.length > 0 ? (
        <Card>
          <CardContent className="p-0">
            <div className="border-b border-border/50 px-4 py-3 text-xs uppercase tracking-wide text-muted-foreground">
              News signal vs recommendation ({signals.length} markets with
              headlines)
            </div>
            <table className="w-full text-sm">
              <thead className="bg-muted/30 text-xs uppercase tracking-wide text-muted-foreground">
                <tr>
                  <th className="px-4 py-2 text-left font-medium">Market</th>
                  <th className="px-4 py-2 text-center font-medium">
                    News dir
                  </th>
                  <th className="px-4 py-2 text-center font-medium">
                    Rec dir
                  </th>
                  <th className="px-4 py-2 text-center font-medium">
                    Agreement
                  </th>
                  <th className="px-4 py-2 text-right font-medium">
                    Recency
                  </th>
                  <th className="px-4 py-2 text-right font-medium">
                    Headlines
                  </th>
                  <th className="px-4 py-2 text-center font-medium">
                    Trade exists
                  </th>
                </tr>
              </thead>
              <tbody>
                {signals.map((s, idx) => {
                  const market = markets.find(
                    (m) => m.marketId === s.marketId,
                  )
                  return (
                    <tr
                      key={s.marketId}
                      className={cn(
                        "border-t border-border/50",
                        idx % 2 === 1 ? "bg-muted/10" : undefined,
                      )}
                    >
                      <td className="px-4 py-2.5 max-w-xs">
                        <div className="truncate" title={s.question ?? s.marketId}>
                          {s.question ?? (
                            <Mono className="text-muted-foreground">
                              {s.marketId}
                            </Mono>
                          )}
                        </div>
                      </td>
                      <td className="px-4 py-2.5 text-center">
                        <DirectionBadge dir={s.direction} size="sm" />
                        <div className="text-[10px] text-muted-foreground mt-0.5">
                          {s.yesLean}Y / {s.noLean}N
                        </div>
                      </td>
                      <td className="px-4 py-2.5 text-center">
                        <DirectionBadge dir={s.recDirection} size="sm" />
                      </td>
                      <td className="px-4 py-2.5 text-center">
                        <AgreementBadge agrees={s.agrees} />
                      </td>
                      <td className="px-4 py-2.5 text-right">
                        <Mono
                          className={cn(
                            "text-xs",
                            (s.confidence ?? 0) >= 0.8
                              ? "text-gain"
                              : (s.confidence ?? 0) >= 0.7
                                ? "text-warn"
                                : "text-muted-foreground",
                          )}
                        >
                          {s.confidence
                            ? `${Math.round(s.confidence * 100)}%`
                            : "—"}
                        </Mono>
                      </td>
                      <td className="px-4 py-2.5 text-right">
                        <Mono>{s.headlines.length}</Mono>
                      </td>
                      <td className="px-4 py-2.5 text-center">
                        {market?.becameTrade ? (
                          <Badge
                            variant="outline"
                            className={cn(
                              "text-[10px] font-mono",
                              market.tradeOutcome === "WIN"
                                ? "border-gain/40 text-gain"
                                : market.tradeOutcome === "LOSE"
                                  ? "border-loss/40 text-loss"
                                  : "border-border text-muted-foreground",
                            )}
                          >
                            {market.tradeOutcome ?? "OPEN"}
                          </Badge>
                        ) : (
                          <span className="text-muted-foreground text-xs">
                            no
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
      ) : null}

      {/* Section 1: Recent Headlines */}
      <Card>
        <CardContent className="p-0">
          <div className="border-b border-border/50 px-4 py-3 text-xs uppercase tracking-wide text-muted-foreground">
            Recent headlines by market ({markets.filter((m) => m.totalArticles > 0).length} markets)
          </div>
          {markets.filter((m) => m.totalArticles > 0).length === 0 ? (
            <div className="p-6 text-center text-sm text-muted-foreground">
              No headlines fetched yet.
            </div>
          ) : (
            <div className="divide-y divide-border/50">
              {markets
                .filter((m) => m.totalArticles > 0)
                .slice(0, 20)
                .map((m) => (
                  <div key={m.marketId} className="px-4 py-3">
                    <div className="flex items-start justify-between gap-3 mb-1.5">
                      <div className="min-w-0 flex-1">
                        <div className="text-sm font-medium truncate" title={m.question ?? m.marketId}>
                          {m.question ?? (
                            <Mono className="text-muted-foreground">
                              {m.marketId}
                            </Mono>
                          )}
                        </div>
                        <div className="flex items-center gap-2 mt-0.5 text-xs text-muted-foreground">
                          {m.category ? (
                            <span>{m.category}</span>
                          ) : null}
                          <span>
                            {m.totalArticles} article
                            {m.totalArticles === 1 ? "" : "s"}
                          </span>
                          {m.emptyFetches > 0 ? (
                            <span>
                              {m.emptyFetches} empty fetch
                              {m.emptyFetches === 1 ? "" : "es"}
                            </span>
                          ) : null}
                          <span>
                            fetched{" "}
                            {m.fetchDates.at(-1) ?? "?"}
                          </span>
                        </div>
                      </div>
                      <div className="flex items-center gap-2 shrink-0">
                        {m.usedInNewsContext ? (
                          <Badge
                            variant="outline"
                            className="text-[10px] font-mono border-primary/40 text-primary"
                            title={m.newsContextHeadlines.join(" | ")}
                          >
                            in AI prompt ({m.newsContextHeadlines.length})
                          </Badge>
                        ) : null}
                        {m.becameTrade ? (
                          <Badge
                            variant="outline"
                            className={cn(
                              "text-[10px] font-mono",
                              m.tradeOutcome === "WIN"
                                ? "border-gain/40 text-gain"
                                : m.tradeOutcome === "LOSE"
                                  ? "border-loss/40 text-loss"
                                  : "border-border",
                            )}
                          >
                            {m.tradeOutcome ?? "OPEN"}
                          </Badge>
                        ) : null}
                      </div>
                    </div>
                    {m.newsContextHeadlines.length > 0 ? (
                      <div className="ml-2 mb-1">
                        <div className="text-[10px] uppercase tracking-wide text-primary/70 mb-0.5">
                          Sent to AI as news_context
                        </div>
                        <ul className="space-y-0.5">
                          {m.newsContextHeadlines.map((h, i) => (
                            <li key={`ctx-${i}`} className="flex items-start gap-2 text-xs">
                              <span className="text-primary/60 shrink-0">→</span>
                              <span className="text-primary/80">{h}</span>
                            </li>
                          ))}
                        </ul>
                      </div>
                    ) : null}
                    <ul className="space-y-0.5 ml-2">
                      {m.articles.slice(0, 5).map((a, i) => (
                        <li
                          key={`${a.title.slice(0, 20)}-${i}`}
                          className="flex items-start gap-2 text-xs"
                        >
                          <NewspaperIcon className="h-3 w-3 mt-0.5 shrink-0 text-muted-foreground" />
                          <span className="text-muted-foreground truncate">
                            {a.title}
                          </span>
                          <span className="shrink-0 text-muted-foreground/60">
                            {a.source}
                          </span>
                        </li>
                      ))}
                      {m.articles.length > 5 ? (
                        <li className="text-xs text-muted-foreground ml-5">
                          +{m.articles.length - 5} more
                        </li>
                      ) : null}
                    </ul>
                  </div>
                ))}
            </div>
          )}
        </CardContent>
      </Card>

      {/* Section 4: Fetch Health */}
      <Card>
        <CardContent className="p-0">
          <div className="border-b border-border/50 px-4 py-3 text-xs uppercase tracking-wide text-muted-foreground">
            Fetch health
          </div>
          <table className="w-full text-sm">
            <tbody>
              <tr className="border-t border-border/50">
                <td className="px-4 py-2 text-muted-foreground">
                  News API
                </td>
                <td className="px-4 py-2 text-right">
                  <Badge
                    variant="outline"
                    className={cn(
                      "text-[10px] font-mono",
                      fetchHealth.newsApiEnabled
                        ? "border-gain/40 text-gain"
                        : "border-loss/40 text-loss",
                    )}
                  >
                    {fetchHealth.newsApiEnabled ? "enabled" : "disabled"}
                  </Badge>
                </td>
              </tr>
              <tr className="border-t border-border/50 bg-muted/10">
                <td className="px-4 py-2 text-muted-foreground">
                  Cache entries
                </td>
                <td className="px-4 py-2 text-right">
                  <Mono>{fetchHealth.totalCacheEntries}</Mono>
                  <span className="ml-1 text-xs text-muted-foreground">
                    ({fetchHealth.entriesWithArticles} with articles,{" "}
                    {fetchHealth.emptyEntries} empty)
                  </span>
                </td>
              </tr>
              <tr className="border-t border-border/50">
                <td className="px-4 py-2 text-muted-foreground">
                  Unique markets fetched
                </td>
                <td className="px-4 py-2 text-right">
                  <Mono>{fetchHealth.uniqueMarkets}</Mono>
                  <span className="ml-1 text-xs text-muted-foreground">
                    ({fetchHealth.marketsWithArticles} returned headlines)
                  </span>
                </td>
              </tr>
              <tr className="border-t border-border/50 bg-muted/10">
                <td className="px-4 py-2 text-muted-foreground">
                  Hit rate
                </td>
                <td className="px-4 py-2 text-right">
                  <Mono>
                    {fetchHealth.totalCacheEntries > 0
                      ? `${Math.round((fetchHealth.entriesWithArticles / fetchHealth.totalCacheEntries) * 100)}%`
                      : "—"}
                  </Mono>
                  <span className="ml-1 text-xs text-muted-foreground">
                    fetches that returned articles
                  </span>
                </td>
              </tr>
              <tr className="border-t border-border/50">
                <td className="px-4 py-2 text-muted-foreground">
                  Date range
                </td>
                <td className="px-4 py-2 text-right">
                  <Mono className="text-xs">
                    {fetchHealth.dateRange.earliest ?? "—"} →{" "}
                    {fetchHealth.dateRange.latest ?? "—"}
                  </Mono>
                </td>
              </tr>
              {fetchHealth.testEntries > 0 ? (
                <tr className="border-t border-border/50 bg-muted/10">
                  <td className="px-4 py-2 text-muted-foreground">
                    Test entries (excluded)
                  </td>
                  <td className="px-4 py-2 text-right">
                    <Mono className="text-muted-foreground">
                      {fetchHealth.testEntries}
                    </Mono>
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </CardContent>
      </Card>

      <Card>
        <CardContent className="p-4">
          <div className="text-xs text-muted-foreground">
            <span className="font-medium">Phase 1 limitations:</span> News
            confidence impact is inferred from headline direction matching, not
            from a persisted attribution record. The bot does not yet write{" "}
            <Mono>confidence_before_news</Mono> or{" "}
            <Mono>news_delta</Mono> fields, so we cannot show exact
            confidence deltas. Phase 2 will add attribution payloads to
            auto_research.py for causal tracking.
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
