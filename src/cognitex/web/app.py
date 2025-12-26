"""FastAPI web application for Cognitex dashboard."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from datetime import date, datetime

from cognitex.services.tasks import (
    get_goal_service,
    get_project_service,
    get_repository_service,
    get_task_service,
)
from cognitex.agent.state_model import (
    OperatingMode,
    ModeRules,
    UserState,
    ContinuousSignals,
    get_state_estimator,
)
from cognitex.agent.interruption_firewall import get_interruption_firewall

# Template and static directories
TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

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
    from cognitex.db.postgres import init_postgres, close_postgres
    from cognitex.db.redis import init_redis, close_redis

    # Initialize database connections
    await init_neo4j()
    await init_graph_schema()
    await init_postgres()
    await init_redis()

    yield

    # Cleanup
    await close_redis()
    await close_neo4j()
    await close_postgres()


app = FastAPI(
    title="Cognitex Dashboard",
    description="Visual overview for tasks, projects, and goals",
    lifespan=lifespan,
)

# Mount static files
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Serve favicon."""
    favicon_path = STATIC_DIR / "favicon.ico"
    if favicon_path.exists():
        return FileResponse(favicon_path)
    raise HTTPException(status_code=404, detail="Favicon not found")


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
# Repositories API
# -------------------------------------------------------------------


