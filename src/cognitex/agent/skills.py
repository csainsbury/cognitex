"""Skills loader for teachable agent behaviors.

Implements OpenClaw-inspired skills pattern:
- Skills are markdown files that define specific behaviors
- Each skill has: Purpose, Rules, Examples
- User skills (~/.cognitex/skills/) override bundled skills (src/cognitex/skills/)
- Supports both AgentSkills YAML frontmatter format and legacy section-header format
"""

import asyncio
import hashlib
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import structlog
import yaml

logger = structlog.get_logger()

# Skill directories
BUNDLED_SKILLS_DIR = Path(__file__).parent.parent / "skills"
USER_SKILLS_DIR = Path.home() / ".cognitex" / "skills"


def parse_frontmatter(content: str) -> tuple[dict, str]:
    """Detect YAML frontmatter delimited by --- lines.

    Returns (frontmatter_dict, body). No frontmatter -> ({}, original content).
    """
    stripped = content.lstrip()
    if not stripped.startswith("---"):
        return {}, content

    # Find end delimiter
    first_newline = stripped.index("\n")
    rest = stripped[first_newline + 1 :]
    end_idx = rest.find("\n---")

    if end_idx == -1:
        return {}, content

    yaml_block = rest[:end_idx]
    # Body starts after the closing ---\n
    body_start = end_idx + 4  # len("\n---")
    body = rest[body_start:].lstrip("\n")

    try:
        frontmatter = yaml.safe_load(yaml_block)
        if not isinstance(frontmatter, dict):
            return {}, content
        return frontmatter, body
    except yaml.YAMLError:
        logger.warning("Malformed YAML frontmatter, falling back to legacy format")
        return {}, content


@dataclass
class SkillExample:
    """An example from a skill file."""

    input_text: str
    output_text: str
    notes: str | None = None


@dataclass
class Skill:
    """A parsed skill definition."""

    name: str
    path: Path
    purpose: str
    rules: list[str]
    examples: list[SkillExample]
    what_is: list[str] = field(default_factory=list)
    what_is_not: list[str] = field(default_factory=list)
    raw_content: str = ""
    is_user_skill: bool = False
    last_modified: datetime | None = None
    content_hash: str = ""
    # AgentSkills format fields
    version: str = "1.0.0"
    description: str = ""
    metadata: dict = field(default_factory=dict)
    format: Literal["agentskills", "cognitex_legacy"] = "cognitex_legacy"
    requires_bins: list[str] = field(default_factory=list)
    requires_env: list[str] = field(default_factory=list)
    requires_config: list[str] = field(default_factory=list)
    eligible: bool = True
    ineligibility_reason: str = ""
    source: Literal["bundled", "user", "community"] = "bundled"
    user_invocable: bool = False


