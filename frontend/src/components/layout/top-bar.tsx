import { useLocation } from "react-router-dom";
import { titleForPath } from "@/app/navigation";
import { SignOutButton } from "@/features/auth/auth-gate";
import { Brand } from "./brand";
import { ConnectionIndicators } from "./connection-indicators";
import { MobileNav } from "./mobile-nav";
import { ThemeToggle } from "./theme-toggle";

export function TopBar() {
  const { pathname } = useLocation();
  return (
    <header className="sticky top-0 z-30 flex h-16 items-center gap-4 border-b bg-background/80 px-4 backdrop-blur md:px-8">
      <MobileNav />
      <div className="md:hidden">
        <Brand showText={false} />
      </div>
      <div className="text-sm font-medium" data-testid="page-title">
        {titleForPath(pathname)}
      </div>
      <div className="ml-auto flex items-center gap-4">
        <div className="hidden sm:block">
          <ConnectionIndicators />
        </div>
        <SignOutButton />
        <ThemeToggle />
      </div>
    </header>
  );
}