@app.get("/api/repositories")
async def api_repositories():
    """List all repositories for dropdowns."""
    repo_service = get_repository_service()
    repos = await repo_service.list(limit=100)
    return JSONResponse([
        {"id": r["id"], "full_name": r["full_name"], "name": r["name"]}
        for r in repos
    ])


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
    project_id: Annotated[str | None, Form()] = None,
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

    # Handle relationships
    from cognitex.db.neo4j import get_neo4j_session
    from cognitex.db.graph_schema import link_task_to_person, link_task_to_project

    async for session in get_neo4j_session():
        # Remove old people relationships
        await session.run(
            "MATCH (t:Task {id: $task_id})-[r:INVOLVES|ASSIGNED_TO]->(:Person) DELETE r",
            {"task_id": task_id}
        )
        # Add new people relationships
        if people:
            for email in people:
                if email:
                    await link_task_to_person(session, task_id, email, relationship_type="INVOLVES")

        # Update project link (remove old, add new if specified)
        await session.run(
            "MATCH (t:Task {id: $task_id})-[r:PART_OF]->(:Project) DELETE r",
            {"task_id": task_id}
        )
        if project_id:
            await link_task_to_project(session, task_id, project_id)
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
    repo_service = get_repository_service()
    goals = await goal_service.list(limit=100)
    repos = await repo_service.list(limit=100)
    people = await get_people()

    return templates.TemplateResponse(
        "partials/project_new.html",
        {"request": request, "goals": goals, "repos": repos, "people": people},
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
    repo_service = get_repository_service()

    project = await project_service.get(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    goals = await goal_service.list(limit=100)
    repos = await repo_service.list(limit=100)
    people = await get_people()

    return templates.TemplateResponse(
        "partials/project_edit.html",
        {"request": request, "project": project, "goals": goals, "repos": repos, "people": people},
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
    repository_id: Annotated[str | None, Form()] = None,
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

    # Handle relationships
    from cognitex.db.neo4j import get_neo4j_session
    from cognitex.db.graph_schema import link_project_to_person, link_project_to_repository

    async for session in get_neo4j_session():
        # Remove old people relationships (both directions)
        await session.run(
            "MATCH (p:Project {id: $project_id})-[r:OWNED_BY]->(:Person) DELETE r",
            {"project_id": project_id}
        )
        await session.run(
            "MATCH (p:Project {id: $project_id})<-[r:STAKEHOLDER]-(:Person) DELETE r",
            {"project_id": project_id}
        )
        # Add new people relationships
        if people:
            for email in people:
                if email:
                    await link_project_to_person(session, project_id, email, role="stakeholder")

        # Update repository link (remove old, add new if specified)
        await session.run(
            "MATCH (p:Project {id: $project_id})-[r:USES_REPO]->(:Repository) DELETE r",
            {"project_id": project_id}
        )
        if repository_id:
            await link_project_to_repository(session, project_id, repository_id)
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
    repository_id: Annotated[str | None, Form()] = None,
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

    from cognitex.db.neo4j import get_neo4j_session
    from cognitex.db.graph_schema import link_project_to_person, link_project_to_repository

    async for session in get_neo4j_session():
        # Link repository if specified
        if repository_id:
            await link_project_to_repository(session, project["id"], repository_id)

        # Link additional people as stakeholders
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


# -------------------------------------------------------------------
# Today / Day Plan
# -------------------------------------------------------------------


async def get_today_events() -> list[dict]:
    """Get calendar events for today."""
    from cognitex.db.neo4j import get_neo4j_session

    today = date.today()
    today_start = datetime.combine(today, datetime.min.time())
    today_end = datetime.combine(today, datetime.max.time())

    events = []
    async for session in get_neo4j_session():
        query = """
        MATCH (e:Event)
        WHERE e.start >= datetime($start) AND e.start <= datetime($end)
        OPTIONAL MATCH (e)-[:ATTENDED_BY]->(p:Person)
        WITH e, collect(p.email) as attendees
        RETURN e.gcal_id as id, e.title as title, e.start as start_time,
               e.end as end_time, e.description as description, attendees
        ORDER BY e.start
        """
        result = await session.run(query, {
            "start": today_start.isoformat(),
            "end": today_end.isoformat(),
        })
        data = await result.data()

        for event in data:
            start = event.get("start_time")
            end = event.get("end_time")
            if start:
                start_dt = datetime.fromisoformat(start) if isinstance(start, str) else start
                event["start_time"] = start_dt.strftime("%H:%M")
            if end:
                end_dt = datetime.fromisoformat(end) if isinstance(end, str) else end
                event["end_time"] = end_dt.strftime("%H:%M")

            # Context pack will be added later via context pack compiler
            event["context_pack"] = None
            events.append(event)
        break
    return events


@app.get("/today", response_class=HTMLResponse)
async def today_page(request: Request):
    """Today / Day Plan page with calendar, tasks, and state."""
    task_service = get_task_service()

    # Get current state
    estimator = get_state_estimator()
    state = await estimator.get_current_state()
    if not state:
        state = UserState(
            mode=OperatingMode.FRAGMENTED,
            signals=ContinuousSignals(),
        )

    # Get today's events
    events = await get_today_events()

    # Get priority tasks
    tasks = await task_service.list(status="pending", limit=10)
    tasks = sorted(tasks, key=lambda t: {"high": 0, "medium": 1, "low": 2}.get(t.get("priority", "medium"), 1))[:5]

    # Get upcoming deadlines
    all_tasks = await task_service.list(include_done=False, limit=50)
    deadlines = [
        {"title": t["title"], "due": t["due_date"]}
        for t in all_tasks
        if t.get("due_date")
    ][:5]

    return templates.TemplateResponse(
        "today.html",
        {
            "request": request,
            "today_date": date.today().strftime("%A, %B %d"),
            "state": state,
            "events": events,
            "tasks": tasks,
            "deadlines": deadlines,
            "briefing": None,  # Generated on demand
        },
    )


# -------------------------------------------------------------------
# Documents / Knowledge Search
# -------------------------------------------------------------------


async def get_document_stats() -> dict:
    """Get document statistics from Neo4j and postgres."""
    from cognitex.db.neo4j import get_neo4j_session
    from cognitex.db.postgres import get_session
    from sqlalchemy import text

    doc_count = 0
    chunk_count = 0

    # Get document count from Neo4j
    async for session in get_neo4j_session():
        result = await session.run("MATCH (d:Document) WHERE d.indexed = true RETURN count(d) as count")
        data = await result.data()
        doc_count = data[0]["count"] if data else 0
        break

    # Get chunk count from postgres
    async for session in get_session():
        chunk_result = await session.execute(text("SELECT COUNT(*) FROM document_chunks"))
        chunk_count = chunk_result.scalar() or 0
        break

    return {"documents": doc_count, "chunks": chunk_count}


async def get_topics(limit: int = 50) -> list[dict]:
    """Get topics with document counts."""
    from cognitex.db.neo4j import get_neo4j_session

    async for session in get_neo4j_session():
        query = """
        MATCH (t:Topic)<-[:HAS_TOPIC]-(c:Chunk)
        WITH t, count(DISTINCT c) as count
        RETURN t.name as name, count
        ORDER BY count DESC
        LIMIT $limit
        """
        result = await session.run(query, {"limit": limit})
        return await result.data()
    return []


async def get_concepts(limit: int = 50) -> list[dict]:
    """Get concepts with document counts."""
    from cognitex.db.neo4j import get_neo4j_session

    async for session in get_neo4j_session():
        query = """
        MATCH (c:Concept)<-[:MENTIONS]-(chunk:Chunk)
        WITH c, count(DISTINCT chunk) as count
        RETURN c.name as name, count
        ORDER BY count DESC
        LIMIT $limit
        """
        result = await session.run(query, {"limit": limit})
        return await result.data()
    return []


async def get_recent_documents(limit: int = 10) -> list[dict]:
    """Get recently indexed documents from Neo4j."""
    from cognitex.db.neo4j import get_neo4j_session

    async for session in get_neo4j_session():
        result = await session.run("""
            MATCH (d:Document)
            WHERE d.indexed = true
            RETURN d.drive_id as id, d.drive_id as drive_id, d.name as name,
                   d.folder_path as folder_path, d.indexed_at as indexed_at
            ORDER BY d.indexed_at DESC
            LIMIT $limit
        """, {"limit": limit})
        return await result.data()
    return []


async def search_documents(query: str, limit: int = 20) -> list[dict]:
    """Search documents by name in Neo4j and text search in postgres."""
    from cognitex.db.neo4j import get_neo4j_session
    from cognitex.db.postgres import get_session
    from sqlalchemy import text

    if not query or len(query) < 2:
        return []

    results = []
    seen_drive_ids = set()

    # First: text search in postgres chunks
    async for session in get_session():
        result = await session.execute(text("""
            SELECT DISTINCT drive_id, content,
                   ts_rank(to_tsvector('english', content), plainto_tsquery('english', :query)) as rank
            FROM document_chunks
            WHERE to_tsvector('english', content) @@ plainto_tsquery('english', :query)
            ORDER BY rank DESC
            LIMIT :limit
        """), {"query": query, "limit": limit})
        rows = result.fetchall()

        for row in rows:
            drive_id = row[0]
            if drive_id not in seen_drive_ids:
                seen_drive_ids.add(drive_id)
                snippet = row[1][:200] + "..." if len(row[1]) > 200 else row[1]
                results.append({
                    "id": drive_id,
                    "drive_id": drive_id,
                    "name": "",  # Will be filled from Neo4j
                    "folder_path": "",
                    "snippet": snippet,
                    "topics": [],
                    "concepts": []
                })
        break

    # Get document metadata from Neo4j
    if results:
        drive_ids = [r["drive_id"] for r in results]
        async for session in get_neo4j_session():
            # Batch query for all documents
            doc_result = await session.run("""
                MATCH (d:Document)
                WHERE d.drive_id IN $drive_ids
                RETURN d.drive_id as drive_id, d.name as name, d.folder_path as folder_path
            """, {"drive_ids": drive_ids})
            doc_data = {d["drive_id"]: d for d in await doc_result.data()}

            for r in results:
                if r["drive_id"] in doc_data:
                    r["name"] = doc_data[r["drive_id"]]["name"]
                    r["folder_path"] = doc_data[r["drive_id"]]["folder_path"]

            # Get topics for each result
            for doc in results:
                topic_result = await session.run("""
                    MATCH (c:Chunk {drive_id: $drive_id})-[:HAS_TOPIC]->(t:Topic)
                    RETURN DISTINCT t.name as name LIMIT 5
                """, {"drive_id": doc["drive_id"]})
                doc["topics"] = [r["name"] for r in await topic_result.data()]

                # Get concepts
                concept_result = await session.run("""
                    MATCH (c:Chunk {drive_id: $drive_id})-[:MENTIONS]->(con:Concept)
                    RETURN DISTINCT con.name as name LIMIT 5
                """, {"drive_id": doc["drive_id"]})
                doc["concepts"] = [r["name"] for r in await concept_result.data()]
            break

    return results


@app.get("/documents", response_class=HTMLResponse)
async def documents_page(
    request: Request,
    topic: str | None = None,
    concept: str | None = None,
):
    """Documents and knowledge search page."""
    topics = await get_topics()
    concepts = await get_concepts()
    recent_docs = await get_recent_documents()
    stats = await get_document_stats()

    results = []
    query = None
    if topic:
        query = f"topic:{topic}"
        results = await search_documents(topic)
    elif concept:
        query = f"concept:{concept}"
        results = await search_documents(concept)

    return templates.TemplateResponse(
        "documents.html",
        {
            "request": request,
            "query": query,
            "results": results,
            "topics": topics,
            "concepts": concepts,
            "recent_docs": recent_docs,
            "stats": stats,
        },
    )


@app.get("/documents/search", response_class=HTMLResponse)
async def documents_search(request: Request, q: str = ""):
    """HTMX endpoint for document search."""
    if not q or len(q) < 2:
        return HTMLResponse('<p class="empty">Enter a search query to find documents</p>')

    results = await search_documents(q)

    if not results:
        return HTMLResponse('<p class="empty">No documents found matching your query</p>')

    html = ""
    for doc in results:
        topics_html = "".join(f'<span class="topic-tag">{t}</span>' for t in doc.get("topics", [])[:5])
        concepts_html = "".join(f'<span class="concept-tag">{c}</span>' for c in doc.get("concepts", [])[:5])
        snippet_html = f'<div class="doc-result-snippet">{doc.get("snippet", "")}</div>' if doc.get("snippet") else ""

        html += f"""
        <div class="doc-result">
            <div class="doc-result-title">{doc['name']}</div>
            <div class="doc-result-path">{doc.get('folder_path', 'Drive')}</div>
            {snippet_html}
            <div class="doc-result-topics">
                {topics_html}
                {concepts_html}
            </div>
        </div>
        """

    return HTMLResponse(html)


# -------------------------------------------------------------------
# State / Mode Management
# -------------------------------------------------------------------


@app.get("/state", response_class=HTMLResponse)
async def state_page(request: Request):
    """Operating state and mode management page."""
    estimator = get_state_estimator()
    state = await estimator.get_current_state()

    if not state:
        state = UserState(
            mode=OperatingMode.FRAGMENTED,
            signals=ContinuousSignals(),
        )

    rules = ModeRules.get_rules(state.mode)
    mode_description = rules.get("description", "")

    # Get captured items from firewall
    firewall = get_interruption_firewall()
    captured_items = await firewall.get_queued_items(limit=10)

    # Get switch stats for today
    switch_stats = await firewall.get_daily_switch_stats()

    # Convert IncomingItem objects to dicts for template
    captured_dicts = [
        {
            "subject": item.subject,
            "source": item.source,
            "urgency": item.urgency.value,
            "suggested_action": item.suggested_action,
        }
        for item in captured_items
    ]

    return templates.TemplateResponse(
        "state.html",
        {
            "request": request,
            "state": state,
            "rules": rules,
            "mode_description": mode_description,
            "available_modes": list(OperatingMode),
            "captured_items": captured_dicts,
            "switch_stats": switch_stats,
        },
    )


@app.post("/api/state/update", response_class=HTMLResponse)
async def api_state_update(mode: str):
    """Update operating mode."""
    try:
        new_mode = OperatingMode(mode)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid mode: {mode}")

    estimator = get_state_estimator()
    await estimator.update_state(mode=new_mode)

    return HTMLResponse("")


@app.post("/api/state/signal", response_class=HTMLResponse)
async def api_state_signal(
    fatigue_delta: float = 0,
):
    """Update continuous signals."""
    estimator = get_state_estimator()
    await estimator.update_state(fatigue_delta=fatigue_delta)

    return HTMLResponse("")


@app.post("/api/briefing/generate", response_class=HTMLResponse)
async def api_generate_briefing(request: Request):
    """Generate morning briefing."""
    from cognitex.agent.core import CognitexAgent

    agent = CognitexAgent()
    briefing = await agent.morning_briefing()

    return HTMLResponse(f'<div class="briefing-content">{briefing}</div>')


# -------------------------------------------------------------------
# Chat
# -------------------------------------------------------------------


@app.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request):
    """Chat page for interacting with the agent."""
    from cognitex.services.model_config import get_model_config_service

    service = get_model_config_service()
    config = await service.get_config()

    return templates.TemplateResponse(
        "chat.html",
        {
            "request": request,
            "provider": config.provider.title(),
            "model": config.planner_model,
        },
    )


@app.post("/api/chat")
async def api_chat(request: Request):
    """Send a message to the agent and get a response."""
    from cognitex.agent.core import Agent

    data = await request.json()
    message = data.get("message", "").strip()

    if not message:
        return JSONResponse({"error": "Message cannot be empty"}, status_code=400)

    try:
        agent = Agent()
        await agent.initialize()
        response, approval_ids = await agent.chat_with_approvals(message)

        return JSONResponse({
            "response": response,
            "approvals": approval_ids,
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/chat/clear")
async def api_chat_clear():
    """Clear chat history and working memory."""
    import redis.asyncio as aioredis
    from cognitex.config import get_settings

    settings = get_settings()
    redis = aioredis.from_url(settings.redis_url)

    try:
        # Clear working memory keys
        keys = await redis.keys("cognitex:memory:working:*")
        if keys:
            await redis.delete(*keys)

        # Clear chat-specific session data if any
        chat_keys = await redis.keys("cognitex:chat:*")
        if chat_keys:
            await redis.delete(*chat_keys)

        return JSONResponse({"status": "cleared"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        await redis.close()


# -------------------------------------------------------------------
# Settings / Model Configuration
# -------------------------------------------------------------------


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    """Settings page for model configuration."""
    from cognitex.services.model_config import get_model_config_service

    service = get_model_config_service()
    config = await service.get_config()

    # Get models for the current provider
    chat_models = service.get_chat_models_for_provider(config.provider)
    embedding_models = service.get_embedding_models_for_provider(config.embedding_provider)

    # Get available providers with API key status
    providers = service.get_available_providers()

    # Check if config is from Redis or defaults
    import redis.asyncio as aioredis
    from cognitex.config import get_settings
    settings = get_settings()
    redis = aioredis.from_url(settings.redis_url)
    try:
        has_redis_config = await redis.exists("cognitex:model_config")
        config_source = "redis" if has_redis_config else "env"
    except Exception:
        config_source = "env"
    finally:
        await redis.close()

    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "config": config,
            "config_source": config_source,
            "chat_models": chat_models,
            "embedding_models": embedding_models,
            "providers": providers,
        },
    )


@app.post("/api/settings/models", response_class=HTMLResponse)
async def api_settings_models_update(
    request: Request,
    planner_model: Annotated[str, Form()],
    executor_model: Annotated[str, Form()],
    embedding_model: Annotated[str, Form()],
    provider: Annotated[str, Form()] = "together",
):
    """Update all model settings."""
    from cognitex.services.model_config import get_model_config_service, ModelConfig

    # Determine embedding provider based on chat provider
    # Anthropic and Google don't have embeddings, so use Together
    embedding_provider = provider if provider in ("together", "openai") else "together"

    service = get_model_config_service()
    config = ModelConfig(
        provider=provider,
        planner_model=planner_model,
        executor_model=executor_model,
        embedding_model=embedding_model,
        embedding_provider=embedding_provider,
    )
    await service.set_config(config)

    return HTMLResponse('<span style="color: #16a34a;">Saved!</span>')


@app.post("/api/settings/models/reset", response_class=HTMLResponse)
async def api_settings_models_reset(request: Request):
    """Reset models to environment defaults."""
    from cognitex.services.model_config import get_model_config_service
    import redis.asyncio as aioredis
    from cognitex.config import get_settings

    settings = get_settings()
    redis = aioredis.from_url(settings.redis_url)
    try:
        await redis.delete("cognitex:model_config")
    finally:
        await redis.close()

    # Return fresh settings page content
    service = get_model_config_service()
    config = await service.get_config()
    chat_models = service.get_chat_models_for_provider(config.provider)
    embedding_models = service.get_embedding_models_for_provider(config.embedding_provider)
    providers = service.get_available_providers()

    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "config": config,
            "config_source": "env",
            "chat_models": chat_models,
            "embedding_models": embedding_models,
            "providers": providers,
        },
    )


@app.get("/api/settings/models/refresh", response_class=HTMLResponse)
async def api_settings_models_refresh(request: Request):
    """Refresh model list from API (Together.ai only)."""
    from cognitex.services.model_config import get_model_config_service

    service = get_model_config_service()
    config = await service.get_config()

    # Refresh Together.ai model list if applicable
    if config.provider == "together":
        chat_models = await service.list_chat_models(refresh=True)
        embedding_models = await service.list_embedding_models(refresh=True)
    else:
        chat_models = service.get_chat_models_for_provider(config.provider)
        embedding_models = service.get_embedding_models_for_provider(config.embedding_provider)

    providers = service.get_available_providers()

    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "config": config,
            "config_source": "redis",
            "chat_models": chat_models,
            "embedding_models": embedding_models,
            "providers": providers,
        },
    )


