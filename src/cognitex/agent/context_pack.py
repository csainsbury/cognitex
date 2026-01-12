"""P2.1 & P2.2: Context pack compiler and readiness scoring.

Implements just-in-time context compilation from Phase 3 blueprint:
- Context packs as build artifacts for events/tasks
- Readiness scoring with automatic prep task scheduling
- Two-track day planning (Plan A/B)
- Slack and buffer management
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

import structlog

from cognitex.db.neo4j import get_neo4j_session
from cognitex.db.phase3_schema import (
    create_context_pack,
    get_context_pack,
    update_context_pack,
)
from cognitex.agent.state_model import get_state_estimator, OperatingMode

logger = structlog.get_logger()


class BuildStage(str, Enum):
    """Context pack build stages."""
    T_24H = "T-24h"   # Day before
    T_2H = "T-2h"     # 2 hours before
    T_15M = "T-15m"   # 15 minutes before
    T_5M = "T-5m"     # 5 minutes before (whisper mode)
    LIVE = "live"     # During the event/task
    POST = "post"     # After completion


@dataclass
class ContextPackContent:
    """Content for a context pack."""

    # Identity
    pack_id: str
    event_id: str | None = None
    task_id: str | None = None

    # One-line purpose
    objective: str | None = None

    # Historical context
    last_touch_recap: str | None = None
    last_interaction_date: datetime | None = None

    # Required materials
    artifact_links: list[dict] = field(default_factory=list)  # {title, url, relevance_score}
    required_documents: list[str] = field(default_factory=list)

    # Decision support
    decision_list: list[str] = field(default_factory=list)
    dont_forget: list[str] = field(default_factory=list)

    # Pre-drafted content
    pre_drafted_agenda: str | None = None
    pre_drafted_emails: list[dict] = field(default_factory=list)  # {to, subject, body}
    pre_drafted_messages: list[str] = field(default_factory=list)

    # Follow-up preparation
    post_event_tasks: list[str] = field(default_factory=list)

    # Readiness
    readiness_score: float = 0.0
    missing_prerequisites: list[str] = field(default_factory=list)
    prep_tasks_needed: list[dict] = field(default_factory=list)  # {task, minutes, priority}

    # Meta
    build_stage: BuildStage = BuildStage.T_24H
    built_at: datetime = field(default_factory=datetime.now)
    expires_at: datetime | None = None


@dataclass
class PrepTask:
    """A preparation task to improve readiness."""

    title: str
    description: str | None = None
    estimated_minutes: int = 10
    priority: str = "medium"  # high, medium, low
    for_event_id: str | None = None
    for_task_id: str | None = None
    due_before: datetime | None = None
    completed: bool = False


class ReadinessScorer:
    """Calculates readiness scores for events and tasks.

    Readiness factors:
    - Pre-reads reviewed
    - Agenda prepared
    - Decision points identified
    - Required materials available
    - Drafts queued
    - Mental preparation done
    """

    # Weights for different readiness factors
    FACTOR_WEIGHTS = {
        "agenda_prepared": 0.20,
        "pre_reads_done": 0.25,
        "decisions_identified": 0.15,
        "materials_available": 0.15,
        "drafts_ready": 0.10,
        "participants_known": 0.10,
        "objectives_clear": 0.05,
    }

    # Minimum readiness thresholds by event type
    MIN_THRESHOLDS = {
        "meeting": 0.6,
        "presentation": 0.8,
        "interview": 0.8,
        "review": 0.7,
        "default": 0.5,
    }

    def calculate_score(
        self,
        factors: dict[str, float],
    ) -> tuple[float, list[str]]:
        """Calculate readiness score from factor values.

        Args:
            factors: Dict of factor_name -> completion (0-1)

        Returns:
            (score, list of missing items)
        """
        total_weight = 0.0
        weighted_sum = 0.0
        missing = []

        for factor, weight in self.FACTOR_WEIGHTS.items():
            value = factors.get(factor, 0.0)
            weighted_sum += weight * value
            total_weight += weight

            if value < 0.5:
                missing.append(f"{factor.replace('_', ' ').title()} ({int(value*100)}%)")

        score = weighted_sum / total_weight if total_weight > 0 else 0.0
        return round(score, 2), missing

    def assess_event_readiness(
        self,
        event: dict,
        pack: ContextPackContent | None = None,
    ) -> tuple[float, list[str], list[PrepTask]]:
        """Assess readiness for a calendar event.

        Returns:
            (score, missing_items, suggested_prep_tasks)
        """
        factors = {}
        prep_tasks = []

        # Check agenda
        if pack and pack.pre_drafted_agenda:
            factors["agenda_prepared"] = 1.0
        else:
            factors["agenda_prepared"] = 0.0
            prep_tasks.append(PrepTask(
                title=f"Draft agenda for: {event.get('summary', 'event')}",
                estimated_minutes=10,
                priority="high",
            ))

        # Check pre-reads
        if pack and pack.artifact_links:
            reviewed = sum(1 for a in pack.artifact_links if a.get("reviewed"))
            total = len(pack.artifact_links)
            factors["pre_reads_done"] = reviewed / total if total > 0 else 1.0
            if reviewed < total:
                prep_tasks.append(PrepTask(
                    title=f"Review {total - reviewed} documents",
                    estimated_minutes=15 * (total - reviewed),
                    priority="medium",
                ))
        else:
            factors["pre_reads_done"] = 0.5  # Unknown

        # Check decision points
        if pack and pack.decision_list:
            factors["decisions_identified"] = 1.0
        else:
            factors["decisions_identified"] = 0.0
            prep_tasks.append(PrepTask(
                title="Identify decision points for meeting",
                estimated_minutes=5,
                priority="medium",
            ))

        # Check materials
        if pack and pack.required_documents:
            factors["materials_available"] = 1.0
        else:
            factors["materials_available"] = 0.5

        # Check drafts
        if pack and (pack.pre_drafted_emails or pack.pre_drafted_messages):
            factors["drafts_ready"] = 1.0
        else:
            factors["drafts_ready"] = 0.0

        # Check participants
        attendees = event.get("attendees", [])
        if attendees:
            factors["participants_known"] = 1.0
        else:
            factors["participants_known"] = 0.5

        # Check objectives
        if pack and pack.objective:
            factors["objectives_clear"] = 1.0
        else:
            factors["objectives_clear"] = 0.0
            prep_tasks.append(PrepTask(
                title="Define objective for meeting",
                estimated_minutes=5,
                priority="high",
            ))

        score, missing = self.calculate_score(factors)
        return score, missing, prep_tasks

    def get_threshold(self, event_type: str) -> float:
        """Get minimum readiness threshold for event type."""
        return self.MIN_THRESHOLDS.get(event_type, self.MIN_THRESHOLDS["default"])


class ContextPackCompiler:
    """Compiles context packs for upcoming events and tasks.

    Build schedule:
    - T-24h: Initial build with historical context
    - T-2h: Refresh with any new emails/docs
    - T-15m: Final prep check
    - Post: Follow-up task generation
    """

    def __init__(self):
        self.readiness = ReadinessScorer()
        self._scheduled_builds: list[dict] = []

    async def compile_for_event(
        self,
        event: dict,
        stage: BuildStage = BuildStage.T_24H,
    ) -> ContextPackContent:
        """Compile a context pack for a calendar event.

        Args:
            event: Calendar event dict
            stage: Build stage (T-24h, T-2h, T-15m)

        Returns:
            ContextPackContent
        """
        pack_id = f"pack_{uuid.uuid4().hex[:12]}"
        event_id = event.get("gcal_id") or event.get("id")

        # Extract event details
        summary = event.get("summary", "Untitled Event")
        description = event.get("description", "")
        attendees = event.get("attendees", [])

        # Build objective
        objective = self._extract_objective(summary, description)

        # Get historical context
        last_touch = await self._get_last_interaction(attendees)

        # Find related artifacts (pass attendee emails for better context)
        attendee_emails = [a.get("email") for a in attendees if a.get("email")]
        artifacts = await self._find_related_artifacts(summary, description, attendee_emails)

        # Identify decisions
        decisions = self._extract_decisions(description)

        # Generate don't forget items
        dont_forget = self._generate_reminders(event, attendees)

        # Draft agenda if meeting
        agenda = None
        if "meeting" in summary.lower() or attendees:
            agenda = self._draft_agenda(summary, description, attendees)

        # Create pack
        pack = ContextPackContent(
            pack_id=pack_id,
            event_id=event_id,
            objective=objective,
            last_touch_recap=last_touch,
            artifact_links=artifacts,
            decision_list=decisions,
            dont_forget=dont_forget,
            pre_drafted_agenda=agenda,
            build_stage=stage,
        )

        # Calculate readiness
        score, missing, prep_tasks = self.readiness.assess_event_readiness(event, pack)
        pack.readiness_score = score
        pack.missing_prerequisites = missing
        pack.prep_tasks_needed = [
            {"task": t.title, "minutes": t.estimated_minutes, "priority": t.priority}
            for t in prep_tasks
        ]

        # Store in graph
        await self._store_pack(pack)

        logger.info(
            "Compiled context pack",
            pack_id=pack_id,
            event_name=summary[:30],
            stage=stage.value,
            readiness=score,
        )
        return pack

    async def compile_for_task(
        self,
        task: dict,
        stage: BuildStage = BuildStage.T_24H,
    ) -> ContextPackContent:
        """Compile a context pack for a task.

        Args:
            task: Task dict
            stage: Build stage

        Returns:
            ContextPackContent
        """
        pack_id = f"pack_{uuid.uuid4().hex[:12]}"
        task_id = task.get("id")

        # Extract task details
        title = task.get("title", "Untitled Task")
        description = task.get("description", "")

        # Build objective
        objective = f"Complete: {title}"

        # Find related artifacts
        artifacts = await self._find_related_artifacts(title, description)

        # Get blocking info
        blockers = task.get("blocked_by", [])
        if blockers:
            pack = ContextPackContent(
                pack_id=pack_id,
                task_id=task_id,
                objective=objective,
                artifact_links=artifacts,
                missing_prerequisites=[f"Blocked by: {b}" for b in blockers],
                readiness_score=0.0,
                build_stage=stage,
            )
        else:
            pack = ContextPackContent(
                pack_id=pack_id,
                task_id=task_id,
                objective=objective,
                artifact_links=artifacts,
                build_stage=stage,
            )

            # Calculate readiness for task
            pack.readiness_score = 0.7 if artifacts else 0.5

        # Store in graph
        await self._store_pack(pack)

        logger.info(
            "Compiled context pack for task",
            pack_id=pack_id,
            task=title[:30],
            stage=stage.value,
        )
        return pack

    async def compile_research_pack(
        self,
        email: dict,
        research_topics: list[str],
    ) -> ContextPackContent:
        """Compile a research briefing pack for an email.

        Gathers background context on specified topics before the user
        needs to respond. Uses semantic search and graph traversal to
        find related documents, people, and prior communications.

        Args:
            email: Email dict with gmail_id, subject, snippet, etc.
            research_topics: List of topics to research (from classification)

        Returns:
            ContextPackContent with research briefing
        """
        from cognitex.services.llm import get_llm_service

        pack_id = f"pack_{uuid.uuid4().hex[:12]}"
        gmail_id = email.get("gmail_id")
        subject = email.get("subject", "")

        llm = get_llm_service()
        topic_briefings = []
        all_artifacts = []
        key_facts = []
        related_people = []

        for topic in research_topics[:5]:  # Max 5 topics
            topic_brief = {
                "topic": topic,
                "summary": "",
                "documents": [],
                "entities": [],
            }

            # Semantic search for related documents
            try:
                from cognitex.db.postgres import get_session as get_postgres_session
                from cognitex.services.ingestion import search_chunks_semantic

                async for pg_session in get_postgres_session():
                    chunks = await search_chunks_semantic(pg_session, topic, limit=5)

                    for chunk in chunks:
                        doc_name = await self._get_document_name(chunk.get("drive_id"))
                        if doc_name and chunk.get("similarity", 0) > 0.5:
                            topic_brief["documents"].append({
                                "name": doc_name,
                                "drive_id": chunk.get("drive_id"),
                                "snippet": chunk.get("content", "")[:200],
                                "relevance": chunk.get("similarity", 0),
                            })
                            all_artifacts.append({
                                "title": f"{doc_name} (re: {topic})",
                                "url": f"https://drive.google.com/file/d/{chunk.get('drive_id')}/view",
                                "drive_id": chunk.get("drive_id"),
                                "relevance_score": chunk.get("similarity", 0.5),
                                "snippet": chunk.get("content", "")[:150] + "...",
                                "reviewed": False,
                            })
                    break

            except Exception as e:
                logger.warning("Failed to search docs for topic", topic=topic, error=str(e))

            # Graph search for related entities (people, projects)
            try:
                async for session in get_neo4j_session():
                    # Search for people/projects mentioned in relation to topic
                    entity_query = """
                    MATCH (c:Chunk)
                    WHERE c.content CONTAINS $topic OR c.summary CONTAINS $topic
                    OPTIONAL MATCH (c)-[:MENTIONS]->(p:Person)
                    OPTIONAL MATCH (c)-[:RELATES_TO]->(proj:Project)
                    WITH p, proj
                    WHERE p IS NOT NULL OR proj IS NOT NULL
                    RETURN
                        COLLECT(DISTINCT {type: 'person', name: p.name, email: p.email}) as people,
                        COLLECT(DISTINCT {type: 'project', title: proj.title, id: proj.id}) as projects
                    LIMIT 1
                    """
                    result = await session.run(entity_query, {"topic": topic})
                    record = await result.single()

                    if record:
                        people = record.get("people", [])
                        projects = record.get("projects", [])

                        for person in people[:3]:
                            if person.get("name"):
                                topic_brief["entities"].append(person)
                                if person not in related_people:
                                    related_people.append(person)

                        for proj in projects[:2]:
                            if proj.get("title"):
                                topic_brief["entities"].append(proj)

            except Exception as e:
                logger.warning("Failed to search entities for topic", topic=topic, error=str(e))

            # Generate topic summary using LLM if we have documents
            if topic_brief["documents"]:
                doc_context = "\n".join([
                    f"- {d['name']}: {d['snippet'][:150]}..."
                    for d in topic_brief["documents"][:3]
                ])
                entity_context = ", ".join([
                    e.get("name") or e.get("title", "")
                    for e in topic_brief["entities"][:5]
                ])

                try:
                    summary_prompt = f"""Summarize the key information about "{topic}" based on these sources:

{doc_context}

Related people/projects: {entity_context or 'None identified'}

Provide a 2-3 sentence briefing that would help someone respond to an email about this topic.
Focus on facts, recent developments, and key relationships."""

                    topic_brief["summary"] = await llm.complete(
                        summary_prompt,
                        model=llm.fast_model,
                        max_tokens=200,
                        temperature=0.3,
                    )

                    # Extract key facts
                    key_facts.append(f"**{topic}**: {topic_brief['summary'][:200]}")

                except Exception as e:
                    logger.warning("Failed to generate topic summary", topic=topic, error=str(e))
                    topic_brief["summary"] = f"Found {len(topic_brief['documents'])} related documents."

            topic_briefings.append(topic_brief)

        # Build the context pack
        objective = f"Research background for: {subject[:60]}"

        # Format last touch as research summary
        research_recap = self._format_research_recap(topic_briefings)

        # Generate decision support
        decisions = []
        if related_people:
            decisions.append(f"Consider cc'ing: {', '.join(p.get('name', p.get('email', '')) for p in related_people[:3])}")

        pack = ContextPackContent(
            pack_id=pack_id,
            task_id=f"email_{gmail_id}",
            objective=objective,
            last_touch_recap=research_recap,
            artifact_links=all_artifacts[:8],
            decision_list=decisions,
            dont_forget=key_facts[:5],
            build_stage=BuildStage.T_24H,
            readiness_score=0.8 if topic_briefings else 0.4,
        )

        # Store pack
        await self._store_pack(pack)

        # Link to email in graph
        try:
            async for session in get_neo4j_session():
                link_query = """
                MATCH (e:Email {gmail_id: $gmail_id})
                MATCH (p:ContextPack {id: $pack_id})
                SET p.pack_type = 'research'
                MERGE (e)-[:HAS_PACK]->(p)
                """
                await session.run(link_query, {
                    "gmail_id": gmail_id,
                    "pack_id": pack.pack_id,
                })
        except Exception as e:
            logger.warning("Failed to link research pack to email", error=str(e))

        logger.info(
            "Compiled research pack",
            pack_id=pack_id,
            email_subject=subject[:30],
            topics=len(research_topics),
            docs_found=len(all_artifacts),
        )
        return pack

    def _format_research_recap(self, topic_briefings: list[dict]) -> str:
        """Format research briefings into readable recap."""
        if not topic_briefings:
            return "No research topics identified."

        lines = ["## Research Briefing\n"]
        for brief in topic_briefings:
            topic = brief.get("topic", "Unknown")
            summary = brief.get("summary", "No information found.")
            docs = brief.get("documents", [])
            entities = brief.get("entities", [])

            lines.append(f"### {topic}\n")
            lines.append(f"{summary}\n")

            if docs:
                lines.append(f"\n_Sources: {len(docs)} documents_")

            if entities:
                entity_names = [e.get("name") or e.get("title") for e in entities if e.get("name") or e.get("title")]
                if entity_names:
                    lines.append(f"\n_Related: {', '.join(entity_names[:4])}_")

            lines.append("")

        return "\n".join(lines)

    async def refresh_pack(
        self,
        pack_id: str,
        new_stage: BuildStage,
    ) -> ContextPackContent | None:
        """Refresh an existing pack with new information.

        Called at T-2h and T-15m to incorporate new emails/docs.
        """
        async for session in get_neo4j_session():
            existing = await get_context_pack(session, pack_id=pack_id)
            if not existing:
                return None

            # Check for new relevant items since last build
            # (In full implementation, would query recent emails/docs)

            # Update stage
            await update_context_pack(
                session,
                pack_id=pack_id,
                build_stage=new_stage.value,
            )

            logger.info("Refreshed context pack", pack_id=pack_id, stage=new_stage.value)

            # Return updated pack
            updated = await get_context_pack(session, pack_id=pack_id)
            if updated:
                return self._dict_to_pack(updated)
            return None

    def _extract_objective(self, summary: str, description: str) -> str:
        """Extract or generate a one-line objective."""
        # Check description for explicit objective
        desc_lower = description.lower()
        if "objective:" in desc_lower:
            start = desc_lower.find("objective:") + 10
            end = description.find("\n", start)
            if end == -1:
                end = len(description)
            return description[start:end].strip()

        if "goal:" in desc_lower:
            start = desc_lower.find("goal:") + 5
            end = description.find("\n", start)
            if end == -1:
                end = len(description)
            return description[start:end].strip()

        # Generate from summary
        return f"Complete: {summary}"

    async def _get_last_interaction(
        self,
        attendees: list[dict],
    ) -> str | None:
        """Get recap of last interaction with attendees.

        Queries the graph for recent emails, meetings, and pending items
        with the attendees.
        """
        if not attendees:
            return None

        emails = [a.get("email") for a in attendees if a.get("email")]
        if not emails:
            return None

        recap_parts = []

        try:
            async for session in get_neo4j_session():
                for email_addr in emails[:3]:  # Check top 3 attendees
                    # Get recent emails with this person
                    email_query = """
                    MATCH (p:Person {email: $email})
                    MATCH (e:Email)-[:SENT_BY|RECEIVED_BY]-(p)
                    RETURN e.subject as subject, e.date as date, e.snippet as snippet
                    ORDER BY e.date DESC
                    LIMIT 3
                    """
                    result = await session.run(email_query, {"email": email_addr})
                    records = await result.data()

                    if records:
                        person_name = email_addr.split("@")[0]
                        last_email = records[0]
                        recap_parts.append(
                            f"• {person_name}: Last email '{last_email.get('subject', 'No subject')[:40]}...' "
                            f"({last_email.get('date', 'unknown date')[:10]})"
                        )

                        # Check for pending items
                        if len(records) > 1:
                            recap_parts.append(f"  ({len(records)} recent threads)")

                    # Check for shared tasks
                    task_query = """
                    MATCH (p:Person {email: $email})-[:ASSIGNED_TO|MENTIONED_IN]-(t:Task)
                    WHERE t.status IN ['pending', 'in_progress']
                    RETURN t.title as title, t.status as status
                    LIMIT 2
                    """
                    task_result = await session.run(task_query, {"email": email_addr})
                    task_records = await task_result.data()

                    if task_records:
                        for task in task_records:
                            recap_parts.append(f"  → Shared task: {task.get('title', 'Untitled')[:35]}...")

        except Exception as e:
            logger.warning("Failed to fetch last interactions", error=str(e))
            # Fallback to simple message
            return f"Check recent emails with: {', '.join(emails[:3])}"

        if recap_parts:
            return "\n".join(recap_parts)
        return f"No recent interactions found with: {', '.join(emails[:3])}"

    async def _find_related_artifacts(
        self,
        title: str,
        description: str,
        attendee_emails: list[str] | None = None,
    ) -> list[dict]:
        """Find documents/code related to the event/task.

        Uses semantic search on document chunks and checks for
        documents shared with attendees.
        """
        artifacts = []
        search_query = f"{title} {description[:200]}" if description else title

        try:
            from cognitex.db.postgres import get_session as get_postgres_session
            from cognitex.services.ingestion import search_chunks_semantic

            # Semantic search on document chunks
            async for pg_session in get_postgres_session():
                chunks = await search_chunks_semantic(pg_session, search_query, limit=5)

                for chunk in chunks:
                    # Get document name from Neo4j
                    doc_name = await self._get_document_name(chunk.get("drive_id"))
                    artifacts.append({
                        "title": doc_name or f"Document chunk ({chunk.get('chunk_index', 0)})",
                        "url": f"https://drive.google.com/file/d/{chunk.get('drive_id')}/view",
                        "drive_id": chunk.get("drive_id"),
                        "relevance_score": chunk.get("similarity", 0.5),
                        "snippet": chunk.get("content", "")[:150] + "...",
                        "reviewed": False,
                    })

            # Also check for documents mentioning attendees
            if attendee_emails:
                async for session in get_neo4j_session():
                    for email_addr in attendee_emails[:2]:
                        doc_query = """
                        MATCH (p:Person {email: $email})<-[:MENTIONS]-(c:Chunk)<-[:HAS_CHUNK]-(d:Document)
                        RETURN DISTINCT d.name as name, d.drive_id as drive_id, c.summary as summary
                        LIMIT 3
                        """
                        result = await session.run(doc_query, {"email": email_addr})
                        records = await result.data()

                        for doc in records:
                            # Avoid duplicates
                            if not any(a.get("drive_id") == doc.get("drive_id") for a in artifacts):
                                artifacts.append({
                                    "title": f"{doc.get('name', 'Document')} (mentions {email_addr.split('@')[0]})",
                                    "url": f"https://drive.google.com/file/d/{doc.get('drive_id')}/view",
                                    "drive_id": doc.get("drive_id"),
                                    "relevance_score": 0.6,
                                    "snippet": doc.get("summary", "")[:100],
                                    "reviewed": False,
                                })

        except Exception as e:
            logger.warning("Failed to search artifacts", error=str(e))
            # Fallback to keyword suggestion
            keywords = title.split()[:3]
            if keywords:
                artifacts.append({
                    "title": f"Search for: {' '.join(keywords)}",
                    "url": None,
                    "relevance_score": 0.3,
                    "reviewed": False,
                })

        # Sort by relevance
        artifacts.sort(key=lambda x: x.get("relevance_score", 0), reverse=True)
        return artifacts[:8]  # Max 8 artifacts

    async def _get_document_name(self, drive_id: str) -> str | None:
        """Get document name from graph."""
        try:
            async for session in get_neo4j_session():
                query = "MATCH (d:Document {drive_id: $drive_id}) RETURN d.name as name"
                result = await session.run(query, {"drive_id": drive_id})
                record = await result.single()
                if record:
                    return record.get("name")
        except Exception:
            pass
        return None

    def _extract_decisions(self, description: str) -> list[str]:
        """Extract decision points from description."""
        decisions = []
        desc_lower = description.lower()

        # Look for decision markers
        markers = ["decide:", "decision:", "choose:", "determine:", "?"]
        lines = description.split("\n")

        for line in lines:
            line_lower = line.lower().strip()
            if any(m in line_lower for m in markers):
                decisions.append(line.strip())

        return decisions[:5]  # Max 5 decisions

    def _generate_reminders(
        self,
        event: dict,
        attendees: list[dict],
    ) -> list[str]:
        """Generate don't-forget reminders for an event."""
        reminders = []

        # Check for recurring meeting patterns
        summary = event.get("summary", "").lower()

        if "1:1" in summary or "one on one" in summary:
            reminders.append("Prepare status update")
            reminders.append("Note any blockers to discuss")

        if "standup" in summary or "stand-up" in summary:
            reminders.append("Prepare: What I did, what I'll do, blockers")

        if "review" in summary:
            reminders.append("Have materials ready for review")

        # Add attendee-specific reminders
        if attendees:
            vip_domains = ["ceo", "cto", "director", "vp"]
            for att in attendees:
                email = att.get("email", "").lower()
                if any(v in email for v in vip_domains):
                    reminders.append("Senior stakeholder attending - prepare executive summary")
                    break

        return reminders

    def _draft_agenda(
        self,
        summary: str,
        description: str,
        attendees: list[dict],
    ) -> str:
        """Draft a meeting agenda."""
        lines = [
            f"# {summary}",
            "",
            "## Attendees",
        ]

        for att in attendees[:5]:
            name = att.get("displayName") or att.get("email", "Unknown")
            lines.append(f"- {name}")

        lines.extend([
            "",
            "## Agenda",
            "1. [Opening/context setting]",
            "2. [Main discussion points]",
            "3. [Decisions needed]",
            "4. [Action items and owners]",
            "5. [Next steps]",
            "",
            "## Notes",
            "[To be filled during meeting]",
        ])

        return "\n".join(lines)

    async def _store_pack(self, pack: ContextPackContent) -> None:
        """Store context pack in the graph."""
        async for session in get_neo4j_session():
            await create_context_pack(
                session,
                pack_id=pack.pack_id,
                event_id=pack.event_id,
                task_id=pack.task_id,
                objective=pack.objective,
                last_touch_recap=pack.last_touch_recap,
                decision_list=pack.decision_list,
                dont_forget=pack.dont_forget,
                readiness_score=pack.readiness_score,
                missing_prerequisites=pack.missing_prerequisites,
                artifact_links=[a.get("title") for a in pack.artifact_links],
                build_stage=pack.build_stage.value,
            )

    def _dict_to_pack(self, data: dict) -> ContextPackContent:
        """Convert graph data to ContextPackContent."""
        return ContextPackContent(
            pack_id=data.get("id", ""),
            event_id=data.get("event_id"),
            task_id=data.get("task_id"),
            objective=data.get("objective"),
            last_touch_recap=data.get("last_touch_recap"),
            decision_list=data.get("decision_list", []),
            dont_forget=data.get("dont_forget", []),
            readiness_score=data.get("readiness_score", 0.0),
            missing_prerequisites=data.get("missing_prerequisites", []),
            build_stage=BuildStage(data.get("build_stage", "T-24h")),
        )

    async def schedule_builds(
        self,
        events: list[dict],
        tasks: list[dict] | None = None,
    ) -> list[dict]:
        """Schedule context pack builds for upcoming items.

        Returns list of scheduled builds with timestamps.
        """
        scheduled = []
        now = datetime.now()

        for event in events:
            start_str = event.get("start")
            if not start_str:
                continue

            try:
                start = datetime.fromisoformat(start_str.replace("Z", "+00:00").replace("+00:00", ""))
            except (ValueError, TypeError):
                continue

            event_id = event.get("gcal_id") or event.get("id")

            # Schedule T-24h if applicable
            t_24h = start - timedelta(hours=24)
            if t_24h > now:
                scheduled.append({
                    "event_id": event_id,
                    "stage": BuildStage.T_24H,
                    "build_at": t_24h.isoformat(),
                })

            # Schedule T-2h
            t_2h = start - timedelta(hours=2)
            if t_2h > now:
                scheduled.append({
                    "event_id": event_id,
                    "stage": BuildStage.T_2H,
                    "build_at": t_2h.isoformat(),
                })

            # Schedule T-15m
            t_15m = start - timedelta(minutes=15)
            if t_15m > now:
                scheduled.append({
                    "event_id": event_id,
                    "stage": BuildStage.T_15M,
                    "build_at": t_15m.isoformat(),
                })

        self._scheduled_builds = scheduled
        logger.info("Scheduled context pack builds", count=len(scheduled))
        return scheduled


