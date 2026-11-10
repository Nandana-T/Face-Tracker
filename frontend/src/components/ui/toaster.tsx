import { Toaster as Sonner } from "sonner";
import { useUiStore } from "@/lib/ui-store";

export { toast } from "sonner";

export function Toaster() {
  const theme = useUiStore((s) => s.theme);
  return <Sonner theme={theme} position="bottom-right" closeButton richColors />;
}
