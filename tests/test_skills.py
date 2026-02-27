"""Tests for the skills system — AgentSkills format, legacy format, eligibility, and registry."""

import json

import pytest

from cognitex.agent.skills import SkillsLoader, parse_frontmatter

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_dirs(tmp_path):
    """Create temp bundled + user skill directories."""
    bundled = tmp_path / "bundled"
    user = tmp_path / "user"
    bundled.mkdir()
    user.mkdir()
    return bundled, user


@pytest.fixture
def loader(tmp_dirs):
    """SkillsLoader pointed at temp directories."""
    bundled, user = tmp_dirs
    return SkillsLoader(bundled_dir=bundled, user_dir=user)


# ---------------------------------------------------------------------------
# parse_frontmatter
# ---------------------------------------------------------------------------


def test_parse_frontmatter_empty():
    """No frontmatter returns ({}, original content)."""
    content = "# Just a heading\n\nSome body text."
    fm, body = parse_frontmatter(content)
    assert fm == {}
    assert body == content


def test_parse_frontmatter_malformed():
    """Malformed YAML falls back to legacy."""
    content = "---\n: [invalid yaml\n---\n\nBody."
    fm, body = parse_frontmatter(content)
    assert fm == {}
    assert body == content


def test_parse_frontmatter_valid():
    """Well-formed frontmatter is parsed correctly."""
    content = '---\nname: test-skill\ndescription: A test\nversion: 2.0.0\nmetadata: { "cognitex": {} }\n---\n\n# Body\nHello'
    fm, body = parse_frontmatter(content)
    assert fm["name"] == "test-skill"
    assert fm["description"] == "A test"
    assert fm["version"] == "2.0.0"
    assert "Body" in body


# ---------------------------------------------------------------------------
# AgentSkills format parsing
# ---------------------------------------------------------------------------


AGENTSKILLS_CONTENT = """\
---
name: my-skill
description: Short description of the skill.
version: 2.1.0
metadata:
  cognitex:
    requires:
      bins: [ffmpeg]
      env: [MY_API_KEY]
---

# My Skill

## Purpose
Do something useful.

## Rules
1. First rule
2. Second rule
"""


@pytest.mark.asyncio
async def test_parse_agentskills_format(tmp_dirs, loader):
    """YAML frontmatter results in format='agentskills' with correct fields."""
    bundled, _ = tmp_dirs
    skill_dir = bundled / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(AGENTSKILLS_CONTENT)

    skill = await loader.get_skill("my-skill")
    assert skill is not None
    assert skill.format == "agentskills"
    assert skill.description == "Short description of the skill."
    assert skill.version == "2.1.0"
    assert skill.name == "my-skill"


@pytest.mark.asyncio
async def test_frontmatter_with_metadata_requires(tmp_dirs, loader):
    """metadata.cognitex.requires.bins/env are extracted correctly."""
    bundled, _ = tmp_dirs
    skill_dir = bundled / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(AGENTSKILLS_CONTENT)

    skill = await loader.get_skill("my-skill")
    assert skill.requires_bins == ["ffmpeg"]
    assert skill.requires_env == ["MY_API_KEY"]


# ---------------------------------------------------------------------------
# Legacy format parsing
# ---------------------------------------------------------------------------

LEGACY_CONTENT = """\
# Legacy Skill

## Purpose
Parse things in the old way.

## What IS
- Something valid

## What is NOT
- Something invalid

## Rules
1. Do this
2. Do that
"""


@pytest.mark.asyncio
async def test_parse_legacy_format(tmp_dirs, loader):
    """No frontmatter results in format='cognitex_legacy'."""
    bundled, _ = tmp_dirs
    skill_dir = bundled / "legacy-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(LEGACY_CONTENT)

    skill = await loader.get_skill("legacy-skill")
    assert skill is not None
    assert skill.format == "cognitex_legacy"
    assert "Parse things in the old way" in skill.purpose
    assert len(skill.rules) == 2
    assert len(skill.what_is) == 1
    assert len(skill.what_is_not) == 1


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_eligibility_missing_binary(tmp_dirs, loader):
    """Skill requiring a nonexistent binary is ineligible."""
    bundled, _ = tmp_dirs
    skill_dir = bundled / "needs-bin"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: needs-bin\ndescription: test\nversion: 1.0.0\nmetadata:\n  cognitex:\n    requires:\n      bins: [__nonexistent_binary_xyz__]\n---\n\n# Skill\n"
    )

    skill = await loader.get_skill("needs-bin")
    assert skill is not None
    assert skill.eligible is False
    assert "__nonexistent_binary_xyz__" in skill.ineligibility_reason


@pytest.mark.asyncio
async def test_eligibility_present_binary(tmp_dirs, loader):
    """Skill requiring 'python3' should be eligible (it's running this test)."""
    bundled, _ = tmp_dirs
    skill_dir = bundled / "needs-python"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: needs-python\ndescription: test\nversion: 1.0.0\nmetadata:\n  cognitex:\n    requires:\n      bins: [python3]\n---\n\n# Skill\n"
    )

    skill = await loader.get_skill("needs-python")
    assert skill is not None
    assert skill.eligible is True


