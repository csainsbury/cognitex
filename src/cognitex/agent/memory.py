"""Agent memory system - working memory (Redis) and episodic memory (Postgres)."""

import json
import uuid
from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()

# Working memory TTL
WORKING_MEMORY_TTL = 60 * 60 * 24  # 24 hours


class WorkingMemory:
    """
    Short-term working memory backed by Redis.

    Stores:
    - Current context/conversation
    - Pending approvals
    - Recent observations
    - Scratch pad for intermediate reasoning
    """

    def __init__(self, redis_client):
        self.redis = redis_client
        self.prefix = "cognitex:memory:working"

    async def get_context(self) -> dict:
        """Get the current working context."""
        key = f"{self.prefix}:context"
        data = await self.redis.get(key)
        if data:
            return json.loads(data)
        return {
            "session_start": datetime.now().isoformat(),
            "interactions": [],
            "focus": None,
        }

    async def update_context(self, updates: dict) -> None:
        """Update the working context."""
        context = await self.get_context()
        context.update(updates)
        context["updated_at"] = datetime.now().isoformat()

        key = f"{self.prefix}:context"
        await self.redis.set(key, json.dumps(context), ex=WORKING_MEMORY_TTL)

    async def add_interaction(self, role: str, content: str, metadata: dict | None = None) -> None:
        """Add an interaction to the context."""
        context = await self.get_context()
        context["interactions"].append({
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
            "metadata": metadata or {},
        })

        # Keep last 50 interactions
        if len(context["interactions"]) > 50:
            context["interactions"] = context["interactions"][-50:]

        await self.update_context(context)

    async def stage_approval(
        self,
        approval_id: str,
        action_type: str,
        params: dict,
        reasoning: str,
        expires_hours: int = 24,
    ) -> None:
        """Stage an action for user approval."""
        key = f"{self.prefix}:approvals:{approval_id}"
        data = {
            "id": approval_id,
            "action_type": action_type,
            "params": params,
            "reasoning": reasoning,
            "created_at": datetime.now().isoformat(),
            "expires_at": (datetime.now() + timedelta(hours=expires_hours)).isoformat(),
            "status": "pending",
        }
        await self.redis.set(key, json.dumps(data), ex=expires_hours * 3600)

        # Also add to pending list
        list_key = f"{self.prefix}:approvals:pending"
        await self.redis.sadd(list_key, approval_id)

    async def get_pending_approvals(self) -> list[dict]:
        """Get all pending approval requests."""
        list_key = f"{self.prefix}:approvals:pending"
        approval_ids = await self.redis.smembers(list_key)

        approvals = []
        for aid in approval_ids:
            key = f"{self.prefix}:approvals:{aid}"
            data = await self.redis.get(key)
            if data:
                approval = json.loads(data)
                if approval.get("status") == "pending":
                    approvals.append(approval)
            else:
                # Expired, remove from set
                await self.redis.srem(list_key, aid)

        return approvals

    async def get_approval(self, approval_id: str) -> dict | None:
        """Get a specific approval request."""
        key = f"{self.prefix}:approvals:{approval_id}"
        data = await self.redis.get(key)
        return json.loads(data) if data else None

    async def resolve_approval(self, approval_id: str, approved: bool, feedback: str | None = None) -> dict | None:
        """Resolve a pending approval."""
        key = f"{self.prefix}:approvals:{approval_id}"
        data = await self.redis.get(key)

        if not data:
            return None

        approval = json.loads(data)
        approval["status"] = "approved" if approved else "rejected"
        approval["resolved_at"] = datetime.now().isoformat()
        approval["feedback"] = feedback

        # Update with short TTL (keep for audit)
        await self.redis.set(key, json.dumps(approval), ex=3600)

        # Remove from pending
        list_key = f"{self.prefix}:approvals:pending"
        await self.redis.srem(list_key, approval_id)

        return approval

    async def add_observation(self, observation: str, category: str = "general") -> None:
        """Add a recent observation."""
        key = f"{self.prefix}:observations"
        obs = {
            "content": observation,
            "category": category,
            "timestamp": datetime.now().isoformat(),
        }

        await self.redis.lpush(key, json.dumps(obs))
        await self.redis.ltrim(key, 0, 99)  # Keep last 100
        await self.redis.expire(key, WORKING_MEMORY_TTL)

    async def get_recent_observations(self, limit: int = 10, category: str | None = None) -> list[dict]:
        """Get recent observations."""
        key = f"{self.prefix}:observations"
        items = await self.redis.lrange(key, 0, limit * 2)  # Fetch extra for filtering

        observations = [json.loads(item) for item in items]

        if category:
            observations = [o for o in observations if o["category"] == category]

        return observations[:limit]

    async def set_scratch(self, key: str, value: Any) -> None:
        """Store something in the scratch pad."""
        full_key = f"{self.prefix}:scratch:{key}"
        await self.redis.set(full_key, json.dumps(value), ex=WORKING_MEMORY_TTL)

    async def get_scratch(self, key: str) -> Any:
        """Get something from the scratch pad."""
        full_key = f"{self.prefix}:scratch:{key}"
        data = await self.redis.get(key)
        return json.loads(data) if data else None

    async def clear(self) -> None:
        """Clear all working memory."""
        pattern = f"{self.prefix}:*"
        cursor = 0
        while True:
            cursor, keys = await self.redis.scan(cursor, match=pattern, count=100)
            if keys:
                await self.redis.delete(*keys)
            if cursor == 0:
                break


