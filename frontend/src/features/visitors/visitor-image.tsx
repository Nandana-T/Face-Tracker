import { useQuery } from "@tanstack/react-query";
import { ImageOff, UserRound } from "lucide-react";
import { useEffect, useMemo } from "react";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

interface Props {
  visitorId: string;
  label: string;
  hasImage: boolean;
  className?: string;
}

/**
 * Representative face image. The file is behind the authenticated API (an <img src> cannot send the
 * bearer token), so it is fetched with the token and shown from a temporary object URL.
 * A visitor without an image, or an image that fails to load, shows a neutral placeholder.
 */
export function VisitorImage({ visitorId, label, hasImage, className }: Props) {
  const query = useQuery({
    queryKey: ["visitor-image", visitorId],
    queryFn: ({ signal }) => api.visitorImage(visitorId, signal),
    enabled: hasImage,
    staleTime: 5 * 60_000,
    retry: false,
  });
  const url = useMemo(() => (query.data ? URL.createObjectURL(query.data) : null), [query.data]);
  useEffect(() => {
    return () => {
      if (url) URL.revokeObjectURL(url);
    };
  }, [url]);

  const frame = cn("flex items-center justify-center overflow-hidden bg-muted text-muted-foreground", className);
  if (url) {
    return (
      <div className={frame}>
        <img src={url} alt={`Face of visitor ${label}`} className="size-full object-cover" />
      </div>
    );
  }
  if (hasImage && query.isPending) {
    return <div className={cn(frame, "animate-pulse")} data-testid="image-loading" aria-hidden />;
  }
  const failed = hasImage && query.isError;
  return (
    <div className={frame} data-testid={failed ? "image-missing" : "image-none"}>
      {failed ? (
        <span className="flex flex-col items-center gap-1 text-[11px]">
          <ImageOff className="size-6" aria-hidden />
          Image unavailable
        </span>
      ) : (
        <UserRound className="size-10" aria-label="No image stored" />
      )}
    </div>
  );
}
