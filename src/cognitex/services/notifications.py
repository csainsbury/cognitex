"""Notification Service with Debouncing and Deduplication.

Inspired by OpenClaw's messaging patterns:
- Debouncing: Buffer rapid notifications into consolidated messages
- Deduplication: Skip duplicate notifications within a time window
- Coalescing: Merge related notifications into single messages
- Routing: Different channels for different urgency levels

Usage:
    from cognitex.services.notifications import publish_notification, get_notification_service

    # Simple notification
    await publish_notification("Task completed", urgency="normal")

    # With category for grouping
    await publish_notification("New email from Alice", category="email", urgency="normal")

    # Urgent bypasses debouncing
    await publish_notification("Meeting in 5 minutes!", urgency="high")
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import structlog

logger = structlog.get_logger()


@dataclass
class PendingNotification:
    """A notification waiting to be sent."""
    message: str
    urgency: str
    category: str | None
    approval_id: str | None
    created_at: datetime
    content_hash: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class NotificationConfig:
    """Configuration for the notification service."""
    # Debounce window - wait this long for more notifications before sending
    debounce_ms: int = 2000

    # Per-category debounce overrides
    debounce_by_category: dict[str, int] = field(default_factory=lambda: {
        "email": 3000,      # Batch email notifications longer
        "task": 2000,       # Task notifications
        "meeting": 1000,    # Meeting notifications are more time-sensitive
        "system": 5000,     # System notifications can wait
    })

    # Deduplication window - ignore duplicate content within this window
    dedupe_window_seconds: int = 300  # 5 minutes

    # Maximum notifications to coalesce into one message
    max_coalesce: int = 5

    # Urgency levels that bypass debouncing
    bypass_debounce_urgency: list[str] = field(default_factory=lambda: ["high", "urgent"])

    # Whether to send to Discord (can be disabled for web-only)
    discord_enabled: bool = True

    # Whether to send to web SSE
    web_enabled: bool = True


class NotificationService:
    """Service for managing notifications with debouncing and deduplication.

    Key features:
    - Debouncing: Waits for a quiet period before sending, allowing multiple
      rapid notifications to be consolidated
    - Deduplication: Tracks content hashes to avoid sending the same notification
      multiple times within a window
    - Coalescing: Groups related notifications (by category) into single messages
    - Routing: Supports different urgency levels with configurable behavior
    """

    def __init__(self, config: NotificationConfig | None = None):
        self.config = config or NotificationConfig()

        # Pending notifications by category (for coalescing)
        self._pending: dict[str, list[PendingNotification]] = {}

        # Debounce timers by category
        self._timers: dict[str, asyncio.Task] = {}

        # Recent notification hashes for deduplication
        self._recent_hashes: dict[str, datetime] = {}

        # Lock for thread safety
        self._lock = asyncio.Lock()

        # Cleanup task
        self._cleanup_task: asyncio.Task | None = None

    async def start(self):
        """Start the notification service background tasks."""
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
            logger.info("Notification service started")

    async def stop(self):
        """Stop the notification service."""
        # Cancel all pending timers
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()

        # Flush any pending notifications
        await self._flush_all()

        # Stop cleanup task
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None

        logger.info("Notification service stopped")

    async def notify(
        self,
        message: str,
        urgency: str = "normal",
        category: str | None = None,
        approval_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Queue a notification for delivery.

        Args:
            message: The notification message
            urgency: 'low', 'normal', 'high', or 'urgent'
            category: Optional category for grouping (email, task, meeting, etc.)
            approval_id: Optional approval ID for actionable notifications
            metadata: Optional additional data

        Returns:
            True if notification was queued/sent, False if deduplicated
        """
        # Generate content hash for deduplication
        content_hash = self._hash_content(message, category, approval_id)

        # Check for duplicates
        if await self._is_duplicate(content_hash):
            logger.debug(
                "Notification deduplicated",
                message_preview=message[:50],
                category=category,
            )
            return False

        # Record this hash
        async with self._lock:
            self._recent_hashes[content_hash] = datetime.now()

        # Create pending notification
        notification = PendingNotification(
            message=message,
            urgency=urgency,
            category=category,
            approval_id=approval_id,
            created_at=datetime.now(),
            content_hash=content_hash,
            metadata=metadata or {},
        )

        # High urgency bypasses debouncing
        if urgency in self.config.bypass_debounce_urgency:
            await self._send_immediate(notification)
            return True

        # Add to pending and schedule flush
        await self._queue_notification(notification)
        return True

    async def _queue_notification(self, notification: PendingNotification):
        """Add notification to pending queue and schedule flush."""
        category_key = notification.category or "_default"

        async with self._lock:
            if category_key not in self._pending:
                self._pending[category_key] = []

            self._pending[category_key].append(notification)

            # Cancel existing timer for this category
            if category_key in self._timers:
                self._timers[category_key].cancel()

            # Get debounce time for this category
            debounce_ms = self.config.debounce_by_category.get(
                category_key,
                self.config.debounce_ms
            )

            # Schedule flush after debounce window
            self._timers[category_key] = asyncio.create_task(
                self._delayed_flush(category_key, debounce_ms)
            )

        logger.debug(
            "Notification queued",
            category=category_key,
            pending_count=len(self._pending.get(category_key, [])),
            debounce_ms=debounce_ms,
        )

    async def _delayed_flush(self, category_key: str, delay_ms: int):
        """Wait for debounce period then flush notifications."""
        try:
            await asyncio.sleep(delay_ms / 1000)
            await self._flush_category(category_key)
        except asyncio.CancelledError:
            # Timer was cancelled (new notification arrived)
            pass

    async def _flush_category(self, category_key: str):
        """Flush all pending notifications for a category."""
        async with self._lock:
            notifications = self._pending.pop(category_key, [])
            self._timers.pop(category_key, None)

        if not notifications:
            return

        # Coalesce notifications into a single message
        coalesced = self._coalesce_notifications(notifications)

        # Send the coalesced notification
        await self._publish_to_channels(coalesced)

    async def _flush_all(self):
        """Flush all pending notifications."""
        async with self._lock:
            categories = list(self._pending.keys())

        for category in categories:
            await self._flush_category(category)

    def _coalesce_notifications(
        self,
        notifications: list[PendingNotification]
    ) -> PendingNotification:
        """Merge multiple notifications into one."""
        if len(notifications) == 1:
            return notifications[0]

        # Use highest urgency
        urgency_order = {"low": 0, "normal": 1, "high": 2, "urgent": 3}
        max_urgency = max(notifications, key=lambda n: urgency_order.get(n.urgency, 1))

        # Collect approval IDs
        approval_ids = [n.approval_id for n in notifications if n.approval_id]

        # Build coalesced message
        if len(notifications) <= self.config.max_coalesce:
            # List all messages
            messages = [f"• {n.message}" for n in notifications]
            category = notifications[0].category
            header = f"**{len(notifications)} {category or 'notifications'}:**\n" if category else ""
            combined_message = header + "\n".join(messages)
        else:
            # Summarize if too many
            shown = notifications[:self.config.max_coalesce]
            remaining = len(notifications) - self.config.max_coalesce
            messages = [f"• {n.message}" for n in shown]
            category = notifications[0].category
            header = f"**{len(notifications)} {category or 'notifications'}:**\n" if category else ""
            combined_message = header + "\n".join(messages) + f"\n_...and {remaining} more_"

        return PendingNotification(
            message=combined_message,
            urgency=max_urgency.urgency,
            category=notifications[0].category,
            approval_id=approval_ids[0] if len(approval_ids) == 1 else None,
            created_at=notifications[0].created_at,
            content_hash="coalesced",
            metadata={
                "coalesced_count": len(notifications),
                "approval_ids": approval_ids if len(approval_ids) > 1 else None,
            },
        )

    async def _send_immediate(self, notification: PendingNotification):
        """Send a notification immediately (bypassing debounce)."""
        await self._publish_to_channels(notification)

    async def _publish_to_channels(self, notification: PendingNotification):
        """Publish notification to configured channels."""
        from cognitex.db.redis import get_redis

        payload = {
            "message": notification.message,
            "urgency": notification.urgency,
            "category": notification.category,
            "timestamp": notification.created_at.isoformat(),
        }

        if notification.approval_id:
            payload["approval_id"] = notification.approval_id

        if notification.metadata:
            payload["metadata"] = notification.metadata

        try:
            redis = get_redis()

            # Publish to the unified channel (both Discord bot and web app subscribe)
            if self.config.discord_enabled or self.config.web_enabled:
                await redis.publish("cognitex:notifications", json.dumps(payload))

            logger.info(
                "Notification published",
                urgency=notification.urgency,
                category=notification.category,
                message_preview=notification.message[:80],
                coalesced=notification.metadata.get("coalesced_count"),
            )

        except Exception as e:
            logger.error("Failed to publish notification", error=str(e))

    async def _is_duplicate(self, content_hash: str) -> bool:
        """Check if this content was recently sent."""
        async with self._lock:
            if content_hash in self._recent_hashes:
                sent_at = self._recent_hashes[content_hash]
                window = timedelta(seconds=self.config.dedupe_window_seconds)
                if datetime.now() - sent_at < window:
                    return True
            return False

    def _hash_content(
        self,
        message: str,
        category: str | None,
        approval_id: str | None
    ) -> str:
        """Generate a hash for deduplication."""
        content = f"{message}|{category}|{approval_id}"
        return hashlib.md5(content.encode()).hexdigest()[:16]

    async def _cleanup_loop(self):
        """Periodically clean up old hash entries."""
        while True:
            try:
                await asyncio.sleep(60)  # Run every minute
                await self._cleanup_old_hashes()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Notification cleanup error", error=str(e))

    async def _cleanup_old_hashes(self):
        """Remove expired hash entries."""
        cutoff = datetime.now() - timedelta(seconds=self.config.dedupe_window_seconds)

        async with self._lock:
            expired = [
                h for h, t in self._recent_hashes.items()
                if t < cutoff
            ]
            for h in expired:
                del self._recent_hashes[h]

            if expired:
                logger.debug("Cleaned up notification hashes", count=len(expired))


# Singleton instance
_service: NotificationService | None = None


def get_notification_service() -> NotificationService:
    """Get or create the notification service singleton."""
    global _service
    if _service is None:
        _service = NotificationService()
    return _service


async def init_notification_service() -> NotificationService:
    """Initialize and start the notification service."""
    service = get_notification_service()
    await service.start()
    return service


async def publish_notification(
    message: str,
    urgency: str = "normal",
    category: str | None = None,
    approval_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> bool:
    """Convenience function to publish a notification.

    Args:
        message: The notification message
        urgency: 'low', 'normal', 'high', or 'urgent'
        category: Optional category for grouping
        approval_id: Optional approval ID
        metadata: Optional additional data

    Returns:
        True if queued/sent, False if deduplicated
    """
    service = get_notification_service()
    return await service.notify(
        message=message,
        urgency=urgency,
        category=category,
        approval_id=approval_id,
        metadata=metadata,
    )
