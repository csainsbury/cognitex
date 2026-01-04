"""P1.1: State estimation model for minute-to-minute control.

Implements the user operating state model from Phase 3 blueprint:
- Discrete modes (Deep Focus, Fragmented, Overloaded, Avoidant, Hyperfocus)
- Continuous signals (block length, interruption pressure, fatigue, time-to-commitment)
- Mode-aware task selection and UI simplification
- State inference from behavioral signals
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

import structlog

from cognitex.db.neo4j import get_neo4j_session
from cognitex.db.phase3_schema import (
    OperatingMode,
    create_state_snapshot,
    get_latest_state_snapshot,
    get_state_history,
)

logger = structlog.get_logger()


@dataclass
class ContinuousSignals:
    """Continuous state signals for decision-making."""

    # Time constraints
    available_block_minutes: int | None = None  # True uninterrupted time
    time_to_next_commitment_minutes: int | None = None  # Hard deadline ahead

    # Load indicators
    interruption_pressure: float = 0.5  # 0-1, incoming demand level
    fatigue_level: float = 0.5  # 0-1, current tiredness
    fatigue_slope: float = 0.0  # Rate of change (-1 recovering, +1 depleting)

    # Attention state
    focus_score: float | None = None  # 0-1, attention bandwidth

    # Context
    location: str | None = None  # home, office, travel
    device: str | None = None  # desktop, mobile
    connectivity: str | None = None  # good, poor, offline


@dataclass
class TaskFriction:
    """Activation energy model for a task."""

    task_id: str
    start_friction: int = 3  # 0-5 scale
    minimum_viable_start: str | None = None  # MVS description
    prep_ladder: list[str] = field(default_factory=list)  # Auto-generated prep steps
    deferral_count: int = 0
    deferral_reasons: list[str] = field(default_factory=list)


@dataclass
class UserState:
    """Complete user state snapshot for decision-making."""

    # Discrete mode
    mode: OperatingMode = OperatingMode.FRAGMENTED

    # Continuous signals
    signals: ContinuousSignals = field(default_factory=ContinuousSignals)

    # Timestamp
    captured_at: datetime = field(default_factory=datetime.now)

    # Context notes
    notes: str | None = None

    def to_dict(self) -> dict:
        """Convert to dict for storage/serialization."""
        return {
            "mode": self.mode.value,
            "available_block_minutes": self.signals.available_block_minutes,
            "interruption_pressure": self.signals.interruption_pressure,
            "fatigue_level": self.signals.fatigue_level,
            "fatigue_slope": self.signals.fatigue_slope,
            "time_to_next_commitment_minutes": self.signals.time_to_next_commitment_minutes,
            "focus_score": self.signals.focus_score,
            "context_notes": self.notes,
            "captured_at": self.captured_at.isoformat(),
        }


class ModeRules:
    """Deterministic rules for each operating mode.

    Each mode defines:
    - Task eligibility constraints
    - Notification gating
    - UI simplification level
    - Default behaviors
    """

    RULES = {
        OperatingMode.DEEP_FOCUS: {
            "description": "Protect focus, block interruptions, deep tasks only",
            "allowed_task_types": ["deep_work", "creative", "analysis"],
            "max_task_friction": 5,  # Can handle high-friction tasks
            "min_block_minutes": 45,  # Need substantial blocks
            "notification_gate": "urgent_only",  # Only truly urgent
            "interrupt_for": ["family_emergency", "critical_deadline"],
            "ui_density": "minimal",  # Hide everything except current task
            "auto_actions": ["queue_incoming", "batch_notifications"],
        },
        OperatingMode.FRAGMENTED: {
            "description": "Short tasks, batching, context packs needed",
            "allowed_task_types": ["quick_wins", "admin", "email", "communication"],
            "max_task_friction": 2,  # Only low-friction tasks
            "min_block_minutes": 5,  # Can use short blocks
            "notification_gate": "batched",  # Batch to windows
            "interrupt_for": ["urgent", "family"],
            "ui_density": "compact",  # Show task list, hide deep work
            "auto_actions": ["prepare_context_packs", "batch_similar_tasks"],
        },
        OperatingMode.OVERLOADED: {
            "description": "Reduce inputs, maintenance and recovery only",
            "allowed_task_types": ["maintenance", "recovery", "urgent_only"],
            "max_task_friction": 1,  # Only trivial tasks
            "min_block_minutes": None,  # Any block okay
            "notification_gate": "critical_only",  # Almost nothing gets through
            "interrupt_for": ["emergency"],
            "ui_density": "minimal",  # Hide everything
            "auto_actions": ["defer_non_essential", "suggest_recovery", "reduce_inbox"],
        },
        OperatingMode.AVOIDANT: {
            "description": "Micro-commitments, prep tasks, external prompts needed",
            "allowed_task_types": ["micro_task", "prep", "clarification"],
            "max_task_friction": 1,  # Only after decomposition
            "min_block_minutes": 5,  # Short commitments
            "notification_gate": "supportive",  # Allow supportive prompts
            "interrupt_for": ["encouragement", "urgent"],
            "ui_density": "focused",  # Single next action
            "auto_actions": [
                "decompose_blocked_tasks",
                "generate_mvs",
                "offer_5min_commitment",
            ],
        },
        OperatingMode.HYPERFOCUS: {
            "description": "Hard stop rails, hydration prompts, time boxing",
            "allowed_task_types": ["current_focus_only"],
            "max_task_friction": 5,  # Deep in the zone
            "min_block_minutes": 60,  # Extended focus
            "notification_gate": "none",  # Block everything
            "interrupt_for": ["hard_stop", "health_reminder"],
            "ui_density": "hidden",  # Nothing visible
            "auto_actions": [
                "set_hard_stop",
                "schedule_break_prompts",
                "prepare_parking_note",
            ],
        },
        OperatingMode.TRANSITION: {
            "description": "Between states, settling period",
            "allowed_task_types": ["quick_wins", "wrap_up", "planning"],
            "max_task_friction": 2,
            "min_block_minutes": 10,
            "notification_gate": "batched",
            "interrupt_for": ["urgent", "scheduled"],
            "ui_density": "normal",
            "auto_actions": ["assess_next_mode", "review_priorities"],
        },
    }

    @classmethod
    def get_rules(cls, mode: OperatingMode) -> dict:
        """Get rules for a specific mode."""
        return cls.RULES.get(mode, cls.RULES[OperatingMode.FRAGMENTED])

    @classmethod
    def can_do_task(
        cls,
        mode: OperatingMode,
        task_type: str,
        friction: int,
        required_minutes: int | None = None,
        available_minutes: int | None = None,
    ) -> tuple[bool, str | None]:
        """Check if a task is eligible in current mode.

        Returns:
            (eligible, reason) - reason explains why not if False
        """
        rules = cls.get_rules(mode)

        # Check friction level
        if friction > rules["max_task_friction"]:
            return False, f"Friction {friction} exceeds max {rules['max_task_friction']} for {mode.value}"

        # Check task type
        allowed = rules["allowed_task_types"]
        if "current_focus_only" not in allowed and task_type not in allowed:
            return False, f"Task type '{task_type}' not allowed in {mode.value}"

        # Check time requirements
        min_block = rules["min_block_minutes"]
        if min_block and available_minutes and available_minutes < min_block:
            return False, f"Need {min_block}+ minutes for {mode.value}, only {available_minutes} available"

        if required_minutes and available_minutes and required_minutes > available_minutes:
            return False, f"Task needs {required_minutes} min, only {available_minutes} available"

        return True, None


class StateEstimator:
    """Estimates current user operating state from signals.

    Uses multiple input sources:
    - Calendar (upcoming commitments, meeting density)
    - Recent behavior (task starts, deferrals, email patterns)
    - Explicit user input (mood, energy level)
    - Time of day patterns
    """

    def __init__(self):
        self._current_state: UserState | None = None
        self._state_history: list[UserState] = []

    async def get_current_state(self) -> UserState:
        """Get the current user state, inferring if needed."""
        if self._current_state and (
            datetime.now() - self._current_state.captured_at
        ) < timedelta(minutes=15):
            return self._current_state

        # Try to load from graph
        async for session in get_neo4j_session():
            snapshot = await get_latest_state_snapshot(session)
            if snapshot:
                self._current_state = self._snapshot_to_state(snapshot)
                return self._current_state

        # Return default state
        return UserState()

    async def infer_state(
        self,
        calendar_events: list[dict] | None = None,
        recent_tasks: list[dict] | None = None,
        explicit_signals: dict | None = None,
    ) -> UserState:
        """Infer current state from available signals.

        Args:
            calendar_events: Upcoming calendar events
            recent_tasks: Recently interacted tasks
            explicit_signals: User-provided signals (energy, mood)

        Returns:
            Inferred UserState
        """
        signals = ContinuousSignals()

        # Get temporal energy baseline from diurnal curve
        temporal_model = get_temporal_model()
        current_hour = datetime.now().hour
        baseline_energy = temporal_model.get_expected_energy(current_hour)

        # Set fatigue as inverse of baseline energy (high energy = low fatigue)
        signals.fatigue_level = 1.0 - baseline_energy

        # Process explicit signals (these override temporal baseline)
        if explicit_signals:
            if "fatigue" in explicit_signals:
                signals.fatigue_level = explicit_signals["fatigue"]
            if "focus" in explicit_signals:
                signals.focus_score = explicit_signals["focus"]
            if "interruption_pressure" in explicit_signals:
                signals.interruption_pressure = explicit_signals["interruption_pressure"]

        # Calculate time to next commitment and interruption pressure
        if calendar_events:
            now = datetime.now()
            upcoming = [
                e for e in calendar_events
                if e.get("start") and datetime.fromisoformat(e["start"].replace("Z", "+00:00").replace("+00:00", "")) > now
            ]
            if upcoming:
                next_event = min(
                    upcoming,
                    key=lambda e: datetime.fromisoformat(e["start"].replace("Z", "+00:00").replace("+00:00", ""))
                )
                delta = datetime.fromisoformat(next_event["start"].replace("Z", "+00:00").replace("+00:00", "")) - now
                minutes_to_next = int(delta.total_seconds() / 60)
                signals.time_to_next_commitment_minutes = minutes_to_next
                signals.available_block_minutes = minutes_to_next

                # Interruption pressure based on time to next event
                if minutes_to_next >= 120:
                    signals.interruption_pressure = 0.1  # Very low - plenty of time
                elif minutes_to_next >= 60:
                    signals.interruption_pressure = 0.2  # Low
                elif minutes_to_next >= 30:
                    signals.interruption_pressure = 0.4  # Moderate
                else:
                    signals.interruption_pressure = 0.7  # High - event soon
            else:
                # Events today but none upcoming - low pressure
                signals.available_block_minutes = 120
                signals.interruption_pressure = 0.15
        else:
            # No calendar events - assume open block with low interruption
            signals.available_block_minutes = 120
            signals.interruption_pressure = 0.1

        # Infer mode from signals (now includes temporal energy)
        mode = self._infer_mode(signals, recent_tasks, baseline_energy)

        state = UserState(mode=mode, signals=signals)
        self._current_state = state
        return state

    def _infer_mode(
        self,
        signals: ContinuousSignals,
        recent_tasks: list[dict] | None = None,
        baseline_energy: float = 0.5,
    ) -> OperatingMode:
        """Infer operating mode from signals and temporal energy.

        Rules-based inference with clear thresholds.
        Baseline energy from diurnal curve influences mode selection.
        """
        # Check for overload (high fatigue + high interruption)
        if signals.fatigue_level > 0.8 and signals.interruption_pressure > 0.7:
            return OperatingMode.OVERLOADED

        # Check for avoidance (repeated deferrals)
        if recent_tasks:
            deferral_count = sum(
                1 for t in recent_tasks
                if t.get("status") == "deferred" or t.get("deferral_count", 0) > 2
            )
            if deferral_count >= 3:
                return OperatingMode.AVOIDANT

        # At peak energy times (0.85+), favor deep focus if time allows
        if baseline_energy >= 0.85:
            if (
                signals.available_block_minutes
                and signals.available_block_minutes >= 45  # Slightly lower threshold at peak
                and signals.interruption_pressure < 0.4
            ):
                return OperatingMode.DEEP_FOCUS

        # Standard deep focus check
        if (
            signals.available_block_minutes
            and signals.available_block_minutes >= 60
            and signals.interruption_pressure < 0.3
            and (signals.focus_score is None or signals.focus_score > 0.6)
        ):
            return OperatingMode.DEEP_FOCUS

        # At low energy times (< 0.4), default to fragmented
        if baseline_energy < 0.4:
            return OperatingMode.FRAGMENTED

        # Check for fragmented state (short time blocks)
        if (
            signals.available_block_minutes
            and signals.available_block_minutes < 30
        ):
            return OperatingMode.FRAGMENTED

        # Moderate energy with available time = transition
        return OperatingMode.TRANSITION

    def _snapshot_to_state(self, snapshot: dict) -> UserState:
        """Convert a graph snapshot to UserState object."""
        signals = ContinuousSignals(
            available_block_minutes=snapshot.get("available_block_minutes"),
            time_to_next_commitment_minutes=snapshot.get("time_to_next_commitment_minutes"),
            interruption_pressure=snapshot.get("interruption_pressure", 0.5),
            fatigue_level=snapshot.get("fatigue_level", 0.5),
            fatigue_slope=snapshot.get("fatigue_slope", 0.0),
            focus_score=snapshot.get("focus_score"),
        )

        mode_str = snapshot.get("mode", "fragmented")
        try:
            mode = OperatingMode(mode_str)
        except ValueError:
            mode = OperatingMode.FRAGMENTED

        return UserState(
            mode=mode,
            signals=signals,
            notes=snapshot.get("context_notes"),
        )

    async def record_state(self, state: UserState) -> str:
        """Record current state to the graph.

        Returns:
            snapshot_id
        """
        snapshot_id = f"state_{uuid.uuid4().hex[:12]}"

        async for session in get_neo4j_session():
            await create_state_snapshot(
                session,
                snapshot_id=snapshot_id,
                mode=state.mode.value,
                available_block_minutes=state.signals.available_block_minutes,
                interruption_pressure=state.signals.interruption_pressure,
                fatigue_level=state.signals.fatigue_level,
                fatigue_slope=state.signals.fatigue_slope,
                time_to_next_commitment_minutes=state.signals.time_to_next_commitment_minutes,
                focus_score=state.signals.focus_score,
                context_notes=state.notes,
            )

        self._current_state = state
        self._state_history.append(state)
        logger.info("Recorded state snapshot", snapshot_id=snapshot_id, mode=state.mode.value)
        return snapshot_id

    async def update_state(
        self,
        mode: OperatingMode | None = None,
        fatigue_delta: float | None = None,
        fatigue_level: float | None = None,
        focus_score: float | None = None,
        notes: str | None = None,
    ) -> UserState:
        """Update current state with new information.

        Args:
            mode: New operating mode
            fatigue_delta: Change in fatigue (-1 to 1), added to current level
            fatigue_level: Absolute fatigue level (0 to 1), overrides current level
            focus_score: Updated focus score
            notes: Context notes

        Returns:
            Updated state
        """
        current = await self.get_current_state()

        if mode:
            current.mode = mode
        if fatigue_level is not None:
            # Absolute fatigue level (used for post-clinical recovery, etc.)
            current.signals.fatigue_level = max(0.0, min(1.0, fatigue_level))
            current.signals.fatigue_slope = 0.0
        elif fatigue_delta is not None:
            # Relative change to fatigue
            current.signals.fatigue_level = max(
                0.0, min(1.0, current.signals.fatigue_level + fatigue_delta)
            )
            current.signals.fatigue_slope = fatigue_delta
        if focus_score is not None:
            current.signals.focus_score = focus_score
        if notes:
            current.notes = notes

        current.captured_at = datetime.now()
        await self.record_state(current)
        return current


# Singleton instance with lock for thread safety
_state_estimator: StateEstimator | None = None
_state_estimator_lock = threading.Lock()


def get_state_estimator() -> StateEstimator:
    """Get the state estimator singleton (thread-safe)."""
    global _state_estimator
    if _state_estimator is not None:
        return _state_estimator
    with _state_estimator_lock:
        # Double-check after acquiring lock
        if _state_estimator is None:
            _state_estimator = StateEstimator()
    return _state_estimator


# =============================================================================
# Phase 5: Temporal Energy Model
# =============================================================================

class TemporalEnergyModel:
    """Models energy patterns by time of day.

    Uses a default diurnal curve with peak energy 8-11am and steady
    decline through the afternoon/evening. Learns from task completion
    observations to personalize the curve over time.
    """

    # Default energy curve by hour (0.0-1.0 scale)
    # Diurnal pattern: peak 8-11am, steady decline through day
    DEFAULT_CURVE: dict[int, float] = {
        0: 0.15,   # midnight - very low
        1: 0.1,
        2: 0.1,
        3: 0.1,
        4: 0.15,   # pre-dawn
        5: 0.25,   # early rise
        6: 0.45,   # waking up
        7: 0.7,    # ramping up
        8: 0.9,    # entering peak zone
        9: 0.95,   # peak
        10: 0.95,  # peak
        11: 0.9,   # still high
        12: 0.75,  # noon - starting to dip
        13: 0.65,  # post-lunch dip
        14: 0.55,  # afternoon low
        15: 0.5,
        16: 0.45,
        17: 0.4,   # late afternoon
        18: 0.35,
        19: 0.3,   # evening
        20: 0.25,
        21: 0.2,
        22: 0.15,
        23: 0.15,
    }

    def __init__(self):
        self._learned_overrides: dict[int, float] = {}

    def get_expected_energy(self, hour: int) -> float:
        """Get expected energy level for hour of day.

        Args:
            hour: Hour of day (0-23)

        Returns:
            Energy level (0.0-1.0)
        """
        # Check for learned overrides first
        if hour in self._learned_overrides:
            return self._learned_overrides[hour]
        return self.DEFAULT_CURVE.get(hour, 0.5)

    def get_peak_hours(self) -> list[int]:
        """Get the hours with highest expected energy.

        Returns:
            List of hours sorted by energy (highest first)
        """
        all_hours = {**self.DEFAULT_CURVE, **self._learned_overrides}
        sorted_hours = sorted(all_hours.items(), key=lambda x: x[1], reverse=True)
        return [h for h, _ in sorted_hours[:5]]

    def get_peak_hour(self) -> int:
        """Get the single best hour for high-energy tasks.

        Returns:
            Hour with highest expected energy
        """
        return self.get_peak_hours()[0]

    async def load_learned_patterns(self) -> None:
        """Load learned temporal patterns from database."""
        try:
            from cognitex.db.postgres import get_session
            from sqlalchemy import text

            async for session in get_session():
                result = await session.execute(text("""
                    SELECT pattern_key, pattern_data
                    FROM learned_patterns
                    WHERE pattern_type = 'temporal_energy'
                """))
                rows = result.fetchall()

                for row in rows:
                    try:
                        hour = int(row[0])
                        data = row[1]
                        if isinstance(data, dict) and "energy" in data:
                            self._learned_overrides[hour] = data["energy"]
                    except (ValueError, TypeError):
                        continue

                break

            logger.debug(
                "Loaded temporal patterns",
                override_count=len(self._learned_overrides),
            )
        except Exception as e:
            logger.debug("Could not load temporal patterns", error=str(e))

    async def update_from_observation(
        self,
        hour: int,
        task_completed: bool,
        task_difficulty: str,  # 'high', 'medium', 'low'
        post_clinical: bool = False,
    ) -> None:
        """Learn from task completion observations.

        Updates the energy model based on whether tasks were completed
        at different hours.

        Args:
            hour: Hour of day when task was attempted
            task_completed: Whether the task was completed
            task_difficulty: Task difficulty level
            post_clinical: Whether this was after a clinical session
        """
        try:
            from cognitex.db.postgres import get_session
            from sqlalchemy import text
            import json

            # Skip learning from post-clinical observations (special case)
            if post_clinical:
                return

            # Calculate adjustment based on outcome
            # Completed tasks suggest energy was sufficient
            # Deferred/failed tasks suggest energy was too low
            adjustment = 0.02 if task_completed else -0.02

            # Weight by task difficulty (harder tasks = stronger signal)
            difficulty_weight = {"high": 1.5, "medium": 1.0, "low": 0.5}.get(
                task_difficulty, 1.0
            )
            adjustment *= difficulty_weight

            # Get current value
            current = self.get_expected_energy(hour)
            new_value = max(0.05, min(0.95, current + adjustment))

            # Update learned pattern in database
            async for session in get_session():
                await session.execute(text("""
                    INSERT INTO learned_patterns (id, pattern_type, pattern_key, pattern_data, sample_size, last_updated)
                    VALUES (:id, 'temporal_energy', :hour, :data, 1, NOW())
                    ON CONFLICT (pattern_type, pattern_key) DO UPDATE SET
                        pattern_data = :data,
                        sample_size = learned_patterns.sample_size + 1,
                        last_updated = NOW()
                """), {
                    "id": f"temporal_energy_{hour}",
                    "hour": str(hour),
                    "data": json.dumps({"energy": new_value}),
                })
                await session.commit()
                break

            # Update in-memory cache
            self._learned_overrides[hour] = new_value

            logger.debug(
                "Updated temporal pattern",
                hour=hour,
                completed=task_completed,
                old_value=current,
                new_value=new_value,
            )
        except Exception as e:
            logger.debug("Could not update temporal pattern", error=str(e))

    def is_peak_time(self) -> bool:
        """Check if current time is in a peak energy window.

        Returns:
            True if current hour is in top 5 energy hours
        """
        current_hour = datetime.now().hour
        return current_hour in self.get_peak_hours()

    def suggest_reschedule_time(self, for_high_energy: bool = True) -> str:
        """Suggest a better time for a task.

        Args:
            for_high_energy: If True, suggest peak hours; if False, suggest low-energy hours

        Returns:
            Human-readable time suggestion
        """
        peak_hour = self.get_peak_hour()
        now = datetime.now()

        if for_high_energy:
            if now.hour < peak_hour:
                # Today, at peak hour
                return f"today around {peak_hour}:00"
            else:
                # Tomorrow morning
                return f"tomorrow around {peak_hour}:00"
        else:
            # For low-energy tasks, suggest afternoon/evening
            return "later this afternoon when energy naturally dips"


# Singleton instance
_temporal_model: TemporalEnergyModel | None = None


def get_temporal_model() -> TemporalEnergyModel:
    """Get the temporal energy model singleton."""
    global _temporal_model
    if _temporal_model is None:
        _temporal_model = TemporalEnergyModel()
    return _temporal_model


# =============================================================================
# Phase 4: Deferral Prediction (1.3)
# =============================================================================

@dataclass
class DeferralRisk:
    """Predicted risk of task deferral."""

    score: float  # 0-1 probability of deferral
    factors: list[str]  # Contributing factors
    recommended_intervention: str | None = None  # Suggested action

    @classmethod
    async def calculate(cls, task: dict) -> "DeferralRisk":
        """
        Calculate deferral risk for a task based on multiple factors.

        Args:
            task: Task dict with id, title, deferral_count, project_id, etc.

        Returns:
            DeferralRisk with score, factors, and recommended intervention
        """
        factors = []
        score = 0.0

        # Factor 1: Task has been deferred before (strongest signal)
        deferral_count = task.get("deferral_count", 0)
        if deferral_count > 0:
            score += 0.3 * min(deferral_count, 3) / 3
            factors.append(f"deferred {deferral_count}x before")

        # Factor 2: Project deferral rate
        project_id = task.get("project_id")
        if project_id:
            project_rate = await get_project_deferral_rate(project_id)
            if project_rate > 0.5:
                score += 0.2
                factors.append(f"project has {project_rate:.0%} deferral rate")
            elif project_rate > 0.3:
                score += 0.1
                factors.append(f"project has moderate deferral rate ({project_rate:.0%})")

        # Factor 3: High start friction
        start_friction = task.get("start_friction", 3)
        if start_friction >= 4:
            score += 0.2
            factors.append("high start friction")
        elif start_friction >= 3:
            score += 0.1
            factors.append("moderate start friction")

        # Factor 4: No clear next step (no MVS)
        if not task.get("minimum_viable_start"):
            score += 0.15
            factors.append("no MVS defined")

        # Factor 5: Large estimated time
        estimated_minutes = task.get("estimated_minutes", 0)
        if estimated_minutes > 120:
            score += 0.15
            factors.append("large time estimate (>2hr)")
        elif estimated_minutes > 60:
            score += 0.1
            factors.append("substantial time estimate (>1hr)")

        # Factor 6: No deadline (lower urgency)
        if not task.get("due") and not task.get("due_date"):
            score += 0.1
            factors.append("no deadline set")

        # Factor 7: Low priority
        priority = task.get("priority", "medium")
        if priority == "low":
            score += 0.1
            factors.append("low priority")

        # Determine recommended intervention
        intervention = None
        if score >= 0.7:
            if not task.get("minimum_viable_start"):
                intervention = "generate_mvs"
            elif estimated_minutes > 90:
                intervention = "decompose"
            else:
                intervention = "schedule_now"
        elif score >= 0.5:
            if not task.get("minimum_viable_start"):
                intervention = "generate_mvs"
            else:
                intervention = "add_deadline"

        return cls(
            score=min(score, 1.0),
            factors=factors,
            recommended_intervention=intervention,
        )


async def get_project_deferral_rate(project_id: str) -> float:
    """
    Get the historical deferral rate for a project.

    Returns:
        Float 0-1 representing proportion of tasks deferred at least once
    """
    from cognitex.db.postgres import get_session
    from sqlalchemy import text

    async for session in get_session():
        result = await session.execute(text("""
            SELECT
                COUNT(*) as total,
                COUNT(*) FILTER (WHERE deferral_count > 0) as deferred
            FROM tasks
            WHERE project_id = :project_id
              AND status IN ('pending', 'in_progress', 'completed')
        """), {"project_id": project_id})
        row = result.fetchone()
        if row and row.total > 0:
            return row.deferred / row.total
        break

    return 0.0


async def get_high_risk_tasks(min_risk: float = 0.5, limit: int = 10) -> list[dict]:
    """
    Get tasks with high deferral risk.

    Args:
        min_risk: Minimum risk score to include
        limit: Maximum tasks to return

    Returns:
        List of tasks with their deferral risk assessment
    """
    from cognitex.db.postgres import get_session
    from sqlalchemy import text

    high_risk_tasks = []

    async for session in get_session():
        # Get pending tasks with potential risk factors
        result = await session.execute(text("""
            SELECT
                id, title, deferral_count, project_id, priority,
                estimated_minutes, due_date
            FROM tasks
            WHERE status = 'pending'
            ORDER BY deferral_count DESC, created_at ASC
            LIMIT 50
        """))

        for row in result.fetchall():
            task = {
                "id": row.id,
                "title": row.title,
                "deferral_count": row.deferral_count or 0,
                "project_id": row.project_id,
                "priority": row.priority,
                "estimated_minutes": row.estimated_minutes,
                "due": row.due_date,
            }

            risk = await DeferralRisk.calculate(task)
            if risk.score >= min_risk:
                high_risk_tasks.append({
                    **task,
                    "risk_score": round(risk.score, 2),
                    "risk_factors": risk.factors,
                    "recommended_intervention": risk.recommended_intervention,
                })

        break

    # Sort by risk and limit
    high_risk_tasks.sort(key=lambda x: x["risk_score"], reverse=True)
    return high_risk_tasks[:limit]


async def record_deferral(
    task_id: str,
    inferred_reason: str | None = None,
    friction_at_deferral: float | None = None,
) -> str:
    """
    Record a task deferral for analysis.

    Args:
        task_id: The task being deferred
        inferred_reason: Why we think it was deferred
        friction_at_deferral: Current friction level

    Returns:
        Deferral analysis record ID
    """
    from cognitex.db.postgres import get_session
    from sqlalchemy import text

    deferral_id = f"def_{uuid.uuid4().hex[:12]}"

    async for session in get_session():
        # Get current deferral count
        result = await session.execute(text("""
            SELECT deferral_count FROM tasks WHERE id = :task_id
        """), {"task_id": task_id})
        row = result.fetchone()
        current_count = (row.deferral_count or 0) if row else 0

        # Record the deferral analysis
        await session.execute(text("""
            INSERT INTO deferral_analysis (
                id, task_id, inferred_reason, friction_at_deferral, deferral_count_at_time
            ) VALUES (
                :id, :task_id, :reason, :friction, :count
            )
        """), {
            "id": deferral_id,
            "task_id": task_id,
            "reason": inferred_reason,
            "friction": friction_at_deferral,
            "count": current_count + 1,
        })

        # Update task deferral count
        await session.execute(text("""
            UPDATE tasks
            SET deferral_count = COALESCE(deferral_count, 0) + 1,
                last_deferred_at = NOW()
            WHERE id = :task_id
        """), {"task_id": task_id})

        await session.commit()
        break

    logger.debug("Recorded deferral", task_id=task_id, reason=inferred_reason)
    return deferral_id
