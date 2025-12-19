"""P1.4: Interruption firewall and context switch manager.

Implements interruption management from Phase 3 blueprint:
- Mode-gated notifications
- Inbound capture without engagement
- Fixed inbox windows with pre-drafted replies
- Context switch cost accounting
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable

import structlog

from cognitex.agent.state_model import (
    OperatingMode,
    ModeRules,
    UserState,
    get_state_estimator,
)

logger = structlog.get_logger()


class NotificationUrgency(str, Enum):
    """Notification urgency levels."""
    CRITICAL = "critical"       # System failure, emergency
    URGENT = "urgent"           # Time-sensitive, needs attention soon
    IMPORTANT = "important"     # Significant but not time-sensitive
    NORMAL = "normal"           # Standard notification
    LOW = "low"                 # Can be batched/deferred
    SUPPORTIVE = "supportive"   # Encouragement, gentle prompts


class NotificationGate(str, Enum):
    """Notification gating levels."""
    ALL = "all"                 # All notifications pass
    BATCHED = "batched"         # Batch to windows
    URGENT_ONLY = "urgent_only" # Only urgent/critical
    CRITICAL_ONLY = "critical_only"  # Only critical
    SUPPORTIVE = "supportive"   # Allow supportive + urgent
    NONE = "none"               # Block everything


@dataclass
class IncomingItem:
    """An incoming item captured by the firewall."""

    item_id: str
    item_type: str  # email, message, notification, calendar_invite
    source: str  # gmail, discord, calendar, etc.
    subject: str
    preview: str
    urgency: NotificationUrgency = NotificationUrgency.NORMAL
    sender: str | None = None
    suggested_action: str | None = None
    scheduled_revisit: datetime | None = None
    captured_at: datetime = field(default_factory=datetime.now)
    parked: bool = True  # True = captured, not engaged
    queue: str = "inbox"  # inbox, work, personal, research


@dataclass
class InboxWindow:
    """A scheduled window for processing inbox items."""

    window_id: str
    start_time: datetime
    duration_minutes: int = 30
    queue: str = "inbox"  # Which queue to process
    pre_drafted_replies: dict = field(default_factory=dict)
    completed: bool = False


@dataclass
class ContextSwitchCost:
    """Track context switch costs for task transitions."""

    from_task_id: str | None
    to_task_id: str
    estimated_recovery_minutes: int = 5
    actual_recovery_minutes: int | None = None
    switch_time: datetime = field(default_factory=datetime.now)
    interrupted: bool = False  # Was this an unplanned switch?


class InterruptionFirewall:
    """Manages interruptions and notifications based on mode.

    Core responsibilities:
    - Gate notifications based on current operating mode
    - Capture incoming items without requiring engagement
    - Schedule inbox processing windows
    - Track context switch costs
    """

    # Default inbox windows (can be configured per user)
    DEFAULT_INBOX_WINDOWS = [
        {"hour": 9, "minute": 0, "duration": 30, "queue": "inbox"},
        {"hour": 13, "minute": 0, "duration": 20, "queue": "inbox"},
        {"hour": 17, "minute": 0, "duration": 30, "queue": "inbox"},
    ]

    def __init__(self):
        self.state_estimator = get_state_estimator()
        self._captured_items: list[IncomingItem] = []
        self._scheduled_windows: list[InboxWindow] = []
        self._recent_switches: list[ContextSwitchCost] = []
        self._current_task_id: str | None = None

    async def should_notify(
        self,
        urgency: NotificationUrgency,
        source: str | None = None,
    ) -> tuple[bool, str | None]:
        """Check if a notification should pass through.

        Args:
            urgency: Notification urgency level
            source: Source of the notification

        Returns:
            (should_pass, reason)
        """
        state = await self.state_estimator.get_current_state()
        rules = ModeRules.get_rules(state.mode)
        gate_level = rules.get("notification_gate", "batched")

        # Map gate level to allowed urgencies
        allowed = self._get_allowed_urgencies(NotificationGate(gate_level))

        if urgency in allowed:
            return True, None

        # Check for special interrupt conditions
        interrupt_for = rules.get("interrupt_for", [])
        if urgency == NotificationUrgency.CRITICAL and "emergency" in interrupt_for:
            return True, "Critical notification overrides gate"

        return False, f"Blocked by {gate_level} gate in {state.mode.value} mode"

    def _get_allowed_urgencies(
        self,
        gate: NotificationGate,
    ) -> set[NotificationUrgency]:
        """Get urgencies allowed by a gate level."""
        if gate == NotificationGate.ALL:
            return set(NotificationUrgency)
        elif gate == NotificationGate.BATCHED:
            return {NotificationUrgency.CRITICAL, NotificationUrgency.URGENT}
        elif gate == NotificationGate.URGENT_ONLY:
            return {NotificationUrgency.CRITICAL, NotificationUrgency.URGENT}
        elif gate == NotificationGate.CRITICAL_ONLY:
            return {NotificationUrgency.CRITICAL}
        elif gate == NotificationGate.SUPPORTIVE:
            return {
                NotificationUrgency.CRITICAL,
                NotificationUrgency.URGENT,
                NotificationUrgency.SUPPORTIVE,
            }
        elif gate == NotificationGate.NONE:
            return set()  # Nothing passes
        return {NotificationUrgency.CRITICAL}

    async def capture_incoming(
        self,
        item_type: str,
        source: str,
        subject: str,
        preview: str,
        sender: str | None = None,
        urgency: NotificationUrgency = NotificationUrgency.NORMAL,
    ) -> IncomingItem:
        """Capture an incoming item without engaging.

        The item is parked in a queue with a suggested next action
        for processing during the next inbox window.
        """
        item = IncomingItem(
            item_id=f"inc_{uuid.uuid4().hex[:12]}",
            item_type=item_type,
            source=source,
            subject=subject,
            preview=preview,
            sender=sender,
            urgency=urgency,
            suggested_action=self._suggest_action(item_type, preview),
            queue=self._determine_queue(source, subject),
        )

        self._captured_items.append(item)
        logger.info(
            "Captured incoming item",
            item_id=item.item_id,
            type=item_type,
            urgency=urgency.value,
            queue=item.queue,
        )
        return item

    def _suggest_action(self, item_type: str, preview: str) -> str:
        """Generate a suggested next action for a captured item."""
        preview_lower = preview.lower()

        # Check for common patterns
        if item_type == "email":
            if "?" in preview:
                return "Reply with brief answer"
            elif any(w in preview_lower for w in ["fyi", "for your information", "no action"]):
                return "Archive after reading"
            elif any(w in preview_lower for w in ["please review", "feedback", "comments"]):
                return "Schedule review time"
            elif any(w in preview_lower for w in ["deadline", "due", "by end of"]):
                return "Check deadline and add to calendar"
            else:
                return "Read and decide"
        elif item_type == "calendar_invite":
            return "Review and respond to invite"
        elif item_type == "message":
            if "?" in preview:
                return "Reply to question"
            else:
                return "Acknowledge when ready"
        return "Review during next inbox window"

    def _determine_queue(self, source: str, subject: str) -> str:
        """Determine which queue an item belongs to."""
        subject_lower = subject.lower()

        # Research-related
        if any(w in subject_lower for w in ["paper", "manuscript", "grant", "review", "analysis"]):
            return "research"

        # Work admin
        if any(w in subject_lower for w in ["meeting", "schedule", "admin", "hr", "expense"]):
            return "work"

        # Personal
        if source in ["personal_email", "family"]:
            return "personal"

        return "inbox"

    async def get_queued_items(
        self,
        queue: str | None = None,
        urgency: NotificationUrgency | None = None,
        limit: int = 50,
    ) -> list[IncomingItem]:
        """Get items in the capture queue."""
        items = self._captured_items

        if queue:
            items = [i for i in items if i.queue == queue]
        if urgency:
            items = [i for i in items if i.urgency == urgency]

        # Sort by urgency then capture time
        urgency_order = {
            NotificationUrgency.CRITICAL: 0,
            NotificationUrgency.URGENT: 1,
            NotificationUrgency.IMPORTANT: 2,
            NotificationUrgency.NORMAL: 3,
            NotificationUrgency.LOW: 4,
            NotificationUrgency.SUPPORTIVE: 5,
        }
        items.sort(key=lambda i: (urgency_order.get(i.urgency, 3), i.captured_at))

        return items[:limit]

    async def schedule_inbox_window(
        self,
        start_time: datetime,
        duration_minutes: int = 30,
        queue: str = "inbox",
    ) -> InboxWindow:
        """Schedule an inbox processing window."""
        window = InboxWindow(
            window_id=f"win_{uuid.uuid4().hex[:8]}",
            start_time=start_time,
            duration_minutes=duration_minutes,
            queue=queue,
        )
        self._scheduled_windows.append(window)
        logger.info(
            "Scheduled inbox window",
            window_id=window.window_id,
            start=start_time.isoformat(),
            queue=queue,
        )
        return window

    async def get_next_inbox_window(self) -> InboxWindow | None:
        """Get the next scheduled inbox window."""
        now = datetime.now()
        future_windows = [
            w for w in self._scheduled_windows
            if w.start_time > now and not w.completed
        ]
        if not future_windows:
            return None
        return min(future_windows, key=lambda w: w.start_time)

    async def generate_reply_drafts(
        self,
        items: list[IncomingItem],
    ) -> dict[str, str]:
        """Generate draft replies for queued items.

        Returns dict of item_id -> draft_reply
        """
        drafts = {}

        for item in items:
            if item.item_type == "email":
                draft = self._generate_email_draft(item)
                if draft:
                    drafts[item.item_id] = draft

        return drafts

    def _generate_email_draft(self, item: IncomingItem) -> str | None:
        """Generate a draft reply for an email."""
        preview_lower = item.preview.lower()

        # Template-based drafts
        if "?" in item.preview:
            # Question - provide structure
            return f"Hi,\n\nThank you for your message.\n\n[Answer to: {item.preview[:50]}...]\n\nBest regards"

        if any(w in preview_lower for w in ["meeting", "schedule", "available"]):
            return "Hi,\n\nThank you for reaching out about scheduling.\n\n[Check calendar and suggest times]\n\nBest regards"

        if any(w in preview_lower for w in ["thank", "thanks"]):
            return None  # No reply needed for thanks

        # Default acknowledgment
        return f"Hi,\n\nThank you for your email regarding '{item.subject[:30]}...'.\n\n[Your response here]\n\nBest regards"

    async def record_context_switch(
        self,
        to_task_id: str,
        interrupted: bool = False,
    ) -> ContextSwitchCost:
        """Record a context switch between tasks."""
        from_task = self._current_task_id

        # Estimate recovery time based on switch type
        if interrupted:
            recovery = 15  # Interrupted switches are costly
        elif from_task is None:
            recovery = 2  # Starting fresh
        else:
            recovery = 5  # Normal switch

        switch = ContextSwitchCost(
            from_task_id=from_task,
            to_task_id=to_task_id,
            estimated_recovery_minutes=recovery,
            interrupted=interrupted,
        )

        self._recent_switches.append(switch)
        self._current_task_id = to_task_id

        logger.info(
            "Context switch recorded",
            from_task=from_task,
            to_task=to_task_id,
            recovery_estimate=recovery,
            interrupted=interrupted,
        )
        return switch

    async def get_switch_cost_for_task(
        self,
        task_id: str,
    ) -> float:
        """Estimate the context switch cost (0-1) to move to a task."""
        if self._current_task_id == task_id:
            return 0.0  # No switch needed

        if self._current_task_id is None:
            return 0.1  # Starting fresh is cheap

        # Check recent switches for patterns
        recent_to_same = [
            s for s in self._recent_switches[-10:]
            if s.to_task_id == task_id
        ]

        if recent_to_same:
            # Recently worked on this - cheaper to return
            return 0.2

        # Default moderate cost
        return 0.5

    async def get_daily_switch_stats(self) -> dict:
        """Get statistics on context switches for the day."""
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        today_switches = [
            s for s in self._recent_switches
            if s.switch_time >= today
        ]

        total_switches = len(today_switches)
        interrupted = sum(1 for s in today_switches if s.interrupted)
        total_recovery = sum(s.estimated_recovery_minutes for s in today_switches)

        return {
            "total_switches": total_switches,
            "interrupted_switches": interrupted,
            "planned_switches": total_switches - interrupted,
            "estimated_recovery_minutes": total_recovery,
            "switch_rate_per_hour": (
                total_switches / max(1, (datetime.now() - today).seconds / 3600)
            ),
        }

    async def clear_processed_items(self, item_ids: list[str]) -> int:
        """Remove processed items from the capture queue."""
        before = len(self._captured_items)
        self._captured_items = [
            i for i in self._captured_items
            if i.item_id not in item_ids
        ]
        cleared = before - len(self._captured_items)
        logger.info("Cleared processed items", count=cleared)
        return cleared


# Response templates for common communication patterns
RESPONSE_TEMPLATES = {
    "decline": {
        "polite": "Thank you for thinking of me, but I'm unable to take this on at the moment.",
        "busy": "I appreciate the invitation, but my schedule is fully committed right now.",
        "boundary": "I need to decline to protect my current commitments.",
    },
    "defer": {
        "later": "I'd be happy to discuss this, but could we revisit it [next week/after the deadline]?",
        "delegate": "I think [Name] would be better suited to help with this.",
        "clarify_first": "Before I commit, could you help me understand [specific question]?",
    },
    "acknowledge": {
        "received": "Thanks for sending this - I'll review it during my next focused work block.",
        "will_respond": "Got it! I'll get back to you by [time/date].",
        "fyi_noted": "Thanks for the heads up - noted.",
    },
    "boundary": {
        "meeting_limit": "I've reached my meeting limit this week. Could we handle this async?",
        "focus_time": "I'm in focused work mode until [time]. I'll respond after.",
        "off_hours": "I'll pick this up during work hours tomorrow.",
    },
}


class CommunicationTemplates:
    """Pre-built response templates for common patterns.

    Reduces friction for socially ambiguous communications.
    """

    @staticmethod
    def get_template(
        category: str,
        variant: str,
        **kwargs,
    ) -> str | None:
        """Get a response template with optional substitutions."""
        templates = RESPONSE_TEMPLATES.get(category, {})
        template = templates.get(variant)

        if template and kwargs:
            for key, value in kwargs.items():
                template = template.replace(f"[{key}]", str(value))

        return template

    @staticmethod
    def list_templates() -> dict:
        """List all available template categories and variants."""
        return {
            cat: list(variants.keys())
            for cat, variants in RESPONSE_TEMPLATES.items()
        }


# Singleton instance
_firewall: InterruptionFirewall | None = None


def get_interruption_firewall() -> InterruptionFirewall:
    """Get the interruption firewall singleton."""
    global _firewall
    if _firewall is None:
        _firewall = InterruptionFirewall()
    return _firewall
