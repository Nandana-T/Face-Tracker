# GitHub review and safe commit plan

**Nothing has been committed or pushed in Phase 6. Do not push until you approve.**

## Review results (2026-11-11)
* Branch `main`; Phases 0–5 are already committed (latest `420c013`). Phase 6 changes are **uncommitted** (list below).
* Secrets: none tracked (see [security-audit.md](../security-audit.md) §1). `.env` is ignored and never in history.
* Not tracked and must stay so (all covered by `.gitignore`): `.env*`, `.venv/`, `.tools/` (incl. MediaMTX and ffmpeg downloads), `models/`, `datasets/` (hackathon footage, LFW, evidence), `storage/`, `logs/` (events.log, evidence crops), `temp/`, `cache/`, `backups/`, `frontend/dist`, `frontend/test-results`, `*.onnx`, `*.mp4`.
* The only tracked image is the NASA public-domain test fixture. `docs/sample-output/images/` contains two crops **of that fixture**, so they are safe to commit; the folder README says so.
* Before pushing, run: `git status`, `git diff --stat`, and `git ls-files | findstr /i ".env .onnx .mp4"` (must print only `.env.example`).

## Phase 6 changes to commit (suggested, three commits)
1. `feat(storage): private Supabase object storage backend with local fallback` – `backend/app/services/blob_store.py`, `blob_factory.py`, `image_store.py`, `evidence_store.py`, `core/settings.py`, `main.py`, `pyproject.toml`, `tests/unit/test_blob_store.py`, `render.yaml`, `frontend/.env.example`, `scripts/verify_storage.py`.
2. `fix(rtsp): forward source state to the API and detect stalled streams` – `backend/app/workers/agent.py`, `backend/app/vision/sources/rtsp_source.py`, `tests/unit/test_sources.py`, `tests/unit/test_worker_source_state.py`.
3. `docs(release): deployment guide, verification scripts, security audit, submission package` – `scripts/verify_remote_path.py`, `verify_rtsp_local.py`, `smoke_deployed.py`, `make_sample_output.py`, `docs/*` (phase-6 report, deployment guide, security audit, submission/, sample-output/, final-evaluation, ADR 0016), `README.md`.
Commit trailer: `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.

## Commands (run only after you approve)
```powershell
git status
git add backend/app/services/blob_store.py backend/app/services/blob_factory.py backend/app/services/image_store.py backend/app/services/evidence_store.py backend/app/core/settings.py backend/app/main.py backend/pyproject.toml backend/tests/unit/test_blob_store.py render.yaml frontend/.env.example scripts/verify_storage.py
git commit -m "feat(storage): private Supabase object storage backend with local fallback"
# ... then the other two groups; finally:
git push origin main
```
After pushing: confirm the GitHub repository page shows no `.env`, models, videos or datasets, and that the README renders and ends with the Katomaran line.
