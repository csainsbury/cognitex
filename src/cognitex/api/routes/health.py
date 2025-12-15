"""Health check endpoints."""

from fastapi import APIRouter
from pydantic import BaseModel

import structlog

from cognitex import __version__

router = APIRouter()
logger = structlog.get_logger()


class HealthResponse(BaseModel):
    status: str
    version: str


class DeepHealthResponse(BaseModel):
    status: str
    version: str
    checks: dict[str, str]


@router.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    """Basic health check endpoint."""
    return HealthResponse(status="healthy", version=__version__)


@router.get("/health/deep", response_model=DeepHealthResponse)
async def deep_health_check() -> DeepHealthResponse:
    """
    Deep health check that verifies all dependencies.

    Checks: Neo4j, PostgreSQL, Redis, Together.ai API
    """
    checks = {}

    # Check Neo4j
    try:
        from cognitex.db.neo4j import get_neo4j_session
        async for session in get_neo4j_session():
            result = await session.run("RETURN 1 as n")
            await result.single()
        checks["neo4j"] = "healthy"
    except Exception as e:
        logger.warning("Neo4j health check failed", error=str(e))
        checks["neo4j"] = f"unhealthy: {str(e)[:100]}"

    # Check PostgreSQL
    try:
        from cognitex.db.postgres import get_session
        from sqlalchemy import text
        async for session in get_session():
            await session.execute(text("SELECT 1"))
        checks["postgres"] = "healthy"
    except Exception as e:
        logger.warning("PostgreSQL health check failed", error=str(e))
        checks["postgres"] = f"unhealthy: {str(e)[:100]}"

    # Check Redis
    try:
        from cognitex.db.redis import get_redis
        redis = get_redis()
        await redis.ping()
        checks["redis"] = "healthy"
    except Exception as e:
        logger.warning("Redis health check failed", error=str(e))
        checks["redis"] = f"unhealthy: {str(e)[:100]}"

    # Check Together.ai API (light check - just verify client creation)
    try:
        from cognitex.config import get_settings
        settings = get_settings()
        api_key = settings.together_api_key.get_secret_value()
        if api_key:
            checks["together_api"] = "configured"
        else:
            checks["together_api"] = "not configured"
    except Exception as e:
        logger.warning("Together.ai health check failed", error=str(e))
        checks["together_api"] = f"unhealthy: {str(e)[:100]}"

    # Determine overall status
    all_healthy = all(
        v in ("healthy", "configured")
        for v in checks.values()
    )
    status = "healthy" if all_healthy else "degraded"

    return DeepHealthResponse(
        status=status,
        version=__version__,
        checks=checks,
    )
