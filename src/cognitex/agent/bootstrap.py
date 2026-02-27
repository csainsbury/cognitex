"""Bootstrap file loader for personality, identity, and context.

Implements OpenClaw-inspired bootstrap pattern with 7 files:
- SOUL.md - Core personality and voice
- USER.md - Operator profile (replaces IDENTITY.md)
- AGENTS.md - Operating constitution and safety boundaries
- TOOLS.md - Infrastructure and tool configuration
- MEMORY.md - Curated operational memory
- IDENTITY.md - Legacy fallback (superseded by USER.md)
- CONTEXT.md - Agent-maintained ambient context

Files are loaded from ~/.cognitex/bootstrap/ with file-watch invalidation.
"""

import asyncio
import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()

# Default bootstrap directory
BOOTSTRAP_DIR = Path.home() / ".cognitex" / "bootstrap"

# Registry of all bootstrap files
BOOTSTRAP_FILES = {
    "SOUL.md": {"writable_by_agent": False, "required": True},
    "USER.md": {"writable_by_agent": False, "required": True},
    "AGENTS.md": {"writable_by_agent": False, "required": True},
    "TOOLS.md": {"writable_by_agent": False, "required": False},
    "MEMORY.md": {"writable_by_agent": True, "required": False},
    "IDENTITY.md": {"writable_by_agent": False, "required": False},  # Legacy fallback
    "CONTEXT.md": {"writable_by_agent": True, "required": False},
    "LEDGER.yaml": {"writable_by_agent": True, "required": False},
}

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

DEFAULT_USER = """## About Me
<!-- Fill in your details -->
- Role: [FILL]
- Company/Organization: [FILL]
- Key projects: [FILL]
- Communication preferences: [FILL]

## Key Relationships
<!-- Who you work with frequently -->
- [FILL] - [Role/Context]

## Current Priorities
<!-- What matters this week/month -->
- [FILL]

## Context Packs
<!-- Rules for automatic context pack generation -->
- Generate context packs for all meetings by default
"""

DEFAULT_AGENTS = """## 1. Core Mission
Act as a digital twin that advances the user's work, responds in their voice,
and maintains their knowledge graph.

## 2. Operating Principles
- Lead with action, flag only genuine ambiguity
- Respect calendar boundaries and energy state
- Never fabricate information; say "I don't know" when uncertain

## 3. Communication Standards
- Match the user's voice from SOUL.md
- Keep notifications concise and actionable
- Batch low-priority updates

## 4. Decision Authority
- AUTO: Graph linking, context pack compilation, low-risk email drafts
- APPROVAL: Sending emails, creating calendar events, task deletion
- NEVER: Financial actions, sharing private data, contacting people not in graph

## 5. Safety and Action Boundaries
- Never send emails without user approval
- Never delete data without explicit confirmation
- Never share personal or medical information
- Never take actions outside the user's defined tool set
- Rate-limit outbound actions: max 5 emails/hour, max 10 notifications/hour
- All drafted content must be reviewed before sending
"""

DEFAULT_TOOLS = """## Infrastructure
<!-- Fill in your tool and infrastructure details -->
- Primary email: [FILL]
- Calendar system: [FILL]

## SSH Hosts
<!-- Remote machines the agent can reference -->
- [FILL]

## API Keys & Services
<!-- Services available to the agent (not the keys themselves) -->
- [FILL]
"""

DEFAULT_MEMORY = """## Key Decisions
<!-- Curated decisions and their rationale -->
_No decisions recorded yet._

## Recurring Patterns
<!-- Patterns the agent has observed -->
_No patterns recorded yet._

## User Preferences Learned
<!-- Preferences discovered through interaction -->
_No preferences recorded yet._
"""

