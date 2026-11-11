"""Blob backends for biometric images: local filesystem and Supabase PRIVATE object storage.

The identity image store (``faces/``) and the event evidence store (``evidence/``) sit on top of a
``BlobBackend``; they keep their validation, naming and idempotency rules and only delegate I/O.

Contract
* ``put`` is idempotent: keys are content/event-id addressed, so an existing object is left alone.
* keys are relative, validated by the callers (hex ids / dates / ``.jpg``) and confined here again.
* nothing is ever public: the Supabase backend talks to a PRIVATE bucket with the service-role key,
  which exists only inside the trusted API process (never the worker, never the browser). Images reach
  browsers only through the authenticated API endpoints.
* transient failures raise ``BlobError`` (callers retry); a missing object raises ``BlobNotFound``.
* image bytes are never logged.
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

import httpx


class BlobError(Exception):
    """The storage failed (unreachable, quota, 5xx). Retrying later may help."""


class BlobNotFound(BlobError):
    pass


class BlobBackend(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...
    def delete(self, key: str) -> bool: ...
    def delete_prefix(self, prefix: str) -> int: ...
    def list(self, prefix: str = "") -> list[tuple[str, float]]: ...


class LocalBlobBackend:
    """Files under ``root`` (D: in development). Writes are temp file + fsync + rename."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        if "\\" in key or key.startswith("/") or ":" in key:
            raise BlobError("invalid object key")
        path = (self.root / key).resolve()
        if self.root not in path.parents:
            raise BlobError("path escapes the storage root")
        return path

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        if path.is_file():
            return
        partial = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.part")  # unique per writer
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with partial.open("wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            partial.replace(path)
        except OSError as exc:
            partial.unlink(missing_ok=True)
            if path.is_file():  # a concurrent writer of the same object won the race
                return
            raise BlobError(f"could not write: {exc}") from exc

    def get(self, key: str) -> bytes:
        try:
            return self._path(key).read_bytes()
        except FileNotFoundError as exc:
            raise BlobNotFound("not found") from exc
        except OSError as exc:
            raise BlobError(f"could not read: {exc}") from exc

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> bool:
        path = self._path(key)
        existed = path.is_file()
        path.unlink(missing_ok=True)
        return existed

    def delete_prefix(self, prefix: str) -> int:
        folder = self._path(prefix.rstrip("/"))
        if not folder.is_dir():
            return 0
        n = 0
        for f in folder.rglob("*"):
            if f.is_file():
                f.unlink(missing_ok=True)
                n += 1
        for d in sorted((p for p in folder.rglob("*") if p.is_dir()), reverse=True):
            d.rmdir()
        folder.rmdir()
        return n

    def list(self, prefix: str = "") -> list[tuple[str, float]]:
        base = self._path(prefix.rstrip("/")) if prefix else self.root
        if not base.is_dir():
            return []
        return [
            (f.relative_to(self.root).as_posix(), f.stat().st_mtime)
            for f in base.rglob("*")
            if f.is_file()
        ]


class SupabaseBlobBackend:
    """Supabase Storage (REST) PRIVATE bucket, object keys under ``prefix/``.

    Authorisation: ``Authorization: Bearer <service-role key>`` (server side only). The bucket must be
    private; ``verify_private`` checks it. Uploads use ``x-upsert: false``: an existing object (HTTP
    400/409 "Duplicate") means "already stored" - the keys are deterministic, so that is success.
    """

    def __init__(
        self,
        url: str,
        service_key: str,
        bucket: str,
        prefix: str,
        client: httpx.Client | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._base = url.rstrip("/") + "/storage/v1"
        self._bucket = bucket
        self._prefix = prefix.strip("/")
        self._client = client or httpx.Client(timeout=timeout)
        self._headers = {"Authorization": f"Bearer {service_key}", "apikey": service_key}

    root = None  # no local filesystem

    def _key(self, key: str) -> str:
        if key.startswith("/") or ".." in key.split("/") or "\\" in key:
            raise BlobError("invalid object key")
        return f"{self._prefix}/{key}" if self._prefix else key

    def _url(self, op: str, key: str) -> str:
        return f"{self._base}/object/{op}{self._bucket}/{quote(self._key(key), safe='/')}"

    def _send(self, method: str, url: str, **kw: Any) -> httpx.Response:
        headers = {**self._headers, **kw.pop("headers", {})}
        try:
            return self._client.request(method, url, headers=headers, **kw)
        except httpx.HTTPError as exc:
            raise BlobError(f"storage unreachable: {type(exc).__name__}") from exc

    def put(self, key: str, data: bytes) -> None:
        r = self._send(
            "POST",
            self._url("", key),
            content=data,
            headers={"Content-Type": "image/jpeg", "x-upsert": "false"},
        )
        if r.status_code in (200, 201):
            return
        if r.status_code in (400, 409) and "uplicate" in r.text:
            return  # already stored: deterministic key => same evidence
        raise BlobError(f"upload failed (HTTP {r.status_code})")

    def get(self, key: str) -> bytes:
        r = self._send("GET", self._url("authenticated/", key))
        if r.status_code == 200:
            return r.content
        if r.status_code in (400, 404):
            raise BlobNotFound("not found")
        raise BlobError(f"download failed (HTTP {r.status_code})")

    def exists(self, key: str) -> bool:
        r = self._send("HEAD", self._url("authenticated/", key))
        if r.status_code == 200:
            return True
        if r.status_code in (400, 404):
            return False
        raise BlobError(f"lookup failed (HTTP {r.status_code})")

    def delete(self, key: str) -> bool:
        existed = self.exists(key)
        r = self._send(
            "DELETE", f"{self._base}/object/{self._bucket}", json={"prefixes": [self._key(key)]}
        )
        if r.status_code not in (200, 204):
            raise BlobError(f"delete failed (HTTP {r.status_code})")
        return existed

    def delete_prefix(self, prefix: str) -> int:
        keys = [k for k, _ in self.list(prefix)]
        for i in range(0, len(keys), 100):
            r = self._send(
                "DELETE",
                f"{self._base}/object/{self._bucket}",
                json={"prefixes": [self._key(k) for k in keys[i : i + 100]]},
            )
            if r.status_code not in (200, 204):
                raise BlobError(f"delete failed (HTTP {r.status_code})")
        return len(keys)

    def list(self, prefix: str = "") -> list[tuple[str, float]]:
        """Recursive listing: (key relative to this backend's prefix, last-modified epoch seconds)."""
        out: list[tuple[str, float]] = []
        stack = [prefix.strip("/")]
        while stack:
            folder = stack.pop()
            offset = 0
            while True:
                r = self._send(
                    "POST",
                    f"{self._base}/object/list/{self._bucket}",
                    json={
                        "prefix": self._key(folder) if folder else self._prefix,
                        "limit": 100,
                        "offset": offset,
                    },
                )
                if r.status_code != 200:
                    raise BlobError(f"list failed (HTTP {r.status_code})")
                items = r.json()
                for it in items:
                    name = it["name"]
                    key = f"{folder}/{name}" if folder else name
                    if it.get("id") is None:  # a folder
                        stack.append(key)
                    else:
                        stamp = it.get("updated_at") or it.get("created_at")
                        mtime = (
                            datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
                            if stamp
                            else time.time()
                        )
                        out.append((key, mtime))
                if len(items) < 100:
                    break
                offset += 100
        return out

    def verify_private(self) -> bool:
        """True when the bucket exists and is NOT public (a public bucket would expose biometric images)."""
        r = self._send("GET", f"{self._base}/bucket/{self._bucket}")
        if r.status_code != 200:
            raise BlobError(f"bucket lookup failed (HTTP {r.status_code})")
        return not bool(r.json().get("public"))
