# Demo video – recording plan and walkthrough (target 6–8 minutes)

**The hackathon requires a Loom or YouTube explanation link. The submission is not complete until that link is in the README (section "Demo and links").** This file is a plan; **no video has been recorded.**

## Before recording (10 min)
1. Use the **controlled fixture** (public-domain astronaut portrait clips `E2E alignment clip`, `E2E leaves clip`) for everything on screen. **Do not show hackathon footage faces or any real person** unless you have authorisation; if you want to mention real-footage results, show only the aggregate tables from the reports.
2. Close other windows (no tokens, no `.env`, no dashboard secrets). Browser zoom 110 %, dark theme, window 1920×1080.
3. Warm up: if the API is on Render free tier, open the app 2 minutes before recording (cold start ≈ 1 min). Start the worker (`scripts\start-worker.ps1`) and confirm *Worker connected*.
4. Empty state: `python scripts\e2e_reset_identity.py` (local DB only) or use a fresh deployment so "V00001" appears live.
5. Pre-open tabs: Live Monitoring, Visitors, Event Logs, Analytics; a terminal with `python scripts\smoke_deployed.py`.

## Script (time, screen, what to say)
| # | ≈ min | Screen | Say / show |
|---|---|---|---|
| 1 | 0:30 | Title slide / README | "Katomaran AI: detects, tracks, recognises and registers faces from video or RTSP, logs entry/exit with images, and counts unique registered visitors." |
| 2 | 0:30 | Problem statement | The brief: detect, track, recognise, auto-register, log entries/exits with timestamped images in DB + `events.log`, count unique visitors, dashboard. |
| 3 | 0:45 | Architecture diagram (README) | Vercel SPA → FastAPI on Render → Supabase Postgres + private storage; the AI worker runs next to the camera and dials out over WSS with a token; it never gets database or storage keys. |
| 4 | 0:30 | Live Monitoring: pick *E2E alignment clip* → Start | "Live ingestion: file or RTSP; honest states." |
| 5 | 0:30 | Boxes on video | "YOLO face detection, every Nth frame." |
| 6 | 0:30 | Track chip `#1`, track details | "Temporary session-scoped tracks – a track id is not a person." |
| 7 | 0:45 | Toast "New visitor registered: V00001", Recognition panel | "Quality gate → alignment → SFace embedding → automatic registration; persistent id V00001." |
| 8 | 0:45 | Run the clip again → "Visitor recognized: V00001"; Visitors page | "Same identity recognised in a new session: unique identities don't grow." |
| 9 | 0:45 | Event Logs: entry + exit rows | "One entry and one terminal event per visit; the exit is the last sighting or 'observation ended', not a measured departure." |
| 10 | 0:30 | Visit drawer: entry/exit images | "Timestamped evidence; exit image is the last good observation." |
| 11 | 0:30 | Analytics tiles | "Unique visitors = distinct registered identities; visits and returning shown separately." |
| 12 | 0:45 | Analytics charts, filters, coverage panel | Point at the counting note and the coverage disclaimer: "not a count of physical people". |
| 13 | 0:30 | Restart API (or Render redeploy) → refresh | "Everything is rebuilt from PostgreSQL; images persist in private storage; open visits are closed as interrupted." |
| 14 | 0:45 | Terminal: test totals, `smoke_deployed.py`, `reconcile_counts.py` | State the **actual** totals from `docs/phase-6-report.md`; show reconciliation `consistent=True`. |
| 15 | 0:30 | Limitations slide | identities ≠ people; most real surveillance faces fail the quality gate; no real-camera test; model licence; single instance. Thank you. |

## Recording checklist
* Microphone check; record at 1080p; keep each take < 8 min; trim silence.
* Never show: `.env`, Supabase/Render dashboards with keys, the admin password (type it off-camera or use a password manager), real faces.
* Upload to Loom/YouTube (unlisted is fine), open it in a private window to confirm it plays without sign-in, then paste the link in README → *Demo and links*.
