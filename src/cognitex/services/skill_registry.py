"""Community skill registry client.

Syncs, searches, installs, and updates skills from the OpenClaw community
skill registry (a git repository of AgentSkills-format SKILL.md files).
"""

import asyncio
import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import structlog

from cognitex.agent.skills import USER_SKILLS_DIR, parse_frontmatter

logger = structlog.get_logger()

SKILLS_REPO_URL = "https://github.com/openclaw/skills.git"
CACHE_DIR = Path.home() / ".cognitex" / "cache" / "community-skills"


@dataclass
class SkillListing:
    """A skill available in the community registry."""

    name: str
    slug: str
    description: str
    version: str
    path: Path
    installed: bool


async def _run_git(*args: str, cwd: Path | None = None) -> tuple[int, str]:
    """Run a git command asynchronously. Returns (returncode, output)."""
    if shutil.which("git") is None:
        raise RuntimeError("git is not installed. Install git to use the community skill registry.")

    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    return proc.returncode or 0, stdout.decode().strip()


class SkillRegistry:
    """Community skill registry — clone/sync a git repo of shared skills."""

    def __init__(
        self,
        repo_url: str = SKILLS_REPO_URL,
        cache_dir: Path | None = None,
        user_skills_dir: Path | None = None,
    ):
        self.repo_url = repo_url
        self.cache_dir = cache_dir or CACHE_DIR
        self.user_skills_dir = user_skills_dir or USER_SKILLS_DIR
        self._listings_cache: list[SkillListing] | None = None

    async def sync_registry(self) -> int:
        """Clone or pull the community skills repository. Returns skill count."""
        if (self.cache_dir / ".git").exists():
            code, output = await _run_git("pull", "--ff-only", cwd=self.cache_dir)
            if code != 0:
                logger.warning("git pull failed, re-cloning", output=output)
                shutil.rmtree(self.cache_dir)
            else:
                self._listings_cache = None
                listings = await self._scan_registry()
                return len(listings)

        self.cache_dir.parent.mkdir(parents=True, exist_ok=True)
        code, output = await _run_git("clone", "--depth", "1", self.repo_url, str(self.cache_dir))
        if code != 0:
            raise RuntimeError(f"Failed to clone skill registry: {output}")

        self._listings_cache = None
        listings = await self._scan_registry()
        return len(listings)

    async def _scan_registry(self) -> list[SkillListing]:
        """Scan the cached repo for skills with SKILL.md files."""
        if self._listings_cache is not None:
            return self._listings_cache

        listings: list[SkillListing] = []

        if not self.cache_dir.exists():
            return listings

        installed_slugs = (
            {
                d.name
                for d in self.user_skills_dir.iterdir()
                if d.is_dir() and (d / ".community").exists()
            }
            if self.user_skills_dir.exists()
            else set()
        )

        for entry in sorted(self.cache_dir.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            skill_file = entry / "SKILL.md"
            if not skill_file.exists():
                continue

            content = skill_file.read_text()
            frontmatter, _ = parse_frontmatter(content)

            name = frontmatter.get("name", entry.name)
            slug = entry.name

            listings.append(
                SkillListing(
                    name=name,
                    slug=slug,
                    description=frontmatter.get("description", ""),
                    version=str(frontmatter.get("version", "1.0.0")),
                    path=entry,
                    installed=slug in installed_slugs,
                )
            )

        self._listings_cache = listings
        return listings

    async def search(self, query: str) -> list[SkillListing]:
        """Search community registry by name/description (case-insensitive substring)."""
        listings = await self._scan_registry()
        q = query.lower()
        return [
            listing
            for listing in listings
            if q in listing.name.lower() or q in listing.description.lower()
        ]

    async def install(self, slug: str) -> bool:
        """Install a community skill into user skills directory."""
        listings = await self._scan_registry()
        listing = next((s for s in listings if s.slug == slug), None)
        if listing is None:
            logger.warning("Skill not found in registry", slug=slug)
            return False

        dest = self.user_skills_dir / slug
        if dest.exists():
            shutil.rmtree(dest)

        shutil.copytree(listing.path, dest)

        # Write .community marker
        marker = dest / ".community"
        marker.write_text(
            json.dumps(
                {
                    "source_repo": self.repo_url,
                    "installed_at": datetime.now(UTC).isoformat(),
                    "version": listing.version,
                },
                indent=2,
            )
        )

        # Invalidate cache
        self._listings_cache = None
        logger.info("Installed community skill", slug=slug, version=listing.version)
        return True

    async def update(self, slug: str | None = None) -> list[str]:
        """Update community skills. If slug is None, update all installed."""
        await self.sync_registry()
        updated: list[str] = []

        installed = await self.list_installed()
        targets = [i for i in installed if i["slug"] == slug] if slug else installed

        for info in targets:
            success = await self.install(info["slug"])
            if success:
                updated.append(info["slug"])

        return updated

    async def list_installed(self) -> list[dict]:
        """List user skills that were installed from the community registry."""
        results: list[dict] = []

        if not self.user_skills_dir.exists():
            return results

        for entry in sorted(self.user_skills_dir.iterdir()):
            if not entry.is_dir():
                continue
            marker = entry / ".community"
            if not marker.exists():
                continue

            try:
                marker_data = json.loads(marker.read_text())
            except (json.JSONDecodeError, OSError):
                marker_data = {}

            results.append(
                {
                    "slug": entry.name,
                    "version": marker_data.get("version", "unknown"),
                    "installed_at": marker_data.get("installed_at", ""),
                    "source_repo": marker_data.get("source_repo", ""),
                }
            )

        return results


# Module-level singleton
_registry: SkillRegistry | None = None


def get_skill_registry() -> SkillRegistry:
    """Get or create the skill registry singleton."""
    global _registry
    if _registry is None:
        _registry = SkillRegistry()
    return _registry


__all__ = [
    "SkillListing",
    "SkillRegistry",
    "get_skill_registry",
    "SKILLS_REPO_URL",
    "CACHE_DIR",
]
