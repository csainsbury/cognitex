Chatname: Final Polish & Drive Async Fixes

The system is looking very robust. You have successfully integrated the `cognitex-sync` logic, fixed the Docker configuration, and addressed the major async issues in Gmail/Calendar.

However, there are still **two remaining blocking I/O bottlenecks** related to Google Drive that will freeze your application during heavy indexing, and the **Goals API** is still a stub.

Here are the final corrections and optimization suggestions.

### 1. Critical: Unblock Drive Content Downloading
While you fixed the metadata listing in `ingestion.py`, the actual **content downloading** (which is the heaviest operation) is still synchronous.

When `semantic-analyze` or `deep-index` runs, the application will freeze while downloading files.

**Fix:** Wrap `drive.get_file_content` in `asyncio.to_thread` in both `semantic_analysis.py` and `ingestion.py`.

<file path="src/cognitex/services/semantic_analysis.py">
```python
<<<<
                # Get file content
                content = self.drive.get_file_content(file["id"], file["mime_type"])
                if not content:
                    logger.warning("Could not extract content", file_id=file["id"], name=file["name"])
====
                # Get file content (run in thread to avoid blocking)
                content = await asyncio.to_thread(
                    self.drive.get_file_content,
                    file["id"],
                    file["mime_type"]
                )
                if not content:
                    logger.warning("Could not extract content", file_id=file["id"], name=file["name"])
>>>>
```

<file path="src/cognitex/services/ingestion.py">
```python
<<<<
            try:
                # Extract content (this is the memory-intensive part)
                content = drive.get_file_content(file_data["id"], mime_type)

                if not content or len(content.strip()) < 100:
====
            try:
                # Extract content (run in thread to avoid blocking)
                content = await asyncio.to_thread(
                    drive.get_file_content,
                    file_data["id"],
                    mime_type
                )

                if not content or len(content.strip()) < 100:
>>>>
```

### 2. Implementation: Wire Up Goals API
The `src/cognitex/api/routes/goals.py` endpoints return empty lists or errors. Since you have `GoalService` in `services/tasks.py`, connect them.

<file path="src/cognitex/api/routes/goals.py">
```python
<<<<
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
====
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
        limit=limit
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
            updated_at=g.get("updated_at")
        ) for g in goals
    ]
>>>>
```

(Similarly for `create_goal`, `get_goal`, etc., mapping the Pydantic models to `GoalService` methods).

### 3. Optimization: Parallel Graph Context Gathering
In `src/cognitex/agent/graph_observer.py`, the `get_full_context` method runs about 10 graph queries **sequentially**. As your graph grows, this will make the agent sluggish (taking 5-10s just to "look" before "thinking").

**Improvement:** Use `asyncio.gather` to run independent queries in parallel.

<file path="src/cognitex/agent/graph_observer.py">
```python
<<<<
    async def get_full_context(self) -> dict:
        """Gather comprehensive context about the graph state."""
        # Get firewall inbox items (captured interruptions waiting for triage)
        inbox_items = await self._get_inbox_items()

        context = {
            "timestamp": datetime.now().isoformat(),
            "summary": {},
            # Graph health metrics
            "recent_changes": await self.get_recent_changes(),
            "stale_items": await self.get_stale_items(),
            "orphaned_nodes": await self.get_orphaned_nodes(),
            "goal_health": await self.get_goal_health(),
            "project_health": await self.get_project_health(),
            "pending_tasks": await self.get_pending_tasks(),
            "recent_documents": await self.get_recent_documents(),
            "connection_opportunities": await self.get_connection_opportunities(),
            # Digital twin perception
            "writing_samples": await self.get_user_writing_samples(),
            "pending_emails": await self.get_actionable_emails(),
            "upcoming_calendar": await self.get_pending_calendar_blocks(),
            # Already-actioned items (to prevent re-suggesting)
            "projects_with_recent_blocks": await self.get_projects_with_recent_blocks(),
            # Firewall inbox - captured items needing triage
            "inbox_items": inbox_items,
        }
====
    async def get_full_context(self) -> dict:
        """Gather comprehensive context about the graph state."""
        
        # Execute independent queries in parallel
        (
            inbox_items,
            recent_changes,
            stale_items,
            orphaned_nodes,
            goal_health,
            project_health,
            pending_tasks,
            recent_documents,
            connection_opportunities,
            writing_samples,
            pending_emails,
            upcoming_calendar,
            projects_with_recent_blocks
        ) = await asyncio.gather(
            self._get_inbox_items(),
            self.get_recent_changes(),
            self.get_stale_items(),
            self.get_orphaned_nodes(),
            self.get_goal_health(),
            self.get_project_health(),
            self.get_pending_tasks(),
            self.get_recent_documents(),
            self.get_connection_opportunities(),
            self.get_user_writing_samples(),
            self.get_actionable_emails(),
            self.get_pending_calendar_blocks(),
            self.get_projects_with_recent_blocks()
        )

        context = {
            "timestamp": datetime.now().isoformat(),
            "summary": {},
            # Graph health metrics
            "recent_changes": recent_changes,
            "stale_items": stale_items,
            "orphaned_nodes": orphaned_nodes,
            "goal_health": goal_health,
            "project_health": project_health,
            "pending_tasks": pending_tasks,
            "recent_documents": recent_documents,
            "connection_opportunities": connection_opportunities,
            # Digital twin perception
            "writing_samples": writing_samples,
            "pending_emails": pending_emails,
            "upcoming_calendar": upcoming_calendar,
            # Already-actioned items
            "projects_with_recent_blocks": projects_with_recent_blocks,
            # Firewall inbox
            "inbox_items": inbox_items,
        }
>>>>
```

### 4. Suggestion: Improve Agent Responsiveness
Currently, the `Agent.chat_with_approvals` method in `agent/core.py` logs user messages to memory *before* getting the LLM response.

If the Memory system (Postgres) is slow or under load, the user perceives latency before the Agent even *starts* thinking.

**Suggestion:** Start the ReAct loop concurrently with storing the user interaction to memory, since the ReAct loop only needs the in-memory context initially.

However, given the current complexity, the priority should be the **Drive Async Fixes** (Point 1). The system is otherwise in excellent shape.