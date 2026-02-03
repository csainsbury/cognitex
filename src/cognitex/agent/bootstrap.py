"""Bootstrap file loader for personality, identity, and context.

Implements OpenClaw-inspired bootstrap pattern:
- SOUL.md - Core personality and voice
- IDENTITY.md - User context and preferences
- CONTEXT.md - Agent-maintained ambient context

Files are loaded from ~/.cognitex/bootstrap/ with file-watch invalidation.
"""

import asyncio
import hashlib
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()

# Default bootstrap directory
BOOTSTRAP_DIR = Path.home() / ".cognitex" / "bootstrap"

# Default file templates
DEFAULT_SOUL = """## Communication Style
- Direct but warm, never corporate-speak
- Lead with the answer, then context if needed
- Short sentences, active voice
- Use "I" not "we" for personal emails

## Email Voice
- Greeting: First name only ("Hi Sarah,")
- Sign-off: Just name, no "Best regards"
- Length: 2-4 short paragraphs max
- Never use: "I hope this email finds you well", "Please don't hesitate"

## Tone by Context
- Work requests: Collaborative, not commanding
- Declining: Honest but kind, offer alternative if possible
- Following up: Assume good intent, brief reminder
"""

DEFAULT_IDENTITY = """## About Me
<!-- Fill in your details -->
- Role: [Your role/title]
- Company/Organization: [Your company]
- Key projects: [What you're working on]
- Communication preferences: [How you prefer to communicate]

## Key Relationships
<!-- Who you work with frequently -->
- [Name] - [Role/Context]

## Current Priorities
<!-- What matters this week/month -->
- [Priority 1]
- [Priority 2]
"""

DEFAULT_CONTEXT = """## Recent Activity
<!-- Auto-updated by agent -->
_No recent activity recorded yet._

## Open Threads
<!-- Auto-updated: conversations requiring attention -->
_No open threads._

## Upcoming Deadlines
<!-- Auto-updated from calendar/tasks -->
_No upcoming deadlines._

## Relationship Notes
<!-- Auto-updated: recent interactions -->
_No recent interactions recorded._
"""


@dataclass
class BootstrapSection:
    """A section from a bootstrap file."""

    name: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class BootstrapFile:
    """Parsed bootstrap file."""

    path: Path
    name: str
    raw_content: str
    sections: list[BootstrapSection]
    last_modified: datetime
    content_hash: str


