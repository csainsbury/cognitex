"""Agent Trigger System - Scheduled, event-driven, and user-initiated triggers."""

import asyncio
from datetime import datetime
from typing import Callable, Any

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from cognitex.agent.core import Agent, get_agent

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

        redis = get_redis()  # get_redis() is sync, returns async Redis client

        # Subscribe to channels
        pubsub = redis.pubsub()
        await pubsub.subscribe(
            "cognitex:events:email",
            "cognitex:events:calendar",
            "cognitex:events:task",
            "cognitex:events:drive",
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

                # Normalize channel to string for comparison
                channel_str = channel.decode() if isinstance(channel, bytes) else channel
                logger.debug("Event received", channel=channel_str)

                try:
                    import json
                    event_data = json.loads(data) if isinstance(data, (str, bytes)) else data

                    if channel_str == "cognitex:events:email":
                        await self._on_new_email(event_data)
                    elif channel_str == "cognitex:events:calendar":
                        await self._on_calendar_change(event_data)
                    elif channel_str == "cognitex:events:task":
                        await self._on_task_event(event_data)
                    elif channel_str == "cognitex:events:drive":
                        await self._on_drive_change(event_data)
                    else:
                        logger.warning("Unknown event channel", channel=channel_str)

                except Exception as e:
                    logger.error("Event handling failed", channel=channel_str, error=str(e))

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
            # Use ReAct agent to check for urgent items
            response = await self.agent.chat(
                "Quick check: are there any urgent tasks overdue or high-priority emails "
                "that need immediate attention? Only notify me if something is truly urgent."
            )
            # Only send notification if the agent found something urgent
            if response and "no urgent" not in response.lower() and "nothing urgent" not in response.lower():
                await self._send_notification(response, urgency="normal")
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
                    # Use ReAct agent to handle escalation
                    task_titles = [t.get("title", "Unknown") for t in overdue_tasks[:5]]
                    response = await self.agent.chat(
                        f"These tasks are overdue: {', '.join(task_titles)}. "
                        "What should I prioritize and are there any I should follow up on?"
                    )
                    if response:
                        await self._send_notification(
                            f"**Overdue Task Check**\n\n{response}",
                            urgency="normal"
                        )

        except Exception as e:
            logger.error("Overdue check failed", error=str(e))

    # =========================================================================
    # Event trigger handlers
    # =========================================================================

    async def _on_new_email(self, email_data: dict) -> None:
        """Handle new email event from Gmail push notification."""
        history_id = email_data.get("history_id")
        email_address = email_data.get("email_address")
        logger.info("Processing Gmail push notification", history_id=history_id, email_address=email_address)

        try:
            # First, sync new emails from Gmail using the history ID
            from cognitex.services.ingestion import run_incremental_sync

            if history_id:
                logger.info("Syncing emails from history", history_id=history_id)
                sync_result = await run_incremental_sync(history_id)

                if sync_result.get("first_sync"):
                    logger.info("First sync - history ID stored, waiting for next email")
                    return  # First sync just stores the baseline

                if sync_result.get("error"):
                    logger.warning("Incremental sync failed", error=sync_result.get("error"))
                    return  # Will retry on next push

                if sync_result.get("total", 0) == 0:
                    logger.info("No new emails found in history")
                    return  # No new emails, nothing to notify about

                # Check if any tasks were auto-completed from sent emails
                auto_completed = sync_result.get("auto_completed_tasks", [])
                if auto_completed:
                    logger.info("Tasks auto-completed from sent emails", task_ids=auto_completed)
                    await self._send_notification(
                        f"**Tasks Auto-Completed**\n\n"
                        f"I detected that you replied to emails related to {len(auto_completed)} task(s). "
                        f"These tasks have been automatically marked as done.",
                        urgency="low"
                    )

                # We have new emails - prepare summary for agent
                emails = sync_result.get("emails", [])
                email_count = len(emails)
                logger.info("Synced new emails", count=email_count)

                # Build email summary for agent
                email_summaries = []
                for email in emails[:5]:  # Limit to 5 most recent
                    sender = email.get("sender_email", "unknown")
                    subject = email.get("subject", "(no subject)")[:80]
                    snippet = email.get("snippet", "")[:150]
                    email_summaries.append(f"- From: {sender}\n  Subject: {subject}\n  Preview: {snippet}")

                email_list = "\n\n".join(email_summaries)

                # Ask the agent to analyze the new emails
                response = await self.agent.chat(
                    f"I just received {email_count} new email(s). Here are the details:\n\n"
                    f"{email_list}\n\n"
                    "Please analyze these emails and let me know:\n"
                    "1. Are any of these urgent or need immediate attention?\n"
                    "2. Should any tasks be created from these?\n"
                    "3. Do any require a reply?\n"
                    "Only highlight what's truly important."
                )
            else:
                # No history ID - just ask agent to check emails
                response = await self.agent.chat(
                    "A new email notification was received. "
                    "Please check my recent emails for anything that needs my attention."
                )

            # Only notify if the agent found something important
            if response and any(kw in response.lower() for kw in ["task", "reply", "urgent", "important", "action", "need", "require", "attention"]):
                await self._send_notification(
                    f"**New Email Alert**\n\n{response}",
                    urgency="normal"
                )

        except Exception as e:
            logger.error("Email processing failed", error=str(e))

    async def _on_calendar_change(self, event_data: dict) -> None:
        """Handle calendar change event."""
        resource_state = event_data.get("resource_state", "change")
        calendar_id = event_data.get("calendar_id", "primary")
        logger.info("Processing calendar change", resource_state=resource_state, calendar_id=calendar_id)

        try:
            # First, sync calendar from Google to get latest data
            from cognitex.services.ingestion import run_calendar_sync
            logger.info("Syncing calendar before processing change")
            await run_calendar_sync(months_back=0, days_ahead=7)
            logger.info("Calendar sync complete")

            # Now use the agent to check for any notable calendar updates
            response = await self.agent.chat(
                "A calendar change was just detected and I've synced the latest data. "
                "Please check my calendar for today and the next few days "
                "to see if there are any new or updated events I should know about. "
                "Tell me about any meetings that were just added or changed."
            )

            # Only send notification if agent found something worth mentioning
            if response and not any(phrase in response.lower() for phrase in [
                "no new", "no notable", "nothing new", "no changes", "no updates"
            ]):
                await self._send_notification(
                    f"**Calendar Update**\n\n{response}",
                    urgency="low"
                )
        except Exception as e:
            logger.error("Calendar processing failed", error=str(e))

    async def _on_task_event(self, task_data: dict) -> None:
        """Handle task-related events."""
        event_type = task_data.get("event_type", "unknown")
        title = task_data.get("title", "Unknown")
        logger.info("Processing task event", event_type=event_type)

        if event_type == "became_overdue":
            response = await self.agent.chat(
                f"Task '{title}' just became overdue. What should I do about this?"
            )
            if response:
                await self._send_notification(
                    f"**Task Overdue**: {title}\n\n{response}",
                    urgency="high"
                )

    async def _on_drive_change(self, event_data: dict) -> None:
        """Handle Drive change event (file added/modified/deleted)."""
        resource_state = event_data.get("resource_state", "change")
        changed_types = event_data.get("changed", [])
        logger.info("Processing Drive change", state=resource_state, changed=changed_types)

        try:
            # Fetch actual changes from Drive API
            from cognitex.services.drive import DriveService

            drive = DriveService()

            # Get the watch info to find our page token
            from cognitex.services.push_notifications import get_watch_manager
            watch_manager = get_watch_manager()
            watch_info = watch_manager.get_active_watches().get('drive', {})
            page_token = watch_info.get('page_token')

            if not page_token:
                logger.warning("No page token for Drive changes, fetching new one")
                return

            # Fetch changes since last token
            changes = drive.get_changes(page_token)

            if not changes:
                logger.debug("No new Drive changes found")
                return

            # Filter for files in indexed folders (priority folders)
            from cognitex.config import get_settings
            settings = get_settings()
            priority_folders = getattr(settings, 'drive_priority_folders', [])

            relevant_changes = []
            for change in changes.get('changes', []):
                file_info = change.get('file', {})
                file_name = file_info.get('name', 'Unknown')
                parents = file_info.get('parents', [])

                # Check if file is in a priority folder
                if any(folder_id in parents for folder_id in priority_folders):
                    relevant_changes.append({
                        'name': file_name,
                        'mime_type': file_info.get('mimeType'),
                        'modified': file_info.get('modifiedTime'),
                        'change_type': 'removed' if change.get('removed') else 'modified',
                    })

            if relevant_changes:
                # Notify about relevant changes
                change_summary = "\n".join(
                    f"- {c['name']} ({c['change_type']})"
                    for c in relevant_changes[:5]
                )

                response = await self.agent.chat(
                    f"Files changed in your priority folders:\n{change_summary}\n\n"
                    "Should I update my index or is there anything I should note about these changes?"
                )

                if response:
                    await self._send_notification(
                        f"**Drive Changes Detected**\n\n{response}",
                        urgency="low"
                    )

            # Update page token for next time
            new_token = changes.get('newStartPageToken')
            if new_token and 'drive' in watch_manager._active_watches:
                watch_manager._active_watches['drive']['page_token'] = new_token

        except Exception as e:
            logger.error("Drive change processing failed", error=str(e))

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
