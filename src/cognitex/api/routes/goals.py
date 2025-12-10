"""Goal management API endpoints."""

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter()


class GoalCreate(BaseModel):
    title: str = Field(..., max_length=500)
    description: str | None = None
    timeframe: str = Field(..., pattern="^(yearly|quarterly|monthly|weekly)$")
    domain: str | None = None
    key_results: list[dict[str, Any]] = Field(default_factory=list)
    parent_id: UUID | None = None


class GoalUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=500)
    description: str | None = None
    status: str | None = None
    progress: int | None = Field(default=None, ge=0, le=100)
    key_results: list[dict[str, Any]] | None = None


class GoalResponse(BaseModel):
    id: UUID
    title: str
    description: str | None
    timeframe: str
    domain: str | None
    status: str
    progress: int
    key_results: list[dict[str, Any]]
    parent_id: UUID | None
    created_at: datetime
    updated_at: datetime


@router.get("/")
async def list_goals(
    timeframe: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[GoalResponse]:
    """List goals with optional filtering."""
    # TODO: Implement with database
    return []


@router.post("/", status_code=201)
async def create_goal(goal: GoalCreate) -> GoalResponse:
    """Create a new goal."""
    # TODO: Implement with database
    raise HTTPException(status_code=501, detail="Not implemented")


@router.get("/{goal_id}")
async def get_goal(goal_id: UUID) -> GoalResponse:
    """Get a specific goal by ID."""
    # TODO: Implement with database
    raise HTTPException(status_code=404, detail="Goal not found")


@router.patch("/{goal_id}")
async def update_goal(goal_id: UUID, updates: GoalUpdate) -> GoalResponse:
    """Update a goal."""
    # TODO: Implement with database
    raise HTTPException(status_code=404, detail="Goal not found")


@router.delete("/{goal_id}", status_code=204)
async def delete_goal(goal_id: UUID) -> None:
    """Delete a goal."""
    # TODO: Implement with database
    pass
