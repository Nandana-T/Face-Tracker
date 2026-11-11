import { useQuery } from "@tanstack/react-query";
import { Activity, Eye, LogIn, LogOut, Repeat, ScanFace, Users, Video } from "lucide-react";
import { Link } from "react-router-dom";
import { MetricTile } from "@/features/analytics/analytics-metrics";
import { analyticsKeys, POLL_MS, useAnalyticsLive, useOverview } from "@/features/analytics/use-analytics";
import { formatDateTimeSeconds } from "@/features/visitors/format";
import { api } from "@/lib/api";
import { eventHeadline, visitorText } from "@/lib/visits";
import { EmptyState } from "@/components/common/empty-state";
import { MetricCard } from "@/components/common/metric-card";
import { PageHeader } from "@/components/common/page-header";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { OnboardingCard } from "./onboarding-card";
import { SystemStatusPanel } from "./system-status-panel";

const METRIC_HINT = "Available after processing begins";
const ALL_TIME = {};

export function OverviewPage() {
  useAnalyticsLive();
  const overview = useOverview(ALL_TIME);
  const recent = useQuery({
    queryKey: analyticsKeys.recent(ALL_TIME),
    queryFn: ({ signal }) => api.analyticsRecent(ALL_TIME, 5, signal),
    refetchInterval: POLL_MS,
  });
  const o = overview.data;
  const exits = o ? o.terminal_by_basis.absence + o.terminal_by_basis.observation_ended : null;
  const hint = (text: string) => (o ? text : overview.isError ? "Analytics unavailable" : METRIC_HINT);
  return (
    <>
      <PageHeader
        title="Welcome to Katomaran AI"
        description="Detect, recognise and count unique visitors from video streams. Live figures appear here once video processing is running."
      />

      <section aria-label="Key metrics" className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <MetricCard index={0} label="Unique visitors" value={o?.registered_visitors ?? null} hint={hint("Distinct registered identities, not a count of people")} icon={Users} />
        <MetricCard index={1} label="Currently present" value={o?.active_visits ?? null} hint={hint("Open visits of the live session")} icon={ScanFace} />
        <MetricCard index={2} label="Entries" value={o?.entry_events ?? null} hint={hint("One per visit")} icon={LogIn} />
        <MetricCard index={3} label="Exits" value={exits} hint={hint(o ? `${o.terminal_by_basis.interrupted} interrupted not included; not a verified departure` : "")} icon={LogOut} />
      </section>

      {o ? (
        <section aria-label="More figures" className="mt-4 grid gap-4 sm:grid-cols-3" data-testid="overview-more">
          <MetricTile name="visits" label="Visits" value={o.visits} scope="global" hint="A visitor can have several" icon={Eye} />
          <MetricTile name="returning" label="Returning visitors" value={o.returning_visitors_in_scope} scope="global" hint="Seen in a later session than their registration" icon={Repeat} />
          <MetricTile name="unresolved" label="Unresolved tracks" value={o.unresolved_tracks} scope="diagnostic" hint="Faces never reliably recognised; not extra people" icon={Activity} />
        </section>
      ) : null}
      {overview.isError && !o ? (
        <p role="alert" className="mt-3 text-xs text-muted-foreground" data-testid="overview-analytics-error">
          Analytics could not be loaded. No numbers are shown rather than invented ones.
        </p>
      ) : null}

      <div className="grid gap-6 xl:grid-cols-3">
        <Card className="xl:col-span-2">
          <CardHeader>
            <CardTitle>Monitoring preview</CardTitle>
            <CardDescription>The live annotated feed will be shown here.</CardDescription>
          </CardHeader>
          <CardContent>
            <EmptyState
              icon={Video}
              title="No video source connected."
              description="Configure a video file or RTSP stream to preview detections here."
              className="aspect-video"
            />
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Recent activity</CardTitle>
            <CardDescription>The latest entries and exits are listed on the Event Logs page.</CardDescription>
          </CardHeader>
          <CardContent>
            {(recent.data?.items.length ?? 0) > 0 ? (
              <ul className="space-y-1.5" data-testid="overview-recent">
                {recent.data!.items.map((e) => (
                  <li key={e.id} className="flex items-center justify-between gap-2 rounded-md border px-2.5 py-1.5 text-xs">
                    <span><strong>{eventHeadline(e)}</strong> <span className="font-mono">{visitorText(e.visitor)}</span></span>
                    <Link to={`/events?visit=${e.visit_id}`} className="tabular-nums text-muted-foreground underline-offset-4 hover:text-foreground hover:underline">
                      {formatDateTimeSeconds(e.event_time).split(", ").pop()}
                    </Link>
                  </li>
                ))}
              </ul>
            ) : (
              <EmptyState
                icon={Activity}
                title="No activity yet"
                description="Visitor analytics will appear after processing begins."
              />
            )}
          </CardContent>
        </Card>
      </div>

      <div className="grid gap-6 xl:grid-cols-3">
        <div className="xl:col-span-2">
          <SystemStatusPanel />
        </div>
        <OnboardingCard />
      </div>
    </>
  );
}
