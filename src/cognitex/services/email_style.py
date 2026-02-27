"""Email Writing Style Analysis Service.

Extracts and manages writing style profiles from sent emails to enable
personalized draft generation that matches the user's voice.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import text

logger = structlog.get_logger()


@dataclass
class StyleMetrics:
    """Metrics describing an email's writing style."""

    # Structure
    avg_length: int = 0                      # Average email length in chars
    greeting_style: str = ""                 # "Hi NAME", "Dear NAME", "NAME,", "none"
    closing_style: str = ""                  # "Best", "Thanks", "Cheers", "none"
    signature_present: bool = False

    # Tone (0.0 to 1.0 scale)
    formality: float = 0.5                   # 0 (casual) to 1 (formal)
    directness: float = 0.5                  # 0 (indirect) to 1 (direct)
    warmth: float = 0.5                      # 0 (cold) to 1 (warm)

    # Patterns
    uses_bullet_points: bool = False
    uses_questions: bool = False
    typical_paragraph_count: int = 1
    sentence_complexity: float = 15.0        # avg words per sentence

    # Vocabulary
    common_phrases: list[str] = field(default_factory=list)
    avoided_words: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StyleMetrics":
        """Create from dictionary."""
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class StyleProfile:
    """Aggregated style profile for a recipient or general use."""

    recipient_email: str | None              # None for general profile
    metrics: StyleMetrics
    sample_count: int = 1
    last_updated: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "recipient_email": self.recipient_email,
            "metrics": self.metrics.to_dict(),
            "sample_count": self.sample_count,
            "last_updated": self.last_updated.isoformat(),
        }


@dataclass
class StyleDiff:
    """Difference between a draft and target style."""

    formality_diff: float = 0.0              # Positive = draft more formal
    directness_diff: float = 0.0
    warmth_diff: float = 0.0
    length_diff: int = 0                     # Positive = draft longer
    greeting_matches: bool = True
    closing_matches: bool = True
    suggestions: list[str] = field(default_factory=list)

    @property
    def needs_adjustment(self) -> bool:
        """Check if draft significantly differs from target."""
        return (
            abs(self.formality_diff) > 0.3
            or abs(self.directness_diff) > 0.3
            or abs(self.warmth_diff) > 0.3
            or not self.greeting_matches
            or not self.closing_matches
        )