# ---------------------------------------------------------------------------
# Prompt formatting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_format_agentskills_for_prompt(tmp_dirs, loader):
    """AgentSkills format returns the markdown body after frontmatter."""
    bundled, _ = tmp_dirs
    skill_dir = bundled / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(AGENTSKILLS_CONTENT)

    skill = await loader.get_skill("my-skill")
    prompt = loader.format_skill_for_prompt(skill)
    assert "# My Skill" in prompt
    assert "---" not in prompt  # No frontmatter delimiters


@pytest.mark.asyncio
async def test_format_legacy_for_prompt(tmp_dirs, loader):
    """Legacy format returns structured **Purpose:** / **Rules:** sections."""
    bundled, _ = tmp_dirs
    skill_dir = bundled / "legacy-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(LEGACY_CONTENT)

    skill = await loader.get_skill("legacy-skill")
    prompt = loader.format_skill_for_prompt(skill)
    assert "**Purpose:**" in prompt
    assert "**Rules:**" in prompt


# ---------------------------------------------------------------------------
# User override
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_skill_overrides_bundled(tmp_dirs, loader):
    """User skill takes precedence over bundled skill with same name."""
    bundled, user = tmp_dirs

    # Bundled
    bdir = bundled / "override-test"
    bdir.mkdir()
    (bdir / "SKILL.md").write_text("# Bundled\n\n## Purpose\nBundled version.")

    # User
    udir = user / "override-test"
    udir.mkdir()
    (udir / "SKILL.md").write_text("# User\n\n## Purpose\nUser version.")

    skill = await loader.get_skill("override-test")
    assert skill is not None
    assert skill.is_user_skill is True
    assert "User version" in skill.purpose


# ---------------------------------------------------------------------------
# list_skills includes new fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_skills_includes_new_fields(tmp_dirs, loader):
    """list_skills() returns format, version, eligible, source."""
    bundled, _ = tmp_dirs
    skill_dir = bundled / "listed"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: listed\ndescription: Listed skill\nversion: 3.0.0\nmetadata: {}\n---\n\n# Listed\n"
    )

    skills = await loader.list_skills()
    assert len(skills) == 1
    s = skills[0]
    assert s["format"] == "agentskills"
    assert s["version"] == "3.0.0"
    assert s["eligible"] is True
    assert s["source"] == "bundled"
    assert s["description"] == "Listed skill"


# ---------------------------------------------------------------------------
# Community marker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_community_marker_sets_source(tmp_dirs, loader):
    """Skill with .community marker file has source='community'."""
    _, user = tmp_dirs
    skill_dir = user / "comm-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Community\n\n## Purpose\nFrom the community.")
    (skill_dir / ".community").write_text(json.dumps({"source_repo": "https://example.com"}))

    skill = await loader.get_skill("comm-skill")
    assert skill is not None
    assert skill.source == "community"


# ---------------------------------------------------------------------------
# Registry (unit tests with mocked file system)
# ---------------------------------------------------------------------------


@pytest.fixture
def registry_dirs(tmp_path):
    """Create temp cache + user dirs for registry tests."""
    cache = tmp_path / "cache"
    user = tmp_path / "user"
    cache.mkdir()
    user.mkdir()
    return cache, user


@pytest.fixture
def registry(registry_dirs):
    from cognitex.services.skill_registry import SkillRegistry

    cache, user = registry_dirs
    return SkillRegistry(repo_url="unused", cache_dir=cache, user_skills_dir=user)


@pytest.mark.asyncio
async def test_registry_search(registry_dirs, registry):
    """Substring matching on name/description."""
    cache, _ = registry_dirs
    # Create a fake skill in cache
    skill_dir = cache / "test-email"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: test-email\ndescription: Handle email things\nversion: 1.0.0\n---\n\n# Test\n"
    )
    # Create .git so it looks like a cloned repo
    (cache / ".git").mkdir()

    results = await registry.search("email")
    assert len(results) == 1
    assert results[0].slug == "test-email"
    assert results[0].description == "Handle email things"


@pytest.mark.asyncio
async def test_registry_install(registry_dirs, registry):
    """Install copies skill dir and writes .community marker."""
    cache, user = registry_dirs
    # Create skill in cache
    skill_dir = cache / "install-me"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: install-me\ndescription: Installable\nversion: 1.2.0\n---\n\n# Install\n"
    )

    success = await registry.install("install-me")
    assert success is True

    dest = user / "install-me"
    assert dest.exists()
    assert (dest / "SKILL.md").exists()
    assert (dest / ".community").exists()

    marker = json.loads((dest / ".community").read_text())
    assert marker["version"] == "1.2.0"


# ---------------------------------------------------------------------------
# Migrated bundled skills
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_migrated_bundled_skills_load():
    """All 3 real bundled skills load with format='agentskills'."""
    loader = SkillsLoader()
    await loader.initialize()

    for name in ("email-tasks", "goal-linking", "meeting-prep"):
        skill = await loader.get_skill(name)
        assert skill is not None, f"Bundled skill '{name}' not found"
        assert skill.format == "agentskills", f"Skill '{name}' has format={skill.format}"
        assert skill.version == "1.0.0"
        assert skill.description != ""
