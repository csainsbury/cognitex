"""Feedback Learning System - Semantic retrieval and rule extraction from user feedback.

This module provides the core learning functionality:
1. Record feedback with semantic embeddings for retrieval
2. Retrieve similar past feedback at decision time
3. Extract preference rules from accumulated feedback
4. Provide combined learning context for prompt injection
"""

import json
import uuid
from datetime import datetime, timedelta

import structlog
from sqlalchemy import text

from cognitex.db.postgres import get_session

logger = structlog.get_logger()


async def record_feedback(
    target_type: str,
    target_id: str,
    feedback_category: str | None = None,
    feedback_text: str | None = None,
    was_rejection: bool = False,
    context: dict | None = None,
    action_taken: str | None = None,
) -> str:
    """
    Record user feedback and generate embedding for retrieval.

    Args:
        target_type: Type of item (context_pack, email_draft, task, proposal)
        target_id: ID of the specific item
        feedback_category: Quick-select category (e.g., 'spam_marketing', 'not_needed')
        feedback_text: Free text details for nuanced learning
        was_rejection: Whether this feedback is a rejection
        context: Rich context snapshot (email_subject, sender, etc.)
        action_taken: What action was taken ('rejected', 'edited', 'approved_with_note')

    Returns:
        Feedback ID
    """
    feedback_id = f"fb_{uuid.uuid4().hex[:12]}"
    context = context or {}

    # Generate embedding if there's meaningful text
    embedding_str = None
    if feedback_text and len(feedback_text.strip()) > 10:
        try:
            from cognitex.services.llm import get_llm_service
            llm = get_llm_service()

            # Combine category + text + context for richer embedding
            embed_parts = []
            if feedback_category:
                embed_parts.append(f"Category: {feedback_category}")
            embed_parts.append(feedback_text)
            if context.get("email_subject"):
                embed_parts.append(f"Email subject: {context['email_subject']}")
            if context.get("sender"):
                embed_parts.append(f"From: {context['sender']}")

            embed_text = " | ".join(embed_parts)
            embedding = await llm.generate_embedding(embed_text[:500])
            embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"
        except Exception as e:
            logger.warning("Failed to generate feedback embedding", error=str(e))

    # Store in database
    async for session in get_session():
        try:
            await session.execute(
                text("""
                    INSERT INTO user_feedback (
                        id, target_type, target_id, feedback_category, feedback_text,
                        feedback_embedding, context, was_rejection, action_taken, created_at
                    ) VALUES (
                        :id, :target_type, :target_id, :category, :text,
                        CAST(:embedding AS vector), :context, :was_rejection, :action_taken, NOW()
                    )
                """),
                {
                    "id": feedback_id,
                    "target_type": target_type,
                    "target_id": target_id,
                    "category": feedback_category,
                    "text": feedback_text,
                    "embedding": embedding_str,
                    "context": json.dumps(context),
                    "was_rejection": was_rejection,
                    "action_taken": action_taken,
                },
            )
            await session.commit()
            logger.info(
                "Recorded user feedback",
                feedback_id=feedback_id,
                target_type=target_type,
                category=feedback_category,
                has_text=bool(feedback_text),
            )
        except Exception as e:
            logger.error("Failed to record feedback", error=str(e))
            await session.rollback()
        break

    return feedback_id


