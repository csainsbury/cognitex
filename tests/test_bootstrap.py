"""Tests for the extended bootstrap system (7-file OpenClaw pattern)."""

import pytest

from cognitex.agent.bootstrap import BOOTSTRAP_FILES, BootstrapLoader


@pytest.fixture
def bootstrap_dir(tmp_path):
    """Create a temporary bootstrap directory."""
    return tmp_path / "bootstrap"


@pytest.fixture
def loader(bootstrap_dir):
    """Create a BootstrapLoader with a temp directory."""
    return BootstrapLoader(bootstrap_dir=bootstrap_dir)


@pytest.mark.asyncio
async def test_initialize_creates_all_files(loader, bootstrap_dir):
    """initialize() should create all 7 bootstrap files."""
    await loader.initialize()

    expected_files = list(BOOTSTRAP_FILES.keys())
    for filename in expected_files:
        filepath = bootstrap_dir / filename
        assert filepath.exists(), f"{filename} was not created"
        assert filepath.read_text().strip(), f"{filename} is empty"


@pytest.mark.asyncio
async def test_initialize_does_not_overwrite_existing(loader, bootstrap_dir):
    """initialize() should not overwrite files that already exist."""
    bootstrap_dir.mkdir(parents=True, exist_ok=True)
    custom_content = "## My Custom Soul\nI am unique."
    (bootstrap_dir / "SOUL.md").write_text(custom_content)

    await loader.initialize()

    assert (bootstrap_dir / "SOUL.md").read_text() == custom_content


@pytest.mark.asyncio
async def test_get_user_fallback_to_identity(loader, bootstrap_dir):
    """get_user() should fall back to IDENTITY.md when USER.md doesn't exist."""
    bootstrap_dir.mkdir(parents=True, exist_ok=True)
    identity_content = "## About Me\nI am the identity file."
    (bootstrap_dir / "IDENTITY.md").write_text(identity_content)

    result = await loader.get_user()
    assert result is not None
    assert result.name == "IDENTITY"
    assert "identity file" in result.raw_content


@pytest.mark.asyncio
async def test_get_user_prefers_user_md(loader, bootstrap_dir):
    """get_user() should prefer USER.md over IDENTITY.md when both exist."""
    bootstrap_dir.mkdir(parents=True, exist_ok=True)
    (bootstrap_dir / "USER.md").write_text("## About Me\nI am the user file.")
    (bootstrap_dir / "IDENTITY.md").write_text("## About Me\nI am the identity file.")

    result = await loader.get_user()
    assert result is not None
    assert result.name == "USER"
    assert "user file" in result.raw_content


@pytest.mark.asyncio
async def test_get_all_returns_all_files(loader):
    """get_all() should return all existing bootstrap files."""
    await loader.initialize()

    files = await loader.get_all()
    assert "SOUL" in files
    assert "USER" in files
    assert "AGENTS" in files
    assert "TOOLS" in files
    assert "MEMORY" in files
    assert "IDENTITY" in files
    assert "CONTEXT" in files
    assert len(files) == 7


@pytest.mark.asyncio
async def test_format_skips_fill_placeholders(loader):
    """Default templates should produce no [FILL] content in formatted output."""
    await loader.initialize()
    files = await loader.get_all()

    output = loader.format_for_prompt(files)
    assert "[FILL]" not in output


@pytest.mark.asyncio
async def test_format_safety_section_full_injection(loader, bootstrap_dir):
    """AGENTS.md safety section should be present verbatim in formatted output."""
    bootstrap_dir.mkdir(parents=True, exist_ok=True)
    agents_content = """## 1. Core Mission
Do good things.

## 5. Safety and Action Boundaries
- Never send emails without approval
- Never delete data without confirmation
"""
    (bootstrap_dir / "AGENTS.md").write_text(agents_content)

    files = await loader.get_all()
    output = loader.format_for_prompt(files)

    assert "Never send emails without approval" in output
    assert "Never delete data without confirmation" in output
    assert "Operating Constitution" in output


@pytest.mark.asyncio
async def test_format_agents_other_sections_summarised(loader, bootstrap_dir):
    """Non-safety AGENTS.md sections should get first-line summary only."""
    bootstrap_dir.mkdir(parents=True, exist_ok=True)
    agents_content = """## 1. Core Mission
Act as a digital twin that advances work.
This is a second line that should not appear.

## 5. Safety and Action Boundaries
- Never send emails without approval
"""
    (bootstrap_dir / "AGENTS.md").write_text(agents_content)

    files = await loader.get_all()
    output = loader.format_for_prompt(files)

    # First line of Core Mission should appear as summary
    assert "Act as a digital twin that advances work." in output
    # Second line should NOT appear (summarised)
    assert "This is a second line that should not appear." not in output
    # Safety content should appear fully
    assert "Never send emails without approval" in output


