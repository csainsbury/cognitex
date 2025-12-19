"""P1.2 & P1.3: Decision policy with utility function and activation energy model.

Implements the decision machinery from Phase 3 blueprint:
- Utility function for task selection
- Activation energy model with MVS (Minimum Viable Start)
- Critical path and dependency awareness
- Context-switch cost accounting
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

import structlog

from cognitex.db.neo4j import get_neo4j_session
from cognitex.agent.state_model import (
    UserState,
    OperatingMode,
    ModeRules,
    TaskFriction,
    get_state_estimator,
)

logger = structlog.get_logger()


class TaskType(str, Enum):
    """Task type classification for mode matching."""
    DEEP_WORK = "deep_work"
    CREATIVE = "creative"
    ANALYSIS = "analysis"
    QUICK_WINS = "quick_wins"
    ADMIN = "admin"
    EMAIL = "email"
    COMMUNICATION = "communication"
    MAINTENANCE = "maintenance"
    RECOVERY = "recovery"
    URGENT_ONLY = "urgent_only"
    MICRO_TASK = "micro_task"
    PREP = "prep"
    CLARIFICATION = "clarification"


@dataclass
class TaskContext:
    """Context for a task being considered for selection."""

    task_id: str
    title: str
    task_type: TaskType = TaskType.QUICK_WINS

    # Urgency factors
    deadline: datetime | None = None
    deadline_hard: bool = False  # True = immovable deadline
    urgency_score: float = 0.5  # 0-1, computed from deadline proximity

    # Value factors
    goal_alignment: float = 0.5  # 0-1, how well aligned with active goals
    critical_path_score: float = 0.0  # 0-1, blocks other work if not done
    blocking_count: int = 0  # Number of tasks blocked by this one
    blocked_by_count: int = 0  # Number of tasks blocking this one

    # Cost factors
    estimated_minutes: int = 30
    cognitive_cost: float = 0.5  # 0-1, mental load
    start_friction: int = 3  # 0-5, activation energy needed
    context_switch_cost: float = 0.0  # 0-1, cost to switch to this task

    # Activation energy
    minimum_viable_start: str | None = None
    prep_ladder: list[str] = field(default_factory=list)
    deferral_count: int = 0
    last_deferred: datetime | None = None

    # Risk
    reversibility: float = 1.0  # 0-1, how reversible if wrong
    approval_required: bool = False

    # Current state
    status: str = "pending"
    project_id: str | None = None
    goal_id: str | None = None


@dataclass
class ActionRecommendation:
    """A recommended action from the decision policy."""

    task: TaskContext
    utility_score: float
    reasoning: str
    mvs_action: str | None = None  # Minimum viable start if friction is high
    prep_needed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class UtilityFunction:
    """Computes utility score for task selection.

    The utility function balances:
    - Urgency/deadlines (time pressure)
    - Critical path impact (blocks other work)
    - Goal alignment (long-term value)
    - Start friction / activation energy
    - Context switch cost
    - Risk/reversibility
    """

    # Weight configuration - can be adjusted per user preferences
    DEFAULT_WEIGHTS = {
        "urgency": 0.25,
        "critical_path": 0.20,
        "goal_alignment": 0.15,
        "friction_inverse": 0.20,  # Lower friction = higher score
        "context_switch_inverse": 0.10,  # Lower switch cost = higher
        "reversibility": 0.05,
        "momentum": 0.05,  # Bonus for continuing current work
    }

    def __init__(self, weights: dict | None = None):
        self.weights = weights or self.DEFAULT_WEIGHTS.copy()

    def compute(
        self,
        task: TaskContext,
        state: UserState,
        current_task_id: str | None = None,
    ) -> tuple[float, str]:
        """Compute utility score for a task given current state.

        Args:
            task: Task to evaluate
            state: Current user state
            current_task_id: ID of currently active task (for momentum)

        Returns:
            (utility_score, reasoning_string)
        """
        components = {}
        reasoning_parts = []

        # 1. Urgency score (deadline proximity)
        urgency = self._compute_urgency(task)
        components["urgency"] = urgency
        if urgency > 0.7:
            reasoning_parts.append(f"High urgency ({urgency:.2f})")

        # 2. Critical path score
        critical = self._compute_critical_path(task)
        components["critical_path"] = critical
        if critical > 0.5:
            reasoning_parts.append(f"Blocks {task.blocking_count} tasks")

        # 3. Goal alignment
        components["goal_alignment"] = task.goal_alignment
        if task.goal_alignment > 0.7:
            reasoning_parts.append("Strong goal alignment")

        # 4. Friction (inverted - lower friction = higher utility)
        # Adjusted for current mode
        mode_rules = ModeRules.get_rules(state.mode)
        max_friction = mode_rules["max_task_friction"]

        if task.start_friction > max_friction:
            # Task friction exceeds mode tolerance - heavy penalty
            friction_score = 0.0
            reasoning_parts.append(f"Friction too high for {state.mode.value}")
        else:
            # Normalize friction to 0-1 (inverted)
            friction_score = 1.0 - (task.start_friction / 5.0)

        components["friction_inverse"] = friction_score

        # 5. Context switch cost (inverted)
        switch_score = 1.0 - task.context_switch_cost
        components["context_switch_inverse"] = switch_score

        # 6. Reversibility
        components["reversibility"] = task.reversibility
        if task.reversibility < 0.5:
            reasoning_parts.append("Low reversibility - careful")

        # 7. Momentum bonus
        if current_task_id and task.task_id == current_task_id:
            components["momentum"] = 1.0
            reasoning_parts.append("Momentum from current work")
        elif task.project_id and current_task_id:
            # Small bonus for same-project work
            components["momentum"] = 0.3
        else:
            components["momentum"] = 0.0

        # Compute weighted sum
        utility = sum(
            self.weights.get(k, 0) * v
            for k, v in components.items()
        )

        # Apply mode-specific adjustments
        utility = self._apply_mode_adjustments(utility, task, state)

        # Penalty for blocked tasks
        if task.blocked_by_count > 0:
            utility *= 0.5  # Heavy penalty for blocked work
            reasoning_parts.append(f"Blocked by {task.blocked_by_count} tasks")

        # Bonus for reducing deferral patterns
        if task.deferral_count >= 3:
            # This task has been avoided - if it's now viable, boost it
            if friction_score > 0.5:
                utility *= 1.2
                reasoning_parts.append("Breaking avoidance pattern")

        reasoning = "; ".join(reasoning_parts) if reasoning_parts else "Standard utility"
        return round(utility, 3), reasoning

    def _compute_urgency(self, task: TaskContext) -> float:
        """Compute urgency from deadline proximity."""
        if not task.deadline:
            return task.urgency_score  # Use provided score if no deadline

        now = datetime.now()
        if task.deadline <= now:
            return 1.0  # Overdue

        time_remaining = (task.deadline - now).total_seconds() / 3600  # hours

        if task.deadline_hard:
            # Hard deadlines ramp up more steeply
            if time_remaining <= 1:
                return 1.0
            elif time_remaining <= 4:
                return 0.9
            elif time_remaining <= 24:
                return 0.7
            elif time_remaining <= 72:
                return 0.5
            else:
                return 0.3
        else:
            # Soft deadlines are gentler
            if time_remaining <= 4:
                return 0.8
            elif time_remaining <= 24:
                return 0.5
            elif time_remaining <= 72:
                return 0.3
            else:
                return 0.2

    def _compute_critical_path(self, task: TaskContext) -> float:
        """Compute critical path score based on blocking relationships."""
        if task.blocking_count == 0:
            return task.critical_path_score

        # More blocks = higher critical path score
        if task.blocking_count >= 5:
            return 1.0
        elif task.blocking_count >= 3:
            return 0.8
        elif task.blocking_count >= 1:
            return 0.6
        return task.critical_path_score

    def _apply_mode_adjustments(
        self,
        utility: float,
        task: TaskContext,
        state: UserState,
    ) -> float:
        """Apply mode-specific utility adjustments."""
        mode = state.mode

        if mode == OperatingMode.DEEP_FOCUS:
            # Boost deep work, penalize admin
            if task.task_type in [TaskType.DEEP_WORK, TaskType.CREATIVE, TaskType.ANALYSIS]:
                utility *= 1.3
            elif task.task_type in [TaskType.ADMIN, TaskType.EMAIL]:
                utility *= 0.5

        elif mode == OperatingMode.FRAGMENTED:
            # Boost quick wins, penalize long tasks
            if task.task_type in [TaskType.QUICK_WINS, TaskType.ADMIN, TaskType.EMAIL]:
                utility *= 1.2
            if task.estimated_minutes > 30:
                utility *= 0.7

        elif mode == OperatingMode.OVERLOADED:
            # Only urgent and maintenance work
            if task.task_type not in [TaskType.URGENT_ONLY, TaskType.MAINTENANCE]:
                utility *= 0.3

        elif mode == OperatingMode.AVOIDANT:
            # Boost micro-tasks and prep work
            if task.task_type in [TaskType.MICRO_TASK, TaskType.PREP, TaskType.CLARIFICATION]:
                utility *= 1.5

        elif mode == OperatingMode.HYPERFOCUS:
            # Only current focus, nothing else
            utility *= 0.1  # Almost everything penalized except current task

        return utility


class ActivationEnergyModel:
    """Manages task activation energy and MVS generation.

    Implements neurodivergent-first execution model:
    - Track start friction per task
    - Generate Minimum Viable Start (MVS) actions
    - Auto-decompose repeatedly deferred tasks
    - Build prep ladders to reduce friction
    """

    # Friction level descriptions
    FRICTION_LEVELS = {
        0: "Trivial - can start instantly",
        1: "Low - needs brief setup",
        2: "Medium - requires some preparation",
        3: "Moderate - needs mental gear-up",
        4: "High - significant resistance",
        5: "Very high - major activation barrier",
    }

    # MVS templates by task type
    MVS_TEMPLATES = {
        TaskType.DEEP_WORK: [
            "Open the document/IDE",
            "Read the last paragraph written",
            "Write one sentence",
        ],
        TaskType.CREATIVE: [
            "Open blank canvas/document",
            "Set a 5-minute timer",
            "Make one mark/write one word",
        ],
        TaskType.EMAIL: [
            "Open the email",
            "Read it fully",
            "Type 'Hi' and the first sentence",
        ],
        TaskType.ANALYSIS: [
            "Open the notebook/script",
            "Run the first cell",
            "Check the output",
        ],
        TaskType.ADMIN: [
            "Open the form/system",
            "Fill the first field",
            "Save draft",
        ],
    }

    def estimate_friction(self, task: TaskContext, state: UserState) -> int:
        """Estimate start friction for a task.

        Factors:
        - Inherent task complexity
        - Current state/fatigue
        - Time since last touch
        - Ambiguity level
        """
        base_friction = task.start_friction

        # Adjust for fatigue
        if state.signals.fatigue_level > 0.7:
            base_friction = min(5, base_friction + 1)
        elif state.signals.fatigue_level < 0.3:
            base_friction = max(0, base_friction - 1)

        # Adjust for repeated deferrals (task feels harder each time)
        if task.deferral_count >= 3:
            base_friction = min(5, base_friction + 1)

        # Adjust for blocking state
        if task.blocked_by_count > 0:
            base_friction = min(5, base_friction + 2)

        return base_friction

    def generate_mvs(self, task: TaskContext) -> str:
        """Generate a Minimum Viable Start action for a task.

        MVS is the smallest action that counts as 'started'.
        """
        if task.minimum_viable_start:
            return task.minimum_viable_start

        # Get template for task type
        templates = self.MVS_TEMPLATES.get(task.task_type, [])
        if templates:
            return templates[0]  # First step is the MVS

        # Generic MVS
        return f"Open the first related file/document for '{task.title[:30]}'"

    def generate_prep_ladder(self, task: TaskContext) -> list[str]:
        """Generate a prep ladder to reduce friction.

        A prep ladder is a sequence of small steps that collectively
        reduce the activation energy needed to start the main task.
        """
        if task.prep_ladder:
            return task.prep_ladder

        ladder = []

        # Templates based on task type
        templates = self.MVS_TEMPLATES.get(task.task_type, [])
        if templates:
            ladder.extend(templates)
        else:
            # Generic ladder
            ladder = [
                f"Clear workspace for '{task.title[:20]}'",
                "Close unrelated tabs/apps",
                "Set a 5-minute timer",
                "Open the first file/document",
                "Read the first paragraph/section",
                "Make one small edit or note",
            ]

        return ladder

    async def record_deferral(
        self,
        task_id: str,
        reason: str | None = None,
    ) -> TaskFriction:
        """Record that a task was deferred.

        Tracks deferral patterns for automatic decomposition.
        """
        # In a full implementation, this would update the graph
        logger.info(
            "Task deferred",
            task_id=task_id,
            reason=reason,
        )

        # Return updated friction info
        return TaskFriction(
            task_id=task_id,
            deferral_count=1,  # Would be incremented from graph
            deferral_reasons=[reason] if reason else [],
        )

    async def should_decompose(self, task: TaskContext) -> bool:
        """Check if a task should be auto-decomposed due to avoidance.

        Trigger decomposition when:
        - Deferred 3+ times
        - High friction score
        - Not yet decomposed
        """
        return (
            task.deferral_count >= 3
            and task.start_friction >= 3
            and not task.prep_ladder
        )

    async def decompose_task(
        self,
        task: TaskContext,
    ) -> list[TaskContext]:
        """Decompose a high-friction task into smaller sub-tasks.

        Creates prep tasks that reduce overall friction.
        """
        subtasks = []

        # Generate prep ladder
        ladder = self.generate_prep_ladder(task)

        for i, step in enumerate(ladder[:3]):  # Max 3 prep tasks
            subtask = TaskContext(
                task_id=f"{task.task_id}_prep_{i}",
                title=step,
                task_type=TaskType.PREP,
                estimated_minutes=5,
                start_friction=1,  # Prep tasks are low friction
                cognitive_cost=0.2,
                goal_id=task.goal_id,
                project_id=task.project_id,
            )
            subtasks.append(subtask)

        logger.info(
            "Decomposed task",
            task_id=task.task_id,
            subtask_count=len(subtasks),
        )
        return subtasks


class DecisionPolicy:
    """Main decision policy orchestrator.

    Combines:
    - State estimation
    - Utility function
    - Activation energy model
    - Mode rules

    To produce 1-3 recommended next actions.
    """

    def __init__(self):
        self.utility = UtilityFunction()
        self.activation = ActivationEnergyModel()
        self.state_estimator = get_state_estimator()

    async def select_next_actions(
        self,
        available_tasks: list[TaskContext],
        current_task_id: str | None = None,
        max_recommendations: int = 3,
    ) -> list[ActionRecommendation]:
        """Select the best next actions from available tasks.

        Args:
            available_tasks: Tasks that could be worked on
            current_task_id: Currently active task (for momentum)
            max_recommendations: Max number of recommendations (1-3)

        Returns:
            List of ActionRecommendations, ordered by utility
        """
        state = await self.state_estimator.get_current_state()
        recommendations = []

        for task in available_tasks:
            # Check mode eligibility
            friction = self.activation.estimate_friction(task, state)
            eligible, reason = ModeRules.can_do_task(
                state.mode,
                task.task_type.value,
                friction,
                task.estimated_minutes,
                state.signals.available_block_minutes,
            )

            if not eligible:
                # Still compute utility for context, but mark as ineligible
                utility, reasoning = self.utility.compute(task, state, current_task_id)
                rec = ActionRecommendation(
                    task=task,
                    utility_score=0.0,
                    reasoning=f"Ineligible: {reason}",
                    warnings=[reason] if reason else [],
                )
            else:
                # Compute utility
                utility, reasoning = self.utility.compute(task, state, current_task_id)

                # Generate MVS if high friction
                mvs = None
                prep = []
                if friction >= 3:
                    mvs = self.activation.generate_mvs(task)
                    prep = self.activation.generate_prep_ladder(task)

                # Check for decomposition need
                warnings = []
                if await self.activation.should_decompose(task):
                    warnings.append("Consider breaking this task down")

                rec = ActionRecommendation(
                    task=task,
                    utility_score=utility,
                    reasoning=reasoning,
                    mvs_action=mvs,
                    prep_needed=prep[:3],  # Top 3 prep steps
                    warnings=warnings,
                )

            recommendations.append(rec)

        # Sort by utility score descending
        recommendations.sort(key=lambda r: r.utility_score, reverse=True)

        # Return top N
        return recommendations[:max_recommendations]

    async def get_mode_appropriate_tasks(
        self,
        all_tasks: list[TaskContext],
    ) -> list[TaskContext]:
        """Filter tasks appropriate for current mode.

        Returns only tasks that pass mode eligibility.
        """
        state = await self.state_estimator.get_current_state()
        appropriate = []

        for task in all_tasks:
            friction = self.activation.estimate_friction(task, state)
            eligible, _ = ModeRules.can_do_task(
                state.mode,
                task.task_type.value,
                friction,
                task.estimated_minutes,
                state.signals.available_block_minutes,
            )
            if eligible:
                appropriate.append(task)

        return appropriate

    async def explain_recommendation(
        self,
        recommendation: ActionRecommendation,
    ) -> str:
        """Generate human-readable explanation for a recommendation."""
        task = recommendation.task
        lines = [
            f"**{task.title}**",
            f"Utility: {recommendation.utility_score:.2f}",
            f"Reason: {recommendation.reasoning}",
        ]

        if recommendation.mvs_action:
            lines.append(f"Start with: {recommendation.mvs_action}")

        if recommendation.prep_needed:
            lines.append("Prep steps:")
            for step in recommendation.prep_needed:
                lines.append(f"  • {step}")

        if recommendation.warnings:
            lines.append("Warnings:")
            for w in recommendation.warnings:
                lines.append(f"  ⚠ {w}")

        return "\n".join(lines)


# Singleton instance
_decision_policy: DecisionPolicy | None = None


def get_decision_policy() -> DecisionPolicy:
    """Get the decision policy singleton."""
    global _decision_policy
    if _decision_policy is None:
        _decision_policy = DecisionPolicy()
    return _decision_policy