async def get_relevant_feedback(
    target_type: str,
    context_text: str,
    limit: int = 5,
    min_similarity: float = 0.65,
) -> list[dict]:
    """
    Retrieve semantically similar past feedback for prompt injection.

    Used at decision time to inject relevant user preferences into the prompt.

    Args:
        target_type: Type of item being decided (email_draft, task, etc.)
        context_text: Current context to match against (email subject, task title, etc.)
        limit: Max feedback items to return
        min_similarity: Minimum cosine similarity threshold

    Returns:
        List of relevant feedback items with similarity scores
    """
    from cognitex.services.llm import get_llm_service

    try:
        llm = get_llm_service()
        query_embedding = await llm.generate_embedding(context_text[:500])
        query_emb_str = "[" + ",".join(str(x) for x in query_embedding) + "]"
    except Exception as e:
        logger.warning("Failed to generate query embedding", error=str(e))
        return []

    results = []
    async for session in get_session():
        try:
            result = await session.execute(
                text("""
                    SELECT
                        id, feedback_category, feedback_text, context, was_rejection,
                        action_taken, created_at,
                        1 - (feedback_embedding <=> CAST(:query_embedding AS vector)) as similarity
                    FROM user_feedback
                    WHERE target_type = :target_type
                      AND feedback_embedding IS NOT NULL
                      AND 1 - (feedback_embedding <=> CAST(:query_embedding AS vector)) >= :min_similarity
                    ORDER BY feedback_embedding <=> CAST(:query_embedding AS vector)
                    LIMIT :limit
                """),
                {
                    "query_embedding": query_emb_str,
                    "target_type": target_type,
                    "min_similarity": min_similarity,
                    "limit": limit,
                },
            )

            for row in result.fetchall():
                context_data = row.context
                if isinstance(context_data, str):
                    try:
                        context_data = json.loads(context_data)
                    except json.JSONDecodeError:
                        context_data = {}

                results.append({
                    "id": row.id,
                    "category": row.feedback_category,
                    "text": row.feedback_text,
                    "context": context_data,
                    "was_rejection": row.was_rejection,
                    "action_taken": row.action_taken,
                    "similarity": float(row.similarity),
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                })
        except Exception as e:
            logger.warning("Failed to retrieve feedback", error=str(e))
        break

    return results


async def get_feedback_summary(
    days_back: int = 30,
    min_samples: int = 2,
) -> dict[str, list[dict]]:
    """
    Get feedback grouped by category for pattern analysis.

    Args:
        days_back: How many days of feedback to include
        min_samples: Minimum samples per category to include

    Returns:
        Dict mapping categories to lists of feedback items
    """
    feedback_by_category: dict[str, list[dict]] = {}
    cutoff = datetime.utcnow() - timedelta(days=days_back)

    async for session in get_session():
        try:
            result = await session.execute(
                text("""
                    SELECT
                        id, target_type, target_id, feedback_category, feedback_text,
                        context, was_rejection, action_taken, created_at
                    FROM user_feedback
                    WHERE created_at >= :cutoff
                      AND feedback_text IS NOT NULL
                    ORDER BY created_at DESC
                """),
                {"cutoff": cutoff},
            )

            for row in result.fetchall():
                category = row.feedback_category or "uncategorized"
                if category not in feedback_by_category:
                    feedback_by_category[category] = []

                context_data = row.context
                if isinstance(context_data, str):
                    try:
                        context_data = json.loads(context_data)
                    except json.JSONDecodeError:
                        context_data = {}

                feedback_by_category[category].append({
                    "id": row.id,
                    "target_type": row.target_type,
                    "target_id": row.target_id,
                    "text": row.feedback_text,
                    "context": context_data,
                    "was_rejection": row.was_rejection,
                    "action_taken": row.action_taken,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                })
        except Exception as e:
            logger.warning("Failed to get feedback summary", error=str(e))
        break

    # Filter to categories with enough samples
    return {
        cat: items
        for cat, items in feedback_by_category.items()
        if len(items) >= min_samples
    }


