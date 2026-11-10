import { ScanFace } from "lucide-react";

export function Brand({ showText = true }: { showText?: boolean }) {
  return (
    <div className="flex items-center gap-2">
      <div className="flex size-8 items-center justify-center rounded-md bg-primary/15 text-primary">
        <ScanFace className="size-5" aria-hidden />
      </div>
      {showText && (
        <div className="leading-tight">
          <div className="text-sm font-semibold tracking-tight">Katomaran AI</div>
          <div className="text-[11px] text-muted-foreground">Visitor Intelligence</div>
        </div>
      )}
    </div>
  );
}
