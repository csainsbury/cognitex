"""
Decision Trace Memory System

Captures the full context of agent decisions for:
1. Immediate RAG retrieval (find similar past decisions)
2. Future fine-tuning (learn to act like the user)
3. Preference extraction (patterns in behavior)

Key tables:
- decision_traces: Full context → action → feedback for each decision
- communication_patterns: Per-person communication style
- preference_rules: Extracted behavioral rules
"""

import json
import uuid
from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()


# =============================================================================
# Schema Setup
# =============================================================================

SCHEMA_SQL = """
-- Decision traces for fine-tuning and RAG
CREATE TABLE IF NOT EXISTS decision_traces (
    id TEXT PRIMARY KEY,

    -- Trigger information
    trigger_type TEXT NOT NULL,                    -- email, calendar, user_request, scheduled, discord
    trigger_id TEXT,                               -- gmail_id, gcal_id, message_id, etc.
    trigger_summary TEXT,                          -- Brief description of what triggered this

    -- Full context snapshot (denormalized for training)
    context JSONB NOT NULL DEFAULT '{}',
    -- Structure:
    -- {
    --   "trigger_content": {...},                 -- The email/event/message content
    --   "sender_profile": {                       -- Who initiated
    --     "email", "name", "role", "org",
    --     "relationship_type",                    -- colleague, client, vendor, personal
    --     "communication_history_summary"
    --   },
    --   "related_entities": {
    --     "tasks": [...],
    --     "projects": [...],
    --     "goals": [...],
    --     "prior_thread_messages": [...],
    --     "upcoming_events": [...]
    --   },
    --   "temporal_context": {
    --     "day_of_week", "time_of_day", "is_business_hours",
    --     "pending_deadlines": [...]
    --   }
    -- }

    -- The decision/action
    action_type TEXT NOT NULL,                     -- reply, create_task, schedule_meeting, defer, delegate, ignore, notify
    proposed_action JSONB NOT NULL DEFAULT '{}',   -- What the agent proposed
    final_action JSONB,                            -- What was actually executed (after edits)
    reasoning TEXT,                                -- Agent's reasoning for this action

    -- Outcome & feedback
    status TEXT NOT NULL DEFAULT 'pending',        -- pending, approved, edited, rejected, auto_executed
    user_edits JSONB,                              -- Diff or description of changes made
    explicit_feedback TEXT,                        -- User's verbal feedback
    implicit_signals JSONB DEFAULT '{}',           -- {edit_distance, time_to_approve, modifications_count}

    -- Computed quality for training
    quality_score FLOAT,                           -- 0.0-1.0, computed from feedback signals
    include_in_training BOOLEAN DEFAULT true,     -- Whether to use for fine-tuning

    -- Embeddings for RAG
    context_embedding vector(768),                 -- Embedding of context for similarity search
    action_embedding vector(768),                  -- Embedding of action for similarity search

    -- Metadata
    created_at TIMESTAMP DEFAULT NOW(),
    resolved_at TIMESTAMP,
    metadata JSONB DEFAULT '{}'
);

-- Indexes for decision_traces
CREATE INDEX IF NOT EXISTS idx_decision_traces_trigger_type ON decision_traces(trigger_type);
CREATE INDEX IF NOT EXISTS idx_decision_traces_action_type ON decision_traces(action_type);
CREATE INDEX IF NOT EXISTS idx_decision_traces_status ON decision_traces(status);
CREATE INDEX IF NOT EXISTS idx_decision_traces_created ON decision_traces(created_at);
CREATE INDEX IF NOT EXISTS idx_decision_traces_quality ON decision_traces(quality_score) WHERE quality_score IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_decision_traces_training ON decision_traces(include_in_training) WHERE include_in_training = true;

-- Per-person communication patterns
CREATE TABLE IF NOT EXISTS communication_patterns (
    id TEXT PRIMARY KEY,
    person_email TEXT UNIQUE NOT NULL,
    person_name TEXT,

    -- Relationship classification
    relationship_type TEXT,                        -- colleague, manager, report, client, vendor, personal, unknown
    organization TEXT,
    role TEXT,

    -- Learned communication preferences
    preferred_tone TEXT,                           -- formal, professional, casual, friendly, direct
    response_urgency TEXT,                         -- immediate, same_day, within_week, relaxed
    typical_response_length TEXT,                  -- brief, moderate, detailed
    greeting_style TEXT,                           -- formal_greeting, first_name, no_greeting
    sign_off_style TEXT,                           -- formal, casual, none

    -- Common action patterns
    typical_actions TEXT[] DEFAULT '{}',           -- Most common actions taken for this person
    topics TEXT[] DEFAULT '{}',                    -- Common topics discussed

    -- Example traces for reference
    example_trace_ids TEXT[] DEFAULT '{}',         -- Best example decision traces

    -- Confidence metrics
    interaction_count INTEGER DEFAULT 0,
    last_interaction TIMESTAMP,
    pattern_confidence FLOAT DEFAULT 0.0,         -- How confident we are in these patterns

    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_comm_patterns_email ON communication_patterns(person_email);
CREATE INDEX IF NOT EXISTS idx_comm_patterns_relationship ON communication_patterns(relationship_type);

-- Extracted preference rules
CREATE TABLE IF NOT EXISTS preference_rules (
    id TEXT PRIMARY KEY,

    -- Rule definition
    rule_type TEXT NOT NULL,                       -- communication, scheduling, task_management, notification
    rule_name TEXT,                                -- Human-readable name

    -- Condition (when does this apply)
    condition JSONB NOT NULL,
    -- Examples:
    -- {"trigger_type": "email", "sender_relationship": "client"}
    -- {"time_of_day": "evening", "is_weekend": true}
    -- {"action_type": "schedule_meeting", "with_role": "executive"}

    -- Preference (what to do)
    preference JSONB NOT NULL,
    -- Examples:
    -- {"tone": "formal", "response_speed": "immediate"}
    -- {"action": "defer_to_monday"}
    -- {"meeting_buffer": "15_minutes", "prefer_morning": true}

    -- Confidence and provenance
    confidence FLOAT DEFAULT 0.5,                  -- 0.0-1.0
    evidence_count INTEGER DEFAULT 0,              -- Number of traces supporting this
    source_trace_ids TEXT[] DEFAULT '{}',          -- Which traces led to this rule

    -- Status
    is_active BOOLEAN DEFAULT true,
    user_confirmed BOOLEAN DEFAULT false,          -- User explicitly confirmed this rule

    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pref_rules_type ON preference_rules(rule_type);
CREATE INDEX IF NOT EXISTS idx_pref_rules_active ON preference_rules(is_active) WHERE is_active = true;
"""


