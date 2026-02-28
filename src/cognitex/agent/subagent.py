"""Sub-agent infrastructure: config, registry, runner, and spawn tool.

Provides named and ad-hoc sub-agents that the orchestrator can spawn at runtime.
Each sub-agent has its own model, tool access, and iteration limit.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

import structlog

from cognitex.agent.tools import BaseTool, ToolCategory, ToolResult, ToolRisk

logger = structlog.get_logger()

# Maximum nesting depth for sub-agent spawning
MAX_SPAWN_DEPTH = 2

# Redis key for user-defined sub-agent configs
REDIS_KEY = "cognitex:subagents"


# ---------------------------------------------------------------------------
# SubAgentConfig
# ---------------------------------------------------------------------------


@dataclass
class SubAgentConfig:
    """Configuration for a named sub-agent."""

    name: str
    purpose: str
    model: str = ""  # empty = inherit from orchestrator
    provider: str = ""  # empty = inherit from orchestrator
    allowed_tools: list[str] = field(default_factory=list)
    denied_tools: list[str] = field(default_factory=list)
    max_iterations: int = 5
    system_prompt_extra: str = ""
    is_builtin: bool = False
    legacy_task_slot: str = ""  # bridges to TASK_MODEL_SLOTS in model_config.py

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SubAgentConfig:
        return cls(
            name=data.get("name", ""),
            purpose=data.get("purpose", ""),
            model=data.get("model", ""),
            provider=data.get("provider", ""),
            allowed_tools=data.get("allowed_tools", []),
            denied_tools=data.get("denied_tools", []),
            max_iterations=data.get("max_iterations", 5),
            system_prompt_extra=data.get("system_prompt_extra", ""),
            is_builtin=data.get("is_builtin", False),
            legacy_task_slot=data.get("legacy_task_slot", ""),
        )


# Built-in sub-agents (backwards-compatible with existing task model slots)
BUILTIN_SUBAGENTS: dict[str, SubAgentConfig] = {
    "autonomous": SubAgentConfig(
        name="autonomous",
        purpose="Autonomous background agent loop",
        is_builtin=True,
        legacy_task_slot="autonomous",
        denied_tools=["draft_email", "create_event"],
        max_iterations=8,
    ),
    "triage": SubAgentConfig(
        name="triage",
        purpose="Email triage and classification",
        is_builtin=True,
        legacy_task_slot="triage",
        allowed_tools=[
            "graph_query", "search_documents", "get_contact", "recall_memory",
        ],
        max_iterations=3,
    ),
    "drafter": SubAgentConfig(
        name="drafter",
        purpose="Draft composition for emails and documents",
        is_builtin=True,
        legacy_task_slot="draft",
        allowed_tools=[
            "graph_query", "get_contact", "recall_memory",
            "search_documents", "draft_email",
        ],
        max_iterations=5,
    ),
    "context-pack": SubAgentConfig(
        name="context-pack",
        purpose="Meeting context pack assembly",
        is_builtin=True,
        legacy_task_slot="context_pack",
        allowed_tools=[
            "graph_query", "search_documents", "get_calendar",
            "get_contact", "recall_memory", "analyze_document",
        ],
        max_iterations=5,
    ),
    "skill-evolution": SubAgentConfig(
        name="skill-evolution",
        purpose="Skill analysis and evolution",
        is_builtin=True,
        legacy_task_slot="skill_evolution",
        allowed_tools=[
            "graph_query", "recall_memory", "search_documents",
        ],
        max_iterations=3,
    ),
}


# ---------------------------------------------------------------------------
# SubAgentRegistry
# ---------------------------------------------------------------------------


class SubAgentRegistry:
    """Registry for sub-agent configs, backed by Redis for user-defined agents."""

    def __init__(self) -> None:
        self._redis = None

    async def _get_redis(self):
        import redis.asyncio as redis
        if self._redis is None:
            from cognitex.config import get_settings
            settings = get_settings()
            self._redis = redis.from_url(settings.redis_url)
        return self._redis

    async def _load_user_agents(self) -> dict[str, SubAgentConfig]:
        """Load user-defined sub-agents from Redis."""
        try:
            r = await self._get_redis()
            data = await r.get(REDIS_KEY)
            if data:
                agents_raw = json.loads(data)
                return {
                    name: SubAgentConfig.from_dict(cfg)
                    for name, cfg in agents_raw.items()
                }
        except Exception as e:
            logger.warning("Failed to load user sub-agents from Redis", error=str(e))
        return {}

    async def _save_user_agents(self, agents: dict[str, SubAgentConfig]) -> None:
        """Persist user-defined sub-agents to Redis."""
        r = await self._get_redis()
        data = {name: cfg.to_dict() for name, cfg in agents.items()}
        await r.set(REDIS_KEY, json.dumps(data))

    async def get_all(self) -> dict[str, SubAgentConfig]:
        """Get all sub-agents (builtins merged with user-defined)."""
        result = dict(BUILTIN_SUBAGENTS)
        user_agents = await self._load_user_agents()
        result.update(user_agents)
        return result

    async def get(self, name: str) -> SubAgentConfig | None:
        """Get a single sub-agent config by name."""
        if name in BUILTIN_SUBAGENTS:
            return BUILTIN_SUBAGENTS[name]
        user_agents = await self._load_user_agents()
        return user_agents.get(name)

    async def save_user_agent(self, config: SubAgentConfig) -> None:
        """Create or update a user-defined sub-agent."""
        if config.name in BUILTIN_SUBAGENTS:
            raise ValueError(f"Cannot overwrite builtin sub-agent '{config.name}'")
        config.is_builtin = False
        agents = await self._load_user_agents()
        agents[config.name] = config
        await self._save_user_agents(agents)
        logger.info("Saved user sub-agent", name=config.name)

    async def delete_user_agent(self, name: str) -> bool:
        """Delete a user-defined sub-agent. Returns True if deleted."""
        if name in BUILTIN_SUBAGENTS:
            raise ValueError(f"Cannot delete builtin sub-agent '{name}'")
        agents = await self._load_user_agents()
        if name not in agents:
            return False
        del agents[name]
        await self._save_user_agents(agents)
        logger.info("Deleted user sub-agent", name=name)
        return True

    async def update_builtin_model(self, name: str, model: str) -> None:
        """Update a builtin sub-agent's model via its legacy task slot."""
        builtin = BUILTIN_SUBAGENTS.get(name)
        if not builtin or not builtin.legacy_task_slot:
            raise ValueError(f"Not a builtin with legacy slot: '{name}'")
        from cognitex.services.model_config import get_model_config_service
        svc = get_model_config_service()
        await svc.update_task_model(builtin.legacy_task_slot, model)
        logger.info(
            "Updated builtin sub-agent model via legacy slot",
            name=name,
            slot=builtin.legacy_task_slot,
            model=model,
        )


