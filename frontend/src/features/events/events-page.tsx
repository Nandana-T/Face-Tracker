import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { AlertTriangle, ChevronLeft, ChevronRight, ListChecks, RefreshCw, Search } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { EmptyState } from "@/components/common/empty-state";
import { PageHeader } from "@/components/common/page-header";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { api } from "@/lib/api";
import type { EventSort, EventType, VisitEventRecord } from "@/lib/api-types";
import { cn } from "@/lib/utils";
import { EVENT_TYPE_LABEL, eventHeadline, visitorText } from "@/lib/visits";
import { formatDateTimeSeconds } from "@/features/visitors/format";
import { PersistenceBadge, TypeBadge } from "./event-badges";
import { EvidenceImage } from "./evidence-image";
import { VisitDrawer } from "./visit-drawer";

const PAGE_SIZE = 15;
const TYPES: (EventType | "")[] = ["", "entry", "exit", "interrupted"];

/** `datetime-local` value (no zone, browser-local) -> ISO instant; empty/invalid -> undefined. */
export function localToIso(value: string): string | undefined {
  if (!value) return undefined;
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? undefined : d.toISOString();
}

const field =
  "mt-1 h-10 w-full rounded-md border bg-background px-3 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary";

export function EventsPage() {
  const [params, setParams] = useSearchParams();
  const visitId = params.get("visit");
  const [type, setType] = useState<EventType | "">((params.get("type") as EventType | null) ?? "");
  const [draft, setDraft] = useState(params.get("visitor") ?? "");
  const [visitor, setVisitor] = useState(params.get("visitor") ?? "");
  const [since, setSince] = useState("");
  const [until, setUntil] = useState("");
  const [sort, setSort] = useState<EventSort>("event_time");
  const [order, setOrder] = useState<"asc" | "desc">("desc");
  const [page, setPage] = useState(0);

  const sinceIso = localToIso(since);
  const untilIso = localToIso(until);
  const rangeInvalid = !!sinceIso && !!untilIso && sinceIso > untilIso;

  const query = useQuery({
    queryKey: ["events", { type, visitor, sinceIso, untilIso, sort, order, page }],
    queryFn: ({ signal }) =>
      api.events(
        {
          limit: PAGE_SIZE,
          offset: page * PAGE_SIZE,
          type: type || undefined,
          visitor,
          since: sinceIso,
          until: untilIso,
          sort,
          order,
        },
        signal,
      ),
    enabled: !rangeInvalid,
    placeholderData: keepPreviousData,
    refetchInterval: 8_000, // new events appear while monitoring runs
  });

  const total = query.data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const items = query.data?.items ?? [];
  const filtered = !!(type || visitor || since || until);

  const openVisit = (e: VisitEventRecord) =>
    setParams((prev) => {
      const next = new URLSearchParams(prev);
      next.set("visit", e.visit_id);
      return next;
    });
  const closeVisit = () =>
    setParams((prev) => {
      const next = new URLSearchParams(prev);
      next.delete("visit");
      return next;
    });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    setPage(0);
    setVisitor(draft.trim());
  };
  const reset = () => {
    setType("");
    setDraft("");
    setVisitor("");
    setSince("");
    setUntil("");
    setPage(0);
  };

  let body;
  if (rangeInvalid) {
    body = (
      <p role="alert" className="rounded-md border border-dashed px-4 py-6 text-sm" data-testid="events-range-error">
        The start of the time range is after its end. Adjust the “From” and “To” fields.
      </p>
    );
  } else if (query.isPending) {
    body = (
      <ul className="space-y-2" data-testid="events-loading" aria-label="Loading events">
        {Array.from({ length: 6 }, (_, i) => (
          <li key={i}>
            <Skeleton className="h-[72px] w-full" />
          </li>
        ))}
      </ul>
    );
  } else if (query.isError && !query.data) {
    body = (
      <div role="alert" className="flex flex-col items-center gap-3 rounded-lg border border-dashed px-6 py-12 text-center" data-testid="events-error">
        <AlertTriangle className="size-8 text-amber-500" aria-hidden />
        <h2 className="text-base font-semibold">Events could not be loaded</h2>
        <p className="max-w-md text-sm text-muted-foreground">
          {query.error instanceof Error ? query.error.message : "The API did not answer."} The event log may
          be switched off, or the backend is unreachable.
        </p>
        <Button type="button" variant="outline" size="sm" onClick={() => void query.refetch()} data-testid="events-retry">
          <RefreshCw className="size-4" aria-hidden />
          Retry
        </Button>
      </div>
    );
  } else if (items.length === 0) {
    body = filtered ? (
      <EmptyState
        icon={Search}
        title="No events match these filters"
        description="Try a wider time range, another event type, or search by visitor number (V00012 or 12) or the start of a visitor ID."
        action={
          <Button type="button" variant="outline" size="sm" onClick={reset} data-testid="events-clear">
            Clear filters
          </Button>
        }
      />
    ) : (
      <EmptyState
        icon={ListChecks}
        title="No events recorded"
        description="An entry is recorded when a recognized or newly registered visitor is confirmed in view, and an exit when they have not been seen for the configured period or monitoring ends. Start Live Monitoring to see events here."
      />
    );
  } else {
    body = (
      <ol className="space-y-2" aria-label="Event timeline" data-testid="event-timeline">
        <AnimatePresence initial={false} mode="popLayout">
          {items.map((e, i) => (
            <EventRow key={e.id} event={e} index={i} selected={visitId === e.visit_id} onOpen={openVisit} />
          ))}
        </AnimatePresence>
      </ol>
    );
  }

  return (
    <>
      <PageHeader
        title="Event Logs"
        description="Entry and exit events of visits, recorded in the database with their evidence image. One visit is one presence episode; a visitor can have several."
      />

      <div className="mb-4 flex flex-wrap items-end gap-3">
        <label className="text-xs text-muted-foreground">
          Event type
          <select
            value={type}
            onChange={(e) => { setType(e.target.value as EventType | ""); setPage(0); }}
            className={field}
            data-testid="event-filter-type"
          >
            {TYPES.map((t) => (
              <option key={t || "all"} value={t}>{t ? EVENT_TYPE_LABEL[t] : "All events"}</option>
            ))}
          </select>
        </label>
        <form onSubmit={submit} role="search" className="flex flex-1 items-end gap-2" style={{ minWidth: 220 }}>
          <label className="flex-1 text-xs text-muted-foreground">
            Visitor
            <input
              type="search"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              placeholder="V00012, 12 or an ID prefix"
              maxLength={40}
              className={field}
              data-testid="event-filter-visitor"
            />
          </label>
          <Button type="submit" size="sm" className="h-10" data-testid="event-search-submit">
            <Search className="size-4" aria-hidden />
            Search
          </Button>
        </form>
        <label className="text-xs text-muted-foreground">
          From
          <input type="datetime-local" value={since} onChange={(e) => { setSince(e.target.value); setPage(0); }} className={field} data-testid="event-filter-since" />
        </label>
        <label className="text-xs text-muted-foreground">
          To
          <input type="datetime-local" value={until} onChange={(e) => { setUntil(e.target.value); setPage(0); }} className={field} data-testid="event-filter-until" />
        </label>
        <label className="text-xs text-muted-foreground">
          Sort by
          <select value={sort} onChange={(e) => { setSort(e.target.value as EventSort); setPage(0); }} className={field} data-testid="event-sort">
            <option value="event_time">Event time</option>
            <option value="recorded_at">Time recorded</option>
          </select>
        </label>
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="h-10"
          onClick={() => { setOrder((o) => (o === "desc" ? "asc" : "desc")); setPage(0); }}
          aria-label={`Order: ${order === "desc" ? "newest first" : "oldest first"}. Click to reverse`}
          data-testid="event-order"
        >
          {order === "desc" ? "Newest first" : "Oldest first"}
        </Button>
        {filtered ? (
          <Button type="button" variant="ghost" size="sm" className="h-10" onClick={reset} data-testid="events-reset">
            Reset
          </Button>
        ) : null}
      </div>

      <p className="sr-only" role="status" aria-live="polite" data-testid="events-status">
        {query.data ? `${total} event${total === 1 ? "" : "s"}${filtered ? " match the filters" : ""}.` : ""}
      </p>

      {body}

      {items.length > 0 && !rangeInvalid ? (
        <motion.nav initial={{ opacity: 0 }} animate={{ opacity: 1 }} aria-label="Pagination" className="mt-5 flex items-center justify-between gap-3 text-sm">
          <span className="text-muted-foreground" data-testid="events-range">
            Showing {page * PAGE_SIZE + 1}–{page * PAGE_SIZE + items.length} of {total}
          </span>
          <span className="flex items-center gap-2">
            <Button type="button" variant="outline" size="sm" disabled={page === 0} onClick={() => setPage((p) => p - 1)} data-testid="page-prev">
              <ChevronLeft className="size-4" aria-hidden />
              Previous
            </Button>
            <span className="tabular-nums text-muted-foreground" data-testid="page-indicator">
              Page {page + 1} of {pages}
            </span>
            <Button type="button" variant="outline" size="sm" disabled={page + 1 >= pages} onClick={() => setPage((p) => p + 1)} data-testid="page-next">
              Next
              <ChevronRight className="size-4" aria-hidden />
            </Button>
          </span>
        </motion.nav>
      ) : null}

      <p className="mt-6 max-w-3xl text-xs text-muted-foreground">
        Times are the first (entry) or last (exit) sighting. An exit is not a measured departure, and the number
        of visits or events is not a count of unique people.
      </p>

      <VisitDrawer visitId={visitId} onClose={closeVisit} />
    </>
  );
}

