Chatname: Broad Architecture Review & Improvements

I have conducted a holistic review of the Cognitex system, looking beyond the recent sync implementation to the broader architecture, data flow, and intended "Digital Twin" functionality.

While the foundation is excellent (hybrid graph/vector memory, hierarchical agent), I have identified **one critical architectural disconnect**, **two reliability risks**, and **three opportunities** to make the agent significantly smarter.

### 1. Critical Architecture Flaw: The Web UI Bypasses the Agent
Currently, the Discord bot and the Web UI handle approvals differently.
*   **Discord Bot:** Calls `agent.handle_approval()`. This updates Working Memory, records the decision in Episodic Memory, and triggers the Learning System.
*   **Web UI (`web/app.py`):** Directly updates Neo4j nodes (`SET d.status = 'approved'`).

**Consequence:** When you approve an action via the Web UI, **the agent does not know about it.** The learning system receives no feedback, and the decision trace remains "pending" in the SQL database forever.

**Fix:** Refactor `web/app.py` to delegate approval logic to the Agent, just like the Discord bot does.

<file path="src/cognitex/web/app.py">
```python
<<<<
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
====
@app.post("/api/twin/drafts/{draft_id}/approve")
async def approve_draft(draft_id: str):
    """Approve and send an email draft via the Agent."""
    from cognitex.agent.core import get_agent
    from cognitex.agent.memory import get_memory
    
    # 1. Find the approval ID associated with this draft node ID
    # The Web UI knows the Neo4j Node ID (draft_id), but the Agent uses an Approval ID (apr_...)
    # We need to look up the approval ID from Redis or assume a mapping.
    # Ideally, the draft node in Neo4j should store the approval_id, OR we search Redis.
    
    memory = get_memory()
    approvals = await memory.working.get_pending_approvals()
    
    # Find approval where params.draft_node_id matches our draft_id
    target_approval = None
    for app in approvals:
        if app.get("params", {}).get("draft_node_id") == draft_id:
            target_approval = app
            break
            
    if not target_approval:
        # Fallback: If not in working memory (expired?), perform direct graph update
        # but warn that learning is skipped.
        logger.warning("Approval not found in working memory, performing direct update", draft_id=draft_id)
        # ... (keep existing direct update logic as fallback) ...
        return HTMLResponse('<div class="draft-card">Approved (Direct)</div>')

    # 2. Use Agent to handle approval (triggers sending + learning)
    agent = await get_agent()
    result = await agent.handle_approval(target_approval["id"], approved=True)
    
    if result.get("success"):
        return HTMLResponse(f'''
            <div class="draft-card" style="background: #d1fae5; border-color: #065f46;">
                <p><strong>Approved!</strong> {result.get('action', 'Action executed')}</p>
            </div>
        ''')
    
    raise HTTPException(status_code=500, detail=result.get("error", "Failed to approve"))
>>>>
```

*Note: You will need to apply similar logic to `approve_block` (Suggested Blocks).*

### 2. Reliability Risk: Fragile Context Gathering
In `graph_observer.py`, you use `asyncio.gather` for about 12 queries. If *one* query fails (e.g., a timeout on a complex Neo4j match), the entire `get_full_context` call fails, causing the Autonomous Agent to crash for that cycle.

**Fix:** Use `return_exceptions=True` and handle errors gracefully so the agent can continue with partial context.

<file path="src/cognitex/agent/graph_observer.py">
```python
<<<<
        ) = await asyncio.gather(
            self._get_inbox_items(),
            self.get_recent_changes(),
            # ... others ...
            self.get_projects_with_recent_blocks()
        )

        context = {
====
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
            self.get_projects_with_recent_blocks(),
            return_exceptions=True  # Prevent one failure from killing the cycle
        )

        # Helper to safely unpack results or return empty defaults
        def unwrap(res, default):
            if isinstance(res, Exception):
                logger.error("Graph observer query failed", error=str(res))
                return default
            return res

        context = {
            "timestamp": datetime.now().isoformat(),
            "summary": {},
            # Graph health metrics
            "recent_changes": unwrap(recent_changes, []),
            "stale_items": unwrap(stale_items, []),
            "orphaned_nodes": unwrap(orphaned_nodes, []),
            "goal_health": unwrap(goal_health, []),
            "project_health": unwrap(project_health, []),
            "pending_tasks": unwrap(pending_tasks, []),
            "recent_documents": unwrap(recent_documents, []),
            "connection_opportunities": unwrap(connection_opportunities, []),
            # Digital twin perception
            "writing_samples": unwrap(writing_samples, []),
            "pending_emails": unwrap(pending_emails, []),
            "upcoming_calendar": unwrap(upcoming_calendar, []),
            # Already-actioned items
            "projects_with_recent_blocks": unwrap(projects_with_recent_blocks, set()),
            # Firewall inbox
            "inbox_items": unwrap(inbox_items, []),
        }
>>>>
```