class SkillsLoader:
    """
    Loads and caches skills from bundled and user directories.

    Skills define specific agent behaviors through:
    - Purpose: What the skill accomplishes
    - Rules: Explicit guidelines
    - Examples: Few-shot learning examples
    - What IS/NOT: Classification guidance

    User skills override bundled skills with the same name.
    Supports both AgentSkills YAML frontmatter and legacy section-header formats.
    """

    def __init__(
        self,
        bundled_dir: Path | None = None,
        user_dir: Path | None = None,
    ):
        self.bundled_dir = bundled_dir or BUNDLED_SKILLS_DIR
        self.user_dir = user_dir or USER_SKILLS_DIR
        self._cache: dict[str, Skill] = {}
        self._cache_lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Initialize skills directories if needed."""
        # Ensure user skills directory exists
        if not self.user_dir.exists():
            self.user_dir.mkdir(parents=True, exist_ok=True)
            logger.info("Created user skills directory", path=str(self.user_dir))

        # Bundled skills are created by separate script/deployment
        if not self.bundled_dir.exists():
            self.bundled_dir.mkdir(parents=True, exist_ok=True)
            logger.info("Created bundled skills directory", path=str(self.bundled_dir))

    def _compute_hash(self, content: str) -> str:
        """Compute content hash for cache invalidation."""
        return hashlib.md5(content.encode()).hexdigest()

    def _parse_skill_file(self, content: str, name: str, path: Path, is_user: bool) -> Skill:
        """Parse a SKILL.md file into a Skill object (dual-format dispatch)."""
        frontmatter, body = parse_frontmatter(content)
        if frontmatter and "name" in frontmatter:
            return self._parse_agentskills_format(frontmatter, body, content, name, path, is_user)
        else:
            return self._parse_legacy_format(content, name, path, is_user)

    def _parse_agentskills_format(
        self,
        frontmatter: dict,
        body: str,
        raw_content: str,
        name: str,
        path: Path,
        is_user: bool,
    ) -> Skill:
        """Parse AgentSkills YAML frontmatter format."""
        metadata = frontmatter.get("metadata", {})

        # Extract requires from metadata.cognitex.requires or metadata.openclaw.requires
        requires = {}
        for ns in ("cognitex", "openclaw"):
            ns_data = metadata.get(ns, {})
            if isinstance(ns_data, dict) and "requires" in ns_data:
                requires = ns_data["requires"]
                break

        # Parse purpose from body (first paragraph after title)
        purpose = frontmatter.get("description", "")
        if not purpose:
            for line in body.split("\n"):
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    purpose = stripped
                    break

        skill = Skill(
            name=frontmatter.get("name", name),
            path=path,
            purpose=purpose,
            rules=[],
            examples=[],
            raw_content=raw_content,
            is_user_skill=is_user,
            content_hash=self._compute_hash(raw_content),
            version=str(frontmatter.get("version", "1.0.0")),
            description=frontmatter.get("description", ""),
            metadata=metadata,
            format="agentskills",
            requires_bins=requires.get("bins", []) if isinstance(requires, dict) else [],
            requires_env=requires.get("env", []) if isinstance(requires, dict) else [],
            requires_config=requires.get("config", []) if isinstance(requires, dict) else [],
            user_invocable=bool(frontmatter.get("user-invocable", False)),
        )

        # Also parse the body with legacy parser for rules/what_is/examples
        self._parse_body_sections(skill, body)

        return skill

    def _parse_legacy_format(self, content: str, name: str, path: Path, is_user: bool) -> Skill:
        """Parse legacy section-header format (## Purpose, ## Rules, etc.)."""
        skill = Skill(
            name=name,
            path=path,
            purpose="",
            rules=[],
            examples=[],
            raw_content=content,
            is_user_skill=is_user,
            content_hash=self._compute_hash(content),
            format="cognitex_legacy",
        )

        self._parse_body_sections(skill, content)
        return skill

    def _parse_body_sections(self, skill: Skill, content: str) -> None:
        """Parse section headers from markdown body into skill fields."""
        current_section = None
        current_example_input = None
        current_example_output = []

        lines = content.split("\n")
        i = 0

        while i < len(lines):
            line = lines[i]

            # Section headers
            if line.startswith("## "):
                section = line[3:].strip().lower()
                current_section = section

                # Handle example blocks
                if section.startswith("example"):
                    current_example_input = None
                    current_example_output = []

            elif line.startswith("### "):
                # Subsection - often example input/output
                subsection = line[4:].strip().lower()
                if "email:" in subsection or "input:" in subsection:
                    current_example_input = (
                        subsection.split(":", 1)[1].strip() if ":" in subsection else ""
                    )
                elif "task" in subsection or "output" in subsection:
                    pass

            elif current_section:
                stripped = line.strip()

                if current_section == "purpose":
                    if stripped:
                        skill.purpose += " " + stripped if skill.purpose else stripped

                elif current_section in ("what is a task", "what is"):
                    if stripped.startswith("- "):
                        skill.what_is.append(stripped[2:])

                elif current_section in ("what is not a task", "what is not"):
                    if stripped.startswith("- "):
                        skill.what_is_not.append(stripped[2:])

                elif "rule" in current_section or current_section == "extraction rules":
                    if stripped.startswith(
                        ("- ", "1.", "2.", "3.", "4.", "5.", "6.", "7.", "8.", "9.")
                    ):
                        rule = stripped.lstrip("- 0123456789.").strip()
                        if rule:
                            skill.rules.append(rule)

                elif current_section.startswith("example"):
                    if stripped.startswith("Tasks:"):
                        pass
                    elif stripped.startswith("- [ ]") or stripped.startswith("- [x]"):
                        current_example_output.append(stripped)
                    elif stripped and current_example_input is None:
                        current_example_input = stripped

            i += 1

    def _check_eligibility(self, skill: Skill) -> None:
        """Check whether a skill's requirements are met."""
        reasons = []
        for bin_name in skill.requires_bins:
            if shutil.which(bin_name) is None:
                reasons.append(f"binary '{bin_name}' not found on PATH")
        for env_var in skill.requires_env:
            if not os.environ.get(env_var):
                reasons.append(f"env var '{env_var}' not set")
        # Config keys could be checked against get_settings() but we keep it
        # lightweight — just note them as requirements for now
        for config_key in skill.requires_config:
            reasons.append(f"config key '{config_key}' required (unchecked)")
        if reasons:
            skill.eligible = False
            skill.ineligibility_reason = "; ".join(reasons)

    def _determine_source(
        self, skill_dir: Path, is_user: bool
    ) -> Literal["bundled", "user", "community"]:
        """Determine skill source based on location and .community marker."""
        marker = skill_dir / ".community"
        if marker.exists():
            return "community"
        return "user" if is_user else "bundled"

    async def _load_skill(self, skill_dir: Path, is_user: bool) -> Skill | None:
        """Load a skill from its directory."""
        skill_file = skill_dir / "SKILL.md"

        if not skill_file.exists():
            return None

        try:
            content = await asyncio.to_thread(skill_file.read_text)
            stat = await asyncio.to_thread(skill_file.stat)

            skill = self._parse_skill_file(
                content=content,
                name=skill_dir.name,
                path=skill_dir,
                is_user=is_user,
            )
            skill.last_modified = datetime.fromtimestamp(stat.st_mtime)
            skill.source = self._determine_source(skill_dir, is_user)

            # Check eligibility for agentskills format
            if skill.format == "agentskills":
                self._check_eligibility(skill)

            return skill
        except Exception as e:
            logger.warning("Failed to load skill", skill=skill_dir.name, error=str(e))
            return None

    async def _discover_skills(self) -> dict[str, Path]:
        """
        Discover all skill directories.

        Returns dict of skill_name -> path, with user skills overriding bundled.
        """
        skills: dict[str, Path] = {}

        # Load bundled skills first
        if self.bundled_dir.exists():
            for path in self.bundled_dir.iterdir():
                if path.is_dir() and (path / "SKILL.md").exists():
                    skills[path.name] = path

        # User skills override bundled
        if self.user_dir.exists():
            for path in self.user_dir.iterdir():
                if path.is_dir() and (path / "SKILL.md").exists():
                    skills[path.name] = path

        return skills

    async def _is_cache_valid(self, name: str, path: Path) -> bool:
        """Check if cached skill is still valid."""
        if name not in self._cache:
            return False

        cached = self._cache[name]
        skill_file = path / "SKILL.md"

        if not skill_file.exists():
            return False

        try:
            stat = await asyncio.to_thread(skill_file.stat)
            return (
                cached.path == path
                and cached.last_modified is not None
                and stat.st_mtime <= cached.last_modified.timestamp()
            )
        except Exception:
            return False

    async def get_skill(self, name: str) -> Skill | None:
        """
        Get a skill by name.

        Checks user skills first (override), then bundled.
        """
        async with self._cache_lock:
            # Check user skills first
            user_path = self.user_dir / name
            if user_path.exists():
                if await self._is_cache_valid(name, user_path):
                    return self._cache[name]
                skill = await self._load_skill(user_path, is_user=True)
                if skill:
                    self._cache[name] = skill
                    return skill

            # Check bundled skills
            bundled_path = self.bundled_dir / name
            if bundled_path.exists():
                if await self._is_cache_valid(name, bundled_path):
                    return self._cache[name]
                skill = await self._load_skill(bundled_path, is_user=False)
                if skill:
                    self._cache[name] = skill
                    return skill

            return None

    async def list_skills(self) -> list[dict[str, Any]]:
        """List all available skills with metadata."""
        skill_paths = await self._discover_skills()
        skills = []

        for name in skill_paths:
            skill = await self.get_skill(name)
            if skill:
                skills.append(
                    {
                        "name": skill.name,
                        "purpose": (
                            skill.purpose[:100] + "..."
                            if len(skill.purpose) > 100
                            else skill.purpose
                        ),
                        "description": skill.description,
                        "rules_count": len(skill.rules),
                        "examples_count": len(skill.examples),
                        "is_user_skill": skill.is_user_skill,
                        "path": str(skill.path),
                        "format": skill.format,
                        "version": skill.version,
                        "eligible": skill.eligible,
                        "ineligibility_reason": skill.ineligibility_reason,
                        "source": skill.source,
                        "user_invocable": skill.user_invocable,
                    }
                )

        return sorted(skills, key=lambda x: x["name"])

    async def get_all_skills(self) -> dict[str, Skill]:
        """Get all available skills."""
        skill_paths = await self._discover_skills()
        result = {}

        for name in skill_paths:
            skill = await self.get_skill(name)
            if skill:
                result[name] = skill

        return result

    def format_skill_for_prompt(self, skill: Skill) -> str:
        """Format a skill for injection into a prompt.

        AgentSkills format: returns the markdown body directly.
        Legacy format: returns structured Purpose/Rules/Examples.
        """
        if skill.format == "agentskills":
            return self._format_agentskills_for_prompt(skill)
        return self._format_legacy_for_prompt(skill)

    def _format_agentskills_for_prompt(self, skill: Skill) -> str:
        """Format AgentSkills skill — return markdown body after frontmatter."""
        _, body = parse_frontmatter(skill.raw_content)
        return body

    def _format_legacy_for_prompt(self, skill: Skill) -> str:
        """Format legacy skill with structured sections."""
        sections = []

        if skill.purpose:
            sections.append(f"**Purpose:** {skill.purpose}")

        if skill.what_is:
            sections.append("\n**What IS:**")
            for item in skill.what_is:
                sections.append(f"- {item}")

        if skill.what_is_not:
            sections.append("\n**What is NOT:**")
            for item in skill.what_is_not:
                sections.append(f"- {item}")

        if skill.rules:
            sections.append("\n**Rules:**")
            for i, rule in enumerate(skill.rules, 1):
                sections.append(f"{i}. {rule}")

        if skill.examples:
            sections.append("\n**Examples:**")
            for ex in skill.examples[:3]:
                sections.append(f"\nInput: {ex.input_text}")
                sections.append(f"Output: {ex.output_text}")

        return "\n".join(sections)

    async def get_skill_prompt_section(self, skill_name: str) -> str:
        """Get a formatted prompt section for a specific skill."""
        skill = await self.get_skill(skill_name)
        if not skill:
            return ""
        return self.format_skill_for_prompt(skill)

    async def save_skill(
        self,
        name: str,
        content: str,
        create_if_missing: bool = True,
    ) -> bool:
        """
        Save a skill to the user skills directory.

        Args:
            name: Skill name (directory name)
            content: SKILL.md content
            create_if_missing: Create skill directory if it doesn't exist

        Returns:
            True if successful
        """
        skill_dir = self.user_dir / name
        skill_file = skill_dir / "SKILL.md"

        try:
            if not skill_dir.exists():
                if create_if_missing:
                    skill_dir.mkdir(parents=True)
                else:
                    return False

            await asyncio.to_thread(skill_file.write_text, content)

            # Invalidate cache
            async with self._cache_lock:
                self._cache.pop(name, None)

            logger.info("Saved skill", name=name)
            return True
        except Exception as e:
            logger.error("Failed to save skill", name=name, error=str(e))
            return False

    async def delete_skill(self, name: str) -> bool:
        """
        Delete a user skill.

        Only user skills can be deleted, not bundled skills.
        """
        skill_dir = self.user_dir / name

        if not skill_dir.exists():
            return False

        try:
            await asyncio.to_thread(shutil.rmtree, skill_dir)

            # Invalidate cache
            async with self._cache_lock:
                self._cache.pop(name, None)

            logger.info("Deleted skill", name=name)
            return True
        except Exception as e:
            logger.error("Failed to delete skill", name=name, error=str(e))
            return False


# Singleton instance
_skills_loader: SkillsLoader | None = None


def get_skills_loader() -> SkillsLoader:
    """Get or create the skills loader singleton."""
    global _skills_loader
    if _skills_loader is None:
        _skills_loader = SkillsLoader()
    return _skills_loader


async def init_skills() -> SkillsLoader:
    """Initialize the skills system and return the loader."""
    loader = get_skills_loader()
    await loader.initialize()
    return loader


__all__ = [
    "Skill",
    "SkillExample",
    "SkillsLoader",
    "get_skills_loader",
    "init_skills",
    "parse_frontmatter",
    "BUNDLED_SKILLS_DIR",
    "USER_SKILLS_DIR",
]
