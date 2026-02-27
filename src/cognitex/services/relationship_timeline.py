"""Relationship Timeline Service.

Builds ambient context for relationships - answers "what happened since
we last met?" and "what's pending between us?"
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import structlog

logger = structlog.get_logger()


@dataclass
class RelationshipEvent:
    """A single event in the relationship timeline."""

    event_type: str  # "email_sent", "email_received", "meeting", "task_created", "task_completed"
    timestamp: datetime
    summary: str
    details: dict = field(default_factory=dict)


@dataclass
class AmbientContext:
    """What happened since we last interacted with this person."""

    person_email: str
    person_name: str | None = None

    # Last interaction
    last_meeting: dict | None = None
    last_meeting_date: datetime | None = None

    # Activity since last meeting
    emails_since: int = 0
    emails_from_them: int = 0
    emails_to_them: int = 0
    email_topics: list[str] = field(default_factory=list)

    # Pending items
    open_tasks_involving: list[dict] = field(default_factory=list)
    pending_requests: list[str] = field(default_factory=list)
    awaiting_their_response: bool = False

    # Narrative summary
    summary: str | None = None


class RelationshipTimelineBuilder:
    """Builds ambient context for relationships.

    Queries the knowledge graph to understand:
    - When we last met with someone
    - What emails have been exchanged since
    - What tasks/items are pending
    - Overall relationship context
    """

    def __init__(self):
        self._llm = None

    @property
    def llm(self):
        """Lazy-load LLM service."""
        if self._llm is None:
            from cognitex.services.llm import get_llm_service
            self._llm = get_llm_service()
        return self._llm

    async def build_ambient_context(
        self,
        person_email: str,
        reference_event_id: str | None = None,
    ) -> AmbientContext:
        """Build ambient context for a relationship.

        Args:
            person_email: Email address of the person
            reference_event_id: Optional event ID to use as reference point

        Returns:
            AmbientContext with relationship summary
        """
        context = AmbientContext(person_email=person_email)

        # Get person info
        person_info = await self._get_person_info(person_email)
        context.person_name = person_info.get("name")

        # Find last meeting
        last_meeting = await self._get_last_meeting(person_email)
        if last_meeting:
            context.last_meeting = last_meeting
            context.last_meeting_date = last_meeting.get("date")

        # Get emails since last meeting (or last 30 days)
        since_date = context.last_meeting_date or (datetime.now() - timedelta(days=30))
        email_stats = await self._get_email_activity(person_email, since_date)
        context.emails_since = email_stats.get("total", 0)
        context.emails_from_them = email_stats.get("from_them", 0)
        context.emails_to_them = email_stats.get("to_them", 0)
        context.email_topics = email_stats.get("topics", [])

        # Get pending items
        pending = await self._get_pending_items(person_email)
        context.open_tasks_involving = pending.get("tasks", [])
        context.pending_requests = pending.get("requests", [])
        context.awaiting_their_response = pending.get("awaiting_response", False)

        # Generate narrative summary
        context.summary = await self._generate_summary(context)

        return context

    async def _get_person_info(self, email: str) -> dict:
        """Get basic person info from the graph."""
        from cognitex.db.neo4j import get_neo4j_session

        async for session in get_neo4j_session():
            try:
                result = await session.run("""
                    MATCH (p:Person {email: $email})
                    RETURN p.name as name, p.org as org, p.title as title
                """, {"email": email.lower()})

                record = await result.single()
                if record:
                    return {
                        "name": record["name"],
                        "org": record["org"],
                        "title": record["title"],
                    }
            except Exception as e:
                logger.warning("Failed to get person info", email=email, error=str(e))
            break

        # Try to extract name from email
        name = email.split("@")[0].replace(".", " ").title()
        return {"name": name}

    async def _get_last_meeting(self, person_email: str) -> dict | None:
        """Find the last meeting with this person."""
        from cognitex.db.neo4j import get_neo4j_session

        async for session in get_neo4j_session():
            try:
                result = await session.run("""
                    MATCH (e:CalendarEvent)-[:ATTENDED_BY]->(p:Person {email: $email})
                    WHERE e.start_time < datetime()
                    RETURN e.gcal_id as id,
                           e.summary as title,
                           e.start_time as start,
                           e.description as description
                    ORDER BY e.start_time DESC
                    LIMIT 1
                """, {"email": person_email.lower()})

                record = await result.single()
                if record:
                    start = record["start"]
                    if hasattr(start, "to_native"):
                        start = start.to_native()

                    return {
                        "id": record["id"],
                        "title": record["title"],
                        "date": start,
                        "description": record["description"],
                    }
            except Exception as e:
                logger.warning("Failed to get last meeting", email=person_email, error=str(e))
            break

        return None

    async def _get_email_activity(
        self,
        person_email: str,
        since: datetime,
    ) -> dict:
        """Get email activity with this person since a date."""
        from cognitex.db.neo4j import get_neo4j_session

        stats = {
            "total": 0,
            "from_them": 0,
            "to_them": 0,
            "topics": [],
        }

        async for session in get_neo4j_session():
            try:
                # Emails from them
                result = await session.run("""
                    MATCH (e:Email)-[:SENT_BY]->(p:Person {email: $email})
                    WHERE e.date > datetime($since)
                    RETURN count(e) as count,
                           collect(DISTINCT e.subject)[..5] as subjects
                """, {"email": person_email.lower(), "since": since.isoformat()})

                record = await result.single()
                if record:
                    stats["from_them"] = record["count"]
                    stats["topics"].extend(record["subjects"] or [])

                # Emails to them
                result = await session.run("""
                    MATCH (e:Email)-[:RECEIVED_BY]->(p:Person {email: $email})
                    WHERE e.date > datetime($since)
                    RETURN count(e) as count,
                           collect(DISTINCT e.subject)[..5] as subjects
                """, {"email": person_email.lower(), "since": since.isoformat()})

                record = await result.single()
                if record:
                    stats["to_them"] = record["count"]
                    # Dedupe topics
                    for subj in (record["subjects"] or []):
                        if subj and subj not in stats["topics"]:
                            stats["topics"].append(subj)

                stats["total"] = stats["from_them"] + stats["to_them"]

            except Exception as e:
                logger.warning("Failed to get email activity", email=person_email, error=str(e))
            break

        return stats

    async def _get_pending_items(self, person_email: str) -> dict:
        """Get pending items involving this person."""
        from cognitex.db.neo4j import get_neo4j_session
        from cognitex.db.postgres import get_session
        from sqlalchemy import text

        pending = {
            "tasks": [],
            "requests": [],
            "awaiting_response": False,
        }

        # Check for tasks involving this person
        async for pg_session in get_session():
            try:
                # Tasks where they're mentioned or assigned
                result = await pg_session.execute(text("""
                    SELECT id, title, status, priority
                    FROM tasks
                    WHERE status NOT IN ('done', 'archived')
                      AND (
                        description ILIKE :email_pattern
                        OR title ILIKE :email_pattern
                      )
                    ORDER BY priority DESC, created_at DESC
                    LIMIT 5
                """), {"email_pattern": f"%{person_email}%"})

                for row in result.fetchall():
                    pending["tasks"].append({
                        "id": row[0],
                        "title": row[1],
                        "status": row[2],
                        "priority": row[3],
                    })
            except Exception as e:
                logger.warning("Failed to get pending tasks", error=str(e))
            break

        # Check for emails awaiting response
        async for session in get_neo4j_session():
            try:
                result = await session.run("""
                    MATCH (e:Email)-[:SENT_BY]->(p:Person {email: $email})
                    WHERE e.needs_response = true
                      AND NOT EXISTS { (reply:Email)-[:REPLY_TO]->(e) }
                    RETURN e.subject as subject, e.date as date
                    ORDER BY e.date DESC
                    LIMIT 3
                """, {"email": person_email.lower()})

                records = await result.data()
                for record in records:
                    pending["requests"].append(record["subject"])
                    pending["awaiting_response"] = True

            except Exception as e:
                logger.warning("Failed to check pending emails", error=str(e))
            break

        return pending

    async def _generate_summary(self, context: AmbientContext) -> str:
        """Generate a narrative summary of the relationship context."""
        parts = []

        # Last meeting
        if context.last_meeting:
            meeting_title = context.last_meeting.get("title", "a meeting")
            if context.last_meeting_date:
                days_ago = (datetime.now() - context.last_meeting_date).days
                if days_ago == 0:
                    time_str = "earlier today"
                elif days_ago == 1:
                    time_str = "yesterday"
                elif days_ago < 7:
                    time_str = f"{days_ago} days ago"
                elif days_ago < 30:
                    weeks = days_ago // 7
                    time_str = f"{weeks} week{'s' if weeks > 1 else ''} ago"
                else:
                    time_str = context.last_meeting_date.strftime("%B %d")

                parts.append(f"Last met {time_str} ({meeting_title})")
        else:
            parts.append("No previous meetings found in calendar")

        # Email activity
        if context.emails_since > 0:
            parts.append(
                f"{context.emails_since} emails exchanged since then "
                f"({context.emails_from_them} from them, {context.emails_to_them} from you)"
            )

            if context.email_topics:
                topics = ", ".join(context.email_topics[:3])
                parts.append(f"Topics: {topics}")

        # Pending items
        if context.open_tasks_involving:
            task_count = len(context.open_tasks_involving)
            parts.append(f"{task_count} open task{'s' if task_count > 1 else ''} involving them")

        if context.awaiting_their_response:
            parts.append("You're awaiting their response on something")

        if context.pending_requests:
            parts.append(f"They're waiting for your response on: {context.pending_requests[0]}")

        return ". ".join(parts) + "." if parts else "No prior relationship history found."


# Singleton instance
_builder: RelationshipTimelineBuilder | None = None


def get_relationship_timeline_builder() -> RelationshipTimelineBuilder:
    """Get or create the relationship timeline builder singleton."""
    global _builder
    if _builder is None:
        _builder = RelationshipTimelineBuilder()
    return _builder
