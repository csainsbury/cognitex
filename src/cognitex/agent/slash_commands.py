"""Unified slash command framework for CLI, web, and Discord.

Intercepts `/command` input before the agent, executing deterministic
operations (model switching, status checks, approvals) without an LLM call.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger()


@dataclass
class SlashCommand:
    """A registered slash command."""

    name: str
    description: str
    handler: Callable[[str], Awaitable[str]]
    aliases: list[str] = field(default_factory=list)
    usage: str = ""
    category: str = "general"


@dataclass
class CommandResult:
    """Result of dispatching a slash command."""

    handled: bool
    response: str = ""
    command_name: str = ""


class SlashCommandRegistry:
    """Registry and dispatcher for slash commands."""

    def __init__(self) -> None:
        self._commands: dict[str, SlashCommand] = {}
        self._alias_map: dict[str, str] = {}
        self._initialized: bool = False

    def register(self, command: SlashCommand) -> None:
        """Register a slash command."""
        self._commands[command.name] = command
        for alias in command.aliases:
            self._alias_map[alias.lower()] = command.name

    async def dispatch(self, raw_input: str) -> CommandResult:
        """Dispatch a slash command from raw user input.

        Returns CommandResult with handled=False if not a recognized command.
        """
        raw_input = raw_input.strip()
        if not raw_input.startswith("/"):
            return CommandResult(handled=False)

        parts = raw_input[1:].split(None, 1)
        if not parts:
            return CommandResult(handled=False)

        command_name = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        # Resolve alias
        if command_name in self._alias_map:
            command_name = self._alias_map[command_name]

        cmd = self._commands.get(command_name)
        if not cmd:
            return CommandResult(handled=False)

        try:
            response = await cmd.handler(args)
            return CommandResult(handled=True, response=response, command_name=cmd.name)
        except Exception as e:
            logger.error("Slash command error", command=command_name, error=str(e))
            return CommandResult(
                handled=True,
                response=f"Error executing /{command_name}: {e}",
                command_name=command_name,
            )

    def list_commands(self) -> list[dict[str, Any]]:
        """List all registered commands (deduplicated, no aliases)."""
        result = []
        for cmd in self._commands.values():
            result.append({
                "name": cmd.name,
                "description": cmd.description,
                "usage": cmd.usage,
                "category": cmd.category,
                "aliases": cmd.aliases,
            })
        return sorted(result, key=lambda c: (c["category"], c["name"]))

    async def initialize(self) -> None:
        """Register all built-in commands and skill-based commands."""
        if self._initialized:
            return
        _register_builtin_commands(self)
        await _register_skill_commands(self)
        self._initialized = True


# ---------------------------------------------------------------------------
# Built-in handlers
# ---------------------------------------------------------------------------


def _is_known_model(model_id: str) -> bool:
    """Check if a model ID appears in any provider's model list or aliases."""
    from cognitex.services.model_config import (
        ANTHROPIC_CHAT_MODELS,
        GOOGLE_CHAT_MODELS,
        MODEL_ALIASES,
        OPENAI_CHAT_MODELS,
        OPENROUTER_CHAT_MODELS,
        TOGETHER_CHAT_MODELS,
    )

    all_ids = {m["id"] for m in (
        TOGETHER_CHAT_MODELS + ANTHROPIC_CHAT_MODELS + OPENAI_CHAT_MODELS
        + GOOGLE_CHAT_MODELS + OPENROUTER_CHAT_MODELS
    )}
    return model_id in all_ids or model_id in MODEL_ALIASES


