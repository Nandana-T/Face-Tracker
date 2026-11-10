import { motion } from "motion/react";
import type { LucideIcon } from "lucide-react";
import { Card } from "@/components/ui/card";

interface MetricCardProps {
  label: string;
  /** `null` renders an em dash: no data exists yet and none is invented. */
  value: string | number | null;
  hint: string;
  icon: LucideIcon;
  index?: number;
}

export function MetricCard({ label, value, hint, icon: Icon, index = 0 }: MetricCardProps) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.25, delay: index * 0.05 }}
    >
      <Card className="flex flex-col gap-4 p-6">
        <div className="flex items-center justify-between">
          <span className="text-sm text-muted-foreground">{label}</span>
          <Icon className="size-4 text-primary" aria-hidden />
        </div>
        <div>
          <div className="font-mono text-3xl font-semibold tabular-nums" data-testid="metric-value">
            {value ?? "—"}
          </div>
          <p className="mt-1 text-xs text-muted-foreground">{hint}</p>
        </div>
      </Card>
    </motion.div>
  );
}
