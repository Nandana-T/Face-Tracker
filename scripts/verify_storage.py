r"""Verify a REAL Supabase Storage bucket for biometric evidence (run once after creating the bucket).

    set SUPABASE_URL=https://<ref>.supabase.co
    set SUPABASE_SERVICE_ROLE_KEY=...        (server secret - never commit it, never put it on the command line)
    set SUPABASE_BUCKET=katomaran-evidence
    python scripts\verify_storage.py

Writes ONE throw-away synthetic object under ``healthcheck/`` (random noise, no face) and removes it.
Checks: the bucket exists and is PRIVATE, upload, idempotent duplicate upload, read back identical bytes,
listing, the PUBLIC URL is refused (HTTP 4xx), delete. Exit code 0 only if all pass. Prints no secrets.
The client logic is unit-tested against a fake; this script is the only thing that proves the real service.
"""

from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

import cv2
import httpx
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.services.blob_store import BlobError, BlobNotFound, SupabaseBlobBackend  # noqa: E402


def main() -> int:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    bucket = os.environ.get("SUPABASE_BUCKET", "katomaran-evidence")
    if not url.startswith("https://") or not key:
        print("Set SUPABASE_URL (https://...) and SUPABASE_SERVICE_ROLE_KEY", file=sys.stderr)
        return 2
    backend = SupabaseBlobBackend(url, key, bucket, "healthcheck")
    results: list[tuple[str, bool]] = []

    def check(name: str, ok: bool) -> None:
        results.append((name, ok))
        print(("PASS " if ok else "FAIL ") + name, flush=True)

    obj = f"probe-{secrets.token_hex(6)}.jpg"
    img = np.random.default_rng().integers(0, 255, size=(64, 64, 3), dtype=np.uint8)
    payload = bytes(cv2.imencode(".jpg", img)[1].tobytes())
    try:
        check("bucket exists and is private", backend.verify_private())
        backend.put(obj, payload)
        check("upload", backend.exists(obj))
        backend.put(obj, payload)  # deterministic key: a second upload is a no-op, not an error
        check("idempotent duplicate upload", True)
        check("read back identical bytes", backend.get(obj) == payload)
        check("listing sees the object", any(k == obj for k, _ in backend.list("")))
        public = httpx.get(f"{url}/storage/v1/object/public/{bucket}/healthcheck/{obj}", timeout=20)
        check("the PUBLIC URL is refused", public.status_code >= 400)
        anon = httpx.get(f"{url}/storage/v1/object/authenticated/{bucket}/healthcheck/{obj}", timeout=20)
        check("unauthenticated download is refused (no key)", anon.status_code in (400, 401, 403, 404))
        anon_list = httpx.post(f"{url}/storage/v1/object/list/{bucket}", json={"prefix": "", "limit": 5}, timeout=20)
        check("unauthenticated listing is refused (no key)", anon_list.status_code in (400, 401, 403) or anon_list.json() == [])
        try:
            backend.get("does-not-exist.jpg")
            check("a missing object raises 'not found'", False)
        except BlobNotFound:
            check("a missing object raises 'not found'", True)
        check("exists() is False for a missing object", backend.exists("does-not-exist.jpg") is False)
        check("delete", backend.delete(obj) and not backend.exists(obj))
    except BlobError as exc:
        check(f"storage error: {exc}", False)
    finally:
        try:
            backend.delete(obj)
        except BlobError:
            pass
    bad = [n for n, ok in results if not ok]
    print(f"{len(results) - len(bad)}/{len(results)} checks passed")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
