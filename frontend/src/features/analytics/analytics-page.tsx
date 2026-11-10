import { BarChart3 } from "lucide-react";
import { EmptyState } from "@/components/common/empty-state";
import { PageHeader } from "@/components/common/page-header";

export function AnalyticsPage() {
  return (
    <>
      <PageHeader title="Analytics" description="Visitor trends and traffic patterns over time." />
      <EmptyState
        icon={BarChart3}
        title="No analytics available"
        description="Visitor analytics will appear after processing begins."
      />
    </>
  );
}
