"""Short ID management for CLI interaction.

Maps short numeric IDs (1, 2, 3...) to full UUIDs for easier CLI usage.
IDs are session-based and stored in Redis with a TTL.

Supports tasks, projects, and goals with separate namespaces.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redis.asyncio import Redis

# Redis key prefix and TTL
SHORT_ID_PREFIX = "cognitex:cli:ids"
SHORT_ID_TTL = 3600 * 4  # 4 hours


async def store_ids(redis: "Redis", entity_type: str, ids: list[str]) -> dict[int, str]:
    """
    Store a list of IDs and return the short ID mapping.

    Args:
        redis: Redis client
        entity_type: Type of entity (task, project, goal)
        ids: List of full UUIDs in display order

    Returns:
        Dict mapping short ID (1-based) to full UUID
    """
    mapping = {i + 1: entity_id for i, entity_id in enumerate(ids)}

    key = f"{SHORT_ID_PREFIX}:{entity_type}"
    await redis.setex(key, SHORT_ID_TTL, json.dumps(mapping))

    return mapping


async def get_id_mapping(redis: "Redis", entity_type: str) -> dict[int, str]:
    """
    Get the current short ID to full UUID mapping.

    Returns:
        Dict mapping short ID to full UUID, or empty dict if not found
    """
    key = f"{SHORT_ID_PREFIX}:{entity_type}"
    data = await redis.get(key)
    if data:
        raw = json.loads(data)
        return {int(k): v for k, v in raw.items()}
    return {}


async def resolve_id(redis: "Redis", entity_type: str, id_or_short: str, prefix: str = "") -> str | None:
    """
    Resolve an identifier to a full UUID.

    Accepts either:
    - A short numeric ID (e.g., "1", "2", "15")
    - A full or partial UUID (e.g., "task_abc123", "proj_xyz")
    - A partial match on the UUID suffix

    Args:
        redis: Redis client
        entity_type: Type of entity (task, project, goal)
        id_or_short: The identifier to resolve
        prefix: Expected prefix for full IDs (e.g., "task_", "proj_", "goal_")

    Returns:
        Full UUID or None if not found
    """
    # Check if it's a short numeric ID
    if id_or_short.isdigit():
        short_id = int(id_or_short)
        mapping = await get_id_mapping(redis, entity_type)
        if short_id in mapping:
            return mapping[short_id]
        return None

    # Check if it starts with the expected prefix - likely a full UUID
    if prefix and id_or_short.startswith(prefix):
        return id_or_short

    # Try to match as a partial UUID suffix
    mapping = await get_id_mapping(redis, entity_type)
    for full_id in mapping.values():
        if full_id.endswith(id_or_short) or id_or_short in full_id:
            return full_id

    return None


# Convenience functions for each entity type

async def store_task_ids(redis: "Redis", ids: list[str]) -> dict[int, str]:
    """Store task IDs."""
    return await store_ids(redis, "task", ids)


async def store_project_ids(redis: "Redis", ids: list[str]) -> dict[int, str]:
    """Store project IDs."""
    return await store_ids(redis, "project", ids)


async def store_goal_ids(redis: "Redis", ids: list[str]) -> dict[int, str]:
    """Store goal IDs."""
    return await store_ids(redis, "goal", ids)


async def resolve_task_id(redis: "Redis", id_or_short: str) -> str | None:
    """Resolve a task identifier."""
    return await resolve_id(redis, "task", id_or_short, prefix="task_")


async def resolve_project_id(redis: "Redis", id_or_short: str) -> str | None:
    """Resolve a project identifier."""
    return await resolve_id(redis, "project", id_or_short, prefix="proj_")


async def resolve_goal_id(redis: "Redis", id_or_short: str) -> str | None:
    """Resolve a goal identifier."""
    return await resolve_id(redis, "goal", id_or_short, prefix="goal_")
