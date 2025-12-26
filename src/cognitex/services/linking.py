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

    async def approve_suggestion(
        self,
        session: AsyncSession,
        suggestion_id: str,
    ) -> bool:
        """Approve a suggested link and create the actual link in Neo4j."""
        # Get the suggestion
        result = await session.execute(
            text("SELECT * FROM suggested_links WHERE id = :id"),
            {"id": suggestion_id}
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
            {"id": suggestion_id}
        )
        await session.commit()

        logger.info("Approved link suggestion", suggestion_id=suggestion_id)
        return True

    async def reject_suggestion(
        self,
        session: AsyncSession,
        suggestion_id: str,
    ) -> bool:
        """Reject a suggested link."""
        result = await session.execute(
            text("""
                UPDATE suggested_links
                SET status = 'rejected', reviewed_at = NOW()
                WHERE id = :id
            """),
            {"id": suggestion_id}
        )
        await session.commit()
        return result.rowcount > 0

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
        async for neo_session in get_neo4j_session():
            if source_type == "document" and target_type == "project":
                await gs.link_project_to_document(neo_session, target_id, source_id)
            elif source_type == "document" and target_type == "task":
                await gs.link_task_to_document(neo_session, target_id, source_id)
            elif source_type == "email" and target_type == "project":
                await self._link_email_to_project(neo_session, source_id, target_id)
            elif source_type == "email" and target_type == "task":
                await self._link_email_to_task(neo_session, source_id, target_id)
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


# Singleton
_linking_service: LinkingService | None = None


def get_linking_service() -> LinkingService:
    """Get the linking service singleton."""
    global _linking_service
    if _linking_service is None:
        _linking_service = LinkingService()
    return _linking_service
