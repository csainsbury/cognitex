"""FastAPI web application for Cognitex dashboard."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from cognitex.services.tasks import (
    get_goal_service,
    get_project_service,
    get_task_service,
)

# Template directory
TEMPLATE_DIR = Path(__file__).parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


async def get_people(limit: int = 50) -> list[dict]:
    """Fetch people from the graph for dropdowns."""
    from cognitex.db.neo4j import get_neo4j_session

    async for session in get_neo4j_session():
        query = """
        MATCH (p:Person)
        WHERE p.email IS NOT NULL
        RETURN p.email as email, p.name as name
        ORDER BY p.name, p.email
        LIMIT $limit
        """
        result = await session.run(query, {"limit": limit})
        data = await result.data()
        return data
    return []


async def search_people(query: str, limit: int = 10) -> list[dict]:
    """Search people by name or email."""
    from cognitex.db.neo4j import get_neo4j_session

    if not query or len(query) < 2:
        return []

    async for session in get_neo4j_session():
        cypher = """
        MATCH (p:Person)
        WHERE p.email IS NOT NULL
          AND (toLower(p.email) CONTAINS toLower($query)
               OR toLower(p.name) CONTAINS toLower($query))
        RETURN p.email as email, p.name as name
        ORDER BY
            CASE WHEN toLower(p.name) STARTS WITH toLower($query) THEN 0
                 WHEN toLower(p.email) STARTS WITH toLower($query) THEN 1
                 ELSE 2 END,
            p.name, p.email
        LIMIT $limit
        """
        result = await session.run(cypher, {"query": query, "limit": limit})
        data = await result.data()
        return data
    return []


async def create_person(email: str, name: str | None = None) -> dict:
    """Create a new person in the graph."""
    from cognitex.db.neo4j import get_neo4j_session

    async for session in get_neo4j_session():
        query = """
        MERGE (p:Person {email: $email})
        ON CREATE SET p.name = $name, p.created_at = datetime()
        ON MATCH SET p.name = COALESCE($name, p.name)
        RETURN p.email as email, p.name as name
        """
        result = await session.run(query, {"email": email, "name": name})
        record = await result.single()
        return dict(record) if record else {"email": email, "name": name}
    return {"email": email, "name": name}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    from cognitex.db.neo4j import init_neo4j, close_neo4j
    from cognitex.db.graph_schema import init_graph_schema

    # Initialize database connections
    await init_neo4j()
    await init_graph_schema()

    yield

    # Cleanup
    await close_neo4j()


app = FastAPI(
    title="Cognitex Dashboard",
    description="Visual overview for tasks, projects, and goals",
    lifespan=lifespan,
)


# -------------------------------------------------------------------
# Home / Navigation
# -------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Dashboard home page with quick overview."""
    task_service = get_task_service()
    project_service = get_project_service()
    goal_service = get_goal_service()

    tasks = await task_service.list(limit=10)
    projects = await project_service.list(limit=10)
    goals = await goal_service.list(limit=10)

    # Count stats
    pending_tasks = len([t for t in tasks if t.get("status") != "done"])
    active_projects = len([p for p in projects if p.get("status") == "active"])
    active_goals = len([g for g in goals if g.get("status") == "active"])

    return templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "pending_tasks": pending_tasks,
            "active_projects": active_projects,
            "active_goals": active_goals,
            "recent_tasks": tasks[:5],
            "recent_projects": projects[:5],
        },
    )


# -------------------------------------------------------------------
# People API (for autocomplete)
# -------------------------------------------------------------------


@app.get("/api/people/search")
async def api_people_search(q: str = ""):
    """Search people for autocomplete."""
    results = await search_people(q)
    return JSONResponse(results)


@app.post("/api/people")
async def api_people_create(
    email: Annotated[str, Form()],
    name: Annotated[str | None, Form()] = None,
):
    """Create a new person."""
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Valid email required")
    person = await create_person(email, name)
    return JSONResponse(person)


# -------------------------------------------------------------------
# Tasks
# -------------------------------------------------------------------