# Singleton
_subagent_registry: SubAgentRegistry | None = None


def get_subagent_registry() -> SubAgentRegistry:
    """Get the sub-agent registry singleton."""
    global _subagent_registry
    if _subagent_registry is None:
        _subagent_registry = SubAgentRegistry()
    return _subagent_registry


# ---------------------------------------------------------------------------
# SubAgentResult
# ---------------------------------------------------------------------------


@dataclass
class SubAgentResult:
    """Result from running a sub-agent."""

    agent_name: str
    success: bool
    response: str
    steps_taken: int = 0
    error: str | None = None


# ---------------------------------------------------------------------------
# SubAgent runner
# ---------------------------------------------------------------------------


class SubAgent:
    """Runs a mini ReAct loop with a scoped tool set and model."""

    def __init__(
        self,
        config: SubAgentConfig,
        parent_model: str = "",
        parent_provider: str = "",
    ) -> None:
        self.config = config
        self._parent_model = parent_model
        self._parent_provider = parent_provider

    async def _resolve_model(self) -> tuple[str, str]:
        """Resolve which model and provider to use.

        Chain: explicit config → legacy task slot override → parent model → global planner.
        """
        # 1. Explicit config
        if self.config.model and self.config.provider:
            return self.config.model, self.config.provider

        # 2. Legacy task slot override
        if self.config.legacy_task_slot:
            try:
                from cognitex.services.model_config import get_model_config_service
                mc = await get_model_config_service().get_config()
                override = mc.get_model_for_task(self.config.legacy_task_slot)
                if override and override != mc.planner_model:
                    return override, mc.provider
            except Exception:
                pass

        # 3. Parent model
        if self._parent_model and self._parent_provider:
            return self._parent_model, self._parent_provider

        # 4. Global planner
        try:
            from cognitex.services.model_config import get_model_config_service
            mc = await get_model_config_service().get_config()
            return mc.planner_model, mc.provider
        except Exception:
            return "deepseek-ai/DeepSeek-V3", "together"

    def _filter_tools(self, all_tools: list[BaseTool]) -> list[BaseTool]:
        """Filter tools based on allowed/denied lists."""
        # Never give sub-agents the spawn tool
        excluded = {"spawn_subagent"}

        if self.config.allowed_tools:
            return [
                t for t in all_tools
                if t.name in self.config.allowed_tools and t.name not in excluded
            ]

        # Default: all non-APPROVAL tools minus denied
        denied = set(self.config.denied_tools) | excluded
        return [
            t for t in all_tools
            if t.risk != ToolRisk.APPROVAL and t.name not in denied
        ]

    async def run(self, task: str, context: str = "") -> SubAgentResult:
        """Execute the sub-agent's mini ReAct loop."""
        from cognitex.agent.tools import get_tool_registry
        from cognitex.services.llm import get_llm_service

        model_id, provider = await self._resolve_model()
        llm = get_llm_service()
        registry = get_tool_registry()
        tools = self._filter_tools(registry.all())

        tool_map = {t.name: t for t in tools}
        tool_desc = "\n".join(
            f"- {t.name}: {t.description} | Params: "
            + ", ".join(f"{k}: {v.get('type', 'any')}" for k, v in t.parameters.items())
            for t in tools
        )

        system = (
            f"You are a sub-agent named '{self.config.name}'. "
            f"Purpose: {self.config.purpose}\n\n"
            f"Available tools:\n{tool_desc}\n\n"
            "Respond with JSON: "
            '{"thought": "...", "action": "tool_name", "action_input": {...}} '
            "or when done: "
            '{"thought": "...", "action": null, "response": "..."}\n'
        )
        if self.config.system_prompt_extra:
            system += f"\n{self.config.system_prompt_extra}\n"

        messages: list[dict[str, str]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": f"Task: {task}"},
        ]
        if context:
            messages.append({"role": "user", "content": f"Context: {context}"})

        steps = 0
        for _ in range(self.config.max_iterations):
            steps += 1
            try:
                raw = await llm.complete_messages(
                    messages=messages,
                    model=model_id,
                    provider=provider,
                    max_tokens=2048,
                    temperature=0.3,
                )
            except Exception as e:
                return SubAgentResult(
                    agent_name=self.config.name,
                    success=False,
                    response="",
                    steps_taken=steps,
                    error=f"LLM error: {e}",
                )

            messages.append({"role": "assistant", "content": raw})

            # Parse response
            parsed = _parse_json(raw)
            action = parsed.get("action")

            if action is None:
                # Done
                return SubAgentResult(
                    agent_name=self.config.name,
                    success=True,
                    response=parsed.get("response", raw),
                    steps_taken=steps,
                )

            # Execute tool
            tool = tool_map.get(action)
            if not tool:
                observation = f"Error: unknown tool '{action}'. Available: {list(tool_map.keys())}"
            else:
                try:
                    result = await tool.execute(**parsed.get("action_input", {}))
                    observation = (
                        str(result.data) if result.success
                        else f"Tool error: {result.error}"
                    )
                except Exception as e:
                    observation = f"Tool execution error: {e}"

            messages.append({"role": "user", "content": f"Observation: {observation}"})

        # Exhausted iterations
        return SubAgentResult(
            agent_name=self.config.name,
            success=True,
            response=f"Sub-agent reached iteration limit ({self.config.max_iterations}). "
            f"Last output: {messages[-1]['content'][:500]}",
            steps_taken=steps,
        )


