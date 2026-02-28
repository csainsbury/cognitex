"""Agent tools - capabilities available to the planner and executors."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import structlog

logger = structlog.get_logger()


class ToolRisk(Enum):
    """Risk level for tool execution."""
    READONLY = "readonly"      # Always allowed, no side effects
    AUTO = "auto"              # Auto-execute, low risk side effects
    APPROVAL = "approval"      # Requires user approval before execution


class ToolCategory(Enum):
    """Functional category for mode-based tool filtering.

    Used by the state-aware tool filter to determine which tools
    are appropriate for the user's current operating mode.
    """
    READONLY = "readonly"           # Graph queries, searches, data retrieval
    NOTIFICATION = "notification"   # Sending notifications to user
    EMAIL = "email"                 # Email drafting and management
    TASK_MUTATION = "task_mutation" # Creating or updating tasks
    PROJECT_MUTATION = "project_mutation"  # Creating or updating projects/goals
    EVENT = "event"                 # Calendar event creation
    MEMORY = "memory"               # Memory storage and retrieval
    WEB = "web"                     # External web searches and fetches


@dataclass
class ToolResult:
    """Result from executing a tool."""
    success: bool
    data: Any = None
    error: str | None = None
    needs_approval: bool = False
    approval_id: str | None = None


@dataclass
class ToolDefinition:
    """Definition of a tool available to the agent."""
    name: str
    description: str
    risk: ToolRisk
    category: ToolCategory
    parameters: dict[str, Any] = field(default_factory=dict)
    examples: list[str] = field(default_factory=list)


class BaseTool(ABC):
    """Base class for all agent tools."""

    name: str
    description: str
    risk: ToolRisk
    category: ToolCategory  # Functional category for mode-based filtering
    parameters: dict[str, Any] = {}

    @abstractmethod
    async def execute(self, **kwargs) -> ToolResult:
        """Execute the tool with given parameters."""
        pass

    def to_definition(self) -> ToolDefinition:
        """Convert to a tool definition for the planner."""
        return ToolDefinition(
            name=self.name,
            description=self.description,
            risk=self.risk,
            category=self.category,
            parameters=self.parameters,
        )


# =============================================================================
# READ-ONLY TOOLS (always allowed)
# =============================================================================

class GraphQueryTool(BaseTool):
    """Execute Cypher queries against Neo4j."""

    name = "graph_query"
    description = "Query the knowledge graph using Cypher. Returns nodes and relationships."
    risk = ToolRisk.READONLY
    category = ToolCategory.READONLY
    parameters = {
        "query": {"type": "string", "description": "Cypher query to execute"},
        "params": {"type": "object", "description": "Query parameters", "optional": True},
    }

    async def execute(self, query: str, params: dict | None = None) -> ToolResult:
        from cognitex.db.neo4j import get_neo4j_session

        # Security check: block write operations (LLMs sometimes ignore instructions)
        write_keywords = ["CREATE", "DELETE", "SET", "MERGE", "DETACH", "REMOVE", "DROP"]
        upper_query = query.upper()
        if any(kw in upper_query for kw in write_keywords):
            logger.warning("Blocked write operation in GraphQueryTool", query=query[:100])
            return ToolResult(
                success=False,
                error="GraphQueryTool is read-only. Write operations (CREATE, DELETE, SET, MERGE) are not allowed.",
            )

        try:
            async for session in get_neo4j_session():
                result = await session.run(query, params or {})
                data = await result.data()
                return ToolResult(success=True, data=data)
        except Exception as e:
            logger.warning("Graph query failed", query=query[:100], error=str(e))
            return ToolResult(success=False, error=str(e))


class GetInboxTool(BaseTool):
    """Get pending inbox items (task proposals, email drafts, flagged items)."""

    name = "get_inbox"
    description = (
        "Get pending inbox items requiring user attention. "
        "Returns task proposals, email drafts, context packs, and flagged items."
    )
    risk = ToolRisk.READONLY
    category = ToolCategory.READONLY
    parameters = {
        "item_type": {
            "type": "string",
            "description": "Filter by type: task_proposal, email_draft, context_pack, flagged_item",
            "optional": True,
        },
        "limit": {"type": "integer", "description": "Max results", "default": 20},
    }

    async def execute(self, item_type: str | None = None, limit: int = 20) -> ToolResult:
        from cognitex.services.inbox import get_inbox_service

        try:
            inbox = get_inbox_service()
            items = await inbox.get_pending_items(item_type=item_type, limit=limit)
            if not items:
                return ToolResult(
                    success=True,
                    data={"count": 0, "items": [], "message": "Inbox is empty"},
                )
            return ToolResult(
                success=True,
                data={
                    "count": len(items),
                    "items": [
                        {
                            "id": item.id,
                            "type": item.item_type,
                            "priority": item.priority,
                            "title": item.title,
                            "summary": item.summary,
                            "created_at": item.created_at.isoformat() if item.created_at else None,
                        }
                        for item in items
                    ],
                },
            )
        except Exception as e:
            logger.warning("Get inbox failed", error=str(e))
            return ToolResult(success=False, error=str(e))


class CheckEmailTool(BaseTool):
    """Fetch recent emails from the live email provider (Gmail or AgentMail)."""

    name = "check_email"
    description = (
        "Check the user's email for recent messages. Fetches live from Gmail or AgentMail "
        "(not the knowledge graph). Use this when the user asks to check, read, or review "
        "their email. Returns sender, subject, date, and snippet for each message."
    )
    risk = ToolRisk.READONLY
    category = ToolCategory.READONLY
    parameters = {
        "limit": {
            "type": "integer",
            "description": "Max messages to return (default 15)",
            "default": 15,
        },
        "query": {
            "type": "string",
            "description": "Search query to filter emails (Gmail search syntax or label filter)",
            "optional": True,
        },
    }

    async def execute(self, limit: int = 15, query: str | None = None) -> ToolResult:
        from cognitex.services.email_provider import get_email_provider

        try:
            provider = get_email_provider()
            labels = None
            if query and query.lower() in ("inbox", "unread", "starred", "important"):
                labels = [query.upper()]
                query = None

            if labels:
                messages = await provider.get_messages(limit=limit, labels=labels)
            else:
                messages = await provider.get_messages(limit=limit)

            if not messages:
                return ToolResult(
                    success=True,
                    data={
                        "provider": provider.provider_name,
                        "count": 0,
                        "messages": [],
                        "message": "No messages found",
                    },
                )

            summary = []
            for msg in messages:
                summary.append({
                    "id": msg.get("gmail_id", msg.get("id", "")),
                    "from": msg.get("sender_name") or msg.get("sender_email", "unknown"),
                    "from_email": msg.get("sender_email", ""),
                    "subject": msg.get("subject", "(no subject)"),
                    "date": msg.get("date", ""),
                    "snippet": msg.get("snippet", "")[:150],
                    "labels": msg.get("labels", []),
                })

            return ToolResult(
                success=True,
                data={
                    "provider": provider.provider_name,
                    "count": len(summary),
                    "messages": summary,
                },
            )
        except Exception as e:
            logger.warning("Check email failed", error=str(e))
            return ToolResult(success=False, error=f"Failed to check email: {e}")


class SearchDocumentsTool(BaseTool):
    """Semantic search over indexed documents."""

    name = "search_documents"
    description = "Search documents using semantic similarity. Returns matching docs with relevance scores."
    risk = ToolRisk.READONLY
    category = ToolCategory.READONLY
    parameters = {
        "query": {"type": "string", "description": "Search query text"},
        "limit": {"type": "integer", "description": "Max results", "default": 10},
    }

    async def execute(self, query: str, limit: int = 10) -> ToolResult:
        from cognitex.db.postgres import get_session
        from cognitex.services.ingestion import search_documents_semantic

        try:
            async for session in get_session():
                results = await search_documents_semantic(session, query, limit=limit)
                return ToolResult(success=True, data=results)
        except Exception as e:
            logger.warning("Document search failed", query=query[:50], error=str(e))
            return ToolResult(success=False, error=str(e))


class GetCalendarTool(BaseTool):
    """Fetch calendar events for a date range."""

    name = "get_calendar"
    description = "Get calendar events. Can filter by date range."
    risk = ToolRisk.READONLY
    category = ToolCategory.READONLY
    parameters = {
        "days_back": {"type": "integer", "description": "Days in the past", "default": 0},
        "days_ahead": {"type": "integer", "description": "Days in the future", "default": 7},
    }

    async def execute(self, days_back: int = 0, days_ahead: int = 1) -> ToolResult:
        from cognitex.db.neo4j import get_neo4j_session
        from datetime import datetime, timedelta, timezone

        try:
            # Use UTC and format for Neo4j datetime comparison
            now = datetime.now(timezone.utc)

            # For "today" queries, use start of day and end of day
            if days_back == 0 and days_ahead <= 1:
                start_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
                end_date = start_date + timedelta(days=max(1, days_ahead))
            else:
                start_date = now - timedelta(days=days_back)
                end_date = now + timedelta(days=days_ahead)

            start_str = start_date.strftime("%Y-%m-%dT%H:%M:%SZ")
            end_str = end_date.strftime("%Y-%m-%dT%H:%M:%SZ")

            logger.info("Calendar query", start=start_str, end=end_str, days_back=days_back, days_ahead=days_ahead)

            # Neo4j datetime comparison needs datetime() function
            query = """
            MATCH (e:Event)
            WHERE e.start >= datetime($start) AND e.start <= datetime($end)
            RETURN e {
                .title, .gcal_id, .location, .description,
                start: toString(e.start),
                end: toString(e.end)
            } as event
            ORDER BY e.start
            """

            async for session in get_neo4j_session():
                result = await session.run(query, {
                    "start": start_str,
                    "end": end_str,
                })
                events = await result.data()
                # Flatten the result
                flattened = [e["event"] for e in events]
                logger.info("Calendar query result", event_count=len(flattened))
                return ToolResult(success=True, data=flattened)
        except Exception as e:
            logger.warning("Calendar fetch failed", error=str(e))
            return ToolResult(success=False, error=str(e))


class GetTasksTool(BaseTool):
    """Fetch tasks with optional filters."""

    name = "get_tasks"
    description = "Get tasks from the graph. Can filter by status, assignee, due date."
    risk = ToolRisk.READONLY
    category = ToolCategory.READONLY
    parameters = {
        "status": {"type": "string", "description": "Filter by status: pending, in_progress, done", "optional": True},
        "limit": {"type": "integer", "description": "Max results", "default": 20},
        "include_overdue": {"type": "boolean", "description": "Only show overdue tasks", "default": False},
    }

    async def execute(
        self,
        status: str | None = None,
        limit: int = 20,
        include_overdue: bool = False,
    ) -> ToolResult:
        from cognitex.db.neo4j import get_neo4j_session
        from cognitex.db.graph_schema import get_tasks

        try:
            # Normalize status: "all" or empty string means no filter
            if status in ("all", ""):
                status = None

            async for session in get_neo4j_session():
                tasks = await get_tasks(session, status=status, limit=limit)

                if include_overdue:
                    from datetime import datetime
                    now = datetime.now()
                    tasks = [t for t in tasks if t.get("due") and t["due"] < now]

                return ToolResult(success=True, data=tasks)
        except Exception as e:
            logger.warning("Task fetch failed", error=str(e))
            return ToolResult(success=False, error=str(e))


class GetContactTool(BaseTool):
    """Get detailed information about a contact."""

    name = "get_contact"
    description = "Get contact profile including relationship history and learned communication preferences (tone, greeting style, etc.)."
    risk = ToolRisk.READONLY
    category = ToolCategory.READONLY
    parameters = {
        "email": {"type": "string", "description": "Contact's email address"},
    }

    async def execute(self, email: str) -> ToolResult:
        from cognitex.db.neo4j import get_neo4j_session

        try:
            query = """
            MATCH (p:Person {email: $email})
            OPTIONAL MATCH (p)<-[:SENT_BY]-(e:Email)
            OPTIONAL MATCH (p)<-[:ATTENDED_BY]-(ev:Event)
            WITH p,
                 count(DISTINCT e) as email_count,
                 count(DISTINCT ev) as event_count,
                 collect(DISTINCT e.subject)[..5] as recent_subjects,
                 max(e.date) as last_email
            RETURN p {
                .*,
                email_count: email_count,
                event_count: event_count,
                recent_subjects: recent_subjects,
                last_email: last_email
            } as contact
            """

            contact_data = None
            async for session in get_neo4j_session():
                result = await session.run(query, {"email": email})
                record = await result.single()

                if record:
                    contact_data = dict(record["contact"])
                    break

            if not contact_data:
                return ToolResult(success=False, error=f"Contact not found: {email}")

            # Enhance with learned communication patterns
            try:
                from cognitex.agent.decision_memory import get_decision_memory
                decision_memory = get_decision_memory()
                comm_pattern = await decision_memory.patterns.get_pattern(email)

                if comm_pattern:
                    contact_data["learned_preferences"] = {
                        "preferred_tone": comm_pattern.get("preferred_tone"),
                        "response_urgency": comm_pattern.get("response_urgency"),
                        "typical_response_length": comm_pattern.get("typical_response_length"),
                        "greeting_style": comm_pattern.get("greeting_style"),
                        "sign_off_style": comm_pattern.get("sign_off_style"),
                        "interaction_count": comm_pattern.get("interaction_count", 0),
                        "pattern_confidence": comm_pattern.get("pattern_confidence", 0),
                    }
            except Exception as e:
                logger.debug("Could not fetch communication pattern", email=email, error=str(e))

            return ToolResult(success=True, data=contact_data)
        except Exception as e:
            logger.warning("Contact fetch failed", email=email, error=str(e))
            return ToolResult(success=False, error=str(e))


class RecallMemoryTool(BaseTool):
    """Search the agent's episodic memory."""

    name = "recall_memory"
    description = "Search past interactions, decisions, and observations. Use for context about past events."
    risk = ToolRisk.READONLY
    category = ToolCategory.MEMORY
    parameters = {
        "query": {"type": "string", "description": "What to search for in memory"},
        "memory_type": {"type": "string", "description": "Type: interaction, decision, observation", "optional": True},
        "limit": {"type": "integer", "description": "Max results", "default": 5},
    }

    async def execute(
        self,
        query: str,
        memory_type: str | None = None,
        limit: int = 5,
    ) -> ToolResult:
        from cognitex.agent.memory import get_memory

        try:
            memory = get_memory()
            results = await memory.episodic.search(
                query=query,
                memory_type=memory_type,
                limit=limit,
            )
            return ToolResult(success=True, data=results)
        except Exception as e:
            logger.warning("Memory recall failed", query=query[:50], error=str(e))
            return ToolResult(success=False, error=str(e))