async def init_decision_memory_schema(session: AsyncSession) -> None:
    """Initialize the decision memory tables."""
    # Split into individual statements for execution
    statements = [s.strip() for s in SCHEMA_SQL.split(';') if s.strip()]
    for stmt in statements:
        try:
            await session.execute(text(stmt))
        except Exception as e:
            # Index might already exist, etc.
            logger.debug("Schema statement skipped", error=str(e)[:100])
    await session.commit()
    logger.info("Decision memory schema initialized")


# =============================================================================
# Decision Trace Memory
# =============================================================================

class DecisionTraceMemory:
    """
    Manages decision traces for learning user behavior.

    Usage:
        # When agent proposes an action
        trace_id = await memory.create_trace(
            trigger_type="email",
            trigger_id="msg_123",
            context={...},
            action_type="reply",
            proposed_action={"content": "...", "tone": "formal"},
            reasoning="Client email requires prompt response"
        )

        # When user approves/edits/rejects
        await memory.record_feedback(
            trace_id,
            status="edited",
            final_action={"content": "...(edited)..."},
            user_edits={"changed_tone": "more casual"},
            explicit_feedback="Too formal for Dave"
        )

        # Find similar past decisions for RAG
        similar = await memory.find_similar_decisions(
            context={...},
            action_type="reply",
            limit=5
        )
    """

    def __init__(self, get_session_func):
        self._get_session = get_session_func
        self._initialized = False

    async def _ensure_schema(self, session: AsyncSession) -> None:
        """Ensure schema exists."""
        if not self._initialized:
            await init_decision_memory_schema(session)
            self._initialized = True

    async def create_trace(
        self,
        trigger_type: str,
        action_type: str,
        proposed_action: dict,
        context: dict | None = None,
        trigger_id: str | None = None,
        trigger_summary: str | None = None,
        reasoning: str | None = None,
        metadata: dict | None = None,
    ) -> str:
        """
        Create a new decision trace when agent proposes an action.

        Args:
            trigger_type: What initiated this (email, calendar, user_request, etc.)
            action_type: Type of action proposed (reply, create_task, etc.)
            proposed_action: The action details
            context: Full context snapshot
            trigger_id: ID of the trigger (gmail_id, etc.)
            trigger_summary: Brief description
            reasoning: Agent's reasoning
            metadata: Additional metadata

        Returns:
            Trace ID
        """
        from cognitex.services.llm import get_llm_service

        trace_id = f"trace_{uuid.uuid4().hex[:12]}"
        context = context or {}

        # Generate embeddings for RAG
        llm = get_llm_service()

        # Context embedding - what situation is this?
        context_text = json.dumps(context, default=str)[:2000]
        context_embedding = await llm.generate_embedding(context_text)

        # Action embedding - what action was taken?
        action_text = f"{action_type}: {json.dumps(proposed_action, default=str)[:1000]}"
        action_embedding = await llm.generate_embedding(action_text)

        async for session in self._get_session():
            await self._ensure_schema(session)

            # Convert embeddings to pgvector format
            context_emb_str = "[" + ",".join(str(x) for x in context_embedding) + "]"
            action_emb_str = "[" + ",".join(str(x) for x in action_embedding) + "]"

            await session.execute(text("""
                INSERT INTO decision_traces (
                    id, trigger_type, trigger_id, trigger_summary,
                    context, action_type, proposed_action, reasoning,
                    status, context_embedding, action_embedding, metadata
                ) VALUES (
                    :id, :trigger_type, :trigger_id, :trigger_summary,
                    :context, :action_type, :proposed_action, :reasoning,
                    'pending', CAST(:context_embedding AS vector),
                    CAST(:action_embedding AS vector), :metadata
                )
            """), {
                "id": trace_id,
                "trigger_type": trigger_type,
                "trigger_id": trigger_id,
                "trigger_summary": trigger_summary,
                "context": json.dumps(context, default=str),
                "action_type": action_type,
                "proposed_action": json.dumps(proposed_action, default=str),
                "reasoning": reasoning,
                "context_embedding": context_emb_str,
                "action_embedding": action_emb_str,
                "metadata": json.dumps(metadata or {}),
            })

            await session.commit()
            logger.info("Created decision trace", trace_id=trace_id, action_type=action_type)
            return trace_id

    async def record_feedback(
        self,
        trace_id: str,
        status: str,
        final_action: dict | None = None,
        user_edits: dict | None = None,
        explicit_feedback: str | None = None,
        implicit_signals: dict | None = None,
    ) -> bool:
        """
        Record feedback on a decision trace.

        Args:
            trace_id: The trace to update
            status: approved, edited, rejected, auto_executed
            final_action: The action as executed (after any edits)
            user_edits: Description of changes made
            explicit_feedback: User's verbal feedback
            implicit_signals: {edit_distance, time_to_approve, etc.}

        Returns:
            Success boolean
        """
        # Compute quality score from signals
        quality_score = self._compute_quality_score(status, user_edits, implicit_signals)

        async for session in self._get_session():
            result = await session.execute(text("""
                UPDATE decision_traces
                SET status = :status,
                    final_action = :final_action,
                    user_edits = :user_edits,
                    explicit_feedback = :explicit_feedback,
                    implicit_signals = :implicit_signals,
                    quality_score = :quality_score,
                    resolved_at = NOW()
                WHERE id = :trace_id
                RETURNING id
            """), {
                "trace_id": trace_id,
                "status": status,
                "final_action": json.dumps(final_action, default=str) if final_action else None,
                "user_edits": json.dumps(user_edits, default=str) if user_edits else None,
                "explicit_feedback": explicit_feedback,
                "implicit_signals": json.dumps(implicit_signals or {}),
                "quality_score": quality_score,
            })

            row = result.fetchone()
            await session.commit()

            if row:
                logger.info("Recorded feedback", trace_id=trace_id, status=status, quality=quality_score)
                return True
            return False

    def _compute_quality_score(
        self,
        status: str,
        user_edits: dict | None,
        implicit_signals: dict | None,
    ) -> float:
        """Compute quality score from feedback signals."""
        signals = implicit_signals or {}

        # Base score from status
        if status == "approved":
            score = 1.0
        elif status == "auto_executed":
            score = 0.9  # Assume good if auto-allowed
        elif status == "edited":
            score = 0.5  # Start at 0.5 for edits
        elif status == "rejected":
            score = 0.0
        else:
            score = 0.5  # Unknown

        # Adjust for edits
        if status == "edited" and user_edits:
            # If edits were minor, score higher
            edit_distance = signals.get("edit_distance", 0.5)
            if edit_distance < 0.1:
                score = 0.9  # Very minor edits
            elif edit_distance < 0.3:
                score = 0.7  # Moderate edits
            else:
                score = 0.4  # Major edits

        # Factor in time to approve (faster = more confident)
        time_to_approve = signals.get("time_to_approve_seconds")
        if time_to_approve and time_to_approve < 5:
            score = min(score + 0.1, 1.0)  # Quick approval = good

        return round(score, 2)

    async def find_similar_decisions(
        self,
        context: dict | None = None,
        query_text: str | None = None,
        action_type: str | None = None,
        trigger_type: str | None = None,
        min_quality: float = 0.5,
        limit: int = 5,
    ) -> list[dict]:
        """
        Find similar past decisions for RAG.

        Args:
            context: Current context to match against
            query_text: Alternative text query
            action_type: Filter by action type
            trigger_type: Filter by trigger type
            min_quality: Minimum quality score
            limit: Max results

        Returns:
            List of similar decision traces
        """
        from cognitex.services.llm import get_llm_service

        llm = get_llm_service()

        # Generate query embedding
        if context:
            query_text = json.dumps(context, default=str)[:2000]
        elif not query_text:
            return []

        query_embedding = await llm.generate_embedding(query_text)
        query_emb_str = "[" + ",".join(str(x) for x in query_embedding) + "]"

        # Build filters
        filters = ["quality_score >= :min_quality", "status != 'pending'"]
        params = {"min_quality": min_quality, "limit": limit, "query_embedding": query_emb_str}

        if action_type:
            filters.append("action_type = :action_type")
            params["action_type"] = action_type

        if trigger_type:
            filters.append("trigger_type = :trigger_type")
            params["trigger_type"] = trigger_type

        where_clause = " AND ".join(filters)

        async for session in self._get_session():
            result = await session.execute(text(f"""
                SELECT
                    id, trigger_type, trigger_summary, context,
                    action_type, proposed_action, final_action,
                    reasoning, status, explicit_feedback,
                    quality_score, created_at,
                    1 - (context_embedding <=> CAST(:query_embedding AS vector)) as similarity
                FROM decision_traces
                WHERE {where_clause}
                ORDER BY context_embedding <=> CAST(:query_embedding AS vector)
                LIMIT :limit
            """), params)

            traces = []
            for row in result.fetchall():
                traces.append({
                    "id": row.id,
                    "trigger_type": row.trigger_type,
                    "trigger_summary": row.trigger_summary,
                    "context": row.context,
                    "action_type": row.action_type,
                    "proposed_action": row.proposed_action,
                    "final_action": row.final_action,
                    "reasoning": row.reasoning,
                    "status": row.status,
                    "explicit_feedback": row.explicit_feedback,
                    "quality_score": row.quality_score,
                    "similarity": float(row.similarity),
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                })

            return traces

    async def get_trace(self, trace_id: str) -> dict | None:
        """Get a specific trace by ID."""
        async for session in self._get_session():
            result = await session.execute(text("""
                SELECT * FROM decision_traces WHERE id = :trace_id
            """), {"trace_id": trace_id})

            row = result.fetchone()
            if not row:
                return None

            return {
                "id": row.id,
                "trigger_type": row.trigger_type,
                "trigger_id": row.trigger_id,
                "trigger_summary": row.trigger_summary,
                "context": row.context,
                "action_type": row.action_type,
                "proposed_action": row.proposed_action,
                "final_action": row.final_action,
                "reasoning": row.reasoning,
                "status": row.status,
                "user_edits": row.user_edits,
                "explicit_feedback": row.explicit_feedback,
                "implicit_signals": row.implicit_signals,
                "quality_score": row.quality_score,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
            }

    async def get_recent_traces(
        self,
        limit: int = 20,
        status: str | None = None,
        action_type: str | None = None,
    ) -> list[dict]:
        """Get recent decision traces."""
        filters = []
        params = {"limit": limit}

        if status:
            filters.append("status = :status")
            params["status"] = status

        if action_type:
            filters.append("action_type = :action_type")
            params["action_type"] = action_type

        where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""

        async for session in self._get_session():
            result = await session.execute(text(f"""
                SELECT id, trigger_type, trigger_summary, action_type,
                       status, quality_score, created_at, resolved_at
                FROM decision_traces
                {where_clause}
                ORDER BY created_at DESC
                LIMIT :limit
            """), params)

            return [{
                "id": row.id,
                "trigger_type": row.trigger_type,
                "trigger_summary": row.trigger_summary,
                "action_type": row.action_type,
                "status": row.status,
                "quality_score": row.quality_score,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
            } for row in result.fetchall()]

    async def find_by_approval_id(self, approval_id: str) -> dict | None:
        """Find a decision trace by its associated approval ID."""
        async for session in self._get_session():
            result = await session.execute(text("""
                SELECT id FROM decision_traces
                WHERE metadata->>'approval_id' = :approval_id
                  AND status = 'pending'
                ORDER BY created_at DESC
                LIMIT 1
            """), {"approval_id": approval_id})

            row = result.fetchone()
            if row:
                return await self.get_trace(row.id)
            return None