@app.get("/tasks", response_class=HTMLResponse)
async def tasks_page(request: Request, status: str | None = None, project: str | None = None):
    """Tasks overview page."""
    task_service = get_task_service()
    project_service = get_project_service()

    tasks = await task_service.list(
        status=status,
        project_id=project,
        include_done=(status == "done"),
        limit=100,
    )
    projects = await project_service.list(limit=100)

    return templates.TemplateResponse(
        "tasks.html",
        {
            "request": request,
            "tasks": tasks,
            "projects": projects,
            "current_status": status,
            "current_project": project,
        },
    )


@app.get("/tasks/new", response_class=HTMLResponse)
async def task_new_form(request: Request):
    """Return new task form (HTMX partial)."""
    project_service = get_project_service()
    projects = await project_service.list(limit=100)
    people = await get_people()

    return templates.TemplateResponse(
        "partials/task_new.html",
        {"request": request, "projects": projects, "people": people},
    )


@app.get("/tasks/cancel-new", response_class=HTMLResponse)
async def task_cancel_new():
    """Return empty row for cancel."""
    return HTMLResponse('<tr id="new-task-row"></tr>')


@app.get("/tasks/{task_id}/edit", response_class=HTMLResponse)
async def task_edit_form(request: Request, task_id: str):
    """Return inline edit form for a task (HTMX partial)."""
    task_service = get_task_service()
    project_service = get_project_service()

    task = await task_service.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    projects = await project_service.list(limit=100)
    people = await get_people()

    return templates.TemplateResponse(
        "partials/task_edit.html",
        {"request": request, "task": task, "projects": projects, "people": people},
    )


@app.get("/tasks/{task_id}/row", response_class=HTMLResponse)
async def task_row(request: Request, task_id: str):
    """Return task row (HTMX partial for cancel)."""
    task_service = get_task_service()

    task = await task_service.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    return templates.TemplateResponse(
        "partials/task_row.html",
        {"request": request, "task": task},
    )


@app.post("/tasks/{task_id}", response_class=HTMLResponse)
async def task_update(
    request: Request,
    task_id: str,
    title: Annotated[str, Form()],
    status: Annotated[str, Form()],
    priority: Annotated[str, Form()],
    due_date: Annotated[str | None, Form()] = None,
    description: Annotated[str | None, Form()] = None,
    people: Annotated[list[str] | None, Form()] = None,
):
    """Update a task and return the updated row."""
    task_service = get_task_service()

    task = await task_service.update(
        task_id=task_id,
        title=title,
        status=status,
        priority=priority,
        due_date=due_date if due_date else None,
        description=description if description else None,
    )

    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Handle people linking
    from cognitex.db.neo4j import get_neo4j_session
    from cognitex.db.graph_schema import link_task_to_person

    async for session in get_neo4j_session():
        # Remove old relationships first
        await session.run(
            "MATCH (t:Task {id: $task_id})-[r:INVOLVES|ASSIGNED_TO]->(:Person) DELETE r",
            {"task_id": task_id}
        )
        # Add new relationships
        if people:
            for email in people:
                if email:
                    await link_task_to_person(session, task_id, email, relationship_type="INVOLVES")
        break

    # Re-fetch to get relationships
    task = await task_service.get(task_id)

    return templates.TemplateResponse(
        "partials/task_row.html",
        {"request": request, "task": task},
    )


@app.post("/tasks/{task_id}/complete", response_class=HTMLResponse)
async def task_complete(request: Request, task_id: str):
    """Mark task as complete."""
    task_service = get_task_service()
    task = await task_service.complete(task_id)

    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    task = await task_service.get(task_id)
    return templates.TemplateResponse(
        "partials/task_row.html",
        {"request": request, "task": task},
    )


@app.delete("/tasks/{task_id}", response_class=HTMLResponse)
async def task_delete(task_id: str):
    """Delete a task."""
    task_service = get_task_service()
    await task_service.delete(task_id)
    return HTMLResponse("")


