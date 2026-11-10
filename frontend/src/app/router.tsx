import { createBrowserRouter, createMemoryRouter, type RouteObject } from "react-router-dom";
import { AppShell } from "@/components/layout/app-shell";
import { AuthGate } from "@/features/auth/auth-gate";
import { AnalyticsPage } from "@/features/analytics/analytics-page";
import { EventsPage } from "@/features/events/events-page";
import { MonitoringPage } from "@/features/monitoring/monitoring-page";
import { OverviewPage } from "@/features/overview/overview-page";
import { SettingsPage } from "@/features/settings/settings-page";
import { VisitorsPage } from "@/features/visitors/visitors-page";
import { NotFoundPage } from "@/routes/not-found";

export const routes: RouteObject[] = [
  {
    path: "/",
    element: (
      <AuthGate>
        <AppShell />
      </AuthGate>
    ),
    children: [
      { index: true, element: <OverviewPage /> },
      { path: "live", element: <MonitoringPage /> },
      { path: "visitors", element: <VisitorsPage /> },
      { path: "events", element: <EventsPage /> },
      { path: "analytics", element: <AnalyticsPage /> },
      { path: "settings", element: <SettingsPage /> },
      { path: "*", element: <NotFoundPage /> },
    ],
  },
];

export const createAppRouter = () => createBrowserRouter(routes);
export const createTestRouter = (initialEntries: string[]) =>
  createMemoryRouter(routes, { initialEntries });
