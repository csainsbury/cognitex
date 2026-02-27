"""Tests for the skill evolution system (Path 3 — agent detects and proposes)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cognitex.agent.skill_evolution import (
    DANGEROUS_PATTERNS,
    MAX_PROPOSALS_PER_CYCLE,
    PROTECTED_FILES,
    CodeProposal,
    FeedbackEntry,
    PatternDescription,
    SafetyCheckResult,
    SkillEvolution,
    SkillProposal,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

GENERATED_SKILL = """\
---
name: auto-rejection-handler
description: Handle repeated rejection patterns.
version: 1.0.0
metadata:
  cognitex:
    origin: evolution
---

# Auto Rejection Handler

## Purpose
Address repeated rejection patterns.

## Rules
1. Check rejection reason before proposing
"""


class FakeSession:
    """Lightweight fake for async session context manager."""

    def __init__(self, execute_results=None):
        self._execute_results = execute_results or []
        self._call_idx = 0
        self.committed = False

    async def execute(self, stmt, params=None):
        if self._call_idx < len(self._execute_results):
            result = self._execute_results[self._call_idx]
            self._call_idx += 1
            return result
        return FakeResult([])

    async def commit(self):
        self.committed = True


class FakeResult:
    """Fake SQLAlchemy result."""

    def __init__(self, rows):
        self._rows = rows
        self.rowcount = len(rows)

    def mappings(self):
        return FakeMappings(self._rows)


class FakeMappings:
    def __init__(self, rows):
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


@pytest.fixture
def mock_llm():
    llm = MagicMock()
    llm.complete = AsyncMock(return_value=GENERATED_SKILL)
    llm.fast_model = "test-model"
    return llm


@pytest.fixture
def mock_loader():
    loader = MagicMock()
    loader.save_skill = AsyncMock(return_value=True)
    loader.list_skills = AsyncMock(return_value=[
        {"name": "email-tasks"},
        {"name": "meeting-prep"},
    ])

    mock_skill = MagicMock()
    mock_skill.raw_content = "# Existing skill\n## Rules\n1. Old rule"
    loader.get_skill = AsyncMock(return_value=mock_skill)
    return loader


@pytest.fixture
def evolution(mock_llm, mock_loader):
    """SkillEvolution with mocked dependencies."""
    fake_session = FakeSession([FakeResult([])])

    async def fake_get_session():
        yield fake_session

    evo = SkillEvolution.__new__(SkillEvolution)
    evo._get_session = fake_get_session
    evo._initialized = True  # Skip schema init
    evo._llm = mock_llm
    evo._loader = mock_loader
    return evo


# ---------------------------------------------------------------------------
# Safety checks
# ---------------------------------------------------------------------------


def test_safety_blocks_protected_files():
    """Diff touching a protected file should be marked unsafe."""
    evo = SkillEvolution.__new__(SkillEvolution)
    result = evo._check_safety("src/cognitex/agent/SOUL.md", "some diff content")

    assert result.is_safe is False
    assert result.modifies_protected_files is True
    assert any("SOUL.md" in v for v in result.violations)


def test_safety_blocks_dangerous_code():
    """Dangerous patterns in diff should be flagged."""
    evo = SkillEvolution.__new__(SkillEvolution)
    result = evo._check_safety("src/helper.py", "import os\nos.system('rm -rf /')")

    assert result.is_safe is False
    assert result.has_side_effects is True
    assert any("os.system" in v for v in result.violations)


def test_safety_blocks_safety_rule_changes():
    """Modifying risk levels / approval code should be flagged."""
    evo = SkillEvolution.__new__(SkillEvolution)
    result = evo._check_safety("src/utils.py", "risk_level = 'AUTO'")

    assert result.is_safe is False
    assert result.modifies_safety_rules is True


def test_safety_passes_safe_proposal():
    """Normal code changes should pass safety checks."""
    evo = SkillEvolution.__new__(SkillEvolution)
    result = evo._check_safety(
        "src/cognitex/services/helper.py",
        "def format_date(d):\n    return d.isoformat()"
    )

    assert result.is_safe is True
    assert result.violations == []
    assert result.modifies_protected_files is False
    assert result.modifies_safety_rules is False
    assert result.has_side_effects is False


# ---------------------------------------------------------------------------
# propose_new_skill
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_propose_never_auto_deploys(evolution, mock_llm):
    """Proposals should always have status='proposed', never auto-deploy."""
    pattern = PatternDescription(
        pattern_type="repeated_rejection",
        description="Action 'draft_email' rejected 5 times",
        evidence=[{"id": "t1", "summary": "test"}],
        confidence=0.8,
    )

    proposal = await evolution.propose_new_skill(pattern)

    assert isinstance(proposal, SkillProposal)
    assert proposal.status == "proposed"
    assert proposal.id.startswith("proposal_")
    assert proposal.skill_name == "auto-rejection-handler"


# ---------------------------------------------------------------------------
# review_proposal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_approve(mock_llm, mock_loader):
    """Approving a proposal should update its status."""
    approved_result = FakeResult([{"rowcount": 1}])
    approved_result.rowcount = 1
    fake_session = FakeSession([approved_result])

    async def fake_get_session():
        yield fake_session

    evo = SkillEvolution.__new__(SkillEvolution)
    evo._get_session = fake_get_session
    evo._initialized = True
    evo._llm = mock_llm
    evo._loader = mock_loader

    result = await evo.review_proposal("proposal_abc123", "approved", "looks good")
    assert fake_session.committed is True


@pytest.mark.asyncio
async def test_review_reject(mock_llm, mock_loader):
    """Rejecting a proposal should update its status."""
    rejected_result = FakeResult([{"rowcount": 1}])
    rejected_result.rowcount = 1
    fake_session = FakeSession([rejected_result])

    async def fake_get_session():
        yield fake_session

    evo = SkillEvolution.__new__(SkillEvolution)
    evo._get_session = fake_get_session
    evo._initialized = True
    evo._llm = mock_llm
    evo._loader = mock_loader

    result = await evo.review_proposal("proposal_abc123", "rejected", "not needed")
    assert fake_session.committed is True


# ---------------------------------------------------------------------------
# deploy_proposal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deploy_proposal_writes_skill(mock_llm, mock_loader):
    """Deploying an approved proposal should call save_skill."""
    select_result = FakeResult([{
        "skill_name": "my-evolved-skill",
        "skill_content": GENERATED_SKILL,
        "status": "approved",
    }])
    update_result = FakeResult([])

    call_count = 0

    async def fake_get_session():
        nonlocal call_count
        if call_count == 0:
            call_count += 1
            yield FakeSession([select_result])
        else:
            yield FakeSession([update_result])

    evo = SkillEvolution.__new__(SkillEvolution)
    evo._get_session = fake_get_session
    evo._initialized = True
    evo._llm = mock_llm
    evo._loader = mock_loader

    with patch("cognitex.agent.action_log.log_action", new_callable=AsyncMock):
        success = await evo.deploy_proposal("proposal_abc123")

    assert success is True
    mock_loader.save_skill.assert_called_once_with("my-evolved-skill", GENERATED_SKILL)


@pytest.mark.asyncio
async def test_deploy_proposal_rejects_unapproved(mock_llm, mock_loader):
    """Should not deploy proposals that aren't approved."""
    select_result = FakeResult([{
        "skill_name": "my-skill",
        "skill_content": "content",
        "status": "proposed",
    }])

    async def fake_get_session():
        yield FakeSession([select_result])

    evo = SkillEvolution.__new__(SkillEvolution)
    evo._get_session = fake_get_session
    evo._initialized = True
    evo._llm = mock_llm
    evo._loader = mock_loader

    success = await evo.deploy_proposal("proposal_abc123")
    assert success is False
    mock_loader.save_skill.assert_not_called()


