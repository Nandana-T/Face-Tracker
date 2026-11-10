"""Project-root discovery and path resolution.

All relative paths in settings/config are resolved against the project root so the
application behaves identically regardless of the current working directory.
"""

from __future__ import annotations

import os
from pathlib import Path

# backend/app/core/paths.py -> parents[3] is the repository root.
_DEFAULT_ROOT = Path(__file__).resolve().parents[3]


def project_root() -> Path:
    """Return the project root. ``KATOMARAN_ROOT`` overrides (useful in containers)."""
    override = os.environ.get("KATOMARAN_ROOT")
    return Path(override).resolve() if override else _DEFAULT_ROOT


def resolve_path(value: str | Path, base: Path | None = None) -> Path:
    """Resolve ``value`` against ``base`` (default: project root) unless it is absolute."""
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return ((base or project_root()) / path).resolve()
