"""Agent Executors - Multi-provider LLM powered task execution."""

import json
from abc import ABC, abstractmethod
from typing import Any

import structlog

from cognitex.agent.tools import ToolResult, get_tool_registry
from cognitex.services.llm import get_llm_service

logger = structlog.get_logger()


class BaseExecutor(ABC):
    """Base class for all executors - uses LLMService singleton."""

    name: str
    description: str

    def __init__(self):
        self.llm_service = get_llm_service()
        self.registry = get_tool_registry()

    @abstractmethod
    async def execute(self, tool: str, args: dict, reasoning: str) -> ToolResult:
        """Execute a tool with the given arguments."""
        pass

    async def _call_llm(self, prompt: str, max_tokens: int = 1024) -> str:
        """Make an LLM call for content generation via LLMService."""
        # Use the fast model for executor tasks (quicker responses)
        return await self.llm_service.complete(
            prompt,
            model=self.llm_service.fast_model,
            max_tokens=max_tokens,
            temperature=0.4,
        )


class EmailExecutor(BaseExecutor):
    """Executor for email-related tasks."""

    name = "email"
    description = "Handles email drafting, sending, and management"

    # Default style (used when no learned style is available)
    DEFAULT_STYLE = """
Writing style:
- Tone: Professional but warm, not overly formal
- Length: Concise, usually 2-4 short paragraphs
- Structure: Brief acknowledgment, main point, clear next step
- Sign-off: First name only
- Avoid: Excessive pleasantries, filler phrases, buzzwords
"""

    async def _get_style_guidance(self, recipient_email: str | None) -> str:
        """Get personalized style guidance for drafting emails.

        Prioritizes bootstrap voice from SOUL.md (explicit user preferences),
        falling back to learned style analyzer, then defaults.
        """
        # First try bootstrap voice (explicit user preferences)
        try:
            from cognitex.agent.bootstrap import get_bootstrap_loader
            loader = get_bootstrap_loader()
            bootstrap_voice = await loader.get_voice_guidance()

            if bootstrap_voice and len(bootstrap_voice) > 30:
                return bootstrap_voice
        except Exception as e:
            logger.debug("Failed to get bootstrap voice", error=str(e))

        # Fall back to learned style analyzer
        try:
            from cognitex.services.email_style import get_email_style_analyzer
            analyzer = get_email_style_analyzer()
            guidance = await analyzer.generate_style_guidance(recipient_email)

            if guidance and len(guidance) > 30:  # Has meaningful learned guidance
                return f"Writing style (learned from your sent emails):\n{guidance}"
        except Exception as e:
            logger.warning("Failed to get style guidance", error=str(e))

        return self.DEFAULT_STYLE

    async def execute(self, tool: str, args: dict, reasoning: str) -> ToolResult:
        """Execute an email tool."""
        if tool == "draft_email":
            return await self._draft_email(args, reasoning)
        else:
            # Direct tool execution
            return await self.registry.execute(tool, **args)

    async def _get_deep_context(self, gmail_id: str) -> dict | None:
        """Fetch deep context for an email reply."""
        if not gmail_id:
            return None

        try:
            from cognitex.db.neo4j import get_neo4j_session
            from cognitex.agent.graph_observer import GraphObserver

            async for session in get_neo4j_session():
                observer = GraphObserver(session)
                context = await observer.get_email_deep_context(gmail_id)
                return context
        except Exception as e:
            logger.warning("Failed to get deep context", gmail_id=gmail_id, error=str(e))
            return None

    async def _draft_email(self, args: dict, reasoning: str) -> ToolResult:
        """Draft an email using LLM with deep context."""
        # If body is already provided, use it directly
        if args.get("body"):
            return await self.registry.execute("draft_email", **args)

        # Fetch deep context if this is a reply
        reply_to_id = args.get("reply_to_id")
        deep_context = await self._get_deep_context(reply_to_id) if reply_to_id else None

        # Build context section for the prompt
        context_section = f"Context: {reasoning}"

        if deep_context:
            # Clinical firewall — filter before LLM
            try:
                from cognitex.config import get_settings

                settings = get_settings()
                if settings.clinical_firewall_enabled:
                    from cognitex.services.clinical_firewall import get_firewall

                    fw = get_firewall()
                    body_text = deep_context.get("full_body", "")
                    if body_text:
                        scan = fw.scan(body_text)
                        if scan.is_clinical:
                            if settings.clinical_firewall_mode == "block":
                                return ToolResult(
                                    success=False,
                                    message="Cannot draft reply: email contains clinical content (blocked by firewall)",
                                )
                            else:
                                deep_context["full_body"] = fw.filter_text(body_text)
                                for msg in deep_context.get("thread_history", []):
                                    if msg.get("body"):
                                        msg["body"] = fw.filter_text(msg["body"])
                                for i, item in enumerate(
                                    deep_context.get("action_items_extracted", [])
                                ):
                                    deep_context["action_items_extracted"][i] = (
                                        fw.filter_text(item)
                                    )
            except Exception as e:
                logger.debug("Clinical firewall check failed in executor", error=str(e))

            # Include full email body
            if deep_context.get("full_body"):
                context_section += f"\n\n--- Original Email Body ---\n{deep_context['full_body'][:3000]}"

            # Include thread history
            if deep_context.get("thread_history"):
                context_section += "\n\n--- Thread History ---"
                for msg in deep_context["thread_history"][-3:]:  # Last 3 messages
                    sender = msg.get("sender_name") or msg.get("sender_email", "Unknown")
                    body_preview = (msg.get("body") or "")[:300]
                    context_section += f"\n• From {sender}: {body_preview}..."

            # Include action items extracted from email
            if deep_context.get("action_items_extracted"):
                context_section += "\n\n--- Action Items in Email ---"
                for item in deep_context["action_items_extracted"]:
                    context_section += f"\n• {item[:150]}"

            # Include related documents
            if deep_context.get("related_documents"):
                context_section += "\n\n--- Related Documents ---"
                for doc in deep_context["related_documents"][:3]:
                    context_section += f"\n• {doc.get('name', 'Document')}"
                    if doc.get("matched_content"):
                        context_section += f": {doc['matched_content'][:100]}..."

            # Include sender context
            if deep_context.get("sender_context"):
                sc = deep_context["sender_context"]
                context_section += f"\n\n--- Sender Info ---"
                context_section += f"\n{sc.get('name', 'Unknown')} ({sc.get('org', 'Unknown org')})"
                context_section += f" - {sc.get('email_count', 0)} prior emails, {sc.get('shared_task_count', 0)} shared tasks"

        # Get personalized style guidance for this recipient
        recipient = args.get('to')
        style_guidance = await self._get_style_guidance(recipient)

        # Build the prompt with enhanced context
        prompt = f"""Write an email body for the following situation.

{context_section}

To: {args.get('to', 'unknown')}
Subject: {args.get('subject', 'No subject')}

{style_guidance}

Instructions:
{args.get('instructions', 'Write an appropriate response.')}

IMPORTANT: Address all the key points and action items from the original email.
If documents or files are mentioned, acknowledge them.
If there are specific questions, answer them.

Write ONLY the email body. No subject line, no "Dear X" unless appropriate.
Start directly with the content."""

        try:
            body = await self._call_llm(prompt, max_tokens=1024)  # Increased for richer responses
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
