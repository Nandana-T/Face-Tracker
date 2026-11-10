import { Loader2, Play, RotateCcw, Square, Film, Camera } from "lucide-react";
import { motion } from "motion/react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import type { SourceOut } from "@/lib/api-types";
import { cn } from "@/lib/utils";
import { sourceTypeLabel } from "./status-copy";

interface Props {
  sources: SourceOut[];
  selectedId: string | null;
  onSelect(id: string): void;
  /** Source cannot be changed while a session is active. */
  locked: boolean;
  active: boolean;
  canStart: boolean;
  startBlockedReason: string | null;
  showRetry: boolean;
  starting: boolean;
  stopping: boolean;
  onStart(): void;
  onStop(): void;
}

export function SourceControls(p: Props) {
  const reasonId = "start-blocked-reason";
  return (
    <Card>
      <CardHeader>
        <CardTitle>Video source</CardTitle>
        <CardDescription>Sources are configured on the server.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {p.sources.length === 0 ? (
          <p className="text-sm text-muted-foreground">No sources are configured.</p>
        ) : (
          <div role="radiogroup" aria-label="Video source" className="space-y-2">
            {p.sources.map((s) => {
              const selected = s.id === p.selectedId;
              const disabled = p.locked || s.availability !== "available";
              const Icon = s.type === "file" ? Film : Camera;
              return (
                <label
                  key={s.id}
                  className={cn(
                    "relative flex cursor-pointer items-start gap-3 rounded-md border p-3 text-sm transition-colors",
                    "focus-within:ring-2 focus-within:ring-primary",
                    selected ? "border-primary bg-primary/5" : "hover:bg-muted",
                    disabled && !selected && "cursor-not-allowed opacity-60",
                  )}
                >
                  <input
                    type="radio"
                    name="video-source"
                    className="sr-only"
                    checked={selected}
                    disabled={disabled}
                    onChange={() => p.onSelect(s.id)}
                    aria-label={`${s.label} (${sourceTypeLabel(s.type)})`}
                  />
                  {selected && (
                    <motion.span
                      layoutId="source-selection"
                      className="absolute inset-y-2 left-0 w-1 rounded-r bg-primary"
                      aria-hidden
                    />
                  )}
                  <Icon className="mt-0.5 size-4 shrink-0 text-primary" aria-hidden />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate font-medium">{s.label}</span>
                    <span className="block text-xs text-muted-foreground">
                      {sourceTypeLabel(s.type)}
                      {s.availability !== "available" && ` - ${s.detail ?? "unavailable"}`}
                    </span>
                  </span>
                </label>
              );
            })}
          </div>
        )}

        <div className="flex flex-wrap gap-2">
          {p.active ? (
            <Button
              variant="destructive"
              onClick={p.onStop}
              disabled={p.stopping}
              data-testid="stop-button"
            >
              {p.stopping ? <Loader2 className="animate-spin" /> : <Square />} Stop processing
            </Button>
          ) : (
            <>
              <Button
                onClick={p.onStart}
                disabled={!p.canStart || p.starting}
                aria-describedby={p.startBlockedReason ? reasonId : undefined}
                data-testid="start-button"
              >
                {p.starting ? <Loader2 className="animate-spin" /> : <Play />} Start processing
              </Button>
              {p.showRetry && (
                <Button
                  variant="outline"
                  onClick={p.onStart}
                  disabled={!p.canStart || p.starting}
                  data-testid="retry-button"
                >
                  <RotateCcw /> Retry
                </Button>
              )}
            </>
          )}
        </div>
        {!p.active && p.startBlockedReason && (
          <p id={reasonId} className="text-xs text-muted-foreground">
            {p.startBlockedReason}
          </p>
        )}
        {p.active && (
          <p className="text-xs text-muted-foreground">
            Leaving this page does not stop processing; use Stop when you are done.
          </p>
        )}
      </CardContent>
    </Card>
  );
}