# =============================================================================
# Communication Patterns
# =============================================================================

class CommunicationPatternMemory:
    """Manages per-person communication patterns."""

    def __init__(self, get_session_func):
        self._get_session = get_session_func

    async def get_pattern(self, person_email: str) -> dict | None:
        """Get communication pattern for a person."""
        async for session in self._get_session():
            result = await session.execute(text("""
                SELECT * FROM communication_patterns WHERE person_email = :email
            """), {"email": person_email})

            row = result.fetchone()
            if not row:
                return None

            return {
                "person_email": row.person_email,
                "person_name": row.person_name,
                "relationship_type": row.relationship_type,
                "organization": row.organization,
                "role": row.role,
                "preferred_tone": row.preferred_tone,
                "response_urgency": row.response_urgency,
                "typical_response_length": row.typical_response_length,
                "greeting_style": row.greeting_style,
                "sign_off_style": row.sign_off_style,
                "typical_actions": row.typical_actions,
                "topics": row.topics,
                "interaction_count": row.interaction_count,
                "pattern_confidence": row.pattern_confidence,
            }

    async def update_pattern(
        self,
        person_email: str,
        updates: dict,
        increment_interaction: bool = True,
    ) -> None:
        """Update or create communication pattern."""
        pattern_id = f"cp_{uuid.uuid4().hex[:12]}"

        async for session in self._get_session():
            # Upsert pattern
            await session.execute(text("""
                INSERT INTO communication_patterns (id, person_email, person_name,
                    relationship_type, organization, role,
                    preferred_tone, response_urgency, typical_response_length,
                    greeting_style, sign_off_style, typical_actions, topics,
                    interaction_count, pattern_confidence, updated_at, last_interaction)
                VALUES (
                    :id, :email, :name, :relationship, :org, :role,
                    :tone, :urgency, :length, :greeting, :signoff,
                    :actions, :topics, 1, 0.1, NOW(), NOW()
                )
                ON CONFLICT (person_email) DO UPDATE SET
                    person_name = COALESCE(:name, communication_patterns.person_name),
                    relationship_type = COALESCE(:relationship, communication_patterns.relationship_type),
                    organization = COALESCE(:org, communication_patterns.organization),
                    role = COALESCE(:role, communication_patterns.role),
                    preferred_tone = COALESCE(:tone, communication_patterns.preferred_tone),
                    response_urgency = COALESCE(:urgency, communication_patterns.response_urgency),
                    typical_response_length = COALESCE(:length, communication_patterns.typical_response_length),
                    greeting_style = COALESCE(:greeting, communication_patterns.greeting_style),
                    sign_off_style = COALESCE(:signoff, communication_patterns.sign_off_style),
                    interaction_count = communication_patterns.interaction_count + CASE WHEN :increment THEN 1 ELSE 0 END,
                    pattern_confidence = LEAST(0.95, communication_patterns.pattern_confidence + 0.05),
                    updated_at = NOW(),
                    last_interaction = NOW()
            """), {
                "id": pattern_id,
                "email": person_email,
                "name": updates.get("person_name"),
                "relationship": updates.get("relationship_type"),
                "org": updates.get("organization"),
                "role": updates.get("role"),
                "tone": updates.get("preferred_tone"),
                "urgency": updates.get("response_urgency"),
                "length": updates.get("typical_response_length"),
                "greeting": updates.get("greeting_style"),
                "signoff": updates.get("sign_off_style"),
                "actions": updates.get("typical_actions", []),
                "topics": updates.get("topics", []),
                "increment": increment_interaction,
            })

            await session.commit()

    async def add_example_trace(self, person_email: str, trace_id: str) -> None:
        """Add an example trace to a person's pattern."""
        async for session in self._get_session():
            await session.execute(text("""
                UPDATE communication_patterns
                SET example_trace_ids = array_append(
                    CASE WHEN array_length(example_trace_ids, 1) >= 10
                         THEN example_trace_ids[2:]
                         ELSE example_trace_ids END,
                    :trace_id
                )
                WHERE person_email = :email
            """), {"email": person_email, "trace_id": trace_id})
            await session.commit()