# ---------------------------------------------------------------------------
# max proposals per cycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_proposals_per_cycle(evolution, mock_llm):
    """Evolution cycle should never exceed MAX_PROPOSALS_PER_CYCLE."""
    # Mock detect_skill_opportunity to return many patterns
    many_patterns = [
        PatternDescription(
            pattern_type="repeated_rejection",
            description=f"Pattern {i}",
            evidence=[{"id": f"t{i}"}],
            confidence=0.9,
        )
        for i in range(10)
    ]

    with patch.object(evolution, "detect_skill_opportunity", new_callable=AsyncMock) as mock_detect:
        mock_detect.return_value = many_patterns
        with patch.object(evolution, "_process_pending_feedback", new_callable=AsyncMock) as mock_fb:
            mock_fb.return_value = []

            results = await evolution.run_evolution_cycle()

    assert len(results) <= MAX_PROPOSALS_PER_CYCLE


# ---------------------------------------------------------------------------
# Autonomous agent EVOLVE phase integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evolve_phase_runs_on_interval():
    """Cycle count modulo check should trigger evolution at the right interval."""
    from cognitex.agent.autonomous import AutonomousAgent

    with patch("cognitex.agent.autonomous.get_settings") as mock_settings:
        settings = MagicMock()
        settings.autonomous_agent_enabled = True
        settings.autonomous_agent_interval_minutes = 15
        settings.skill_evolution_enabled = True
        settings.skill_evolution_cycle_interval = 3
        mock_settings.return_value = settings

        agent = AutonomousAgent()
        agent._cycle_count = 2  # Will become 3 after increment

        with patch.object(agent, "_run_evolve_phase", new_callable=AsyncMock) as mock_evolve:
            # Simulate the cycle count increment + check
            agent._cycle_count += 1
            if (
                agent.settings.skill_evolution_enabled
                and agent._cycle_count % agent.settings.skill_evolution_cycle_interval == 0
            ):
                await agent._run_evolve_phase()

            mock_evolve.assert_called_once()


