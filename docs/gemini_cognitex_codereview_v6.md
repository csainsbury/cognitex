Chatname: Cognitex Sync & Architecture Review

I've reviewed the new `cognitex-sync` integration and the codebase. The implementation of the remote sync client is well-architected, separating the client logic (local discovery/parsing) from the server logic (LLM summarization/graph storage).

However, there are a few **critical integration gaps** that will prevent it from working smoothly, particularly regarding configuration and data linking.

### Critical Issues

1.  **Missing `SYNC_API_KEY` Configuration**: The `.env.example` doesn't list the `SYNC_API_KEY`, which is required for the sync endpoint to accept requests.
2.  **Project Linking Gaps**: The automatic linking logic (`coding_sessions.py`) tries to match sessions to projects via `local_path`, but the `Project` nodes in the database don't currently store a `local_path` property.
3.  **Endpoint Duplication**: The sync endpoint implementation is duplicated in both `web/app.py` and `api/routes/sync.py`. This violates DRY and can lead to bugs if one is updated but not the other.
4.  **Docker Volume Missing**: The scheduled server-side sync trigger (`_coding_sessions_sync` in `triggers.py`) will likely fail or do nothing in Docker because `~/.claude` isn't mounted into the container.

### 1. Fix Configuration
Update `.env.example` to include the required API key.

```example
File: .env.example
<<<<
# Application settings
LOG_LEVEL=INFO
ENVIRONMENT=development
====
# Application settings
LOG_LEVEL=INFO
ENVIRONMENT=development

# Sync API (for remote coding sessions)
SYNC_API_KEY=change_me_to_random_string_for_sync_client
>>>>
```

### 2. Enable Robust Project Linking
Add `local_path` support to Projects so the system can automatically link coding sessions based on directory paths.

**Step 2a: Update Graph Schema**
<file path="src/cognitex/db/graph_schema.py">
```python
<<<<
async def create_project(
    session: AsyncSession,
    project_id: str,
    title: str,
    description: str | None = None,
    status: str = "active",
    target_date: str | None = None,
) -> dict:
    """
    Create a Project node in the graph.
====
async def create_project(
    session: AsyncSession,
    project_id: str,
    title: str,
    description: str | None = None,
    status: str = "active",
    target_date: str | None = None,
    local_path: str | None = None,
) -> dict:
    """
    Create a Project node in the graph.
>>>>
```

```python
<<<<
    ON CREATE SET
        p.title = $title,
        p.description = $description,
        p.status = $status,
        p.target_date = CASE WHEN $target_date IS NOT NULL THEN datetime($target_date) ELSE null END,
        p.created_at = datetime(),
        p.updated_at = datetime()
    ON MATCH SET
        p.title = $title,
        p.description = COALESCE($description, p.description),
        p.status = COALESCE($status, p.status),
        p.target_date = CASE WHEN $target_date IS NOT NULL THEN datetime($target_date) ELSE p.target_date END,
        p.updated_at = datetime()
    RETURN p
    """
    result = await session.run(
        query,
        project_id=project_id,
        title=title,
        description=description,
        status=status,
        target_date=target_date,
    )
====
    ON CREATE SET
        p.title = $title,
        p.description = $description,
        p.status = $status,
        p.target_date = CASE WHEN $target_date IS NOT NULL THEN datetime($target_date) ELSE null END,
        p.local_path = $local_path,
        p.created_at = datetime(),
        p.updated_at = datetime()
    ON MATCH SET
        p.title = $title,
        p.description = COALESCE($description, p.description),
        p.status = COALESCE($status, p.status),
        p.target_date = CASE WHEN $target_date IS NOT NULL THEN datetime($target_date) ELSE p.target_date END,
        p.local_path = COALESCE($local_path, p.local_path),
        p.updated_at = datetime()
    RETURN p
    """
    result = await session.run(
        query,
        project_id=project_id,
        title=title,
        description=description,
        status=status,
        target_date=target_date,
        local_path=local_path,
    )
>>>>
```

(Apply similar changes to `update_project` in `graph_schema.py` to allow updating the path).

