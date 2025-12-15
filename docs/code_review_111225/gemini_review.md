This is a very strong implementation of the blueprint. You have successfully implemented the core hierarchical agent architecture (Planner/Executor), the Graph/SQL hybrid storage, and the specific tool logic.

However, there are a few **critical disconnections** preventing the system from running autonomously as described in the blueprint, particularly regarding how the background worker, the trigger system, and the Discord bot communicate.

Here are the specific errors, omissions, and improvements required to make this fully functional.

### 1. Critical: Connect Worker to Agent Triggers
**Issue:** The `docker-compose.yml` runs `cognitex.worker`, which uses `arq`. However, the Agent's autonomous loop (morning briefings, hourly checks) lives in `agent/triggers.py` (using `APScheduler`), and `worker.py` currently **does not start** this system. The agent is currently effectively "asleep" unless triggered manually via CLI.

**Fix:** Initialize and start the `TriggerSystem` inside the worker's startup lifecycle.

```python
File: /Users/csainsbury/Downloads/cognitex-main/src/cognitex/worker.py
<<<<
async def startup(ctx: dict) -> None:
    """Initialize connections on worker startup."""
    logger.info("Worker starting up")
    await init_postgres()
    await init_neo4j()
    ctx["initialized"] = True


async def shutdown(ctx: dict) -> None:
    """Cleanup connections on worker shutdown."""
    logger.info("Worker shutting down")
    await close_neo4j()
    await close_postgres()
====
async def startup(ctx: dict) -> None:
    """Initialize connections on worker startup."""
    logger.info("Worker starting up")
    await init_postgres()
    await init_neo4j()
    
    # Start the Agent Trigger System (Scheduler & Event Listeners)
    from cognitex.agent.triggers import start_triggers
    ctx["trigger_system"] = await start_triggers()
    
    ctx["initialized"] = True


async def shutdown(ctx: dict) -> None:
    """Cleanup connections on worker shutdown."""
    logger.info("Worker shutting down")
    
    # Stop triggers
    if "trigger_system" in ctx:
        from cognitex.agent.triggers import stop_triggers
        await stop_triggers()
        
    await close_neo4j()
    await close_postgres()
>>>>
```

### 2. Critical: Connect Discord Bot to Redis
**Issue:** The Agent sends notifications (via `SendNotificationTool`) by publishing to the Redis channel `cognitex:notifications`. The Discord bot (`discord_bot/__main__.py`) connects to Discord but **does not listen** to Redis. It will never receive the messages the Agent sends.

**Fix:** Add a background task to the Discord bot to consume the Redis pub/sub channel.

```python
File: /Users/csainsbury/Downloads/cognitex-main/src/cognitex/discord_bot/__main__.py
<<<<
    async def on_ready(self) -> None:
        """Called when the bot is connected and ready."""
        logger.info("Bot connected", user=str(self.user), guilds=len(self.guilds))

    async def on_message(self, message: discord.Message) -> None:
====
    async def on_ready(self) -> None:
        """Called when the bot is connected and ready."""
        logger.info("Bot connected", user=str(self.user), guilds=len(self.guilds))
        # Start Redis listener task
        self.bg_task = self.loop.create_task(self.listen_for_notifications())

    async def listen_for_notifications(self) -> None:
        """Listen to Redis for notifications from the Agent."""
        from cognitex.db.redis import init_redis, get_redis
        import json

        await init_redis()
        redis = get_redis()
        pubsub = redis.pubsub()
        await pubsub.subscribe("cognitex:notifications")

        logger.info("Listening for internal notifications...")

        try:
            async for message in pubsub.listen():
                if message["type"] == "message":
                    try:
                        data = json.loads(message["data"])
                        content = data.get("message")
                        # You could use data.get("urgency") to change formatting/color
                        if content:
                            await self.send_notification(content)
                    except Exception as e:
                        logger.error("Failed to process notification", error=str(e))
        except Exception as e:
            logger.error("Redis listener died", error=str(e))

    async def on_message(self, message: discord.Message) -> None:
>>>>
```

### 3. Omission: "Read Document" Tool
**Issue:** You added Drive ingestion and a `SearchDocumentsTool` (semantic search). The Agent can find *reference* to a document, but currently has no tool to actually **read the full content** of a specific file if it needs to answer a question about it.

**Fix:** Add a `ReadDocumentTool`.