function EventRow({
  event: e,
  index,
  selected,
  onOpen,
}: {
  event: VisitEventRecord;
  index: number;
  selected: boolean;
  onOpen: (e: VisitEventRecord) => void;
}) {
  const who = visitorText(e.visitor);
  return (
    <motion.li
      layout="position"
      initial={{ opacity: 0, y: -8 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0 }}
      transition={{ duration: 0.2, delay: Math.min(index, 6) * 0.025 }}
      data-testid="event-row"
      data-event-type={e.type}
    >
      <div
        className={cn(
          "flex items-center gap-3 rounded-lg border bg-card p-2 pr-3 shadow-[var(--shadow-card)] transition-colors hover:border-primary",
          selected && "border-primary ring-1 ring-primary",
        )}
      >
        <EvidenceImage
          eventId={e.id}
          status={e.image_status}
          alt={`${eventHeadline(e)} image of ${who}`}
          className="size-14 shrink-0 rounded-md"
        />
        <button
          type="button"
          onClick={() => onOpen(e)}
          aria-label={`Open visit of ${who}, ${eventHeadline(e).toLowerCase()} at ${formatDateTimeSeconds(e.event_time)}`}
          className="flex min-w-0 flex-1 flex-col gap-1 rounded text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
          data-testid="event-open"
        >
          <span className="flex flex-wrap items-center gap-2">
            <TypeBadge type={e.type} label={eventHeadline(e)} />
            <span className="font-mono text-sm font-semibold" data-testid="event-visitor">{who}</span>
            <time dateTime={e.event_time} className="text-xs text-muted-foreground tabular-nums" data-testid="event-time">
              {formatDateTimeSeconds(e.event_time)}
            </time>
          </span>
          <span className="truncate text-xs text-muted-foreground">
            {e.source_label} · session {e.session_id.slice(0, 8)}
          </span>
        </button>
        <span className="hidden shrink-0 flex-col items-end gap-1 sm:flex">
          <PersistenceBadge status={e.persistence_status} />
          {e.visitor ? (
            <Link
              to={`/visitors?visitor=${e.visitor.id}`}
              className="text-[11px] font-medium text-primary underline-offset-4 hover:underline"
              data-testid="event-visitor-link"
            >
              Open visitor →
            </Link>
          ) : null}
        </span>
      </div>
    </motion.li>
  );
}
