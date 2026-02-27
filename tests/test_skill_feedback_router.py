"""Tests for the skill feedback router (WP3 Phase C)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cognitex.agent.skill_feedback_router import (
    ACTION_SKILL_MAP,
    REASON_FEEDBACK_MAP,
    infer_feedback_type,
    route_correction_to_skill,
    route_rejection_to_skill,
    submit_manual_feedback,
)


# ---------------------------------------------------------------------------
# infer_feedback_type (pure function)
# ---------------------------------------------------------------------------


def test_infer_feedback_type_spam():
    assert infer_feedback_type("spam_marketing") == "false_positive"


def test_infer_feedback_type_automated():
    assert infer_feedback_type("automated_email") == "false_positive"


def test_infer_feedback_type_not_actionable():
    assert infer_feedback_type("not_actionable") == "false_positive"


def test_infer_feedback_type_bad_suggestion():
    assert infer_feedback_type("bad_suggestion") == "correction"


def test_infer_feedback_type_wrong_timing():
    assert infer_feedback_type("wrong_timing") == "correction"


def test_infer_feedback_type_manual():
    assert infer_feedback_type("will_handle_manually") == "missing_case"


def test_infer_feedback_type_default():
    assert infer_feedback_type("totally_unknown_reason") == "suggestion"


def test_infer_feedback_type_other():
    assert infer_feedback_type("other") == "suggestion"


# ---------------------------------------------------------------------------
# route_rejection_to_skill
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_rejection_known_skill():
    """Task rejection should route to 'email-tasks' skill."""
    mock_evo = MagicMock()
    mock_evo.add_feedback = AsyncMock(return_value="fb_abc123")

    with patch("cognitex.agent.skill_feedback_router.get_skill_evolution", return_value=mock_evo):
        result = await route_rejection_to_skill(
            proposal_type="create_task",
            reason="spam_marketing",
            context={"task_title": "Follow up on promo", "email_subject": "50% off sale"},
        )

    assert result == "fb_abc123"
    mock_evo.add_feedback.assert_called_once()
    entry = mock_evo.add_feedback.call_args[0][0]
    assert entry.skill_name == "email-tasks"
    assert entry.feedback_type == "false_positive"
    assert "spam_marketing" in entry.description


@pytest.mark.asyncio
async def test_route_rejection_unknown_type():
    """Unknown proposal_type should return None without calling add_feedback."""
    mock_evo = MagicMock()
    mock_evo.add_feedback = AsyncMock()

    with patch("cognitex.agent.skill_feedback_router.get_skill_evolution", return_value=mock_evo):
        result = await route_rejection_to_skill(
            proposal_type="unknown_action_type",
            reason="bad_suggestion",
        )

    assert result is None
    mock_evo.add_feedback.assert_not_called()


@pytest.mark.asyncio
async def test_route_rejection_draft_email():
    """Draft rejection should route to 'email-tasks' skill."""
    mock_evo = MagicMock()
    mock_evo.add_feedback = AsyncMock(return_value="fb_draft1")

    with patch("cognitex.agent.skill_feedback_router.get_skill_evolution", return_value=mock_evo):
        result = await route_rejection_to_skill(
            proposal_type="draft_email",
            reason="wrong_recipient",
            context={"email_subject": "Re: meeting", "email_sender": "alice@example.com"},
        )

    assert result == "fb_draft1"
    entry = mock_evo.add_feedback.call_args[0][0]
    assert entry.skill_name == "email-tasks"
    assert entry.feedback_type == "correction"


@pytest.mark.asyncio
async def test_route_rejection_context_pack():
    """Context pack rejection should route to 'meeting-prep' skill."""
    mock_evo = MagicMock()
    mock_evo.add_feedback = AsyncMock(return_value="fb_ctx1")

    with patch("cognitex.agent.skill_feedback_router.get_skill_evolution", return_value=mock_evo):
        result = await route_rejection_to_skill(
            proposal_type="context_pack",
            reason="not_relevant",
        )

    assert result == "fb_ctx1"
    entry = mock_evo.add_feedback.call_args[0][0]
    assert entry.skill_name == "meeting-prep"
    assert entry.feedback_type == "false_positive"


# ---------------------------------------------------------------------------
# route_correction_to_skill
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_correction():
    """Edits should create 'correction' feedback."""
    mock_evo = MagicMock()
    mock_evo.add_feedback = AsyncMock(return_value="fb_corr1")

    with patch("cognitex.agent.skill_feedback_router.get_skill_evolution", return_value=mock_evo):
        result = await route_correction_to_skill(
            proposal_type="create_task",
            original="Follow up on newsletter",
            corrected="Ignore newsletter emails",
            context={"email_subject": "Weekly digest"},
        )

    assert result == "fb_corr1"
    entry = mock_evo.add_feedback.call_args[0][0]
    assert entry.skill_name == "email-tasks"
    assert entry.feedback_type == "correction"
    assert "original=" in entry.description
    assert "corrected=" in entry.description


@pytest.mark.asyncio
async def test_route_correction_unknown_type():
    """Unknown proposal_type in correction should return None."""
    mock_evo = MagicMock()
    mock_evo.add_feedback = AsyncMock()

    with patch("cognitex.agent.skill_feedback_router.get_skill_evolution", return_value=mock_evo):
        result = await route_correction_to_skill(
            proposal_type="unknown_type",
            original="x",
            corrected="y",
        )

    assert result is None
    mock_evo.add_feedback.assert_not_called()


# ---------------------------------------------------------------------------
# submit_manual_feedback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_manual_validates_skill():
    """Invalid skill name should raise ValueError."""
    mock_loader = MagicMock()
    mock_loader.get_skill = AsyncMock(return_value=None)

    with patch("cognitex.agent.skill_feedback_router.get_skills_loader", return_value=mock_loader):
        with pytest.raises(ValueError, match="not found"):
            await submit_manual_feedback("nonexistent-skill", "correction", "test")


@pytest.mark.asyncio
async def test_submit_manual_success():
    """Valid submission should call add_feedback and return feedback_id."""
    mock_loader = MagicMock()
    mock_loader.get_skill = AsyncMock(return_value=MagicMock())

    mock_evo = MagicMock()
    mock_evo.add_feedback = AsyncMock(return_value="fb_manual1")

    with (
        patch("cognitex.agent.skill_feedback_router.get_skills_loader", return_value=mock_loader),
        patch("cognitex.agent.skill_feedback_router.get_skill_evolution", return_value=mock_evo),
    ):
        result = await submit_manual_feedback("email-tasks", "false_positive", "Too many tasks")

    assert result == "fb_manual1"
    entry = mock_evo.add_feedback.call_args[0][0]
    assert entry.skill_name == "email-tasks"
    assert entry.feedback_type == "false_positive"
    assert entry.description == "Too many tasks"


@pytest.mark.asyncio
async def test_submit_manual_normalizes_invalid_type():
    """Invalid feedback_type should be normalized to 'suggestion'."""
    mock_loader = MagicMock()
    mock_loader.get_skill = AsyncMock(return_value=MagicMock())

    mock_evo = MagicMock()
    mock_evo.add_feedback = AsyncMock(return_value="fb_manual2")

    with (
        patch("cognitex.agent.skill_feedback_router.get_skills_loader", return_value=mock_loader),
        patch("cognitex.agent.skill_feedback_router.get_skill_evolution", return_value=mock_evo),
    ):
        await submit_manual_feedback("email-tasks", "invalid_type", "test")

    entry = mock_evo.add_feedback.call_args[0][0]
    assert entry.feedback_type == "suggestion"


# ---------------------------------------------------------------------------
# Routing failure is silent (integration-style)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_routing_failure_is_silent():
    """Exception in add_feedback should propagate (caller wraps in try/except)."""
    mock_evo = MagicMock()
    mock_evo.add_feedback = AsyncMock(side_effect=RuntimeError("DB down"))

    with patch("cognitex.agent.skill_feedback_router.get_skill_evolution", return_value=mock_evo):
        # The router itself doesn't swallow exceptions — callers in app.py do
        with pytest.raises(RuntimeError, match="DB down"):
            await route_rejection_to_skill(
                proposal_type="create_task",
                reason="bad_suggestion",
            )
