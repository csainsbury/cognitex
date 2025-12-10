"""Task management API endpoints."""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter()


class TaskCreate(BaseModel):
    title: str = Field(..., max_length=500)
    description: str | None = None
    energy_cost: int | None = Field(default=None, ge=1, le=10)
    due_date: datetime | None = None
    project_id: UUID | None = None


class TaskUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=500)
    description: str | None = None
    status: str | None = None
    energy_cost: int | None = Field(default=None, ge=1, le=10)
    due_date: datetime | None = None


class TaskResponse(BaseModel):
    id: UUID
    title: str
    description: str | None
    status: str
    energy_cost: int | None
    due_date: datetime | None
    source_type: str | None
    source_id: str | None
    project_id: UUID | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


@router.get("/")
async def list_tasks(
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[TaskResponse]:
    """List tasks with optional filtering."""
    # TODO: Implement with database
    return []


@router.post("/", status_code=201)
async def create_task(task: TaskCreate) -> TaskResponse:
    """Create a new task."""
    # TODO: Implement with database
    raise HTTPException(status_code=501, detail="Not implemented")


@router.get("/{task_id}")
async def get_task(task_id: UUID) -> TaskResponse:
    """Get a specific task by ID."""
    # TODO: Implement with database
    raise HTTPException(status_code=404, detail="Task not found")


@router.patch("/{task_id}")
async def update_task(task_id: UUID, updates: TaskUpdate) -> TaskResponse:
    """Update a task."""
    # TODO: Implement with database
    raise HTTPException(status_code=404, detail="Task not found")


@router.delete("/{task_id}", status_code=204)
async def delete_task(task_id: UUID) -> None:
    """Delete a task."""
    # TODO: Implement with database
    pass
