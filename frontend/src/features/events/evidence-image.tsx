import { useQuery } from "@tanstack/react-query";
import { Ban, ImageOff, UserRound } from "lucide-react";
import { useMemo } from "react";
import { api } from "@/lib/api";
import { objectUrl } from "@/lib/object-url";
import type { ImageStatus } from "@/lib/api-types";
import { IMAGE_STATUS_LABEL } from "@/lib/visits";
import { cn } from "@/lib/utils";

interface Props {
  eventId: string;
  status: ImageStatus;
  alt: string;
  className?: string;
}

/**
 * Entry/exit evidence crop. The file is behind the authenticated API, so it is fetched with the
 * bearer token and shown from a temporary object URL. When there is no image the reason is shown
 * truthfully (never captured / file missing / erased) - nothing is substituted.
 */
export function EvidenceImage({ eventId, status, alt, className }: Props) {
  const stored = status === "STORED";
  const query = useQuery({
    queryKey: ["event-image", eventId],
    queryFn: ({ signal }) => api.eventImage(eventId, signal),
    enabled: stored,
    staleTime: 5 * 60_000,
    retry: false,
  });
  const url = useMemo(() => (query.data ? objectUrl(query.data) : null), [query.data]);

  const frame = cn("flex items-center justify-center overflow-hidden bg-muted text-muted-foreground", className);
  if (url) {
    return (
      <div className={frame}>
        <img src={url} alt={alt} className="size-full object-cover" />
      </div>
    );
  }
  if (stored && query.isPending) return <div className={cn(frame, "animate-pulse")} data-testid="evidence-loading" aria-hidden />;
  const failed = stored && query.isError;
  const label = failed ? "Image unavailable" : IMAGE_STATUS_LABEL[status];
  const Icon = status === "ERASED" ? Ban : status === "STORED" || status === "MISSING" ? ImageOff : UserRound;
  return (
    <div className={frame} data-testid="evidence-none" title={label}>
      <span className="flex flex-col items-center gap-1 px-1 text-center text-[10px] leading-tight">
        <Icon className="size-5" aria-hidden />
        <span>{label}</span>
      </span>
    </div>
  );
}
