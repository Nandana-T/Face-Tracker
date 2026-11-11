r"""Create (or verify) the PRIVATE Supabase Storage bucket for biometric evidence. Idempotent.

    set SUPABASE_URL=https://pmrrthdvtiqtikdllgll.supabase.co
    set SUPABASE_SERVICE_ROLE_KEY=...            (server secret; never printed, never committed)
    set SUPABASE_BUCKET=katomaran-evidence       (default)
    python scripts\setup_storage.py              DRY RUN: reads only - shows what --apply would do
    python scripts\setup_storage.py --apply      creates the bucket if missing (private) / verifies it if present

The ONLY write it can perform is creating one bucket with:
  public = false                 no anonymous listing or download, ever
  file_size_limit = 524288       512 KB (the API rejects larger images anyway)
  allowed_mime_types = image/jpeg
It never changes an existing bucket, never touches objects, never creates storage policies (none are needed:
the service-role key bypasses RLS on the server, and with no policies the anon/authenticated roles get nothing).
If the bucket already exists and is PUBLIC it stops and tells you; it does not flip it automatically.
Exit code 0 = bucket exists/created and is private.
"""

from __future__ import annotations

import os
import sys

import httpx


def main() -> int:
    apply = "--apply" in sys.argv[1:]
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    bucket = os.environ.get("SUPABASE_BUCKET", "katomaran-evidence")
    if not url.startswith("https://") or not key:
        print("Set SUPABASE_URL (https://<ref>.supabase.co) and SUPABASE_SERVICE_ROLE_KEY", file=sys.stderr)
        return 2
    headers = {"Authorization": f"Bearer {key}", "apikey": key}
    base = f"{url}/storage/v1"
    c = httpx.Client(timeout=20, headers=headers)
    print(f"project   {url}\nbucket    {bucket}\nmode      {'APPLY' if apply else 'dry run (read-only)'}")
    r = c.get(f"{base}/bucket/{bucket}")
    if r.status_code == 200:
        info = r.json()
        public = bool(info.get("public"))
        print(f"exists    yes   public={public}  size_limit={info.get('file_size_limit')}  mime={info.get('allowed_mime_types')}")
        if public:
            print("STOP: the bucket is PUBLIC. Make it private in the dashboard (Storage -> bucket -> Edit -> Public off) or use another name.")
            return 1
        print("OK: private bucket already in place; nothing to do.")
        return 0
    if r.status_code not in (400, 404):
        print(f"unexpected answer while looking for the bucket (HTTP {r.status_code}); no changes made")
        return 1
    print("exists    no")
    body = {"id": bucket, "name": bucket, "public": False, "file_size_limit": 524288, "allowed_mime_types": ["image/jpeg"]}
    if not apply:
        print(f"would create: POST {base}/bucket  {body}")
        print("re-run with --apply to perform exactly this one write")
        return 0
    created = c.post(f"{base}/bucket", json=body)
    if created.status_code not in (200, 201):
        print(f"create failed (HTTP {created.status_code})")
        return 1
    check = c.get(f"{base}/bucket/{bucket}").json()
    print(f"created   public={check.get('public')}  size_limit={check.get('file_size_limit')}  mime={check.get('allowed_mime_types')}")
    return 0 if not check.get("public") else 1


if __name__ == "__main__":
    raise SystemExit(main())
