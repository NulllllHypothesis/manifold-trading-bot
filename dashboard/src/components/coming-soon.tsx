import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { ConstructionIcon } from "lucide-react"

export function ComingSoon({
  feature,
  description,
  dataSources,
  buildOrder,
}: {
  feature: string
  description: string
  /** What data files / DBs this page will read */
  dataSources?: string[]
  /** Where in the MVP order this falls (1-10) */
  buildOrder?: number
}) {
  return (
    <Card className="border-dashed">
      <CardHeader>
        <div className="flex items-start justify-between gap-3">
          <div className="space-y-1">
            <CardTitle className="flex items-center gap-2">
              <ConstructionIcon className="h-4 w-4 text-warn" />
              {feature}
            </CardTitle>
            <CardDescription>{description}</CardDescription>
          </div>
          {buildOrder !== undefined ? (
            <Badge variant="outline" className="font-mono">
              MVP #{buildOrder}
            </Badge>
          ) : null}
        </div>
      </CardHeader>
      {dataSources && dataSources.length > 0 ? (
        <CardContent>
          <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
            Will read from
          </div>
          <ul className="mt-2 space-y-1 text-sm">
            {dataSources.map((src) => (
              <li key={src} className="font-mono text-muted-foreground">
                {src}
              </li>
            ))}
          </ul>
        </CardContent>
      ) : null}
    </Card>
  )
}
