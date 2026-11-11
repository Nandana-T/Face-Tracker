import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState, type RefObject } from "react";
import { toast } from "@/components/ui/toaster";
import { ApiError, api, streamUrl } from "@/lib/api";
import {
  ACTIVE_STATES,
  type FrameHeader,
  type IdentityEventMessage,
  type SessionOut,
  type SessionStats,
  type Track,
  type TrackIdentity,
  type VisitEventMessage,
  type VisitState,
  type VisitStateMessage,
  type VisitsSnapshotMessage,
} from "@/lib/api-types";
import { noticeFor, noticeKey } from "@/lib/identity";
import { LiveStream, type LinkState } from "@/lib/live-stream";
import { visitNotice } from "@/lib/visits";
import type { OverlayOptions, VideoCanvasHandle } from "./video-canvas";

const SOURCES_POLL_MS = 5000;
const SESSION_POLL_MS = 2000;
const FRAME_SNAPSHOT_MS = 400;

export interface LatestFrame {
  frameId: number;
  timestampMs: number;
  width: number;
  height: number;
  faces: number;
  bestConfidence: number | null;
  inferenceMs: number;
}

export interface SelectedTrack {
  track: Track;
  /** The track is no longer in the latest frame (removed or the session ended). */
  ended: boolean;
}

/** A visit that is currently open in this session (from the server; never inferred from tracks). */
export interface ActiveVisit {
  visitId: string;
  visitorLabel: string;
  state: VisitState;
}

/** The most recent durable events of this session, newest first. */
export type RecentVisitEvent = VisitEventMessage["event"];
export const RECENT_EVENTS_LIMIT = 8;

export function isActive(session: SessionOut | null): boolean {
  return session !== null && ACTIVE_STATES.includes(session.state);
}

/**
 * All state shown on the monitoring page originates from the backend: `session` is only ever
 * replaced by a server response, a server-pushed stream message, or a server poll - never by a
 * guess. The canvas is drawn imperatively so frames never cause React renders.
 */
