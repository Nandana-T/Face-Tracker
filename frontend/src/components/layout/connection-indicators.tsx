import { StatusIndicator, type StatusTone } from "@/components/common/status-indicator";
import {
  useConnection,
  type ApiConnection,
  type DatabaseConnection,
} from "@/hooks/use-connection";

const apiView: Record<ApiConnection, { value: string; tone: StatusTone }> = {
  checking: { value: "Checking…", tone: "loading" },
  connected: { value: "Connected", tone: "ok" },
  disconnected: { value: "Disconnected", tone: "error" },
};

const dbView: Record<DatabaseConnection, { value: string; tone: StatusTone }> = {
  checking: { value: "Checking…", tone: "loading" },
  connected: { value: "Connected", tone: "ok" },
  unavailable: { value: "Unavailable", tone: "error" },
  unknown: { value: "Unknown", tone: "idle" },
};

export function ConnectionIndicators() {
  const { api, database } = useConnection();
  return (
    <div className="flex items-center gap-4" data-testid="connection-indicators">
      <StatusIndicator label="API" {...apiView[api]} />
      <StatusIndicator label="Database" {...dbView[database]} />
    </div>
  );
}
