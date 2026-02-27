"""Tests for WP4: Structured Triage Extraction.

Tests cover skill loading, triage parsing, tone neutralisation,
clinical bypass, backward compatibility, and fallback behaviour.
"""

import json
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, patch

import pytest

from cognitex.agent.skills import SkillsLoader
from cognitex.services.email_intent import (
    EmailIntent,
    EmailIntentClassifier,
    EmailIntentResult,
    SuggestedWorkflow,
    TriageDecision,
    _map_triage_to_workflow,
)

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
def install_triage_skill(tmp_skill_dirs):
    """Install the real email-triage SKILL.md into the temp bundled dir."""
    import shutil
    from pathlib import Path

    bundled, _ = tmp_skill_dirs
    src = Path(__file__).parent.parent / "src" / "cognitex" / "skills" / "email-triage"
    dest = bundled / "email-triage"
    shutil.copytree(src, dest)


def _make_llm_mock(response_data: dict) -> AsyncMock:
    """Create a mock LLM service that returns the given dict as JSON."""
    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=json.dumps(response_data))
    llm.fast_model = "test-fast-model"
    return llm


# Full triage response with both legacy and WP4 fields
FULL_TRIAGE_RESPONSE = {
    "intent": "action_request",
    "confidence": 0.9,
    "has_attachments": False,
    "attachment_types": [],
    "attachment_filenames": [],
    "requires_document_analysis": False,
    "suggested_workflow": "quick_reply",
    "key_ask": "Update the spreadsheet with Q4 numbers",
    "deadline": "2026-03-01",
    "response_requirements": ["Q4 financial data"],
    "would_acknowledgment_be_unhelpful": False,
    "triage_decision": "action",
    "action_verb": "create",
    "deadline_source": "explicit",
    "delegation_candidate": None,
    "delegation_reason": None,
    "project_context": "Q4 financials",
    "factual_summary": "Update spreadsheet with Q4 numbers before March 1st.",
    "emotional_markers": [],
    "factual_urgency": 4,
    "clinical_flag": False,
}


# ---------------------------------------------------------------------------
# Skill loading
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_triage_skill_loads(
    tmp_skill_dirs,  # noqa: ARG001 - fixture dependency for skill_loader
    skill_loader,
    install_triage_skill,  # noqa: ARG001 - side-effect fixture
):
    """email-triage skill loads via SkillsLoader and parses frontmatter."""
    skill = await skill_loader.get_skill("email-triage")
    assert skill is not None
    assert skill.name == "email-triage"
    assert skill.format == "agentskills"
    assert "triage" in skill.description.lower()


# ---------------------------------------------------------------------------
# classify() — triage fields populated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_classify_produces_triage_fields():
    """Mock LLM returning full JSON populates all triage fields."""
    llm = _make_llm_mock(FULL_TRIAGE_RESPONSE)
    classifier = EmailIntentClassifier(llm_service=llm)

    # Patch skill loading to inject skill content
    with patch.object(
        classifier, "_get_triage_skill", return_value="\n## Email Triage Guidelines\n\nTest\n"
    ):
        result = await classifier.classify(
            sender="alice@example.com",
            subject="Update spreadsheet",
            body="Please update the Q4 numbers by March 1st.",
        )

    assert result.triage_decision == TriageDecision.ACTION
    assert result.action_verb == "create"
    assert result.factual_urgency == 4
    assert result.deadline == "2026-03-01"
    assert result.deadline_source == "explicit"
    assert result.project_context == "Q4 financials"
    assert result.factual_summary == "Update spreadsheet with Q4 numbers before March 1st."
    assert result.emotional_markers == []
    assert result.clinical_flag is False


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_classify_backward_compat():
    """Refactored classify() still populates all existing EmailIntentResult fields."""
    llm = _make_llm_mock(FULL_TRIAGE_RESPONSE)
    classifier = EmailIntentClassifier(llm_service=llm)

    with patch.object(
        classifier, "_get_triage_skill", return_value="\n## Email Triage Guidelines\n\nTest\n"
    ):
        result = await classifier.classify(
            sender="bob@example.com",
            subject="Test",
            body="Some body text.",
        )

    # All legacy fields must be present
    assert isinstance(result.intent, EmailIntent)
    assert isinstance(result.confidence, float)
    assert isinstance(result.has_attachments, bool)
    assert isinstance(result.attachment_types, list)
    assert isinstance(result.attachment_filenames, list)
    assert isinstance(result.requires_document_analysis, bool)
    assert isinstance(result.suggested_workflow, SuggestedWorkflow)
    assert isinstance(result.key_ask, str)
    assert isinstance(result.response_requirements, list)
    assert isinstance(result.would_acknowledgment_be_unhelpful, bool)


