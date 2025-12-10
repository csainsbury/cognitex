"""Agent Trigger System - Scheduled, event-driven, and user-initiated triggers."""

import asyncio
from datetime import datetime
from typing import Callable, Any

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from cognitex.agent.core import Agent, AgentMode, get_agent

logger = structlog.get_logger()


class TriggerSystem:
    """
    Manages all agent triggers:
    - Scheduled (cron-based)
    - Event-driven (Redis pub/sub)
    - Threshold-based (monitors)
    """

    def __init__(self):
        self.scheduler = AsyncIOScheduler()
        self.agent: Agent | None = None
        self._running = False
        self._event_tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        """Start the trigger system."""
        if self._running:
            return

        logger.info("Starting trigger system")

        # Get agent instance
        self.agent = await get_agent()

        # Setup scheduled triggers
        self._setup_scheduled_triggers()

        # Start event listeners
        await self._start_event_listeners()

        # Start scheduler
        self.scheduler.start()
        self._running = True

        logger.info("Trigger system started")

    async def stop(self) -> None:
        """Stop the trigger system."""
        if not self._running:
            return

        logger.info("Stopping trigger system")

        # Stop scheduler
        self.scheduler.shutdown(wait=True)

        # Cancel event tasks
        for task in self._event_tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        self._running = False
        logger.info("Trigger system stopped")

    def _setup_scheduled_triggers(self) -> None:
        """Setup cron-based scheduled triggers."""

        # Morning briefing at 8am
        self.scheduler.add_job(
            self._morning_briefing,
            CronTrigger(hour=8, minute=0),
            id="morning_briefing",
            name="Morning Briefing",
            replace_existing=True,
        )

        # Evening review at 6pm
        self.scheduler.add_job(
            self._evening_review,
            CronTrigger(hour=18, minute=0),
            id="evening_review",
            name="Evening Review",
            replace_existing=True,
        )

        # Hourly monitoring (during work hours 9am-6pm)
        self.scheduler.add_job(
            self._hourly_check,
            CronTrigger(hour="9-18", minute=0),
            id="hourly_check",
            name="Hourly Check",
            replace_existing=True,
        )

        # Task overdue check (twice daily)
        self.scheduler.add_job(
            self._check_overdue_tasks,
            CronTrigger(hour="10,15", minute=0),
            id="overdue_check",
            name="Overdue Task Check",
            replace_existing=True,
        )

        logger.info("Scheduled triggers configured")

    async def _start_event_listeners(self) -> None:
        """Start Redis pub/sub listeners for events."""
        from cognitex.db.redis import get_redis

        redis = await get_redis()

        # Subscribe to channels
        pubsub = redis.pubsub()
        await pubsub.subscribe(
            "cognitex:events:email",
            "cognitex:events:calendar",
            "cognitex:events:task",
        )

        # Start listener task
        task = asyncio.create_task(self._event_listener(pubsub))
        self._event_tasks.append(task)

        logger.info("Event listeners started")

    async def _event_listener(self, pubsub) -> None:
        """Listen for events on Redis pub/sub."""
        try:
            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue

                channel = message["channel"]
                data = message["data"]

                logger.debug("Event received", channel=channel)

                try:
                    import json
                    event_data = json.loads(data) if isinstance(data, (str, bytes)) else data

                    if channel == b"cognitex:events:email":
                        await self._on_new_email(event_data)
                    elif channel == b"cognitex:events:calendar":
                        await self._on_calendar_change(event_data)
                    elif channel == b"cognitex:events:task":
                        await self._on_task_event(event_data)

                except Exception as e:
                    logger.error("Event handling failed", channel=channel, error=str(e))

        except asyncio.CancelledError:
            await pubsub.unsubscribe()
            raise

    # =========================================================================
    # Scheduled trigger handlers
    # =========================================================================

    async def _morning_briefing(self) -> None:
        """Handle morning briefing trigger."""
        logger.info("Triggering morning briefing")
        try:
            briefing = await self.agent.morning_briefing()
            # Send via Discord notification
            await self._send_notification(briefing, urgency="normal")
        except Exception as e:
            logger.error("Morning briefing failed", error=str(e))

    async def _evening_review(self) -> None:
        """Handle evening review trigger."""
        logger.info("Triggering evening review")
        try:
            review = await self.agent.evening_review()
            await self._send_notification(review, urgency="low")
        except Exception as e:
            logger.error("Evening review failed", error=str(e))

    async def _hourly_check(self) -> None:
        """Handle hourly monitoring check."""
        logger.debug("Triggering hourly check")
        try:
            result = await self.agent.check_for_urgent()
            if result.user_notification:
                await self._send_notification(result.user_notification, urgency="normal")
        except Exception as e:
            logger.error("Hourly check failed", error=str(e))

    async def _check_overdue_tasks(self) -> None:
        """Check for overdue tasks and escalate if needed."""
        logger.info("Checking for overdue tasks")
        try:
            from cognitex.agent.tools import GetTasksTool

            tool = GetTasksTool()
            result = await tool.execute(include_overdue=True, limit=10)

            if result.success and result.data:
                overdue_tasks = result.data
                if overdue_tasks:
                    # Escalate
                    await self.agent.run(
                        mode=AgentMode.ESCALATE,
                        trigger=f"Found {len(overdue_tasks)} overdue tasks",
                        trigger_data={"tasks": overdue_tasks},
                    )

        except Exception as e:
            logger.error("Overdue check failed", error=str(e))

    # =========================================================================
    # Event trigger handlers
    # =========================================================================

    async def _on_new_email(self, email_data: dict) -> None:
        """Handle new email event."""
        logger.info("Processing new email", subject=email_data.get("subject", "")[:50])
        try:
            result = await self.agent.process_new_email(email_data)
            if result.user_notification:
                await self._send_notification(result.user_notification, urgency="normal")
        except Exception as e:
            logger.error("Email processing failed", error=str(e))

    async def _on_calendar_change(self, event_data: dict) -> None:
        """Handle calendar change event."""
        logger.info("Processing calendar change", title=event_data.get("title", "")[:50])
        try:
            result = await self.agent.process_calendar_change(event_data)
            if result.user_notification:
                await self._send_notification(result.user_notification, urgency="low")
        except Exception as e:
            logger.error("Calendar processing failed", error=str(e))

    async def _on_task_event(self, task_data: dict) -> None:
        """Handle task-related events."""
        event_type = task_data.get("event_type", "unknown")
        logger.info("Processing task event", event_type=event_type)

        # Could trigger escalation for certain events
        if event_type == "became_overdue":
            await self.agent.run(
                mode=AgentMode.ESCALATE,
                trigger=f"Task became overdue: {task_data.get('title', 'Unknown')}",
                trigger_data=task_data,
            )

    # =========================================================================
    # Utilities
    # =========================================================================

    async def _send_notification(self, message: str, urgency: str = "normal") -> None:
        """Send a notification via the notification tool."""
        from cognitex.agent.tools import SendNotificationTool

        tool = SendNotificationTool()
        await tool.execute(message=message, urgency=urgency)

    def trigger_now(self, trigger_id: str) -> None:
        """Manually trigger a scheduled job immediately."""
        job = self.scheduler.get_job(trigger_id)
        if job:
            logger.info("Manual trigger", trigger_id=trigger_id)
            job.modify(next_run_time=datetime.now())
        else:
            logger.warning("Trigger not found", trigger_id=trigger_id)

    def list_scheduled(self) -> list[dict]:
        """List all scheduled triggers."""
        jobs = []
        for job in self.scheduler.get_jobs():
            jobs.append({
                "id": job.id,
                "name": job.name,
                "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
            })
        return jobs


# Singleton
_trigger_system: TriggerSystem | None = None


async def get_trigger_system() -> TriggerSystem:
    """Get or create the trigger system singleton."""
    global _trigger_system
    if _trigger_system is None:
        _trigger_system = TriggerSystem()
    return _trigger_system


async def start_triggers() -> TriggerSystem:
    """Start the trigger system."""
    system = await get_trigger_system()
    await system.start()
    return system


async def stop_triggers() -> None:
    """Stop the trigger system."""
    if _trigger_system:
        await _trigger_system.stop()
