import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Trash2, X } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { Dialog as DialogPrimitive } from "radix-ui";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { toast } from "@/components/ui/toaster";
import { api } from "@/lib/api";
import { IDENTITY_STATE_LABEL } from "@/lib/identity";
import { formatDateTime } from "./format";
import { VisitorImage } from "./visitor-image";

interface Props {
  visitorId: string | null;
  onClose: () => void;
}

/** Visitor details: identity, timestamps, recognition history. Raw embeddings are never available here. */
export function VisitorDrawer({ visitorId, onClose }: Props) {
  const open = visitorId !== null;
  return (
    <DialogPrimitive.Root open={open} onOpenChange={(v) => !v && onClose()}>
      <AnimatePresence>
        {open ? (
          <DialogPrimitive.Portal forceMount>
            <DialogPrimitive.Overlay asChild forceMount>
              <motion.div
                className="fixed inset-0 z-40 bg-black/50"
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                transition={{ duration: 0.18 }}
              />
            </DialogPrimitive.Overlay>
            <DialogPrimitive.Content asChild forceMount aria-describedby="visitor-drawer-desc">
              <motion.aside
                className="fixed inset-y-0 right-0 z-50 flex w-full max-w-md flex-col overflow-y-auto border-l bg-card shadow-xl focus:outline-none"
                initial={{ x: "100%" }}
                animate={{ x: 0 }}
                exit={{ x: "100%" }}
                transition={{ type: "tween", duration: 0.24 }}
                data-testid="visitor-drawer"
              >
                <DrawerBody visitorId={visitorId} onClose={onClose} />
              </motion.aside>
            </DialogPrimitive.Content>
          </DialogPrimitive.Portal>
        ) : null}
      </AnimatePresence>
    </DialogPrimitive.Root>
  );
}

