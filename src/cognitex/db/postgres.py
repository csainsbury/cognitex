"""PostgreSQL database connection management."""

from typing import AsyncGenerator

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from cognitex.config import get_settings

logger = structlog.get_logger()

# Global engine and session factory
_engine = None
_session_factory = None


async def init_postgres() -> None:
    """Initialize PostgreSQL connection pool."""
    global _engine, _session_factory

    settings = get_settings()
    # Convert postgresql:// to postgresql+asyncpg://
    db_url = settings.database_url.replace("postgresql://", "postgresql+asyncpg://")

    _engine = create_async_engine(
        db_url,
        echo=settings.is_development,
        pool_size=5,
        max_overflow=10,
    )
    _session_factory = async_sessionmaker(
        bind=_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    logger.info("PostgreSQL connection initialized")


async def close_postgres() -> None:
    """Close PostgreSQL connection pool."""
    global _engine
    if _engine:
        await _engine.dispose()
        logger.info("PostgreSQL connection closed")


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Get a database session."""
    if _session_factory is None:
        raise RuntimeError("Database not initialized. Call init_postgres() first.")

    async with _session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
