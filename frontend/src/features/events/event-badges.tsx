import { AlertTriangle, CheckCircle2, Clock } from "lucide-react";
import type { EventType, Integrity, PersistenceStatus, VisitState } from "@/lib/api-types";
import { cn } from "@/lib/utils";
import {
  EVENT_TYPE_LABEL,
  INTEGRITY_LABEL,
  PERSISTENCE_LABEL,
  VISIT_STATE_LABEL,
} from "@/lib/visits";

const TYPE_STYLE: Record<EventType, string> = {
  entry: "border-teal-500/40 bg-teal-500/10 text-teal-300",
  exit: "border-sky-500/40 bg-sky-500/10 text-sky-300",
  interrupted: "border-amber-500/40 bg-amber-500/10 text-amber-300",
};

const base = "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium";

export function TypeBadge({ type, label }: { type: EventType; label?: string }) {
  return (
    <span className={cn(base, TYPE_STYLE[type])} data-testid="event-type">
      {label ?? EVENT_TYPE_LABEL[type]}
    </span>
  );
}

export function PersistenceBadge({ status }: { status: PersistenceStatus }) {
  const Icon = status === "DELIVERED" ? CheckCircle2 : status === "PENDING" ? Clock : AlertTriangle;
  const tone =
    status === "DELIVERED"
      ? "border-border text-muted-foreground"
      : status === "PENDING"
        ? "border-sky-500/40 text-sky-300"
        : "border-red-500/50 bg-red-500/10 text-red-300";
  return (
    <span className={cn(base, tone)} data-testid="event-persistence" data-status={status}>
      <Icon className="size-3" aria-hidden />
      {PERSISTENCE_LABEL[status]}
    </span>
  );
}

export function IntegrityBadge({ integrity }: { integrity: Integrity }) {
  const good = integrity === "complete" || integrity === "open";
  return (
    <span
      className={cn(base, good ? "border-border text-muted-foreground" : "border-amber-500/50 bg-amber-500/10 text-amber-300")}
      data-testid="visit-integrity"
      data-integrity={integrity}
    >
      {good ? <CheckCircle2 className="size-3" aria-hidden /> : <AlertTriangle className="size-3" aria-hidden />}
      {INTEGRITY_LABEL[integrity]}
    </span>
  );
}

export function StateBadge({ state }: { state: VisitState }) {
  const open = state === "ACTIVE" || state === "TEMPORARILY_MISSING" || state === "EXIT_PENDING";
  return (
    <span
      className={cn(base, open ? "border-teal-500/40 text-teal-300" : state === "INTERRUPTED" ? "border-amber-500/40 text-amber-300" : "border-border text-muted-foreground")}
      data-testid="visit-state"
      data-state={state}
    >
      {VISIT_STATE_LABEL[state]}
    </span>
  );
}
