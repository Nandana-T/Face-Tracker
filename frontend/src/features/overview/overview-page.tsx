import { Activity, LogIn, LogOut, ScanFace, Users, Video } from "lucide-react";
import { EmptyState } from "@/components/common/empty-state";
import { MetricCard } from "@/components/common/metric-card";
import { PageHeader } from "@/components/common/page-header";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { OnboardingCard } from "./onboarding-card";
import { SystemStatusPanel } from "./system-status-panel";

const METRIC_HINT = "Available after processing begins";

export function OverviewPage() {
  return (
    <>
      <PageHeader
        title="Welcome to Katomaran AI"
        description="Detect, recognise and count unique visitors from video streams. Live figures appear here once video processing is running."
      />

      <section aria-label="Key metrics" className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <MetricCard index={0} label="Unique visitors" value={null} hint={METRIC_HINT} icon={Users} />
        <MetricCard index={1} label="Currently present" value={null} hint={METRIC_HINT} icon={ScanFace} />
        <MetricCard index={2} label="Entries" value={null} hint={METRIC_HINT} icon={LogIn} />
        <MetricCard index={3} label="Exits" value={null} hint={METRIC_HINT} icon={LogOut} />
      </section>

      <div className="grid gap-6 xl:grid-cols-3">
        <Card className="xl:col-span-2">
          <CardHeader>
            <CardTitle>Monitoring preview</CardTitle>
            <CardDescription>The live annotated feed will be shown here.</CardDescription>
          </CardHeader>
          <CardContent>
            <EmptyState
              icon={Video}
              title="No video source connected."
              description="Configure a video file or RTSP stream to preview detections here."
              className="aspect-video"
            />
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Recent activity</CardTitle>
            <CardDescription>The latest entries and exits are listed on the Event Logs page.</CardDescription>
          </CardHeader>
          <CardContent>
            <EmptyState
              icon={Activity}
              title="No activity yet"
              description="Visitor analytics will appear after processing begins."
            />
          </CardContent>
        </Card>
      </div>

      <div className="grid gap-6 xl:grid-cols-3">
        <div className="xl:col-span-2">
          <SystemStatusPanel />
        </div>
        <OnboardingCard />
      </div>
    </>
  );
}