# =============================================================================
# Preference Rules
# =============================================================================

class PreferenceRuleMemory:
    """Manages extracted preference rules."""

    def __init__(self, get_session_func):
        self._get_session = get_session_func

    async def get_matching_rules(
        self,
        context: dict,
        rule_type: str | None = None,
    ) -> list[dict]:
        """Get preference rules that match the given context."""
        filters = ["is_active = true"]
        params = {}

        if rule_type:
            filters.append("rule_type = :rule_type")
            params["rule_type"] = rule_type

        where_clause = " AND ".join(filters)

        async for session in self._get_session():
            result = await session.execute(text(f"""
                SELECT * FROM preference_rules
                WHERE {where_clause}
                ORDER BY confidence DESC, evidence_count DESC
            """), params)

            rules = []
            for row in result.fetchall():
                # Check if rule condition matches context
                condition = row.condition or {}
                if self._condition_matches(condition, context):
                    rules.append({
                        "id": row.id,
                        "rule_type": row.rule_type,
                        "rule_name": row.rule_name,
                        "condition": condition,
                        "preference": row.preference,
                        "confidence": row.confidence,
                        "user_confirmed": row.user_confirmed,
                    })

            return rules

    def _condition_matches(self, condition: dict, context: dict) -> bool:
        """Check if a rule condition matches the context."""
        for key, expected in condition.items():
            actual = context.get(key)
            if actual is None:
                continue  # Skip missing keys
            if isinstance(expected, list):
                if actual not in expected:
                    return False
            elif actual != expected:
                return False
        return True

    async def create_rule(
        self,
        rule_type: str,
        condition: dict,
        preference: dict,
        rule_name: str | None = None,
        source_trace_ids: list[str] | None = None,
        confidence: float = 0.5,
    ) -> str:
        """Create a new preference rule."""
        rule_id = f"rule_{uuid.uuid4().hex[:12]}"

        async for session in self._get_session():
            await session.execute(text("""
                INSERT INTO preference_rules (
                    id, rule_type, rule_name, condition, preference,
                    confidence, evidence_count, source_trace_ids
                ) VALUES (
                    :id, :rule_type, :rule_name, :condition, :preference,
                    :confidence, :evidence_count, :source_trace_ids
                )
            """), {
                "id": rule_id,
                "rule_type": rule_type,
                "rule_name": rule_name,
                "condition": json.dumps(condition),
                "preference": json.dumps(preference),
                "confidence": confidence,
                "evidence_count": len(source_trace_ids or []),
                "source_trace_ids": source_trace_ids or [],
            })

            await session.commit()
            return rule_id

    async def reinforce_rule(self, rule_id: str, trace_id: str) -> None:
        """Reinforce a rule with additional evidence."""
        async for session in self._get_session():
            await session.execute(text("""
                UPDATE preference_rules
                SET evidence_count = evidence_count + 1,
                    confidence = LEAST(0.95, confidence + 0.02),
                    source_trace_ids = array_append(source_trace_ids, :trace_id),
                    updated_at = NOW()
                WHERE id = :rule_id
            """), {"rule_id": rule_id, "trace_id": trace_id})
            await session.commit()

    # =========================================================================
    # Phase 4: Preference Rule Validation Lifecycle (4.1)
    # =========================================================================

    async def record_rule_application(
        self,
        rule_id: str,
        trace_id: str,
        was_successful: bool,
    ) -> None:
        """
        Record when a rule was applied and whether it led to a good outcome.

        Args:
            rule_id: The rule that was applied
            trace_id: The decision trace where it was applied
            was_successful: Whether the decision had quality_score >= 0.7
        """
        async for session in self._get_session():
            await session.execute(text("""
                UPDATE preference_rules
                SET applications = COALESCE(applications, 0) + 1,
                    successful_applications = COALESCE(successful_applications, 0) + :success_inc,
                    success_rate = CASE
                        WHEN COALESCE(applications, 0) + 1 > 0
                        THEN (COALESCE(successful_applications, 0) + :success_inc)::float /
                             (COALESCE(applications, 0) + 1)
                        ELSE NULL
                    END,
                    last_applied_at = NOW(),
                    updated_at = NOW()
                WHERE id = :rule_id
            """), {
                "rule_id": rule_id,
                "success_inc": 1 if was_successful else 0,
            })
            await session.commit()

    async def validate_rules(self) -> dict:
        """
        Validate all rules based on their application history.

        Updates lifecycle status:
        - CANDIDATE (< 3 applications)
        - ACTIVE (3+ applications, > 50% success)
        - VALIDATED (10+ applications, > 70% success)
        - DEPRECATED (< 30% success after 5+ applications)

        Returns:
            Dict with counts of rules in each lifecycle stage
        """
        stats = {
            "validated": 0,
            "deprecated": 0,
            "promoted_to_active": 0,
            "total_evaluated": 0,
        }

        async for session in self._get_session():
            # Promote to VALIDATED (10+ applications, >70% success)
            result = await session.execute(text("""
                UPDATE preference_rules
                SET lifecycle = 'validated',
                    validated_at = NOW(),
                    updated_at = NOW()
                WHERE lifecycle IN ('candidate', 'active')
                  AND applications >= 10
                  AND success_rate >= 0.7
                  AND is_active = true
                RETURNING id
            """))
            stats["validated"] = len(result.fetchall())

            # Promote to ACTIVE (3+ applications, >50% success)
            result = await session.execute(text("""
                UPDATE preference_rules
                SET lifecycle = 'active',
                    updated_at = NOW()
                WHERE lifecycle = 'candidate'
                  AND applications >= 3
                  AND success_rate >= 0.5
                  AND is_active = true
                RETURNING id
            """))
            stats["promoted_to_active"] = len(result.fetchall())

            # Deprecate (5+ applications, <30% success)
            result = await session.execute(text("""
                UPDATE preference_rules
                SET lifecycle = 'deprecated',
                    is_active = false,
                    deprecated_at = NOW(),
                    deprecation_reason = 'Low success rate after ' || applications || ' applications',
                    updated_at = NOW()
                WHERE lifecycle IN ('candidate', 'active')
                  AND applications >= 5
                  AND success_rate < 0.3
                  AND is_active = true
                RETURNING id
            """))
            stats["deprecated"] = len(result.fetchall())

            # Get total evaluated
            result = await session.execute(text("""
                SELECT COUNT(*) as total FROM preference_rules
                WHERE applications > 0
            """))
            row = result.fetchone()
            stats["total_evaluated"] = row.total if row else 0

            await session.commit()

        logger.info("Rule validation complete", **stats)
        return stats

    async def get_rules_by_lifecycle(self) -> dict:
        """
        Get all rules grouped by lifecycle stage.

        Returns:
            Dict with lifecycle stage as key and list of rules as value
        """
        rules_by_stage = {
            "candidate": [],
            "active": [],
            "validated": [],
            "deprecated": [],
        }

        async for session in self._get_session():
            result = await session.execute(text("""
                SELECT
                    id, rule_name, rule_type, lifecycle,
                    confidence, applications, success_rate,
                    created_at, validated_at, deprecated_at, deprecation_reason
                FROM preference_rules
                ORDER BY
                    CASE lifecycle
                        WHEN 'validated' THEN 1
                        WHEN 'active' THEN 2
                        WHEN 'candidate' THEN 3
                        WHEN 'deprecated' THEN 4
                    END,
                    success_rate DESC NULLS LAST
            """))

            for row in result.fetchall():
                stage = row.lifecycle or "candidate"
                rules_by_stage[stage].append({
                    "id": row.id,
                    "name": row.rule_name,
                    "type": row.rule_type,
                    "confidence": row.confidence,
                    "applications": row.applications or 0,
                    "success_rate": round(row.success_rate * 100, 1) if row.success_rate else None,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                    "validated_at": row.validated_at.isoformat() if row.validated_at else None,
                    "deprecated_at": row.deprecated_at.isoformat() if row.deprecated_at else None,
                    "deprecation_reason": row.deprecation_reason,
                })
            break

        return rules_by_stage

    async def deprecate_rule(self, rule_id: str, reason: str) -> bool:
        """Manually deprecate a rule."""
        async for session in self._get_session():
            result = await session.execute(text("""
                UPDATE preference_rules
                SET lifecycle = 'deprecated',
                    is_active = false,
                    deprecated_at = NOW(),
                    deprecation_reason = :reason,
                    updated_at = NOW()
                WHERE id = :rule_id AND is_active = true
                RETURNING id
            """), {"rule_id": rule_id, "reason": reason})
            row = result.fetchone()
            await session.commit()
            return row is not None

    async def get_rule_stats(self) -> dict:
        """Get statistics about preference rules."""
        async for session in self._get_session():
            result = await session.execute(text("""
                SELECT
                    COUNT(*) as total,
                    COUNT(*) FILTER (WHERE is_active = true) as active,
                    COUNT(*) FILTER (WHERE lifecycle = 'validated') as validated,
                    COUNT(*) FILTER (WHERE lifecycle = 'deprecated') as deprecated,
                    COUNT(*) FILTER (WHERE lifecycle = 'candidate') as candidate,
                    AVG(success_rate) FILTER (WHERE applications >= 3) as avg_success_rate,
                    SUM(applications) as total_applications
                FROM preference_rules
            """))
            row = result.fetchone()
            return {
                "total": row.total or 0,
                "active": row.active or 0,
                "validated": row.validated or 0,
                "deprecated": row.deprecated or 0,
                "candidate": row.candidate or 0,
                "avg_success_rate": round(row.avg_success_rate * 100, 1) if row.avg_success_rate else None,
                "total_applications": row.total_applications or 0,
            }