### 3. Reliability Risk: Token Limit Explosion
The `AutonomousAgent` logic injects raw text from emails and writing samples into the prompt.
If you have a long email thread or verbose writing samples, you will hit the token limit (even with 128k context, costs rise and latency increases).

**Fix:** Truncate dynamic inputs in `autonomous.py`.

<file path="src/cognitex/agent/autonomous.py">
```python
<<<<
        # Format pending emails needing response
        pending_emails = context.get('pending_emails', [])[:5]
        if pending_emails:
            email_lines = []
            for e in pending_emails:
                urgency = str(e.get('urgency', 'normal')).upper()
                sender = e.get('sender_name') or e.get('sender_email') or 'Unknown'
                snippet = str(e.get('snippet', '') or '')[:150]
====
        # Format pending emails needing response (Hard limit 5)
        pending_emails = context.get('pending_emails', [])[:5]
        if pending_emails:
            email_lines = []
            for e in pending_emails:
                urgency = str(e.get('urgency', 'normal')).upper()
                sender = e.get('sender_name') or e.get('sender_email') or 'Unknown'
                # Truncate snippet to protect token budget
                snippet = str(e.get('snippet', '') or '')[:300] 
                email_lines.append(
                    f"  - [{urgency}] From: {sender}\n"
                    f"    Subject: {e.get('subject', 'No subject')[:100]}\n"
                    f"    ID: {e.get('id')}\n"
                    f"    Snippet: {snippet}..."
                )
>>>>
```

### 4. Smart Feature: "Procedural Memory" (RAG for Instructions)
Right now, the agent relies on its generic training or the system prompt to know *how* to do things. It doesn't know *your* specific procedures (e.g., "How do I format the monthly report?").

**Improvement:** Add a tool to search specifically for "Instructions" or "Guides" in your documents, separate from general fact retrieval.

<file path="src/cognitex/agent/tools.py">
```python
class SearchProceduresTool(BaseTool):
    """Search for user-specific procedures or how-to guides."""
    
    name = "search_procedures"
    description = "Find instructions on how the user likes specific tasks done (e.g., 'how to write invoice', 'report format')."
    risk = ToolRisk.READONLY
    parameters = {
        "task_type": {"type": "string", "description": "The task being performed"},
    }

    async def execute(self, task_type: str) -> ToolResult:
        # Searches documents specifically tagged as 'procedure', 'template', or 'guide'
        # ... implementation using search_documents_semantic with filter ...
        return ToolResult(success=True, data="...")
```

### 5. Smart Feature: "Morning Briefing" via Web
Currently, the Morning Briefing is triggered by cron and sent to Discord. It would be valuable to view it on demand in the Web UI.

**Implementation:**
Add a route in `web/app.py`:
```python
@app.get("/briefing", response_class=HTMLResponse)
async def view_briefing(request: Request):
    """Render the morning briefing as a web page."""
    from cognitex.agent.core import get_agent
    agent = await get_agent()
    # Reuse the logic but return HTML
    content = await agent.morning_briefing()
    # Convert markdown to HTML (using a simple lib or regex)
    return templates.TemplateResponse("briefing.html", {"request": request, "content": content})
```

### 6. Code Style: `logging` vs `structlog`
I noticed a mix of `logging` and `structlog` usage in `src/cognitex/cli/main.py`.
It initializes `structlog` but sometimes the libraries used might default to standard logging. Ensure `structlog` is configured to intercept standard library logs to maintain a consistent JSON format across the app.

### Summary
The system is architecturally sound but has a "split brain" between the Web UI and the Agent. Fixing **Issue #1 (Web UI Approval)** is the most important step to ensure your learning system actually receives data. Implementing **Issue #2 (Asyncio Gathering)** prevents the agent from being brittle to single-query failures.