import { Users } from "lucide-react";
import { EmptyState } from "@/components/common/empty-state";
import { PageHeader } from "@/components/common/page-header";

export function VisitorsPage() {
  return (
    <>
      <PageHeader
        title="Visitors"
        description="Registered visitor identities and their visit history."
      />
      <EmptyState
        icon={Users}
        title="No visitors registered yet"
        description="Visitors are registered automatically when faces are first recognised. Visitor analytics will appear after processing begins."
      />
    </>
  );
}
