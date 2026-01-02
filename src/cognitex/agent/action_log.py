"""
Simple Agent Action Log

Lightweight logging for all agent actions - designed to be called from anywhere.
Unlike decision_traces (for ML training), this is just for visibility.
"""

import uuid
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import text

logger = structlog.get_logger()

# Schema for simple action log
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS agent_actions (
    id TEXT PRIMARY KEY,
    timestamp TIMESTAMP DEFAULT NOW(),
    action_type TEXT NOT NULL,
    source TEXT NOT NULL,
    summary TEXT,
    details JSONB DEFAULT '{}',
    status TEXT DEFAULT 'completed',
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_agent_actions_timestamp ON agent_actions(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_agent_actions_type ON agent_actions(action_type);
CREATE INDEX IF NOT EXISTS idx_agent_actions_source ON agent_actions(source);

CREATE TABLE IF NOT EXISTS task_proposals (
    id TEXT PRIMARY KEY,
    timestamp TIMESTAMP DEFAULT NOW(),
    title TEXT NOT NULL,
    description TEXT,
    project_id TEXT,
    goal_id TEXT,
    priority TEXT DEFAULT 'medium',
    reason TEXT,
    status TEXT DEFAULT 'pending',
    decision_at TIMESTAMP,
    decision_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_task_proposals_status ON task_proposals(status);
CREATE INDEX IF NOT EXISTS idx_task_proposals_timestamp ON task_proposals(timestamp DESC);
"""


async def ensure_schema():
    """Ensure the action log table exists."""
    from cognitex.db.postgres import get_session

    async for session in get_session():
        for stmt in SCHEMA_SQL.split(';'):
            stmt = stmt.strip()
            if stmt:
                try:
                    await session.execute(text(stmt))
                except Exception:
                    pass  # Table/index may already exist
        await session.commit()
        break


async def log_action(
    action_type: str,
    source: str,
    summary: str | None = None,
    details: dict | None = None,
    status: str = "completed",
    error: str | None = None,
) -> str:
    """
    Log an agent action.

    Args:
        action_type: Type of action (e.g., "morning_briefing", "email_analysis", "task_created")
        source: Where this came from (e.g., "trigger", "discord", "email", "chat")
        summary: Brief human-readable summary
        details: Optional JSON details
        status: "completed", "failed", "pending"
        error: Error message if failed

    Returns:
        Action ID
    """
    import json
    from cognitex.db.postgres import get_session

    action_id = f"act_{uuid.uuid4().hex[:12]}"

    try:
        async for session in get_session():
            await session.execute(text("""
                INSERT INTO agent_actions (id, action_type, source, summary, details, status, error)
                VALUES (:id, :action_type, :source, :summary, :details, :status, :error)
            """), {
                "id": action_id,
                "action_type": action_type,
                "source": source,
                "summary": summary,
                "details": json.dumps(details or {}),
                "status": status,
                "error": error,
            })
            await session.commit()
            break

        logger.debug("Logged action", action_id=action_id, action_type=action_type, source=source)

    except Exception as e:
        logger.warning("Failed to log action", error=str(e), action_type=action_type)

    return action_id


async def get_recent_actions(limit: int = 100) -> list[dict]:
    """Get recent actions."""
    from cognitex.db.postgres import get_session

    async for session in get_session():
        result = await session.execute(text("""
            SELECT id, timestamp, action_type, source, summary, details, status, error
            FROM agent_actions
            ORDER BY timestamp DESC
            LIMIT :limit
        """), {"limit": limit})

        return [{
            "id": row.id,
            "timestamp": row.timestamp.isoformat() if row.timestamp else None,
            "action_type": row.action_type,
            "source": row.source,
            "summary": row.summary,
            "details": row.details,
            "status": row.status,
            "error": row.error,
        } for row in result.fetchall()]

    return []


async def get_recent_notifications(hours: int = 48) -> list[dict]:
    """
    Get recent notification-related actions for context.

    This helps the agent avoid sending duplicate or repetitive notifications
    by showing what was already notified about.

    Args:
        hours: How many hours back to look (default 48)

    Returns:
        List of notification actions with summary and details
    """
    from cognitex.db.postgres import get_session

    async for session in get_session():
        result = await session.execute(text("""
            SELECT
                timestamp,
                action_type,
                summary,
                details
            FROM agent_actions
            WHERE timestamp > NOW() - INTERVAL '%s hours'
              AND action_type IN (
                  'notification_sent', 'schedule_block', 'compile_context_pack',
                  'draft_email', 'morning_briefing', 'evening_review',
                  'email_analysis', 'overdue_check'
              )
              AND status = 'completed'
            ORDER BY timestamp DESC
            LIMIT 50
        """ % hours))

        return [{
            "timestamp": row.timestamp.strftime("%Y-%m-%d %H:%M") if row.timestamp else None,
            "action_type": row.action_type,
            "summary": row.summary,
            "details": row.details,
        } for row in result.fetchall()]

    return []


async def get_action_stats() -> dict:
    """Get action statistics."""
    from cognitex.db.postgres import get_session

    async for session in get_session():
        result = await session.execute(text("""
            SELECT
                COUNT(*) as total,
                COUNT(*) FILTER (WHERE timestamp > NOW() - INTERVAL '24 hours') as last_24h,
                COUNT(*) FILTER (WHERE status = 'failed') as failed,
                COUNT(DISTINCT action_type) as action_types,
                COUNT(DISTINCT source) as sources
            FROM agent_actions
        """))
        row = result.fetchone()
        return {
            "total": row.total or 0,
            "last_24h": row.last_24h or 0,
            "failed": row.failed or 0,
            "action_types": row.action_types or 0,
            "sources": row.sources or 0,
        }

    return {}


# ============================================================================
# Task Proposal Functions
# ============================================================================

async def propose_task(
    title: str,
    description: str | None = None,
    project_id: str | None = None,
    goal_id: str | None = None,
    priority: str = "medium",
    reason: str | None = None,
) -> str:
    """
    Propose a task for approval instead of creating it directly.

    Returns:
        Proposal ID
    """
    from cognitex.db.postgres import get_session

    proposal_id = f"prop_{uuid.uuid4().hex[:12]}"

    try:
        async for session in get_session():
            await session.execute(text("""
                INSERT INTO task_proposals (id, title, description, project_id, goal_id, priority, reason)
                VALUES (:id, :title, :description, :project_id, :goal_id, :priority, :reason)
            """), {
                "id": proposal_id,
                "title": title,
                "description": description,
                "project_id": project_id,
                "goal_id": goal_id,
                "priority": priority,
                "reason": reason,
            })
            await session.commit()
            break

        logger.info("Task proposed for approval", proposal_id=proposal_id, title=title[:50])

    except Exception as e:
        logger.warning("Failed to propose task", error=str(e), title=title[:50])

    return proposal_id


async def get_pending_proposals(limit: int = 20) -> list[dict]:
    """Get pending task proposals."""
    from cognitex.db.postgres import get_session

    async for session in get_session():
        result = await session.execute(text("""
            SELECT id, timestamp, title, description, project_id, goal_id, priority, reason
            FROM task_proposals
            WHERE status = 'pending'
            ORDER BY timestamp DESC
            LIMIT :limit
        """), {"limit": limit})

        return [{
            "id": row.id,
            "timestamp": row.timestamp.isoformat() if row.timestamp else None,
            "title": row.title,
            "description": row.description,
            "project_id": row.project_id,
            "goal_id": row.goal_id,
            "priority": row.priority,
            "reason": row.reason,
        } for row in result.fetchall()]

    return []


async def approve_proposal(proposal_id: str, reason: str | None = None) -> dict | None:
    """
    Approve a task proposal and create the actual task.

    Returns:
        Created task dict or None if proposal not found
    """
    from cognitex.db.postgres import get_session
    from cognitex.services.tasks import get_task_service

    async for session in get_session():
        # Get the proposal
        result = await session.execute(text("""
            SELECT id, title, description, project_id, goal_id, priority, reason
            FROM task_proposals
            WHERE id = :id AND status = 'pending'
        """), {"id": proposal_id})
        row = result.fetchone()

        if not row:
            return None

        # Create the actual task
        task_service = get_task_service()
        task = await task_service.create(
            title=row.title,
            description=row.description,
            project_id=row.project_id,
            goal_id=row.goal_id,
            priority=row.priority,
            source_type="agent_proposal",
        )

        # Mark proposal as approved
        await session.execute(text("""
            UPDATE task_proposals
            SET status = 'approved', decision_at = NOW(), decision_reason = :reason
            WHERE id = :id
        """), {"id": proposal_id, "reason": reason})
        await session.commit()

        logger.info("Task proposal approved", proposal_id=proposal_id, task_id=task.get("id"))
        return task

    return None


async def reject_proposal(proposal_id: str, reason: str | None = None) -> bool:
    """
    Reject a task proposal.

    Returns:
        True if rejected, False if not found
    """
    from cognitex.db.postgres import get_session

    async for session in get_session():
        result = await session.execute(text("""
            UPDATE task_proposals
            SET status = 'rejected', decision_at = NOW(), decision_reason = :reason
            WHERE id = :id AND status = 'pending'
            RETURNING id
        """), {"id": proposal_id, "reason": reason})
        row = result.fetchone()
        await session.commit()

        if row:
            logger.info("Task proposal rejected", proposal_id=proposal_id, reason=reason)
            return True

    return False


async def get_proposal_stats() -> dict:
    """Get task proposal statistics for learning."""
    from cognitex.db.postgres import get_session

    async for session in get_session():
        result = await session.execute(text("""
            SELECT
                COUNT(*) FILTER (WHERE status = 'pending') as pending,
                COUNT(*) FILTER (WHERE status = 'approved') as approved,
                COUNT(*) FILTER (WHERE status = 'rejected') as rejected,
                COUNT(*) as total
            FROM task_proposals
        """))
        row = result.fetchone()
        return {
            "pending": row.pending or 0,
            "approved": row.approved or 0,
            "rejected": row.rejected or 0,
            "total": row.total or 0,
            "approval_rate": (row.approved / row.total * 100) if row.total > 0 else 0,
        }

    return {}
