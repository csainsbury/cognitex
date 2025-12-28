"""
Autonomous Agent Loop - Proactive graph management and orchestration.

This agent runs continuously, observing the knowledge graph and taking
autonomous actions to:
- Monitor and maintain graph health
- Create connections between related entities
- Assess and update task/project/goal status
- Identify actions that will progress goals
- Keep the knowledge graph coherent and actionable
"""

import asyncio
import json
from datetime import datetime
from typing import Any

import structlog

from cognitex.agent.action_log import log_action
from cognitex.agent.graph_observer import GraphObserver
from cognitex.config import get_settings
from cognitex.prompts import format_prompt

logger = structlog.get_logger()

# Default interval in minutes (can be overridden in settings)
DEFAULT_INTERVAL_MINUTES = 15


class AutonomousAgent:
    """
    Autonomous agent that proactively manages the knowledge graph.

    Runs on a configurable interval, observing graph state and taking
    actions to maintain health, create connections, and progress goals.
    """

    def __init__(self):
        self.settings = get_settings()
        self.interval_minutes = self.settings.autonomous_agent_interval_minutes
        self.enabled = self.settings.autonomous_agent_enabled
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the autonomous agent loop."""
        if not self.enabled:
            logger.info("Autonomous agent disabled in settings")
            return

        if self._running:
            logger.warning("Autonomous agent already running")
            return

        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Autonomous agent started", interval_minutes=self.interval_minutes)

        await log_action(
            "autonomous_agent_started",
            "system",
            summary=f"Autonomous agent started with {self.interval_minutes}min interval"
        )

    async def stop(self) -> None:
        """Stop the autonomous agent loop."""
        if not self._running:
            return

        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        logger.info("Autonomous agent stopped")
        await log_action("autonomous_agent_stopped", "system", summary="Autonomous agent stopped")

    async def _run_loop(self) -> None:
        """Main agent loop - observe, reason, act."""
        # Initial delay to let system stabilize
        await asyncio.sleep(30)

        while self._running:
            try:
                await self._run_cycle()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Autonomous agent cycle failed", error=str(e))
                await log_action(
                    "autonomous_cycle",
                    "agent",
                    status="failed",
                    error=str(e)
                )

            # Wait for next cycle
            await asyncio.sleep(self.interval_minutes * 60)

    async def _run_cycle(self) -> None:
        """Run a single observation-reasoning-action cycle."""
        cycle_start = datetime.now()
        logger.info("Starting autonomous agent cycle")

        from cognitex.db.neo4j import get_neo4j_session

        actions_taken = []

        async for session in get_neo4j_session():
            # 1. OBSERVE - Gather graph context
            observer = GraphObserver(session)
            context = await observer.get_full_context()

            logger.info(
                "Graph context gathered",
                changes_24h=context["summary"]["total_changes_24h"],
                stale_tasks=context["summary"]["stale_tasks"],
                orphaned_docs=context["summary"]["orphaned_documents"],
                connection_opps=context["summary"]["connection_opportunities"],
            )

            # 2. ORIENT & DECIDE - Use LLM to reason about what to do
            decisions = await self._reason_about_context(context)

            # 3. ACT - Execute decisions
            for decision in decisions:
                try:
                    result = await self._execute_decision(session, decision)
                    if result:
                        actions_taken.append({
                            "decision": decision,
                            "result": result
                        })
                except Exception as e:
                    logger.warning(
                        "Decision execution failed",
                        decision_type=decision.get("action"),
                        error=str(e)
                    )

            break

        # Log the cycle results
        cycle_duration = (datetime.now() - cycle_start).total_seconds()
        await log_action(
            "autonomous_cycle",
            "agent",
            summary=f"Cycle completed: {len(actions_taken)} actions taken",
            details={
                "duration_seconds": cycle_duration,
                "context_summary": context["summary"],
                "actions_taken": actions_taken,
            }
        )

        logger.info(
            "Autonomous agent cycle completed",
            duration_seconds=cycle_duration,
            actions_taken=len(actions_taken)
        )

    async def _reason_about_context(self, context: dict) -> list[dict]:
        """Use LLM to reason about the graph context and decide on actions."""
        from cognitex.services.llm import get_llm_service

        # Build a concise summary for the LLM
        summary = context["summary"]

        # Format connection opportunities with explicit action suggestions
        opportunities = context.get('connection_opportunities', [])[:10]
        opp_lines = []
        for o in opportunities:
            opp_type = o.get('opportunity_type', '')
            source_type = o.get('source_type', '')
            source_id = o.get('source_id', '')
            source_name = o.get('source_name', '')
            target_id = o.get('target_id', '')
            target_name = o.get('target_name', '')
            reason = o.get('match_reason', 'match')

            # Map to explicit action with params
            if 'document_project' in opp_type:
                action = f'LINK_DOCUMENT with document_id="{source_id}", document_name="{source_name}", project_id="{target_id}", project_name="{target_name}"'
            elif 'repository_project' in opp_type:
                action = f'LINK_REPOSITORY with repository_id="{source_id}", repository_name="{source_name}", project_id="{target_id}", project_name="{target_name}"'
            elif 'task_project' in opp_type:
                action = f'LINK_TASK with task_id="{source_id}", task_name="{source_name}", project_id="{target_id}", project_name="{target_name}"'
            elif 'project_goal' in opp_type:
                action = f'LINK_PROJECT_TO_GOAL with project_id="{source_id}", project_name="{source_name}", goal_id="{target_id}", goal_name="{target_name}"'
            else:
                action = f'{source_type} "{source_name}" -> {o.get("target_type", "")} "{target_name}"'

            opp_lines.append(f"  - {action} ({reason})")

        opp_text = "\n".join(opp_lines) if opp_lines else "  None found"

        # Format goals needing attention
        goals_text = "\n".join([
            f"  - '{g.get('title')}' (id: {g.get('id')}) - {g.get('status_reason')}"
            for g in context.get('goal_health', []) if g.get('needs_attention')
        ][:5]) or "  None"

        # Format projects needing attention
        projects_text = "\n".join([
            f"  - '{p.get('title')}' (id: {p.get('id')}) - {p.get('status_reason')}, {p.get('total_tasks')} tasks, {p.get('overdue_count', 0)} overdue"
            for p in context.get('project_health', []) if p.get('needs_attention')
        ][:5]) or "  None"

        # Format orphaned items
        orphaned_text = "\n".join([
            f"  - {o.get('type')} '{o.get('label')}' (id: {o.get('id')}) - {o.get('issue')}"
            for o in context.get('orphaned_nodes', [])
        ][:8]) or "  None"

        prompt = format_prompt(
            "autonomous_agent",
            total_changes_24h=summary['total_changes_24h'],
            stale_tasks=summary['stale_tasks'],
            stale_projects=summary['stale_projects'],
            orphaned_documents=summary['orphaned_documents'],
            connection_opportunities=summary['connection_opportunities'],
            opportunities_text=opp_text,
            goals_text=goals_text,
            projects_text=projects_text,
            orphaned_text=orphaned_text,
        )

        try:
            llm = get_llm_service()
            logger.info("Calling LLM for autonomous reasoning...")
            response = await llm.complete(prompt, max_tokens=2000)
            logger.info("LLM response received", response_length=len(response) if response else 0)

            if not response:
                logger.warning("LLM returned empty response")
                return []

            # Parse the JSON response
            # Try to extract JSON from the response
            response_text = response.strip()

            # Log first part of response for debugging
            logger.debug("LLM raw response", response=response_text[:500])

            if response_text.startswith("```"):
                # Remove markdown code blocks
                lines = response_text.split("\n")
                response_text = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])

            # Try to find JSON array in the response
            if "[" in response_text:
                start = response_text.find("[")
                end = response_text.rfind("]") + 1
                if end > start:
                    response_text = response_text[start:end]

            decisions = json.loads(response_text)

            if not isinstance(decisions, list):
                decisions = [decisions]

            # Normalize decision format - handle multiple formats:
            # 1. {"action": "X", "params": {...}} (expected format)
            # 2. {"action": "X", "repository_id": "...", ...} (flat params)
            # 3. {"LINK_X": {...}, "reason": "..."} (action as key)
            normalized = []
            for d in decisions:
                if "action" in d:
                    action = d["action"]
                    if "params" in d:
                        # Already in expected format
                        normalized.append(d)
                    else:
                        # Flat params - extract all non-action/reason keys as params
                        params = {k: v for k, v in d.items() if k not in ("action", "reason")}
                        normalized.append({
                            "action": action,
                            "params": params,
                            "reason": d.get("reason", "")
                        })
                else:
                    # Find action type key (LINK_*, CREATE_*, FLAG_*)
                    action_type = None
                    params = {}
                    reason = d.get("reason", "")
                    for key in d:
                        if key.startswith(("LINK_", "CREATE_", "FLAG_", "UPDATE_")):
                            action_type = key
                            params = d[key] if isinstance(d[key], dict) else {}
                            break
                    if action_type:
                        normalized.append({
                            "action": action_type,
                            "params": params,
                            "reason": reason
                        })

            logger.info("LLM reasoning complete", decisions_count=len(normalized))
            return normalized

        except json.JSONDecodeError as e:
            logger.warning("Failed to parse LLM response as JSON", error=str(e), response=response[:500] if response else None)
            return []
        except Exception as e:
            logger.error("LLM reasoning failed", error=str(e), exc_info=True)
            return []

    async def _execute_decision(self, session, decision: dict) -> dict | None:
        """Execute a single decision."""
        action = decision.get("action")
        params = decision.get("params", {})
        reason = decision.get("reason", "")

        logger.info("Executing decision", action=action, reason=reason[:100])

        if action == "LINK_DOCUMENT":
            return await self._link_document(session, params, reason)
        elif action == "LINK_REPOSITORY":
            return await self._link_repository(session, params, reason)
        elif action == "LINK_TASK":
            return await self._link_task(session, params, reason)
        elif action == "LINK_PROJECT_TO_GOAL":
            return await self._link_project_to_goal(session, params, reason)
        elif action == "UPDATE_PROJECT_STATUS":
            return await self._update_project_status(session, params, reason)
        elif action == "CREATE_TASK":
            return await self._create_task(session, params, reason)
        elif action == "FLAG_FOR_REVIEW":
            return await self._flag_for_review(params, reason)
        else:
            logger.warning("Unknown action type", action=action)
            return None

    async def _link_document(self, session, params: dict, reason: str) -> dict | None:
        """Link a document to a project."""
        doc_id = params.get("document_id")
        project_id = params.get("project_id")

        if not doc_id or not project_id:
            return None

        query = """
        MATCH (d:Document {drive_id: $doc_id})
        MATCH (p:Project {id: $project_id})
        MERGE (d)-[r:BELONGS_TO]->(p)
        SET r.created_at = datetime(),
            r.created_by = 'autonomous_agent',
            r.reason = $reason
        RETURN d.name as doc_name, p.title as project_title
        """
        try:
            result = await session.run(query, {
                "doc_id": doc_id,
                "project_id": project_id,
                "reason": reason
            })
            data = await result.single()
            if data:
                await log_action(
                    "link_document",
                    "agent",
                    summary=f"Linked '{data['doc_name']}' to '{data['project_title']}'",
                    details={"document_id": doc_id, "project_id": project_id, "reason": reason}
                )
                return {"linked": True, "doc": data["doc_name"], "project": data["project_title"]}
        except Exception as e:
            logger.warning("Failed to link document", error=str(e))
        return None

    async def _link_task(self, session, params: dict, reason: str) -> dict | None:
        """Link a task to a project."""
        task_id = params.get("task_id")
        project_id = params.get("project_id")

        if not task_id or not project_id:
            return None

        query = """
        MATCH (t:Task {id: $task_id})
        MATCH (p:Project {id: $project_id})
        MERGE (t)-[r:BELONGS_TO]->(p)
        SET r.created_at = datetime(),
            r.created_by = 'autonomous_agent',
            r.reason = $reason
        RETURN t.title as task_title, p.title as project_title
        """
        try:
            result = await session.run(query, {
                "task_id": task_id,
                "project_id": project_id,
                "reason": reason
            })
            data = await result.single()
            if data:
                await log_action(
                    "link_task",
                    "agent",
                    summary=f"Linked task '{data['task_title']}' to '{data['project_title']}'",
                    details={"task_id": task_id, "project_id": project_id, "reason": reason}
                )
                return {"linked": True, "task": data["task_title"], "project": data["project_title"]}
        except Exception as e:
            logger.warning("Failed to link task", error=str(e))
        return None

    async def _link_repository(self, session, params: dict, reason: str) -> dict | None:
        """Link a GitHub repository to a project."""
        repo_id = params.get("repository_id")
        project_id = params.get("project_id")
        repo_name = params.get("repository_name", "")
        project_name = params.get("project_name", "")

        if not repo_id or not project_id:
            return None

        query = """
        MATCH (r:Repository {id: $repo_id})
        MATCH (p:Project {id: $project_id})
        MERGE (r)-[rel:BELONGS_TO]->(p)
        SET rel.created_at = datetime(),
            rel.created_by = 'autonomous_agent',
            rel.reason = $reason
        RETURN r.full_name as repo_name, p.title as project_title
        """
        try:
            result = await session.run(query, {
                "repo_id": repo_id,
                "project_id": project_id,
                "reason": reason
            })
            data = await result.single()
            if data:
                await log_action(
                    "link_repository",
                    "agent",
                    summary=f"Linked repo '{data['repo_name']}' to '{data['project_title']}'",
                    details={
                        "repository_id": repo_id,
                        "repository_name": repo_name or data['repo_name'],
                        "project_id": project_id,
                        "project_name": project_name or data['project_title'],
                        "reason": reason
                    }
                )
                return {"linked": True, "repository": data["repo_name"], "project": data["project_title"]}
        except Exception as e:
            logger.warning("Failed to link repository", error=str(e))
        return None

    async def _link_project_to_goal(self, session, params: dict, reason: str) -> dict | None:
        """Link a project to a goal."""
        project_id = params.get("project_id")
        goal_id = params.get("goal_id")
        project_name = params.get("project_name", "")
        goal_name = params.get("goal_name", "")

        if not project_id or not goal_id:
            return None

        query = """
        MATCH (p:Project {id: $project_id})
        MATCH (g:Goal {id: $goal_id})
        MERGE (p)-[rel:PART_OF]->(g)
        SET rel.created_at = datetime(),
            rel.created_by = 'autonomous_agent',
            rel.reason = $reason
        RETURN p.title as project_title, g.title as goal_title
        """
        try:
            result = await session.run(query, {
                "project_id": project_id,
                "goal_id": goal_id,
                "reason": reason
            })
            data = await result.single()
            if data:
                await log_action(
                    "link_project_to_goal",
                    "agent",
                    summary=f"Linked project '{data['project_title']}' to goal '{data['goal_title']}'",
                    details={
                        "project_id": project_id,
                        "project_name": project_name or data['project_title'],
                        "goal_id": goal_id,
                        "goal_name": goal_name or data['goal_title'],
                        "reason": reason
                    }
                )
                return {"linked": True, "project": data["project_title"], "goal": data["goal_title"]}
        except Exception as e:
            logger.warning("Failed to link project to goal", error=str(e))
        return None

    async def _update_project_status(self, session, params: dict, reason: str) -> dict | None:
        """Update a project's status based on task completion."""
        project_id = params.get("project_id")
        new_status = params.get("new_status")

        if not project_id or not new_status:
            return None

        if new_status not in ["active", "completed", "on_hold", "cancelled"]:
            return None

        query = """
        MATCH (p:Project {id: $project_id})
        SET p.status = $new_status,
            p.updated_at = datetime(),
            p.status_updated_by = 'autonomous_agent',
            p.status_reason = $reason
        RETURN p.title as title, p.status as status
        """
        try:
            result = await session.run(query, {
                "project_id": project_id,
                "new_status": new_status,
                "reason": reason
            })
            data = await result.single()
            if data:
                await log_action(
                    "update_project_status",
                    "agent",
                    summary=f"Updated '{data['title']}' status to '{new_status}'",
                    details={"project_id": project_id, "new_status": new_status, "reason": reason}
                )
                return {"updated": True, "project": data["title"], "status": new_status}
        except Exception as e:
            logger.warning("Failed to update project status", error=str(e))
        return None

    async def _create_task(self, session, params: dict, reason: str) -> dict | None:
        """Create a new task to progress a goal or project."""
        import uuid

        # Handle both "title" and "task_title" since LLM may use either
        title = params.get("title") or params.get("task_title")
        project_id = params.get("project_id")
        description = params.get("description") or params.get("task_description", "")

        if not title:
            return None

        task_id = f"task_{uuid.uuid4().hex[:12]}"

        # Create task
        query = """
        CREATE (t:Task {
            id: $task_id,
            title: $title,
            description: $description,
            status: 'pending',
            created_at: datetime(),
            created_by: 'autonomous_agent',
            creation_reason: $reason
        })
        RETURN t.id as id, t.title as title
        """
        try:
            result = await session.run(query, {
                "task_id": task_id,
                "title": title,
                "description": description,
                "reason": reason
            })
            data = await result.single()

            # Link to project if specified
            if project_id and data:
                link_query = """
                MATCH (t:Task {id: $task_id})
                MATCH (p:Project {id: $project_id})
                MERGE (t)-[:BELONGS_TO]->(p)
                RETURN p.title as project_title
                """
                link_result = await session.run(link_query, {
                    "task_id": task_id,
                    "project_id": project_id
                })
                await link_result.consume()

            if data:
                await log_action(
                    "create_task",
                    "agent",
                    summary=f"Created task '{title}'",
                    details={
                        "task_id": task_id,
                        "title": title,
                        "project_id": project_id,
                        "reason": reason
                    }
                )
                return {"created": True, "task_id": task_id, "title": title}
        except Exception as e:
            logger.warning("Failed to create task", error=str(e))
        return None

    async def _flag_for_review(self, params: dict, reason: str) -> dict | None:
        """Flag an entity for human review and notify via Discord."""
        entity_type = params.get("entity_type")
        entity_name = params.get("entity_name", params.get("entity_id", "Unknown"))
        entity_id = params.get("entity_id")
        issue = params.get("issue", reason)

        await log_action(
            "flag_for_review",
            "agent",
            summary=f"Flagged {entity_type} for review: {issue[:100]}",
            details={
                "entity_type": entity_type,
                "entity_id": entity_id,
                "entity_name": entity_name,
                "issue": issue,
                "reason": reason
            }
        )

        # Send Discord notification
        await self._send_review_notification(entity_type, entity_name, issue)

        return {"flagged": True, "entity_type": entity_type, "entity_id": entity_id}

    async def _send_review_notification(
        self, entity_type: str, entity_name: str, issue: str
    ) -> None:
        """Send a Discord notification for items flagged for review."""
        from cognitex.agent.tools import SendNotificationTool

        message = (
            f"**Review Required**\n\n"
            f"**{entity_type}:** {entity_name}\n"
            f"**Issue:** {issue}\n\n"
            f"_Flagged by autonomous agent_"
        )

        tool = SendNotificationTool()
        await tool.execute(message=message, urgency="normal")

    async def run_once(self) -> dict:
        """Run a single cycle manually (for testing/triggering)."""
        await self._run_cycle()
        return {"status": "completed"}


# Singleton instance
_autonomous_agent: AutonomousAgent | None = None


async def get_autonomous_agent() -> AutonomousAgent:
    """Get or create the autonomous agent instance."""
    global _autonomous_agent
    if _autonomous_agent is None:
        _autonomous_agent = AutonomousAgent()
    return _autonomous_agent


async def start_autonomous_agent() -> AutonomousAgent:
    """Start the autonomous agent."""
    agent = await get_autonomous_agent()
    await agent.start()
    return agent


async def stop_autonomous_agent() -> None:
    """Stop the autonomous agent."""
    global _autonomous_agent
    if _autonomous_agent:
        await _autonomous_agent.stop()
