"""FastAPI web application for Cognitex dashboard."""

from __future__ import annotations

import html
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import BackgroundTasks, Depends, FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse
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


async def _record_task_outcome(
    task_id: str,
    task_title: str,
    outcome: str,  # 'completed', 'deferred', 'abandoned'
    energy_cost: str = "medium",
) -> None:
    """Record task outcome with state context for learning.

    Stores the observation in state_observations table and updates
    the temporal energy model.
    """
    from datetime import datetime
    import structlog
    from cognitex.db.postgres import get_session
    from cognitex.db.redis import get_redis
    from sqlalchemy import text

    logger = structlog.get_logger()

    try:
        # Get current state
        state_estimator = get_state_estimator()
        state = await state_estimator.get_current_state()

        # Check if in clinical recovery
        redis = get_redis()
        recovery_until = await redis.get("cognitex:clinical_recovery_until")
        post_clinical = False
        minutes_since = None

        if recovery_until:
            post_clinical = True
            # Could calculate minutes since clinical ended if needed

        now = datetime.now()
        hour = now.hour
        day_of_week = now.weekday()

        # Store observation
        async for session in get_session():
            await session.execute(text("""
                INSERT INTO state_observations (
                    task_id, task_title, outcome, mode, fatigue_level,
                    focus_score, hour_of_day, day_of_week, post_clinical,
                    minutes_since_clinical, energy_cost, observed_at
                ) VALUES (
                    :task_id, :task_title, :outcome, :mode, :fatigue,
                    :focus, :hour, :dow, :post_clinical,
                    :minutes_since, :energy_cost, NOW()
                )
            """), {
                "task_id": task_id,
                "task_title": task_title[:100],
                "outcome": outcome,
                "mode": state.mode.value,
                "fatigue": state.signals.fatigue_level,
                "focus": state.signals.focus_score,
                "hour": hour,
                "dow": day_of_week,
                "post_clinical": post_clinical,
                "minutes_since": minutes_since,
                "energy_cost": energy_cost,
            })
            await session.commit()
            break

        # Update temporal model with observation
        try:
            from cognitex.agent.state_model import get_temporal_model
            temporal = get_temporal_model()
            await temporal.update_from_observation(
                hour=hour,
                task_completed=(outcome == "completed"),
                task_difficulty=energy_cost,
                post_clinical=post_clinical,
            )
        except Exception as e:
            logger.debug("Could not update temporal model", error=str(e))

        logger.debug(
            "Recorded task outcome",
            task_id=task_id,
            outcome=outcome,
            hour=hour,
            mode=state.mode.value,
        )

    except Exception as e:
        # Don't fail the main operation if recording fails
        logger = structlog.get_logger()
        logger.warning("Failed to record task outcome", error=str(e))


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
    from cognitex.db.phase4_schema import init_phase4_schema
    from cognitex.agent.triggers import start_triggers, stop_triggers
    from cognitex.services.push_notifications import get_watch_manager
    from cognitex.services.notifications import init_notification_service, get_notification_service
    from cognitex.config import get_settings

    logger = structlog.get_logger()

    # Initialize database connections
    await init_neo4j()
    await init_graph_schema()
    await init_postgres()
    await init_redis()

    # Initialize Phase 4 learning schema
    await init_phase4_schema()

    # Sync LEDGER.yaml -> graph on boot (WP5)
    try:
        from cognitex.services.ledger_sync import get_ledger_sync_service
        ledger = get_ledger_sync_service()
        await ledger.initialize()
        changes = await ledger.sync_file_to_graph()
        if changes:
            logger.info("Synced LEDGER.yaml to graph on boot", changes=changes)
    except Exception as e:
        logger.warning("Failed to sync LEDGER.yaml on boot", error=str(e))

    # Initialize notification service (debouncing + deduplication)
    await init_notification_service()
    logger.info("Notification service started (debouncing + deduplication)")

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

    # Start real-time notification subscriber for web UI
    global _notification_subscriber_task
    _notification_subscriber_task = asyncio.create_task(_notification_subscriber())
    logger.info("Real-time notification subscriber started for web UI")

    yield

    # Cleanup
    # Cancel notification subscriber
    if _notification_subscriber_task:
        _notification_subscriber_task.cancel()
        try:
            await _notification_subscriber_task
        except asyncio.CancelledError:
            pass

    # Stop notification service (flushes pending notifications)
    try:
        notification_service = get_notification_service()
        await notification_service.stop()
    except Exception:
        pass

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


# -------------------------------------------------------------------
# Authentication
# -------------------------------------------------------------------

from cognitex.web.auth import (
    SESSION_COOKIE_NAME,
    SESSION_MAX_AGE,
    create_oauth_flow,
    create_session,
    verify_session,
    destroy_session,
    get_allowed_emails,
    store_oauth_state,
    verify_oauth_state,
)
from cognitex.config import get_settings
from cognitex.db.redis import get_redis
import structlog
import asyncio
import json
from asyncio import Queue

auth_logger = structlog.get_logger("auth")

# -------------------------------------------------------------------
# Real-time Notification System (SSE)
# -------------------------------------------------------------------
# Connected SSE clients for real-time notifications
_notification_clients: set[Queue] = set()
_notification_subscriber_task: asyncio.Task | None = None


async def _notification_subscriber():
    """Background task that subscribes to Redis notifications and broadcasts to SSE clients."""
    logger = structlog.get_logger("notifications")
    redis = get_redis()
    pubsub = redis.pubsub()

    try:
        await pubsub.subscribe("cognitex:notifications")
        logger.info("Web notification subscriber started")

        async for message in pubsub.listen():
            if message["type"] == "message":
                try:
                    data = message["data"]
                    if isinstance(data, bytes):
                        data = data.decode("utf-8")

                    # Broadcast to all connected clients
                    disconnected = set()
                    for client_queue in _notification_clients:
                        try:
                            client_queue.put_nowait(data)
                        except asyncio.QueueFull:
                            disconnected.add(client_queue)

                    # Clean up disconnected clients
                    for q in disconnected:
                        _notification_clients.discard(q)

                    logger.debug(
                        "Notification broadcast",
                        clients=len(_notification_clients),
                        data_preview=data[:100] if data else None
                    )
                except Exception as e:
                    logger.warning("Failed to broadcast notification", error=str(e))
    except asyncio.CancelledError:
        logger.info("Notification subscriber cancelled")
    except Exception as e:
        logger.error("Notification subscriber error", error=str(e))
    finally:
        await pubsub.unsubscribe("cognitex:notifications")
        await pubsub.close()


async def get_current_user(request: Request) -> dict:
    """Get current authenticated user or redirect to login."""
    session_cookie = request.cookies.get(SESSION_COOKIE_NAME)

    if not session_cookie:
        next_url = request.url.path
        if request.url.query:
            next_url += f"?{request.url.query}"
        raise HTTPException(
            status_code=303,
            headers={"Location": f"/auth/login?next={next_url}"}
        )

    user = await verify_session(session_cookie)
    if not user:
        raise HTTPException(
            status_code=303,
            headers={"Location": f"/auth/login?next={request.url.path}"}
        )

    return user


async def get_optional_user(request: Request) -> dict | None:
    """Get current user if logged in, or None."""
    session_cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_cookie:
        return None
    return await verify_session(session_cookie)


@app.get("/auth/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str | None = None, next: str = "/"):
    """Show login page."""
    # If already logged in, redirect to home
    user = await get_optional_user(request)
    if user:
        return RedirectResponse(url="/", status_code=303)

    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": error, "next": next, "user": None}
    )


@app.get("/auth/google")
async def google_login(request: Request, next: str = "/"):
    """Initiate Google OAuth flow."""
    import secrets

    # Determine redirect URI based on request
    base_url = str(request.base_url).rstrip("/")
    redirect_uri = f"{base_url}/auth/callback"

    try:
        flow = create_oauth_flow(redirect_uri)
    except ValueError as e:
        auth_logger.error("OAuth flow creation failed", error=str(e))
        return RedirectResponse(url="/auth/login?error=OAuth+not+configured", status_code=303)

    # Generate state for CSRF protection
    state = secrets.token_urlsafe(32)

    authorization_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="select_account",
        state=state,
    )

    # Store state and next URL in Redis
    await store_oauth_state(state, next)

    return RedirectResponse(url=authorization_url, status_code=303)


@app.get("/auth/callback")
async def google_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None
):
    """Handle Google OAuth callback."""
    if error:
        auth_logger.warning("OAuth error from Google", error=error)
        return RedirectResponse(url=f"/auth/login?error={error}", status_code=303)

    if not code or not state:
        return RedirectResponse(url="/auth/login?error=Missing+authorization+code", status_code=303)

    # Verify state
    next_url = await verify_oauth_state(state)
    if not next_url:
        auth_logger.warning("Invalid OAuth state")
        return RedirectResponse(url="/auth/login?error=Invalid+state", status_code=303)

    # Exchange code for tokens using direct HTTP (avoids scope mismatch issues)
    import httpx

    settings = get_settings()
    base_url = str(request.base_url).rstrip("/")
    redirect_uri = f"{base_url}/auth/callback"

    try:
        # Exchange authorization code for tokens
        async with httpx.AsyncClient() as client:
            token_response = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": code,
                    "client_id": settings.google_client_id,
                    "client_secret": settings.google_client_secret.get_secret_value(),
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
            )

            if token_response.status_code != 200:
                auth_logger.error("Token exchange failed", status=token_response.status_code)
                return RedirectResponse(url="/auth/login?error=Token+exchange+failed", status_code=303)

            tokens = token_response.json()

            # Get user info from userinfo endpoint
            userinfo_response = await client.get(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                headers={"Authorization": f"Bearer {tokens['access_token']}"},
            )

            if userinfo_response.status_code != 200:
                auth_logger.error("Userinfo fetch failed", status=userinfo_response.status_code)
                return RedirectResponse(url="/auth/login?error=Failed+to+get+user+info", status_code=303)

            userinfo = userinfo_response.json()

        user_email = userinfo.get("email", "").lower()
        user_name = userinfo.get("name")

    except Exception as e:
        auth_logger.error("OAuth callback failed", error=str(e))
        return RedirectResponse(url="/auth/login?error=Authentication+failed", status_code=303)

    # Check if email is allowed
    allowed_emails = get_allowed_emails()
    if allowed_emails and user_email not in allowed_emails:
        auth_logger.warning("Unauthorized login attempt", email=user_email)
        return RedirectResponse(
            url="/auth/login?error=Access+not+authorized.+Contact+administrator.",
            status_code=303
        )

    # Create session
    signed_session = await create_session(user_email, user_name)

    auth_logger.info("User logged in", email=user_email)

    # Set cookie and redirect
    response = RedirectResponse(url=next_url or "/", status_code=303)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=signed_session,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=settings.is_production,
    )
    return response