@app.post("/api/settings/provider/{provider}", response_class=HTMLResponse)
async def api_settings_provider_switch(request: Request, provider: str):
    """Switch to a different LLM provider."""
    from cognitex.services.model_config import get_model_config_service, ModelConfig

    valid_providers = {"together", "anthropic", "openai", "google"}
    if provider not in valid_providers:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {provider}")

    service = get_model_config_service()

    # Get default models for the new provider
    chat_models = service.get_chat_models_for_provider(provider)

    # Use first model as default for both planner and executor
    default_model = chat_models[0]["id"] if chat_models else ""

    # Determine embedding provider (Anthropic/Google don't have embeddings)
    embedding_provider = provider if provider in ("together", "openai") else "together"
    embedding_models = service.get_embedding_models_for_provider(embedding_provider)
    default_embedding = embedding_models[0]["id"] if embedding_models else "BAAI/bge-base-en-v1.5"

    config = ModelConfig(
        provider=provider,
        planner_model=default_model,
        executor_model=default_model,
        embedding_model=default_embedding,
        embedding_provider=embedding_provider,
    )
    await service.set_config(config)

    providers = service.get_available_providers()

    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "config": config,
            "config_source": "redis",
            "chat_models": chat_models,
            "embedding_models": embedding_models,
            "providers": providers,
        },
    )


