# ADR 0016 – Private object storage for biometric images

Status: accepted (Phase 6).

## Context
Visitor representative images and entry/exit evidence were files under D: (`storage/faces`, `logs/entries|exits`). A cloud API (Render free tier) has an **ephemeral filesystem**: every redeploy, restart or spin-down would delete evidence while the database still referenced it.

## Decision
* A small `BlobBackend` interface (`put/get/exists/delete/delete_prefix/list`) with two implementations: `LocalBlobBackend` (the existing behaviour: atomic temp-file writes, confined paths) and `SupabaseBlobBackend` (Storage REST, **private** bucket, service-role key).
* The existing `LocalImageStore` (visitors) and `EvidenceStore` (events) keep their validation, deterministic names and public methods and delegate I/O to a backend, so the identity manager, the recorder and every endpoint are unchanged. One bucket, two prefixes: `faces/`, `evidence/`.
* Selected by `STORAGE_BACKEND=local|supabase` (default local). `supabase` requires an https `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`; the key is a `SecretStr`, exists only in the API process, is never passed to the worker, logged or exposed.
* Idempotency: keys are content/event-id addressed and uploads use `x-upsert: false`; a "duplicate" answer is success. Failures are `BlobError` → `EvidenceWriteError` → the existing PENDING/retry/MISSING machinery (an outage never loses the event, and evidence is never fabricated).
* Authorization stays server-side: browsers get images only through `GET /visitors/{id}/image` and `GET /events/{id}/image` (token required, `private, no-store`). No signed URLs, no public bucket (`verify_private()` and `scripts/verify_storage.py` check).
* Erasure deletes objects by prefix; reconciliation lists the bucket to remove orphans and flag missing objects; retention is the existing `recognition.retention_days`.
* The remote worker still transmits crops inside its authenticated WSS observation messages; it receives no storage credentials.

## Consequences
* The client logic is verified against an in-memory fake of the Storage API (outages, 5xx, duplicates, paging, private bucket); **behaviour against the real service is unverified until `verify_storage.py` is run with credentials.**
* Listing for reconciliation costs one request per 100 objects per folder: fine for thousands of evidence files; revisit for far larger volumes.
* Free-tier quota is 1 GB (≈ 70 k crops at 15 KB); retention and a quota check are the operator's responsibility.
