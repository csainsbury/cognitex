"""Phase 4 schema: Adaptive Memory & Learning System.

This module provides schema migrations for the Phase 4 learning system:
- Task timing for duration calibration
- Deferral analysis for procrastination patterns
- Interruption events for context switch costs
- Preference rule lifecycle for validation
- Learned patterns storage in Neo4j
"""

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from neo4j import AsyncSession as Neo4jSession

logger = structlog.get_logger()


# =============================================================================
# PostgreSQL Schema
# =============================================================================

POSTGRES_SCHEMA_SQL = """
-- Task timing for duration calibration (2.1)
CREATE TABLE IF NOT EXISTS task_timing (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    estimated_minutes INTEGER,
    actual_minutes INTEGER,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    interruption_count INTEGER DEFAULT 0,
    context TEXT,  -- 'morning', 'afternoon', 'evening', 'after_meeting', 'fragmented'
    project_id TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_task_timing_task ON task_timing(task_id);
CREATE INDEX IF NOT EXISTS idx_task_timing_project ON task_timing(project_id);
CREATE INDEX IF NOT EXISTS idx_task_timing_completed ON task_timing(completed_at);

-- Deferral analysis for procrastination patterns (3.1)
CREATE TABLE IF NOT EXISTS deferral_analysis (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    deferred_at TIMESTAMP DEFAULT NOW(),
    inferred_reason TEXT,  -- 'unclear_next_step', 'too_large', 'boring', 'anxiety', 'dependency', 'unknown'
    context JSONB DEFAULT '{}',
    friction_at_deferral FLOAT,
    deferral_count_at_time INTEGER DEFAULT 1,
    eventually_completed BOOLEAN DEFAULT FALSE,
    completion_trigger TEXT,  -- 'deadline', 'decomposition', 'energy_spike', 'external_push', 'mvs_generated'
    completed_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_deferral_task ON deferral_analysis(task_id);
CREATE INDEX IF NOT EXISTS idx_deferral_reason ON deferral_analysis(inferred_reason);
CREATE INDEX IF NOT EXISTS idx_deferral_completed ON deferral_analysis(eventually_completed);

-- Interruption events for context switch costs (3.2)
CREATE TABLE IF NOT EXISTS interruption_events (
    id TEXT PRIMARY KEY,
    timestamp TIMESTAMP DEFAULT NOW(),
    source TEXT NOT NULL,  -- 'email', 'calendar', 'discord', 'slack', 'phone', 'in_person'
    urgency TEXT DEFAULT 'medium',  -- 'low', 'medium', 'high', 'critical'
    mode_at_interrupt TEXT,  -- Operating mode when interrupted
    task_in_progress TEXT,  -- Task ID or description
    response_action TEXT,  -- 'engaged', 'batched', 'ignored', 'deferred'
    recovery_minutes INTEGER,  -- Time to return to original task
    context_lost BOOLEAN DEFAULT FALSE,
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_interrupt_source ON interruption_events(source);
CREATE INDEX IF NOT EXISTS idx_interrupt_mode ON interruption_events(mode_at_interrupt);
CREATE INDEX IF NOT EXISTS idx_interrupt_timestamp ON interruption_events(timestamp);

-- Learned patterns cache (for quick lookups)
CREATE TABLE IF NOT EXISTS learned_patterns (
    id TEXT PRIMARY KEY,
    pattern_type TEXT NOT NULL,  -- 'proposal_acceptance', 'temporal', 'deferral', 'duration', 'interruption'
    pattern_key TEXT NOT NULL,  -- project_id, hour, reason, etc.
    pattern_data JSONB NOT NULL,
    sample_size INTEGER DEFAULT 0,
    confidence FLOAT DEFAULT 0.5,
    last_updated TIMESTAMP DEFAULT NOW(),
    UNIQUE(pattern_type, pattern_key)
);

CREATE INDEX IF NOT EXISTS idx_patterns_type ON learned_patterns(pattern_type);
CREATE INDEX IF NOT EXISTS idx_patterns_updated ON learned_patterns(last_updated);

-- State observations for learning energy patterns (Phase 5)
-- Records task outcomes with state context to learn temporal/state patterns
CREATE TABLE IF NOT EXISTS state_observations (
    id SERIAL PRIMARY KEY,
    task_id TEXT,
    task_title TEXT,
    outcome TEXT NOT NULL,  -- 'completed', 'deferred', 'abandoned'
    mode TEXT,  -- Operating mode when task was attempted
    fatigue_level FLOAT,  -- 0-1 scale
    focus_score FLOAT,  -- 0-1 scale
    hour_of_day INTEGER,  -- 0-23
    day_of_week INTEGER,  -- 0=Monday, 6=Sunday
    post_clinical BOOLEAN DEFAULT FALSE,
    minutes_since_clinical INTEGER,
    energy_cost TEXT,  -- 'high', 'medium', 'low' (task property)
    task_friction INTEGER,  -- 0-5 scale
    observed_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_state_obs_hour ON state_observations(hour_of_day);
CREATE INDEX IF NOT EXISTS idx_state_obs_mode ON state_observations(mode);
CREATE INDEX IF NOT EXISTS idx_state_obs_outcome ON state_observations(outcome);
CREATE INDEX IF NOT EXISTS idx_state_obs_clinical ON state_observations(post_clinical);
CREATE INDEX IF NOT EXISTS idx_state_obs_timestamp ON state_observations(observed_at);

-- User feedback for nuanced learning (semantic retrieval + rule extraction)
CREATE TABLE IF NOT EXISTS user_feedback (
    id TEXT PRIMARY KEY,
    target_type TEXT NOT NULL,      -- 'context_pack', 'email_draft', 'task', 'proposal'
    target_id TEXT NOT NULL,
    feedback_category TEXT,         -- Quick-select category (e.g., 'not_needed', 'spam_marketing')
    feedback_text TEXT,             -- Free text details for nuanced learning
    feedback_embedding vector(768), -- Semantic embedding for retrieval
    context JSONB DEFAULT '{}',     -- Rich context snapshot (email_subject, sender, etc.)
    was_rejection BOOLEAN DEFAULT false,
    action_taken TEXT,              -- 'rejected', 'edited', 'approved_with_note'
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_user_feedback_type ON user_feedback(target_type);
CREATE INDEX IF NOT EXISTS idx_user_feedback_target ON user_feedback(target_id);
CREATE INDEX IF NOT EXISTS idx_user_feedback_rejection ON user_feedback(was_rejection);
CREATE INDEX IF NOT EXISTS idx_user_feedback_created ON user_feedback(created_at);
CREATE INDEX IF NOT EXISTS idx_user_feedback_category ON user_feedback(feedback_category);

-- Domain expertise: Agent's "mental model" for specific domains
-- These are self-improving knowledge bases that agents update after successful actions
CREATE TABLE IF NOT EXISTS domain_expertise (
    id TEXT PRIMARY KEY,
    domain TEXT NOT NULL UNIQUE,        -- e.g., 'project:cognitex', 'email_drafting', 'task_extraction'
    domain_type TEXT NOT NULL,          -- 'project', 'skill', 'entity', 'workflow'
    title TEXT,                         -- Human-readable title
    expertise_content JSONB NOT NULL,   -- Structured expertise data (mental model)
    expertise_embedding vector(768),    -- Semantic embedding for matching
    version INTEGER DEFAULT 1,
    learnings_count INTEGER DEFAULT 0,  -- How many times this has been updated
    last_improved_at TIMESTAMP,
    last_used_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW(),
    created_by TEXT DEFAULT 'system'
);

CREATE INDEX IF NOT EXISTS idx_expertise_domain ON domain_expertise(domain);
CREATE INDEX IF NOT EXISTS idx_expertise_type ON domain_expertise(domain_type);
CREATE INDEX IF NOT EXISTS idx_expertise_improved ON domain_expertise(last_improved_at);
CREATE INDEX IF NOT EXISTS idx_expertise_used ON domain_expertise(last_used_at);

-- Expertise learnings log: Track what was learned and when
CREATE TABLE IF NOT EXISTS expertise_learnings (
    id TEXT PRIMARY KEY,
    expertise_id TEXT NOT NULL REFERENCES domain_expertise(id),
    learning_type TEXT NOT NULL,        -- 'pattern', 'preference', 'fact', 'relationship', 'correction'
    learning_content JSONB NOT NULL,    -- The actual learning
    source_action TEXT,                 -- What action triggered this learning
    source_id TEXT,                     -- ID of the task/email/etc that was completed
    confidence FLOAT DEFAULT 0.7,
    applied_count INTEGER DEFAULT 0,    -- How many times this learning was used
    successful_applications INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_learnings_expertise ON expertise_learnings(expertise_id);
CREATE INDEX IF NOT EXISTS idx_learnings_type ON expertise_learnings(learning_type);
CREATE INDEX IF NOT EXISTS idx_learnings_created ON expertise_learnings(created_at);

-- Add lifecycle columns to preference_rules if they don't exist
-- These are added via ALTER TABLE to preserve existing data
"""

