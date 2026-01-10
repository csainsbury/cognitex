"""Coding CLI session ingestion for development context.

Ingests sessions from AI coding assistants (Claude Code, etc.) to provide
rich context about project development progress, decisions, and next steps.
"""

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog
from neo4j import AsyncSession

from cognitex.config import get_settings
from cognitex.db.neo4j import get_driver
from cognitex.services.llm import LLMService

logger = structlog.get_logger()

# Supported CLI tools and their session locations
CLI_SESSION_PATHS = {
    "claude": Path.home() / ".claude" / "projects",
    # Future: Add other coding CLIs as they expose session data
    # "codex": Path.home() / ".codex" / "sessions",
    # "gemini": Path.home() / ".gemini-cli" / "history",
}


class CodingSessionIngester:
    """Ingests and indexes coding CLI sessions."""

    def __init__(self):
        self.llm = LLMService()
        self._processed_sessions: set[str] = set()
        self._last_positions: dict[str, int] = {}  # Track file positions for incremental reads

    async def discover_sessions(self, cli_type: str = "claude") -> list[dict]:
        """Discover available coding sessions from CLI tools.

        Args:
            cli_type: Which CLI to scan (default: claude)

        Returns:
            List of session metadata dicts
        """
        sessions = []
        base_path = CLI_SESSION_PATHS.get(cli_type)

        if not base_path or not base_path.exists():
            logger.debug("CLI session path not found", cli=cli_type, path=str(base_path))
            return sessions

        # Each subdirectory is a project (path-encoded)
        for project_dir in base_path.iterdir():
            if not project_dir.is_dir():
                continue

            # Decode project path from directory name
            project_path = "/" + project_dir.name.replace("-", "/")

            # Find session files
            for session_file in project_dir.glob("*.jsonl"):
                stat = session_file.stat()
                sessions.append({
                    "cli_type": cli_type,
                    "session_file": str(session_file),
                    "session_id": session_file.stem,
                    "project_path": project_path,
                    "size_bytes": stat.st_size,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime),
                })

        logger.info("Discovered coding sessions", cli=cli_type, count=len(sessions))
        return sessions

    async def parse_session_file(
        self,
        session_file: str,
        incremental: bool = True,
    ) -> list[dict]:
        """Parse a session JSONL file.

        Args:
            session_file: Path to the JSONL session file
            incremental: If True, only read new lines since last parse

        Returns:
            List of message dicts
        """
        path = Path(session_file)

        if not path.exists():
            return []

        # Get starting position for incremental reads
        start_pos = 0
        if incremental and session_file in self._last_positions:
            start_pos = self._last_positions[session_file]

        def _read_file() -> tuple[list[dict], int]:
            """Blocking file read - runs in thread pool."""
            messages = []
            end_pos = start_pos
            try:
                with open(path, "r") as f:
                    if start_pos > 0:
                        f.seek(start_pos)

                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            msg = json.loads(line)
                            messages.append(msg)
                        except json.JSONDecodeError:
                            continue

                    end_pos = f.tell()
            except Exception as e:
                logger.error("Failed to parse session file", file=session_file, error=str(e))

            return messages, end_pos

        # Run blocking file I/O in thread pool
        messages, end_pos = await asyncio.to_thread(_read_file)

        # Save position for next incremental read
        self._last_positions[session_file] = end_pos

        return messages

    async def extract_session_summary(
        self,
        messages: list[dict],
        project_path: str,
    ) -> dict:
        """Use LLM to extract key information from session messages.

        Returns:
            Dict with: summary, decisions, files_changed, next_steps, topics
        """
        if not messages:
            return {}

        # Build conversation text (limit to avoid token overflow)
        conversation_parts = []
        token_estimate = 0
        max_tokens = 8000  # Leave room for prompt and response

        for msg in messages:
            if msg.get("type") not in ("user", "assistant"):
                continue

            content = msg.get("message", {})
            if isinstance(content, dict):
                role = content.get("role", msg.get("type", "unknown"))
                text = ""
                if "content" in content:
                    if isinstance(content["content"], str):
                        text = content["content"]
                    elif isinstance(content["content"], list):
                        text = " ".join(
                            c.get("text", "") for c in content["content"]
                            if isinstance(c, dict) and c.get("type") == "text"
                        )
            else:
                role = msg.get("type", "unknown")
                text = str(content)

            # Rough token estimate
            msg_tokens = len(text) // 4
            if token_estimate + msg_tokens > max_tokens:
                break

            conversation_parts.append(f"[{role}]: {text[:2000]}")
            token_estimate += msg_tokens

        if not conversation_parts:
            return {}

        conversation_text = "\n\n".join(conversation_parts[-20:])  # Last 20 messages

        prompt = f"""Analyze this coding session conversation and extract key information.

Project: {project_path}

Conversation:
{conversation_text}

Extract and return as JSON:
{{
    "summary": "2-3 sentence summary of what was accomplished",
    "decisions": ["list of technical decisions made"],
    "files_changed": ["list of files modified or discussed"],
    "next_steps": ["list of pending work or planned next steps"],
    "topics": ["key technical topics/technologies discussed"],
    "completion_state": "completed|in_progress|blocked|abandoned"
}}

Return ONLY valid JSON, no other text."""

        try:
            result = await self.llm.complete(prompt, model=self.llm.fast_model)
            # Parse JSON from response
            result = result.strip()
            if result.startswith("```"):
                result = result.split("```")[1]
                if result.startswith("json"):
                    result = result[4:]
            return json.loads(result)
        except Exception as e:
            logger.warning("Failed to extract session summary", error=str(e))
            return {}

    async def ingest_session(
        self,
        session_meta: dict,
        force: bool = False,
    ) -> dict | None:
        """Ingest a single coding session into the graph.

        Args:
            session_meta: Session metadata from discover_sessions()
            force: If True, re-process even if already ingested

        Returns:
            The created/updated CodingSession node data, or None if skipped
        """
        session_id = session_meta["session_id"]
        session_file = session_meta["session_file"]

        # Check if already processed (unless forced)
        if not force and session_id in self._processed_sessions:
            return None

        # Parse messages
        messages = await self.parse_session_file(session_file, incremental=not force)
        if not messages:
            return None

        # Extract session metadata from first message
        first_msg = messages[0]
        git_branch = first_msg.get("gitBranch", "unknown")
        session_slug = first_msg.get("slug", session_id[:8])
        cwd = first_msg.get("cwd", session_meta["project_path"])

        # Get timestamps
        timestamps = [
            msg.get("timestamp") for msg in messages
            if msg.get("timestamp")
        ]
        started_at = min(timestamps) if timestamps else None
        ended_at = max(timestamps) if timestamps else None

        # Extract summary using LLM
        summary_data = await self.extract_session_summary(messages, cwd)

        # Count messages by type
        user_msgs = sum(1 for m in messages if m.get("type") == "user")
        assistant_msgs = sum(1 for m in messages if m.get("type") == "assistant")

        # Store in Neo4j
        driver = get_driver()
        async with driver.session() as neo_session:
            result = await self._store_session(
                neo_session,
                session_id=session_id,
                cli_type=session_meta["cli_type"],
                project_path=cwd,
                git_branch=git_branch,
                slug=session_slug,
                started_at=started_at,
                ended_at=ended_at,
                user_messages=user_msgs,
                assistant_messages=assistant_msgs,
                summary=summary_data.get("summary"),
                decisions=summary_data.get("decisions", []),
                files_changed=summary_data.get("files_changed", []),
                next_steps=summary_data.get("next_steps", []),
                topics=summary_data.get("topics", []),
                completion_state=summary_data.get("completion_state", "unknown"),
            )

        self._processed_sessions.add(session_id)

        logger.info(
            "Ingested coding session",
            session_id=session_id,
            project=cwd,
            messages=user_msgs + assistant_msgs,
            summary=summary_data.get("summary", "")[:100],
        )

        return result

    async def _store_session(
        self,
        session: AsyncSession,
        session_id: str,
        cli_type: str,
        project_path: str,
        git_branch: str,
        slug: str,
        started_at: str | None,
        ended_at: str | None,
        user_messages: int,
        assistant_messages: int,
        summary: str | None,
        decisions: list[str],
        files_changed: list[str],
        next_steps: list[str],
        topics: list[str],
        completion_state: str,
    ) -> dict:
        """Store coding session in Neo4j and link to project."""

        # Create/update the CodingSession node
        query = """
        MERGE (cs:CodingSession {session_id: $session_id})
        ON CREATE SET
            cs.cli_type = $cli_type,
            cs.project_path = $project_path,
            cs.git_branch = $git_branch,
            cs.slug = $slug,
            cs.started_at = $started_at,
            cs.ended_at = $ended_at,
            cs.user_messages = $user_messages,
            cs.assistant_messages = $assistant_messages,
            cs.summary = $summary,
            cs.decisions = $decisions,
            cs.files_changed = $files_changed,
            cs.next_steps = $next_steps,
            cs.topics = $topics,
            cs.completion_state = $completion_state,
            cs.created_at = datetime(),
            cs.updated_at = datetime()
        ON MATCH SET
            cs.ended_at = $ended_at,
            cs.user_messages = $user_messages,
            cs.assistant_messages = $assistant_messages,
            cs.summary = COALESCE($summary, cs.summary),
            cs.decisions = CASE WHEN size($decisions) > 0 THEN $decisions ELSE cs.decisions END,
            cs.files_changed = CASE WHEN size($files_changed) > 0 THEN $files_changed ELSE cs.files_changed END,
            cs.next_steps = CASE WHEN size($next_steps) > 0 THEN $next_steps ELSE cs.next_steps END,
            cs.topics = CASE WHEN size($topics) > 0 THEN $topics ELSE cs.topics END,
            cs.completion_state = $completion_state,
            cs.updated_at = datetime()
        RETURN cs
        """

        result = await session.run(
            query,
            session_id=session_id,
            cli_type=cli_type,
            project_path=project_path,
            git_branch=git_branch,
            slug=slug,
            started_at=started_at,
            ended_at=ended_at,
            user_messages=user_messages,
            assistant_messages=assistant_messages,
            summary=summary,
            decisions=decisions,
            files_changed=files_changed,
            next_steps=next_steps,
            topics=topics,
            completion_state=completion_state,
        )
        record = await result.single()
        session_data = dict(record["cs"]) if record else {}

        # Try to link to a Project node based on project_path
        await self._link_to_project(session, session_id, project_path)

        # Link to Repository if git repo
        await self._link_to_repository(session, session_id, project_path)

        return session_data

    async def _link_to_project(
        self,
        session: AsyncSession,
        session_id: str,
        project_path: str,
    ) -> None:
        """Link coding session to matching Project node."""

        # Normalize path (remove double slashes, trailing slashes)
        normalized_path = project_path.replace("//", "/").rstrip("/").lower()

        # Try to find a project that matches the path
        # Match by: local_path, or project title appears in path
        query = """
        MATCH (cs:CodingSession {session_id: $session_id})
        MATCH (p:Project)
        WHERE p.local_path = $project_path
           OR $normalized_path CONTAINS toLower(p.title)
           OR $normalized_path CONTAINS toLower(p.name)
        MERGE (cs)-[:DEVELOPS]->(p)
        RETURN COALESCE(p.title, p.name) as project_name
        """

        result = await session.run(
            query,
            session_id=session_id,
            project_path=project_path,
            normalized_path=normalized_path,
        )
        record = await result.single()
        if record:
            logger.debug(
                "Linked session to project",
                session=session_id,
                project=record["project_name"],
            )

    async def _link_to_repository(
        self,
        session: AsyncSession,
        session_id: str,
        project_path: str,
    ) -> None:
        """Link coding session to matching Repository node."""

        # Extract repo name from path (last component)
        repo_name = Path(project_path).name

        query = """
        MATCH (cs:CodingSession {session_id: $session_id})
        MATCH (r:Repository)
        WHERE r.name = $repo_name
           OR r.full_name ENDS WITH ('/' + $repo_name)
        MERGE (cs)-[:WORKS_ON]->(r)
        RETURN r.name as repo_name
        """

        result = await session.run(query, session_id=session_id, repo_name=repo_name)
        record = await result.single()
        if record:
            logger.debug(
                "Linked session to repository",
                session=session_id,
                repo=record["repo_name"],
            )

    async def sync_all_sessions(self, cli_type: str = "claude") -> dict:
        """Sync all sessions from a CLI tool.

        Returns:
            Stats dict with counts
        """
        sessions = await self.discover_sessions(cli_type)

        ingested = 0
        skipped = 0
        errors = 0

        for session_meta in sessions:
            try:
                result = await self.ingest_session(session_meta)
                if result:
                    ingested += 1
                else:
                    skipped += 1
            except Exception as e:
                logger.error(
                    "Failed to ingest session",
                    session=session_meta["session_id"],
                    error=str(e),
                )
                errors += 1

        logger.info(
            "Session sync complete",
            cli=cli_type,
            ingested=ingested,
            skipped=skipped,
            errors=errors,
        )

        return {
            "cli_type": cli_type,
            "discovered": len(sessions),
            "ingested": ingested,
            "skipped": skipped,
            "errors": errors,
        }

    async def get_project_development_context(
        self,
        project_name: str,
        limit: int = 5,
    ) -> list[dict]:
        """Get recent coding session context for a project.

        Useful for building context packs for development discussions.
        """
        driver = get_driver()
        async with driver.session() as session:
            query = """
            MATCH (cs:CodingSession)-[:DEVELOPS]->(p:Project)
            WHERE p.name = $project_name OR p.id = $project_name
            RETURN cs
            ORDER BY cs.ended_at DESC
            LIMIT $limit
            """

            result = await session.run(query, project_name=project_name, limit=limit)
            records = await result.values()

            return [dict(r[0]) for r in records]

    async def get_development_next_steps(self, project_name: str) -> list[str]:
        """Get aggregated next steps from recent coding sessions."""
        sessions = await self.get_project_development_context(project_name, limit=3)

        next_steps = []
        for s in sessions:
            steps = s.get("next_steps", [])
            if isinstance(steps, list):
                next_steps.extend(steps)

        # Deduplicate while preserving order
        seen = set()
        unique_steps = []
        for step in next_steps:
            if step not in seen:
                seen.add(step)
                unique_steps.append(step)

        return unique_steps

    async def process_sync_batch(
        self,
        machine_id: str,
        cli_type: str,
        sessions: list[dict],
    ) -> dict:
        """Process a batch of sessions from remote sync.

        This is the canonical implementation used by both web app and API endpoints.
        Processes sessions in background to avoid HTTP timeouts.

        Args:
            machine_id: Identifier for the source machine
            cli_type: Type of CLI (e.g., 'claude')
            sessions: List of session data dicts

        Returns:
            Dict with ingested count and errors
        """
        driver = get_driver()
        ingested = 0
        errors = []

        for session_data in sessions:
            try:
                session_id = session_data.get("session_id")
                if not session_id:
                    errors.append({"error": "Missing session_id"})
                    continue

                # Check if we have pre-extracted summary or need to extract from messages
                summary = session_data.get("summary")
                decisions = session_data.get("decisions", [])
                next_steps = session_data.get("next_steps", [])
                topics = session_data.get("topics", [])
                files_changed = session_data.get("files_changed", [])
                completion_state = session_data.get("completion_state", "unknown")

                # If no summary but messages provided, extract using LLM
                if not summary and session_data.get("messages"):
                    extracted = await self.extract_session_summary(
                        session_data["messages"],
                        session_data.get("project_path", "unknown"),
                    )
                    summary = extracted.get("summary")
                    decisions = extracted.get("decisions", decisions)
                    next_steps = extracted.get("next_steps", next_steps)
                    topics = extracted.get("topics", topics)
                    files_changed = extracted.get("files_changed", files_changed)
                    completion_state = extracted.get("completion_state", completion_state)

                # Store in Neo4j
                async with driver.session() as neo_session:
                    await self._store_session(
                        neo_session,
                        session_id=f"{machine_id}:{session_id}",  # Namespace by machine
                        cli_type=cli_type,
                        project_path=session_data.get("project_path", "unknown"),
                        git_branch=session_data.get("git_branch", "unknown"),
                        slug=session_data.get("slug", session_id[:8]),
                        started_at=session_data.get("started_at"),
                        ended_at=session_data.get("ended_at"),
                        user_messages=session_data.get("user_messages", 0),
                        assistant_messages=session_data.get("assistant_messages", 0),
                        summary=summary,
                        decisions=decisions,
                        files_changed=files_changed,
                        next_steps=next_steps,
                        topics=topics,
                        completion_state=completion_state,
                    )

                ingested += 1

            except Exception as e:
                errors.append({
                    "session_id": session_data.get("session_id"),
                    "error": str(e),
                })
                logger.error(
                    "Session sync failed",
                    session_id=session_data.get("session_id"),
                    error=str(e),
                )

        logger.info(
            "Session sync batch completed",
            machine_id=machine_id,
            ingested=ingested,
            errors=len(errors),
        )

        return {"ingested": ingested, "errors": errors}


# Module-level singleton
_ingester: CodingSessionIngester | None = None


def get_session_ingester() -> CodingSessionIngester:
    """Get or create the session ingester singleton."""
    global _ingester
    if _ingester is None:
        _ingester = CodingSessionIngester()
    return _ingester
