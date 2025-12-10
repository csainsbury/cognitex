"""Agent Executors - DeepSeek V3 powered task execution."""

import json
from abc import ABC, abstractmethod
from typing import Any

import structlog
from together import Together

from cognitex.config import get_settings
from cognitex.agent.tools import ToolResult, get_tool_registry

logger = structlog.get_logger()


class BaseExecutor(ABC):
    """Base class for all executors."""

    name: str
    description: str

    def __init__(self):
        settings = get_settings()
        api_key = settings.together_api_key.get_secret_value()
        if not api_key:
            raise ValueError("TOGETHER_API_KEY not configured")

        self.client = Together(api_key=api_key)
        self.model = settings.together_model_executor
        self.registry = get_tool_registry()

    @abstractmethod
    async def execute(self, tool: str, args: dict, reasoning: str) -> ToolResult:
        """Execute a tool with the given arguments."""
        pass

    async def _call_llm(self, prompt: str, max_tokens: int = 1024) -> str:
        """Make an LLM call for content generation."""
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=0.4,
        )
        return response.choices[0].message.content


class EmailExecutor(BaseExecutor):
    """Executor for email-related tasks."""

    name = "email"
    description = "Handles email drafting, sending, and management"

    # User's email style (should be loaded from config/learned)
    EMAIL_STYLE = """
Writing style:
- Tone: Professional but warm, not overly formal
- Length: Concise, usually 2-4 short paragraphs
- Structure: Brief acknowledgment, main point, clear next step
- Sign-off: First name only (Chris)
- Avoid: Excessive pleasantries, filler phrases, buzzwords
"""

    async def execute(self, tool: str, args: dict, reasoning: str) -> ToolResult:
        """Execute an email tool."""
        if tool == "draft_email":
            return await self._draft_email(args, reasoning)
        else:
            # Direct tool execution
            return await self.registry.execute(tool, **args)

    async def _draft_email(self, args: dict, reasoning: str) -> ToolResult:
        """Draft an email using LLM."""
        # If body is already provided, use it directly
        if args.get("body"):
            return await self.registry.execute("draft_email", **args)

        # Otherwise, generate the body
        prompt = f"""Write an email body for the following situation.

Context: {reasoning}
To: {args.get('to', 'unknown')}
Subject: {args.get('subject', 'No subject')}

{self.EMAIL_STYLE}

Instructions:
{args.get('instructions', 'Write an appropriate response.')}

Write ONLY the email body. No subject line, no "Dear X" unless appropriate.
Start directly with the content."""

        try:
            body = await self._call_llm(prompt, max_tokens=512)
            body = body.strip()

            # Remove any accidental salutations the model might add
            if body.lower().startswith("dear ") or body.lower().startswith("hi "):
                lines = body.split("\n", 1)
                if len(lines) > 1:
                    body = lines[1].strip()

            args["body"] = body
            args["reasoning"] = reasoning

            return await self.registry.execute("draft_email", **args)

        except Exception as e:
            logger.error("Email drafting failed", error=str(e))
            return ToolResult(success=False, error=str(e))


class TaskExecutor(BaseExecutor):
    """Executor for task-related operations."""

    name = "task"
    description = "Handles task creation, updates, and management"

    async def execute(self, tool: str, args: dict, reasoning: str) -> ToolResult:
        """Execute a task tool."""
        if tool == "create_task" and not args.get("description"):
            # Generate a better description if not provided
            args["description"] = await self._generate_description(args, reasoning)

        return await self.registry.execute(tool, **args)

    async def _generate_description(self, args: dict, reasoning: str) -> str:
        """Generate a task description."""
        prompt = f"""Generate a brief task description (1-2 sentences).

Task title: {args.get('title', 'Untitled')}
Context: {reasoning}
Source: {args.get('source_email_id') or args.get('source_event_id') or 'User request'}

Be specific and actionable. Include any relevant details like deadlines or contacts."""

        try:
            description = await self._call_llm(prompt, max_tokens=128)
            return description.strip()
        except Exception:
            return reasoning[:200] if reasoning else ""


class CalendarExecutor(BaseExecutor):
    """Executor for calendar-related operations."""

    name = "calendar"
    description = "Handles event creation, modification, and scheduling"

    async def execute(self, tool: str, args: dict, reasoning: str) -> ToolResult:
        """Execute a calendar tool."""
        if tool == "create_event":
            # Validate times and add context
            if not args.get("description"):
                args["description"] = f"Created by Cognitex: {reasoning[:200]}"

        args["reasoning"] = reasoning
        return await self.registry.execute(tool, **args)


class NotifyExecutor(BaseExecutor):
    """Executor for notification operations."""

    name = "notify"
    description = "Handles sending notifications to the user"

    async def execute(self, tool: str, args: dict, reasoning: str) -> ToolResult:
        """Execute a notification tool."""
        if tool == "send_notification":
            # Format the message nicely
            message = args.get("message", "")
            if not message:
                message = await self._format_notification(args, reasoning)
                args["message"] = message

        return await self.registry.execute(tool, **args)

    async def _format_notification(self, args: dict, reasoning: str) -> str:
        """Format a notification message."""
        prompt = f"""Write a brief, helpful notification message for the user.

Context: {reasoning}
Urgency: {args.get('urgency', 'normal')}

Guidelines:
- Be concise (1-3 sentences max)
- Be specific about what needs attention
- If action is needed, make it clear
- Don't be alarmist unless truly urgent
- Use markdown formatting sparingly

Write just the notification text:"""

        try:
            message = await self._call_llm(prompt, max_tokens=128)
            return message.strip()
        except Exception:
            return reasoning[:200] if reasoning else "You have a pending item to review."


class GeneralExecutor(BaseExecutor):
    """General-purpose executor for any tool."""

    name = "general"
    description = "Handles any tool that doesn't have a specialized executor"

    async def execute(self, tool: str, args: dict, reasoning: str) -> ToolResult:
        """Execute any tool directly."""
        return await self.registry.execute(tool, **args)


class ExecutorRegistry:
    """Registry of all executors."""

    def __init__(self):
        self._executors: dict[str, BaseExecutor] = {}
        self._register_defaults()

    def _register_defaults(self):
        """Register default executors."""
        executors = [
            EmailExecutor(),
            TaskExecutor(),
            CalendarExecutor(),
            NotifyExecutor(),
            GeneralExecutor(),
        ]

        for executor in executors:
            self._executors[executor.name] = executor

    def get(self, name: str) -> BaseExecutor:
        """Get an executor by name, falling back to general."""
        return self._executors.get(name, self._executors["general"])

    async def execute(self, executor_name: str, tool: str, args: dict, reasoning: str) -> ToolResult:
        """Execute a tool through the appropriate executor."""
        executor = self.get(executor_name)
        logger.debug(
            "Executing tool",
            executor=executor.name,
            tool=tool,
            args_keys=list(args.keys()),
        )

        try:
            result = await executor.execute(tool, args, reasoning)
            logger.info(
                "Tool executed",
                executor=executor.name,
                tool=tool,
                success=result.success,
                needs_approval=result.needs_approval,
            )
            return result
        except Exception as e:
            logger.error(
                "Executor error",
                executor=executor.name,
                tool=tool,
                error=str(e),
            )
            return ToolResult(success=False, error=str(e))


# Singleton
_executor_registry: ExecutorRegistry | None = None


def get_executor_registry() -> ExecutorRegistry:
    """Get or create the executor registry singleton."""
    global _executor_registry
    if _executor_registry is None:
        _executor_registry = ExecutorRegistry()
    return _executor_registry