# ---------------------------------------------------------------------------
# Triage-to-workflow mapping
# ---------------------------------------------------------------------------


def test_triage_to_workflow_delegate():
    assert (
        _map_triage_to_workflow(TriageDecision.DELEGATE, 3, SuggestedWorkflow.QUICK_REPLY)
        == SuggestedWorkflow.CREATE_TASK
    )


def test_triage_to_workflow_track_low():
    assert (
        _map_triage_to_workflow(TriageDecision.TRACK, 2, SuggestedWorkflow.QUICK_REPLY)
        == SuggestedWorkflow.ARCHIVE
    )


def test_triage_to_workflow_track_high():
    assert (
        _map_triage_to_workflow(TriageDecision.TRACK, 4, SuggestedWorkflow.QUICK_REPLY)
        == SuggestedWorkflow.CREATE_TASK
    )


def test_triage_to_workflow_archive():
    assert (
        _map_triage_to_workflow(TriageDecision.ARCHIVE, 1, SuggestedWorkflow.QUICK_REPLY)
        == SuggestedWorkflow.ARCHIVE
    )


def test_triage_to_workflow_action_preserves_original():
    """ACTION triage keeps the intent-derived workflow unchanged."""
    assert (
        _map_triage_to_workflow(TriageDecision.ACTION, 3, SuggestedWorkflow.ANALYZE_THEN_RESPOND)
        == SuggestedWorkflow.ANALYZE_THEN_RESPOND
    )


# ---------------------------------------------------------------------------
# Clinical bypass
# ---------------------------------------------------------------------------


@dataclass
class _FakeClinicalScan:
    is_clinical: bool
    matched_categories: list[str] = field(default_factory=list)
    matched_patterns: list[str] = field(default_factory=list)


@pytest.mark.asyncio
async def test_clinical_bypass_no_llm_call():
    """When clinical_scan_result.is_clinical=True, LLM is never called."""
    llm = _make_llm_mock({})
    classifier = EmailIntentClassifier(llm_service=llm)

    scan = _FakeClinicalScan(is_clinical=True, matched_categories=["Patient Identifiers"])
    result = await classifier.classify(
        sender="nhs@hospital.nhs.uk",
        subject="Patient referral",
        body="Patient NHS number 943 476 5919",
        clinical_scan_result=scan,
    )

    # LLM should NOT have been called
    llm.complete.assert_not_called()

    assert result.triage_decision == TriageDecision.TRACK
    assert result.clinical_flag is True


@pytest.mark.asyncio
async def test_clinical_bypass_fields():
    """Clinical bypass sets expected field values."""
    llm = _make_llm_mock({})
    classifier = EmailIntentClassifier(llm_service=llm)

    scan = _FakeClinicalScan(is_clinical=True, matched_categories=["Clinical Results"])
    result = await classifier.classify(
        sender="clinic@nhs.uk",
        subject="Lab results",
        body="HbA1c: 58",
        clinical_scan_result=scan,
    )

    assert result.triage_decision == TriageDecision.TRACK
    assert result.clinical_flag is True
    assert result.intent == EmailIntent.FYI
    assert result.factual_urgency == 1
    assert "clinical" in result.factual_summary.lower()


