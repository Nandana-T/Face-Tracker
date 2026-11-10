import { motion } from "motion/react";
import { PanelLeftClose, PanelLeftOpen } from "lucide-react";
import { NavLink } from "react-router-dom";
import { NAV_ITEMS, type NavItem } from "@/app/navigation";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useUiStore } from "@/lib/ui-store";
import { cn } from "@/lib/utils";
import { Brand } from "./brand";

export function NavItemLink({
  item,
  collapsed,
  onNavigate,
}: {
  item: NavItem;
  collapsed: boolean;
  onNavigate?: () => void;
}) {
  const link = (
    <NavLink
      to={item.to}
      end={item.end}
      onClick={onNavigate}
      aria-label={collapsed ? item.title : undefined}
      className={({ isActive }) =>
        cn(
          "relative flex h-10 items-center gap-3 rounded-md px-3 text-sm font-medium transition-colors",
          isActive
            ? "text-foreground"
            : "text-muted-foreground hover:bg-muted/60 hover:text-foreground",
        )
      }
    >
      {({ isActive }) => (
        <>
          {isActive && (
            <motion.span
              layoutId="nav-active"
              className="absolute inset-0 rounded-md bg-primary/10 ring-1 ring-primary/30"
              transition={{ type: "spring", stiffness: 500, damping: 40 }}
            />
          )}
          <item.icon
            className={cn("relative size-4 shrink-0", isActive && "text-primary")}
            aria-hidden
          />
          {!collapsed && <span className="relative truncate">{item.title}</span>}
        </>
      )}
    </NavLink>
  );

  if (!collapsed) return link;
  return (
    <Tooltip>
      <TooltipTrigger asChild>{link}</TooltipTrigger>
      <TooltipContent side="right">{item.title}</TooltipContent>
    </Tooltip>
  );
}

export function Sidebar() {
  const collapsed = useUiStore((s) => s.sidebarCollapsed);
  const toggle = useUiStore((s) => s.toggleSidebar);

  return (
    <motion.aside
      initial={false}
      animate={{ width: collapsed ? 72 : 248 }}
      transition={{ duration: 0.2, ease: "easeOut" }}
      className="sticky top-0 hidden h-screen shrink-0 flex-col border-r bg-sidebar md:flex"
      aria-label="Primary"
      data-collapsed={collapsed}
    >
      <div className="flex h-16 items-center px-4">
        <Brand showText={!collapsed} />
      </div>
      <nav aria-label="Main navigation" className="flex flex-1 flex-col gap-1 px-3 py-2">
        {NAV_ITEMS.map((item) => (
          <NavItemLink key={item.to} item={item} collapsed={collapsed} />
        ))}
      </nav>
      <div className="border-t p-3">
        <Button
          variant="ghost"
          size={collapsed ? "icon" : "default"}
          className={cn(collapsed ? "w-full" : "w-full justify-start")}
          onClick={toggle}
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          aria-expanded={!collapsed}
        >
          {collapsed ? <PanelLeftOpen /> : <PanelLeftClose />}
          {!collapsed && <span>Collapse</span>}
        </Button>
      </div>
    </motion.aside>
  );
}