# =============================================================================
# AUTO-EXECUTE TOOLS (low risk, executed automatically)
# =============================================================================

class CreateTaskTool(BaseTool):
    """Create a new task in the graph."""

    name = "create_task"
    description = "Create a new task. Can link to projects, goals, emails, or events."
    risk = ToolRisk.AUTO
    category = ToolCategory.TASK_MUTATION
    parameters = {
        "title": {"type": "string", "description": "Task title"},
        "description": {"type": "string", "description": "Task description", "optional": True},
        "priority": {"type": "string", "description": "Priority: low, medium, high, critical", "default": "medium"},
        "due_date": {"type": "string", "description": "ISO date string", "optional": True},
        "effort_estimate": {"type": "number", "description": "Estimated hours", "optional": True},
        "energy_cost": {"type": "string", "description": "Energy: low, medium, high", "optional": True},
        "project_id": {"type": "string", "description": "Project ID to link to", "optional": True},
        "goal_id": {"type": "string", "description": "Goal ID to link to", "optional": True},
        "source_email_id": {"type": "string", "description": "Gmail ID if from email", "optional": True},
        "source_event_id": {"type": "string", "description": "GCal ID if from event", "optional": True},
    }

    async def execute(
        self,
        title: str,
        description: str | None = None,
        priority: str = "medium",
        due_date: str | None = None,
        effort_estimate: float | None = None,
        energy_cost: str | None = None,
        project_id: str | None = None,
        goal_id: str | None = None,
        source_email_id: str | None = None,
        source_event_id: str | None = None,
    ) -> ToolResult:
        from cognitex.services.tasks import get_task_service

        try:
            task_service = get_task_service()
            source_type = "email" if source_email_id else "event" if source_event_id else "agent"
            source_id = source_email_id or source_event_id

            task = await task_service.create(
                title=title,
                description=description,
                priority=priority,
                due_date=due_date,
                effort_estimate=effort_estimate,
                energy_cost=energy_cost,
                project_id=project_id,
                goal_id=goal_id,
                source_type=source_type,
                source_id=source_id,
            )

            logger.info("Created task", task_id=task["id"], title=title[:50])
            return ToolResult(success=True, data={"task_id": task["id"], "task": task})
        except Exception as e:
            logger.warning("Task creation failed", title=title[:50], error=str(e))
            return ToolResult(success=False, error=str(e))


