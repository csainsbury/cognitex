"""Agent Trigger System - Scheduled, event-driven, and user-initiated triggers."""

import asyncio
from datetime import datetime
from typing import Callable, Any

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

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

        # Start context pack trigger system
        await self._start_context_pack_triggers()

        # Start scheduler
        self.scheduler.start()
        self._running = True

        logger.info("Trigger system started")

    async def _start_context_pack_triggers(self) -> None:
        """Start the context pack trigger system."""
        try:
            from cognitex.agent.context_pack import get_context_pack_triggers
            pack_triggers = get_context_pack_triggers()
            await pack_triggers.start()
            logger.info("Context pack triggers started")
        except Exception as e:
            logger.warning("Failed to start context pack triggers", error=str(e))

    async def stop(self) -> None:
        """Stop the trigger system."""
        if not self._running:
            return

        logger.info("Stopping trigger system")

        # Stop context pack triggers
        try:
            from cognitex.agent.context_pack import get_context_pack_triggers
            pack_triggers = get_context_pack_triggers()
            await pack_triggers.stop()
        except Exception:
            pass

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

        # Hourly monitoring DISABLED - too noisy
        # Only do morning/evening briefings and specific task checks
        # self.scheduler.add_job(
        #     self._hourly_check,
        #     CronTrigger(hour="9-18", minute=0),
        #     id="hourly_check",
        #     name="Hourly Check",
        #     replace_existing=True,
        # )

        # Task overdue check (twice daily)
        self.scheduler.add_job(
            self._check_overdue_tasks,
            CronTrigger(hour="10,15", minute=0),
            id="overdue_check",
            name="Overdue Task Check",
            replace_existing=True,
        )

        # Daily GitHub sync at 3am (overnight, low activity time)
        self.scheduler.add_job(
            self._github_sync,
            CronTrigger(hour=3, minute=0),
            id="github_sync",
            name="Daily GitHub Sync",
            replace_existing=True,
        )

        # Drive changes poll every 15 minutes
        self.scheduler.add_job(
            self._drive_poll,
            IntervalTrigger(minutes=15),
            id="drive_poll",
            name="Drive Changes Poll",
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

        # Start Google Pub/Sub listener for Gmail (pull-based, no webhook needed)
        pubsub_task = asyncio.create_task(self._gmail_pubsub_listener())
        self._event_tasks.append(pubsub_task)

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

    async def _gmail_pubsub_listener(self) -> None:
        """Pull Gmail notifications from Google Cloud Pub/Sub (no webhook needed)."""
        from cognitex.config import get_settings

        settings = get_settings()
        topic = settings.google_pubsub_topic

        if not topic:
            logger.info("No GOOGLE_PUBSUB_TOPIC configured, skipping Pub/Sub listener")
            return

        # Extract project and topic name from full path
        # Format: projects/PROJECT_ID/topics/TOPIC_NAME
        try:
            parts = topic.split("/")
            project_id = parts[1]
            topic_name = parts[3]
            subscription_name = f"{topic_name}-pull"
        except (IndexError, ValueError):
            logger.error("Invalid GOOGLE_PUBSUB_TOPIC format", topic=topic)
            return

        try:
            from google.cloud import pubsub_v1
            from google.api_core import retry
        except ImportError:
            logger.warning("google-cloud-pubsub not installed, skipping Pub/Sub listener")
            return

        subscriber = pubsub_v1.SubscriberClient()
        subscription_path = subscriber.subscription_path(project_id, subscription_name)

        # Try to create subscription if it doesn't exist
        try:
            subscriber.create_subscription(
                request={
                    "name": subscription_path,
                    "topic": topic,
                    "ack_deadline_seconds": 60,
                }
            )
            logger.info("Created Pub/Sub subscription", subscription=subscription_name)
        except Exception as e:
            if "already exists" not in str(e).lower():
                logger.debug("Subscription exists or error", error=str(e))

        logger.info("Starting Gmail Pub/Sub listener", subscription=subscription_path)

        while True:
            try:
                # Pull messages with a timeout
                response = await asyncio.to_thread(
                    subscriber.pull,
                    request={"subscription": subscription_path, "max_messages": 10},
                    retry=retry.Retry(deadline=30),
                )

                if response.received_messages:
                    ack_ids = []
                    for msg in response.received_messages:
                        try:
                            import base64
                            import json

                            # Decode the message data
                            data = json.loads(msg.message.data.decode("utf-8"))
                            email_address = data.get("emailAddress")
                            history_id = data.get("historyId")

                            logger.info(
                                "Gmail notification received via Pub/Sub",
                                email_address=email_address,
                                history_id=history_id,
                            )

                            # Process as email event
                            await self._on_new_email({
                                "type": "gmail_push",
                                "email_address": email_address,
                                "history_id": history_id,
                            })

                            ack_ids.append(msg.ack_id)
                        except Exception as e:
                            logger.error("Failed to process Pub/Sub message", error=str(e))
                            ack_ids.append(msg.ack_id)  # Ack anyway to avoid redelivery

                    # Acknowledge processed messages
                    if ack_ids:
                        await asyncio.to_thread(
                            subscriber.acknowledge,
                            request={"subscription": subscription_path, "ack_ids": ack_ids},
                        )

                # Wait before next poll
                await asyncio.sleep(5)

            except asyncio.CancelledError:
                logger.info("Gmail Pub/Sub listener cancelled")
                raise
            except Exception as e:
                logger.error("Pub/Sub pull error", error=str(e))
                await asyncio.sleep(30)  # Wait longer on error

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

    async def _github_sync(self) -> None:
        """Daily sync of configured GitHub repositories."""
        from cognitex.config import get_settings

        settings = get_settings()
        repos_str = settings.github_auto_sync_repos

        if not repos_str:
            logger.info("No repos configured for auto-sync (GITHUB_AUTO_SYNC_REPOS)")
            return

        repos = [r.strip() for r in repos_str.split(",") if r.strip()]
        if not repos:
            return

        logger.info("Starting daily GitHub sync", repos=repos)

        try:
            from cognitex.services.github import GitHubService
            from cognitex.services.ingestion import sync_github_repo

            synced = []
            failed = []

            for repo in repos:
                try:
                    logger.info("Syncing repository", repo=repo)
                    result = await sync_github_repo(repo)
                    files_synced = result.get("files_synced", 0)
                    synced.append(f"{repo} ({files_synced} files)")
                    logger.info("Repository synced", repo=repo, files=files_synced)
                except Exception as e:
                    failed.append(f"{repo}: {str(e)[:50]}")
                    logger.error("Failed to sync repository", repo=repo, error=str(e))

            # Log summary
            if synced:
                logger.info("GitHub sync complete", synced=synced, failed=failed)

            # Only notify if there were failures
            if failed:
                await self._send_notification(
                    f"**GitHub Sync Issues**\n\n"
                    f"Synced: {len(synced)} repos\n"
                    f"Failed: {', '.join(failed)}",
                    urgency="low"
                )

        except Exception as e:
            logger.error("GitHub sync failed", error=str(e))

    async def _drive_poll(self) -> None:
        """
        Poll Google Drive for changes every 15 minutes.

        Uses the Drive changes API with a stored page token to detect
        new/modified files in priority folders and auto-index them.
        """
        logger.debug("Starting Drive poll")

        try:
            from cognitex.db.redis import get_redis
            from cognitex.services.drive import DriveService, PRIORITY_FOLDERS
            from cognitex.services.ingestion import auto_index_drive_file

            redis = get_redis()
            drive = DriveService()

            # Get stored page token or initialize
            page_token = await redis.get("cognitex:drive:page_token")

            if page_token:
                page_token = page_token.decode() if isinstance(page_token, bytes) else page_token
            else:
                # First run - get current token and store it
                page_token = drive.get_start_page_token()
                await redis.set("cognitex:drive:page_token", page_token)
                logger.info("Drive poll initialized", page_token=page_token)
                return  # First run just establishes baseline

            # Fetch changes since last poll
            changes_response = drive.get_changes(page_token)
            changes = changes_response.get('changes', [])
            new_token = changes_response.get('newStartPageToken', page_token)

            if not changes:
                logger.debug("No Drive changes detected")
                await redis.set("cognitex:drive:page_token", new_token)
                return

            logger.info("Drive changes detected", count=len(changes))

            # Get priority folder IDs for filtering
            priority_folder_ids = set()
            for folder_name in PRIORITY_FOLDERS:
                folder_id = drive.get_folder_id_by_name(folder_name)
                if folder_id:
                    priority_folder_ids.add(folder_id)

            # Process changes, filtering to priority folders
            relevant_changes = []
            files_to_index = []

            for change in changes:
                file_info = change.get('file', {})
                file_id = file_info.get('id')
                file_name = file_info.get('name', 'Unknown')
                mime_type = file_info.get('mimeType', '')
                parents = file_info.get('parents', [])
                is_removed = change.get('removed', False)
                is_trashed = file_info.get('trashed', False)

                # Skip folders, removed files, and trashed files
                if mime_type == 'application/vnd.google-apps.folder':
                    continue
                if is_removed or is_trashed:
                    continue

                # Check if file is in a priority folder (direct parent or ancestor)
                in_priority = any(pid in priority_folder_ids for pid in parents)

                if not in_priority:
                    # Check one level up (for nested files)
                    for parent_id in parents:
                        try:
                            parent_info = drive.get_file_metadata(parent_id)
                            grandparents = parent_info.get('parents', [])
                            if any(gp in priority_folder_ids for gp in grandparents):
                                in_priority = True
                                break
                        except Exception:
                            pass

                if in_priority:
                    relevant_changes.append({
                        'id': file_id,
                        'name': file_name,
                        'mime_type': mime_type,
                        'modified': file_info.get('modifiedTime'),
                    })

                    if file_id:
                        files_to_index.append({
                            'id': file_id,
                            'name': file_name,
                            'mime_type': mime_type,
                        })

            # Auto-index changed files in priority folders
            indexed_files = []
            for file_data in files_to_index:
                try:
                    logger.info(
                        "Auto-indexing changed Drive file",
                        file=file_data['name'],
                        mime_type=file_data['mime_type']
                    )
                    result = await auto_index_drive_file(
                        file_id=file_data['id'],
                        file_name=file_data['name'],
                        mime_type=file_data['mime_type'],
                    )
                    if result.get('indexed'):
                        indexed_files.append({
                            'name': file_data['name'],
                            'chunks': result.get('chunks_created', 0),
                        })
                except Exception as e:
                    logger.warning(
                        "Failed to index Drive file",
                        file=file_data['name'],
                        error=str(e)
                    )

            # Store new page token
            await redis.set("cognitex:drive:page_token", new_token)

            # Log summary
            if indexed_files:
                logger.info(
                    "Drive poll complete",
                    changes=len(relevant_changes),
                    indexed=len(indexed_files),
                )

                # Notify about significant changes (3+ files or important docs)
                if len(indexed_files) >= 3:
                    file_list = ", ".join(f['name'] for f in indexed_files[:5])
                    await self._send_notification(
                        f"**Drive Sync**\n\n"
                        f"Indexed {len(indexed_files)} updated files: {file_list}"
                        + ("..." if len(indexed_files) > 5 else ""),
                        urgency="low"
                    )

        except Exception as e:
            logger.error("Drive poll failed", error=str(e))

    # =========================================================================
    # Event trigger handlers
    # =========================================================================

    async def _on_new_email(self, email_data: dict) -> None:
        """Handle new email event from Gmail push notification.

        Only surfaces emails that are truly actionable:
        - Require a response/reply
        - Have deadlines or time-sensitive content
        - Are from important contacts
        - Result in task creation

        Filters out:
        - FYI/informational emails
        - Newsletters/marketing
        - Auto-generated notifications
        - Calendar invites (handled separately)
        """
        history_id = email_data.get("history_id")
        email_address = email_data.get("email_address")
        logger.info("Processing Gmail push notification", history_id=history_id, email_address=email_address)

        try:
            from cognitex.db.redis import get_redis
            from cognitex.services.ingestion import run_incremental_sync

            # Deduplicate: Skip if we've already processed this history_id recently
            if history_id:
                redis = get_redis()
                dedup_key = f"cognitex:email:processed:{history_id}"
                already_processed = await redis.get(dedup_key)
                if already_processed:
                    logger.debug("Skipping duplicate email notification", history_id=history_id)
                    return
                # Mark as processed with 5-minute TTL (Google may send duplicates within seconds)
                await redis.set(dedup_key, "1", ex=300)

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
                    # Only notify for task completions - this IS actionable feedback
                    await self._send_notification(
                        f"**Tasks Auto-Completed**\n\n"
                        f"I detected that you replied to emails related to {len(auto_completed)} task(s). "
                        f"These tasks have been automatically marked as done.",
                        urgency="low"
                    )

                # We have new emails - filter to only actionable ones
                emails = sync_result.get("emails", [])
                actionable_emails = self._filter_actionable_emails(emails)

                if not actionable_emails:
                    logger.info("No actionable emails in batch", total=len(emails))
                    return  # All emails were filtered as non-actionable

                logger.info("Found actionable emails", actionable=len(actionable_emails), total=len(emails))

                # Build email summary for agent (only actionable ones)
                email_summaries = []
                for email in actionable_emails[:5]:  # Limit to 5 most recent
                    sender = email.get("sender_email", "unknown")
                    subject = email.get("subject", "(no subject)")[:80]
                    snippet = email.get("snippet", "")[:150]
                    email_summaries.append(f"- From: {sender}\n  Subject: {subject}\n  Preview: {snippet}")

                email_list = "\n\n".join(email_summaries)

                # Ask the agent to analyze ONLY if we have actionable emails
                response = await self.agent.chat(
                    f"I received {len(actionable_emails)} email(s) that may need action. Here are the details:\n\n"
                    f"{email_list}\n\n"
                    "Please analyze these emails:\n"
                    "1. Which ones require a reply? (be specific about what to reply)\n"
                    "2. Should any tasks be created? (create them if so)\n"
                    "3. Are any truly urgent (deadline within 24h)?\n\n"
                    "If none of these require immediate action, just say 'No action needed' and I won't notify."
                )
            else:
                # No history ID - skip notification (this shouldn't happen often)
                logger.debug("Email notification without history ID, skipping")
                return

            # STRICT filter: Only notify if agent explicitly found action items
            # Look for concrete action indicators, not just general keywords
            if response:
                response_lower = response.lower()
                # Skip if agent explicitly says no action needed
                if any(phrase in response_lower for phrase in [
                    "no action needed", "no action required", "nothing urgent",
                    "no immediate action", "none of these require", "no tasks to create",
                    "fyi only", "informational only"
                ]):
                    logger.info("Agent determined no action needed, skipping notification")
                    return

                # Only notify if there are concrete action items
                action_indicators = [
                    "should reply", "need to reply", "reply to", "respond to",
                    "created task", "creating task", "task created",
                    "deadline", "due by", "due date", "urgent",
                    "action required", "please", "waiting for your",
                    "follow up", "follow-up"
                ]
                if any(indicator in response_lower for indicator in action_indicators):
                    await self._send_notification(
                        f"**Email Action Needed**\n\n{response}",
                        urgency="normal"
                    )
                else:
                    logger.info("No concrete action items found, skipping notification")

            # Trigger context pack refresh for related events
            try:
                from cognitex.agent.context_pack import get_context_pack_triggers
                pack_triggers = get_context_pack_triggers()
                for email in emails[:3]:  # Check first 3 emails
                    await pack_triggers.on_email_received({
                        "sender": email.get("sender_email", ""),
                        "subject": email.get("subject", ""),
                    })
            except Exception as pack_err:
                logger.warning("Context pack refresh failed", error=str(pack_err))

        except Exception as e:
            logger.error("Email processing failed", error=str(e))

    async def _on_calendar_change(self, event_data: dict) -> None:
        """Handle calendar change event.

        Calendar changes are synced silently - no Discord notification.
        Users can check /today or the morning briefing for calendar updates.
        Only truly urgent changes (same-day new meetings) might warrant notification.
        """
        resource_state = event_data.get("resource_state", "change")
        calendar_id = event_data.get("calendar_id", "primary")
        logger.info("Processing calendar change", resource_state=resource_state, calendar_id=calendar_id)

        try:
            # Sync calendar from Google to get latest data (silent)
            from cognitex.services.ingestion import run_calendar_sync
            logger.info("Syncing calendar silently")
            await run_calendar_sync(months_back=0, days_ahead=7)
            logger.info("Calendar sync complete - no notification sent")

            # NO notification for calendar changes - too noisy
            # Users can check /today or morning briefing for updates
            # Only exception would be urgent same-day meetings, but we skip those too
            # to avoid noise from every calendar invite response

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
            from cognitex.services.drive import DriveService, PRIORITY_FOLDERS

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

            # Get priority folder IDs
            priority_folder_ids = set()
            for folder_name in PRIORITY_FOLDERS:
                folder_id = drive.get_folder_id_by_name(folder_name)
                if folder_id:
                    priority_folder_ids.add(folder_id)

            relevant_changes = []
            files_to_index = []

            for change in changes.get('changes', []):
                file_info = change.get('file', {})
                file_id = file_info.get('id')
                file_name = file_info.get('name', 'Unknown')
                mime_type = file_info.get('mimeType', '')
                parents = file_info.get('parents', [])
                is_removed = change.get('removed', False)

                # Check if file is in a priority folder (direct or nested)
                in_priority_folder = any(pid in priority_folder_ids for pid in parents)

                if in_priority_folder or self._is_in_priority_tree(drive, parents, priority_folder_ids):
                    relevant_changes.append({
                        'id': file_id,
                        'name': file_name,
                        'mime_type': mime_type,
                        'modified': file_info.get('modifiedTime'),
                        'change_type': 'removed' if is_removed else 'modified',
                    })

                    # Queue for indexing if not removed
                    if not is_removed and file_id:
                        files_to_index.append({
                            'id': file_id,
                            'name': file_name,
                            'mime_type': mime_type,
                        })

            # Auto-index changed files
            indexed_files = []
            if files_to_index:
                from cognitex.services.ingestion import auto_index_drive_file

                for file_data in files_to_index:
                    logger.info(
                        "Auto-indexing changed file",
                        file=file_data['name'],
                        mime_type=file_data['mime_type']
                    )
                    result = await auto_index_drive_file(
                        file_id=file_data['id'],
                        file_name=file_data['name'],
                        mime_type=file_data['mime_type'],
                    )
                    if result.get('indexed'):
                        indexed_files.append({
                            'name': file_data['name'],
                            'chunks': result.get('chunks_created', 0),
                            'topics': result.get('topics_created', 0),
                        })

            # Drive changes are indexed silently - no notification
            # Users can use /documents or doc-search to find updated files
            if relevant_changes:
                logger.info(
                    "Drive changes indexed silently",
                    changes=len(relevant_changes),
                    indexed=len(indexed_files),
                )
                # NO notification - too noisy. Files are indexed and searchable.

            # Update page token for next time
            new_token = changes.get('newStartPageToken')
            if new_token and 'drive' in watch_manager._active_watches:
                watch_manager._active_watches['drive']['page_token'] = new_token

        except Exception as e:
            logger.error("Drive change processing failed", error=str(e))

    def _is_in_priority_tree(self, drive, parent_ids: list, priority_folder_ids: set) -> bool:
        """Check if any parent is in the priority folder tree (for nested files)."""
        # Simple depth-limited check to avoid infinite loops
        for parent_id in parent_ids:
            try:
                parent_info = drive.get_file_metadata(parent_id)
                if parent_info:
                    grandparents = parent_info.get('parents', [])
                    if any(gp in priority_folder_ids for gp in grandparents):
                        return True
            except Exception:
                pass
        return False

    # =========================================================================
    # Email Filtering
    # =========================================================================

    # Patterns that indicate non-actionable emails
    NOISE_SENDERS = [
        "noreply", "no-reply", "donotreply", "mailer-daemon",
        "notifications@", "alerts@", "digest@", "newsletter@",
        "marketing@", "promo@", "info@", "support@", "help@",
        "calendar-notification", "notify@", "updates@",
    ]

    NOISE_SUBJECTS = [
        "unsubscribe", "newsletter", "weekly digest", "daily digest",
        "your order", "shipping confirmation", "delivery notification",
        "password reset", "verify your email", "confirm your",
        "receipt for", "invoice", "payment received",
        "out of office", "automatic reply", "auto-reply",
        "calendar:", "invitation:", "accepted:", "declined:",
        "fyi:", "fyi -", "[fyi]", "for your information",
        "thank you for", "thanks for your",
    ]

    def _filter_actionable_emails(self, emails: list[dict]) -> list[dict]:
        """Filter emails to only those that are potentially actionable.

        Filters out:
        - Auto-generated notifications
        - Marketing/newsletters
        - Calendar invites (handled separately)
        - FYI/informational emails
        - Receipts and confirmations
        - Out-of-office replies
        """
        actionable = []

        for email in emails:
            sender = email.get("sender_email", "").lower()
            subject = email.get("subject", "").lower()
            snippet = email.get("snippet", "").lower()

            # Skip noise senders
            if any(noise in sender for noise in self.NOISE_SENDERS):
                logger.debug("Filtered noise sender", sender=sender[:30])
                continue

            # Skip noise subjects
            if any(noise in subject for noise in self.NOISE_SUBJECTS):
                logger.debug("Filtered noise subject", subject=subject[:30])
                continue

            # Skip calendar invites (these come through calendar notifications)
            if "calendar-notification@google.com" in sender:
                continue
            if subject.startswith(("invitation:", "updated invitation:", "accepted:", "declined:")):
                continue

            # Look for signals that indicate actionable content
            actionable_signals = [
                "?" in snippet,  # Questions typically need answers
                "please" in snippet,
                "could you" in snippet,
                "can you" in snippet,
                "would you" in snippet,
                "need" in snippet and "your" in snippet,
                "deadline" in snippet,
                "urgent" in subject or "urgent" in snippet,
                "asap" in subject or "asap" in snippet,
                "action required" in subject or "action required" in snippet,
                "waiting for" in snippet,
                "follow up" in snippet or "follow-up" in snippet,
                "reminder" in subject,  # But not auto-reminders
            ]

            # If it has actionable signals, include it
            if any(actionable_signals):
                actionable.append(email)
                continue

            # For emails without clear signals, check if it's from a person (not automated)
            # Real emails from people are more likely to need attention
            is_from_person = (
                "@gmail.com" in sender or
                "@yahoo.com" in sender or
                "@outlook.com" in sender or
                "@hotmail.com" in sender or
                # Assume company emails are from people if not in noise list
                not any(noise in sender for noise in ["notification", "alert", "system", "auto"])
            )

            # Only include person emails that have some content suggesting interaction
            if is_from_person:
                # Check for interaction patterns in snippet
                interaction_patterns = ["hi ", "hello", "hey ", "dear ", "thanks", "thank you"]
                if any(pattern in snippet[:50] for pattern in interaction_patterns):
                    actionable.append(email)

        return actionable

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