@app.post("/tasks", response_class=HTMLResponse)
async def task_create(
    request: Request,
    title: Annotated[str, Form()],
    status: Annotated[str, Form()] = "pending",
    priority: Annotated[str, Form()] = "medium",
    due_date: Annotated[str | None, Form()] = None,
    description: Annotated[str | None, Form()] = None,
    project_id: Annotated[str | None, Form()] = None,
    people: Annotated[list[str] | None, Form()] = None,
):
    """Create a new task."""
    task_service = get_task_service()

    # Filter out empty strings
    assignee_emails = [e for e in (people or []) if e] or None

    task = await task_service.create(
        title=title,
        status=status,
        priority=priority,
        due_date=due_date if due_date else None,
        description=description if description else None,
        project_id=project_id if project_id else None,
        assignee_emails=assignee_emails,
    )

    task = await task_service.get(task["id"])

    return templates.TemplateResponse(
        "partials/task_row.html",
        {"request": request, "task": task},
    )


# -------------------------------------------------------------------
# Projects
# -------------------------------------------------------------------


@app.get("/projects", response_class=HTMLResponse)
async def projects_page(request: Request, status: str | None = None):
    """Projects overview page."""
    project_service = get_project_service()
    goal_service = get_goal_service()

    projects = await project_service.list(
        status=status,
        include_archived=(status == "archived"),
        limit=100,
    )
    goals = await goal_service.list(limit=100)

    return templates.TemplateResponse(
        "projects.html",
        {
            "request": request,
            "projects": projects,
            "goals": goals,
            "current_status": status,
        },
    )


@app.get("/projects/new", response_class=HTMLResponse)
async def project_new_form(request: Request):
    """Return new project form (HTMX partial)."""
    goal_service = get_goal_service()
    goals = await goal_service.list(limit=100)
    people = await get_people()

    return templates.TemplateResponse(
        "partials/project_new.html",
        {"request": request, "goals": goals, "people": people},
    )


@app.get("/projects/cancel-new", response_class=HTMLResponse)
async def project_cancel_new():
    """Return empty row for cancel."""
    return HTMLResponse('<tr id="new-project-row"></tr>')


