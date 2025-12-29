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