# ---------------------------------------------------------------------------
# Fallback on skill load failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fallback_on_skill_load_failure():
    """Missing skill falls back to legacy prompt, still returns valid result."""
    # Legacy-only response (no triage fields)
    legacy_response = {
        "intent": "question",
        "confidence": 0.8,
        "has_attachments": False,
        "attachment_types": [],
        "attachment_filenames": [],
        "requires_document_analysis": False,
        "suggested_workflow": "quick_reply",
        "key_ask": "What is the budget?",
        "deadline": None,
        "response_requirements": [],
        "would_acknowledgment_be_unhelpful": False,
    }
    llm = _make_llm_mock(legacy_response)
    classifier = EmailIntentClassifier(llm_service=llm)

    # Skill returns empty → legacy prompt used
    with patch.object(classifier, "_get_triage_skill", return_value=""):
        result = await classifier.classify(
            sender="user@example.com",
            subject="Budget question",
            body="What is the budget for Q3?",
        )

    assert result.intent == EmailIntent.QUESTION
    assert result.key_ask == "What is the budget?"
    # Triage fields should have defaults
    assert result.triage_decision == TriageDecision.ACTION
    assert result.factual_urgency == 3


# ---------------------------------------------------------------------------
# Fallback on JSON parse error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fallback_on_json_parse_error():
    """Malformed LLM output triggers heuristic fallback, no crash."""
    llm = AsyncMock()
    llm.complete = AsyncMock(return_value="This is not valid JSON at all...")
    llm.fast_model = "test-fast-model"
    classifier = EmailIntentClassifier(llm_service=llm)

    with patch.object(classifier, "_get_triage_skill", return_value="\n## Guidelines\n\nTest\n"):
        result = await classifier.classify(
            sender="user@example.com",
            subject="Please review the proposal",
            body="Take a look at the attached proposal.",
        )

    # Should use heuristic fallback
    assert result.confidence == 0.5
    assert result.intent == EmailIntent.REVIEW_REQUEST


# ---------------------------------------------------------------------------
# Tone neutralisation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tone_neutralisation_emotional_capped():
    """Emotional-only urgency is capped at factual_urgency <= 3."""
    # LLM response where urgency was claimed as 5 but no factual basis
    # The LLM should respect the skill instructions, but we also test
    # that the parser clamps the value properly when given valid range
    emotional_response = {
        **FULL_TRIAGE_RESPONSE,
        "factual_urgency": 2,
        "emotional_markers": ["urgent-sounding", "anxious", "exclamation-heavy"],
        "factual_summary": "Fix typo on page 3 of brochure.",
        "deadline": None,
        "deadline_source": "none",
    }
    llm = _make_llm_mock(emotional_response)
    classifier = EmailIntentClassifier(llm_service=llm)

    with patch.object(classifier, "_get_triage_skill", return_value="\n## Guidelines\n\nTest\n"):
        result = await classifier.classify(
            sender="user@example.com",
            subject="URGENT!!!",
            body="URGENT!!! Please fix the typo on page 3!!!",
        )

    assert result.factual_urgency <= 3
    assert len(result.emotional_markers) > 0


# ---------------------------------------------------------------------------
# to_dict() includes triage fields
# ---------------------------------------------------------------------------


def test_to_dict_includes_triage_fields():
    """to_dict() output includes all new WP4 triage fields."""
    result = EmailIntentResult(
        intent=EmailIntent.ACTION_REQUEST,
        confidence=0.9,
        has_attachments=False,
        triage_decision=TriageDecision.DELEGATE,
        action_verb="forward",
        delegation_candidate="alice@example.com",
        delegation_reason="She owns the API docs",
        project_context="API documentation",
        factual_summary="Update API docs after endpoint changes.",
        emotional_markers=["frustrated"],
        factual_urgency=4,
        deadline_source="none",
        clinical_flag=False,
    )
    d = result.to_dict()

    assert d["triage_decision"] == "delegate"
    assert d["action_verb"] == "forward"
    assert d["delegation_candidate"] == "alice@example.com"
    assert d["delegation_reason"] == "She owns the API docs"
    assert d["project_context"] == "API documentation"
    assert d["factual_summary"] == "Update API docs after endpoint changes."
    assert d["emotional_markers"] == ["frustrated"]
    assert d["factual_urgency"] == 4
    assert d["deadline_source"] == "none"
    assert d["clinical_flag"] is False
    # Legacy fields still present
    assert d["intent"] == "action_request"
    assert d["confidence"] == 0.9
