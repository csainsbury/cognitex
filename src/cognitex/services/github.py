"""GitHub API service for repository sync and code indexing."""

import base64
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Iterator

import structlog
from github import Github, GithubException
from github.Repository import Repository
from github.ContentFile import ContentFile

from cognitex.config import get_settings

logger = structlog.get_logger()

# File extensions to index for code understanding
CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx",
    ".go", ".rs", ".java", ".kt", ".scala",
    ".c", ".cpp", ".h", ".hpp",
    ".rb", ".php", ".swift", ".m",
    ".sql", ".graphql",
    ".yaml", ".yml", ".json", ".toml",
    ".md", ".rst", ".txt",
    ".sh", ".bash", ".zsh",
    ".html", ".css", ".scss",
    ".dockerfile", ".docker-compose.yml",
}

# Files to always index regardless of extension
IMPORTANT_FILES = {
    "README.md", "README", "readme.md",
    "Makefile", "makefile",
    "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
    "pyproject.toml", "setup.py", "requirements.txt",
    "package.json", "tsconfig.json",
    "Cargo.toml", "go.mod", "go.sum",
    ".env.example", "config.yaml", "config.yml",
}

# Directories to skip
SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    "dist", "build", ".next", ".nuxt", "target",
    ".pytest_cache", ".mypy_cache", ".tox",
    "vendor", "deps", "_build",
}

# Maximum file size to index (in bytes)
MAX_FILE_SIZE = 100_000  # 100KB


