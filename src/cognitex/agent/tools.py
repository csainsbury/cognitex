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
    parameters: dict[str, Any] = field(default_factory=dict)
    examples: list[str] = field(default_factory=list)


class BaseTool(ABC):
    """Base class for all agent tools."""

    name: str
    description: str
    risk: ToolRisk
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
    parameters = {
        "query": {"type": "string", "description": "Cypher query to execute"},
        "params": {"type": "object", "description": "Query parameters", "optional": True},
    }

    async def execute(self, query: str, params: dict | None = None) -> ToolResult:
        from cognitex.db.neo4j import get_neo4j_session

        try:
            async for session in get_neo4j_session():
                result = await session.run(query, params or {})
                data = await result.data()
                return ToolResult(success=True, data=data)
        except Exception as e:
            logger.warning("Graph query failed", query=query[:100], error=str(e))
            return ToolResult(success=False, error=str(e))


class SearchDocumentsTool(BaseTool):
    """Semantic search over indexed documents."""

    name = "search_documents"
    description = "Search documents using semantic similarity. Returns matching docs with relevance scores."
    risk = ToolRisk.READONLY
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
    parameters = {
        "days_back": {"type": "integer", "description": "Days in the past", "default": 0},
        "days_ahead": {"type": "integer", "description": "Days in the future", "default": 7},
    }

    async def execute(self, days_back: int = 0, days_ahead: int = 7) -> ToolResult:
        from cognitex.db.neo4j import get_neo4j_session
        from datetime import datetime, timedelta

        try:
            start_date = datetime.now() - timedelta(days=days_back)
            end_date = datetime.now() + timedelta(days=days_ahead)

            query = """
            MATCH (e:Event)
            WHERE e.start >= $start AND e.start <= $end
            RETURN e
            ORDER BY e.start
            """

            async for session in get_neo4j_session():
                result = await session.run(query, {
                    "start": start_date.isoformat(),
                    "end": end_date.isoformat(),
                })
                events = await result.data()
                return ToolResult(success=True, data=events)
        except Exception as e:
            logger.warning("Calendar fetch failed", error=str(e))
            return ToolResult(success=False, error=str(e))


class GetTasksTool(BaseTool):
    """Fetch tasks with optional filters."""

    name = "get_tasks"
    description = "Get tasks from the graph. Can filter by status, assignee, due date."
    risk = ToolRisk.READONLY
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
    description = "Get contact profile including relationship history, communication patterns."
    risk = ToolRisk.READONLY
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

            async for session in get_neo4j_session():
                result = await session.run(query, {"email": email})
                record = await result.single()

                if record:
                    return ToolResult(success=True, data=record["contact"])
                return ToolResult(success=False, error=f"Contact not found: {email}")
        except Exception as e:
            logger.warning("Contact fetch failed", email=email, error=str(e))
            return ToolResult(success=False, error=str(e))


class RecallMemoryTool(BaseTool):
    """Search the agent's episodic memory."""

    name = "recall_memory"
    description = "Search past interactions, decisions, and observations. Use for context about past events."
    risk = ToolRisk.READONLY
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
    description = "Create a new task. Automatically links to source email/event if provided."
    risk = ToolRisk.AUTO
    parameters = {
        "title": {"type": "string", "description": "Task title"},
        "description": {"type": "string", "description": "Task description", "optional": True},
        "energy_cost": {"type": "integer", "description": "Energy cost 1-10", "default": 3},
        "due_date": {"type": "string", "description": "ISO date string", "optional": True},
        "source_email_id": {"type": "string", "description": "Gmail ID if from email", "optional": True},
        "source_event_id": {"type": "string", "description": "GCal ID if from event", "optional": True},
    }

    async def execute(
        self,
        title: str,
        description: str | None = None,
        energy_cost: int = 3,
        due_date: str | None = None,
        source_email_id: str | None = None,
        source_event_id: str | None = None,
    ) -> ToolResult:
        from cognitex.db.neo4j import get_neo4j_session
        from cognitex.db.graph_schema import create_task, link_task_to_email
        import uuid

        try:
            task_id = f"task_{uuid.uuid4().hex[:12]}"
            source_type = "email" if source_email_id else "event" if source_event_id else "agent"
            source_id = source_email_id or source_event_id

            async for session in get_neo4j_session():
                await create_task(
                    session,
                    task_id=task_id,
                    title=title,
                    description=description,
                    energy_cost=energy_cost,
                    due_date=due_date,
                    source_type=source_type,
                    source_id=source_id,
                )

                if source_email_id:
                    await link_task_to_email(session, task_id, source_email_id)

                logger.info("Created task", task_id=task_id, title=title[:50])
                return ToolResult(success=True, data={"task_id": task_id})
        except Exception as e:
            logger.warning("Task creation failed", title=title[:50], error=str(e))
            return ToolResult(success=False, error=str(e))