class FindTaskTool(BaseTool):
    """Find a task by title or keywords."""

    name = "find_task"
    description = "Find a specific task by title or keywords. Use this before update_task to get the task_id. Returns matching tasks with their IDs."
    risk = ToolRisk.READONLY
    category = ToolCategory.READONLY
    parameters = {
        "title_search": {"type": "string", "description": "Title or keywords to search for"},
        "status": {"type": "string", "description": "Filter by status: pending, in_progress, done, all", "optional": True},
    }

    async def execute(
        self,
        title_search: str,
        status: str | None = None,
    ) -> ToolResult:
        from cognitex.db.neo4j import get_neo4j_session

        try:
            # Use case-insensitive contains search
            status_filter = ""
            if status and status != "all":
                status_filter = "AND t.status = $status"

            query = f"""
            MATCH (t:Task)
            WHERE toLower(t.title) CONTAINS toLower($search)
            {status_filter}
            RETURN t.id as task_id, t.title as title, t.status as status,
                   t.energy_cost as energy_cost, t.due as due,
                   t.source_type as source_type, t.source_id as source_id
            ORDER BY t.created_at DESC
            LIMIT 10
            """

            params = {"search": title_search}
            if status and status != "all":
                params["status"] = status

            async for session in get_neo4j_session():
                result = await session.run(query, params)
                tasks = await result.data()

                if tasks:
                    logger.info("Found tasks", search=title_search[:30], count=len(tasks))
                    return ToolResult(success=True, data=tasks)
                return ToolResult(
                    success=True,
                    data=[],
                    error=f"No tasks found matching '{title_search}'"
                )
        except Exception as e:
            logger.warning("Task search failed", search=title_search[:30], error=str(e))
            return ToolResult(success=False, error=str(e))


class UpdateTaskTool(BaseTool):
    """Update an existing task."""

    name = "update_task"
    description = "Update task status, due date, priority, or other properties. Use find_task first to get the task_id if you only have the title."
    risk = ToolRisk.AUTO
    category = ToolCategory.TASK_MUTATION
    parameters = {
        "task_id": {"type": "string", "description": "Task ID to update (use find_task to get this from title)"},
        "title": {"type": "string", "description": "New title", "optional": True},
        "status": {"type": "string", "description": "New status: pending, in_progress, done", "optional": True},
        "priority": {"type": "string", "description": "New priority: low, medium, high, critical", "optional": True},
        "due_date": {"type": "string", "description": "New due date (ISO)", "optional": True},
        "effort_estimate": {"type": "number", "description": "Updated effort estimate in hours", "optional": True},
        "energy_cost": {"type": "string", "description": "Updated energy cost: low, medium, high", "optional": True},
    }

    async def execute(
        self,
        task_id: str,
        title: str | None = None,
        status: str | None = None,
        priority: str | None = None,
        due_date: str | None = None,
        effort_estimate: float | None = None,
        energy_cost: str | None = None,
    ) -> ToolResult:
        from cognitex.services.tasks import get_task_service
        from datetime import datetime

        try:
            if not any([title, status, priority, due_date, effort_estimate, energy_cost]):
                return ToolResult(success=False, error="No updates provided")

            task_service = get_task_service()

            # Get original task for learning system comparisons
            original_task = await task_service.get(task_id)
            original_status = original_task.get("status") if original_task else None
            original_due = original_task.get("due") or original_task.get("due_date") if original_task else None

            task = await task_service.update(
                task_id=task_id,
                title=title,
                status=status,
                priority=priority,
                due_date=due_date,
                effort_estimate=effort_estimate,
                energy_cost=energy_cost,
            )

            if task:
                # Phase 4 Learning: Record timing and deferrals
                await self._record_learning_events(
                    task_id=task_id,
                    original_task=original_task,
                    original_status=original_status,
                    original_due=original_due,
                    new_status=status,
                    new_due=due_date,
                )

                logger.info("Updated task", task_id=task_id)
                return ToolResult(success=True, data=task)
            return ToolResult(success=False, error=f"Task not found: {task_id}")
        except Exception as e:
            logger.warning("Task update failed", task_id=task_id, error=str(e))
            return ToolResult(success=False, error=str(e))

    async def _record_learning_events(
        self,
        task_id: str,
        original_task: dict | None,
        original_status: str | None,
        original_due: str | None,
        new_status: str | None,
        new_due: str | None,
    ) -> None:
        """Record timing and deferral events for the learning system."""
        from datetime import datetime
        from cognitex.db.postgres import get_session
        from sqlalchemy import text

        try:
            # 1. Record start time when status changes to in_progress
            if new_status == "in_progress" and original_status != "in_progress":
                async for session in get_session():
                    await session.execute(text("""
                        UPDATE tasks
                        SET started_at = NOW()
                        WHERE id = :task_id AND started_at IS NULL
                    """), {"task_id": task_id})
                    await session.commit()
                    break
                logger.debug("Recorded task start time", task_id=task_id)

            # 2. Record timing when status changes to done
            if new_status == "done" and original_status != "done" and original_task:
                started_at_str = original_task.get("started_at")
                estimated_minutes = original_task.get("estimated_minutes")

                if started_at_str:
                    try:
                        from cognitex.services.tasks import record_task_timing

                        # Parse started_at
                        if isinstance(started_at_str, str):
                            started_at = datetime.fromisoformat(started_at_str.replace("Z", "+00:00"))
                        else:
                            started_at = started_at_str

                        await record_task_timing(
                            task_id=task_id,
                            started_at=started_at,
                            completed_at=datetime.now(),
                            estimated_minutes=int(estimated_minutes) if estimated_minutes else None,
                        )
                        logger.debug("Recorded task timing", task_id=task_id)
                    except Exception as e:
                        logger.warning("Failed to record task timing", error=str(e))

            # 3. Record deferral when due date is pushed back
            if new_due and original_due:
                try:
                    from dateutil.parser import parse as parse_date
                    from cognitex.agent.state_model import record_deferral

                    original_dt = parse_date(original_due) if isinstance(original_due, str) else original_due
                    new_dt = parse_date(new_due) if isinstance(new_due, str) else new_due

                    if new_dt > original_dt:
                        await record_deferral(
                            task_id=task_id,
                            inferred_reason="due_date_extended",
                        )
                        logger.debug("Recorded task deferral", task_id=task_id)
                except Exception as e:
                    logger.warning("Failed to record deferral", error=str(e))

        except Exception as e:
            logger.warning("Failed to record learning events", error=str(e))