POSTGRES_ALTER_STATEMENTS = [
    # Preference rule lifecycle additions
    "ALTER TABLE preference_rules ADD COLUMN IF NOT EXISTS lifecycle TEXT DEFAULT 'candidate'",
    "ALTER TABLE preference_rules ADD COLUMN IF NOT EXISTS applications INTEGER DEFAULT 0",
    "ALTER TABLE preference_rules ADD COLUMN IF NOT EXISTS successful_applications INTEGER DEFAULT 0",
    "ALTER TABLE preference_rules ADD COLUMN IF NOT EXISTS success_rate FLOAT",
    "ALTER TABLE preference_rules ADD COLUMN IF NOT EXISTS last_applied_at TIMESTAMP",
    "ALTER TABLE preference_rules ADD COLUMN IF NOT EXISTS validated_at TIMESTAMP",
    "ALTER TABLE preference_rules ADD COLUMN IF NOT EXISTS deprecated_at TIMESTAMP",
    "ALTER TABLE preference_rules ADD COLUMN IF NOT EXISTS deprecation_reason TEXT",
    # Task timing additions to tasks table
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS started_at TIMESTAMP",
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS actual_minutes INTEGER",
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS deferral_count INTEGER DEFAULT 0",
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS last_deferred_at TIMESTAMP",
    # Subtasks (lightweight steps) as JSONB
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS subtasks JSONB DEFAULT '[]'",
]


