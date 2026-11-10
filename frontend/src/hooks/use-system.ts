import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

export const POLL_INTERVAL_MS = 15_000;

export const queryKeys = {
  health: ["system", "health"] as const,
  readiness: ["system", "readiness"] as const,
  status: ["system", "status"] as const,
  config: ["system", "config"] as const,
};

export const useHealth = () =>
  useQuery({
    queryKey: queryKeys.health,
    queryFn: ({ signal }) => api.health(signal),
    refetchInterval: POLL_INTERVAL_MS,
  });

export const useReadiness = () =>
  useQuery({
    queryKey: queryKeys.readiness,
    queryFn: ({ signal }) => api.readiness(signal),
    refetchInterval: POLL_INTERVAL_MS,
  });

export const useSystemStatus = () =>
  useQuery({
    queryKey: queryKeys.status,
    queryFn: ({ signal }) => api.systemStatus(signal),
    refetchInterval: POLL_INTERVAL_MS,
  });

export const useConfig = () =>
  useQuery({
    queryKey: queryKeys.config,
    queryFn: ({ signal }) => api.config(signal),
  });
