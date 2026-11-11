import { motion } from "motion/react";
import type { Visitor } from "@/lib/api-types";
import { cn } from "@/lib/utils";
import { formatDateTime, formatRelative } from "./format";
import { VisitorImage } from "./visitor-image";

interface Props {
  visitor: Visitor;
  index: number;
  selected: boolean;
  onOpen: (visitor: Visitor) => void;
}

export function VisitorCard({ visitor, index, selected, onOpen }: Props) {
  return (
    <motion.li
      layout="position"
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0 }}
      // a short stagger, capped so a full page never takes noticeable time
      transition={{ duration: 0.22, delay: Math.min(index, 8) * 0.03 }}
    >
      <button
        type="button"
        onClick={() => onOpen(visitor)}
        aria-label={`Open visitor ${visitor.label}`}
        aria-pressed={selected}
        data-testid={`visitor-card-${visitor.number}`}
        className={cn(
          "group flex w-full flex-col overflow-hidden rounded-lg border bg-card text-left shadow-[var(--shadow-card)] transition-colors hover:border-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary",
          selected && "border-primary ring-1 ring-primary",
        )}
      >
        <VisitorImage
          visitorId={visitor.id}
          label={visitor.label}
          hasImage={visitor.has_image}
          className="aspect-square w-full"
        />
        <div className="space-y-1 p-3">
          <div className="flex items-center justify-between gap-2">
            <span className="font-mono text-sm font-semibold" data-testid="visitor-label">
              {visitor.label}
            </span>
            <span className="rounded bg-teal-500/15 px-1.5 py-0.5 text-[10px] font-medium text-teal-700 dark:text-teal-300">
              {visitor.status === "REGISTERED" ? "Registered" : visitor.status}
            </span>
          </div>
          <dl className="space-y-0.5 text-xs text-muted-foreground">
            <div className="flex justify-between gap-2">
              <dt>First seen</dt>
              <dd title={formatDateTime(visitor.first_seen_at)} data-testid="visitor-first">
                {formatRelative(visitor.first_seen_at)}
              </dd>
            </div>
            <div className="flex justify-between gap-2">
              <dt>Last seen</dt>
              <dd title={formatDateTime(visitor.last_seen_at)} data-testid="visitor-last">
                {formatRelative(visitor.last_seen_at)}
              </dd>
            </div>
          </dl>
        </div>
      </button>
    </motion.li>
  );
}