function DrawerBody({ visitorId, onClose }: { visitorId: string; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const detail = useQuery({
    queryKey: ["visitor", visitorId],
    queryFn: ({ signal }) => api.visitor(visitorId, signal),
    retry: false,
  });
  const erase = useMutation({
    mutationFn: () => api.deleteVisitor(visitorId),
    onSuccess: () => {
      toast.success("Visitor erased", { description: "Embeddings, image and history were deleted." });
      void queryClient.invalidateQueries({ queryKey: ["visitors"] });
      setConfirming(false);
      onClose();
    },
    onError: () => toast.error("Could not erase the visitor - try again"),
  });
  const v = detail.data;

  return (
    <>
      <header className="flex items-start justify-between gap-3 border-b p-4">
        <div className="min-w-0">
          <DialogPrimitive.Title className="font-mono text-lg font-semibold" data-testid="drawer-title">
            {v ? v.label : "Visitor"}
          </DialogPrimitive.Title>
          <DialogPrimitive.Description id="visitor-drawer-desc" className="text-xs text-muted-foreground">
            Persistent identity. Track numbers below are temporary and belong to one session.
          </DialogPrimitive.Description>
        </div>
        <DialogClose asChild>
          <Button type="button" variant="ghost" size="icon" aria-label="Close visitor details" data-testid="drawer-close">
            <X className="size-4" aria-hidden />
          </Button>
        </DialogClose>
      </header>

      {detail.isPending ? (
        <div className="space-y-3 p-4" data-testid="drawer-loading">
          <Skeleton className="aspect-square w-full" />
          <Skeleton className="h-4 w-1/2" />
          <Skeleton className="h-4 w-2/3" />
        </div>
      ) : detail.isError || !v ? (
        <div className="space-y-3 p-4 text-sm" role="alert" data-testid="drawer-error">
          <p className="flex items-center gap-2 font-medium">
            <AlertTriangle className="size-4 text-amber-500" aria-hidden />
            This visitor could not be loaded.
          </p>
          <p className="text-muted-foreground">It may have been erased, or the API is unreachable.</p>
          <Button type="button" variant="outline" size="sm" onClick={() => void detail.refetch()}>
            Retry
          </Button>
        </div>
      ) : (
        <div className="space-y-4 p-4">
          <VisitorImage
            visitorId={v.id}
            label={v.label}
            hasImage={v.has_image}
            className="aspect-square w-full rounded-lg"
          />
          <dl className="space-y-1.5 text-sm" data-testid="drawer-facts">
            <Fact label="Visitor ID" value={v.label} />
            <Fact label="Registered" value={formatDateTime(v.first_seen_at)} testId="drawer-first" />
            <Fact label="Last recognized" value={formatDateTime(v.last_seen_at)} testId="drawer-last" />
            <Fact label="Recognized on" value={`${v.recognition_count} track${v.recognition_count === 1 ? "" : "s"}`} />
            <Fact label="Seen in sessions" value={String(v.sessions_count)} testId="drawer-sessions" />
            <Fact label="Embedding model" value={v.model_version} />
            {v.image_quality !== null ? (
              <Fact label="Image quality" value={`${Math.round(v.image_quality * 100)}%`} />
            ) : null}
          </dl>

          <section aria-labelledby="history-title">
            <h3 id="history-title" className="mb-1.5 text-sm font-medium">
              Recognition history
            </h3>
            {v.associations.length === 0 ? (
              <p className="text-xs text-muted-foreground" data-testid="history-empty">
                No tracks have been associated with this visitor yet.
              </p>
            ) : (
              <ul className="space-y-1.5" data-testid="history-list">
                {v.associations.map((a) => (
                  <li
                    key={`${a.session_id}-${a.track_id}`}
                    className="rounded-md border px-3 py-2 text-xs"
                    data-testid="history-item"
                  >
                    <div className="flex justify-between gap-2">
                      <span className="font-medium">{a.source_label ?? "Processing session"}</span>
                      <span className="text-muted-foreground">{formatDateTime(a.last_seen_at)}</span>
                    </div>
                    <div className="mt-0.5 text-muted-foreground">
                      Track #{a.track_id} (session-only) ·{" "}
                      {a.registered_in_session ? "registered here" : IDENTITY_STATE_LABEL[a.state].toLowerCase()}
                      {a.best_similarity !== null ? ` · similarity ${a.best_similarity.toFixed(2)}` : ""}
                      {a.conflicts ? ` · ${a.conflicts} conflicting observation${a.conflicts === 1 ? "" : "s"}` : ""}
                    </div>
                  </li>
                ))}
              </ul>
            )}
            <p className="mt-2 text-[11px] text-muted-foreground">
              This is recognition history, not a visit log: entry/exit events and visit counts are not
              part of this release.
            </p>
          </section>

          <Button
            type="button"
            variant="destructive"
            size="sm"
            onClick={() => setConfirming(true)}
            data-testid="drawer-erase"
          >
            <Trash2 className="size-4" aria-hidden />
            Erase this visitor
          </Button>
        </div>
      )}

      <Dialog open={confirming} onOpenChange={setConfirming}>
        <DialogContent>
          <DialogTitle className="text-base font-semibold">Erase {v?.label ?? "visitor"}?</DialogTitle>
          <DialogDescription className="mt-2 text-sm text-muted-foreground">
            This permanently deletes the stored face image, the face embeddings and the recognition
            history of this visitor. If the same person appears again they will be registered as a new
            visitor. This cannot be undone.
          </DialogDescription>
          <div className="mt-5 flex justify-end gap-2">
            <Button type="button" variant="outline" size="sm" onClick={() => setConfirming(false)} data-testid="erase-cancel">
              Cancel
            </Button>
            <Button
              type="button"
              variant="destructive"
              size="sm"
              disabled={erase.isPending}
              onClick={() => erase.mutate()}
              data-testid="erase-confirm"
            >
              {erase.isPending ? "Erasing…" : "Erase permanently"}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}

function Fact({ label, value, testId }: { label: string; value: string; testId?: string }) {
  return (
    <div className="flex justify-between gap-4 border-b pb-1">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="text-right font-medium tabular-nums" data-testid={testId}>
        {value}
      </dd>
    </div>
  );
}
