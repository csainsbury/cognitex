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
        self._user_email: str | None = None  # Cached user email for filtering sent emails

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

        # Start autonomous agent
        await self._start_autonomous_agent()

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

    async def _start_autonomous_agent(self) -> None:
        """Start the autonomous agent for proactive graph management."""
        try:
            from cognitex.agent.autonomous import start_autonomous_agent
            await start_autonomous_agent()
            logger.info("Autonomous agent started")
        except Exception as e:
            logger.warning("Failed to start autonomous agent", error=str(e))

    async def stop(self) -> None:
        """Stop the trigger system."""
        if not self._running:
            return

        logger.info("Stopping trigger system")

        # Stop autonomous agent
        try:
            from cognitex.agent.autonomous import stop_autonomous_agent
            await stop_autonomous_agent()
        except Exception:
            pass

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

        # Daily learning policy update (2am - low activity time)
        self.scheduler.add_job(
            self._run_policy_update,
            CronTrigger(hour=2, minute=0),
            id="policy_update",
            name="Learning Policy Update",
            replace_existing=True,
        )

        # Coding sessions sync (every 2 hours during work hours)
        # Ingests Claude Code and other CLI sessions for project context
        self.scheduler.add_job(
            self._coding_sessions_sync,
            CronTrigger(hour="8,10,12,14,16,18", minute=30),
            id="coding_sessions_sync",
            name="Coding Sessions Sync",
            replace_existing=True,
        )

        # Stuck task detection (every 30 minutes during work hours)
        # Checks for tasks where elapsed time > 150% of estimated time
        self.scheduler.add_job(
            self._check_stuck_tasks,
            IntervalTrigger(minutes=30),
            id="stuck_task_check",
            name="Stuck Task Detection",
            replace_existing=True,
        )

        # Transition nudge check (every 10 minutes)
        # Checks if user just finished a heavy meeting and suggests recovery tasks
        self.scheduler.add_job(
            self._check_mode_transition,
            IntervalTrigger(minutes=10),
            id="transition_check",
            name="Transition Nudge Check",
            replace_existing=True,
        )

        # Hourly state re-evaluation based on diurnal energy curve
        # Automatically transitions modes as energy naturally shifts through the day
        self.scheduler.add_job(
            self._hourly_state_update,
            CronTrigger(minute=0),  # Every hour on the hour
            id="hourly_state_update",
            name="Hourly State Update",
            replace_existing=True,
        )

        # Memory consolidation ("Dreaming") at 4am
        # Summarizes daily events, extracts patterns, archives old memories
        self.scheduler.add_job(
            self._run_consolidation,
            CronTrigger(hour=4, minute=0),
            id="memory_consolidation",
            name="Memory Consolidation",
            replace_existing=True,
        )

        # Semantic analysis sweep (every 2 hours during daytime)
        # Catches any priority folder docs that were indexed but not semantically analyzed
        self.scheduler.add_job(
            self._semantic_analysis_sweep,
            CronTrigger(hour="9,11,13,15,17", minute=15),
            id="semantic_sweep",
            name="Semantic Analysis Sweep",
            replace_existing=True,
        )

        # Weekly memory distillation — Sunday at 8pm
        # Proposes MEMORY.md updates from the week's observations
        self.scheduler.add_job(
            self._run_weekly_distillation,
            CronTrigger(day_of_week="sun", hour=20, minute=0),
            id="weekly_distillation",
            name="Weekly Memory Distillation",
            replace_existing=True,
        )

        # Monthly archive — 1st of month at 3:30am
        # Moves daily log files older than 30 days to archive/
        self.scheduler.add_job(
            self._run_monthly_archive,
            CronTrigger(day=1, hour=3, minute=30),
            id="monthly_archive",
            name="Monthly Log Archive",
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
        from cognitex.agent.action_log import log_action
        try:
            briefing = await self.agent.morning_briefing()
            # Send via Discord notification
            await self._send_notification(briefing, urgency="normal")
            await log_action("morning_briefing", "trigger", summary=briefing[:200] if briefing else "Generated morning briefing")
        except Exception as e:
            logger.error("Morning briefing failed", error=str(e))
            await log_action("morning_briefing", "trigger", status="failed", error=str(e))

    async def _evening_review(self) -> None:
        """Handle evening review trigger."""
        logger.info("Triggering evening review")
        from cognitex.agent.action_log import log_action
        try:
            review = await self.agent.evening_review()
            await self._send_notification(review, urgency="low")
            await log_action("evening_review", "trigger", summary=review[:200] if review else "Generated evening review")
        except Exception as e:
            logger.error("Evening review failed", error=str(e))
            await log_action("evening_review", "trigger", status="failed", error=str(e))

    async def _hourly_check(self) -> None:
        """Handle hourly monitoring check."""
        logger.debug("Triggering hourly check")
        from cognitex.agent.action_log import log_action
        try:
            # Use ReAct agent to check for urgent items
            response = await self.agent.chat(
                "Quick check: are there any urgent tasks overdue or high-priority emails "
                "that need immediate attention? Only notify me if something is truly urgent."
            )
            # Only send notification if the agent found something urgent
            has_urgent = response and "no urgent" not in response.lower() and "nothing urgent" not in response.lower()
            if has_urgent:
                await self._send_notification(response, urgency="normal")
            await log_action("hourly_check", "trigger",
                           summary=f"{'Found urgent items' if has_urgent else 'No urgent items'}: {response[:100] if response else ''}")
        except Exception as e:
            logger.error("Hourly check failed", error=str(e))
            await log_action("hourly_check", "trigger", status="failed", error=str(e))

    async def _check_overdue_tasks(self) -> None:
        """Check for overdue tasks and escalate if needed."""
        logger.info("Checking for overdue tasks")
        from cognitex.agent.action_log import log_action
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
                    await log_action("overdue_check", "trigger",
                                   summary=f"Found {len(overdue_tasks)} overdue tasks",
                                   details={"task_count": len(overdue_tasks), "tasks": task_titles})
                else:
                    await log_action("overdue_check", "trigger", summary="No overdue tasks")
            else:
                await log_action("overdue_check", "trigger", summary="No overdue tasks")

        except Exception as e:
            logger.error("Overdue check failed", error=str(e))
            await log_action("overdue_check", "trigger", status="failed", error=str(e))

    async def _github_sync(self) -> None:
        """Daily sync of configured GitHub repositories."""
        from cognitex.config import get_settings
        from cognitex.agent.action_log import log_action

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

            await log_action("github_sync", "trigger",
                           summary=f"Synced {len(synced)} repos, {len(failed)} failed",
                           details={"synced": synced, "failed": failed})

        except Exception as e:
            logger.error("GitHub sync failed", error=str(e))
            await log_action("github_sync", "trigger", status="failed", error=str(e))

    async def _run_policy_update(self) -> None:
        """Run daily learning policy update cycle.

        Validates preference rules, extracts patterns from feedback,
        and updates learned patterns for proposal filtering.
        """
        logger.info("Running learning policy update")
        from cognitex.agent.action_log import log_action

        try:
            from cognitex.agent.learning import init_learning_system, get_learning_system

            # Initialize learning system if needed
            await init_learning_system()
            ls = get_learning_system()

            # Run the policy update cycle
            results = await ls.run_policy_update()

            logger.info(
                "Policy update completed",
                rules_validated=results.get("rules_validated", 0),
                patterns_extracted=results.get("patterns_extracted", 0),
                rules_deprecated=results.get("rules_deprecated", 0),
            )

            await log_action(
                "policy_update",
                "trigger",
                summary=f"Policy update: {results.get('rules_validated', 0)} rules validated, "
                        f"{results.get('patterns_extracted', 0)} patterns extracted",
                details=results
            )

            # Optionally notify if significant changes occurred
            deprecated = results.get("rules_deprecated", 0)
            if deprecated > 0:
                await self._send_notification(
                    f"**Learning Update**\n\n"
                    f"Policy update completed:\n"
                    f"- {results.get('rules_validated', 0)} rules validated\n"
                    f"- {deprecated} rules deprecated (low performance)\n"
                    f"- {results.get('patterns_extracted', 0)} new patterns learned",
                    urgency="low"
                )

        except Exception as e:
            logger.error("Policy update failed", error=str(e))
            await log_action(
                "policy_update",
                "trigger",
                status="failed",
                error=str(e)
            )

    async def _run_consolidation(self) -> None:
        """Run nightly memory consolidation ("Dreaming").

        Consolidates yesterday's memories into a daily summary,
        extracts behavioral patterns, and archives old logs.
        """
        logger.info("Starting nightly memory consolidation")
        from cognitex.agent.action_log import log_action

        try:
            from cognitex.agent.consolidation import MemoryConsolidator

            consolidator = MemoryConsolidator()

            # Consolidate yesterday
            result = await consolidator.run_nightly_consolidation()

            # Prune old logs (keep 30 days)
            prune_result = await consolidator.archive_old_memories(older_than_days=30)

            logger.info(
                "Memory consolidation completed",
                summary_id=result.get("summary_id"),
                event_count=result.get("event_count", 0),
                archived_count=prune_result.get("archived_count", 0),
            )

            await log_action(
                "consolidation",
                "trigger",
                summary=f"Memory consolidation: {result.get('event_count', 0)} events summarized, "
                        f"{prune_result.get('archived_count', 0)} old logs archived",
                details={"consolidation": result, "pruning": prune_result}
            )

            # Daily forgetting: remove trivial entries from yesterday's log
            try:
                from cognitex.services.memory_files import get_memory_file_service

                memory_svc = get_memory_file_service()
                forget_result = await memory_svc.apply_daily_forgetting()
                if forget_result["removed"] > 0:
                    logger.info(
                        "Daily forgetting applied",
                        removed=forget_result["removed"],
                    )
            except Exception as fe:
                logger.warning("Daily forgetting failed", error=str(fe))

        except Exception as e:
            logger.error("Memory consolidation failed", error=str(e))
            await log_action(
                "consolidation",
                "trigger",
                status="failed",
                summary=f"Consolidation failed: {str(e)}"
            )

    async def _run_weekly_distillation(self) -> None:
        """Run weekly memory distillation.

        Reviews last week's daily logs and proposes MEMORY.md updates
        as an inbox item for operator approval.
        """
        logger.info("Starting weekly memory distillation")
        from cognitex.agent.action_log import log_action

        try:
            from cognitex.agent.consolidation import MemoryConsolidator

            consolidator = MemoryConsolidator()
            result = await consolidator.run_weekly_distillation()

            updates_count = len(result.get("proposed_updates", []))
            logger.info(
                "Weekly distillation completed",
                updates=updates_count,
                discarded=result.get("discarded_count", 0),
            )

            await log_action(
                "weekly_distillation",
                "trigger",
                summary=f"Weekly distillation: {updates_count} updates proposed, "
                        f"{result.get('discarded_count', 0)} observations discarded",
                details=result,
            )

        except Exception as e:
            logger.error("Weekly distillation failed", error=str(e))
            await log_action(
                "weekly_distillation",
                "trigger",
                status="failed",
                summary=f"Weekly distillation failed: {str(e)}",
            )

    async def _run_monthly_archive(self) -> None:
        """Archive daily log files older than 30 days.

        Moves old .md files to ~/.cognitex/memory/archive/.
        """
        logger.info("Starting monthly log archive")
        from cognitex.agent.action_log import log_action

        try:
            from cognitex.services.memory_files import get_memory_file_service

            memory_svc = get_memory_file_service()
            result = await memory_svc.archive_old_daily_logs(older_than_days=30)

            logger.info(
                "Monthly archive completed",
                archived=result["archived_count"],
            )

            await log_action(
                "monthly_archive",
                "trigger",
                summary=f"Monthly archive: {result['archived_count']} log files archived",
                details=result,
            )

        except Exception as e:
            logger.error("Monthly archive failed", error=str(e))
            await log_action(
                "monthly_archive",
                "trigger",
                status="failed",
                summary=f"Monthly archive failed: {str(e)}",
            )

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
            from cognitex.agent.action_log import log_action
            if indexed_files:
                logger.info(
                    "Drive poll complete",
                    changes=len(relevant_changes),
                    indexed=len(indexed_files),
                )
                await log_action("drive_poll", "trigger",
                               summary=f"Indexed {len(indexed_files)} files from {len(relevant_changes)} changes",
                               details={"indexed": [f['name'] for f in indexed_files[:10]], "change_count": len(relevant_changes)})

                # Notify about significant changes (3+ files or important docs)
                if len(indexed_files) >= 3:
                    file_list = ", ".join(f['name'] for f in indexed_files[:5])
                    await self._send_notification(
                        f"**Drive Sync**\n\n"
                        f"Indexed {len(indexed_files)} updated files: {file_list}"
                        + ("..." if len(indexed_files) > 5 else ""),
                        urgency="low"
                    )
            elif relevant_changes:
                await log_action("drive_poll", "trigger",
                               summary=f"Found {len(relevant_changes)} changes, no files indexed")
            # else: no changes - don't log (too noisy every 15 min)

        except Exception as e:
            logger.error("Drive poll failed", error=str(e))
            try:
                from cognitex.agent.action_log import log_action
                await log_action("drive_poll", "trigger", status="failed", error=str(e))
            except Exception:
                pass

    async def _semantic_analysis_sweep(self) -> None:
        """
        Periodic sweep to run semantic analysis on documents that haven't been analyzed.

        Finds priority folder documents that have been indexed (have chunks) but
        don't have semantic analysis (no summary in Neo4j), and runs Gemini
        analysis to extract summary, topics, and concepts.
        """
        logger.debug("Starting semantic analysis sweep")

        try:
            from cognitex.db.postgres import init_postgres, close_postgres, get_session
            from cognitex.db.neo4j import init_neo4j, close_neo4j, get_neo4j_session
            from cognitex.services.semantic_analysis import SemanticAnalyzer
            from cognitex.services.drive import get_drive_service
            from cognitex.agent.action_log import log_action
            from sqlalchemy import text

            await init_postgres()
            await init_neo4j()

            try:
                # Find priority files that are indexed but not semantically analyzed
                # These are files in drive_files with is_priority=true that have
                # document_chunks but no entry in document_analysis
                async for pg_session in get_session():
                    query = text("""
                        SELECT DISTINCT df.id, df.name, df.mime_type, df.modified_time
                        FROM drive_files df
                        JOIN document_chunks dc ON df.id = dc.drive_id
                        LEFT JOIN document_analysis da ON df.id = da.file_id
                        WHERE df.is_priority = true
                          AND da.file_id IS NULL
                          AND df.mime_type IN (
                              'application/vnd.google-apps.document',
                              'text/plain',
                              'text/markdown',
                              'application/pdf',
                              'text/csv'
                          )
                        ORDER BY df.modified_time DESC
                        LIMIT 10
                    """)
                    result = await pg_session.execute(query)
                    files_to_analyze = result.fetchall()
                    break

                if not files_to_analyze:
                    logger.debug("No files need semantic analysis")
                    return

                logger.info("Found files needing semantic analysis", count=len(files_to_analyze))

                drive = get_drive_service()
                analyzer = SemanticAnalyzer()
                analyzed_count = 0

                for file_row in files_to_analyze:
                    file_id, file_name, mime_type = file_row

                    try:
                        # Get file content
                        content = drive.get_file_content(file_id, mime_type)
                        if not content or len(content.strip()) < 100:
                            logger.debug("Skipping file with no content", file=file_name)
                            continue

                        # Run semantic analysis
                        logger.info("Running semantic analysis", file=file_name)
                        analysis = await analyzer.analyze_document(file_id, content)

                        if analysis:
                            analyzed_count += 1
                            logger.info(
                                "Semantic analysis complete",
                                file=file_name,
                                has_summary=bool(analysis.summary),
                            )

                    except Exception as e:
                        logger.warning("Semantic analysis failed for file", file=file_name, error=str(e))

                if analyzed_count > 0:
                    await log_action(
                        "semantic_sweep",
                        "trigger",
                        summary=f"Analyzed {analyzed_count} documents",
                    )
                    logger.info("Semantic analysis sweep complete", analyzed=analyzed_count)

            finally:
                await close_postgres()
                await close_neo4j()

        except Exception as e:
            logger.error("Semantic analysis sweep failed", error=str(e))
            try:
                from cognitex.agent.action_log import log_action
                await log_action("semantic_sweep", "trigger", status="failed", error=str(e))
            except Exception:
                pass

    async def _coding_sessions_sync(self) -> None:
        """
        Sync coding CLI sessions (Claude Code, etc.) to capture development context.

        Ingests session conversations to extract:
        - Project summaries and progress
        - Technical decisions made
        - Files changed
        - Next steps planned
        """
        logger.debug("Starting coding sessions sync")

        try:
            from cognitex.services.coding_sessions import get_session_ingester
            from cognitex.agent.action_log import log_action

            ingester = get_session_ingester()

            # Sync all Claude Code sessions
            stats = await ingester.sync_all_sessions(cli_type="claude")

            if stats["ingested"] > 0:
                logger.info(
                    "Coding sessions synced",
                    ingested=stats["ingested"],
                    discovered=stats["discovered"],
                )
                await log_action(
                    "coding_sessions_sync",
                    "trigger",
                    summary=f"Synced {stats['ingested']} coding sessions",
                    details=stats,
                )

                # Notify if significant sessions ingested
                if stats["ingested"] >= 2:
                    await self._send_notification(
                        f"**Coding Sessions Synced**\n\n"
                        f"Ingested {stats['ingested']} new coding sessions with "
                        f"development context.",
                        urgency="low"
                    )
            else:
                logger.debug("No new coding sessions to sync")

        except Exception as e:
            logger.error("Coding sessions sync failed", error=str(e))
            try:
                from cognitex.agent.action_log import log_action
                await log_action("coding_sessions_sync", "trigger", status="failed", error=str(e))
            except Exception:
                pass

    async def _check_stuck_tasks(self) -> None:
        """Check for tasks where user has been working > 150% of estimated time.

        Proactively asks if user wants to break down the task or is in flow.
        """
        logger.debug("Checking for stuck tasks")
        from cognitex.agent.action_log import log_action

        try:
            from cognitex.db.neo4j import get_neo4j_session

            async for session in get_neo4j_session():
                # Find in-progress tasks where elapsed time > 150% of estimate
                query = """
                MATCH (t:Task)
                WHERE t.status = 'in_progress'
                  AND t.started_at IS NOT NULL
                  AND t.estimated_minutes IS NOT NULL
                  AND t.estimated_minutes > 0
                WITH t,
                     duration.inMinutes(t.started_at, datetime()).minutes as elapsed_mins,
                     t.estimated_minutes * 1.5 as threshold_mins
                WHERE elapsed_mins > threshold_mins
                RETURN t.id as id, t.title as title,
                       t.estimated_minutes as estimated,
                       elapsed_mins as elapsed,
                       t.project_id as project_id
                LIMIT 3
                """
                result = await session.run(query)
                stuck_tasks = await result.data()

                if not stuck_tasks:
                    logger.debug("No stuck tasks found")
                    return

                for task in stuck_tasks:
                    title = task.get("title", "Unknown")
                    estimated = task.get("estimated", 0)
                    elapsed = task.get("elapsed", 0)
                    overtime_pct = int((elapsed / estimated - 1) * 100) if estimated > 0 else 0

                    logger.info(
                        "Stuck task detected",
                        task=title[:30],
                        estimated=estimated,
                        elapsed=elapsed,
                        overtime_pct=overtime_pct,
                    )

                    # Send a gentle nudge
                    message = (
                        f"**⏱️ Taking longer than expected**\n\n"
                        f"**Task:** {title}\n"
                        f"**Elapsed:** {elapsed} mins (estimated: {estimated} mins, +{overtime_pct}%)\n\n"
                        f"Options:\n"
                        f"• Break this into smaller tasks?\n"
                        f"• Adjust the estimate?\n"
                        f"• Or are you in flow? (I'll stop asking)\n\n"
                        f"_Reply with your preference_"
                    )

                    await self._send_notification(message, urgency="normal")

                    await log_action(
                        "stuck_task_detected",
                        "trigger",
                        summary=f"Task '{title[:30]}' at {overtime_pct}% over estimate",
                        details={
                            "task_id": task.get("id"),
                            "estimated": estimated,
                            "elapsed": elapsed,
                        }
                    )

        except Exception as e:
            logger.error("Stuck task check failed", error=str(e))

    async def _check_mode_transition(self) -> None:
        """Check for mode transitions after meetings.

        Handles:
        - CLINICAL sessions: Triggers rest-of-day OVERLOADED mode
        - Heavy meetings: Suggests low-energy recovery tasks
        """
        logger.debug("Checking for mode transitions")
        from cognitex.agent.action_log import log_action
        from cognitex.db.redis import get_redis

        try:
            from cognitex.agent.state_model import get_state_estimator, OperatingMode
            from cognitex.services.calendar import CalendarService, classify_event_drain, EventDrainLevel
            from datetime import datetime, timedelta

            state_estimator = get_state_estimator()
            redis = get_redis()

            # Check if we just finished a meeting (within last 15 minutes)
            now = datetime.now()
            cal = CalendarService()

            # Get events from last hour
            result = cal.list_events(
                time_min=now - timedelta(hours=1),
                time_max=now,
            )
            recent_events = result.get("items", [])

            # Find events that ended in the last 15 minutes
            for event in recent_events:
                end_raw = event.get("end", {})
                if isinstance(end_raw, dict):
                    end_str = end_raw.get("dateTime") or end_raw.get("date")
                else:
                    end_str = end_raw

                if not end_str:
                    continue

                try:
                    end_str = str(end_str).replace("Z", "").replace("+00:00", "")
                    event_end = datetime.fromisoformat(end_str)
                except (ValueError, TypeError):
                    continue

                # Check if event ended in last 15 minutes
                time_since_end = now - event_end
                if timedelta(0) < time_since_end <= timedelta(minutes=15):
                    summary = event.get("summary", "Meeting")
                    event_id = event.get("id", "unknown")

                    # Classify the event drain level
                    drain_level = classify_event_drain(event)

                    # Check if we already processed this event
                    nudge_key = f"cognitex:transition_nudge:{event_id}"
                    already_nudged = await redis.get(nudge_key)
                    if already_nudged:
                        continue

                    # Handle CLINICAL sessions - REST OF DAY recovery
                    if drain_level == EventDrainLevel.CLINICAL:
                        # Mark as processed (expires at midnight)
                        seconds_until_midnight = (
                            (now.replace(hour=23, minute=59, second=59) - now).seconds + 1
                        )
                        await redis.set(nudge_key, "1", ex=seconds_until_midnight)

                        # Set OVERLOADED mode for rest of day
                        await state_estimator.update_state(
                            mode=OperatingMode.OVERLOADED,
                            fatigue_level=0.95,  # High fatigue after clinical session
                            notes=f"Post-clinical recovery (rest of day): {summary}"
                        )

                        # Store clinical recovery expiry in Redis
                        recovery_until = now.replace(hour=23, minute=59, second=59).isoformat()
                        await redis.set(
                            "cognitex:clinical_recovery_until",
                            recovery_until,
                            ex=seconds_until_midnight
                        )

                        logger.info(
                            "Clinical session ended - entering rest-of-day recovery",
                            event=summary[:30],
                            recovery_until=recovery_until,
                        )

                        # Notify user
                        await self._send_notification(
                            f"**Clinical session ended**\n\n"
                            f"*{summary}*\n\n"
                            f"Switching to recovery mode for the rest of the day. "
                            f"Only low-energy tasks will be suggested.\n\n"
                            f"_Take care of yourself._",
                            urgency="low"
                        )

                        await log_action(
                            "clinical_recovery",
                            "trigger",
                            summary=f"Rest-of-day recovery after clinical: '{summary[:30]}'",
                            details={
                                "event_id": event_id,
                                "event_summary": summary,
                                "recovery_until": recovery_until,
                            }
                        )
                        continue

                    # Handle HIGH drain meetings - suggest recovery
                    if drain_level == EventDrainLevel.HIGH or self._is_heavy_meeting(event):
                        # Mark as nudged (expires in 1 hour)
                        await redis.set(nudge_key, "1", ex=3600)

                        logger.info(
                            "Heavy meeting ended, sending transition nudge",
                            event=summary[:30],
                            drain_level=drain_level.value,
                        )

                        # Suggest low-energy tasks
                        low_energy_tasks = await self._get_low_energy_tasks()

                        if low_energy_tasks:
                            task_list = "\n".join([f"• {t}" for t in low_energy_tasks[:3]])
                            message = (
                                f"**🧘 Recovery Time**\n\n"
                                f"Heavy meeting finished: *{summary}*\n\n"
                                f"I've queued some low-energy tasks for the next 30 mins:\n"
                                f"{task_list}\n\n"
                                f"_Take a moment to decompress before diving back in_"
                            )
                        else:
                            message = (
                                f"**🧘 Recovery Time**\n\n"
                                f"Heavy meeting finished: *{summary}*\n\n"
                                f"Consider taking 5-10 mins for:\n"
                                f"• Quick walk or stretch\n"
                                f"• Process notes from the meeting\n"
                                f"• Light admin tasks\n\n"
                                f"_Give yourself time to decompress_"
                            )

                        await self._send_notification(message, urgency="low")

                        await log_action(
                            "transition_nudge",
                            "trigger",
                            summary=f"Recovery suggestion after '{summary[:30]}'",
                            details={
                                "event_id": event_id,
                                "event_summary": summary,
                                "drain_level": drain_level.value,
                            }
                        )

        except Exception as e:
            logger.error("Mode transition check failed", error=str(e))

    def _is_heavy_meeting(self, event: dict) -> bool:
        """Determine if a meeting is 'heavy' (high cognitive load).

        Heavy meetings:
        - 60+ minutes duration
        - Board/executive meetings
        - Presentations
        - Interviews
        - All-hands / town halls
        - Meetings with 5+ attendees
        """
        summary = event.get("summary", "").lower()

        # Check for heavy meeting keywords
        heavy_keywords = [
            "board", "executive", "presentation", "interview",
            "all-hands", "town hall", "strategy", "review",
            "performance", "1-on-1", "1:1", "sync",
        ]

        if any(kw in summary for kw in heavy_keywords):
            return True

        # Check duration (60+ mins is heavy)
        start_raw = event.get("start", {})
        end_raw = event.get("end", {})

        if isinstance(start_raw, dict):
            start_str = start_raw.get("dateTime")
        else:
            start_str = start_raw

        if isinstance(end_raw, dict):
            end_str = end_raw.get("dateTime")
        else:
            end_str = end_raw

        if start_str and end_str:
            try:
                start = datetime.fromisoformat(str(start_str).replace("Z", "").replace("+00:00", ""))
                end = datetime.fromisoformat(str(end_str).replace("Z", "").replace("+00:00", ""))
                duration_mins = (end - start).total_seconds() / 60
                if duration_mins >= 60:
                    return True
            except (ValueError, TypeError):
                pass

        # Check attendee count (5+ is heavy)
        attendees = event.get("attendees", [])
        if len(attendees) >= 5:
            return True

        return False

    async def _get_low_energy_tasks(self) -> list[str]:
        """Get a list of low-energy tasks suitable for recovery time."""
        try:
            from cognitex.db.neo4j import get_neo4j_session

            async for session in get_neo4j_session():
                # Find simple/quick tasks marked as low energy or quick wins
                query = """
                MATCH (t:Task)
                WHERE t.status = 'pending'
                  AND (
                    t.priority = 'low'
                    OR t.estimated_minutes <= 15
                    OR t.energy_level = 'low'
                    OR t.title =~ '(?i).*(review|check|update|organize|clean).*'
                  )
                RETURN t.title as title
                LIMIT 5
                """
                result = await session.run(query)
                tasks = await result.data()

                return [t.get("title") for t in tasks if t.get("title")]

        except Exception as e:
            logger.warning("Failed to get low energy tasks", error=str(e))
            return []

    async def _hourly_state_update(self) -> None:
        """Re-evaluate operating state based on diurnal energy and context.

        Updates state automatically as energy naturally shifts through the day:
        - Morning: Favor DEEP_FOCUS if calendar is clear
        - Afternoon: Transition toward FRAGMENTED as energy dips
        - Evening: Low-energy modes only

        Respects explicit overrides (clinical recovery, manual state changes).
        """
        logger.info("Running hourly state update")
        from cognitex.agent.action_log import log_action

        try:
            from cognitex.agent.state_model import get_state_estimator, get_temporal_model
            from cognitex.services.calendar import CalendarService
            from cognitex.db.redis import get_redis
            from datetime import timedelta

            redis = get_redis()

            # Skip if in clinical recovery mode (rest-of-day override)
            clinical_recovery = await redis.get("cognitex:clinical_recovery_until")
            if clinical_recovery:
                logger.debug("Skipping state update - in clinical recovery mode")
                return

            # Get current calendar events for context
            cal = CalendarService()
            now = datetime.now()
            events_result = cal.list_events(
                time_min=now,
                time_max=now + timedelta(hours=4),
            )
            calendar_events = events_result.get("items", [])

            # Get current state before update
            state_estimator = get_state_estimator()
            old_state = await state_estimator.get_current_state()
            old_mode = old_state.mode

            # Infer new state based on diurnal curve + calendar
            new_state = await state_estimator.infer_state(
                calendar_events=calendar_events,
            )

            # Only record if mode changed
            if new_state.mode != old_mode:
                await state_estimator.record_state(new_state)

                temporal_model = get_temporal_model()
                current_energy = temporal_model.get_expected_energy(now.hour)

                logger.info(
                    "State auto-transitioned",
                    from_mode=old_mode.value,
                    to_mode=new_state.mode.value,
                    hour=now.hour,
                    energy=current_energy,
                )

                await log_action(
                    "state_transition",
                    "trigger",
                    summary=f"Auto-transition: {old_mode.value} → {new_state.mode.value} (energy: {current_energy:.0%})",
                    details={
                        "from_mode": old_mode.value,
                        "to_mode": new_state.mode.value,
                        "hour": now.hour,
                        "energy_level": current_energy,
                        "upcoming_events": len(calendar_events),
                    }
                )
            else:
                logger.debug(
                    "State unchanged after hourly check",
                    mode=new_state.mode.value,
                )

            # Sync Drive files to Neo4j (maintenance task)
            try:
                from cognitex.db.postgres import get_session
                from cognitex.services.linking import sync_drive_to_neo4j

                async for pg_session in get_session():
                    sync_stats = await sync_drive_to_neo4j(pg_session, limit=50)
                    if sync_stats.get("created", 0) > 0:
                        logger.info(
                            "Graph sync created new Document nodes",
                            created=sync_stats["created"],
                        )
            except Exception as sync_error:
                logger.warning("Graph sync failed", error=str(sync_error))

        except Exception as e:
            logger.error("Hourly state update failed", error=str(e))

    # =========================================================================
    # Event trigger handlers
    # =========================================================================

    async def _on_new_email(self, email_data: dict) -> None:
        """Handle new email event with deep semantic analysis.

        Pipeline:
        1. Sync new emails from Gmail
        2. For each actionable email, classify intent deeply
        3. If review_request with attachments -> analyze documents, create inbox item
        4. If quick_reply -> use existing agent chat workflow
        5. If archive/fyi -> silently process, no notification
        """
        history_id = email_data.get("history_id")
        email_address = email_data.get("email_address")
        logger.info("Processing Gmail push notification", history_id=history_id, email_address=email_address)
        from cognitex.agent.action_log import log_action

        try:
            from cognitex.db.redis import get_redis
            from cognitex.services.ingestion import run_incremental_sync

            # Deduplicate: Skip if we've already processed this history_id recently
            if history_id:
                redis = get_redis()
                dedup_key = f"cognitex:email:processed:{history_id}"
                was_set = await redis.set(dedup_key, "1", ex=300, nx=True)
                if not was_set:
                    logger.debug("Skipping duplicate email notification", history_id=history_id)
                    return

            if history_id:
                logger.info("Syncing emails from history", history_id=history_id)
                sync_result = await run_incremental_sync(history_id)

                if sync_result.get("first_sync"):
                    logger.info("First sync - history ID stored, waiting for next email")
                    return

                if sync_result.get("error"):
                    logger.warning("Incremental sync failed", error=sync_result.get("error"))
                    return

                if sync_result.get("total", 0) == 0:
                    logger.info("No new emails found in history")
                    return

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

                # Filter to actionable emails
                emails = sync_result.get("emails", [])
                actionable_emails = self._filter_actionable_emails(emails)

                if not actionable_emails:
                    logger.info("No actionable emails in batch", total=len(emails))
                    return

                logger.info("Found actionable emails", actionable=len(actionable_emails), total=len(emails))

                # Process each actionable email with deep semantic analysis
                await self._process_emails_with_deep_analysis(actionable_emails)

            else:
                logger.debug("Email notification without history ID, skipping")
                return

            # Trigger context pack refresh for related events
            try:
                from cognitex.agent.context_pack import get_context_pack_triggers
                pack_triggers = get_context_pack_triggers()
                for email in emails[:3]:
                    await pack_triggers.on_email_received({
                        "sender": email.get("sender_email", ""),
                        "subject": email.get("subject", ""),
                    })
            except Exception as pack_err:
                logger.warning("Context pack refresh failed", error=str(pack_err))

        except Exception as e:
            logger.error("Email processing failed", error=str(e))

    async def _process_emails_with_deep_analysis(self, emails: list[dict]) -> None:
        """Process emails with deep semantic analysis using intent classification.

        For each email:
        1. Classify intent (review_request, question, action_request, fyi, etc.)
        2. If requires document analysis, download and analyze attachments
        3. Create appropriate inbox item based on workflow type
        """
        from cognitex.agent.action_log import log_action
        from cognitex.services.email_intent import (
            get_email_intent_classifier,
            EmailIntent,
            SuggestedWorkflow,
        )
        from cognitex.services.gmail import GmailService, extract_email_body
        from cognitex.services.document_analyzer import get_document_analyzer

        intent_classifier = get_email_intent_classifier()
        gmail = GmailService()
        doc_analyzer = get_document_analyzer()

        for email in emails[:5]:  # Process up to 5 emails
            try:
                gmail_id = email.get("gmail_id")
                sender = email.get("sender_email", "unknown")
                sender_name = email.get("sender_name", "")
                subject = email.get("subject", "(no subject)")
                snippet = email.get("snippet", "")

                # Get full email body for better classification
                if gmail_id:
                    try:
                        full_message = gmail.get_message(gmail_id, format="full")
                        body = extract_email_body(full_message)
                    except Exception:
                        body = snippet
                else:
                    body = snippet

                # Get attachment metadata without downloading yet
                attachment_metadata = []
                if gmail_id:
                    try:
                        attachment_metadata = gmail.get_attachment_metadata(gmail_id)
                    except Exception as e:
                        logger.warning("Failed to get attachment metadata", error=str(e))

                # Step 1: Deep intent classification
                logger.info(
                    "Classifying email intent",
                    subject=subject[:50],
                    has_attachments=len(attachment_metadata) > 0,
                )

                intent_result = await intent_classifier.classify(
                    sender=f"{sender_name} <{sender}>" if sender_name else sender,
                    subject=subject,
                    body=body,
                    attachments=attachment_metadata,
                )

                logger.info(
                    "Email intent classified",
                    intent=intent_result.intent.value,
                    workflow=intent_result.suggested_workflow.value,
                    requires_doc_analysis=intent_result.requires_document_analysis,
                    confidence=intent_result.confidence,
                    triage_decision=intent_result.triage_decision.value,
                )

                # WP4: Persist triage result on Email node (non-critical)
                if gmail_id:
                    await self._store_triage_result(gmail_id, intent_result)

                # Step 2: Handle based on suggested workflow
                if intent_result.suggested_workflow == SuggestedWorkflow.ARCHIVE:
                    # FYI only - silently archive, no action needed
                    logger.info("Email is FYI only, archiving silently", subject=subject[:50])
                    await log_action(
                        "email_classified",
                        "email",
                        summary=f"FYI email archived: {subject[:50]}",
                        details={"intent": intent_result.intent.value, "gmail_id": gmail_id},
                    )
                    continue

                elif intent_result.suggested_workflow == SuggestedWorkflow.ANALYZE_THEN_RESPOND:
                    # Need to analyze attachments before responding
                    await self._handle_review_request_email(
                        email=email,
                        gmail_id=gmail_id,
                        intent_result=intent_result,
                        gmail=gmail,
                        doc_analyzer=doc_analyzer,
                    )

                elif intent_result.suggested_workflow == SuggestedWorkflow.CREATE_TASK:
                    # Create a task and possibly acknowledge
                    await self._handle_task_creation_email(
                        email=email,
                        intent_result=intent_result,
                    )

                elif intent_result.suggested_workflow == SuggestedWorkflow.SCHEDULE:
                    # Calendar-related - use agent to handle
                    await self._handle_scheduling_email(
                        email=email,
                        intent_result=intent_result,
                    )

                else:
                    # QUICK_REPLY or fallback - use existing agent workflow
                    await self._handle_quick_reply_email(
                        email=email,
                        intent_result=intent_result,
                    )

            except Exception as e:
                logger.error(
                    "Failed to process email with deep analysis",
                    subject=email.get("subject", "")[:50],
                    error=str(e),
                )

    async def _store_triage_result(self, gmail_id: str, intent_result) -> None:
        """Persist WP4 triage fields on the Email node in Neo4j.

        Non-critical — failures are logged but do not affect routing.
        """
        try:
            from cognitex.db.graph_schema import update_email_triage
            from cognitex.db.neo4j import get_neo4j_session

            async for session in get_neo4j_session():
                await update_email_triage(
                    session,
                    gmail_id=gmail_id,
                    triage_decision=intent_result.triage_decision.value,
                    action_verb=intent_result.action_verb or None,
                    delegation_candidate=intent_result.delegation_candidate,
                    factual_summary=intent_result.factual_summary or None,
                    factual_urgency=intent_result.factual_urgency,
                    deadline=intent_result.deadline,
                    deadline_source=intent_result.deadline_source or None,
                    project_context=intent_result.project_context,
                    confidence=intent_result.confidence,
                    clinical_flag=intent_result.clinical_flag,
                    emotional_markers=intent_result.emotional_markers or [],
                    intent=intent_result.intent.value,
                )
                break
        except Exception as e:
            logger.warning(
                "Failed to store triage result",
                gmail_id=gmail_id,
                error=str(e),
            )

    async def _handle_review_request_email(
        self,
        email: dict,
        gmail_id: str,
        intent_result,
        gmail,
        doc_analyzer,
    ) -> None:
        """Handle emails that require document analysis before responding.

        Downloads attachments, analyzes them for changes/highlights/comments,
        and creates a rich inbox item with decision options.
        """
        from cognitex.agent.action_log import log_action
        from cognitex.db.postgres import get_session
        from cognitex.db.models import InboxItem

        sender = email.get("sender_email", "unknown")
        sender_name = email.get("sender_name", "")
        subject = email.get("subject", "(no subject)")
        thread_id = email.get("thread_id")

        logger.info(
            "Processing review request email with attachments",
            subject=subject[:50],
            gmail_id=gmail_id,
        )

        # Download and analyze attachments
        analysis_results = []
        try:
            attachments = gmail.get_email_attachments(gmail_id)
            logger.info("Downloaded attachments", count=len(attachments))

            for att in attachments:
                filename = att.get("filename", "unknown")
                mime_type = att.get("mime_type", "")
                content = att.get("data")

                if not content:
                    continue

                # Check if document type is supported
                if doc_analyzer.is_supported(filename, mime_type):
                    logger.info("Analyzing document", filename=filename)
                    try:
                        analysis = await doc_analyzer.analyze_for_review(
                            filename=filename,
                            content=content,
                            context=f"Email subject: {subject}. Key ask: {intent_result.key_ask}",
                            mime_type=mime_type,
                        )
                        analysis_results.append(analysis.to_dict())
                        logger.info(
                            "Document analyzed",
                            filename=filename,
                            changes=len(analysis.changes),
                            review_items=len(analysis.review_items),
                        )
                    except Exception as e:
                        logger.warning("Document analysis failed", filename=filename, error=str(e))
                else:
                    logger.debug("Unsupported document type, skipping", filename=filename, mime_type=mime_type)

        except Exception as e:
            logger.error("Failed to download/analyze attachments", error=str(e))

        # Generate decision options based on intent
        decision_options = self._generate_decision_options(intent_result, analysis_results)

        # Create rich inbox item
        inbox_payload = {
            "email_id": gmail_id,
            "thread_id": thread_id,
            "from": sender,
            "from_name": sender_name,
            "subject": subject,
            "intent": intent_result.intent.value,
            "intent_confidence": intent_result.confidence,
            "key_ask": intent_result.key_ask,
            "deadline": intent_result.deadline,
            "response_requirements": intent_result.response_requirements,
            "document_analysis": analysis_results,
            "decision_options": decision_options,
            "would_acknowledgment_be_unhelpful": intent_result.would_acknowledgment_be_unhelpful,
        }

        async for session in get_session():
            inbox_item = InboxItem(
                item_type="email_review",
                title=f"Review: {subject[:80]}",
                summary=intent_result.key_ask or f"Document review request from {sender_name or sender}",
                payload=inbox_payload,
                priority="high" if intent_result.deadline else "normal",
                source="email",
                source_id=gmail_id,
            )
            session.add(inbox_item)
            await session.commit()
            await session.refresh(inbox_item)

            logger.info(
                "Created email_review inbox item",
                inbox_id=inbox_item.id,
                documents_analyzed=len(analysis_results),
            )

            # Send notification about the review request
            doc_summary = ""
            if analysis_results:
                total_changes = sum(len(a.get("changes", [])) for a in analysis_results)
                total_items = sum(len(a.get("review_items", [])) for a in analysis_results)
                doc_summary = f"\n\n**Document Analysis:**\n- {len(analysis_results)} document(s) analyzed\n- {total_changes} change(s) found\n- {total_items} item(s) flagged for review"

            await self._send_notification(
                f"**Document Review Request**\n\n"
                f"**From:** {sender_name or sender}\n"
                f"**Subject:** {subject}\n"
                f"**Ask:** {intent_result.key_ask}"
                f"{doc_summary}\n\n"
                f"_Check your inbox to review and decide on your response._",
                urgency="high" if intent_result.deadline else "normal",
            )

            await log_action(
                "email_review_created",
                "email",
                summary=f"Review request: {subject[:50]} ({len(analysis_results)} docs)",
                details={
                    "gmail_id": gmail_id,
                    "inbox_item_id": str(inbox_item.id),
                    "documents_analyzed": len(analysis_results),
                    "intent": intent_result.intent.value,
                },
            )
            break

    def _generate_decision_options(self, intent_result, analyses: list[dict]) -> list[dict]:
        """Generate decision options based on email intent and document analysis."""
        from cognitex.services.email_intent import EmailIntent

        options = []

        if intent_result.intent == EmailIntent.REVIEW_REQUEST:
            options = [
                {
                    "id": "approve",
                    "label": "Approve changes",
                    "description": "Confirm the changes look good",
                    "response_template": "Thanks for sending this over - the changes look good to me. [Add specific feedback if needed]",
                },
                {
                    "id": "revisions",
                    "label": "Request revisions",
                    "description": "Ask for specific changes",
                    "response_template": "Thanks for this. A few items need adjustment:\n\n- [Item 1]\n- [Item 2]\n\nLet me know if you have any questions.",
                },
                {
                    "id": "discuss",
                    "label": "Schedule discussion",
                    "description": "Set up a call to discuss",
                    "response_template": "Thanks for sharing this. I have some thoughts I'd like to discuss - can we find 15-30 minutes this week to chat?",
                },
                {
                    "id": "custom",
                    "label": "Custom response",
                    "description": "Write your own response",
                    "response_template": "",
                },
            ]
        elif intent_result.intent == EmailIntent.QUESTION:
            options = [
                {
                    "id": "answer",
                    "label": "Answer question",
                    "description": "Provide the requested information",
                    "response_template": "",
                },
                {
                    "id": "defer",
                    "label": "Need to check",
                    "description": "Let them know you'll follow up",
                    "response_template": "Good question - let me look into this and get back to you by [date].",
                },
                {
                    "id": "redirect",
                    "label": "Redirect to someone else",
                    "description": "Point them to the right person",
                    "response_template": "I think [Name] would be the best person to answer this - copying them here.",
                },
            ]
        elif intent_result.intent == EmailIntent.ACTION_REQUEST:
            options = [
                {
                    "id": "will_do",
                    "label": "Will do",
                    "description": "Confirm you'll handle it",
                    "response_template": "Got it - I'll take care of this and let you know when it's done.",
                },
                {
                    "id": "need_info",
                    "label": "Need more info",
                    "description": "Ask clarifying questions",
                    "response_template": "Happy to help with this. Before I get started, could you clarify:\n\n- [Question 1]\n- [Question 2]",
                },
                {
                    "id": "cant_do",
                    "label": "Can't do this",
                    "description": "Decline or redirect",
                    "response_template": "Thanks for thinking of me for this. Unfortunately, [reason]. Perhaps [alternative suggestion]?",
                },
            ]
        else:
            # Generic options
            options = [
                {
                    "id": "reply",
                    "label": "Reply",
                    "description": "Send a response",
                    "response_template": "",
                },
                {
                    "id": "archive",
                    "label": "Archive",
                    "description": "No response needed",
                    "response_template": None,
                },
            ]

        return options

    async def _handle_task_creation_email(self, email: dict, intent_result) -> None:
        """Handle emails that should result in task creation."""
        from cognitex.agent.action_log import log_action

        sender = email.get("sender_email", "unknown")
        subject = email.get("subject", "(no subject)")

        # Use agent to create task
        task_prompt = (
            f"An email from {sender} with subject '{subject}' needs a task created.\n"
            f"Key ask: {intent_result.key_ask}\n"
            f"Deadline: {intent_result.deadline or 'None specified'}\n\n"
            f"Please create an appropriate task for this. If it's urgent, set high priority."
        )

        response = await self.agent.chat(task_prompt)

        if response:
            await self._send_notification(
                f"**Task Created from Email**\n\n"
                f"**From:** {sender}\n"
                f"**Subject:** {subject}\n\n"
                f"{response}",
                urgency="normal",
            )

        await log_action(
            "email_task_created",
            "email",
            summary=f"Task from email: {subject[:50]}",
            details={"intent": intent_result.intent.value, "key_ask": intent_result.key_ask},
        )

    async def _handle_scheduling_email(self, email: dict, intent_result) -> None:
        """Handle calendar/scheduling related emails."""
        from cognitex.agent.action_log import log_action

        sender = email.get("sender_email", "unknown")
        subject = email.get("subject", "(no subject)")

        # Use agent to handle scheduling
        schedule_prompt = (
            f"An email from {sender} about scheduling: '{subject}'\n"
            f"Key ask: {intent_result.key_ask}\n\n"
            f"Please check my calendar and suggest available times or handle this scheduling request."
        )

        response = await self.agent.chat(schedule_prompt)

        if response:
            await self._send_notification(
                f"**Scheduling Request**\n\n"
                f"**From:** {sender}\n"
                f"**Subject:** {subject}\n\n"
                f"{response}",
                urgency="normal",
            )

        await log_action(
            "email_scheduling",
            "email",
            summary=f"Scheduling: {subject[:50]}",
            details={"intent": intent_result.intent.value},
        )

    async def _handle_quick_reply_email(self, email: dict, intent_result) -> None:
        """Handle emails that can be processed with a quick reply workflow."""
        from cognitex.agent.action_log import log_action

        sender = email.get("sender_email", "unknown")
        sender_name = email.get("sender_name", "")
        subject = email.get("subject", "(no subject)")
        snippet = email.get("snippet", "")

        # Skip notification if acknowledgment would be unhelpful
        if intent_result.would_acknowledgment_be_unhelpful:
            logger.info("Skipping quick reply - acknowledgment would be unhelpful", subject=subject[:50])
            return

        # Use existing agent workflow for simple emails
        response = await self.agent.chat(
            f"I received an email that may need a quick response:\n\n"
            f"**From:** {sender_name or sender}\n"
            f"**Subject:** {subject}\n"
            f"**Preview:** {snippet[:200]}\n"
            f"**Intent:** {intent_result.intent.value}\n"
            f"**Key Ask:** {intent_result.key_ask}\n\n"
            f"Please analyze: Does this need a reply? If so, what should I say? "
            f"If no action is needed, just say 'No action needed'."
        )

        if response:
            response_lower = response.lower()
            # Only notify if action is needed
            if not any(phrase in response_lower for phrase in [
                "no action needed", "no action required", "nothing urgent",
                "no immediate action", "fyi only",
            ]):
                await self._send_notification(
                    f"**Email Action Needed**\n\n{response}",
                    urgency="normal",
                )
                await log_action(
                    "email_quick_reply",
                    "email",
                    summary=f"Quick reply: {subject[:50]}",
                    details={"intent": intent_result.intent.value, "notified": True},
                )
            else:
                await log_action(
                    "email_quick_reply",
                    "email",
                    summary=f"No action needed: {subject[:50]}",
                    details={"intent": intent_result.intent.value, "notified": False},
                )

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
        task_id = task_data.get("task_id")
        project_id = task_data.get("project_id")
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

        elif event_type == "completed" and project_id:
            # Suggest next task from same project
            try:
                from cognitex.db.neo4j import get_neo4j_session

                async for session in get_neo4j_session():
                    # Get next pending task from same project
                    result = await session.run("""
                        MATCH (p:Project {id: $project_id})-[:CONTAINS]->(t:Task)
                        WHERE t.status IN ['pending', 'todo', 'not_started']
                          AND t.id <> $completed_task_id
                        RETURN t.id as id, t.title as title, t.priority as priority
                        ORDER BY
                            CASE t.priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
                            t.created_at
                        LIMIT 1
                    """, {"project_id": project_id, "completed_task_id": task_id or ""})

                    record = await result.single()
                    if record:
                        next_title = record["title"]
                        next_priority = record["priority"] or "medium"
                        logger.info(
                            "Next task available after completion",
                            completed=title[:30],
                            next_task=next_title[:30],
                            priority=next_priority
                        )
                    break

            except Exception as e:
                logger.debug("Failed to suggest next task", error=str(e))

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
        - Emails SENT BY the user (outgoing emails)
        - Auto-generated notifications
        - Marketing/newsletters
        - Calendar invites (handled separately)
        - FYI/informational emails
        - Receipts and confirmations
        - Out-of-office replies
        """
        actionable = []

        # Get user email for filtering sent emails
        user_email = self._get_user_email()

        for email in emails:
            sender = email.get("sender_email", "").lower()
            subject = email.get("subject", "").lower()
            snippet = email.get("snippet", "").lower()

            # Skip emails sent BY the user (outgoing emails)
            if user_email and sender == user_email:
                logger.debug("Filtered sent email (from user)", subject=subject[:30])
                continue

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

    def _get_user_email(self) -> str | None:
        """Get the authenticated user's email address (cached).

        Used to filter out sent emails from actionable email processing.
        """
        if self._user_email:
            return self._user_email

        try:
            from cognitex.services.gmail import GmailService
            gmail = GmailService()
            profile = gmail.get_profile()
            self._user_email = profile.get("emailAddress", "").lower()
            logger.debug("Cached user email for filtering", email=self._user_email)
            return self._user_email
        except Exception as e:
            logger.warning("Could not get user email for filtering", error=str(e))
            return None

    # =========================================================================
    # Utilities
    # =========================================================================

    async def _send_notification(self, message: str, urgency: str = "normal") -> None:
        """Send a notification via the notification tool."""
        from cognitex.agent.tools import SendNotificationTool
        from cognitex.agent.action_log import log_action

        tool = SendNotificationTool()
        await tool.execute(message=message, urgency=urgency)
        await log_action(
            "notification_sent",
            "trigger",
            summary=f"[{urgency}] {message[:100]}...",
            details={"urgency": urgency, "message_length": len(message)}
        )

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