class SendNotificationTool(BaseTool):
    """Send a notification to the user via Discord."""

    name = "send_notification"
    description = "Send a message to the user's Discord channel. Use for updates, alerts, questions."
    risk = ToolRisk.AUTO
    category = ToolCategory.NOTIFICATION
    parameters = {
        "message": {"type": "string", "description": "Message content (supports markdown)"},
        "urgency": {"type": "string", "description": "low, normal, high", "default": "normal"},
    }

    async def execute(self, message: str, urgency: str = "normal") -> ToolResult:
        from cognitex.services.notifications import publish_notification

        try:
            # Use notification service with debouncing and deduplication
            sent = await publish_notification(
                message=message,
                urgency=urgency,
                category="agent",
            )

            if sent:
                logger.info("Notification queued", urgency=urgency, length=len(message))
                return ToolResult(success=True, data={"queued": True})
            else:
                logger.debug("Notification deduplicated", urgency=urgency)
                return ToolResult(success=True, data={"queued": False, "deduplicated": True})
        except Exception as e:
            logger.warning("Notification failed", error=str(e))
            return ToolResult(success=False, error=str(e))


class AddMemoryTool(BaseTool):
    """Store something in the agent's episodic memory."""

    name = "add_memory"
    description = "Store an observation, decision, or interaction in memory for future reference."
    risk = ToolRisk.AUTO
    category = ToolCategory.MEMORY
    parameters = {
        "content": {"type": "string", "description": "What to remember"},
        "memory_type": {"type": "string", "description": "Type: observation, decision, interaction"},
        "entities": {"type": "array", "description": "Related entity IDs (emails, people)", "optional": True},
        "importance": {"type": "integer", "description": "Importance 1-5", "default": 3},
    }

    async def execute(
        self,
        content: str,
        memory_type: str,
        entities: list[str] | None = None,
        importance: int = 3,
    ) -> ToolResult:
        from cognitex.agent.memory import get_memory

        try:
            memory = get_memory()
            memory_id = await memory.episodic.store(
                content=content,
                memory_type=memory_type,
                entities=entities or [],
                importance=importance,
            )

            logger.info("Memory stored", memory_type=memory_type, importance=importance)
            return ToolResult(success=True, data={"memory_id": memory_id})
        except Exception as e:
            logger.warning("Memory storage failed", error=str(e))
            return ToolResult(success=False, error=str(e))


# =============================================================================
# APPROVAL-REQUIRED TOOLS (staged for user approval)
# =============================================================================

class DraftEmailTool(BaseTool):
    """Draft an email for user review.

    Stores drafts in both Neo4j (for web dashboard visibility) and Redis
    (for chat/CLI approval workflow). This ensures drafts appear in the
    Twin dashboard regardless of which interface created them.
    """

    name = "draft_email"
    description = "Create a draft email. Will be staged for user approval before sending."
    risk = ToolRisk.APPROVAL
    category = ToolCategory.EMAIL
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
            draft_id = f"draft_{uuid.uuid4().hex[:12]}"
            approval_id = f"apr_{uuid.uuid4().hex[:12]}"

            # 1. Create Neo4j Node (for Web Dashboard visibility in Twin)
            try:
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
                        "reply_to_id": reply_to_id or ""
                    })
                    break
            except Exception as e:
                logger.warning("Failed to create Neo4j draft node", error=str(e))
                # Continue - Redis storage is still valuable

            # 2. Stage approval in Redis (for Chat/CLI workflow)
            memory = get_memory()
            await memory.working.stage_approval(
                approval_id=approval_id,
                action_type="send_email",
                params={
                    "to": to,
                    "subject": subject,
                    "body": body,
                    "reply_to_id": reply_to_id,
                    "draft_node_id": draft_id,  # Link to graph node
                },
                reasoning=reasoning,
            )

            # 3. Send notification with approval buttons (uses debouncing service)
            try:
                from cognitex.services.notifications import publish_notification
                reasoning_line = f"\n_{reasoning}_" if reasoning else ""
                await publish_notification(
                    message=(
                        f"**📧 Email Draft for Approval**\n\n"
                        f"**To:** {to}\n"
                        f"**Subject:** {subject}\n\n"
                        f"**Body:**\n```\n{body[:800]}{'...' if len(body) > 800 else ''}\n```"
                        f"{reasoning_line}"
                    ),
                    urgency="normal",
                    category="email",
                    approval_id=approval_id,
                )
                logger.info("Approval notification queued", approval_id=approval_id)
            except Exception as e:
                logger.warning("Failed to send approval notification", error=str(e))

            # 4. Create inbox item for unified view
            try:
                from cognitex.services.inbox import get_inbox_service
                inbox = get_inbox_service()
                await inbox.create_item(
                    item_type="email_draft",
                    title=f"Draft: {subject[:50]}{'...' if len(subject) > 50 else ''}",
                    summary=f"To: {to}",
                    payload={
                        "draft_id": draft_id,
                        "approval_id": approval_id,
                        "to": to,
                        "subject": subject,
                        "body_preview": body[:200] if body else "",
                        "reply_to_id": reply_to_id,
                        "reasoning": reasoning,
                    },
                    source_id=draft_id,
                    source_type="email_drafts",
                    priority="normal",
                )
            except Exception as inbox_err:
                logger.debug("Failed to create inbox item for email draft", error=str(inbox_err))

            # 5. Track draft for edit learning
            try:
                from cognitex.services.email_style import track_draft_created
                await track_draft_created(
                    draft_id=draft_id,
                    recipient_email=to,
                    subject=subject,
                    body=body,
                    reply_to_email_id=reply_to_id,
                    created_by="agent",
                )
            except Exception as track_err:
                logger.debug("Failed to track draft lifecycle", error=str(track_err))

            logger.info("Email draft staged", approval_id=approval_id, draft_id=draft_id, to=to)
            return ToolResult(
                success=True,
                needs_approval=True,
                approval_id=approval_id,
                data={"to": to, "subject": subject, "draft_id": draft_id},
            )
        except Exception as e:
            logger.warning("Email draft failed", error=str(e))
            return ToolResult(success=False, error=str(e))


class CreateEventTool(BaseTool):
    """Create a calendar event (requires approval)."""

    name = "create_event"
    description = "Create a new calendar event. Will be staged for user approval."
    risk = ToolRisk.APPROVAL
    category = ToolCategory.EVENT
    parameters = {
        "title": {"type": "string", "description": "Event title"},
        "start": {"type": "string", "description": "Start time (ISO datetime)"},
        "end": {"type": "string", "description": "End time (ISO datetime)"},
        "attendees": {"type": "array", "description": "List of attendee emails", "optional": True},
        "description": {"type": "string", "description": "Event description", "optional": True},
        "reasoning": {"type": "string", "description": "Why this event is needed"},
    }

    async def execute(
        self,
        title: str,
        start: str,
        end: str,
        attendees: list[str] | None = None,
        description: str | None = None,
        reasoning: str = "",
    ) -> ToolResult:
        from cognitex.agent.memory import get_memory
        import uuid

        try:
            memory = get_memory()
            approval_id = f"apr_{uuid.uuid4().hex[:12]}"

            await memory.working.stage_approval(
                approval_id=approval_id,
                action_type="create_event",
                params={
                    "title": title,
                    "start": start,
                    "end": end,
                    "attendees": attendees or [],
                    "description": description,
                },
                reasoning=reasoning,
            )

            logger.info("Event staged", approval_id=approval_id, title=title)
            return ToolResult(
                success=True,
                needs_approval=True,
                approval_id=approval_id,
                data={"title": title, "start": start},
            )
        except Exception as e:
            logger.warning("Event creation failed", error=str(e))
            return ToolResult(success=False, error=str(e))


