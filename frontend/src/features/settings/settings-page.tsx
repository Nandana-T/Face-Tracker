import { RotateCcw } from "lucide-react";
import { useState } from "react";
import { ConfirmDialog } from "@/components/common/confirm-dialog";
import { ErrorState } from "@/components/common/error-state";
import { PageHeader } from "@/components/common/page-header";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { toast } from "@/components/ui/toaster";
import { useConfig } from "@/hooks/use-system";
import type { PublicConfigResponse } from "@/lib/api-types";
import { useUiStore } from "@/lib/ui-store";

function sections(c: PublicConfigResponse): [string, [string, string][]][] {
  return [
    [
      "Video",
      [
        ["Source type", c.video.source_type],
        ["Source", c.video.source_configured ? "Configured (hidden)" : "Not configured"],
        ["Reconnect interval", `${c.video.reconnect_interval_seconds}s`],
      ],
    ],
    [
      "Detection",
      [
        ["Frame skip", String(c.detection.frame_skip)],
        ["Confidence threshold", String(c.detection.confidence_threshold)],
      ],
    ],
    ["Recognition", [["Similarity threshold", String(c.recognition.similarity_threshold)]]],
    ["Tracking", [["Max missing", `${c.tracking.max_missing_seconds}s`]]],
    ["Logging", [["Level", c.logging.level]]],
  ];
}

export function SettingsPage() {
  const config = useConfig();
  const resetUi = useUiStore((s) => s.reset);
  const [confirmOpen, setConfirmOpen] = useState(false);

  return (
    <>
      <PageHeader
        title="Settings"
        description="Read-only view of the validated backend configuration. Edit config/config.json and restart the backend to change values."
      />

      <section aria-labelledby="runtime-config" className="space-y-4">
        <h2 id="runtime-config" className="text-sm font-semibold">
          Runtime configuration
        </h2>
        {config.isPending ? (
          <Skeleton className="h-40 w-full" />
        ) : config.isError ? (
          <ErrorState
            title="Configuration unavailable"
            message={config.error.message}
            onRetry={() => void config.refetch()}
          />
        ) : (
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3" data-testid="config-sections">
            {sections(config.data).map(([title, rows]) => (
              <Card key={title}>
                <CardHeader>
                  <CardTitle>{title}</CardTitle>
                </CardHeader>
                <CardContent>
                  <dl className="space-y-2 text-sm">
                    {rows.map(([k, v]) => (
                      <div key={k} className="flex justify-between gap-4">
                        <dt className="text-muted-foreground">{k}</dt>
                        <dd className="font-mono text-xs">{v}</dd>
                      </div>
                    ))}
                  </dl>
                </CardContent>
              </Card>
            ))}
          </div>
        )}
        <p className="text-xs text-muted-foreground">
          Thresholds are preliminary defaults, not validated production values.
        </p>
      </section>

      <Card>
        <CardHeader>
          <CardTitle>Interface preferences</CardTitle>
          <CardDescription>Theme and sidebar state are stored in this browser only.</CardDescription>
        </CardHeader>
        <CardContent>
          <Button variant="outline" onClick={() => setConfirmOpen(true)}>
            <RotateCcw /> Reset interface preferences
          </Button>
        </CardContent>
      </Card>

      <ConfirmDialog
        open={confirmOpen}
        onOpenChange={setConfirmOpen}
        title="Reset interface preferences?"
        description="This restores the default theme and sidebar layout in this browser."
        confirmLabel="Reset"
        destructive
        onConfirm={() => {
          resetUi();
          toast.success("Interface preferences reset");
        }}
      />
    </>
  );
}
