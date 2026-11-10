import { useCallback } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { queryKeys, useHealth, useReadiness } from "./use-system";

export type ApiConnection = "checking" | "connected" | "disconnected";
export type DatabaseConnection = "checking" | "connected" | "unavailable" | "unknown";

export interface ConnectionState {
  api: ApiConnection;
  database: DatabaseConnection;
  /** Re-run both probes immediately. */
  recheck: () => Promise<void>;
}

/**
 * Derives connection state strictly from real probe results:
 *  - /health unreachable            -> API disconnected, database unknown (cannot be determined)
 *  - /health ok, /ready 503         -> API connected, database unavailable
 *  - /health ok, /ready 200         -> both connected
 */
export function useConnection(): ConnectionState {
  const health = useHealth();
  const readiness = useReadiness();
  const queryClient = useQueryClient();

  const recheck = useCallback(async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: queryKeys.health }),
      queryClient.invalidateQueries({ queryKey: queryKeys.readiness }),
      queryClient.invalidateQueries({ queryKey: queryKeys.status }),
    ]);
  }, [queryClient]);

  let api: ApiConnection;
  if (health.isError) api = "disconnected";
  else if (health.isSuccess) api = "connected";
  else api = "checking";

  let database: DatabaseConnection;
  if (api === "disconnected") database = "unknown";
  else if (readiness.isSuccess) {
    database = readiness.data.checks.database.status === "ok" ? "connected" : "unavailable";
  } else if (readiness.isError) database = "unknown";
  else database = "checking";

  return { api, database, recheck };
}