# =============================================================================
# PROJECT AND GOAL TOOLS
# =============================================================================

class GetProjectsTool(BaseTool):
    """List projects with optional filters."""

    name = "get_projects"
    description = "Get a list of projects. Can filter by status."
    risk = ToolRisk.READONLY
    category = ToolCategory.READONLY
    parameters = {
        "status": {"type": "string", "description": "Filter by status: planning, active, paused, completed, archived", "optional": True},
        "include_archived": {"type": "boolean", "description": "Include archived projects", "default": False},
        "limit": {"type": "integer", "description": "Max results", "default": 20},
    }

    async def execute(
        self,
        status: str | None = None,
        include_archived: bool = False,
        limit: int = 20,
    ) -> ToolResult:
        from cognitex.services.tasks import get_project_service

        try:
            project_service = get_project_service()
            projects = await project_service.list(
                status=status,
                include_archived=include_archived,
                limit=limit,
            )
            return ToolResult(success=True, data=projects)
        except Exception as e:
            logger.warning("Project fetch failed", error=str(e))
            return ToolResult(success=False, error=str(e))


class GetProjectTool(BaseTool):
    """Get detailed information about a specific project."""

    name = "get_project"
    description = "Get detailed project info including tasks, repos, and related projects."
    risk = ToolRisk.READONLY
    category = ToolCategory.READONLY
    parameters = {
        "project_id": {"type": "string", "description": "Project ID to fetch"},
    }

    async def execute(self, project_id: str) -> ToolResult:
        from cognitex.services.tasks import get_project_service

        try:
            project_service = get_project_service()
            project = await project_service.get(project_id)

            if project:
                return ToolResult(success=True, data=project)
            return ToolResult(success=False, error=f"Project not found: {project_id}")
        except Exception as e:
            logger.warning("Project fetch failed", project_id=project_id, error=str(e))
            return ToolResult(success=False, error=str(e))


class CreateProjectTool(BaseTool):
    """Create a new project."""

    name = "create_project"
    description = "Create a new project. Can link to goal and assign owner/stakeholders."
    risk = ToolRisk.AUTO
    category = ToolCategory.PROJECT_MUTATION
    parameters = {
        "title": {"type": "string", "description": "Project title"},
        "description": {"type": "string", "description": "Project description", "optional": True},
        "status": {"type": "string", "description": "Status: planning, active, paused", "default": "active"},
        "target_date": {"type": "string", "description": "Target completion date (ISO)", "optional": True},
        "goal_id": {"type": "string", "description": "Goal ID to link to", "optional": True},
        "owner_email": {"type": "string", "description": "Email of project owner", "optional": True},
        "stakeholder_emails": {"type": "array", "description": "List of stakeholder emails", "optional": True},
    }

    async def execute(
        self,
        title: str,
        description: str | None = None,
        status: str = "active",
        target_date: str | None = None,
        goal_id: str | None = None,
        owner_email: str | None = None,
        stakeholder_emails: list[str] | None = None,
    ) -> ToolResult:
        from cognitex.db.neo4j import get_neo4j_session
        from cognitex.db.graph_schema import link_project_to_person
        from cognitex.services.tasks import get_project_service

        try:
            project_service = get_project_service()
            project = await project_service.create(
                title=title,
                description=description,
                status=status,
                target_date=target_date,
                goal_id=goal_id,
            )

            # Link to people
            async for session in get_neo4j_session():
                if owner_email:
                    await link_project_to_person(session, project["id"], owner_email, role="owner")
                if stakeholder_emails:
                    for email in stakeholder_emails:
                        await link_project_to_person(session, project["id"], email, role="stakeholder")
                break

            logger.info("Created project", project_id=project["id"], title=title[:50])
            return ToolResult(success=True, data={"project_id": project["id"], "project": project})
        except Exception as e:
            logger.warning("Project creation failed", title=title[:50], error=str(e))
            return ToolResult(success=False, error=str(e))


class LinkProjectToPersonTool(BaseTool):
    """Link a project to a person."""

    name = "link_project_to_person"
    description = "Link a project to a person as owner or stakeholder."
    risk = ToolRisk.AUTO
    category = ToolCategory.PROJECT_MUTATION
    parameters = {
        "project_id": {"type": "string", "description": "Project ID"},
        "person_email": {"type": "string", "description": "Person's email address"},
        "role": {"type": "string", "description": "Role: owner or stakeholder", "default": "stakeholder"},
    }

    async def execute(
        self,
        project_id: str,
        person_email: str,
        role: str = "stakeholder",
    ) -> ToolResult:
        from cognitex.db.neo4j import get_neo4j_session
        from cognitex.db.graph_schema import link_project_to_person

        try:
            async for session in get_neo4j_session():
                await link_project_to_person(session, project_id, person_email, role=role)
                break

            logger.info("Linked project to person", project_id=project_id, person=person_email, role=role)
            return ToolResult(success=True, data={"linked": True, "role": role})
        except Exception as e:
            logger.warning("Project-person link failed", error=str(e))
            return ToolResult(success=False, error=str(e))


class UpdateProjectTool(BaseTool):
    """Update an existing project."""

    name = "update_project"
    description = "Update project status, title, or other properties."
    risk = ToolRisk.AUTO
    category = ToolCategory.PROJECT_MUTATION
    parameters = {
        "project_id": {"type": "string", "description": "Project ID to update"},
        "title": {"type": "string", "description": "New title", "optional": True},
        "description": {"type": "string", "description": "New description", "optional": True},
        "status": {"type": "string", "description": "New status: planning, active, paused, completed, archived", "optional": True},
        "target_date": {"type": "string", "description": "New target date (ISO)", "optional": True},
    }

    async def execute(
        self,
        project_id: str,
        title: str | None = None,
        description: str | None = None,
        status: str | None = None,
        target_date: str | None = None,
    ) -> ToolResult:
        from cognitex.services.tasks import get_project_service

        try:
            if not any([title, description, status, target_date]):
                return ToolResult(success=False, error="No updates provided")

            project_service = get_project_service()
            project = await project_service.update(
                project_id=project_id,
                title=title,
                description=description,
                status=status,
                target_date=target_date,
            )

            if project:
                logger.info("Updated project", project_id=project_id)
                return ToolResult(success=True, data=project)
            return ToolResult(success=False, error=f"Project not found: {project_id}")
        except Exception as e:
            logger.warning("Project update failed", project_id=project_id, error=str(e))
            return ToolResult(success=False, error=str(e))