async def _handle_model(args: str) -> str:
    """Show or switch model configuration."""
    from cognitex.services.llm import reset_llm_service
    from cognitex.services.model_config import (
        MODEL_ALIASES,
        TASK_MODEL_SLOTS,
        get_model_config_service,
    )

    svc = get_model_config_service()
    config = await svc.get_config()

    if not args.strip():
        # Show current config
        lines = [
            f"Provider: {config.provider}",
            f"Orchestrator (planner): {config.planner_model}",
            f"Executor: {config.executor_model}",
            "",
            "Per-task overrides:",
        ]
        for slot in TASK_MODEL_SLOTS:
            val = getattr(config, f"{slot}_model", "")
            lines.append(f"  {slot}: {val or '(default)'}")

        # Show custom sub-agents
        try:
            from cognitex.agent.subagent import get_subagent_registry
            registry = get_subagent_registry()
            all_agents = await registry.get_all()
            custom = [a for a in all_agents.values() if not a.is_builtin]
            if custom:
                lines.append("\nCustom sub-agents:")
                for a in custom:
                    model_str = a.model or "(inherit)"
                    lines.append(f"  {a.name}: {model_str} — {a.purpose}")
        except Exception:
            pass

        lines.append(f"\nAliases: {', '.join(sorted(MODEL_ALIASES.keys()))}")
        return "\n".join(lines)

    parts = args.strip().split()

    # `/model executor <alias>` — change only executor
    if len(parts) >= 2 and parts[0] == "executor":
        alias_or_id = parts[1]
        if alias_or_id in MODEL_ALIASES:
            provider, model_id = MODEL_ALIASES[alias_or_id]
            config.provider = provider
        else:
            model_id = alias_or_id
        config.executor_model = model_id
        await svc.set_config(config)
        reset_llm_service()
        warning = ""
        if alias_or_id not in MODEL_ALIASES and not _is_known_model(model_id):
            warning = "\n(not in known model list — may not work)"
        return (
            f"Executor changed to {model_id}\n"
            f"Orchestrator unchanged: {config.planner_model}{warning}"
        )

    # `/model <slot> <alias>` — per-task override
    if len(parts) >= 2 and parts[0] in TASK_MODEL_SLOTS:
        slot = parts[0]
        alias_or_id = parts[1]
        if alias_or_id in MODEL_ALIASES:
            _, model_id = MODEL_ALIASES[alias_or_id]
        else:
            model_id = alias_or_id
        await svc.update_task_model(slot, model_id)
        return f"Set {slot} model to {model_id}"

    # Single arg: alias or model ID — change ONLY orchestrator (planner)
    alias_or_id = parts[0]
    if alias_or_id in MODEL_ALIASES:
        provider, model_id = MODEL_ALIASES[alias_or_id]
        config.provider = provider
        config.planner_model = model_id
        await svc.set_config(config)
        reset_llm_service()
        return (
            f"Orchestrator changed to {alias_or_id} ({provider}/{model_id})\n"
            f"Executor unchanged: {config.executor_model}"
        )

    # Treat as raw model ID — change only orchestrator
    config.planner_model = alias_or_id
    await svc.set_config(config)
    reset_llm_service()
    warning = ""
    if not _is_known_model(alias_or_id):
        warning = "\n(not in known model list — may not work)"
    return (
        f"Orchestrator changed to {alias_or_id}\n"
        f"Executor unchanged: {config.executor_model}{warning}"
    )


async def _handle_provider(args: str) -> str:
    """Switch LLM provider."""
    from cognitex.services.llm import reset_llm_service
    from cognitex.services.model_config import PROVIDERS, get_model_config_service

    name = args.strip().lower()
    if not name:
        return f"Usage: /provider <name>\nAvailable: {', '.join(PROVIDERS.keys())}"

    if name not in PROVIDERS:
        return f"Unknown provider '{name}'. Available: {', '.join(PROVIDERS.keys())}"

    svc = get_model_config_service()
    config = await svc.get_config()

    # Get default models for provider
    chat_models = svc.get_chat_models_for_provider(name)
    default_model = chat_models[0]["id"] if chat_models else config.planner_model

    config.provider = name
    config.planner_model = default_model
    config.executor_model = default_model
    await svc.set_config(config)
    reset_llm_service()
    return f"Switched to {PROVIDERS[name]} (model: {default_model})"


async def _handle_status(_args: str) -> str:
    """Show system status summary."""
    lines = []

    # Operating mode
    try:
        from cognitex.agent.state_model import get_state_estimator

        estimator = get_state_estimator()
        state = await estimator.get_current_state()
        lines.append(f"Mode: {state.mode.value}")
    except Exception:
        lines.append("Mode: unknown")

    # Model config
    try:
        from cognitex.services.model_config import get_model_config_service

        config = await get_model_config_service().get_config()
        lines.append(f"Model: {config.provider}/{config.planner_model}")
    except Exception:
        lines.append("Model: unknown")

    # Pending inbox items
    try:
        from cognitex.services.inbox import get_inbox_service

        counts = await get_inbox_service().get_pending_count()
        total = sum(counts.values())
        lines.append(f"Pending inbox: {total}")
    except Exception:
        lines.append("Pending inbox: unknown")

    return "\n".join(lines)