class GitHubService:
    """Service for interacting with GitHub API."""

    def __init__(self, token: str | None = None):
        settings = get_settings()
        self.token = token or settings.github_token.get_secret_value()
        if not self.token:
            raise ValueError("GitHub token not configured. Set GITHUB_TOKEN in .env")
        self._client: Github | None = None

    @property
    def client(self) -> Github:
        """Lazy-load the GitHub client."""
        if self._client is None:
            self._client = Github(self.token)
        return self._client

    def get_user(self) -> dict:
        """Get authenticated user info."""
        user = self.client.get_user()
        return {
            "login": user.login,
            "name": user.name,
            "email": user.email,
            "public_repos": user.public_repos,
            "private_repos": user.owned_private_repos,
        }

    def list_repos(
        self,
        include_private: bool = True,
        include_forks: bool = False,
        limit: int = 100,
    ) -> list[dict]:
        """
        List repositories for the authenticated user.

        Args:
            include_private: Include private repositories
            include_forks: Include forked repositories
            limit: Maximum number of repos to return

        Returns:
            List of repository metadata dicts
        """
        user = self.client.get_user()
        repos = []

        visibility = "all" if include_private else "public"

        for repo in user.get_repos(visibility=visibility, sort="updated"):
            if len(repos) >= limit:
                break

            if not include_forks and repo.fork:
                continue

            repos.append(self._repo_to_dict(repo))

        return repos

    def get_repo(self, full_name: str) -> dict | None:
        """
        Get a specific repository by full name (owner/repo).

        Args:
            full_name: Repository full name like 'owner/repo'

        Returns:
            Repository metadata dict or None if not found
        """
        try:
            repo = self.client.get_repo(full_name)
            return self._repo_to_dict(repo)
        except GithubException as e:
            if e.status == 404:
                return None
            raise

    def _repo_to_dict(self, repo: Repository) -> dict:
        """Convert GitHub Repository object to dict."""
        return {
            "id": str(repo.id),
            "full_name": repo.full_name,
            "name": repo.name,
            "owner": repo.owner.login,
            "description": repo.description,
            "url": repo.html_url,
            "clone_url": repo.clone_url,
            "default_branch": repo.default_branch,
            "language": repo.language,
            "languages": self._get_languages(repo),
            "topics": repo.get_topics(),
            "is_private": repo.private,
            "is_fork": repo.fork,
            "created_at": repo.created_at.isoformat() if repo.created_at else None,
            "updated_at": repo.updated_at.isoformat() if repo.updated_at else None,
            "pushed_at": repo.pushed_at.isoformat() if repo.pushed_at else None,
            "size_kb": repo.size,
            "stars": repo.stargazers_count,
            "forks": repo.forks_count,
            "open_issues": repo.open_issues_count,
        }

    def _get_languages(self, repo: Repository) -> dict[str, int]:
        """Get language breakdown for a repository."""
        try:
            return dict(repo.get_languages())
        except GithubException:
            return {}

    def list_files(
        self,
        full_name: str,
        path: str = "",
        ref: str | None = None,
        recursive: bool = True,
    ) -> Iterator[dict]:
        """
        List files in a repository.

        Args:
            full_name: Repository full name
            path: Path within the repo (empty for root)
            ref: Branch/tag/commit ref (default: default branch)
            recursive: Recursively list files in subdirectories

        Yields:
            File metadata dicts
        """
        repo = self.client.get_repo(full_name)
        ref = ref or repo.default_branch

        try:
            contents = repo.get_contents(path, ref=ref)
        except GithubException as e:
            logger.warning("Failed to get contents", path=path, error=str(e))
            return

        # Handle single file case
        if not isinstance(contents, list):
            contents = [contents]

        for content in contents:
            if content.type == "dir":
                # Skip unwanted directories
                if content.name in SKIP_DIRS:
                    continue

                if recursive:
                    yield from self.list_files(full_name, content.path, ref, recursive)
            else:
                yield self._content_to_dict(content, full_name)

    def _content_to_dict(self, content: ContentFile, full_name: str) -> dict:
        """Convert GitHub ContentFile to dict."""
        return {
            "path": content.path,
            "name": content.name,
            "sha": content.sha,
            "size": content.size,
            "type": content.type,
            "url": content.html_url,
            "download_url": content.download_url,
            "repository": full_name,
        }

    def get_file_content(
        self,
        full_name: str,
        path: str,
        ref: str | None = None,
    ) -> str | None:
        """
        Get the content of a file.

        Args:
            full_name: Repository full name
            path: Path to file within repo
            ref: Branch/tag/commit ref

        Returns:
            File content as string, or None if not found/binary
        """
        repo = self.client.get_repo(full_name)
        ref = ref or repo.default_branch

        try:
            content = repo.get_contents(path, ref=ref)
            if isinstance(content, list):
                return None  # It's a directory

            if content.size > MAX_FILE_SIZE:
                logger.debug("File too large to index", path=path, size=content.size)
                return None

            # Decode content
            if content.encoding == "base64":
                try:
                    return base64.b64decode(content.content).decode("utf-8")
                except UnicodeDecodeError:
                    logger.debug("Binary file, skipping", path=path)
                    return None
            elif content.content:
                return content.content

            return None

        except GithubException as e:
            logger.warning("Failed to get file content", path=path, error=str(e))
            return None

    def should_index_file(self, file_info: dict) -> bool:
        """
        Determine if a file should be indexed for code understanding.

        Args:
            file_info: File metadata dict from list_files

        Returns:
            True if file should be indexed
        """
        name = file_info["name"]
        path = file_info["path"]
        size = file_info.get("size", 0)

        # Skip large files
        if size > MAX_FILE_SIZE:
            return False

        # Always index important files
        if name in IMPORTANT_FILES:
            return True

        # Check extension
        ext = Path(name).suffix.lower()
        if ext in CODE_EXTENSIONS:
            return True

        # Check for Dockerfile without extension
        if name.lower().startswith("dockerfile"):
            return True

        return False

    def get_indexable_files(
        self,
        full_name: str,
        ref: str | None = None,
    ) -> Iterator[dict]:
        """
        Get all files that should be indexed from a repository.

        Args:
            full_name: Repository full name
            ref: Branch/tag/commit ref

        Yields:
            File metadata dicts for indexable files
        """
        for file_info in self.list_files(full_name, ref=ref):
            if self.should_index_file(file_info):
                yield file_info

    def get_repo_structure(
        self,
        full_name: str,
        ref: str | None = None,
        max_depth: int = 3,
    ) -> dict:
        """
        Get a summary of repository structure.

        Args:
            full_name: Repository full name
            ref: Branch/tag/commit ref
            max_depth: Maximum directory depth to include

        Returns:
            Dict with repository structure summary
        """
        repo = self.client.get_repo(full_name)
        ref = ref or repo.default_branch

        structure = {
            "directories": [],
            "top_level_files": [],
            "languages": {},
            "file_count": 0,
        }

        def process_dir(path: str, depth: int):
            if depth > max_depth:
                return

            try:
                contents = repo.get_contents(path, ref=ref)
            except GithubException:
                return

            if not isinstance(contents, list):
                contents = [contents]

            for content in contents:
                if content.type == "dir":
                    if content.name not in SKIP_DIRS:
                        structure["directories"].append(content.path)
                        process_dir(content.path, depth + 1)
                else:
                    structure["file_count"] += 1
                    if depth == 0:
                        structure["top_level_files"].append(content.name)

                    # Track languages
                    ext = Path(content.name).suffix.lower()
                    if ext:
                        structure["languages"][ext] = structure["languages"].get(ext, 0) + 1

        process_dir("", 0)

        return structure


def get_github_service() -> GitHubService:
    """Get a GitHub service instance."""
    return GitHubService()
