import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { Activity, AlertTriangle, DoorClosed, DoorOpen, Eye, Info, RefreshCw, Repeat, ShieldCheck, Users, Wifi, WifiOff } from "lucide-react";
import { motion } from "motion/react";
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { EmptyState } from "@/components/common/empty-state";
import { PageHeader } from "@/components/common/page-header";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { api } from "@/lib/api";
import type { AnalyticsCoverage, AnalyticsScope, TimelinePoint } from "@/lib/api-types";
import { eventHeadline, visitorText } from "@/lib/visits";
import { localToIso } from "@/features/events/events-page";
import { formatDateTimeSeconds } from "@/features/visitors/format";
import { MetricTile } from "./analytics-metrics";
import { BarChart, ProportionBar, type ChartPoint } from "./charts";
import { analyticsKeys, POLL_MS, useAnalyticsLive, useOverview } from "./use-analytics";

type Preset = "all" | "24h" | "7d" | "custom";
type Metric = "unique_visitors" | "visits";
const field =
  "mt-1 h-10 w-full rounded-md border bg-background px-3 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary";

export function bucketTick(iso: string, bucket: "hour" | "day"): string {
  const d = new Date(iso);
  return bucket === "hour"
    ? d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" });
}

const pointLabel = (p: TimelinePoint) => p.bucket_start.replace(".000Z", "Z");