class GetGoalsTool(BaseTool):
    """List goals with optional filters."""

    name = "get_goals"
    description = "Get a list of goals. Can filter by status and timeframe."
    risk = ToolRisk.READONLY
    category = ToolCategory.READONLY
    parameters = {
        "status": {"type": "string", "description": "Filter by status: active, achieved, abandoned", "optional": True},
        "timeframe": {"type": "string", "description": "Filter by timeframe: quarterly, yearly, multi_year", "optional": True},
        "include_achieved": {"type": "boolean", "description": "Include achieved goals", "default": False},
        "limit": {"type": "integer", "description": "Max results", "default": 20},
    }

    async def execute(
        self,
        status: str | None = None,
        timeframe: str | None = None,
        include_achieved: bool = False,
        limit: int = 20,
    ) -> ToolResult:
        from cognitex.services.tasks import get_goal_service

        try:
            goal_service = get_goal_service()
            goals = await goal_service.list(
                status=status,
                timeframe=timeframe,
                include_achieved=include_achieved,
                limit=limit,
            )
            return ToolResult(success=True, data=goals)
        except Exception as e:
            logger.warning("Goal fetch failed", error=str(e))
            return ToolResult(success=False, error=str(e))


class GetGoalTool(BaseTool):
    """Get detailed information about a specific goal."""

    name = "get_goal"
    description = "Get detailed goal info including child goals and linked projects."
    risk = ToolRisk.READONLY
    category = ToolCategory.READONLY
    parameters = {
        "goal_id": {"type": "string", "description": "Goal ID to fetch"},
    }

    async def execute(self, goal_id: str) -> ToolResult:
        from cognitex.services.tasks import get_goal_service

        try:
            goal_service = get_goal_service()
            goal = await goal_service.get(goal_id)

            if goal:
                return ToolResult(success=True, data=goal)
            return ToolResult(success=False, error=f"Goal not found: {goal_id}")
        except Exception as e:
            logger.warning("Goal fetch failed", goal_id=goal_id, error=str(e))
            return ToolResult(success=False, error=str(e))


class CreateGoalTool(BaseTool):
    """Create a new goal."""

    name = "create_goal"
    description = "Create a new high-level goal. Can be linked to parent goals."
    risk = ToolRisk.AUTO
    category = ToolCategory.PROJECT_MUTATION
    parameters = {
        "title": {"type": "string", "description": "Goal title"},
        "description": {"type": "string", "description": "Goal description", "optional": True},
        "timeframe": {"type": "string", "description": "Timeframe: quarterly, yearly, multi_year", "optional": True},
        "parent_goal_id": {"type": "string", "description": "Parent goal ID for hierarchy", "optional": True},
    }

    async def execute(
        self,
        title: str,
        description: str | None = None,
        timeframe: str | None = None,
        parent_goal_id: str | None = None,
    ) -> ToolResult:
        from cognitex.services.tasks import get_goal_service

        try:
            goal_service = get_goal_service()
            goal = await goal_service.create(
                title=title,
                description=description,
                timeframe=timeframe,
                parent_goal_id=parent_goal_id,
            )

            logger.info("Created goal", goal_id=goal["id"], title=title[:50])
            return ToolResult(success=True, data={"goal_id": goal["id"], "goal": goal})
        except Exception as e:
            logger.warning("Goal creation failed", title=title[:50], error=str(e))
            return ToolResult(success=False, error=str(e))


class ParseGoalTool(BaseTool):
    """Parse a goal description and create structured graph entities."""

    name = "parse_goal"
    description = """Parse a natural language goal description and automatically create:
    - The goal itself
    - Related projects (if mentioned)
    - Tasks/milestones (if mentioned)
    - Links to people (stakeholders, owners)

    Use this when the user describes a complex goal with multiple components.
    Example: "Build a health analytics platform by Q2, with Scott leading backend
    and data pipelines as the first phase"
    """
    risk = ToolRisk.AUTO
    category = ToolCategory.PROJECT_MUTATION
    parameters = {
        "description": {"type": "string", "description": "Natural language goal description"},
        "create_projects": {"type": "boolean", "description": "Create extracted projects", "default": True, "optional": True},
        "create_tasks": {"type": "boolean", "description": "Create extracted tasks", "default": True, "optional": True},
    }

    async def execute(
        self,
        description: str,
        create_projects: bool = True,
        create_tasks: bool = True,
    ) -> ToolResult:
        from cognitex.services.goal_parser import parse_and_create_goal

        try:
            result = await parse_and_create_goal(
                description,
                create_projects=create_projects,
                create_tasks=create_tasks,
                link_people=True,
                dry_run=False,
            )

            created = result["created"]
            parsed = result["parsed"]

            summary = {
                "goal_id": created["goal"]["id"] if created["goal"] else None,
                "goal_title": parsed["title"],
                "projects_created": len(created["projects"]),
                "tasks_created": len(created["tasks"]),
                "people_linked": len(created["people_linked"]),
                "themes": parsed["themes"],
                "confidence": parsed["confidence"],
            }

            logger.info("Parsed and created goal", goal_title=parsed["title"][:50], **summary)
            return ToolResult(success=True, data=summary)

        except Exception as e:
            logger.warning("Goal parsing failed", error=str(e))
            return ToolResult(success=False, error=str(e))


class UpdateGoalTool(BaseTool):
    """Update an existing goal."""

    name = "update_goal"
    description = "Update goal status, title, or other properties."
    risk = ToolRisk.AUTO
    category = ToolCategory.PROJECT_MUTATION
    parameters = {
        "goal_id": {"type": "string", "description": "Goal ID to update"},
        "title": {"type": "string", "description": "New title", "optional": True},
        "description": {"type": "string", "description": "New description", "optional": True},
        "status": {"type": "string", "description": "New status: active, achieved, abandoned", "optional": True},
        "timeframe": {"type": "string", "description": "New timeframe", "optional": True},
    }

    async def execute(
        self,
        goal_id: str,
        title: str | None = None,
        description: str | None = None,
        status: str | None = None,
        timeframe: str | None = None,
    ) -> ToolResult:
        from cognitex.services.tasks import get_goal_service

        try:
            if not any([title, description, status, timeframe]):
                return ToolResult(success=False, error="No updates provided")

            goal_service = get_goal_service()
            goal = await goal_service.update(
                goal_id=goal_id,
                title=title,
                description=description,
                status=status,
                timeframe=timeframe,
            )

            if goal:
                logger.info("Updated goal", goal_id=goal_id)
                return ToolResult(success=True, data=goal)
            return ToolResult(success=False, error=f"Goal not found: {goal_id}")
        except Exception as e:
            logger.warning("Goal update failed", goal_id=goal_id, error=str(e))
            return ToolResult(success=False, error=str(e))


class LinkTaskTool(BaseTool):
    """Link a task to projects, goals, documents, or other tasks."""

    name = "link_task"
    description = "Link a task to a project, goal, document, or set it as blocked by another task."
    risk = ToolRisk.AUTO
    category = ToolCategory.TASK_MUTATION
    parameters = {
        "task_id": {"type": "string", "description": "Task ID to link"},
        "project_id": {"type": "string", "description": "Project ID to link to", "optional": True},
        "goal_id": {"type": "string", "description": "Goal ID to link to", "optional": True},
        "document_id": {"type": "string", "description": "Drive document ID to link", "optional": True},
        "blocked_by_task_id": {"type": "string", "description": "Task ID that blocks this task", "optional": True},
    }

    async def execute(
        self,
        task_id: str,
        project_id: str | None = None,
        goal_id: str | None = None,
        document_id: str | None = None,
        blocked_by_task_id: str | None = None,
    ) -> ToolResult:
        from cognitex.services.tasks import get_task_service

        try:
            if not any([project_id, goal_id, document_id, blocked_by_task_id]):
                return ToolResult(success=False, error="No link target provided")

            task_service = get_task_service()
            linked = []

            if project_id:
                if await task_service.link_to_project(task_id, project_id):
                    linked.append(f"project:{project_id}")

            if goal_id:
                if await task_service.link_to_goal(task_id, goal_id):
                    linked.append(f"goal:{goal_id}")

            if document_id:
                if await task_service.link_to_document(task_id, document_id):
                    linked.append(f"document:{document_id}")

            if blocked_by_task_id:
                if await task_service.set_blocked_by(task_id, blocked_by_task_id):
                    linked.append(f"blocked_by:{blocked_by_task_id}")

            if linked:
                logger.info("Linked task", task_id=task_id, links=linked)
                return ToolResult(success=True, data={"task_id": task_id, "linked": linked})
            return ToolResult(success=False, error="No links were created")
        except Exception as e:
            logger.warning("Task linking failed", task_id=task_id, error=str(e))
            return ToolResult(success=False, error=str(e))


