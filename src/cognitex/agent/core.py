"""Agent Core - Main orchestrator for the Cognitex agent system."""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import structlog

from cognitex.agent.memory import Memory, init_memory, get_memory
from cognitex.agent.planner import Planner, Plan, PlanStep, AgentMode, get_planner
from cognitex.agent.executors import get_executor_registry
from cognitex.agent.tools import ToolRisk, ToolResult

logger = structlog.get_logger()


@dataclass
class ExecutionResult:
    """Result from executing a plan."""
    success: bool
    steps_executed: int
    steps_total: int
    results: list[ToolResult]
    pending_approvals: list[str]
    errors: list[str]
    user_notification: str | None


class Agent:
    """
    Main agent orchestrator.

    Coordinates:
    - Memory (working + episodic)
    - Planner (Qwen3-30B-A3B)
    - Executors (DeepSeek V3)
    - Tools

    Handles the full observe → think → plan → act loop.
    """

    def __init__(self):
        self.memory: Memory | None = None
        self.planner: Planner | None = None
        self.executor_registry = get_executor_registry()
        self._initialized = False

    async def initialize(self) -> None:
        """Initialize the agent and all subsystems."""
        if self._initialized:
            return

        logger.info("Initializing agent")

        # Initialize memory
        self.memory = await init_memory()

        # Initialize planner
        self.planner = get_planner()

        self._initialized = True
        logger.info("Agent initialized")

    def _ensure_initialized(self) -> None:
        """Ensure agent is initialized."""
        if not self._initialized:
            raise RuntimeError("Agent not initialized. Call initialize() first.")

    async def run(
        self,
        mode: AgentMode,
        trigger: str,
        trigger_data: dict | None = None,
    ) -> ExecutionResult:
        """
        Run the agent for a given trigger.

        This is the main entry point for agent execution.

        Args:
            mode: Operating mode
            trigger: Description of what triggered this run
            trigger_data: Additional trigger data

        Returns:
            ExecutionResult with what happened
        """
        self._ensure_initialized()

        logger.info("Agent run starting", mode=mode.value, trigger=trigger[:100])

        # Build context from memory
        context = await self.memory.build_context(trigger)

        # Plan
        plan = await self.planner.plan(
            mode=mode,
            context=context,
            trigger=trigger,
            trigger_data=trigger_data,
        )

        # Store planning decision in memory
        await self.memory.episodic.store(
            content=f"Trigger: {trigger}\nReasoning: {plan.reasoning}\nSteps: {len(plan.steps)}",
            memory_type="decision",
            importance=3,
            metadata={
                "mode": mode.value,
                "confidence": plan.confidence,
                "step_count": len(plan.steps),
            },
        )

        # Execute plan
        result = await self._execute_plan(plan)

        # Record interaction in working memory
        await self.memory.working.add_interaction(
            role="agent",
            content=f"Executed {result.steps_executed}/{result.steps_total} steps",
            metadata={
                "mode": mode.value,
                "success": result.success,
                "pending_approvals": result.pending_approvals,
            },
        )

        logger.info(
            "Agent run complete",
            mode=mode.value,
            steps_executed=result.steps_executed,
            pending_approvals=len(result.pending_approvals),
            success=result.success,
        )

        return result

    async def _execute_plan(self, plan: Plan) -> ExecutionResult:
        """Execute a plan step by step."""
        results = []
        pending_approvals = []
        errors = []
        steps_executed = 0

        for step in plan.steps:
            try:
                result = await self._execute_step(step)
                results.append(result)

                if result.success:
                    steps_executed += 1

                    if result.needs_approval and result.approval_id:
                        pending_approvals.append(result.approval_id)
                else:
                    if result.error:
                        errors.append(f"{step.tool}: {result.error}")

            except Exception as e:
                logger.error("Step execution failed", step=step.tool, error=str(e))
                errors.append(f"{step.tool}: {str(e)}")
                results.append(ToolResult(success=False, error=str(e)))

        return ExecutionResult(
            success=len(errors) == 0,
            steps_executed=steps_executed,
            steps_total=len(plan.steps),
            results=results,
            pending_approvals=pending_approvals,
            errors=errors,
            user_notification=plan.user_notification,
        )

    async def _execute_step(self, step: PlanStep) -> ToolResult:
        """Execute a single plan step."""
        logger.debug(
            "Executing step",
            executor=step.executor,
            tool=step.tool,
            risk=step.risk.value,
        )

        return await self.executor_registry.execute(
            executor_name=step.executor,
            tool=step.tool,
            args=step.args,
            reasoning=step.reasoning,
        )

    async def chat(self, message: str) -> str:
        """
        Handle a conversational message from the user.

        Args:
            message: User's message

        Returns:
            Agent's response
        """
        self._ensure_initialized()

        logger.info("Chat message received", length=len(message))

        # Record user message
        await self.memory.working.add_interaction(
            role="user",
            content=message,
        )

        # Get response and plan from planner
        context = await self.memory.build_context(f"User chat: {message}")
        response, plan = await self.planner.respond_to_user(context, message)

        # Execute any planned actions
        if plan:
            result = await self._execute_plan(plan)

            # Append info about actions to response if needed
            if result.pending_approvals:
                response += f"\n\n_(Staged {len(result.pending_approvals)} action(s) for your approval)_"

        # Record response
        await self.memory.working.add_interaction(
            role="agent",
            content=response,
        )

        return response

    async def handle_approval(self, approval_id: str, approved: bool, feedback: str | None = None) -> dict:
        """
        Handle user approval or rejection of a staged action.

        Args:
            approval_id: ID of the approval request
            approved: Whether the user approved
            feedback: Optional feedback from user

        Returns:
            Result of the approval handling
        """
        self._ensure_initialized()

        logger.info("Handling approval", approval_id=approval_id, approved=approved)

        # Get and resolve the approval
        approval = await self.memory.working.resolve_approval(approval_id, approved, feedback)

        if not approval:
            return {"success": False, "error": "Approval not found or expired"}

        result = {"success": True, "approval_id": approval_id, "action": approval["action_type"]}

        if approved:
            # Execute the approved action
            action_type = approval["action_type"]
            params = approval["params"]

            if action_type == "send_email":
                # Actually send the email via Gmail API
                from cognitex.services.gmail import GmailService
                gmail = GmailService()

                try:
                    if params.get("reply_to_id"):
                        sent = gmail.send_reply(
                            thread_id=params["reply_to_id"],
                            to=params["to"],
                            subject=params["subject"],
                            body=params["body"],
                        )
                    else:
                        sent = gmail.send_message(
                            to=params["to"],
                            subject=params["subject"],
                            body=params["body"],
                        )
                    result["sent"] = True
                    result["message_id"] = sent.get("id")

                    # Store in memory
                    await self.memory.episodic.store(
                        content=f"Sent email to {params['to']}: {params['subject']}",
                        memory_type="interaction",
                        importance=4,
                        entities=[params["to"]],
                    )

                except Exception as e:
                    result["success"] = False
                    result["error"] = str(e)

            elif action_type == "create_event":
                # Create the calendar event
                from cognitex.services.calendar import CalendarService
                calendar = CalendarService()

                try:
                    event = calendar.create_event(
                        title=params["title"],
                        start=params["start"],
                        end=params["end"],
                        attendees=params.get("attendees"),
                        description=params.get("description"),
                    )
                    result["created"] = True
                    result["event_id"] = event.get("id")

                    # Store in memory
                    await self.memory.episodic.store(
                        content=f"Created event: {params['title']} at {params['start']}",
                        memory_type="interaction",
                        importance=3,
                    )

                except Exception as e:
                    result["success"] = False
                    result["error"] = str(e)

        else:
            # User rejected - store feedback for learning
            if feedback:
                await self.memory.episodic.store(
                    content=f"User rejected {approval['action_type']}: {feedback}",
                    memory_type="feedback",
                    importance=4,
                    metadata={"approval_id": approval_id, "action_type": approval["action_type"]},
                )

        return result

    async def get_pending_approvals(self) -> list[dict]:
        """Get all pending approval requests."""
        self._ensure_initialized()
        return await self.memory.working.get_pending_approvals()

    async def morning_briefing(self) -> str:
        """Generate and return a morning briefing."""
        result = await self.run(
            mode=AgentMode.BRIEFING,
            trigger="Scheduled morning briefing",
            trigger_data={"time": "morning"},
        )

        return result.user_notification or "Good morning! No urgent items today."

    async def evening_review(self) -> str:
        """Generate and return an evening review."""
        result = await self.run(
            mode=AgentMode.REVIEW,
            trigger="Scheduled evening review",
            trigger_data={"time": "evening"},
        )

        return result.user_notification or "End of day review complete. Rest well!"

    async def process_new_email(self, email_data: dict) -> ExecutionResult:
        """Process a newly received email."""
        return await self.run(
            mode=AgentMode.PROCESS_EMAIL,
            trigger=f"New email from {email_data.get('sender_email', 'unknown')}: {email_data.get('subject', 'No subject')}",
            trigger_data=email_data,
        )

    async def process_calendar_change(self, event_data: dict) -> ExecutionResult:
        """Process a calendar change."""
        return await self.run(
            mode=AgentMode.PROCESS_EVENT,
            trigger=f"Calendar change: {event_data.get('title', 'Unknown event')}",
            trigger_data=event_data,
        )

    async def check_for_urgent(self) -> ExecutionResult:
        """Hourly check for urgent items."""
        return await self.run(
            mode=AgentMode.MONITOR,
            trigger="Scheduled hourly monitoring check",
        )


# Singleton
_agent: Agent | None = None


async def get_agent() -> Agent:
    """Get or create the agent singleton."""
    global _agent
    if _agent is None:
        _agent = Agent()
        await _agent.initialize()
    return _agent


# Re-export AgentMode for convenience
__all__ = ["Agent", "AgentMode", "ExecutionResult", "get_agent"]
