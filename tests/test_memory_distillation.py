"""Tests for WP6: Memory Curation & Distillation.

Tests cover skill loading, forgetting policies, weekly distillation pipeline,
MEMORY.md update application, trigger registration, and inbox integration.
"""

import json
import shutil
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cognitex.agent.skills import SkillsLoader
from cognitex.services.memory_files import MemoryFileService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_skill_dirs(tmp_path):
    """Create temp bundled + user skill directories."""
    bundled = tmp_path / "bundled"
    user = tmp_path / "user"
    bundled.mkdir()
    user.mkdir()
    return bundled, user


@pytest.fixture
def skill_loader(tmp_skill_dirs):
    """SkillsLoader pointed at temp directories."""
    bundled, user = tmp_skill_dirs
    return SkillsLoader(bundled_dir=bundled, user_dir=user)


@pytest.fixture
def install_memory_curation_skill(tmp_skill_dirs):
    """Install the real memory-curation SKILL.md into the temp bundled dir."""
    bundled, _ = tmp_skill_dirs
    src = Path(__file__).parent.parent / "src" / "cognitex" / "skills" / "memory-curation"
    dest = bundled / "memory-curation"
    shutil.copytree(src, dest)


@pytest.fixture
def memory_dir(tmp_path):
    """Create a temp memory directory."""
    d = tmp_path / "memory"
    d.mkdir()
    return d


@pytest.fixture
def memory_service(memory_dir):
    """MemoryFileService pointed at temp directory."""
    return MemoryFileService(memory_dir=memory_dir)


def _write_daily_log(memory_dir: Path, log_date: date, entries: list[tuple[str, str, str]]):
    """Write a daily log file with given entries.

    Each entry is (time_str, category, content).
    """
    path = memory_dir / f"{log_date.isoformat()}.md"
    lines = [f"## {log_date.isoformat()}\n"]
    for time_str, category, content in entries:
        lines.append(f"\n### {time_str} - {category}\n{content}\n")
    path.write_text("\n".join(lines))


def _make_llm_mock(response_data: dict) -> AsyncMock:
    """Create a mock LLM service that returns the given dict as JSON."""
    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=json.dumps(response_data))
    return llm


# ---------------------------------------------------------------------------
# Step 1: Skill loading
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("install_memory_curation_skill")
async def test_memory_curation_skill_loads(skill_loader):
    """Skill loads, parses frontmatter, has purpose and rules."""
    skill = await skill_loader.get_skill("memory-curation")
    assert skill is not None
    assert skill.name == "memory-curation"
    assert "Distill" in skill.purpose or "distill" in skill.purpose
    assert len(skill.rules) > 0
    assert skill.format == "agentskills"


# ---------------------------------------------------------------------------
# Step 2: Forgetting policy — archive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_archive_old_daily_logs_moves_files(memory_service, memory_dir):
    """Files older than 30 days get moved to archive/."""
    old_date = date.today() - timedelta(days=45)
    recent_date = date.today() - timedelta(days=5)

    _write_daily_log(memory_dir, old_date, [("10:00", "Meeting", "Discussed project roadmap")])
    _write_daily_log(memory_dir, recent_date, [("14:00", "Email", "Replied to Sarah about budget")])

    result = await memory_service.archive_old_daily_logs(older_than_days=30)

    assert result["archived_count"] == 1
    assert f"{old_date.isoformat()}.md" in result["archived_files"]
    assert (memory_dir / "archive" / f"{old_date.isoformat()}.md").exists()
    assert (memory_dir / f"{recent_date.isoformat()}.md").exists()


@pytest.mark.asyncio
async def test_archive_creates_directory(memory_service, memory_dir):
    """archive/ is created if it doesn't exist."""
    old_date = date.today() - timedelta(days=45)
    _write_daily_log(memory_dir, old_date, [("10:00", "Note", "Something worth remembering")])

    assert not (memory_dir / "archive").exists()
    await memory_service.archive_old_daily_logs(older_than_days=30)
    assert (memory_dir / "archive").exists()


