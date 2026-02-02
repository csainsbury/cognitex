"""Memory file service for human-readable, graph-integrated memory.

Implements OpenClaw-inspired memory pattern:
- Daily logs (~/.cognitex/memory/YYYY-MM-DD.md) - append-only observations
- Curated memory (~/.cognitex/memory/MEMORY.md) - long-term knowledge
- MemoryEntry nodes in Neo4j for graph queries

Memory entries are both human-readable AND queryable.
"""

import asyncio
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, date
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()

# Memory directory
MEMORY_DIR = Path.home() / ".cognitex" / "memory"

# Default curated memory template
DEFAULT_CURATED_MEMORY = """# Long-Term Memory

This file contains important observations and knowledge that should persist across sessions.
Edit this file to add or correct information the agent should always know.

## User Preferences
<!-- Add observations about user preferences here -->

## Important Relationships
<!-- Notes about key people and relationships -->

## Recurring Patterns
<!-- Patterns the agent has learned -->

## Corrections
<!-- Things the agent got wrong that should be remembered -->
"""


@dataclass
class MemoryEntry:
    """A single memory entry."""

    id: str
    timestamp: datetime
    content: str
    tags: list[str] = field(default_factory=list)
    source: str = "agent"  # agent, user, system
    category: str | None = None  # email_pattern, task_completion, meeting_note, etc.
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class DailyMemory:
    """A day's memory log."""

    date: date
    path: Path
    entries: list[MemoryEntry]
    raw_content: str