@app.get("/projects/{project_id}/edit", response_class=HTMLResponse)
async def project_edit_form(request: Request, project_id: str):
    """Return inline edit form for a project (HTMX partial)."""
    project_service = get_project_service()
    goal_service = get_goal_service()

    project = await project_service.get(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    goals = await goal_service.list(limit=100)
    people = await get_people()

    return templates.TemplateResponse(
        "partials/project_edit.html",
        {"request": request, "project": project, "goals": goals, "people": people},
    )


@app.get("/projects/{project_id}/row", response_class=HTMLResponse)
async def project_row(request: Request, project_id: str):
    """Return project row (HTMX partial for cancel)."""
    project_service = get_project_service()

    project = await project_service.get(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    return templates.TemplateResponse(
        "partials/project_row.html",
        {"request": request, "project": project},
    )


@app.post("/projects/{project_id}", response_class=HTMLResponse)
async def project_update(
    request: Request,
    project_id: str,
    title: Annotated[str, Form()],
    status: Annotated[str, Form()],
    target_date: Annotated[str | None, Form()] = None,
    description: Annotated[str | None, Form()] = None,
    people: Annotated[list[str] | None, Form()] = None,
):
    """Update a project and return the updated row."""
    project_service = get_project_service()

    project = await project_service.update(
        project_id=project_id,
        title=title,
        status=status,
        target_date=target_date if target_date else None,
        description=description if description else None,
    )

    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Handle people linking
    from cognitex.db.neo4j import get_neo4j_session
    from cognitex.db.graph_schema import link_project_to_person

    async for session in get_neo4j_session():
        # Remove old relationships first
        await session.run(
            "MATCH (p:Project {id: $project_id})-[r:INVOLVES|OWNED_BY|STAKEHOLDER]->(:Person) DELETE r",
            {"project_id": project_id}
        )
        # Add new relationships
        if people:
            for email in people:
                if email:
                    await link_project_to_person(session, project_id, email, role="stakeholder")
        break

    project = await project_service.get(project_id)

    return templates.TemplateResponse(
        "partials/project_row.html",
        {"request": request, "project": project},
    )


@app.delete("/projects/{project_id}", response_class=HTMLResponse)
async def project_delete(project_id: str):
    """Delete a project."""
    project_service = get_project_service()
    await project_service.delete(project_id)
    return HTMLResponse("")


@app.post("/projects", response_class=HTMLResponse)
async def project_create(
    request: Request,
    title: Annotated[str, Form()],
    status: Annotated[str, Form()] = "planning",
    target_date: Annotated[str | None, Form()] = None,
    description: Annotated[str | None, Form()] = None,
    goal_id: Annotated[str | None, Form()] = None,
    people: Annotated[list[str] | None, Form()] = None,
):
    """Create a new project."""
    project_service = get_project_service()

    # Filter out empty strings, use first person as owner
    people_emails = [e for e in (people or []) if e]
    owner_email = people_emails[0] if people_emails else None

    project = await project_service.create(
        title=title,
        status=status,
        target_date=target_date if target_date else None,
        description=description if description else None,
        goal_id=goal_id if goal_id else None,
        owner_email=owner_email,
    )

    # Link additional people as stakeholders
    if len(people_emails) > 1:
        from cognitex.db.neo4j import get_neo4j_session
        from cognitex.db.graph_schema import link_project_to_person

        async for session in get_neo4j_session():
            for email in people_emails[1:]:
                await link_project_to_person(session, project["id"], email, role="stakeholder")
            break

    project = await project_service.get(project["id"])

    return templates.TemplateResponse(
        "partials/project_row.html",
        {"request": request, "project": project},
    )


# -------------------------------------------------------------------
# Goals
# -------------------------------------------------------------------


@app.get("/goals", response_class=HTMLResponse)
async def goals_page(request: Request, status: str | None = None, timeframe: str | None = None):
    """Goals overview page."""
    goal_service = get_goal_service()

    goals = await goal_service.list(
        status=status,
        timeframe=timeframe,
        include_achieved=(status == "achieved"),
        limit=100,
    )
    people = await get_people()

    return templates.TemplateResponse(
        "goals.html",
        {
            "request": request,
            "goals": goals,
            "people": people,
            "current_status": status,
            "current_timeframe": timeframe,
        },
    )


@app.get("/goals/new", response_class=HTMLResponse)
async def goal_new_form(request: Request):
    """Return new goal form (HTMX partial)."""
    goal_service = get_goal_service()
    goals = await goal_service.list(limit=100)
    people = await get_people()

    return templates.TemplateResponse(
        "partials/goal_new.html",
        {"request": request, "goals": goals, "people": people},
    )


@app.get("/goals/cancel-new", response_class=HTMLResponse)
async def goal_cancel_new():
    """Return empty row for cancel."""
    return HTMLResponse('<tr id="new-goal-row"></tr>')


@app.get("/goals/{goal_id}/edit", response_class=HTMLResponse)
async def goal_edit_form(request: Request, goal_id: str):
    """Return inline edit form for a goal (HTMX partial)."""
    goal_service = get_goal_service()

    goal = await goal_service.get(goal_id)
    if not goal:
        raise HTTPException(status_code=404, detail="Goal not found")

    all_goals = await goal_service.list(limit=100)
    other_goals = [g for g in all_goals if g["id"] != goal_id]
    people = await get_people()

    return templates.TemplateResponse(
        "partials/goal_edit.html",
        {"request": request, "goal": goal, "other_goals": other_goals, "people": people},
    )


@app.get("/goals/{goal_id}/row", response_class=HTMLResponse)
async def goal_row(request: Request, goal_id: str):
    """Return goal row (HTMX partial for cancel)."""
    goal_service = get_goal_service()

    goal = await goal_service.get(goal_id)
    if not goal:
        raise HTTPException(status_code=404, detail="Goal not found")

    return templates.TemplateResponse(
        "partials/goal_row.html",
        {"request": request, "goal": goal},
    )


@app.post("/goals/{goal_id}", response_class=HTMLResponse)
async def goal_update(
    request: Request,
    goal_id: str,
    title: Annotated[str, Form()],
    status: Annotated[str, Form()],
    timeframe: Annotated[str | None, Form()] = None,
    description: Annotated[str | None, Form()] = None,
    people: Annotated[list[str] | None, Form()] = None,
):
    """Update a goal and return the updated row."""
    goal_service = get_goal_service()

    goal = await goal_service.update(
        goal_id=goal_id,
        title=title,
        status=status,
        timeframe=timeframe if timeframe else None,
        description=description if description else None,
    )

    if not goal:
        raise HTTPException(status_code=404, detail="Goal not found")

    # Handle people linking
    from cognitex.db.neo4j import get_neo4j_session
    from cognitex.db.graph_schema import link_goal_to_person

    async for session in get_neo4j_session():
        # Remove old relationships first
        await session.run(
            "MATCH (g:Goal {id: $goal_id})-[r:OWNED_BY|STAKEHOLDER]->(:Person) DELETE r",
            {"goal_id": goal_id}
        )
        # Add new relationships
        if people:
            for email in people:
                if email:
                    await link_goal_to_person(session, goal_id, email, role="stakeholder")
        break

    goal = await goal_service.get(goal_id)

    return templates.TemplateResponse(
        "partials/goal_row.html",
        {"request": request, "goal": goal},
    )


@app.delete("/goals/{goal_id}", response_class=HTMLResponse)
async def goal_delete(goal_id: str):
    """Delete a goal."""
    goal_service = get_goal_service()
    await goal_service.delete(goal_id)
    return HTMLResponse("")


@app.post("/goals", response_class=HTMLResponse)
async def goal_create(
    request: Request,
    title: Annotated[str, Form()],
    status: Annotated[str, Form()] = "active",
    timeframe: Annotated[str | None, Form()] = None,
    description: Annotated[str | None, Form()] = None,
    parent_goal_id: Annotated[str | None, Form()] = None,
    people: Annotated[list[str] | None, Form()] = None,
):
    """Create a new goal."""
    goal_service = get_goal_service()

    goal = await goal_service.create(
        title=title,
        status=status,
        timeframe=timeframe if timeframe else None,
        description=description if description else None,
        parent_goal_id=parent_goal_id if parent_goal_id else None,
    )

    # Handle people linking
    people_emails = [e for e in (people or []) if e]
    if people_emails:
        from cognitex.db.neo4j import get_neo4j_session
        from cognitex.db.graph_schema import link_goal_to_person

        async for session in get_neo4j_session():
            # First person is owner, rest are stakeholders
            await link_goal_to_person(session, goal["id"], people_emails[0], role="owner")
            for email in people_emails[1:]:
                await link_goal_to_person(session, goal["id"], email, role="stakeholder")
            break

    goal = await goal_service.get(goal["id"])

    return templates.TemplateResponse(
        "partials/goal_row.html",
        {"request": request, "goal": goal},
    )


# -------------------------------------------------------------------
# Goal detail with projects and tasks
# -------------------------------------------------------------------


@app.get("/goals/{goal_id}", response_class=HTMLResponse)
async def goal_detail(request: Request, goal_id: str):
    """Goal detail page showing linked projects and tasks."""
    goal_service = get_goal_service()

    goal = await goal_service.get(goal_id)
    if not goal:
        raise HTTPException(status_code=404, detail="Goal not found")

    projects = await goal_service.get_projects(goal_id)
    tasks = await goal_service.get_tasks(goal_id, include_done=True)

    return templates.TemplateResponse(
        "goal_detail.html",
        {
            "request": request,
            "goal": goal,
            "projects": projects,
            "tasks": tasks,
        },
    )


def run_server(host: str = "127.0.0.1", port: int = 8080):
    """Run the web server."""
    import uvicorn
    uvicorn.run(app, host=host, port=port)
