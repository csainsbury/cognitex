"""Memory Consolidation System ("Dreaming")

Implements nightly memory consolidation inspired by human sleep:
- Summarizes daily events into distilled DailySummary nodes
- Extracts behavioral patterns for learning
- Archives old raw logs to reduce noise
- Strengthens important memories for better retrieval

Run via: cognitex consolidate (or schedule via cron at 3am)
"""

from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import text

logger = structlog.get_logger()


# PostgreSQL schema for consolidation
CONSOLIDATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_summaries (
    id TEXT PRIMARY KEY,
    date DATE NOT NULL UNIQUE,
    summary TEXT NOT NULL,

    -- Aggregated metrics
    emails_processed INTEGER DEFAULT 0,
    emails_actioned INTEGER DEFAULT 0,
    tasks_completed INTEGER DEFAULT 0,
    tasks_created INTEGER DEFAULT 0,
    tasks_deferred INTEGER DEFAULT 0,
    meetings_attended INTEGER DEFAULT 0,
    documents_indexed INTEGER DEFAULT 0,

    -- Extracted patterns (JSONB)
    productivity_patterns JSONB DEFAULT '[]',
    deferral_patterns JSONB DEFAULT '[]',
    communication_patterns JSONB DEFAULT '[]',

    -- Key events worth remembering
    key_events JSONB DEFAULT '[]',

    -- Learning signals extracted
    learning_signals JSONB DEFAULT '[]',

    created_at TIMESTAMP DEFAULT NOW(),
    raw_action_count INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_daily_summaries_date ON daily_summaries(date DESC);

-- Track which memories have been archived
CREATE TABLE IF NOT EXISTS memory_archive_log (
    id SERIAL PRIMARY KEY,
    archived_at TIMESTAMP DEFAULT NOW(),
    date_range_start DATE NOT NULL,
    date_range_end DATE NOT NULL,
    actions_archived INTEGER DEFAULT 0,
    actions_deleted INTEGER DEFAULT 0,
    summary TEXT
);
"""


async def ensure_consolidation_schema():
    """Ensure consolidation tables exist."""
    from cognitex.db.postgres import get_session

    async for session in get_session():
        for stmt in CONSOLIDATION_SCHEMA.split(';'):
            stmt = stmt.strip()
            if stmt:
                try:
                    await session.execute(text(stmt))
                except Exception as e:
                    if "already exists" not in str(e).lower():
                        logger.warning("Schema statement failed", error=str(e))
        await session.commit()
        break


class MemoryConsolidator:
    """
    Nightly 'dreaming' process for memory consolidation.

    Consolidates raw action logs into structured summaries,
    extracts behavioral patterns, and prunes old data.
    """

    def __init__(self):
        self.llm_service = None  # Lazy load

    async def _get_llm(self):
        """Get LLM service for summarization."""
        if self.llm_service is None:
            from cognitex.services.llm import get_llm_service
            self.llm_service = get_llm_service()
        return self.llm_service

    async def run_nightly_consolidation(
        self,
        target_date: datetime | None = None,
        dry_run: bool = False,
    ) -> dict:
        """
        Run the full consolidation process for a given date.

        Args:
            target_date: Date to consolidate (defaults to yesterday)
            dry_run: If True, don't write anything, just report what would happen

        Returns:
            Dict with consolidation results
        """
        await ensure_consolidation_schema()

        if target_date is None:
            target_date = datetime.now() - timedelta(days=1)

        date_str = target_date.strftime("%Y-%m-%d")
        logger.info("Starting memory consolidation", date=date_str, dry_run=dry_run)

        results = {
            "date": date_str,
            "dry_run": dry_run,
            "steps": {},
        }

        # 1. Check if already consolidated
        if await self._already_consolidated(target_date):
            logger.info("Date already consolidated", date=date_str)
            results["status"] = "already_consolidated"
            return results

        # 2. Gather day's raw memories
        memories = await self._gather_days_memories(target_date)
        results["steps"]["gather"] = {
            "action_count": len(memories.get("actions", [])),
            "email_count": len(memories.get("emails", [])),
            "task_count": len(memories.get("tasks", [])),
        }

        if not memories.get("actions"):
            logger.info("No actions to consolidate", date=date_str)
            results["status"] = "no_data"
            return results

        # 3. Generate daily summary using LLM
        summary = await self._generate_daily_summary(memories, target_date)
        results["steps"]["summarize"] = {"summary_length": len(summary.get("text", ""))}

        # 4. Extract behavioral patterns
        patterns = await self._extract_patterns(memories, target_date)
        results["steps"]["patterns"] = {
            "productivity": len(patterns.get("productivity", [])),
            "deferrals": len(patterns.get("deferrals", [])),
            "communication": len(patterns.get("communication", [])),
        }

        # 5. Identify key events worth preserving
        key_events = await self._identify_key_events(memories)
        results["steps"]["key_events"] = {"count": len(key_events)}

        if not dry_run:
            # 6. Store consolidated summary
            await self._store_daily_summary(
                target_date, summary, patterns, key_events, memories
            )

            # 7. Update learning signals
            await self._update_learning_signals(patterns)

            # 8. Create Neo4j DailySummary node
            await self._create_summary_node(target_date, summary, patterns)

            results["status"] = "completed"
            logger.info("Consolidation complete", date=date_str)
        else:
            results["status"] = "dry_run_complete"
            results["would_store"] = {
                "summary": summary,
                "patterns": patterns,
                "key_events": key_events,
            }

        return results

    async def archive_old_memories(
        self,
        days_to_keep: int = 7,
        dry_run: bool = False,
    ) -> dict:
        """
        Archive/prune raw action logs older than specified days.

        Keeps:
        - High-importance events (rejections, errors, key decisions)
        - Events referenced by learning patterns

        Deletes:
        - Routine sync logs ("synced 0 emails")
        - Low-value repeated actions

        Args:
            days_to_keep: Number of days of raw logs to keep
            dry_run: If True, report but don't delete

        Returns:
            Dict with archival results
        """
        from cognitex.db.postgres import get_session

        cutoff_date = datetime.now() - timedelta(days=days_to_keep)

        results = {
            "cutoff_date": cutoff_date.isoformat(),
            "dry_run": dry_run,
        }

        async for session in get_session():
            # Count what would be affected
            count_result = await session.execute(text("""
                SELECT
                    count(*) as total,
                    count(*) FILTER (WHERE action_type IN ('sync', 'check', 'poll')) as low_value,
                    count(*) FILTER (WHERE status = 'error' OR action_type IN ('rejection', 'flag_for_review')) as high_value
                FROM agent_actions
                WHERE timestamp < :cutoff
            """), {"cutoff": cutoff_date})
            counts = count_result.mappings().first()

            results["total_eligible"] = counts["total"] if counts else 0
            results["low_value"] = counts["low_value"] if counts else 0
            results["high_value"] = counts["high_value"] if counts else 0
            results["would_delete"] = results["low_value"]
            results["would_keep"] = results["high_value"]

            if not dry_run and results["would_delete"] > 0:
                # Delete low-value routine logs
                delete_result = await session.execute(text("""
                    DELETE FROM agent_actions
                    WHERE timestamp < :cutoff
                    AND action_type IN ('sync', 'check', 'poll', 'heartbeat')
                    AND status != 'error'
                """), {"cutoff": cutoff_date})

                results["deleted"] = delete_result.rowcount

                # Log the archival
                await session.execute(text("""
                    INSERT INTO memory_archive_log
                    (date_range_start, date_range_end, actions_archived, actions_deleted, summary)
                    VALUES (:start, :end, :archived, :deleted, :summary)
                """), {
                    "start": (cutoff_date - timedelta(days=30)).date(),
                    "end": cutoff_date.date(),
                    "archived": 0,
                    "deleted": results["deleted"],
                    "summary": f"Pruned {results['deleted']} low-value actions",
                })

                await session.commit()
                logger.info("Memory pruning complete", deleted=results["deleted"])

            break

        return results

    async def _already_consolidated(self, date: datetime) -> bool:
        """Check if a date has already been consolidated."""
        from cognitex.db.postgres import get_session

        async for session in get_session():
            result = await session.execute(text("""
                SELECT 1 FROM daily_summaries WHERE date = :date
            """), {"date": date.date()})
            return result.scalar() is not None

    async def _gather_days_memories(self, date: datetime) -> dict:
        """Gather all memories/actions from a specific day."""
        from cognitex.db.postgres import get_session
        from cognitex.db.neo4j import get_neo4j_session

        start_of_day = date.replace(hour=0, minute=0, second=0, microsecond=0)
        end_of_day = start_of_day + timedelta(days=1)

        memories = {
            "actions": [],
            "emails": [],
            "tasks": [],
            "calendar": [],
            "decisions": [],
        }

        # Get actions from PostgreSQL
        async for session in get_session():
            result = await session.execute(text("""
                SELECT id, timestamp, action_type, source, summary, details, status
                FROM agent_actions
                WHERE timestamp >= :start AND timestamp < :end
                ORDER BY timestamp
            """), {"start": start_of_day, "end": end_of_day})

            for row in result.mappings():
                memories["actions"].append(dict(row))
            break

        # Get email activity from Neo4j
        async for session in get_neo4j_session():
            # Emails processed that day
            email_result = await session.run("""
                MATCH (e:Email)
                WHERE e.synced_at >= datetime($start) AND e.synced_at < datetime($end)
                RETURN e.gmail_id as id, e.subject as subject, e.sender_name as sender,
                       e.action_required as action_required, e.urgency as urgency
                LIMIT 100
            """, {"start": start_of_day.isoformat(), "end": end_of_day.isoformat()})
            memories["emails"] = await email_result.data()

            # Tasks completed that day
            task_result = await session.run("""
                MATCH (t:Task)
                WHERE t.completed_at >= datetime($start) AND t.completed_at < datetime($end)
                RETURN t.id as id, t.title as title, t.priority as priority,
                       t.energy_cost as energy_cost
                LIMIT 50
            """, {"start": start_of_day.isoformat(), "end": end_of_day.isoformat()})
            memories["tasks"] = await task_result.data()

            # Calendar events that day
            cal_result = await session.run("""
                MATCH (ev:Event)
                WHERE date(ev.start) = date($date)
                RETURN ev.gcal_id as id, ev.title as title, ev.duration_minutes as duration,
                       ev.energy_impact as energy_impact, ev.event_type as type
                LIMIT 20
            """, {"date": date.strftime("%Y-%m-%d")})
            memories["calendar"] = await cal_result.data()
            break

        return memories

    async def _generate_daily_summary(self, memories: dict, date: datetime) -> dict:
        """Generate a human-readable summary of the day."""
        llm = await self._get_llm()

        # Build context from memories
        action_summary = self._summarize_actions(memories["actions"])
        email_summary = self._summarize_emails(memories["emails"])
        task_summary = self._summarize_tasks(memories["tasks"])
        calendar_summary = self._summarize_calendar(memories["calendar"])

        prompt = f"""Summarize this day's activity for a personal knowledge system.
Be concise but capture key events and patterns.

Date: {date.strftime("%A, %B %d, %Y")}

## Actions ({len(memories['actions'])} total)
{action_summary}

## Emails ({len(memories['emails'])} processed)
{email_summary}

## Tasks ({len(memories['tasks'])} completed)
{task_summary}

## Calendar ({len(memories['calendar'])} events)
{calendar_summary}

Write a 2-3 paragraph summary that:
1. Highlights the most significant activities
2. Notes any patterns (productivity peaks, avoided tasks, etc.)
3. Identifies anything notable for future reference

Keep it factual and useful for future retrieval."""

        try:
            summary_text = await llm.complete(prompt, max_tokens=500)
            return {
                "text": summary_text.strip(),
                "generated_at": datetime.now().isoformat(),
            }
        except Exception as e:
            logger.error("Failed to generate summary", error=str(e))
            return {
                "text": f"Auto-summary: {len(memories['actions'])} actions, "
                        f"{len(memories['emails'])} emails, {len(memories['tasks'])} tasks completed.",
                "error": str(e),
            }

    async def _extract_patterns(self, memories: dict, date: datetime) -> dict:
        """Extract behavioral patterns from the day's data."""
        patterns = {
            "productivity": [],
            "deferrals": [],
            "communication": [],
        }

        actions = memories.get("actions", [])

        # Analyze productivity by hour
        hourly_counts = {}
        for action in actions:
            if action.get("timestamp"):
                hour = action["timestamp"].hour
                hourly_counts[hour] = hourly_counts.get(hour, 0) + 1

        if hourly_counts:
            peak_hour = max(hourly_counts, key=hourly_counts.get)
            patterns["productivity"].append({
                "type": "peak_hour",
                "hour": peak_hour,
                "count": hourly_counts[peak_hour],
                "date": date.strftime("%Y-%m-%d"),
            })

        # Look for deferral patterns
        deferrals = [a for a in actions if a.get("action_type") == "defer_task"]
        if deferrals:
            patterns["deferrals"].append({
                "type": "deferral_count",
                "count": len(deferrals),
                "date": date.strftime("%Y-%m-%d"),
            })

        # Communication patterns from emails
        emails = memories.get("emails", [])
        urgent_emails = [e for e in emails if e.get("urgency") == "high"]
        if urgent_emails:
            patterns["communication"].append({
                "type": "urgent_emails",
                "count": len(urgent_emails),
                "date": date.strftime("%Y-%m-%d"),
            })

        return patterns

    async def _identify_key_events(self, memories: dict) -> list:
        """Identify high-importance events worth preserving."""
        key_events = []

        for action in memories.get("actions", []):
            # Keep rejections and errors
            if action.get("status") == "error":
                key_events.append({
                    "type": "error",
                    "action_type": action.get("action_type"),
                    "summary": action.get("summary"),
                    "timestamp": action.get("timestamp").isoformat() if action.get("timestamp") else None,
                })
            elif action.get("action_type") in ("rejection", "flag_for_review", "approval"):
                key_events.append({
                    "type": action.get("action_type"),
                    "summary": action.get("summary"),
                    "timestamp": action.get("timestamp").isoformat() if action.get("timestamp") else None,
                })

        # Limit to most important
        return key_events[:20]

    async def _store_daily_summary(
        self,
        date: datetime,
        summary: dict,
        patterns: dict,
        key_events: list,
        memories: dict,
    ) -> None:
        """Store the consolidated summary in PostgreSQL."""
        from cognitex.db.postgres import get_session
        import json

        async for session in get_session():
            await session.execute(text("""
                INSERT INTO daily_summaries (
                    id, date, summary,
                    emails_processed, tasks_completed, meetings_attended,
                    productivity_patterns, deferral_patterns, communication_patterns,
                    key_events, raw_action_count
                ) VALUES (
                    :id, :date, :summary,
                    :emails, :tasks, :meetings,
                    :prod_patterns, :def_patterns, :comm_patterns,
                    :key_events, :action_count
                )
            """), {
                "id": f"ds_{date.strftime('%Y%m%d')}",
                "date": date.date(),
                "summary": summary.get("text", ""),
                "emails": len(memories.get("emails", [])),
                "tasks": len(memories.get("tasks", [])),
                "meetings": len(memories.get("calendar", [])),
                "prod_patterns": json.dumps(patterns.get("productivity", [])),
                "def_patterns": json.dumps(patterns.get("deferrals", [])),
                "comm_patterns": json.dumps(patterns.get("communication", [])),
                "key_events": json.dumps(key_events),
                "action_count": len(memories.get("actions", [])),
            })
            await session.commit()
            break

    async def _update_learning_signals(self, patterns: dict) -> None:
        """Update learning system with extracted patterns."""
        # Update temporal energy model with productivity patterns
        from cognitex.agent.state_model import get_temporal_model

        try:
            temporal = get_temporal_model()
            for prod in patterns.get("productivity", []):
                if prod.get("type") == "peak_hour":
                    await temporal.record_productivity_observation(
                        hour=prod["hour"],
                        task_count=prod["count"],
                    )
        except Exception as e:
            logger.warning("Failed to update learning signals", error=str(e))

    async def _create_summary_node(
        self,
        date: datetime,
        summary: dict,
        patterns: dict,
    ) -> None:
        """Create a DailySummary node in Neo4j for graph queries."""
        from cognitex.db.neo4j import get_neo4j_session
        import json

        async for session in get_neo4j_session():
            await session.run("""
                MERGE (ds:DailySummary {date: date($date)})
                SET ds.summary = $summary,
                    ds.patterns = $patterns,
                    ds.created_at = datetime()
            """, {
                "date": date.strftime("%Y-%m-%d"),
                "summary": summary.get("text", ""),
                "patterns": json.dumps(patterns),
            })
            break

    def _summarize_actions(self, actions: list) -> str:
        """Create a brief summary of actions."""
        if not actions:
            return "No actions recorded."

        type_counts = {}
        for a in actions:
            t = a.get("action_type", "unknown")
            type_counts[t] = type_counts.get(t, 0) + 1

        lines = [f"- {t}: {c}" for t, c in sorted(type_counts.items(), key=lambda x: -x[1])[:10]]
        return "\n".join(lines)

    def _summarize_emails(self, emails: list) -> str:
        """Create a brief summary of emails."""
        if not emails:
            return "No emails processed."

        urgent = sum(1 for e in emails if e.get("urgency") == "high")
        action_needed = sum(1 for e in emails if e.get("action_required"))

        return f"- {len(emails)} total, {urgent} urgent, {action_needed} requiring action"

    def _summarize_tasks(self, tasks: list) -> str:
        """Create a brief summary of tasks."""
        if not tasks:
            return "No tasks completed."

        high_pri = sum(1 for t in tasks if t.get("priority") == "high")
        titles = [t.get("title", "Untitled")[:50] for t in tasks[:5]]

        return f"- {len(tasks)} completed ({high_pri} high priority)\n- " + "\n- ".join(titles)

    def _summarize_calendar(self, events: list) -> str:
        """Create a brief summary of calendar events."""
        if not events:
            return "No calendar events."

        total_mins = sum(e.get("duration", 0) or 0 for e in events)
        titles = [e.get("title", "Untitled")[:40] for e in events[:5]]

        return f"- {len(events)} events, {total_mins // 60}h {total_mins % 60}m total\n- " + "\n- ".join(titles)


# Singleton
_consolidator: MemoryConsolidator | None = None


def get_consolidator() -> MemoryConsolidator:
    """Get the memory consolidator singleton."""
    global _consolidator
    if _consolidator is None:
        _consolidator = MemoryConsolidator()
    return _consolidator


async def run_consolidation(
    days_back: int = 1,
    prune: bool = True,
    dry_run: bool = False,
) -> dict:
    """
    Convenience function to run consolidation.

    Args:
        days_back: How many days back to consolidate (1 = yesterday)
        prune: Whether to also prune old logs
        dry_run: If True, don't write anything

    Returns:
        Consolidation results
    """
    consolidator = get_consolidator()

    results = {
        "consolidations": [],
        "pruning": None,
    }

    # Consolidate specified days
    for i in range(days_back):
        target_date = datetime.now() - timedelta(days=i + 1)
        result = await consolidator.run_nightly_consolidation(target_date, dry_run=dry_run)
        results["consolidations"].append(result)

    # Optionally prune old logs
    if prune:
        results["pruning"] = await consolidator.archive_old_memories(
            days_to_keep=7, dry_run=dry_run
        )

    return results