async def _handle_mode(args: str) -> str:
    """Set operating mode."""
    from cognitex.db.phase3_schema import OperatingMode

    name = args.strip().lower().replace(" ", "_")
    if not name:
        modes = [m.value for m in OperatingMode]
        return f"Usage: /mode <mode>\nAvailable: {', '.join(modes)}"

    try:
        mode = OperatingMode(name)
    except ValueError:
        modes = [m.value for m in OperatingMode]
        return f"Unknown mode '{name}'. Available: {', '.join(modes)}"

    from cognitex.agent.state_model import get_state_estimator

    estimator = get_state_estimator()
    await estimator.infer_state(explicit_signals={"mode_override": mode.value})
    return f"Mode set to {mode.value}"


async def _handle_skills(_args: str) -> str:
    """List available skills."""
    from cognitex.agent.skills import get_skills_loader

    loader = get_skills_loader()
    skills = await loader.list_skills()

    if not skills:
        return "No skills loaded."

    lines = []
    for s in skills:
        status = "ok" if s["eligible"] else "ineligible"
        invocable = " [invocable]" if s.get("user_invocable") else ""
        lines.append(f"  {s['name']} ({s['source']}, {status}{invocable}): {s['purpose']}")
    return f"Skills ({len(skills)}):\n" + "\n".join(lines)


async def _handle_skill(args: str) -> str:
    """Stub for skill install/create."""
    parts = args.strip().split()
    if not parts:
        return "Usage: /skill install <url>  or  /skill create <name>"

    action = parts[0].lower()
    if action == "install":
        return (
            "To install a skill, place a SKILL.md folder in ~/.cognitex/skills/<name>/\n"
            "See docs for AgentSkills frontmatter format."
        )
    elif action == "create":
        name = parts[1] if len(parts) > 1 else "<name>"
        return (
            f"To create skill '{name}':\n"
            f"  mkdir -p ~/.cognitex/skills/{name}\n"
            f"  # Edit ~/.cognitex/skills/{name}/SKILL.md"
        )
    return f"Unknown skill action: {action}. Use 'install' or 'create'."


async def _handle_briefing(_args: str) -> str:
    """Trigger a morning briefing."""
    try:
        from cognitex.agent.core import Agent

        agent = Agent()
        await agent.initialize()
        briefing = await agent.morning_briefing()
        return briefing or "Briefing generated (empty response)."
    except Exception as e:
        return f"Failed to generate briefing: {e}"


async def _handle_next(_args: str) -> str:
    """Show next pending inbox items."""
    from cognitex.services.inbox import get_inbox_service

    inbox = get_inbox_service()
    items = await inbox.get_pending_items(limit=5)

    if not items:
        return "No pending items."

    lines = []
    for item in items:
        lines.append(f"  [{item.item_id[:8]}] {item.item_type}: {item.title}")
    return f"Next {len(items)} pending:\n" + "\n".join(lines)


async def _handle_approve(args: str) -> str:
    """Approve an inbox item."""
    item_id = args.strip()
    if not item_id:
        return "Usage: /approve <item-id>"

    from cognitex.services.inbox import get_inbox_service

    inbox = get_inbox_service()
    item = await inbox.approve_item(item_id)
    if item:
        return f"Approved: {item.title}"
    return f"Item not found: {item_id}"


async def _handle_reject(args: str) -> str:
    """Reject an inbox item."""
    parts = args.strip().split(None, 1)
    if not parts:
        return "Usage: /reject <item-id> [reason]"

    item_id = parts[0]
    reason = parts[1] if len(parts) > 1 else "user_rejected"

    from cognitex.services.inbox import get_inbox_service

    inbox = get_inbox_service()
    item = await inbox.reject_item(item_id, reason_category="user_rejected", reason_text=reason)
    if item:
        return f"Rejected: {item.title}"
    return f"Item not found: {item_id}"


async def _handle_email(args: str) -> str:
    """Show email provider status or trigger sync."""
    from cognitex.services.email_provider import get_email_provider

    provider = get_email_provider()
    sub = args.strip().lower()

    if sub == "sync":
        if provider.provider_name == "agentmail":
            from cognitex.services.agentmail import get_agentmail_service

            svc = get_agentmail_service()
            if not svc:
                return "AgentMail enabled but service unavailable (check API key)."
            messages = await svc.get_messages(limit=50)
            return f"Fetched {len(messages)} recent messages from AgentMail."
        return "Gmail sync: use `cognitex sync` CLI command."

    try:
        profile = await provider.get_profile()
    except Exception as e:
        return f"Provider: {provider.provider_name} (error: {e})"

    if provider.provider_name == "agentmail":
        return (
            f"Provider: AgentMail\n"
            f"Inbox: {profile.get('inbox_id', 'unknown')}\n"
            f"Display name: {profile.get('display_name', 'N/A')}"
        )
    return (
        f"Provider: Gmail\n"
        f"Email: {profile.get('email', 'unknown')}\n"
        f"Total messages: {profile.get('messages_total', 'unknown')}"
    )


