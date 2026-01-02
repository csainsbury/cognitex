I've reviewed the codebase and identified a few additional opportunities to improve system coherence and performance, beyond the critical fixes for the autonomous loop.

### 1. Optimize Graph Queries (Prevent Token Explosion)
The `GraphObserver.get_stale_items` method currently fetches *all* stale tasks and projects. If your database grows, this could flood the LLM context window with thousands of items, increasing costs and degrading reasoning performance. I've added a `LIMIT` clause to the Cypher query.

### 2. Unify Email Drafting (Fix Visibility Gap)
There was a discrepancy between how the **Chat Agent** and the **Autonomous Agent** handled email drafts.
*   **Chat Agent** (`DraftEmailTool`): Stored drafts in Redis (Working Memory).
*   **Autonomous Agent**: Stored drafts in Neo4j nodes (`EmailDraft`).
*   **Web Dashboard**: Only queried Neo4j.

This meant if you asked the chatbot to "draft an email to John," it would theoretically do so, but the draft would **not** appear on your "Twin" dashboard for review. I've updated `DraftEmailTool` to write to Neo4j as well, ensuring all drafts appear in the dashboard regardless of who created them.

Here are the applied improvements:

### Step 1: Optimize Stale Item Query
Add limits to prevent overloading the LLM context.

<file_path="src/cognitex/agent/graph_observer.py">
```python
    async def get_stale_items(self, task_days: int = 7, project_days: int = 14) -> list[dict]:
        """Get tasks and projects that haven't been updated recently."""
        # Added LIMIT to prevent massive context injection
        query = """
        // Stale pending tasks
        MATCH (t:Task)
        WHERE t.status IN ['pending', 'in_progress']
          AND (t.updated_at < datetime() - duration({days: $task_days})
               OR t.updated_at IS NULL)
        RETURN
            'Task' as type,
            t.id as id,
            t.title as label,
            t.status as status,
            t.updated_at as last_updated,
            t.due as due_date
        ORDER BY t.updated_at ASC
        LIMIT 20

        UNION ALL

        // Stale active projects
        MATCH (p:Project)
        WHERE p.status = 'active'
          AND (p.updated_at < datetime() - duration({days: $project_days})
               OR p.updated_at IS NULL)
        RETURN
            'Project' as type,
            p.id as id,
            p.title as label,
            p.status as status,
            p.updated_at as last_updated,
            null as due_date
        ORDER BY p.updated_at ASC
        LIMIT 10
        """
        try:
            result = await self.session.run(query, {
                "task_days": task_days,
                "project_days": project_days
            })
            data = await result.data()
            return data
        except Exception as e:
            logger.warning("Failed to get stale items", error=str(e))
            return []
```
</file_path>

### Step 2: Unify Draft Storage
Update `DraftEmailTool` to write to Neo4j so chat-created drafts appear in the dashboard.

<file_path="src/cognitex/agent/tools.py">
```python
class DraftEmailTool(BaseTool):
    """Draft an email for user review."""

    name = "draft_email"
    description = "Create a draft email. Will be staged for user approval before sending."
    risk = ToolRisk.APPROVAL
    parameters = {
        "to": {"type": "string", "description": "Recipient email address"},
        "subject": {"type": "string", "description": "Email subject"},
        "body": {"type": "string", "description": "Email body content"},
        "reply_to_id": {"type": "string", "description": "Gmail ID if this is a reply", "optional": True},
        "reasoning": {"type": "string", "description": "Why this email is needed"},
    }

    async def execute(
        self,
        to: str,
        subject: str,
        body: str,
        reply_to_id: str | None = None,
        reasoning: str = "",
    ) -> ToolResult:
        from cognitex.agent.memory import get_memory
        from cognitex.db.redis import get_redis
        from cognitex.db.neo4j import get_neo4j_session
        import uuid
        import json

        try:
            # 1. Create Neo4j Node (for Web Dashboard Visibility)
            draft_id = f"draft_{uuid.uuid4().hex[:12]}"
            
            async for session in get_neo4j_session():
                query = """
                CREATE (draft:EmailDraft {
                    id: $draft_id,
                    to: $to,
                    subject: $subject,
                    body: $body,
                    status: 'pending_review',
                    created_at: datetime(),
                    created_by: 'user_agent',
                    reason: $reason
                })
                WITH draft
                OPTIONAL MATCH (original:Email {gmail_id: $reply_to_id})
                FOREACH (_ IN CASE WHEN original IS NOT NULL THEN [1] ELSE [] END |
                    CREATE (draft)-[:REPLY_TO]->(original)
                )
                RETURN draft.id
                """
                await session.run(query, {
                    "draft_id": draft_id,
                    "to": to,
                    "subject": subject,
                    "body": body,
                    "reason": reasoning,
                    "reply_to_id": reply_to_id
                })
                break

            # 2. Stage Approval in Redis (for Chat/CLI consistency)
            memory = get_memory()
            approval_id = f"apr_{uuid.uuid4().hex[:12]}"

            await memory.working.stage_approval(
                approval_id=approval_id,
                action_type="send_email",
                params={
                    "to": to,
                    "subject": subject,
                    "body": body,
                    "reply_to_id": reply_to_id,
                    "draft_node_id": draft_id, # Link to graph node
                },
                reasoning=reasoning,
            )

            # ... existing notification code ...
            try:
                redis = get_redis()
                reasoning_line = f"\n_{reasoning}_" if reasoning else ""
                notification = {
                    "message": (
                        f"**📧 Email Draft for Approval**\n\n"
                        f"**To:** {to}\n"
                        f"**Subject:** {subject}\n\n"
                        f"**Body:**\n```\n{body[:800]}{'...' if len(body) > 800 else ''}\n```"
                        f"{reasoning_line}"
                    ),
                    "urgency": "normal",
                    "approval_id": approval_id,
                }
                await redis.publish("cognitex:notifications", json.dumps(notification))
                logger.info("Approval notification sent", approval_id=approval_id)
            except Exception as e:
                logger.warning("Failed to send approval notification", error=str(e))

            logger.info("Email draft staged", approval_id=approval_id, to=to)
            return ToolResult(
                success=True,
                needs_approval=True,
                approval_id=approval_id,
                data={"to": to, "subject": subject, "draft_id": draft_id},
            )
        except Exception as e:
            logger.warning("Email draft failed", error=str(e))
            return ToolResult(success=False, error=str(e))
```
</file_path>

These changes ensure the system is more performant and the user experience is consistent across different interfaces.

<chatName="Optimize graph queries and unify draft storage"/>