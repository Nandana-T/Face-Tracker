import {
  BarChart3,
  LayoutDashboard,
  ListChecks,
  Settings,
  Users,
  Video,
  type LucideIcon,
} from "lucide-react";

export interface NavItem {
  to: string;
  title: string;
  icon: LucideIcon;
  end?: boolean;
}

export const NAV_ITEMS: readonly NavItem[] = [
  { to: "/", title: "Overview", icon: LayoutDashboard, end: true },
  { to: "/live", title: "Live Monitoring", icon: Video },
  { to: "/visitors", title: "Visitors", icon: Users },
  { to: "/events", title: "Event Logs", icon: ListChecks },
  { to: "/analytics", title: "Analytics", icon: BarChart3 },
  { to: "/settings", title: "Settings", icon: Settings },
];

export function titleForPath(pathname: string): string {
  const match = NAV_ITEMS.find((i) => (i.end ? pathname === i.to : pathname.startsWith(i.to)));
  return match?.title ?? "Not found";
}
