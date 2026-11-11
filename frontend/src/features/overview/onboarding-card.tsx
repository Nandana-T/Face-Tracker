import { Check, Circle, Clock } from "lucide-react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { useConnection } from "@/hooks/use-connection";
import { useConfig, useSystemStatus } from "@/hooks/use-system";

type StepState = "done" | "todo" | "later";

interface Step {
  title: string;
  detail: string;
  state: StepState;
}

export function OnboardingCard() {
  const { api, database } = useConnection();
  const status = useSystemStatus();
  const config = useConfig();

  const steps: Step[] = [
    {
      title: "Connect the backend",
      detail:
        api === "connected"
          ? "The API is reachable."
          : "Start the API server (scripts\\start-backend.ps1).",
      state: api === "connected" ? "done" : "todo",
    },
    {
      title: "Connect the database",
      detail:
        database === "connected"
          ? status.data?.database.migrations_applied
            ? "PostgreSQL is reachable and migrations are applied."
            : "PostgreSQL is reachable. Run scripts\\migrate.ps1 to apply migrations."
          : "Set DATABASE_URL in .env, then run scripts\\migrate.ps1.",
      state: database === "connected" && status.data?.database.migrations_applied ? "done" : "todo",
    },
    {
      title: "Choose a video source",
      detail: config.data?.video.source_configured
        ? `A ${config.data.video.source_type} source is set in config/config.json.`
        : "Set video.source in config/config.json (file or RTSP).",
      state: config.data?.video.source_configured ? "done" : "todo",
    },
    {
      title: "Start processing",
      detail: "Open Live Monitoring, pick a source and press Start processing.",
      state: "todo",
    },
  ];

  return (
    <Card>
      <CardHeader>
        <CardTitle>Getting started</CardTitle>
        <CardDescription>Progress reflects the real state of this installation.</CardDescription>
      </CardHeader>
      <CardContent>
        <ol className="space-y-4">
          {steps.map((step) => (
            <li key={step.title} className="flex gap-3">
              <span className="mt-0.5">
                {step.state === "done" ? (
                  <Check className="size-4 text-success" aria-label="Done" />
                ) : step.state === "later" ? (
                  <Clock className="size-4 text-muted-foreground" aria-label="Planned" />
                ) : (
                  <Circle className="size-4 text-muted-foreground" aria-label="To do" />
                )}
              </span>
              <div>
                <div className="text-sm font-medium">{step.title}</div>
                <div className="text-xs text-muted-foreground">{step.detail}</div>
              </div>
            </li>
          ))}
        </ol>
      </CardContent>
    </Card>
  );
}