class EmailStyleAnalyzer:
    """Analyzes and manages email writing style profiles.

    Extracts style metrics from sent emails and maintains profiles
    for personalized draft generation.
    """

    def __init__(self):
        self._llm = None
        self._common_greetings = [
            "hi", "hello", "hey", "dear", "good morning",
            "good afternoon", "good evening"
        ]
        self._common_closings = [
            "best", "thanks", "thank you", "cheers", "regards",
            "best regards", "kind regards", "sincerely", "take care"
        ]

    @property
    def llm(self):
        """Lazy-load LLM service."""
        if self._llm is None:
            from cognitex.services.llm import get_llm_service
            self._llm = get_llm_service()
        return self._llm

    async def extract_style(
        self,
        email_body: str,
        recipient: str | None = None,
    ) -> StyleMetrics:
        """Extract style metrics from an email body.

        Args:
            email_body: The email text content
            recipient: Optional recipient email for context

        Returns:
            StyleMetrics with extracted characteristics
        """
        if not email_body or len(email_body.strip()) < 10:
            return StyleMetrics()

        # Basic structural analysis (fast, no LLM)
        metrics = self._analyze_structure(email_body)

        # LLM-based tone analysis (slower, more accurate)
        try:
            tone_metrics = await self._analyze_tone_with_llm(email_body)
            metrics.formality = tone_metrics.get("formality", 0.5)
            metrics.directness = tone_metrics.get("directness", 0.5)
            metrics.warmth = tone_metrics.get("warmth", 0.5)
            metrics.common_phrases = tone_metrics.get("common_phrases", [])
        except Exception as e:
            logger.warning("LLM tone analysis failed, using defaults", error=str(e))

        return metrics

    def _analyze_structure(self, email_body: str) -> StyleMetrics:
        """Analyze structural elements of the email."""
        lines = email_body.strip().split("\n")
        paragraphs = [p for p in email_body.split("\n\n") if p.strip()]

        # Length
        avg_length = len(email_body)

        # Greeting detection
        greeting_style = "none"
        if lines:
            first_line = lines[0].lower().strip()
            for greeting in self._common_greetings:
                if first_line.startswith(greeting):
                    # Detect pattern: "Hi NAME" vs "Hi,"
                    if "," in first_line:
                        if first_line == f"{greeting},":
                            greeting_style = f"{greeting.title()},"
                        else:
                            # "Hi NAME," pattern
                            greeting_style = f"{greeting.title()} NAME,"
                    else:
                        greeting_style = f"{greeting.title()} NAME"
                    break

        # Closing detection
        closing_style = "none"
        signature_present = False
        for i, line in enumerate(reversed(lines[-5:])):
            line_lower = line.lower().strip().rstrip(",")
            for closing in self._common_closings:
                if line_lower == closing or line_lower.startswith(closing):
                    closing_style = closing.title()
                    # Check for signature (name after closing)
                    if i > 0:
                        signature_present = True
                    break
            if closing_style != "none":
                break

        # Bullet points
        uses_bullet_points = any(
            line.strip().startswith(("-", "*", "•", "1.", "2."))
            for line in lines
        )

        # Questions
        uses_questions = "?" in email_body

        # Sentence complexity
        sentences = re.split(r"[.!?]+", email_body)
        sentences = [s.strip() for s in sentences if s.strip()]
        if sentences:
            total_words = sum(len(s.split()) for s in sentences)
            sentence_complexity = total_words / len(sentences)
        else:
            sentence_complexity = 15.0

        return StyleMetrics(
            avg_length=avg_length,
            greeting_style=greeting_style,
            closing_style=closing_style,
            signature_present=signature_present,
            uses_bullet_points=uses_bullet_points,
            uses_questions=uses_questions,
            typical_paragraph_count=len(paragraphs),
            sentence_complexity=sentence_complexity,
        )

    async def _analyze_tone_with_llm(self, email_body: str) -> dict:
        """Use LLM to analyze tone characteristics."""
        prompt = f"""Analyze the writing style of this email and return JSON with these fields:

- formality: 0.0 (very casual, slang) to 1.0 (very formal, professional)
- directness: 0.0 (indirect, hedging) to 1.0 (direct, to the point)
- warmth: 0.0 (cold, detached) to 1.0 (warm, friendly)
- common_phrases: list of 2-5 distinctive phrases the writer uses (e.g., "Let me know", "Happy to help")

EMAIL:
{email_body[:2000]}

Return ONLY valid JSON, no explanation."""

        response = await self.llm.complete(
            prompt=prompt,
            max_tokens=300,
            temperature=0.1,
            task="draft",
        )

        # Parse JSON from response
        try:
            # Try to extract JSON from response
            text = response.strip()
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0]
            elif "```" in text:
                text = text.split("```")[1].split("```")[0]

            return json.loads(text)
        except json.JSONDecodeError:
            logger.warning("Failed to parse LLM style response", response=response[:100])
            return {}

    async def compare_styles(
        self,
        draft: str,
        target_style: StyleMetrics,
    ) -> StyleDiff:
        """Compare a draft against a target style profile.

        Args:
            draft: The draft email text
            target_style: The target StyleMetrics to match

        Returns:
            StyleDiff with comparison results and suggestions
        """
        draft_style = await self.extract_style(draft)

        diff = StyleDiff(
            formality_diff=draft_style.formality - target_style.formality,
            directness_diff=draft_style.directness - target_style.directness,
            warmth_diff=draft_style.warmth - target_style.warmth,
            length_diff=draft_style.avg_length - target_style.avg_length,
            greeting_matches=(
                draft_style.greeting_style == target_style.greeting_style
                or target_style.greeting_style == "none"
            ),
            closing_matches=(
                draft_style.closing_style == target_style.closing_style
                or target_style.closing_style == "none"
            ),
        )

        # Generate suggestions
        suggestions = []
        if diff.formality_diff > 0.3:
            suggestions.append("Make the tone more casual")
        elif diff.formality_diff < -0.3:
            suggestions.append("Make the tone more professional")

        if diff.warmth_diff < -0.3:
            suggestions.append("Add warmer, friendlier language")
        elif diff.warmth_diff > 0.3:
            suggestions.append("Tone down the friendliness slightly")

        if not diff.greeting_matches and target_style.greeting_style != "none":
            suggestions.append(f"Use greeting style: {target_style.greeting_style}")

        if not diff.closing_matches and target_style.closing_style != "none":
            suggestions.append(f"Use closing: {target_style.closing_style}")

        diff.suggestions = suggestions
        return diff

    async def generate_style_guidance(
        self,
        recipient_email: str | None = None,
    ) -> str:
        """Generate style guidance text for prompt injection.

        Args:
            recipient_email: Optional specific recipient to get style for

        Returns:
            Human-readable style guidance for the LLM
        """
        # Get profile (recipient-specific or general)
        profile = await self.get_profile(recipient_email)

        if profile is None or profile.sample_count < 2:
            # Not enough data, return minimal guidance
            general_profile = await self.get_profile(None)
            if general_profile and general_profile.sample_count >= 3:
                profile = general_profile
            else:
                return "Write in a professional but friendly tone."

        m = profile.metrics

        # Build guidance
        guidance_parts = []

        # Tone
        if m.formality > 0.7:
            guidance_parts.append("Use a formal, professional tone")
        elif m.formality < 0.3:
            guidance_parts.append("Use a casual, conversational tone")
        else:
            guidance_parts.append("Use a balanced professional-yet-friendly tone")

        if m.warmth > 0.7:
            guidance_parts.append("Be warm and personable")
        elif m.warmth < 0.3:
            guidance_parts.append("Keep the tone businesslike")

        if m.directness > 0.7:
            guidance_parts.append("Be direct and to the point")
        elif m.directness < 0.3:
            guidance_parts.append("Use softer, more diplomatic phrasing")

        # Structure
        if m.greeting_style and m.greeting_style != "none":
            guidance_parts.append(f"Start with: {m.greeting_style}")

        if m.closing_style and m.closing_style != "none":
            guidance_parts.append(f"End with: {m.closing_style}")

        if m.uses_bullet_points:
            guidance_parts.append("Use bullet points for lists")

        # Length guidance
        if m.avg_length < 200:
            guidance_parts.append("Keep it brief (under 200 chars)")
        elif m.avg_length > 1000:
            guidance_parts.append("Longer, detailed responses are okay")

        # Common phrases
        if m.common_phrases:
            phrases = ", ".join(f'"{p}"' for p in m.common_phrases[:3])
            guidance_parts.append(f"Consider using phrases like: {phrases}")

        return ". ".join(guidance_parts) + "."

    async def get_profile(
        self,
        recipient_email: str | None = None,
    ) -> StyleProfile | None:
        """Get a style profile from the database.

        Args:
            recipient_email: Specific recipient, or None for general profile

        Returns:
            StyleProfile if found, None otherwise
        """
        from cognitex.db.postgres import get_session

        async for session in get_session():
            try:
                if recipient_email:
                    result = await session.execute(
                        text("""
                            SELECT recipient_email, metrics, sample_count, last_updated
                            FROM email_style_profiles
                            WHERE recipient_email = :email
                        """),
                        {"email": recipient_email},
                    )
                else:
                    result = await session.execute(
                        text("""
                            SELECT recipient_email, metrics, sample_count, last_updated
                            FROM email_style_profiles
                            WHERE recipient_email IS NULL
                        """)
                    )

                row = result.fetchone()
                if row:
                    metrics_data = row[1]
                    if isinstance(metrics_data, str):
                        metrics_data = json.loads(metrics_data)

                    return StyleProfile(
                        recipient_email=row[0],
                        metrics=StyleMetrics.from_dict(metrics_data),
                        sample_count=row[2],
                        last_updated=row[3],
                    )
            except Exception as e:
                logger.warning("Failed to get style profile", error=str(e))
            break

        return None

    async def update_recipient_profile(
        self,
        recipient: str | None,
        metrics: StyleMetrics,
    ) -> None:
        """Update the style profile for a recipient.

        Merges new metrics with existing profile using weighted average.

        Args:
            recipient: Recipient email, or None for general profile
            metrics: New StyleMetrics to incorporate
        """
        from cognitex.db.postgres import get_session

        existing = await self.get_profile(recipient)

        if existing:
            # Weighted average with existing metrics
            weight = 1.0 / (existing.sample_count + 1)
            old_weight = 1.0 - weight

            merged = StyleMetrics(
                avg_length=int(
                    existing.metrics.avg_length * old_weight
                    + metrics.avg_length * weight
                ),
                greeting_style=metrics.greeting_style or existing.metrics.greeting_style,
                closing_style=metrics.closing_style or existing.metrics.closing_style,
                signature_present=metrics.signature_present or existing.metrics.signature_present,
                formality=existing.metrics.formality * old_weight + metrics.formality * weight,
                directness=existing.metrics.directness * old_weight + metrics.directness * weight,
                warmth=existing.metrics.warmth * old_weight + metrics.warmth * weight,
                uses_bullet_points=metrics.uses_bullet_points or existing.metrics.uses_bullet_points,
                uses_questions=metrics.uses_questions or existing.metrics.uses_questions,
                typical_paragraph_count=int(
                    existing.metrics.typical_paragraph_count * old_weight
                    + metrics.typical_paragraph_count * weight
                ),
                sentence_complexity=(
                    existing.metrics.sentence_complexity * old_weight
                    + metrics.sentence_complexity * weight
                ),
                common_phrases=self._merge_phrases(
                    existing.metrics.common_phrases,
                    metrics.common_phrases,
                ),
            )
            new_count = existing.sample_count + 1
        else:
            merged = metrics
            new_count = 1

        async for session in get_session():
            try:
                await session.execute(
                    text("""
                        INSERT INTO email_style_profiles (recipient_email, metrics, sample_count, last_updated)
                        VALUES (:recipient, :metrics, :count, NOW())
                        ON CONFLICT (recipient_email)
                        DO UPDATE SET
                            metrics = :metrics,
                            sample_count = :count,
                            last_updated = NOW()
                    """),
                    {
                        "recipient": recipient,
                        "metrics": json.dumps(merged.to_dict()),
                        "count": new_count,
                    },
                )
                await session.commit()
                logger.debug(
                    "Updated style profile",
                    recipient=recipient,
                    sample_count=new_count,
                )
            except Exception as e:
                logger.error("Failed to update style profile", error=str(e))
            break

    def _merge_phrases(
        self,
        existing: list[str],
        new: list[str],
        max_phrases: int = 10,
    ) -> list[str]:
        """Merge phrase lists, keeping most common."""
        # Simple merge keeping unique phrases
        combined = list(dict.fromkeys(existing + new))
        return combined[:max_phrases]

    async def analyze_sent_emails_batch(
        self,
        emails: list[dict],
    ) -> None:
        """Analyze a batch of sent emails to build profiles.

        Args:
            emails: List of email dicts with 'body', 'to' keys
        """
        for email in emails:
            body = email.get("body", "")
            recipient = email.get("to")

            if not body:
                continue

            try:
                metrics = await self.extract_style(body, recipient)

                # Update recipient-specific profile
                if recipient:
                    await self.update_recipient_profile(recipient, metrics)

                # Always update general profile
                await self.update_recipient_profile(None, metrics)

            except Exception as e:
                logger.warning(
                    "Failed to analyze email style",
                    recipient=recipient,
                    error=str(e),
                )


