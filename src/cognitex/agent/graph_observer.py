"""
Graph Observer - Queries to understand the current state of the knowledge graph.

Provides the autonomous agent with visibility into:
- Recent changes (new nodes, updates)
- Stale items (tasks not touched, inactive projects)
- Orphaned nodes (unlinked documents, disconnected entities)
- Goal/project health metrics
- Opportunities for new connections
"""

from datetime import datetime, timedelta
from typing import Any

import structlog

logger = structlog.get_logger()


class GraphObserver:
    """Observes and reports on the state of the knowledge graph."""

    def __init__(self, session):
        """Initialize with a Neo4j session."""
        self.session = session

    async def get_full_context(self) -> dict:
        """Gather comprehensive context about the graph state."""
        context = {
            "timestamp": datetime.now().isoformat(),
            "summary": {},
            # Graph health metrics
            "recent_changes": await self.get_recent_changes(),
            "stale_items": await self.get_stale_items(),
            "orphaned_nodes": await self.get_orphaned_nodes(),
            "goal_health": await self.get_goal_health(),
            "project_health": await self.get_project_health(),
            "pending_tasks": await self.get_pending_tasks(),
            "recent_documents": await self.get_recent_documents(),
            "connection_opportunities": await self.get_connection_opportunities(),
            # Digital twin perception
            "writing_samples": await self.get_user_writing_samples(),
            "pending_emails": await self.get_actionable_emails(),
            "upcoming_calendar": await self.get_pending_calendar_blocks(),
            # Already-actioned items (to prevent re-suggesting)
            "projects_with_recent_blocks": await self.get_projects_with_recent_blocks(),
        }

        # Build summary
        context["summary"] = {
            # Graph health
            "total_changes_24h": len(context["recent_changes"]),
            "stale_tasks": len([i for i in context["stale_items"] if i["type"] == "Task"]),
            "stale_projects": len([i for i in context["stale_items"] if i["type"] == "Project"]),
            "orphaned_documents": len([n for n in context["orphaned_nodes"] if n["type"] == "Document"]),
            "goals_needing_attention": len([g for g in context["goal_health"] if g.get("needs_attention")]),
            "projects_needing_attention": len([p for p in context["project_health"] if p.get("needs_attention")]),
            "pending_task_count": len(context["pending_tasks"]),
            "connection_opportunities": len(context["connection_opportunities"]),
            # Digital twin actionable items
            "emails_needing_response": len(context["pending_emails"]),
            "meetings_needing_prep": len([c for c in context["upcoming_calendar"] if c.get("needs_context")]),
            "has_writing_samples": len(context["writing_samples"]) > 0,
        }

        return context

    async def get_recent_changes(self, hours: int = 24) -> list[dict]:
        """Get nodes that were created or updated recently."""
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
            result = await self.session.run(query, {"hours": hours})
            data = await result.data()
            return data
        except Exception as e:
            logger.warning("Failed to get recent changes", error=str(e))
            return []

    async def get_stale_items(self, task_days: int = 7, project_days: int = 14) -> list[dict]:
        """Get tasks and projects that haven't been updated recently."""
        query = """
        // Stale pending tasks
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

        UNION ALL

        // Stale active projects
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
        """
        try:
            result = await self.session.run(query, {
                "task_days": task_days,
                "project_days": project_days
            })
            data = await result.data()
            return data
        except Exception as e:
            logger.warning("Failed to get stale items", error=str(e))
            return []

    async def get_orphaned_nodes(self) -> list[dict]:
        """Get nodes that should have connections but don't."""
        query = """
        // Documents not linked to any project
        MATCH (d:Document)
        WHERE NOT (d)-[:BELONGS_TO|REFERENCES|MENTIONED_IN]->(:Project)
          AND NOT (d)-[:BELONGS_TO|REFERENCES|MENTIONED_IN]->(:Goal)
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
            result = await self.session.run(query)
            data = await result.data()
            return data
        except Exception as e:
            logger.warning("Failed to get orphaned nodes", error=str(e))
            return []

    async def get_goal_health(self) -> list[dict]:
        """Assess health of each active goal."""
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
            result = await self.session.run(query)
            data = await result.data()
            return data
        except Exception as e:
            logger.warning("Failed to get goal health", error=str(e))
            return []

    async def get_project_health(self) -> list[dict]:
        """Assess health of each active project."""
        query = """
        MATCH (p:Project)
        WHERE p.status = 'active'
        OPTIONAL MATCH (p)<-[:BELONGS_TO]-(t:Task)
        OPTIONAL MATCH (p)-[:PART_OF]->(g:Goal)
        WITH p, g,
             collect(t) as tasks
        WITH p, g,
             size(tasks) as total_tasks,
             size([t IN tasks WHERE t.status = 'completed']) as completed_tasks,
             size([t IN tasks WHERE t.status = 'in_progress']) as in_progress_tasks,
             size([t IN tasks WHERE t.status = 'pending']) as pending_tasks,
             [t IN tasks WHERE t.due IS NOT NULL AND t.due < date() AND t.status <> 'completed'] as overdue_tasks
        RETURN
            p.id as id,
            p.title as title,
            g.title as goal_title,
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
            result = await self.session.run(query)
            data = await result.data()
            return data
        except Exception as e:
            logger.warning("Failed to get project health", error=str(e))
            return []

    async def get_pending_tasks(self, limit: int = 30) -> list[dict]:
        """Get pending tasks with context."""
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
            result = await self.session.run(query, {"limit": limit})
            data = await result.data()
            return data
        except Exception as e:
            logger.warning("Failed to get pending tasks", error=str(e))
            return []

    async def get_recent_documents(self, days: int = 7, limit: int = 20) -> list[dict]:
        """Get recently added/modified documents."""
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
            result = await self.session.run(query, {"days": days, "limit": limit})
            data = await result.data()
            return data
        except Exception as e:
            logger.warning("Failed to get recent documents", error=str(e))
            return []

    async def get_connection_opportunities(self) -> list[dict]:
        """Find opportunities to create new connections based on names, topics, and content."""
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

        for query in queries:
            try:
                result = await self.session.run(query)
                data = await result.data()
                opportunities.extend(data)
            except Exception as e:
                logger.warning("Connection opportunity query failed", error=str(e))

        # Sort by relevance and deduplicate
        opportunities.sort(key=lambda x: x.get('relevance_score', 0), reverse=True)
        return opportunities[:30]

    async def get_graph_stats(self) -> dict:
        """Get overall graph statistics."""
        query = """
        MATCH (n)
        WITH labels(n)[0] as label, count(*) as count
        RETURN label, count
        ORDER BY count DESC
        """
        try:
            result = await self.session.run(query)
            data = await result.data()
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
        # Get user email for fallback matching
        from cognitex.services.ingestion import get_user_email
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
            result = await self.session.run(query, {"limit": limit, "user_email": user_email})
            data = await result.data()
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
        query = """
        MATCH (e:Email)
        WHERE e.classification IN ['actionable', 'urgent']
          AND e.date >= datetime() - duration({days: $days_back})
          AND NOT e.classification IN ['automated', 'marketing', 'newsletter', 'informational']
          AND NOT (e)<-[:REPLY_TO]-(:Email)
          AND NOT (e)<-[:REPLY_TO]-(:EmailDraft)
          AND NOT (e)<-[:DERIVED_FROM]-(:Task)
          AND NOT (e)-[:SENT_BY]->(:Person {is_user: true})
        OPTIONAL MATCH (e)-[:SENT_BY]->(sender:Person)
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
            result = await self.session.run(query, {"limit": limit, "days_back": days_back})
            data = await result.data()
            return data
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
            result = await self.session.run(query, {"days_ahead": days_ahead})
            data = await result.data()
            return data
        except Exception as e:
            logger.warning("Failed to get pending calendar blocks", error=str(e))
            return []

    async def get_projects_with_recent_blocks(self, days: int = 7) -> set[str]:
        """
        Get project IDs that have had focus blocks suggested recently.

        Used to prevent the agent from repeatedly suggesting blocks for
        the same projects.
        """
        query = """
        MATCH (sb:SuggestedBlock)-[:FOR_PROJECT]->(p:Project)
        WHERE sb.created_at > datetime() - duration({days: $days})
        RETURN DISTINCT p.id as project_id
        """
        try:
            result = await self.session.run(query, {"days": days})
            data = await result.data()
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
            result = await self.session.run(query, params)
            data = await result.data()
            return data
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
            result = await self.session.run(email_query, {"gmail_id": gmail_id})
            email_data = await result.single()

            if not email_data:
                logger.warning("Email not found in graph", gmail_id=gmail_id)
                return context

            email_dict = dict(email_data)
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
                thread_result = await self.session.run(
                    thread_query, {"thread_id": thread_id, "gmail_id": gmail_id}
                )
                thread_data = await thread_result.data()
                context["thread_history"] = thread_data

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
                            doc_result = await self.session.run(doc_query, {"drive_id": drive_id})
                            doc_data = await doc_result.single()
                            if doc_data:
                                context["related_documents"].append({
                                    **dict(doc_data),
                                    "relevance": chunk.get("similarity", 0.5),
                                    "matched_content": chunk.get("content", "")[:200],
                                })
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
                    (w for w in subject.split() if len(w) > 4 and w.lower() not in ["about", "follow", "update"]),
                    subject[:20]
                )
                task_result = await self.session.run(task_query, {
                    "gmail_id": gmail_id,
                    "sender_email": sender_email,
                    "subject_keyword": subject_keyword,
                })
                task_data = await task_result.data()
                context["related_tasks"] = task_data

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
                sender_result = await self.session.run(sender_query, {"email": sender_email})
                sender_data = await sender_result.single()
                if sender_data:
                    context["sender_context"] = dict(sender_data)

            # 7. Extract action items from the email body using simple pattern matching
            if context["full_body"]:
                action_patterns = [
                    "please ", "could you ", "can you ", "would you ",
                    "need to ", "should ", "must ", "by ", "deadline",
                    "action required", "follow up", "let me know",
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
