"""State-aware tool filtering for the agent.

Filters available tools based on user's current operating mode,
implementing mode-appropriate behavior from state_model.py.
"""

from __future__ import annotations

import structlog

from cognitex.agent.tools import BaseTool, ToolCategory
from cognitex.db.phase3_schema import OperatingMode

logger = structlog.get_logger()


# Tool categories allowed per operating mode
# Each mode restricts which tools the agent can use to prevent
# inappropriate actions based on user's current state
TOOL_ELIGIBILITY: dict[OperatingMode, list[ToolCategory]] = {
    # Normal mode - all categories available (fallback/default)
    # This is used when no specific mode restrictions apply

    OperatingMode.DEEP_FOCUS: [
        # Protect focus - only readonly and essential mutations
        ToolCategory.READONLY,
        ToolCategory.MEMORY,
        ToolCategory.TASK_MUTATION,  # Can update task status
        # NO: EMAIL, NOTIFICATION, EVENT, PROJECT_MUTATION, WEB
    ],

    OperatingMode.FRAGMENTED: [
        # Short tasks, batching - most tools available
        ToolCategory.READONLY,
        ToolCategory.MEMORY,
        ToolCategory.TASK_MUTATION,
        ToolCategory.PROJECT_MUTATION,
        ToolCategory.WEB,
        # NO: EMAIL (batched), EVENT (needs focus), NOTIFICATION (batched)
    ],

    OperatingMode.OVERLOADED: [
        # Reduce inputs - only readonly and memory
        ToolCategory.READONLY,
        ToolCategory.MEMORY,
        # NO: Everything else - no new inputs or actions
    ],

    OperatingMode.AVOIDANT: [
        # Micro-commitments, prep tasks
        ToolCategory.READONLY,
        ToolCategory.MEMORY,
        ToolCategory.TASK_MUTATION,  # For small task updates
        # NO: EMAIL, EVENT, NOTIFICATION, PROJECT_MUTATION, WEB
    ],

    OperatingMode.HYPERFOCUS: [
        # Similar to DEEP_FOCUS - protect the flow
        ToolCategory.READONLY,
        ToolCategory.MEMORY,
        ToolCategory.TASK_MUTATION,
        # NO: EMAIL, NOTIFICATION, EVENT, PROJECT_MUTATION, WEB
    ],

    OperatingMode.TRANSITION: [
        # Recovery/transition - gentle tools only
        ToolCategory.READONLY,
        ToolCategory.MEMORY,
        # NO: Active mutations during transition
    ],
}

# All categories - used when mode is not in TOOL_ELIGIBILITY or override is active
ALL_CATEGORIES = list(ToolCategory)


class ModeToolFilter:
    """Filters tools based on user's current operating mode.

    Integrates with StateEstimator to get current mode and filters
    the tool registry accordingly.
    """

    def __init__(self):
        self._override_active = False
        self._override_reason: str | None = None

    def set_override(self, active: bool, reason: str | None = None) -> None:
        """Enable or disable mode filtering override."""
        self._override_active = active
        self._override_reason = reason
        if active:
            logger.info("Tool filter override enabled", reason=reason)
        else:
            logger.info("Tool filter override disabled")

    @property
    def is_override_active(self) -> bool:
        """Check if override is currently active."""
        return self._override_active

    async def get_current_mode(self) -> OperatingMode:
        """Get the current operating mode from state estimator."""
        try:
            from cognitex.agent.state_model import get_state_estimator

            estimator = get_state_estimator()
            state = await estimator.get_current_state()
            return state.mode
        except Exception as e:
            logger.warning("Failed to get current mode, defaulting to FRAGMENTED", error=str(e))
            return OperatingMode.FRAGMENTED

    def get_allowed_categories(
        self,
        mode: OperatingMode,
        override: bool = False,
    ) -> list[ToolCategory]:
        """Get the tool categories allowed for a given mode."""
        if override or self._override_active:
            return ALL_CATEGORIES

        return TOOL_ELIGIBILITY.get(mode, ALL_CATEGORIES)

    async def get_eligible_tools(
        self,
        all_tools: list[BaseTool],
        override: bool = False,
    ) -> tuple[list[BaseTool], list[str], OperatingMode]:
        """Filter tools based on current operating mode.

        Args:
            all_tools: List of all registered tools
            override: If True, skip filtering and return all tools

        Returns:
            Tuple of (eligible_tools, filtered_tool_names, current_mode)
        """
        mode = await self.get_current_mode()

        if override or self._override_active:
            logger.debug("Tool filter override active, returning all tools")
            return all_tools, [], mode

        allowed_categories = self.get_allowed_categories(mode)

        eligible = []
        filtered = []

        for tool in all_tools:
            if tool.category in allowed_categories:
                eligible.append(tool)
            else:
                filtered.append(tool.name)

        if filtered:
            logger.info(
                "Tools filtered by operating mode",
                mode=mode.value,
                filtered_count=len(filtered),
                filtered_tools=filtered[:5],  # Log first 5 only
            )

        return eligible, filtered, mode

    def format_filter_notice(
        self,
        filtered_tools: list[str],
        mode: OperatingMode,
    ) -> str:
        """Format a notice about filtered tools for the agent prompt."""
        if not filtered_tools:
            return ""

        mode_descriptions = {
            OperatingMode.DEEP_FOCUS: "deep focus (protecting concentration)",
            OperatingMode.FRAGMENTED: "fragmented attention (batching mode)",
            OperatingMode.OVERLOADED: "overloaded (reducing inputs)",
            OperatingMode.AVOIDANT: "avoidant (micro-commitments mode)",
            OperatingMode.HYPERFOCUS: "hyperfocus (in the zone)",
            OperatingMode.TRANSITION: "transition (recovery mode)",
        }

        mode_desc = mode_descriptions.get(mode, mode.value)

        return (
            f"\n**Note:** {len(filtered_tools)} tools are currently unavailable "
            f"because you are in {mode_desc} mode. "
            f"Filtered tools: {', '.join(filtered_tools[:5])}"
            f"{'...' if len(filtered_tools) > 5 else ''}.\n"
            "Use the override command if this action is urgent."
        )


# Singleton instance
_filter: ModeToolFilter | None = None


def get_tool_filter() -> ModeToolFilter:
    """Get or create the tool filter singleton."""
    global _filter
    if _filter is None:
        _filter = ModeToolFilter()
    return _filter
