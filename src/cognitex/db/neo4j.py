"""Neo4j graph database connection management."""

from typing import AsyncGenerator

import structlog
from neo4j import AsyncGraphDatabase, AsyncDriver, AsyncSession, NotificationDisabledCategory

from cognitex.config import get_settings

logger = structlog.get_logger()

# Global driver
_driver: AsyncDriver | None = None


async def init_neo4j() -> None:
    """Initialize Neo4j connection."""
    global _driver

    settings = get_settings()
    _driver = AsyncGraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value()),
        # Disable noisy warnings about non-existent relationship types/properties
        notifications_disabled_categories=[
            NotificationDisabledCategory.UNRECOGNIZED,
        ],
    )
    # Verify connectivity
    await _driver.verify_connectivity()
    logger.info("Neo4j connection initialized")


async def close_neo4j() -> None:
    """Close Neo4j connection."""
    global _driver
    if _driver:
        await _driver.close()
        logger.info("Neo4j connection closed")


async def get_neo4j_session() -> AsyncGenerator[AsyncSession, None]:
    """Get a Neo4j session."""
    if _driver is None:
        raise RuntimeError("Neo4j not initialized. Call init_neo4j() first.")

    async with _driver.session() as session:
        yield session


def get_driver() -> AsyncDriver:
    """Get the Neo4j driver instance."""
    if _driver is None:
        raise RuntimeError("Neo4j not initialized. Call init_neo4j() first.")
    return _driver
