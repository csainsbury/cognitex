"""Ideas service - scratch pad for quick capture of thoughts and ideas.

Ideas are lightweight nodes that can be quickly captured from:
- Web form
- Email (subject starting with "idea:")
- API endpoint (for mobile shortcuts)

Ideas can later be triaged: converted to tasks, linked to projects, or dismissed.
"""

import json
import secrets
from datetime import datetime
from typing import Literal

import structlog

from cognitex.db.neo4j import get_neo4j_session

logger = structlog.get_logger()

IdeaStatus = Literal["pending", "triaged", "converted", "dismissed"]
IdeaSource = Literal["web", "email", "api", "voice"]


def generate_idea_id() -> str:
    """Generate a unique idea ID."""
    return f"idea_{secrets.token_hex(6)}"


async def create_idea(
    text: str,
    source: IdeaSource = "web",
    source_ref: str | None = None,
    tags: list[str] | None = None,
) -> dict:
    """
    Create a new idea.

    Args:
        text: The idea content
        source: Where the idea came from (web, email, api, voice)
        source_ref: Optional reference (e.g., gmail_id for email-sourced ideas)
        tags: Optional list of tags

    Returns:
        The created idea as a dict
    """
    idea_id = generate_idea_id()
    now = datetime.utcnow().isoformat()

    query = """
    CREATE (i:Idea {
        id: $id,
        text: $text,
        source: $source,
        source_ref: $source_ref,
        tags: $tags,
        status: 'pending',
        created_at: datetime($created_at),
        updated_at: datetime($created_at)
    })
    RETURN i {
        .id, .text, .source, .source_ref, .tags, .status,
        created_at: toString(i.created_at),
        updated_at: toString(i.updated_at)
    } as idea
    """

    async for session in get_neo4j_session():
        result = await session.run(query, {
            "id": idea_id,
            "text": text.strip(),
            "source": source,
            "source_ref": source_ref,
            "tags": json.dumps(tags or []),
            "created_at": now,
        })
        record = await result.single()
        idea = record["idea"]
        logger.info("Idea created", idea_id=idea_id, source=source)
        return idea

    return {}


async def list_ideas(
    status: IdeaStatus | None = None,
    source: IdeaSource | None = None,
    limit: int = 50,
) -> list[dict]:
    """
    List ideas with optional filtering.

    Args:
        status: Filter by status (pending, triaged, converted, dismissed)
        source: Filter by source (web, email, api, voice)
        limit: Maximum number of ideas to return

    Returns:
        List of idea dicts
    """
    conditions = []
    params = {"limit": limit}

    if status:
        conditions.append("i.status = $status")
        params["status"] = status

    if source:
        conditions.append("i.source = $source")
        params["source"] = source

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    query = f"""
    MATCH (i:Idea)
    {where_clause}
    OPTIONAL MATCH (i)-[:CONVERTED_TO]->(t:Task)
    OPTIONAL MATCH (i)-[:LINKED_TO]->(p:Project)
    RETURN i {{
        .id, .text, .source, .source_ref, .tags, .status,
        created_at: toString(i.created_at),
        updated_at: toString(i.updated_at)
    }} as idea,
    t.id as converted_task_id,
    t.title as converted_task_title,
    p.id as linked_project_id,
    p.title as linked_project_title
    ORDER BY i.created_at DESC
    LIMIT $limit
    """

    async for session in get_neo4j_session():
        result = await session.run(query, params)
        data = await result.data()

        ideas = []
        for row in data:
            idea = row["idea"]
            # Parse tags JSON
            if idea.get("tags"):
                try:
                    idea["tags"] = json.loads(idea["tags"])
                except (json.JSONDecodeError, TypeError):
                    idea["tags"] = []
            else:
                idea["tags"] = []

            # Add conversion/link info
            if row.get("converted_task_id"):
                idea["converted_to"] = {
                    "id": row["converted_task_id"],
                    "title": row["converted_task_title"],
                }
            if row.get("linked_project_id"):
                idea["linked_to"] = {
                    "id": row["linked_project_id"],
                    "title": row["linked_project_title"],
                }

            ideas.append(idea)

        return ideas

    return []


async def get_idea(idea_id: str) -> dict | None:
    """Get a single idea by ID."""
    query = """
    MATCH (i:Idea {id: $id})
    OPTIONAL MATCH (i)-[:CONVERTED_TO]->(t:Task)
    OPTIONAL MATCH (i)-[:LINKED_TO]->(p:Project)
    RETURN i {
        .id, .text, .source, .source_ref, .tags, .status,
        created_at: toString(i.created_at),
        updated_at: toString(i.updated_at)
    } as idea,
    t.id as converted_task_id,
    t.title as converted_task_title,
    p.id as linked_project_id,
    p.title as linked_project_title
    """

    async for session in get_neo4j_session():
        result = await session.run(query, {"id": idea_id})
        record = await result.single()
        if not record:
            return None

        idea = record["idea"]
        if idea.get("tags"):
            try:
                idea["tags"] = json.loads(idea["tags"])
            except (json.JSONDecodeError, TypeError):
                idea["tags"] = []

        if record.get("converted_task_id"):
            idea["converted_to"] = {
                "id": record["converted_task_id"],
                "title": record["converted_task_title"],
            }
        if record.get("linked_project_id"):
            idea["linked_to"] = {
                "id": record["linked_project_id"],
                "title": record["linked_project_title"],
            }

        return idea

    return None


