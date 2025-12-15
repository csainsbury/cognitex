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
    yield

    # Cleanup
    logger.info("Shutting down Cognitex")
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
    from cognitex.api.routes import health, tasks, goals, webhooks

    app.include_router(health.router, tags=["health"])
    app.include_router(tasks.router, prefix="/api/tasks", tags=["tasks"])
    app.include_router(goals.router, prefix="/api/goals", tags=["goals"])
    app.include_router(webhooks.router, prefix="/webhooks", tags=["webhooks"])

    return app


app = create_app()