DEFAULT_LEDGER = """# Commitment Ledger
# Auto-managed by the agent — tracks promises, deadlines, and follow-ups.
commitments: []
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
    - USER.md: Operator profile (replaces IDENTITY.md)
    - AGENTS.md: Operating constitution and safety boundaries
    - TOOLS.md: Infrastructure and tool configuration
    - MEMORY.md: Curated operational memory
    - IDENTITY.md: Legacy fallback (superseded by USER.md)
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
            "USER.md": DEFAULT_USER,
            "AGENTS.md": DEFAULT_AGENTS,
            "TOOLS.md": DEFAULT_TOOLS,
            "MEMORY.md": DEFAULT_MEMORY,
            "IDENTITY.md": DEFAULT_IDENTITY,
            "CONTEXT.md": DEFAULT_CONTEXT,
            "LEDGER.yaml": DEFAULT_LEDGER,
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
                name=Path(filename).stem.upper(),
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

    async def get_user(self) -> BootstrapFile | None:
        """Get USER.md - operator profile. Falls back to IDENTITY.md."""
        user = await self.get_file("USER.md")
        if user:
            return user
        return await self.get_identity()

    async def get_agents(self) -> BootstrapFile | None:
        """Get AGENTS.md - operating constitution."""
        return await self.get_file("AGENTS.md")

    async def get_tools(self) -> BootstrapFile | None:
        """Get TOOLS.md - infrastructure and tool configuration."""
        return await self.get_file("TOOLS.md")

    async def get_memory_file(self) -> BootstrapFile | None:
        """Get MEMORY.md - curated operational memory."""
        return await self.get_file("MEMORY.md")

    async def get_all(self) -> dict[str, BootstrapFile]:
        """Get all bootstrap files."""
        result = {}
        for filename in BOOTSTRAP_FILES:
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
        if filename not in BOOTSTRAP_FILES:
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

    def _has_real_content(self, content: str) -> bool:
        """Check if content has real user data (not just [FILL] placeholders)."""
        lines = content.strip().split("\n")
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("#") or stripped.startswith("<!--"):
                continue
            if "[FILL]" in stripped:
                continue
            if stripped.startswith("_No ") and stripped.endswith("_"):
                continue
            if stripped.startswith("-") and "[FILL]" in stripped:
                continue
            # Found a line with real content
            return True
        return False

    def _extract_sections_by_name(
        self, bf: BootstrapFile, match_terms: list[str]
    ) -> tuple[list[BootstrapSection], list[BootstrapSection]]:
        """Split sections into matched and unmatched by name keywords."""
        matched = []
        unmatched = []
        for section in bf.sections:
            name_lower = section.name.lower()
            if any(term in name_lower for term in match_terms):
                matched.append(section)
            else:
                unmatched.append(section)
        return matched, unmatched

    def format_for_prompt(self, files: dict[str, BootstrapFile]) -> str:
        """
        Format bootstrap files for injection into system prompt.

        Differential injection order:
        1. SOUL.md → full injection as "Communication Style & Voice"
        2. AGENTS.md → safety sections full, other sections summarised
        3. USER.md (fallback: IDENTITY) → skip [FILL] sections
        4. MEMORY.md → full injection as "Operational Memory"
        5. TOOLS.md → non-placeholder sections as "Tools & Infrastructure"
        6. CONTEXT.md → full injection as "Current Context"
        """
        parts = []

        # 1. SOUL — full injection
        if "SOUL" in files:
            parts.append("## Communication Style & Voice\n")
            parts.append(files["SOUL"].raw_content.strip())
            parts.append("")

        # 2. AGENTS — safety sections full, others summarised (first line each)
        if "AGENTS" in files:
            agents = files["AGENTS"]
            safety, others = self._extract_sections_by_name(agents, ["safety", "boundar"])
            if safety:
                parts.append("## Operating Constitution\n")
                for s in safety:
                    parts.append(f"### {s.name}")
                    parts.append(s.content.strip())
                    parts.append("")
            if others:
                if not safety:
                    parts.append("## Operating Constitution\n")
                for s in others:
                    # Summarise: first non-empty line only
                    first_line = ""
                    for line in s.content.strip().split("\n"):
                        stripped = line.strip()
                        if stripped and not stripped.startswith("<!--"):
                            first_line = stripped
                            break
                    if first_line:
                        parts.append(f"- **{s.name}**: {first_line}")
                parts.append("")

        # 3. USER (fallback: IDENTITY) — skip [FILL] placeholder sections
        user_file = files.get("USER") or files.get("IDENTITY")
        if user_file:
            # Filter sections that have real content
            real_sections = [s for s in user_file.sections if self._has_real_content(s.content)]
            if real_sections:
                parts.append("## Operator Profile\n")
                for s in real_sections:
                    parts.append(f"### {s.name}")
                    parts.append(s.content.strip())
                    parts.append("")

        # 4. MEMORY — full injection if real content
        if "MEMORY" in files:
            memory = files["MEMORY"]
            if self._has_real_content(memory.raw_content):
                parts.append("## Operational Memory\n")
                parts.append(memory.raw_content.strip())
                parts.append("")

        # 5. TOOLS — non-placeholder sections
        if "TOOLS" in files:
            tools = files["TOOLS"]
            real_sections = [s for s in tools.sections if self._has_real_content(s.content)]
            if real_sections:
                parts.append("## Tools & Infrastructure\n")
                for s in real_sections:
                    parts.append(f"### {s.name}")
                    parts.append(s.content.strip())
                    parts.append("")

        # 6. CONTEXT — full injection (existing _No check)
        if "CONTEXT" in files:
            context = files["CONTEXT"]
            if "_No " not in context.raw_content:
                parts.append("## Current Context\n")
                parts.append(context.raw_content.strip())
                parts.append("")

        return "\n".join(parts)

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

    async def get_safety_rules(self) -> str:
        """
        Extract safety section from AGENTS.md for injection into LLM calls.

        Returns formatted string of safety rules, or empty string if not available.
        """
        agents = await self.get_agents()
        if not agents:
            return ""

        safety_sections, _ = self._extract_sections_by_name(agents, ["safety", "boundar"])
        if not safety_sections:
            return ""

        parts = []
        for s in safety_sections:
            parts.append(f"### {s.name}")
            parts.append(s.content.strip())
            parts.append("")

        return "\n".join(parts).strip()

    async def should_skip_context_pack(self, event_title: str) -> tuple[bool, str | None]:
        """
        Check if an event should skip context pack generation based on USER.md rules.

        Looks for "Context Packs" section in USER.md (falls back to IDENTITY.md) for skip rules like:
        - "do not need to create these for clinical events"
        - "do not need to create context packs for Lunch events"

        Args:
            event_title: The calendar event title to check

        Returns:
            Tuple of (should_skip, reason) - reason is None if not skipping
        """
        user = await self.get_user()
        if not user:
            return False, None

        # Find Context Packs section
        context_pack_section = None
        for section in user.sections:
            if "context pack" in section.name.lower():
                context_pack_section = section
                break

        if not context_pack_section:
            return False, None

        title_lower = event_title.lower()

        # Parse skip rules from the content
        skip_patterns = []

        # Look for patterns like "do not need to create" or "don't need"
        lines = context_pack_section.content.split("\n")
        for line in lines:
            line_lower = line.lower()
            if "do not need" in line_lower or "don't need" in line_lower or "skip" in line_lower:
                # Extract keywords from this line
                # Common patterns: clinical, MDT, lunch, prep for
                if "clinical" in line_lower:
                    skip_patterns.append(("clinic", "clinical event"))
                    skip_patterns.append(("mdt", "clinical event (MDT)"))
                if "mdt" in line_lower and "clinical" not in line_lower:
                    skip_patterns.append(("mdt", "MDT"))
                if "lunch" in line_lower:
                    skip_patterns.append(("lunch", "lunch event"))
                if "prep for mdt" in line_lower or "prep for" in line_lower:
                    skip_patterns.append(("prep for", "prep event"))
                if "diabetes" in line_lower:
                    skip_patterns.append(("diabetes", "diabetes clinic"))
                if "type 1" in line_lower:
                    skip_patterns.append(("type 1", "Type 1 clinic"))
                if "type 2" in line_lower:
                    skip_patterns.append(("type 2", "Type 2 clinic"))

        # Check if event title matches any skip pattern
        for pattern, reason in skip_patterns:
            if pattern in title_lower:
                logger.debug(
                    "Skipping context pack per USER.md rules",
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
    "BOOTSTRAP_DIR",
    "BOOTSTRAP_FILES",
    "BootstrapFile",
    "BootstrapLoader",
    "BootstrapSection",
    "get_bootstrap_loader",
    "init_bootstrap",
]
