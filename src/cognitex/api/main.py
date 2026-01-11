"""FastAPI application entry point."""

from contextlib import asynccontextmanager
from typing import AsyncIterator

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from cognitex import __version__
from cognitex.config import get_settings
from cognitex.db.postgres import close_postgres, init_postgres
from cognitex.db.neo4j import close_neo4j, init_neo4j
from cognitex.db.redis import close_redis, init_redis

logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage application startup and shutdown."""
    settings = get_settings()
    logger.info("Starting Cognitex", version=__version__, environment=settings.environment)

    # Initialize database connections
    await init_postgres()
    await init_neo4j()
    await init_redis()

    logger.info("All connections initialized")

    # Initialize Phase 4 learning schema
    from cognitex.db.phase4_schema import init_phase4_schema
    await init_phase4_schema()

    # Start trigger system (handles email events, scheduled jobs, autonomous agent)
    from cognitex.agent.triggers import start_triggers, stop_triggers
    try:
        await start_triggers()
        logger.info("Trigger system started (email events, scheduled jobs, autonomous agent)")
    except Exception as e:
        logger.error("Failed to start trigger system", error=str(e))

    # Set up Gmail watch for push notifications
    from cognitex.services.push_notifications import get_watch_manager
    try:
        if settings.google_pubsub_topic:
            watch_manager = get_watch_manager()
            result = await watch_manager.setup_gmail_watch()
            if "error" not in result:
                logger.info("Gmail watch set up", history_id=result.get("historyId"))
            else:
                logger.warning("Gmail watch setup failed", error=result.get("error"))
    except Exception as e:
        logger.warning("Failed to set up Gmail watch", error=str(e))

    yield

    # Cleanup
    logger.info("Shutting down Cognitex")
    try:
        await stop_triggers()
    except Exception:
        pass
    await close_redis()
    await close_neo4j()
    await close_postgres()


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    settings = get_settings()

    app = FastAPI(
        title="Cognitex",
        description="Personal agent system for cognitive load management",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if settings.is_development else None,
        redoc_url="/redoc" if settings.is_development else None,
    )

    # CORS middleware for development
    if settings.is_development:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # Register routes
    from cognitex.api.routes import health, tasks, goals, webhooks, sync

    app.include_router(health.router, tags=["health"])
    app.include_router(tasks.router, prefix="/api/tasks", tags=["tasks"])
    app.include_router(goals.router, prefix="/api/goals", tags=["goals"])
    app.include_router(webhooks.router, prefix="/webhooks", tags=["webhooks"])
    app.include_router(sync.router, prefix="/api/sync", tags=["sync"])

    return app


app = create_app()