class MemoryFileService:
    """
    Service for managing memory files and syncing with Neo4j.

    Memory is stored in two forms:
    1. Daily logs: Append-only markdown files with timestamped entries
    2. Curated memory: User-editable file with long-term knowledge

    Both are synced to Neo4j MemoryEntry nodes for graph queries.
    """

    def __init__(self, memory_dir: Path | None = None):
        self.memory_dir = memory_dir or MEMORY_DIR
        self._write_lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Initialize memory directory and curated memory file."""
        if not self.memory_dir.exists():
            self.memory_dir.mkdir(parents=True, exist_ok=True)
            logger.info("Created memory directory", path=str(self.memory_dir))

        # Create curated memory file if it doesn't exist
        curated_path = self.memory_dir / "MEMORY.md"
        if not curated_path.exists():
            curated_path.write_text(DEFAULT_CURATED_MEMORY)
            logger.info("Created curated memory file")

    def _get_daily_path(self, d: date) -> Path:
        """Get path to daily memory file."""
        return self.memory_dir / f"{d.isoformat()}.md"

    def _parse_tags(self, content: str) -> list[str]:
        """Extract #tags from content."""
        return re.findall(r"#([\w/\-]+)", content)

    def _parse_daily_log(self, content: str, d: date) -> list[MemoryEntry]:
        """Parse a daily memory log into entries."""
        entries = []
        current_entry = None
        current_lines = []

        for line in content.split("\n"):
            # Look for entry headers like "### 10:30 - Category Name"
            match = re.match(r"^### (\d{1,2}:\d{2}) - (.+)$", line)
            if match:
                # Save previous entry
                if current_entry is not None:
                    current_entry.content = "\n".join(current_lines).strip()
                    current_entry.tags = self._parse_tags(current_entry.content)
                    entries.append(current_entry)

                # Start new entry
                time_str = match.group(1)
                category = match.group(2)
                hour, minute = map(int, time_str.split(":"))
                timestamp = datetime.combine(d, datetime.min.time().replace(hour=hour, minute=minute))

                current_entry = MemoryEntry(
                    id=f"mem_{uuid.uuid4().hex[:12]}",
                    timestamp=timestamp,
                    content="",
                    category=category,
                )
                current_lines = []
            elif current_entry is not None:
                current_lines.append(line)

        # Save last entry
        if current_entry is not None:
            current_entry.content = "\n".join(current_lines).strip()
            current_entry.tags = self._parse_tags(current_entry.content)
            entries.append(current_entry)

        return entries

    async def get_daily_log(self, d: date | None = None) -> DailyMemory | None:
        """Get a daily memory log."""
        d = d or date.today()
        path = self._get_daily_path(d)

        if not path.exists():
            return None

        try:
            content = await asyncio.to_thread(path.read_text)
            entries = self._parse_daily_log(content, d)

            return DailyMemory(
                date=d,
                path=path,
                entries=entries,
                raw_content=content,
            )
        except Exception as e:
            logger.warning("Failed to read daily log", date=d.isoformat(), error=str(e))
            return None

    async def get_recent_logs(self, days: int = 7) -> list[DailyMemory]:
        """Get memory logs for the last N days."""
        logs = []
        today = date.today()

        for i in range(days):
            d = date.fromordinal(today.toordinal() - i)
            log = await self.get_daily_log(d)
            if log:
                logs.append(log)

        return logs

    async def write_entry(
        self,
        content: str,
        category: str = "Observation",
        tags: list[str] | None = None,
        source: str = "agent",
        sync_to_graph: bool = True,
    ) -> MemoryEntry:
        """
        Write a new memory entry to today's daily log.

        Args:
            content: The memory content
            category: Entry category (e.g., "Email Pattern", "Task Completion")
            tags: Optional explicit tags (auto-extracted from content too)
            source: Who created this (agent, user, system)
            sync_to_graph: Whether to sync to Neo4j

        Returns:
            The created MemoryEntry
        """
        now = datetime.now()
        today = now.date()

        entry = MemoryEntry(
            id=f"mem_{uuid.uuid4().hex[:12]}",
            timestamp=now,
            content=content,
            category=category,
            source=source,
            tags=tags or [],
        )

        # Add auto-extracted tags
        entry.tags.extend(self._parse_tags(content))
        entry.tags = list(set(entry.tags))  # Dedupe

        async with self._write_lock:
            path = self._get_daily_path(today)

            # Create file with header if it doesn't exist
            if not path.exists():
                header = f"## {today.strftime('%Y-%m-%d')}\n\n"
                await asyncio.to_thread(path.write_text, header)

            # Format entry
            time_str = now.strftime("%H:%M")
            tag_str = " ".join(f"#{t}" for t in entry.tags) if entry.tags else ""
            entry_text = f"\n### {time_str} - {category}\n{content}\n"
            if tag_str and tag_str not in content:
                entry_text += f"Tags: {tag_str}\n"

            # Append to file
            current = await asyncio.to_thread(path.read_text)
            await asyncio.to_thread(path.write_text, current + entry_text)

        logger.debug(
            "Wrote memory entry",
            category=category,
            tags=entry.tags,
        )

        # Sync to graph
        if sync_to_graph:
            try:
                await self._sync_entry_to_graph(entry)
            except Exception as e:
                logger.warning("Failed to sync memory entry to graph", error=str(e))

        return entry

    async def _sync_entry_to_graph(self, entry: MemoryEntry) -> None:
        """Sync a memory entry to Neo4j."""
        from cognitex.db.neo4j import get_neo4j_session

        async for session in get_neo4j_session():
            # Create MemoryEntry node
            await session.run(
                """
                MERGE (m:MemoryEntry {id: $id})
                SET m.timestamp = datetime($timestamp),
                    m.content = $content,
                    m.category = $category,
                    m.source = $source,
                    m.tags = $tags,
                    m.date = date($date)
                """,
                {
                    "id": entry.id,
                    "timestamp": entry.timestamp.isoformat(),
                    "content": entry.content,
                    "category": entry.category,
                    "source": entry.source,
                    "tags": entry.tags,
                    "date": entry.timestamp.date().isoformat(),
                },
            )

            # Link to Person nodes based on tags
            for tag in entry.tags:
                if tag.startswith("person/"):
                    person_ref = tag[7:]  # Remove "person/" prefix
                    await session.run(
                        """
                        MATCH (m:MemoryEntry {id: $mem_id})
                        MATCH (p:Person)
                        WHERE toLower(p.email) CONTAINS toLower($person_ref)
                           OR toLower(p.name) CONTAINS toLower($person_ref)
                        MERGE (m)-[:ABOUT]->(p)
                        """,
                        {"mem_id": entry.id, "person_ref": person_ref},
                    )
                elif tag.startswith("project/"):
                    project_ref = tag[8:]
                    await session.run(
                        """
                        MATCH (m:MemoryEntry {id: $mem_id})
                        MATCH (pr:Project)
                        WHERE toLower(pr.title) CONTAINS toLower($project_ref)
                           OR pr.id = $project_ref
                        MERGE (m)-[:ABOUT]->(pr)
                        """,
                        {"mem_id": entry.id, "project_ref": project_ref},
                    )
                elif tag.startswith("task/"):
                    task_ref = tag[5:]
                    await session.run(
                        """
                        MATCH (m:MemoryEntry {id: $mem_id})
                        MATCH (t:Task)
                        WHERE toLower(t.title) CONTAINS toLower($task_ref)
                           OR t.id = $task_ref
                        MERGE (m)-[:ABOUT]->(t)
                        """,
                        {"mem_id": entry.id, "task_ref": task_ref},
                    )
            break

    async def get_curated_memory(self) -> str:
        """Get the curated long-term memory content."""
        path = self.memory_dir / "MEMORY.md"

        if not path.exists():
            return ""

        try:
            return await asyncio.to_thread(path.read_text)
        except Exception as e:
            logger.warning("Failed to read curated memory", error=str(e))
            return ""

    async def save_curated_memory(self, content: str) -> bool:
        """Save curated memory content."""
        path = self.memory_dir / "MEMORY.md"

        try:
            await asyncio.to_thread(path.write_text, content)
            logger.info("Saved curated memory")
            return True
        except Exception as e:
            logger.error("Failed to save curated memory", error=str(e))
            return False

    async def promote_to_curated(self, entry_id: str) -> bool:
        """
        Promote a daily entry to curated memory.

        Appends the entry content to MEMORY.md.
        """
        # Find entry in recent logs
        logs = await self.get_recent_logs(days=30)
        entry = None

        for log in logs:
            for e in log.entries:
                if e.id == entry_id:
                    entry = e
                    break
            if entry:
                break

        if not entry:
            logger.warning("Entry not found for promotion", entry_id=entry_id)
            return False

        # Append to curated memory
        curated = await self.get_curated_memory()

        # Add entry under appropriate section or at end
        new_content = f"\n\n### {entry.timestamp.strftime('%Y-%m-%d')} - {entry.category}\n{entry.content}"
        curated += new_content

        return await self.save_curated_memory(curated)

    async def search_memories(
        self,
        query: str,
        days: int = 30,
        category: str | None = None,
    ) -> list[MemoryEntry]:
        """
        Search memory entries.

        Args:
            query: Search query (searches content and tags)
            days: How many days back to search
            category: Filter by category

        Returns:
            Matching entries
        """
        logs = await self.get_recent_logs(days=days)
        results = []
        query_lower = query.lower()

        for log in logs:
            for entry in log.entries:
                # Category filter
                if category and entry.category != category:
                    continue

                # Search in content and tags
                if query_lower in entry.content.lower():
                    results.append(entry)
                elif any(query_lower in tag.lower() for tag in entry.tags):
                    results.append(entry)

        return results

    async def get_context_for_prompt(self, max_entries: int = 10) -> str:
        """
        Get recent memory formatted for prompt injection.

        Returns today and yesterday's entries, plus curated memory.
        """
        sections = []

        # Recent entries
        logs = await self.get_recent_logs(days=2)
        recent_entries = []
        for log in logs:
            recent_entries.extend(log.entries)

        recent_entries.sort(key=lambda e: e.timestamp, reverse=True)
        recent_entries = recent_entries[:max_entries]

        if recent_entries:
            sections.append("## Recent Observations")
            for entry in recent_entries:
                time_str = entry.timestamp.strftime("%Y-%m-%d %H:%M")
                sections.append(f"- [{time_str}] {entry.category}: {entry.content[:150]}...")

        # Include relevant parts of curated memory
        curated = await self.get_curated_memory()
        if curated and len(curated) > 100:
            # Extract non-placeholder content
            curated_lines = []
            for line in curated.split("\n"):
                if line.strip() and not line.startswith("<!--") and not line.startswith("# Long-Term"):
                    curated_lines.append(line)

            if curated_lines:
                sections.append("\n## Long-Term Memory")
                sections.append("\n".join(curated_lines[:20]))  # Limit length

        return "\n".join(sections)

    async def get_entries_for_person(self, email: str, limit: int = 10) -> list[MemoryEntry]:
        """Get memory entries related to a person."""
        from cognitex.db.neo4j import get_neo4j_session

        entries = []

        async for session in get_neo4j_session():
            result = await session.run(
                """
                MATCH (m:MemoryEntry)-[:ABOUT]->(p:Person {email: $email})
                RETURN m.id as id, m.timestamp as timestamp, m.content as content,
                       m.category as category, m.source as source, m.tags as tags
                ORDER BY m.timestamp DESC
                LIMIT $limit
                """,
                {"email": email, "limit": limit},
            )
            records = await result.data()

            for r in records:
                entries.append(
                    MemoryEntry(
                        id=r["id"],
                        timestamp=r["timestamp"].to_native() if hasattr(r["timestamp"], "to_native") else r["timestamp"],
                        content=r["content"],
                        category=r["category"],
                        source=r["source"],
                        tags=r["tags"] or [],
                    )
                )
            break

        return entries


# Singleton instance
_memory_service: MemoryFileService | None = None


def get_memory_file_service() -> MemoryFileService:
    """Get or create the memory file service singleton."""
    global _memory_service
    if _memory_service is None:
        _memory_service = MemoryFileService()
    return _memory_service


async def init_memory_files() -> MemoryFileService:
    """Initialize the memory file system and return the service."""
    service = get_memory_file_service()
    await service.initialize()
    return service


__all__ = [
    "MemoryEntry",
    "DailyMemory",
    "MemoryFileService",
    "get_memory_file_service",
    "init_memory_files",
    "MEMORY_DIR",
]
