import { AnimatePresence, motion } from "motion/react";
import { cn } from "@/lib/utils";

export type StatusTone = "ok" | "warn" | "error" | "idle" | "loading";

const dot: Record<StatusTone, string> = {
  ok: "bg-success",
  warn: "bg-warning",
  error: "bg-destructive",
  idle: "bg-muted-foreground/60",
  loading: "bg-muted-foreground animate-pulse",
};

interface StatusIndicatorProps {
  label: string;
  /** Short state text, e.g. "Connected". Changes are animated. */
  value: string;
  tone: StatusTone;
  className?: string;
}

export function StatusIndicator({ label, value, tone, className }: StatusIndicatorProps) {
  return (
    <div
      className={cn("flex items-center gap-2 text-xs", className)}
      role="status"
      aria-label={label ? `${label}: ${value}` : value}
      data-tone={tone}
    >
      <span aria-hidden className={cn("size-2 rounded-full transition-colors", dot[tone])} />
      {label && <span className="text-muted-foreground">{label}</span>}
      <AnimatePresence mode="wait" initial={false}>
        <motion.span
          key={value}
          className="font-medium text-foreground"
          initial={{ opacity: 0, y: 2 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.15 }}
        >
          {value}
        </motion.span>
      </AnimatePresence>
    </div>
  );
}
