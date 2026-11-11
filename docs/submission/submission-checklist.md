# Submission checklist – deadline **Friday, 13 November 2026, 2:00 PM IST**

Legend: ✅ done and verified · ⏳ needs you · ⛔ not done. Nothing below was submitted by this assistant; the form has **not** been submitted.

| # | Item | Status | Notes |
|---|---|---|---|
| 1 | Functional software (detect, track, recognise, register, log, count, dashboard) | ✅ | phases 0–5, tests in `phase-6-report.md` |
| 2 | Local reproducible demonstration | ✅ | `scripts\start-all.ps1` + `start-frontend.ps1`; production-like TLS run: `scripts\verify_remote_path.py` |
| 3 | Supabase migrations `0003`/`0004` applied | ⏳ | **needs your approval** – [deployment-guide §3](../deployment-guide.md); hosted DB is still at `0002` |
| 4 | Private storage bucket + `verify_storage.py` against the real bucket | ⏳ | needs your Supabase project and service-role key |
| 5 | Render API deployed, URL verified | ⏳ | needs your account; blueprint ready, never built here (no Docker) |
| 6 | Vercel frontend deployed, URL verified | ⏳ | needs your account; build verified locally |
| 7 | `smoke_deployed.py --require-worker` passes against the URLs | ⏳ | passes locally (16/16 on the TLS production-mode stack) |
| 8 | Local worker connected to the hosted API over WSS | ⏳ | verified over loopback TLS only |
| 9 | **Demo video (Loom/YouTube) recorded and linked in README** | ⛔ | required by the brief; plan in `demo-video-script.md` |
| 10 | README has the demo link, deployment links, final attribution line | ⏳ | placeholders marked `TODO` in *Demo and links* |
| 11 | GitHub repository: commit + push (secret/biometric review done) | ⏳ | [github-commit-plan.md](github-commit-plan.md); needs your approval |
| 12 | Hackathon form submitted with repository + demo links | ⛔ | only you can do this; nothing was submitted |
| 13 | Private data not published (hackathon faces, `.env`, keys) | ✅ | audit in `security-audit.md`; sample outputs use the NASA public-domain fixture |

## Suggested order for the remaining time
1. (30 min) Decide on cloud: if yes → Gate A (backup → migrate) → bucket → Render → Vercel → `smoke_deployed.py`. If not, submit the verified local deployment and say so.
2. (45 min) Record the demo video from the fixture clips; upload; paste links in README.
3. (15 min) Commit/push per the plan; re-open the GitHub page in a private window.
4. (10 min) Fill the hackathon form: repository URL, demo URL, deployed URLs (if any), a note on limitations (identities ≠ people; no real-camera test).