def _parse_json(text: str) -> dict:
    """Best-effort JSON extraction from LLM output."""
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0]
    elif "```" in text:
        text = text.split("```")[1].split("```")[0]
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        # Fallback: treat as final response
        return {"thought": text, "action": None, "response": text}


# ---------------------------------------------------------------------------
# SpawnSubAgentTool
# ---------------------------------------------------------------------------


class SpawnSubAgentTool(BaseTool):
    """Spawn a named or ad-hoc sub-agent to handle a delegated task."""

    name = "spawn_subagent"
    description = (
        "Spawn a sub-agent to handle a delegated task. Use a named agent "
        "(autonomous, triage, drafter, context-pack, skill-evolution, or custom) "
        "or pass purpose + allowed_tools for an ad-hoc agent."
    )
    risk = ToolRisk.AUTO
    category = ToolCategory.READONLY
    parameters = {
        "agent_name": {
            "type": "string",
            "description": (
                "Name of a registered sub-agent, or 'ad-hoc' for a temporary one"
            ),
        },
        "task": {
            "type": "string",
            "description": "The task to delegate to the sub-agent",
        },
        "context": {
            "type": "string",
            "description": "Additional context for the sub-agent",
            "optional": True,
        },
        "purpose": {
            "type": "string",
            "description": "Purpose for ad-hoc agents (ignored for named agents)",
            "optional": True,
        },
        "allowed_tools": {
            "type": "array",
            "description": "Tool whitelist for ad-hoc agents (ignored for named agents)",
            "optional": True,
        },
    }

    # Set by the orchestrator before execution
    _current_depth: int = 0
    _parent_model: str = ""
    _parent_provider: str = ""

    async def execute(
        self,
        agent_name: str,
        task: str,
        context: str = "",
        purpose: str = "",
        allowed_tools: list[str] | None = None,
    ) -> ToolResult:
        if self._current_depth >= MAX_SPAWN_DEPTH:
            return ToolResult(
                success=False,
                error=f"Maximum sub-agent depth ({MAX_SPAWN_DEPTH}) reached.",
            )

        if agent_name == "ad-hoc":
            config = SubAgentConfig(
                name=f"ad-hoc-{id(task) % 10000}",
                purpose=purpose or "Ad-hoc sub-agent",
                allowed_tools=allowed_tools or [],
                max_iterations=5,
            )
        else:
            registry = get_subagent_registry()
            config = await registry.get(agent_name)
            if not config:
                return ToolResult(
                    success=False,
                    error=f"Unknown sub-agent '{agent_name}'. "
                    f"Available: {list(BUILTIN_SUBAGENTS.keys())}",
                )

        agent = SubAgent(
            config=config,
            parent_model=self._parent_model,
            parent_provider=self._parent_provider,
        )

        logger.info(
            "Spawning sub-agent",
            name=config.name,
            depth=self._current_depth + 1,
            task=task[:100],
        )

        result = await agent.run(task=task, context=context)

        if result.success:
            return ToolResult(
                success=True,
                data={
                    "agent": result.agent_name,
                    "response": result.response,
                    "steps": result.steps_taken,
                },
            )
        return ToolResult(
            success=False,
            error=f"Sub-agent '{result.agent_name}' failed: {result.error}",
        )
