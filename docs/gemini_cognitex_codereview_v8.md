Chatname: Code Review & Functionality Boost

I have performed a comprehensive review of the latest codebase, focusing on the interactions between the new `cognitex-sync` features, the autonomous agent, and the core services.

Here are **3 Critical Fixes** (logic errors/omissions) and **3 High-Value Improvements** to significantly increase system utility.

### 1. Critical Omission: Task Timing Logic in Service Layer
**The Issue:** The Phase 4 "Duration Calibration" relies on comparing `estimated_minutes` vs `actual_minutes`. While the database schema exists, the `TaskService` logic to automatically set `started_at` (when moving to in-progress) and calculate `actual_minutes` (when moving to done) is missing from the `update` method. Without this, the learning system receives no data.

**The Fix:** Update `src/cognitex/services/tasks.py`.

<file path="src/cognitex/services/tasks.py">
```python
<<<<
    async def update(
        self,
        task_id: str,
        title: str | None = None,
        description: str | None = None,
        status: str | None = None,
        priority: str | None = None,
        due_date: str | None = None,
        effort_estimate: float | None = None,
        energy_cost: str | None = None,
    ) -> dict | None:
        """
        Update task properties.

        Returns:
            Updated task dict or None if not found
        """
        async for session in get_neo4j_session():
            task = await gs.update_task(
                session,
                task_id=task_id,
                title=title,
                description=description,
                status=status,
                priority=priority,
                due_date=due_date,
                effort_estimate=effort_estimate,
                energy_cost=energy_cost,
            )

            if task:
                logger.info("Updated task", task_id=task_id)
            return task
====
    async def update(
        self,
        task_id: str,
        title: str | None = None,
        description: str | None = None,
        status: str | None = None,
        priority: str | None = None,
        due_date: str | None = None,
        effort_estimate: float | None = None,
        energy_cost: str | None = None,
    ) -> dict | None:
        """
        Update task properties.
        Handles automatic timing recording for learning system.
        """
        from cognitex.db.postgres import get_session
        from sqlalchemy import text
        
        # 1. Capture previous state for timing logic
        prev_task = await self.get(task_id)
        if not prev_task:
            return None

        # 2. Update Graph
        async for session in get_neo4j_session():
            task = await gs.update_task(
                session,
                task_id=task_id,
                title=title,
                description=description,
                status=status,
                priority=priority,
                due_date=due_date,
                effort_estimate=effort_estimate,
                energy_cost=energy_cost,
            )

        if not task:
            return None

        # 3. Handle Timing Logic (PostgreSQL)
        async for pg_session in get_session():
            # Start Timer: pending -> in_progress
            if status == "in_progress" and prev_task.get("status") != "in_progress":
                await pg_session.execute(text("""
                    UPDATE tasks 
                    SET started_at = NOW() 
                    WHERE id = :id AND started_at IS NULL
                """), {"id": task_id})
            
            # Stop Timer: in_progress -> done
            elif status == "done" and prev_task.get("status") != "done":
                # Record completion time
                await pg_session.execute(text("""
                    UPDATE tasks 
                    SET completed_at = NOW() 
                    WHERE id = :id
                """), {"id": task_id})
                
                # Calculate duration if we have a start time
                # Note: This logic assumes the record_task_timing function handles the math
                # and inserting into task_timing table, but we need to trigger it.
                if prev_task.get("started_at"):
                    from datetime import datetime
                    start_time = datetime.fromisoformat(str(prev_task["started_at"]))
                    # Calculate actual minutes
                    await self.record_task_timing(
                        task_id=task_id,
                        started_at=start_time,
                        completed_at=datetime.now(),
                        estimated_minutes=int(prev_task.get("effort_estimate") or 30),
                        project_id=task.get("project_id")
                    )

        logger.info("Updated task", task_id=task_id, status=status)
        return task
>>>>
```

### 2. Logic Error: Duplicate Email Draft Logic
**The Issue:** `src/cognitex/agent/autonomous.py` implements its own `_draft_email` method that creates a Neo4j node. `src/cognitex/agent/tools.py` has `DraftEmailTool` which *also* creates a Neo4j node and *also* stages it in Redis.
If the agent decides to draft an email via the tool registry (standard flow), it works fine. But if the autonomous loop calls its internal private method `_draft_email`, it might skip the Redis staging required for the Chat interface/CLI approval flow.

