"use client"

import * as React from "react"
import Link from "next/link"
import { usePathname } from "next/navigation"

import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarRail,
} from "@/components/ui/sidebar"
import { Badge } from "@/components/ui/badge"
import { NAV_SECTIONS } from "@/lib/nav"
import { BOT_NAME } from "@/lib/config"
import { ActivityIcon } from "lucide-react"

export type SidebarBadges = Partial<Record<"pendingSwaps" | "alerts", number>>

export function AppSidebar({
  badges = {},
  ...props
}: React.ComponentProps<typeof Sidebar> & { badges?: SidebarBadges }) {
  const pathname = usePathname()

  return (
    <Sidebar collapsible="icon" {...props}>
      <SidebarHeader>
        <div className="flex items-center gap-2 px-2 py-2">
          <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-sidebar-primary text-sidebar-primary-foreground">
            <ActivityIcon className="h-4 w-4 shrink-0" />
          </div>
          <div className="flex flex-col text-sm leading-tight group-data-[collapsible=icon]:hidden">
            <span className="font-semibold">{BOT_NAME}</span>
            <span className="text-xs text-muted-foreground">
              Operator Dashboard
            </span>
          </div>
        </div>
      </SidebarHeader>

      <SidebarContent>
        {NAV_SECTIONS.map((section) => (
          <SidebarGroup key={section.label}>
            <SidebarGroupLabel>{section.label}</SidebarGroupLabel>
            <SidebarMenu>
              {section.items.map((item) => {
                const isActive =
                  pathname === item.href ||
                  pathname.startsWith(item.href + "/")
                const badgeCount = item.badgeKey ? badges[item.badgeKey] : undefined
                const Icon = item.icon
                return (
                  <SidebarMenuItem key={item.href}>
                    <SidebarMenuButton
                      asChild
                      isActive={isActive}
                      tooltip={item.title}
                    >
                      <Link href={item.href}>
                        <Icon />
                        <span>{item.title}</span>
                        {badgeCount !== undefined && badgeCount > 0 ? (
                          <Badge
                            variant="secondary"
                            className="ml-auto h-5 px-1.5 text-[10px] font-mono"
                          >
                            {badgeCount}
                          </Badge>
                        ) : null}
                      </Link>
                    </SidebarMenuButton>
                  </SidebarMenuItem>
                )
              })}
            </SidebarMenu>
          </SidebarGroup>
        ))}
      </SidebarContent>

      <SidebarFooter>
        <div className="px-3 py-2 text-[10px] uppercase tracking-wide text-muted-foreground group-data-[collapsible=icon]:hidden">
          v0.1 · paper trading
        </div>
      </SidebarFooter>
      <SidebarRail />
    </Sidebar>
  )
}
