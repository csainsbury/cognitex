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
    from cognitex.services.tasks import get_goal_service

    service = get_goal_service()
    goals = await service.list(
        status=status,
        timeframe=timeframe,
        limit=limit,
    )

    return [
        GoalResponse(
            id=g["id"],
            title=g["title"],
            description=g.get("description"),
            timeframe=g.get("timeframe", "ongoing"),
            domain=g.get("domain"),
            status=g.get("status", "active"),
            progress=g.get("progress", 0),
            key_results=g.get("key_results", []),
            parent_id=g.get("parent_goal_id"),
            created_at=g.get("created_at"),
            updated_at=g.get("updated_at"),
        )
        for g in goals
    ]


@router.post("/", status_code=201)
async def create_goal(goal: GoalCreate) -> GoalResponse:
    """Create a new goal."""
    from cognitex.services.tasks import get_goal_service

    service = get_goal_service()
    created = await service.create(
        title=goal.title,
        description=goal.description,
        timeframe=goal.timeframe,
        parent_goal_id=str(goal.parent_id) if goal.parent_id else None,
    )

    # Re-fetch to get full object
    full_goal = await service.get(created["id"])
    if not full_goal:
        raise HTTPException(status_code=500, detail="Failed to retrieve created goal")

    return GoalResponse(
        id=full_goal["id"],
        title=full_goal["title"],
        description=full_goal.get("description"),
        timeframe=full_goal.get("timeframe", "ongoing"),
        domain=full_goal.get("domain"),
        status=full_goal.get("status", "active"),
        progress=full_goal.get("progress", 0),
        key_results=full_goal.get("key_results", []),
        parent_id=full_goal.get("parent_goal_id"),
        created_at=full_goal.get("created_at"),
        updated_at=full_goal.get("updated_at"),
    )


@router.get("/{goal_id}")
async def get_goal(goal_id: UUID) -> GoalResponse:
    """Get a specific goal by ID."""
    from cognitex.services.tasks import get_goal_service

    service = get_goal_service()
    goal = await service.get(str(goal_id))

    if not goal:
        raise HTTPException(status_code=404, detail="Goal not found")

    return GoalResponse(
        id=goal["id"],
        title=goal["title"],
        description=goal.get("description"),
        timeframe=goal.get("timeframe", "ongoing"),
        domain=goal.get("domain"),
        status=goal.get("status", "active"),
        progress=goal.get("progress", 0),
        key_results=goal.get("key_results", []),
        parent_id=goal.get("parent_goal_id"),
        created_at=goal.get("created_at"),
        updated_at=goal.get("updated_at"),
    )


@router.patch("/{goal_id}")
async def update_goal(goal_id: UUID, updates: GoalUpdate) -> GoalResponse:
    """Update a goal."""
    from cognitex.services.tasks import get_goal_service

    service = get_goal_service()

    # Check goal exists
    existing = await service.get(str(goal_id))
    if not existing:
        raise HTTPException(status_code=404, detail="Goal not found")

    # Apply updates
    await service.update(
        goal_id=str(goal_id),
        title=updates.title,
        description=updates.description,
        status=updates.status,
    )

    # Re-fetch to get full object
    goal = await service.get(str(goal_id))
    return GoalResponse(
        id=goal["id"],
        title=goal["title"],
        description=goal.get("description"),
        timeframe=goal.get("timeframe", "ongoing"),
        domain=goal.get("domain"),
        status=goal.get("status", "active"),
        progress=goal.get("progress", 0),
        key_results=goal.get("key_results", []),
        parent_id=goal.get("parent_goal_id"),
        created_at=goal.get("created_at"),
        updated_at=goal.get("updated_at"),
    )


@router.delete("/{goal_id}", status_code=204)
async def delete_goal(goal_id: UUID) -> None:
    """Delete a goal."""
    from cognitex.services.tasks import get_goal_service

    service = get_goal_service()

    # Check goal exists
    existing = await service.get(str(goal_id))
    if not existing:
        raise HTTPException(status_code=404, detail="Goal not found")

    await service.delete(str(goal_id))