**The Fix:** Make `autonomous.py` use the `DraftEmailTool` directly rather than re-implementing the logic.

<file path="src/cognitex/agent/autonomous.py">
```python
<<<<
    async def _draft_email(self, session, params: dict, reason: str) -> dict | None:
        """
        Draft an email reply in the user's voice.

        The draft is stored in the graph for user review before sending.
        """
        import uuid

        email_id = params.get("email_id")
        to = params.get("to")
        subject = params.get("subject", "")
        body = params.get("body", "")
        original_subject = params.get("original_subject", "")

        if not email_id or not body:
            logger.warning("DRAFT_EMAIL missing required fields", params=params)
            return None

        draft_id = f"draft_{uuid.uuid4().hex[:12]}"

        # Store the draft in the graph, linked to the original email
        query = """
        MATCH (original:Email {gmail_id: $email_id})
        CREATE (draft:EmailDraft {
            id: $draft_id,
            to: $to,
            subject: $subject,
            body: $body,
            status: 'pending_review',
            created_at: datetime(),
            created_by: 'autonomous_agent',
            reason: $reason
        })
        CREATE (draft)-[:REPLY_TO]->(original)
        RETURN draft.id as id, original.subject as original_subject
        """
        try:
            # ... execution logic ...
====
    async def _draft_email(self, session, params: dict, reason: str) -> dict | None:
        """
        Draft an email reply in the user's voice.
        Delegates to DraftEmailTool to ensure consistent storage (Graph + Redis).
        """
        from cognitex.agent.tools import DraftEmailTool
        
        email_id = params.get("email_id")
        if not email_id or not params.get("body"):
             logger.warning("DRAFT_EMAIL missing required fields", params=params)
             return None

        tool = DraftEmailTool()
        result = await tool.execute(
            to=params.get("to"),
            subject=params.get("subject", ""),
            body=params.get("body", ""),
            reply_to_id=email_id,
            reasoning=reason
        )

        if result.success:
            # Add to local action log (tool already handles graph/redis/notification)
            await log_action(
                "draft_email",
                "agent",
                summary=f"Drafted reply to {params.get('to')}",
                details={**params, "draft_id": result.data.get("draft_id"), "reason": reason}
            )
            return {"drafted": True, "draft_id": result.data.get("draft_id")}
        
        return None
>>>>
```

### 3. Omission: `drive_files` Schema in Init
**The Issue:** `src/cognitex/services/drive_metadata.py` creates the `drive_files` table dynamically in code. If the service isn't run, the table doesn't exist, which might break queries in the web UI that join on it. It should be in `init.sql`.

<file path="docker/postgres/init.sql">
```sql
<<<<
CREATE INDEX idx_document_chunks_fts ON document_chunks USING gin(to_tsvector('english', content));

COMMENT ON TABLE document_chunks IS 'Stores document chunks for semantic search with overlap';
====
CREATE INDEX idx_document_chunks_fts ON document_chunks USING gin(to_tsvector('english', content));

COMMENT ON TABLE document_chunks IS 'Stores document chunks for semantic search with overlap';

-- Drive metadata cache
CREATE TABLE IF NOT EXISTS drive_files (
    id VARCHAR(255) PRIMARY KEY,
    name VARCHAR(500) NOT NULL,
    mime_type VARCHAR(100),
    folder_path TEXT,
    parent_id VARCHAR(255),
    created_time TIMESTAMP,
    modified_time TIMESTAMP,
    size_bytes BIGINT,
    owner_email VARCHAR(255),
    is_priority BOOLEAN DEFAULT FALSE,
    indexed_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_drive_files_priority ON drive_files(is_priority);
CREATE INDEX idx_drive_files_folder_path ON drive_files(folder_path);
>>>>
```

---

### Improvement 1: Coding Session Search (CLI)
You have the data in Neo4j, but no way to search it from the CLI to answer "What did I do on Project X last week?".

