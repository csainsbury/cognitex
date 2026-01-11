"""Context summarization for long conversations.

Compresses older conversation turns to manage token limits while
preserving essential context. Based on PA video transcript patterns.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

import structlog

from cognitex.config import get_settings

logger = structlog.get_logger()


class SummarizationStrategy(Enum):
    """How aggressively to summarize conversation history."""

    AGGRESSIVE = "aggressive"  # Keep last 3 turns, summarize heavily
    MODERATE = "moderate"  # Keep last 7 turns, balanced summary
    MINIMAL = "minimal"  # Keep last 10 turns, light compression


# Configuration per strategy
STRATEGY_CONFIG = {
    SummarizationStrategy.AGGRESSIVE: {
        "recent_turns_to_keep": 3,
        "summary_max_tokens": 500,
        "detail_level": "brief",
    },
    SummarizationStrategy.MODERATE: {
        "recent_turns_to_keep": 7,
        "summary_max_tokens": 800,
        "detail_level": "moderate",
    },
    SummarizationStrategy.MINIMAL: {
        "recent_turns_to_keep": 10,
        "summary_max_tokens": 1200,
        "detail_level": "detailed",
    },
}


@dataclass
class SummarizationResult:
    """Result of summarizing conversation history."""

    summary: str
    recent_messages: list[dict]
    messages_summarized: int
    estimated_tokens_saved: int
    strategy_used: SummarizationStrategy


class ConversationSummarizer:
    """Summarizes older conversation turns to manage context length.

    Uses LLM to create compressed summaries of older interactions,
    keeping recent turns verbatim for immediate context.
    """

    def __init__(
        self,
        strategy: SummarizationStrategy | str = SummarizationStrategy.MODERATE,
        max_context_tokens: int = 8000,
    ):
        if isinstance(strategy, str):
            strategy = SummarizationStrategy(strategy)

        self.strategy = strategy
        self.max_context_tokens = max_context_tokens
        self.config = STRATEGY_CONFIG[strategy]

    def estimate_tokens(self, messages: list[dict]) -> int:
        """Estimate token count for messages.

        Simple estimation: ~4 characters per token average.
        """
        total_chars = sum(len(msg.get("content", "")) for msg in messages)
        return total_chars // 4

    def should_summarize(self, messages: list[dict]) -> bool:
        """Check if summarization is needed based on token estimate."""
        estimated_tokens = self.estimate_tokens(messages)

        # Keep a buffer for system prompt and response
        threshold = self.max_context_tokens * 0.7

        return estimated_tokens > threshold and len(messages) > self.config["recent_turns_to_keep"]

    async def summarize_older_messages(
        self,
        messages: list[dict],
        session_id: str | None = None,
    ) -> SummarizationResult:
        """Summarize older messages, keeping recent ones verbatim.

        Args:
            messages: List of conversation messages
            session_id: Optional session ID for storing summary

        Returns:
            SummarizationResult with summary and recent messages
        """
        keep_recent = self.config["recent_turns_to_keep"]

        # If not enough messages to warrant summarization
        if len(messages) <= keep_recent:
            return SummarizationResult(
                summary="",
                recent_messages=messages,
                messages_summarized=0,
                estimated_tokens_saved=0,
                strategy_used=self.strategy,
            )

        # Split messages
        older_messages = messages[:-keep_recent]
        recent_messages = messages[-keep_recent:]

        # Generate summary of older messages
        summary = await self._generate_summary(older_messages)

        # Store summary if session_id provided
        if session_id:
            await self._store_summary(
                session_id=session_id,
                summary=summary,
                messages_summarized=len(older_messages),
                oldest_timestamp=self._get_timestamp(older_messages[0]),
                newest_timestamp=self._get_timestamp(older_messages[-1]),
            )

        # Calculate token savings
        old_tokens = self.estimate_tokens(older_messages)
        new_tokens = self.estimate_tokens([{"content": summary}])

        return SummarizationResult(
            summary=summary,
            recent_messages=recent_messages,
            messages_summarized=len(older_messages),
            estimated_tokens_saved=old_tokens - new_tokens,
            strategy_used=self.strategy,
        )

    async def _generate_summary(self, messages: list[dict]) -> str:
        """Generate a summary of messages using LLM."""
        from cognitex.services.llm import get_llm_service

        # Format messages for summarization
        formatted = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")[:500]  # Truncate long messages
            formatted.append(f"{role.upper()}: {content}")

        conversation_text = "\n".join(formatted)

        detail_level = self.config["detail_level"]

        prompt = f"""Summarize the following conversation history into a {detail_level} summary.