@pytest.mark.asyncio
async def test_get_safety_rules(loader, bootstrap_dir):
    """get_safety_rules() should extract the safety section from AGENTS.md."""
    bootstrap_dir.mkdir(parents=True, exist_ok=True)
    agents_content = """## 1. Core Mission
Do things.

## 5. Safety and Action Boundaries
- Rule one
- Rule two
"""
    (bootstrap_dir / "AGENTS.md").write_text(agents_content)

    rules = await loader.get_safety_rules()
    assert "Rule one" in rules
    assert "Rule two" in rules
    assert "Core Mission" not in rules


@pytest.mark.asyncio
async def test_get_safety_rules_empty_without_agents(loader, bootstrap_dir):
    """get_safety_rules() should return empty string when AGENTS.md doesn't exist."""
    bootstrap_dir.mkdir(parents=True, exist_ok=True)
    rules = await loader.get_safety_rules()
    assert rules == ""


@pytest.mark.asyncio
async def test_save_file_accepts_new_filenames(loader):
    """save_file() should accept all 7 bootstrap filenames."""
    await loader.initialize()

    for filename in BOOTSTRAP_FILES:
        result = await loader.save_file(filename, f"# Updated {filename}")
        assert result is True, f"save_file rejected {filename}"


@pytest.mark.asyncio
async def test_save_file_rejects_unknown(loader):
    """save_file() should reject unknown filenames."""
    await loader.initialize()
    result = await loader.save_file("HACK.md", "malicious content")
    assert result is False


@pytest.mark.asyncio
async def test_backward_compat_soul_identity_context(loader, bootstrap_dir):
    """Existing 3-file flow (SOUL, IDENTITY, CONTEXT) should still work."""
    bootstrap_dir.mkdir(parents=True, exist_ok=True)
    (bootstrap_dir / "SOUL.md").write_text("## Email Voice\nBe friendly.")
    (bootstrap_dir / "IDENTITY.md").write_text("## About Me\nI am a developer.")
    (bootstrap_dir / "CONTEXT.md").write_text("## Recent Activity\nDid some work today.")

    files = await loader.get_all()
    output = loader.format_for_prompt(files)

    assert "Communication Style & Voice" in output
    assert "Be friendly." in output
    assert "Operator Profile" in output
    assert "I am a developer." in output
    assert "Current Context" in output
    assert "Did some work today." in output


@pytest.mark.asyncio
async def test_cache_invalidation_on_save(loader):
    """Saving a file should invalidate the cache so get returns new content."""
    await loader.initialize()

    # Load SOUL into cache
    soul = await loader.get_soul()
    assert soul is not None
    original = soul.raw_content

    # Save new content
    new_content = "## New Style\nCompletely different voice."
    await loader.save_file("SOUL.md", new_content)

    # Get should return new content
    soul = await loader.get_soul()
    assert soul is not None
    assert soul.raw_content == new_content
    assert soul.raw_content != original


@pytest.mark.asyncio
async def test_format_with_real_user_content(loader, bootstrap_dir):
    """USER.md with real content should appear in formatted output."""
    bootstrap_dir.mkdir(parents=True, exist_ok=True)
    (bootstrap_dir / "USER.md").write_text(
        "## About Me\n- Role: Senior Engineer\n- Company: Acme Corp"
    )

    files = await loader.get_all()
    output = loader.format_for_prompt(files)

    assert "Operator Profile" in output
    assert "Senior Engineer" in output
    assert "Acme Corp" in output


@pytest.mark.asyncio
async def test_format_memory_with_real_content(loader, bootstrap_dir):
    """MEMORY.md with real content should appear in formatted output."""
    bootstrap_dir.mkdir(parents=True, exist_ok=True)
    (bootstrap_dir / "MEMORY.md").write_text(
        "## Key Decisions\nDecided to use PostgreSQL for storage."
    )

    files = await loader.get_all()
    output = loader.format_for_prompt(files)

    assert "Operational Memory" in output
    assert "PostgreSQL" in output


@pytest.mark.asyncio
async def test_format_tools_with_real_content(loader, bootstrap_dir):
    """TOOLS.md with real content should appear in formatted output."""
    bootstrap_dir.mkdir(parents=True, exist_ok=True)
    (bootstrap_dir / "TOOLS.md").write_text("## Infrastructure\n- Primary email: chris@example.com")

    files = await loader.get_all()
    output = loader.format_for_prompt(files)

    assert "Tools & Infrastructure" in output
    assert "chris@example.com" in output