@pytest.mark.asyncio
async def test_archive_ignores_non_date_files(memory_service, memory_dir):
    """MEMORY.md and other non-date files are untouched."""
    (memory_dir / "MEMORY.md").write_text("# Long-Term Memory\n")
    (memory_dir / "notes.md").write_text("Some notes\n")
    old_date = date.today() - timedelta(days=45)
    _write_daily_log(memory_dir, old_date, [("10:00", "Note", "Old entry here")])

    result = await memory_service.archive_old_daily_logs(older_than_days=30)

    assert result["archived_count"] == 1
    assert (memory_dir / "MEMORY.md").exists()
    assert (memory_dir / "notes.md").exists()


# ---------------------------------------------------------------------------
# Step 2: Forgetting policy — daily forgetting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_daily_forgetting_removes_trivial(memory_service, memory_dir):
    """Sync/Check/Poll entries removed, real entries kept."""
    yesterday = date.today() - timedelta(days=1)
    _write_daily_log(memory_dir, yesterday, [
        ("09:00", "Sync", "Synced 0 emails"),
        ("09:05", "Check", "Checked calendar"),
        ("10:00", "Email Pattern", "User always replies to Sarah within 5 minutes of receiving her emails"),
        ("11:00", "Poll", "Polled drive"),
        ("14:00", "Meeting Note", "Discussed Q2 roadmap with engineering team, agreed on 3 priorities"),
    ])

    result = await memory_service.apply_daily_forgetting(target_date=yesterday)

    assert result["entries_before"] == 5
    assert result["entries_after"] == 2
    assert result["removed"] == 3

    # Verify the file still has the kept entries
    log = await memory_service.get_daily_log(yesterday)
    assert log is not None
    categories = [e.category for e in log.entries]
    assert "Email Pattern" in categories
    assert "Meeting Note" in categories
    assert "Sync" not in categories


@pytest.mark.asyncio
async def test_daily_forgetting_handles_empty_log(memory_service):
    """No crash on empty/missing log."""
    yesterday = date.today() - timedelta(days=1)
    result = await memory_service.apply_daily_forgetting(target_date=yesterday)

    assert result["entries_before"] == 0
    assert result["entries_after"] == 0
    assert result["removed"] == 0


# ---------------------------------------------------------------------------
# Step 2: Weekly logs for distillation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_weekly_logs_for_distillation(memory_service, memory_dir):
    """Returns up to 7 days of logs for the last complete week."""
    today = date.today()
    # Find last Sunday
    days_since_sunday = (today.weekday() + 1) % 7
    last_sunday = today - timedelta(days=days_since_sunday)
    # The week is Monday-Sunday, so start = last_sunday - 6
    start = last_sunday - timedelta(days=6)

    # Create logs for 3 days within that week
    for i in [0, 2, 5]:
        d = start + timedelta(days=i)
        _write_daily_log(memory_dir, d, [
            ("10:00", "Observation", f"Something noteworthy happened on day {i} of the week"),
        ])

    logs = await memory_service.get_weekly_logs_for_distillation(weeks_back=1)

    assert len(logs) <= 7
    assert len(logs) == 3
    for log in logs:
        assert start <= log.date <= last_sunday


# ---------------------------------------------------------------------------
# Step 6: MEMORY.md update application
# ---------------------------------------------------------------------------


def test_apply_memory_updates_adds_entries():
    """New entries placed in correct sections."""
    from cognitex.web.app import _apply_memory_updates

    current = """# Long-Term Memory

## User Preferences
- [2026-02-10] Prefers dark mode

## Important Relationships
<!-- Notes about key people -->

## Recurring Patterns
- [2026-02-10] Morning deep work blocks

## Corrections
<!-- Things the agent got wrong -->
"""

    updates = [
        {
            "section": "User Preferences",
            "content": "Prefers Cursor over VS Code",
            "confidence": 0.8,
            "source_dates": ["2026-02-20"],
            "merge_with_existing": None,
        },
        {
            "section": "Corrections",
            "content": "London meetings use Europe/London timezone",
            "confidence": 0.95,
            "source_dates": ["2026-02-18"],
            "merge_with_existing": None,
        },
    ]

    result = _apply_memory_updates(current, updates)

    assert "- [2026-02-20] Prefers Cursor over VS Code" in result
    assert "- [2026-02-18] London meetings use Europe/London timezone" in result
    # Original entries preserved
    assert "Prefers dark mode" in result
    assert "Morning deep work blocks" in result


