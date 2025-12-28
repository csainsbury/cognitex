"""Drive metadata indexing service.

Indexes all Google Drive files for metadata (name, folder path, etc.)
without downloading content. Priority folder files are flagged for
deeper semantic analysis.
"""

from datetime import datetime
from typing import Generator

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from cognitex.db.postgres import get_session, init_postgres
from cognitex.db.neo4j import get_driver
from cognitex.services.drive import DriveService, get_drive_service, PRIORITY_FOLDERS

logger = structlog.get_logger()


# SQL statements for creating the drive_files table
CREATE_TABLE_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS drive_files (
        id VARCHAR(255) PRIMARY KEY,
        name VARCHAR(500) NOT NULL,
        mime_type VARCHAR(100),
        folder_path TEXT,
        parent_id VARCHAR(255),
        created_time TIMESTAMP,
        modified_time TIMESTAMP,
        size_bytes BIGINT,
        owner_email VARCHAR(255),
        is_priority BOOLEAN DEFAULT FALSE,
        indexed_at TIMESTAMP DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_drive_files_priority ON drive_files(is_priority)",
    "CREATE INDEX IF NOT EXISTS idx_drive_files_mime_type ON drive_files(mime_type)",
    "CREATE INDEX IF NOT EXISTS idx_drive_files_folder_path ON drive_files(folder_path)",
]


async def ensure_tables() -> None:
    """Create the drive_files table if it doesn't exist."""
    async for session in get_session():
        for stmt in CREATE_TABLE_STATEMENTS:
            await session.execute(text(stmt))
        await session.commit()
        logger.info("drive_files table ensured")


class DriveMetadataIndexer:
    """Service for indexing Drive file metadata."""

    def __init__(self):
        self.drive = get_drive_service()
        self._folder_cache: dict[str, str] = {}  # parent_id -> path
        self._priority_folder_ids: set[str] = set()

    async def _build_folder_path(self, file: dict) -> str:
        """Build the folder path for a file by traversing parents."""
        parents = file.get("parents", [])
        if not parents:
            return ""

        parent_id = parents[0]

        # Check cache
        if parent_id in self._folder_cache:
            return self._folder_cache[parent_id]

        # Build path by traversing up
        path_parts = []
        current_id = parent_id

        while current_id:
            try:
                parent_file = self.drive.service.files().get(
                    fileId=current_id,
                    fields="name, parents",
                    supportsAllDrives=True
                ).execute()

                path_parts.insert(0, parent_file.get("name", ""))
                parents = parent_file.get("parents", [])
                current_id = parents[0] if parents else None

            except Exception:
                break

        path = "/" + "/".join(path_parts) if path_parts else ""
        self._folder_cache[parent_id] = path
        return path

    def _is_priority_path(self, folder_path: str) -> bool:
        """Check if a folder path is under a priority folder."""
        if not folder_path:
            return False

        path_lower = folder_path.lower()
        for priority in PRIORITY_FOLDERS:
            if f"/{priority.lower()}" in path_lower or path_lower.startswith(priority.lower()):
                return True
        return False

    async def index_all_files(self, limit: int | None = None) -> dict:
        """
        Index all Drive files for metadata.

        Args:
            limit: Optional limit on number of files to index

        Returns:
            Stats dict with counts
        """
        await ensure_tables()

        stats = {
            "total": 0,
            "priority": 0,
            "errors": 0,
        }

        async for session in get_session():
            count = 0
            for file in self.drive.list_all_files():
                if limit and count >= limit:
                    break

                try:
                    folder_path = await self._build_folder_path(file)
                    is_priority = self._is_priority_path(folder_path)

                    # Parse dates (as naive datetimes for PostgreSQL compatibility)
                    created_time = None
                    modified_time = None
                    if file.get("createdTime"):
                        try:
                            dt = datetime.fromisoformat(
                                file["createdTime"].replace("Z", "+00:00")
                            )
                            created_time = dt.replace(tzinfo=None)  # Make naive
                        except ValueError:
                            pass
                    if file.get("modifiedTime"):
                        try:
                            dt = datetime.fromisoformat(
                                file["modifiedTime"].replace("Z", "+00:00")
                            )
                            modified_time = dt.replace(tzinfo=None)  # Make naive
                        except ValueError:
                            pass

                    # Get owner email
                    owners = file.get("owners", [])
                    owner_email = owners[0].get("emailAddress") if owners else None

                    # Upsert into database
                    await session.execute(
                        text("""
                        INSERT INTO drive_files (id, name, mime_type, folder_path, parent_id,
                                                 created_time, modified_time, size_bytes,
                                                 owner_email, is_priority, indexed_at)
                        VALUES (:id, :name, :mime_type, :folder_path, :parent_id,
                                :created_time, :modified_time, :size_bytes,
                                :owner_email, :is_priority, NOW())
                        ON CONFLICT (id) DO UPDATE SET
                            name = EXCLUDED.name,
                            mime_type = EXCLUDED.mime_type,
                            folder_path = EXCLUDED.folder_path,
                            modified_time = EXCLUDED.modified_time,
                            is_priority = EXCLUDED.is_priority,
                            indexed_at = NOW()
                        """),
                        {
                            "id": file["id"],
                            "name": file.get("name", ""),
                            "mime_type": file.get("mimeType"),
                            "folder_path": folder_path,
                            "parent_id": file.get("parents", [None])[0],
                            "created_time": created_time,
                            "modified_time": modified_time,
                            "size_bytes": int(file.get("size", 0)) if file.get("size") else None,
                            "owner_email": owner_email,
                            "is_priority": is_priority,
                        }
                    )

                    stats["total"] += 1
                    if is_priority:
                        stats["priority"] += 1

                    count += 1

                    if count % 100 == 0:
                        await session.commit()
                        logger.info(
                            "Indexing progress",
                            indexed=count,
                            priority=stats["priority"],
                        )

                except Exception as e:
                    stats["errors"] += 1
                    logger.warning("Failed to index file", file_id=file.get("id"), error=str(e))

            await session.commit()

        logger.info("Drive metadata indexing complete", **stats)
        return stats

    async def get_priority_files(
        self,
        mime_types: list[str] | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """
        Get priority folder files from the index.

        Args:
            mime_types: Optional list of MIME types to filter
            limit: Optional limit

        Returns:
            List of file metadata dicts
        """
        async for session in get_session():
            query = "SELECT * FROM drive_files WHERE is_priority = true"
            params = {}

            if mime_types:
                placeholders = ", ".join(f":mime_{i}" for i in range(len(mime_types)))
                query += f" AND mime_type IN ({placeholders})"
                for i, mt in enumerate(mime_types):
                    params[f"mime_{i}"] = mt

            query += " ORDER BY modified_time DESC"

            if limit:
                query += f" LIMIT {limit}"

            result = await session.execute(text(query), params)
            rows = result.mappings().all()
            return [dict(row) for row in rows]

        return []

    async def get_stats(self) -> dict:
        """Get statistics about the indexed files."""
        async for session in get_session():
            result = await session.execute(text("""
                SELECT
                    COUNT(*) as total,
                    COUNT(*) FILTER (WHERE is_priority) as priority,
                    COUNT(DISTINCT mime_type) as mime_types,
                    COUNT(DISTINCT folder_path) as folders
                FROM drive_files
            """))
            row = result.mappings().one()
            return dict(row)
        return {}