# =============================================================================
# TOOL REGISTRY
# =============================================================================

class SearchCodeTool(BaseTool):
    """Search indexed code files using semantic similarity."""

    name = "search_code"
    description = "Search code across all indexed GitHub repositories using semantic similarity. Use to find relevant code for a task or concept."
    risk = ToolRisk.READONLY
    category = ToolCategory.READONLY
    parameters = {
        "query": {"type": "string", "description": "Search query describing what code you're looking for"},
        "repo": {"type": "string", "description": "Optional: limit search to specific repository (owner/repo format)"},
        "limit": {"type": "integer", "description": "Maximum results to return", "default": 5},
    }

    async def execute(self, query: str, repo: str | None = None, limit: int = 5) -> ToolResult:
        from cognitex.db.postgres import get_session
        from cognitex.services.ingestion import search_code_semantic

        try:
            async for session in get_session():
                results = await search_code_semantic(session, query, repo_filter=repo, limit=limit)
                break

            if not results:
                return ToolResult(success=True, data={"results": [], "message": "No matching code found"})

            return ToolResult(
                success=True,
                data={
                    "results": results,
                    "count": len(results),
                }
            )

        except Exception as e:
            logger.warning("Code search failed", query=query, error=str(e))
            return ToolResult(success=False, error=str(e))


class ReadCodeFileTool(BaseTool):
    """Read the full content of a specific code file."""

    name = "read_code_file"
    description = "Read the full content of a code file by its file ID. Use after search_code to get complete file content."
    risk = ToolRisk.READONLY
    category = ToolCategory.READONLY
    parameters = {
        "file_id": {"type": "string", "description": "Code file ID (from search_code results)"},
        "max_length": {"type": "integer", "description": "Maximum content length to return", "default": 15000},
    }

    async def execute(self, file_id: str, max_length: int = 15000) -> ToolResult:
        from cognitex.db.postgres import get_session
        from sqlalchemy import text

        try:
            async for session in get_session():
                result = await session.execute(
                    text("SELECT repo_name, path, content, char_count FROM code_content WHERE file_id = :file_id"),
                    {"file_id": file_id}
                )
                row = result.fetchone()

                if row:
                    content = row.content
                    truncated = len(content) > max_length

                    return ToolResult(
                        success=True,
                        data={
                            "file_id": file_id,
                            "repo_name": row.repo_name,
                            "path": row.path,
                            "content": content[:max_length],
                            "char_count": row.char_count,
                            "truncated": truncated,
                        }
                    )

                return ToolResult(success=False, error=f"Code file not found: {file_id}")

        except Exception as e:
            logger.warning("Read code file failed", file_id=file_id, error=str(e))
            return ToolResult(success=False, error=str(e))


class GetRepositoriesTool(BaseTool):
    """List indexed GitHub repositories."""

    name = "get_repositories"
    description = "List all GitHub repositories that have been synced and indexed. Shows repo name, language, and file count."
    risk = ToolRisk.READONLY
    category = ToolCategory.READONLY
    parameters = {}

    async def execute(self) -> ToolResult:
        from cognitex.db.neo4j import get_neo4j_session
        from cognitex.db.graph_schema import list_repositories

        try:
            async for session in get_neo4j_session():
                repos = await list_repositories(session)
                break

            return ToolResult(
                success=True,
                data={
                    "repositories": repos,
                    "count": len(repos),
                }
            )

        except Exception as e:
            logger.warning("Get repositories failed", error=str(e))
            return ToolResult(success=False, error=str(e))


class WebSearchTool(BaseTool):
    """Search the web for external information."""

    name = "web_search"
    description = """Search the web for external information. Use this to:
    - Research products, services, or pricing (e.g., Azure GPU VMs, cloud services)
    - Find documentation, specifications, or technical details
    - Gather competitive intelligence or market information
    - Research topics for meeting preparation

    Returns search results with titles, snippets, and URLs."""
    risk = ToolRisk.READONLY
    category = ToolCategory.WEB
    parameters = {
        "query": {"type": "string", "description": "Search query"},
        "num_results": {"type": "integer", "description": "Number of results to return", "default": 5},
    }

    async def execute(self, query: str, num_results: int = 5) -> ToolResult:
        import asyncio

        try:
            # Use the duckduckgo-search library which provides reliable results
            search_results = await asyncio.to_thread(
                self._search_duckduckgo_sync, query, num_results
            )

            if search_results:
                logger.info("Web search completed", query=query[:50], results=len(search_results))
                return ToolResult(success=True, data={
                    "query": query,
                    "results": search_results,
                    "count": len(search_results),
                })

            return ToolResult(success=True, data={
                "query": query,
                "results": [],
                "message": "No results found"
            })

        except Exception as e:
            logger.warning("Web search failed", query=query[:50], error=str(e))
            return ToolResult(success=False, error=str(e))

    def _search_duckduckgo_sync(self, query: str, num_results: int) -> list[dict]:
        """Search using ddgs library (sync, run in thread)."""
        from ddgs import DDGS

        results = []

        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=num_results):
                results.append({
                    "title": r.get("title", ""),
                    "snippet": r.get("body", ""),
                    "url": r.get("href", ""),
                })

        return results


class WebFetchTool(BaseTool):
    """Fetch and extract content from a specific URL."""

    name = "web_fetch"
    description = """Fetch and extract the main content from a web page. Use after web_search to get detailed information from a specific URL."""
    risk = ToolRisk.READONLY
    category = ToolCategory.WEB
    parameters = {
        "url": {"type": "string", "description": "URL to fetch"},
        "max_length": {"type": "integer", "description": "Maximum content length", "default": 8000},
    }

    async def execute(self, url: str, max_length: int = 8000) -> ToolResult:
        import httpx
        import re

        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                response = await client.get(
                    url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (compatible; Cognitex/1.0)",
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    }
                )

                if response.status_code != 200:
                    return ToolResult(
                        success=False,
                        error=f"HTTP {response.status_code} fetching {url}"
                    )

                html = response.text

                # Extract title
                title_match = re.search(r'<title[^>]*>([^<]+)</title>', html, re.IGNORECASE)
                title = title_match.group(1).strip() if title_match else ""

                # Remove script and style elements
                html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
                html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL | re.IGNORECASE)
                html = re.sub(r'<nav[^>]*>.*?</nav>', '', html, flags=re.DOTALL | re.IGNORECASE)
                html = re.sub(r'<footer[^>]*>.*?</footer>', '', html, flags=re.DOTALL | re.IGNORECASE)
                html = re.sub(r'<header[^>]*>.*?</header>', '', html, flags=re.DOTALL | re.IGNORECASE)

                # Remove all HTML tags
                text = re.sub(r'<[^>]+>', ' ', html)

                # Clean up whitespace
                text = re.sub(r'\s+', ' ', text).strip()

                # Decode HTML entities
                import html as html_module
                text = html_module.unescape(text)

                content = text[:max_length]
                truncated = len(text) > max_length

                logger.info("Web fetch completed", url=url[:50], chars=len(content))
                return ToolResult(success=True, data={
                    "url": url,
                    "title": title,
                    "content": content,
                    "truncated": truncated,
                    "original_length": len(text),
                })

        except Exception as e:
            logger.warning("Web fetch failed", url=url[:50], error=str(e))
            return ToolResult(success=False, error=str(e))