**Step 2b: Update Service Layer**
<file path="src/cognitex/services/tasks.py">
```python
<<<<
    async def create(
        self,
        title: str,
        description: str | None = None,
        status: str = "active",
        target_date: str | None = None,
        goal_id: str | None = None,
        owner_email: str | None = None,
        member_emails: list[str] | None = None,
        repository_ids: list[str] | None = None,
    ) -> dict:
====
    async def create(
        self,
        title: str,
        description: str | None = None,
        status: str = "active",
        target_date: str | None = None,
        goal_id: str | None = None,
        owner_email: str | None = None,
        member_emails: list[str] | None = None,
        repository_ids: list[str] | None = None,
        local_path: str | None = None,
    ) -> dict:
>>>>
```

```python
<<<<
            project = await gs.create_project(
                session,
                project_id=project_id,
                title=title,
                description=description,
                status=status,
                target_date=target_date,
            )
====
            project = await gs.create_project(
                session,
                project_id=project_id,
                title=title,
                description=description,
                status=status,
                target_date=target_date,
                local_path=local_path,
            )
>>>>
```

### 3. Deduplicate Logic
The logic in `web/app.py` for `/api/sync/sessions` is redundant since `api/routes/sync.py` handles it. I recommend removing the duplicated endpoint from `web/app.py` or having it delegate to the service layer purely.

For now, a quick fix to `web/app.py` to make it robust:

<file path="src/cognitex/web/app.py">
```python
<<<<
@app.post("/api/sync/sessions")
async def api_sync_sessions(
    request: Request,
    _auth: bool = Depends(verify_sync_api_key),
):
    """
    Ingest coding sessions from remote machines.
====
# NOTE: This logic is also present in api/routes/sync.py
# Ideally, clients should hit the API port (8000) but we support 8080 for convenience.
@app.post("/api/sync/sessions")
async def api_sync_sessions(
    request: Request,
    _auth: bool = Depends(verify_sync_api_key),
):
    """
    Ingest coding sessions from remote machines.
>>>>
```

### 4. Functional Improvements

**A. Project List Update:** Add a column to the project list command to show `local_path` so you know if linking is set up correctly.

<file path="src/cognitex/cli/main.py">
```python
<<<<
            table = Table(title=f"Projects ({len(project_list)})")
            table.add_column("#", style="cyan", width=3)
            table.add_column("Title", style="white", width=32)
            table.add_column("Status", style="green", width=10)
            table.add_column("Tasks", style="yellow", width=8)
            table.add_column("Target", style="magenta", width=12)

            for i, project in enumerate(project_list, 1):
====
            table = Table(title=f"Projects ({len(project_list)})")
            table.add_column("#", style="cyan", width=3)
            table.add_column("Title", style="white", width=32)
            table.add_column("Path", style="dim", width=20)
            table.add_column("Status", style="green", width=10)
            table.add_column("Tasks", style="yellow", width=8)
            table.add_column("Target", style="magenta", width=12)

            for i, project in enumerate(project_list, 1):
>>>>
```

```python
<<<<
                table.add_row(
                    str(i),
                    project['title'][:32],
                    project.get('status', 'active'),
                    task_str,
                    target_str,
                )
====
                local_path = project.get('local_path') or "-"
                if len(local_path) > 20: local_path = "..." + local_path[-17:]

                table.add_row(
                    str(i),
                    project['title'][:32],
                    local_path,
                    project.get('status', 'active'),
                    task_str,
                    target_str,
                )
>>>>
```

**B. Add `cognitex-sync` Key Generation:**
A small helper to generate a key for your `.env` file.

<file path="src/cognitex/cli/main.py">
```python
<<<<
@app.command("agent-status")
def agent_status() -> None:
====
@app.command("generate-sync-key")
def generate_sync_key() -> None:
    """Generate a secure random key for SYNC_API_KEY."""
    import secrets
    key = secrets.token_urlsafe(32)
    console.print(f"\n[bold green]Generated Sync API Key:[/bold green]")
    console.print(f"{key}\n")
    console.print("Add this to your .env file:")
    console.print(f"SYNC_API_KEY={key}")

@app.command("agent-status")
def agent_status() -> None:
>>>>
```

### Summary of Suggestions

1.  **Use `cognitex-sync` primarily:** The server-side scheduled trigger will likely fail in Docker. Stick to using the standalone tool to push data.
2.  **Add Project Paths:** Update your projects with `local_path` (e.g., `/Users/chris/projects/cognitex`) so sessions link automatically.
3.  **Configure `.env`:** Don't forget to set `SYNC_API_KEY` on the server and use it when configuring the sync client.