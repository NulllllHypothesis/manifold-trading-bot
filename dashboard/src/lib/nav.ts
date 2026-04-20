/**
 * Navigation config — drives the sidebar.
 * Two surfaces (Command Center, Management Console) per the architecture decision.
 */

import {
  ActivityIcon,
  AlertTriangleIcon,
  BarChart3Icon,
  BeakerIcon,
  BookOpenIcon,
  BrainIcon,
  BriefcaseIcon,
  ClockIcon,
  GaugeIcon,
  HeartPulseIcon,
  LayoutDashboardIcon,
  NewspaperIcon,
  RepeatIcon,
  SearchIcon,
  SettingsIcon,
  StethoscopeIcon,
  TrendingUpIcon,
  type LucideIcon,
} from "lucide-react"

export type NavItem = {
  title: string
  href: string
  icon: LucideIcon
  description?: string
  /** Optional badge — e.g., pending swap count */
  badgeKey?: "pendingSwaps" | "alerts"
}

export type NavSection = {
  label: string
  items: NavItem[]
}

export const NAV_SECTIONS: NavSection[] = [
  {
    label: "Command Center",
    items: [
      {
        title: "Overview",
        href: "/overview",
        icon: LayoutDashboardIcon,
        description: "Balance, alerts, today at a glance",
      },
      {
        title: "Opportunities",
        href: "/opportunities",
        icon: SearchIcon,
        description: "Live research recommendations",
      },
      {
        title: "Portfolio",
        href: "/portfolio",
        icon: BriefcaseIcon,
        description: "Open positions and current book",
      },
      {
        title: "Swaps",
        href: "/swaps",
        icon: RepeatIcon,
        description: "Position swap proposals",
        badgeKey: "pendingSwaps",
      },
      {
        title: "Positions",
        href: "/positions",
        icon: HeartPulseIcon,
        description: "Live book, close/abandon proposals, write-offs (Phase 2.5)",
      },
      {
        title: "Performance",
        href: "/performance",
        icon: TrendingUpIcon,
        description: "P&L, equity curve, win rates",
      },
    ],
  },
  {
    label: "Management Console",
    items: [
      {
        title: "Pipeline Health",
        href: "/pipeline",
        icon: ActivityIcon,
        description: "Research → execution funnel",
      },
      {
        title: "Automation",
        href: "/automation",
        icon: ClockIcon,
        description: "Cron jobs and freshness",
      },
      {
        title: "Strategy Lab",
        href: "/strategy-lab",
        icon: BeakerIcon,
        description: "Weights, contributions, accuracy",
      },
      {
        title: "Calibration",
        href: "/calibration",
        icon: GaugeIcon,
        description: "EV calibration and learning loops",
      },
      {
        title: "Learning Loop",
        href: "/learning",
        icon: BrainIcon,
        description: "Feedback loop health, weight adaptation, eval",
      },
      {
        title: "News Impact",
        href: "/news-impact",
        icon: NewspaperIcon,
        description: "Headlines, signal direction, trade attribution",
      },
      {
        title: "Controls",
        href: "/controls",
        icon: SettingsIcon,
        description: "Risk settings and config (read-only v1)",
      },
      {
        title: "Audit",
        href: "/audit",
        icon: BookOpenIcon,
        description: "Recent activity and events",
      },
      {
        title: "Diagnostics",
        href: "/diagnostics",
        icon: StethoscopeIcon,
        description: "File freshness, schemas, plumbing",
      },
    ],
  },
]

export const STATUS_INDICATORS = {
  botEnabled: { icon: HeartPulseIcon, label: "Bot" },
  research: { icon: SearchIcon, label: "Research" },
  trader: { icon: BarChart3Icon, label: "Trader" },
  swaps: { icon: RepeatIcon, label: "Swaps" },
  ai: { icon: BeakerIcon, label: "AI" },
  alerts: { icon: AlertTriangleIcon, label: "Alerts" },
} as const
