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
    import structlog
    from cognitex.db.neo4j import init_neo4j, close_neo4j
    from cognitex.db.graph_schema import init_graph_schema
    from cognitex.db.postgres import init_postgres, close_postgres
    from cognitex.db.redis import init_redis, close_redis
    from cognitex.agent.triggers import start_triggers, stop_triggers
    from cognitex.services.push_notifications import get_watch_manager
    from cognitex.config import get_settings

    logger = structlog.get_logger()

    # Initialize database connections
    await init_neo4j()
    await init_graph_schema()
    await init_postgres()
    await init_redis()

    # Start the full trigger system (includes autonomous agent + event listeners)
    try:
        await start_triggers()
        logger.info("Trigger system started (includes event listeners + autonomous agent)")
    except Exception as e:
        logger.error("Failed to start trigger system", error=str(e))

    # Set up Gmail watch for push notifications
    try:
        settings = get_settings()
        if settings.google_pubsub_topic:
            watch_manager = get_watch_manager()
            result = await watch_manager.setup_gmail_watch()
            if "error" not in result:
                logger.info("Gmail watch set up successfully", history_id=result.get("historyId"))
            else:
                logger.warning("Gmail watch setup failed", error=result.get("error"))
        else:
            logger.info("No GOOGLE_PUBSUB_TOPIC configured, skipping Gmail watch setup")
    except Exception as e:
        logger.warning("Failed to set up Gmail watch", error=str(e))

    yield

    # Cleanup
    try:
        await stop_triggers()
    except Exception:
        pass
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
    goal_service = get_goal_service()

    task = await task_service.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    projects = await project_service.list(limit=100)
    goals = await goal_service.list(limit=100)
    people = await get_people()

    return templates.TemplateResponse(
        "partials/task_edit.html",
        {"request": request, "task": task, "projects": projects, "goals": goals, "people": people},
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
    goal_id: Annotated[str | None, Form()] = None,
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
    from cognitex.db.graph_schema import link_task_to_person, link_task_to_project, link_task_to_goal

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

        # Update goal link (remove old, add new if specified)
        await session.run(
            "MATCH (t:Task {id: $task_id})-[r:CONTRIBUTES_TO]->(:Goal) DELETE r",
            {"task_id": task_id}
        )
        if goal_id:
            await link_task_to_goal(session, task_id, goal_id)
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
    goal_id: Annotated[str | None, Form()] = None,
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
        goal_id=goal_id if goal_id else "",  # Empty string to unlink
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

    stats = {"documents": 0, "analyzed": 0, "topics": 0, "concepts": 0}

    # Get counts from Neo4j
    async for session in get_neo4j_session():
        result = await session.run("""
            MATCH (d:Document)
            WITH count(d) as total,
                 count(CASE WHEN d.summary IS NOT NULL THEN 1 END) as analyzed
            OPTIONAL MATCH (t:Topic)
            WITH total, analyzed, count(DISTINCT t) as topics
            OPTIONAL MATCH (c:Concept)
            RETURN total, analyzed, topics, count(DISTINCT c) as concepts
        """)
        data = await result.single()
        if data:
            stats["documents"] = data["total"]
            stats["analyzed"] = data["analyzed"]
            stats["topics"] = data["topics"]
            stats["concepts"] = data["concepts"]
        break

    return stats


async def get_topics(limit: int = 50) -> list[dict]:
    """Get topics with document counts."""
    from cognitex.db.neo4j import get_neo4j_session

    async for session in get_neo4j_session():
        # Look for topics linked to Documents (from semantic-analyze)
        query = """
        MATCH (t:Topic)<-[:COVERS]-(d:Document)
        WITH t, count(DISTINCT d) as count
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
        # Look for concepts linked to Documents (from semantic-analyze)
        query = """
        MATCH (c:Concept)<-[:ABOUT]-(d:Document)
        WITH c, count(DISTINCT d) as count
        RETURN c.name as name, count
        ORDER BY count DESC
        LIMIT $limit
        """
        result = await session.run(query, {"limit": limit})
        return await result.data()
    return []


async def get_recent_documents(limit: int = 10) -> list[dict]:
    """Get recently analyzed documents from Neo4j."""
    from cognitex.db.neo4j import get_neo4j_session

    async for session in get_neo4j_session():
        # Get documents that have been analyzed (have summary)
        result = await session.run("""
            MATCH (d:Document)
            WHERE d.summary IS NOT NULL
            RETURN d.drive_id as id, d.drive_id as drive_id, d.name as name,
                   d.folder_path as folder_path, d.analyzed_at as analyzed_at,
                   d.summary as summary
            ORDER BY d.analyzed_at DESC
            LIMIT $limit
        """, {"limit": limit})
        return await result.data()
    return []


async def search_documents(query: str, limit: int = 20) -> list[dict]:
    """Search documents by name, summary, topics, and concepts."""
    from cognitex.db.neo4j import get_neo4j_session

    if not query or len(query) < 2:
        return []

    results = []
    seen_drive_ids = set()
    query_lower = query.lower()

    # Search in Neo4j: document names, summaries, topics, concepts
    async for session in get_neo4j_session():
        # Search documents by name or summary
        doc_result = await session.run("""
            MATCH (d:Document)
            WHERE d.summary IS NOT NULL
              AND (toLower(d.name) CONTAINS $query
                   OR toLower(d.summary) CONTAINS $query)
            RETURN d.drive_id as drive_id, d.name as name, d.folder_path as folder_path,
                   d.summary as snippet
            ORDER BY d.analyzed_at DESC
            LIMIT $limit
        """, {"query": query_lower, "limit": limit})

        for doc in await doc_result.data():
            if doc["drive_id"] not in seen_drive_ids:
                seen_drive_ids.add(doc["drive_id"])
                results.append({
                    "drive_id": doc["drive_id"],
                    "name": doc["name"],
                    "folder_path": doc["folder_path"],
                    "snippet": doc["snippet"][:200] + "..." if doc["snippet"] and len(doc["snippet"]) > 200 else doc["snippet"],
                    "topics": [],
                    "concepts": []
                })

        # Also search by topic/concept names
        if len(results) < limit:
            remaining = limit - len(results)
            topic_result = await session.run("""
                MATCH (d:Document)-[:COVERS]->(t:Topic)
                WHERE toLower(t.name) CONTAINS $query
                  AND d.summary IS NOT NULL
                RETURN DISTINCT d.drive_id as drive_id, d.name as name,
                       d.folder_path as folder_path, d.summary as snippet
                LIMIT $limit
            """, {"query": query_lower, "limit": remaining})

            for doc in await topic_result.data():
                if doc["drive_id"] not in seen_drive_ids:
                    seen_drive_ids.add(doc["drive_id"])
                    results.append({
                        "drive_id": doc["drive_id"],
                        "name": doc["name"],
                        "folder_path": doc["folder_path"],
                        "snippet": doc["snippet"][:200] + "..." if doc["snippet"] and len(doc["snippet"]) > 200 else doc["snippet"],
                        "topics": [],
                        "concepts": []
                    })

        # Get topics and concepts for results
        for doc in results:
            topic_result = await session.run("""
                MATCH (d:Document {drive_id: $drive_id})-[:COVERS]->(t:Topic)
                RETURN DISTINCT t.name as name LIMIT 5
            """, {"drive_id": doc["drive_id"]})
            doc["topics"] = [r["name"] for r in await topic_result.data()]

            concept_result = await session.run("""
                MATCH (d:Document {drive_id: $drive_id})-[:ABOUT]->(c:Concept)
                RETURN DISTINCT c.name as name LIMIT 5
            """, {"drive_id": doc["drive_id"]})
            doc["concepts"] = [r["name"] for r in await concept_result.data()]

        return results
    return []


async def get_documents_by_topic(topic_name: str, limit: int = 20) -> list[dict]:
    """Get documents linked to a specific topic."""
    from cognitex.db.neo4j import get_neo4j_session

    async for session in get_neo4j_session():
        result = await session.run("""
            MATCH (d:Document)-[:COVERS]->(t:Topic {name: $topic_name})
            RETURN d.drive_id as drive_id, d.name as name, d.folder_path as folder_path,
                   d.summary as snippet
            ORDER BY d.analyzed_at DESC
            LIMIT $limit
        """, {"topic_name": topic_name, "limit": limit})
        docs = await result.data()

        # Get topics and concepts for each doc
        for doc in docs:
            doc["topics"] = [topic_name]
            concept_result = await session.run("""
                MATCH (d:Document {drive_id: $drive_id})-[:ABOUT]->(c:Concept)
                RETURN c.name as name LIMIT 5
            """, {"drive_id": doc["drive_id"]})
            doc["concepts"] = [r["name"] for r in await concept_result.data()]
        return docs
    return []


async def get_documents_by_concept(concept_name: str, limit: int = 20) -> list[dict]:
    """Get documents linked to a specific concept."""
    from cognitex.db.neo4j import get_neo4j_session

    async for session in get_neo4j_session():
        result = await session.run("""
            MATCH (d:Document)-[:ABOUT]->(c:Concept {name: $concept_name})
            RETURN d.drive_id as drive_id, d.name as name, d.folder_path as folder_path,
                   d.summary as snippet
            ORDER BY d.analyzed_at DESC
            LIMIT $limit
        """, {"concept_name": concept_name, "limit": limit})
        docs = await result.data()

        # Get topics and concepts for each doc
        for doc in docs:
            doc["concepts"] = [concept_name]
            topic_result = await session.run("""
                MATCH (d:Document {drive_id: $drive_id})-[:COVERS]->(t:Topic)
                RETURN t.name as name LIMIT 5
            """, {"drive_id": doc["drive_id"]})
            doc["topics"] = [r["name"] for r in await topic_result.data()]
        return docs
    return []


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
        results = await get_documents_by_topic(topic)
    elif concept:
        query = f"concept:{concept}"
        results = await get_documents_by_concept(concept)

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
# Agent Log
# -------------------------------------------------------------------


@app.get("/agent-log", response_class=HTMLResponse)
async def agent_log_page(request: Request):
    """Agent action log page - shows all agent actions."""
    from cognitex.db.postgres import get_session
    from sqlalchemy import text

    actions = []
    stats = {"total": 0, "last_24h": 0, "failed": 0, "action_types": 0, "sources": 0}
    action_counts = []
    source_counts = []

    try:
        async for session in get_session():
            # Ensure the agent_actions table exists
            try:
                await session.execute(text("""
                    CREATE TABLE IF NOT EXISTS agent_actions (
                        id TEXT PRIMARY KEY,
                        timestamp TIMESTAMP DEFAULT NOW(),
                        action_type TEXT NOT NULL,
                        source TEXT NOT NULL,
                        summary TEXT,
                        details JSONB DEFAULT '{}',
                        status TEXT DEFAULT 'completed',
                        error TEXT
                    )
                """))
                await session.commit()
            except Exception:
                pass  # Table may already exist

            # Get recent actions
            result = await session.execute(text("""
                SELECT id, timestamp, action_type, source, summary, details, status, error
                FROM agent_actions
                ORDER BY timestamp DESC
                LIMIT 100
            """))
            actions = [{
                "id": row.id,
                "timestamp": row.timestamp.isoformat() if row.timestamp else None,
                "action_type": row.action_type,
                "source": row.source,
                "summary": row.summary,
                "details": row.details,
                "status": row.status,
                "error": row.error,
            } for row in result.fetchall()]

            # Get stats
            stat_result = await session.execute(text("""
                SELECT
                    COUNT(*) as total,
                    COUNT(*) FILTER (WHERE timestamp > NOW() - INTERVAL '24 hours') as last_24h,
                    COUNT(*) FILTER (WHERE status = 'failed') as failed,
                    COUNT(DISTINCT action_type) as action_types,
                    COUNT(DISTINCT source) as sources
                FROM agent_actions
            """))
            stat_row = stat_result.fetchone()
            if stat_row:
                stats = {
                    "total": stat_row.total or 0,
                    "last_24h": stat_row.last_24h or 0,
                    "failed": stat_row.failed or 0,
                    "action_types": stat_row.action_types or 0,
                    "sources": stat_row.sources or 0,
                }

            # Action type counts
            action_result = await session.execute(text("""
                SELECT action_type, COUNT(*) as count
                FROM agent_actions
                GROUP BY action_type
                ORDER BY count DESC
            """))
            action_counts = [{"action_type": row.action_type, "count": row.count}
                             for row in action_result.fetchall()]

            # Source counts
            source_result = await session.execute(text("""
                SELECT source, COUNT(*) as count
                FROM agent_actions
                GROUP BY source
                ORDER BY count DESC
            """))
            source_counts = [{"source": row.source, "count": row.count}
                              for row in source_result.fetchall()]
            break
    except Exception as e:
        logger.warning("Failed to load agent actions", error=str(e))

    return templates.TemplateResponse(
        "agent_log.html",
        {
            "request": request,
            "actions": actions,
            "stats": stats,
            "action_counts": action_counts,
            "source_counts": source_counts,
        },
    )


@app.get("/agent-log/{action_id}", response_class=HTMLResponse)
async def agent_log_detail(action_id: str):
    """Get action details for modal."""
    from cognitex.db.postgres import get_session
    from sqlalchemy import text
    import json

    async for session in get_session():
        result = await session.execute(text("""
            SELECT * FROM agent_actions WHERE id = :action_id
        """), {"action_id": action_id})
        row = result.fetchone()

        if not row:
            return HTMLResponse("<p>Action not found</p>")

        # Format the details
        status_color = "#2e7d32" if row.status == "completed" else "#c62828"
        html = f"""
        <div style="display: grid; gap: 1rem;">
            <div>
                <strong>Action Type:</strong> <code>{row.action_type}</code>
            </div>
            <div>
                <strong>Source:</strong> <span style="color: #1565c0;">{row.source}</span>
            </div>
            <div>
                <strong>Status:</strong> <span style="color: {status_color};">{row.status}</span>
            </div>
        """

        if row.summary:
            html += f"""
            <div>
                <strong>Summary:</strong>
                <p style="margin: 0.5rem 0; padding: 0.5rem; background: #f8f9fa; border-radius: 4px;">{row.summary}</p>
            </div>
            """

        if row.details:
            try:
                details = row.details if isinstance(row.details, dict) else json.loads(row.details)
                details_str = json.dumps(details, indent=2)
            except:
                details_str = str(row.details)
            html += f"""
            <div>
                <strong>Details:</strong>
                <pre style="margin: 0.5rem 0; padding: 0.5rem; background: #f8f9fa; border-radius: 4px; overflow-x: auto; font-size: 0.85rem; white-space: pre-wrap;">{details_str[:1000]}</pre>
            </div>
            """

        if row.error:
            html += f"""
            <div>
                <strong>Error:</strong>
                <p style="margin: 0.5rem 0; padding: 0.5rem; background: #ffebee; border-radius: 4px; color: #c62828;">{row.error}</p>
            </div>
            """

        html += f"""
            <div style="font-size: 0.85rem; color: #666;">
                Timestamp: {row.timestamp.isoformat() if row.timestamp else '-'}
            </div>
        </div>
        """

        return HTMLResponse(html)

    return HTMLResponse("<p>Error loading action</p>")


# -------------------------------------------------------------------
# Autonomous Agent
# -------------------------------------------------------------------


@app.post("/api/agent/run-cycle")
async def run_autonomous_cycle():
    """Manually trigger an autonomous agent cycle."""
    try:
        from cognitex.agent.autonomous import get_autonomous_agent
        agent = await get_autonomous_agent()
        result = await agent.run_once()
        return JSONResponse({"status": "success", "result": result})
    except Exception as e:
        logger.error("Failed to run autonomous cycle", error=str(e))
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@app.get("/api/agent/status")
async def get_autonomous_status():
    """Get autonomous agent status."""
    from cognitex.config import get_settings
    settings = get_settings()

    try:
        from cognitex.agent.autonomous import get_autonomous_agent
        agent = await get_autonomous_agent()
        running = agent._running
    except Exception:
        running = False

    return JSONResponse({
        "enabled": settings.autonomous_agent_enabled,
        "running": running,
        "interval_minutes": settings.autonomous_agent_interval_minutes,
    })


@app.post("/api/agent/test-notification")
async def test_agent_notification():
    """Test the Discord notification system."""
    try:
        from cognitex.agent.tools import SendNotificationTool

        tool = SendNotificationTool()
        result = await tool.execute(
            message="**Test Notification**\n\nThis is a test from the autonomous agent notification system.",
            urgency="normal"
        )

        if result.success:
            return JSONResponse({"status": "success", "message": "Notification sent"})
        else:
            return JSONResponse({"status": "error", "error": result.error}, status_code=500)
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


# -------------------------------------------------------------------
# Digital Twin Review
# -------------------------------------------------------------------

import structlog
logger = structlog.get_logger()


async def get_pending_drafts() -> list[dict]:
    """Get all pending email drafts."""
    from cognitex.db.neo4j import get_neo4j_session

    async for session in get_neo4j_session():
        query = """
        MATCH (d:EmailDraft {status: 'pending_review'})-[:REPLY_TO]->(e:Email)
        OPTIONAL MATCH (e)-[:SENT_BY]->(sender:Person)
        RETURN
            d.id as id,
            d.to as to,
            d.subject as subject,
            d.body as body,
            d.reason as reason,
            d.created_at as created_at,
            e.subject as original_subject,
            e.gmail_id as original_email_id,
            sender.email as sender_email
        ORDER BY d.created_at DESC
        """
        result = await session.run(query)
        data = await result.data()
        return data
    return []


async def get_context_packs() -> list[dict]:
    """Get all ready context packs."""
    from cognitex.db.neo4j import get_neo4j_session

    async for session in get_neo4j_session():
        query = """
        MATCH (cp:ContextPack {status: 'ready'})
        OPTIONAL MATCH (cp)-[:PREPARED_FOR]->(ce:CalendarEvent)
        OPTIONAL MATCH (cp)-[:REFERENCES]->(d:Document)
        OPTIONAL MATCH (cp)-[:REFERENCES]->(t:Task)
        WITH cp, ce,
             collect(DISTINCT {id: d.drive_id, name: d.name}) as documents,
             collect(DISTINCT {id: t.id, title: t.title}) as tasks
        RETURN
            cp.id as id,
            cp.title as title,
            cp.summary as summary,
            cp.key_points as key_points,
            cp.created_at as created_at,
            ce.id as calendar_id,
            ce.title as event_title,
            ce.start_time as event_time,
            documents,
            tasks
        ORDER BY cp.created_at DESC
        """
        result = await session.run(query)
        data = await result.data()
        # Filter out null entries in documents/tasks
        for pack in data:
            pack['documents'] = [d for d in pack.get('documents', []) if d.get('id')]
            pack['tasks'] = [t for t in pack.get('tasks', []) if t.get('id')]
        return data
    return []


async def get_suggested_blocks() -> list[dict]:
    """Get all pending suggested calendar blocks."""
    from cognitex.db.neo4j import get_neo4j_session

    async for session in get_neo4j_session():
        query = """
        MATCH (sb:SuggestedBlock {status: 'pending_approval'})
        OPTIONAL MATCH (sb)-[:FOR_PROJECT]->(p:Project)
        OPTIONAL MATCH (sb)-[:FOR_TASK]->(t:Task)
        RETURN
            sb.id as id,
            sb.title as title,
            sb.duration_hours as duration_hours,
            sb.suggested_day as suggested_day,
            sb.reason as reason,
            sb.created_at as created_at,
            p.id as project_id,
            p.title as project_title,
            t.id as task_id,
            t.title as task_title
        ORDER BY sb.created_at DESC
        """
        result = await session.run(query)
        data = await result.data()
        return data
    return []


@app.get("/twin", response_class=HTMLResponse)
async def twin_page(request: Request):
    """Digital Twin review page - review and approve agent outputs."""
    drafts = await get_pending_drafts()
    packs = await get_context_packs()
    blocks = await get_suggested_blocks()

    return templates.TemplateResponse(
        "twin.html",
        {
            "request": request,
            "drafts": drafts,
            "packs": packs,
            "blocks": blocks,
        },
    )


@app.post("/api/twin/drafts/{draft_id}/approve")
async def approve_draft(draft_id: str):
    """Approve and send an email draft."""
    from cognitex.db.neo4j import get_neo4j_session

    async for session in get_neo4j_session():
        # Get draft details
        query = """
        MATCH (d:EmailDraft {id: $draft_id})-[:REPLY_TO]->(e:Email)
        RETURN d.to as to, d.subject as subject, d.body as body, e.gmail_id as thread_id
        """
        result = await session.run(query, {"draft_id": draft_id})
        draft = await result.single()

        if not draft:
            raise HTTPException(status_code=404, detail="Draft not found")

        # TODO: Actually send the email via Gmail API
        # For now, just mark as approved
        update_query = """
        MATCH (d:EmailDraft {id: $draft_id})
        SET d.status = 'approved', d.approved_at = datetime()
        """
        await session.run(update_query, {"draft_id": draft_id})

        logger.info("Email draft approved", draft_id=draft_id)

        return HTMLResponse(f'''
            <div class="draft-card" style="background: #d1fae5; border-color: #065f46;">
                <p><strong>Approved!</strong> Email will be sent to {draft["to"]}</p>
            </div>
        ''')

    raise HTTPException(status_code=500, detail="Failed to approve draft")


@app.get("/api/twin/drafts/{draft_id}/edit")
async def edit_draft_form(request: Request, draft_id: str):
    """Get edit form for a draft."""
    from cognitex.db.neo4j import get_neo4j_session

    async for session in get_neo4j_session():
        query = """
        MATCH (d:EmailDraft {id: $draft_id})-[:REPLY_TO]->(e:Email)
        RETURN d.id as id, d.to as to, d.subject as subject, d.body as body,
               e.subject as original_subject
        """
        result = await session.run(query, {"draft_id": draft_id})
        draft = await result.single()

        if not draft:
            raise HTTPException(status_code=404, detail="Draft not found")

        return HTMLResponse(f'''
            <form class="draft-edit-form" hx-put="/api/twin/drafts/{draft_id}" hx-target="#draft-{draft_id}" hx-swap="outerHTML">
                <div class="draft-meta">
                    <strong>To:</strong> <input type="text" name="to" value="{draft['to']}" style="width: 300px;"><br>
                    <strong>Subject:</strong> <input type="text" name="subject" value="{draft['subject']}" style="width: 100%;">
                </div>
                <textarea name="body" class="draft-body" style="min-height: 200px; width: 100%;">{draft['body']}</textarea>
                <div class="draft-actions" style="margin-top: 0.5rem;">
                    <button type="submit" class="btn btn-success">Save & Send</button>
                    <button type="button" class="btn btn-secondary" hx-get="/twin" hx-target="body">Cancel</button>
                </div>
            </form>
        ''')

    raise HTTPException(status_code=500, detail="Failed to load draft")


@app.put("/api/twin/drafts/{draft_id}")
async def update_draft(
    draft_id: str,
    to: Annotated[str, Form()],
    subject: Annotated[str, Form()],
    body: Annotated[str, Form()],
):
    """Update and approve a draft."""
    from cognitex.db.neo4j import get_neo4j_session

    async for session in get_neo4j_session():
        query = """
        MATCH (d:EmailDraft {id: $draft_id})
        SET d.to = $to, d.subject = $subject, d.body = $body,
            d.status = 'approved', d.approved_at = datetime()
        RETURN d.id as id
        """
        result = await session.run(query, {
            "draft_id": draft_id,
            "to": to,
            "subject": subject,
            "body": body,
        })
        data = await result.single()

        if not data:
            raise HTTPException(status_code=404, detail="Draft not found")

        return HTMLResponse(f'''
            <div class="draft-card" style="background: #d1fae5; border-color: #065f46;">
                <p><strong>Updated & Approved!</strong> Email will be sent to {to}</p>
            </div>
        ''')

    raise HTTPException(status_code=500, detail="Failed to update draft")


@app.delete("/api/twin/drafts/{draft_id}")
async def delete_draft(draft_id: str):
    """Discard a draft."""
    from cognitex.db.neo4j import get_neo4j_session

    async for session in get_neo4j_session():
        query = """
        MATCH (d:EmailDraft {id: $draft_id})
        SET d.status = 'discarded'
        """
        await session.run(query, {"draft_id": draft_id})
        return HTMLResponse("")  # Empty to remove from DOM

    raise HTTPException(status_code=500, detail="Failed to delete draft")


@app.delete("/api/twin/packs/{pack_id}")
async def archive_pack(pack_id: str):
    """Archive a context pack."""
    from cognitex.db.neo4j import get_neo4j_session

    async for session in get_neo4j_session():
        query = """
        MATCH (cp:ContextPack {id: $pack_id})
        SET cp.status = 'archived'
        """
        await session.run(query, {"pack_id": pack_id})
        return HTMLResponse("")  # Empty to remove from DOM

    raise HTTPException(status_code=500, detail="Failed to archive pack")


@app.post("/api/twin/blocks/{block_id}/approve")
async def approve_block(block_id: str):
    """Approve a suggested focus block (add to calendar)."""
    from datetime import datetime, timedelta
    from cognitex.db.neo4j import get_neo4j_session
    from cognitex.services.calendar import CalendarService

    async for session in get_neo4j_session():
        # Get block details
        query = """
        MATCH (sb:SuggestedBlock {id: $block_id})
        OPTIONAL MATCH (sb)-[:FOR_PROJECT]->(p:Project)
        RETURN sb.title as title, sb.duration_hours as duration_hours,
               sb.suggested_day as suggested_day, sb.reason as reason,
               p.title as project_title
        """
        result = await session.run(query, {"block_id": block_id})
        block = await result.single()

        if not block:
            raise HTTPException(status_code=404, detail="Block not found")

        # Calculate start time based on suggested_day
        today = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
        suggested_day = (block.get("suggested_day") or "tomorrow").lower()

        if suggested_day == "today":
            start_date = today
        elif suggested_day == "tomorrow":
            start_date = today + timedelta(days=1)
        elif suggested_day == "next week":
            # Next Monday
            days_until_monday = (7 - today.weekday()) % 7
            if days_until_monday == 0:
                days_until_monday = 7
            start_date = today + timedelta(days=days_until_monday)
        else:
            # Default to tomorrow
            start_date = today + timedelta(days=1)

        duration_hours = block.get("duration_hours") or 2
        end_date = start_date + timedelta(hours=duration_hours)

        # Format as ISO strings
        start_iso = start_date.strftime("%Y-%m-%dT%H:%M:%S")
        end_iso = end_date.strftime("%Y-%m-%dT%H:%M:%S")

        # Create the calendar event
        try:
            calendar_service = CalendarService()
            description = f"Focus time suggested by Cognitex Digital Twin.\n\nReason: {block.get('reason', 'No reason provided')}"
            if block.get("project_title"):
                description = f"Project: {block['project_title']}\n\n{description}"

            event = calendar_service.create_event(
                title=block["title"],
                start=start_iso,
                end=end_iso,
                description=description,
                send_notifications=False,
            )
            event_id = event.get("id", "unknown")
            logger.info("Calendar event created for focus block", block_id=block_id, event_id=event_id)
        except Exception as e:
            logger.error("Failed to create calendar event", error=str(e), block_id=block_id)
            return HTMLResponse(f'''
                <tr style="background: #fee2e2;">
                    <td colspan="6"><strong>Error:</strong> Failed to create calendar event: {str(e)[:100]}</td>
                </tr>
            ''')

        # Mark as approved and store calendar event ID
        update_query = """
        MATCH (sb:SuggestedBlock {id: $block_id})
        SET sb.status = 'approved', sb.approved_at = datetime(), sb.calendar_event_id = $event_id
        """
        await session.run(update_query, {"block_id": block_id, "event_id": event_id})

        logger.info("Focus block approved", block_id=block_id)

        return HTMLResponse(f'''
            <tr style="background: #d1fae5;">
                <td colspan="6"><strong>Added to calendar:</strong> {block["title"]} ({duration_hours}h) on {start_date.strftime("%A %d %b at %H:%M")}</td>
            </tr>
        ''')

    raise HTTPException(status_code=500, detail="Failed to approve block")


@app.delete("/api/twin/blocks/{block_id}")
async def dismiss_block(block_id: str):
    """Dismiss a suggested block."""
    from cognitex.db.neo4j import get_neo4j_session

    async for session in get_neo4j_session():
        query = """
        MATCH (sb:SuggestedBlock {id: $block_id})
        SET sb.status = 'dismissed'
        """
        await session.run(query, {"block_id": block_id})
        return HTMLResponse("")  # Empty to remove from DOM

    raise HTTPException(status_code=500, detail="Failed to dismiss block")


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
# Graph Visualization
# -------------------------------------------------------------------


@app.get("/graph", response_class=HTMLResponse)
async def graph_page(request: Request):
    """Graph visualization page for exploring the knowledge graph."""
    return templates.TemplateResponse("graph.html", {"request": request})


@app.get("/api/graph/data")
async def api_graph_data(
    center: str | None = None,
    node_type: str | None = None,
    types: str | None = None,  # Comma-separated list of types to include
    depth: int = 2,
    limit: int = 100,
    hide_completed: bool = True,  # Hide completed tasks by default
):
    """Get graph data for D3.js visualization.

    Behavior:
    - No types selected: Return empty graph
    - One type selected: Return nodes of that type only (no connections)
    - Two+ types selected: Return nodes of those types AND connections between them

    Topics and Concepts are sorted by reference count (most connected first).
    """
    from cognitex.db.neo4j import get_neo4j_session

    nodes = []
    links = []
    seen_nodes = set()

    # Parse types parameter
    allowed_types = []
    if types:
        allowed_types = [t.strip() for t in types.split(",") if t.strip()]
    elif node_type:
        allowed_types = [node_type]

    # No types selected = empty graph
    if not allowed_types:
        return JSONResponse({"nodes": [], "links": []})

    async for session in get_neo4j_session():
        if center:
            # Parse center node - format is "type:id"
            parts = center.split(":", 1)
            if len(parts) == 2:
                center_type, center_id = parts
                # Query centered on specific node - only show selected types
                type_list = "['" + "','".join(allowed_types) + "']"
                query = f"""
                MATCH path = (start)-[r*1..{min(depth, 3)}]-(end)
                WHERE ((start:Person AND start.email = $center_id)
                   OR (start:Task AND start.id = $center_id)
                   OR (start:Project AND start.id = $center_id)
                   OR (start:Goal AND start.id = $center_id)
                   OR (start:Document AND start.drive_id = $center_id)
                   OR (start:Email AND start.message_id = $center_id)
                   OR (start:Event AND start.gcal_id = $center_id)
                   OR (start:Topic AND start.name = $center_id)
                   OR (start:Concept AND start.name = $center_id))
                  AND labels(start)[0] IN {type_list}
                  AND labels(end)[0] IN {type_list}
                WITH start, end, r, path
                LIMIT $limit
                RETURN DISTINCT
                    labels(start)[0] as start_type,
                    CASE labels(start)[0]
                        WHEN 'Person' THEN start.email
                        WHEN 'Task' THEN start.id
                        WHEN 'Project' THEN start.id
                        WHEN 'Goal' THEN start.id
                        WHEN 'Document' THEN start.drive_id
                        WHEN 'Email' THEN start.message_id
                        WHEN 'Event' THEN start.gcal_id
                        WHEN 'Topic' THEN start.name
                        WHEN 'Concept' THEN start.name
                        ELSE coalesce(start.id, start.email, start.drive_id, start.name)
                    END as start_id,
                    CASE labels(start)[0]
                        WHEN 'Person' THEN coalesce(start.name, start.email)
                        WHEN 'Task' THEN start.title
                        WHEN 'Project' THEN start.title
                        WHEN 'Goal' THEN start.title
                        WHEN 'Document' THEN start.name
                        WHEN 'Email' THEN start.subject
                        WHEN 'Event' THEN start.title
                        WHEN 'Topic' THEN start.name
                        WHEN 'Concept' THEN start.name
                        ELSE coalesce(start.title, start.name, start.subject)
                    END as start_label,
                    labels(end)[0] as end_type,
                    CASE labels(end)[0]
                        WHEN 'Person' THEN end.email
                        WHEN 'Task' THEN end.id
                        WHEN 'Project' THEN end.id
                        WHEN 'Goal' THEN end.id
                        WHEN 'Document' THEN end.drive_id
                        WHEN 'Email' THEN end.message_id
                        WHEN 'Event' THEN end.gcal_id
                        WHEN 'Topic' THEN end.name
                        WHEN 'Concept' THEN end.name
                        ELSE coalesce(end.id, end.email, end.drive_id, end.name)
                    END as end_id,
                    CASE labels(end)[0]
                        WHEN 'Person' THEN coalesce(end.name, end.email)
                        WHEN 'Task' THEN end.title
                        WHEN 'Project' THEN end.title
                        WHEN 'Goal' THEN end.title
                        WHEN 'Document' THEN end.name
                        WHEN 'Email' THEN end.subject
                        WHEN 'Event' THEN end.title
                        WHEN 'Topic' THEN end.name
                        WHEN 'Concept' THEN end.name
                        ELSE coalesce(end.title, end.name, end.subject)
                    END as end_label,
                    type(r[0]) as rel_type
                """
                result = await session.run(query, {
                    "center_id": center_id,
                    "limit": limit * 2,
                })
                data = await result.data()

                for row in data:
                    start_key = f"{row['start_type']}:{row['start_id']}"
                    if start_key not in seen_nodes and row['start_id']:
                        seen_nodes.add(start_key)
                        nodes.append({
                            "id": start_key,
                            "type": row["start_type"],
                            "label": row["start_label"] or row["start_id"] or "Unknown",
                        })

                    end_key = f"{row['end_type']}:{row['end_id']}"
                    if end_key not in seen_nodes and row['end_id']:
                        seen_nodes.add(end_key)
                        nodes.append({
                            "id": end_key,
                            "type": row["end_type"],
                            "label": row["end_label"] or row["end_id"] or "Unknown",
                        })

                    if row['start_id'] and row['end_id']:
                        links.append({
                            "source": start_key,
                            "target": end_key,
                            "type": row["rel_type"],
                        })
        else:
            # Overview query - behavior depends on number of types selected
            if len(allowed_types) == 1:
                # Single type: get nodes of that type only, sorted by connections
                single_type = allowed_types[0]

                # Build completed filter for Task type
                completed_filter = ""
                if hide_completed and single_type == 'Task':
                    completed_filter = "AND n.status <> 'completed'"

                # For Topic/Concept, order by connection count
                if single_type in ('Topic', 'Concept'):
                    query = f"""
                    MATCH (n:{single_type})
                    OPTIONAL MATCH (n)-[r]-()
                    WITH n, count(r) as conn_count
                    ORDER BY conn_count DESC
                    LIMIT $limit
                    RETURN
                        '{single_type}' as node_type,
                        n.name as node_id,
                        n.name as node_label,
                        conn_count
                    """
                else:
                    query = f"""
                    MATCH (n:{single_type})
                    WHERE true {completed_filter}
                    OPTIONAL MATCH (n)-[r]-()
                    WITH n, count(r) as conn_count
                    ORDER BY conn_count DESC
                    LIMIT $limit
                    RETURN
                        '{single_type}' as node_type,
                        CASE '{single_type}'
                            WHEN 'Person' THEN n.email
                            WHEN 'Task' THEN n.id
                            WHEN 'Project' THEN n.id
                            WHEN 'Goal' THEN n.id
                            WHEN 'Document' THEN n.drive_id
                            WHEN 'Email' THEN n.message_id
                            WHEN 'Event' THEN n.gcal_id
                            WHEN 'Repository' THEN n.id
                            ELSE coalesce(n.id, n.email, n.drive_id, n.name)
                        END as node_id,
                        CASE '{single_type}'
                            WHEN 'Person' THEN coalesce(n.name, n.email)
                            WHEN 'Task' THEN n.title
                            WHEN 'Project' THEN n.title
                            WHEN 'Goal' THEN n.title
                            WHEN 'Document' THEN n.name
                            WHEN 'Email' THEN n.subject
                            WHEN 'Event' THEN n.title
                            WHEN 'Repository' THEN n.full_name
                            ELSE coalesce(n.title, n.name, n.subject)
                        END as node_label,
                        conn_count
                    """
                result = await session.run(query, {"limit": limit})
                data = await result.data()

                for row in data:
                    if row['node_id']:
                        node_key = f"{row['node_type']}:{row['node_id']}"
                        if node_key not in seen_nodes:
                            seen_nodes.add(node_key)
                            nodes.append({
                                "id": node_key,
                                "type": row["node_type"],
                                "label": row["node_label"] or row["node_id"] or "Unknown",
                            })
                # No links for single type
            else:
                # Multiple types: get connections between ALL pairs of selected types
                # Both endpoints must be in selected types
                type_list = "['" + "','".join(allowed_types) + "']"

                # Build completed filter
                completed_filter = ""
                if hide_completed:
                    completed_filter = "AND NOT (labels(n)[0] = 'Task' AND n.status = 'completed') AND NOT (labels(m)[0] = 'Task' AND m.status = 'completed')"

                # For better Topic/Concept ordering, use a subquery approach
                query = f"""
                MATCH (n)-[r]-(m)
                WHERE labels(n)[0] IN {type_list}
                  AND labels(m)[0] IN {type_list}
                  {completed_filter}
                WITH n, m, r
                LIMIT $limit
                RETURN DISTINCT
                    labels(n)[0] as start_type,
                    CASE labels(n)[0]
                        WHEN 'Person' THEN n.email
                        WHEN 'Task' THEN n.id
                        WHEN 'Project' THEN n.id
                        WHEN 'Goal' THEN n.id
                        WHEN 'Document' THEN n.drive_id
                        WHEN 'Email' THEN n.message_id
                        WHEN 'Event' THEN n.gcal_id
                        WHEN 'Repository' THEN n.id
                        WHEN 'Topic' THEN n.name
                        WHEN 'Concept' THEN n.name
                        ELSE coalesce(n.id, n.email, n.drive_id, n.name)
                    END as start_id,
                    CASE labels(n)[0]
                        WHEN 'Person' THEN coalesce(n.name, n.email)
                        WHEN 'Task' THEN n.title
                        WHEN 'Project' THEN n.title
                        WHEN 'Goal' THEN n.title
                        WHEN 'Document' THEN n.name
                        WHEN 'Email' THEN n.subject
                        WHEN 'Event' THEN n.title
                        WHEN 'Repository' THEN n.full_name
                        WHEN 'Topic' THEN n.name
                        WHEN 'Concept' THEN n.name
                        ELSE coalesce(n.title, n.name, n.subject)
                    END as start_label,
                    labels(m)[0] as end_type,
                    CASE labels(m)[0]
                        WHEN 'Person' THEN m.email
                        WHEN 'Task' THEN m.id
                        WHEN 'Project' THEN m.id
                        WHEN 'Goal' THEN m.id
                        WHEN 'Document' THEN m.drive_id
                        WHEN 'Email' THEN m.message_id
                        WHEN 'Event' THEN m.gcal_id
                        WHEN 'Repository' THEN m.id
                        WHEN 'Topic' THEN m.name
                        WHEN 'Concept' THEN m.name
                        ELSE coalesce(m.id, m.email, m.drive_id, m.name)
                    END as end_id,
                    CASE labels(m)[0]
                        WHEN 'Person' THEN coalesce(m.name, m.email)
                        WHEN 'Task' THEN m.title
                        WHEN 'Project' THEN m.title
                        WHEN 'Goal' THEN m.title
                        WHEN 'Document' THEN m.name
                        WHEN 'Email' THEN m.subject
                        WHEN 'Event' THEN m.title
                        WHEN 'Repository' THEN m.full_name
                        WHEN 'Topic' THEN m.name
                        WHEN 'Concept' THEN m.name
                        ELSE coalesce(m.title, m.name, m.subject)
                    END as end_label,
                    type(r) as rel_type
                """
                result = await session.run(query, {"limit": limit * 3})
                data = await result.data()

                for row in data:
                    start_key = f"{row['start_type']}:{row['start_id']}"
                    if start_key not in seen_nodes and row['start_id']:
                        seen_nodes.add(start_key)
                        nodes.append({
                            "id": start_key,
                            "type": row["start_type"],
                            "label": row["start_label"] or row["start_id"] or "Unknown",
                        })

                    end_key = f"{row['end_type']}:{row['end_id']}"
                    if end_key not in seen_nodes and row['end_id']:
                        seen_nodes.add(end_key)
                        nodes.append({
                            "id": end_key,
                            "type": row["end_type"],
                            "label": row["end_label"] or row["end_id"] or "Unknown",
                        })

                    if row['start_id'] and row['end_id']:
                        links.append({
                            "source": start_key,
                            "target": end_key,
                            "type": row["rel_type"],
                        })

        break

    # Limit nodes if too many
    if len(nodes) > limit:
        nodes = nodes[:limit]
        node_ids = {n["id"] for n in nodes}
        links = [l for l in links if l["source"] in node_ids and l["target"] in node_ids]

    return JSONResponse({"nodes": nodes, "links": links})


@app.get("/api/graph/search")
async def api_graph_search(q: str = "", limit: int = 20):
    """Search for nodes by name/title to center the graph on."""
    from cognitex.db.neo4j import get_neo4j_session

    if not q or len(q) < 2:
        return JSONResponse([])

    async for session in get_neo4j_session():
        query = """
        CALL {
            MATCH (p:Person)
            WHERE toLower(p.name) CONTAINS toLower($query)
               OR toLower(p.email) CONTAINS toLower($query)
            RETURN 'Person' as type, p.email as id, coalesce(p.name, p.email) as label
            LIMIT 5

            UNION ALL

            MATCH (t:Task)
            WHERE toLower(t.title) CONTAINS toLower($query)
            RETURN 'Task' as type, t.id as id, t.title as label
            LIMIT 5

            UNION ALL

            MATCH (p:Project)
            WHERE toLower(p.title) CONTAINS toLower($query)
            RETURN 'Project' as type, p.id as id, p.title as label
            LIMIT 5

            UNION ALL

            MATCH (g:Goal)
            WHERE toLower(g.title) CONTAINS toLower($query)
            RETURN 'Goal' as type, g.id as id, g.title as label
            LIMIT 5
        }
        RETURN type, id, label
        LIMIT $limit
        """
        result = await session.run(query, {"query": q, "limit": limit})
        data = await result.data()

        return JSONResponse([
            {"id": f"{row['type']}:{row['id']}", "type": row["type"], "label": row["label"]}
            for row in data
            if row["id"]
        ])

    return JSONResponse([])


@app.post("/api/graph/link")
async def api_graph_link(
    source_type: str,
    source_id: str,
    target_type: str,
    target_id: str,
    relationship: str,
    action: str = "create",  # "create" or "delete"
):
    """Create or delete a relationship between two nodes."""
    from cognitex.db.neo4j import get_neo4j_session
    from cognitex.db.graph_schema import (
        link_task_to_project, link_task_to_goal, link_task_to_person,
        link_project_to_goal, link_project_to_person, link_project_to_repository,
        link_goal_to_person, link_goal_parent,
    )

    # Map of valid relationships by source->target types
    # Each entry: (source_type, target_type) -> (relationship_type, link_function)
    valid_links = {
        ("Task", "Project"): ("PART_OF", link_task_to_project),
        ("Task", "Goal"): ("CONTRIBUTES_TO", link_task_to_goal),
        ("Task", "Person"): ("INVOLVES", link_task_to_person),
        ("Project", "Goal"): ("PART_OF", link_project_to_goal),
        ("Project", "Person"): ("INVOLVES", link_project_to_person),
        ("Project", "Repository"): ("USES", link_project_to_repository),
        ("Goal", "Person"): ("OWNED_BY", link_goal_to_person),
        ("Goal", "Goal"): ("PARENT_OF", link_goal_parent),
    }

    link_key = (source_type, target_type)
    if link_key not in valid_links:
        return JSONResponse(
            {"success": False, "error": f"Cannot link {source_type} to {target_type}"},
            status_code=400
        )

    rel_type, link_func = valid_links[link_key]

    async for session in get_neo4j_session():
        try:
            if action == "delete":
                # Delete the relationship
                query = f"""
                MATCH (s:{source_type} {{id: $source_id}})-[r:{rel_type}]->(t:{target_type} {{id: $target_id}})
                DELETE r
                RETURN count(r) as deleted
                """
                # Handle Person nodes which use email as id
                if source_type == "Person":
                    query = query.replace("{id: $source_id}", "{email: $source_id}")
                if target_type == "Person":
                    query = query.replace("{id: $target_id}", "{email: $target_id}")

                result = await session.run(query, {
                    "source_id": source_id,
                    "target_id": target_id
                })
                data = await result.single()
                return JSONResponse({
                    "success": True,
                    "action": "deleted",
                    "deleted": data["deleted"] if data else 0
                })
            else:
                # Create the relationship using the appropriate link function
                # Person links use email as identifier
                if target_type == "Person":
                    await link_func(session, source_id, target_id)
                elif source_type == "Person":
                    # Reverse: we need to call differently
                    await link_func(session, target_id, source_id)
                else:
                    await link_func(session, source_id, target_id)

                return JSONResponse({
                    "success": True,
                    "action": "created",
                    "relationship": rel_type
                })
        except Exception as e:
            return JSONResponse(
                {"success": False, "error": str(e)},
                status_code=500
            )

    return JSONResponse({"success": False, "error": "No session"}, status_code=500)


@app.get("/api/graph/link-targets")
async def api_graph_link_targets(node_type: str, q: str = "", limit: int = 20):
    """Get possible link targets for a node type."""
    from cognitex.db.neo4j import get_neo4j_session

    # Define what each node type can link to
    linkable_types = {
        "Task": ["Project", "Goal", "Person"],
        "Project": ["Goal", "Person", "Repository"],
        "Goal": ["Person", "Goal"],  # Goals can have parent goals
    }

    if node_type not in linkable_types:
        return JSONResponse({"targets": [], "linkable_types": []})

    targets = []
    async for session in get_neo4j_session():
        for target_type in linkable_types[node_type]:
            if target_type == "Person":
                query = """
                MATCH (p:Person)
                WHERE $query = '' OR toLower(coalesce(p.name, p.email)) CONTAINS toLower($query)
                RETURN 'Person' as type, p.email as id, coalesce(p.name, p.email) as label
                LIMIT $limit
                """
            elif target_type == "Goal":
                query = """
                MATCH (g:Goal)
                WHERE $query = '' OR toLower(g.title) CONTAINS toLower($query)
                RETURN 'Goal' as type, g.id as id, g.title as label
                LIMIT $limit
                """
            elif target_type == "Project":
                query = """
                MATCH (p:Project)
                WHERE $query = '' OR toLower(p.title) CONTAINS toLower($query)
                RETURN 'Project' as type, p.id as id, p.title as label
                LIMIT $limit
                """
            elif target_type == "Repository":
                query = """
                MATCH (r:Repository)
                WHERE $query = '' OR toLower(r.name) CONTAINS toLower($query)
                RETURN 'Repository' as type, r.id as id, r.name as label
                LIMIT $limit
                """
            else:
                continue

            result = await session.run(query, {"query": q, "limit": limit})
            data = await result.data()
            targets.extend([
                {"type": row["type"], "id": row["id"], "label": row["label"]}
                for row in data if row["id"]
            ])
        break

    return JSONResponse({
        "targets": targets[:limit],
        "linkable_types": linkable_types.get(node_type, [])
    })


@app.post("/api/graph/analyze")
async def api_analyze_node(
    node_type: str,
    node_id: str,
    node_name: str,
    node_description: str | None = None,
):
    """Analyze a node and suggest/auto-apply links using AI.

    Called after creating a task/project/goal to suggest relationships.
    High confidence (>=90%) links are auto-applied.
    """
    from cognitex.services.linking import get_linking_service
    from cognitex.db.postgres import get_postgres_session

    linking_service = get_linking_service()

    try:
        async for pg_session in get_postgres_session():
            suggestions = await linking_service.analyze_single_node(
                pg_session=pg_session,
                node_type=node_type,
                node_id=node_id,
                node_name=node_name,
                node_description=node_description,
                auto_apply_threshold=0.9,
            )

            return JSONResponse({
                "status": "ok",
                "suggestions": suggestions,
                "auto_applied": [s for s in suggestions if s.get("status") == "auto"],
                "pending": [s for s in suggestions if s.get("status") == "pending"],
            })

    except Exception as e:
        return JSONResponse(
            {"status": "error", "message": str(e)},
            status_code=500,
        )


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
