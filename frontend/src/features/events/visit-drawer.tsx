import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, X } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { Dialog as DialogPrimitive } from "radix-ui";
import { Link } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { DialogClose } from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { api } from "@/lib/api";
import type { VisitDetailRecord, VisitEventRecord } from "@/lib/api-types";
import {
  CLOSURE_REASON_LABEL,
  EXIT_BASIS_HELP,
  EXIT_BASIS_LABEL,
  IMAGE_KIND_LABEL,
  IMAGE_STATUS_LABEL,
  formatDuration,
  visitorText,
} from "@/lib/visits";
import { formatDateTimeSeconds } from "@/features/visitors/format";
import { IntegrityBadge, PersistenceBadge, StateBadge, TypeBadge } from "./event-badges";
import { EvidenceImage } from "./evidence-image";

interface Props {
  visitId: string | null;
  onClose: () => void;
}

/** One visit: who, when it started and ended (and on what basis), evidence images and data integrity. */
export function VisitDrawer({ visitId, onClose }: Props) {
  const open = visitId !== null;
  return (
    <DialogPrimitive.Root open={open} onOpenChange={(v) => !v && onClose()}>
      <AnimatePresence>
        {open ? (
          <DialogPrimitive.Portal forceMount>
            <DialogPrimitive.Overlay asChild forceMount>
              <motion.div
                className="fixed inset-0 z-40 bg-black/50"
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                transition={{ duration: 0.18 }}
              />
            </DialogPrimitive.Overlay>
            <DialogPrimitive.Content asChild forceMount aria-describedby="visit-drawer-desc">
              <motion.aside
                className="fixed inset-y-0 right-0 z-50 flex w-full max-w-lg flex-col overflow-y-auto border-l bg-card shadow-xl focus:outline-none"
                initial={{ x: "100%" }}
                animate={{ x: 0 }}
                exit={{ x: "100%" }}
                transition={{ type: "tween", duration: 0.24 }}
                data-testid="visit-drawer"
              >
                <DrawerBody visitId={visitId} />
              </motion.aside>
            </DialogPrimitive.Content>
          </DialogPrimitive.Portal>
        ) : null}
      </AnimatePresence>
    </DialogPrimitive.Root>
  );
}

function DrawerBody({ visitId }: { visitId: string }) {
  const detail = useQuery({
    queryKey: ["visit", visitId],
    queryFn: ({ signal }) => api.visit(visitId, signal),
    retry: false,
    refetchInterval: (q) => (q.state.data && q.state.data.integrity !== "complete" ? 5_000 : false),
  });
  const v = detail.data;
  return (
    <>
      <header className="flex items-start justify-between gap-3 border-b p-4">
        <div className="min-w-0">
          <DialogPrimitive.Title className="text-lg font-semibold" data-testid="visit-title">
            {v ? `Visit of ${visitorText(v.visitor)}` : "Visit"}
          </DialogPrimitive.Title>
          <DialogPrimitive.Description id="visit-drawer-desc" className="text-xs text-muted-foreground">
            One presence episode. The visit ID is not the visitor ID: a visitor can have several visits.
          </DialogPrimitive.Description>
        </div>
        <DialogClose asChild>
          <Button type="button" variant="ghost" size="icon" aria-label="Close visit details" data-testid="visit-close">
            <X className="size-4" aria-hidden />
          </Button>
        </DialogClose>
      </header>

      {detail.isPending ? (
        <div className="space-y-3 p-4" data-testid="visit-loading">
          <Skeleton className="h-28 w-full" />
          <Skeleton className="h-4 w-1/2" />
          <Skeleton className="h-4 w-2/3" />
        </div>
      ) : detail.isError || !v ? (
        <div className="space-y-3 p-4 text-sm" role="alert" data-testid="visit-error">
          <p className="flex items-center gap-2 font-medium">
            <AlertTriangle className="size-4 text-amber-500" aria-hidden />
            This visit could not be loaded.
          </p>
          <p className="text-muted-foreground">It may not exist, or the API is unreachable.</p>
          <Button type="button" variant="outline" size="sm" onClick={() => void detail.refetch()}>
            Retry
          </Button>
        </div>
      ) : (
        <VisitBody v={v} />
      )}
    </>
  );
}

