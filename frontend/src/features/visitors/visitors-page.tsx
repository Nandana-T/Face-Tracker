import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { AlertTriangle, ChevronLeft, ChevronRight, RefreshCw, Search, Users } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { useState, type FormEvent } from "react";
import { useSearchParams } from "react-router-dom";
import { EmptyState } from "@/components/common/empty-state";
import { PageHeader } from "@/components/common/page-header";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { api } from "@/lib/api";
import type { Visitor, VisitorSort } from "@/lib/api-types";
import { SORT_LABEL } from "./format";
import { VisitorCard } from "./visitor-card";
import { VisitorDrawer } from "./visitor-drawer";

const PAGE_SIZE = 12;

export function VisitorsPage() {
  const [params, setParams] = useSearchParams();
  const selectedId = params.get("visitor");
  const [draft, setDraft] = useState("");
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState<VisitorSort>("last_seen");
  const [order, setOrder] = useState<"asc" | "desc">("desc");
  const [page, setPage] = useState(0);

  const query = useQuery({
    queryKey: ["visitors", { search, sort, order, page }],
    queryFn: ({ signal }) =>
      api.visitors({ limit: PAGE_SIZE, offset: page * PAGE_SIZE, q: search, sort, order }, signal),
    placeholderData: keepPreviousData,
    refetchInterval: 15_000, // new registrations appear while processing runs elsewhere
  });

  const total = query.data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const items = query.data?.items ?? [];

  const open = (v: Visitor) => setParams({ visitor: v.id });
  const close = () => setParams({});
  const submit = (e: FormEvent) => {
    e.preventDefault();
    setPage(0);
    setSearch(draft.trim());
  };

  let body;
  if (query.isPending) {
    body = (
      <ul className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4" data-testid="visitors-loading">
        {Array.from({ length: 8 }, (_, i) => (
          <li key={i}>
            <Skeleton className="aspect-[3/4] w-full" />
          </li>
        ))}
      </ul>
    );
  } else if (query.isError && !query.data) {
    body = (
      <div
        role="alert"
        className="flex flex-col items-center gap-3 rounded-lg border border-dashed px-6 py-12 text-center"
        data-testid="visitors-error"
      >
        <AlertTriangle className="size-8 text-amber-500" aria-hidden />
        <h2 className="text-base font-semibold">Visitors could not be loaded</h2>
        <p className="max-w-md text-sm text-muted-foreground">
          {query.error instanceof Error ? query.error.message : "The API did not answer."} Recognition may
          be switched off, or the backend is unreachable.
        </p>
        <Button type="button" variant="outline" size="sm" onClick={() => void query.refetch()} data-testid="visitors-retry">
          <RefreshCw className="size-4" aria-hidden />
          Retry
        </Button>
      </div>
    );
  } else if (items.length === 0) {
    body = search ? (
      <EmptyState
        icon={Search}
        title="No visitors match your search"
        description={`Nothing matches “${search}”. Search by visitor number (V00012 or 12) or the start of a visitor ID.`}
        action={
          <Button type="button" variant="outline" size="sm" onClick={() => { setDraft(""); setSearch(""); setPage(0); }} data-testid="clear-search">
            Clear search
          </Button>
        }
      />
    ) : (
      <EmptyState
        icon={Users}
        title="No visitors registered yet"
        description="A visitor is registered automatically after several reliable observations of a new face. Start processing a video in Live Monitoring to see this fill up."
      />
    );
  } else {
    body = (
      <ul
        className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4"
        aria-label="Registered visitors"
        data-testid="visitor-grid"
      >
        <AnimatePresence initial={false} mode="popLayout">
          {items.map((v, i) => (
            <VisitorCard key={v.id} visitor={v} index={i} selected={selectedId === v.id} onOpen={open} />
          ))}
        </AnimatePresence>
      </ul>
    );
  }

  return (
    <>
      <PageHeader
        title="Visitors"
        description="Registered visitor identities (persistent) and what recognition has concluded about them."
      />

      <div className="mb-4 flex flex-wrap items-end gap-3">
        <form onSubmit={submit} role="search" className="flex flex-1 items-end gap-2" style={{ minWidth: 240 }}>
          <label className="flex-1 text-xs text-muted-foreground">
            Search by visitor ID
            <input
              type="search"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              placeholder="V00012, 12 or an ID prefix"
              maxLength={40}
              className="mt-1 h-10 w-full rounded-md border bg-background px-3 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
              data-testid="visitor-search"
            />
          </label>
          <Button type="submit" size="sm" className="h-10" data-testid="visitor-search-submit">
            <Search className="size-4" aria-hidden />
            Search
          </Button>
        </form>
        <label className="text-xs text-muted-foreground">
          Sort by
          <select
            value={sort}
            onChange={(e) => { setSort(e.target.value as VisitorSort); setPage(0); }}
            className="mt-1 h-10 w-full rounded-md border bg-background px-3 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
            data-testid="visitor-sort"
          >
            {(Object.keys(SORT_LABEL) as VisitorSort[]).map((k) => (
              <option key={k} value={k}>{SORT_LABEL[k]}</option>
            ))}
          </select>
        </label>
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="h-10"
          onClick={() => { setOrder((o) => (o === "desc" ? "asc" : "desc")); setPage(0); }}
          aria-label={`Order: ${order === "desc" ? "newest first" : "oldest first"}. Click to reverse`}
          data-testid="visitor-order"
        >
          {order === "desc" ? "Newest first" : "Oldest first"}
        </Button>
      </div>

      <p className="sr-only" role="status" aria-live="polite" data-testid="visitors-status">
        {query.data ? `${total} visitor${total === 1 ? "" : "s"}${search ? ` matching ${search}` : ""}.` : ""}
      </p>

      {body}

      {items.length > 0 ? (
        <motion.nav
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          aria-label="Pagination"
          className="mt-5 flex items-center justify-between gap-3 text-sm"
        >
          <span className="text-muted-foreground" data-testid="visitors-range">
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

      <VisitorDrawer visitorId={selectedId} onClose={close} />
    </>
  );
}
