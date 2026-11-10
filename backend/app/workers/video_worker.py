"""Video-processing worker entrypoint (``python -m app.workers.video_worker``).

Default: run the long-lived agent that connects to the API and processes sessions on request.
``--check``: Phase 0 behaviour - validate config + database reachability and exit.

The worker is its own process: inference never shares the API event loop, and it needs no database
credentials in agent mode (all persistence goes through the API).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
import uuid

from app.core.config import ConfigError, load_app_config
from app.core.context import session_id_var
from app.core.logging import setup_logging
from app.core.settings import get_settings
from app.db.session import check_database, create_db_engine

logger = logging.getLogger("katomaran.worker")


def run_check() -> int:
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
        logger.info("Worker check passed; database reachable.")
        return 0
    finally:
        engine.dispose()


def run_agent() -> int:
    from app.workers.agent import WorkerAgent, model_status

    settings = get_settings()
    try:
        config = load_app_config(settings.config_path)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    setup_logging(
        settings.log_level or config.logging.level, settings.log_path, config.events_log_path
    )
    ok, detail = model_status(settings, config)
    logger.info(
        "Worker starting",
        extra={
            "worker_id": settings.worker_id,
            "model": config.detection.model,
            "model_available": ok,
        },
    )
    if not ok:
        logger.error(
            "Face detection model unavailable: %s", detail
        )  # still runs: API will report it
    agent = WorkerAgent(settings, config)

    async def main() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, agent.request_shutdown)
            except NotImplementedError:  # Windows: fall back to signal.signal
                signal.signal(sig, lambda *_: loop.call_soon_threadsafe(agent.request_shutdown))
        await agent.run_forever()

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
    logger.info("Worker stopped")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Katomaran video worker")
    parser.add_argument("--check", action="store_true", help="validate config + database and exit")
    args = parser.parse_args(argv)
    return run_check() if args.check else run_agent()


if __name__ == "__main__":
    sys.exit(main())
