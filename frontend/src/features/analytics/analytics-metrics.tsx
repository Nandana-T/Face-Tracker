import { AnimatePresence, motion } from "motion/react";
import type { LucideIcon } from "lucide-react";
import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

export type MetricScope = "global" | "filtered" | "live" | "diagnostic";

const SCOPE_TEXT: Record<MetricScope, string> = {
  global: "all time · ignores filters",
  filtered: "follows the filters",
  live: "right now · ignores dates",
  diagnostic: "diagnostic · not people",
};

interface Props {
  name: string;
  label: string;
  value: number | null;
  loading?: boolean;
  scope: MetricScope;
  hint: string;
  icon: LucideIcon;
  tone?: "default" | "warn";
}

/**
 * One analytics number. It says which scope it belongs to (so a global value is never mistaken for a
 * filtered one), shows an em dash instead of an invented number, and swaps the value with a fade -
 * never a count-up, which would suggest counts that never existed.
 */
export function MetricTile({ name, label, value, loading, scope, hint, icon: Icon, tone = "default" }: Props) {
  return (
    <Card className="flex flex-col gap-3 p-5" data-testid="analytics-metric" data-metric={name} data-scope={scope}>
      <div className="flex items-center justify-between gap-2">
        <span className="text-sm text-muted-foreground">{label}</span>
        <Icon className={cn("size-4", tone === "warn" ? "text-amber-400" : "text-primary")} aria-hidden />
      </div>
      <div className="min-h-9">
        {loading ? (
          <Skeleton className="h-8 w-16" data-testid="metric-skeleton" />
        ) : (
          <AnimatePresence mode="wait" initial={false}>
            <motion.div
              key={String(value)}
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.15 }}
              className="font-mono text-3xl font-semibold tabular-nums"
              data-testid="analytics-value"
            >
              {value ?? "—"}
            </motion.div>
          </AnimatePresence>
        )}
      </div>
      <div>
        <p className="text-xs text-muted-foreground">{hint}</p>
        <p className="mt-0.5 text-[10px] uppercase tracking-wide text-muted-foreground/80" data-testid="metric-scope">
          {SCOPE_TEXT[scope]}
        </p>
      </div>
    </Card>
  );
}
