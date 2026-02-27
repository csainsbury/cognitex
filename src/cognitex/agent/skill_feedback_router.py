"""Skill Feedback Router (WP3 Phase C)

Routes rejection and correction signals from the web UI into the skill
feedback system so the evolution cycle can refine skills based on real
operator behaviour.

Three entry points:
- route_rejection_to_skill() — called from task/draft/inbox rejection flows
- route_correction_to_skill() — called when edits are approved-with-changes
- submit_manual_feedback() — called from the evolution dashboard form
"""

import structlog

from cognitex.agent.skill_evolution import FeedbackEntry, get_skill_evolution
from cognitex.agent.skills import get_skills_loader

logger = structlog.get_logger()


# =============================================================================
# Mappings
# =============================================================================

# Known mappings from proposal_type/action_type to skill name
ACTION_SKILL_MAP: dict[str, str] = {
    "create_task": "email-tasks",
    "task_proposal": "email-tasks",
    "draft_email": "email-tasks",
    "context_pack": "meeting-prep",
    "compile_context_pack": "meeting-prep",
}

# Reason category → feedback_type for skill_feedback
REASON_FEEDBACK_MAP: dict[str, str] = {
    # False positives — skill triggered when it shouldn't have
    "spam_marketing": "false_positive",
    "automated_email": "false_positive",
    "not_actionable": "false_positive",
    "not_relevant": "false_positive",
    # Corrections — skill output was wrong
    "bad_suggestion": "correction",
    "wrong_timing": "correction",
    "wrong_recipient": "correction",
    # Missing cases — skill didn't account for something
    "will_handle_manually": "missing_case",
    "other": "suggestion",
}


# =============================================================================
# Pure helpers
# =============================================================================


def infer_feedback_type(reason_category: str) -> str:
    """Map a rejection reason category to a FeedbackEntry feedback_type."""
    return REASON_FEEDBACK_MAP.get(reason_category, "suggestion")


# =============================================================================
# Routing functions
# =============================================================================


async def route_rejection_to_skill(
    proposal_type: str,
    reason: str,
    context: dict | None = None,
) -> str | None:
    """Route a rejection event to the appropriate skill's feedback.

    Returns the feedback_id if routed, or None if the proposal_type has no
    known skill mapping.
    """
    skill_name = ACTION_SKILL_MAP.get(proposal_type)
    if not skill_name:
        return None

    feedback_type = infer_feedback_type(reason)

    # Build a human-readable description from context
    ctx = context or {}
    parts = [f"Rejected ({reason})"]
    if ctx.get("task_title"):
        parts.append(f"task={ctx['task_title'][:80]}")
    if ctx.get("email_subject"):
        parts.append(f"subject={ctx['email_subject'][:80]}")
    description = "; ".join(parts)

    entry = FeedbackEntry(
        skill_name=skill_name,
        feedback_type=feedback_type,
        description=description,
    )

    evolution = get_skill_evolution()
    feedback_id = await evolution.add_feedback(entry)
    logger.info(
        "Routed rejection to skill feedback",
        skill=skill_name,
        feedback_type=feedback_type,
        feedback_id=feedback_id,
    )
    return feedback_id


async def route_correction_to_skill(
    proposal_type: str,
    original: str,
    corrected: str,
    context: dict | None = None,
) -> str | None:
    """Route an edit/correction (approved-with-changes) to skill feedback.

    Returns the feedback_id if routed, or None if no skill mapping exists.
    """
    skill_name = ACTION_SKILL_MAP.get(proposal_type)
    if not skill_name:
        return None

    ctx = context or {}
    description = f"Corrected output: original={original[:100]}, corrected={corrected[:100]}"
    if ctx.get("email_subject"):
        description += f"; subject={ctx['email_subject'][:80]}"

    entry = FeedbackEntry(
        skill_name=skill_name,
        feedback_type="correction",
        description=description,
    )

    evolution = get_skill_evolution()
    feedback_id = await evolution.add_feedback(entry)
    logger.info(
        "Routed correction to skill feedback",
        skill=skill_name,
        feedback_id=feedback_id,
    )
    return feedback_id


async def submit_manual_feedback(
    skill_name: str,
    feedback_type: str,
    description: str,
) -> str:
    """Submit manual feedback from the evolution dashboard.

    Validates the skill exists before recording.

    Raises:
        ValueError: If the skill_name doesn't exist.
    """
    loader = get_skills_loader()
    skill = await loader.get_skill(skill_name)
    if not skill:
        raise ValueError(f"Skill '{skill_name}' not found")

    if feedback_type not in ("correction", "missing_case", "false_positive", "suggestion"):
        feedback_type = "suggestion"

    entry = FeedbackEntry(
        skill_name=skill_name,
        feedback_type=feedback_type,
        description=description,
    )

    evolution = get_skill_evolution()
    feedback_id = await evolution.add_feedback(entry)
    logger.info(
        "Manual skill feedback submitted",
        skill=skill_name,
        feedback_type=feedback_type,
        feedback_id=feedback_id,
    )
    return feedback_id