async def extract_rules_from_feedback(
    min_occurrences: int = 3,
    days_back: int = 30,
) -> list[str]:
    """
    Use LLM to extract preference rules from accumulated feedback.

    Groups feedback by category, uses LLM to identify patterns,
    and creates preference rules in the decision memory system.

    Args:
        min_occurrences: Minimum feedback items per category to analyze
        days_back: How many days of feedback to consider

    Returns:
        List of created rule IDs
    """
    from cognitex.services.llm import get_llm_service

    # Gather feedback by category
    feedback_by_category = await get_feedback_summary(days_back, min_occurrences)

    if not feedback_by_category:
        logger.info("No feedback patterns to extract rules from")
        return []

    llm = get_llm_service()
    rule_ids = []

    for category, feedback_items in feedback_by_category.items():
        if len(feedback_items) < min_occurrences:
            continue

        # Format feedback for LLM analysis
        feedback_text = "\n".join([
            f"- \"{item['text'][:150]}...\" (target: {item['target_type']}, "
            f"context: {item['context'].get('email_subject', item['context'].get('task_title', 'N/A'))[:50]})"
            for item in feedback_items[:10]  # Limit to 10 most recent
        ])

        prompt = f"""Analyze these {len(feedback_items)} user feedback items and extract a clear preference rule.

Feedback items (category: {category}):
{feedback_text}

Based on these feedback patterns, extract a rule that captures the user's preference.

Return a JSON object with:
- rule_name: Short descriptive name (e.g., "Ignore marketing emails", "Prefer concise task titles")
- condition: When this rule applies - JSON object with fields like target_type, sender_pattern, subject_pattern
- preference: What to do when condition matches - JSON object with fields like action, reasoning
- confidence: 0.0-1.0 based on consistency of the feedback
- guidance: A natural language statement of the rule for injection into prompts

Example:
{{"rule_name": "Ignore newsletters", "condition": {{"classification": "newsletter"}}, "preference": {{"action": "skip", "reason": "User never wants tasks from newsletters"}}, "confidence": 0.85, "guidance": "Do not create tasks or drafts for newsletter emails"}}

Return ONLY valid JSON."""

        try:
            response = await llm.complete(
                prompt,
                model=llm.fast_model,
                max_tokens=500,
                temperature=0.2,
            )

            # Parse JSON response
            response = response.strip()
            if response.startswith("```"):
                response = response.split("\n", 1)[1]
                response = response.rsplit("```", 1)[0]

            rule_data = json.loads(response)

            # Store rule in preference_rules via decision memory
            try:
                from cognitex.agent.decision_memory import get_decision_memory
                dm = get_decision_memory()

                rule_id = await dm.rules.create_rule(
                    rule_type="user_feedback",
                    condition=rule_data.get("condition", {}),
                    preference=rule_data.get("preference", {}),
                    rule_name=rule_data.get("rule_name"),
                    confidence=rule_data.get("confidence", 0.5),
                    guidance=rule_data.get("guidance"),
                )
                rule_ids.append(rule_id)
                logger.info(
                    "Extracted rule from feedback",
                    rule_id=rule_id,
                    rule_name=rule_data.get("rule_name"),
                    category=category,
                    samples=len(feedback_items),
                )
            except Exception as e:
                logger.warning("Failed to store extracted rule", error=str(e))

        except json.JSONDecodeError as e:
            logger.warning("Failed to parse rule extraction response", error=str(e))
        except Exception as e:
            logger.warning("Rule extraction failed", category=category, error=str(e))

    return rule_ids


async def get_learning_context_for_decision(
    target_type: str,
    context_summary: str,
    include_rules: bool = True,
    include_similar_feedback: bool = True,
    max_rules: int = 5,
    max_feedback: int = 3,
) -> dict:
    """
    Get combined learning context for injecting into agent prompt.

    Combines:
    1. Validated preference rules from rule memory
    2. Semantically similar past feedback

    Args:
        target_type: Type of decision being made (email_draft, task, etc.)
        context_summary: Brief description of current context for matching
        include_rules: Whether to include preference rules
        include_similar_feedback: Whether to include similar past feedback
        max_rules: Maximum rules to include
        max_feedback: Maximum feedback items to include

    Returns:
        Dict with rules, similar_feedback, and formatted prompt_text
    """
    context = {
        "rules": [],
        "similar_feedback": [],
        "prompt_text": "",
    }

    # Get applicable rules from decision memory
    if include_rules:
        try:
            from cognitex.agent.decision_memory import get_decision_memory
            dm = get_decision_memory()
            rules = await dm.rules.get_weighted_rules_for_prompt(limit=max_rules)
            context["rules"] = rules
        except Exception as e:
            logger.warning("Failed to get rules for learning context", error=str(e))

    # Get similar feedback
    if include_similar_feedback:
        try:
            feedback = await get_relevant_feedback(
                target_type=target_type,
                context_text=context_summary,
                limit=max_feedback,
            )
            context["similar_feedback"] = feedback
        except Exception as e:
            logger.warning("Failed to get similar feedback", error=str(e))

    # Format for prompt injection
    if context["rules"] or context["similar_feedback"]:
        context["prompt_text"] = _format_learning_context_for_prompt(
            context["rules"],
            context["similar_feedback"],
        )

    return context