function VisitBody({ v }: { v: VisitDetailRecord }) {
  const entry = v.events.find((e) => e.type === "entry");
  const terminal = v.events.find((e) => e.type !== "entry");
  const duration = formatDuration(v.duration_seconds);
  const open = v.state === "ACTIVE" || v.state === "TEMPORARILY_MISSING" || v.state === "EXIT_PENDING";
  return (
    <div className="space-y-5 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <StateBadge state={v.state} />
        <IntegrityBadge integrity={v.integrity} />
        {v.visitor ? (
          <Link
            to={`/visitors?visitor=${v.visitor.id}`}
            className="text-xs font-medium text-primary underline-offset-4 hover:underline"
            data-testid="visit-visitor-link"
          >
            Open {v.visitor.label} →
          </Link>
        ) : (
          <span className="text-xs text-muted-foreground" data-testid="visit-erased">
            Visitor erased; this record is kept without identity.
          </span>
        )}
      </div>

      <div className="grid grid-cols-2 gap-3">
        <EventPanel title="Entry" event={entry} />
        {terminal ? (
          <EventPanel title={v.state === "INTERRUPTED" ? "Interruption" : "Exit"} event={terminal} />
        ) : (
          <section className="rounded-lg border border-dashed p-3 text-xs text-muted-foreground" data-testid="visit-no-exit">
            <h3 className="mb-1 text-sm font-medium text-foreground">Exit</h3>
            {open ? "No exit yet — the visitor is still being observed." : "No exit event is recorded."}
          </section>
        )}
      </div>

      <dl className="space-y-1.5 text-sm" data-testid="visit-facts">
        <Fact label="Visit ID" value={v.id.slice(0, 12)} title={v.id} />
        <Fact label="Visitor ID" value={v.visitor ? v.visitor.label : "—"} testId="visit-visitor" />
        <Fact label="Source" value={v.source_label} />
        <Fact label="Session" value={v.session_id.slice(0, 12)} title={v.session_id} />
        <Fact label="Entry" value={formatDateTimeSeconds(v.entry_time)} testId="visit-entry-time" />
        <Fact
          label={v.exit_time ? "Last observed" : "Last observed so far"}
          value={formatDateTimeSeconds(v.exit_time ?? v.last_observed_time)}
          testId="visit-exit-time"
        />
        <Fact
          label="Observed duration"
          value={duration ?? (open ? "in progress" : "unknown")}
          testId="visit-duration"
        />
        <Fact label="Track segments" value={String(v.track_segments)} />
        {v.exit_basis ? <Fact label="Ended because" value={EXIT_BASIS_LABEL[v.exit_basis]} testId="visit-basis" /> : null}
        {v.closure_reason ? (
          <Fact label="Closure reason" value={CLOSURE_REASON_LABEL[v.closure_reason] ?? v.closure_reason} testId="visit-reason" />
        ) : null}
      </dl>

      {v.exit_basis ? (
        <p className="rounded-md bg-muted px-3 py-2 text-xs text-muted-foreground" data-testid="visit-basis-help">
          {EXIT_BASIS_HELP[v.exit_basis]}
          {v.duration_seconds === null && v.state !== "ACTIVE" ? " No duration is shown because the end of this visit is not known." : ""}
        </p>
      ) : null}
    </div>
  );
}

function EventPanel({ title, event }: { title: string; event: VisitEventRecord | undefined }) {
  if (!event) {
    return (
      <section className="rounded-lg border border-dashed p-3 text-xs text-muted-foreground">
        <h3 className="mb-1 text-sm font-medium text-foreground">{title}</h3>
        Not recorded.
      </section>
    );
  }
  return (
    <section className="space-y-2 rounded-lg border p-3" data-testid={`visit-${event.type === "entry" ? "entry" : "exit"}-panel`}>
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-sm font-medium">{title}</h3>
        <TypeBadge type={event.type} />
      </div>
      <EvidenceImage
        eventId={event.id}
        status={event.image_status}
        alt={`${title} image`}
        className="aspect-square w-full rounded-md"
      />
      <p className="text-[11px] text-muted-foreground" data-testid="evidence-note">
        {event.image_status === "STORED"
          ? (event.image_kind && IMAGE_KIND_LABEL[event.image_kind]) || "Stored image"
          : IMAGE_STATUS_LABEL[event.image_status]}
      </p>
      <p className="text-xs tabular-nums">{formatDateTimeSeconds(event.event_time)}</p>
      <PersistenceBadge status={event.persistence_status} />
    </section>
  );
}

function Fact({ label, value, testId, title }: { label: string; value: string; testId?: string; title?: string }) {
  return (
    <div className="flex justify-between gap-4 border-b pb-1">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="text-right font-medium tabular-nums" data-testid={testId} title={title}>
        {value}
      </dd>
    </div>
  );
}