@app.get("/auth/logout")
async def logout(request: Request):
    """Log out and destroy session."""
    session_cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if session_cookie:
        await destroy_session(session_cookie)
        auth_logger.info("User logged out")

    response = RedirectResponse(url="/auth/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Serve favicon."""
    favicon_path = STATIC_DIR / "favicon.ico"
    if favicon_path.exists():
        return FileResponse(favicon_path)
    raise HTTPException(status_code=404, detail="Favicon not found")


# -------------------------------------------------------------------
# Authentication Middleware
# -------------------------------------------------------------------

# Public paths that don't require authentication
PUBLIC_PATHS = {
    "/auth/login",
    "/auth/google",
    "/auth/callback",
    "/auth/logout",
    "/favicon.ico",
    "/api/sync/sessions",
    "/api/sync/status",
    "/downloads/cognitex-sync-install.sh",
    "/downloads/cognitex-sync.tar.gz",
}

PUBLIC_PREFIXES = (
    "/static/",
    "/auth/",
)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Require authentication for all routes except public ones."""
    path = request.url.path

    # Skip auth for public paths
    if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
        return await call_next(request)

    # Check for valid session
    session_cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if session_cookie:
        user = await verify_session(session_cookie)
        if user:
            # Store user in request state for templates
            request.state.user = user
            return await call_next(request)

    # Not authenticated
    # For API endpoints, return 401 instead of redirecting
    if path.startswith("/api/"):
        return JSONResponse(
            status_code=401,
            content={"error": "Not authenticated", "redirect": "/auth/login"}
        )

    # For regular pages, redirect to login
    next_url = path
    if request.url.query:
        next_url += f"?{request.url.query}"

    return RedirectResponse(
        url=f"/auth/login?next={next_url}",
        status_code=303
    )


# -------------------------------------------------------------------
# Home / Navigation
# -------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Chat-first landing page with Erdos."""
    from cognitex.services.model_config import get_model_config_service

    service = get_model_config_service()
    config = await service.get_config()

    return templates.TemplateResponse(
        "chat_home.html",
        {
            "request": request,
            "provider": config.provider.title(),
            "model": config.planner_model,
        },
    )


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Dashboard page with comprehensive overview (formerly /)."""
    from cognitex.services.inbox import get_inbox_service
    from cognitex.services.ideas import list_ideas
    from cognitex.db.neo4j import get_neo4j_session
    from cognitex.agent.action_log import get_recent_actions
    from datetime import datetime, timedelta

    task_service = get_task_service()
    project_service = get_project_service()
    goal_service = get_goal_service()
    inbox_service = get_inbox_service()

    tasks = await task_service.list(limit=20)
    projects = await project_service.list(limit=10)
    goals = await goal_service.list(limit=10)

    # Count stats
    pending_tasks = len([t for t in tasks if t.get("status") != "done"])
    active_projects = len([p for p in projects if p.get("status") == "active"])
    active_goals = len([g for g in goals if g.get("status") == "active"])

    # Inbox counts
    inbox_counts = await inbox_service.get_pending_count()
    pending_inbox = inbox_counts.get("total", 0)
    urgent_inbox = inbox_counts.get("urgent", 0)

    # Get inbox items for preview
    inbox_items = await inbox_service.get_pending_items(limit=5)

    # Get ideas count
    try:
        ideas = await list_ideas(status="active", limit=100)
        idea_count = len(ideas)
    except Exception:
        idea_count = 0

    # Get today's events
    today_events = []
    try:
        async for session in get_neo4j_session():
            result = await session.run("""
                MATCH (e:Event)
                WHERE date(e.start) = date()
                RETURN e.gcal_id as id, e.summary as title, e.start as start, e.end as end
                ORDER BY e.start
                LIMIT 5
            """)
            today_events = await result.data()
            break
    except Exception:
        pass

    # Get recent agent actions
    try:
        recent_actions = await get_recent_actions(limit=5)
    except Exception:
        recent_actions = []

    # Get upcoming deadlines (tasks due within 7 days)
    upcoming_deadlines = [
        t for t in tasks
        if t.get("due") and t.get("status") != "done"
    ][:5]

    # High priority tasks
    high_priority_tasks = [
        t for t in tasks
        if t.get("priority") in ("high", "urgent") and t.get("status") != "done"
    ][:5]

    return templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            # Stats
            "pending_tasks": pending_tasks,
            "active_projects": active_projects,
            "active_goals": active_goals,
            "pending_inbox": pending_inbox,
            "urgent_inbox": urgent_inbox,
            "idea_count": idea_count,
            # Lists
            "recent_tasks": tasks[:5],
            "recent_projects": projects[:5],
            "inbox_items": inbox_items,
            "today_events": today_events,
            "recent_actions": recent_actions,
            "upcoming_deadlines": upcoming_deadlines,
            "high_priority_tasks": high_priority_tasks,
            # Inbox breakdown
            "inbox_by_type": inbox_counts.get("by_type", {}),
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
    """Mark task as complete and record state observation for learning."""
    task_service = get_task_service()

    try:
        # Get task info before completing (for learning)
        task_before = await task_service.get(task_id)

        if not task_before:
            raise HTTPException(status_code=404, detail="Task not found")

        task = await task_service.complete(task_id)

        if not task:
            raise HTTPException(status_code=404, detail="Task not found")

        # Record state observation for learning (non-blocking)
        try:
            await _record_task_outcome(
                task_id=task_id,
                task_title=task_before.get("title", "") if task_before else "",
                outcome="completed",
                energy_cost=task_before.get("energy_cost", "medium") if task_before else "medium",
            )
        except Exception as e:
            logger.debug("Task outcome recording skipped", error=str(e))

        # Self-improve expertise based on completed task (non-blocking)
        if task_before:
            try:
                from cognitex.agent.expertise import get_expertise_manager
                em = get_expertise_manager()

                # Determine domain from project or general task completion
                projects = task_before.get("projects", [])
                project_id = projects[0].get("id") if projects else None
                domain = f"project:{project_id}" if project_id else "task_completion"

                await em.self_improve(
                    domain=domain,
                    action_type="task_completed",
                    action_result={
                        "task_id": task_id,
                        "title": task_before.get("title"),
                        "priority": task_before.get("priority"),
                        "project_id": project_id,
                        "subtasks_count": len(task_before.get("subtasks", [])),
                    },
                    context={
                        "created_by": task_before.get("created_by"),
                        "was_autonomous": task_before.get("created_by") == "autonomous_agent",
                    },
                )
            except Exception as e:
                logger.debug("Expertise self-improve skipped", error=str(e))

        # Return empty response - hx-swap="delete" will remove the row
        return HTMLResponse("")

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Task completion failed", task_id=task_id, error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/tasks/{task_id}", response_class=HTMLResponse)
async def task_delete(task_id: str):
    """Delete a task."""
    task_service = get_task_service()
    await task_service.delete(task_id)
    return HTMLResponse("")


@app.post("/tasks/{task_id}/reject", response_class=HTMLResponse)
async def task_reject_and_learn(
    request: Request,
    task_id: str,
    reason: str = Form(...),
):
    """Reject a task and record feedback for learning.

    This is used when the autonomous agent created a task that shouldn't exist.
    Records the rejection pattern to prevent similar tasks in the future.
    """
    from cognitex.db.neo4j import get_neo4j_session
    from cognitex.agent.action_log import log_action, learn_from_rejection

    task_service = get_task_service()

    # Get task details and source email before deleting
    task_info = None
    email_info = None

    async for session in get_neo4j_session():
        # Get task details
        result = await session.run("""
            MATCH (t:Task {id: $task_id})
            OPTIONAL MATCH (t)-[:DERIVED_FROM]->(e:Email)
            OPTIONAL MATCH (e)-[:SENT_BY]->(sender:Person)
            RETURN t.title as title,
                   t.description as description,
                   t.created_by as created_by,
                   e.gmail_id as email_id,
                   e.subject as email_subject,
                   e.snippet as email_snippet,
                   sender.email as sender_email
        """, {"task_id": task_id})
        record = await result.single()

        if record:
            task_info = {
                "title": record["title"],
                "description": record["description"],
                "created_by": record["created_by"],
            }
            if record["email_id"]:
                email_info = {
                    "gmail_id": record["email_id"],
                    "subject": record["email_subject"],
                    "snippet": record["email_snippet"],
                    "sender": record["sender_email"],
                }
        break

    if not task_info:
        return HTMLResponse("<span style='color: red;'>Task not found</span>")

    # Record rejection pattern for learning
    pattern_data = {
        "task_title": task_info["title"],
        "reason": reason,
        "rejected_at": datetime.now().isoformat(),
    }

    if email_info:
        pattern_data["email_subject"] = email_info["subject"]
        pattern_data["email_sender"] = email_info["sender"]
        pattern_data["email_snippet"] = email_info.get("snippet", "")[:200]

        # Record as rejection pattern for learning
        # Include email subject and sender in the rejection reason for pattern matching
        rejection_context = f"From: {email_info['sender']} | Subject: {email_info['subject']} | Task: {task_info['title']}"
        await learn_from_rejection(
            proposal_type="create_task",
            rejection_reason=rejection_context,
            context={
                "reason_category": reason,
                "email_sender": email_info["sender"],
                "email_subject": email_info["subject"],
                "task_title": task_info["title"],
            }
        )

    # Log the rejection action
    await log_action(
        "task_rejected",
        "user",
        summary=f"Rejected task: {task_info['title'][:50]}",
        details={
            "task_id": task_id,
            "reason": reason,
            "had_source_email": email_info is not None,
            **pattern_data,
        }
    )

    # Route to skill feedback
    try:
        from cognitex.agent.skill_feedback_router import route_rejection_to_skill
        await route_rejection_to_skill(
            proposal_type="create_task",
            reason=reason,
            context={
                "task_title": task_info["title"],
                "email_subject": email_info.get("subject") if email_info else None,
            },
        )
    except Exception:
        pass  # Skill feedback is non-critical

    # Delete the task
    await task_service.delete(task_id)

    logger.info(
        "Task rejected with learning feedback",
        task_id=task_id,
        reason=reason,
        had_email=email_info is not None,
    )

    return HTMLResponse("")


@app.delete("/tasks/{task_id}/project/{project_id}", response_class=HTMLResponse)
async def task_unlink_project(request: Request, task_id: str, project_id: str):
    """Remove a project link from a task.

    Records as learning feedback if the link was created by the autonomous agent.
    """
    from cognitex.db.neo4j import get_neo4j_session
    from cognitex.agent.action_log import log_action

    task_service = get_task_service()

    # Get info about the link before removing
    link_info = None
    async for session in get_neo4j_session():
        result = await session.run("""
            MATCH (t:Task {id: $task_id})-[r:BELONGS_TO|PART_OF]->(p:Project {id: $project_id})
            RETURN t.title as task_title, p.title as project_title, r.created_by as created_by
        """, {"task_id": task_id, "project_id": project_id})
        link_info = await result.single()

        if link_info:
            # Remove the relationship
            await session.run("""
                MATCH (t:Task {id: $task_id})-[r:BELONGS_TO|PART_OF]->(p:Project {id: $project_id})
                DELETE r
            """, {"task_id": task_id, "project_id": project_id})

        break

    if link_info:
        # Log the action
        await log_action(
            "unlink_task_project",
            "web_ui",
            summary=f"Removed link: '{link_info['task_title']}' from '{link_info['project_title']}'",
            details={
                "task_id": task_id,
                "project_id": project_id,
                "task_title": link_info["task_title"],
                "project_title": link_info["project_title"],
                "original_created_by": link_info["created_by"],
            }
        )

        # Record as negative learning signal if autonomous agent created this link
        if link_info["created_by"] == "autonomous_agent":
            try:
                from cognitex.db.postgres import get_postgres_session
                from sqlalchemy import text
                import uuid

                async for session in get_postgres_session():
                    # Record as a learned pattern (negative feedback on link suggestion)
                    await session.execute(text("""
                        INSERT INTO learned_patterns (id, pattern_type, pattern_data, confidence, sample_count)
                        VALUES (:id, 'rejected_link', :pattern_data, 0.0, 1)
                        ON CONFLICT (pattern_type, (pattern_data->>'task_keywords'), (pattern_data->>'project_id'))
                        DO UPDATE SET
                            sample_count = learned_patterns.sample_count + 1,
                            confidence = LEAST(learned_patterns.confidence - 0.1, 0),
                            updated_at = NOW()
                    """), {
                        "id": f"pattern_{uuid.uuid4().hex[:12]}",
                        "pattern_data": {
                            "task_keywords": link_info["task_title"].lower().split()[:3],
                            "project_id": project_id,
                            "project_title": link_info["project_title"],
                            "feedback": "user_removed_link",
                        },
                    })
                    await session.commit()
                    break

                logger.info(
                    "Recorded negative learning signal for autonomous link",
                    task_id=task_id,
                    project_id=project_id,
                )
            except Exception as e:
                logger.warning("Failed to record learning signal", error=str(e))

    # Return updated task row
    task = await task_service.get(task_id)
    if task:
        return templates.TemplateResponse(
            "partials/task_row.html",
            {"request": request, "task": task},
        )
    return HTMLResponse("")


# =============================================================================
# Subtask endpoints (lightweight steps within tasks)
# =============================================================================


@app.get("/tasks/{task_id}/subtasks", response_class=HTMLResponse)
async def get_subtasks(request: Request, task_id: str):
    """Get subtasks partial for a task."""
    from cognitex.db.neo4j import get_neo4j_session
    import json

    subtasks = []
    async for session in get_neo4j_session():
        result = await session.run(
            "MATCH (t:Task {id: $task_id}) RETURN t.subtasks as subtasks",
            {"task_id": task_id}
        )
        record = await result.single()
        if record and record["subtasks"]:
            try:
                subtasks = json.loads(record["subtasks"]) if isinstance(record["subtasks"], str) else record["subtasks"]
            except (json.JSONDecodeError, TypeError):
                subtasks = []
        break

    # Sort by order
    subtasks = sorted(subtasks, key=lambda x: x.get("order", 0))

    return templates.TemplateResponse(
        "partials/task_subtasks.html",
        {"request": request, "task_id": task_id, "subtasks": subtasks},
    )


@app.post("/tasks/{task_id}/subtasks", response_class=HTMLResponse)
async def add_subtask(request: Request, task_id: str, text: Annotated[str, Form()]):
    """Add a new subtask to a task."""
    from cognitex.db.neo4j import get_neo4j_session
    import json
    import secrets

    async for session in get_neo4j_session():
        # Get existing subtasks
        result = await session.run(
            "MATCH (t:Task {id: $task_id}) RETURN t.subtasks as subtasks",
            {"task_id": task_id}
        )
        record = await result.single()

        subtasks = []
        if record and record["subtasks"]:
            try:
                subtasks = json.loads(record["subtasks"]) if isinstance(record["subtasks"], str) else record["subtasks"]
            except (json.JSONDecodeError, TypeError):
                subtasks = []

        # Add new subtask
        new_subtask = {
            "id": f"s_{secrets.token_hex(3)}",
            "text": text.strip(),
            "done": False,
            "order": len(subtasks),
        }
        subtasks.append(new_subtask)

        # Save back to Neo4j
        await session.run(
            "MATCH (t:Task {id: $task_id}) SET t.subtasks = $subtasks",
            {"task_id": task_id, "subtasks": json.dumps(subtasks)}
        )
        break

    return await get_subtasks(request, task_id)


@app.post("/tasks/{task_id}/subtasks/{subtask_id}/toggle", response_class=HTMLResponse)
async def toggle_subtask(request: Request, task_id: str, subtask_id: str):
    """Toggle a subtask's done status."""
    from cognitex.db.neo4j import get_neo4j_session
    import json

    async for session in get_neo4j_session():
        result = await session.run(
            "MATCH (t:Task {id: $task_id}) RETURN t.subtasks as subtasks",
            {"task_id": task_id}
        )
        record = await result.single()

        subtasks = []
        if record and record["subtasks"]:
            try:
                subtasks = json.loads(record["subtasks"]) if isinstance(record["subtasks"], str) else record["subtasks"]
            except (json.JSONDecodeError, TypeError):
                subtasks = []

        # Toggle the subtask
        for subtask in subtasks:
            if subtask.get("id") == subtask_id:
                subtask["done"] = not subtask.get("done", False)
                break

        # Save back
        await session.run(
            "MATCH (t:Task {id: $task_id}) SET t.subtasks = $subtasks",
            {"task_id": task_id, "subtasks": json.dumps(subtasks)}
        )
        break

    return await get_subtasks(request, task_id)


@app.delete("/tasks/{task_id}/subtasks/{subtask_id}", response_class=HTMLResponse)
async def delete_subtask(request: Request, task_id: str, subtask_id: str):
    """Delete a subtask."""
    from cognitex.db.neo4j import get_neo4j_session
    import json

    async for session in get_neo4j_session():
        result = await session.run(
            "MATCH (t:Task {id: $task_id}) RETURN t.subtasks as subtasks",
            {"task_id": task_id}
        )
        record = await result.single()

        subtasks = []
        if record and record["subtasks"]:
            try:
                subtasks = json.loads(record["subtasks"]) if isinstance(record["subtasks"], str) else record["subtasks"]
            except (json.JSONDecodeError, TypeError):
                subtasks = []

        # Remove the subtask
        subtasks = [s for s in subtasks if s.get("id") != subtask_id]

        # Reorder remaining subtasks
        for i, subtask in enumerate(subtasks):
            subtask["order"] = i

        # Save back
        await session.run(
            "MATCH (t:Task {id: $task_id}) SET t.subtasks = $subtasks",
            {"task_id": task_id, "subtasks": json.dumps(subtasks)}
        )
        break

    return await get_subtasks(request, task_id)


@app.post("/tasks/{task_id}/subtasks/reorder")
async def reorder_subtasks(request: Request, task_id: str):
    """Reorder subtasks based on drag-and-drop."""
    from cognitex.db.neo4j import get_neo4j_session
    import json

    body = await request.json()
    ordered_ids = body.get("order", [])

    async for session in get_neo4j_session():
        result = await session.run(
            "MATCH (t:Task {id: $task_id}) RETURN t.subtasks as subtasks",
            {"task_id": task_id}
        )
        record = await result.single()

        subtasks = []
        if record and record["subtasks"]:
            try:
                subtasks = json.loads(record["subtasks"]) if isinstance(record["subtasks"], str) else record["subtasks"]
            except (json.JSONDecodeError, TypeError):
                subtasks = []

        # Create lookup by id
        subtask_map = {s["id"]: s for s in subtasks}

        # Rebuild list in new order
        reordered = []
        for i, subtask_id in enumerate(ordered_ids):
            if subtask_id in subtask_map:
                subtask_map[subtask_id]["order"] = i
                reordered.append(subtask_map[subtask_id])

        # Add any subtasks that weren't in the order list
        for subtask in subtasks:
            if subtask["id"] not in ordered_ids:
                subtask["order"] = len(reordered)
                reordered.append(subtask)

        # Save back
        await session.run(
            "MATCH (t:Task {id: $task_id}) SET t.subtasks = $subtasks",
            {"task_id": task_id, "subtasks": json.dumps(reordered)}
        )
        break

    return {"status": "ok"}


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


@app.get("/projects/{project_id}", response_class=HTMLResponse)
async def project_detail(request: Request, project_id: str):
    """Deep dive project page with development context."""
    from cognitex.services.coding_sessions import get_session_ingester

    project_service = get_project_service()
    project = await project_service.get(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Get coding sessions
    ingester = get_session_ingester()
    sessions = await ingester.get_project_development_context(project["title"], limit=10)

    # Get tasks
    tasks = await project_service.get_tasks(project_id)

    return templates.TemplateResponse(
        "project_detail.html",
        {
            "request": request,
            "project": project,
            "sessions": sessions,
            "tasks": tasks,
        },
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
# Commitments
# -------------------------------------------------------------------


@app.get("/commitments", response_class=HTMLResponse)
async def commitments_page(request: Request):
    """Commitments page showing all tracked commitments with status grouping."""
    import structlog
    from cognitex.db.neo4j import get_neo4j_session
    from cognitex.db.graph_schema import get_commitments
    from cognitex.agent.graph_observer import GraphObserver

    logger = structlog.get_logger()

    all_commitments: list[dict] = []
    approaching: list[dict] = []
    overdue: list[dict] = []

    try:
        async for session in get_neo4j_session():
            # Get all commitments
            all_commitments = await get_commitments(session)

            # Get approaching and overdue via GraphObserver
            observer = GraphObserver(session)
            approaching = await observer.get_approaching_commitments(hours=48)
            overdue = await observer.get_overdue_commitments()
            break
    except Exception as e:
        logger.warning("Failed to load commitments", error=str(e))

    # Build sets of overdue/approaching IDs for status annotation
    overdue_ids = {c.get("id") for c in overdue}
    approaching_ids = {c.get("id") for c in approaching}

    # Annotate commitments with display status and sort
    for c in all_commitments:
        cid = c.get("id")
        if cid in overdue_ids:
            c["status"] = "overdue"
        elif cid in approaching_ids and c.get("status") not in ("completed",):
            c["status"] = "approaching"

    # Sort: overdue first, then approaching, then active, then completed
    status_order = {
        "overdue": 0, "approaching": 1, "in_progress": 2,
        "accepted": 3, "blocked": 4, "waiting_on": 5, "completed": 6,
    }
    all_commitments.sort(
        key=lambda c: status_order.get(c.get("status", ""), 4)
    )

    # Count active (non-completed, non-overdue)
    active_count = sum(
        1 for c in all_commitments
        if c.get("status") not in ("completed", "overdue", "approaching")
    )
    completed_count = sum(1 for c in all_commitments if c.get("status") == "completed")

    # Cognitive load counts (active commitments only)
    load_counts = {"high": 0, "medium": 0, "low": 0}
    for c in all_commitments:
        if c.get("status") not in ("completed",):
            load = c.get("cognitive_load", "medium") or "medium"
            if load in load_counts:
                load_counts[load] += 1

    # Waiting on: aggregate people with pending commitments
    waiting_on_map: dict[str, int] = {}
    for c in all_commitments:
        if c.get("status") not in ("completed",) and c.get("waiting_on_email"):
            email = c["waiting_on_email"]
            waiting_on_map[email] = waiting_on_map.get(email, 0) + 1
    waiting_on = [
        {"email": email, "count": count}
        for email, count in sorted(waiting_on_map.items(), key=lambda x: -x[1])
    ]

    stats = {
        "active": active_count,
        "overdue": len(overdue),
        "approaching": len(approaching),
        "completed": completed_count,
    }

    return templates.TemplateResponse(
        "commitments.html",
        {
            "request": request,
            "commitments": all_commitments,
            "stats": stats,
            "load_counts": load_counts,
            "waiting_on": waiting_on,
        },
    )


# -------------------------------------------------------------------
# Today / Day Plan
# -------------------------------------------------------------------


async def get_today_events() -> list[dict]:
    """Get calendar events for today with context packs."""
    from cognitex.db.neo4j import get_neo4j_session
    from cognitex.db.phase3_schema import get_context_pack

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

            # Query context pack for this event
            event_id = event.get("id")
            if event_id:
                pack = await get_context_pack(session, event_id=event_id)
                if pack:
                    # Extract key fields for display
                    event["context_pack"] = {
                        "objective": pack.get("objective"),
                        "last_interaction": pack.get("last_touch_recap"),
                        "artifacts": pack.get("artifact_links", [])[:3],  # Top 3 docs
                        "readiness_score": pack.get("readiness_score"),
                        "dont_forget": pack.get("dont_forget", []),
                    }
                else:
                    event["context_pack"] = None
            else:
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

    # Get upcoming deadlines (list query returns 'due' as alias for due_date)
    all_tasks = await task_service.list(include_done=False, limit=50)
    deadlines = [
        {"title": t["title"], "due": t["due"][:10] if t.get("due") else None}
        for t in all_tasks
        if t.get("due")
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


@app.get("/briefing", response_class=HTMLResponse)
async def view_briefing(request: Request):
    """Render the morning briefing as a web page (on-demand)."""
    import structlog
    from cognitex.agent.core import get_agent

    logger = structlog.get_logger()

    try:
        agent = await get_agent()
        content = await agent.morning_briefing()

        # Convert markdown to HTML using markdown library if available
        try:
            import markdown
            html_content = markdown.markdown(content, extensions=['tables', 'fenced_code'])
        except ImportError:
            # Fallback: wrap in pre tag if markdown not available
            html_content = f"<pre style='white-space: pre-wrap;'>{content}</pre>"

    except Exception as e:
        logger.error("Failed to generate briefing", error=str(e))
        html_content = f"<p class='error'>Failed to generate briefing: {e}</p>"

    return templates.TemplateResponse(
        "briefing.html",
        {
            "request": request,
            "today_date": date.today().strftime("%A, %B %d, %Y"),
            "briefing_html": html_content,
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
    """Get pending suggested calendar blocks that are still relevant.

    Filters out stale blocks based on suggested_day and created_at:
    - "today" blocks: only valid if created today
    - "tomorrow" blocks: only valid if created today or yesterday
    - "next week" blocks: valid if created within last 7 days

    Also deduplicates by project to avoid showing multiple similar suggestions.
    """
    from cognitex.db.neo4j import get_neo4j_session

    async for session in get_neo4j_session():
        # Filter blocks based on their suggested_day relative to created_at
        # A "tomorrow" block created 3 days ago is stale
        query = """
        MATCH (sb:SuggestedBlock {status: 'pending_approval'})
        WHERE
            // "today" blocks: must be created today
            (sb.suggested_day = 'today' AND sb.created_at >= datetime() - duration({hours: 18}))
            OR
            // "tomorrow" blocks: must be created today or yesterday
            (sb.suggested_day = 'tomorrow' AND sb.created_at >= datetime() - duration({days: 1, hours: 12}))
            OR
            // "next week" blocks: valid for 7 days
            (sb.suggested_day = 'next week' AND sb.created_at >= datetime() - duration({days: 7}))
            OR
            // Any other suggested_day: valid for 3 days
            (NOT sb.suggested_day IN ['today', 'tomorrow', 'next week']
             AND sb.created_at >= datetime() - duration({days: 3}))
        OPTIONAL MATCH (sb)-[:FOR_PROJECT]->(p:Project)
        OPTIONAL MATCH (sb)-[:FOR_TASK]->(t:Task)
        WITH sb, p, t
        // Deduplicate: keep only the most recent block per project
        ORDER BY sb.created_at DESC
        WITH p, t, COLLECT(sb)[0] as sb
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
        LIMIT 10
        """
        result = await session.run(query)
        data = await result.data()
        return data
    return []


@app.get("/twin")
async def twin_redirect():
    """Redirect to settings Agent tab."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/settings?tab=agent", status_code=302)


@app.get("/twin_DEPRECATED", response_class=HTMLResponse)
async def twin_page_deprecated(request: Request):
    """Digital Twin configuration page - configure agent persona and voice.
    DEPRECATED: Now part of unified Settings page.
    """
    from cognitex.db.postgres import get_session
    from cognitex.db.neo4j import get_neo4j_session
    from sqlalchemy import text

    # Get approval stats
    approved_drafts = 0
    approved_tasks = 0
    learning_samples = 0

    async for session in get_session():
        try:
            # Count approved emails from inbox feedback
            result = await session.execute(text("""
                SELECT COUNT(*) FROM inbox_feedback
                WHERE item_type = 'email_draft' AND action = 'approved'
            """))
            row = result.fetchone()
            approved_drafts = row[0] if row else 0

            # Count approved tasks
            result = await session.execute(text("""
                SELECT COUNT(*) FROM inbox_feedback
                WHERE item_type = 'task_proposal' AND action = 'approved'
            """))
            row = result.fetchone()
            approved_tasks = row[0] if row else 0

            # Count total learning samples
            result = await session.execute(text("""
                SELECT COUNT(*) FROM inbox_feedback
            """))
            row = result.fetchone()
            learning_samples = row[0] if row else 0
        except Exception as e:
            logger.debug("Could not get twin stats", error=str(e))
        break

    # Get twin settings from preferences
    settings = {}
    async for session in get_session():
        try:
            result = await session.execute(text("""
                SELECT preference FROM preference_rules
                WHERE rule_type = 'twin_settings'
                LIMIT 1
            """))
            row = result.fetchone()
            if row and row[0]:
                import json
                settings = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        except Exception:
            pass
        break

    # Get recent approved emails from Neo4j
    recent_emails = []
    async for neo_session in get_neo4j_session():
        try:
            result = await neo_session.run("""
                MATCH (d:EmailDraft)
                WHERE d.status = 'approved'
                RETURN d.to as to, d.subject as subject, d.body as body,
                       d.approved_at as approved_at
                ORDER BY d.approved_at DESC
                LIMIT 5
            """)
            records = await result.data()
            recent_emails = records
        except Exception:
            pass
        break

    # Patterns would come from learning system - placeholder for now
    patterns = []

    return templates.TemplateResponse(
        "twin.html",
        {
            "request": request,
            "approved_drafts": approved_drafts,
            "approved_tasks": approved_tasks,
            "learning_samples": learning_samples,
            "settings": settings,
            "patterns": patterns,
            "recent_emails": recent_emails,
        },
    )


@app.post("/api/twin/settings")
async def save_twin_settings(
    formality: str = Form("balanced"),
    brevity: str = Form("balanced"),
    signature: str = Form(""),
):
    """Save digital twin voice/tone settings."""
    from cognitex.db.postgres import get_session
    from sqlalchemy import text
    import json
    import uuid

    settings = {
        "formality": formality,
        "brevity": brevity,
        "signature": signature,
    }

    async for session in get_session():
        try:
            # Delete existing then insert (simple approach)
            await session.execute(text("""
                DELETE FROM preference_rules WHERE rule_type = 'twin_settings'
            """))
            await session.execute(text("""
                INSERT INTO preference_rules (id, rule_type, rule_name, condition, preference, source_trace_ids, created_at)
                VALUES (:id, 'twin_settings', 'Voice & Tone Settings', '{}'::jsonb, :pref, ARRAY['user'], NOW())
            """), {"id": f"pref_{uuid.uuid4().hex[:12]}", "pref": json.dumps(settings)})
            await session.commit()
            logger.info("Twin settings saved", settings=settings)
        except Exception as e:
            await session.rollback()
            logger.warning("Failed to save twin settings", error=str(e))
        break

    return {"success": True}


@app.post("/api/twin/drafts/{draft_id}/approve")
async def approve_draft(draft_id: str):
    """Approve and send an email draft via the Agent."""
    from cognitex.agent.core import get_agent
    from cognitex.db.neo4j import get_neo4j_session

    # 1. Try to find the approval in working memory to use Agent flow
    try:
        agent = await get_agent()
        approvals = await agent.get_pending_approvals()

        # Find approval where params.draft_node_id or draft_id matches
        target_approval = None
        for app in approvals:
            params = app.get("params", {})
            if params.get("draft_node_id") == draft_id or params.get("draft_id") == draft_id:
                target_approval = app
                break

        if target_approval:
            # Use Agent to handle approval (triggers sending + learning)
            result = await agent.handle_approval(target_approval["id"], approved=True)

            if result.get("success"):
                # Self-improve expertise based on successful email draft
                try:
                    from cognitex.agent.expertise import get_expertise_manager
                    em = get_expertise_manager()

                    # Determine domain from sender/project context
                    params = target_approval.get("params", {})
                    sender = params.get("to", "").split("@")[0] if params.get("to") else None
                    domain = f"email:{sender}" if sender else "email_drafting"

                    await em.self_improve(
                        domain=domain,
                        action_type="email_draft_approved",
                        action_result={
                            "draft_id": draft_id,
                            "to": params.get("to"),
                            "subject": params.get("subject"),
                            "body_preview": params.get("body", "")[:200],
                        },
                        context={"approval_id": target_approval["id"]},
                    )
                except Exception as e:
                    logger.debug("Expertise self-improve skipped", error=str(e))

                action_text = html.escape(str(result.get('action', 'Email queued for sending')))
                return HTMLResponse(f'''
                    <div class="draft-card" style="background: #d1fae5; border-color: #065f46;">
                        <p><strong>Approved!</strong> {action_text}</p>
                    </div>
                ''')
            else:
                logger.warning("Agent approval failed", error=result.get("error"))
    except Exception as e:
        logger.warning("Could not use agent for approval", error=str(e))

    # 2. Fallback: Direct graph update if approval not in working memory (expired)
    logger.warning("Approval not in working memory, using direct update", draft_id=draft_id)

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

        # Mark as approved (learning system won't receive this feedback)
        update_query = """
        MATCH (d:EmailDraft {id: $draft_id})
        SET d.status = 'approved', d.approved_at = datetime()
        """
        await session.run(update_query, {"draft_id": draft_id})

        logger.info("Email draft approved (direct)", draft_id=draft_id)

        return HTMLResponse(f'''
            <div class="draft-card" style="background: #d1fae5; border-color: #065f46;">
                <p><strong>Approved!</strong> Email will be sent to {html.escape(str(draft["to"]))}</p>
                <p style="font-size: 0.8rem; color: #666;">(Direct approval - learning skipped)</p>
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

        # Escape all user-provided values for HTML safety
        safe_to = html.escape(str(draft['to'] or ''))
        safe_subject = html.escape(str(draft['subject'] or ''))
        safe_body = html.escape(str(draft['body'] or ''))

        return HTMLResponse(f'''
            <form class="draft-edit-form" hx-put="/api/twin/drafts/{draft_id}" hx-target="#draft-{draft_id}" hx-swap="outerHTML">
                <div class="draft-meta">
                    <strong>To:</strong> <input type="text" name="to" value="{safe_to}" style="width: 300px;"><br>
                    <strong>Subject:</strong> <input type="text" name="subject" value="{safe_subject}" style="width: 100%;">
                </div>
                <textarea name="body" class="draft-body" style="min-height: 200px; width: 100%;">{safe_body}</textarea>
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
                <p><strong>Updated & Approved!</strong> Email will be sent to {html.escape(to)}</p>
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


@app.get("/api/twin/drafts/{draft_id}/reject-form")
async def get_draft_reject_form(draft_id: str):
    """Get the rejection feedback form for a draft."""
    # Quick-select rejection reasons
    reasons = [
        ("wrong_timing", "Wrong timing / not now"),
        ("not_relevant", "Not relevant to what I'm doing"),
        ("bad_suggestion", "Poor draft quality"),
        ("wrong_recipient", "Wrong recipient or context"),
        ("will_handle_manually", "I'll handle this myself"),
        ("other", "Other reason"),
    ]

    reason_buttons = "\n".join([
        f'''<button type="button" class="btn btn-outline-secondary btn-sm rejection-reason"
            data-reason="{code}"
            hx-post="/api/twin/drafts/{draft_id}/reject"
            hx-vals='{{"reason": "{code}", "reason_text": "{label}"}}'
            hx-target="#draft-{draft_id}"
            hx-swap="outerHTML"
            style="margin: 0.25rem;">{html.escape(label)}</button>'''
        for code, label in reasons
    ])

    return HTMLResponse(f'''
        <div class="rejection-form" style="background: #fef2f2; padding: 1rem; border-radius: 5px; margin-top: 0.5rem;">
            <p style="margin-bottom: 0.5rem;"><strong>Why are you rejecting this draft?</strong></p>
            <div class="rejection-reasons" style="display: flex; flex-wrap: wrap;">
                {reason_buttons}
            </div>
            <button type="button" class="btn btn-link btn-sm"
                hx-get="/api/twin/drafts/{draft_id}"
                hx-target="#draft-{draft_id}"
                hx-swap="outerHTML"
                style="margin-top: 0.5rem;">Cancel</button>
        </div>
    ''')


@app.post("/api/twin/drafts/{draft_id}/reject")
async def reject_draft_with_feedback(
    draft_id: str,
    reason: Annotated[str, Form()],
    reason_text: Annotated[str, Form()] = "",
):
    """Reject a draft with feedback reason for learning."""
    from cognitex.db.neo4j import get_neo4j_session
    from cognitex.agent.decision_memory import get_decision_memory
    from cognitex.agent.action_log import learn_from_rejection, log_action

    async for session in get_neo4j_session():
        # Update draft status and store rejection reason, also get original email info
        query = """
        MATCH (d:EmailDraft {id: $draft_id})
        SET d.status = 'rejected',
            d.rejected_at = datetime(),
            d.rejection_reason = $reason,
            d.rejection_reason_text = $reason_text
        WITH d
        OPTIONAL MATCH (d)-[:REPLIES_TO]->(e:Email)
        RETURN d.id as id, d.reasoning as reasoning, d.to as recipient,
               e.from as email_sender, e.subject as email_subject, e.gmail_id as email_id
        """
        result = await session.run(query, {
            "draft_id": draft_id,
            "reason": reason,
            "reason_text": reason_text,
        })
        data = await result.single()

        if not data:
            raise HTTPException(status_code=404, detail="Draft not found")

        # Record feedback in decision memory for learning
        try:
            dm = get_decision_memory()
            # Create a trace for learning from this rejection
            await dm.traces.create_trace(
                trigger_type="email_draft_rejection",
                action_type="draft_email",
                proposed_action={"draft_id": draft_id},
                trigger_id=draft_id,
                trigger_summary=f"Draft rejected: {reason_text or reason}",
                reasoning=data.get("reasoning", ""),
                metadata={"rejection_reason": reason, "rejection_reason_text": reason_text},
            )
            logger.info(
                "Draft rejection recorded for learning",
                draft_id=draft_id,
                reason=reason,
            )
        except Exception as e:
            logger.warning("Failed to record rejection for learning", error=str(e))

        # Also record in unified learning system (cross-learns with task rejection)
        # This helps the agent learn patterns like "don't engage with sender X"
        try:
            email_sender = data.get("email_sender") or data.get("recipient") or ""
            email_subject = data.get("email_subject") or ""

            await learn_from_rejection(
                proposal_type="draft_email",
                rejection_reason=f"{reason}: {email_subject[:50]}",
                context={
                    "reason_category": reason,
                    "email_sender": email_sender,
                    "email_subject": email_subject,
                    "email_id": data.get("email_id"),
                    "draft_id": draft_id,
                }
            )

            # Map rejection reasons to human-readable text for logging
            reason_labels = {
                "spam_marketing": "Spam/Marketing email",
                "automated_email": "Automated notification",
                "not_actionable": "No reply needed",
                "wrong_timing": "Wrong timing",
                "bad_suggestion": "Poor draft quality",
                "wrong_recipient": "Wrong recipient/context",
                "will_handle_manually": "Will handle manually",
            }

            await log_action(
                "draft_rejected",
                "web_ui",
                summary=f"Rejected draft to {email_sender[:30]}",
                details={
                    "draft_id": draft_id,
                    "reason": reason,
                    "reason_label": reason_labels.get(reason, reason),
                    "email_sender": email_sender,
                    "email_subject": email_subject[:100],
                }
            )
        except Exception as e:
            logger.warning("Failed to record draft rejection in unified learning", error=str(e))

        # Route to skill feedback
        try:
            from cognitex.agent.skill_feedback_router import route_rejection_to_skill
            await route_rejection_to_skill(
                proposal_type="draft_email",
                reason=reason,
                context={"email_subject": email_subject, "email_sender": email_sender},
            )
        except Exception:
            pass

        return HTMLResponse(f'''
            <div id="draft-{draft_id}" class="draft-card" style="background: #fef2f2; border-color: #b91c1c; opacity: 0.7;">
                <p><strong>Draft rejected</strong> - {html.escape(reason_text or reason)}</p>
                <p class="text-muted" style="font-size: 0.85rem;">This feedback helps improve future suggestions.</p>
            </div>
        ''')

    raise HTTPException(status_code=500, detail="Failed to reject draft")


# ========================================================================
# Generic User Feedback API
# ========================================================================

@app.post("/api/feedback")
async def record_user_feedback_api(
    target_type: Annotated[str, Form()],
    target_id: Annotated[str, Form()],
    feedback_category: Annotated[str | None, Form()] = None,
    feedback_text: Annotated[str | None, Form()] = None,
    was_rejection: Annotated[bool, Form()] = False,
    context: Annotated[str | None, Form()] = None,
    action_taken: Annotated[str | None, Form()] = None,
):
    """
    Generic endpoint for recording user feedback with optional free text.

    This endpoint stores feedback in the user_feedback table with semantic
    embeddings for later retrieval and rule extraction.

    Args:
        target_type: Type of item (context_pack, email_draft, task, proposal)
        target_id: ID of the specific item
        feedback_category: Quick-select category (e.g., 'spam_marketing', 'not_needed')
        feedback_text: Free text details for nuanced learning
        was_rejection: Whether this feedback is a rejection
        context: JSON string with rich context (email_subject, sender, etc.)
        action_taken: What action was taken ('rejected', 'edited', 'approved_with_note')
    """
    from cognitex.agent.feedback_learning import record_feedback
    import json as json_module

    # Parse context JSON if provided
    context_data = {}
    if context:
        try:
            context_data = json_module.loads(context)
        except json_module.JSONDecodeError:
            pass

    try:
        feedback_id = await record_feedback(
            target_type=target_type,
            target_id=target_id,
            feedback_category=feedback_category,
            feedback_text=feedback_text,
            was_rejection=was_rejection,
            context=context_data,
            action_taken=action_taken,
        )

        return HTMLResponse(f'''
            <div class="alert alert-success" style="padding: 0.5rem; font-size: 0.85rem;">
                <strong>Feedback recorded</strong> - This helps improve future suggestions.
            </div>
        ''')

    except Exception as e:
        logger.error("Failed to record feedback", error=str(e))
        return HTMLResponse(f'''
            <div class="alert alert-warning" style="padding: 0.5rem; font-size: 0.85rem;">
                <strong>Note:</strong> Feedback could not be saved, but action was completed.
            </div>
        ''', status_code=200)  # Still return 200 so the UI flow isn't broken


@app.get("/api/feedback/stats")
async def get_feedback_stats_api():
    """Get statistics about collected feedback for the learning dashboard."""
    from cognitex.agent.feedback_learning import get_feedback_stats

    try:
        stats = await get_feedback_stats()
        return stats
    except Exception as e:
        logger.error("Failed to get feedback stats", error=str(e))
        return {"error": str(e)}


# ========================================================================
# Phase 5.1: Multi-turn Email Drafting
# ========================================================================

@app.get("/api/twin/drafts/{draft_id}/refine-form")
async def get_draft_refine_form(draft_id: str):
    """Get the refinement form for iterating on a draft."""
    # Quick-select refinement suggestions
    refinements = [
        ("shorter", "Make it shorter"),
        ("longer", "Add more detail"),
        ("formal", "More formal tone"),
        ("casual", "More casual tone"),
        ("bullet_points", "Use bullet points"),
        ("custom", "Custom request..."),
    ]

    refinement_buttons = "\n".join([
        f'''<button type="button" class="btn btn-outline-primary btn-sm refinement-option"
            data-refinement="{code}"
            onclick="selectRefinement('{draft_id}', '{code}', '{label}')"
            style="margin: 0.25rem;">{html.escape(label)}</button>'''
        for code, label in refinements
    ])

    return HTMLResponse(f'''
        <div class="refinement-form" style="background: #f0f9ff; padding: 1rem; border-radius: 5px; margin-top: 0.5rem;">
            <p style="margin-bottom: 0.5rem;"><strong>How would you like to refine this draft?</strong></p>
            <div class="refinement-options" style="display: flex; flex-wrap: wrap; margin-bottom: 0.5rem;">
                {refinement_buttons}
            </div>
            <form id="refine-form-{draft_id}"
                  hx-post="/api/twin/drafts/{draft_id}/refine"
                  hx-target="#draft-{draft_id}"
                  hx-swap="outerHTML"
                  style="display: none;">
                <input type="hidden" name="refinement_type" id="refinement-type-{draft_id}" value="">
                <div id="custom-input-{draft_id}" style="display: none; margin-top: 0.5rem;">
                    <input type="text" name="custom_request" placeholder="Enter your refinement request..."
                           class="form-control" style="width: 100%;">
                </div>
                <button type="submit" class="btn btn-primary btn-sm" style="margin-top: 0.5rem;">
                    Apply Refinement
                </button>
            </form>
            <button type="button" class="btn btn-link btn-sm"
                hx-get="/twin"
                hx-target="body"
                style="margin-top: 0.5rem;">Cancel</button>
        </div>
        <script>
        function selectRefinement(draftId, type, label) {{
            document.getElementById('refinement-type-' + draftId).value = type;
            document.getElementById('refine-form-' + draftId).style.display = 'block';
            if (type === 'custom') {{
                document.getElementById('custom-input-' + draftId).style.display = 'block';
            }} else {{
                document.getElementById('custom-input-' + draftId).style.display = 'none';
            }}
            // Highlight selected button
            document.querySelectorAll('.refinement-option').forEach(btn => btn.classList.remove('btn-primary'));
            event.target.classList.add('btn-primary');
            event.target.classList.remove('btn-outline-primary');
        }}
        </script>
    ''')


@app.post("/api/twin/drafts/{draft_id}/refine")
async def refine_draft(
    draft_id: str,
    refinement_type: Annotated[str, Form()],
    custom_request: Annotated[str, Form()] = "",
):
    """Refine a draft using LLM based on user feedback (Phase 5.1 multi-turn drafting)."""
    from cognitex.db.neo4j import get_neo4j_session
    from cognitex.services.llm import LLMService

    async for session in get_neo4j_session():
        # Get current draft
        query = """
        MATCH (d:EmailDraft {id: $draft_id})
        OPTIONAL MATCH (d)-[:REPLY_TO]->(e:Email)
        RETURN d.id as id, d.to as to, d.subject as subject, d.body as body,
               d.revision_count as revision_count,
               e.subject as original_subject, e.body as original_body
        """
        result = await session.run(query, {"draft_id": draft_id})
        draft = await result.single()

        if not draft:
            raise HTTPException(status_code=404, detail="Draft not found")

        current_body = draft["body"]
        revision_count = (draft.get("revision_count") or 0) + 1

        # Build refinement prompt
        refinement_prompts = {
            "shorter": "Make this email significantly shorter and more concise. Keep the key points but remove unnecessary words.",
            "longer": "Expand this email with more detail. Add context, explanations, or relevant information.",
            "formal": "Rewrite this email in a more formal, professional tone. Use proper business language.",
            "casual": "Rewrite this email in a more casual, friendly tone. Keep it natural and approachable.",
            "bullet_points": "Restructure this email using bullet points for clarity. Organize the main points.",
        }

        if refinement_type == "custom" and custom_request:
            refinement_instruction = custom_request
        elif refinement_type in refinement_prompts:
            refinement_instruction = refinement_prompts[refinement_type]
        else:
            refinement_instruction = "Improve this email based on best practices."

        # Use LLM to refine
        llm = LLMService()
        refine_prompt = f"""You are refining an email draft based on user feedback.

CURRENT DRAFT:
To: {draft['to']}
Subject: {draft['subject']}

{current_body}

USER REQUEST: {refinement_instruction}

Rewrite the email body ONLY (not the subject or recipient). Return ONLY the refined email body text, nothing else.
Keep the same general message but apply the requested changes.
"""
        try:
            refined_body = await llm.complete(refine_prompt, max_tokens=1500)
            refined_body = refined_body.strip()

            # Store the refinement and increment revision count
            update_query = """
            MATCH (d:EmailDraft {id: $draft_id})
            SET d.body = $new_body,
                d.revision_count = $revision_count,
                d.last_refined_at = datetime(),
                d.refinement_history = COALESCE(d.refinement_history, []) + [$refinement]
            RETURN d.id as id, d.to as to, d.subject as subject, d.body as body,
                   d.revision_count as revision_count
            """
            update_result = await session.run(update_query, {
                "draft_id": draft_id,
                "new_body": refined_body,
                "revision_count": revision_count,
                "refinement": f"[{refinement_type}] {refinement_instruction[:100]}",
            })
            updated = await update_result.single()

            # Return updated draft card
            safe_to = html.escape(str(updated['to'] or ''))
            safe_subject = html.escape(str(updated['subject'] or ''))
            safe_body = html.escape(str(updated['body'] or ''))
            revision_badge = f'<span class="badge bg-info" style="margin-left: 0.5rem;">v{revision_count}</span>' if revision_count > 1 else ''

            return HTMLResponse(f'''
                <div id="draft-{draft_id}" class="draft-card" style="border: 2px solid #0ea5e9;">
                    <div class="draft-meta">
                        <strong>To:</strong> {safe_to} {revision_badge}<br>
                        <strong>Subject:</strong> {safe_subject}
                    </div>
                    <div class="draft-body" style="background: #f0f9ff; padding: 0.75rem; border-radius: 4px; margin: 0.5rem 0;">
                        <pre style="white-space: pre-wrap; margin: 0; font-family: inherit;">{safe_body}</pre>
                    </div>
                    <div class="draft-actions" style="margin-top: 0.5rem;">
                        <button class="btn btn-success btn-sm"
                            hx-post="/api/twin/drafts/{draft_id}/approve"
                            hx-target="#draft-{draft_id}"
                            hx-swap="outerHTML">
                            Send Email
                        </button>
                        <button class="btn btn-outline-primary btn-sm"
                            hx-get="/api/twin/drafts/{draft_id}/edit"
                            hx-target="#draft-{draft_id}"
                            hx-swap="innerHTML">
                            Edit
                        </button>
                        <button class="btn btn-outline-secondary btn-sm"
                            hx-get="/api/twin/drafts/{draft_id}/refine-form"
                            hx-target="#draft-{draft_id}"
                            hx-swap="beforeend">
                            Refine Again
                        </button>
                        <button class="btn btn-outline-danger btn-sm"
                            hx-get="/api/twin/drafts/{draft_id}/reject-form"
                            hx-target="#draft-{draft_id}"
                            hx-swap="beforeend">
                            Reject
                        </button>
                    </div>
                    <p class="text-success" style="font-size: 0.85rem; margin-top: 0.5rem;">
                        Draft refined! ({refinement_type})
                    </p>
                </div>
            ''')

        except Exception as e:
            logger.error("Draft refinement failed", error=str(e), draft_id=draft_id)
            return HTMLResponse(f'''
                <div id="draft-{draft_id}" class="draft-card" style="border: 2px solid #ef4444;">
                    <p class="text-danger"><strong>Refinement failed:</strong> {html.escape(str(e))}</p>
                    <button class="btn btn-link btn-sm" hx-get="/twin" hx-target="body">Reload</button>
                </div>
            ''')

    raise HTTPException(status_code=500, detail="Failed to refine draft")


@app.get("/api/twin/drafts/{draft_id}/context")
async def get_draft_deep_context(draft_id: str):
    """Build a context pack for the email being replied to.

    Uses LLM to analyze the email, extract requirements and deliverables,
    then searches the graph for relevant documents, tasks, and context.
    """
    import json
    from cognitex.db.neo4j import get_neo4j_session
    from cognitex.agent.graph_observer import GraphObserver
    from cognitex.services.llm import LLMService

    async for session in get_neo4j_session():
        # Get the original email's gmail_id
        query = """
        MATCH (d:EmailDraft {id: $draft_id})-[:REPLY_TO]->(e:Email)
        RETURN e.gmail_id as gmail_id, e.subject as subject
        """
        result = await session.run(query, {"draft_id": draft_id})
        data = await result.single()

        if not data:
            raise HTTPException(status_code=404, detail="Draft not found")

        gmail_id = data.get("gmail_id")
        if not gmail_id:
            return HTMLResponse("""
                <div class="context-panel" style="background: #fef3c7; padding: 1rem; margin: 1rem 0; border-radius: 5px;">
                    <strong>No original email found</strong>
                    <p>Unable to build context pack - original email reference missing.</p>
                </div>
            """)

        # Get deep context (full body, thread history, etc.)
        observer = GraphObserver(session)
        context = await observer.get_email_deep_context(gmail_id)

        # If we don't have the email body, we can't analyze it
        if not context.get("full_body"):
            return HTMLResponse("""
                <div class="context-panel" style="background: #fef3c7; padding: 1rem; margin: 1rem 0; border-radius: 5px;">
                    <strong>Email content not available</strong>
                    <p>Could not retrieve full email body for analysis.</p>
                </div>
            """)

        # Use LLM to analyze the email and extract requirements
        try:
            llm = LLMService()
            analysis_prompt = f"""Analyze this email and extract what is being requested or needed for a response.

EMAIL SUBJECT: {data.get("subject", "No subject")}

EMAIL BODY:
{context["full_body"][:4000]}

SENDER: {context.get("sender_name", "Unknown")} ({context.get("sender_email", "unknown")})

Analyze and return a JSON object with:
{{
    "summary": "1-2 sentence summary of what this email is about",
    "deliverables": ["list of specific things being asked for or needed"],
    "key_requirements": ["specific requirements, constraints, or instructions mentioned"],
    "deadlines": ["any dates or deadlines mentioned"],
    "questions_to_answer": ["specific questions asked that need answers"],
    "stakeholders_mentioned": ["names or roles of people mentioned"],
    "topics_for_search": ["2-3 key topics/keywords to search for related documents"],
    "suggested_response_points": ["key points to address in the response"]
}}

Return ONLY valid JSON, no other text."""

            analysis_response = await llm.complete(
                analysis_prompt,
                temperature=0.2,
            )

            # Parse the analysis
            analysis_text = analysis_response.strip()
            if analysis_text.startswith("```"):
                analysis_text = analysis_text.split("\n", 1)[1]
                analysis_text = analysis_text.rsplit("```", 1)[0]

            analysis = json.loads(analysis_text)

        except Exception as e:
            logger.warning("LLM analysis failed", error=str(e))
            analysis = {
                "summary": "Could not analyze email",
                "deliverables": [],
                "key_requirements": [],
                "deadlines": [],
                "questions_to_answer": [],
                "stakeholders_mentioned": [],
                "topics_for_search": [],
                "suggested_response_points": [],
            }

        # Search for related documents based on the analysis
        related_docs = []
        if analysis.get("topics_for_search"):
            try:
                from cognitex.db.postgres import get_session
                from cognitex.services.ingestion import search_chunks_semantic

                search_query = " ".join(analysis["topics_for_search"][:3])
                async for pg_session in get_session():
                    chunks = await search_chunks_semantic(pg_session, search_query, limit=5)
                    break

                for chunk in chunks:
                    drive_id = chunk.get("drive_id")
                    if drive_id:
                        doc_query = """
                        MATCH (d:Document {drive_id: $drive_id})
                        RETURN d.name as name, d.drive_id as drive_id, d.summary as summary
                        """
                        doc_result = await session.run(doc_query, {"drive_id": drive_id})
                        doc_data = await doc_result.single()
                        if doc_data:
                            related_docs.append({
                                **dict(doc_data),
                                "relevance": chunk.get("similarity", 0.5),
                                "matched_content": chunk.get("content", "")[:200],
                            })
            except Exception as e:
                logger.warning("Document search failed", error=str(e))

        # Build HTML response - structured context pack
        def escape_html(text):
            if not text:
                return ""
            return str(text).replace("<", "&lt;").replace(">", "&gt;")

        html_parts = [
            '<div class="context-panel" style="background: #f0f9ff; padding: 1rem; margin: 1rem 0; border-radius: 5px; max-height: 80vh; overflow-y: auto;">',
            f'<h4 style="margin-top: 0; border-bottom: 1px solid #bfdbfe; padding-bottom: 0.5rem;">Context Pack: {escape_html(data.get("subject", "Email")[:50])}</h4>',
        ]

        # Summary
        if analysis.get("summary"):
            html_parts.append('<div class="context-section" style="margin-bottom: 1rem; background: #dbeafe; padding: 0.75rem; border-radius: 4px;">')
            html_parts.append(f'<strong>Summary:</strong> {escape_html(analysis["summary"])}')
            html_parts.append('</div>')

        # Deliverables - what's being asked for
        if analysis.get("deliverables"):
            html_parts.append('<div class="context-section" style="margin-bottom: 1rem;">')
            html_parts.append('<strong style="color: #1e40af;">Deliverables Requested:</strong>')
            html_parts.append('<ul style="margin: 0.25rem 0; padding-left: 1.25rem;">')
            for item in analysis["deliverables"]:
                html_parts.append(f'<li style="font-size: 0.9rem; margin-bottom: 0.25rem;">{escape_html(item)}</li>')
            html_parts.append('</ul></div>')

        # Key requirements/instructions
        if analysis.get("key_requirements"):
            html_parts.append('<div class="context-section" style="margin-bottom: 1rem;">')
            html_parts.append('<strong style="color: #1e40af;">Key Requirements:</strong>')
            html_parts.append('<ul style="margin: 0.25rem 0; padding-left: 1.25rem;">')
            for item in analysis["key_requirements"]:
                html_parts.append(f'<li style="font-size: 0.9rem; margin-bottom: 0.25rem;">{escape_html(item)}</li>')
            html_parts.append('</ul></div>')

        # Questions to answer
        if analysis.get("questions_to_answer"):
            html_parts.append('<div class="context-section" style="margin-bottom: 1rem;">')
            html_parts.append('<strong style="color: #1e40af;">Questions to Answer:</strong>')
            html_parts.append('<ul style="margin: 0.25rem 0; padding-left: 1.25rem;">')
            for item in analysis["questions_to_answer"]:
                html_parts.append(f'<li style="font-size: 0.9rem; margin-bottom: 0.25rem;">{escape_html(item)}</li>')
            html_parts.append('</ul></div>')

        # Deadlines
        if analysis.get("deadlines"):
            html_parts.append('<div class="context-section" style="margin-bottom: 1rem; background: #fef3c7; padding: 0.5rem; border-radius: 4px;">')
            html_parts.append('<strong style="color: #92400e;">Deadlines:</strong> ')
            html_parts.append(escape_html(", ".join(analysis["deadlines"])))
            html_parts.append('</div>')

        # Suggested response points
        if analysis.get("suggested_response_points"):
            html_parts.append('<div class="context-section" style="margin-bottom: 1rem;">')
            html_parts.append('<strong style="color: #047857;">Suggested Response Points:</strong>')
            html_parts.append('<ul style="margin: 0.25rem 0; padding-left: 1.25rem;">')
            for item in analysis["suggested_response_points"]:
                html_parts.append(f'<li style="font-size: 0.9rem; margin-bottom: 0.25rem;">{escape_html(item)}</li>')
            html_parts.append('</ul></div>')

        # Related documents found
        if related_docs:
            html_parts.append('<div class="context-section" style="margin-bottom: 1rem; border-top: 1px solid #bfdbfe; padding-top: 0.75rem;">')
            html_parts.append(f'<strong>Related Documents ({len(related_docs)}):</strong>')
            html_parts.append('<ul style="margin: 0.25rem 0; padding-left: 1.25rem;">')
            for doc in related_docs[:5]:
                name = escape_html(doc.get("name", "Document"))
                drive_id = doc.get("drive_id", "")
                relevance = doc.get("relevance", 0)
                summary = escape_html((doc.get("summary") or doc.get("matched_content") or "")[:100])
                html_parts.append(f'<li style="font-size: 0.9rem; margin-bottom: 0.5rem;">')
                html_parts.append(f'<a href="https://drive.google.com/file/d/{drive_id}/view" target="_blank">{name}</a>')
                html_parts.append(f' <span style="color: #6b7280;">({relevance:.0%})</span>')
                if summary:
                    html_parts.append(f'<br><span style="font-size: 0.8rem; color: #6b7280;">{summary}...</span>')
                html_parts.append('</li>')
            html_parts.append('</ul></div>')

        # Related tasks from graph
        if context.get("related_tasks"):
            html_parts.append('<div class="context-section" style="margin-bottom: 1rem;">')
            html_parts.append(f'<strong>Related Tasks ({len(context["related_tasks"])}):</strong>')
            html_parts.append('<ul style="margin: 0.25rem 0; padding-left: 1.25rem;">')
            for task in context["related_tasks"]:
                title = escape_html(task.get("title", "Task"))
                status = task.get("status", "unknown")
                project = task.get("project_title", "")
                status_color = "#047857" if status == "done" else "#d97706" if status == "in_progress" else "#6b7280"
                html_parts.append(f'<li style="font-size: 0.9rem;">{title} <span style="color: {status_color};">[{status}]</span>{" - " + escape_html(project) if project else ""}</li>')
            html_parts.append('</ul></div>')

        # Sender context
        if context.get("sender_context"):
            sc = context["sender_context"]
            html_parts.append('<div class="context-section" style="margin-bottom: 0.5rem; font-size: 0.85rem; color: #6b7280;">')
            html_parts.append(f'<strong>Sender:</strong> {escape_html(sc.get("name") or "Unknown")} ({escape_html(sc.get("org") or "Unknown org")})')
            html_parts.append(f' - {sc.get("email_count", 0)} prior emails, {sc.get("shared_task_count", 0)} shared tasks')
            html_parts.append('</div>')

        # Action buttons - Research and Close
        html_parts.append('<div style="margin-top: 1rem; display: flex; gap: 0.5rem; align-items: center;">')
        html_parts.append(f'''<button class="btn btn-primary btn-sm" id="research-btn-{draft_id}"
            onclick="startResearch('{draft_id}')"
            title="Search internal docs and web for information to address requirements">
            🔍 Research (Internal + Web)
        </button>''')
        html_parts.append('<button class="btn btn-secondary btn-sm" onclick="this.parentElement.parentElement.remove()">Close</button>')
        html_parts.append('</div>')
        html_parts.append(f'<div id="research-status-{draft_id}" style="margin-top: 0.5rem;"></div>')
        html_parts.append(f'<div id="research-results-{draft_id}" style="margin-top: 1rem;"></div>')

        # Add the SSE JavaScript for this draft
        html_parts.append(f'''
        <script>
        function startResearch(draftId) {{
            const btn = document.getElementById('research-btn-' + draftId);
            const statusDiv = document.getElementById('research-status-' + draftId);
            const resultsDiv = document.getElementById('research-results-' + draftId);

            // Disable button and show initial status
            btn.disabled = true;
            btn.innerHTML = '🔄 Researching...';
            statusDiv.innerHTML = '<div style="padding: 0.5rem; background: #f0f9ff; border-radius: 4px;"><span style="margin-right: 0.5rem;">🚀</span>Connecting...</div>';
            resultsDiv.innerHTML = '';

            // Start SSE connection
            const eventSource = new EventSource('/api/twin/drafts/' + draftId + '/research/stream');

            eventSource.addEventListener('status', function(e) {{
                // Unescape newlines from SSE data
                statusDiv.innerHTML = e.data.replace(/\\\\n/g, '\\n');
            }});

            eventSource.addEventListener('result', function(e) {{
                statusDiv.innerHTML = '';  // Clear status
                // Unescape newlines and render HTML
                resultsDiv.innerHTML = e.data.replace(/\\\\n/g, '\\n');
            }});

            eventSource.addEventListener('error', function(e) {{
                if (e.data) {{
                    statusDiv.innerHTML = '';
                    resultsDiv.innerHTML = e.data.replace(/\\\\n/g, '\\n');
                }}
            }});

            eventSource.addEventListener('done', function(e) {{
                eventSource.close();
                btn.disabled = false;
                btn.innerHTML = '🔍 Research (Internal + Web)';
                if (e.data === 'error') {{
                    statusDiv.innerHTML = '';
                }}
            }});

            eventSource.onerror = function(e) {{
                eventSource.close();
                btn.disabled = false;
                btn.innerHTML = '🔍 Research (Internal + Web)';
                if (!resultsDiv.innerHTML) {{
                    statusDiv.innerHTML = '<div class="alert alert-error">Connection error. Please try again.</div>';
                }}
            }};
        }}
        </script>
        ''')
        html_parts.append('</div>')

        return HTMLResponse("".join(html_parts))

    raise HTTPException(status_code=500, detail="Failed to build context pack")


@app.get("/api/twin/drafts/{draft_id}/research/stream")
async def research_for_draft_stream(draft_id: str):
    """
    Deep research for email response with SSE streaming progress.

    Streams progress updates to prevent timeout and provide real-time feedback.
    """
    import json
    import asyncio
    import re
    from fastapi.responses import StreamingResponse
    from cognitex.db.neo4j import get_neo4j_session
    from cognitex.agent.graph_observer import GraphObserver
    from cognitex.services.llm import get_llm_service
    from cognitex.db.postgres import get_session
    from cognitex.services.ingestion import search_chunks_semantic

    # Helper functions for SSE
    def sse_event(event_type: str, data: str) -> str:
        """Format as SSE event."""
        # SSE data lines can't have newlines - replace with escaped version
        safe_data = data.replace('\n', '\\n').replace('\r', '')
        return f"event: {event_type}\ndata: {safe_data}\n\n"

    def status_html(message: str, progress: int = 0, icon: str = "🔄") -> str:
        """Generate status update HTML."""
        return f'<div class="research-status" style="padding: 0.5rem; background: #f0f9ff; border-radius: 4px; margin-bottom: 0.5rem;"><span style="margin-right: 0.5rem;">{icon}</span>{message}<span style="float:right; color: #6b7280;">{progress}%</span></div>'

    # First, gather all the initial data we need OUTSIDE the generator
    # This prevents holding async context managers open during streaming
    email_data = None
    email_body = None
    subject = None

    try:
        async for session in get_neo4j_session():
            query = """
            MATCH (d:EmailDraft {id: $draft_id})-[:REPLY_TO]->(e:Email)
            RETURN e.gmail_id as gmail_id, e.subject as subject
            """
            result = await session.run(query, {"draft_id": draft_id})
            email_data = await result.single()

            if email_data:
                gmail_id = email_data.get("gmail_id")
                subject = email_data.get("subject", "Email")
                observer = GraphObserver(session)
                context = await observer.get_email_deep_context(gmail_id)
                email_body = context.get("full_body", "")
            break
    except Exception as e:
        logger.error("Failed to get email data", error=str(e))

    async def generate_research_stream():
        """Generator that yields SSE events as research progresses."""
        nonlocal email_data, email_body, subject

        try:
            yield sse_event("status", status_html("Starting research...", 5, "🚀"))

            if not email_data:
                yield sse_event("error", '<div class="alert alert-error">Draft not found</div>')
                yield sse_event("done", "error")
                return

            if not email_body:
                yield sse_event("error", '<div class="alert alert-warning">No email content to research</div>')
                yield sse_event("done", "error")
                return

            yield sse_event("status", status_html(f"Analyzing: {subject[:40]}...", 10, "📧"))

            llm = get_llm_service()

            yield sse_event("status", status_html("Extracting research queries...", 15, "🧠"))

            # Step 1: Extract research queries from the email
            extraction_prompt = f"""Analyze this email and extract specific research queries.

EMAIL SUBJECT: {subject}
EMAIL BODY:
{email_body[:6000]}

Generate research queries for both internal documents and web search.
Return JSON:
{{
    "internal_queries": [
        {{"query": "semantic search query for internal docs", "purpose": "what we're looking for"}}
    ],
    "web_queries": [
        {{"query": "web search query", "purpose": "what external info we need"}}
    ],
    "key_questions": ["specific questions that need answering"],
    "context_needed": "brief description of what background is needed to respond"
}}

Focus on:
- Technical details or specifications mentioned
- Industry/domain context needed
- Standards, regulations, or best practices
- Competitor or market information
- Historical context or prior work

Return ONLY valid JSON."""

            try:
                extraction_response = await llm.complete(extraction_prompt, temperature=0.2)
                extraction_text = extraction_response.strip()
                if extraction_text.startswith("```"):
                    extraction_text = extraction_text.split("\n", 1)[1].rsplit("```", 1)[0]
                research_plan = json.loads(extraction_text)
                yield sse_event("status", status_html("Research plan ready", 25, "✅"))
            except Exception as e:
                logger.warning("Failed to extract research plan", error=str(e))
                research_plan = {
                    "internal_queries": [{"query": subject, "purpose": "General search"}],
                    "web_queries": [{"query": subject, "purpose": "Background info"}],
                    "key_questions": [],
                    "context_needed": "General background"
                }
                yield sse_event("status", status_html("Using fallback plan", 25, "⚠️"))

            internal_queries = research_plan.get("internal_queries", [])[:3]  # Reduced to 3 for speed
            web_queries = research_plan.get("web_queries", [])[:3]

            yield sse_event("status", status_html(f"{len(internal_queries)} internal + {len(web_queries)} web queries", 30, "📋"))

            # Define search functions that don't hold connections
            async def search_internal_docs(query_item: dict) -> list[dict]:
                """Search internal documents for a single query."""
                results = []
                query_text = query_item.get("query", "")
                if not query_text:
                    return results
                try:
                    async for pg_session in get_session():
                        chunks = await search_chunks_semantic(pg_session, query_text, limit=3)
                        for chunk in chunks:
                            results.append({
                                "query": query_text,
                                "purpose": query_item.get("purpose", ""),
                                "content": chunk.get("content", "")[:500],
                                "drive_id": chunk.get("drive_id"),
                                "similarity": chunk.get("similarity", 0),
                            })
                        break
                except Exception as e:
                    logger.warning("Internal search failed", query=query_text, error=str(e))
                return results

            async def research_web(query_item: dict) -> dict | None:
                """Research a single web query using LLM knowledge."""
                query_text = query_item.get("query", "")
                if not query_text:
                    return None
                try:
                    # Shorter prompt for faster response
                    web_prompt = f"""Brief info about: {query_text}
Purpose: {query_item.get("purpose", "Background")}
Give 3-5 bullet points of key facts. Be concise."""

                    web_response = await llm.complete(web_prompt, temperature=0.3, max_tokens=1024)
                    return {
                        "query": query_text,
                        "purpose": query_item.get("purpose", ""),
                        "findings": web_response.strip()
                    }
                except Exception as e:
                    logger.warning("Web research failed", query=query_text, error=str(e))
                    return None

            yield sse_event("status", status_html("Searching internal docs...", 35, "📁"))

            # Run internal searches (usually fast)
            internal_results = []
            for i, q in enumerate(internal_queries):
                try:
                    results = await asyncio.wait_for(search_internal_docs(q), timeout=10.0)
                    internal_results.extend(results)
                    yield sse_event("status", status_html(f"Internal {i+1}/{len(internal_queries)}", 35 + (i+1)*5, "📁"))
                except asyncio.TimeoutError:
                    logger.warning("Internal search timeout", query=q.get("query"))

            yield sse_event("status", status_html("External research...", 50, "🌐"))

            # Run web research with heartbeats
            web_results = []
            for i, q in enumerate(web_queries):
                query_text = q.get("query", "")[:30]
                yield sse_event("status", status_html(f"Researching: {query_text}...", 50 + i*10, "🌐"))

                # Run with heartbeat
                web_task = asyncio.create_task(research_web(q))
                hb = 0
                while not web_task.done():
                    try:
                        await asyncio.wait_for(asyncio.shield(web_task), timeout=5.0)
                    except asyncio.TimeoutError:
                        hb += 1
                        yield f": hb\n\n"
                        if hb > 4:  # 20 second timeout per query
                            web_task.cancel()
                            break

                try:
                    result = web_task.result() if web_task.done() and not web_task.cancelled() else None
                    if result:
                        web_results.append(result)
                except Exception:
                    pass

                yield sse_event("status", status_html(f"Web {i+1}/{len(web_queries)} done", 55 + (i+1)*10, "🌐"))

            yield sse_event("status", status_html(f"Found {len(internal_results)} docs, {len(web_results)} web results", 85, "✅"))

            logger.info("Research complete",
                       internal_results=len(internal_results),
                       web_results=len(web_results))

            # Step 4: Compile research into a summary with heartbeat to keep connection alive
            yield sse_event("status", status_html("Compiling briefing...", 90, "📝"))

            # Simplified compile prompt for faster response
            compile_prompt = f"""Summarize this research for an email response.

EMAIL: {subject}

INTERNAL DOCS: {len(internal_results)} found
{json.dumps([r.get('content', '')[:200] for r in internal_results[:5]], indent=1) if internal_results else "None"}

EXTERNAL INFO: {len(web_results)} queries
{json.dumps([{'q': r.get('query', ''), 'findings': r.get('findings', '')[:300]} for r in web_results[:3]], indent=1) if web_results else "None"}

Provide a brief summary (2-3 paragraphs) with:
- Key findings relevant to the email
- Recommended response approach
- Any information gaps

Be concise."""

            # Run compile with heartbeat task to keep SSE alive
            compiled_brief = None
            compile_task = asyncio.create_task(
                llm.complete(compile_prompt, temperature=0.3, max_tokens=2048)
            )

            heartbeat_count = 0
            while not compile_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(compile_task), timeout=5.0)
                except asyncio.TimeoutError:
                    # Send heartbeat to keep connection alive
                    heartbeat_count += 1
                    yield f": heartbeat {heartbeat_count}\n\n"
                    yield sse_event("status", status_html(f"Compiling briefing... ({heartbeat_count * 5}s)", 90, "📝"))

            try:
                compiled_brief = compile_task.result().strip()
            except Exception as e:
                logger.warning("Failed to compile research", error=str(e))
                compiled_brief = f"Research completed but summary failed: {str(e)}\n\nRaw findings: {len(internal_results)} internal docs, {len(web_results)} web results."

            yield sse_event("status", status_html("Formatting...", 95, "✨"))

            # Convert markdown to basic HTML
            brief_html = compiled_brief
            brief_html = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', brief_html)
            brief_html = re.sub(r'^# (.+)$', r'<h3>\1</h3>', brief_html, flags=re.MULTILINE)
            brief_html = re.sub(r'^## (.+)$', r'<h4>\1</h4>', brief_html, flags=re.MULTILINE)
            brief_html = re.sub(r'^- (.+)$', r'<li>\1</li>', brief_html, flags=re.MULTILINE)
            brief_html = re.sub(r'^(\d+)\. (.+)$', r'<li>\2</li>', brief_html, flags=re.MULTILINE)
            brief_html = brief_html.replace('\n\n', '</p><p>').replace('\n', '<br>')
            brief_html = f'<p>{brief_html}</p>'

            final_html = f'''<div class="research-results" style="background: #f0fdf4; padding: 1rem; border-radius: 5px; border: 1px solid #86efac;"><h4 style="margin-top: 0; color: #166534; border-bottom: 1px solid #86efac; padding-bottom: 0.5rem;">📚 Research Complete</h4><div style="margin-bottom: 1rem;"><strong>Queries Executed:</strong> <span style="color: #6b7280;">{len(internal_results)} internal, {len(web_results)} web</span></div><div class="research-brief" style="background: white; padding: 1rem; border-radius: 4px; font-size: 0.9rem; line-height: 1.6;">{brief_html}</div><div style="margin-top: 1rem; display: flex; gap: 0.5rem;"><button class="btn btn-success btn-sm" onclick="navigator.clipboard.writeText(this.parentElement.previousElementSibling.innerText); this.innerText=\\'Copied!\\';">📋 Copy</button><button class="btn btn-secondary btn-sm" onclick="this.parentElement.parentElement.remove();">Dismiss</button></div></div>'''

            yield sse_event("result", final_html)
            yield sse_event("done", "success")

        except Exception as e:
            logger.error("Research stream error", error=str(e), exc_info=True)
            yield sse_event("error", f'<div class="alert alert-error">Research failed: {str(e)}</div>')
            yield sse_event("done", "error")

    return StreamingResponse(
        generate_research_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )


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


@app.post("/api/twin/emails/{gmail_id}/research")
async def trigger_research_pack(
    gmail_id: str,
    topics: Annotated[str | None, Form()] = None,
):
    """Manually trigger research pack compilation for an email.

    If topics is provided, uses those. Otherwise, uses the email's
    stored research_topics from classification.
    """
    from cognitex.db.neo4j import get_neo4j_session
    from cognitex.agent.context_pack import get_context_pack_compiler

    async for session in get_neo4j_session():
        # Get email details and stored research topics
        query = """
        MATCH (e:Email {gmail_id: $gmail_id})
        RETURN e.gmail_id as gmail_id, e.subject as subject,
               e.snippet as snippet, e.research_topics as research_topics
        """
        result = await session.run(query, {"gmail_id": gmail_id})
        record = await result.single()

        if not record:
            raise HTTPException(status_code=404, detail="Email not found")

        email = dict(record)

        # Determine topics to research
        if topics:
            # User-provided topics (comma-separated)
            research_topics = [t.strip() for t in topics.split(",") if t.strip()]
        else:
            # Use stored topics from classification
            research_topics = email.get("research_topics") or []

        if not research_topics:
            return HTMLResponse('''
                <div class="alert alert-warning" style="padding: 0.5rem; margin-top: 0.5rem;">
                    No research topics identified. Please specify topics or classify the email first.
                </div>
            ''')

        # Compile research pack
        compiler = get_context_pack_compiler()
        pack = await compiler.compile_research_pack(email, research_topics)

        # Return success message
        return HTMLResponse(f'''
            <div class="alert alert-success" style="padding: 0.5rem; margin-top: 0.5rem; background: #ecfdf5; border: 1px solid #a7f3d0; border-radius: 4px;">
                <strong>Research pack created!</strong>
                <br>Researched {len(research_topics)} topic(s): {", ".join(research_topics[:3])}
                <br>Found {len(pack.artifact_links)} related documents.
                <br><a href="/twin" style="color: #047857;">View in Digital Twin &rarr;</a>
            </div>
        ''')

    raise HTTPException(status_code=500, detail="Failed to create research pack")


# ========================================================================
# Phase 5.2: Intelligent Calendar Blocking
# ========================================================================

@app.post("/api/twin/blocks/generate")
async def generate_focus_blocks():
    """Generate intelligent focus block suggestions based on tasks and calendar.

    Uses GraphObserver to analyze:
    - High-energy tasks needing focus time
    - Upcoming deadlines
    - User's energy patterns (peak hours)
    - Calendar availability
    """
    from cognitex.db.neo4j import get_neo4j_session
    from cognitex.agent.graph_observer import GraphObserver
    import uuid

    try:
        async for session in get_neo4j_session():
            observer = GraphObserver(session)
            suggestions = await observer.get_focus_block_suggestions(max_suggestions=3)

            if not suggestions:
                return HTMLResponse('''
                    <div class="alert alert-info" style="margin: 1rem 0;">
                        No focus blocks needed right now. All tasks look manageable!
                    </div>
                ''')

            # Create SuggestedBlock nodes for each suggestion
            created_blocks = []
            for suggestion in suggestions:
                block_id = f"block_{uuid.uuid4().hex[:12]}"

                # Parse suggested time to determine day
                suggested_time = suggestion.get("suggested_time", "")
                from datetime import datetime
                try:
                    dt = datetime.fromisoformat(suggested_time)
                    today = datetime.now().date()
                    if dt.date() == today:
                        suggested_day = "today"
                    elif (dt.date() - today).days == 1:
                        suggested_day = "tomorrow"
                    elif (dt.date() - today).days <= 7:
                        suggested_day = dt.strftime("%A")  # Day name
                    else:
                        suggested_day = "next week"
                except Exception:
                    suggested_day = "tomorrow"

                # Create the SuggestedBlock node
                create_query = """
                CREATE (sb:SuggestedBlock {
                    id: $block_id,
                    title: $title,
                    duration_hours: $duration_hours,
                    suggested_day: $suggested_day,
                    suggested_time: $suggested_time,
                    status: 'pending_approval',
                    created_at: datetime(),
                    created_by: 'intelligent_blocking',
                    reason: $reason
                })
                WITH sb
                OPTIONAL MATCH (p:Project {id: $project_id})
                FOREACH (_ IN CASE WHEN p IS NOT NULL THEN [1] ELSE [] END |
                    CREATE (sb)-[:FOR_PROJECT]->(p)
                )
                WITH sb
                OPTIONAL MATCH (t:Task {id: $task_id})
                FOREACH (_ IN CASE WHEN t IS NOT NULL THEN [1] ELSE [] END |
                    CREATE (sb)-[:FOR_TASK]->(t)
                )
                RETURN sb.id as id
                """
                await session.run(create_query, {
                    "block_id": block_id,
                    "title": f"Focus: {suggestion.get('task_title', 'Deep work')[:40]}",
                    "duration_hours": suggestion.get("duration_hours", 2),
                    "suggested_day": suggested_day,
                    "suggested_time": suggested_time,
                    "reason": suggestion.get("reason", "Task needs focused attention"),
                    "project_id": suggestion.get("project_id", ""),
                    "task_id": suggestion.get("task_id", ""),
                })
                created_blocks.append({
                    "id": block_id,
                    **suggestion,
                    "suggested_day": suggested_day,
                })

            # Return HTML for the new blocks
            blocks_html = []
            for block in created_blocks:
                blocks_html.append(f'''
                    <tr id="block-row-{block['id']}" style="background: #f0fdf4;">
                        <td><strong>{html.escape(block.get('task_title', 'Focus Time')[:30])}</strong></td>
                        <td>{block.get('duration_hours', 2)}h</td>
                        <td>{html.escape(block.get('suggested_day', 'tomorrow'))}</td>
                        <td>{html.escape(block.get('project_title', '-')[:25] if block.get('project_title') else '-')}</td>
                        <td style="font-size: 0.85rem;">{html.escape(block.get('reason', '')[:50])}</td>
                        <td>
                            <button class="btn btn-success btn-sm"
                                hx-post="/api/twin/blocks/{block['id']}/approve"
                                hx-target="#block-row-{block['id']}"
                                hx-swap="outerHTML">
                                Add to Calendar
                            </button>
                            <button class="btn btn-outline-danger btn-sm"
                                hx-get="/api/twin/blocks/{block['id']}/reject-form"
                                hx-target="#block-row-{block['id']}"
                                hx-swap="outerHTML">
                                Dismiss
                            </button>
                        </td>
                    </tr>
                ''')

            return HTMLResponse(f'''
                <div class="alert alert-success" style="margin: 1rem 0;">
                    Generated {len(created_blocks)} focus block suggestions based on your tasks and calendar.
                </div>
                {''.join(blocks_html)}
            ''')

    except Exception as e:
        logger.error("Failed to generate focus blocks", error=str(e))
        return HTMLResponse(f'''
            <div class="alert alert-danger" style="margin: 1rem 0;">
                Failed to generate suggestions: {html.escape(str(e))}
            </div>
        ''')


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
                    <td colspan="6"><strong>Error:</strong> Failed to create calendar event: {html.escape(str(e)[:100])}</td>
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
                <td colspan="6"><strong>Added to calendar:</strong> {html.escape(block["title"])} ({duration_hours}h) on {start_date.strftime("%A %d %b at %H:%M")}</td>
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


@app.get("/api/twin/blocks/{block_id}/reject-form")
async def get_block_reject_form(block_id: str):
    """Get the rejection feedback form for a focus block."""
    # Quick-select rejection reasons
    reasons = [
        ("wrong_timing", "Wrong timing / not this day"),
        ("too_long", "Duration too long"),
        ("too_short", "Duration too short"),
        ("wrong_project", "Wrong project or focus area"),
        ("not_needed", "Don't need focus time for this"),
        ("other", "Other reason"),
    ]

    reason_buttons = "\n".join([
        f'''<button type="button" class="btn btn-outline-secondary btn-sm rejection-reason"
            data-reason="{code}"
            hx-post="/api/twin/blocks/{block_id}/reject"
            hx-vals='{{"reason": "{code}", "reason_text": "{label}"}}'
            hx-target="#block-row-{block_id}"
            hx-swap="outerHTML"
            style="margin: 0.25rem;">{html.escape(label)}</button>'''
        for code, label in reasons
    ])

    return HTMLResponse(f'''
        <tr id="block-row-{block_id}">
            <td colspan="6">
                <div class="rejection-form" style="background: #fef2f2; padding: 1rem; border-radius: 5px;">
                    <p style="margin-bottom: 0.5rem;"><strong>Why are you rejecting this suggestion?</strong></p>
                    <div class="rejection-reasons" style="display: flex; flex-wrap: wrap;">
                        {reason_buttons}
                    </div>
                    <button type="button" class="btn btn-link btn-sm"
                        hx-get="/twin"
                        hx-target="body"
                        style="margin-top: 0.5rem;">Cancel</button>
                </div>
            </td>
        </tr>
    ''')


@app.post("/api/twin/blocks/{block_id}/reject")
async def reject_block_with_feedback(
    block_id: str,
    reason: Annotated[str, Form()],
    reason_text: Annotated[str, Form()] = "",
):
    """Reject a focus block suggestion with feedback for learning."""
    from cognitex.db.neo4j import get_neo4j_session
    from cognitex.agent.decision_memory import get_decision_memory

    async for session in get_neo4j_session():
        # Update block status and store rejection reason
        query = """
        MATCH (sb:SuggestedBlock {id: $block_id})
        SET sb.status = 'rejected',
            sb.rejected_at = datetime(),
            sb.rejection_reason = $reason,
            sb.rejection_reason_text = $reason_text
        RETURN sb.id as id, sb.title as title, sb.reason as reasoning
        """
        result = await session.run(query, {
            "block_id": block_id,
            "reason": reason,
            "reason_text": reason_text,
        })
        data = await result.single()

        if not data:
            raise HTTPException(status_code=404, detail="Block not found")

        # Record feedback in decision memory for learning
        try:
            dm = get_decision_memory()
            await dm.traces.create_trace(
                trigger_type="focus_block_rejection",
                action_type="schedule_block",
                proposed_action={"block_id": block_id, "title": data.get("title")},
                trigger_id=block_id,
                trigger_summary=f"Focus block rejected: {reason_text or reason}",
                reasoning=data.get("reasoning", ""),
                metadata={"rejection_reason": reason, "rejection_reason_text": reason_text},
            )
            logger.info(
                "Block rejection recorded for learning",
                block_id=block_id,
                reason=reason,
            )
        except Exception as e:
            logger.warning("Failed to record rejection for learning", error=str(e))

        return HTMLResponse(f'''
            <tr id="block-row-{block_id}" style="background: #fef2f2; opacity: 0.7;">
                <td colspan="6">
                    <strong>Suggestion rejected</strong> - {html.escape(reason_text or reason)}
                    <span class="text-muted" style="font-size: 0.85rem; margin-left: 1rem;">Feedback recorded</span>
                </td>
            </tr>
        ''')


# -------------------------------------------------------------------
# State / Mode Management
# -------------------------------------------------------------------


@app.get("/state")
async def state_redirect():
    """Redirect to settings State tab."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/settings?tab=state", status_code=302)


@app.get("/state_DEPRECATED", response_class=HTMLResponse)
async def state_page_deprecated(request: Request):
    """Operating state and mode management page.
    DEPRECATED: Now part of unified Settings page.
    """
    estimator = get_state_estimator()

    # Infer fresh state based on current time and conditions
    # (uses temporal energy model for diurnal patterns)
    calendar_events = await get_today_events()
    state = await estimator.infer_state(calendar_events=calendar_events)

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


@app.post("/api/state/tool-override")
async def api_state_tool_override(
    reason: str = Form(None),
    duration_minutes: int = Form(5),
):
    """Enable temporary override of mode-based tool filtering.

    Allows the user to bypass tool filtering for urgent actions.
    Override expires after the specified duration.
    """
    from cognitex.agent.tool_filter import get_tool_filter
    from cognitex.config import get_settings

    settings = get_settings()
    if not settings.allow_tool_override:
        raise HTTPException(
            status_code=403,
            detail="Tool override is disabled in configuration",
        )

    tool_filter = get_tool_filter()
    tool_filter.set_override(active=True, reason=reason)

    # Store override in Redis with TTL
    redis = await get_redis()
    ttl_seconds = duration_minutes * 60
    await redis.set(
        "cognitex:tool_override",
        json.dumps({"reason": reason, "enabled_at": datetime.now().isoformat()}),
        ex=ttl_seconds,
    )

    logger.info(
        "Tool filter override enabled",
        reason=reason,
        duration_minutes=duration_minutes,
    )

    return {"status": "ok", "override_active": True, "expires_in_minutes": duration_minutes}


@app.delete("/api/state/tool-override")
async def api_state_tool_override_disable():
    """Disable the tool filter override."""
    from cognitex.agent.tool_filter import get_tool_filter

    tool_filter = get_tool_filter()
    tool_filter.set_override(active=False)

    # Remove from Redis
    redis = await get_redis()
    await redis.delete("cognitex:tool_override")

    logger.info("Tool filter override disabled")

    return {"status": "ok", "override_active": False}


# -------------------------------------------------------------------
# Focus Mode Dashboard
# -------------------------------------------------------------------


@app.get("/focus", response_class=HTMLResponse)
async def focus_dashboard(request: Request):
    """Focus mode dashboard - minimal view with state-eligible tasks only.

    Shows only tasks that match current energy/mode, hiding everything else
    for a distraction-free work session.
    """
    from cognitex.agent.state_model import get_state_estimator, ModeRules, get_temporal_model
    from cognitex.services.tasks import get_tasks

    estimator = get_state_estimator()
    temporal = get_temporal_model()
    calendar_events = await get_today_events()
    state = await estimator.infer_state(calendar_events=calendar_events)

    rules = ModeRules.get_rules(state.mode)
    max_friction = rules.get("max_task_friction", 5)

    # Get all pending tasks
    all_tasks = await get_tasks(status="pending", limit=100)

    # Filter to state-eligible tasks
    eligible_tasks = []
    for task in all_tasks:
        # Estimate friction from energy_cost
        energy_cost = task.get("energy_cost", 5)
        friction = min(5, max(1, (energy_cost or 3) // 2))  # Map 1-10 to 1-5

        if friction <= max_friction:
            eligible_tasks.append({
                **task,
                "friction": friction,
            })

    # Sort by priority, then due date
    eligible_tasks.sort(key=lambda t: (
        {"high": 0, "medium": 1, "low": 2}.get(t.get("priority", "medium"), 1),
        t.get("due_date") or "9999-12-31",
    ))

    # Limit to top 5 for focus
    focus_tasks = eligible_tasks[:5]

    # Get peak time info
    is_peak = temporal.is_peak_time()
    peak_hours = temporal.get_peak_hours()[:3]

    return templates.TemplateResponse(
        "focus.html",
        {
            "request": request,
            "state": state,
            "tasks": focus_tasks,
            "mode_name": state.mode.value,
            "max_friction": max_friction,
            "is_peak_time": is_peak,
            "peak_hours": peak_hours,
            "fatigue_percent": int(state.signals.fatigue_level * 100),
        },
    )


# -------------------------------------------------------------------
# Weekly Review Generator
# -------------------------------------------------------------------


@app.get("/review", response_class=HTMLResponse)
async def weekly_review_page(request: Request):
    """Weekly review page showing progress, patterns, and insights."""
    return templates.TemplateResponse(
        "review.html",
        {"request": request},
    )


@app.get("/api/review/generate", response_class=HTMLResponse)
async def api_generate_review():
    """Generate weekly review using LLM summarization."""
    from cognitex.services.llm import get_llm_service
    from cognitex.services.tasks import get_tasks
    from cognitex.agent.learning import get_state_observations_summary
    from cognitex.agent.action_log import get_action_summary
    from datetime import datetime, timedelta

    llm = get_llm_service()

    # Gather data from the past week
    week_ago = datetime.now() - timedelta(days=7)

    # Get completed tasks
    completed_tasks = await get_tasks(status="completed", limit=50)
    recent_completed = [
        t for t in completed_tasks
        if t.get("completed_at") and datetime.fromisoformat(str(t["completed_at"]).replace("Z", "")) > week_ago
    ]

    # Get state observations summary
    state_summary = await get_state_observations_summary(days=7)

    # Get action log summary
    action_summary = await get_action_summary(days=7)

    # Build review prompt
    prompt = f"""Generate a brief weekly review summary based on this data:

## Tasks Completed ({len(recent_completed)})
{chr(10).join(f"- {t.get('title', 'Untitled')}" for t in recent_completed[:15])}

## State Patterns
- Observations: {state_summary.get('total_observations', 0)}
- By mode: {state_summary.get('by_mode', {})}
- Post-clinical impact: {state_summary.get('post_clinical_impact', {})}

## Actions Taken
- Total: {action_summary.get('total_actions', 0)}
- By type: {action_summary.get('by_type', {})}

Generate a 3-paragraph review covering:
1. What was accomplished this week
2. Patterns observed in productivity/energy
3. Suggestions for next week

Keep it concise and actionable. Use markdown formatting.
"""

    try:
        review_text = await llm.complete(prompt, max_tokens=800)
    except Exception as e:
        review_text = f"Could not generate review: {str(e)}"

    return HTMLResponse(f"""
        <div class="review-content">
            <h3>Weekly Review - {datetime.now().strftime('%B %d, %Y')}</h3>
            <div class="review-body">
                {review_text}
            </div>
            <div class="review-stats">
                <span>Tasks completed: {len(recent_completed)}</span>
                <span>Observations: {state_summary.get('total_observations', 0)}</span>
            </div>
        </div>
    """)


# -------------------------------------------------------------------
# Voice Capture API
# -------------------------------------------------------------------


@app.post("/api/voice/transcribe")
async def api_voice_transcribe(request: Request):
    """Transcribe audio and add to inbox.

    Accepts audio file upload, transcribes using speech-to-text,
    and creates an inbox item for processing.
    """
    from cognitex.agent.interruption_firewall import get_interruption_firewall, IncomingItem, Urgency

    form = await request.form()
    audio_file = form.get("audio")

    if not audio_file:
        raise HTTPException(status_code=400, detail="No audio file provided")

    # Read audio content
    audio_content = await audio_file.read()

    # Transcribe using LLM service (if supported) or placeholder
    try:
        from cognitex.services.llm import get_llm_service
        llm = get_llm_service()

        # For now, use a simple placeholder since most LLM services
        # don't directly support audio transcription
        # In production, you'd use Whisper or similar
        transcription = "(Voice note - transcription pending)"

        # If using a service that supports audio:
        # transcription = await llm.transcribe_audio(audio_content)
    except Exception as e:
        transcription = f"(Could not transcribe: {str(e)})"

    # Add to inbox
    firewall = get_interruption_firewall()
    item = IncomingItem(
        source="voice",
        subject="Voice Note",
        preview=transcription[:200],
        urgency=Urgency.NORMAL,
        suggested_action="review",
        metadata={"transcription": transcription},
    )
    await firewall.capture_item(item)

    return {
        "status": "captured",
        "transcription": transcription[:200],
    }


# -------------------------------------------------------------------
# Quick Capture Widget
# -------------------------------------------------------------------


@app.post("/api/capture")
async def api_quick_capture(
    content: str = "",
    source: str = "widget",
    url: str = "",
    title: str = "",
):
    """Quick capture endpoint for browser extensions and widgets.

    Accepts text content, URLs, or both and adds to inbox for triage.
    """
    from cognitex.agent.interruption_firewall import get_interruption_firewall, IncomingItem, Urgency

    if not content and not url:
        raise HTTPException(status_code=400, detail="No content or URL provided")

    # Build preview
    if url and content:
        preview = f"{url}\n\n{content[:200]}"
        subject = title or url[:50]
    elif url:
        preview = url
        subject = title or "Captured URL"
    else:
        preview = content[:300]
        subject = title or content[:50]

    # Capture to inbox
    firewall = get_interruption_firewall()
    item = IncomingItem(
        source=source,
        subject=subject,
        preview=preview,
        urgency=Urgency.NORMAL,
        suggested_action="review",
        metadata={
            "url": url,
            "content": content,
            "title": title,
        },
    )
    await firewall.capture_item(item)

    return {
        "status": "captured",
        "subject": subject,
    }


@app.get("/api/capture/status")
async def api_capture_status():
    """Get capture status for widget sync."""
    from cognitex.agent.interruption_firewall import get_interruption_firewall

    firewall = get_interruption_firewall()
    items = await firewall.get_queued_items(limit=5)

    return {
        "inbox_count": len(items),
        "recent": [
            {"subject": i.subject, "source": i.source}
            for i in items[:3]
        ],
    }


# -------------------------------------------------------------------
# Learning System
# -------------------------------------------------------------------


# ========================================================================
# Proposals Management (redirects to unified inbox)
# ========================================================================

@app.get("/proposals")
async def proposals_redirect():
    """Redirect to unified inbox filtered to task proposals."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/inbox?type=task_proposal", status_code=302)


# Legacy proposals page routes kept for API compatibility
@app.get("/proposals-legacy", response_class=HTMLResponse)
async def proposals_page_legacy(request: Request):
    """Task proposals management page (legacy - use /inbox instead)."""
    from cognitex.db.postgres import get_session
    from cognitex.db.neo4j import get_neo4j_session
    from sqlalchemy import text

    proposals = []
    stats = {"pending": 0, "approved": 0, "rejected": 0}

    async for session in get_session():
        # Get all proposals with stats
        result = await session.execute(text("""
            SELECT
                id, title, description, project_id, goal_id,
                priority, reason, status, timestamp, decision_at, decision_reason
            FROM task_proposals
            ORDER BY
                CASE status WHEN 'pending' THEN 0 ELSE 1 END,
                timestamp DESC
            LIMIT 200
        """))
        rows = result.fetchall()

        for row in rows:
            proposals.append({
                "id": row.id,
                "title": row.title,
                "description": row.description,
                "project_id": row.project_id,
                "goal_id": row.goal_id,
                "priority": row.priority,
                "reason": row.reason,
                "status": row.status,
                "timestamp": row.timestamp,
                "decision_at": row.decision_at,
                "decision_reason": row.decision_reason,
            })

        # Get stats
        stats_result = await session.execute(text("""
            SELECT status, COUNT(*) as count
            FROM task_proposals
            GROUP BY status
        """))
        for row in stats_result.fetchall():
            stats[row.status] = row.count
        break

    # Get project names for display
    project_names = {}
    async for neo_session in get_neo4j_session():
        project_ids = list(set(p["project_id"] for p in proposals if p["project_id"]))
        if project_ids:
            result = await neo_session.run("""
                MATCH (p:Project)
                WHERE p.id IN $ids
                RETURN p.id as id, p.title as title
            """, {"ids": project_ids})
            records = await result.data()
            project_names = {r["id"]: r["title"] for r in records}
        break

    # Add project names to proposals
    for p in proposals:
        p["project_name"] = project_names.get(p["project_id"], p["project_id"])

    return templates.TemplateResponse(
        "proposals.html",
        {
            "request": request,
            "proposals": proposals,
            "stats": stats,
        }
    )


@app.post("/proposals/{proposal_id}/approve", response_class=HTMLResponse)
async def approve_proposal_web(proposal_id: str):
    """Approve a task proposal from the web UI."""
    from cognitex.agent.action_log import approve_proposal, log_action

    task = await approve_proposal(proposal_id)

    if task:
        await log_action(
            "proposal_approved",
            "web_ui",
            summary=f"Approved proposal: {task.get('title', '')[:50]}",
            details={"proposal_id": proposal_id, "task_id": task.get("id")}
        )

        # Self-improve expertise based on approved task proposal
        try:
            from cognitex.agent.expertise import get_expertise_manager
            em = get_expertise_manager()

            # Use project domain if available, otherwise general task creation
            project_id = task.get("project_id")
            domain = f"project:{project_id}" if project_id else "task_extraction"

            await em.self_improve(
                domain=domain,
                action_type="task_proposal_approved",
                action_result={
                    "task_id": task.get("id"),
                    "title": task.get("title"),
                    "priority": task.get("priority"),
                    "project_id": project_id,
                },
                context={"proposal_id": proposal_id},
            )
        except Exception as e:
            logger.debug("Expertise self-improve skipped", error=str(e))

        return HTMLResponse(f"""
            <tr id="proposal-{proposal_id}" class="proposal-row approved">
                <td colspan="6" style="text-align: center; color: var(--success); padding: 1rem;">
                    ✓ Approved - Task created
                </td>
            </tr>
        """)

    return HTMLResponse(f"""
        <tr id="proposal-{proposal_id}">
            <td colspan="6" style="color: var(--danger);">Failed to approve</td>
        </tr>
    """)


@app.post("/proposals/{proposal_id}/reject", response_class=HTMLResponse)
async def reject_proposal_web(
    proposal_id: str,
    reason: str = Form("not_needed"),
):
    """Reject a task proposal from the web UI."""
    from cognitex.agent.action_log import reject_proposal, log_action, learn_from_rejection
    from cognitex.db.postgres import get_session
    from sqlalchemy import text

    # Get proposal details before rejection
    proposal_title = ""
    async for session in get_session():
        result = await session.execute(text("""
            SELECT title, project_id, priority FROM task_proposals WHERE id = :id
        """), {"id": proposal_id})
        row = result.fetchone()
        if row:
            proposal_title = row.title
            # Record rejection for learning
            await learn_from_rejection(
                proposal_type="create_task",
                rejection_reason=f"{reason}: {proposal_title}",
                context={
                    "reason_category": reason,
                    "project_id": row.project_id,
                    "priority": row.priority,
                }
            )
        break

    success = await reject_proposal(proposal_id, reason)

    if success:
        await log_action(
            "proposal_rejected",
            "web_ui",
            summary=f"Rejected proposal: {proposal_title[:50]}",
            details={"proposal_id": proposal_id, "reason": reason}
        )
        return HTMLResponse(f"""
            <tr id="proposal-{proposal_id}" class="proposal-row rejected">
                <td colspan="6" style="text-align: center; color: var(--danger); padding: 1rem;">
                    ✗ Rejected ({reason})
                </td>
            </tr>
        """)

    return HTMLResponse(f"""
        <tr id="proposal-{proposal_id}">
            <td colspan="6" style="color: var(--danger);">Failed to reject</td>
        </tr>
    """)


@app.post("/proposals/bulk-reject", response_class=HTMLResponse)
async def bulk_reject_proposals(
    request: Request,
    reason: str = Form("duplicate"),
):
    """Bulk reject selected proposals."""
    from cognitex.agent.action_log import reject_proposal, log_action, learn_from_rejection
    from cognitex.db.postgres import get_session
    from sqlalchemy import text

    form_data = await request.form()
    proposal_ids = form_data.getlist("proposal_ids")

    rejected_count = 0
    for proposal_id in proposal_ids:
        # Get proposal details for learning
        async for session in get_session():
            result = await session.execute(text("""
                SELECT title, project_id, priority FROM task_proposals WHERE id = :id
            """), {"id": proposal_id})
            row = result.fetchone()
            if row:
                await learn_from_rejection(
                    proposal_type="create_task",
                    rejection_reason=f"{reason}: {row.title}",
                    context={
                        "reason_category": reason,
                        "project_id": row.project_id,
                        "priority": row.priority,
                    }
                )
            break

        if await reject_proposal(proposal_id, reason):
            rejected_count += 1

    await log_action(
        "proposals_bulk_rejected",
        "web_ui",
        summary=f"Bulk rejected {rejected_count} proposals",
        details={"count": rejected_count, "reason": reason}
    )

    # Redirect back to proposals page
    return HTMLResponse(
        content="",
        status_code=303,
        headers={"HX-Redirect": "/proposals"}
    )


# ========================================================================
# Agent Inbox
# ========================================================================

@app.get("/inbox", response_class=HTMLResponse)
async def inbox_page(request: Request, type: str | None = None):
    """Unified agent inbox page."""
    from cognitex.services.inbox import get_inbox_service

    inbox = get_inbox_service()
    counts = await inbox.get_pending_count()
    items = await inbox.get_pending_items(item_type=type)
    recent_decisions = await inbox.get_recent_decisions(limit=15)

    return templates.TemplateResponse(
        "inbox.html",
        {
            "request": request,
            "counts": counts,
            "items": items,
            "recent_decisions": recent_decisions,
            "filter_type": type,
        }
    )


# -------------------------------------------------------------------
# Real-time Notifications SSE Endpoint
# -------------------------------------------------------------------

@app.get("/api/notifications/stream")
async def notification_stream(request: Request):
    """SSE endpoint for real-time notifications.

    Clients connect here to receive notifications as they happen.
    This provides real-time updates matching what Discord receives.
    """
    from fastapi.responses import StreamingResponse

    # Create a queue for this client
    client_queue: Queue = Queue(maxsize=50)
    _notification_clients.add(client_queue)

    async def generate():
        try:
            # Send initial connection confirmation
            yield "event: connected\ndata: {\"status\": \"connected\"}\n\n"

            while True:
                try:
                    # Wait for notifications with timeout for keepalive
                    data = await asyncio.wait_for(client_queue.get(), timeout=30.0)
                    yield f"event: notification\ndata: {data}\n\n"
                except asyncio.TimeoutError:
                    # Send keepalive ping
                    yield "event: ping\ndata: {}\n\n"
                except asyncio.CancelledError:
                    break
        finally:
            _notification_clients.discard(client_queue)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )


@app.get("/api/inbox/count", response_class=HTMLResponse)
async def inbox_count():
    """Get pending inbox item counts (for badge polling)."""
    from cognitex.services.inbox import get_inbox_service

    inbox = get_inbox_service()
    counts = await inbox.get_pending_count()

    # Return badge HTML - show count if > 0, hide otherwise
    total = counts.get("total", 0)
    if total > 0:
        urgent = counts.get("urgent", 0)
        badge_style = "background: var(--danger);" if urgent > 0 else "background: var(--primary);"
        return HTMLResponse(content=f"""
            <span style="display: inline-block; {badge_style} color: white; font-size: 0.65rem; padding: 0.1rem 0.4rem; border-radius: 10px; min-width: 16px; text-align: center;">
                {total}
            </span>
        """)
    else:
        return HTMLResponse(content="")


# -------------------------------------------------------------------
# Sidebar API endpoints (HTMX fragments for chat landing page)
# -------------------------------------------------------------------


@app.get("/api/sidebar/commitments", response_class=HTMLResponse)
async def sidebar_commitments(request: Request):
    """Get commitment alerts for chat sidebar."""
    from cognitex.db.neo4j import get_neo4j_session
    from cognitex.agent.graph_observer import GraphObserver

    overdue = []
    approaching = []
    try:
        async for session in get_neo4j_session():
            observer = GraphObserver(session)
            approaching = await observer.get_approaching_commitments(hours=48)
            overdue = await observer.get_overdue_commitments()
            break
    except Exception:
        pass

    return templates.TemplateResponse(
        "partials/sidebar_commitments.html",
        {"request": request, "overdue": overdue, "approaching": approaching},
    )


@app.get("/api/sidebar/next-meeting", response_class=HTMLResponse)
async def sidebar_next_meeting(request: Request):
    """Get next upcoming meeting for chat sidebar."""
    from datetime import datetime as dt

    events = []
    try:
        events = await get_today_events()
    except Exception:
        pass

    # Find next event (start time in the future)
    now = dt.now()
    next_event = None
    for event in events:
        start_str = event.get("start_formatted", "")
        # Events from get_today_events() have start_formatted; if the event
        # hasn't passed yet, use it as next. We compare raw start_time if available.
        raw_start = event.get("start_time")
        if raw_start:
            try:
                # neo4j datetime objects have .to_native()
                if hasattr(raw_start, "to_native"):
                    event_dt = raw_start.to_native()
                elif isinstance(raw_start, str):
                    event_dt = dt.fromisoformat(raw_start.replace("Z", "+00:00"))
                else:
                    event_dt = raw_start
                # Compare naive datetimes
                if event_dt.replace(tzinfo=None) > now:
                    next_event = event
                    break
            except Exception:
                # Fallback: just use first event
                next_event = event
                break
        else:
            # No raw start available, use first event as fallback
            next_event = event
            break

    return templates.TemplateResponse(
        "partials/sidebar_next_meeting.html",
        {"request": request, "event": next_event},
    )


@app.get("/api/sidebar/mode", response_class=HTMLResponse)
async def sidebar_mode(request: Request):
    """Get current operating mode for chat sidebar."""
    mode_label = "Fragmented"
    mode_class = "fragmented"
    notes = None

    try:
        estimator = get_state_estimator()
        state = await estimator.get_current_state()
        mode_label = state.mode.value.replace("_", " ").title()
        mode_class = state.mode.value
        notes = state.notes
    except Exception:
        pass

    return templates.TemplateResponse(
        "partials/sidebar_mode.html",
        {"request": request, "mode_label": mode_label, "mode_class": mode_class, "notes": notes},
    )


@app.get("/api/inbox/items", response_class=HTMLResponse)
async def inbox_items_partial(request: Request, type: str | None = None):
    """Get inbox items partial (for HTMX refresh)."""
    from cognitex.services.inbox import get_inbox_service

    inbox = get_inbox_service()
    items = await inbox.get_pending_items(item_type=type)

    return templates.TemplateResponse(
        "partials/inbox_items.html",
        {"request": request, "items": items}
    )


def _apply_memory_updates(current_content: str, proposed_updates: list[dict]) -> str:
    """Apply proposed memory updates to MEMORY.md content.

    Inserts new entries into the correct section, merges with existing
    entries when indicated, and creates missing sections as needed.

    Args:
        current_content: Current MEMORY.md text.
        proposed_updates: List of update dicts from the distillation LLM.

    Returns:
        Updated MEMORY.md text.
    """
    # Section name mapping: skill output name -> MEMORY.md header
    section_map = {
        "User Preferences": "## User Preferences",
        "Important Relationships": "## Important Relationships",
        "Recurring Patterns": "## Recurring Patterns",
        "Corrections": "## Corrections",
        "Key Decisions": "## Key Decisions",
    }

    lines = current_content.split("\n")

    for update in proposed_updates:
        section_name = update.get("section", "Recurring Patterns")
        content = update.get("content", "")
        merge_target = update.get("merge_with_existing")
        source_dates = update.get("source_dates", [])
        date_prefix = source_dates[0] if source_dates else datetime.now().strftime("%Y-%m-%d")
        entry_line = f"- [{date_prefix}] {content}"

        header = section_map.get(section_name, f"## {section_name}")

        if merge_target:
            # Find and replace the existing entry
            replaced = False
            for i, line in enumerate(lines):
                # Match on the content portion (after any date prefix)
                stripped = line.strip()
                if stripped.startswith("- ") and merge_target in stripped:
                    lines[i] = entry_line
                    replaced = True
                    break
            if not replaced:
                # Fallback: append to section
                lines = _append_to_section(lines, header, entry_line)
        else:
            lines = _append_to_section(lines, header, entry_line)

    return "\n".join(lines)


def _append_to_section(lines: list[str], header: str, entry: str) -> list[str]:
    """Append an entry to a section, creating the section if needed."""
    # Find the section header
    header_idx = None
    for i, line in enumerate(lines):
        if line.strip() == header:
            header_idx = i
            break

    if header_idx is None:
        # Create section at end
        lines.append("")
        lines.append(header)
        lines.append(entry)
        return lines

    # Find end of section (next ## header or end of file)
    insert_idx = len(lines)
    for i in range(header_idx + 1, len(lines)):
        if lines[i].strip().startswith("## "):
            insert_idx = i
            break

    # Insert before the next section (or at end), after any existing content
    # Skip backwards over blank lines to insert right after last content
    while insert_idx > header_idx + 1 and not lines[insert_idx - 1].strip():
        insert_idx -= 1

    lines.insert(insert_idx, entry)
    return lines


@app.post("/api/inbox/{item_id}/approve", response_class=HTMLResponse)
async def approve_inbox_item(item_id: str):
    """Approve an inbox item."""
    from cognitex.services.inbox import get_inbox_service
    from cognitex.agent.action_log import log_action

    inbox = get_inbox_service()
    item = await inbox.get_item(item_id)

    if not item:
        return HTMLResponse(content="<div class='inbox-item'>Item not found</div>", status_code=404)

    # Handle type-specific approval
    if item.item_type == "task_proposal":
        # Delegate to existing proposal approval
        from cognitex.agent.action_log import approve_proposal
        if item.source_id:
            await approve_proposal(item.source_id)
    elif item.item_type == "commitment_proposal":
        # Accept commitment: pending -> accepted
        try:
            from cognitex.db.graph_schema import update_commitment
            from cognitex.db.neo4j import get_neo4j_session
            commitment_id = item.payload.get("commitment_id") or item.source_id
            if commitment_id:
                async for session in get_neo4j_session():
                    await update_commitment(session, commitment_id, status="accepted")
                    break
        except Exception as e:
            logger.warning("Failed to accept commitment", error=str(e))

    elif item.item_type == "memory_update_proposal":
        # Apply proposed updates to bootstrap MEMORY.md
        try:
            from cognitex.agent.bootstrap import get_bootstrap_loader

            loader = get_bootstrap_loader()
            memory_file = await loader.get_memory_file()
            current_content = memory_file.raw_content if memory_file else ""
            proposed_updates = item.payload.get("proposed_updates", [])

            new_content = _apply_memory_updates(current_content, proposed_updates)
            await loader.save_file("MEMORY.md", new_content)
            logger.info(
                "Applied memory distillation updates",
                updates=len(proposed_updates),
            )
        except Exception as e:
            logger.error("Failed to apply memory updates", error=str(e))

    # Mark item as approved
    await inbox.approve_item(item_id)

    # Record feedback for learning
    await inbox.record_feedback(
        item_id=item_id,
        item_type=item.item_type,
        action="approved",
        context=item.payload,
    )

    # Trigger expertise learning
    try:
        from cognitex.agent.expertise import get_expertise_manager
        em = get_expertise_manager()
        if item.item_type == "task_proposal":
            project_id = item.payload.get("project_id")
            if project_id:
                await em.self_improve(
                    domain=f"project:{project_id}",
                    action_type="task_approved",
                    context={"title": item.title, "payload": item.payload}
                )
        elif item.item_type == "context_pack":
            await em.self_improve(
                domain="context_pack",
                action_type="pack_approved",
                context=item.payload
            )
        elif item.item_type == "email_draft":
            await em.self_improve(
                domain="email_drafting",
                action_type="draft_approved",
                context=item.payload
            )
    except Exception as e:
        logger.warning("Failed to trigger learning on approval", error=str(e))

    # For email drafts, actually send the email
    if item.item_type == "email_draft":
        try:
            from cognitex.services.gmail import GmailService
            import asyncio

            gmail = GmailService()
            to = item.payload.get("to", "")
            subject = item.payload.get("subject", "")
            body = item.payload.get("body", item.payload.get("body_preview", ""))
            thread_id = item.payload.get("thread_id")

            if thread_id:
                result = await asyncio.to_thread(
                    gmail.send_reply,
                    thread_id=thread_id,
                    to=to,
                    subject=subject,
                    body=body,
                )
            else:
                result = await asyncio.to_thread(
                    gmail.send_message,
                    to=to,
                    subject=subject,
                    body=body,
                )

            message_id = result.get("id", "unknown")
            logger.info("Email sent via Gmail", to=to, message_id=message_id)

            await log_action(
                "email_sent",
                "web_ui",
                summary=f"Sent email to {to}: {subject[:50]}",
                details={"item_id": item_id, "to": to, "subject": subject, "message_id": message_id}
            )

            return HTMLResponse(content=f"""
                <div class="inbox-item" style="background: #d1fae5; border-color: #a7f3d0; opacity: 0.8;">
                    <div class="inbox-item-title" style="color: #065f46;">
                        ✓ Email sent to {html.escape(to)}
                    </div>
                </div>
            """)

        except Exception as e:
            logger.error("Failed to send email", error=str(e))
            return HTMLResponse(content=f"""
                <div class="inbox-item" style="background: #fee2e2; border-color: #fca5a5;">
                    <div class="inbox-item-title" style="color: #991b1b;">
                        Failed to send email: {html.escape(str(e))}
                    </div>
                </div>
            """, status_code=500)

    await log_action(
        "inbox_item_approved",
        "web_ui",
        summary=f"Approved {item.item_type}: {item.title[:50]}",
        details={"item_id": item_id, "item_type": item.item_type}
    )

    # Return success message that fades out
    return HTMLResponse(content=f"""
        <div class="inbox-item" style="background: #d1fae5; border-color: #a7f3d0; opacity: 0.8;">
            <div class="inbox-item-title" style="color: #065f46;">
                ✓ Approved: {item.title}
            </div>
        </div>
    """)


@app.post("/api/inbox/{item_id}/edit-and-send", response_class=HTMLResponse)
async def edit_and_send_email(
    item_id: str,
    to: str = Form(...),
    subject: str = Form(...),
    body: str = Form(...),
    original_body: str = Form(""),
):
    """Edit an email draft and send it, learning from the edits."""
    from cognitex.services.inbox import get_inbox_service
    from cognitex.agent.action_log import log_action
    from cognitex.agent.feedback_learning import record_feedback
    from difflib import SequenceMatcher

    inbox = get_inbox_service()
    item = await inbox.get_item(item_id)

    if not item:
        return HTMLResponse(content="<div class='inbox-item'>Item not found</div>", status_code=404)

    if item.item_type != "email_draft":
        return HTMLResponse(content="<div class='inbox-item'>Not an email draft</div>", status_code=400)

    # Calculate edit similarity (how much was changed)
    similarity = SequenceMatcher(None, original_body, body).ratio()
    edit_ratio = 1 - similarity  # Higher = more edits

    # Analyze the edits for learning
    edit_analysis = {
        "similarity": similarity,
        "edit_ratio": edit_ratio,
        "original_length": len(original_body),
        "final_length": len(body),
        "length_change": len(body) - len(original_body),
        "subject_changed": subject != item.payload.get("subject", ""),
        "to_changed": to != item.payload.get("to", ""),
    }

    # Record for learning - this is valuable training data
    try:
        await record_feedback(
            target_type="email_draft",
            target_id=item_id,
            feedback_category="edit_before_send",
            feedback_text=f"User edited {edit_ratio:.0%} of the draft before sending",
            was_rejection=False,
            context={
                "original_body": original_body[:1000],  # Store first 1000 chars
                "edited_body": body[:1000],
                "recipient": to,
                "subject": subject,
                **edit_analysis,
            },
        )
    except Exception as e:
        logger.warning("Failed to record edit feedback", error=str(e))

    # Trigger expertise learning with edit data
    try:
        from cognitex.agent.expertise import get_expertise_manager
        em = get_expertise_manager()

        # Learn from the edit patterns
        await em.self_improve(
            domain="email_drafting",
            action_type="draft_edited_and_sent",
            action_result={
                "edit_ratio": edit_ratio,
                "subject_changed": edit_analysis["subject_changed"],
                "length_change_percent": (edit_analysis["length_change"] / max(1, edit_analysis["original_length"])) * 100,
            },
            context={
                "recipient": to,
                "original_body_preview": original_body[:200],
                "final_body_preview": body[:200],
            }
        )

        # If significant edits, store as a learning example
        if edit_ratio > 0.2:  # More than 20% edited
            logger.info(
                "Significant email edit for learning",
                edit_ratio=edit_ratio,
                recipient=to,
            )
    except Exception as e:
        logger.warning("Failed to trigger learning on email edit", error=str(e))

    # Actually send the email via EmailProvider (routes to AgentMail or Gmail)
    try:
        from cognitex.services.email_provider import get_email_provider

        provider = get_email_provider()
        thread_id = item.payload.get("thread_id")

        if thread_id:
            result = await provider.reply_to_message(
                thread_id=thread_id,
                to=to,
                subject=subject,
                body=body,
                in_reply_to=item.payload.get("reply_to_id"),
            )
        else:
            result = await provider.send_message(to=to, subject=subject, body=body)

        message_id = result.get("id", "unknown")
        logger.info(
            "Email sent",
            provider=provider.provider_name,
            to=to,
            message_id=message_id,
        )

    except Exception as e:
        logger.error("Failed to send email", error=str(e), to=to)
        return HTMLResponse(content=f"""
            <div class="inbox-item" style="background: #fee2e2; border-color: #fca5a5;">
                <div class="inbox-item-title" style="color: #991b1b;">
                    Failed to send email: {html.escape(str(e))}
                </div>
                <div style="font-size: 0.85rem; color: #b91c1c;">
                    The email was not sent. Please try again or check your connection.
                </div>
            </div>
        """, status_code=500)

    # Mark item as approved
    await inbox.approve_item(item_id)

    await log_action(
        "email_draft_edited_sent",
        "web_ui",
        summary=f"Edited and sent email to {to}: {subject[:50]}",
        details={
            "item_id": item_id,
            "to": to,
            "subject": subject,
            "edit_ratio": edit_ratio,
            "similarity": similarity,
            "message_id": message_id,
        }
    )

    # Return success message
    edit_note = f" (edited {edit_ratio:.0%})" if edit_ratio > 0.05 else ""
    return HTMLResponse(content=f"""
        <div class="inbox-item" style="background: #d1fae5; border-color: #a7f3d0; opacity: 0.8;">
            <div class="inbox-item-title" style="color: #065f46;">
                ✓ Email sent to {html.escape(to)}{edit_note}
            </div>
            <div style="font-size: 0.85rem; color: #047857;">
                Thanks for the edit - this helps improve future drafts!
            </div>
        </div>
    """)


@app.post("/api/inbox/{item_id}/decide", response_class=HTMLResponse)
async def decide_inbox_item(
    item_id: str,
    decision: str = Form(...),
    custom_notes: str = Form(""),
):
    """Handle user decision on email_review inbox item.

    Creates a draft response based on the selected decision and document analysis,
    then presents it for final approval.
    """
    from cognitex.services.inbox import get_inbox_service
    from cognitex.agent.action_log import log_action
    from cognitex.db.postgres import get_session
    from cognitex.db.models import InboxItem as InboxItemModel
    import structlog

    logger = structlog.get_logger()
    inbox = get_inbox_service()
    item = await inbox.get_item(item_id)

    if not item:
        return HTMLResponse(content="<div class='inbox-item'>Item not found</div>", status_code=404)

    if item.item_type != "email_review":
        return HTMLResponse(content="<div class='inbox-item'>Invalid item type for decision</div>", status_code=400)

    payload = item.payload or {}

    # Find the selected decision option
    decision_options = payload.get("decision_options", [])
    selected_option = next(
        (opt for opt in decision_options if opt.get("id") == decision),
        None
    )

    if not selected_option and decision != "archive":
        return HTMLResponse(content="<div class='inbox-item'>Invalid decision option</div>", status_code=400)

    # Handle archive decision - just mark as decided, no draft needed
    if decision == "archive":
        await inbox.approve_item(item_id, decision_reason=f"Decision: {decision}")

        await log_action(
            "email_review_decided",
            "web_ui",
            summary=f"Archived email review: {item.title[:50]}",
            details={"item_id": item_id, "decision": decision}
        )

        return HTMLResponse(content=f"""
            <div class="inbox-item" style="background: #f3f4f6; border-color: #d1d5db; opacity: 0.8;">
                <div class="inbox-item-title" style="color: #4b5563;">
                    ✓ Archived: {item.title}
                </div>
            </div>
        """)

    # Generate a draft response based on decision + document analysis
    from cognitex.agent.core import get_agent

    agent = get_agent()

    # Build context for draft generation
    doc_analysis_summary = ""
    doc_analyses = payload.get("document_analysis", [])
    if doc_analyses:
        analysis_parts = []
        for doc in doc_analyses:
            parts = [f"**{doc.get('filename', 'Document')}**"]
            if doc.get("summary"):
                parts.append(f"Summary: {doc['summary'][:200]}")
            if doc.get("changes"):
                parts.append(f"Changes found: {len(doc['changes'])}")
            if doc.get("review_items"):
                parts.append(f"Review items: {', '.join(doc['review_items'][:3])}")
            analysis_parts.append("\n".join(parts))
        doc_analysis_summary = "\n\n".join(analysis_parts)

    response_template = selected_option.get("response_template", "") if selected_option else ""

    draft_prompt = f"""Please draft an email response based on the following:

**Original Email:**
- From: {payload.get('from_name') or payload.get('from', 'Unknown')}
- Subject: {payload.get('subject', 'No subject')}
- Key Ask: {payload.get('key_ask', 'Unknown')}

**User's Decision:** {selected_option.get('label') if selected_option else decision}
{f"**User's Notes:** {custom_notes}" if custom_notes else ""}

**Response Template to use as starting point:**
{response_template}

**Document Analysis Summary:**
{doc_analysis_summary if doc_analysis_summary else "No documents analyzed."}

Please draft a professional, concise response that:
1. Uses the template as a starting point but makes it specific
2. References specific findings from the document analysis if relevant
3. Is appropriate for the decision made ({decision})

Return ONLY the email body text, no subject line or headers."""

    try:
        draft_body = await agent.chat(draft_prompt)
    except Exception as e:
        logger.error("Failed to generate draft response", error=str(e))
        draft_body = response_template or f"[Draft generation failed - please write manually]"

    # Create a new email_draft inbox item for final approval
    draft_payload = {
        "original_email_id": payload.get("email_id"),
        "thread_id": payload.get("thread_id"),
        "to": payload.get("from"),
        "to_name": payload.get("from_name"),
        "subject": f"Re: {payload.get('subject', '')}",
        "body": draft_body,
        "body_preview": draft_body[:150] if draft_body else "",
        "based_on_decision": decision,
        "original_review_item_id": item_id,
        "document_analysis": doc_analyses,
    }

    async for session in get_session():
        draft_item = InboxItemModel(
            item_type="email_draft",
            title=f"Draft: Re: {payload.get('subject', '')[:60]}",
            summary=f"Draft response ({selected_option.get('label') if selected_option else decision}) to {payload.get('from_name') or payload.get('from', 'sender')}",
            payload=draft_payload,
            priority=item.priority,
            source="email_review",
            source_id=item_id,
        )
        session.add(draft_item)
        await session.commit()
        await session.refresh(draft_item)

        logger.info(
            "Created email draft from decision",
            draft_id=draft_item.id,
            original_item_id=item_id,
            decision=decision,
        )

        # Mark the original review item as decided
        await inbox.approve_item(item_id, decision_reason=f"Decision: {decision}")

        await log_action(
            "email_review_decided",
            "web_ui",
            summary=f"Decided on review ({decision}): {item.title[:50]}",
            details={
                "item_id": item_id,
                "decision": decision,
                "draft_id": str(draft_item.id),
            }
        )

        # Return success message with link to see draft
        return HTMLResponse(content=f"""
            <div class="inbox-item" style="background: #dbeafe; border-color: #93c5fd;">
                <div class="inbox-item-title" style="color: #1e40af;">
                    ✓ Draft created: Re: {payload.get('subject', '')[:50]}
                </div>
                <div class="inbox-item-summary" style="margin-top: 0.5rem;">
                    Decision: <strong>{selected_option.get('label') if selected_option else decision}</strong><br>
                    <small>Check the Emails filter to review and send the draft.</small>
                </div>
            </div>
        """)

    return HTMLResponse(content="<div class='inbox-item'>Error creating draft</div>", status_code=500)


@app.post("/api/inbox/{item_id}/reject", response_class=HTMLResponse)
async def reject_inbox_item(
    item_id: str,
    reason: str = Form("other"),
    reason_text: str = Form(None),
):
    """Reject an inbox item with feedback."""
    from cognitex.services.inbox import get_inbox_service
    from cognitex.agent.action_log import log_action, learn_from_rejection

    inbox = get_inbox_service()
    item = await inbox.get_item(item_id)

    if not item:
        return HTMLResponse(content="<div class='inbox-item'>Item not found</div>", status_code=404)

    # Handle type-specific rejection
    if item.item_type == "task_proposal" and item.source_id:
        from cognitex.agent.action_log import reject_proposal
        await reject_proposal(item.source_id, f"{reason}: {reason_text}" if reason_text else reason)
    elif item.item_type == "commitment_proposal":
        # Abandon commitment
        try:
            from cognitex.db.graph_schema import update_commitment
            from cognitex.db.neo4j import get_neo4j_session
            commitment_id = item.payload.get("commitment_id") or item.source_id
            if commitment_id:
                async for session in get_neo4j_session():
                    await update_commitment(session, commitment_id, status="abandoned")
                    break
        except Exception as e:
            logger.warning("Failed to abandon commitment", error=str(e))

    # Mark item as rejected
    await inbox.reject_item(item_id, reason, reason_text)

    # Learn from rejection
    await learn_from_rejection(
        proposal_type=item.item_type,
        rejection_reason=f"{reason}: {item.title}",
        context={
            "reason_category": reason,
            "reason_text": reason_text,
            **item.payload,
        }
    )

    await log_action(
        "inbox_item_rejected",
        "web_ui",
        summary=f"Rejected {item.item_type}: {item.title[:50]} ({reason})",
        details={"item_id": item_id, "item_type": item.item_type, "reason": reason}
    )

    # Route to skill feedback
    try:
        from cognitex.agent.skill_feedback_router import route_rejection_to_skill
        await route_rejection_to_skill(
            proposal_type=item.item_type,
            reason=reason,
            context=item.payload,
        )
    except Exception:
        pass

    return HTMLResponse(content=f"""
        <div class="inbox-item" style="background: #fee2e2; border-color: #fecaca; opacity: 0.8;">
            <div class="inbox-item-title" style="color: #991b1b;">
                ✗ Rejected: {item.title}
            </div>
        </div>
    """)


@app.post("/api/inbox/{item_id}/dismiss", response_class=HTMLResponse)
async def dismiss_inbox_item(item_id: str):
    """Dismiss an inbox item (no longer relevant)."""
    from cognitex.services.inbox import get_inbox_service
    from cognitex.agent.action_log import log_action

    inbox = get_inbox_service()
    item = await inbox.get_item(item_id)

    if not item:
        return HTMLResponse(content="<div class='inbox-item'>Item not found</div>", status_code=404)

    await inbox.dismiss_item(item_id)

    await log_action(
        "inbox_item_dismissed",
        "web_ui",
        summary=f"Dismissed {item.item_type}: {item.title[:50]}",
        details={"item_id": item_id, "item_type": item.item_type}
    )

    return HTMLResponse(content=f"""
        <div class="inbox-item" style="background: #f1f5f9; border-color: #e2e8f0; opacity: 0.8;">
            <div class="inbox-item-title" style="color: #64748b;">
                Dismissed: {item.title}
            </div>
        </div>
    """)


@app.post("/api/inbox/{item_id}/skip", response_class=HTMLResponse)
async def skip_inbox_email(
    item_id: str,
    reason: str = Form(None),
):
    """Skip an email item (won't respond) and record for learning.

    This endpoint specifically handles email-related inbox items and
    records the decision to help learn which emails need responses.
    """
    from cognitex.services.inbox import get_inbox_service
    from cognitex.agent.action_log import log_action

    inbox = get_inbox_service()
    item = await inbox.get_item(item_id)

    if not item:
        return HTMLResponse(content="<div class='inbox-item'>Item not found</div>", status_code=404)

    # Record email response decision for learning
    if item.item_type in ["email_draft", "email_review", "flagged_item"]:
        await _record_email_skip_decision(item, reason)

    # Dismiss the item
    await inbox.dismiss_item(item_id)

    await log_action(
        "inbox_email_skipped",
        "web_ui",
        summary=f"Skipped email: {item.title[:50]}",
        details={"item_id": item_id, "item_type": item.item_type, "reason": reason}
    )

    return HTMLResponse(content=f"""
        <div class="inbox-item" style="background: #fef3c7; border-color: #fde68a; opacity: 0.8;">
            <div class="inbox-item-title" style="color: #92400e;">
                Skipped: {item.title}
            </div>
        </div>
    """)


async def _record_email_skip_decision(item, reason: str | None) -> None:
    """Record an email skip decision for response pattern learning.

    Args:
        item: The inbox item being skipped
        reason: Optional reason for skipping
    """
    from cognitex.db.postgres import get_session
    from sqlalchemy import text
    from datetime import datetime

    try:
        # Extract email details from payload
        email_id = item.payload.get("email_id") or item.payload.get("gmail_id")
        sender = item.payload.get("from") or item.payload.get("sender_email", "")
        subject = item.payload.get("subject", item.title)
        intent = item.payload.get("intent")
        intent_confidence = item.payload.get("intent_confidence")

        if not email_id:
            return

        # Extract sender domain
        sender_domain = None
        if "@" in sender:
            sender_domain = sender.split("@")[1].lower()

        # Get current context
        hour_of_day = datetime.now().hour
        day_of_week = datetime.now().weekday()

        # Try to get operating mode
        operating_mode = None
        try:
            from cognitex.agent.state_model import get_state_estimator
            state = await get_state_estimator().get_current_state()
            operating_mode = state.mode.value if state else None
        except Exception:
            pass

        async for session in get_session():
            try:
                await session.execute(
                    text("""
                        INSERT INTO email_response_decisions (
                            email_id, sender_email, sender_domain, subject,
                            intent, intent_confidence, predicted_needs_response,
                            user_decision, decision_reason,
                            operating_mode, hour_of_day, day_of_week,
                            did_respond
                        ) VALUES (
                            :email_id, :sender, :domain, :subject,
                            :intent, :confidence, :predicted,
                            'skipped', :reason,
                            :mode, :hour, :day,
                            false
                        )
                        ON CONFLICT DO NOTHING
                    """),
                    {
                        "email_id": email_id,
                        "sender": sender,
                        "domain": sender_domain,
                        "subject": subject[:200] if subject else None,
                        "intent": intent,
                        "confidence": intent_confidence,
                        "predicted": item.payload.get("predicted_needs_response", True),
                        "reason": reason,
                        "mode": operating_mode,
                        "hour": hour_of_day,
                        "day": day_of_week,
                    },
                )
                await session.commit()
                logger.debug("Recorded email skip decision", email_id=email_id)
            except Exception as e:
                logger.warning("Failed to record email skip decision", error=str(e))
            break

    except Exception as e:
        logger.warning("Error recording email skip", error=str(e))


@app.post("/api/inbox/{item_id}/helpful")
async def mark_inbox_item_helpful(item_id: str, helpful: bool = True):
    """Mark a context pack or similar item as helpful/not helpful."""
    from cognitex.services.inbox import get_inbox_service
    from cognitex.agent.action_log import log_action

    inbox = get_inbox_service()
    item = await inbox.get_item(item_id)

    if not item:
        return {"success": False, "error": "Item not found"}

    await inbox.mark_helpful(item_id, helpful)

    # Trigger learning
    try:
        from cognitex.agent.expertise import get_expertise_manager
        em = get_expertise_manager()
        await em.self_improve(
            domain="context_pack",
            action_type="pack_feedback",
            context={"helpful": helpful, **item.payload}
        )
    except Exception as e:
        logger.warning("Failed to trigger learning on feedback", error=str(e))

    await log_action(
        "inbox_item_feedback",
        "web_ui",
        summary=f"Marked {item.item_type} as {'helpful' if helpful else 'not helpful'}",
        details={"item_id": item_id, "helpful": helpful}
    )

    return {"success": True, "helpful": helpful}


@app.post("/api/inbox/{item_id}/rebuild", response_class=HTMLResponse)
async def rebuild_context_pack(request: Request, item_id: str):
    """Rebuild a context pack with fresh data."""
    from cognitex.services.inbox import get_inbox_service
    from cognitex.agent.context_pack import get_context_pack_compiler, BuildStage
    from cognitex.services.calendar import CalendarService
    from cognitex.agent.action_log import log_action
    from datetime import datetime, timedelta
    from dateutil.parser import parse as parse_dt

    inbox = get_inbox_service()
    item = await inbox.get_item(item_id)

    if not item:
        return HTMLResponse(content="<div class='alert alert-danger'>Item not found</div>", status_code=404)

    if item.item_type != "context_pack":
        return HTMLResponse(content="<div class='alert alert-danger'>Not a context pack</div>", status_code=400)

    event_id = item.payload.get("event_id")
    if not event_id:
        return HTMLResponse(content="<div class='alert alert-danger'>No event ID in pack</div>", status_code=400)

    try:
        # Get the calendar event
        calendar = CalendarService()
        event = calendar.get_event(event_id)

        if not event:
            return HTMLResponse(content="<div class='alert alert-danger'>Calendar event not found</div>", status_code=404)

        # Determine appropriate build stage based on time until event
        event_time_str = event.get("start", {}).get("dateTime", "")
        stage = BuildStage.T_24H
        if event_time_str:
            event_dt = parse_dt(event_time_str)
            hours_until = (event_dt - datetime.now(event_dt.tzinfo)).total_seconds() / 3600

            if hours_until <= 0.25:
                stage = BuildStage.T_15M
            elif hours_until <= 2:
                stage = BuildStage.T_2H

        # Compile fresh pack
        compiler = get_context_pack_compiler()
        pack = await compiler.compile_for_event(event, stage)

        # Convert dataclasses to dicts for JSON serialization
        attendee_profiles_data = []
        for profile in pack.attendee_profiles:
            profile_dict = {
                "email": profile.email,
                "name": profile.name,
                "organization": profile.organization,
                "role": profile.role,
                "relationship_summary": profile.relationship_summary,
                "email_count": profile.email_count,
                "shared_projects": profile.shared_projects,
                "open_tasks": profile.open_tasks,
            }
            attendee_profiles_data.append(profile_dict)

        ambient_context_data = []
        for ctx in pack.ambient_context:
            ctx_dict = {
                "person_email": ctx.person_email,
                "person_name": ctx.person_name,
                "last_meeting": ctx.last_meeting,
                "emails_since": ctx.emails_since,
                "emails_from_them": ctx.emails_from_them,
                "emails_to_them": ctx.emails_to_them,
                "email_topics": ctx.email_topics,
                "pending_requests": ctx.pending_requests,
                "awaiting_their_response": ctx.awaiting_their_response,
                "summary": ctx.summary,
            }
            ambient_context_data.append(ctx_dict)

        # Determine priority
        priority = "normal"
        if stage == BuildStage.T_15M:
            priority = "urgent"
        elif stage == BuildStage.T_2H:
            priority = "high"

        # Set expiry
        expires_at = None
        if event_time_str:
            try:
                event_dt = parse_dt(event_time_str)
                expires_at = event_dt + timedelta(minutes=30)
            except Exception:
                pass

        summary = event.get("summary", "Meeting")

        # Update the existing inbox item with fresh data (don't dismiss and recreate)
        new_payload = {
            "pack_id": pack.pack_id,
            "readiness": pack.readiness_score,
            "event_id": event.get("id"),
            "event_title": summary,
            "event_time": event_time_str,
            "stage": stage.value,
            "missing_count": len(pack.missing_prerequisites),
            # Rich content
            "objective": pack.objective,
            "what_you_need_to_know": pack.what_you_need_to_know,
            "attendee_profiles": attendee_profiles_data,
            "ambient_context": ambient_context_data,
            "decision_list": pack.decision_list,
            "dont_forget": pack.dont_forget,
            "missing_prerequisites": pack.missing_prerequisites,
            "artifact_links": pack.artifact_links,
        }

        # Update the item in place
        await inbox.update_item(
            item_id,
            payload=new_payload,
            summary=f"Readiness: {pack.readiness_score:.0%} | {len(pack.missing_prerequisites)} items need attention",
            priority=priority,
        )

        await log_action(
            "context_pack_rebuilt",
            "web_ui",
            summary=f"Rebuilt context pack for: {summary}",
            details={"event_id": event_id, "stage": stage.value, "readiness": pack.readiness_score}
        )

        # Re-fetch the updated item and render it
        updated_item = await inbox.get_item(item_id)
        return templates.TemplateResponse(
            "partials/inbox_item_single.html",
            {"request": request, "item": updated_item}
        )

    except Exception as e:
        logger.warning("Failed to rebuild context pack", error=str(e), item_id=item_id)
        return HTMLResponse(
            content=f"<div class='alert alert-danger'>Failed to rebuild: {html.escape(str(e))}</div>",
            status_code=500
        )


@app.post("/api/inbox/{item_id}/notes")
async def save_inbox_notes(item_id: str, notes: str = Form(...)):
    """Save user notes on an inbox item for learning."""
    from cognitex.services.inbox import get_inbox_service
    from cognitex.agent.action_log import log_action
    from cognitex.agent.feedback_learning import record_feedback

    inbox = get_inbox_service()
    item = await inbox.get_item(item_id)

    if not item:
        return {"success": False, "error": "Item not found"}

    # Record for learning
    try:
        await record_feedback(
            target_type=item.item_type,
            target_id=item_id,
            feedback_category="user_notes",
            feedback_text=notes,
            was_rejection=False,
            context=item.payload,
        )
    except Exception as e:
        logger.warning("Failed to record feedback", error=str(e))

    # Also log the action
    await log_action(
        "inbox_item_notes",
        "web_ui",
        summary=f"User added notes to {item.item_type}: {item.title[:50]}",
        details={"item_id": item_id, "notes": notes[:500], "item_type": item.item_type}
    )

    return {"success": True, "message": "Notes saved - thanks for the feedback!"}


@app.get("/context-packs", response_class=HTMLResponse)
async def context_packs_page(request: Request):
    """View all context packs (pending and past)."""
    from cognitex.services.inbox import get_inbox_service
    from cognitex.db.postgres import get_session
    from sqlalchemy import text

    inbox = get_inbox_service()

    # Get pending context packs from inbox
    pending_items = await inbox.get_pending_items(item_type="context_pack", limit=20)

    # Get recent approved/dismissed context packs
    past_items = []
    async for session in get_session():
        try:
            result = await session.execute(text("""
                SELECT id, item_type, status, priority, title, summary,
                       payload, source_id, source_type, created_at,
                       decided_at, decision_reason, expires_at
                FROM inbox_items
                WHERE item_type = 'context_pack'
                  AND status IN ('approved', 'dismissed')
                ORDER BY decided_at DESC
                LIMIT 20
            """))
            from cognitex.services.inbox import InboxItem
            for row in result.fetchall():
                past_items.append(InboxItem.from_row(row))
        except Exception as e:
            logger.warning("Failed to get past context packs", error=str(e))
        break

    return templates.TemplateResponse(
        "context_packs.html",
        {
            "request": request,
            "pending_items": pending_items,
            "past_items": past_items,
        }
    )


@app.get("/learning", response_class=HTMLResponse)
async def learning_page(request: Request):
    """Learning system dashboard showing accumulated patterns and insights."""
    from cognitex.agent.learning import get_learning_system, init_learning_system
    from cognitex.agent.action_log import get_proposal_patterns, get_recent_rejections
    from cognitex.services.tasks import get_calibration_summary
    from cognitex.agent.state_model import get_high_risk_tasks

    # Initialize learning system if needed
    try:
        await init_learning_system()
    except Exception:
        pass

    # Get learning system summary
    ls = get_learning_system()
    summary = {}
    if ls:
        try:
            summary = await ls.get_learning_summary()
        except Exception as e:
            logger.warning("Failed to get learning summary", error=str(e))

    # Get proposal patterns
    proposals = {}
    try:
        proposals = await get_proposal_patterns(min_samples=1)
    except Exception as e:
        logger.warning("Failed to get proposal patterns", error=str(e))

    # Get recent rejections
    recent_rejections = []
    try:
        recent_rejections = await get_recent_rejections(limit=10)
    except Exception as e:
        logger.warning("Failed to get recent rejections", error=str(e))

    # Get duration calibration - normalize to template expectations
    calibration = {"samples": 0, "avg_error": 0, "avg_actual_minutes": 0, "by_project": {}}
    try:
        raw_calibration = await get_calibration_summary()
        overall = raw_calibration.get("overall", {})
        calibration = {
            "samples": overall.get("total_records", 0),
            "avg_error": (overall.get("overall_pace_factor", 1.0) - 1.0),  # Convert pace to error
            "avg_actual_minutes": overall.get("total_hours_tracked", 0) * 60 / max(overall.get("total_records", 1), 1),
            "by_project": raw_calibration.get("by_project", {}),
            "insights": raw_calibration.get("insights", []),
        }
    except Exception as e:
        logger.warning("Failed to get calibration summary", error=str(e))

    # Get high-risk tasks
    high_risk_tasks = []
    try:
        high_risk_tasks = await get_high_risk_tasks(min_risk=0.3, limit=10)
    except Exception as e:
        logger.warning("Failed to get high risk tasks", error=str(e))

    # Get deferral stats - ensure all expected keys exist
    raw_deferrals = summary.get("deferrals", {})
    deferral_stats = {
        "total": raw_deferrals.get("total", 0),
        "avg_per_task": raw_deferrals.get("avg_per_task", 0),
        "by_reason": raw_deferrals.get("by_reason", {}),
    }

    # Get learned patterns from database
    learned_patterns = []
    try:
        from cognitex.db.postgres import get_session as get_postgres_session
        from sqlalchemy import text

        async for session in get_postgres_session():
            result = await session.execute(text("""
                SELECT pattern_type, pattern_data, confidence, sample_size, last_updated
                FROM learned_patterns
                WHERE confidence >= 0.3
                ORDER BY confidence DESC, sample_size DESC
                LIMIT 10
            """))
            rows = result.fetchall()
            for row in rows:
                pattern_data = row[1] if isinstance(row[1], dict) else {}
                learned_patterns.append({
                    "pattern_type": row[0],
                    "description": pattern_data.get("description", str(pattern_data)[:100]),
                    "confidence": row[2],
                    "sample_count": row[3],
                    "created_at": row[4],
                })
            break
    except Exception as e:
        logger.warning("Failed to get learned patterns", error=str(e))

    # Get rules by lifecycle
    rules_by_lifecycle = {}
    try:
        from cognitex.agent.decision_memory import get_decision_memory

        dm = get_decision_memory()
        if dm and dm.rules:
            rules_by_lifecycle = await dm.rules.get_rules_by_lifecycle()
    except Exception as e:
        logger.warning("Failed to get rules by lifecycle", error=str(e))

    # Get last policy update from action log
    last_update = None
    try:
        from cognitex.db.postgres import get_session as get_pg_session
        from sqlalchemy import text

        async for session in get_pg_session():
            result = await session.execute(text("""
                SELECT timestamp, details
                FROM agent_actions
                WHERE action_type = 'policy_update' AND status IS DISTINCT FROM 'failed'
                ORDER BY timestamp DESC
                LIMIT 1
            """))
            row = result.fetchone()
            if row:
                details = row[1] if isinstance(row[1], dict) else {}
                last_update = {
                    "timestamp": row[0].strftime("%Y-%m-%d %H:%M") if row[0] else "Unknown",
                    "rules_validated": details.get("rules_validated", 0),
                    "patterns_extracted": details.get("patterns_extracted", 0),
                    "rules_deprecated": details.get("rules_deprecated", 0),
                }
            break
    except Exception as e:
        logger.warning("Failed to get last policy update", error=str(e))

    # Get task rejections (from reject & learn feature)
    task_rejections = []
    try:
        from cognitex.db.postgres import get_session as get_pg_session2
        async for session in get_pg_session2():
            result = await session.execute(text("""
                SELECT timestamp, summary, details
                FROM agent_actions
                WHERE action_type = 'task_rejected'
                ORDER BY timestamp DESC
                LIMIT 10
            """))
            for row in result.fetchall():
                details = row[2] if isinstance(row[2], dict) else {}
                task_rejections.append({
                    "timestamp": row[0].strftime("%Y-%m-%d %H:%M") if row[0] else "",
                    "summary": row[1],
                    "reason": details.get("reason", ""),
                    "email_sender": details.get("email_sender", ""),
                    "email_subject": details.get("email_subject", ""),
                })
            break
    except Exception as e:
        logger.warning("Failed to get task rejections", error=str(e))

    # Get decision traces summary
    decision_summary = {"total": 0, "by_action": {}, "recent": []}
    try:
        from cognitex.db.postgres import get_session as get_pg_session3
        async for session in get_pg_session3():
            # Total count and by action type
            result = await session.execute(text("""
                SELECT action_type, COUNT(*) as count
                FROM decision_traces
                GROUP BY action_type
                ORDER BY count DESC
            """))
            for row in result.fetchall():
                decision_summary["by_action"][row[0]] = row[1]
                decision_summary["total"] += row[1]

            # Recent decisions
            result = await session.execute(text("""
                SELECT created_at, action_type, status, quality_score
                FROM decision_traces
                ORDER BY created_at DESC
                LIMIT 10
            """))
            for row in result.fetchall():
                decision_summary["recent"].append({
                    "timestamp": row[0].strftime("%Y-%m-%d %H:%M") if row[0] else "",
                    "action_type": row[1],
                    "decision": row[2],
                    "quality_score": row[3],
                })
            break
    except Exception as e:
        logger.warning("Failed to get decision traces", error=str(e))

    # Get pending proposals count for link
    pending_proposals_count = 0
    try:
        from cognitex.db.postgres import get_session as get_pg_session4
        async for session in get_pg_session4():
            result = await session.execute(text("""
                SELECT COUNT(*) FROM task_proposals WHERE status = 'pending'
            """))
            row = result.fetchone()
            pending_proposals_count = row[0] if row else 0
            break
    except Exception:
        pass

    # Calculate stats
    total_proposals = proposals.get("total_proposals", 0)
    approval_rate = proposals.get("overall_approval_rate", 0)

    stats = {
        "total_timing_records": calibration.get("samples", 0),
        "total_deferrals": deferral_stats.get("total", 0),
        "total_proposals": total_proposals,
        "approval_rate": approval_rate,
        "pending_proposals": pending_proposals_count,
        "total_decisions": decision_summary["total"],
        "total_task_rejections": len(task_rejections),
    }

    return templates.TemplateResponse(
        "learning.html",
        {
            "request": request,
            "stats": stats,
            "proposals": proposals,
            "recent_rejections": recent_rejections,
            "calibration": calibration,
            "high_risk_tasks": high_risk_tasks,
            "deferral_stats": deferral_stats,
            "learned_patterns": learned_patterns,
            "rules_by_lifecycle": rules_by_lifecycle,
            "last_update": last_update,
            "task_rejections": task_rejections,
            "decision_summary": decision_summary,
        },
    )


@app.get("/api/learning/refresh")
async def api_learning_refresh():
    """Refresh learning data (triggers re-analysis)."""
    return {"status": "ok"}


@app.post("/api/learning/run-update")
async def api_learning_run_update():
    """Manually trigger a policy update cycle."""
    from cognitex.agent.learning import init_learning_system, get_learning_system
    from cognitex.agent.action_log import log_action

    try:
        await init_learning_system()
        ls = get_learning_system()

        if ls:
            results = await ls.run_policy_update()

            await log_action(
                "policy_update",
                "web_ui",
                summary=f"Manual policy update: {results.get('rules_validated', 0)} rules validated",
                details=results,
            )

            return {"status": "ok", "results": results}

        return {"status": "error", "message": "Learning system not initialized"}

    except Exception as e:
        logger.error("Manual policy update failed", error=str(e))
        return {"status": "error", "message": str(e)}


# ========================================================================
# Agent Expertise System
# ========================================================================

@app.get("/expertise", response_class=HTMLResponse)
async def expertise_page(request: Request):
    """Agent Expertise dashboard - view and manage agent mental models."""
    from cognitex.agent.expertise import get_expertise_manager

    em = get_expertise_manager()

    # Get all expertise domains
    expertise_list = []
    try:
        expertise_list = await em.list_expertise()
    except Exception as e:
        logger.warning("Failed to list expertise", error=str(e))

    # Get recent learnings
    recent_learnings = []
    try:
        recent_learnings = await em.get_recent_learnings(limit=15)
    except Exception as e:
        logger.warning("Failed to get recent learnings", error=str(e))

    # Stats
    stats = {
        "total_domains": len(expertise_list),
        "total_learnings": sum(e.get("learnings_count", 0) for e in expertise_list),
        "project_count": len([e for e in expertise_list if e.get("domain_type") == "project"]),
        "skill_count": len([e for e in expertise_list if e.get("domain_type") == "skill"]),
    }

    # Build HTML (inline template for simplicity)
    expertise_rows = ""
    for exp in expertise_list:
        domain_type = exp.get("domain_type", "unknown")
        badge_color = {
            "project": "#3b82f6",
            "skill": "#10b981",
            "entity": "#8b5cf6",
            "workflow": "#f59e0b",
        }.get(domain_type, "#6b7280")

        last_improved = exp.get("last_improved_at", "")
        if last_improved:
            last_improved = last_improved[:10]

        expertise_rows += f"""
        <tr>
            <td><strong>{html.escape(exp.get('title', exp.get('domain', '')))}</strong></td>
            <td><span style="background: {badge_color}; color: white; padding: 2px 8px; border-radius: 3px; font-size: 0.75rem;">{domain_type}</span></td>
            <td>{exp.get('learnings_count', 0)}</td>
            <td>v{exp.get('version', 1)}</td>
            <td>{last_improved or '-'}</td>
            <td>
                <button class="btn btn-secondary btn-sm"
                        hx-get="/api/expertise/{exp.get('id')}"
                        hx-target="#expertise-detail"
                        hx-swap="innerHTML">View</button>
            </td>
        </tr>
        """

    learning_items = ""
    for learn in recent_learnings:
        content = learn.get("content", {})
        content_text = content.get("content", str(content))[:100] if isinstance(content, dict) else str(content)[:100]
        learning_items += f"""
        <div style="padding: 0.5rem; border-bottom: 1px solid #eee;">
            <div style="display: flex; justify-content: space-between; align-items: center;">
                <span style="font-weight: 500;">{html.escape(learn.get('domain', '')[:30])}</span>
                <span style="font-size: 0.75rem; color: #6b7280;">{learn.get('type', '')}</span>
            </div>
            <div style="font-size: 0.85rem; color: #4b5563; margin-top: 0.25rem;">
                {html.escape(content_text)}...
            </div>
            <div style="font-size: 0.7rem; color: #9ca3af; margin-top: 0.25rem;">
                {learn.get('source_action', '')} | {learn.get('created_at', '')[:10] if learn.get('created_at') else ''}
            </div>
        </div>
        """

    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Agent Expertise - Cognitex</title>
        <script src="https://unpkg.com/htmx.org@1.9.10"></script>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 0; padding: 20px; background: #f9fafb; }}
            .container {{ max-width: 1200px; margin: 0 auto; }}
            h1 {{ color: #111827; margin-bottom: 0.5rem; }}
            .subtitle {{ color: #6b7280; margin-bottom: 1.5rem; }}
            .stats {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 1rem; margin-bottom: 1.5rem; }}
            .stat {{ background: white; padding: 1rem; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
            .stat-value {{ font-size: 1.5rem; font-weight: 600; color: #111827; }}
            .stat-label {{ font-size: 0.8rem; color: #6b7280; }}
            .card {{ background: white; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); margin-bottom: 1rem; }}
            .card-header {{ padding: 1rem; border-bottom: 1px solid #e5e7eb; }}
            .card-header h2 {{ margin: 0; font-size: 1.1rem; color: #111827; }}
            table {{ width: 100%; border-collapse: collapse; }}
            th, td {{ padding: 0.75rem; text-align: left; border-bottom: 1px solid #e5e7eb; }}
            th {{ background: #f9fafb; font-weight: 500; color: #374151; font-size: 0.85rem; }}
            .btn {{ padding: 0.375rem 0.75rem; border: 1px solid #d1d5db; background: white; border-radius: 4px; cursor: pointer; }}
            .btn:hover {{ background: #f3f4f6; }}
            .btn-sm {{ font-size: 0.8rem; padding: 0.25rem 0.5rem; }}
            .grid {{ display: grid; grid-template-columns: 2fr 1fr; gap: 1rem; }}
            a {{ color: #2563eb; text-decoration: none; }}
            a:hover {{ text-decoration: underline; }}
            #expertise-detail {{ background: #f3f4f6; padding: 1rem; border-radius: 4px; margin-top: 1rem; min-height: 100px; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Agent Expertise</h1>
            <p class="subtitle">Self-improving mental models that make your agent an expert. <a href="/learning">← Back to Learning</a></p>

            <div class="stats">
                <div class="stat">
                    <div class="stat-value">{stats['total_domains']}</div>
                    <div class="stat-label">Expertise Domains</div>
                </div>
                <div class="stat">
                    <div class="stat-value">{stats['total_learnings']}</div>
                    <div class="stat-label">Total Learnings</div>
                </div>
                <div class="stat">
                    <div class="stat-value">{stats['project_count']}</div>
                    <div class="stat-label">Project Experts</div>
                </div>
                <div class="stat">
                    <div class="stat-value">{stats['skill_count']}</div>
                    <div class="stat-label">Skill Experts</div>
                </div>
            </div>

            <div class="grid">
                <div class="card">
                    <div class="card-header">
                        <h2>Expertise Domains</h2>
                    </div>
                    <table>
                        <thead>
                            <tr>
                                <th>Domain</th>
                                <th>Type</th>
                                <th>Learnings</th>
                                <th>Version</th>
                                <th>Last Improved</th>
                                <th>Actions</th>
                            </tr>
                        </thead>
                        <tbody>
                            {expertise_rows if expertise_rows else '<tr><td colspan="6" style="text-align:center; color:#6b7280;">No expertise yet. Complete tasks to start learning.</td></tr>'}
                        </tbody>
                    </table>

                    <div id="expertise-detail">
                        <p style="color: #6b7280; text-align: center;">Select an expertise domain to view details</p>
                    </div>
                </div>

                <div class="card">
                    <div class="card-header">
                        <h2>Recent Learnings</h2>
                    </div>
                    <div style="max-height: 500px; overflow-y: auto;">
                        {learning_items if learning_items else '<p style="padding: 1rem; color: #6b7280; text-align: center;">No learnings yet</p>'}
                    </div>
                </div>
            </div>
        </div>
    </body>
    </html>
    """

    return HTMLResponse(html_content)


@app.get("/api/expertise/{expertise_id}")
async def get_expertise_detail(expertise_id: str):
    """Get detailed view of an expertise domain."""
    from cognitex.agent.expertise import get_expertise_manager

    em = get_expertise_manager()

    # Get expertise by iterating (since we don't have get by ID directly)
    all_exp = await em.list_expertise()
    target = next((e for e in all_exp if e.get("id") == expertise_id), None)

    if not target:
        return HTMLResponse("<p>Expertise not found</p>")

    # Get full expertise with content
    full_exp = await em.get_expertise(target["domain"])

    if not full_exp:
        return HTMLResponse("<p>Could not load expertise details</p>")

    content = full_exp.get("content", {})

    # Format content for display
    content_html = "<div style='font-size: 0.9rem;'>"

    for key, value in content.items():
        if not value:
            continue

        content_html += f"<div style='margin-bottom: 0.75rem;'><strong>{key.replace('_', ' ').title()}:</strong><br>"

        if isinstance(value, list):
            if value:
                content_html += "<ul style='margin: 0.25rem 0; padding-left: 1.25rem;'>"
                for item in value[:5]:  # Limit to 5 items
                    if isinstance(item, dict):
                        item_text = item.get("content", item.get("name", str(item)))
                    else:
                        item_text = str(item)
                    content_html += f"<li>{html.escape(str(item_text)[:100])}</li>"
                if len(value) > 5:
                    content_html += f"<li style='color: #6b7280;'>... and {len(value) - 5} more</li>"
                content_html += "</ul>"
        else:
            content_html += f"<span style='color: #4b5563;'>{html.escape(str(value)[:200])}</span>"

        content_html += "</div>"

    content_html += "</div>"

    return HTMLResponse(f"""
        <h3 style="margin-top: 0;">{html.escape(full_exp.get('title', full_exp.get('domain', '')))}</h3>
        <p style="font-size: 0.8rem; color: #6b7280;">
            Domain: {html.escape(full_exp.get('domain', ''))} |
            Version: {full_exp.get('version', 1)} |
            Learnings: {full_exp.get('learnings_count', 0)}
        </p>
        {content_html}
    """)


# ========================================================================
# Phase 5.3: Proactive Task Suggestions
# ========================================================================

@app.get("/api/tasks/insights")
async def get_task_insights():
    """Get proactive task insights and suggestions.

    Returns:
    - Stalled projects (no activity 7+ days)
    - Large tasks needing breakdown
    - Tasks blocking others
    - Chronically deferred tasks
    - Commitments extracted from emails
    """
    from cognitex.db.neo4j import get_neo4j_session
    from cognitex.agent.graph_observer import GraphObserver

    async for session in get_neo4j_session():
        observer = GraphObserver(session)
        insights = await observer.get_proactive_task_insights()

        # Optionally extract commitments (this can be slow)
        # commitments = await observer.extract_commitments_from_emails(days_back=7)
        # insights["commitments"] = commitments

        return insights

    return {"error": "Failed to get insights"}


@app.get("/api/tasks/insights/html")
async def get_task_insights_html():
    """Get proactive task insights as HTML for dashboard embedding."""
    from cognitex.db.neo4j import get_neo4j_session
    from cognitex.agent.graph_observer import GraphObserver

    async for session in get_neo4j_session():
        observer = GraphObserver(session)
        insights = await observer.get_proactive_task_insights()

        html_parts = []

        # Stalled projects
        if insights.get("stalled_projects"):
            html_parts.append('<div class="insight-section"><h5>Stalled Projects</h5><ul>')
            for p in insights["stalled_projects"]:
                days = p.get("days_stalled", "?")
                html_parts.append(
                    f'<li><strong>{html.escape(p.get("title", "Untitled"))}</strong> - '
                    f'{days} days since last activity</li>'
                )
            html_parts.append('</ul></div>')

        # Large tasks
        if insights.get("large_tasks_needing_breakdown"):
            html_parts.append('<div class="insight-section"><h5>Tasks Needing Breakdown</h5><ul>')
            for t in insights["large_tasks_needing_breakdown"]:
                html_parts.append(
                    f'<li><strong>{html.escape(t.get("title", "Untitled")[:40])}</strong> - '
                    f'{html.escape(t.get("breakdown_reason", "Complex"))}</li>'
                )
            html_parts.append('</ul></div>')

        # Blocking tasks
        if insights.get("blocking_tasks"):
            html_parts.append('<div class="insight-section"><h5>Blocking Tasks (unblock these first!)</h5><ul>')
            for t in insights["blocking_tasks"]:
                count = t.get("blocks_count", 0)
                html_parts.append(
                    f'<li><strong>{html.escape(t.get("title", "Untitled")[:40])}</strong> - '
                    f'blocking {count} other task(s)</li>'
                )
            html_parts.append('</ul></div>')

        # Chronic deferrals
        if insights.get("chronic_deferrals"):
            html_parts.append('<div class="insight-section"><h5>Chronically Deferred</h5><ul>')
            for t in insights["chronic_deferrals"]:
                count = t.get("defer_count", 0)
                html_parts.append(
                    f'<li><strong>{html.escape(t.get("title", "Untitled")[:40])}</strong> - '
                    f'deferred {count}x ({html.escape(t.get("suggestion", ""))})</li>'
                )
            html_parts.append('</ul></div>')

        if not html_parts:
            return HTMLResponse('''
                <div class="alert alert-success">
                    <strong>All clear!</strong> No urgent task issues detected.
                </div>
            ''')

        return HTMLResponse(f'''
            <style>
                .insight-section {{ margin-bottom: 1rem; }}
                .insight-section h5 {{ color: #b45309; margin-bottom: 0.5rem; }}
                .insight-section ul {{ margin: 0; padding-left: 1.5rem; }}
                .insight-section li {{ margin-bottom: 0.25rem; }}
            </style>
            {''.join(html_parts)}
        ''')

    return HTMLResponse('<div class="alert alert-warning">Failed to load insights</div>')


@app.post("/api/tasks/extract-commitments")
async def extract_email_commitments():
    """Extract commitments from recent sent emails.

    Analyzes sent emails from the past 7 days to find implied
    commitments that should be tracked as tasks.
    """
    from cognitex.db.neo4j import get_neo4j_session
    from cognitex.agent.graph_observer import GraphObserver

    async for session in get_neo4j_session():
        observer = GraphObserver(session)
        commitments = await observer.extract_commitments_from_emails(days_back=7, limit=15)

        if not commitments:
            return HTMLResponse('''
                <div class="alert alert-info">
                    No commitments found in recent sent emails.
                </div>
            ''')

        # Build HTML for commitments
        commitment_rows = []
        for c in commitments:
            commitment_rows.append(f'''
                <tr>
                    <td>{html.escape(c.get('action', 'Unknown')[:50])}</td>
                    <td>{html.escape(c.get('deadline', 'Not specified')[:20])}</td>
                    <td>{html.escape(c.get('recipient', 'Unknown')[:30])}</td>
                    <td>{html.escape(c.get('email_subject', '')[:30])}</td>
                    <td>
                        <button class="btn btn-sm btn-primary"
                            hx-post="/api/tasks/create-from-commitment"
                            hx-vals='{{"action": "{html.escape(c.get("action", "")[:100])}", "deadline": "{html.escape(c.get("deadline", "")[:30])}", "recipient": "{html.escape(c.get("recipient", "")[:50])}", "email_id": "{c.get("source_email_id", "")}"}}'
                            hx-target="closest tr"
                            hx-swap="outerHTML">
                            Create Task
                        </button>
                    </td>
                </tr>
            ''')

        return HTMLResponse(f'''
            <div class="alert alert-info" style="margin-bottom: 1rem;">
                Found {len(commitments)} potential commitments in recent emails.
            </div>
            <table class="table table-sm">
                <thead>
                    <tr>
                        <th>Commitment</th>
                        <th>Deadline</th>
                        <th>To</th>
                        <th>Email</th>
                        <th>Action</th>
                    </tr>
                </thead>
                <tbody>
                    {''.join(commitment_rows)}
                </tbody>
            </table>
        ''')

    return HTMLResponse('<div class="alert alert-warning">Failed to extract commitments</div>')


# ========================================================================
# Phase 5.4: Daily Momentum System
# ========================================================================

@app.get("/api/momentum/today")
async def get_todays_momentum():
    """Get today's momentum status including must-do items."""
    from cognitex.db.neo4j import get_neo4j_session
    from datetime import datetime

    today = datetime.now().date().isoformat()

    async for session in get_neo4j_session():
        # Get today's must-do items
        query = """
        MATCH (md:MustDo)
        WHERE md.date = $today
        OPTIONAL MATCH (md)-[:TARGETS]->(t:Task)
        RETURN
            md.id as id,
            md.title as title,
            md.completed as completed,
            md.completed_at as completed_at,
            md.priority as priority,
            t.id as task_id,
            t.title as task_title
        ORDER BY md.priority ASC, md.created_at ASC
        """
        result = await session.run(query, {"today": today})
        must_dos = await result.data()

        # Calculate momentum stats
        total = len(must_dos)
        completed = sum(1 for m in must_dos if m.get("completed"))
        momentum_pct = (completed / total * 100) if total > 0 else 0

        # Get weekly stats
        week_query = """
        MATCH (md:MustDo)
        WHERE md.date >= date() - duration({days: 7})
        RETURN
            md.date as date,
            md.completed as completed
        ORDER BY md.date ASC
        """
        week_result = await session.run(week_query, {})
        week_data = await week_result.data()

        # Calculate weekly completion by day
        daily_stats = {}
        for item in week_data:
            day = item.get("date")
            if day not in daily_stats:
                daily_stats[day] = {"total": 0, "completed": 0}
            daily_stats[day]["total"] += 1
            if item.get("completed"):
                daily_stats[day]["completed"] += 1

        weekly_momentum = []
        for day, stats in sorted(daily_stats.items()):
            pct = (stats["completed"] / stats["total"] * 100) if stats["total"] > 0 else 0
            weekly_momentum.append({
                "date": day,
                "total": stats["total"],
                "completed": stats["completed"],
                "percentage": pct,
            })

        return {
            "date": today,
            "must_dos": must_dos,
            "today_stats": {
                "total": total,
                "completed": completed,
                "percentage": momentum_pct,
            },
            "weekly_momentum": weekly_momentum,
            "streak": calculate_streak(weekly_momentum),
        }

    return {"error": "Failed to get momentum data"}


def calculate_streak(weekly_momentum: list) -> int:
    """Calculate current streak of days with 100% completion."""
    streak = 0
    for day in reversed(weekly_momentum):
        if day.get("percentage", 0) == 100:
            streak += 1
        else:
            break
    return streak


@app.get("/api/momentum/today/html")
async def get_momentum_html():
    """Get today's momentum as HTML widget."""
    data = await get_todays_momentum()

    if "error" in data:
        return HTMLResponse('<div class="alert alert-warning">Failed to load momentum</div>')

    must_dos = data.get("must_dos", [])
    stats = data.get("today_stats", {})
    streak = data.get("streak", 0)

    # Build must-do list
    must_do_items = []
    for md in must_dos:
        checked = "checked" if md.get("completed") else ""
        style = "text-decoration: line-through; opacity: 0.7;" if md.get("completed") else ""
        must_do_items.append(f'''
            <div class="must-do-item" style="display: flex; align-items: center; margin-bottom: 0.5rem; {style}">
                <input type="checkbox" {checked}
                    hx-post="/api/momentum/toggle/{md['id']}"
                    hx-target="#momentum-widget"
                    hx-swap="innerHTML"
                    style="margin-right: 0.75rem; width: 1.2rem; height: 1.2rem;">
                <span>{html.escape(md.get('title', 'Untitled')[:50])}</span>
            </div>
        ''')

    # Progress bar
    pct = stats.get("percentage", 0)
    bar_color = "#22c55e" if pct >= 100 else "#eab308" if pct >= 50 else "#ef4444"

    streak_badge = f'<span class="badge bg-success" style="margin-left: 1rem;">{streak} day streak!</span>' if streak > 0 else ''

    return HTMLResponse(f'''
        <div id="momentum-widget">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem;">
                <h5 style="margin: 0;">Today's Must-Dos</h5>
                <span>{stats.get('completed', 0)}/{stats.get('total', 0)} complete {streak_badge}</span>
            </div>

            <div class="progress" style="height: 8px; margin-bottom: 1rem; background: #e5e7eb;">
                <div class="progress-bar" style="width: {pct}%; background: {bar_color};"></div>
            </div>

            <div class="must-do-list">
                {''.join(must_do_items) if must_do_items else '<p class="text-muted">No must-dos set for today. Add some below!</p>'}
            </div>

            <form hx-post="/api/momentum/add"
                  hx-target="#momentum-widget"
                  hx-swap="innerHTML"
                  style="margin-top: 1rem; display: flex; gap: 0.5rem;">
                <input type="text" name="title" placeholder="Add a must-do for today..."
                       class="form-control form-control-sm" style="flex: 1;">
                <button type="submit" class="btn btn-primary btn-sm">Add</button>
            </form>
        </div>
    ''')


@app.post("/api/momentum/add")
async def add_must_do(title: Annotated[str, Form()]):
    """Add a new must-do item for today."""
    from cognitex.db.neo4j import get_neo4j_session
    from datetime import datetime
    import uuid

    if not title or len(title.strip()) < 2:
        return await get_momentum_html()

    today = datetime.now().date().isoformat()
    must_do_id = f"mustdo_{uuid.uuid4().hex[:12]}"

    async for session in get_neo4j_session():
        # Count existing must-dos to set priority
        count_query = """
        MATCH (md:MustDo)
        WHERE md.date = $today
        RETURN COUNT(md) as count
        """
        count_result = await session.run(count_query, {"today": today})
        count_data = await count_result.single()
        priority = (count_data["count"] if count_data else 0) + 1

        # Create the must-do
        create_query = """
        CREATE (md:MustDo {
            id: $id,
            title: $title,
            date: $today,
            priority: $priority,
            completed: false,
            created_at: datetime()
        })
        RETURN md.id as id
        """
        await session.run(create_query, {
            "id": must_do_id,
            "title": title.strip(),
            "today": today,
            "priority": priority,
        })

        logger.info("Must-do added", id=must_do_id, title=title[:30])

    return await get_momentum_html()


@app.post("/api/momentum/toggle/{must_do_id}")
async def toggle_must_do(must_do_id: str):
    """Toggle a must-do item's completion status."""
    from cognitex.db.neo4j import get_neo4j_session

    async for session in get_neo4j_session():
        query = """
        MATCH (md:MustDo {id: $id})
        SET md.completed = NOT COALESCE(md.completed, false),
            md.completed_at = CASE WHEN NOT COALESCE(md.completed, false) THEN datetime() ELSE null END
        RETURN md.completed as completed
        """
        result = await session.run(query, {"id": must_do_id})
        data = await result.single()

        if data:
            logger.info("Must-do toggled", id=must_do_id, completed=data["completed"])

    return await get_momentum_html()


@app.delete("/api/momentum/{must_do_id}")
async def delete_must_do(must_do_id: str):
    """Delete a must-do item."""
    from cognitex.db.neo4j import get_neo4j_session

    async for session in get_neo4j_session():
        query = """
        MATCH (md:MustDo {id: $id})
        DELETE md
        """
        await session.run(query, {"id": must_do_id})
        logger.info("Must-do deleted", id=must_do_id)

    return await get_momentum_html()


@app.get("/api/momentum/weekly")
async def get_weekly_momentum():
    """Get weekly momentum summary with trends."""
    from cognitex.db.neo4j import get_neo4j_session
    from datetime import datetime, timedelta

    async for session in get_neo4j_session():
        # Get all must-dos from the past 4 weeks
        query = """
        MATCH (md:MustDo)
        WHERE md.date >= date() - duration({days: 28})
        RETURN
            md.date as date,
            md.completed as completed,
            md.title as title
        ORDER BY md.date ASC
        """
        result = await session.run(query, {})
        data = await result.data()

        # Group by week
        weeks = {}
        for item in data:
            date_str = item.get("date")
            if not date_str:
                continue
            # Calculate week number
            try:
                dt = datetime.fromisoformat(str(date_str))
                week_start = dt - timedelta(days=dt.weekday())
                week_key = week_start.strftime("%Y-%m-%d")
            except Exception:
                continue

            if week_key not in weeks:
                weeks[week_key] = {"total": 0, "completed": 0}
            weeks[week_key]["total"] += 1
            if item.get("completed"):
                weeks[week_key]["completed"] += 1

        # Calculate weekly percentages and trends
        weekly_data = []
        prev_pct = None
        for week, stats in sorted(weeks.items()):
            pct = (stats["completed"] / stats["total"] * 100) if stats["total"] > 0 else 0
            trend = None
            if prev_pct is not None:
                if pct > prev_pct + 5:
                    trend = "up"
                elif pct < prev_pct - 5:
                    trend = "down"
                else:
                    trend = "stable"
            weekly_data.append({
                "week_start": week,
                "total": stats["total"],
                "completed": stats["completed"],
                "percentage": round(pct, 1),
                "trend": trend,
            })
            prev_pct = pct

        # Calculate overall momentum score
        if weekly_data:
            recent_avg = sum(w["percentage"] for w in weekly_data[-2:]) / min(len(weekly_data), 2)
            momentum_score = round(recent_avg, 0)
        else:
            momentum_score = 0

        return {
            "weeks": weekly_data,
            "momentum_score": momentum_score,
            "total_days_tracked": len(set(item.get("date") for item in data if item.get("date"))),
            "current_streak": calculate_streak([{"percentage": w["percentage"]} for w in weekly_data]),
        }

    return {"error": "Failed to get weekly momentum"}


@app.post("/api/briefing/generate", response_class=HTMLResponse)
async def api_generate_briefing(request: Request):
    """Generate morning briefing."""
    from cognitex.agent.core import CognitexAgent

    agent = CognitexAgent()
    briefing = await agent.morning_briefing()

    # Convert markdown to HTML safely
    try:
        import markdown
        briefing_html = markdown.markdown(briefing, extensions=['tables', 'fenced_code'])
    except ImportError:
        # Fallback: escape and wrap in pre tag
        briefing_html = f"<pre style='white-space: pre-wrap;'>{html.escape(briefing)}</pre>"

    return HTMLResponse(f'<div class="briefing-content">{briefing_html}</div>')


# -------------------------------------------------------------------
# Chat
# -------------------------------------------------------------------


@app.get("/chat")
async def chat_page():
    """Redirect /chat to / (chat is now the landing page)."""
    return RedirectResponse(url="/", status_code=302)


@app.post("/api/chat")
async def api_chat(request: Request):
    """Send a message to the agent and get a response."""
    from cognitex.agent.core import Agent

    data = await request.json()
    message = data.get("message", "").strip()

    if not message:
        return JSONResponse({"error": "Message cannot be empty"}, status_code=400)

    # Intercept slash commands before agent
    if message.startswith("/"):
        from cognitex.agent.slash_commands import get_slash_registry

        registry = get_slash_registry()
        if not registry._initialized:
            await registry.initialize()
        result = await registry.dispatch(message)
        if result.handled:
            return JSONResponse({
                "response": result.response,
                "approvals": [],
                "is_command": True,
            })

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


@app.get("/api/chat/history")
async def api_chat_history():
    """Return the current session's chat history from working memory."""
    import redis.asyncio as aioredis

    settings = get_settings()
    redis = aioredis.from_url(settings.redis_url)

    try:
        data = await redis.get("cognitex:memory:working:context")
        if data:
            context = json.loads(data)
            interactions = context.get("interactions", [])
            # Return only role + content for rendering
            return JSONResponse({
                "interactions": [
                    {"role": i["role"], "content": i["content"]}
                    for i in interactions
                ],
            })
        return JSONResponse({"interactions": []})
    except Exception as e:
        return JSONResponse({"interactions": [], "error": str(e)})
    finally:
        await redis.close()


@app.get("/api/chat/commands")
async def api_chat_commands():
    """List available slash commands for the chat command helper."""
    from cognitex.agent.slash_commands import get_slash_registry

    registry = get_slash_registry()
    if not registry._initialized:
        await registry.initialize()
    commands = registry.list_commands()
    return JSONResponse(commands)


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
async def settings_page(request: Request, tab: str = "general"):
    """Unified settings page with tabs for General, Agent, State, Integrations."""
    from cognitex.services.model_config import get_model_config_service, MODEL_ALIASES
    from cognitex.db.neo4j import get_driver, get_neo4j_session
    from cognitex.db.postgres import get_session
    from sqlalchemy import text

    # Initialize all template variables
    template_data = {
        "request": request,
        "tab": tab,
    }

    # --- General Tab Data ---
    service = get_model_config_service()
    config = await service.get_config()
    chat_models = service.get_chat_models_for_provider(config.provider)
    embedding_models = service.get_embedding_models_for_provider(config.embedding_provider)
    providers = service.get_available_providers()

    import redis.asyncio as aioredis
    from cognitex.config import get_settings
    app_settings = get_settings()
    redis_client = aioredis.from_url(app_settings.redis_url)
    try:
        has_redis_config = await redis_client.exists("cognitex:model_config")
        config_source = "redis" if has_redis_config else "env"
    except Exception:
        config_source = "env"
    finally:
        await redis_client.close()

    sync_api_key = app_settings.sync_api_key.get_secret_value()
    sync_session_count = 0
    sync_machine_count = 0

    try:
        driver = get_driver()
        async with driver.session() as neo_session:
            result = await neo_session.run("""
                MATCH (cs:CodingSession)
                RETURN count(cs) as count,
                       count(DISTINCT split(cs.session_id, ':')[0]) as machines
            """)
            record = await result.single()
            if record:
                sync_session_count = record["count"]
                sync_machine_count = record["machines"]
    except Exception:
        pass

    template_data.update({
        "config": config,
        "config_source": config_source,
        "chat_models": chat_models,
        "embedding_models": embedding_models,
        "providers": providers,
        "sync_api_key": sync_api_key,
        "sync_session_count": sync_session_count,
        "sync_machine_count": sync_machine_count,
        "model_aliases": MODEL_ALIASES,
    })

    # --- Agent Tab Data ---
    approved_drafts = 0
    approved_tasks = 0
    learning_samples = 0
    twin_settings = {}
    recent_emails = []

    async for session in get_session():
        try:
            result = await session.execute(text("""
                SELECT COUNT(*) FROM inbox_feedback
                WHERE item_type = 'email_draft' AND action = 'approved'
            """))
            row = result.fetchone()
            approved_drafts = row[0] if row else 0

            result = await session.execute(text("""
                SELECT COUNT(*) FROM inbox_feedback
                WHERE item_type = 'task_proposal' AND action = 'approved'
            """))
            row = result.fetchone()
            approved_tasks = row[0] if row else 0

            result = await session.execute(text("""
                SELECT COUNT(*) FROM inbox_feedback
            """))
            row = result.fetchone()
            learning_samples = row[0] if row else 0

            result = await session.execute(text("""
                SELECT preference FROM preference_rules
                WHERE rule_type = 'twin_settings'
                LIMIT 1
            """))
            row = result.fetchone()
            if row and row[0]:
                twin_settings = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        except Exception as e:
            logger.debug("Could not get agent tab stats", error=str(e))
        break

    async for neo_session in get_neo4j_session():
        try:
            result = await neo_session.run("""
                MATCH (d:EmailDraft)
                WHERE d.status = 'approved'
                RETURN d.to as to, d.subject as subject, d.body as body,
                       d.approved_at as approved_at
                ORDER BY d.approved_at DESC
                LIMIT 5
            """)
            recent_emails = await result.data()
        except Exception:
            pass
        break

    template_data.update({
        "agent_stats": {
            "approved_drafts": approved_drafts,
            "approved_tasks": approved_tasks,
            "learning_samples": learning_samples,
        },
        "twin_settings": twin_settings,
        "recent_emails": recent_emails,
    })

    # --- State Tab Data ---
    estimator = get_state_estimator()
    calendar_events = await get_today_events()
    state = await estimator.infer_state(calendar_events=calendar_events)

    if not state:
        state = UserState(
            mode=OperatingMode.FRAGMENTED,
            signals=ContinuousSignals(),
        )

    rules = ModeRules.get_rules(state.mode)
    mode_description = rules.get("description", "")

    firewall = get_interruption_firewall()
    captured_items = await firewall.get_queued_items(limit=10)
    switch_stats = await firewall.get_daily_switch_stats()

    captured_dicts = [
        {
            "subject": item.subject,
            "source": item.source,
            "urgency": item.urgency.value,
            "suggested_action": item.suggested_action,
        }
        for item in captured_items
    ]

    template_data.update({
        "state": state,
        "rules": rules,
        "mode_description": mode_description,
        "available_modes": list(OperatingMode),
        "captured_items": captured_dicts,
        "switch_stats": switch_stats,
    })

    # --- Integrations Tab: AgentMail ---
    template_data.update({
        "agentmail_enabled": app_settings.agentmail_enabled,
        "agentmail_inbox_id": app_settings.agentmail_inbox_id,
        "agentmail_has_key": bool(app_settings.agentmail_api_key.get_secret_value()),
        "agentmail_has_webhook_secret": bool(
            app_settings.agentmail_webhook_secret.get_secret_value()
        ),
    })

    # --- Integrations Tab: Clinical Firewall ---
    firewall_pattern_count = 0
    firewall_category_count = 0
    try:
        from cognitex.services.clinical_firewall import get_firewall
        fw = get_firewall()
        firewall_category_count = len(fw._compiled)
        firewall_pattern_count = sum(len(v) for v in fw._compiled.values())
    except Exception:
        pass

    template_data.update({
        "firewall_enabled": app_settings.clinical_firewall_enabled,
        "firewall_mode": app_settings.clinical_firewall_mode,
        "firewall_pattern_count": firewall_pattern_count,
        "firewall_category_count": firewall_category_count,
        "firewall_patterns_path": app_settings.clinical_firewall_patterns_path,
    })

    return templates.TemplateResponse("settings.html", template_data)


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
    # Anthropic, Google, and OpenRouter don't have embeddings, so use Together
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

    valid_providers = {"together", "anthropic", "openai", "google", "openrouter"}
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
        # OpenRouter presets
        "openrouter-claude": ModelConfig(
            provider="openrouter",
            planner_model="anthropic/claude-sonnet-4",
            executor_model="anthropic/claude-sonnet-4",
            embedding_model="BAAI/bge-base-en-v1.5",
            embedding_provider="together",
        ),
        "openrouter-deepseek": ModelConfig(
            provider="openrouter",
            planner_model="deepseek/deepseek-r1",
            executor_model="deepseek/deepseek-chat-v3-0324",
            embedding_model="BAAI/bge-base-en-v1.5",
            embedding_provider="together",
        ),
        "openrouter-grok": ModelConfig(
            provider="openrouter",
            planner_model="x-ai/grok-3-mini-beta",
            executor_model="x-ai/grok-3-mini-beta",
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


@app.post("/api/settings/models/routing", response_class=HTMLResponse)
async def update_model_routing(request: Request):
    """Update per-task model routing overrides."""
    from cognitex.services.model_config import (
        TASK_MODEL_SLOTS,
        get_model_config_service,
    )

    form = await request.form()
    service = get_model_config_service()
    config = await service.get_config()

    for slot in TASK_MODEL_SLOTS:
        value = form.get(f"{slot}_model", "").strip()
        setattr(config, f"{slot}_model", value)

    await service.set_config(config)

    return HTMLResponse('<span style="color: #16a34a;">Routing saved</span>')


# =============================================================================
# Models & Sub-Agents Page
# =============================================================================


@app.get("/models", response_class=HTMLResponse)
async def models_page(request: Request):
    """Models & Sub-Agents management page."""
    from cognitex.services.model_config import (
        MODEL_ALIASES,
        get_model_config_service,
    )
    from cognitex.agent.subagent import get_subagent_registry
    from cognitex.agent.tools import get_tool_registry

    from cognitex.services.model_config import PROVIDERS

    service = get_model_config_service()
    config = await service.get_config()
    chat_models = service.get_chat_models_for_provider(config.provider)
    providers = service.get_available_providers()

    # Build all-provider model map for grouped dropdowns
    all_chat_models: dict[str, list[dict]] = {}
    for p_id in ["anthropic", "openai", "google", "together", "openrouter"]:
        models = service.get_chat_models_for_provider(p_id)
        if models:
            all_chat_models[PROVIDERS.get(p_id, p_id)] = models

    registry = get_subagent_registry()
    subagents = await registry.get_all()

    tool_registry = get_tool_registry()
    tool_names = sorted(t.name for t in tool_registry.all())

    return templates.TemplateResponse(
        "models.html",
        {
            "request": request,
            "config": config.to_dict(),
            "chat_models": chat_models,
            "all_chat_models": all_chat_models,
            "providers": providers,
            "model_aliases": MODEL_ALIASES,
            "subagents": subagents,
            "tool_names": tool_names,
        },
    )


@app.post("/api/models/core", response_class=HTMLResponse)
async def update_core_models(request: Request):
    """Update orchestrator (planner) and executor models."""
    from cognitex.services.llm import reset_llm_service
    from cognitex.services.model_config import get_model_config_service

    form = await request.form()
    service = get_model_config_service()
    config = await service.get_config()

    provider = form.get("provider", "").strip()
    planner = form.get("planner_model", "").strip()
    executor = form.get("executor_model", "").strip()

    if provider:
        config.provider = provider
    if planner:
        config.planner_model = planner
    if executor:
        config.executor_model = executor

    await service.set_config(config)
    reset_llm_service()

    return HTMLResponse('<span style="color: #16a34a;">Saved</span>')


@app.get("/api/models/subagents/{name}", response_class=JSONResponse)
async def get_subagent(name: str):
    """Get a sub-agent config as JSON."""
    from cognitex.agent.subagent import get_subagent_registry

    registry = get_subagent_registry()
    agent = await registry.get(name)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Sub-agent '{name}' not found")
    return JSONResponse(agent.to_dict())


@app.get("/api/models/subagents/{name}/edit", response_class=HTMLResponse)
async def edit_subagent_form(request: Request, name: str):
    """Render the sub-agent edit form partial."""
    from cognitex.services.model_config import MODEL_ALIASES, get_model_config_service
    from cognitex.agent.subagent import SubAgentConfig, get_subagent_registry
    from cognitex.agent.tools import get_tool_registry

    service = get_model_config_service()
    providers = service.get_available_providers()

    tool_registry = get_tool_registry()
    tool_names = sorted(t.name for t in tool_registry.all())

    is_new = name == "_new"
    if is_new:
        agent = SubAgentConfig(name="", purpose="")
    else:
        registry = get_subagent_registry()
        agent = await registry.get(name)
        if not agent:
            raise HTTPException(status_code=404, detail=f"Sub-agent '{name}' not found")

    return templates.TemplateResponse(
        "partials/subagent_edit.html",
        {
            "request": request,
            "agent": agent,
            "is_new": is_new,
            "model_aliases": MODEL_ALIASES,
            "providers": providers,
            "tool_names": tool_names,
        },
    )


@app.post("/api/models/subagents", response_class=HTMLResponse)
async def save_subagent(request: Request):
    """Create or update a user-defined sub-agent."""
    from cognitex.agent.subagent import SubAgentConfig, get_subagent_registry, BUILTIN_SUBAGENTS

    form = await request.form()
    name = form.get("name", "").strip()
    if not name:
        return HTMLResponse('<span style="color: var(--danger);">Name is required</span>')

    # For builtins, only allow model/provider changes
    if name in BUILTIN_SUBAGENTS:
        model = form.get("model", "").strip()
        provider = form.get("provider", "").strip()
        registry = get_subagent_registry()
        if model:
            await registry.update_builtin_model(name, model)
        return HTMLResponse(
            f'<span style="color: #16a34a;">Updated {name} model override</span>'
        )

    allowed = form.getlist("allowed_tools")
    denied = form.getlist("denied_tools")

    config = SubAgentConfig(
        name=name,
        purpose=form.get("purpose", "").strip(),
        model=form.get("model", "").strip(),
        provider=form.get("provider", "").strip(),
        allowed_tools=allowed,
        denied_tools=denied,
        max_iterations=int(form.get("max_iterations", "5")),
        system_prompt_extra=form.get("system_prompt_extra", "").strip(),
    )

    registry = get_subagent_registry()
    await registry.save_user_agent(config)

    return HTMLResponse(f'<span style="color: #16a34a;">Saved {name}</span>')


@app.delete("/api/models/subagents/{name}", response_class=HTMLResponse)
async def delete_subagent(request: Request, name: str):
    """Delete a user-defined sub-agent."""
    from cognitex.agent.subagent import get_subagent_registry

    registry = get_subagent_registry()
    try:
        deleted = await registry.delete_user_agent(name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not deleted:
        raise HTTPException(status_code=404, detail=f"Sub-agent '{name}' not found")

    # Redirect to /models to refresh the page
    return RedirectResponse(url="/models", status_code=303)


# =============================================================================
# Session Sync API (for cognitex-sync clients)
# =============================================================================


def verify_sync_api_key(authorization: str = Header(None)) -> bool:
    """Verify the sync API key from Authorization header."""
    import hmac
    from cognitex.config import get_settings

    settings = get_settings()
    expected_key = settings.sync_api_key.get_secret_value()

    if not expected_key:
        raise HTTPException(
            status_code=503,
            detail="Sync API not configured. Set SYNC_API_KEY in environment.",
        )

    if not authorization:
        raise HTTPException(
            status_code=401,
            detail="Missing Authorization header. Use: Authorization: Bearer <api_key>",
        )

    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Invalid Authorization format. Use: Bearer <api_key>",
        )

    provided_key = authorization[7:]  # Remove "Bearer " prefix
    # Use timing-safe comparison to prevent timing attacks
    if not hmac.compare_digest(provided_key, expected_key):
        raise HTTPException(status_code=403, detail="Invalid API key")

    return True


# NOTE: This endpoint is also available in api/routes/sync.py for the API server (port 8000).
# This duplicate exists so cognitex-sync clients can hit either port (8080 web or 8000 API).


async def _process_sync_batch_web(
    machine_id: str,
    cli_type: str,
    sessions: list[dict],
) -> None:
    """Process session sync in background - delegates to shared service method."""
    from cognitex.services.coding_sessions import get_session_ingester

    ingester = get_session_ingester()
    await ingester.process_sync_batch(machine_id, cli_type, sessions)


@app.post("/api/sync/sessions")
async def api_sync_sessions(
    request: Request,
    background_tasks: BackgroundTasks,
    _auth: bool = Depends(verify_sync_api_key),
):
    """
    Ingest coding sessions from remote machines.

    Processing happens in background to avoid HTTP timeouts on large batches.
    Returns immediately with 'accepted' status.

    Accepts JSON with session data:
    {
        "machine_id": "laptop-chris",
        "cli_type": "claude",
        "sessions": [
            {
                "session_id": "abc123",
                "project_path": "/Users/chris/projects/myapp",
                "git_branch": "main",
                "started_at": "2025-01-03T10:00:00Z",
                "ended_at": "2025-01-03T11:30:00Z",
                "messages": [...],  # Optional: raw messages for LLM extraction
                "summary": "...",   # Optional: pre-extracted summary
                "decisions": [...],
                "next_steps": [...],
                "topics": [...],
            }
        ]
    }
    """
    try:
        data = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")

    machine_id = data.get("machine_id", "unknown")
    cli_type = data.get("cli_type", "claude")
    sessions = data.get("sessions", [])

    if not sessions:
        return {"status": "ok", "message": "No sessions to ingest", "queued": 0}

    # Offload processing to background task to prevent HTTP timeout
    background_tasks.add_task(_process_sync_batch_web, machine_id, cli_type, sessions)

    logger.info(
        "Session sync accepted",
        machine_id=machine_id,
        session_count=len(sessions),
    )

    return {
        "status": "accepted",
        "message": "Processing started in background",
        "machine_id": machine_id,
        "queued": len(sessions),
    }


@app.get("/api/sync/status")
async def api_sync_status(_auth: bool = Depends(verify_sync_api_key)):
    """Check sync API status and get server info."""
    from cognitex.db.neo4j import get_driver

    driver = get_driver()
    async with driver.session() as session:
        result = await session.run(
            "MATCH (cs:CodingSession) RETURN count(cs) as count"
        )
        record = await result.single()
        session_count = record["count"] if record else 0

    return {
        "status": "ok",
        "version": "1.0.0",
        "total_sessions": session_count,
    }


@app.get("/downloads/cognitex-sync-install.sh", name="download_sync_installer")
async def download_sync_installer(request: Request):
    """Download the cognitex-sync installer script."""
    from fastapi.responses import PlainTextResponse

    # Get the server URL and API key for the install script
    from cognitex.config import get_settings
    settings = get_settings()
    sync_api_key = settings.sync_api_key.get_secret_value()

    script = f'''#!/bin/bash
# cognitex-sync installer
# Usage: curl -sSL {request.base_url}downloads/cognitex-sync-install.sh | bash

set -e

echo "Installing cognitex-sync..."

# Check for Python
if ! command -v python3 &> /dev/null; then
    echo "Error: Python 3 is required but not installed."
    exit 1
fi

# Create temp directory and download package
TMPDIR=$(mktemp -d)
cd "$TMPDIR"

echo "Downloading package..."
curl -sSL "{request.base_url}downloads/cognitex-sync.tar.gz" -o cognitex-sync.tar.gz
tar xzf cognitex-sync.tar.gz
cd cognitex-sync

echo "Installing..."
pip install --user -q .

# Add to PATH if needed
if [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
    export PATH="$HOME/.local/bin:$PATH"
fi

# Cleanup
cd /
rm -rf "$TMPDIR"

echo ""
echo "cognitex-sync installed successfully!"
echo ""
echo "Configure with:"
echo "  cognitex-sync configure --server {request.base_url} --api-key YOUR_API_KEY"
echo ""
echo "Then sync your sessions:"
echo "  cognitex-sync push"
'''

    return PlainTextResponse(
        content=script,
        media_type="text/x-shellscript",
        headers={"Content-Disposition": "attachment; filename=cognitex-sync-install.sh"}
    )


@app.get("/downloads/cognitex-sync.tar.gz", name="download_sync_package")
async def download_sync_package():
    """Download the cognitex-sync package as a tarball."""
    import tarfile
    import io
    from pathlib import Path

    # Find the cognitex-sync package directory
    package_dir = Path(__file__).parent.parent.parent.parent / "tools" / "cognitex-sync"

    if not package_dir.exists():
        raise HTTPException(status_code=404, detail="Package not found")

    # Create tarball in memory
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for file_path in package_dir.rglob("*"):
            if file_path.is_file() and "__pycache__" not in str(file_path):
                arcname = f"cognitex-sync/{file_path.relative_to(package_dir)}"
                tar.add(file_path, arcname=arcname)

    buffer.seek(0)

    from fastapi.responses import StreamingResponse
    return StreamingResponse(
        buffer,
        media_type="application/gzip",
        headers={"Content-Disposition": "attachment; filename=cognitex-sync.tar.gz"}
    )


# -------------------------------------------------------------------
# Session Linking (Human-in-the-loop fuzzy matching)
# -------------------------------------------------------------------


def _fuzzy_match_score(s1: str, s2: str) -> float:
    """Calculate fuzzy match score between two strings."""
    from difflib import SequenceMatcher
    s1_lower = s1.lower()
    s2_lower = s2.lower()

    # Direct containment is a strong signal
    if s2_lower in s1_lower:
        return 0.9 + (len(s2) / len(s1)) * 0.1

    # Use SequenceMatcher for fuzzy matching
    return SequenceMatcher(None, s1_lower, s2_lower).ratio()


def _extract_path_components(path: str) -> list[str]:
    """Extract meaningful components from a path for matching."""
    # Normalize and split
    path = path.replace("//", "/").rstrip("/")
    parts = path.split("/")
    # Filter out common non-meaningful parts
    skip = {"home", "chris", "projects", "Documents", "codex", ""}
    return [p for p in parts if p not in skip]


@app.get("/sessions/link", response_class=HTMLResponse)
async def sessions_link_page(request: Request):
    """Page to review and approve session-to-project links."""
    from cognitex.db.neo4j import get_driver

    driver = get_driver()
    async with driver.session() as session:
        # Get unlinked sessions
        unlinked_result = await session.run("""
            MATCH (cs:CodingSession)
            WHERE NOT (cs)-[:DEVELOPS]->(:Project)
            RETURN cs.session_id as session_id,
                   cs.project_path as project_path,
                   cs.summary as summary,
                   cs.ended_at as ended_at,
                   cs.cli_type as cli_type
            ORDER BY cs.ended_at DESC
            LIMIT 100
        """)
        unlinked_sessions = await unlinked_result.data()

        # Get all projects for matching
        projects_result = await session.run("""
            MATCH (p:Project)
            RETURN p.id as id, p.title as title, p.status as status
            ORDER BY p.title
        """)
        projects = await projects_result.data()

    # Calculate fuzzy matches for each unlinked session
    suggestions = []
    for sess in unlinked_sessions:
        path = sess.get("project_path", "")
        path_components = _extract_path_components(path)
        path_str = "/".join(path_components).lower()

        matches = []
        for proj in projects:
            title = proj.get("title", "")
            if not title:
                continue

            # Score based on path components
            best_score = 0
            for component in path_components:
                score = _fuzzy_match_score(component, title)
                best_score = max(best_score, score)

            # Also try full path match
            full_score = _fuzzy_match_score(path_str, title)
            best_score = max(best_score, full_score)

            if best_score >= 0.4:  # Threshold for showing as suggestion
                matches.append({
                    "project_id": proj["id"],
                    "project_title": title,
                    "score": best_score,
                    "status": proj.get("status", ""),
                })

        # Sort by score descending
        matches.sort(key=lambda x: x["score"], reverse=True)

        suggestions.append({
            "session_id": sess["session_id"],
            "project_path": path,
            "summary": sess.get("summary", "")[:200] if sess.get("summary") else "",
            "ended_at": sess.get("ended_at", ""),
            "cli_type": sess.get("cli_type", "claude"),
            "matches": matches[:5],  # Top 5 suggestions
        })

    return templates.TemplateResponse(
        "sessions_link.html",
        {
            "request": request,
            "suggestions": suggestions,
            "projects": projects,
            "total_unlinked": len(unlinked_sessions),
        },
    )


@app.post("/api/sessions/link", response_class=HTMLResponse)
async def api_sessions_link(
    session_id: Annotated[str, Form()],
    project_id: Annotated[str, Form()],
):
    """Approve a session-to-project link."""
    from cognitex.db.neo4j import get_driver

    driver = get_driver()
    async with driver.session() as session:
        result = await session.run("""
            MATCH (cs:CodingSession {session_id: $session_id})
            MATCH (p:Project {id: $project_id})
            MERGE (cs)-[:DEVELOPS]->(p)
            RETURN p.title as project_title
        """, session_id=session_id, project_id=project_id)
        record = await result.single()

        if record:
            return HTMLResponse(
                f'<span class="badge badge-active">Linked to {record["project_title"]}</span>'
            )
        else:
            return HTMLResponse(
                '<span class="badge badge-error">Link failed</span>',
                status_code=400
            )


@app.post("/api/sessions/link-bulk", response_class=JSONResponse)
async def api_sessions_link_bulk(request: Request):
    """Approve multiple session-to-project links at once."""
    from cognitex.db.neo4j import get_driver

    data = await request.json()
    links = data.get("links", [])

    driver = get_driver()
    linked = 0

    async with driver.session() as session:
        for link in links:
            session_id = link.get("session_id")
            project_id = link.get("project_id")
            if session_id and project_id:
                await session.run("""
                    MATCH (cs:CodingSession {session_id: $session_id})
                    MATCH (p:Project {id: $project_id})
                    MERGE (cs)-[:DEVELOPS]->(p)
                """, session_id=session_id, project_id=project_id)
                linked += 1

    return {"status": "ok", "linked": linked}


@app.post("/api/sessions/skip", response_class=HTMLResponse)
async def api_sessions_skip(session_id: Annotated[str, Form()]):
    """Mark a session as skipped (no project link needed)."""
    # For now, just return a visual indicator - could store in Redis if we want persistence
    return HTMLResponse('<span class="badge">Skipped</span>')


# -------------------------------------------------------------------
# Ideas
# -------------------------------------------------------------------


@app.get("/ideas", response_class=HTMLResponse)
async def ideas_page(request: Request, status: str | None = None):
    """Ideas capture and triage page."""
    from cognitex.services.ideas import list_ideas, get_idea_stats

    # Default to showing pending ideas
    filter_status = status if status else None
    ideas = await list_ideas(status=filter_status)
    stats = await get_idea_stats()

    # Get projects for the convert-to-task dropdown
    project_service = get_project_service()
    projects = await project_service.list(limit=100)

    return templates.TemplateResponse(
        "ideas.html",
        {
            "request": request,
            "ideas": ideas,
            "stats": stats,
            "projects": projects,
            "current_status": status,
        },
    )


@app.post("/ideas", response_class=HTMLResponse)
async def create_idea_web(
    request: Request,
    text: Annotated[str, Form()],
):
    """Create a new idea from web form."""
    from cognitex.services.ideas import create_idea, list_ideas, get_idea_stats

    await create_idea(text=text, source="web")

    # Return updated list
    ideas = await list_ideas()
    stats = await get_idea_stats()
    project_service = get_project_service()
    projects = await project_service.list(limit=100)

    return templates.TemplateResponse(
        "partials/ideas_list.html",
        {
            "request": request,
            "ideas": ideas,
            "stats": stats,
            "projects": projects,
        },
    )


@app.post("/api/ideas", response_class=JSONResponse)
async def create_idea_api(request: Request):
    """Create a new idea via API (for mobile shortcuts, etc.)."""
    from cognitex.services.ideas import create_idea

    data = await request.json()
    text = data.get("text", "").strip()

    if not text:
        return JSONResponse({"error": "Text is required"}, status_code=400)

    idea = await create_idea(
        text=text,
        source="api",
        tags=data.get("tags"),
    )

    return JSONResponse({"status": "ok", "idea": idea})


@app.post("/ideas/{idea_id}/convert", response_class=HTMLResponse)
async def convert_idea_to_task(
    request: Request,
    idea_id: str,
    title: Annotated[str | None, Form()] = None,
    project_id: Annotated[str | None, Form()] = None,
    priority: Annotated[str, Form()] = "medium",
):
    """Convert an idea to a task."""
    from cognitex.services.ideas import convert_to_task, list_ideas, get_idea_stats

    task = await convert_to_task(
        idea_id=idea_id,
        title=title if title else None,
        project_id=project_id if project_id else None,
        priority=priority,
    )

    # Return updated list
    ideas = await list_ideas()
    stats = await get_idea_stats()
    project_service = get_project_service()
    projects = await project_service.list(limit=100)

    return templates.TemplateResponse(
        "partials/ideas_list.html",
        {
            "request": request,
            "ideas": ideas,
            "stats": stats,
            "projects": projects,
            "flash_message": f"Converted to task: {task['title']}" if task else "Conversion failed",
        },
    )


@app.post("/ideas/{idea_id}/dismiss", response_class=HTMLResponse)
async def dismiss_idea_route(request: Request, idea_id: str):
    """Dismiss an idea."""
    from cognitex.services.ideas import dismiss_idea, list_ideas, get_idea_stats

    await dismiss_idea(idea_id)

    # Return updated list
    ideas = await list_ideas()
    stats = await get_idea_stats()
    project_service = get_project_service()
    projects = await project_service.list(limit=100)

    return templates.TemplateResponse(
        "partials/ideas_list.html",
        {
            "request": request,
            "ideas": ideas,
            "stats": stats,
            "projects": projects,
        },
    )


@app.delete("/ideas/{idea_id}", response_class=HTMLResponse)
async def delete_idea_route(request: Request, idea_id: str):
    """Delete an idea permanently."""
    from cognitex.services.ideas import delete_idea, list_ideas, get_idea_stats

    await delete_idea(idea_id)

    # Return updated list
    ideas = await list_ideas()
    stats = await get_idea_stats()
    project_service = get_project_service()
    projects = await project_service.list(limit=100)

    return templates.TemplateResponse(
        "partials/ideas_list.html",
        {
            "request": request,
            "ideas": ideas,
            "stats": stats,
            "projects": projects,
        },
    )


@app.post("/ideas/{idea_id}/link", response_class=HTMLResponse)
async def link_idea_to_project(
    request: Request,
    idea_id: str,
    project_id: Annotated[str, Form()],
):
    """Link an idea to a project."""
    from cognitex.services.ideas import link_to_project, list_ideas, get_idea_stats

    await link_to_project(idea_id, project_id)

    # Return updated list
    ideas = await list_ideas()
    stats = await get_idea_stats()
    project_service = get_project_service()
    projects = await project_service.list(limit=100)

    return templates.TemplateResponse(
        "partials/ideas_list.html",
        {
            "request": request,
            "ideas": ideas,
            "stats": stats,
            "projects": projects,
        },
    )


# -------------------------------------------------------------------
# Help Page
# -------------------------------------------------------------------


@app.get("/help", response_class=HTMLResponse)
async def help_page(request: Request):
    """Help page with usage information and learning stats."""
    from cognitex.db.postgres import get_session
    from sqlalchemy import text

    stats = {}

    async for session in get_session():
        try:
            # Style profiles count
            result = await session.execute(text(
                "SELECT COUNT(*) FROM email_style_profiles"
            ))
            row = result.fetchone()
            stats["style_profiles"] = row[0] if row else 0

            # Response decisions count
            result = await session.execute(text(
                "SELECT COUNT(*) FROM email_response_decisions"
            ))
            row = result.fetchone()
            stats["response_decisions"] = row[0] if row else 0

            # Draft tracking stats
            result = await session.execute(text("""
                SELECT COUNT(*), AVG(edit_ratio)
                FROM email_draft_lifecycle
                WHERE status = 'sent'
            """))
            row = result.fetchone()
            stats["drafts_tracked"] = row[0] if row else 0
            stats["avg_edit_ratio"] = row[1] if row and row[1] else None

        except Exception:
            pass
        break

    return templates.TemplateResponse(
        "help.html",
        {"request": request, "stats": stats},
    )


# -------------------------------------------------------------------
# Maintenance Endpoints
# -------------------------------------------------------------------


@app.post("/api/maintenance/clear-old-inbox", response_class=HTMLResponse)
async def clear_old_inbox(request: Request):
    """Clear inbox items older than 7 days."""
    from cognitex.services.inbox import get_inbox_service

    inbox = get_inbox_service()
    cleared = await inbox.clear_old_items(days=7)

    return f"""
    <div class="alert alert-success" style="padding: 1rem; background: #166534; border-radius: 8px; margin: 1rem 0;">
        Cleared {cleared} old inbox items. <a href="/help">Back to Help</a>
    </div>
    """


@app.post("/api/maintenance/clear-old-drafts", response_class=HTMLResponse)
async def clear_old_drafts(request: Request):
    """Clear old email drafts from Neo4j."""
    from cognitex.db.neo4j import get_neo4j_session

    cleared = 0
    async for session in get_neo4j_session():
        try:
            result = await session.run("""
                MATCH (d:EmailDraft)
                WHERE d.created_at < datetime() - duration('P7D')
                DETACH DELETE d
                RETURN count(*) as deleted
            """)
            record = await result.single()
            cleared = record["deleted"] if record else 0
        except Exception:
            pass
        break

    return f"""
    <div class="alert alert-success" style="padding: 1rem; background: #166534; border-radius: 8px; margin: 1rem 0;">
        Cleared {cleared} old draft nodes. <a href="/help">Back to Help</a>
    </div>
    """


@app.post("/api/maintenance/clear-dismissed", response_class=HTMLResponse)
async def clear_dismissed_items(request: Request):
    """Clear all dismissed inbox items."""
    from cognitex.services.inbox import get_inbox_service

    inbox = get_inbox_service()
    cleared = await inbox.clear_dismissed()

    return f"""
    <div class="alert alert-success" style="padding: 1rem; background: #166534; border-radius: 8px; margin: 1rem 0;">
        Cleared {cleared} dismissed items. <a href="/help">Back to Help</a>
    </div>
    """


# =============================================================================
# Bootstrap Routes - Personality, Identity, Context editing
# =============================================================================


@app.get("/bootstrap", response_class=HTMLResponse)
async def bootstrap_page(request: Request):
    """Bootstrap files editor page."""
    from cognitex.agent.bootstrap import init_bootstrap, get_bootstrap_loader

    await init_bootstrap()
    loader = get_bootstrap_loader()
    files = await loader.get_all()

    return templates.TemplateResponse(
        "bootstrap.html",
        {
            "request": request,
            "files": files,
            "soul": files.get("SOUL"),
            "user": files.get("USER"),
            "agents": files.get("AGENTS"),
            "tools": files.get("TOOLS"),
            "memory": files.get("MEMORY"),
            "identity": files.get("IDENTITY"),
            "context": files.get("CONTEXT"),
        },
    )


@app.get("/api/bootstrap/{filename}", response_class=JSONResponse)
async def get_bootstrap_file(filename: str):
    """Get a bootstrap file's content."""
    from cognitex.agent.bootstrap import get_bootstrap_loader

    loader = get_bootstrap_loader()
    file = await loader.get_file(f"{filename.upper()}.md")

    if not file:
        raise HTTPException(status_code=404, detail="File not found")

    return {
        "name": file.name,
        "content": file.raw_content,
        "last_modified": file.last_modified.isoformat() if file.last_modified else None,
    }


@app.post("/api/bootstrap/{filename}", response_class=HTMLResponse)
async def save_bootstrap_file(
    request: Request,
    filename: str,
    content: Annotated[str, Form()],
):
    """Save a bootstrap file."""
    from cognitex.agent.bootstrap import get_bootstrap_loader

    valid_files = ["soul", "user", "agents", "tools", "memory", "identity", "context"]
    if filename.lower() not in valid_files:
        raise HTTPException(status_code=400, detail="Invalid filename")

    loader = get_bootstrap_loader()
    success = await loader.save_file(f"{filename.upper()}.md", content)

    if not success:
        return HTMLResponse('<div class="alert alert-danger">Failed to save</div>')

    return HTMLResponse(f'<div class="alert alert-success" style="padding: 0.5rem; background: var(--success-bg); border-radius: 6px;">Saved {filename.upper()}.md</div>')


@app.post("/api/bootstrap/test-voice", response_class=HTMLResponse)
async def test_bootstrap_voice(
    request: Request,
    to: Annotated[str, Form()] = "colleague@example.com",
    subject: Annotated[str, Form()] = "Test Email",
    instructions: Annotated[str, Form()] = "Write a brief follow-up",
):
    """Test email drafting with current bootstrap voice settings."""
    from cognitex.agent.executors import EmailExecutor

    executor = EmailExecutor()

    try:
        result = await executor._draft_email(
            args={
                "to": to,
                "subject": subject,
                "instructions": instructions,
            },
            reasoning="Testing bootstrap voice settings",
        )

        if result.success and result.data:
            body = result.data.get("body", "")
            return HTMLResponse(f"""
                <div style="background: var(--bg-card); padding: 1rem; border-radius: var(--radius); border: 1px solid var(--border);">
                    <div style="font-weight: 600; margin-bottom: 0.5rem;">Generated Draft:</div>
                    <div style="white-space: pre-wrap; font-family: var(--font-mono); font-size: 0.9rem;">{html.escape(body)}</div>
                </div>
            """)
        else:
            return HTMLResponse(f'<div class="alert alert-danger">Error: {result.error}</div>')
    except Exception as e:
        return HTMLResponse(f'<div class="alert alert-danger">Error: {str(e)}</div>')


# =============================================================================
# Skills Routes - Teachable agent behaviors
# =============================================================================


@app.get("/skills", response_class=HTMLResponse)
async def skills_page(request: Request):
    """Skills editor page."""
    from cognitex.agent.skills import init_skills, get_skills_loader

    await init_skills()
    loader = get_skills_loader()
    skills = await loader.list_skills()

    return templates.TemplateResponse(
        "skills.html",
        {
            "request": request,
            "skills": skills,
        },
    )


@app.get("/api/skills/{name}", response_class=JSONResponse)
async def get_skill(name: str):
    """Get a skill's content."""
    from cognitex.agent.skills import get_skills_loader

    loader = get_skills_loader()
    skill = await loader.get_skill(name)

    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")

    return {
        "name": skill.name,
        "content": skill.raw_content,
        "purpose": skill.purpose,
        "description": skill.description,
        "rules_count": len(skill.rules),
        "is_user_skill": skill.is_user_skill,
        "path": str(skill.path),
        "format": skill.format,
        "version": skill.version,
        "eligible": skill.eligible,
        "ineligibility_reason": skill.ineligibility_reason,
        "source": skill.source,
    }


@app.post("/api/skills/{name}", response_class=HTMLResponse)
async def save_skill(
    request: Request,
    name: str,
    content: Annotated[str, Form()],
):
    """Save a skill (creates user skill)."""
    from cognitex.agent.skills import get_skills_loader

    loader = get_skills_loader()
    success = await loader.save_skill(name, content)

    if not success:
        return HTMLResponse('<div class="alert alert-danger">Failed to save</div>')

    return HTMLResponse(f'<div class="alert alert-success" style="padding: 0.5rem; background: var(--success-bg); border-radius: 6px;">Saved skill: {name}</div>')


@app.delete("/api/skills/{name}", response_class=HTMLResponse)
async def delete_skill(name: str):
    """Delete a user skill."""
    from cognitex.agent.skills import get_skills_loader

    loader = get_skills_loader()
    success = await loader.delete_skill(name)

    if not success:
        return HTMLResponse('<div class="alert alert-warning">Cannot delete (only user skills can be deleted)</div>')

    return HTMLResponse(f'<div class="alert alert-success">Deleted skill: {name}</div>')


@app.post("/api/skills/test/{name}", response_class=HTMLResponse)
async def test_skill(
    request: Request,
    name: str,
    input_text: Annotated[str, Form()],
):
    """Test a skill with sample input."""
    from cognitex.agent.skills import get_skills_loader
    from cognitex.services.llm import get_llm_service

    loader = get_skills_loader()
    skill = await loader.get_skill(name)

    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")

    llm = get_llm_service()
    skill_prompt = loader.format_skill_for_prompt(skill)

    # Build test prompt
    prompt = f"""Apply the following skill to the given input.

## Skill: {skill.name}
{skill_prompt}

## Input:
{input_text}

## Task:
Based on the skill rules and examples, analyze the input and provide the appropriate output.
For task extraction skills, return a JSON array of tasks.
For other skills, return the expected output format.

Output:"""

    try:
        result = await llm.complete(prompt, max_tokens=1024, temperature=0.2)
        return HTMLResponse(f"""
            <div style="background: var(--bg-card); padding: 1rem; border-radius: var(--radius); border: 1px solid var(--border);">
                <div style="font-weight: 600; margin-bottom: 0.5rem;">Skill Output:</div>
                <div style="white-space: pre-wrap; font-family: var(--font-mono); font-size: 0.9rem;">{html.escape(result)}</div>
            </div>
        """)
    except Exception as e:
        return HTMLResponse(f'<div class="alert alert-danger">Error: {str(e)}</div>')


# --- Community skill registry routes ---


@app.get("/api/skills/community/search", response_class=JSONResponse)
async def community_skill_search(q: str = ""):
    """Search community skill registry."""
    from cognitex.services.skill_registry import get_skill_registry

    registry = get_skill_registry()
    results = await registry.search(q) if q else []
    return [
        {
            "slug": r.slug,
            "name": r.name,
            "description": r.description,
            "version": r.version,
            "installed": r.installed,
        }
        for r in results
    ]


@app.post("/api/skills/community/install/{slug}", response_class=HTMLResponse)
async def community_skill_install(slug: str):
    """Install a community skill."""
    from cognitex.services.skill_registry import get_skill_registry

    registry = get_skill_registry()
    success = await registry.install(slug)
    if success:
        return HTMLResponse(
            f'<div class="alert alert-success" style="padding: 0.5rem; background: var(--success-bg); border-radius: 6px;">Installed: {html.escape(slug)}</div>'
        )
    return HTMLResponse(
        f'<div class="alert alert-danger">Failed to install {html.escape(slug)}. Run sync first.</div>'
    )


@app.post("/api/skills/community/sync", response_class=HTMLResponse)
async def community_skill_sync():
    """Sync the community skill registry."""
    from cognitex.services.skill_registry import get_skill_registry

    registry = get_skill_registry()
    try:
        count = await registry.sync_registry()
        return HTMLResponse(
            f'<div class="alert alert-success" style="padding: 0.5rem; background: var(--success-bg); border-radius: 6px;">Registry synced — {count} skill(s) available.</div>'
        )
    except RuntimeError as e:
        return HTMLResponse(f'<div class="alert alert-danger">{html.escape(str(e))}</div>')


# =============================================================================
# Skill Authoring Routes - AI-assisted skill creation
# =============================================================================


@app.post("/api/skills/author/create", response_class=JSONResponse)
async def skill_author_create(
    request: Request,
    description: str = Form(...),
    name: str = Form(None),
):
    """Generate a skill draft from a description."""
    from cognitex.agent.skill_authoring import get_skill_authoring

    authoring = get_skill_authoring()
    draft = await authoring.create_from_description(
        description=description,
        name=name or None,
    )
    return {
        "name": draft.name,
        "description": draft.description,
        "content": draft.content,
        "version": draft.version,
        "status": draft.status,
    }


@app.post("/api/skills/author/refine", response_class=JSONResponse)
async def skill_author_refine(
    request: Request,
    name: str = Form(...),
    content: str = Form(...),
    feedback: str = Form(...),
    version: int = Form(1),
):
    """Refine a skill draft with feedback."""
    from cognitex.agent.skill_authoring import SkillDraft, get_skill_authoring

    authoring = get_skill_authoring()
    draft = SkillDraft(
        name=name,
        description="",
        content=content,
        version=version,
    )
    refined = await authoring.refine_draft(draft, feedback)
    return {
        "name": refined.name,
        "description": refined.description,
        "content": refined.content,
        "version": refined.version,
        "status": refined.status,
    }


@app.post("/api/skills/author/test", response_class=JSONResponse)
async def skill_author_test(
    request: Request,
    name: str = Form(...),
    content: str = Form(...),
    test_input: str = Form(...),
):
    """Test a skill draft against input."""
    from cognitex.agent.skill_authoring import SkillDraft, get_skill_authoring

    authoring = get_skill_authoring()
    draft = SkillDraft(name=name, description="", content=content)
    results = await authoring.test_skill(draft, [test_input])
    return [
        {
            "input": r.input_text,
            "output": r.output_text,
            "success": r.success,
            "error": r.error,
        }
        for r in results
    ]


@app.post("/api/skills/author/deploy", response_class=HTMLResponse)
async def skill_author_deploy(
    request: Request,
    name: str = Form(...),
    content: str = Form(...),
):
    """Deploy a skill draft."""
    from cognitex.agent.skill_authoring import SkillDraft, get_skill_authoring

    authoring = get_skill_authoring()
    draft = SkillDraft(name=name, description="", content=content)
    success = await authoring.deploy_skill(draft)
    if success:
        return HTMLResponse(
            f'<span style="color: var(--success);">Deployed \'{html.escape(name)}\'</span>'
        )
    return HTMLResponse('<span style="color: var(--danger);">Deployment failed</span>')


# =============================================================================
# Skill Evolution Routes - Autonomous pattern detection & proposals
# =============================================================================


@app.get("/evolution", response_class=HTMLResponse)
async def evolution_page(request: Request):
    """Evolution dashboard page."""
    from cognitex.agent.skill_evolution import get_skill_evolution
    from cognitex.agent.skills import get_skills_loader

    evolution = get_skill_evolution()
    pending = await evolution._get_pending_proposals()
    history = await evolution._get_all_proposals(limit=50)
    feedback_summary = await evolution.get_feedback_summary()

    loader = get_skills_loader()
    all_skills = await loader.list_skills()
    skill_names = [s["name"] for s in all_skills]

    return templates.TemplateResponse(
        "evolution.html",
        {
            "request": request,
            "pending": pending,
            "history": [h for h in history if h["status"] != "proposed"],
            "feedback_summary": feedback_summary,
            "skill_names": skill_names,
        },
    )


@app.post("/api/evolution/review/{proposal_id}", response_class=HTMLResponse)
async def evolution_review(
    proposal_id: str,
    decision: str = Form(...),
    feedback: str = Form(None),
):
    """Review a pending proposal (approve/reject)."""
    from cognitex.agent.skill_evolution import get_skill_evolution

    evolution = get_skill_evolution()

    if decision == "approve":
        await evolution.review_proposal(proposal_id, "approved", feedback)
        success = await evolution.deploy_proposal(proposal_id)
        if success:
            return HTMLResponse(
                '<div style="color: var(--success); padding: 0.5rem;">Approved and deployed.</div>'
            )
        return HTMLResponse(
            '<div style="color: var(--warning); padding: 0.5rem;">Approved but deployment failed.</div>'
        )
    else:
        await evolution.review_proposal(proposal_id, "rejected", feedback)
        return HTMLResponse(
            '<div style="color: var(--text-muted); padding: 0.5rem;">Rejected.</div>'
        )


@app.post("/api/evolution/trigger", response_class=HTMLResponse)
async def evolution_trigger():
    """Manually trigger an evolution cycle."""
    from cognitex.agent.skill_evolution import get_skill_evolution

    evolution = get_skill_evolution()
    try:
        results = await evolution.run_evolution_cycle()
        count = len(results)
        if count:
            return HTMLResponse(
                f'<div style="color: var(--success); padding: 0.5rem;">'
                f'Analysis complete: {count} proposal(s) generated. Refresh to see them.</div>'
            )
        return HTMLResponse(
            '<div style="color: var(--text-muted); padding: 0.5rem;">'
            'Analysis complete: no new patterns detected.</div>'
        )
    except Exception as e:
        return HTMLResponse(
            f'<div style="color: var(--danger); padding: 0.5rem;">'
            f'Error: {html.escape(str(e)[:200])}</div>'
        )


@app.post("/api/evolution/feedback", response_class=HTMLResponse)
async def evolution_manual_feedback(
    skill_name: str = Form(...),
    feedback_type: str = Form(...),
    description: str = Form(...),
):
    """Submit manual feedback about a skill from the evolution dashboard."""
    from cognitex.agent.skill_feedback_router import submit_manual_feedback

    try:
        feedback_id = await submit_manual_feedback(skill_name, feedback_type, description)
        return HTMLResponse(
            f'<div style="color: var(--success); padding: 0.5rem;">'
            f'Feedback recorded ({feedback_id}). This will be considered in the next evolution cycle.</div>'
        )
    except ValueError as e:
        return HTMLResponse(
            f'<div style="color: var(--danger); padding: 0.5rem;">'
            f'Error: {html.escape(str(e))}</div>'
        )
    except Exception as e:
        return HTMLResponse(
            f'<div style="color: var(--danger); padding: 0.5rem;">'
            f'Error: {html.escape(str(e)[:200])}</div>'
        )


# =============================================================================
# Memory Routes - Daily logs and curated knowledge
# =============================================================================


@app.get("/memory", response_class=HTMLResponse)
async def memory_page(request: Request, date: str | None = None):
    """Memory browser page."""
    from datetime import date as date_type, datetime, timedelta
    from cognitex.services.memory_files import init_memory_files, get_memory_file_service

    await init_memory_files()
    service = get_memory_file_service()

    # Parse date or use today
    if date:
        try:
            target_date = datetime.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            target_date = date_type.today()
    else:
        target_date = date_type.today()

    # Get daily log
    daily_log = await service.get_daily_log(target_date)

    # Get recent logs for navigation
    recent_logs = await service.get_recent_logs(days=14)
    available_dates = [log.date for log in recent_logs]

    # Get curated memory
    curated = await service.get_curated_memory()

    return templates.TemplateResponse(
        "memory.html",
        {
            "request": request,
            "daily_log": daily_log,
            "target_date": target_date,
            "available_dates": available_dates,
            "curated_memory": curated,
            "entries": daily_log.entries if daily_log else [],
            "timedelta": timedelta,  # Pass timedelta for template use
        },
    )


@app.get("/api/memory/daily/{date_str}", response_class=JSONResponse)
async def get_daily_memory(date_str: str):
    """Get a daily memory log."""
    from datetime import datetime
    from cognitex.services.memory_files import get_memory_file_service

    service = get_memory_file_service()

    try:
        target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format")

    log = await service.get_daily_log(target_date)

    if not log:
        return {"date": date_str, "entries": []}

    return {
        "date": date_str,
        "entries": [
            {
                "id": e.id,
                "timestamp": e.timestamp.isoformat(),
                "category": e.category,
                "content": e.content,
                "tags": e.tags,
                "source": e.source,
            }
            for e in log.entries
        ],
    }


@app.post("/api/memory/write", response_class=HTMLResponse)
async def write_memory_entry(
    request: Request,
    content: Annotated[str, Form()],
    category: Annotated[str, Form()] = "User Note",
):
    """Write a new memory entry."""
    from cognitex.services.memory_files import get_memory_file_service

    service = get_memory_file_service()

    entry = await service.write_entry(
        content=content,
        category=category,
        source="user",
    )

    return HTMLResponse(f"""
        <div class="alert alert-success" style="padding: 0.5rem; background: var(--success-bg); border-radius: 6px;">
            Entry recorded (ID: {entry.id[:12]}...)
        </div>
    """)


@app.get("/api/memory/curated", response_class=JSONResponse)
async def get_curated_memory():
    """Get curated memory content."""
    from cognitex.services.memory_files import get_memory_file_service

    service = get_memory_file_service()
    content = await service.get_curated_memory()

    return {"content": content}


@app.post("/api/memory/curated", response_class=HTMLResponse)
async def save_curated_memory(
    request: Request,
    content: Annotated[str, Form()],
):
    """Save curated memory."""
    from cognitex.services.memory_files import get_memory_file_service

    service = get_memory_file_service()
    success = await service.save_curated_memory(content)

    if not success:
        return HTMLResponse('<div class="alert alert-danger">Failed to save</div>')

    return HTMLResponse('<div class="alert alert-success" style="padding: 0.5rem; background: var(--success-bg); border-radius: 6px;">Saved curated memory</div>')


@app.post("/api/memory/promote/{entry_id}", response_class=HTMLResponse)
async def promote_to_curated(entry_id: str):
    """Promote a daily entry to curated memory."""
    from cognitex.services.memory_files import get_memory_file_service

    service = get_memory_file_service()
    success = await service.promote_to_curated(entry_id)

    if not success:
        return HTMLResponse('<div class="alert alert-warning">Entry not found or promotion failed</div>')

    return HTMLResponse('<div class="alert alert-success" style="padding: 0.5rem; background: var(--success-bg); border-radius: 6px;">Promoted to long-term memory</div>')


@app.get("/api/memory/search", response_class=JSONResponse)
async def search_memory(
    q: str,
    days: int = 30,
):
    """Search memory entries."""
    from cognitex.services.memory_files import get_memory_file_service

    service = get_memory_file_service()
    results = await service.search_memories(query=q, days=days)

    return {
        "query": q,
        "results": [
            {
                "id": e.id,
                "timestamp": e.timestamp.isoformat(),
                "category": e.category,
                "content": e.content,
                "tags": e.tags,
            }
            for e in results[:50]
        ],
    }


def run_server(host: str = "127.0.0.1", port: int = 8080):
    """Run the web server."""
    import uvicorn
    uvicorn.run(app, host=host, port=port)