class TwoTrackPlanner:
    """Implements two-track day planning: Plan A (normal) and Plan B (minimum viable).

    Plan A: Normal capacity day
    Plan B: Minimum viable day that protects critical commitments

    Automatic switching when overload signals rise.
    """

    @dataclass
    class DayPlan:
        """A day plan with scheduled items."""
        date: datetime
        plan_type: str  # "A" or "B"
        items: list[dict] = field(default_factory=list)
        total_minutes: int = 0
        capacity_used: float = 0.0
        protected_items: list[str] = field(default_factory=list)  # IDs of critical items

    def __init__(self):
        self.state_estimator = get_state_estimator()
        self._current_plan: TwoTrackPlanner.DayPlan | None = None
        self._plan_b: TwoTrackPlanner.DayPlan | None = None

    async def create_day_plans(
        self,
        events: list[dict],
        tasks: list[dict],
        date: datetime | None = None,
    ) -> tuple[DayPlan, DayPlan]:
        """Create Plan A (normal) and Plan B (minimum viable) for a day.

        Returns:
            (plan_a, plan_b)
        """
        if date is None:
            date = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

        # Filter to relevant date
        day_events = [e for e in events if self._is_on_date(e, date)]
        day_tasks = [t for t in tasks if self._task_for_date(t, date)]

        # Calculate available capacity (assume 8-hour workday minus meetings)
        meeting_minutes = sum(self._event_duration(e) for e in day_events)
        available_minutes = 8 * 60 - meeting_minutes

        # Identify critical items (hard deadlines, commitments)
        critical_events = [e for e in day_events if self._is_critical(e)]
        critical_tasks = [t for t in day_tasks if t.get("priority") in ["critical", "high"]]

        # Plan A: Fill with normal capacity
        plan_a = self.DayPlan(date=date, plan_type="A")
        plan_a.items = day_events.copy()

        remaining_a = available_minutes
        for task in day_tasks:
            est = task.get("effort_estimate", 30)
            if est <= remaining_a:
                plan_a.items.append(task)
                remaining_a -= est
                plan_a.total_minutes += est

        plan_a.capacity_used = (8 * 60 - remaining_a) / (8 * 60)
        plan_a.protected_items = [e.get("id") for e in critical_events]

        # Plan B: Only critical commitments + one key task
        plan_b = self.DayPlan(date=date, plan_type="B")
        plan_b.items = critical_events.copy()

        if critical_tasks:
            plan_b.items.append(critical_tasks[0])  # Only the most critical task

        plan_b.total_minutes = sum(
            self._event_duration(e) if "start" in e else e.get("effort_estimate", 30)
            for e in plan_b.items
        )
        plan_b.capacity_used = plan_b.total_minutes / (8 * 60)
        plan_b.protected_items = [e.get("id") for e in critical_events]

        self._current_plan = plan_a
        self._plan_b = plan_b

        logger.info(
            "Created day plans",
            date=date.isoformat(),
            plan_a_items=len(plan_a.items),
            plan_b_items=len(plan_b.items),
            plan_a_capacity=f"{plan_a.capacity_used:.0%}",
            plan_b_capacity=f"{plan_b.capacity_used:.0%}",
        )

        return plan_a, plan_b

    async def should_switch_to_plan_b(self) -> tuple[bool, str | None]:
        """Check if we should switch from Plan A to Plan B.

        Triggers:
        - Overload state detected
        - Multiple disruptions
        - Energy/fatigue signals
        """
        state = await self.state_estimator.get_current_state()

        if state.mode == OperatingMode.OVERLOADED:
            return True, "Overloaded state detected"

        if state.signals.fatigue_level > 0.8:
            return True, "High fatigue level"

        if state.signals.interruption_pressure > 0.8:
            return True, "High interruption pressure"

        return False, None

    async def switch_to_plan_b(self) -> DayPlan | None:
        """Switch to Plan B, notifying about deferred items."""
        if not self._plan_b:
            return None

        # Calculate what's being deferred
        if self._current_plan:
            deferred = [
                item for item in self._current_plan.items
                if item.get("id") not in self._plan_b.protected_items
            ]
            if deferred:
                logger.info(
                    "Switching to Plan B",
                    deferred_count=len(deferred),
                    protected_count=len(self._plan_b.items),
                )

        self._current_plan = self._plan_b
        return self._plan_b

    def _is_on_date(self, event: dict, date: datetime) -> bool:
        """Check if event is on the given date."""
        start = event.get("start")
        if not start:
            return False

        # Handle Google Calendar API format where start is a dict
        if isinstance(start, dict):
            start_str = start.get("dateTime") or start.get("date")
        else:
            start_str = start

        if not start_str:
            return False

        try:
            # Handle various ISO formats
            start_str = str(start_str).replace("Z", "+00:00")
            if "+00:00" in start_str:
                start_str = start_str.replace("+00:00", "")
            start_dt = datetime.fromisoformat(start_str)
            return start_dt.date() == date.date()
        except (ValueError, TypeError):
            return False

    def _task_for_date(self, task: dict, date: datetime) -> bool:
        """Check if task is scheduled or due on the given date."""
        due_str = task.get("due") or task.get("due_date")
        if due_str:
            try:
                due = datetime.fromisoformat(due_str.replace("Z", "+00:00").replace("+00:00", ""))
                return due.date() == date.date()
            except (ValueError, TypeError):
                pass
        # Include pending tasks without specific dates
        return task.get("status") in ["pending", "in_progress"]

    def _event_duration(self, event: dict) -> int:
        """Get event duration in minutes."""
        start = event.get("start")
        end = event.get("end")
        if not start or not end:
            return 30  # Default

        # Handle Google Calendar API format where start/end are dicts
        if isinstance(start, dict):
            start_str = start.get("dateTime") or start.get("date")
        else:
            start_str = start

        if isinstance(end, dict):
            end_str = end.get("dateTime") or end.get("date")
        else:
            end_str = end

        if not start_str or not end_str:
            return 30

        try:
            start_str = str(start_str).replace("Z", "").replace("+00:00", "")
            end_str = str(end_str).replace("Z", "").replace("+00:00", "")
            start_dt = datetime.fromisoformat(start_str)
            end_dt = datetime.fromisoformat(end_str)
            return int((end_dt - start_dt).total_seconds() / 60)
        except (ValueError, TypeError):
            return 30

    def _is_critical(self, event: dict) -> bool:
        """Determine if an event is critical and must be protected."""
        summary = event.get("summary", "").lower()

        # Check for critical markers
        critical_markers = [
            "deadline",
            "presentation",
            "interview",
            "client",
            "board",
            "urgent",
            "critical",
        ]

        return any(m in summary for m in critical_markers)