export function useMonitoring(canvas: RefObject<VideoCanvasHandle | null>) {
  const stream = useRef<LiveStream | null>(null);
  const lastHeader = useRef<FrameHeader | null>(null);
  const arrivals = useRef<number[]>([]);

  const [session, setSession] = useState<SessionOut | null>(null);
  const [link, setLink] = useState<LinkState>("closed");
  const [latest, setLatest] = useState<LatestFrame | null>(null);
  const [streamFps, setStreamFps] = useState<number | null>(null);
  const [startError, setStartError] = useState<ApiError | null>(null);
  const [tracks, setTracks] = useState<Track[]>([]);
  const [trackingOn, setTrackingOn] = useState(false);
  const [showOverlay, setShowOverlayState] = useState(true);
  const [showLabels, setShowLabelsState] = useState(true);
  const overlayRef = useRef<OverlayOptions>({ show: true, labels: true, selectedId: null });
  const [selected, setSelected] = useState<SelectedTrack | null>(null);
  const selectedId = useRef<number | null>(null);
  // Recognition results of THIS session only (track ids restart in every session).
  const identityMap = useRef(new Map<number, TrackIdentity>());
  const noticed = useRef(new Set<string>());
  const [identities, setIdentities] = useState<ReadonlyMap<number, TrackIdentity>>(new Map());
  const [registeredThisSession, setRegisteredThisSession] = useState(0);
  const queryClient = useQueryClient();
  // Phase 4: durable visit events of THIS session. Notifications are deduplicated by event id.
  const seenEvents = useRef(new Set<string>());
  const [recentEvents, setRecentEvents] = useState<RecentVisitEvent[]>([]);
  const [activeVisits, setActiveVisits] = useState<ReadonlyMap<string, ActiveVisit>>(new Map());
  const [recognisedTracks, setRecognisedTracks] = useState(0);

  const sources = useQuery({
    queryKey: ["processing", "sources"],
    queryFn: ({ signal }) => api.sources(signal),
    refetchInterval: SOURCES_POLL_MS,
  });

  const detach = useCallback(() => {
    stream.current?.close();
    stream.current = null;
    setLink("closed");
  }, []);

  const applySession = useCallback((next: SessionOut) => {
    setSession((prev) => (prev && prev.id !== next.id && isActive(prev) ? prev : next));
  }, []);

  const resetVisits = useCallback(() => {
    seenEvents.current = new Set();
    setRecentEvents([]);
    setActiveVisits(new Map());
  }, []);

  const applyVisitEvent = useCallback(
    (id: string, message: VisitEventMessage) => {
      const e = message.event;
      if (e.session_id !== id) return; // never apply another session's events
      if (seenEvents.current.has(e.id)) return; // a replay / duplicate message: no second notification
      seenEvents.current.add(e.id);
      setRecentEvents((prev) => [e, ...prev].slice(0, RECENT_EVENTS_LIMIT));
      setActiveVisits((prev) => {
        const next = new Map(prev);
        if (e.type === "entry") {
          next.set(e.visit_id, {
            visitId: e.visit_id,
            visitorLabel: e.visitor?.label ?? "Erased visitor",
            state: "ACTIVE",
          });
        } else {
          next.delete(e.visit_id);
        }
        return next;
      });
      const notice = visitNotice(message);
      toast[notice.level === "info" ? "message" : notice.level](notice.title, {
        description: notice.description,
        id: `visit-event:${e.id}`,
      });
      void queryClient.invalidateQueries({ queryKey: ["events"] });
      void queryClient.invalidateQueries({ queryKey: ["visit", e.visit_id] });
    },
    [queryClient],
  );

  const applyVisitState = useCallback((message: VisitStateMessage) => {
    setActiveVisits((prev) => {
      const current = prev.get(message.visit_id);
      if (!current || current.state === message.state) return prev;
      return new Map(prev).set(message.visit_id, { ...current, state: message.state });
    });
  }, []);

  const applyVisitsSnapshot = useCallback((message: VisitsSnapshotMessage) => {
    const next = new Map<string, ActiveVisit>();
    for (const v of message.visits) {
      next.set(v.visit_id, {
        visitId: v.visit_id,
        visitorLabel: `V${String(v.visitor_number).padStart(5, "0")}`,
        state: v.state,
      });
    }
    setActiveVisits(next);
  }, []);

  const resetIdentities = useCallback(() => {
    identityMap.current = new Map();
    noticed.current = new Set();
    setIdentities(new Map());
    setRegisteredThisSession(0);
    setRecognisedTracks(0);
    canvas.current?.setIdentities(null);
  }, [canvas]);

  const applyIdentity = useCallback(
    (id: string, e: IdentityEventMessage, replay: boolean) => {
      if (e.session_id !== id) return; // never apply another session's tracks
      const next = new Map(identityMap.current);
      next.set(e.track_id, { state: e.state, visitor: e.visitor });
      identityMap.current = next;
      canvas.current?.setIdentities(next);
      setIdentities(next);
      const states = [...next.values()];
      setRegisteredThisSession(states.filter((v) => v.state === "REGISTERED").length);
      setRecognisedTracks(states.filter((v) => v.state === "RECOGNIZED").length);
      const notice = replay ? null : noticeFor(e);
      const key = noticeKey(e);
      if (!notice || noticed.current.has(key)) return;
      noticed.current.add(key);
      toast[notice.level === "info" ? "message" : notice.level](notice.title, {
        description: notice.description,
        id: key,
      });
    },
    [canvas],
  );

  const attach = useCallback(
    (id: string) => {
      detach();
      resetIdentities();
      resetVisits();
      arrivals.current = [];
      const s = new LiveStream(
        {
          url: async () => streamUrl(id, (await api.streamTicket(id)).ticket),
        },
        {
          onSession: applySession,
          onIdentity: (e, replay) => applyIdentity(id, e, replay),
          onVisitEvent: (m) => applyVisitEvent(id, m),
          onVisitState: applyVisitState,
          onVisits: applyVisitsSnapshot,
          onStats: (stats: SessionStats) =>
            setSession((prev) => (prev && prev.id === id ? { ...prev, stats } : prev)),
          onFrame: (image, header) => {
            lastHeader.current = header;
            arrivals.current.push(performance.now());
            if (arrivals.current.length > 30) arrivals.current.shift();
            canvas.current?.draw(image, header);
          },
          onLink: setLink,
        },
      );
      stream.current = s;
      s.connect();
    },
    [
      applySession,
      detach,
      canvas,
      resetIdentities,
      resetVisits,
      applyIdentity,
      applyVisitEvent,
      applyVisitState,
      applyVisitsSnapshot,
    ],
  );

  // Rehydrate after navigation/reload: processing continues server-side while the page is closed.
  useEffect(() => {
    const controller = new AbortController();
    api
      .activeSession(controller.signal)
      .then(({ session: active }) => {
        if (active) {
          setSession(active);
          attach(active.id);
        }
      })
      .catch(() => undefined);
    return () => {
      controller.abort();
      detach();
    };
  }, [attach, detach]);

  // Authoritative poll while a session is active: the UI converges on the backend state even if
  // the WebSocket is down or a message was missed.
  const active = isActive(session);
  const sessionId = session?.id;
  useQuery({
    queryKey: ["processing", "session", sessionId],
    queryFn: async ({ signal }) => {
      const fresh = await api.session(sessionId!, signal);
      applySession(fresh);
      return fresh;
    },
    enabled: active && !!sessionId,
    refetchInterval: SESSION_POLL_MS,
  });

  // Overlay choices go straight to the canvas (imperative: no per-frame React renders).
  const pushOverlay = useCallback(
    (patch: Partial<OverlayOptions>) => {
      overlayRef.current = { ...overlayRef.current, ...patch };
      canvas.current?.setOverlay(overlayRef.current);
    },
    [canvas],
  );
  const selectTrack = useCallback(
    (track: Track | null) => {
      selectedId.current = track ? track.track_id : null;
      setSelected(track ? { track, ended: false } : null);
      pushOverlay({ selectedId: selectedId.current });
    },
    [pushOverlay],
  );
  const setShowOverlay = useCallback(
    (show: boolean) => {
      setShowOverlayState(show);
      pushOverlay({ show });
    },
    [pushOverlay],
  );
  const setShowLabels = useCallback(
    (labels: boolean) => {
      setShowLabelsState(labels);
      pushOverlay({ labels });
    },
    [pushOverlay],
  );

  // Copy the newest frame's numbers into React state (called on a timer, never per frame).
  const lastSnapshotId = useRef(-1);
  const snapshot = useCallback(() => {
    const h = lastHeader.current;
    const a = arrivals.current;
    if (a.length >= 2) {
      const span = (a[a.length - 1]! - a[0]!) / 1000;
      setStreamFps(span > 0 ? Math.round(((a.length - 1) / span) * 10) / 10 : null);
    }
    if (h && h.frame_id !== lastSnapshotId.current) {
      lastSnapshotId.current = h.frame_id;
      setLatest({
        frameId: h.frame_id,
        timestampMs: h.timestamp_ms,
        width: h.frame_width,
        height: h.frame_height,
        faces: h.faces.length,
        bestConfidence: h.faces.length ? Math.max(...h.faces.map((f) => f.confidence)) : null,
        inferenceMs: h.inference_ms,
      });
      const list = h.tracks ?? [];
      setTracks(list);
      setTrackingOn(!!h.tracking);
      const id = selectedId.current;
      if (id !== null) {
        const hit = list.find((t) => t.track_id === id);
        setSelected((prev) =>
          hit ? { track: hit, ended: false } : prev ? { track: prev.track, ended: true } : null,
        );
      }
    }
  }, []);

  // Release the stream once the session reached a terminal state (the last frame stays visible,
  // so the numbers beside it are synced one final time to match what is drawn).
  useEffect(() => {
    if (session && !isActive(session)) {
      const t = setTimeout(() => {
        snapshot();
        detach();
      }, 500);
      return () => clearTimeout(t);
    }
  }, [session, detach, snapshot]);

  useEffect(() => {
    if (!active) return;
    const timer = setInterval(snapshot, FRAME_SNAPSHOT_MS);
    return () => clearInterval(timer);
  }, [active, snapshot]);

  const start = useMutation({
    mutationFn: (sourceId: string) => api.startSession(sourceId),
    onMutate: () => setStartError(null),
    onSuccess: (created) => {
      canvas.current?.clear();
      lastHeader.current = null;
      lastSnapshotId.current = -1;
      setLatest(null);
      setTracks([]);
      setTrackingOn(false);
      selectTrack(null);
      setStreamFps(null);
      setSession(created);
      attach(created.id);
    },
    onError: (error) => {
      if (error instanceof ApiError) {
        setStartError(error);
        if (error.code === "session_active") void refreshActive();
      } else {
        toast.error("Could not start processing");
      }
    },
  });

  async function refreshActive() {
    const { session: current } = await api.activeSession();
    if (current) {
      setSession(current);
      attach(current.id);
    }
  }

  const stop = useMutation({
    mutationFn: (id: string) => api.stopSession(id),
    onSuccess: (updated) => applySession(updated),
    onError: () => toast.error("Could not stop processing - try again"),
  });

  return {
    session,
    link,
    latest,
    streamFps,
    sources,
    start,
    stop,
    startError,
    active,
    tracks,
    identities,
    recentEvents,
    activeVisits,
    registeredThisSession,
    recognisedTracks,
    trackingOn,
    showOverlay,
    setShowOverlay,
    showLabels,
    setShowLabels,
    selected,
    selectTrack,
  };
}
