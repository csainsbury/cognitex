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


async def _ensure_connected() -> None:
    """Ensure Neo4j driver is connected, reconnecting if necessary."""
    global _driver

    if _driver is None:
        await init_neo4j()
        return

    # Check if driver is still alive
    try:
        await _driver.verify_connectivity()
    except Exception as e:
        logger.warning("Neo4j connection lost, reconnecting...", error=str(e))
        try:
            await _driver.close()
        except Exception:
            pass
        _driver = None
        await init_neo4j()
        logger.info("Neo4j reconnected successfully")


async def close_neo4j() -> None:
    """Close Neo4j connection."""
    global _driver
    if _driver:
        await _driver.close()
        _driver = None
        logger.info("Neo4j connection closed")


async def get_neo4j_session(
    database: str = "neo4j",
    access_mode: str = "WRITE",
) -> AsyncGenerator[AsyncSession, None]:
    """
    Get a Neo4j session.

    Automatically reconnects if the driver connection was lost.

    Args:
        database: Database name (default: neo4j)
        access_mode: "WRITE" or "READ" (default: WRITE for backwards compatibility)
    """
    from neo4j import WRITE_ACCESS, READ_ACCESS

    await _ensure_connected()

    mode = WRITE_ACCESS if access_mode == "WRITE" else READ_ACCESS
    async with _driver.session(database=database, default_access_mode=mode) as session:
        yield session


def get_driver() -> AsyncDriver:
    """Get the Neo4j driver instance."""
    if _driver is None:
        raise RuntimeError("Neo4j not initialized. Call init_neo4j() first.")
    return _driver


async def run_query(query: str, params: dict | None = None) -> list[dict]:
    """
    Run a Neo4j query with its own session.

    This is safe for concurrent use - each call gets its own session from the pool.
    Use this for parallel queries instead of sharing a session across coroutines.

    Args:
        query: Cypher query string
        params: Optional query parameters

    Returns:
        List of result records as dictionaries
    """
    async for session in get_neo4j_session(access_mode="READ"):
        try:
            result = await session.run(query, params or {})
            data = await result.data()
            return data
        except Exception as e:
            logger.warning("Query failed", error=str(e), query=query[:100])
            raise


async def run_query_single(query: str, params: dict | None = None) -> dict | None:
    """
    Run a Neo4j query and return a single record.

    Args:
        query: Cypher query string
        params: Optional query parameters

    Returns:
        Single result record as dict, or None if no results
    """
    async for session in get_neo4j_session(access_mode="READ"):
        try:
            result = await session.run(query, params or {})
            record = await result.single()
            return dict(record) if record else None
        except Exception as e:
            logger.warning("Query failed", error=str(e), query=query[:100])
            raise