async def update_idea(idea_id: str, text: str | None = None, tags: list[str] | None = None) -> dict | None:
    """Update an idea's text or tags."""
    updates = ["i.updated_at = datetime()"]
    params = {"id": idea_id}

    if text is not None:
        updates.append("i.text = $text")
        params["text"] = text.strip()

    if tags is not None:
        updates.append("i.tags = $tags")
        params["tags"] = json.dumps(tags)

    query = f"""
    MATCH (i:Idea {{id: $id}})
    SET {', '.join(updates)}
    RETURN i {{
        .id, .text, .source, .tags, .status,
        created_at: toString(i.created_at),
        updated_at: toString(i.updated_at)
    }} as idea
    """

    async for session in get_neo4j_session():
        result = await session.run(query, params)
        record = await result.single()
        if record:
            idea = record["idea"]
            if idea.get("tags"):
                try:
                    idea["tags"] = json.loads(idea["tags"])
                except (json.JSONDecodeError, TypeError):
                    idea["tags"] = []
            return idea
        return None

    return None


async def dismiss_idea(idea_id: str) -> bool:
    """Mark an idea as dismissed."""
    query = """
    MATCH (i:Idea {id: $id})
    SET i.status = 'dismissed', i.updated_at = datetime()
    RETURN i.id as id
    """

    async for session in get_neo4j_session():
        result = await session.run(query, {"id": idea_id})
        record = await result.single()
        if record:
            logger.info("Idea dismissed", idea_id=idea_id)
            return True
        return False

    return False


async def delete_idea(idea_id: str) -> bool:
    """Permanently delete an idea."""
    query = """
    MATCH (i:Idea {id: $id})
    DETACH DELETE i
    RETURN count(*) as deleted
    """

    async for session in get_neo4j_session():
        result = await session.run(query, {"id": idea_id})
        record = await result.single()
        if record and record["deleted"] > 0:
            logger.info("Idea deleted", idea_id=idea_id)
            return True
        return False

    return False


async def convert_to_task(
    idea_id: str,
    title: str | None = None,
    project_id: str | None = None,
    priority: str = "medium",
) -> dict | None:
    """
    Convert an idea to a task.

    Args:
        idea_id: The idea to convert
        title: Task title (defaults to idea text)
        project_id: Optional project to link the task to
        priority: Task priority (low, medium, high, critical)

    Returns:
        The created task dict, or None if failed
    """
    from cognitex.services.tasks import get_task_service

    # Get the idea first
    idea = await get_idea(idea_id)
    if not idea:
        return None

    # Create the task
    task_service = get_task_service()
    task_title = title or idea["text"][:200]  # Truncate if needed
    task = await task_service.create(
        title=task_title,
        description=f"Converted from idea: {idea['text']}" if title else None,
        priority=priority,
        project_id=project_id,
    )

    if not task:
        return None

    # Create the CONVERTED_TO relationship and update status
    query = """
    MATCH (i:Idea {id: $idea_id})
    MATCH (t:Task {id: $task_id})
    MERGE (i)-[:CONVERTED_TO]->(t)
    SET i.status = 'converted', i.updated_at = datetime()
    RETURN t.id as task_id
    """

    async for session in get_neo4j_session():
        await session.run(query, {"idea_id": idea_id, "task_id": task["id"]})
        logger.info("Idea converted to task", idea_id=idea_id, task_id=task["id"])
        return task

    return None


async def link_to_project(idea_id: str, project_id: str) -> bool:
    """Link an idea to a project for context."""
    query = """
    MATCH (i:Idea {id: $idea_id})
    MATCH (p:Project {id: $project_id})
    MERGE (i)-[:LINKED_TO]->(p)
    SET i.status = 'triaged', i.updated_at = datetime()
    RETURN i.id as id
    """

    async for session in get_neo4j_session():
        result = await session.run(query, {"idea_id": idea_id, "project_id": project_id})
        record = await result.single()
        if record:
            logger.info("Idea linked to project", idea_id=idea_id, project_id=project_id)
            return True
        return False

    return False


async def get_idea_stats() -> dict:
    """Get statistics about ideas."""
    query = """
    MATCH (i:Idea)
    RETURN
        count(*) as total,
        count(CASE WHEN i.status = 'pending' THEN 1 END) as pending,
        count(CASE WHEN i.status = 'triaged' THEN 1 END) as triaged,
        count(CASE WHEN i.status = 'converted' THEN 1 END) as converted,
        count(CASE WHEN i.status = 'dismissed' THEN 1 END) as dismissed,
        count(CASE WHEN i.source = 'web' THEN 1 END) as from_web,
        count(CASE WHEN i.source = 'email' THEN 1 END) as from_email,
        count(CASE WHEN i.source = 'api' THEN 1 END) as from_api
    """

    async for session in get_neo4j_session():
        result = await session.run(query)
        record = await result.single()
        return dict(record) if record else {}

    return {}
