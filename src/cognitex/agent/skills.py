"""Skills loader for teachable agent behaviors.

Implements OpenClaw-inspired skills pattern:
- Skills are markdown files that define specific behaviors
- Each skill has: Purpose, Rules, Examples
- User skills (~/.cognitex/skills/) override bundled skills (src/cognitex/skills/)
"""

import asyncio
import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()

# Skill directories
BUNDLED_SKILLS_DIR = Path(__file__).parent.parent / "skills"
USER_SKILLS_DIR = Path.home() / ".cognitex" / "skills"


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
    what_is: list[str] = field(default_factory=list)  # What IS a thing
    what_is_not: list[str] = field(default_factory=list)  # What is NOT
    raw_content: str = ""
    is_user_skill: bool = False
    last_modified: datetime | None = None
    content_hash: str = ""


class SkillsLoader:
    """
    Loads and caches skills from bundled and user directories.

    Skills define specific agent behaviors through:
    - Purpose: What the skill accomplishes
    - Rules: Explicit guidelines
    - Examples: Few-shot learning examples
    - What IS/NOT: Classification guidance

    User skills override bundled skills with the same name.
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
        """Parse a SKILL.md file into a Skill object."""
        skill = Skill(
            name=name,
            path=path,
            purpose="",
            rules=[],
            examples=[],
            raw_content=content,
            is_user_skill=is_user,
            content_hash=self._compute_hash(content),
        )

        current_section = None
        current_example_input = None
        current_example_output = []
        in_example = False

        lines = content.split("\n")
        i = 0

        while i < len(lines):
            line = lines[i]

            # Section headers
            if line.startswith("## "):
                section = line[3:].strip().lower()
                current_section = section
                in_example = False

                # Handle example blocks
                if section.startswith("example"):
                    in_example = True
                    current_example_input = None
                    current_example_output = []

            elif line.startswith("### "):
                # Subsection - often example input/output
                subsection = line[4:].strip().lower()
                if "email:" in subsection or "input:" in subsection:
                    # Start of example input
                    current_example_input = subsection.split(":", 1)[1].strip() if ":" in subsection else ""
                elif "task" in subsection or "output" in subsection:
                    # Start of example output
                    pass

            elif current_section:
                stripped = line.strip()

                if current_section == "purpose":
                    if stripped:
                        skill.purpose += " " + stripped if skill.purpose else stripped

                elif current_section == "what is a task" or current_section == "what is":
                    if stripped.startswith("- "):
                        skill.what_is.append(stripped[2:])

                elif current_section == "what is not a task" or current_section == "what is not":
                    if stripped.startswith("- "):
                        skill.what_is_not.append(stripped[2:])

                elif "rule" in current_section or current_section == "extraction rules":
                    if stripped.startswith(("- ", "1.", "2.", "3.", "4.", "5.", "6.", "7.", "8.", "9.")):
                        # Strip leading number/bullet
                        rule = stripped.lstrip("- 0123456789.").strip()
                        if rule:
                            skill.rules.append(rule)

                elif current_section.startswith("example"):
                    # Accumulate example content
                    if stripped.startswith("Tasks:"):
                        # Output section
                        pass
                    elif stripped.startswith("- [ ]") or stripped.startswith("- [x]"):
                        current_example_output.append(stripped)
                    elif stripped and current_example_input is None:
                        current_example_input = stripped

            i += 1

        return skill

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

        for name, path in skill_paths.items():
            skill = await self.get_skill(name)
            if skill:
                skills.append({
                    "name": skill.name,
                    "purpose": skill.purpose[:100] + "..." if len(skill.purpose) > 100 else skill.purpose,
                    "rules_count": len(skill.rules),
                    "examples_count": len(skill.examples),
                    "is_user_skill": skill.is_user_skill,
                    "path": str(skill.path),
                })

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
        """
        Format a skill for injection into a prompt.

        Provides structured guidance based on the skill definition.
        """
        sections = []

        # Purpose
        if skill.purpose:
            sections.append(f"**Purpose:** {skill.purpose}")

        # What IS / IS NOT
        if skill.what_is:
            sections.append("\n**What IS:**")
            for item in skill.what_is:
                sections.append(f"- {item}")

        if skill.what_is_not:
            sections.append("\n**What is NOT:**")
            for item in skill.what_is_not:
                sections.append(f"- {item}")

        # Rules
        if skill.rules:
            sections.append("\n**Rules:**")
            for i, rule in enumerate(skill.rules, 1):
                sections.append(f"{i}. {rule}")

        # Examples (if any parsed)
        if skill.examples:
            sections.append("\n**Examples:**")
            for ex in skill.examples[:3]:  # Limit to 3
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
            import shutil

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
    "BUNDLED_SKILLS_DIR",
    "USER_SKILLS_DIR",
]