# Singleton instances
_compiler: ContextPackCompiler | None = None
_planner: TwoTrackPlanner | None = None


def get_context_pack_compiler() -> ContextPackCompiler:
    """Get the context pack compiler singleton."""
    global _compiler
    if _compiler is None:
        _compiler = ContextPackCompiler()
    return _compiler


def get_two_track_planner() -> TwoTrackPlanner:
    """Get the two-track planner singleton."""
    global _planner
    if _planner is None:
        _planner = TwoTrackPlanner()
    return _planner


# ============================================================================
# Context Pack Trigger System
# ============================================================================


class ContextPackTriggerSystem:
    """Manages context pack builds triggered by calendar events and emails.

    Triggers:
    - T-24h, T-2h, T-15m builds for upcoming calendar events
    - Email arrival triggers refresh for related events
    - Deadline detection from tasks/emails creates response packs
    """

    def __init__(self):
        self.compiler = get_context_pack_compiler()
        self._running = False
        self._check_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the context pack trigger system."""
        if self._running:
            return

        logger.info("Starting context pack trigger system")
        self._running = True

        # Start background checker
        self._check_task = asyncio.create_task(self._periodic_check())

    async def stop(self) -> None:
        """Stop the trigger system."""
        if not self._running:
            return

        self._running = False
        if self._check_task:
            self._check_task.cancel()
            try:
                await self._check_task
            except asyncio.CancelledError:
                pass

        logger.info("Context pack trigger system stopped")

    async def _periodic_check(self) -> None:
        """Periodically check for events needing context packs."""
        while self._running:
            try:
                await self._check_upcoming_events()
                await self._check_actionable_emails()
                await self._check_task_deadlines()
            except Exception as e:
                logger.error("Error in context pack check", error=str(e))

            # Check every 15 minutes
            await asyncio.sleep(15 * 60)

    async def _check_upcoming_events(self) -> None:
        """Check for calendar events needing context packs."""
        from cognitex.services.calendar import CalendarService

        try:
            cal = CalendarService()
            now = datetime.now()

            # Get events in next 48 hours
            result = cal.list_events(time_max=now + timedelta(hours=48))
            events = result.get("items", [])

            for event in events:
                event_id = event.get("id")
                start_raw = event.get("start", {})
                if isinstance(start_raw, dict):
                    start_str = start_raw.get("dateTime") or start_raw.get("date")
                else:
                    start_str = start_raw

                if not start_str:
                    continue

                try:
                    start_str = str(start_str).replace("Z", "").replace("+00:00", "")
                    event_start = datetime.fromisoformat(start_str)
                except (ValueError, TypeError):
                    continue

                time_until = event_start - now

                # Determine which stage to build
                stage = None
                is_whisper = False
                if timedelta(hours=23) < time_until <= timedelta(hours=25):
                    stage = BuildStage.T_24H
                elif timedelta(hours=1, minutes=45) < time_until <= timedelta(hours=2, minutes=15):
                    stage = BuildStage.T_2H
                elif timedelta(minutes=10) < time_until <= timedelta(minutes=20):
                    stage = BuildStage.T_15M
                elif timedelta(minutes=3) < time_until <= timedelta(minutes=7):
                    # T-5m WHISPER MODE - Send quick cheat sheet
                    stage = BuildStage.T_5M
                    is_whisper = True

                if stage:
                    # Check if pack already exists for this stage
                    existing = await self._get_existing_pack(event_id, stage)
                    if not existing:
                        logger.info(
                            "Building context pack",
                            event_title=event.get("summary", "Unknown")[:30],
                            stage=stage.value,
                            whisper=is_whisper,
                        )
                        pack = await self.compiler.compile_for_event(event, stage)

                        if is_whisper:
                            # Send high-priority whisper notification
                            await self._send_meeting_whisper(pack, event)
                        else:
                            await self._notify_pack_ready(pack, event)

        except Exception as e:
            logger.warning("Failed to check upcoming events", error=str(e))

    async def _get_existing_pack(
        self,
        event_id: str,
        stage: BuildStage,
    ) -> dict | None:
        """Check if a context pack already exists for event/stage."""
        try:
            async for session in get_neo4j_session():
                query = """
                MATCH (p:ContextPack {event_id: $event_id, build_stage: $stage})
                RETURN p
                LIMIT 1
                """
                result = await session.run(query, {
                    "event_id": event_id,
                    "stage": stage.value,
                })
                record = await result.single()
                return dict(record["p"]) if record else None
        except Exception:
            return None

    async def _check_actionable_emails(self) -> None:
        """Check for emails requiring responses and create packs."""
        try:
            async for session in get_neo4j_session():
                # Find recent emails marked as actionable but no response pack
                query = """
                MATCH (e:Email)
                WHERE e.classification IN ['actionable', 'urgent', 'needs_response']
                  AND e.date > datetime() - duration('P7D')
                  AND NOT EXISTS {
                    MATCH (e)-[:HAS_PACK]->(p:ContextPack)
                  }
                RETURN e.gmail_id as gmail_id, e.subject as subject,
                       e.snippet as snippet, e.classification as classification,
                       e.date as date
                ORDER BY e.date DESC
                LIMIT 10
                """
                result = await session.run(query)
                emails = await result.data()

                for email in emails:
                    await self._create_email_response_pack(email)

                # Also check for emails needing research
                research_query = """
                MATCH (e:Email)
                WHERE e.needs_research = true
                  AND e.date > datetime() - duration('P7D')
                  AND NOT EXISTS {
                    MATCH (e)-[:HAS_PACK]->(p:ContextPack {pack_type: 'research'})
                  }
                RETURN e.gmail_id as gmail_id, e.subject as subject,
                       e.snippet as snippet, e.research_topics as research_topics,
                       e.date as date
                ORDER BY e.date DESC
                LIMIT 5
                """
                research_result = await session.run(research_query)
                research_emails = await research_result.data()

                for email in research_emails:
                    topics = email.get("research_topics", [])
                    if topics:
                        await self.compiler.compile_research_pack(email, topics)

        except Exception as e:
            logger.warning("Failed to check actionable emails", error=str(e))

    async def _create_email_response_pack(self, email: dict) -> ContextPackContent | None:
        """Create a context pack for responding to an email."""
        gmail_id = email.get("gmail_id")
        subject = email.get("subject", "No subject")
        snippet = email.get("snippet", "")

        try:
            # Get sender info
            async for session in get_neo4j_session():
                sender_query = """
                MATCH (e:Email {gmail_id: $gmail_id})-[:SENT_BY]->(p:Person)
                RETURN p.email as email, p.name as name
                """
                result = await session.run(sender_query, {"gmail_id": gmail_id})
                sender_record = await result.single()

            sender_email = sender_record.get("email") if sender_record else None
            sender_name = sender_record.get("name") if sender_record else "Unknown"

            # Create a task-like dict for the pack compiler
            email_task = {
                "id": f"email_{gmail_id}",
                "title": f"Respond to: {subject[:50]}",
                "description": f"From: {sender_name}\n\n{snippet}",
                "blocked_by": [],
            }

            pack = await self.compiler.compile_for_task(email_task, BuildStage.T_24H)

            # Link pack to email in graph
            async for session in get_neo4j_session():
                link_query = """
                MATCH (e:Email {gmail_id: $gmail_id})
                MATCH (p:ContextPack {id: $pack_id})
                MERGE (e)-[:HAS_PACK]->(p)
                """
                await session.run(link_query, {
                    "gmail_id": gmail_id,
                    "pack_id": pack.pack_id,
                })

            logger.info("Created email response pack", email_subject=subject[:30])
            return pack

        except Exception as e:
            logger.warning("Failed to create email pack", error=str(e))
            return None

    async def _check_task_deadlines(self) -> None:
        """Check for tasks with approaching deadlines."""
        try:
            now = datetime.now()
            tomorrow = now + timedelta(days=1)

            async for session in get_neo4j_session():
                # Find tasks due within 24 hours without packs
                query = """
                MATCH (t:Task)
                WHERE t.due IS NOT NULL
                  AND datetime(t.due) <= datetime($tomorrow)
                  AND datetime(t.due) >= datetime($now)
                  AND t.status IN ['pending', 'in_progress']
                  AND NOT EXISTS {
                    MATCH (t)-[:HAS_PACK]->(p:ContextPack)
                  }
                RETURN t.id as id, t.title as title, t.description as description,
                       t.due as due, t.priority as priority
                ORDER BY t.due ASC
                LIMIT 5
                """
                result = await session.run(query, {
                    "now": now.isoformat(),
                    "tomorrow": tomorrow.isoformat(),
                })
                tasks = await result.data()

                for task in tasks:
                    pack = await self.compiler.compile_for_task(task, BuildStage.T_24H)

                    # Link pack to task
                    async for session2 in get_neo4j_session():
                        link_query = """
                        MATCH (t:Task {id: $task_id})
                        MATCH (p:ContextPack {id: $pack_id})
                        MERGE (t)-[:HAS_PACK]->(p)
                        """
                        await session2.run(link_query, {
                            "task_id": task.get("id"),
                            "pack_id": pack.pack_id,
                        })

                    logger.info("Created deadline pack", task=task.get("title", "")[:30])

        except Exception as e:
            logger.warning("Failed to check task deadlines", error=str(e))

    async def _notify_pack_ready(
        self,
        pack: ContextPackContent,
        event: dict,
    ) -> None:
        """Notify user when a context pack is ready (via Discord)."""
        try:
            from cognitex.db.redis import get_redis
            import json

            summary = event.get("summary", "Event")

            # Format missing prerequisites as bullet points
            missing_points = "\n".join([f"- {p}" for p in pack.missing_prerequisites[:3]])

            # Create notification in the format the Discord bot expects
            # Bot listens to cognitex:notifications and expects "message" and "urgency"
            notification_payload = {
                "message": (
                    f"**🧠 Context Pack Ready: {summary}**\n"
                    f"Readiness: {pack.readiness_score:.0%}\n"
                    f"Stage: {pack.build_stage.value}\n\n"
                    f"**Missing / Needs Attention:**\n{missing_points or 'None'}\n\n"
                    f"_Check dashboard for full briefing_"
                ),
                "urgency": "normal",
                "type": "context_pack",
                "pack_id": pack.pack_id,
            }

            redis = get_redis()
            # FIX: Publish to the correct channel that the bot actually listens to
            await redis.publish("cognitex:notifications", json.dumps(notification_payload))

            # Also create inbox item for unified view
            try:
                from cognitex.services.inbox import get_inbox_service
                from datetime import datetime, timedelta

                inbox = get_inbox_service()
                event_start = event.get("start", {})
                event_time_str = event_start.get("dateTime", "")

                # Determine priority based on stage
                priority = "normal"
                if pack.build_stage.value == "T_15M":
                    priority = "urgent"
                elif pack.build_stage.value == "T_1H":
                    priority = "high"

                # Parse event time for expiry
                expires_at = None
                if event_time_str:
                    try:
                        from dateutil.parser import parse as parse_dt
                        event_dt = parse_dt(event_time_str)
                        expires_at = event_dt + timedelta(minutes=30)  # Expire 30min after event starts
                    except Exception:
                        pass

                await inbox.create_item(
                    item_type="context_pack",
                    title=f"Context: {summary}",
                    summary=f"Readiness: {pack.readiness_score:.0%} | {len(pack.missing_prerequisites)} items need attention",
                    payload={
                        "pack_id": pack.pack_id,
                        "readiness": pack.readiness_score,
                        "event_id": event.get("id"),
                        "event_title": summary,
                        "event_time": event_time_str,
                        "stage": pack.build_stage.value,
                        "missing_count": len(pack.missing_prerequisites),
                    },
                    source_id=pack.pack_id,
                    source_type="context_packs",
                    priority=priority,
                    expires_at=expires_at,
                )
            except Exception as inbox_err:
                logger.debug("Failed to create inbox item for context pack", error=str(inbox_err))

        except Exception as e:
            logger.warning("Failed to notify pack ready", error=str(e))

    async def _send_meeting_whisper(
        self,
        pack: ContextPackContent,
        event: dict,
    ) -> None:
        """Send a T-5m 'whisper' - a high-priority quick cheat sheet.

        Just the 3 key points you need to know before walking in.
        """
        try:
            from cognitex.db.redis import get_redis
            import json

            summary = event.get("summary", "Meeting")
            attendees = event.get("attendees", [])

            # Build concise cheat sheet
            cheat_lines = []

            # Key objective
            if pack.objective:
                cheat_lines.append(f"**Goal:** {pack.objective}")

            # Top 3 don't forget items or decision points
            reminders = pack.dont_forget[:2] + pack.decision_list[:1]
            if reminders:
                cheat_lines.append("**Remember:**")
                for r in reminders[:3]:
                    cheat_lines.append(f"  • {r[:60]}")

            # Who you're meeting
            if attendees:
                names = [a.get("displayName") or a.get("email", "").split("@")[0]
                         for a in attendees[:3]]
                cheat_lines.append(f"**With:** {', '.join(names)}")

            # Last touch recap (brief)
            if pack.last_touch_recap:
                first_line = pack.last_touch_recap.split("\n")[0][:80]
                cheat_lines.append(f"**Last contact:** {first_line}")

            cheat_text = "\n".join(cheat_lines) if cheat_lines else "No prep notes"

            notification_payload = {
                "message": (
                    f"**🎯 {summary} starting in ~5 mins**\n\n"
                    f"{cheat_text}\n\n"
                    f"_Good luck!_"
                ),
                "urgency": "high",  # High priority for whisper
                "type": "meeting_whisper",
                "pack_id": pack.pack_id,
            }

            redis = get_redis()
            await redis.publish("cognitex:notifications", json.dumps(notification_payload))

            logger.info(
                "Sent meeting whisper",
                event=summary[:30],
                pack_id=pack.pack_id,
            )

        except Exception as e:
            logger.warning("Failed to send meeting whisper", error=str(e))

    async def on_email_received(self, email_data: dict) -> None:
        """Handle new email arrival - refresh related event packs.

        Called by the email trigger system when new emails arrive.
        """
        sender = email_data.get("sender", "")
        subject = email_data.get("subject", "")

        try:
            # Check if email is related to any upcoming events
            async for session in get_neo4j_session():
                # Find events with matching attendees
                query = """
                MATCH (e:Event)-[:HAS_ATTENDEE]->(p:Person {email: $sender})
                WHERE e.start > datetime()
                  AND e.start < datetime() + duration('P7D')
                RETURN e.gcal_id as event_id, e.summary as summary
                LIMIT 5
                """
                result = await session.run(query, {"sender": sender})
                events = await result.data()

                for event in events:
                    # Refresh the context pack
                    event_id = event.get("event_id")
                    pack_query = """
                    MATCH (p:ContextPack {event_id: $event_id})
                    RETURN p.id as pack_id
                    ORDER BY p.built_at DESC
                    LIMIT 1
                    """
                    pack_result = await session.run(pack_query, {"event_id": event_id})
                    pack_record = await pack_result.single()

                    if pack_record:
                        pack_id = pack_record.get("pack_id")
                        await self.compiler.refresh_pack(pack_id, BuildStage.T_2H)
                        logger.info(
                            "Refreshed pack on email arrival",
                            pack_id=pack_id,
                            email_subject=subject[:30],
                        )

        except Exception as e:
            logger.warning("Failed to refresh packs on email", error=str(e))


# Singleton
_trigger_system: ContextPackTriggerSystem | None = None


def get_context_pack_triggers() -> ContextPackTriggerSystem:
    """Get the context pack trigger system singleton."""
    global _trigger_system
    if _trigger_system is None:
        _trigger_system = ContextPackTriggerSystem()
    return _trigger_system


