# ADR 0008 – Worker ⇄ API transport: worker-initiated WebSocket

**Status:** accepted (Phase 1).

## Context
Inference must run outside the API event loop, possibly on another machine (a hosted API on Render cannot reach an
RTSP camera on a private LAN). Worker and API must not share memory. Options: (a) API spawns the worker as a child
process; (b) shared queue/database polling; (c) worker connects out to the API over a WebSocket; (d) a message
broker (Redis/NATS).

## Decision
**(c)** The worker dials `WORKER_API_URL + /api/v1/worker/ws` with `Authorization: Bearer <WORKER_TOKEN>` and keeps
the connection open (reconnecting with 1→15 s back-off). Everything flows over it:

* worker → API: `hello` (worker id, model availability), `heartbeat`, `session.accepted`, `session.state`
  (`running`, `reconnecting`, `connected`), `session.stats` (~1/s), `session.ended`, and **binary frames**
  (`[uint32 N][N-byte JSON header][JPEG]`).
* API → worker: `session.start {session_id, source_id}`, `session.stop {session_id}`.

## Why this fits
* Works with a worker anywhere with outbound internet: no inbound port, no VPN. Local development is the same code.
* Needs no new infrastructure (no broker), which keeps the free-tier constraint.
* The browser never talks to the worker; the API relays the *same* binary message to browsers, so overlay metadata and
  pixels cannot drift apart.
* The worker holds **no database credentials** - all persistence goes through the API.

## Safety properties
* Auth: constant-time bearer-token comparison; no token configured ⇒ every worker connection is refused. Only one
  worker is active; a new one replaces the old (and fails the session it was running).
* Bounded messages: 16 KiB control, 32 KiB frame header, 2 MiB frame; oversize/malformed messages are dropped, not fatal.
* Back-pressure: worker side holds one pending frame (latest wins) and ≤ 500 control messages; API side keeps one
  frame slot and ≤ 100 control messages per browser. A slow consumer loses *frames*, never grows memory.
* Liveness: worker heartbeats every `heartbeat_interval_seconds`; no traffic for 3 intervals ⇒ the API closes the
  socket and fails the session (`worker_lost`). A heartbeat that does not mention the active session ⇒ `worker_lost_session`.
* Disconnect cleanup runs in a shielded task (a cancelled WebSocket handler must still finish failing the session).

## Trade-offs
Single API process owns live state (one worker, one active session) - fine for Phase 1; scaling out needs a shared
bus (Redis) and is deliberately deferred. Browser access has **no user authentication yet** (Origin allow-list +
unguessable session id only); see `docs/privacy-and-security.md`.
