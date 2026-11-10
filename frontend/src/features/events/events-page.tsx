import { ListChecks } from "lucide-react";
import { EmptyState } from "@/components/common/empty-state";
import { PageHeader } from "@/components/common/page-header";

export function EventsPage() {
  return (
    <>
      <PageHeader title="Event Logs" description="Entry and exit events with captured face images." />
      <EmptyState
        icon={ListChecks}
        title="No events recorded"
        description="Entry and exit events will be listed here after processing begins."
      />
    </>
  );
}
