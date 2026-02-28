"""Unified Agent Inbox Service.

Provides a central location for all agent suggestions requiring user decisions:
- Task proposals
- Context packs
- Email drafts
- Flagged items for review

Each item can be approved, rejected, or dismissed, with feedback captured
for continuous learning.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import text

logger = structlog.get_logger()


@dataclass
class InboxItem:
    """A single item in the agent inbox."""

    id: str
    item_type: str  # 'task_proposal', 'context_pack', 'email_draft', 'flagged_item'
    status: str  # 'pending', 'approved', 'rejected', 'dismissed'
    priority: str  # 'urgent', 'high', 'normal', 'low'
    title: str
    summary: str | None
    payload: dict[str, Any]
    source_id: str | None
    source_type: str | None
    created_at: datetime
    decided_at: datetime | None = None
    decision_reason: str | None = None
    expires_at: datetime | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "item_type": self.item_type,
            "status": self.status,
            "priority": self.priority,
            "title": self.title,
            "summary": self.summary,
            "payload": self.payload,
            "source_id": self.source_id,
            "source_type": self.source_type,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "decided_at": self.decided_at.isoformat() if self.decided_at else None,
            "decision_reason": self.decision_reason,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }

    @classmethod
    def from_row(cls, row) -> "InboxItem":
        """Create InboxItem from database row."""
        payload = row[6]
        if isinstance(payload, str):
            payload = json.loads(payload)
        elif payload is None:
            payload = {}

        return cls(
            id=row[0],
            item_type=row[1],
            status=row[2],
            priority=row[3],
            title=row[4],
            summary=row[5],
            payload=payload,
            source_id=row[7],
            source_type=row[8],
            created_at=row[9],
            decided_at=row[10],
            decision_reason=row[11],
            expires_at=row[12],
        )


class InboxService:
    """Service for managing the unified agent inbox.

    Provides CRUD operations for inbox items and feedback,
    plus methods for common workflows like approve/reject.
    """

    async def create_item(
        self,
        item_type: str,
        title: str,
        payload: dict[str, Any],
        summary: str | None = None,
        source_id: str | None = None,
        source_type: str | None = None,
        priority: str = "normal",
        expires_at: datetime | None = None,
    ) -> InboxItem:
        """Create a new inbox item.

        Args:
            item_type: Type of item ('task_proposal', 'context_pack', etc.)
            title: Display title for the item
            payload: Type-specific data (project_id, draft content, etc.)
            summary: Optional brief description
            source_id: ID of the originating entity
            source_type: Table name of the originating entity
            priority: 'urgent', 'high', 'normal', or 'low'
            expires_at: When this item becomes irrelevant

        Returns:
            The created InboxItem
        """
        from cognitex.db.postgres import get_session

        # Deduplicate: skip if a pending item with the same source already exists
        if source_id:
            async for session in get_session():
                try:
                    result = await session.execute(
                        text("""
                            SELECT id FROM inbox_items
                            WHERE source_id = :source_id
                              AND status = 'pending'
                            LIMIT 1
                        """),
                        {"source_id": source_id},
                    )
                    existing = result.fetchone()
                    if existing:
                        logger.debug(
                            "Inbox item already exists for source",
                            source_id=source_id,
                            existing_id=existing[0],
                        )
                        return await self.get_item(existing[0])
                except Exception:
                    pass  # Proceed with creation on error
                break

        item_id = f"inbox_{uuid.uuid4().hex[:12]}"
        now = datetime.now()

        # Normalize expires_at to naive datetime (PostgreSQL compatibility)
        if expires_at is not None and expires_at.tzinfo is not None:
            expires_at = expires_at.replace(tzinfo=None)

        item = InboxItem(
            id=item_id,
            item_type=item_type,
            status="pending",
            priority=priority,
            title=title,
            summary=summary,
            payload=payload,
            source_id=source_id,
            source_type=source_type,
            created_at=now,
            expires_at=expires_at,
        )

        async for session in get_session():
            try:
                await session.execute(
                    text("""
                        INSERT INTO inbox_items (
                            id, item_type, status, priority, title, summary,
                            payload, source_id, source_type, created_at, expires_at
                        ) VALUES (
                            :id, :item_type, :status, :priority, :title, :summary,
                            :payload, :source_id, :source_type, :created_at, :expires_at
                        )
                    """),
                    {
                        "id": item_id,
                        "item_type": item_type,
                        "status": "pending",
                        "priority": priority,
                        "title": title,
                        "summary": summary,
                        "payload": json.dumps(payload),
                        "source_id": source_id,
                        "source_type": source_type,
                        "created_at": now,
                        "expires_at": expires_at,
                    },
                )
                await session.commit()
                logger.info(
                    "Created inbox item",
                    item_id=item_id,
                    item_type=item_type,
                    priority=priority,
                )
            except Exception as e:
                logger.error("Failed to create inbox item", error=str(e))
                raise
            break

        return item

    async def update_item(
        self,
        item_id: str,
        payload: dict[str, Any] | None = None,
        summary: str | None = None,
        priority: str | None = None,
        title: str | None = None,
    ) -> bool:
        """Update an existing inbox item.

        Args:
            item_id: ID of the item to update
            payload: New payload data (replaces existing)
            summary: New summary text
            priority: New priority level
            title: New title

        Returns:
            True if updated successfully
        """
        from cognitex.db.postgres import get_session

        updates = []
        params = {"id": item_id}

        if payload is not None:
            updates.append("payload = :payload")
            params["payload"] = json.dumps(payload)
        if summary is not None:
            updates.append("summary = :summary")
            params["summary"] = summary
        if priority is not None:
            updates.append("priority = :priority")
            params["priority"] = priority
        if title is not None:
            updates.append("title = :title")
            params["title"] = title

        if not updates:
            return True  # Nothing to update

        query = f"UPDATE inbox_items SET {', '.join(updates)} WHERE id = :id"

        async for session in get_session():
            try:
                await session.execute(text(query), params)
                await session.commit()
                logger.info("Updated inbox item", item_id=item_id)
                return True
            except Exception as e:
                logger.error("Failed to update inbox item", item_id=item_id, error=str(e))
                return False
            break

        return False

    async def get_item(self, item_id: str) -> InboxItem | None:
        """Get a single inbox item by ID."""
        from cognitex.db.postgres import get_session

        async for session in get_session():
            try:
                result = await session.execute(
                    text("""
                        SELECT id, item_type, status, priority, title, summary,
                               payload, source_id, source_type, created_at,
                               decided_at, decision_reason, expires_at
                        FROM inbox_items
                        WHERE id = :id
                    """),
                    {"id": item_id},
                )
                row = result.fetchone()
                if row:
                    return InboxItem.from_row(row)
            except Exception as e:
                logger.error("Failed to get inbox item", item_id=item_id, error=str(e))
            break

        return None

    async def get_pending_items(
        self,
        item_type: str | None = None,
        limit: int = 50,
        include_expired: bool = False,
    ) -> list[InboxItem]:
        """Get pending inbox items.

        Args:
            item_type: Filter by type, or None for all types
            limit: Maximum items to return
            include_expired: Whether to include expired items

        Returns:
            List of pending InboxItems, ordered by priority and creation time
        """
        from cognitex.db.postgres import get_session

        async for session in get_session():
            try:
                # Build query with optional filters
                query = """
                    SELECT id, item_type, status, priority, title, summary,
                           payload, source_id, source_type, created_at,
                           decided_at, decision_reason, expires_at
                    FROM inbox_items
                    WHERE status = 'pending'
                """
                params: dict[str, Any] = {"limit": limit}

                if item_type:
                    query += " AND item_type = :item_type"
                    params["item_type"] = item_type

                if not include_expired:
                    query += " AND (expires_at IS NULL OR expires_at > NOW())"

                # Order by priority (urgent first), then by creation time
                query += """
                    ORDER BY
                        CASE priority
                            WHEN 'urgent' THEN 1
                            WHEN 'high' THEN 2
                            WHEN 'normal' THEN 3
                            WHEN 'low' THEN 4
                            ELSE 5
                        END,
                        created_at DESC
                    LIMIT :limit
                """

                result = await session.execute(text(query), params)
                rows = result.fetchall()
                return [InboxItem.from_row(row) for row in rows]
            except Exception as e:
                logger.error("Failed to get pending items", error=str(e))
            break

        return []

    async def get_pending_count(self) -> dict[str, int]:
        """Get counts of pending items by type and urgency.

        Returns:
            Dict with total count, urgent count, and counts by type
        """
        from cognitex.db.postgres import get_session

        counts = {
            "total": 0,
            "urgent": 0,
            "by_type": {
                "task_proposal": 0,
                "context_pack": 0,
                "email_draft": 0,
                "flagged_item": 0,
            },
        }

        async for session in get_session():
            try:
                result = await session.execute(
                    text("""
                        SELECT item_type, priority, COUNT(*)
                        FROM inbox_items
                        WHERE status = 'pending'
                          AND (expires_at IS NULL OR expires_at > NOW())
                        GROUP BY item_type, priority
                    """)
                )
                rows = result.fetchall()

                for row in rows:
                    item_type, priority, count = row
                    counts["total"] += count
                    if priority == "urgent":
                        counts["urgent"] += count
                    if item_type in counts["by_type"]:
                        counts["by_type"][item_type] += count
            except Exception as e:
                logger.error("Failed to get pending count", error=str(e))
            break

        return counts

    async def approve_item(
        self,
        item_id: str,
        reason: str | None = None,
    ) -> InboxItem | None:
        """Approve an inbox item.

        Args:
            item_id: The item to approve
            reason: Optional reason/note for approval

        Returns:
            The updated item, or None if not found
        """
        return await self._update_item_status(item_id, "approved", reason)

    async def reject_item(
        self,
        item_id: str,
        reason_category: str,
        reason_text: str | None = None,
    ) -> InboxItem | None:
        """Reject an inbox item with feedback.

        Args:
            item_id: The item to reject
            reason_category: Category of rejection (e.g., 'not_relevant', 'bad_timing')
            reason_text: Optional detailed explanation

        Returns:
            The updated item, or None if not found
        """
        reason = f"{reason_category}: {reason_text}" if reason_text else reason_category
        item = await self._update_item_status(item_id, "rejected", reason)

        if item:
            # Record detailed feedback
            await self.record_feedback(
                item_id=item_id,
                item_type=item.item_type,
                action="rejected",
                reason_category=reason_category,
                reason_text=reason_text,
                context=item.payload,
            )

        return item

    async def dismiss_item(self, item_id: str) -> InboxItem | None:
        """Dismiss an item without approving or rejecting.

        Used for items that are no longer relevant (e.g., meeting passed).
        """
        return await self._update_item_status(item_id, "dismissed", "dismissed by user")

    async def clear_old_items(self, days: int = 7) -> int:
        """Clear inbox items older than specified days.

        Returns the number of items cleared.
        """
        from cognitex.db.postgres import get_session

        cleared = 0
        async for session in get_session():
            try:
                result = await session.execute(
                    text("""
                        DELETE FROM inbox_items
                        WHERE created_at < NOW() - INTERVAL ':days days'
                        RETURNING id
                    """).bindparams(days=days)
                )
                rows = result.fetchall()
                cleared = len(rows)
                await session.commit()
                logger.info("Cleared old inbox items", count=cleared, days=days)
            except Exception as e:
                logger.error("Failed to clear old items", error=str(e))
            break

        return cleared

    async def clear_dismissed(self) -> int:
        """Clear all dismissed inbox items.

        Returns the number of items cleared.
        """
        from cognitex.db.postgres import get_session

        cleared = 0
        async for session in get_session():
            try:
                result = await session.execute(
                    text("""
                        DELETE FROM inbox_items
                        WHERE status = 'dismissed'
                        RETURNING id
                    """)
                )
                rows = result.fetchall()
                cleared = len(rows)
                await session.commit()
                logger.info("Cleared dismissed inbox items", count=cleared)
            except Exception as e:
                logger.error("Failed to clear dismissed items", error=str(e))
            break

        return cleared

    async def _update_item_status(
        self,
        item_id: str,
        status: str,
        reason: str | None = None,
    ) -> InboxItem | None:
        """Update an item's status."""
        from cognitex.db.postgres import get_session

        async for session in get_session():
            try:
                await session.execute(
                    text("""
                        UPDATE inbox_items
                        SET status = :status,
                            decided_at = NOW(),
                            decision_reason = :reason
                        WHERE id = :id
                    """),
                    {"id": item_id, "status": status, "reason": reason},
                )
                await session.commit()
                logger.info(
                    "Updated inbox item status",
                    item_id=item_id,
                    status=status,
                )
            except Exception as e:
                logger.error(
                    "Failed to update inbox item",
                    item_id=item_id,
                    error=str(e),
                )
                return None
            break

        return await self.get_item(item_id)

    async def record_feedback(
        self,
        item_id: str,
        item_type: str,
        action: str,
        reason_category: str | None = None,
        reason_text: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> str:
        """Record user feedback for learning.

        Args:
            item_id: The inbox item ID
            item_type: Type of item
            action: 'approved', 'rejected', 'helpful', 'not_helpful', 'dismissed'
            reason_category: Quick-select category
            reason_text: Free-form explanation
            context: Additional context for learning

        Returns:
            The feedback record ID
        """
        from cognitex.db.postgres import get_session

        feedback_id = f"feedback_{uuid.uuid4().hex[:12]}"

        async for session in get_session():
            try:
                await session.execute(
                    text("""
                        INSERT INTO inbox_feedback (
                            id, inbox_item_id, item_type, action,
                            reason_category, reason_text, context
                        ) VALUES (
                            :id, :inbox_item_id, :item_type, :action,
                            :reason_category, :reason_text, :context
                        )
                    """),
                    {
                        "id": feedback_id,
                        "inbox_item_id": item_id,
                        "item_type": item_type,
                        "action": action,
                        "reason_category": reason_category,
                        "reason_text": reason_text,
                        "context": json.dumps(context or {}),
                    },
                )
                await session.commit()
                logger.info(
                    "Recorded inbox feedback",
                    feedback_id=feedback_id,
                    action=action,
                )
            except Exception as e:
                logger.error("Failed to record feedback", error=str(e))
            break

        return feedback_id

    async def mark_helpful(
        self,
        item_id: str,
        helpful: bool,
        reason: str | None = None,
    ) -> None:
        """Mark a context pack or other item as helpful/not helpful.

        Used for context pack feedback after the user has used it.
        """
        item = await self.get_item(item_id)
        if not item:
            return

        action = "helpful" if helpful else "not_helpful"
        await self.record_feedback(
            item_id=item_id,
            item_type=item.item_type,
            action=action,
            reason_text=reason,
            context=item.payload,
        )

        logger.info(
            "Marked item helpfulness",
            item_id=item_id,
            helpful=helpful,
        )

    async def get_recent_decisions(
        self,
        item_type: str | None = None,
        limit: int = 20,
    ) -> list[InboxItem]:
        """Get recently decided items for review.

        Args:
            item_type: Filter by type, or None for all
            limit: Maximum items to return

        Returns:
            List of recently decided items
        """
        from cognitex.db.postgres import get_session

        async for session in get_session():
            try:
                query = """
                    SELECT id, item_type, status, priority, title, summary,
                           payload, source_id, source_type, created_at,
                           decided_at, decision_reason, expires_at
                    FROM inbox_items
                    WHERE status != 'pending'
                """
                params: dict[str, Any] = {"limit": limit}

                if item_type:
                    query += " AND item_type = :item_type"
                    params["item_type"] = item_type

                query += " ORDER BY decided_at DESC LIMIT :limit"

                result = await session.execute(text(query), params)
                rows = result.fetchall()
                return [InboxItem.from_row(row) for row in rows]
            except Exception as e:
                logger.error("Failed to get recent decisions", error=str(e))
            break

        return []

    async def cleanup_expired(self) -> int:
        """Mark expired pending items as dismissed.

        Returns:
            Number of items cleaned up
        """
        from cognitex.db.postgres import get_session

        async for session in get_session():
            try:
                result = await session.execute(
                    text("""
                        UPDATE inbox_items
                        SET status = 'dismissed',
                            decided_at = NOW(),
                            decision_reason = 'expired'
                        WHERE status = 'pending'
                          AND expires_at IS NOT NULL
                          AND expires_at < NOW()
                        RETURNING id
                    """)
                )
                rows = result.fetchall()
                await session.commit()

                count = len(rows)
                if count > 0:
                    logger.info("Cleaned up expired inbox items", count=count)
                return count
            except Exception as e:
                logger.error("Failed to cleanup expired items", error=str(e))
            break

        return 0


# Singleton instance
_service: InboxService | None = None


def get_inbox_service() -> InboxService:
    """Get or create the inbox service singleton."""
    global _service
    if _service is None:
        _service = InboxService()
    return _service
