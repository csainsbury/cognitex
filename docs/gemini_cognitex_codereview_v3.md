Based on your codebase and the issues you've described regarding the autonomous agent creating too many tasks, I've identified several critical safeguards and improvements.

### 1. Fix the "Stale Project" Loop
The agent identifies projects as "stale" based on their `updated_at` timestamp. Currently, creating a task for a project **does not update** the project's timestamp. This causes the agent to see the project as still stale in the next cycle (15 mins later) and create another task, leading to the "hundreds of tasks" issue.

**Fix:** Update `link_task_to_project` to touch the project's timestamp.

### 2. Implement "Pending Proposal" Throttling
The current learning system checks *approval rates*, but it doesn't check how many proposals are currently *pending*. If you are away for a day, the agent could generate 50 proposals because none have been rejected yet.

**Fix:** Limit the number of pending proposals allowed per project.

### 3. Task De-duplication
The agent doesn't check if a task with a similar title already exists before creating a new one.

**Fix:** Add a fuzzy match check before creation.

Here are the applied changes:

### Step 1: Update Graph Schema to Touch Projects
Update the linking function to refresh the project's timestamp when a task is added.

<file_path="src/cognitex/db/graph_schema.py">
```python
async def link_task_to_project(
    session: AsyncSession,
    task_id: str,
    project_id: str,
) -> bool:
    """Create PART_OF relationship between Task and Project."""
    # Added SET p.updated_at = datetime() to break the "stale project" loop
    query = """
    MATCH (t:Task {id: $task_id})
    MATCH (p:Project {id: $project_id})
    MERGE (t)-[:PART_OF]->(p)
    SET p.updated_at = datetime()
    RETURN t.id as id
    """
    result = await session.run(query, task_id=task_id, project_id=project_id)
    record = await result.single()
    return record is not None
```
</file_path>

### Step 2: Add Pending Count Logic to Action Log
Add a helper to count pending proposals for a specific target.

<file_path="src/cognitex/agent/action_log.py">
```python
async def get_pending_proposal_count(project_id: str = None, goal_id: str = None) -> int:
    """Get count of pending proposals for a specific project or goal."""
    from cognitex.db.postgres import get_session
    from sqlalchemy import text

    if not project_id and not goal_id:
        return 0

    filters = ["status = 'pending'"]
    params = {}

    if project_id:
        filters.append("project_id = :project_id")
        params["project_id"] = project_id
    if goal_id:
        filters.append("goal_id = :goal_id")
        params["goal_id"] = goal_id

    query = f"SELECT COUNT(*) FROM task_proposals WHERE {' AND '.join(filters)}"

    async for session in get_session():
        result = await session.execute(text(query), params)
        return result.scalar() or 0

    return 0
```
</file_path>

### Step 3: Secure the Autonomous Agent
Update `autonomous.py` to implement the safeguards:
1.  **Deduplication:** Check if a similar task exists.
2.  **Throttling:** Don't propose if > 3 proposals are pending for this project.
3.  **Hard Limit:** Max 3 creations per cycle.

<file_path="src/cognitex/agent/autonomous.py">
```python
    async def _execute_decision(
        self, session, decision: dict, flagged_this_cycle: set[str] | None = None
    ) -> dict | None:
        """Execute a single decision."""
        # ... existing code ...
        
        # Add safety counters to class instance or pass them in
        # For simplicity, we assume this is handled in _run_cycle iteration limit
        
        if action == "DRAFT_EMAIL":
            return await self._draft_email(session, params, reason)
        # ... existing dispatch ...
        elif action == "CREATE_TASK":
            return await self._create_task(session, params, reason)
        # ... existing dispatch ...

    async def _create_task(self, session, params: dict, reason: str) -> dict | None:
        """Create a new task to progress a goal or project."""
        import uuid
        from cognitex.config import get_settings
        from cognitex.agent.action_log import propose_task, get_proposal_recommendation, get_pending_proposal_count

        settings = get_settings()

        title = params.get("title") or params.get("task_title")
        project_id = params.get("project_id")
        goal_id = params.get("goal_id")
        description = params.get("description") or params.get("task_description", "")
        priority = params.get("priority", "medium")

        if not title:
            return None

        # 1. Deduplication Check
        # Check if a task with this title already exists in pending/in_progress
        check_query = """
        MATCH (t:Task)
        WHERE t.status IN ['pending', 'in_progress']
          AND toLower(t.title) = toLower($title)
        RETURN t.id
        """
        existing = await session.run(check_query, {"title": title})
        if await existing.single():
            logger.info("Skipping duplicate task creation", title=title)
            return {"skipped": True, "reason": "duplicate_task_exists"}

        # 2. Throttling Check (Phase 4 Integration)
        if settings.task_creation_mode == "propose":
            # Don't flood the user with proposals for the same project
            if project_id:
                pending_count = await get_pending_proposal_count(project_id=project_id)
                if pending_count >= 3:
                    logger.info("Skipping proposal - too many pending for project", project_id=project_id)
                    return {"skipped": True, "reason": f"3 pending proposals exist for project"}

            # ... existing learning check ...
            try:
                recommendation = await get_proposal_recommendation(
                    project_id=project_id,
                    priority=priority,
                )
        # ... rest of existing _create_task ...
```
</file_path>

### 4. Enforce Action Limits in the Loop
Ensure the agent doesn't run away in a single cycle.

<file_path="src/cognitex/agent/autonomous.py">
```python
    async def _run_cycle(self) -> None:
        """Run a single observation-reasoning-action cycle."""
        # ... existing setup ...

        # 2. ORIENT & DECIDE
        decisions = await self._reason_about_context(context)

        # 3. ACT - Execute decisions
        MAX_ACTIONS_PER_CYCLE = 5  # Hard safety limit
        action_count = 0
        
        flagged_this_cycle: set[str] = set()

        for decision in decisions:
            if action_count >= MAX_ACTIONS_PER_CYCLE:
                logger.warning("Hit max actions per cycle limit", limit=MAX_ACTIONS_PER_CYCLE)
                break

            try:
                result = await self._execute_decision(session, decision, flagged_this_cycle)
                if result and not result.get("skipped"):
                    actions_taken.append({
                        "decision": decision,
                        "result": result
                    })
                    action_count += 1
            except Exception as e:
                # ... existing error handling ...
                pass
        
        # ... existing logging ...
```
</file_path>

### Summary of Improvements

1.  **Stop Stale Loops**: `link_task_to_project` now updates the project timestamp, stopping the agent from thinking the project is "stale" immediately after it fixes it.
2.  **Spam Prevention**: Added `get_pending_proposal_count` to stop proposing tasks if 3 are already waiting for approval on that project.
3.  **Deduplication**: Added a check for existing tasks with the same title before creation.
4.  **Cycle Limit**: Added a hard code limit of 5 actions per cycle to backup the prompt instructions.

These changes should stabilize the autonomous behavior significantly.

<chatName="Fix Autonomous Agent task explosion"/>