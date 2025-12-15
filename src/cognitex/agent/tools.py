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
    description = "Create a new task. Can link to projects, goals, emails, or events."
    risk = ToolRisk.AUTO
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

        try:
            if not any([title, status, priority, due_date, effort_estimate, energy_cost]):
                return ToolResult(success=False, error="No updates provided")

            task_service = get_task_service()
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
                logger.info("Updated task", task_id=task_id)
                return ToolResult(success=True, data=task)
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
            redis = get_redis()  # get_redis() is sync, returns async Redis client
            notification_data = json.dumps({
                "message": message,
                "urgency": urgency,
            })
            subscribers = await redis.publish("cognitex:notifications", notification_data)

            logger.info("Notification published", urgency=urgency, length=len(message), subscribers=subscribers)
            return ToolResult(success=True, data={"queued": True, "subscribers": subscribers})
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
# PROJECT AND GOAL TOOLS
# =============================================================================

class GetProjectsTool(BaseTool):
    """List projects with optional filters."""

    name = "get_projects"
    description = "Get a list of projects. Can filter by status."
    risk = ToolRisk.READONLY
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
    description = "Create a new project. Can optionally link to a goal."
    risk = ToolRisk.AUTO
    parameters = {
        "title": {"type": "string", "description": "Project title"},
        "description": {"type": "string", "description": "Project description", "optional": True},
        "status": {"type": "string", "description": "Status: planning, active, paused", "default": "active"},
        "target_date": {"type": "string", "description": "Target completion date (ISO)", "optional": True},
        "goal_id": {"type": "string", "description": "Goal ID to link to", "optional": True},
    }

    async def execute(
        self,
        title: str,
        description: str | None = None,
        status: str = "active",
        target_date: str | None = None,
        goal_id: str | None = None,
    ) -> ToolResult:
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

            logger.info("Created project", project_id=project["id"], title=title[:50])
            return ToolResult(success=True, data={"project_id": project["id"], "project": project})
        except Exception as e:
            logger.warning("Project creation failed", title=title[:50], error=str(e))
            return ToolResult(success=False, error=str(e))


class UpdateProjectTool(BaseTool):
    """Update an existing project."""

    name = "update_project"
    description = "Update project status, title, or other properties."
    risk = ToolRisk.AUTO
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


class UpdateGoalTool(BaseTool):
    """Update an existing goal."""

    name = "update_goal"
    description = "Update goal status, title, or other properties."
    risk = ToolRisk.AUTO
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

class ReadDocumentTool(BaseTool):
    """Read the full content of a specific document."""

    name = "read_document"
    description = "Read the full text content of a document by its Drive ID. Use after search_documents to get full content."
    risk = ToolRisk.READONLY
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
            ReadDocumentTool(),
            GetCalendarTool(),
            GetTasksTool(),
            FindTaskTool(),
            GetContactTool(),
            RecallMemoryTool(),
            GetProjectsTool(),
            GetProjectTool(),
            GetGoalsTool(),
            GetGoalTool(),
            # Auto-execute
            CreateTaskTool(),
            UpdateTaskTool(),
            LinkTaskTool(),
            CreateProjectTool(),
            UpdateProjectTool(),
            CreateGoalTool(),
            UpdateGoalTool(),
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
