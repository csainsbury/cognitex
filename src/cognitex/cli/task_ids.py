"""Short ID management for CLI task interaction.

Maps short numeric IDs (1, 2, 3...) to full task UUIDs for easier CLI usage.
IDs are session-based and stored in Redis with a TTL.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redis.asyncio import Redis

# Redis key for the short ID mapping
SHORT_ID_KEY = "cognitex:cli:task_ids"
SHORT_ID_TTL = 3600 * 4  # 4 hours


async def store_task_ids(redis: "Redis", task_ids: list[str]) -> dict[int, str]:
    """
    Store a list of task IDs and return the short ID mapping.

    Args:
        redis: Redis client
        task_ids: List of full task UUIDs in display order

    Returns:
        Dict mapping short ID (1-based) to full UUID
    """
    mapping = {i + 1: task_id for i, task_id in enumerate(task_ids)}

    # Store in Redis as JSON
    await redis.setex(
        SHORT_ID_KEY,
        SHORT_ID_TTL,
        json.dumps(mapping),
    )

    return mapping


async def get_task_id_mapping(redis: "Redis") -> dict[int, str]:
    """
    Get the current short ID to full UUID mapping.

    Returns:
        Dict mapping short ID to full UUID, or empty dict if not found
    """
    data = await redis.get(SHORT_ID_KEY)
    if data:
        # Keys come back as strings from JSON, convert to int
        raw = json.loads(data)
        return {int(k): v for k, v in raw.items()}
    return {}


async def resolve_task_id(redis: "Redis", id_or_short: str) -> str | None:
    """
    Resolve a task identifier to a full UUID.

    Accepts either:
    - A short numeric ID (e.g., "1", "2", "15")
    - A full or partial UUID (e.g., "task_abc123")
    - A partial match on the UUID suffix

    Args:
        redis: Redis client
        id_or_short: The identifier to resolve

    Returns:
        Full task UUID or None if not found
    """
    # Check if it's a short numeric ID
    if id_or_short.isdigit():
        short_id = int(id_or_short)
        mapping = await get_task_id_mapping(redis)
        if short_id in mapping:
            return mapping[short_id]
        return None

    # Check if it starts with "task_" - likely a full UUID
    if id_or_short.startswith("task_"):
        return id_or_short

    # Try to match as a partial UUID suffix
    mapping = await get_task_id_mapping(redis)
    for full_id in mapping.values():
        if full_id.endswith(id_or_short) or id_or_short in full_id:
            return full_id

    return None


async def resolve_task_id_or_search(
    redis: "Redis",
    id_or_title: str,
    neo4j_session=None,
) -> str | None:
    """
    Resolve a task identifier, falling back to title search.

    Args:
        redis: Redis client
        id_or_title: Short ID, UUID, or title search string
        neo4j_session: Optional Neo4j session for title search

    Returns:
        Full task UUID or None if not found
    """
    # First try direct ID resolution
    resolved = await resolve_task_id(redis, id_or_title)
    if resolved:
        return resolved

    # Fall back to fuzzy title search if we have a session
    if neo4j_session:
        query = """
        MATCH (t:Task)
        WHERE toLower(t.title) CONTAINS toLower($search)
        RETURN t.id as id, t.title as title
        ORDER BY
            CASE WHEN toLower(t.title) = toLower($search) THEN 0 ELSE 1 END,
            t.created_at DESC
        LIMIT 1
        """
        result = await neo4j_session.run(query, {"search": id_or_title})
        record = await result.single()
        if record:
            return record["id"]

    return None