class EpisodicMemory:
    """
    Long-term episodic memory backed by PostgreSQL with pgvector.

    Stores:
    - Past interactions and their outcomes
    - Decisions made and reasoning
    - User feedback and corrections
    - Observations and patterns noticed
    """

    def __init__(self, get_session_func):
        self._get_session = get_session_func

    async def _ensure_tables(self, session: AsyncSession) -> None:
        """Ensure episodic memory tables exist."""
        await session.execute(text("""
            CREATE TABLE IF NOT EXISTS agent_memory (
                id TEXT PRIMARY KEY,
                memory_type TEXT NOT NULL,
                content TEXT NOT NULL,
                entities TEXT[] DEFAULT '{}',
                importance INTEGER DEFAULT 3,
                embedding vector(768),
                created_at TIMESTAMP DEFAULT NOW(),
                accessed_at TIMESTAMP DEFAULT NOW(),
                access_count INTEGER DEFAULT 0,
                metadata JSONB DEFAULT '{}'
            )
        """))

        await session.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_agent_memory_type ON agent_memory(memory_type)
        """))

        await session.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_agent_memory_embedding
            ON agent_memory USING ivfflat (embedding vector_cosine_ops)
        """))

        await session.commit()

    async def store(
        self,
        content: str,
        memory_type: str,
        entities: list[str] | None = None,
        importance: int = 3,
        metadata: dict | None = None,
    ) -> str:
        """
        Store a new memory.

        Args:
            content: The memory content
            memory_type: Type (interaction, decision, observation, feedback)
            entities: Related entity IDs
            importance: Importance 1-5
            metadata: Additional metadata

        Returns:
            Memory ID
        """
        from cognitex.services.llm import get_llm_service

        memory_id = f"mem_{uuid.uuid4().hex[:12]}"

        # Generate embedding for semantic search
        llm = get_llm_service()
        embedding = await llm.generate_embedding(content[:2000])

        async for session in self._get_session():
            await self._ensure_tables(session)

            await session.execute(text("""
                INSERT INTO agent_memory (id, memory_type, content, entities, importance, embedding, metadata)
                VALUES (:id, :memory_type, :content, :entities, :importance, :embedding, :metadata)
            """), {
                "id": memory_id,
                "memory_type": memory_type,
                "content": content,
                "entities": entities or [],
                "importance": importance,
                "embedding": embedding,
                "metadata": json.dumps(metadata or {}),
            })

            await session.commit()
            logger.debug("Stored memory", id=memory_id, type=memory_type)
            return memory_id

    async def search(
        self,
        query: str,
        memory_type: str | None = None,
        limit: int = 5,
        min_importance: int = 1,
    ) -> list[dict]:
        """
        Search memories using semantic similarity.

        Args:
            query: Search query
            memory_type: Filter by type
            limit: Max results
            min_importance: Minimum importance level

        Returns:
            List of matching memories
        """
        from cognitex.services.llm import get_llm_service

        llm = get_llm_service()
        query_embedding = await llm.generate_embedding(query)

        type_filter = "AND memory_type = :memory_type" if memory_type else ""

        async for session in self._get_session():
            result = await session.execute(text(f"""
                SELECT
                    id, memory_type, content, entities, importance,
                    created_at, metadata,
                    1 - (embedding <=> :query_embedding::vector) as similarity
                FROM agent_memory
                WHERE importance >= :min_importance
                {type_filter}
                ORDER BY embedding <=> :query_embedding::vector
                LIMIT :limit
            """), {
                "query_embedding": query_embedding,
                "memory_type": memory_type,
                "min_importance": min_importance,
                "limit": limit,
            })

            memories = []
            for row in result.fetchall():
                # Update access stats
                await session.execute(text("""
                    UPDATE agent_memory
                    SET accessed_at = NOW(), access_count = access_count + 1
                    WHERE id = :id
                """), {"id": row.id})

                memories.append({
                    "id": row.id,
                    "type": row.memory_type,
                    "content": row.content,
                    "entities": row.entities,
                    "importance": row.importance,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                    "similarity": float(row.similarity),
                    "metadata": row.metadata,
                })

            await session.commit()
            return memories

    async def get_recent(
        self,
        memory_type: str | None = None,
        limit: int = 10,
        hours: int = 24,
    ) -> list[dict]:
        """Get recent memories by time."""
        type_filter = "AND memory_type = :memory_type" if memory_type else ""
        since = datetime.now() - timedelta(hours=hours)

        async for session in self._get_session():
            result = await session.execute(text(f"""
                SELECT id, memory_type, content, entities, importance, created_at, metadata
                FROM agent_memory
                WHERE created_at >= :since
                {type_filter}
                ORDER BY created_at DESC
                LIMIT :limit
            """), {
                "since": since,
                "memory_type": memory_type,
                "limit": limit,
            })

            return [{
                "id": row.id,
                "type": row.memory_type,
                "content": row.content,
                "entities": row.entities,
                "importance": row.importance,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "metadata": row.metadata,
            } for row in result.fetchall()]

    async def get_by_entity(self, entity_id: str, limit: int = 10) -> list[dict]:
        """Get memories related to a specific entity."""
        async for session in self._get_session():
            result = await session.execute(text("""
                SELECT id, memory_type, content, entities, importance, created_at, metadata
                FROM agent_memory
                WHERE :entity_id = ANY(entities)
                ORDER BY importance DESC, created_at DESC
                LIMIT :limit
            """), {
                "entity_id": entity_id,
                "limit": limit,
            })

            return [{
                "id": row.id,
                "type": row.memory_type,
                "content": row.content,
                "entities": row.entities,
                "importance": row.importance,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "metadata": row.metadata,
            } for row in result.fetchall()]

    async def summarize_period(self, hours: int = 24) -> str:
        """Generate a summary of memories from a time period."""
        memories = await self.get_recent(hours=hours, limit=50)

        if not memories:
            return "No significant memories from this period."

        # Group by type
        by_type = {}
        for mem in memories:
            t = mem["type"]
            if t not in by_type:
                by_type[t] = []
            by_type[t].append(mem["content"])

        summary_parts = []
        for mem_type, contents in by_type.items():
            summary_parts.append(f"**{mem_type.title()}s ({len(contents)}):**")
            for content in contents[:5]:  # Top 5 per type
                summary_parts.append(f"  - {content[:100]}...")

        return "\n".join(summary_parts)


