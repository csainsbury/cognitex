"""Background worker for task processing using ARQ."""

import asyncio

import structlog
from arq import create_pool
from arq.connections import RedisSettings

from cognitex.config import get_settings
from cognitex.db.postgres import init_postgres, close_postgres
from cognitex.db.neo4j import init_neo4j, close_neo4j

logger = structlog.get_logger()


async def startup(ctx: dict) -> None:
    """Initialize connections on worker startup."""
    logger.info("Worker starting up")
    await init_postgres()
    await init_neo4j()

    # Initialize Redis for agent triggers
    from cognitex.db.redis import init_redis
    await init_redis()

    # Start the Agent Trigger System (Scheduler & Event Listeners)
    from cognitex.agent.triggers import start_triggers
    ctx["trigger_system"] = await start_triggers()

    ctx["initialized"] = True
    logger.info("Worker initialized with agent triggers")


async def shutdown(ctx: dict) -> None:
    """Cleanup connections on worker shutdown."""
    logger.info("Worker shutting down")

    # Stop agent triggers first
    if "trigger_system" in ctx:
        from cognitex.agent.triggers import stop_triggers
        await stop_triggers()

    # Close database connections
    from cognitex.db.redis import close_redis
    await close_redis()
    await close_neo4j()
    await close_postgres()


async def process_email_batch(ctx: dict, email_ids: list[str]) -> dict:
    """Process a batch of emails through the classification pipeline."""
    logger.info("Processing email batch", count=len(email_ids))
    # TODO: Implement email processing pipeline
    return {"processed": len(email_ids), "tasks_created": 0}


async def sync_gmail(ctx: dict) -> dict:
    """Sync new emails from Gmail."""
    logger.info("Starting Gmail sync")
    # TODO: Implement Gmail sync
    return {"new_emails": 0}


async def sync_calendar(ctx: dict) -> dict:
    """Sync events from Google Calendar."""
    logger.info("Starting Calendar sync")
    # TODO: Implement Calendar sync
    return {"new_events": 0}


def _parse_redis_settings() -> RedisSettings:
    """Parse Redis URL into ARQ RedisSettings."""
    settings = get_settings()
    url = settings.redis_url
    if url.startswith("redis://"):
        url = url[8:]
    parts = url.split("/")
    host_port = parts[0].split(":")
    host = host_port[0]
    port = int(host_port[1]) if len(host_port) > 1 else 6379
    database = int(parts[1]) if len(parts) > 1 else 0
    return RedisSettings(host=host, port=port, database=database)


class WorkerSettings:
    """ARQ worker settings."""

    functions = [process_email_batch, sync_gmail, sync_calendar]
    on_startup = startup
    on_shutdown = shutdown
    max_jobs = 10
    job_timeout = 300  # 5 minutes
    redis_settings = _parse_redis_settings()


if __name__ == "__main__":
    # Run the worker
    from arq import run_worker

    run_worker(WorkerSettings)