class UpdateTaskTool(BaseTool):
    """Update an existing task."""

    name = "update_task"
    description = "Update task status, due date, or other properties."
    risk = ToolRisk.AUTO
    parameters = {
        "task_id": {"type": "string", "description": "Task ID to update"},
        "status": {"type": "string", "description": "New status: pending, in_progress, done", "optional": True},
        "due_date": {"type": "string", "description": "New due date (ISO)", "optional": True},
        "energy_cost": {"type": "integer", "description": "Updated energy cost", "optional": True},
    }

    async def execute(
        self,
        task_id: str,
        status: str | None = None,
        due_date: str | None = None,
        energy_cost: int | None = None,
    ) -> ToolResult:
        from cognitex.db.neo4j import get_neo4j_session

        try:
            updates = []
            params = {"task_id": task_id}

            if status:
                updates.append("t.status = $status")
                params["status"] = status
            if due_date:
                updates.append("t.due = datetime($due_date)")
                params["due_date"] = due_date
            if energy_cost:
                updates.append("t.energy_cost = $energy_cost")
                params["energy_cost"] = energy_cost

            if not updates:
                return ToolResult(success=False, error="No updates provided")

            query = f"""
            MATCH (t:Task {{id: $task_id}})
            SET {', '.join(updates)}, t.updated_at = datetime()
            RETURN t
            """

            async for session in get_neo4j_session():
                result = await session.run(query, params)
                record = await result.single()

                if record:
                    logger.info("Updated task", task_id=task_id)
                    return ToolResult(success=True, data=dict(record["t"]))
                return ToolResult(success=False, error=f"Task not found: {task_id}")
        except Exception as e:
            logger.warning("Task update failed", task_id=task_id, error=str(e))
            return ToolResult(success=False, error=str(e))


class SendNotificationTool(BaseTool):
    """Send a notification to the user via Discord."""

    name = "send_notification"
    description = "Send a message to the user's Discord channel. Use for updates, alerts, questions."
    risk = ToolRisk.AUTO
    parameters = {
        "message": {"type": "string", "description": "Message content (supports markdown)"},
        "urgency": {"type": "string", "description": "low, normal, high", "default": "normal"},
    }

    async def execute(self, message: str, urgency: str = "normal") -> ToolResult:
        from cognitex.db.redis import get_redis
        import json

        try:
            # Publish to notification channel for the Discord bot to pick up
            redis = await get_redis()
            await redis.publish("cognitex:notifications", json.dumps({
                "message": message,
                "urgency": urgency,
            }))

            logger.info("Notification queued", urgency=urgency, length=len(message))
            return ToolResult(success=True, data={"queued": True})
        except Exception as e:
            logger.warning("Notification failed", error=str(e))
            return ToolResult(success=False, error=str(e))


class AddMemoryTool(BaseTool):
    """Store something in the agent's episodic memory."""

    name = "add_memory"
    description = "Store an observation, decision, or interaction in memory for future reference."
    risk = ToolRisk.AUTO
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
        import uuid

        try:
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
                },
                reasoning=reasoning,
            )

            logger.info("Email draft staged", approval_id=approval_id, to=to)
            return ToolResult(
                success=True,
                needs_approval=True,
                approval_id=approval_id,
                data={"to": to, "subject": subject},
            )
        except Exception as e:
            logger.warning("Email draft failed", error=str(e))
            return ToolResult(success=False, error=str(e))


class CreateEventTool(BaseTool):
    """Create a calendar event (requires approval)."""

    name = "create_event"
    description = "Create a new calendar event. Will be staged for user approval."
    risk = ToolRisk.APPROVAL
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
# TOOL REGISTRY
# =============================================================================

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
            SearchDocumentsTool(),
            GetCalendarTool(),
            GetTasksTool(),
            GetContactTool(),
            RecallMemoryTool(),
            # Auto-execute
            CreateTaskTool(),
            UpdateTaskTool(),
            SendNotificationTool(),
            AddMemoryTool(),
            # Approval required
            DraftEmailTool(),
            CreateEventTool(),
        ]

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