async def _handle_help(_args: str) -> str:
    """List all commands."""
    registry = get_slash_registry()
    commands = registry.list_commands()

    categories: dict[str, list[dict]] = {}
    for cmd in commands:
        cat = cmd["category"]
        categories.setdefault(cat, []).append(cmd)

    lines = []
    for cat, cmds in sorted(categories.items()):
        lines.append(f"\n{cat.upper()}:")
        for cmd in cmds:
            alias_str = f" (aliases: {', '.join(cmd['aliases'])})" if cmd["aliases"] else ""
            usage = f" {cmd['usage']}" if cmd["usage"] else ""
            lines.append(f"  /{cmd['name']}{usage} — {cmd['description']}{alias_str}")
    return "Available commands:" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def _register_builtin_commands(registry: SlashCommandRegistry) -> None:
    """Register all built-in slash commands."""
    registry.register(SlashCommand(
        name="model",
        description="Show or switch model",
        handler=_handle_model,
        aliases=["m"],
        usage="[alias | executor <alias> | <slot> <alias>]",
        category="config",
    ))
    registry.register(SlashCommand(
        name="provider",
        description="Switch LLM provider",
        handler=_handle_provider,
        usage="<name>",
        category="config",
    ))
    registry.register(SlashCommand(
        name="status",
        description="Show system status",
        handler=_handle_status,
        aliases=["st"],
        category="general",
    ))
    registry.register(SlashCommand(
        name="mode",
        description="Set operating mode",
        handler=_handle_mode,
        usage="<mode>",
        category="config",
    ))
    registry.register(SlashCommand(
        name="skills",
        description="List available skills",
        handler=_handle_skills,
        category="general",
    ))
    registry.register(SlashCommand(
        name="skill",
        description="Install or create a skill",
        handler=_handle_skill,
        usage="install|create [name]",
        category="general",
    ))
    registry.register(SlashCommand(
        name="briefing",
        description="Trigger morning briefing",
        handler=_handle_briefing,
        category="agent",
    ))
    registry.register(SlashCommand(
        name="next",
        description="Show next pending inbox items",
        handler=_handle_next,
        aliases=["n"],
        category="inbox",
    ))
    registry.register(SlashCommand(
        name="approve",
        description="Approve an inbox item",
        handler=_handle_approve,
        usage="<item-id>",
        category="inbox",
    ))
    registry.register(SlashCommand(
        name="reject",
        description="Reject an inbox item",
        handler=_handle_reject,
        usage="<item-id> [reason]",
        category="inbox",
    ))
    registry.register(SlashCommand(
        name="email",
        description="Show email provider status or sync",
        handler=_handle_email,
        usage="[sync]",
        category="config",
    ))
    registry.register(SlashCommand(
        name="help",
        description="Show all commands",
        handler=_handle_help,
        aliases=["h", "?"],
        category="general",
    ))


def _make_skill_handler(name: str) -> Callable[[str], Awaitable[str]]:
    """Create a slash command handler for a user-invocable skill."""

    async def handler(args: str) -> str:
        from cognitex.agent.core import Agent

        agent = Agent()
        await agent.initialize()
        prompt = f"[Skill: {name}] {args}" if args else f"[Skill: {name}]"
        return await agent.chat(prompt)

    return handler


async def _register_skill_commands(registry: SlashCommandRegistry) -> None:
    """Register slash commands for user-invocable skills."""
    try:
        from cognitex.agent.skills import get_skills_loader

        loader = get_skills_loader()
        skills = await loader.list_skills()

        for s in skills:
            if s.get("user_invocable") and s.get("eligible", True):
                skill_name = s["name"]
                registry.register(SlashCommand(
                    name=skill_name,
                    description=s.get("description") or s.get("purpose", ""),
                    handler=_make_skill_handler(skill_name),
                    category="skills",
                ))
    except Exception as e:
        logger.warning("Failed to register skill commands", error=str(e))


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_slash_registry: SlashCommandRegistry | None = None


def get_slash_registry() -> SlashCommandRegistry:
    """Get the slash command registry singleton."""
    global _slash_registry
    if _slash_registry is None:
        _slash_registry = SlashCommandRegistry()
    return _slash_registry
