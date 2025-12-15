"""Google Push Notifications - Watch management for Gmail, Calendar, and Drive."""

import json
import uuid
from datetime import datetime, timedelta
from typing import Any

import structlog

from cognitex.config import get_settings
from cognitex.services.google_auth import get_google_credentials

logger = structlog.get_logger()


class WatchManager:
    """
    Manages Google API watch subscriptions for real-time notifications.

    - Gmail: Uses Google Cloud Pub/Sub (requires topic setup)
    - Calendar: Direct webhook
    - Drive: Direct webhook
    """

    def __init__(self, webhook_base_url: str | None = None):
        """
        Initialize watch manager.

        Args:
            webhook_base_url: Public HTTPS URL for webhooks (e.g., https://your-domain.com)
                             If not provided, watches requiring webhooks won't work.
        """
        self.settings = get_settings()
        self.webhook_base_url = webhook_base_url
        self._credentials = None
        self._gmail_service = None
        self._calendar_service = None
        self._drive_service = None

        # Track active watches for renewal
        self._active_watches: dict[str, dict] = {}

    @property
    def credentials(self):
        if self._credentials is None:
            self._credentials = get_google_credentials()
        return self._credentials

    @property
    def gmail(self):
        if self._gmail_service is None:
            from googleapiclient.discovery import build
            self._gmail_service = build("gmail", "v1", credentials=self.credentials)
        return self._gmail_service

    @property
    def calendar(self):
        if self._calendar_service is None:
            from googleapiclient.discovery import build
            self._calendar_service = build("calendar", "v3", credentials=self.credentials)
        return self._calendar_service

    @property
    def drive(self):
        if self._drive_service is None:
            from googleapiclient.discovery import build
            self._drive_service = build("drive", "v3", credentials=self.credentials)
        return self._drive_service

    # =========================================================================
    # Gmail Watch (requires Pub/Sub)
    # =========================================================================

    async def setup_gmail_watch(self, pubsub_topic: str | None = None) -> dict:
        """
        Set up Gmail push notifications via Pub/Sub.

        Args:
            pubsub_topic: Full topic name (projects/PROJECT_ID/topics/TOPIC_NAME)
                         If not provided, uses GOOGLE_PUBSUB_TOPIC from settings.

        Returns:
            Watch response with historyId and expiration

        Note: Requires Pub/Sub topic with gmail-api-push@system.gserviceaccount.com
              as a Publisher.
        """
        topic = pubsub_topic or getattr(self.settings, 'google_pubsub_topic', None)

        if not topic:
            logger.warning("No Pub/Sub topic configured for Gmail watch")
            return {"error": "No Pub/Sub topic configured"}

        try:
            request = {
                'topicName': topic,
                'labelIds': ['INBOX'],
                'labelFilterBehavior': 'INCLUDE',
            }

            response = self.gmail.users().watch(userId='me', body=request).execute()

            # Store watch info for renewal
            self._active_watches['gmail'] = {
                'type': 'gmail',
                'historyId': response.get('historyId'),
                'expiration': response.get('expiration'),
                'topic': topic,
                'created_at': datetime.now().isoformat(),
            }

            logger.info(
                "Gmail watch created",
                history_id=response.get('historyId'),
                expiration=response.get('expiration'),
            )

            return response

        except Exception as e:
            logger.error("Failed to create Gmail watch", error=str(e))
            return {"error": str(e)}

    async def stop_gmail_watch(self) -> bool:
        """Stop Gmail watch (by not renewing - Gmail has no explicit stop)."""
        if 'gmail' in self._active_watches:
            del self._active_watches['gmail']
            logger.info("Gmail watch stopped (will expire naturally)")
        return True

    # =========================================================================
    # Calendar Watch (direct webhook)
    # =========================================================================

    async def setup_calendar_watch(self, calendar_id: str = "primary") -> dict:
        """
        Set up Calendar push notifications.

        Args:
            calendar_id: Calendar to watch (default: primary)

        Returns:
            Watch response with channel info
        """
        if not self.webhook_base_url:
            logger.warning("No webhook URL configured for Calendar watch")
            return {"error": "No webhook URL configured"}

        try:
            channel_id = str(uuid.uuid4())
            token = f"calendar:{calendar_id}:{uuid.uuid4().hex[:8]}"

            # Expire in 7 days (max for Calendar)
            expiration = int((datetime.now() + timedelta(days=7)).timestamp() * 1000)

            body = {
                'id': channel_id,
                'type': 'web_hook',
                'address': f"{self.webhook_base_url}/webhooks/google/calendar",
                'token': token,
                'expiration': expiration,
            }

            response = self.calendar.events().watch(
                calendarId=calendar_id,
                body=body
            ).execute()

            # Store for renewal and stopping
            self._active_watches[f'calendar:{calendar_id}'] = {
                'type': 'calendar',
                'calendar_id': calendar_id,
                'channel_id': channel_id,
                'resource_id': response.get('resourceId'),
                'expiration': response.get('expiration'),
                'token': token,
                'created_at': datetime.now().isoformat(),
            }

            logger.info(
                "Calendar watch created",
                calendar_id=calendar_id,
                channel_id=channel_id,
                expiration=response.get('expiration'),
            )

            return response

        except Exception as e:
            logger.error("Failed to create Calendar watch", error=str(e), calendar_id=calendar_id)
            return {"error": str(e)}

    async def stop_calendar_watch(self, calendar_id: str = "primary") -> bool:
        """Stop Calendar watch."""
        key = f'calendar:{calendar_id}'
        watch = self._active_watches.get(key)

        if not watch:
            return True

        try:
            self.calendar.channels().stop(body={
                'id': watch['channel_id'],
                'resourceId': watch['resource_id'],
            }).execute()

            del self._active_watches[key]
            logger.info("Calendar watch stopped", calendar_id=calendar_id)
            return True

        except Exception as e:
            logger.error("Failed to stop Calendar watch", error=str(e))
            return False

    # =========================================================================
    # Drive Watch (direct webhook)
    # =========================================================================

    async def setup_drive_watch(self) -> dict:
        """
        Set up Drive push notifications for all changes.

        Note: Drive watches all changes, filtering by folder must be done
        when processing the changes.

        Returns:
            Watch response with channel info
        """
        if not self.webhook_base_url:
            logger.warning("No webhook URL configured for Drive watch")
            return {"error": "No webhook URL configured"}

        try:
            # Get starting page token
            start_token_response = self.drive.changes().getStartPageToken().execute()
            page_token = start_token_response.get('startPageToken')

            channel_id = str(uuid.uuid4())
            token = f"drive:{uuid.uuid4().hex[:8]}"

            # Expire in 1 week (max for Drive changes)
            expiration = int((datetime.now() + timedelta(days=7)).timestamp() * 1000)

            body = {
                'id': channel_id,
                'type': 'web_hook',
                'address': f"{self.webhook_base_url}/webhooks/google/drive",
                'token': token,
                'expiration': expiration,
            }

            response = self.drive.changes().watch(
                pageToken=page_token,
                body=body,
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
            ).execute()

            # Store for renewal and stopping
            self._active_watches['drive'] = {
                'type': 'drive',
                'channel_id': channel_id,
                'resource_id': response.get('resourceId'),
                'page_token': page_token,
                'expiration': response.get('expiration'),
                'token': token,
                'created_at': datetime.now().isoformat(),
            }

            logger.info(
                "Drive watch created",
                channel_id=channel_id,
                page_token=page_token,
                expiration=response.get('expiration'),
            )

            return response

        except Exception as e:
            logger.error("Failed to create Drive watch", error=str(e))
            return {"error": str(e)}

    async def stop_drive_watch(self) -> bool:
        """Stop Drive watch."""
        watch = self._active_watches.get('drive')

        if not watch:
            return True

        try:
            self.drive.channels().stop(body={
                'id': watch['channel_id'],
                'resourceId': watch['resource_id'],
            }).execute()

            del self._active_watches['drive']
            logger.info("Drive watch stopped")
            return True

        except Exception as e:
            logger.error("Failed to stop Drive watch", error=str(e))
            return False

    # =========================================================================
    # Renewal and Status
    # =========================================================================

    async def renew_all_watches(self) -> dict:
        """
        Renew all active watches that are close to expiration.

        Call this daily to ensure watches don't expire.
        """
        results = {}

        for key, watch in list(self._active_watches.items()):
            expiration_ms = watch.get('expiration')
            if expiration_ms:
                expiration = datetime.fromtimestamp(int(expiration_ms) / 1000)
                # Renew if expiring within 2 days
                if expiration < datetime.now() + timedelta(days=2):
                    logger.info(f"Renewing watch: {key}")

                    if watch['type'] == 'gmail':
                        results[key] = await self.setup_gmail_watch(watch.get('topic'))
                    elif watch['type'] == 'calendar':
                        results[key] = await self.setup_calendar_watch(watch.get('calendar_id'))
                    elif watch['type'] == 'drive':
                        results[key] = await self.setup_drive_watch()

        return results

    def get_active_watches(self) -> dict:
        """Get info about all active watches."""
        return self._active_watches.copy()

    async def setup_all_watches(self, pubsub_topic: str | None = None) -> dict:
        """
        Set up all watches (Gmail, Calendar, Drive).

        Args:
            pubsub_topic: Pub/Sub topic for Gmail

        Returns:
            Results for each watch setup
        """
        results = {}

        # Gmail (if topic configured)
        if pubsub_topic or getattr(self.settings, 'google_pubsub_topic', None):
            results['gmail'] = await self.setup_gmail_watch(pubsub_topic)

        # Calendar
        if self.webhook_base_url:
            results['calendar'] = await self.setup_calendar_watch()
            results['drive'] = await self.setup_drive_watch()

        return results


# Singleton
_watch_manager: WatchManager | None = None


def get_watch_manager(webhook_base_url: str | None = None) -> WatchManager:
    """Get or create the watch manager singleton."""
    global _watch_manager
    if _watch_manager is None:
        _watch_manager = WatchManager(webhook_base_url)
    return _watch_manager
