"""GitHub metadata indexing service.

Indexes all GitHub repositories for metadata (name, language, etc.)
without downloading content. Priority repos are flagged for
deeper semantic analysis of code files.
"""

from datetime import datetime

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from cognitex.db.postgres import get_session, init_postgres
from cognitex.config import get_settings
from cognitex.services.github import GitHubService, CODE_EXTENSIONS, SKIP_DIRS

logger = structlog.get_logger()


def get_priority_repos() -> list[str]:
    """Get list of priority repository names from config."""
    settings = get_settings()
    repos_str = getattr(settings, 'github_priority_repos', '')
    if not repos_str:
        return []
    return [r.strip().lower() for r in repos_str.split(',') if r.strip()]


PRIORITY_REPOS = get_priority_repos()


# SQL statements for creating GitHub metadata tables
CREATE_TABLE_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS github_repos (
        id BIGINT PRIMARY KEY,
        full_name VARCHAR(255) NOT NULL UNIQUE,
        name VARCHAR(255) NOT NULL,
        owner VARCHAR(255) NOT NULL,
        description TEXT,
        primary_language VARCHAR(50),
        default_branch VARCHAR(100) DEFAULT 'main',
        is_private BOOLEAN DEFAULT FALSE,
        is_fork BOOLEAN DEFAULT FALSE,
        stars_count INTEGER DEFAULT 0,
        forks_count INTEGER DEFAULT 0,
        created_at TIMESTAMP,
        pushed_at TIMESTAMP,
        is_priority BOOLEAN DEFAULT FALSE,
        indexed_at TIMESTAMP DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_github_repos_priority ON github_repos(is_priority)",
    "CREATE INDEX IF NOT EXISTS idx_github_repos_owner ON github_repos(owner)",
    "CREATE INDEX IF NOT EXISTS idx_github_repos_language ON github_repos(primary_language)",
    """
    CREATE TABLE IF NOT EXISTS github_files (
        id VARCHAR(500) PRIMARY KEY,
        repo_id BIGINT NOT NULL,
        repo_full_name VARCHAR(255) NOT NULL,
        path VARCHAR(500) NOT NULL,
        name VARCHAR(255) NOT NULL,
        language VARCHAR(50),
        size_bytes INTEGER,
        sha VARCHAR(40),
        is_priority BOOLEAN DEFAULT FALSE,
        indexed_at TIMESTAMP DEFAULT NOW(),
        UNIQUE(repo_id, path)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_github_files_repo ON github_files(repo_id)",
    "CREATE INDEX IF NOT EXISTS idx_github_files_priority ON github_files(is_priority)",
    "CREATE INDEX IF NOT EXISTS idx_github_files_language ON github_files(language)",
]


async def ensure_tables() -> None:
    """Create the GitHub metadata tables if they don't exist."""
    async for session in get_session():
        for stmt in CREATE_TABLE_STATEMENTS:
            await session.execute(text(stmt))
        await session.commit()
        logger.info("github_repos and github_files tables ensured")


# Language detection from file extension
EXTENSION_TO_LANGUAGE = {
    ".py": "Python",
    ".js": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".jsx": "JavaScript",
    ".go": "Go",
    ".rs": "Rust",
    ".java": "Java",
    ".kt": "Kotlin",
    ".scala": "Scala",
    ".c": "C",
    ".cpp": "C++",
    ".h": "C",
    ".hpp": "C++",
    ".rb": "Ruby",
    ".php": "PHP",
    ".swift": "Swift",
    ".sql": "SQL",
    ".yaml": "YAML",
    ".yml": "YAML",
    ".json": "JSON",
    ".toml": "TOML",
    ".md": "Markdown",
    ".sh": "Shell",
    ".bash": "Shell",
    ".html": "HTML",
    ".css": "CSS",
    ".scss": "SCSS",
}


def detect_language(path: str) -> str | None:
    """Detect programming language from file path."""
    from pathlib import Path
    ext = Path(path).suffix.lower()
    return EXTENSION_TO_LANGUAGE.get(ext)


class GithubMetadataIndexer:
    """Service for indexing GitHub repository metadata."""

    def __init__(self):
        self.github = GitHubService()
        self._priority_repos: set[str] = set(PRIORITY_REPOS)

    def _is_priority_repo(self, full_name: str) -> bool:
        """Check if repo is in priority list.

        Matches case-insensitively against configured priority repos.
        """
        if not full_name:
            return False
        return full_name.lower() in self._priority_repos

    def _should_index_file(self, path: str) -> bool:
        """Check if a file should be indexed based on extension."""
        from pathlib import Path

        # Skip files in ignored directories
        path_parts = Path(path).parts
        if any(part in SKIP_DIRS for part in path_parts):
            return False

        # Check extension
        ext = Path(path).suffix.lower()
        return ext in CODE_EXTENSIONS

    async def index_all_repos(self, limit: int | None = None) -> dict:
        """
        Index all accessible repository metadata.

        Args:
            limit: Optional limit on number of repos to index

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

            try:
                repos = self.github.list_repos(
                    include_private=True,
                    include_forks=False,
                    limit=limit or 500,
                )
            except Exception as e:
                logger.error("Failed to list repos", error=str(e))
                stats["errors"] += 1
                return stats

            for repo in repos:
                if limit and count >= limit:
                    break

                try:
                    is_priority = self._is_priority_repo(repo["full_name"])

                    # Parse dates
                    created_at = None
                    pushed_at = None
                    if repo.get("created_at"):
                        try:
                            created_at = datetime.fromisoformat(
                                repo["created_at"].replace("Z", "+00:00")
                            ).replace(tzinfo=None)
                        except ValueError:
                            pass
                    if repo.get("pushed_at"):
                        try:
                            pushed_at = datetime.fromisoformat(
                                repo["pushed_at"].replace("Z", "+00:00")
                            ).replace(tzinfo=None)
                        except ValueError:
                            pass

                    # Upsert into database
                    await session.execute(
                        text("""
                        INSERT INTO github_repos (
                            id, full_name, name, owner, description,
                            primary_language, default_branch, is_private, is_fork,
                            stars_count, forks_count, created_at, pushed_at,
                            is_priority, indexed_at
                        )
                        VALUES (
                            :id, :full_name, :name, :owner, :description,
                            :primary_language, :default_branch, :is_private, :is_fork,
                            :stars_count, :forks_count, :created_at, :pushed_at,
                            :is_priority, NOW()
                        )
                        ON CONFLICT (id) DO UPDATE SET
                            full_name = EXCLUDED.full_name,
                            name = EXCLUDED.name,
                            description = EXCLUDED.description,
                            primary_language = EXCLUDED.primary_language,
                            pushed_at = EXCLUDED.pushed_at,
                            stars_count = EXCLUDED.stars_count,
                            forks_count = EXCLUDED.forks_count,
                            is_priority = EXCLUDED.is_priority,
                            indexed_at = NOW()
                        """),
                        {
                            "id": int(repo["id"]),
                            "full_name": repo["full_name"],
                            "name": repo["name"],
                            "owner": repo["owner"],
                            "description": repo.get("description"),
                            "primary_language": repo.get("language"),
                            "default_branch": repo.get("default_branch", "main"),
                            "is_private": repo.get("is_private", False),
                            "is_fork": repo.get("is_fork", False),
                            "stars_count": repo.get("stars", 0),
                            "forks_count": repo.get("forks", 0),
                            "created_at": created_at,
                            "pushed_at": pushed_at,
                            "is_priority": is_priority,
                        }
                    )

                    stats["total"] += 1
                    if is_priority:
                        stats["priority"] += 1

                    count += 1

                    if count % 10 == 0:
                        await session.commit()
                        logger.info(
                            "Repo indexing progress",
                            indexed=count,
                            priority=stats["priority"],
                        )

                except Exception as e:
                    stats["errors"] += 1
                    logger.warning(
                        "Failed to index repo",
                        repo=repo.get("full_name"),
                        error=str(e),
                    )

            await session.commit()

        logger.info("GitHub repo metadata indexing complete", **stats)
        return stats

    async def index_repo_files(
        self,
        repo_id: int,
        full_name: str,
        is_priority: bool = False,
    ) -> dict:
        """
        Index file metadata for a specific repository.

        Args:
            repo_id: GitHub repository ID
            full_name: Repository full name (owner/repo)
            is_priority: Whether this is a priority repo

        Returns:
            Stats dict with file counts
        """
        stats = {
            "files_total": 0,
            "files_indexed": 0,
            "errors": 0,
        }

        async for session in get_session():
            try:
                for file_data in self.github.list_files(full_name, recursive=True):
                    stats["files_total"] += 1

                    # Skip non-indexable files
                    if not self._should_index_file(file_data["path"]):
                        continue

                    try:
                        file_id = f"{repo_id}:{file_data['path']}"
                        language = detect_language(file_data["path"])

                        await session.execute(
                            text("""
                            INSERT INTO github_files (
                                id, repo_id, repo_full_name, path, name,
                                language, size_bytes, sha, is_priority, indexed_at
                            )
                            VALUES (
                                :id, :repo_id, :repo_full_name, :path, :name,
                                :language, :size_bytes, :sha, :is_priority, NOW()
                            )
                            ON CONFLICT (id) DO UPDATE SET
                                sha = EXCLUDED.sha,
                                size_bytes = EXCLUDED.size_bytes,
                                is_priority = EXCLUDED.is_priority,
                                indexed_at = NOW()
                            """),
                            {
                                "id": file_id,
                                "repo_id": repo_id,
                                "repo_full_name": full_name,
                                "path": file_data["path"],
                                "name": file_data["name"],
                                "language": language,
                                "size_bytes": file_data.get("size"),
                                "sha": file_data.get("sha"),
                                "is_priority": is_priority,
                            }
                        )
                        stats["files_indexed"] += 1

                    except Exception as e:
                        stats["errors"] += 1
                        logger.warning(
                            "Failed to index file",
                            file=file_data.get("path"),
                            error=str(e),
                        )

                await session.commit()

            except Exception as e:
                stats["errors"] += 1
                logger.error(
                    "Failed to list files for repo",
                    repo=full_name,
                    error=str(e),
                )

        logger.info(
            "Repo file metadata indexing complete",
            repo=full_name,
            **stats,
        )
        return stats

    async def index_priority_repo_files(self) -> dict:
        """Index files for all priority repositories."""
        stats = {
            "repos_processed": 0,
            "files_total": 0,
            "files_indexed": 0,
            "errors": 0,
        }

        async for session in get_session():
            result = await session.execute(
                text("SELECT id, full_name FROM github_repos WHERE is_priority = true")
            )
            priority_repos = result.fetchall()

            for repo in priority_repos:
                repo_stats = await self.index_repo_files(
                    repo_id=repo.id,
                    full_name=repo.full_name,
                    is_priority=True,
                )
                stats["repos_processed"] += 1
                stats["files_total"] += repo_stats["files_total"]
                stats["files_indexed"] += repo_stats["files_indexed"]
                stats["errors"] += repo_stats["errors"]

        logger.info("Priority repo files indexing complete", **stats)
        return stats

    async def get_priority_repos(self) -> list[dict]:
        """Get priority repos from database."""
        async for session in get_session():
            result = await session.execute(
                text("""
                    SELECT id, full_name, name, primary_language, pushed_at
                    FROM github_repos
                    WHERE is_priority = true
                    ORDER BY pushed_at DESC
                """)
            )
            rows = result.mappings().all()
            return [dict(row) for row in rows]
        return []

    async def get_stale_files(
        self,
        limit: int = 100,
        max_file_size: int = 100_000,
    ) -> list[dict]:
        """
        Get priority files that need indexing or re-indexing.

        Returns files where:
        - is_priority = true
        - Not yet in code_content OR sha has changed

        Args:
            limit: Maximum files to return
            max_file_size: Skip files larger than this

        Returns:
            List of file metadata dicts
        """
        async for session in get_session():
            result = await session.execute(
                text("""
                    SELECT
                        gf.id, gf.path, gf.name, gf.language, gf.repo_id,
                        gf.repo_full_name, gf.sha, gf.size_bytes
                    FROM github_files gf
                    LEFT JOIN code_content cc ON gf.id = cc.file_id
                    WHERE gf.is_priority = true
                      AND (gf.size_bytes IS NULL OR gf.size_bytes <= :max_size)
                      AND (cc.file_id IS NULL OR gf.sha != cc.content_hash)
                    ORDER BY gf.indexed_at DESC
                    LIMIT :limit
                """),
                {"max_size": max_file_size, "limit": limit}
            )
            rows = result.mappings().all()
            return [dict(row) for row in rows]
        return []

    async def get_stats(self) -> dict:
        """Get statistics about indexed repositories and files."""
        async for session in get_session():
            result = await session.execute(text("""
                SELECT
                    (SELECT COUNT(*) FROM github_repos) as total_repos,
                    (SELECT COUNT(*) FROM github_repos WHERE is_priority) as priority_repos,
                    (SELECT COUNT(*) FROM github_files) as total_files,
                    (SELECT COUNT(*) FROM github_files WHERE is_priority) as priority_files,
                    (SELECT COUNT(DISTINCT primary_language) FROM github_repos) as languages
            """))
            row = result.mappings().one()
            return dict(row)
        return {}