# =============================================================================
# Neo4j Schema
# =============================================================================

NEO4J_SCHEMA_STATEMENTS = [
    # Temporal pattern nodes
    "CREATE CONSTRAINT temporal_pattern_id IF NOT EXISTS FOR (p:TemporalPattern) REQUIRE p.id IS UNIQUE",
    "CREATE INDEX temporal_pattern_project IF NOT EXISTS FOR (p:TemporalPattern) ON (p.project_id)",

    # Pace factor nodes (duration calibration)
    "CREATE CONSTRAINT pace_factor_id IF NOT EXISTS FOR (p:PaceFactor) REQUIRE p.id IS UNIQUE",
    "CREATE INDEX pace_factor_project IF NOT EXISTS FOR (p:PaceFactor) ON (p.project_id)",

    # Deferral pattern nodes
    "CREATE CONSTRAINT deferral_pattern_id IF NOT EXISTS FOR (p:DeferralPattern) REQUIRE p.id IS UNIQUE",

    # Learning event nodes (for audit trail)
    "CREATE CONSTRAINT learning_event_id IF NOT EXISTS FOR (e:LearningEvent) REQUIRE e.id IS UNIQUE",
    "CREATE INDEX learning_event_type IF NOT EXISTS FOR (e:LearningEvent) ON (e.event_type)",
    "CREATE INDEX learning_event_timestamp IF NOT EXISTS FOR (e:LearningEvent) ON (e.timestamp)",
]


# =============================================================================
# Initialization Functions
# =============================================================================

async def init_phase4_postgres_schema(session: AsyncSession) -> None:
    """Initialize Phase 4 PostgreSQL tables."""
    # Create tables
    statements = [s.strip() for s in POSTGRES_SCHEMA_SQL.split(';') if s.strip()]
    for stmt in statements:
        try:
            await session.execute(text(stmt))
        except Exception as e:
            logger.debug("Phase 4 schema statement skipped", error=str(e)[:100])

    # Run ALTER statements
    for stmt in POSTGRES_ALTER_STATEMENTS:
        try:
            await session.execute(text(stmt))
        except Exception as e:
            logger.debug("Phase 4 alter statement skipped", error=str(e)[:100])

    await session.commit()
    logger.info("Phase 4 PostgreSQL schema initialized")


async def init_phase4_neo4j_schema(session: Neo4jSession) -> None:
    """Initialize Phase 4 Neo4j constraints and indexes."""
    for statement in NEO4J_SCHEMA_STATEMENTS:
        try:
            await session.run(statement)
        except Exception as e:
            logger.debug("Phase 4 Neo4j statement skipped", error=str(e)[:100])

    logger.info("Phase 4 Neo4j schema initialized")


async def init_phase4_schema() -> None:
    """Initialize all Phase 4 schema (PostgreSQL + Neo4j)."""
    from cognitex.db.postgres import get_session
    from cognitex.db.neo4j import get_neo4j_session

    # PostgreSQL
    async for pg_session in get_session():
        await init_phase4_postgres_schema(pg_session)
        break

    # Neo4j
    async for neo_session in get_neo4j_session():
        await init_phase4_neo4j_schema(neo_session)
        break

    logger.info("Phase 4 schema initialization complete")
