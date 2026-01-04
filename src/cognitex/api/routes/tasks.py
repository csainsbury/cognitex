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
    from cognitex.services.tasks import get_task_service

    service = get_task_service()
    tasks = await service.list(status=status, limit=limit)

    # Map dictionary response to Pydantic model
    return [
        TaskResponse(
            id=t["id"],
            title=t["title"],
            description=t.get("description"),
            status=t.get("status", "pending"),
            energy_cost=t.get("energy_cost"),
            due_date=t.get("due"),  # Service returns 'due' alias
            created_at=t.get("created_at"),
            updated_at=t.get("updated_at"),
            completed_at=t.get("completed_at"),
            source_type=t.get("source_type"),
            source_id=t.get("source_id"),
            project_id=t.get("project_id"),
        )
        for t in tasks
    ]


@router.post("/", status_code=201)
async def create_task(task: TaskCreate) -> TaskResponse:
    """Create a new task."""
    from cognitex.services.tasks import get_task_service

    service = get_task_service()
    created = await service.create(
        title=task.title,
        description=task.description,
        energy_cost=str(task.energy_cost) if task.energy_cost else None,
        due_date=task.due_date.isoformat() if task.due_date else None,
        project_id=str(task.project_id) if task.project_id else None,
    )

    # Re-fetch to get full object for response
    full_task = await service.get(created["id"])
    if not full_task:
        raise HTTPException(status_code=500, detail="Failed to retrieve created task")

    return TaskResponse(
        id=full_task["id"],
        title=full_task["title"],
        description=full_task.get("description"),
        status=full_task.get("status", "pending"),
        energy_cost=full_task.get("energy_cost"),
        due_date=full_task.get("due"),
        created_at=full_task.get("created_at"),
        updated_at=full_task.get("updated_at"),
        project_id=full_task.get("project_id"),
        source_type=full_task.get("source_type"),
        source_id=full_task.get("source_id"),
        completed_at=None,
    )


@router.get("/{task_id}")
async def get_task(task_id: UUID) -> TaskResponse:
    """Get a specific task by ID."""
    from cognitex.services.tasks import get_task_service

    service = get_task_service()
    task = await service.get(str(task_id))

    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    return TaskResponse(
        id=task["id"],
        title=task["title"],
        description=task.get("description"),
        status=task.get("status", "pending"),
        energy_cost=task.get("energy_cost"),
        due_date=task.get("due"),
        created_at=task.get("created_at"),
        updated_at=task.get("updated_at"),
        completed_at=task.get("completed_at"),
        source_type=task.get("source_type"),
        source_id=task.get("source_id"),
        project_id=task.get("project_id"),
    )


@router.patch("/{task_id}")
async def update_task(task_id: UUID, updates: TaskUpdate) -> TaskResponse:
    """Update a task."""
    from cognitex.services.tasks import get_task_service

    service = get_task_service()

    # Check task exists
    existing = await service.get(str(task_id))
    if not existing:
        raise HTTPException(status_code=404, detail="Task not found")

    # Apply updates
    await service.update(
        task_id=str(task_id),
        title=updates.title,
        description=updates.description,
        status=updates.status,
        energy_cost=str(updates.energy_cost) if updates.energy_cost else None,
        due_date=updates.due_date.isoformat() if updates.due_date else None,
    )

    # Re-fetch to get full object for response
    task = await service.get(str(task_id))
    return TaskResponse(
        id=task["id"],
        title=task["title"],
        description=task.get("description"),
        status=task.get("status", "pending"),
        energy_cost=task.get("energy_cost"),
        due_date=task.get("due"),
        created_at=task.get("created_at"),
        updated_at=task.get("updated_at"),
        completed_at=task.get("completed_at"),
        source_type=task.get("source_type"),
        source_id=task.get("source_id"),
        project_id=task.get("project_id"),
    )


@router.delete("/{task_id}", status_code=204)
async def delete_task(task_id: UUID) -> None:
    """Delete a task."""
    from cognitex.services.tasks import get_task_service

    service = get_task_service()

    # Check task exists
    existing = await service.get(str(task_id))
    if not existing:
        raise HTTPException(status_code=404, detail="Task not found")

    await service.delete(str(task_id))