# =============================================================================
# Training Data Export
# =============================================================================

class TrainingDataExporter:
    """Export decision traces for fine-tuning."""

    def __init__(self, get_session_func):
        self._get_session = get_session_func

    async def export_training_data(
        self,
        min_quality: float = 0.6,
        limit: int | None = None,
        format: str = "jsonl",
    ) -> list[dict]:
        """
        Export training data in a format suitable for fine-tuning.

        Args:
            min_quality: Minimum quality score
            limit: Max records
            format: Output format (jsonl, messages)

        Returns:
            List of training examples
        """
        async for session in self._get_session():
            query = """
                SELECT id, trigger_type, trigger_summary, context,
                       action_type, proposed_action, final_action,
                       reasoning, status, explicit_feedback, quality_score
                FROM decision_traces
                WHERE include_in_training = true
                  AND quality_score >= :min_quality
                  AND status IN ('approved', 'edited', 'auto_executed')
                ORDER BY quality_score DESC, created_at DESC
            """
            if limit:
                query += f" LIMIT {limit}"

            result = await session.execute(text(query), {"min_quality": min_quality})

            examples = []
            for row in result.fetchall():
                # Use final_action if available, else proposed_action
                action = row.final_action or row.proposed_action
                context = row.context or {}

                # Build training example
                example = {
                    "id": row.id,
                    "quality_score": row.quality_score,
                    "messages": [
                        {
                            "role": "system",
                            "content": "You are a personal AI assistant. Respond to situations in the user's preferred style based on context."
                        },
                        {
                            "role": "user",
                            "content": self._format_context_prompt(
                                row.trigger_type,
                                row.trigger_summary,
                                context,
                            )
                        },
                        {
                            "role": "assistant",
                            "content": self._format_action_response(
                                row.action_type,
                                action,
                                row.reasoning,
                            )
                        }
                    ]
                }

                # Add feedback as refinement if available
                if row.status == "edited" and row.explicit_feedback:
                    example["refinement_feedback"] = row.explicit_feedback

                examples.append(example)

            return examples

    def _format_context_prompt(
        self,
        trigger_type: str,
        trigger_summary: str | None,
        context: dict,
    ) -> str:
        """Format context into a prompt."""
        parts = [f"Situation: {trigger_type}"]

        if trigger_summary:
            parts.append(f"Summary: {trigger_summary}")

        # Add key context elements
        if "sender_profile" in context:
            profile = context["sender_profile"]
            parts.append(f"From: {profile.get('name', 'Unknown')} ({profile.get('relationship_type', 'unknown')})")

        if "trigger_content" in context:
            content = context["trigger_content"]
            if isinstance(content, dict):
                if "subject" in content:
                    parts.append(f"Subject: {content['subject']}")
                if "body_snippet" in content:
                    parts.append(f"Content: {content['body_snippet'][:500]}")
            else:
                parts.append(f"Content: {str(content)[:500]}")

        if "related_entities" in context:
            related = context["related_entities"]
            if related.get("tasks"):
                parts.append(f"Related tasks: {len(related['tasks'])}")
            if related.get("projects"):
                parts.append(f"Related projects: {len(related['projects'])}")

        parts.append("\nWhat action should be taken?")

        return "\n".join(parts)

    def _format_action_response(
        self,
        action_type: str,
        action: dict | None,
        reasoning: str | None,
    ) -> str:
        """Format action into a response."""
        parts = [f"Action: {action_type}"]

        if reasoning:
            parts.append(f"Reasoning: {reasoning}")

        if action:
            if isinstance(action, dict):
                if "content" in action:
                    parts.append(f"Content: {action['content']}")
                if "tone" in action:
                    parts.append(f"Tone: {action['tone']}")
                # Add other relevant action fields
                for key in ["priority", "due_date", "assignee"]:
                    if key in action:
                        parts.append(f"{key.title()}: {action[key]}")
            else:
                parts.append(f"Details: {action}")

        return "\n".join(parts)

    async def get_training_stats(self) -> dict:
        """Get statistics about available training data."""
        async for session in self._get_session():
            result = await session.execute(text("""
                SELECT
                    COUNT(*) as total,
                    COUNT(*) FILTER (WHERE quality_score >= 0.8) as high_quality,
                    COUNT(*) FILTER (WHERE quality_score >= 0.6 AND quality_score < 0.8) as medium_quality,
                    COUNT(*) FILTER (WHERE quality_score < 0.6) as low_quality,
                    COUNT(*) FILTER (WHERE include_in_training = true AND quality_score >= 0.6) as trainable,
                    AVG(quality_score) as avg_quality
                FROM decision_traces
                WHERE status != 'pending'
            """))

            row = result.fetchone()
            return {
                "total_traces": row.total,
                "high_quality": row.high_quality,
                "medium_quality": row.medium_quality,
                "low_quality": row.low_quality,
                "trainable": row.trainable,
                "avg_quality": round(float(row.avg_quality or 0), 2),
            }


