import type { Metadata } from "next"
import { Geist, Geist_Mono } from "next/font/google"
import { Suspense } from "react"
import "./globals.css"

import { AppSidebar } from "@/components/app-sidebar"
import {
  GlobalStatusBar,
  getSidebarBadges,
} from "@/components/global-status-bar"
import {
  SidebarInset,
  SidebarProvider,
  SidebarTrigger,
} from "@/components/ui/sidebar"
import { Separator } from "@/components/ui/separator"
import { Skeleton } from "@/components/ui/skeleton"
import { TooltipProvider } from "@/components/ui/tooltip"
import { BOT_NAME } from "@/lib/config"

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
})

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
})

export const metadata: Metadata = {
  title: `${BOT_NAME} · Dashboard`,
  description: "Operator console for the Manifold paper trading bot.",
}

function StatusBarFallback() {
  return (
    <div className="flex h-11 items-center gap-2 border-b border-border px-3">
      <Skeleton className="h-4 w-16" />
      <Skeleton className="h-4 w-16" />
      <Skeleton className="h-4 w-16" />
    </div>
  )
}

export default async function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode
}>) {
  const badges = await getSidebarBadges().catch(() => ({
    pendingSwaps: 0,
    alerts: 0,
  }))

  return (
    <html
      lang="en"
      className={`${geistSans.variable} ${geistMono.variable} dark h-full antialiased`}
      suppressHydrationWarning
    >
      <body className="min-h-full bg-background text-foreground">
        <TooltipProvider delayDuration={100}>
          <SidebarProvider>
            <AppSidebar badges={badges} />
            <SidebarInset>
              <header className="sticky top-0 z-20 flex flex-col bg-background/80 backdrop-blur supports-backdrop-filter:bg-background/60">
                <div className="flex h-14 items-center gap-2 border-b border-border px-4">
                  <SidebarTrigger className="-ml-1" />
                  <Separator orientation="vertical" className="mx-1 h-5" />
                  <span className="text-sm uppercase tracking-wide text-muted-foreground">
                    {BOT_NAME}
                  </span>
                </div>
                <Suspense fallback={<StatusBarFallback />}>
                  <GlobalStatusBar />
                </Suspense>
              </header>
              <div className="flex-1 px-8 py-8">{children}</div>
            </SidebarInset>
          </SidebarProvider>
        </TooltipProvider>
      </body>
    </html>
  )
}