# Singleton instance
_analyzer: EmailStyleAnalyzer | None = None


def get_email_style_analyzer() -> EmailStyleAnalyzer:
    """Get or create the email style analyzer singleton."""
    global _analyzer
    if _analyzer is None:
        _analyzer = EmailStyleAnalyzer()
    return _analyzer


# =============================================================================
# Email Draft Lifecycle Tracking
# =============================================================================

def _levenshtein_ratio(s1: str, s2: str) -> float:
    """Calculate the Levenshtein similarity ratio between two strings.

    Returns a float between 0.0 (completely different) and 1.0 (identical).
    """
    if not s1 and not s2:
        return 1.0
    if not s1 or not s2:
        return 0.0

    # For efficiency, just use a simple character-based ratio
    # In production, consider using rapidfuzz or similar
    len1, len2 = len(s1), len(s2)
    max_len = max(len1, len2)

    if max_len == 0:
        return 1.0

    # Simple approach: count common characters at same positions
    # (not true Levenshtein but fast approximation)
    min_len = min(len1, len2)
    matches = sum(1 for i in range(min_len) if s1[i] == s2[i])

    # Also consider overall length difference
    length_penalty = abs(len1 - len2) / max_len

    return (matches / max_len) * (1 - length_penalty * 0.5)


def _classify_edit_type(edit_ratio: float) -> str:
    """Classify the type of edit based on similarity ratio."""
    if edit_ratio >= 0.95:
        return "none"  # Essentially unchanged
    elif edit_ratio >= 0.8:
        return "minor"  # Small tweaks
    elif edit_ratio >= 0.5:
        return "moderate"  # Significant changes
    elif edit_ratio >= 0.2:
        return "major"  # Substantial rewrite
    else:
        return "rewrite"  # Completely rewritten


