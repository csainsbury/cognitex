"""Redis connection management for caching and queues."""

import structlog
from redis.asyncio import Redis

from cognitex.config import get_settings

logger = structlog.get_logger()

# Global Redis client
_redis: Redis | None = None


async def init_redis() -> None:
    """Initialize Redis connection."""
    global _redis

    settings = get_settings()
    _redis = Redis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
    )
    # Verify connectivity
    await _redis.ping()
    logger.info("Redis connection initialized")


async def close_redis() -> None:
    """Close Redis connection."""
    global _redis
    if _redis:
        await _redis.close()
        logger.info("Redis connection closed")


def get_redis() -> Redis:
    """Get the Redis client instance."""
    if _redis is None:
        raise RuntimeError("Redis not initialized. Call init_redis() first.")
    return _redis
