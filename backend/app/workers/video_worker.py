"""Standalone video-processing worker entrypoint (``python -m app.workers.video_worker``).

Runs as its own process so inference never shares the API event loop. Phase 0 only proves the
process boundary: it loads the same validated settings/config, configures logging, verifies
database connectivity and exits. Ingestion and inference begin in Phase 1.
"""

from __future__ import annotations

import logging
import sys
import uuid

from app.core.config import ConfigError, load_app_config
from app.core.context import session_id_var
from app.core.logging import setup_logging
from app.core.settings import get_settings
from app.db.session import check_database, create_db_engine

logger = logging.getLogger("katomaran.worker")


def main() -> int:
    settings = get_settings()
    try:
        config = load_app_config(settings.config_path)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    setup_logging(
        settings.log_level or config.logging.level, settings.log_path, config.events_log_path
    )
    session_id_var.set(uuid.uuid4().hex)
    engine = create_db_engine(settings)
    try:
        check = check_database(
            engine, settings.db_connect_retries, settings.db_retry_backoff_seconds
        )
        if not check.ok:
            logger.error("Worker cannot reach the database (%s)", check.error)
            return 1
        logger.info("Worker process started; database reachable. No processing is implemented yet.")
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