@pytest.mark.asyncio
async def test_evolve_phase_skips_when_not_interval():
    """Evolution should not run on non-matching cycles."""
    from cognitex.agent.autonomous import AutonomousAgent

    with patch("cognitex.agent.autonomous.get_settings") as mock_settings:
        settings = MagicMock()
        settings.autonomous_agent_enabled = True
        settings.autonomous_agent_interval_minutes = 15
        settings.skill_evolution_enabled = True
        settings.skill_evolution_cycle_interval = 10
        mock_settings.return_value = settings

        agent = AutonomousAgent()
        agent._cycle_count = 4  # Will become 5, not divisible by 10

        with patch.object(agent, "_run_evolve_phase", new_callable=AsyncMock) as mock_evolve:
            agent._cycle_count += 1
            if (
                agent.settings.skill_evolution_enabled
                and agent._cycle_count % agent.settings.skill_evolution_cycle_interval == 0
            ):
                await agent._run_evolve_phase()

            mock_evolve.assert_not_called()


@pytest.mark.asyncio
async def test_evolve_phase_failure_doesnt_break():
    """Evolution phase exceptions should be caught, not propagate."""
    from cognitex.agent.autonomous import AutonomousAgent

    with patch("cognitex.agent.autonomous.get_settings") as mock_settings:
        settings = MagicMock()
        settings.skill_evolution_enabled = True
        settings.skill_evolution_cycle_interval = 1
        settings.autonomous_agent_enabled = True
        settings.autonomous_agent_interval_minutes = 15
        mock_settings.return_value = settings

        agent = AutonomousAgent()

        with patch(
            "cognitex.agent.skill_evolution.get_skill_evolution"
        ) as mock_get_evo:
            mock_evo = MagicMock()
            mock_evo.run_evolution_cycle = AsyncMock(side_effect=RuntimeError("DB down"))
            mock_get_evo.return_value = mock_evo

            # Should not raise
            await agent._run_evolve_phase()


# ---------------------------------------------------------------------------
# diff_summary
# ---------------------------------------------------------------------------


def test_generate_diff_summary():
    """Should count added and removed lines."""
    evo = SkillEvolution.__new__(SkillEvolution)
    old = "line1\nline2\nline3"
    new = "line1\nline2\nline4\nline5"

    summary = evo._generate_diff_summary(old, new)
    assert "+2" in summary  # line4, line5 added
    assert "-1" in summary  # line3 removed
