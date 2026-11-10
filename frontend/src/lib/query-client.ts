import { QueryClient } from "@tanstack/react-query";
import { ApiError } from "./api";

/** Retry transient failures once; never retry definitive 4xx answers. */
export function shouldRetry(failureCount: number, error: unknown): boolean {
  if (error instanceof ApiError && error.kind === "http" && (error.status ?? 500) < 500) {
    return false;
  }
  return failureCount < 1;
}

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: shouldRetry,
        retryDelay: (attempt) => Math.min(1000 * 2 ** attempt, 4000),
        staleTime: 5_000,
        refetchOnWindowFocus: true,
      },
    },
  });
}