async def track_draft_created(
    draft_id: str,
    recipient_email: str | None,
    subject: str,
    body: str,
    reply_to_email_id: str | None = None,
    created_by: str = "agent",
) -> None:
    """Record a new draft being created for lifecycle tracking."""
    from cognitex.db.postgres import get_session

    async for session in get_session():
        try:
            await session.execute(
                text("""
                    INSERT INTO email_draft_lifecycle (
                        draft_id, recipient_email, subject,
                        original_body, original_length,
                        reply_to_email_id, created_by, status
                    ) VALUES (
                        :draft_id, :recipient, :subject,
                        :body, :length,
                        :reply_to, :created_by, 'created'
                    )
                    ON CONFLICT (draft_id) DO NOTHING
                """),
                {
                    "draft_id": draft_id,
                    "recipient": recipient_email,
                    "subject": subject,
                    "body": body,
                    "length": len(body),
                    "reply_to": reply_to_email_id,
                    "created_by": created_by,
                },
            )
            await session.commit()
            logger.debug("Tracked draft creation", draft_id=draft_id)
        except Exception as e:
            logger.warning("Failed to track draft creation", error=str(e))
        break


async def track_draft_sent(
    draft_id: str,
    final_body: str,
) -> dict | None:
    """Record a draft being sent and analyze the edits.

    Returns dict with edit analysis if significant changes were made.
    """
    from cognitex.db.postgres import get_session

    edit_analysis = None

    async for session in get_session():
        try:
            # Get the original draft
            result = await session.execute(
                text("""
                    SELECT original_body, recipient_email
                    FROM email_draft_lifecycle
                    WHERE draft_id = :draft_id
                """),
                {"draft_id": draft_id},
            )
            row = result.fetchone()

            if not row:
                logger.warning("Draft not found for tracking", draft_id=draft_id)
                return None

            original_body = row[0]
            recipient_email = row[1]

            # Calculate edit metrics
            edit_ratio = _levenshtein_ratio(original_body, final_body)
            edit_type = _classify_edit_type(edit_ratio)

            # Update the record
            await session.execute(
                text("""
                    UPDATE email_draft_lifecycle
                    SET final_body = :final_body,
                        final_length = :final_length,
                        edit_ratio = :edit_ratio,
                        edit_type = :edit_type,
                        status = 'sent',
                        sent_at = NOW()
                    WHERE draft_id = :draft_id
                """),
                {
                    "draft_id": draft_id,
                    "final_body": final_body,
                    "final_length": len(final_body),
                    "edit_ratio": edit_ratio,
                    "edit_type": edit_type,
                },
            )
            await session.commit()

            logger.info(
                "Tracked draft sent",
                draft_id=draft_id,
                edit_ratio=round(edit_ratio, 2),
                edit_type=edit_type,
            )

            # If significant edits were made, return analysis for learning
            if edit_type in ["moderate", "major", "rewrite"]:
                edit_analysis = {
                    "draft_id": draft_id,
                    "recipient_email": recipient_email,
                    "original_body": original_body,
                    "final_body": final_body,
                    "edit_ratio": edit_ratio,
                    "edit_type": edit_type,
                }

        except Exception as e:
            logger.warning("Failed to track draft sent", error=str(e))
        break

    # If significant edits, trigger learning
    if edit_analysis:
        await _learn_from_draft_edits(edit_analysis)

    return edit_analysis