class BootstrapLoader:
    """
    Loads and caches bootstrap files with file-watch invalidation.

    Bootstrap files are markdown files that define:
    - SOUL.md: Communication style and voice
    - IDENTITY.md: User context and preferences
    - CONTEXT.md: Ambient context (agent-maintained)
    """

    def __init__(self, bootstrap_dir: Path | None = None):
        self.bootstrap_dir = bootstrap_dir or BOOTSTRAP_DIR
        self._cache: dict[str, BootstrapFile] = {}
        self._cache_lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Initialize bootstrap directory with default files if needed."""
        if not self.bootstrap_dir.exists():
            self.bootstrap_dir.mkdir(parents=True, exist_ok=True)
            logger.info("Created bootstrap directory", path=str(self.bootstrap_dir))

        # Create default files if they don't exist
        defaults = {
            "SOUL.md": DEFAULT_SOUL,
            "IDENTITY.md": DEFAULT_IDENTITY,
            "CONTEXT.md": DEFAULT_CONTEXT,
        }

        for filename, content in defaults.items():
            filepath = self.bootstrap_dir / filename
            if not filepath.exists():
                filepath.write_text(content)
                logger.info("Created default bootstrap file", file=filename)

    def _compute_hash(self, content: str) -> str:
        """Compute content hash for cache invalidation."""
        return hashlib.md5(content.encode()).hexdigest()

    def _parse_sections(self, content: str) -> list[BootstrapSection]:
        """Parse markdown into sections based on ## headers."""
        sections = []
        current_section = None
        current_content = []

        for line in content.split("\n"):
            if line.startswith("## "):
                # Save previous section
                if current_section is not None:
                    sections.append(
                        BootstrapSection(
                            name=current_section,
                            content="\n".join(current_content).strip(),
                        )
                    )
                # Start new section
                current_section = line[3:].strip()
                current_content = []
            else:
                current_content.append(line)

        # Save last section
        if current_section is not None:
            sections.append(
                BootstrapSection(
                    name=current_section,
                    content="\n".join(current_content).strip(),
                )
            )

        return sections

    async def _load_file(self, filename: str) -> BootstrapFile | None:
        """Load and parse a single bootstrap file."""
        filepath = self.bootstrap_dir / filename

        if not filepath.exists():
            return None

        try:
            content = await asyncio.to_thread(filepath.read_text)
            stat = await asyncio.to_thread(filepath.stat)

            return BootstrapFile(
                path=filepath,
                name=filename.replace(".md", "").upper(),
                raw_content=content,
                sections=self._parse_sections(content),
                last_modified=datetime.fromtimestamp(stat.st_mtime),
                content_hash=self._compute_hash(content),
            )
        except Exception as e:
            logger.warning("Failed to load bootstrap file", file=filename, error=str(e))
            return None

    async def _is_cache_valid(self, filename: str) -> bool:
        """Check if cached version is still valid."""
        if filename not in self._cache:
            return False

        cached = self._cache[filename]
        filepath = self.bootstrap_dir / filename

        if not filepath.exists():
            return False

        try:
            stat = await asyncio.to_thread(filepath.stat)
            return stat.st_mtime <= cached.last_modified.timestamp()
        except Exception:
            return False

    async def get_file(self, filename: str) -> BootstrapFile | None:
        """Get a bootstrap file, using cache if valid."""
        async with self._cache_lock:
            if await self._is_cache_valid(filename):
                return self._cache[filename]

            loaded = await self._load_file(filename)
            if loaded:
                self._cache[filename] = loaded
            return loaded

    async def get_soul(self) -> BootstrapFile | None:
        """Get SOUL.md - communication style and voice."""
        return await self.get_file("SOUL.md")

    async def get_identity(self) -> BootstrapFile | None:
        """Get IDENTITY.md - user context and preferences."""
        return await self.get_file("IDENTITY.md")

    async def get_context(self) -> BootstrapFile | None:
        """Get CONTEXT.md - ambient context."""
        return await self.get_file("CONTEXT.md")

    async def get_all(self) -> dict[str, BootstrapFile]:
        """Get all bootstrap files."""
        result = {}
        for filename in ["SOUL.md", "IDENTITY.md", "CONTEXT.md"]:
            loaded = await self.get_file(filename)
            if loaded:
                result[loaded.name] = loaded
        return result

    async def update_context(self, section_updates: dict[str, str]) -> bool:
        """
        Update specific sections in CONTEXT.md (agent-maintained).

        Args:
            section_updates: Dict of section_name -> new_content

        Returns:
            True if successful
        """
        context_file = await self.get_context()

        if not context_file:
            # Create with defaults first
            await self.initialize()
            context_file = await self.get_context()
            if not context_file:
                return False

        # Update sections
        sections_dict = {s.name: s.content for s in context_file.sections}
        sections_dict.update(section_updates)

        # Rebuild content
        new_content = ""
        for name, content in sections_dict.items():
            new_content += f"## {name}\n{content}\n\n"

        try:
            filepath = self.bootstrap_dir / "CONTEXT.md"
            await asyncio.to_thread(filepath.write_text, new_content.strip())

            # Invalidate cache
            async with self._cache_lock:
                self._cache.pop("CONTEXT.md", None)

            logger.debug("Updated CONTEXT.md", sections=list(section_updates.keys()))
            return True
        except Exception as e:
            logger.error("Failed to update CONTEXT.md", error=str(e))
            return False

    async def save_file(self, filename: str, content: str) -> bool:
        """
        Save content to a bootstrap file.

        Args:
            filename: File name (e.g., "SOUL.md")
            content: New file content

        Returns:
            True if successful
        """
        if filename not in ["SOUL.md", "IDENTITY.md", "CONTEXT.md"]:
            logger.error("Invalid bootstrap filename", filename=filename)
            return False

        try:
            filepath = self.bootstrap_dir / filename
            await asyncio.to_thread(filepath.write_text, content)

            # Invalidate cache
            async with self._cache_lock:
                self._cache.pop(filename, None)

            logger.info("Saved bootstrap file", file=filename)
            return True
        except Exception as e:
            logger.error("Failed to save bootstrap file", file=filename, error=str(e))
            return False

    def format_for_prompt(self, files: dict[str, BootstrapFile]) -> str:
        """
        Format bootstrap files for injection into system prompt.

        Args:
            files: Dict of name -> BootstrapFile

        Returns:
            Formatted string for system prompt
        """
        sections = []

        # SOUL first (communication style)
        if "SOUL" in files:
            soul = files["SOUL"]
            sections.append("## Communication Style & Voice\n")
            sections.append(soul.raw_content.strip())
            sections.append("")

        # IDENTITY second (user context)
        if "IDENTITY" in files:
            identity = files["IDENTITY"]
            # Filter out placeholder content
            content = identity.raw_content
            if "[Your role" not in content and "[Your company" not in content:
                sections.append("## User Context\n")
                sections.append(content.strip())
                sections.append("")

        # CONTEXT last (ambient state)
        if "CONTEXT" in files:
            context = files["CONTEXT"]
            # Only include if there's real content (not just placeholders)
            if "_No " not in context.raw_content:
                sections.append("## Current Context\n")
                sections.append(context.raw_content.strip())
                sections.append("")

        return "\n".join(sections)

    async def get_formatted_prompt_section(self) -> str:
        """Get all bootstrap files formatted for prompt injection."""
        files = await self.get_all()
        if not files:
            return ""
        return self.format_for_prompt(files)

    async def get_voice_guidance(self) -> str:
        """
        Get voice guidance from SOUL.md for email drafting.

        Returns formatted guidance or empty string if not available.
        """
        soul = await self.get_soul()
        if not soul:
            return ""

        # Extract Email Voice section if present
        for section in soul.sections:
            if "email" in section.name.lower() or "voice" in section.name.lower():
                return f"Writing style (from user preferences):\n{section.content}"

        # Fall back to full SOUL content
        return f"Writing style:\n{soul.raw_content.strip()}"

    async def should_skip_context_pack(self, event_title: str) -> tuple[bool, str | None]:
        """
        Check if an event should skip context pack generation based on IDENTITY.md rules.

        Looks for "Context Packs" section in IDENTITY.md for skip rules like:
        - "do not need to create these for clinical events"
        - "do not need to create context packs for Lunch events"

        Args:
            event_title: The calendar event title to check

        Returns:
            Tuple of (should_skip, reason) - reason is None if not skipping
        """
        identity = await self.get_identity()
        if not identity:
            return False, None

        # Find Context Packs section
        context_pack_section = None
        for section in identity.sections:
            if "context pack" in section.name.lower():
                context_pack_section = section
                break

        if not context_pack_section:
            return False, None

        content_lower = context_pack_section.content.lower()
        title_lower = event_title.lower()

        # Parse skip rules from the content
        skip_patterns = []

        # Look for patterns like "do not need to create" or "don't need"
        lines = context_pack_section.content.split('\n')
        for line in lines:
            line_lower = line.lower()
            if 'do not need' in line_lower or "don't need" in line_lower or 'skip' in line_lower:
                # Extract keywords from this line
                # Common patterns: clinical, MDT, lunch, prep for
                if 'clinical' in line_lower:
                    skip_patterns.append(('clinic', 'clinical event'))
                    skip_patterns.append(('mdt', 'clinical event (MDT)'))
                if 'mdt' in line_lower and 'clinical' not in line_lower:
                    skip_patterns.append(('mdt', 'MDT'))
                if 'lunch' in line_lower:
                    skip_patterns.append(('lunch', 'lunch event'))
                if 'prep for mdt' in line_lower or 'prep for' in line_lower:
                    skip_patterns.append(('prep for', 'prep event'))
                if 'diabetes' in line_lower:
                    skip_patterns.append(('diabetes', 'diabetes clinic'))
                if 'type 1' in line_lower:
                    skip_patterns.append(('type 1', 'Type 1 clinic'))
                if 'type 2' in line_lower:
                    skip_patterns.append(('type 2', 'Type 2 clinic'))

        # Check if event title matches any skip pattern
        for pattern, reason in skip_patterns:
            if pattern in title_lower:
                logger.debug(
                    "Skipping context pack per IDENTITY.md rules",
                    event=event_title[:30],
                    reason=reason,
                )
                return True, reason

        return False, None


# Singleton instance
_bootstrap_loader: BootstrapLoader | None = None


def get_bootstrap_loader() -> BootstrapLoader:
    """Get or create the bootstrap loader singleton."""
    global _bootstrap_loader
    if _bootstrap_loader is None:
        _bootstrap_loader = BootstrapLoader()
    return _bootstrap_loader


async def init_bootstrap() -> BootstrapLoader:
    """Initialize the bootstrap system and return the loader."""
    loader = get_bootstrap_loader()
    await loader.initialize()
    return loader


__all__ = [
    "BootstrapLoader",
    "BootstrapFile",
    "BootstrapSection",
    "get_bootstrap_loader",
    "init_bootstrap",
    "BOOTSTRAP_DIR",
]
