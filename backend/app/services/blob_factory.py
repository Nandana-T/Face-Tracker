"""Choose the image storage backends (local folders or Supabase private storage)."""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

from app.core.config import AppConfig
from app.core.paths import resolve_path
from app.core.settings import Settings
from app.services.blob_store import BlobBackend, LocalBlobBackend, SupabaseBlobBackend
from app.services.evidence_store import EvidenceStore
from app.services.image_store import LocalImageStore

logger = logging.getLogger("katomaran.storage")


def build_stores(
    settings: Settings, config: AppConfig, client: httpx.Client | None = None
) -> tuple[LocalImageStore, EvidenceStore, str]:
    """(visitor image store, evidence store, backend name). One private bucket, two prefixes."""
    faces: BlobBackend
    evidence: BlobBackend
    if settings.storage_backend == "supabase":
        assert settings.supabase_url and settings.supabase_service_role_key  # validated by Settings
        key = settings.supabase_service_role_key.get_secret_value()
        faces = SupabaseBlobBackend(
            settings.supabase_url, key, settings.supabase_bucket, "faces", client
        )
        evidence = SupabaseBlobBackend(
            settings.supabase_url, key, settings.supabase_bucket, "evidence", client
        )
        return LocalImageStore(backend=faces), EvidenceStore(backend=evidence), "supabase"
    if settings.app_env == "production":
        logger.warning(
            "Production API with LOCAL image storage: files live on this host's disk and are lost "
            "when the instance is rebuilt. Set STORAGE_BACKEND=supabase for durable evidence."
        )
    faces_root: Path = resolve_path(config.recognition.faces_dir)
    return (
        LocalImageStore(backend=LocalBlobBackend(faces_root)),
        EvidenceStore(backend=LocalBlobBackend(config.evidence_path)),
        "local",
    )