Focus on:
- Key topics discussed
- Important decisions made
- Actions taken or requested
- Relevant context for continuing the conversation

Keep the summary under {self.config['summary_max_tokens']} tokens.

CONVERSATION:
{conversation_text}

SUMMARY:"""

        try:
            llm = get_llm_service()
            summary = await llm.generate_text(
                prompt=prompt,
                max_tokens=self.config["summary_max_tokens"],
                temperature=0.3,
            )
            return summary.strip()
        except Exception as e:
            logger.warning("Failed to generate summary, using fallback", error=str(e))
            return self._fallback_summary(messages)

    def _fallback_summary(self, messages: list[dict]) -> str:
        """Simple fallback summary if LLM fails."""
        topics = []
        for msg in messages:
            content = msg.get("content", "")[:100]
            if content:
                topics.append(content.split(".")[0])

        unique_topics = list(dict.fromkeys(topics))[:5]

        return f"Previous conversation covered: {'; '.join(unique_topics)}"

    def _get_timestamp(self, message: dict) -> datetime | None:
        """Extract timestamp from message if available."""
        ts = message.get("timestamp")
        if isinstance(ts, datetime):
            return ts
        if isinstance(ts, str):
            try:
                return datetime.fromisoformat(ts)
            except ValueError:
                pass
        return None

    async def _store_summary(
        self,
        session_id: str,
        summary: str,
        messages_summarized: int,
        oldest_timestamp: datetime | None,
        newest_timestamp: datetime | None,
    ) -> str:
        """Store summary in database for retrieval."""
        from cognitex.db.postgres import get_session
        from cognitex.services.embeddings import get_embedding
        from sqlalchemy import text

        summary_id = f"summary_{uuid.uuid4().hex[:12]}"

        # Generate embedding for semantic retrieval
        try:
            embedding = await get_embedding(summary)
        except Exception:
            embedding = None

        async for session in get_session():
            try:
                await session.execute(
                    text("""
                        INSERT INTO conversation_summaries (
                            id, session_id, summary_text, summary_embedding,
                            token_count, messages_summarized,
                            message_range_start, message_range_end, strategy
                        ) VALUES (
                            :id, :session_id, :summary, :embedding,
                            :token_count, :messages_summarized,
                            :range_start, :range_end, :strategy
                        )
                    """),
                    {
                        "id": summary_id,
                        "session_id": session_id,
                        "summary": summary,
                        "embedding": embedding,
                        "token_count": self.estimate_tokens([{"content": summary}]),
                        "messages_summarized": messages_summarized,
                        "range_start": oldest_timestamp,
                        "range_end": newest_timestamp,
                        "strategy": self.strategy.value,
                    },
                )
                await session.commit()

                logger.info(
                    "Stored conversation summary",
                    summary_id=summary_id,
                    session_id=session_id,
                    messages_summarized=messages_summarized,
                )
            except Exception as e:
                logger.warning("Failed to store summary", error=str(e))
            break

        return summary_id

    async def get_session_summaries(
        self,
        session_id: str,
        limit: int = 5,
    ) -> list[dict]:
        """Retrieve past summaries for a session."""
        from cognitex.db.postgres import get_session
        from sqlalchemy import text

        async for session in get_session():
            try:
                result = await session.execute(
                    text("""
                        SELECT id, summary_text, messages_summarized,
                               message_range_start, message_range_end, created_at
                        FROM conversation_summaries
                        WHERE session_id = :session_id
                        ORDER BY created_at DESC
                        LIMIT :limit
                    """),
                    {"session_id": session_id, "limit": limit},
                )
                rows = result.fetchall()
                return [
                    {
                        "id": row[0],
                        "summary": row[1],
                        "messages_summarized": row[2],
                        "range_start": row[3],
                        "range_end": row[4],
                        "created_at": row[5],
                    }
                    for row in rows
                ]
            except Exception as e:
                logger.warning("Failed to retrieve summaries", error=str(e))
            break

        return []


def format_summary_for_prompt(summary: str) -> str:
    """Format a summary for injection into the agent prompt."""
    if not summary:
        return ""

    return f"""## Previous Context (summarized)
{summary}

---
"""


# Singleton instance
_summarizer: ConversationSummarizer | None = None


def get_summarizer() -> ConversationSummarizer:
    """Get or create the summarizer singleton."""
    global _summarizer
    if _summarizer is None:
        settings = get_settings()
        _summarizer = ConversationSummarizer(
            strategy=settings.summarization_strategy,
            max_context_tokens=settings.max_context_tokens,
        )
    return _summarizer
