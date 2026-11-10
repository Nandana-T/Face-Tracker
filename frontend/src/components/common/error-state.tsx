import { AlertTriangle, RefreshCw } from "lucide-react";
import type { ReactNode } from "react";
import { Button } from "@/components/ui/button";

interface ErrorStateProps {
  title: string;
  message: string;
  onRetry?: () => void;
  children?: ReactNode;
}

export function ErrorState({ title, message, onRetry, children }: ErrorStateProps) {
  return (
    <div
      role="alert"
      className="flex flex-col gap-3 rounded-lg border border-destructive/40 bg-destructive/5 p-6"
    >
      <div className="flex items-center gap-2 text-destructive">
        <AlertTriangle className="size-4" aria-hidden />
        <h2 className="text-sm font-semibold">{title}</h2>
      </div>
      <p className="text-sm text-muted-foreground">{message}</p>
      {children}
      {onRetry && (
        <Button variant="outline" size="sm" className="self-start" onClick={onRetry}>
          <RefreshCw /> Retry
        </Button>
      )}
    </div>
  );
}
