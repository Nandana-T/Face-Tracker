"""ORM models. Import every model here so Alembic's metadata sees them."""

from app.models.app_metadata import AppMetadata

__all__ = ["AppMetadata"]