export function AnalyticsPage() {
  const [preset, setPreset] = useState<Preset>("all");
  const [since, setSince] = useState("");
  const [until, setUntil] = useState("");
  const [sourceId, setSourceId] = useState("");
  const [sessionId, setSessionId] = useState("");
  const [metric, setMetric] = useState<Metric>("unique_visitors");
  const [bucketChoice, setBucketChoice] = useState<"auto" | "hour" | "day">("auto");
  const [anchor] = useState(() => Date.now());
  const live = useAnalyticsLive();

  const range = useMemo(() => {
    if (preset === "24h") return { since: new Date(anchor - 24 * 3600_000).toISOString(), until: undefined };
    if (preset === "7d") return { since: new Date(anchor - 7 * 24 * 3600_000).toISOString(), until: undefined };
    if (preset === "custom") return { since: localToIso(since), until: localToIso(until) };
    return { since: undefined, until: undefined };
  }, [preset, since, until, anchor]);
  const rangeInvalid = !!range.since && !!range.until && range.since >= range.until;
  const scope: AnalyticsScope = useMemo(
    () => ({ ...range, source_id: sourceId || undefined, session_id: sessionId || undefined }),
    [range, sourceId, sessionId],
  );
  const filtered = preset !== "all" || !!sourceId || !!sessionId;
  const spanHours = range.since ? ((range.until ? Date.parse(range.until) : anchor) - Date.parse(range.since)) / 3600_000 : Infinity;
  const bucket: "hour" | "day" = bucketChoice === "auto" ? (spanHours <= 72 ? "hour" : "day") : bucketChoice;

  const enabled = !rangeInvalid;
  const overview = useOverview(scope);
  const timeline = useQuery({
    queryKey: analyticsKeys.timeline(scope, bucket),
    queryFn: ({ signal }) => api.analyticsTimeline(scope, bucket, signal),
    enabled,
    placeholderData: keepPreviousData,
    refetchInterval: POLL_MS,
  });
  const sources = useQuery({
    queryKey: analyticsKeys.sources(scope),
    queryFn: ({ signal }) => api.analyticsSources(scope, signal),
    enabled,
    refetchInterval: POLL_MS,
  });
  const sessions = useQuery({
    queryKey: analyticsKeys.sessions(scope),
    queryFn: ({ signal }) => api.analyticsSessions(scope, 8, signal),
    enabled,
    refetchInterval: POLL_MS,
  });
  const sessionChoices = useQuery({
    queryKey: [...analyticsKeys.sessions({ ...range }), "choices"],
    queryFn: ({ signal }) => api.analyticsSessions({ ...range }, 30, signal),
    enabled,
  });
  const coverage = useQuery({
    queryKey: analyticsKeys.coverage(scope),
    queryFn: ({ signal }) => api.analyticsCoverage(scope, signal),
    enabled,
    refetchInterval: POLL_MS,
  });
  const recent = useQuery({
    queryKey: analyticsKeys.recent(scope),
    queryFn: ({ signal }) => api.analyticsRecent(scope, 8, signal),
    enabled,
    refetchInterval: POLL_MS,
  });
  const sourceList = useQuery({ queryKey: ["processing", "sources"], queryFn: ({ signal }) => api.sources(signal), staleTime: 60_000 });

  const o = overview.data;
  const loading = overview.isPending && enabled;
  const down = overview.isError && !o;
  const empty = !!o && o.visits === 0;

  const activity: ChartPoint[] = (timeline.data?.points ?? []).map((p) => ({
    label: pointLabel(p),
    tick: bucketTick(p.bucket_start, bucket),
    values: { unique_visitors: p.unique_visitors, visits: p.visits },
  }));
  const flow: ChartPoint[] = (timeline.data?.points ?? []).map((p) => ({
    label: pointLabel(p),
    tick: bucketTick(p.bucket_start, bucket),
    values: { entries: p.entries, left: p.exits_absence, ended: p.exits_observation_ended, interrupted: p.interrupted },
  }));

  const reset = () => {
    setPreset("all");
    setSince("");
    setUntil("");
    setSourceId("");
    setSessionId("");
  };
  const exits = o ? o.terminal_by_basis.absence + o.terminal_by_basis.observation_ended : null;

  return (
    <>
      <PageHeader
        title="Analytics"
        description="Unique registered visitors, visits and entry/exit events, computed from the database."
      />

      <div
        className="mb-4 flex items-start gap-2 rounded-lg border bg-muted/40 px-4 py-3 text-sm"
        data-testid="counting-note"
        role="note"
      >
        <Info className="mt-0.5 size-4 shrink-0 text-primary" aria-hidden />
        <p>
          <strong>Unique visitors</strong> counts distinct registered face identities. Faces that could not be reliably
          recognised are not included, so this is <strong>not</strong> a count of physical people. A visit is one presence
          episode, and a visitor can have several.
        </p>
      </div>

      <section aria-label="Filters" className="mb-5 flex flex-wrap items-end gap-3">
        <fieldset className="text-xs text-muted-foreground">
          <legend className="mb-1">Period</legend>
          <div className="flex gap-1" role="group" aria-label="Period presets">
            {(["all", "24h", "7d", "custom"] as Preset[]).map((p) => (
              <Button key={p} type="button" size="sm" className="h-10" variant={preset === p ? "default" : "outline"} aria-pressed={preset === p} onClick={() => setPreset(p)} data-testid={`preset-${p}`}>
                {{ all: "All time", "24h": "Last 24 h", "7d": "Last 7 days", custom: "Custom" }[p]}
              </Button>
            ))}
          </div>
        </fieldset>
        {preset === "custom" ? (
          <>
            <label className="text-xs text-muted-foreground">
              From
              <input type="datetime-local" value={since} onChange={(e) => setSince(e.target.value)} className={field} data-testid="analytics-since" />
            </label>
            <label className="text-xs text-muted-foreground">
              To
              <input type="datetime-local" value={until} onChange={(e) => setUntil(e.target.value)} className={field} data-testid="analytics-until" />
            </label>
          </>
        ) : null}
        <label className="text-xs text-muted-foreground">
          Source
          <select value={sourceId} onChange={(e) => { setSourceId(e.target.value); setSessionId(""); }} className={field} data-testid="analytics-source">
            <option value="">All sources</option>
            {(sourceList.data?.sources ?? []).map((s) => (
              <option key={s.id} value={s.id}>{s.label}</option>
            ))}
          </select>
        </label>
        <label className="text-xs text-muted-foreground">
          Session
          <select value={sessionId} onChange={(e) => setSessionId(e.target.value)} className={field} data-testid="analytics-session">
            <option value="">All sessions</option>
            {(sessionChoices.data?.items ?? [])
              .filter((s) => !sourceId || s.source_id === sourceId)
              .map((s) => (
                <option key={s.session_id} value={s.session_id}>
                  {s.source_label} · {formatDateTimeSeconds(s.started_at)}
                </option>
              ))}
          </select>
        </label>
        <label className="text-xs text-muted-foreground">
          Chart interval
          <select value={bucketChoice} onChange={(e) => setBucketChoice(e.target.value as "auto" | "hour" | "day")} className={field} data-testid="analytics-bucket">
            <option value="auto">Automatic</option>
            <option value="hour">Hourly</option>
            <option value="day">Daily (UTC)</option>
          </select>
        </label>
        {filtered ? (
          <Button type="button" variant="ghost" size="sm" className="h-10" onClick={reset} data-testid="analytics-reset">
            Reset filters
          </Button>
        ) : null}
        <span className="ml-auto flex items-center gap-1.5 text-xs text-muted-foreground" data-testid="live-indicator" data-state={live} aria-live="polite">
          {live === "live" ? <Wifi className="size-3.5 text-teal-400" aria-hidden /> : <WifiOff className="size-3.5" aria-hidden />}
          {live === "live" ? "Live updates on" : live === "connecting" ? "Connecting…" : "Live updates off — refreshing every 20 s"}
        </span>
      </section>

      {rangeInvalid ? (
        <p role="alert" className="mb-4 rounded-md border border-dashed px-4 py-6 text-sm" data-testid="analytics-range-error">
          The start of the period is after its end. Adjust “From” and “To”.
        </p>
      ) : down ? (
        <div role="alert" className="mb-4 flex flex-col items-center gap-3 rounded-lg border border-dashed px-6 py-10 text-center" data-testid="analytics-error">
          <AlertTriangle className="size-8 text-amber-500" aria-hidden />
          <h2 className="text-base font-semibold">Analytics could not be loaded</h2>
          <p className="max-w-md text-sm text-muted-foreground">
            {overview.error instanceof Error ? overview.error.message : "The API did not answer."} The backend may be
            unreachable. No numbers are shown rather than stale or invented ones.
          </p>
          <Button type="button" variant="outline" size="sm" onClick={() => void overview.refetch()} data-testid="analytics-retry">
            <RefreshCw className="size-4" aria-hidden />
            Retry
          </Button>
        </div>
      ) : null}

      {!rangeInvalid && !down ? (
        <>
          <section aria-label="Key figures" className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
            <MetricTile name="registered_visitors" label="Registered visitors" value={o?.registered_visitors ?? null} loading={loading} scope="global" hint="Persistent identities in the database" icon={Users} />
            <MetricTile name="unique_visitors" label="Unique visitors" value={o?.unique_visitors_in_scope ?? null} loading={loading} scope="filtered" hint="Distinct identities with a visit in scope" icon={Users} />
            <MetricTile name="returning_visitors" label="Returning visitors" value={o?.returning_visitors_in_scope ?? null} loading={loading} scope="filtered" hint="Seen in a later session than their registration" icon={Repeat} />
            <MetricTile name="visits" label="Visits" value={o?.visits ?? null} loading={loading} scope="filtered" hint="Presence episodes (a visitor can have several)" icon={Eye} />
            <MetricTile name="entries" label="Entry events" value={o?.entry_events ?? null} loading={loading} scope="filtered" hint="One per visit" icon={DoorOpen} />
            <MetricTile name="exits" label="Exit events" value={exits} loading={loading} scope="filtered" hint={o ? `${o.terminal_by_basis.absence} left the view · ${o.terminal_by_basis.observation_ended} observation ended` : "Not a verified departure"} icon={DoorClosed} />
            <MetricTile name="interrupted" label="Interrupted visits" value={o?.terminal_by_basis.interrupted ?? null} loading={loading} scope="filtered" hint="Worker, source or API stopped; end unknown" icon={AlertTriangle} tone={o && o.terminal_by_basis.interrupted > 0 ? "warn" : "default"} />
            <MetricTile name="active_visits" label="In view now" value={o?.active_visits ?? null} loading={loading} scope="live" hint={o ? `${o.active_visits_temporarily_missing} temporarily out of view${o.stale_open_visits ? ` · ${o.stale_open_visits} stale open (not counted)` : ""}` : "Open visits of the live session"} icon={Activity} />
          </section>
          {o ? (
            <p className="mt-2 text-xs text-muted-foreground" data-testid="updated-at">
              Updated {formatDateTimeSeconds(o.generated_at)}
              {o.visits_without_identity ? ` · ${o.visits_without_identity} visit(s) of erased visitors are counted as visits but not as unique visitors` : ""}
            </p>
          ) : null}

          {empty ? (
            <div className="mt-6" data-testid="analytics-empty">
              <EmptyState
                icon={Activity}
                title={filtered ? "No visits match these filters" : "No analytics available"}
                description={
                  filtered
                    ? "Try a wider period or another source/session."
                    : "Visitor analytics will appear after processing begins: a visit is recorded when a recognised visitor is confirmed in view."
                }
                action={filtered ? <Button type="button" variant="outline" size="sm" onClick={reset}>Clear filters</Button> : undefined}
              />
            </div>
          ) : (
            <>
              <div className="mt-6 grid gap-6 xl:grid-cols-2">
                <ChartCard
                  title="Visitor activity"
                  description="Per period, by visit entry time (UTC buckets; unique visitors per bar are not additive)."
                  loading={timeline.isPending}
                  error={timeline.isError && !timeline.data}
                  onRetry={() => void timeline.refetch()}
                  extra={
                    <div role="group" aria-label="Metric" className="flex gap-1">
                      {(["unique_visitors", "visits"] as Metric[]).map((m) => (
                        <Button key={m} type="button" size="sm" variant={metric === m ? "default" : "outline"} aria-pressed={metric === m} onClick={() => setMetric(m)} data-testid={`metric-${m}`}>
                          {m === "visits" ? "Visits" : "Unique visitors"}
                        </Button>
                      ))}
                    </div>
                  }
                >
                  <BarChart
                    testId="chart-activity"
                    title={metric === "visits" ? "Visits per period" : "Unique visitors per period"}
                    points={activity}
                    series={[metric === "visits" ? { key: "visits", label: "Visits", color: "#38bdf8" } : { key: "unique_visitors", label: "Unique visitors", color: "#2dd4bf" }]}
                  />
                </ChartCard>
                <ChartCard title="Entries and exits" description="Entry events and how visits ended, per period." loading={timeline.isPending} error={timeline.isError && !timeline.data} onRetry={() => void timeline.refetch()}>
                  <BarChart
                    testId="chart-flow"
                    stacked={false}
                    title="Entry and terminal events per period"
                    points={flow}
                    series={[
                      { key: "entries", label: "Entries", color: "#2dd4bf" },
                      { key: "left", label: "Left the view", color: "#38bdf8" },
                      { key: "ended", label: "Observation ended", color: "#a78bfa" },
                      { key: "interrupted", label: "Interrupted", color: "#f59e0b" },
                    ]}
                  />
                </ChartCard>
              </div>

              <div className="mt-6 grid gap-6 xl:grid-cols-3">
                <Card>
                  <CardHeader>
                    <CardTitle>Visitor composition</CardTitle>
                    <CardDescription>New vs returning registered identities in scope.</CardDescription>
                  </CardHeader>
                  <CardContent>
                    {o ? (
                      o.unique_visitors_in_scope === 0 ? (
                        <p className="text-sm text-muted-foreground" data-testid="composition-empty">No identified visitors in this scope.</p>
                      ) : (
                        <ProportionBar
                          testId="composition"
                          parts={[
                            { label: "New", value: o.new_visitors_in_scope, color: "#2dd4bf" },
                            { label: "Returning", value: o.returning_visitors_in_scope, color: "#a78bfa" },
                          ]}
                        />
                      )
                    ) : (
                      <Skeleton className="h-10 w-full" />
                    )}
                    <p className="mt-3 text-[11px] text-muted-foreground">
                      Returning = seen in a later session than the one that registered them. A second track or a second visit in the same session does not count.
                    </p>
                  </CardContent>
                </Card>
                <BreakdownCard title="By source" loading={sources.isPending} error={sources.isError} onRetry={() => void sources.refetch()} empty={(sources.data?.items.length ?? 0) === 0} testId="sources-table">
                  <table className="w-full text-sm">
                    <thead className="text-left text-xs text-muted-foreground">
                      <tr><th className="pb-1 font-normal">Source</th><th className="pb-1 text-right font-normal">Visits</th><th className="pb-1 text-right font-normal">Unique</th></tr>
                    </thead>
                    <tbody>
                      {sources.data?.items.map((s) => (
                        <tr key={s.source_id} className="border-t" data-testid="source-row">
                          <td className="py-1.5">{s.source_label ?? s.source_id}</td>
                          <td className="py-1.5 text-right tabular-nums">{s.visits}</td>
                          <td className="py-1.5 text-right tabular-nums">{s.unique_visitors}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </BreakdownCard>
                <BreakdownCard title="By session" loading={sessions.isPending} error={sessions.isError} onRetry={() => void sessions.refetch()} empty={(sessions.data?.items.length ?? 0) === 0} testId="sessions-table">
                  <table className="w-full text-sm">
                    <thead className="text-left text-xs text-muted-foreground">
                      <tr><th className="pb-1 font-normal">Session</th><th className="pb-1 text-right font-normal">Visits</th><th className="pb-1 text-right font-normal">Unique</th><th className="pb-1 text-right font-normal">New</th></tr>
                    </thead>
                    <tbody>
                      {sessions.data?.items.map((s) => (
                        <tr key={s.session_id} className="border-t" data-testid="session-row">
                          <td className="py-1.5">
                            {s.source_label}
                            <span className="block text-[11px] text-muted-foreground">
                              {formatDateTimeSeconds(s.started_at)}{s.live ? " · live" : ""}{s.interrupted_visits ? ` · ${s.interrupted_visits} interrupted` : ""}
                            </span>
                          </td>
                          <td className="py-1.5 text-right tabular-nums">{s.visits}</td>
                          <td className="py-1.5 text-right tabular-nums">{s.unique_visitors}</td>
                          <td className="py-1.5 text-right tabular-nums" title="Visitors first registered in this session">{s.visitors_registered_here}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </BreakdownCard>
              </div>
            </>
          )}

          <div className="mt-6 grid gap-6 xl:grid-cols-3">
            <Card className="xl:col-span-2" data-testid="coverage-panel">
              <CardHeader>
                <CardTitle>Recognition coverage</CardTitle>
                <CardDescription>
                  How many tracked faces ended with a resolved identity. An operational diagnostic — not person-level recall.
                </CardDescription>
              </CardHeader>
              <CardContent>
                {coverage.isPending ? (
                  <Skeleton className="h-24 w-full" />
                ) : coverage.isError || !coverage.data ? (
                  <p role="alert" className="text-sm text-muted-foreground" data-testid="coverage-error">
                    Coverage could not be loaded. <button type="button" className="underline" onClick={() => void coverage.refetch()}>Retry</button>
                  </p>
                ) : (
                  <CoverageBody c={coverage.data} />
                )}
              </CardContent>
            </Card>
            <Card data-testid="recent-panel">
              <CardHeader>
                <CardTitle>Recent activity</CardTitle>
                <CardDescription>The latest entry and exit events in scope.</CardDescription>
              </CardHeader>
              <CardContent>
                {recent.isPending ? (
                  <Skeleton className="h-24 w-full" />
                ) : (recent.data?.items.length ?? 0) === 0 ? (
                  <p className="text-sm text-muted-foreground" data-testid="recent-empty">No events in this scope.</p>
                ) : (
                  <ul className="space-y-1.5" data-testid="recent-list">
                    {recent.data!.items.map((e) => (
                      <motion.li key={e.id} layout="position" initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="flex items-center justify-between gap-2 rounded-md border px-2.5 py-1.5 text-xs" data-testid="recent-item">
                        <span><strong>{eventHeadline(e)}</strong> <span className="font-mono">{visitorText(e.visitor)}</span></span>
                        <Link to={`/events?visit=${e.visit_id}`} className="tabular-nums text-muted-foreground underline-offset-4 hover:text-foreground hover:underline">
                          {formatDateTimeSeconds(e.event_time).split(", ").pop()}
                        </Link>
                      </motion.li>
                    ))}
                  </ul>
                )}
                <Link to="/events" className="mt-3 inline-block text-xs font-medium text-primary underline-offset-4 hover:underline">Open Event Logs →</Link>
              </CardContent>
            </Card>
          </div>
        </>
      ) : null}

      <ConsistencyPanel />
    </>
  );
}

function ChartCard({
  title, description, loading, error, onRetry, children, extra,
}: {
  title: string; description: string; loading: boolean; error: boolean; onRetry: () => void; children: React.ReactNode; extra?: React.ReactNode;
}) {
  return (
    <Card>
      <CardHeader className="flex-row items-start justify-between gap-3">
        <div>
          <CardTitle>{title}</CardTitle>
          <CardDescription>{description}</CardDescription>
        </div>
        {extra}
      </CardHeader>
      <CardContent>
        {loading ? (
          <Skeleton className="h-56 w-full" data-testid="chart-skeleton" />
        ) : error ? (
          <p role="alert" className="text-sm text-muted-foreground" data-testid="chart-error">
            This chart could not be loaded. <button type="button" className="underline" onClick={onRetry}>Retry</button>
          </p>
        ) : (
          children
        )}
      </CardContent>
    </Card>
  );
}

function BreakdownCard({
  title, loading, error, empty, onRetry, children, testId,
}: {
  title: string; loading: boolean; error: boolean; empty: boolean; onRetry: () => void; children: React.ReactNode; testId: string;
}) {
  return (
    <Card data-testid={testId}>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
      </CardHeader>
      <CardContent>
        {loading ? <Skeleton className="h-24 w-full" /> : error ? (
          <p role="alert" className="text-sm text-muted-foreground">Could not be loaded. <button type="button" className="underline" onClick={onRetry}>Retry</button></p>
        ) : empty ? (
          <p className="text-sm text-muted-foreground">Nothing in this scope.</p>
        ) : children}
      </CardContent>
    </Card>
  );
}

function CoverageBody({ c }: { c: AnalyticsCoverage }) {
  const share = c.resolved_share_of_evaluated_tracks;
  return (
    <div className="space-y-3">
      <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {[
          ["confirmed_tracks", "Confirmed tracks", c.confirmed_tracks],
          ["tracks_evaluated", "Tracks evaluated", c.tracks_with_recognition_observations],
          ["tracks_resolved", "With a resolved identity", c.tracks_resolved],
          ["tracks_unresolved", "Without a resolved identity", c.tracks_unresolved],
        ].map(([k, label, v]) => (
          <div key={k as string} className="rounded-md border p-2.5">
            <dt className="text-xs text-muted-foreground">{label}</dt>
            <dd className="font-mono text-lg font-semibold tabular-nums" data-testid={`cov-${k}`}>{v}</dd>
          </div>
        ))}
      </dl>
      <p className="text-sm" data-testid="cov-share">
        {share === null ? "No evaluated tracks in this scope." : <>Resolved share of evaluated tracks: <strong className="tabular-nums">{Math.round(share * 100)}%</strong> <span className="text-muted-foreground">(operational diagnostic)</span></>}
      </p>
      <ul className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground" data-testid="cov-breakdown">
        {Object.entries(c.unresolved_breakdown).map(([k, v]) => (
          <li key={k}>{k.replace(/_/g, " ")}: <span className="font-mono tabular-nums text-foreground">{v}</span></li>
        ))}
        <li>crops rejected by the quality gate: <span className="font-mono tabular-nums text-foreground" data-testid="cov-rejected">{c.face_crops_rejected_by_quality_gate}</span> of {c.face_crops_analysed}</li>
        {c.recognition_errors ? <li>recognition errors: <span className="font-mono text-foreground">{c.recognition_errors}</span></li> : null}
      </ul>
      <p className="flex items-start gap-2 rounded-md bg-muted px-3 py-2 text-xs text-muted-foreground" data-testid="cov-disclaimer">
        <Info className="mt-0.5 size-3.5 shrink-0" aria-hidden />
        {c.disclaimer}
      </p>
    </div>
  );
}

function ConsistencyPanel() {
  const [run, setRun] = useState(false);
  const q = useQuery({
    queryKey: ["analytics", "reconciliation"],
    queryFn: ({ signal }) => api.analyticsReconciliation(signal),
    enabled: run,
    staleTime: 0,
  });
  return (
    <Card className="mt-6" data-testid="consistency-panel">
      <CardHeader className="flex-row items-start justify-between gap-3">
        <div>
          <CardTitle>Data consistency</CardTitle>
          <CardDescription>
            Recomputes the global counts independently from the raw rows and reports differences. Read-only: nothing is ever modified.
          </CardDescription>
        </div>
        <Button type="button" size="sm" variant="outline" onClick={() => (run ? void q.refetch() : setRun(true))} disabled={q.isFetching} data-testid="consistency-run">
          <ShieldCheck className="size-4" aria-hidden />
          {q.isFetching ? "Checking…" : "Run check"}
        </Button>
      </CardHeader>
      <CardContent>
        {!run ? (
          <p className="text-sm text-muted-foreground">Not run yet.</p>
        ) : q.isError ? (
          <p role="alert" className="text-sm" data-testid="consistency-error">The check could not be run.</p>
        ) : q.data ? (
          <div className="space-y-2" data-testid="consistency-result" data-consistent={q.data.consistent}>
            <p className="text-sm font-medium">
              {q.data.consistent ? "All counts agree with the underlying records." : `${q.data.errors} check(s) failed.`}
              {q.data.warnings ? ` ${q.data.warnings} warning(s).` : ""}
            </p>
            <ul className="space-y-1 text-xs">
              {q.data.checks.filter((c) => !c.ok).map((c) => (
                <li key={c.name} className="rounded border px-2 py-1" data-testid="consistency-issue">
                  <strong>{c.severity}</strong> · {c.name.replace(/_/g, " ")} — expected {String(c.expected)}, found {String(c.actual)}. {c.detail}
                </li>
              ))}
            </ul>
          </div>
        ) : (
          <Skeleton className="h-8 w-1/2" />
        )}
      </CardContent>
    </Card>
  );
}
