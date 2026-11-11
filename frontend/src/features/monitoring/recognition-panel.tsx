import { ScanFace, UserCheck, UserPlus } from "lucide-react";
import { motion } from "motion/react";
import { Link } from "react-router-dom";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import type { SessionOut, TrackIdentity } from "@/lib/api-types";
import { IDENTITY_STATE_LABEL } from "@/lib/identity";

interface Props {
  session: SessionOut | null;
  identities: ReadonlyMap<number, TrackIdentity>;
  registeredThisSession: number;
  recognisedTracks: number;
  /** Why recognition cannot run (server-reported), or null. */
  unavailableReason: string | null;
}

const fmt = (n: number | undefined) => (n === undefined ? "—" : n.toLocaleString());

/**
 * Recognition status for the current session. Counts come from the identity events the backend
 * sent (visitors) and the worker's counters - nothing is invented. Registered/recognised numbers
 * are for THIS session's tracks; they are not a unique-visitor count.
 */
export function RecognitionPanel({
  session,
  identities,
  registeredThisSession,
  recognisedTracks,
  unavailableReason,
}: Props) {
  const s = session?.stats;
  const active = !!session?.settings?.recognition_enabled;
  const pending = [...identities.values()].filter((v) =>
    ["MATCH_PENDING", "REGISTRATION_PENDING", "UNRESOLVED"].includes(v.state),
  ).length;
  const flagged = [...identities.values()].filter((v) =>
    ["AMBIGUOUS", "INSUFFICIENT_QUALITY", "REJECTED"].includes(v.state),
  ).length;
  return (
    <Card data-testid="recognition-panel">
      <CardHeader>
        <CardTitle>Recognition</CardTitle>
        <CardDescription>
          Visitor labels (V…) are persistent; track labels (#…) are temporary.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {unavailableReason ? (
          <p
            className="rounded-md border border-dashed px-3 py-2 text-xs text-muted-foreground"
            data-testid="recognition-unavailable"
          >
            {unavailableReason}
          </p>
        ) : null}
        <dl className="grid grid-cols-2 gap-2" aria-label="Recognition statistics">
          <Stat
            icon={<UserCheck className="size-4 text-violet-400" aria-hidden />}
            label="Recognized now"
            value={active ? fmt(recognisedTracks) : "—"}
            hint="tracks matched to a visitor"
            testId="rec-recognized"
          />
          <Stat
            icon={<UserPlus className="size-4 text-teal-400" aria-hidden />}
            label="Registered here"
            value={active ? fmt(registeredThisSession) : "—"}
            hint="new visitors this session"
            testId="rec-registered"
          />
          <Stat
            icon={<ScanFace className="size-4 text-muted-foreground" aria-hidden />}
            label="Undecided"
            value={active ? fmt(pending) : "—"}
            hint={`${IDENTITY_STATE_LABEL.MATCH_PENDING.toLowerCase()}…`}
            testId="rec-pending"
          />
          <Stat
            icon={<ScanFace className="size-4 text-amber-400" aria-hidden />}
            label="Flagged"
            value={active ? fmt(flagged) : "—"}
            hint="ambiguous / low quality"
            testId="rec-flagged"
          />
        </dl>
        {active && s?.recog_processed ? (
          <p className="text-xs text-muted-foreground" data-testid="rec-cost">
            {fmt(s.recog_processed)} faces analysed · {(s.recog_avg_ms ?? 0).toFixed(0)} ms each
            {s.recog_dropped ? ` · ${fmt(s.recog_dropped)} skipped (busy)` : ""}
          </p>
        ) : null}
        <motion.div layout>
          <Link
            to="/visitors"
            className="text-xs font-medium text-primary underline-offset-4 hover:underline"
            data-testid="open-visitors"
          >
            Open the visitor gallery →
          </Link>
        </motion.div>
      </CardContent>
    </Card>
  );
}

function Stat({
  icon,
  label,
  value,
  hint,
  testId,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
  hint: string;
  testId: string;
}) {
  return (
    <div className="rounded-md border p-2.5">
      <dt className="flex items-center gap-1.5 text-xs text-muted-foreground">
        {icon}
        {label}
      </dt>
      <dd className="mt-0.5 font-mono text-lg font-semibold tabular-nums" data-testid={testId}>
        {value}
      </dd>
      <p className="text-[11px] text-muted-foreground">{hint}</p>
    </div>
  );
}
