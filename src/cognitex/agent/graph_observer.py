"""
Graph Observer - Queries to understand the current state of the knowledge graph.

Provides the autonomous agent with visibility into:
- Recent changes (new nodes, updates)
- Stale items (tasks not touched, inactive projects)
- Orphaned nodes (unlinked documents, disconnected entities)
- Goal/project health metrics
- Opportunities for new connections
"""

import asyncio
from datetime import datetime, timedelta
from typing import Any

import structlog

logger = structlog.get_logger()


class GraphObserver:
    """Observes and reports on the state of the knowledge graph.

    Note: This class uses session-independent queries for parallel execution.
    The session parameter in __init__ is kept for backward compatibility but
    is only used for write operations. Read queries use the run_query() helper
    which obtains its own session from the connection pool.
    """

    def __init__(self, session=None):
        """Initialize the observer.

        Args:
            session: Optional Neo4j session for write operations. Read queries
                    use independent sessions for safe parallel execution.
        """
        self.session = session

    async def get_full_context(self) -> dict:
        """Gather comprehensive context about the graph state."""
        # Execute independent queries in parallel for performance
        # Use return_exceptions=True so one failure doesn't kill the whole cycle
        (
            inbox_items,
            recent_changes,
            stale_items,
            orphaned_nodes,
            goal_health,
            project_health,
            pending_tasks,
            recent_documents,
            connection_opportunities,
            writing_samples,
            pending_emails,
            upcoming_calendar,
            projects_with_recent_blocks,
            approaching_commitments,
            overdue_commitments,
            stale_commitments,
        ) = await asyncio.gather(
            self._get_inbox_items(),
            self.get_recent_changes(),
            self.get_stale_items(),
            self.get_orphaned_nodes(),
            self.get_goal_health(),
            self.get_project_health(),
            self.get_pending_tasks(),
            self.get_recent_documents(),
            self.get_connection_opportunities(),
            self.get_user_writing_samples(),
            self.get_actionable_emails(),
            self.get_pending_calendar_blocks(),
            self.get_projects_with_recent_blocks(),
            self.get_approaching_commitments(),
            self.get_overdue_commitments(),
            self.get_stale_commitments(),
            return_exceptions=True,
        )

        # Helper to safely unpack results or return empty defaults
        def unwrap(res, default):
            if isinstance(res, Exception):
                logger.error("Graph observer query failed", error=str(res))
                return default
            return res

        context = {
            "timestamp": datetime.now().isoformat(),
            "summary": {},
            # Graph health metrics
            "recent_changes": unwrap(recent_changes, []),
            "stale_items": unwrap(stale_items, []),
            "orphaned_nodes": unwrap(orphaned_nodes, []),
            "goal_health": unwrap(goal_health, []),
            "project_health": unwrap(project_health, []),
            "pending_tasks": unwrap(pending_tasks, []),
            "recent_documents": unwrap(recent_documents, []),
            "connection_opportunities": unwrap(connection_opportunities, []),
            # Digital twin perception
            "writing_samples": unwrap(writing_samples, []),
            "pending_emails": unwrap(pending_emails, []),
            "upcoming_calendar": unwrap(upcoming_calendar, []),
            # Already-actioned items (to prevent re-suggesting)
            "projects_with_recent_blocks": unwrap(projects_with_recent_blocks, set()),
            # Firewall inbox - captured items needing triage
            "inbox_items": unwrap(inbox_items, []),
            # Commitment ledger (WP5)
            "approaching_commitments": unwrap(approaching_commitments, []),
            "overdue_commitments": unwrap(overdue_commitments, []),
            "stale_commitments": unwrap(stale_commitments, []),
        }

        # Build summary
        context["summary"] = {
            # Graph health
            "total_changes_24h": len(context["recent_changes"]),
            "stale_tasks": len([i for i in context["stale_items"] if i["type"] == "Task"]),
            "stale_projects": len([i for i in context["stale_items"] if i["type"] == "Project"]),
            "orphaned_documents": len(
                [n for n in context["orphaned_nodes"] if n["type"] == "Document"]
            ),
            "goals_needing_attention": len(
                [g for g in context["goal_health"] if g.get("needs_attention")]
            ),
            "projects_needing_attention": len(
                [p for p in context["project_health"] if p.get("needs_attention")]
            ),
            "pending_task_count": len(context["pending_tasks"]),
            "connection_opportunities": len(context["connection_opportunities"]),
            # Digital twin actionable items
            "emails_needing_response": len(context["pending_emails"]),
            "meetings_needing_prep": len(
                [c for c in context["upcoming_calendar"] if c.get("needs_context")]
            ),
            "has_writing_samples": len(context["writing_samples"]) > 0,
            # Firewall inbox
            "inbox_count": len(inbox_items),
            # Commitment ledger
            "approaching_commitment_count": len(context["approaching_commitments"]),
            "overdue_commitment_count": len(context["overdue_commitments"]),
            "stale_commitment_count": len(context["stale_commitments"]),
        }

        return context

    async def _get_inbox_items(self) -> list[dict]:
        """Get items from the interruption firewall inbox."""
        try:
            from cognitex.agent.interruption_firewall import get_interruption_firewall

            firewall = get_interruption_firewall()
            items = await firewall.get_queued_items(limit=5)

            return [
                {
                    "id": item.item_id,
                    "source": item.source,
                    "subject": item.subject,
                    "preview": item.preview[:150] if item.preview else "",
                    "suggested": item.suggested_action,
                    "urgency": item.urgency.value
                    if hasattr(item.urgency, "value")
                    else str(item.urgency),
                }
                for item in items
            ]
        except Exception as e:
            logger.debug("Failed to get inbox items", error=str(e))
            return []

    async def get_recent_changes(self, hours: int = 24) -> list[dict]:
        """Get nodes that were created or updated recently."""
        from cognitex.db.neo4j import run_query

        query = """
        MATCH (n)
        WHERE n.created_at > datetime() - duration({hours: $hours})
           OR n.updated_at > datetime() - duration({hours: $hours})
        RETURN
            labels(n)[0] as type,
            CASE labels(n)[0]
                WHEN 'Task' THEN n.id
                WHEN 'Project' THEN n.id
                WHEN 'Goal' THEN n.id
                WHEN 'Document' THEN n.drive_id
                WHEN 'Person' THEN n.email
                ELSE coalesce(n.id, n.name, n.email)
            END as id,
            CASE labels(n)[0]
                WHEN 'Task' THEN n.title
                WHEN 'Project' THEN n.title
                WHEN 'Goal' THEN n.title
                WHEN 'Document' THEN n.name
                WHEN 'Person' THEN coalesce(n.name, n.email)
                ELSE coalesce(n.title, n.name)
            END as label,
            n.created_at as created_at,
            n.updated_at as updated_at
        ORDER BY coalesce(n.updated_at, n.created_at) DESC
        LIMIT 50
        """
        try:
            return await run_query(query, {"hours": hours})
        except Exception as e:
            logger.warning("Failed to get recent changes", error=str(e))
            return []

    async def get_stale_items(self, task_days: int = 7, project_days: int = 14) -> list[dict]:
        """Get tasks and projects that haven't been updated recently.

        Limited to prevent flooding the LLM context window with too many items.
        Returns oldest items first (most stale).
        """
        from cognitex.db.neo4j import run_query

        query = """
        // Stale pending tasks (limited to prevent token explosion)
        MATCH (t:Task)
        WHERE t.status IN ['pending', 'in_progress']
          AND (t.updated_at < datetime() - duration({days: $task_days})
               OR t.updated_at IS NULL)
        RETURN
            'Task' as type,
            t.id as id,
            t.title as label,
            t.status as status,
            t.updated_at as last_updated,
            t.due as due_date
        ORDER BY t.updated_at ASC
        LIMIT 20

        UNION ALL

        // Stale active projects (limited)
        MATCH (p:Project)
        WHERE p.status = 'active'
          AND (p.updated_at < datetime() - duration({days: $project_days})
               OR p.updated_at IS NULL)
        RETURN
            'Project' as type,
            p.id as id,
            p.title as label,
            p.status as status,
            p.updated_at as last_updated,
            null as due_date
        ORDER BY p.updated_at ASC
        LIMIT 10
        """
        try:
            return await run_query(query, {"task_days": task_days, "project_days": project_days})
        except Exception as e:
            logger.warning("Failed to get stale items", error=str(e))
            return []

    async def get_orphaned_nodes(self) -> list[dict]:
        """Get nodes that should have connections but don't.

        Excludes documents matching configured name patterns or MIME types.
        """
        from cognitex.config import get_settings
        from cognitex.db.neo4j import run_query

        settings = get_settings()

        # Parse exclusion patterns from config
        name_patterns = [
            p.strip() for p in settings.orphan_exclude_name_patterns.split(",") if p.strip()
        ]
        mime_patterns = [
            p.strip() for p in settings.orphan_exclude_mime_types.split(",") if p.strip()
        ]

        # Build exclusion conditions for Cypher
        name_exclusions = (
            " AND ".join([f"NOT d.name STARTS WITH '{p}'" for p in name_patterns])
            if name_patterns
            else "true"
        )
        mime_exclusions = (
            " AND ".join([f"NOT d.mime_type STARTS WITH '{p}'" for p in mime_patterns])
            if mime_patterns
            else "true"
        )

        query = f"""
        // Documents not linked to any project (excluding configured patterns)
        MATCH (d:Document)
        WHERE NOT (d)-[:BELONGS_TO|REFERENCES|MENTIONED_IN]->(:Project)
          AND NOT (d)-[:BELONGS_TO|REFERENCES|MENTIONED_IN]->(:Goal)
          AND NOT d.dismissed = true
          AND {name_exclusions}
          AND {mime_exclusions}
        OPTIONAL MATCH (d)-[:COVERS]->(t:Topic)
        WITH d, collect(t.name)[0..3] as topics
        RETURN
            'Document' as type,
            d.drive_id as id,
            d.name as label,
            d.mime_type as mime_type,
            topics,
            'No project/goal link' as issue
        LIMIT 20

        UNION ALL

        // Tasks not linked to any project
        MATCH (t:Task)
        WHERE NOT (t)-[:BELONGS_TO]->(:Project)
          AND t.status <> 'completed'
        RETURN
            'Task' as type,
            t.id as id,
            t.title as label,
            null as mime_type,
            [] as topics,
            'No project link' as issue
        LIMIT 20
        """
        try:
            return await run_query(query)
        except Exception as e:
            logger.warning("Failed to get orphaned nodes", error=str(e))
            return []

    async def get_goal_health(self) -> list[dict]:
        """Assess health of each active goal."""
        from cognitex.db.neo4j import run_query

        query = """
        MATCH (g:Goal)
        WHERE g.status = 'active'
        OPTIONAL MATCH (g)<-[:PART_OF]-(p:Project)
        OPTIONAL MATCH (p)<-[:BELONGS_TO]-(t:Task)
        WITH g,
             collect(DISTINCT p) as projects,
             collect(DISTINCT t) as all_tasks
        WITH g, projects,
             size(projects) as project_count,
             size(all_tasks) as total_tasks,
             size([t IN all_tasks WHERE t.status = 'completed']) as completed_tasks,
             size([t IN all_tasks WHERE t.status = 'in_progress']) as in_progress_tasks
        RETURN
            g.id as id,
            g.title as title,
            g.timeframe as timeframe,
            g.target_date as target_date,
            project_count,
            total_tasks,
            completed_tasks,
            in_progress_tasks,
            CASE
                WHEN project_count = 0 THEN true
                WHEN total_tasks > 0 AND in_progress_tasks = 0 AND completed_tasks < total_tasks THEN true
                ELSE false
            END as needs_attention,
            CASE
                WHEN project_count = 0 THEN 'No projects linked'
                WHEN total_tasks = 0 THEN 'No tasks defined'
                WHEN in_progress_tasks = 0 AND completed_tasks < total_tasks THEN 'No tasks in progress'
                ELSE 'On track'
            END as status_reason
        ORDER BY needs_attention DESC, g.created_at DESC
        """
        try:
            return await run_query(query)
        except Exception as e:
            logger.warning("Failed to get goal health", error=str(e))
            return []

    async def get_project_health(self) -> list[dict]:
        """Assess health of each active project."""
        from cognitex.db.neo4j import run_query

        query = """
        MATCH (p:Project)
        WHERE p.status = 'active'
        OPTIONAL MATCH (p)<-[:BELONGS_TO|PART_OF]-(t:Task)
        OPTIONAL MATCH (p)-[:PART_OF|ACHIEVES]->(g:Goal)
        WITH p,
             collect(DISTINCT t) as tasks,
             collect(DISTINCT g.title) as goal_titles
        WITH p,
             goal_titles[0] as goal_title,
             size(tasks) as total_tasks,
             size([t IN tasks WHERE t.status = 'completed']) as completed_tasks,
             size([t IN tasks WHERE t.status = 'in_progress']) as in_progress_tasks,
             size([t IN tasks WHERE t.status = 'pending']) as pending_tasks,
             [t IN tasks WHERE t.due IS NOT NULL AND t.due < date() AND t.status <> 'completed'] as overdue_tasks
        RETURN
            p.id as id,
            p.title as title,
            goal_title,
            total_tasks,
            completed_tasks,
            in_progress_tasks,
            pending_tasks,
            size(overdue_tasks) as overdue_count,
            CASE
                WHEN total_tasks = 0 THEN true
                WHEN size(overdue_tasks) > 0 THEN true
                WHEN in_progress_tasks = 0 AND pending_tasks > 0 THEN true
                ELSE false
            END as needs_attention,
            CASE
                WHEN total_tasks = 0 THEN 'No tasks defined'
                WHEN size(overdue_tasks) > 0 THEN 'Has overdue tasks'
                WHEN in_progress_tasks = 0 AND pending_tasks > 0 THEN 'No tasks in progress'
                ELSE 'On track'
            END as status_reason,
            CASE
                WHEN total_tasks = 0 THEN 0
                ELSE toFloat(completed_tasks) / total_tasks
            END as completion_ratio
        ORDER BY needs_attention DESC, overdue_count DESC
        """
        try:
            return await run_query(query)
        except Exception as e:
            logger.warning("Failed to get project health", error=str(e))
            return []

    async def get_pending_tasks(self, limit: int = 30) -> list[dict]:
        """Get pending tasks with context."""
        from cognitex.db.neo4j import run_query

        query = """
        MATCH (t:Task)
        WHERE t.status IN ['pending', 'in_progress']
        OPTIONAL MATCH (t)-[:BELONGS_TO]->(p:Project)
        OPTIONAL MATCH (p)-[:PART_OF]->(g:Goal)
        OPTIONAL MATCH (t)-[:ASSIGNED_TO]->(person:Person)
        RETURN
            t.id as id,
            t.title as title,
            t.status as status,
            t.due as due_date,
            t.energy_cost as energy_cost,
            t.created_at as created_at,
            p.title as project_title,
            g.title as goal_title,
            person.name as assigned_to,
            CASE
                WHEN t.due IS NOT NULL AND t.due < date() THEN true
                ELSE false
            END as is_overdue
        ORDER BY
            CASE WHEN t.due IS NOT NULL AND t.due < date() THEN 0 ELSE 1 END,
            CASE WHEN t.due IS NULL THEN 1 ELSE 0 END,
            t.due ASC,
            t.energy_cost ASC
        LIMIT $limit
        """
        try:
            return await run_query(query, {"limit": limit})
        except Exception as e:
            logger.warning("Failed to get pending tasks", error=str(e))
            return []

    async def get_recent_documents(self, days: int = 7, limit: int = 20) -> list[dict]:
        """Get recently added/modified documents."""
        from cognitex.db.neo4j import run_query

        query = """
        MATCH (d:Document)
        WHERE d.indexed_at > datetime() - duration({days: $days})
           OR d.created_at > datetime() - duration({days: $days})
        OPTIONAL MATCH (d)-[:COVERS]->(t:Topic)
        OPTIONAL MATCH (d)-[:ABOUT]->(c:Concept)
        OPTIONAL MATCH (d)-[:BELONGS_TO]->(p:Project)
        WITH d,
             collect(DISTINCT t.name)[0..5] as topics,
             collect(DISTINCT c.name)[0..5] as concepts,
             collect(DISTINCT p.title)[0..3] as projects
        RETURN
            d.drive_id as id,
            d.name as name,
            d.mime_type as mime_type,
            d.summary as summary,
            topics,
            concepts,
            projects,
            size(projects) > 0 as has_project_link
        ORDER BY CASE WHEN d.indexed_at IS NULL THEN 1 ELSE 0 END, d.indexed_at DESC
        LIMIT $limit
        """
        try:
            return await run_query(query, {"days": days, "limit": limit})
        except Exception as e:
            logger.warning("Failed to get recent documents", error=str(e))
            return []

    async def get_connection_opportunities(self) -> list[dict]:
        """Find opportunities to create new connections based on names, topics, and content."""
        from cognitex.db.neo4j import run_query

        opportunities = []

        # 1. Documents with names matching project titles (min 4 chars for the matching term)
        query1 = """
        MATCH (d:Document), (p:Project)
        WHERE NOT (d)-[:BELONGS_TO|REFERENCES]->(p)
          AND (
            (size(p.title) >= 4 AND toLower(d.name) CONTAINS toLower(p.title))
            OR (size(d.name) >= 4 AND toLower(p.title) CONTAINS toLower(d.name))
          )
        RETURN
            'document_project_name' as opportunity_type,
            d.drive_id as source_id,
            d.name as source_name,
            'Document' as source_type,
            p.id as target_id,
            p.title as target_name,
            'Project' as target_type,
            'Name match' as match_reason,
            2 as relevance_score
        LIMIT 15
        """

        # 2. GitHub repositories matching project titles (min 4 chars for matching term)
        query2 = """
        MATCH (r:Repository), (p:Project)
        WHERE NOT (r)-[:BELONGS_TO|PART_OF]->(p)
          AND (
            (size(p.title) >= 4 AND (toLower(r.name) CONTAINS toLower(p.title) OR toLower(r.full_name) CONTAINS toLower(p.title)))
            OR (size(r.name) >= 4 AND toLower(p.title) CONTAINS toLower(r.name))
          )
        RETURN
            'repository_project_name' as opportunity_type,
            r.id as source_id,
            r.full_name as source_name,
            'Repository' as source_type,
            p.id as target_id,
            p.title as target_name,
            'Project' as target_type,
            'Name match' as match_reason,
            3 as relevance_score
        LIMIT 10
        """

        # 3. Documents sharing topics with projects
        query3 = """
        MATCH (d:Document)-[:COVERS]->(t:Topic)<-[:COVERS]-(p:Project)
        WHERE NOT (d)-[:BELONGS_TO|REFERENCES]->(p)
        WITH d, p, collect(DISTINCT t.name) as shared_topics
        WHERE size(shared_topics) >= 1
        RETURN
            'document_project_topic' as opportunity_type,
            d.drive_id as source_id,
            d.name as source_name,
            'Document' as source_type,
            p.id as target_id,
            p.title as target_name,
            'Project' as target_type,
            'Shared topics: ' + reduce(s = '', t IN shared_topics[0..3] | s + t + ', ') as match_reason,
            size(shared_topics) as relevance_score
        ORDER BY relevance_score DESC
        LIMIT 10
        """

        # 4. Orphaned tasks matching project names (min 4 chars for matching term)
        query4 = """
        MATCH (t:Task), (p:Project)
        WHERE NOT (t)-[:BELONGS_TO]->(p)
          AND t.status <> 'completed'
          AND (
            (size(p.title) >= 4 AND toLower(t.title) CONTAINS toLower(p.title))
            OR (size(t.title) >= 4 AND toLower(p.title) CONTAINS toLower(t.title))
          )
        RETURN
            'task_project_name' as opportunity_type,
            t.id as source_id,
            t.title as source_name,
            'Task' as source_type,
            p.id as target_id,
            p.title as target_name,
            'Project' as target_type,
            'Name match' as match_reason,
            2 as relevance_score
        LIMIT 10
        """

        # 5. Projects that could link to goals based on name/description (min 4 chars)
        query5 = """
        MATCH (p:Project), (g:Goal)
        WHERE NOT (p)-[:PART_OF]->(g)
          AND p.status = 'active'
          AND g.status = 'active'
          AND size(g.title) >= 4
          AND (toLower(p.title) CONTAINS toLower(g.title)
               OR toLower(g.title) CONTAINS toLower(p.title)
               OR toLower(coalesce(p.description, '')) CONTAINS toLower(g.title))
        RETURN
            'project_goal_name' as opportunity_type,
            p.id as source_id,
            p.title as source_name,
            'Project' as source_type,
            g.id as target_id,
            g.title as target_name,
            'Goal' as target_type,
            'Name/description match' as match_reason,
            2 as relevance_score
        LIMIT 10
        """

        queries = [query1, query2, query3, query4, query5]

        # Run all queries in parallel - each uses its own session
        async def run_opp_query(query: str) -> list[dict]:
            try:
                return await run_query(query)
            except Exception as e:
                logger.warning("Connection opportunity query failed", error=str(e))
                return []

        results = await asyncio.gather(*[run_opp_query(q) for q in queries])
        for data in results:
            opportunities.extend(data)

        # Sort by relevance and deduplicate
        opportunities.sort(key=lambda x: x.get("relevance_score", 0), reverse=True)
        return opportunities[:30]

    async def get_graph_stats(self) -> dict:
        """Get overall graph statistics."""
        from cognitex.db.neo4j import run_query

        query = """
        MATCH (n)
        WITH labels(n)[0] as label, count(*) as count
        RETURN label, count
        ORDER BY count DESC
        """
        try:
            data = await run_query(query)
            return {row["label"]: row["count"] for row in data}
        except Exception as e:
            logger.warning("Failed to get graph stats", error=str(e))
            return {}

    # =========================================================================
    # Digital Twin Perception Methods
    # =========================================================================

    async def get_user_writing_samples(self, limit: int = 5) -> list[str]:
        """
        Fetch recent emails sent by the user to establish writing style.

        These samples allow the agent to mimic the user's voice when drafting
        emails or other communications. Uses snippet as fallback if body is not stored.
        """
        from cognitex.db.neo4j import run_query
        from cognitex.services.ingestion import get_user_email

        # Get user email for fallback matching
        user_email = await get_user_email()

        query = """
        MATCH (e:Email)-[:SENT_BY]->(p:Person)
        WHERE (p.is_user = true OR p.email = $user_email)
        WITH e, coalesce(e.body, e.snippet) as content
        WHERE content IS NOT NULL AND size(content) > 20
        RETURN content as body
        ORDER BY e.date DESC
        LIMIT $limit
        """
        try:
            data = await run_query(query, {"limit": limit, "user_email": user_email})
            samples = [row["body"] for row in data if row.get("body")]

            if not samples:
                logger.info("No writing samples found for user")
                return []
            return samples
        except Exception as e:
            logger.warning("Failed to get user writing samples", error=str(e))
            return []

    async def get_actionable_emails(self, limit: int = 10, days_back: int = 7) -> list[dict]:
        """
        Get emails that likely require a response or action.

        Finds incoming emails marked as actionable/urgent that haven't been
        addressed yet (no reply sent, no task created, no draft created).

        Filters:
        - Only emails from the last N days (default 7)
        - Only actionable/urgent classification (not automated/marketing/newsletter)
        - Excludes emails already replied to or with drafts
        - Excludes emails sent by the user
        """
        from cognitex.db.neo4j import run_query

        query = """
        MATCH (e:Email)
        WHERE e.classification IN ['actionable', 'urgent']
          AND e.date >= datetime() - duration({days: $days_back})
          AND NOT e.classification IN ['automated', 'marketing', 'newsletter', 'informational']
          AND NOT (e)<-[:REPLY_TO]-(:Email)
          AND NOT (e)<-[:REPLY_TO]-(:EmailDraft)
          AND NOT (e)<-[:DERIVED_FROM]-(:Task)
          AND NOT (e)-[:SENT_BY]->(:Person {is_user: true})
          AND COALESCE(e.is_sent, false) = false
          // Only process INBOX emails (not filtered/archived)
          // Allow emails without labels (pre-backfill) OR with INBOX label
          AND (e.labels IS NULL OR size(e.labels) = 0 OR 'INBOX' IN e.labels)
          // Skip emails already flagged for review in the last 7 days
          AND (e.flagged_at IS NULL OR e.flagged_at < datetime() - duration({days: 7}))
        OPTIONAL MATCH (e)-[:SENT_BY]->(sender:Person)
        // Extra safety: exclude if sender matches user email
        WHERE sender IS NULL OR sender.email <> 'chris@sainsbury.im'
        RETURN
            e.gmail_id as id,
            e.subject as subject,
            e.snippet as snippet,
            e.body as body,
            e.date as date,
            e.classification as classification,
            coalesce(e.urgency, 'normal') as urgency,
            sender.name as sender_name,
            sender.email as sender_email
        ORDER BY
            CASE e.classification
                WHEN 'urgent' THEN 0
                WHEN 'actionable' THEN 1
                ELSE 2
            END,
            e.date DESC
        LIMIT $limit
        """
        try:
            return await run_query(query, {"limit": limit, "days_back": days_back})
        except Exception as e:
            logger.warning("Failed to get actionable emails", error=str(e))
            return []

    async def get_pending_calendar_blocks(self, days_ahead: int = 7) -> list[dict]:
        """
        Get upcoming calendar events that may need preparation or context.

        Identifies meetings that the user should prepare for, especially
        those without attached materials or context. Excludes events that
        already have a context pack prepared (regardless of pack status).
        """
        from cognitex.db.neo4j import run_query

        query = """
        MATCH (c:CalendarEvent)
        WHERE c.start_time > datetime()
          AND c.start_time < datetime() + duration({days: $days_ahead})
          AND NOT (c)<-[:PREPARED_FOR]-(:ContextPack)
        OPTIONAL MATCH (c)-[:ABOUT]->(p:Project)
        OPTIONAL MATCH (c)-[:INVOLVES]->(person:Person)
        OPTIONAL MATCH (c)-[:HAS_ATTACHMENT]->(d:Document)
        WITH c,
             collect(DISTINCT p.title) as related_projects,
             collect(DISTINCT person.name) as attendees,
             collect(DISTINCT d.name) as attachments
        RETURN
            c.id as id,
            c.title as title,
            c.start_time as start_time,
            c.end_time as end_time,
            c.description as description,
            related_projects,
            attendees,
            attachments,
            size(attachments) = 0 AND size(related_projects) = 0 as needs_context
        ORDER BY c.start_time ASC
        LIMIT 20
        """
        try:
            return await run_query(query, {"days_ahead": days_ahead})
        except Exception as e:
            logger.warning("Failed to get pending calendar blocks", error=str(e))
            return []

    async def get_projects_with_recent_blocks(self, days: int = 7) -> set[str]:
        """
        Get project IDs that have had focus blocks suggested recently.

        Used to prevent the agent from repeatedly suggesting blocks for
        the same projects.
        """
        from cognitex.db.neo4j import run_query

        query = """
        MATCH (sb:SuggestedBlock)-[:FOR_PROJECT]->(p:Project)
        WHERE sb.created_at > datetime() - duration({days: $days})
        RETURN DISTINCT p.id as project_id
        """
        try:
            data = await run_query(query, {"days": days})
            return {row["project_id"] for row in data if row.get("project_id")}
        except Exception as e:
            logger.warning("Failed to get projects with recent blocks", error=str(e))
            return set()

    async def get_decision_context(self, topic: str = None) -> list[dict]:
        """
        Gather context for decision-making on a specific topic.

        Finds all relevant documents, emails, and tasks related to a topic
        to help compile a decision pack.
        """
        from cognitex.db.neo4j import run_query

        if not topic:
            # Get topics from recent high-priority items
            query = """
            MATCH (t:Task)
            WHERE t.status = 'in_progress'
              AND t.energy_cost >= 3
            OPTIONAL MATCH (t)-[:BELONGS_TO]->(p:Project)
            OPTIONAL MATCH (t)-[:COVERS]->(topic:Topic)
            RETURN
                t.title as task_title,
                p.title as project_title,
                collect(DISTINCT topic.name) as topics
            ORDER BY t.updated_at DESC
            LIMIT 5
            """
        else:
            query = """
            MATCH (n)
            WHERE (n:Document OR n:Email OR n:Task OR n:Project)
              AND (toLower(coalesce(n.title, n.name, n.subject, '')) CONTAINS toLower($topic)
                   OR toLower(coalesce(n.summary, n.body, n.description, '')) CONTAINS toLower($topic))
            OPTIONAL MATCH (n)-[:COVERS]->(t:Topic)
            OPTIONAL MATCH (n)-[:ABOUT]->(c:Concept)
            RETURN
                labels(n)[0] as type,
                coalesce(n.id, n.drive_id, n.gmail_id) as id,
                coalesce(n.title, n.name, n.subject) as title,
                coalesce(n.summary, n.snippet, n.description) as summary,
                collect(DISTINCT t.name) as topics,
                collect(DISTINCT c.name) as concepts
            ORDER BY n.updated_at DESC, n.date DESC
            LIMIT 20
            """
        try:
            params = {"topic": topic} if topic else {}
            return await run_query(query, params)
        except Exception as e:
            logger.warning("Failed to get decision context", error=str(e))
            return []

    async def get_email_deep_context(self, gmail_id: str) -> dict:
        """
        Get comprehensive context for drafting a response to an email.

        Gathers:
        - Full email body (fetched from Gmail API if needed)
        - Thread history (prior messages in the conversation)
        - Related Drive documents (via semantic search)
        - Related tasks and projects
        - Sender relationship context

        Args:
            gmail_id: The Gmail message ID

        Returns:
            Dict with full context for email response drafting
        """
        from cognitex.db.neo4j import run_query, run_query_single

        context = {
            "gmail_id": gmail_id,
            "full_body": None,
            "thread_history": [],
            "related_documents": [],
            "related_tasks": [],
            "sender_context": None,
            "action_items_extracted": [],
        }

        try:
            # 1. Get email metadata from graph
            email_query = """
            MATCH (e:Email {gmail_id: $gmail_id})
            OPTIONAL MATCH (e)-[:SENT_BY]->(sender:Person)
            RETURN
                e.gmail_id as gmail_id,
                e.thread_id as thread_id,
                e.subject as subject,
                e.snippet as snippet,
                e.body as body,
                e.date as date,
                e.classification as classification,
                sender.email as sender_email,
                sender.name as sender_name,
                sender.org as sender_org,
                sender.role as sender_role
            """
            email_dict = await run_query_single(email_query, {"gmail_id": gmail_id})

            if not email_dict:
                logger.warning("Email not found in graph", gmail_id=gmail_id)
                return context

            context["subject"] = email_dict.get("subject")
            context["sender_email"] = email_dict.get("sender_email")
            context["sender_name"] = email_dict.get("sender_name")

            # 2. Get full email body from Gmail API if not stored
            full_body = email_dict.get("body")
            if not full_body or len(full_body) < 100:
                try:
                    from cognitex.services.gmail import GmailService, extract_email_body

                    gmail = GmailService()
                    full_message = gmail.get_message(gmail_id, format="full")
                    full_body = extract_email_body(full_message, max_length=10000)
                    context["full_body"] = full_body
                except Exception as e:
                    logger.warning("Failed to fetch full email body", error=str(e))
                    context["full_body"] = email_dict.get("snippet", "")
            else:
                context["full_body"] = full_body

            # 3. Get thread history (other emails in same thread)
            thread_id = email_dict.get("thread_id")
            if thread_id:
                thread_query = """
                MATCH (e:Email {thread_id: $thread_id})
                WHERE e.gmail_id <> $gmail_id
                OPTIONAL MATCH (e)-[:SENT_BY]->(sender:Person)
                RETURN
                    e.gmail_id as gmail_id,
                    e.subject as subject,
                    coalesce(e.body, e.snippet) as body,
                    e.date as date,
                    sender.name as sender_name,
                    sender.email as sender_email
                ORDER BY e.date ASC
                LIMIT 10
                """
                context["thread_history"] = await run_query(
                    thread_query, {"thread_id": thread_id, "gmail_id": gmail_id}
                )

            # 4. Semantic search for related Drive documents
            search_text = f"{email_dict.get('subject', '')} {context['full_body'][:500] if context['full_body'] else ''}"
            if search_text.strip():
                try:
                    from cognitex.db.postgres import get_session
                    from cognitex.services.ingestion import search_chunks_semantic

                    async for pg_session in get_session():
                        chunks = await search_chunks_semantic(pg_session, search_text, limit=5)
                        break

                    # Get document details from graph
                    for chunk in chunks:
                        drive_id = chunk.get("drive_id")
                        if drive_id:
                            doc_query = """
                            MATCH (d:Document {drive_id: $drive_id})
                            RETURN d.name as name, d.drive_id as drive_id,
                                   d.summary as summary, d.mime_type as mime_type
                            """
                            doc_data = await run_query_single(doc_query, {"drive_id": drive_id})
                            if doc_data:
                                context["related_documents"].append(
                                    {
                                        **doc_data,
                                        "relevance": chunk.get("similarity", 0.5),
                                        "matched_content": chunk.get("content", "")[:200],
                                    }
                                )
                except Exception as e:
                    logger.warning("Failed to search related documents", error=str(e))

            # 5. Find related tasks (by sender or subject keywords)
            sender_email = email_dict.get("sender_email")
            if sender_email:
                task_query = """
                MATCH (t:Task)
                WHERE t.status IN ['pending', 'in_progress']
                  AND (
                    t.source_id = $gmail_id
                    OR toLower(t.title) CONTAINS toLower($subject_keyword)
                    OR EXISTS {
                      MATCH (t)-[:ASSIGNED_TO|INVOLVES]->(p:Person {email: $sender_email})
                    }
                  )
                OPTIONAL MATCH (t)-[:BELONGS_TO]->(proj:Project)
                RETURN
                    t.id as id,
                    t.title as title,
                    t.status as status,
                    t.due as due,
                    proj.title as project_title
                LIMIT 5
                """
                # Extract first significant word from subject for keyword matching
                subject = email_dict.get("subject", "")
                subject_keyword = next(
                    (
                        w
                        for w in subject.split()
                        if len(w) > 4 and w.lower() not in ["about", "follow", "update"]
                    ),
                    subject[:20],
                )
                context["related_tasks"] = await run_query(
                    task_query,
                    {
                        "gmail_id": gmail_id,
                        "sender_email": sender_email,
                        "subject_keyword": subject_keyword,
                    },
                )

            # 6. Get sender relationship context
            if sender_email:
                sender_query = """
                MATCH (p:Person {email: $email})
                OPTIONAL MATCH (p)<-[:SENT_BY]-(e:Email)
                WITH p, count(e) as email_count, max(e.date) as last_email
                OPTIONAL MATCH (p)-[:ATTENDED]-(ev:Event)
                WITH p, email_count, last_email, count(ev) as meeting_count
                OPTIONAL MATCH (p)<-[:INVOLVES|ASSIGNED_TO]-(t:Task)
                WHERE t.status IN ['pending', 'in_progress']
                RETURN
                    p.name as name,
                    p.org as org,
                    p.role as role,
                    email_count,
                    last_email,
                    meeting_count,
                    count(t) as shared_task_count
                """
                sender_data = await run_query_single(sender_query, {"email": sender_email})
                if sender_data:
                    context["sender_context"] = sender_data

            # 7. Extract action items from the email body using simple pattern matching
            if context["full_body"]:
                action_patterns = [
                    "please ",
                    "could you ",
                    "can you ",
                    "would you ",
                    "need to ",
                    "should ",
                    "must ",
                    "by ",
                    "deadline",
                    "action required",
                    "follow up",
                    "let me know",
                ]
                body_lower = context["full_body"].lower()
                sentences = context["full_body"].split(".")
                for sentence in sentences:
                    sentence_lower = sentence.lower().strip()
                    if any(p in sentence_lower for p in action_patterns) and len(sentence) > 20:
                        context["action_items_extracted"].append(sentence.strip())
                        if len(context["action_items_extracted"]) >= 5:
                            break

            logger.info(
                "Built email deep context",
                gmail_id=gmail_id,
                has_full_body=bool(context["full_body"]),
                thread_messages=len(context["thread_history"]),
                related_docs=len(context["related_documents"]),
                related_tasks=len(context["related_tasks"]),
                action_items=len(context["action_items_extracted"]),
            )

        except Exception as e:
            logger.error("Failed to build email deep context", gmail_id=gmail_id, error=str(e))

        return context

    # =========================================================================
    # Phase 4 Learning: Deadline & Completion Patterns (1.2)
    # =========================================================================

    async def get_deadline_patterns(self) -> dict:
        """
        Analyze task completion timing relative to deadlines.

        Returns patterns like:
        - What percentage of tasks are completed late, on-time, early
        - Which projects have the worst on-time rates
        - Examples of each timing category

        Returns:
            Dict with timing distribution and project-level patterns
        """
        from cognitex.db.neo4j import run_query

        patterns = {
            "timing_distribution": {},
            "by_project": {},
            "summary": {},
        }

        try:
            # Overall timing distribution
            query = """
            MATCH (t:Task)
            WHERE t.completed_at IS NOT NULL AND t.due IS NOT NULL
            WITH t,
                 duration.between(datetime(t.completed_at), datetime(t.due)).days as days_before
            RETURN
                CASE
                    WHEN days_before < -7 THEN 'very_late'
                    WHEN days_before < 0 THEN 'late'
                    WHEN days_before = 0 THEN 'day_of'
                    WHEN days_before <= 1 THEN 'last_minute'
                    WHEN days_before <= 3 THEN 'comfortable'
                    ELSE 'early'
                END as timing,
                count(*) as count,
                collect(t.title)[0..3] as examples
            ORDER BY
                CASE timing
                    WHEN 'very_late' THEN 1
                    WHEN 'late' THEN 2
                    WHEN 'day_of' THEN 3
                    WHEN 'last_minute' THEN 4
                    WHEN 'comfortable' THEN 5
                    WHEN 'early' THEN 6
                END
            """
            data = await run_query(query)

            total = sum(row["count"] for row in data)
            for row in data:
                patterns["timing_distribution"][row["timing"]] = {
                    "count": row["count"],
                    "percentage": round(row["count"] / total * 100, 1) if total > 0 else 0,
                    "examples": row["examples"],
                }

            # By project patterns
            project_query = """
            MATCH (t:Task)-[:BELONGS_TO|PART_OF]->(p:Project)
            WHERE t.completed_at IS NOT NULL AND t.due IS NOT NULL
            WITH p, t,
                 duration.between(datetime(t.completed_at), datetime(t.due)).days as days_before
            WITH p,
                 count(*) as total,
                 count(CASE WHEN days_before >= 0 THEN 1 END) as on_time,
                 avg(days_before) as avg_days_before
            WHERE total >= 3
            RETURN
                p.id as project_id,
                p.title as project_title,
                total,
                on_time,
                round(on_time * 100.0 / total) as on_time_rate,
                round(avg_days_before * 10) / 10 as avg_days_margin
            ORDER BY on_time_rate ASC
            LIMIT 10
            """
            project_data = await run_query(project_query)

            for row in project_data:
                patterns["by_project"][row["project_id"]] = {
                    "title": row["project_title"],
                    "total": row["total"],
                    "on_time": row["on_time"],
                    "on_time_rate": row["on_time_rate"],
                    "avg_days_margin": row["avg_days_margin"],
                }

            # Summary stats
            late_count = sum(
                patterns["timing_distribution"].get(t, {}).get("count", 0)
                for t in ["very_late", "late"]
            )
            on_time_count = sum(
                patterns["timing_distribution"].get(t, {}).get("count", 0)
                for t in ["day_of", "last_minute", "comfortable", "early"]
            )

            patterns["summary"] = {
                "total_with_deadlines": total,
                "late_count": late_count,
                "on_time_count": on_time_count,
                "on_time_rate": round(on_time_count / total * 100, 1) if total > 0 else 0,
                "last_minute_rate": round(
                    patterns["timing_distribution"].get("last_minute", {}).get("count", 0)
                    / total
                    * 100,
                    1,
                )
                if total > 0
                else 0,
            }

            logger.debug("Computed deadline patterns", summary=patterns["summary"])

        except Exception as e:
            logger.warning("Failed to compute deadline patterns", error=str(e))

        return patterns

    async def get_deferral_patterns(self) -> dict:
        """
        Analyze task deferral patterns to identify procrastination signals.

        Returns:
            Dict with deferral statistics by project, priority, and friction level
        """
        patterns = {
            "overall": {},
            "by_project": {},
            "high_deferral_tasks": [],
        }

        try:
            # Overall deferral stats (using tasks table directly via PostgreSQL)
            from cognitex.db.postgres import get_session
            from sqlalchemy import text

            async for session in get_session():
                # Overall stats
                result = await session.execute(
                    text("""
                    SELECT
                        COUNT(*) as total,
                        COUNT(*) FILTER (WHERE deferral_count > 0) as deferred_any,
                        COUNT(*) FILTER (WHERE deferral_count >= 2) as deferred_multiple,
                        COUNT(*) FILTER (WHERE deferral_count >= 3) as chronic_deferrals,
                        AVG(deferral_count) FILTER (WHERE deferral_count > 0) as avg_deferrals
                    FROM tasks
                    WHERE status IN ('pending', 'in_progress', 'completed')
                """)
                )
                row = result.fetchone()
                if row:
                    patterns["overall"] = {
                        "total_tasks": row.total or 0,
                        "deferred_any": row.deferred_any or 0,
                        "deferred_multiple": row.deferred_multiple or 0,
                        "chronic_deferrals": row.chronic_deferrals or 0,
                        "deferral_rate": round((row.deferred_any or 0) / row.total * 100, 1)
                        if row.total > 0
                        else 0,
                        "avg_deferrals_when_deferred": round(row.avg_deferrals or 0, 1),
                    }

                # High deferral tasks (still pending)
                result = await session.execute(
                    text("""
                    SELECT id, title, deferral_count, project_id, priority, estimated_minutes
                    FROM tasks
                    WHERE status = 'pending'
                      AND deferral_count >= 2
                    ORDER BY deferral_count DESC
                    LIMIT 10
                """)
                )
                patterns["high_deferral_tasks"] = [
                    {
                        "id": row.id,
                        "title": row.title,
                        "deferral_count": row.deferral_count,
                        "project_id": row.project_id,
                        "priority": row.priority,
                        "estimated_minutes": row.estimated_minutes,
                    }
                    for row in result.fetchall()
                ]
                break

            logger.debug("Computed deferral patterns", overall=patterns["overall"])

        except Exception as e:
            logger.warning("Failed to compute deferral patterns", error=str(e))

        return patterns

    async def get_learning_summary(self) -> dict:
        """
        Get a comprehensive summary of all learned patterns for briefings.

        Combines:
        - Deadline patterns
        - Deferral patterns
        - Proposal acceptance patterns

        Returns:
            Dict with key insights for the daily briefing
        """
        from cognitex.agent.action_log import get_proposal_patterns

        summary = {
            "deadline_patterns": await self.get_deadline_patterns(),
            "deferral_patterns": await self.get_deferral_patterns(),
            "proposal_patterns": await get_proposal_patterns(),
            "insights": [],
        }

        # Generate actionable insights
        insights = []

        # Deadline insight
        deadline_summary = summary["deadline_patterns"].get("summary", {})
        on_time_rate = deadline_summary.get("on_time_rate", 100)
        last_minute_rate = deadline_summary.get("last_minute_rate", 0)

        if on_time_rate < 80:
            insights.append(
                f"Only {on_time_rate:.0f}% of tasks with deadlines are completed on time. "
                f"Consider earlier reminders or smaller task chunks."
            )
        if last_minute_rate > 50:
            insights.append(
                f"{last_minute_rate:.0f}% of tasks are completed last-minute. "
                f"This suggests deadlines might need more buffer."
            )

        # Deferral insight
        deferral_summary = summary["deferral_patterns"].get("overall", {})
        deferral_rate = deferral_summary.get("deferral_rate", 0)
        chronic_count = deferral_summary.get("chronic_deferrals", 0)

        if deferral_rate > 30:
            insights.append(
                f"{deferral_rate:.0f}% of tasks get deferred at least once. "
                f"Consider adding MVS (minimum viable start) to new tasks."
            )
        if chronic_count > 0:
            insights.append(
                f"{chronic_count} tasks have been deferred 3+ times. "
                f"These may need decomposition or re-evaluation."
            )

        # Proposal insight
        proposal_overall = summary["proposal_patterns"].get("overall", {})
        approval_rate = proposal_overall.get("approval_rate", 50)

        if approval_rate < 50 and proposal_overall.get("decided", 0) >= 5:
            insights.append(
                f"Proposal approval rate is {approval_rate:.0f}%. "
                f"Consider more specific task proposals or different priorities."
            )

        summary["insights"] = insights

        return summary

    # =========================================================================
    # Phase 5.2: Intelligent Calendar Blocking
    # =========================================================================

    async def get_focus_block_suggestions(
        self,
        max_suggestions: int = 3,
    ) -> list[dict]:
        """
        Analyze tasks and suggest optimal focus blocks for deep work.

        Considers:
        - Tasks that need focused time (high energy cost, complex)
        - Upcoming deadlines that need attention
        - User's temporal energy patterns (peak hours)
        - Current calendar availability

        Returns:
            List of suggested focus blocks with timing, duration, and target task/project
        """
        from cognitex.db.neo4j import run_query
        from datetime import datetime, timedelta

        suggestions = []

        try:
            # 1. Get high-energy tasks that need focus time
            focus_tasks_query = """
            MATCH (t:Task)
            WHERE t.status IN ['pending', 'in_progress']
              AND (
                  t.energy_cost >= 3 OR
                  t.focus_required = true OR
                  (t.due IS NOT NULL AND t.due <= date() + duration({days: 3}))
              )
            OPTIONAL MATCH (t)-[:BELONGS_TO]->(p:Project)
            OPTIONAL MATCH (p)-[:PART_OF]->(g:Goal)
            RETURN
                t.id as task_id,
                t.title as task_title,
                t.status as status,
                t.due as due_date,
                t.energy_cost as energy_cost,
                t.estimated_minutes as estimated_minutes,
                p.id as project_id,
                p.title as project_title,
                g.title as goal_title,
                CASE
                    WHEN t.due IS NOT NULL AND t.due <= date() THEN 5
                    WHEN t.due IS NOT NULL AND t.due <= date() + duration({days: 1}) THEN 4
                    WHEN t.due IS NOT NULL AND t.due <= date() + duration({days: 3}) THEN 3
                    WHEN t.energy_cost >= 4 THEN 2
                    ELSE 1
                END as urgency_score
            ORDER BY urgency_score DESC, t.energy_cost DESC
            LIMIT 10
            """
            focus_tasks = await run_query(focus_tasks_query, {})

            if not focus_tasks:
                return []

            # 2. Get user's peak hours from temporal model
            try:
                from cognitex.agent.state_model import get_temporal_model

                temporal = get_temporal_model()
                peak_hours = temporal.get_peak_hours()
            except Exception:
                peak_hours = [9, 10, 11, 14, 15]  # Default peak hours

            # 3. Check calendar for free slots
            try:
                from cognitex.services.calendar import CalendarService
                import asyncio

                cal = CalendarService()
                now = datetime.now()
                tomorrow = now + timedelta(days=1)

                # Get events for next 3 days
                events_result = await asyncio.to_thread(
                    cal.list_events,
                    time_min=now,
                    time_max=now + timedelta(days=3),
                )
                busy_slots = []
                for event in events_result.get("items", []):
                    start = event.get("start", {}).get("dateTime")
                    end = event.get("end", {}).get("dateTime")
                    if start and end:
                        busy_slots.append(
                            {
                                "start": datetime.fromisoformat(
                                    start.replace("Z", "+00:00").replace("+00:00", "")
                                ),
                                "end": datetime.fromisoformat(
                                    end.replace("Z", "+00:00").replace("+00:00", "")
                                ),
                            }
                        )
            except Exception as e:
                logger.debug("Could not check calendar", error=str(e))
                busy_slots = []

            # 4. Generate suggestions for top tasks
            for i, task in enumerate(focus_tasks[:max_suggestions]):
                # Determine duration
                est_minutes = task.get("estimated_minutes") or 90
                duration_hours = min(max(est_minutes / 60, 1), 3)  # 1-3 hours

                # Find best time slot
                suggested_time = self._find_best_slot(
                    peak_hours=peak_hours,
                    busy_slots=busy_slots,
                    duration_hours=duration_hours,
                    days_ahead=i,  # Spread across days
                )

                suggestions.append(
                    {
                        "task_id": task.get("task_id"),
                        "task_title": task.get("task_title"),
                        "project_id": task.get("project_id"),
                        "project_title": task.get("project_title"),
                        "goal_title": task.get("goal_title"),
                        "suggested_time": suggested_time,
                        "duration_hours": duration_hours,
                        "urgency_score": task.get("urgency_score"),
                        "reason": self._generate_block_reason(task),
                    }
                )

            return suggestions

        except Exception as e:
            logger.warning("Failed to get focus block suggestions", error=str(e))
            return []

    def _find_best_slot(
        self,
        peak_hours: list[int],
        busy_slots: list[dict],
        duration_hours: float,
        days_ahead: int = 0,
    ) -> str:
        """Find the best available slot for a focus block."""
        from datetime import datetime, timedelta

        base_date = datetime.now().date() + timedelta(days=days_ahead + 1)

        # Try peak hours first, then fall back to any free time
        for hour in peak_hours + list(range(8, 18)):
            candidate_start = datetime.combine(base_date, datetime.min.time().replace(hour=hour))
            candidate_end = candidate_start + timedelta(hours=duration_hours)

            # Check if this slot is free
            is_free = True
            for slot in busy_slots:
                if candidate_start < slot["end"] and candidate_end > slot["start"]:
                    is_free = False
                    break

            if is_free:
                return candidate_start.isoformat()

        # Fallback to next day morning
        fallback = datetime.combine(
            base_date + timedelta(days=1), datetime.min.time().replace(hour=9)
        )
        return fallback.isoformat()

    def _generate_block_reason(self, task: dict) -> str:
        """Generate a reason string for why this focus block is suggested."""
        reasons = []

        if task.get("due_date"):
            reasons.append(f"deadline approaching ({task['due_date']})")
        if task.get("energy_cost", 0) >= 4:
            reasons.append("high complexity task")
        if task.get("goal_title"):
            reasons.append(f"contributes to goal: {task['goal_title']}")
        if task.get("project_title"):
            reasons.append(f"for project: {task['project_title']}")

        return "; ".join(reasons) if reasons else "needs focused attention"

    # =========================================================================
    # Phase 5.3: Proactive Task Suggestions
    # =========================================================================

    async def get_proactive_task_insights(self) -> dict:
        """
        Analyze task/project state to generate proactive suggestions.

        Identifies:
        - Stalled projects (no activity 7+ days)
        - Large tasks needing breakdown
        - Tasks with dependencies that are blocking others
        - Chronically deferred tasks

        Returns:
            Dict with categorized insights and suggested actions
        """
        from cognitex.db.neo4j import run_query
        from datetime import datetime, timedelta

        insights = {
            "stalled_projects": [],
            "large_tasks_needing_breakdown": [],
            "blocking_tasks": [],
            "chronic_deferrals": [],
            "summary": {},
        }

        try:
            # 1. Stalled projects (no task updates in 7+ days)
            stalled_query = """
            MATCH (p:Project)
            WHERE p.status IN ['active', 'in_progress']
            OPTIONAL MATCH (p)<-[:BELONGS_TO]-(t:Task)
            WITH p, MAX(t.updated_at) as last_task_update, COUNT(t) as task_count
            WHERE
                last_task_update IS NULL OR
                last_task_update < datetime() - duration({days: 7})
            OPTIONAL MATCH (p)-[:PART_OF]->(g:Goal)
            RETURN
                p.id as id,
                p.title as title,
                p.status as status,
                g.title as goal_title,
                task_count,
                last_task_update,
                duration.between(COALESCE(last_task_update, p.created_at), datetime()).days as days_stalled
            ORDER BY days_stalled DESC
            LIMIT 5
            """
            insights["stalled_projects"] = await run_query(stalled_query, {})

            # 2. Large tasks that might need breakdown
            large_tasks_query = """
            MATCH (t:Task)
            WHERE t.status IN ['pending', 'in_progress']
              AND (
                  t.estimated_minutes > 180 OR
                  t.energy_cost >= 4 OR
                  t.complexity >= 4
              )
            OPTIONAL MATCH (t)-[:BELONGS_TO]->(p:Project)
            OPTIONAL MATCH (t)<-[:SUBTASK_OF]-(subtask:Task)
            WITH t, p, COUNT(subtask) as subtask_count
            WHERE subtask_count = 0  // No existing subtasks
            RETURN
                t.id as id,
                t.title as title,
                t.estimated_minutes as estimated_minutes,
                t.energy_cost as energy_cost,
                t.complexity as complexity,
                p.title as project_title,
                CASE
                    WHEN t.estimated_minutes > 180 THEN 'Duration > 3 hours'
                    WHEN t.energy_cost >= 4 THEN 'High energy cost'
                    ELSE 'High complexity'
                END as breakdown_reason
            ORDER BY
                COALESCE(t.estimated_minutes, 0) +
                COALESCE(t.energy_cost, 0) * 30 +
                COALESCE(t.complexity, 0) * 30 DESC
            LIMIT 5
            """
            insights["large_tasks_needing_breakdown"] = await run_query(large_tasks_query, {})

            # 3. Tasks that are blocking others
            blocking_query = """
            MATCH (blocker:Task)-[:BLOCKS]->(blocked:Task)
            WHERE blocker.status IN ['pending', 'in_progress']
              AND blocked.status IN ['pending', 'blocked']
            WITH blocker, COUNT(blocked) as blocks_count,
                 COLLECT(blocked.title)[0..3] as blocked_tasks
            WHERE blocks_count > 0
            OPTIONAL MATCH (blocker)-[:BELONGS_TO]->(p:Project)
            RETURN
                blocker.id as id,
                blocker.title as title,
                blocker.status as status,
                blocker.due as due_date,
                blocks_count,
                blocked_tasks,
                p.title as project_title
            ORDER BY blocks_count DESC
            LIMIT 5
            """
            insights["blocking_tasks"] = await run_query(blocking_query, {})

            # 4. Chronically deferred tasks (3+ deferrals)
            deferred_query = """
            MATCH (t:Task)
            WHERE t.status IN ['pending', 'in_progress', 'deferred']
              AND t.defer_count >= 3
            OPTIONAL MATCH (t)-[:BELONGS_TO]->(p:Project)
            RETURN
                t.id as id,
                t.title as title,
                t.defer_count as defer_count,
                t.last_deferred_at as last_deferred,
                t.original_due as original_due,
                p.title as project_title,
                CASE
                    WHEN t.defer_count >= 5 THEN 'Consider removing or redesigning'
                    WHEN t.defer_count >= 3 THEN 'May need different approach'
                    ELSE 'Monitor'
                END as suggestion
            ORDER BY t.defer_count DESC
            LIMIT 5
            """
            insights["chronic_deferrals"] = await run_query(deferred_query, {})

            # Build summary
            insights["summary"] = {
                "stalled_project_count": len(insights["stalled_projects"]),
                "large_task_count": len(insights["large_tasks_needing_breakdown"]),
                "blocking_task_count": len(insights["blocking_tasks"]),
                "chronic_deferral_count": len(insights["chronic_deferrals"]),
                "needs_attention": (
                    len(insights["stalled_projects"]) > 0
                    or len(insights["blocking_tasks"]) > 0
                    or len(insights["chronic_deferrals"]) >= 3
                ),
            }

            return insights

        except Exception as e:
            logger.warning("Failed to get proactive task insights", error=str(e))
            return insights

    async def extract_commitments_from_emails(
        self,
        days_back: int = 7,
        limit: int = 10,
    ) -> list[dict]:
        """
        Extract implied commitments from recent sent emails.

        Creates Commitment nodes in Neo4j and inbox items for user approval.

        Returns:
            List of created commitments with source email and extracted commitment
        """
        import json
        import uuid

        from cognitex.db.neo4j import run_query
        from cognitex.services.llm import get_llm_service

        try:
            # Get recent sent emails
            sent_query = """
            MATCH (e:Email)
            WHERE e.is_sent = true
              AND e.date > datetime() - duration({days: $days_back})
            RETURN
                e.gmail_id as gmail_id,
                e.subject as subject,
                coalesce(e.body, e.snippet) as body,
                e.date as date,
                e.to as recipient
            ORDER BY e.date DESC
            LIMIT $limit
            """
            sent_emails = await run_query(sent_query, {"days_back": days_back, "limit": limit})

            if not sent_emails:
                return []

            # Load commitment-extraction skill for prompt guidance
            skill_content = ""
            try:
                from cognitex.agent.skills import get_skills_loader

                loader = get_skills_loader()
                skill = await loader.get_skill("commitment-extraction")
                if skill:
                    skill_content = skill.raw_content
            except Exception:
                pass

            llm = get_llm_service()
            commitments = []

            for email in sent_emails:
                body = email.get("body", "")
                if not body or len(body) < 50:
                    continue

                # Quick heuristic check for commitment language
                commitment_keywords = [
                    "i'll",
                    "i will",
                    "let me",
                    "i can",
                    "i should",
                    "by tomorrow",
                    "by friday",
                    "by monday",
                    "this week",
                    "get back to you",
                    "send you",
                    "follow up",
                ]
                body_lower = body.lower()
                if not any(kw in body_lower for kw in commitment_keywords):
                    continue

                gmail_id = email.get("gmail_id", "")

                # Deduplicate: skip if commitment already exists for this email
                existing_query = """
                MATCH (c:Commitment {source_id: $gmail_id})
                RETURN c.id as id LIMIT 1
                """
                existing = await run_query(existing_query, {"gmail_id": gmail_id})
                if existing:
                    continue

                # Build extraction prompt with skill guidance
                if skill_content:
                    extract_prompt = f"""{skill_content}

---

Analyze this sent email and extract commitments per the rules above.

EMAIL TO: {email.get("recipient", "Unknown")}
SUBJECT: {email.get("subject", "No subject")}

BODY:
{body[:2000]}

Return ONLY the JSON, nothing else.
"""
                else:
                    extract_prompt = f"""Analyze this sent email and extract any commitments or promises made.

EMAIL TO: {email.get("recipient", "Unknown")}
SUBJECT: {email.get("subject", "No subject")}

BODY:
{body[:2000]}

If commitments exist, return JSON:
{{"commitments": [{{"action": "what was promised", "deadline": "ISO date or null", "deadline_source": "explicit|inferred|none", "recipient": "to whom", "cognitive_load": "high|medium|low", "confidence": 0.0-1.0}}]}}

If no commitments, return: {{"commitments": []}}

Return ONLY the JSON, nothing else.
"""

                try:
                    result = await llm.complete(extract_prompt, max_tokens=500)
                    parsed = json.loads(result.strip())

                    for commitment_data in parsed.get("commitments", []):
                        commitment_id = f"commit_{uuid.uuid4().hex[:12]}"
                        action = commitment_data.get("action", "")
                        deadline = commitment_data.get("deadline")
                        recipient = commitment_data.get("recipient") or email.get("recipient")
                        cognitive_load = commitment_data.get("cognitive_load", "medium")

                        # Create Commitment node
                        try:
                            from cognitex.db.graph_schema import (
                                create_commitment,
                                link_commitment_to_email,
                                link_commitment_to_person,
                            )
                            from cognitex.db.neo4j import get_neo4j_session

                            async for session in get_neo4j_session():
                                await create_commitment(
                                    session,
                                    commitment_id=commitment_id,
                                    task_description=action,
                                    owner=recipient or "",
                                    status="pending",
                                    deadline=deadline,
                                    cognitive_load=cognitive_load,
                                    source="email",
                                    source_id=gmail_id,
                                )
                                await link_commitment_to_email(session, commitment_id, gmail_id)
                                if recipient:
                                    await link_commitment_to_person(
                                        session, commitment_id, recipient
                                    )
                                break
                        except Exception as e:
                            logger.warning(
                                "Failed to create commitment node",
                                error=str(e),
                                commitment_id=commitment_id,
                            )

                        # Create inbox item for user approval
                        try:
                            from cognitex.services.inbox import get_inbox_service

                            inbox = get_inbox_service()
                            await inbox.create_item(
                                item_type="commitment_proposal",
                                title=f"Commitment: {action[:80]}",
                                summary=(f"From email: {email.get('subject', 'No subject')}"),
                                payload={
                                    "commitment_id": commitment_id,
                                    "action": action,
                                    "deadline": deadline,
                                    "deadline_source": commitment_data.get(
                                        "deadline_source", "none"
                                    ),
                                    "recipient": recipient,
                                    "cognitive_load": cognitive_load,
                                    "confidence": commitment_data.get("confidence", 0.5),
                                    "source_email_subject": email.get("subject"),
                                    "source_email_id": gmail_id,
                                },
                                source_id=commitment_id,
                                source_type="commitment",
                                priority="normal",
                            )
                        except Exception as e:
                            logger.warning(
                                "Failed to create inbox item for commitment",
                                error=str(e),
                            )

                        commitments.append(
                            {
                                "commitment_id": commitment_id,
                                "source_email_id": gmail_id,
                                "email_subject": email.get("subject"),
                                "email_date": email.get("date"),
                                "action": action,
                                "deadline": deadline,
                                "recipient": recipient,
                                "cognitive_load": cognitive_load,
                            }
                        )
                except Exception:
                    continue

            return commitments[:10]

        except Exception as e:
            logger.warning("Failed to extract commitments", error=str(e))
            return []

    async def get_approaching_commitments(self, hours: int = 48) -> list[dict]:
        """Get active commitments with deadlines within the next N hours."""
        from cognitex.db.neo4j import run_query

        query = """
        MATCH (c:Commitment)
        WHERE c.status IN ['accepted', 'in_progress']
          AND c.deadline IS NOT NULL
          AND c.deadline > datetime()
          AND c.deadline <= datetime() + duration({hours: $hours})
        OPTIONAL MATCH (c)-[:COMMITTED_TO]->(p:Project)
        OPTIONAL MATCH (c)-[:WAITING_ON]->(w:Person)
        RETURN
            c.id as id,
            c.task_description as description,
            c.deadline as deadline,
            c.status as status,
            c.owner as owner,
            c.cognitive_load as cognitive_load,
            p.title as project,
            w.email as waiting_on
        ORDER BY c.deadline ASC
        """
        try:
            return await run_query(query, {"hours": hours})
        except Exception as e:
            logger.warning("Failed to get approaching commitments", error=str(e))
            return []

    async def get_overdue_commitments(self) -> list[dict]:
        """Get active commitments with deadlines in the past."""
        from cognitex.db.neo4j import run_query

        query = """
        MATCH (c:Commitment)
        WHERE c.status IN ['accepted', 'in_progress']
          AND c.deadline IS NOT NULL
          AND c.deadline < datetime()
        OPTIONAL MATCH (c)-[:COMMITTED_TO]->(p:Project)
        OPTIONAL MATCH (c)-[:WAITING_ON]->(w:Person)
        RETURN
            c.id as id,
            c.task_description as description,
            c.deadline as deadline,
            c.status as status,
            c.owner as owner,
            c.cognitive_load as cognitive_load,
            p.title as project,
            w.email as waiting_on
        ORDER BY c.deadline ASC
        """
        try:
            return await run_query(query, {})
        except Exception as e:
            logger.warning("Failed to get overdue commitments", error=str(e))
            return []

    async def get_stale_commitments(self, days: int = 7) -> list[dict]:
        """Get blocked or waiting_on commitments not updated in N days."""
        from cognitex.db.neo4j import run_query

        query = """
        MATCH (c:Commitment)
        WHERE c.status IN ['blocked', 'waiting_on']
          AND c.updated_at < datetime() - duration({days: $days})
        OPTIONAL MATCH (c)-[:COMMITTED_TO]->(p:Project)
        OPTIONAL MATCH (c)-[:WAITING_ON]->(w:Person)
        RETURN
            c.id as id,
            c.task_description as description,
            c.deadline as deadline,
            c.status as status,
            c.owner as owner,
            c.cognitive_load as cognitive_load,
            c.updated_at as last_updated,
            p.title as project,
            w.email as waiting_on
        ORDER BY c.updated_at ASC
        """
        try:
            return await run_query(query, {"days": days})
        except Exception as e:
            logger.warning("Failed to get stale commitments", error=str(e))
            return []