def test_apply_memory_updates_merges_existing():
    """Existing entry replaced when merge_with_existing set."""
    from cognitex.web.app import _apply_memory_updates

    current = """# Long-Term Memory

## Recurring Patterns
- [2026-02-10] User prefers morning deep work blocks (9-11am)
- [2026-02-10] Checks email first thing in morning
"""

    updates = [
        {
            "section": "Recurring Patterns",
            "content": "User prefers two deep work blocks: mornings (9-11am) and afternoon (2-3pm)",
            "confidence": 0.85,
            "source_dates": ["2026-02-20"],
            "merge_with_existing": "User prefers morning deep work blocks (9-11am)",
        },
    ]

    result = _apply_memory_updates(current, updates)

    assert "two deep work blocks" in result
    assert "User prefers morning deep work blocks (9-11am)" not in result
    # Other entries preserved
    assert "Checks email first thing" in result


def test_apply_memory_updates_creates_section():
    """Missing section header created when needed."""
    from cognitex.web.app import _apply_memory_updates

    current = """# Long-Term Memory

## User Preferences
- [2026-02-10] Prefers dark mode
"""

    updates = [
        {
            "section": "Key Decisions",
            "content": "Decided to migrate to PostgreSQL 16",
            "confidence": 0.9,
            "source_dates": ["2026-02-20"],
            "merge_with_existing": None,
        },
    ]

    result = _apply_memory_updates(current, updates)

    assert "## Key Decisions" in result
    assert "Decided to migrate to PostgreSQL 16" in result


# ---------------------------------------------------------------------------
# Step 3: Weekly distillation pipeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_weekly_distillation_creates_inbox(memory_service, memory_dir):
    """Full pipeline creates memory_update_proposal inbox item."""
    from cognitex.agent.consolidation import MemoryConsolidator

    llm_response = {
        "proposed_updates": [
            {
                "section": "User Preferences",
                "content": "Prefers Cursor editor",
                "confidence": 0.8,
                "source_dates": ["2026-02-20"],
                "merge_with_existing": None,
            }
        ],
        "discarded_count": 15,
        "summary": "Identified editor preference",
    }

    # Write some logs for last week
    today = date.today()
    days_since_sunday = (today.weekday() + 1) % 7
    last_sunday = today - timedelta(days=days_since_sunday)
    start = last_sunday - timedelta(days=6)

    for i in range(3):
        d = start + timedelta(days=i)
        _write_daily_log(memory_dir, d, [
            ("10:00", "Observation", f"Day {i} observation with meaningful content here"),
        ])

    consolidator = MemoryConsolidator()

    mock_inbox = AsyncMock()
    mock_inbox.create_item = AsyncMock()

    mock_bootstrap_file = MagicMock()
    mock_bootstrap_file.raw_content = "# Long-Term Memory\n## User Preferences\n"

    mock_bootstrap = MagicMock()
    mock_bootstrap.get_memory_file = AsyncMock(return_value=mock_bootstrap_file)

    mock_skill = MagicMock()
    mock_skill.raw_content = "Memory curation skill content"

    mock_skills_loader = MagicMock()
    mock_skills_loader.get_skill = AsyncMock(return_value=mock_skill)

    with (
        patch.object(consolidator, "_get_llm", return_value=_make_llm_mock(llm_response)),
        patch(
            "cognitex.services.memory_files.get_memory_file_service",
            return_value=memory_service,
        ),
        patch(
            "cognitex.agent.bootstrap.get_bootstrap_loader",
            return_value=mock_bootstrap,
        ),
        patch(
            "cognitex.agent.skills.get_skills_loader",
            return_value=mock_skills_loader,
        ),
        patch(
            "cognitex.services.inbox.get_inbox_service",
            return_value=mock_inbox,
        ),
    ):
        # Mock _get_weekly_summaries to avoid DB call
        consolidator._get_weekly_summaries = AsyncMock(return_value=[])

        result = await consolidator.run_weekly_distillation(dry_run=False)

    assert result["status"] == "completed"
    assert len(result["proposed_updates"]) == 1
    assert result["proposed_updates"][0]["content"] == "Prefers Cursor editor"
    mock_inbox.create_item.assert_called_once()
    call_kwargs = mock_inbox.create_item.call_args
    assert call_kwargs.kwargs.get("item_type") == "memory_update_proposal"