def _format_learning_context_for_prompt(rules: list, feedback: list) -> str:
    """Format learning context as text for prompt injection."""
    parts = []

    if rules:
        parts.append("## Learned Preferences (from validated rules)")
        for rule in rules:
            guidance = rule.get("guidance") or rule.get("rule_name", "Unknown rule")
            success = rule.get("success_rate", 0)
            uses = rule.get("applications", 0)
            lifecycle = rule.get("lifecycle", "candidate")

            badge = "[PROVEN]" if lifecycle == "validated" else "[LIKELY]"
            parts.append(f"- {badge} {guidance} (success: {success:.0%}, uses: {uses})")

    if feedback:
        parts.append("\n## Relevant Past Feedback")
        for fb in feedback:
            text = fb.get("text", "")[:100]
            if fb.get("was_rejection"):
                parts.append(f"- User rejected similar: \"{text}...\"")
            else:
                parts.append(f"- User noted: \"{text}...\"")

    return "\n".join(parts)


async def get_feedback_stats() -> dict:
    """
    Get statistics about collected feedback for the learning dashboard.

    Returns:
        Dict with feedback counts, categories, and recent items
    """
    stats = {
        "total_count": 0,
        "with_text_count": 0,
        "rejection_count": 0,
        "by_target_type": {},
        "by_category": {},
        "recent": [],
    }

    async for session in get_session():
        try:
            # Total counts
            result = await session.execute(text("""
                SELECT
                    COUNT(*) as total,
                    COUNT(feedback_text) as with_text,
                    SUM(CASE WHEN was_rejection THEN 1 ELSE 0 END) as rejections
                FROM user_feedback
            """))
            row = result.fetchone()
            if row:
                stats["total_count"] = row.total or 0
                stats["with_text_count"] = row.with_text or 0
                stats["rejection_count"] = row.rejections or 0

            # By target type
            result = await session.execute(text("""
                SELECT target_type, COUNT(*) as count
                FROM user_feedback
                GROUP BY target_type
                ORDER BY count DESC
            """))
            for row in result.fetchall():
                stats["by_target_type"][row.target_type] = row.count

            # By category
            result = await session.execute(text("""
                SELECT feedback_category, COUNT(*) as count
                FROM user_feedback
                WHERE feedback_category IS NOT NULL
                GROUP BY feedback_category
                ORDER BY count DESC
            """))
            for row in result.fetchall():
                stats["by_category"][row.feedback_category] = row.count

            # Recent feedback with text
            result = await session.execute(text("""
                SELECT id, target_type, feedback_category, feedback_text, context, created_at
                FROM user_feedback
                WHERE feedback_text IS NOT NULL
                ORDER BY created_at DESC
                LIMIT 10
            """))
            for row in result.fetchall():
                context_data = row.context
                if isinstance(context_data, str):
                    try:
                        context_data = json.loads(context_data)
                    except json.JSONDecodeError:
                        context_data = {}

                stats["recent"].append({
                    "id": row.id,
                    "target_type": row.target_type,
                    "category": row.feedback_category,
                    "text": row.feedback_text[:200] if row.feedback_text else None,
                    "context": context_data,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                })
        except Exception as e:
            logger.warning("Failed to get feedback stats", error=str(e))
        break

    return stats