@app.post("/api/settings/models/preset/{preset}", response_class=HTMLResponse)
async def api_settings_models_preset(request: Request, preset: str):
    """Apply a preset model configuration."""
    from cognitex.services.model_config import get_model_config_service, ModelConfig

    # Presets organized by provider
    presets = {
        # Together.ai presets
        "together-performance": ModelConfig(
            provider="together",
            planner_model="deepseek-ai/DeepSeek-V3",
            executor_model="deepseek-ai/DeepSeek-V3",
            embedding_model="BAAI/bge-base-en-v1.5",
            embedding_provider="together",
        ),
        "together-reasoning": ModelConfig(
            provider="together",
            planner_model="deepseek-ai/DeepSeek-R1",
            executor_model="deepseek-ai/DeepSeek-V3",
            embedding_model="BAAI/bge-base-en-v1.5",
            embedding_provider="together",
        ),
        "together-fast": ModelConfig(
            provider="together",
            planner_model="meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo",
            executor_model="meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo",
            embedding_model="BAAI/bge-base-en-v1.5",
            embedding_provider="together",
        ),
        # Anthropic presets
        "anthropic-sonnet": ModelConfig(
            provider="anthropic",
            planner_model="claude-sonnet-4-20250514",
            executor_model="claude-sonnet-4-20250514",
            embedding_model="BAAI/bge-base-en-v1.5",
            embedding_provider="together",
        ),
        "anthropic-opus": ModelConfig(
            provider="anthropic",
            planner_model="claude-opus-4-20250514",
            executor_model="claude-sonnet-4-20250514",
            embedding_model="BAAI/bge-base-en-v1.5",
            embedding_provider="together",
        ),
        # OpenAI presets
        "openai-gpt4o": ModelConfig(
            provider="openai",
            planner_model="gpt-4o",
            executor_model="gpt-4o-mini",
            embedding_model="text-embedding-3-small",
            embedding_provider="openai",
        ),
        "openai-o1": ModelConfig(
            provider="openai",
            planner_model="o1",
            executor_model="gpt-4o",
            embedding_model="text-embedding-3-small",
            embedding_provider="openai",
        ),
        # Google presets
        "google-gemini": ModelConfig(
            provider="google",
            planner_model="gemini-3-flash-preview",
            executor_model="gemini-3-flash-preview",
            embedding_model="BAAI/bge-base-en-v1.5",
            embedding_provider="together",
        ),
        "google-gemini-pro": ModelConfig(
            provider="google",
            planner_model="gemini-3-pro-preview",
            executor_model="gemini-3-flash-preview",
            embedding_model="BAAI/bge-base-en-v1.5",
            embedding_provider="together",
        ),
        # Legacy presets (for backwards compatibility)
        "performance": ModelConfig(
            provider="together",
            planner_model="deepseek-ai/DeepSeek-V3",
            executor_model="deepseek-ai/DeepSeek-V3",
            embedding_model="BAAI/bge-base-en-v1.5",
            embedding_provider="together",
        ),
        "reasoning": ModelConfig(
            provider="together",
            planner_model="deepseek-ai/DeepSeek-R1",
            executor_model="deepseek-ai/DeepSeek-V3",
            embedding_model="BAAI/bge-base-en-v1.5",
            embedding_provider="together",
        ),
        "balanced": ModelConfig(
            provider="together",
            planner_model="meta-llama/Llama-3.3-70B-Instruct-Turbo",
            executor_model="meta-llama/Llama-3.3-70B-Instruct-Turbo",
            embedding_model="BAAI/bge-base-en-v1.5",
            embedding_provider="together",
        ),
        "fast": ModelConfig(
            provider="together",
            planner_model="meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo",
            executor_model="meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo",
            embedding_model="BAAI/bge-base-en-v1.5",
            embedding_provider="together",
        ),
    }

    if preset not in presets:
        raise HTTPException(status_code=400, detail=f"Unknown preset: {preset}")

    service = get_model_config_service()
    config = presets[preset]
    await service.set_config(config)

    # Get models for the preset's provider
    chat_models = service.get_chat_models_for_provider(config.provider)
    embedding_models = service.get_embedding_models_for_provider(config.embedding_provider)
    providers = service.get_available_providers()

    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "config": config,
            "config_source": "redis",
            "chat_models": chat_models,
            "embedding_models": embedding_models,
            "providers": providers,
        },
    )


def run_server(host: str = "127.0.0.1", port: int = 8080):
    """Run the web server."""
    import uvicorn
    uvicorn.run(app, host=host, port=port)
