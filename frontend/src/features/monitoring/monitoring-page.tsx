import { Video } from "lucide-react";
import { EmptyState } from "@/components/common/empty-state";
import { PageHeader } from "@/components/common/page-header";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useConfig } from "@/hooks/use-system";

export function MonitoringPage() {
  const config = useConfig();
  return (
    <>
      <PageHeader
        title="Live Monitoring"
        description="Watch detections and tracked visitors in real time."
      />
      <div className="grid gap-6 xl:grid-cols-3">
        <Card className="xl:col-span-2">
          <CardContent className="p-6">
            <EmptyState
              icon={Video}
              title="No video source connected."
              description="Live video with face detection overlays will appear here once ingestion is available (Phase 1)."
              className="aspect-video"
            />
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Configured source</CardTitle>
            <CardDescription>From config/config.json.</CardDescription>
          </CardHeader>
          <CardContent className="text-sm">
            {config.isPending ? (
              <Skeleton className="h-6 w-full" />
            ) : config.isError ? (
              <p className="text-muted-foreground">Configuration unavailable: {config.error.message}</p>
            ) : (
              <dl className="space-y-2">
                <div className="flex justify-between">
                  <dt className="text-muted-foreground">Type</dt>
                  <dd className="font-medium">{config.data.video.source_type}</dd>
                </div>
                <div className="flex justify-between">
                  <dt className="text-muted-foreground">Source</dt>
                  <dd className="font-medium">
                    {config.data.video.source_configured ? "Configured" : "Not configured"}
                  </dd>
                </div>
              </dl>
            )}
          </CardContent>
        </Card>
      </div>
    </>
  );
}