@pytest.mark.asyncio
async def test_run_weekly_distillation_dry_run(memory_service, memory_dir):
    """dry_run=True returns proposals without creating inbox item."""
    from cognitex.agent.consolidation import MemoryConsolidator

    llm_response = {
        "proposed_updates": [
            {
                "section": "Recurring Patterns",
                "content": "Test pattern",
                "confidence": 0.7,
                "source_dates": ["2026-02-20"],
                "merge_with_existing": None,
            }
        ],
        "discarded_count": 5,
        "summary": "Test summary",
    }

    today = date.today()
    days_since_sunday = (today.weekday() + 1) % 7
    last_sunday = today - timedelta(days=days_since_sunday)
    start = last_sunday - timedelta(days=6)

    _write_daily_log(memory_dir, start, [
        ("10:00", "Observation", "Something noteworthy happened during the day"),
    ])

    consolidator = MemoryConsolidator()

    mock_bootstrap_file = MagicMock()
    mock_bootstrap_file.raw_content = "# Memory"
    mock_bootstrap = MagicMock()
    mock_bootstrap.get_memory_file = AsyncMock(return_value=mock_bootstrap_file)
    mock_skill = MagicMock()
    mock_skill.raw_content = "Skill content"
    mock_skills_loader = MagicMock()
    mock_skills_loader.get_skill = AsyncMock(return_value=mock_skill)

    with (
        patch.object(consolidator, "_get_llm", return_value=_make_llm_mock(llm_response)),
        patch(
            "cognitex.services.memory_files.get_memory_file_service",
            return_value=memory_service,
        ),
        patch(
            "cognitex.agent.bootstrap.get_bootstrap_loader",
            return_value=mock_bootstrap,
        ),
        patch(
            "cognitex.agent.skills.get_skills_loader",
            return_value=mock_skills_loader,
        ),
    ):
        consolidator._get_weekly_summaries = AsyncMock(return_value=[])
        result = await consolidator.run_weekly_distillation(dry_run=True)

    assert result["status"] == "dry_run_complete"
    assert len(result["proposed_updates"]) == 1
    assert result["dry_run"] is True


@pytest.mark.asyncio
async def test_run_weekly_distillation_empty_week(memory_service):
    """Graceful return when no logs exist for last week."""
    from cognitex.agent.consolidation import MemoryConsolidator

    consolidator = MemoryConsolidator()

    with patch(
        "cognitex.services.memory_files.get_memory_file_service",
        return_value=memory_service,
    ):
        result = await consolidator.run_weekly_distillation()

    assert result["status"] == "no_data"
    assert result["proposed_updates"] == []


# ---------------------------------------------------------------------------
# Step 4: Trigger existence
# ---------------------------------------------------------------------------


def test_weekly_distillation_trigger_exists():
    """TriggerSystem has _run_weekly_distillation method."""
    from cognitex.agent.triggers import TriggerSystem

    ts = TriggerSystem()
    assert hasattr(ts, "_run_weekly_distillation")
    assert callable(ts._run_weekly_distillation)


def test_monthly_archive_trigger_exists():
    """TriggerSystem has _run_monthly_archive method."""
    from cognitex.agent.triggers import TriggerSystem

    ts = TriggerSystem()
    assert hasattr(ts, "_run_monthly_archive")
    assert callable(ts._run_monthly_archive)
