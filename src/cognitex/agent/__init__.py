"""Cognitex Agent - ReAct-style agent for personal task management.

Architecture:
    ReAct Loop: Thought -> Action -> Observation -> Repeat until done

The agent uses iterative reasoning to:
    - Freely explore the knowledge graph
    - Make connections across emails, tasks, people, events, documents
    - Take actions when needed
    - Respond naturally to any query

Memory system:
    - Working Memory (Redis): Short-term context, pending approvals
    - Episodic Memory (Postgres): Long-term decisions, interactions, feedback

Tools are categorized by risk:
    - READONLY: Always allowed (queries, searches)
    - AUTO: Auto-execute (create tasks, notifications)
    - APPROVAL: Requires user approval (send email, calendar changes)
"""

from cognitex.agent.core import Agent, AgentMode, get_agent
from cognitex.agent.planner import Planner, Plan, PlanStep, get_planner
from cognitex.agent.executors import get_executor_registry
from cognitex.agent.memory import Memory, WorkingMemory, EpisodicMemory, init_memory, get_memory
from cognitex.agent.tools import (
    BaseTool,
    ToolRisk,
    ToolResult,
    ToolDefinition,
    ToolRegistry,
    get_tool_registry,
)
from cognitex.agent.triggers import (
    TriggerSystem,
    get_trigger_system,
    start_triggers,
    stop_triggers,
)

__all__ = [
    # Core
    "Agent",
    "AgentMode",
    "get_agent",
    # Planner
    "Planner",
    "Plan",
    "PlanStep",
    "get_planner",
    # Executors
    "get_executor_registry",
    # Memory
    "Memory",
    "WorkingMemory",
    "EpisodicMemory",
    "init_memory",
    "get_memory",
    # Tools
    "BaseTool",
    "ToolRisk",
    "ToolResult",
    "ToolDefinition",
    "ToolRegistry",
    "get_tool_registry",
    # Triggers
    "TriggerSystem",
    "get_trigger_system",
    "start_triggers",
    "stop_triggers",
]