# =============================================================================
# Combined Decision Memory System
# =============================================================================

class DecisionMemory:
    """Combined decision memory system."""

    def __init__(self, get_session_func):
        self.traces = DecisionTraceMemory(get_session_func)
        self.patterns = CommunicationPatternMemory(get_session_func)
        self.rules = PreferenceRuleMemory(get_session_func)
        self.exporter = TrainingDataExporter(get_session_func)
        self._get_session = get_session_func

    async def extract_rules_from_patterns(self, min_occurrences: int = 3) -> list[str]:
        """
        Analyze decision traces and extract preference rules from consistent patterns.

        This looks for:
        - Consistent action patterns for specific trigger types
        - Time-of-day preferences
        - Sender-specific behaviors

        Returns list of created/reinforced rule IDs.
        """
        rule_ids = []

        try:
            # Find patterns in high-quality traces
            async for session in self._get_session():
                # Look for action patterns by trigger type
                result = await session.execute(text("""
                    SELECT
                        trigger_type,
                        action_type,
                        context->>'temporal_context'->>'time_of_day' as time_of_day,
                        COUNT(*) as occurrences,
                        AVG(quality_score) as avg_quality,
                        array_agg(id) as trace_ids
                    FROM decision_traces
                    WHERE status IN ('approved', 'edited', 'auto_executed')
                      AND quality_score >= 0.6
                      AND created_at > NOW() - INTERVAL '30 days'
                    GROUP BY trigger_type, action_type,
                             context->>'temporal_context'->>'time_of_day'
                    HAVING COUNT(*) >= :min_occurrences
                    ORDER BY occurrences DESC
                    LIMIT 20
                """), {"min_occurrences": min_occurrences})

                for row in result.fetchall():
                    # Create rule for consistent patterns
                    condition = {"trigger_type": row.trigger_type}
                    if row.time_of_day:
                        condition["time_of_day"] = row.time_of_day

                    preference = {
                        "preferred_action": row.action_type,
                        "evidence_quality": float(row.avg_quality),
                    }

                    rule_name = f"When {row.trigger_type}"
                    if row.time_of_day:
                        rule_name += f" during {row.time_of_day}"
                    rule_name += f", prefer {row.action_type}"

                    # Check if similar rule exists
                    existing = await self.rules.get_matching_rules(condition, "action_preference")
                    if existing:
                        # Reinforce existing rule
                        for trace_id in row.trace_ids[:5]:
                            await self.rules.reinforce_rule(existing[0]["id"], trace_id)
                        rule_ids.append(existing[0]["id"])
                    else:
                        # Create new rule
                        confidence = min(0.9, 0.3 + (row.occurrences * 0.1))
                        rule_id = await self.rules.create_rule(
                            rule_type="action_preference",
                            condition=condition,
                            preference=preference,
                            rule_name=rule_name,
                            source_trace_ids=row.trace_ids[:10],
                            confidence=confidence,
                        )
                        rule_ids.append(rule_id)
                        logger.info("Created preference rule", rule_name=rule_name, confidence=confidence)

                break  # Only need one session iteration

        except Exception as e:
            logger.warning("Failed to extract rules from patterns", error=str(e))

        return rule_ids

    async def build_context_for_trigger(
        self,
        trigger_type: str,
        trigger_data: dict,
    ) -> dict:
        """
        Build a rich context snapshot for a trigger.

        This gathers all relevant information that should inform
        the agent's decision.
        """
        context = {
            "trigger_content": trigger_data,
            "temporal_context": {
                "timestamp": datetime.now().isoformat(),
                "day_of_week": datetime.now().strftime("%A"),
                "time_of_day": self._get_time_of_day(),
                "is_business_hours": self._is_business_hours(),
            },
        }

        # Add sender profile if applicable
        sender_email = trigger_data.get("sender_email") or trigger_data.get("from")
        if sender_email:
            pattern = await self.patterns.get_pattern(sender_email)
            if pattern:
                context["sender_profile"] = pattern

        # Find matching preference rules
        matching_rules = await self.rules.get_matching_rules({
            "trigger_type": trigger_type,
            **(context.get("sender_profile") or {}),
        })
        if matching_rules:
            context["applicable_rules"] = matching_rules

        # Find similar past decisions
        similar = await self.traces.find_similar_decisions(
            context=context,
            trigger_type=trigger_type,
            min_quality=0.6,
            limit=3,
        )
        if similar:
            context["similar_past_decisions"] = similar

        return context

    def _get_time_of_day(self) -> str:
        """Get time of day category."""
        hour = datetime.now().hour
        if hour < 9:
            return "early_morning"
        elif hour < 12:
            return "morning"
        elif hour < 14:
            return "lunch"
        elif hour < 17:
            return "afternoon"
        elif hour < 20:
            return "evening"
        else:
            return "night"

    def _is_business_hours(self) -> bool:
        """Check if current time is business hours."""
        now = datetime.now()
        return (
            now.weekday() < 5 and  # Monday-Friday
            9 <= now.hour < 18  # 9am-6pm
        )


# =============================================================================
# Singleton
# =============================================================================

_decision_memory: DecisionMemory | None = None


async def init_decision_memory() -> DecisionMemory:
    """Initialize the decision memory system."""
    global _decision_memory

    from cognitex.db.postgres import get_session

    _decision_memory = DecisionMemory(get_session)

    # Ensure schema exists
    async for session in get_session():
        await init_decision_memory_schema(session)
        break

    logger.info("Decision memory system initialized")
    return _decision_memory


def get_decision_memory() -> DecisionMemory:
    """Get the decision memory singleton."""
    if _decision_memory is None:
        raise RuntimeError("Decision memory not initialized. Call init_decision_memory() first.")
    return _decision_memory