async def track_draft_discarded(draft_id: str) -> None:
    """Record a draft being discarded."""
    from cognitex.db.postgres import get_session

    async for session in get_session():
        try:
            await session.execute(
                text("""
                    UPDATE email_draft_lifecycle
                    SET status = 'discarded',
                        discarded_at = NOW()
                    WHERE draft_id = :draft_id
                """),
                {"draft_id": draft_id},
            )
            await session.commit()
            logger.debug("Tracked draft discarded", draft_id=draft_id)
        except Exception as e:
            logger.warning("Failed to track draft discard", error=str(e))
        break


async def _learn_from_draft_edits(edit_analysis: dict) -> None:
    """Learn from significant draft edits to improve future drafts.

    When the user makes significant edits to a draft, analyze what changed
    and update the style profile accordingly.
    """
    try:
        original = edit_analysis["original_body"]
        final = edit_analysis["final_body"]
        recipient = edit_analysis.get("recipient_email")

        analyzer = get_email_style_analyzer()

        # Extract style from the user's edited version (what they actually wanted)
        final_style = await analyzer.extract_style(final, recipient)

        # Update profile with the user's preferred style
        await analyzer.update_recipient_profile(recipient, final_style)

        # Also update general profile
        await analyzer.update_recipient_profile(None, final_style)

        # Mark as learned
        from cognitex.db.postgres import get_session
        async for session in get_session():
            await session.execute(
                text("""
                    UPDATE email_draft_lifecycle
                    SET learned_from = TRUE
                    WHERE draft_id = :draft_id
                """),
                {"draft_id": edit_analysis["draft_id"]},
            )
            await session.commit()
            break

        logger.info(
            "Learned from draft edits",
            draft_id=edit_analysis["draft_id"],
            edit_type=edit_analysis["edit_type"],
            recipient=recipient,
        )

    except Exception as e:
        logger.warning("Failed to learn from draft edits", error=str(e))