<file path="src/cognitex/cli/main.py">
```python
<<<<
@app.command("sessions-context")
def sessions_context(
    project: str = typer.Argument(..., help="Project name to get context for"),
    limit: int = typer.Option(5, "--limit", "-l", help="Number of recent sessions"),
) -> None:
====
@app.command("sessions-search")
def sessions_search(
    query: str = typer.Argument(..., help="Keyword or semantic search for coding sessions"),
    project: str = typer.Option(None, "--project", "-p", help="Filter by project"),
    limit: int = typer.Option(10, "--limit", "-l", help="Number of sessions"),
) -> None:
    """Search coding sessions for specific decisions or topics."""
    async def run_search():
        from cognitex.db.neo4j import init_neo4j, close_neo4j, get_neo4j_session
        await init_neo4j()

        try:
            async for session in get_neo4j_session():
                # Neo4j Fulltext or simple string matching
                cypher = """
                MATCH (cs:CodingSession)
                WHERE toLower(cs.summary) CONTAINS toLower($query)
                   OR any(d IN cs.decisions WHERE toLower(d) CONTAINS toLower($query))
                   OR any(t IN cs.topics WHERE toLower(t) CONTAINS toLower($query))
                """
                if project:
                    cypher += " AND cs.project_path CONTAINS $project"
                
                cypher += """
                RETURN cs.session_id as id, cs.summary as summary, 
                       cs.project_path as path, cs.ended_at as date,
                       cs.decisions as decisions
                ORDER BY cs.ended_at DESC
                LIMIT $limit
                """
                
                result = await session.run(cypher, query=query, project=project, limit=limit)
                records = await result.data()
                
                if not records:
                    console.print("[yellow]No sessions found matching query.[/yellow]")
                    return

                console.print(f"\n[bold]Found {len(records)} sessions:[/bold]\n")
                
                for r in records:
                    date_str = str(r['date'])[:16]
                    project_name = r['path'].split('/')[-1]
                    console.print(f"[cyan]{date_str}[/cyan] [green]{project_name}[/green] [dim]({r['id'][:8]})[/dim]")
                    console.print(f"  {r['summary']}")
                    if r['decisions']:
                         console.print(f"  [dim]Decisions: {r['decisions'][0]}[/dim]")
                    console.print("")

        finally:
            await close_neo4j()

    asyncio.run(run_search())

@app.command("sessions-context")
def sessions_context(
    project: str = typer.Argument(..., help="Project name to get context for"),
    limit: int = typer.Option(5, "--limit", "-l", help="Number of recent sessions"),
) -> None:
>>>>
```

### Improvement 2: Web UI Project "Deep Dive"
The Project page (`projects.html`) lists projects but doesn't show the rich development context you now have. We should link to a detail page.

<file path="src/cognitex/web/app.py">
```python
<<<<
@app.delete("/projects/{project_id}", response_class=HTMLResponse)
async def project_delete(project_id: str):
====
@app.get("/projects/{project_id}", response_class=HTMLResponse)
async def project_detail(request: Request, project_id: str):
    """Deep dive project page."""
    from cognitex.services.tasks import get_project_service
    from cognitex.services.coding_sessions import get_session_ingester

    project_service = get_project_service()
    project = await project_service.get(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Get coding sessions
    ingester = get_session_ingester()
    # Assuming project title/name maps to session project path logic
    sessions = await ingester.get_project_development_context(project['title'], limit=10)

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
>>>>
```
*(Note: You will need to create `src/cognitex/web/templates/project_detail.html` based on `goal_detail.html` but adding the sessions list).*

### Improvement 3: Auto-Discovery of CLI Paths
The `cognitex-sync` config defaults to `~/.claude/projects`. It would be better if `cognitex-sync` could scan standard locations for other tools (like `.cursor/` or `.vscode/`) or allow multi-path configuration easily.

Update `tools/cognitex-sync/cognitex_sync/config.py`:

```python
<<<<
    # Set default CLI paths
    if not config.cli_paths:
        config.cli_paths = {
            "claude": str(Path.home() / ".claude" / "projects"),
        }

    return config
====
    # Set default CLI paths
    if not config.cli_paths:
        home = Path.home()
        config.cli_paths = {
            "claude": str(home / ".claude" / "projects"),
            # Future support for other tools
            # "cursor": str(home / ".cursor" / "history"), 
        }

    return config
>>>>
```