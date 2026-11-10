import { Menu, X } from "lucide-react";
import { useState } from "react";
import { NAV_ITEMS } from "@/app/navigation";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { Brand } from "./brand";
import { NavItemLink } from "./sidebar";

/** Mobile fallback: a slide-in navigation drawer (focus-trapped, Esc to close). */
export function MobileNav() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button
        variant="ghost"
        size="icon"
        className="md:hidden"
        aria-label="Open navigation"
        onClick={() => setOpen(true)}
      >
        <Menu />
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="left-0 top-0 h-full max-w-72 translate-x-0 translate-y-0 rounded-none rounded-r-xl">
          <DialogTitle className="sr-only">Navigation</DialogTitle>
          <DialogDescription className="sr-only">Site sections</DialogDescription>
          <div className="mb-6 flex items-center justify-between">
            <Brand />
            <Button variant="ghost" size="icon" aria-label="Close navigation" onClick={() => setOpen(false)}>
              <X />
            </Button>
          </div>
          <nav aria-label="Mobile navigation" className="flex flex-col gap-1">
            {NAV_ITEMS.map((item) => (
              <NavItemLink
                key={item.to}
                item={item}
                collapsed={false}
                onNavigate={() => setOpen(false)}
              />
            ))}
          </nav>
        </DialogContent>
      </Dialog>
    </>
  );
}