async def get_draft_edit_stats(days: int = 30) -> dict:
    """Get statistics on draft edits for learning insights."""
    from cognitex.db.postgres import get_session

    stats = {
        "total_drafts": 0,
        "sent_drafts": 0,
        "discarded_drafts": 0,
        "by_edit_type": {},
        "avg_edit_ratio": None,
        "heavy_edit_rate": 0.0,  # % requiring moderate+ edits
    }

    async for session in get_session():
        try:
            result = await session.execute(
                text("""
                    SELECT
                        COUNT(*) as total,
                        COUNT(*) FILTER (WHERE status = 'sent') as sent,
                        COUNT(*) FILTER (WHERE status = 'discarded') as discarded,
                        AVG(edit_ratio) FILTER (WHERE edit_ratio IS NOT NULL) as avg_ratio,
                        COUNT(*) FILTER (WHERE edit_type IN ('moderate', 'major', 'rewrite')) as heavy_edits
                    FROM email_draft_lifecycle
                    WHERE created_at > NOW() - INTERVAL ':days days'
                """).bindparams(days=days)
            )
            row = result.fetchone()
            if row:
                stats["total_drafts"] = row[0] or 0
                stats["sent_drafts"] = row[1] or 0
                stats["discarded_drafts"] = row[2] or 0
                stats["avg_edit_ratio"] = round(row[3], 2) if row[3] else None
                heavy = row[4] or 0
                sent = row[1] or 0
                stats["heavy_edit_rate"] = round(heavy / sent, 2) if sent > 0 else 0.0

            # Breakdown by edit type
            result = await session.execute(
                text("""
                    SELECT edit_type, COUNT(*) as count
                    FROM email_draft_lifecycle
                    WHERE created_at > NOW() - INTERVAL ':days days'
                      AND edit_type IS NOT NULL
                    GROUP BY edit_type
                """).bindparams(days=days)
            )
            for row in result.fetchall():
                stats["by_edit_type"][row[0]] = row[1]

        except Exception as e:
            logger.warning("Failed to get draft edit stats", error=str(e))
        break

    return stats