class Memory:
    """Combined memory system with working and episodic memory."""

    def __init__(self, redis_client, get_session_func):
        self.working = WorkingMemory(redis_client)
        self.episodic = EpisodicMemory(get_session_func)

    async def build_context(self, trigger: str, include_recent: bool = True) -> dict:
        """
        Build a complete context for the agent.

        Combines working memory, recent episodic memories, and trigger info.
        """
        context = {
            "trigger": trigger,
            "timestamp": datetime.now().isoformat(),
            "working": await self.working.get_context(),
            "pending_approvals": await self.working.get_pending_approvals(),
        }

        if include_recent:
            context["recent_observations"] = await self.working.get_recent_observations(limit=5)
            context["recent_memories"] = await self.episodic.get_recent(hours=24, limit=10)

        return context


# Singleton
_memory: Memory | None = None


async def init_memory() -> Memory:
    """Initialize the memory system."""
    global _memory

    from cognitex.db.redis import get_redis
    from cognitex.db.postgres import get_session

    redis = await get_redis()
    _memory = Memory(redis, get_session)

    logger.info("Memory system initialized")
    return _memory


def get_memory() -> Memory:
    """Get the memory system singleton."""
    if _memory is None:
        raise RuntimeError("Memory not initialized. Call init_memory() first.")
    return _memory