```python
File: /Users/csainsbury/Downloads/cognitex-main/src/cognitex/agent/tools.py
<<<<
class ToolRegistry:
    """Registry of all available tools."""

    def __init__(self):
        self._tools: dict[str, BaseTool] = {}
        self._register_defaults()

    def _register_defaults(self):
        """Register all default tools."""
        default_tools = [
            # Read-only
            GraphQueryTool(),
            SearchDocumentsTool(),
            GetCalendarTool(),
====
class ReadDocumentTool(BaseTool):
    """Read the full content of a specific document."""

    name = "read_document"
    description = "Read the full text content of a document by its Drive ID."
    risk = ToolRisk.READONLY
    parameters = {
        "drive_id": {"type": "string", "description": "Google Drive ID of the file"},
    }

    async def execute(self, drive_id: str) -> ToolResult:
        from cognitex.db.postgres import get_session
        from sqlalchemy import text

        try:
            async for session in get_session():
                result = await session.execute(
                    text("SELECT content FROM document_content WHERE drive_id = :drive_id"),
                    {"drive_id": drive_id}
                )
                row = result.fetchone()
                if row:
                    # Truncate if massive to prevent context overflow, or let Planner handle it
                    content = row.content
                    return ToolResult(success=True, data={"content": content[:10000], "truncated": len(content) > 10000})
                return ToolResult(success=False, error=f"Document content not found for ID: {drive_id}")
        except Exception as e:
            logger.warning("Read document failed", drive_id=drive_id, error=str(e))
            return ToolResult(success=False, error=str(e))


class ToolRegistry:
    """Registry of all available tools."""

    def __init__(self):
        self._tools: dict[str, BaseTool] = {}
        self._register_defaults()

    def _register_defaults(self):
        """Register all default tools."""
        default_tools = [
            # Read-only
            GraphQueryTool(),
            SearchDocumentsTool(),
            ReadDocumentTool(),
            GetCalendarTool(),
>>>>
```

### 4. Database Schema Consolidation
**Issue:** `init.sql` defines the core schema, but `agent/memory.py` defines the `agent_memory` table dynamically in Python code. It is cleaner and safer to have the schema defined in one place (`init.sql`) to ensure migrations work correctly and the table exists before the app starts.

**Fix:** Add `agent_memory` to `init.sql` and simplify `memory.py`.

```sql
File: /Users/csainsbury/Downloads/cognitex-main/docker/postgres/init.sql
<<<<
CREATE TRIGGER update_sync_state_updated_at BEFORE UPDATE ON sync_state
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
====
CREATE TRIGGER update_sync_state_updated_at BEFORE UPDATE ON sync_state
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- Episodic Memory for the Agent
CREATE TABLE IF NOT EXISTS agent_memory (
    id TEXT PRIMARY KEY,
    memory_type TEXT NOT NULL,
    content TEXT NOT NULL,
    entities TEXT[] DEFAULT '{}',
    importance INTEGER DEFAULT 3,
    embedding vector(768),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    accessed_at TIMESTAMPTZ DEFAULT NOW(),
    access_count INTEGER DEFAULT 0,
    metadata JSONB DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_agent_memory_type ON agent_memory(memory_type);
CREATE INDEX IF NOT EXISTS idx_agent_memory_embedding
ON agent_memory USING ivfflat (embedding vector_cosine_ops);
>>>>
```

### 5. Config Improvement
**Issue:** The `.env.example` file is missing specific configuration keys used in `config.py` (specifically model names), and `init.sql` creates a `tasks` table that `src/cognitex/api/routes/tasks.py` doesn't fully utilize yet (it returns empty lists).

**Suggestion:** Update `.env.example` to reflect the agent's model needs.

```
File: /Users/csainsbury/Downloads/cognitex-main/.env.example
<<<<
# Together.ai API
TOGETHER_API_KEY=your_together_api_key

# Discord bot
====
# Together.ai API
TOGETHER_API_KEY=your_together_api_key
TOGETHER_MODEL_PLANNER=Qwen/Qwen3-30B-A3B
TOGETHER_MODEL_EXECUTOR=deepseek-ai/DeepSeek-V3
TOGETHER_MODEL_EMBEDDING=togethercomputer/m2-bert-80M-8k-retrieval

# Discord bot
>>>>
```

### Summary of Improvements
1.  **Agent Awakening:** The background worker now actually starts the agent's logic loop.
2.  **Voice:** The Discord bot can now speak what the Agent thinks.
3.  **Vision:** The Agent can now read the documents it finds in Google Drive.
4.  **Stability:** Database schema is centralized in SQL, reducing runtime race conditions.

<chatName="Review and fix Agent/Worker integration"/>