class ReadDocumentTool(BaseTool):
    """Read the full content of a specific document."""

    name = "read_document"
    description = "Read the full text content of a document by its Drive ID. Use after search_documents to get full content."
    risk = ToolRisk.READONLY
    category = ToolCategory.READONLY
    parameters = {
        "drive_id": {"type": "string", "description": "Google Drive ID of the file"},
        "max_length": {"type": "integer", "description": "Maximum content length to return", "default": 10000},
    }

    async def execute(self, drive_id: str, max_length: int = 10000) -> ToolResult:
        from cognitex.db.postgres import get_session
        from sqlalchemy import text

        try:
            async for session in get_session():
                result = await session.execute(
                    text("SELECT content, char_count FROM document_content WHERE drive_id = :drive_id"),
                    {"drive_id": drive_id}
                )
                row = result.fetchone()

                if row:
                    content = row.content
                    char_count = row.char_count
                    truncated = len(content) > max_length

                    return ToolResult(
                        success=True,
                        data={
                            "drive_id": drive_id,
                            "content": content[:max_length],
                            "char_count": char_count,
                            "truncated": truncated,
                        }
                    )

                return ToolResult(success=False, error=f"Document content not found for ID: {drive_id}")

        except Exception as e:
            logger.warning("Read document failed", drive_id=drive_id, error=str(e))
            return ToolResult(success=False, error=str(e))


class AnalyzeDocumentTool(BaseTool):
    """Analyze a document in depth using AI-powered analysis.

    Extracts tracked changes, comments, highlights, key decisions, action items,
    and other semantic content from DOCX, PDF, XLSX, and PPTX files.
    """

    name = "analyze_document"
    description = (
        "Perform deep analysis on a document - extract tracked changes, comments, "
        "highlights, key decisions, action items, and risks. "
        "Best for documents sent for review or when you need to understand "
        "the full content of a complex document."
    )
    risk = ToolRisk.READONLY
    category = ToolCategory.READONLY
    parameters = {
        "drive_id": {"type": "string", "description": "Google Drive ID of the file to analyze"},
        "context": {"type": "string", "description": "Context for the analysis (e.g., 'sent for review')", "optional": True},
    }

    async def execute(
        self,
        drive_id: str,
        context: str = "",
    ) -> ToolResult:
        try:
            from cognitex.services.drive import get_drive_service
            from cognitex.services.document_analyzer import get_document_analyzer

            drive = get_drive_service()
            analyzer = get_document_analyzer()

            # Get file metadata
            file_meta = drive.get_file_metadata(drive_id)
            if not file_meta:
                return ToolResult(success=False, error=f"File not found: {drive_id}")

            filename = file_meta.get("name", "document")
            mime_type = file_meta.get("mimeType", "")

            # Check if document type is supported
            if not analyzer.is_supported(filename, mime_type):
                return ToolResult(
                    success=False,
                    error=f"Document type not supported for analysis: {mime_type}"
                )

            # Get raw file bytes
            content = drive.get_file_bytes(drive_id, mime_type)
            if not content:
                return ToolResult(success=False, error="Could not download file content")

            # Check size limit (10MB)
            if len(content) > 10 * 1024 * 1024:
                return ToolResult(
                    success=False,
                    error=f"File too large for analysis ({len(content) / 1024 / 1024:.1f}MB). Max is 10MB."
                )

            # Determine filename with proper extension
            if not any(filename.endswith(ext) for ext in [".docx", ".pdf", ".xlsx", ".pptx"]):
                ext_map = {
                    "application/vnd.google-apps.document": ".docx",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
                    "application/pdf": ".pdf",
                    "application/vnd.google-apps.spreadsheet": ".xlsx",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
                    "application/vnd.google-apps.presentation": ".pptx",
                    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
                }
                ext = ext_map.get(mime_type, "")
                if ext:
                    filename = filename + ext

            # Analyze the document
            analysis = await analyzer.analyze(
                filename=filename,
                content=content,
                context=context,
            )

            # Build rich response
            result_data = analysis.to_dict()
            result_data["filename"] = filename
            result_data["drive_id"] = drive_id

            logger.info(
                "Document analyzed",
                drive_id=drive_id,
                filename=filename[:30],
                method=analysis.method,
            )

            return ToolResult(success=True, data=result_data)

        except Exception as e:
            logger.warning("Document analysis failed", drive_id=drive_id, error=str(e))
            return ToolResult(success=False, error=str(e))


class ToolRegistry:
    """Registry of all available tools."""

    def __init__(self):
        self._tools: dict[str, BaseTool] = {}
        self._register_defaults()

    def _register_defaults(self):
        """Register all default tools."""
        default_tools = [
            # Read-only
            GraphQueryTool(),
            GetInboxTool(),
            CheckEmailTool(),
            SearchDocumentsTool(),
            ReadDocumentTool(),
            SearchCodeTool(),
            ReadCodeFileTool(),
            GetRepositoriesTool(),
            GetCalendarTool(),
            GetTasksTool(),
            FindTaskTool(),
            GetContactTool(),
            RecallMemoryTool(),
            GetProjectsTool(),
            GetProjectTool(),
            GetGoalsTool(),
            GetGoalTool(),
            WebSearchTool(),
            WebFetchTool(),
            AnalyzeDocumentTool(),
            # Auto-execute
            CreateTaskTool(),
            UpdateTaskTool(),
            LinkTaskTool(),
            CreateProjectTool(),
            UpdateProjectTool(),
            LinkProjectToPersonTool(),
            CreateGoalTool(),
            ParseGoalTool(),
            UpdateGoalTool(),
            SendNotificationTool(),
            AddMemoryTool(),
            # Approval required
            DraftEmailTool(),
            CreateEventTool(),
        ]

        # Sub-agent spawn tool
        from cognitex.agent.subagent import SpawnSubAgentTool
        default_tools.append(SpawnSubAgentTool())

        for tool in default_tools:
            self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool | None:
        """Get a tool by name."""
        return self._tools.get(name)

    def all(self) -> list[BaseTool]:
        """Get all registered tools."""
        return list(self._tools.values())

    def by_risk(self, risk: ToolRisk) -> list[BaseTool]:
        """Get tools filtered by risk level."""
        return [t for t in self._tools.values() if t.risk == risk]

    def by_category(self, category: ToolCategory) -> list[BaseTool]:
        """Get tools filtered by functional category."""
        return [t for t in self._tools.values() if t.category == category]

    def by_categories(self, categories: list[ToolCategory]) -> list[BaseTool]:
        """Get tools that match any of the given categories."""
        return [t for t in self._tools.values() if t.category in categories]

    def definitions(self) -> list[ToolDefinition]:
        """Get definitions of all tools for the planner."""
        return [t.to_definition() for t in self._tools.values()]

    async def execute(self, name: str, **kwargs) -> ToolResult:
        """Execute a tool by name."""
        tool = self.get(name)
        if not tool:
            return ToolResult(success=False, error=f"Unknown tool: {name}")

        return await tool.execute(**kwargs)


# Singleton registry
_registry: ToolRegistry | None = None


def get_tool_registry() -> ToolRegistry:
    """Get or create the tool registry singleton."""
    global _registry
    if _registry is None:
        _registry = ToolRegistry()
    return _registry
