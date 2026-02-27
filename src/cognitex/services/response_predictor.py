"""Email Response Prediction Service.

Predicts whether an email needs a response based on learned patterns
from user behavior, sender history, and email intent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import text

from cognitex.services.email_intent import EmailIntent, EmailIntentResult

logger = structlog.get_logger()


@dataclass
class SimilarDecision:
    """A past email decision similar to the current one."""

    email_id: str
    sender_email: str
    subject: str
    user_decision: str  # 'responded', 'skipped', 'delegated'
    similarity_reason: str
    created_at: datetime


@dataclass
class ResponsePrediction:
    """Prediction result for whether an email needs a response."""

    needs_response: bool
    confidence: float  # 0.0 to 1.0
    reasoning: str
    similar_decisions: list[SimilarDecision] = field(default_factory=list)

    # Factor breakdown for transparency
    sender_rate: float | None = None  # Historical response rate to this sender
    intent_baseline: float | None = None  # Baseline rate for this intent type
    context_modifier: float = 0.0  # Time/mode adjustments

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "needs_response": self.needs_response,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "similar_decisions": [
                {
                    "email_id": d.email_id,
                    "sender": d.sender_email,
                    "subject": d.subject,
                    "decision": d.user_decision,
                    "reason": d.similarity_reason,
                }
                for d in self.similar_decisions
            ],
            "factors": {
                "sender_rate": self.sender_rate,
                "intent_baseline": self.intent_baseline,
                "context_modifier": self.context_modifier,
            },
        }


@dataclass
class SenderStats:
    """Historical response statistics for a sender."""

    sender_email: str
    total_emails: int
    responded_count: int
    skipped_count: int
    avg_response_time_minutes: float | None

    @property
    def response_rate(self) -> float:
        """Calculate response rate (0.0 to 1.0)."""
        if self.total_emails == 0:
            return 0.5  # Unknown sender, use neutral
        return self.responded_count / self.total_emails


class ResponsePredictor:
    """Predicts whether an email needs a response based on learned patterns.

    Uses multiple factors:
    1. Sender history - how often user responds to this sender
    2. Intent baseline - typical response rate for this intent type
    3. Time/context patterns - time of day, operating mode adjustments
    4. Similar decisions - semantic similarity to past decisions
    """

    # Baseline response rates by intent type (can be refined from data)
    INTENT_BASELINES: dict[EmailIntent, float] = {
        EmailIntent.REVIEW_REQUEST: 0.9,    # Almost always need to respond
        EmailIntent.QUESTION: 0.85,          # Questions usually need answers
        EmailIntent.ACTION_REQUEST: 0.8,     # Action items need acknowledgment
        EmailIntent.DEADLINE: 0.9,           # Deadlines are important
        EmailIntent.FOLLOWUP: 0.7,           # Follow-ups often need response
        EmailIntent.SCHEDULING: 0.85,        # Calendar items need response
        EmailIntent.CONFIRMATION: 0.3,       # Often just FYI
        EmailIntent.FYI: 0.1,                # Rarely need response
        EmailIntent.UNKNOWN: 0.5,            # Uncertain
    }

    def __init__(self):
        self._learned_intent_rates: dict[str, float] | None = None

    async def predict_response_needed(
        self,
        email: dict,
        intent_result: EmailIntentResult | None = None,
    ) -> ResponsePrediction:
        """Predict whether an email needs a response.

        Args:
            email: Email dict with 'from', 'subject', 'snippet' keys
            intent_result: Optional pre-computed intent classification

        Returns:
            ResponsePrediction with prediction and reasoning
        """
        sender = email.get("from", email.get("sender_email", ""))
        subject = email.get("subject", "")

        # Factor 1: Sender history
        sender_stats = await self._get_sender_stats(sender)

        # Factor 2: Intent-based baseline
        if intent_result:
            intent_baseline = await self._get_intent_rate(intent_result.intent)
        else:
            intent_baseline = 0.5  # Unknown intent

        # Factor 3: Context modifier (time of day, operating mode)
        context_modifier = await self._get_context_modifier()

        # Factor 4: Similar past decisions
        similar = await self._find_similar_decisions(sender, subject)

        # Combine factors with weights
        score = self._combine_factors(
            sender_stats=sender_stats,
            intent_baseline=intent_baseline,
            context_modifier=context_modifier,
            similar_decisions=similar,
        )

        # Generate reasoning
        reasoning = self._explain_prediction(
            score=score,
            sender_stats=sender_stats,
            intent=intent_result.intent if intent_result else None,
            similar=similar,
        )

        return ResponsePrediction(
            needs_response=score > 0.5,
            confidence=min(abs(score - 0.5) * 2, 1.0),  # 0.5->0, 0.0/1.0->1.0
            reasoning=reasoning,
            similar_decisions=similar[:3],
            sender_rate=sender_stats.response_rate if sender_stats else None,
            intent_baseline=intent_baseline,
            context_modifier=context_modifier,
        )

    async def _get_sender_stats(self, sender_email: str) -> SenderStats | None:
        """Get historical response statistics for a sender."""
        if not sender_email:
            return None

        # Normalize email
        sender_email = sender_email.lower().strip()
        if "<" in sender_email:
            # Extract email from "Name <email>" format
            import re
            match = re.search(r"<([^>]+)>", sender_email)
            if match:
                sender_email = match.group(1)

        from cognitex.db.postgres import get_session

        async for session in get_session():
            try:
                result = await session.execute(
                    text("""
                        SELECT
                            sender_email,
                            COUNT(*) as total,
                            COUNT(*) FILTER (WHERE user_decision = 'responded') as responded,
                            COUNT(*) FILTER (WHERE user_decision = 'skipped') as skipped,
                            AVG(response_time_minutes) FILTER (WHERE did_respond = true) as avg_time
                        FROM email_response_decisions
                        WHERE sender_email = :sender
                        GROUP BY sender_email
                    """),
                    {"sender": sender_email},
                )

                row = result.fetchone()
                if row:
                    return SenderStats(
                        sender_email=row[0],
                        total_emails=row[1],
                        responded_count=row[2] or 0,
                        skipped_count=row[3] or 0,
                        avg_response_time_minutes=row[4],
                    )
            except Exception as e:
                logger.warning("Failed to get sender stats", error=str(e))
            break

        return None

    async def _get_intent_rate(self, intent: EmailIntent) -> float:
        """Get response rate for an intent type, using learned rates if available."""
        # Try to get learned rate from database
        if self._learned_intent_rates is None:
            self._learned_intent_rates = await self._load_learned_intent_rates()

        if intent.value in self._learned_intent_rates:
            return self._learned_intent_rates[intent.value]

        # Fall back to baseline
        return self.INTENT_BASELINES.get(intent, 0.5)

    async def _load_learned_intent_rates(self) -> dict[str, float]:
        """Load learned intent response rates from decision history."""
        from cognitex.db.postgres import get_session

        rates: dict[str, float] = {}

        async for session in get_session():
            try:
                result = await session.execute(
                    text("""
                        SELECT
                            intent,
                            COUNT(*) as total,
                            COUNT(*) FILTER (WHERE user_decision = 'responded') as responded
                        FROM email_response_decisions
                        WHERE intent IS NOT NULL
                        GROUP BY intent
                        HAVING COUNT(*) >= 5  -- Need minimum samples
                    """)
                )

                for row in result.fetchall():
                    intent_name = row[0]
                    total = row[1]
                    responded = row[2] or 0
                    rates[intent_name] = responded / total

                logger.debug("Loaded learned intent rates", rates=rates)
            except Exception as e:
                logger.warning("Failed to load intent rates", error=str(e))
            break

        return rates

    async def _get_context_modifier(self) -> float:
        """Get context-based modifier for response prediction.

        Considers:
        - Time of day (early morning/late night = lower)
        - Operating mode (focused = higher threshold)
        - Day of week (weekend = lower)
        """
        from datetime import datetime

        now = datetime.now()
        modifier = 0.0

        # Time of day adjustment
        hour = now.hour
        if hour < 7 or hour > 22:
            modifier -= 0.1  # Late night/early morning, less likely to respond
        elif 9 <= hour <= 17:
            modifier += 0.05  # Business hours, more likely

        # Day of week adjustment
        if now.weekday() >= 5:  # Weekend
            modifier -= 0.1

        # Operating mode adjustment (if available)
        try:
            from cognitex.agent.state_model import get_state_estimator
            state = await get_state_estimator().get_current_state()

            if state.operating_mode == "focused":
                modifier -= 0.15  # In focused mode, higher bar for responses
            elif state.operating_mode == "available":
                modifier += 0.05  # Available, more likely to respond
        except Exception:
            pass  # State model not available

        return modifier

    async def _find_similar_decisions(
        self,
        sender: str,
        subject: str,
        limit: int = 5,
    ) -> list[SimilarDecision]:
        """Find similar past email decisions.

        For now, uses simple heuristics:
        - Same sender
        - Similar subject keywords

        Future: Use embeddings for semantic similarity.
        """
        from cognitex.db.postgres import get_session

        similar: list[SimilarDecision] = []

        # Normalize sender
        sender_email = sender.lower().strip()
        if "<" in sender_email:
            import re
            match = re.search(r"<([^>]+)>", sender_email)
            if match:
                sender_email = match.group(1)

        async for session in get_session():
            try:
                # First: exact sender matches
                result = await session.execute(
                    text("""
                        SELECT email_id, sender_email, subject, user_decision, created_at
                        FROM email_response_decisions
                        WHERE sender_email = :sender
                        ORDER BY created_at DESC
                        LIMIT :limit
                    """),
                    {"sender": sender_email, "limit": limit},
                )

                for row in result.fetchall():
                    similar.append(SimilarDecision(
                        email_id=row[0],
                        sender_email=row[1],
                        subject=row[2] or "",
                        user_decision=row[3],
                        similarity_reason="Same sender",
                        created_at=row[4],
                    ))

                # If we need more, try domain matching
                if len(similar) < limit and "@" in sender_email:
                    domain = sender_email.split("@")[1]
                    result = await session.execute(
                        text("""
                            SELECT email_id, sender_email, subject, user_decision, created_at
                            FROM email_response_decisions
                            WHERE sender_domain = :domain
                              AND sender_email != :sender
                            ORDER BY created_at DESC
                            LIMIT :limit
                        """),
                        {"domain": domain, "sender": sender_email, "limit": limit - len(similar)},
                    )

                    for row in result.fetchall():
                        similar.append(SimilarDecision(
                            email_id=row[0],
                            sender_email=row[1],
                            subject=row[2] or "",
                            user_decision=row[3],
                            similarity_reason=f"Same domain ({domain})",
                            created_at=row[4],
                        ))

            except Exception as e:
                logger.warning("Failed to find similar decisions", error=str(e))
            break

        return similar[:limit]

    def _combine_factors(
        self,
        sender_stats: SenderStats | None,
        intent_baseline: float,
        context_modifier: float,
        similar_decisions: list[SimilarDecision],
    ) -> float:
        """Combine factors into a single score (0.0 to 1.0).

        Weights:
        - Sender history: 35% (if available)
        - Intent baseline: 35%
        - Similar decisions: 20% (if available)
        - Context modifier: 10%
        """
        score = 0.0
        total_weight = 0.0

        # Sender history
        if sender_stats and sender_stats.total_emails >= 3:
            score += sender_stats.response_rate * 0.35
            total_weight += 0.35
        else:
            # Redistribute weight if no sender history
            pass

        # Intent baseline (always available)
        intent_weight = 0.35 if sender_stats and sender_stats.total_emails >= 3 else 0.50
        score += intent_baseline * intent_weight
        total_weight += intent_weight

        # Similar decisions
        if similar_decisions:
            responded = sum(1 for d in similar_decisions if d.user_decision == "responded")
            similar_rate = responded / len(similar_decisions)
            score += similar_rate * 0.20
            total_weight += 0.20

        # Normalize and apply context modifier
        if total_weight > 0:
            score = score / total_weight

        # Apply context modifier (additive, clamped)
        score = max(0.0, min(1.0, score + context_modifier))

        return score

    def _explain_prediction(
        self,
        score: float,
        sender_stats: SenderStats | None,
        intent: EmailIntent | None,
        similar: list[SimilarDecision],
    ) -> str:
        """Generate human-readable explanation for the prediction."""
        parts = []

        # Score interpretation
        if score > 0.8:
            parts.append("High likelihood of needing a response")
        elif score > 0.6:
            parts.append("Likely needs a response")
        elif score > 0.4:
            parts.append("Uncertain whether response is needed")
        elif score > 0.2:
            parts.append("Probably doesn't need a response")
        else:
            parts.append("Low likelihood of needing a response")

        # Sender context
        if sender_stats and sender_stats.total_emails >= 3:
            rate_pct = int(sender_stats.response_rate * 100)
            parts.append(
                f"You respond to {rate_pct}% of emails from this sender "
                f"({sender_stats.responded_count}/{sender_stats.total_emails})"
            )

        # Intent context
        if intent:
            intent_str = intent.value.replace("_", " ").title()
            if intent == EmailIntent.FYI:
                parts.append(f"Intent: {intent_str} (typically no response needed)")
            elif intent in [EmailIntent.QUESTION, EmailIntent.ACTION_REQUEST]:
                parts.append(f"Intent: {intent_str} (typically requires response)")
            else:
                parts.append(f"Intent: {intent_str}")

        # Similar decisions context
        if similar:
            responded = sum(1 for d in similar if d.user_decision == "responded")
            skipped = sum(1 for d in similar if d.user_decision == "skipped")
            if responded > skipped:
                parts.append(f"Similar emails were usually responded to ({responded}/{len(similar)})")
            elif skipped > responded:
                parts.append(f"Similar emails were usually skipped ({skipped}/{len(similar)})")

        return ". ".join(parts) + "."

    async def refresh_learned_rates(self) -> None:
        """Force refresh of learned intent rates from database."""
        self._learned_intent_rates = await self._load_learned_intent_rates()

    async def record_outcome(
        self,
        email_id: str,
        did_respond: bool,
        response_time_minutes: int | None = None,
    ) -> None:
        """Record the actual outcome of an email (whether user responded).

        Called when we detect a sent email that was a response.
        """
        from cognitex.db.postgres import get_session

        async for session in get_session():
            try:
                await session.execute(
                    text("""
                        UPDATE email_response_decisions
                        SET did_respond = :responded,
                            response_time_minutes = :time
                        WHERE email_id = :email_id
                    """),
                    {
                        "email_id": email_id,
                        "responded": did_respond,
                        "time": response_time_minutes,
                    },
                )
                await session.commit()
                logger.debug(
                    "Recorded email response outcome",
                    email_id=email_id,
                    responded=did_respond,
                )
            except Exception as e:
                logger.warning("Failed to record outcome", error=str(e))
            break


# Singleton instance
_predictor: ResponsePredictor | None = None


def get_response_predictor() -> ResponsePredictor:
    """Get or create the response predictor singleton."""
    global _predictor
    if _predictor is None:
        _predictor = ResponsePredictor()
    return _predictor
