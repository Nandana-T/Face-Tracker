import { AlertTriangle, DoorClosed, DoorOpen, Hourglass } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { Link } from "react-router-dom";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import type { SessionOut } from "@/lib/api-types";
import { eventHeadline, visitorText } from "@/lib/visits";
import { formatDateTimeSeconds } from "@/features/visitors/format";
import type { ActiveVisit, RecentVisitEvent } from "./use-monitoring";

interface Props {
  session: SessionOut | null;
  activeVisits: ReadonlyMap<string, ActiveVisit>;
  recentEvents: RecentVisitEvent[];
}

/**
 * Visits of the current session: who is currently inside (open visits), the latest durable
 * entry/exit events and any persistence problem. Everything comes from server messages. Counts are
 * VISITS in this session - not a number of unique people.
 */
export function VisitActivityPanel({ session, activeVisits, recentEvents }: Props) {
  const running = !!session?.settings?.recognition_enabled;
  const open = [...activeVisits.values()];
  const inView = open.filter((v) => v.state === "ACTIVE").length;
  const waiting = open.filter((v) => v.state === "TEMPORARILY_MISSING").length;
  const problems = recentEvents.filter(
    (e) => e.persistence_status === "DEAD" || e.image_status === "MISSING",
  );
  return (
    <Card data-testid="visit-panel">
      <CardHeader>
        <CardTitle>Visits</CardTitle>
        <CardDescription>
          Entry and exit events of this session. A visit is one presence episode.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {!running ? (
          <p className="rounded-md border border-dashed px-3 py-2 text-xs text-muted-foreground" data-testid="visit-panel-off">
            Visits need face recognition: start a session with recognition available.
          </p>
        ) : null}
        <dl className="grid grid-cols-2 gap-2" aria-label="Visit status">
          <div className="rounded-md border p-2.5">
            <dt className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <DoorOpen className="size-4 text-teal-400" aria-hidden />
              In view now
            </dt>
            <dd className="mt-0.5 font-mono text-lg font-semibold tabular-nums" data-testid="visit-active-count">
              {running ? inView : "—"}
            </dd>
          </div>
          <div className="rounded-md border p-2.5">
            <dt className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <Hourglass className="size-4 text-amber-400" aria-hidden />
              Waiting
            </dt>
            <dd className="mt-0.5 font-mono text-lg font-semibold tabular-nums" data-testid="visit-waiting-count">
              {running ? waiting : "—"}
            </dd>
            <p className="text-[11px] text-muted-foreground">out of view, not yet exited</p>
          </div>
        </dl>

        {problems.length > 0 ? (
          <p role="alert" className="flex items-start gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs" data-testid="visit-problem">
            <AlertTriangle className="mt-0.5 size-4 shrink-0 text-amber-400" aria-hidden />
            {problems.length} recent event{problems.length === 1 ? " has" : "s have"} a recording problem (a
            missing image or a failed log delivery). The event itself is stored; see Event Logs.
          </p>
        ) : null}

        <section aria-label="Recent events">
          <h3 className="mb-1.5 text-xs font-medium text-muted-foreground">Recent events</h3>
          {recentEvents.length === 0 ? (
            <p className="text-xs text-muted-foreground" data-testid="visit-recent-empty">
              No entry or exit has been recorded in this session yet.
            </p>
          ) : (
            <ul className="space-y-1.5" data-testid="visit-recent">
              <AnimatePresence initial={false}>
                {recentEvents.map((e) => (
                  <motion.li
                    key={e.id}
                    layout="position"
                    initial={{ opacity: 0, y: -6 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0 }}
                    transition={{ duration: 0.18 }}
                    className="flex items-center justify-between gap-2 rounded-md border px-2.5 py-1.5 text-xs"
                    data-testid="visit-recent-item"
                  >
                    <span className="flex min-w-0 items-center gap-1.5">
                      {e.type === "entry" ? (
                        <DoorOpen className="size-3.5 shrink-0 text-teal-400" aria-hidden />
                      ) : (
                        <DoorClosed className="size-3.5 shrink-0 text-sky-400" aria-hidden />
                      )}
                      <span className="font-medium">{eventHeadline(e)}</span>
                      <span className="truncate font-mono">{visitorText(e.visitor)}</span>
                    </span>
                    <Link
                      to={`/events?visit=${e.visit_id}`}
                      className="shrink-0 tabular-nums text-muted-foreground underline-offset-4 hover:text-foreground hover:underline"
                      aria-label={`Open the visit of ${visitorText(e.visitor)}`}
                    >
                      {formatDateTimeSeconds(e.event_time).split(", ").pop()}
                    </Link>
                  </motion.li>
                ))}
              </AnimatePresence>
            </ul>
          )}
        </section>

        <Link to="/events" className="text-xs font-medium text-primary underline-offset-4 hover:underline" data-testid="open-events">
          Open Event Logs →
        </Link>
      </CardContent>
    </Card>
  );
}
