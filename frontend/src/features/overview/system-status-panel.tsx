import { RefreshCw } from "lucide-react";
import { useState } from "react";
import { ErrorState } from "@/components/common/error-state";
import { StatusIndicator, type StatusTone } from "@/components/common/status-indicator";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { toast } from "@/components/ui/toaster";
import { Skeleton } from "@/components/ui/skeleton";
import { useConnection } from "@/hooks/use-connection";
import { useSystemStatus } from "@/hooks/use-system";
import { API_BASE_URL } from "@/lib/api";
import type { ComponentStatus } from "@/lib/api-types";

const componentLabel: Record<ComponentStatus["state"], { value: string; tone: StatusTone }> = {
  not_implemented: { value: "Not implemented yet", tone: "idle" },
  not_configured: { value: "Not configured", tone: "warn" },
  not_running: { value: "Not running", tone: "idle" },
  ready: { value: "Ready", tone: "ok" },
  active: { value: "Processing", tone: "ok" },
  unavailable: { value: "Unavailable", tone: "error" },
};

export function SystemStatusPanel() {
  const connection = useConnection();
  const status = useSystemStatus();
  const [checking, setChecking] = useState(false);

  async function recheck() {
    setChecking(true);
    try {
      await connection.recheck();
      toast.success("Status refreshed");
    } finally {
      setChecking(false);
    }
  }

  const refresh = (
    <Button variant="outline" size="sm" onClick={recheck} disabled={checking}>
      <RefreshCw className={checking ? "animate-spin" : undefined} /> Check again
    </Button>
  );

  let body;
  if (connection.api === "disconnected") {
    body = (
      <ErrorState
        title="Backend not reachable"
        message={`The dashboard cannot reach the API at ${API_BASE_URL}. Start it with "scripts\\start-backend.ps1" (or see the README), then check again.`}
      />
    );
  } else if (status.isPending) {
    body = (
      <div className="space-y-2" aria-busy>
        <Skeleton className="h-6 w-full" />
        <Skeleton className="h-6 w-full" />
        <Skeleton className="h-6 w-2/3" />
      </div>
    );
  } else if (status.isError) {
    body = (
      <ErrorState
        title="Could not load system status"
        message={status.error.message}
        onRetry={() => void status.refetch()}
      />
    );
  } else {
    const s = status.data;
    const dbTone: StatusTone = s.database.status === "ok" ? "ok" : "error";
    body = (
      <dl className="grid gap-x-8 gap-y-3 sm:grid-cols-2" data-testid="system-status">
        <Row label="Service">{`${s.service} v${s.version} (${s.environment})`}</Row>
        <Row label="Uptime">{formatUptime(s.uptime_seconds)}</Row>
        <Row label="Database">
          <StatusIndicator
            label=""
            value={s.database.status === "ok" ? "Reachable" : "Unavailable"}
            tone={dbTone}
          />
        </Row>
        <Row label="Migrations">
          {s.database.migrations_applied
            ? `Applied (revision ${s.database.migration_revision})`
            : s.database.status === "ok"
              ? "Not applied"
              : "Unknown"}
        </Row>
        {(
          [
            ["Video worker", s.components.video_worker],
            ["Face detector", s.components.face_detector],
            ["Face recognizer", s.components.face_recognizer],
            ["Tracker", s.components.tracker],
          ] as const
        ).map(([label, c]) => (
          <Row key={label} label={label}>
            <StatusIndicator label="" {...componentLabel[c.state]} />
            <span className="text-xs text-muted-foreground">{c.detail}</span>
          </Row>
        ))}
        <Row label="Active video sources">{String(s.active_video_sources)}</Row>
      </dl>
    );
  }

  return (
    <Card>
      <CardHeader className="flex-row items-start justify-between gap-4">
        <div className="space-y-1">
          <CardTitle>System status</CardTitle>
          <CardDescription>Live values reported by the backend.</CardDescription>
        </div>
        {refresh}
      </CardHeader>
      <CardContent>{body}</CardContent>
    </Card>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-4 border-b pb-2 text-sm">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="flex items-center gap-2 text-right font-medium">{children}</dd>
    </div>
  );
}

export function formatUptime(seconds: number): string {
  const s = Math.floor(seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s % 60}s`;
  return `${s}s`;
}
