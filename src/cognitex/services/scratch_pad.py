"""Scratch pad service for active working memory.

Provides a visible, editable workspace that bridges agent actions
and user context. Based on the "scratch pad" pattern from PA videos.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import text

from cognitex.config import get_settings

logger = structlog.get_logger()


@dataclass
class ScratchEntry:
    """A single entry in a scratch space."""

    timestamp: datetime
    source: str  # 'agent', 'user', 'system'
    text: str
    archived: bool = False

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp.isoformat(),
            "source": self.source,
            "text": self.text,
            "archived": self.archived,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ScratchEntry":
        return cls(
            timestamp=datetime.fromisoformat(data["timestamp"]) if isinstance(data["timestamp"], str) else data["timestamp"],
            source=data.get("source", "system"),
            text=data.get("text", ""),
            archived=data.get("archived", False),
        )


@dataclass
class ScratchSpace:
    """A scratch space for active working memory."""

    id: str
    name: str
    space_type: str  # 'general', 'project', 'task'
    linked_entity_id: str | None
    content: str
    entries: list[ScratchEntry] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "space_type": self.space_type,
            "linked_entity_id": self.linked_entity_id,
            "content": self.content,
            "entries": [e.to_dict() for e in self.entries],
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


class ScratchPadService:
    """Service for managing scratch spaces.

    Provides CRUD operations for scratch spaces and entries,
    plus context generation for agent prompts.
    """

    def __init__(self):
        self._cache: dict[str, ScratchSpace] = {}
        self._cache_ttl: dict[str, datetime] = {}
        self._cache_duration = timedelta(minutes=5)

    async def get_space(self, name: str) -> ScratchSpace | None:
        """Get a scratch space by name."""
        # Check cache first
        if name in self._cache and self._is_cache_valid(name):
            return self._cache[name]

        from cognitex.db.postgres import get_session

        async for session in get_session():
            try:
                result = await session.execute(
                    text("""
                        SELECT id, name, space_type, linked_entity_id,
                               content, entries, created_at, updated_at
                        FROM scratch_spaces
                        WHERE name = :name
                    """),
                    {"name": name},
                )
                row = result.fetchone()

                if row:
                    entries = []
                    if row[5]:
                        entries_data = row[5] if isinstance(row[5], list) else json.loads(row[5])
                        entries = [ScratchEntry.from_dict(e) for e in entries_data]

                    space = ScratchSpace(
                        id=row[0],
                        name=row[1],
                        space_type=row[2],
                        linked_entity_id=row[3],
                        content=row[4] or "",
                        entries=entries,
                        created_at=row[6],
                        updated_at=row[7],
                    )
                    self._update_cache(name, space)
                    return space
            except Exception as e:
                logger.warning("Failed to fetch scratch space", name=name, error=str(e))
            break

        return None

    async def create_space(
        self,
        name: str,
        space_type: str = "general",
        linked_id: str | None = None,
    ) -> ScratchSpace:
        """Create a new scratch space."""
        from cognitex.db.postgres import get_session

        space_id = f"scratch_{uuid.uuid4().hex[:12]}"
        now = datetime.now()

        space = ScratchSpace(
            id=space_id,
            name=name,
            space_type=space_type,
            linked_entity_id=linked_id,
            content="",
            entries=[],
            created_at=now,
            updated_at=now,
        )

        async for session in get_session():
            try:
                await session.execute(
                    text("""
                        INSERT INTO scratch_spaces (
                            id, name, space_type, linked_entity_id,
                            content, entries, created_at, updated_at
                        ) VALUES (
                            :id, :name, :space_type, :linked_id,
                            :content, :entries, :created_at, :updated_at
                        )
                        ON CONFLICT (name) DO NOTHING
                    """),
                    {
                        "id": space_id,
                        "name": name,
                        "space_type": space_type,
                        "linked_id": linked_id,
                        "content": "",
                        "entries": "[]",
                        "created_at": now,
                        "updated_at": now,
                    },
                )
                await session.commit()
                logger.info("Created scratch space", name=name, space_type=space_type)
            except Exception as e:
                logger.warning("Failed to create scratch space", name=name, error=str(e))
                # Try to fetch existing
                existing = await self.get_space(name)
                if existing:
                    return existing
            break

        self._update_cache(name, space)
        return space

    async def get_or_create_space(
        self,
        name: str,
        space_type: str = "general",
        linked_id: str | None = None,
    ) -> ScratchSpace:
        """Get existing space or create new one."""
        space = await self.get_space(name)
        if space:
            return space
        return await self.create_space(name, space_type, linked_id)

    async def append_entry(
        self,
        space_name: str,
        entry_text: str,
        source: str = "agent",
    ) -> ScratchEntry:
        """Append an entry to a scratch space."""
        from cognitex.db.postgres import get_session

        entry = ScratchEntry(
            timestamp=datetime.now(),
            source=source,
            text=entry_text,
        )

        async for session in get_session():
            try:
                # Append to entries array
                await session.execute(
                    text("""
                        UPDATE scratch_spaces
                        SET entries = entries || CAST(:entry AS jsonb),
                            updated_at = NOW()
                        WHERE name = :name
                    """),
                    {
                        "name": space_name,
                        "entry": json.dumps([entry.to_dict()]),
                    },
                )
                await session.commit()
                logger.debug("Appended scratch entry", space=space_name, source=source)
            except Exception as e:
                logger.warning("Failed to append scratch entry", space=space_name, error=str(e))
            break

        # Invalidate cache
        self._invalidate_cache(space_name)

        # Publish event for user entries (triggers agent response)
        if source == "user":
            try:
                from cognitex.db.redis import get_redis
                redis = get_redis()
                await redis.publish("cognitex:events:scratch_pad", json.dumps({
                    "space_name": space_name,
                    "text": entry_text[:200],
                    "source": source,
                }))
            except Exception as e:
                logger.debug("Failed to publish scratch pad event", error=str(e))

        return entry

    async def update_content(
        self,
        space_name: str,
        content: str,
    ) -> bool:
        """Update the main content of a scratch space (user edit)."""
        from cognitex.db.postgres import get_session

        async for session in get_session():
            try:
                await session.execute(
                    text("""
                        UPDATE scratch_spaces
                        SET content = :content,
                            updated_at = NOW()
                        WHERE name = :name
                    """),
                    {"name": space_name, "content": content},
                )
                await session.commit()
                logger.info("Updated scratch content", space=space_name)
                self._invalidate_cache(space_name)
                return True
            except Exception as e:
                logger.warning("Failed to update scratch content", space=space_name, error=str(e))
            break

        return False

    async def get_context_for_prompt(
        self,
        space_names: list[str] | None = None,
        max_entries: int = 10,
        max_length: int = 2000,
    ) -> str:
        """Get formatted scratch pad context for agent prompt injection.

        Args:
            space_names: Specific spaces to include, or None for 'general'
            max_entries: Maximum entries per space
            max_length: Maximum total character length

        Returns:
            Formatted context string for prompt injection
        """
        if space_names is None:
            space_names = ["general"]

        sections = []
        total_length = 0

        for name in space_names:
            space = await self.get_space(name)
            if not space:
                continue

            # Get recent, non-archived entries
            active_entries = [
                e for e in space.entries
                if not e.archived
            ][-max_entries:]

            if not active_entries and not space.content:
                continue

            section_lines = [f"### Scratch: {name}"]

            # Add main content if present
            if space.content:
                section_lines.append(space.content[:500])

            # Add recent entries
            if active_entries:
                section_lines.append("\n**Recent activity:**")
                for entry in active_entries:
                    time_str = entry.timestamp.strftime("%H:%M")
                    section_lines.append(f"- [{time_str}] ({entry.source}) {entry.text[:200]}")

            section_text = "\n".join(section_lines)

            # Check length limit
            if total_length + len(section_text) > max_length:
                break

            sections.append(section_text)
            total_length += len(section_text)

        if not sections:
            return ""

        return "## Active Scratch Pad\n\n" + "\n\n".join(sections)

    async def archive_old_entries(
        self,
        days: int | None = None,
    ) -> int:
        """Archive entries older than specified days."""
        from cognitex.db.postgres import get_session

        if days is None:
            settings = get_settings()
            days = settings.scratch_archive_days

        cutoff = datetime.now() - timedelta(days=days)
        archived_count = 0

        async for session in get_session():
            try:
                # Get all spaces with old entries
                result = await session.execute(
                    text("""
                        SELECT id, name, entries
                        FROM scratch_spaces
                        WHERE entries != '[]'::jsonb
                    """)
                )
                rows = result.fetchall()

                for row in rows:
                    space_id, name, entries_data = row
                    entries = entries_data if isinstance(entries_data, list) else json.loads(entries_data)

                    old_entries = []
                    kept_entries = []

                    for e in entries:
                        entry_time = datetime.fromisoformat(e["timestamp"]) if isinstance(e["timestamp"], str) else e["timestamp"]
                        if entry_time < cutoff:
                            old_entries.append(e)
                        else:
                            kept_entries.append(e)

                    if old_entries:
                        # Create archive record
                        archive_id = f"archive_{uuid.uuid4().hex[:12]}"
                        await session.execute(
                            text("""
                                INSERT INTO scratch_archive (id, scratch_space_id, entries)
                                VALUES (:id, :space_id, :entries)
                            """),
                            {
                                "id": archive_id,
                                "space_id": space_id,
                                "entries": json.dumps(old_entries),
                            },
                        )

                        # Update space with kept entries
                        await session.execute(
                            text("""
                                UPDATE scratch_spaces
                                SET entries = :entries
                                WHERE id = :id
                            """),
                            {"id": space_id, "entries": json.dumps(kept_entries)},
                        )

                        archived_count += len(old_entries)
                        logger.info("Archived scratch entries", space=name, count=len(old_entries))

                await session.commit()
            except Exception as e:
                logger.warning("Failed to archive scratch entries", error=str(e))
            break

        # Clear cache
        self._cache.clear()
        self._cache_ttl.clear()

        return archived_count

    async def list_spaces(
        self,
        space_type: str | None = None,
    ) -> list[ScratchSpace]:
        """List all scratch spaces, optionally filtered by type."""
        from cognitex.db.postgres import get_session

        async for session in get_session():
            try:
                if space_type:
                    result = await session.execute(
                        text("""
                            SELECT id, name, space_type, linked_entity_id,
                                   content, entries, created_at, updated_at
                            FROM scratch_spaces
                            WHERE space_type = :space_type
                            ORDER BY updated_at DESC
                        """),
                        {"space_type": space_type},
                    )
                else:
                    result = await session.execute(
                        text("""
                            SELECT id, name, space_type, linked_entity_id,
                                   content, entries, created_at, updated_at
                            FROM scratch_spaces
                            ORDER BY updated_at DESC
                        """)
                    )

                rows = result.fetchall()
                spaces = []

                for row in rows:
                    entries = []
                    if row[5]:
                        entries_data = row[5] if isinstance(row[5], list) else json.loads(row[5])
                        entries = [ScratchEntry.from_dict(e) for e in entries_data]

                    spaces.append(ScratchSpace(
                        id=row[0],
                        name=row[1],
                        space_type=row[2],
                        linked_entity_id=row[3],
                        content=row[4] or "",
                        entries=entries,
                        created_at=row[6],
                        updated_at=row[7],
                    ))

                return spaces
            except Exception as e:
                logger.warning("Failed to list scratch spaces", error=str(e))
            break

        return []

    async def delete_space(self, name: str) -> bool:
        """Delete a scratch space."""
        from cognitex.db.postgres import get_session

        async for session in get_session():
            try:
                await session.execute(
                    text("DELETE FROM scratch_spaces WHERE name = :name"),
                    {"name": name},
                )
                await session.commit()
                self._invalidate_cache(name)
                logger.info("Deleted scratch space", name=name)
                return True
            except Exception as e:
                logger.warning("Failed to delete scratch space", name=name, error=str(e))
            break

        return False

    def _is_cache_valid(self, name: str) -> bool:
        """Check if cache entry is still valid."""
        if name not in self._cache_ttl:
            return False
        return datetime.now() < self._cache_ttl[name]

    def _update_cache(self, name: str, space: ScratchSpace) -> None:
        """Update cache entry."""
        self._cache[name] = space
        self._cache_ttl[name] = datetime.now() + self._cache_duration

    def _invalidate_cache(self, name: str) -> None:
        """Invalidate cache entry."""
        self._cache.pop(name, None)
        self._cache_ttl.pop(name, None)


# Singleton instance
_service: ScratchPadService | None = None


def get_scratch_pad_service() -> ScratchPadService:
    """Get or create the scratch pad service singleton."""
    global _service
    if _service is None:
        _service = ScratchPadService()
    return _service
