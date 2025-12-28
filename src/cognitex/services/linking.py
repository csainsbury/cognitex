"""Project/Task linking service - connects content to projects and tasks.

This service manages:
1. Folder-to-project mapping rules (auto-link documents in specific folders)
2. Contact-to-project associations (emails from project members)
3. Suggested links queue (AI-inferred links awaiting approval)
4. Link creation and management
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from cognitex.db.neo4j import get_neo4j_session, get_driver
from cognitex.db import graph_schema as gs

logger = structlog.get_logger()


class LinkType(str, Enum):
    """Types of content that can be linked to projects/tasks."""
    DOCUMENT = "document"
    EMAIL = "email"
    FOLDER = "folder"
    CONTACT = "contact"
    REPOSITORY = "repository"


class LinkStatus(str, Enum):
    """Status of suggested links."""
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    AUTO = "auto"  # Auto-created via rules


# SQL statements for linking tables (one per execute for asyncpg compatibility)
LINKING_STATEMENTS = [
    # Folder-to-project mapping rules
    """CREATE TABLE IF NOT EXISTS folder_project_mappings (
        id TEXT PRIMARY KEY,
        folder_id TEXT NOT NULL,
        folder_name TEXT,
        folder_path TEXT,
        project_id TEXT NOT NULL,
        project_title TEXT,
        is_active BOOLEAN DEFAULT true,
        auto_link_new_files BOOLEAN DEFAULT true,
        created_at TIMESTAMP DEFAULT NOW(),
        updated_at TIMESTAMP DEFAULT NOW(),
        UNIQUE(folder_id, project_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_folder_mappings_folder ON folder_project_mappings(folder_id)",
    "CREATE INDEX IF NOT EXISTS idx_folder_mappings_project ON folder_project_mappings(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_folder_mappings_active ON folder_project_mappings(is_active) WHERE is_active = true",

    # Contact-to-project associations
    """CREATE TABLE IF NOT EXISTS contact_project_mappings (
        id TEXT PRIMARY KEY,
        contact_email TEXT NOT NULL,
        contact_name TEXT,
        project_id TEXT NOT NULL,
        project_title TEXT,
        role TEXT,
        auto_link_emails BOOLEAN DEFAULT true,
        created_at TIMESTAMP DEFAULT NOW(),
        UNIQUE(contact_email, project_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_contact_mappings_email ON contact_project_mappings(contact_email)",
    "CREATE INDEX IF NOT EXISTS idx_contact_mappings_project ON contact_project_mappings(project_id)",

    # Suggested links queue
    """CREATE TABLE IF NOT EXISTS suggested_links (
        id TEXT PRIMARY KEY,
        source_type TEXT NOT NULL,
        source_id TEXT NOT NULL,
        source_name TEXT,
        target_type TEXT NOT NULL,
        target_id TEXT NOT NULL,
        target_name TEXT,
        confidence FLOAT DEFAULT 0.5,
        reason TEXT,
        suggested_by TEXT,
        status TEXT DEFAULT 'pending',
        reviewed_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT NOW(),
        UNIQUE(source_type, source_id, target_type, target_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_suggested_links_status ON suggested_links(status)",
    "CREATE INDEX IF NOT EXISTS idx_suggested_links_target ON suggested_links(target_type, target_id)",
    "CREATE INDEX IF NOT EXISTS idx_suggested_links_source ON suggested_links(source_type, source_id)",
]


async def init_linking_schema(session: AsyncSession) -> None:
    """Initialize the linking tables."""
    for stmt in LINKING_STATEMENTS:
        await session.execute(text(stmt))
    await session.commit()
    logger.info("Linking schema initialized")


class LinkingService:
    """Service for managing project/task links and mapping rules."""

    # ==================== Folder Mappings ====================

    async def add_folder_mapping(
        self,
        session: AsyncSession,
        folder_id: str,
        project_id: str,
        folder_name: str | None = None,
        folder_path: str | None = None,
        project_title: str | None = None,
        auto_link_new_files: bool = True,
    ) -> dict:
        """Add a folder-to-project mapping rule."""
        mapping_id = f"fpm_{uuid.uuid4().hex[:12]}"

        await session.execute(
            text("""
                INSERT INTO folder_project_mappings
                    (id, folder_id, folder_name, folder_path, project_id, project_title, auto_link_new_files)
                VALUES (:id, :folder_id, :folder_name, :folder_path, :project_id, :project_title, :auto_link)
                ON CONFLICT (folder_id, project_id) DO UPDATE SET
                    folder_name = EXCLUDED.folder_name,
                    folder_path = EXCLUDED.folder_path,
                    project_title = EXCLUDED.project_title,
                    auto_link_new_files = EXCLUDED.auto_link_new_files,
                    updated_at = NOW()
            """),
            {
                "id": mapping_id,
                "folder_id": folder_id,
                "folder_name": folder_name,
                "folder_path": folder_path,
                "project_id": project_id,
                "project_title": project_title,
                "auto_link": auto_link_new_files,
            }
        )
        await session.commit()

        logger.info("Added folder mapping", folder_id=folder_id, project_id=project_id)
        return {"id": mapping_id, "folder_id": folder_id, "project_id": project_id}

    async def get_folder_mappings(
        self,
        session: AsyncSession,
        project_id: str | None = None,
        active_only: bool = True,
    ) -> list[dict]:
        """Get folder-to-project mappings."""
        query = "SELECT * FROM folder_project_mappings WHERE 1=1"
        params = {}

        if active_only:
            query += " AND is_active = true"
        if project_id:
            query += " AND project_id = :project_id"
            params["project_id"] = project_id

        query += " ORDER BY created_at DESC"

        result = await session.execute(text(query), params)
        return [dict(row._mapping) for row in result.fetchall()]

    async def get_project_for_folder(
        self,
        session: AsyncSession,
        folder_id: str
    ) -> str | None:
        """Get the project ID mapped to a folder (if any)."""
        result = await session.execute(
            text("""
                SELECT project_id FROM folder_project_mappings
                WHERE folder_id = :folder_id AND is_active = true
                LIMIT 1
            """),
            {"folder_id": folder_id}
        )
        row = result.fetchone()
        return row[0] if row else None

    async def remove_folder_mapping(
        self,
        session: AsyncSession,
        folder_id: str,
        project_id: str,
    ) -> bool:
        """Remove a folder-to-project mapping."""
        result = await session.execute(
            text("""
                DELETE FROM folder_project_mappings
                WHERE folder_id = :folder_id AND project_id = :project_id
            """),
            {"folder_id": folder_id, "project_id": project_id}
        )
        await session.commit()
        return result.rowcount > 0

    # ==================== Contact Mappings ====================

    async def add_contact_mapping(
        self,
        session: AsyncSession,
        contact_email: str,
        project_id: str,
        contact_name: str | None = None,
        project_title: str | None = None,
        role: str | None = None,
        auto_link_emails: bool = True,
    ) -> dict:
        """Add a contact-to-project mapping."""
        mapping_id = f"cpm_{uuid.uuid4().hex[:12]}"

        await session.execute(
            text("""
                INSERT INTO contact_project_mappings
                    (id, contact_email, contact_name, project_id, project_title, role, auto_link_emails)
                VALUES (:id, :email, :name, :project_id, :project_title, :role, :auto_link)
                ON CONFLICT (contact_email, project_id) DO UPDATE SET
                    contact_name = EXCLUDED.contact_name,
                    project_title = EXCLUDED.project_title,
                    role = EXCLUDED.role,
                    auto_link_emails = EXCLUDED.auto_link_emails
            """),
            {
                "id": mapping_id,
                "email": contact_email.lower(),
                "name": contact_name,
                "project_id": project_id,
                "project_title": project_title,
                "role": role,
                "auto_link": auto_link_emails,
            }
        )
        await session.commit()

        logger.info("Added contact mapping", email=contact_email, project_id=project_id)
        return {"id": mapping_id, "contact_email": contact_email, "project_id": project_id}

    async def get_contact_mappings(
        self,
        session: AsyncSession,
        project_id: str | None = None,
    ) -> list[dict]:
        """Get contact-to-project mappings."""
        query = "SELECT * FROM contact_project_mappings WHERE 1=1"
        params = {}

        if project_id:
            query += " AND project_id = :project_id"
            params["project_id"] = project_id

        query += " ORDER BY created_at DESC"

        result = await session.execute(text(query), params)
        return [dict(row._mapping) for row in result.fetchall()]

    async def get_projects_for_contact(
        self,
        session: AsyncSession,
        email: str
    ) -> list[str]:
        """Get project IDs mapped to a contact email."""
        result = await session.execute(
            text("""
                SELECT project_id FROM contact_project_mappings
                WHERE contact_email = :email AND auto_link_emails = true
            """),
            {"email": email.lower()}
        )
        return [row[0] for row in result.fetchall()]

    # ==================== Suggested Links ====================

    async def suggest_link(
        self,
        session: AsyncSession,
        source_type: str,
        source_id: str,
        target_type: str,
        target_id: str,
        source_name: str | None = None,
        target_name: str | None = None,
        confidence: float = 0.5,
        reason: str | None = None,
        suggested_by: str = "agent",
    ) -> dict:
        """Add a suggested link to the queue."""
        suggestion_id = f"sug_{uuid.uuid4().hex[:12]}"

        await session.execute(
            text("""
                INSERT INTO suggested_links
                    (id, source_type, source_id, source_name, target_type, target_id,
                     target_name, confidence, reason, suggested_by, status)
                VALUES (:id, :source_type, :source_id, :source_name, :target_type, :target_id,
                        :target_name, :confidence, :reason, :suggested_by, 'pending')
                ON CONFLICT (source_type, source_id, target_type, target_id) DO UPDATE SET
                    confidence = GREATEST(suggested_links.confidence, EXCLUDED.confidence),
                    reason = EXCLUDED.reason,
                    suggested_by = EXCLUDED.suggested_by
            """),
            {
                "id": suggestion_id,
                "source_type": source_type,
                "source_id": source_id,
                "source_name": source_name,
                "target_type": target_type,
                "target_id": target_id,
                "target_name": target_name,
                "confidence": confidence,
                "reason": reason,
                "suggested_by": suggested_by,
            }
        )
        await session.commit()

        logger.debug("Created link suggestion", source=f"{source_type}:{source_id}", target=f"{target_type}:{target_id}")
        return {"id": suggestion_id, "source_id": source_id, "target_id": target_id}

    async def get_pending_suggestions(
        self,
        session: AsyncSession,
        target_type: str | None = None,
        target_id: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Get pending link suggestions."""
        query = "SELECT * FROM suggested_links WHERE status = 'pending'"
        params: dict[str, Any] = {"limit": limit}

        if target_type:
            query += " AND target_type = :target_type"
            params["target_type"] = target_type
        if target_id:
            query += " AND target_id = :target_id"
            params["target_id"] = target_id

        query += " ORDER BY confidence DESC, created_at DESC LIMIT :limit"

        result = await session.execute(text(query), params)
        return [dict(row._mapping) for row in result.fetchall()]

    async def _resolve_suggestion_id(
        self,
        session: AsyncSession,
        suggestion_id: str,
    ) -> str | None:
        """Resolve a partial suggestion ID to a full ID.

        Supports both exact matches and prefix matches for truncated IDs
        (e.g., 'sug_8d70771a' matches 'sug_8d70771a5294').
        """
        # First try exact match
        result = await session.execute(
            text("SELECT id FROM suggested_links WHERE id = :id"),
            {"id": suggestion_id}
        )
        row = result.fetchone()
        if row:
            return row[0]

        # Try prefix match (for truncated IDs)
        result = await session.execute(
            text("SELECT id FROM suggested_links WHERE id LIKE :prefix"),
            {"prefix": f"{suggestion_id}%"}
        )
        rows = result.fetchall()
        if len(rows) == 1:
            return rows[0][0]
        elif len(rows) > 1:
            logger.warning("Multiple suggestions match prefix", prefix=suggestion_id, count=len(rows))
            return None  # Ambiguous - require more specific ID

        return None

    async def approve_suggestion(
        self,
        session: AsyncSession,
        suggestion_id: str,
    ) -> bool:
        """Approve a suggested link and create the actual link in Neo4j."""
        # Resolve partial ID to full ID
        full_id = await self._resolve_suggestion_id(session, suggestion_id)
        if not full_id:
            return False

        # Get the suggestion
        result = await session.execute(
            text("SELECT * FROM suggested_links WHERE id = :id"),
            {"id": full_id}
        )
        row = result.fetchone()
        if not row:
            return False

        suggestion = dict(row._mapping)

        # Create the link in Neo4j
        await self._create_graph_link(
            source_type=suggestion["source_type"],
            source_id=suggestion["source_id"],
            target_type=suggestion["target_type"],
            target_id=suggestion["target_id"],
        )

        # Mark as approved
        await session.execute(
            text("""
                UPDATE suggested_links
                SET status = 'approved', reviewed_at = NOW()
                WHERE id = :id
            """),
            {"id": full_id}
        )
        await session.commit()

        logger.info("Approved link suggestion", suggestion_id=full_id)
        return True

    async def reject_suggestion(
        self,
        session: AsyncSession,
        suggestion_id: str,
    ) -> bool:
        """Reject a suggested link."""
        # Resolve partial ID to full ID
        full_id = await self._resolve_suggestion_id(session, suggestion_id)
        if not full_id:
            return False

        result = await session.execute(
            text("""
                UPDATE suggested_links
                SET status = 'rejected', reviewed_at = NOW()
                WHERE id = :id
            """),
            {"id": full_id}
        )
        await session.commit()
        return result.rowcount > 0

    async def batch_approve_suggestions(
        self,
        session: AsyncSession,
        min_confidence: float = 0.0,
        limit: int | None = None,
    ) -> tuple[int, int]:
        """Batch approve pending suggestions at or above a confidence threshold.

        Args:
            session: PostgreSQL session
            min_confidence: Minimum confidence score (0.0 = all, 0.8 = high confidence only)
            limit: Maximum number to approve (None = no limit)

        Returns:
            Tuple of (approved_count, failed_count)
        """
        # Get pending suggestions meeting threshold
        query = """
            SELECT * FROM suggested_links
            WHERE status = 'pending' AND confidence >= :min_confidence
            ORDER BY confidence DESC
        """
        if limit:
            query += f" LIMIT {limit}"

        result = await session.execute(text(query), {"min_confidence": min_confidence})
        suggestions = [dict(row._mapping) for row in result.fetchall()]

        approved = 0
        failed = 0

        for suggestion in suggestions:
            try:
                # Create the link in Neo4j
                success = await self._create_graph_link(
                    source_type=suggestion["source_type"],
                    source_id=suggestion["source_id"],
                    target_type=suggestion["target_type"],
                    target_id=suggestion["target_id"],
                )

                if success:
                    # Mark as approved
                    await session.execute(
                        text("""
                            UPDATE suggested_links
                            SET status = 'approved', reviewed_at = NOW()
                            WHERE id = :id
                        """),
                        {"id": suggestion["id"]}
                    )
                    approved += 1
                else:
                    failed += 1
                    logger.warning(
                        "Failed to create graph link",
                        suggestion_id=suggestion["id"],
                        source=f"{suggestion['source_type']}:{suggestion['source_id']}",
                        target=f"{suggestion['target_type']}:{suggestion['target_id']}",
                    )
            except Exception as e:
                failed += 1
                logger.error("Error approving suggestion", suggestion_id=suggestion["id"], error=str(e))

        await session.commit()
        logger.info("Batch approved suggestions", approved=approved, failed=failed, min_confidence=min_confidence)
        return approved, failed

    async def get_suggestion_stats(self, session: AsyncSession) -> dict:
        """Get statistics on suggested links."""
        result = await session.execute(
            text("""
                SELECT
                    status,
                    COUNT(*) as count
                FROM suggested_links
                GROUP BY status
            """)
        )
        stats = {row[0]: row[1] for row in result.fetchall()}
        return {
            "pending": stats.get("pending", 0),
            "approved": stats.get("approved", 0),
            "rejected": stats.get("rejected", 0),
            "auto": stats.get("auto", 0),
        }

    # ==================== Direct Link Creation ====================

    async def _create_graph_link(
        self,
        source_type: str,
        source_id: str,
        target_type: str,
        target_id: str,
    ) -> bool:
        """Create a link in the Neo4j graph."""
        # Normalize types to lowercase for matching
        src = source_type.lower()
        tgt = target_type.lower()

        async for neo_session in get_neo4j_session():
            # Document links
            if src == "document" and tgt == "project":
                await gs.link_project_to_document(neo_session, target_id, source_id)
            elif src == "document" and tgt == "task":
                await gs.link_task_to_document(neo_session, target_id, source_id)
            # Email links
            elif src == "email" and tgt == "project":
                await self._link_email_to_project(neo_session, source_id, target_id)
            elif src == "email" and tgt == "task":
                await self._link_email_to_task(neo_session, source_id, target_id)
            # Task links
            elif src == "task" and tgt == "project":
                await gs.link_task_to_project(neo_session, source_id, target_id)
            elif src == "task" and tgt == "goal":
                await gs.link_task_to_goal(neo_session, source_id, target_id)
            elif src == "task" and tgt == "person":
                # target_id is person's display name, look up email
                person_email = await self._get_person_email_by_name(neo_session, target_id)
                if person_email:
                    await gs.link_task_to_person(neo_session, source_id, person_email)
                else:
                    logger.warning("Person not found for task link", person_name=target_id)
            # Project links
            elif src == "project" and tgt == "goal":
                await gs.link_project_to_goal(neo_session, source_id, target_id)
            elif src == "project" and tgt == "repository":
                await gs.link_project_to_repository(neo_session, source_id, target_id)
            elif src == "project" and tgt == "person":
                # target_id is person's display name, look up email
                person_email = await self._get_person_email_by_name(neo_session, target_id)
                if person_email:
                    await gs.link_project_to_person(neo_session, source_id, person_email)
                else:
                    logger.warning("Person not found for project link", person_name=target_id)
            # Goal links
            elif src == "goal" and tgt == "project":
                await gs.link_project_to_goal(neo_session, target_id, source_id)
            elif src == "goal" and tgt == "person":
                # target_id is person's display name, look up email
                person_email = await self._get_person_email_by_name(neo_session, target_id)
                if person_email:
                    await gs.link_goal_to_person(neo_session, source_id, person_email)
                else:
                    logger.warning("Person not found for goal link", person_name=target_id)
            else:
                logger.warning("Unknown link type", source_type=source_type, target_type=target_type)
                return False
        return True

    async def _link_email_to_project(self, session, gmail_id: str, project_id: str) -> None:
        """Create REFERENCED_IN relationship between Email and Project."""
        query = """
        MATCH (e:Email {gmail_id: $gmail_id})
        MATCH (p:Project {id: $project_id})
        MERGE (e)-[:REFERENCED_IN]->(p)
        """
        await session.run(query, gmail_id=gmail_id, project_id=project_id)

    async def _link_email_to_task(self, session, gmail_id: str, task_id: str) -> None:
        """Create REFERENCED_IN relationship between Email and Task."""
        query = """
        MATCH (e:Email {gmail_id: $gmail_id})
        MATCH (t:Task {id: $task_id})
        MERGE (e)-[:REFERENCED_IN]->(t)
        """
        await session.run(query, gmail_id=gmail_id, task_id=task_id)

    async def _get_person_email_by_name(self, session, person_name: str) -> str | None:
        """Look up a person's email by their display name.

        Handles display names that may include extra info like "(Staff)".
        Tries exact match first, then partial match on name portion.
        """
        import re

        # Try to extract base name (strip parenthetical suffixes like "(Staff)")
        base_name = re.sub(r'\s*\([^)]+\)\s*$', '', person_name).strip()

        query = """
        MATCH (p:Person)
        WHERE p.name = $name
           OR p.display_name = $name
           OR p.name = $base_name
           OR p.display_name = $base_name
        RETURN p.email as email
        LIMIT 1
        """
        result = await session.run(query, name=person_name, base_name=base_name)
        record = await result.single()
        return record["email"] if record else None

    # ==================== Auto-Linking ====================

    async def auto_link_document(
        self,
        pg_session: AsyncSession,
        drive_id: str,
        folder_id: str | None = None,
        document_name: str | None = None,
    ) -> list[dict]:
        """Auto-link a document based on folder mapping rules.

        Returns list of links created.
        """
        links_created = []

        if not folder_id:
            return links_created

        # Check folder mappings
        project_id = await self.get_project_for_folder(pg_session, folder_id)

        if project_id:
            # Create direct link
            await self._create_graph_link(
                source_type="document",
                source_id=drive_id,
                target_type="project",
                target_id=project_id,
            )

            # Record in suggestions as auto-created
            await pg_session.execute(
                text("""
                    INSERT INTO suggested_links
                        (id, source_type, source_id, source_name, target_type, target_id,
                         confidence, reason, suggested_by, status, reviewed_at)
                    VALUES (:id, 'document', :source_id, :source_name, 'project', :target_id,
                            1.0, 'Folder mapping rule', 'rule', 'auto', NOW())
                    ON CONFLICT (source_type, source_id, target_type, target_id) DO NOTHING
                """),
                {
                    "id": f"sug_{uuid.uuid4().hex[:12]}",
                    "source_id": drive_id,
                    "source_name": document_name,
                    "target_id": project_id,
                }
            )
            await pg_session.commit()

            links_created.append({
                "source_type": "document",
                "source_id": drive_id,
                "target_type": "project",
                "target_id": project_id,
                "reason": "folder_mapping",
            })

            logger.info("Auto-linked document to project", drive_id=drive_id, project_id=project_id)

        return links_created

    async def auto_link_email(
        self,
        pg_session: AsyncSession,
        gmail_id: str,
        sender_email: str,
        subject: str | None = None,
    ) -> list[dict]:
        """Auto-link an email based on contact mapping rules.

        Returns list of links created.
        """
        links_created = []

        # Check contact mappings
        project_ids = await self.get_projects_for_contact(pg_session, sender_email)

        for project_id in project_ids:
            await self._create_graph_link(
                source_type="email",
                source_id=gmail_id,
                target_type="project",
                target_id=project_id,
            )

            # Record in suggestions as auto-created
            await pg_session.execute(
                text("""
                    INSERT INTO suggested_links
                        (id, source_type, source_id, source_name, target_type, target_id,
                         confidence, reason, suggested_by, status, reviewed_at)
                    VALUES (:id, 'email', :source_id, :source_name, 'project', :target_id,
                            1.0, :reason, 'rule', 'auto', NOW())
                    ON CONFLICT (source_type, source_id, target_type, target_id) DO NOTHING
                """),
                {
                    "id": f"sug_{uuid.uuid4().hex[:12]}",
                    "source_id": gmail_id,
                    "source_name": subject,
                    "target_id": project_id,
                    "reason": f"Contact mapping: {sender_email}",
                }
            )
            await pg_session.commit()

            links_created.append({
                "source_type": "email",
                "source_id": gmail_id,
                "target_type": "project",
                "target_id": project_id,
                "reason": f"contact_mapping:{sender_email}",
            })

            logger.info("Auto-linked email to project", gmail_id=gmail_id, project_id=project_id)

        return links_created

    # ==================== Bulk Operations ====================

    async def link_folder_contents_to_project(
        self,
        pg_session: AsyncSession,
        folder_id: str,
        project_id: str,
    ) -> int:
        """Link all documents in a folder to a project.

        Returns count of documents linked.
        """
        async for neo_session in get_neo4j_session():
            # Find all documents in this folder
            result = await neo_session.run(
                """
                MATCH (d:Document {folder: $folder_id})
                MATCH (p:Project {id: $project_id})
                MERGE (p)-[:DOCUMENTED_IN]->(d)
                RETURN count(d) as count
                """,
                folder_id=folder_id,
                project_id=project_id,
            )
            record = await result.single()
            count = record["count"] if record else 0

            logger.info("Bulk linked folder to project", folder_id=folder_id, project_id=project_id, count=count)
            return count

    async def get_project_content_summary(
        self,
        project_id: str,
    ) -> dict:
        """Get a summary of all content linked to a project."""
        async for neo_session in get_neo4j_session():
            result = await neo_session.run(
                """
                MATCH (p:Project {id: $project_id})
                OPTIONAL MATCH (p)-[:DOCUMENTED_IN]->(d:Document)
                OPTIONAL MATCH (e:Email)-[:REFERENCED_IN]->(p)
                OPTIONAL MATCH (p)-[:USES_REPO]->(r:Repository)
                OPTIONAL MATCH (t:Task)-[:PART_OF]->(p)
                OPTIONAL MATCH (person:Person)-[:WORKS_ON]->(p)
                RETURN
                    p.title as project_title,
                    count(DISTINCT d) as document_count,
                    count(DISTINCT e) as email_count,
                    count(DISTINCT r) as repo_count,
                    count(DISTINCT t) as task_count,
                    count(DISTINCT person) as member_count
                """,
                project_id=project_id,
            )
            record = await result.single()

            if not record:
                return {}

            return {
                "project_title": record["project_title"],
                "documents": record["document_count"],
                "emails": record["email_count"],
                "repositories": record["repo_count"],
                "tasks": record["task_count"],
                "members": record["member_count"],
            }


    # ==================== AI-Powered Link Analysis ====================

    async def get_unlinked_nodes(
        self,
        node_type: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Get nodes that have few or no relationships.

        Returns nodes of type Task, Project, Goal, Document that could benefit
        from being linked to other nodes.
        """
        node_types = [node_type] if node_type else ["Task", "Project", "Goal"]
        unlinked = []

        async for neo_session in get_neo4j_session():
            for ntype in node_types:
                if ntype == "Task":
                    query = """
                    MATCH (t:Task)
                    WHERE t.status <> 'done'
                    OPTIONAL MATCH (t)-[r]-()
                    WITH t, count(r) as rel_count
                    WHERE rel_count < 2
                    RETURN 'Task' as type, t.id as id, t.title as name,
                           t.description as description, rel_count
                    ORDER BY rel_count ASC, t.created_at DESC
                    LIMIT $limit
                    """
                elif ntype == "Project":
                    query = """
                    MATCH (p:Project)
                    WHERE p.status <> 'archived'
                    OPTIONAL MATCH (p)-[r]-()
                    WITH p, count(r) as rel_count
                    WHERE rel_count < 3
                    RETURN 'Project' as type, p.id as id, p.title as name,
                           p.description as description, rel_count
                    ORDER BY rel_count ASC, p.created_at DESC
                    LIMIT $limit
                    """
                elif ntype == "Goal":
                    query = """
                    MATCH (g:Goal)
                    WHERE g.status = 'active'
                    OPTIONAL MATCH (g)-[r]-()
                    WITH g, count(r) as rel_count
                    WHERE rel_count < 2
                    RETURN 'Goal' as type, g.id as id, g.title as name,
                           g.description as description, rel_count
                    ORDER BY rel_count ASC, g.created_at DESC
                    LIMIT $limit
                    """
                else:
                    continue

                result = await neo_session.run(query, limit=limit // len(node_types) + 1)
                records = await result.data()
                unlinked.extend(records)

        return unlinked[:limit]

    async def get_potential_targets(
        self,
        source_type: str,
    ) -> list[dict]:
        """Get potential link targets for a source type."""
        targets = []

        target_types = {
            "Task": ["Project", "Goal", "Person"],
            "Project": ["Goal", "Person", "Repository"],
            "Goal": ["Person", "Project"],
        }.get(source_type, [])

        async for neo_session in get_neo4j_session():
            for ttype in target_types:
                if ttype == "Project":
                    query = """
                    MATCH (p:Project)
                    WHERE p.status <> 'archived'
                    RETURN 'Project' as type, p.id as id, p.title as name
                    ORDER BY p.updated_at DESC
                    LIMIT 30
                    """
                elif ttype == "Goal":
                    query = """
                    MATCH (g:Goal)
                    WHERE g.status = 'active'
                    RETURN 'Goal' as type, g.id as id, g.title as name
                    ORDER BY g.updated_at DESC
                    LIMIT 20
                    """
                elif ttype == "Person":
                    query = """
                    MATCH (p:Person)
                    WHERE p.name IS NOT NULL
                    RETURN 'Person' as type, p.email as id, p.name as name
                    ORDER BY p.email
                    LIMIT 50
                    """
                elif ttype == "Repository":
                    query = """
                    MATCH (r:Repository)
                    RETURN 'Repository' as type, r.id as id, r.name as name
                    LIMIT 20
                    """
                else:
                    continue

                result = await neo_session.run(query)
                records = await result.data()
                targets.extend(records)

        return targets

    async def analyze_and_suggest_links(
        self,
        pg_session: AsyncSession,
        node_type: str | None = None,
        limit: int = 10,
        auto_apply: bool = False,
    ) -> list[dict]:
        """Use LLM to analyze unlinked nodes and suggest appropriate relationships.

        Args:
            pg_session: PostgreSQL session for storing suggestions
            node_type: Optional filter for node type (Task, Project, Goal)
            limit: Maximum number of nodes to analyze
            auto_apply: If True, automatically create high-confidence links

        Returns:
            List of suggestions created
        """
        from together import Together
        from cognitex.config import get_settings
        import json

        settings = get_settings()
        api_key = settings.together_api_key.get_secret_value()
        if not api_key:
            logger.warning("TOGETHER_API_KEY not configured")
            return []

        client = Together(api_key=api_key)

        # Get unlinked nodes
        unlinked = await self.get_unlinked_nodes(node_type=node_type, limit=limit)
        if not unlinked:
            logger.info("No unlinked nodes found")
            return []

        # Get potential targets
        all_targets = {}
        for ntype in set(n["type"] for n in unlinked):
            targets = await self.get_potential_targets(ntype)
            all_targets[ntype] = targets

        suggestions_created = []

        for node in unlinked:
            targets = all_targets.get(node["type"], [])
            if not targets:
                continue

            # Build prompt for LLM
            prompt = self._build_link_analysis_prompt(node, targets)

            try:
                response = client.chat.completions.create(
                    model=settings.together_model_planner,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=1024,
                    temperature=0.2,
                )

                content = response.choices[0].message.content.strip()

                # Parse JSON response
                if "```json" in content:
                    content = content.split("```json")[1].split("```")[0]
                elif "```" in content:
                    content = content.split("```")[1].split("```")[0]

                result = json.loads(content)

                for suggestion in result.get("suggestions", []):
                    target_type = suggestion.get("target_type")
                    target_id = suggestion.get("target_id")
                    confidence = suggestion.get("confidence", 0.5)
                    reason = suggestion.get("reason", "")

                    if not target_type or not target_id:
                        continue

                    # Find target name
                    target_name = None
                    for t in targets:
                        if t["type"] == target_type and t["id"] == target_id:
                            target_name = t.get("name")
                            break

                    # Create or auto-apply suggestion
                    if auto_apply and confidence >= 0.8:
                        # High confidence - create link directly
                        await self._create_graph_link_generic(
                            source_type=node["type"],
                            source_id=node["id"],
                            target_type=target_type,
                            target_id=target_id,
                        )

                        await self.suggest_link(
                            pg_session,
                            source_type=node["type"],
                            source_id=node["id"],
                            target_type=target_type,
                            target_id=target_id,
                            source_name=node.get("name"),
                            target_name=target_name,
                            confidence=confidence,
                            reason=reason,
                            suggested_by="agent_auto",
                        )

                        # Mark as auto-approved
                        await pg_session.execute(
                            text("""
                                UPDATE suggested_links
                                SET status = 'auto', reviewed_at = NOW()
                                WHERE source_type = :source_type AND source_id = :source_id
                                  AND target_type = :target_type AND target_id = :target_id
                            """),
                            {
                                "source_type": node["type"],
                                "source_id": node["id"],
                                "target_type": target_type,
                                "target_id": target_id,
                            }
                        )
                        await pg_session.commit()

                        suggestions_created.append({
                            "source": f"{node['type']}:{node.get('name', node['id'])}",
                            "target": f"{target_type}:{target_name or target_id}",
                            "confidence": confidence,
                            "reason": reason,
                            "status": "auto_applied",
                        })
                    else:
                        # Store as pending suggestion
                        await self.suggest_link(
                            pg_session,
                            source_type=node["type"],
                            source_id=node["id"],
                            target_type=target_type,
                            target_id=target_id,
                            source_name=node.get("name"),
                            target_name=target_name,
                            confidence=confidence,
                            reason=reason,
                            suggested_by="agent",
                        )

                        suggestions_created.append({
                            "source": f"{node['type']}:{node.get('name', node['id'])}",
                            "target": f"{target_type}:{target_name or target_id}",
                            "confidence": confidence,
                            "reason": reason,
                            "status": "pending",
                        })

                logger.info(
                    "Analyzed node for links",
                    node_type=node["type"],
                    node_name=node.get("name", node["id"])[:30],
                    suggestions=len(result.get("suggestions", [])),
                )

            except Exception as e:
                logger.warning(
                    "Failed to analyze node",
                    node_id=node["id"],
                    error=str(e),
                )
                continue

        return suggestions_created

    def _build_link_analysis_prompt(self, node: dict, targets: list[dict]) -> str:
        """Build prompt for LLM to analyze potential links."""
        node_desc = f"{node['type']}: {node.get('name', 'Untitled')}"
        if node.get("description"):
            node_desc += f"\nDescription: {node['description'][:500]}"

        # Group targets by type
        targets_by_type = {}
        for t in targets:
            ttype = t["type"]
            if ttype not in targets_by_type:
                targets_by_type[ttype] = []
            targets_by_type[ttype].append(t)

        targets_text = ""
        for ttype, items in targets_by_type.items():
            targets_text += f"\n{ttype}s:\n"
            for item in items[:15]:  # Limit per type
                targets_text += f"  - id: {item['id']}, name: {item.get('name', 'N/A')}\n"

        return f"""Analyze this node and suggest appropriate relationships to other nodes.

SOURCE NODE:
{node_desc}

AVAILABLE TARGETS:
{targets_text}

Based on the source node's name and description, identify which targets it should be linked to.

Valid relationship types:
- Task -> Project (task is PART_OF project)
- Task -> Goal (task CONTRIBUTES_TO goal)
- Task -> Person (task INVOLVES person)
- Project -> Goal (project is PART_OF goal)
- Project -> Person (person WORKS_ON project)
- Goal -> Person (person OWNS goal)

Return a JSON object with suggested links:
```json
{{
  "suggestions": [
    {{
      "target_type": "Project",
      "target_id": "project_id_here",
      "confidence": 0.85,
      "reason": "Brief explanation of why this link makes sense"
    }}
  ]
}}
```

Rules:
- Only suggest links where there's a clear semantic relationship
- Confidence should be 0.5-1.0 based on how certain the match is
- If no good matches exist, return an empty suggestions array
- Maximum 3 suggestions per node
- Focus on the most relevant and useful connections"""

    async def _create_graph_link_generic(
        self,
        source_type: str,
        source_id: str,
        target_type: str,
        target_id: str,
    ) -> bool:
        """Create a link in the Neo4j graph (extended version with more link types)."""
        async for neo_session in get_neo4j_session():
            if source_type == "Task" and target_type == "Project":
                await gs.link_task_to_project(neo_session, source_id, target_id)
            elif source_type == "Task" and target_type == "Goal":
                await gs.link_task_to_goal(neo_session, source_id, target_id)
            elif source_type == "Task" and target_type == "Person":
                await gs.link_task_to_person(neo_session, source_id, target_id)
            elif source_type == "Project" and target_type == "Goal":
                await gs.link_project_to_goal(neo_session, source_id, target_id)
            elif source_type == "Project" and target_type == "Person":
                await gs.link_project_to_person(neo_session, source_id, target_id)
            elif source_type == "Goal" and target_type == "Person":
                await gs.link_goal_to_person(neo_session, source_id, target_id)
            elif source_type == "document" and target_type == "project":
                await gs.link_project_to_document(neo_session, target_id, source_id)
            elif source_type == "document" and target_type == "task":
                await gs.link_task_to_document(neo_session, target_id, source_id)
            else:
                logger.warning("Unknown link type", source_type=source_type, target_type=target_type)
                return False
        return True


    async def analyze_single_node(
        self,
        pg_session: AsyncSession,
        node_type: str,
        node_id: str,
        node_name: str,
        node_description: str | None = None,
        auto_apply_threshold: float = 0.9,
    ) -> list[dict]:
        """Analyze a single node and suggest/auto-apply links.

        This is called after a task/project/goal is created to suggest relationships.
        High-confidence links (>= threshold) are auto-applied.

        Returns list of suggestions created.
        """
        from together import Together

        api_key = settings.together_api_key
        if not api_key:
            logger.warning("No Together API key configured")
            return []

        client = Together(api_key=api_key)

        # Get potential targets for this node type
        targets = await self.get_potential_targets(node_type)
        if not targets:
            logger.info("No potential targets found", node_type=node_type)
            return []

        # Build node info
        node = {
            "type": node_type,
            "id": node_id,
            "name": node_name,
            "description": node_description,
        }

        # Build prompt and call LLM
        prompt = self._build_link_analysis_prompt(node, targets)

        try:
            response = client.chat.completions.create(
                model=settings.together_model_planner,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1024,
                temperature=0.2,
            )

            content = response.choices[0].message.content.strip()

            # Parse JSON response
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0]
            elif "```" in content:
                content = content.split("```")[1].split("```")[0]

            result = json.loads(content)
            suggestions_created = []

            for suggestion in result.get("suggestions", []):
                target_type = suggestion.get("target_type")
                target_id = suggestion.get("target_id")
                confidence = suggestion.get("confidence", 0.5)
                reason = suggestion.get("reason", "")

                if not target_type or not target_id:
                    continue

                # Find target name
                target_name = None
                for t in targets:
                    if t["type"] == target_type and t["id"] == target_id:
                        target_name = t.get("name")
                        break

                # High confidence - auto-apply the link
                if confidence >= auto_apply_threshold:
                    await self._create_graph_link_generic(
                        source_type=node_type,
                        source_id=node_id,
                        target_type=target_type,
                        target_id=target_id,
                    )
                    status = "auto"
                    logger.info(
                        "Auto-applied link",
                        source=f"{node_type}:{node_id}",
                        target=f"{target_type}:{target_id}",
                        confidence=confidence,
                    )
                else:
                    status = "pending"

                # Store suggestion record
                await self.suggest_link(
                    pg_session,
                    source_type=node_type,
                    source_id=node_id,
                    target_type=target_type,
                    target_id=target_id,
                    source_name=node_name,
                    target_name=target_name,
                    confidence=confidence,
                    reason=reason,
                    suggested_by="agent_auto",
                )

                # Update status if auto-applied
                if status == "auto":
                    await pg_session.execute(
                        text("""
                            UPDATE suggested_links
                            SET status = 'auto', reviewed_at = NOW()
                            WHERE source_type = :source_type AND source_id = :source_id
                              AND target_type = :target_type AND target_id = :target_id
                        """),
                        {
                            "source_type": node_type,
                            "source_id": node_id,
                            "target_type": target_type,
                            "target_id": target_id,
                        }
                    )

                await pg_session.commit()

                suggestions_created.append({
                    "target_type": target_type,
                    "target_id": target_id,
                    "target_name": target_name,
                    "confidence": confidence,
                    "reason": reason,
                    "status": status,
                })

            return suggestions_created

        except Exception as e:
            logger.error("Error analyzing node", error=str(e), node_id=node_id)
            return []


# Singleton
_linking_service: LinkingService | None = None


def get_linking_service() -> LinkingService:
    """Get the linking service singleton."""
    global _linking_service
    if _linking_service is None:
        _linking_service = LinkingService()
    return _linking_service
