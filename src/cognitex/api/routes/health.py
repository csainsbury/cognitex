"""Health check endpoints."""

from fastapi import APIRouter
from pydantic import BaseModel

from cognitex import __version__

router = APIRouter()


class HealthResponse(BaseModel):
    status: str
    version: str


@router.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    """Basic health check endpoint."""
    return HealthResponse(status="healthy", version=__version__)
