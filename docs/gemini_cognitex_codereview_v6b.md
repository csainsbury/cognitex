Chatname: Codebase Audit & Async Fixes

I've conducted a deep review of the codebase. While the architecture (Graph + Vector + Agent) is sophisticated and well-structured, there is a **critical concurrency flaw** that will cause the application to freeze during operation.

Here are the major findings and required fixes.

### 1. Critical Performance Flaw: Blocking I/O in Async Functions

The entire application runs on `asyncio` (FastAPI + Discord.py), but the service layers use **synchronous (blocking) clients** for LLMs and Google APIs.

When the Agent calls `openai.chat.completions.create` or `gmail.users().messages().list`, it blocks the **entire Python event loop**.
*   **Result:** While the Agent thinks, the Web UI will hang, the Discord bot will stop responding to heartbeats (risking disconnection), and background triggers won't fire.

**Fix:** Switch to Async clients for LLMs and wrap Google API calls in threads.

#### A. Fix LLM Service (`src/cognitex/services/llm.py`)

<file path="src/cognitex/services/llm.py">
```python
<<<<
        elif self.provider == "anthropic":
            from anthropic import Anthropic
            api_key = settings.anthropic_api_key.get_secret_value()
            if not api_key:
                raise ValueError("ANTHROPIC_API_KEY not configured")
            self.client = Anthropic(api_key=api_key)
            self.primary_model = settings.anthropic_model_planner
            self.fast_model = settings.anthropic_model_executor
            logger.info("LLMService using Anthropic Claude", model=self.primary_model)

        elif self.provider == "openai":
            from openai import OpenAI
            api_key = settings.openai_api_key.get_secret_value()
            if not api_key:
                raise ValueError("OPENAI_API_KEY not configured")
            self.client = OpenAI(api_key=api_key)
            self.primary_model = settings.openai_model_planner
            self.fast_model = settings.openai_model_executor
            logger.info("LLMService using OpenAI", model=self.primary_model)

        else:  # together (default)
            api_key = settings.together_api_key.get_secret_value()
            if not api_key:
                raise ValueError("TOGETHER_API_KEY not configured")
            self.client = Together(api_key=api_key)
            self.primary_model = settings.together_model_planner
            self.fast_model = settings.together_model_executor
            self.provider = "together"
            logger.info("LLMService using Together.ai", model=self.primary_model)
====
        elif self.provider == "anthropic":
            from anthropic import AsyncAnthropic
            api_key = settings.anthropic_api_key.get_secret_value()
            if not api_key:
                raise ValueError("ANTHROPIC_API_KEY not configured")
            self.client = AsyncAnthropic(api_key=api_key)
            self.primary_model = settings.anthropic_model_planner
            self.fast_model = settings.anthropic_model_executor
            logger.info("LLMService using Anthropic Claude", model=self.primary_model)

        elif self.provider == "openai":
            from openai import AsyncOpenAI
            api_key = settings.openai_api_key.get_secret_value()
            if not api_key:
                raise ValueError("OPENAI_API_KEY not configured")
            self.client = AsyncOpenAI(api_key=api_key)
            self.primary_model = settings.openai_model_planner
            self.fast_model = settings.openai_model_executor
            logger.info("LLMService using OpenAI", model=self.primary_model)

        else:  # together (default)
            from together import AsyncTogether
            api_key = settings.together_api_key.get_secret_value()
            if not api_key:
                raise ValueError("TOGETHER_API_KEY not configured")
            self.client = AsyncTogether(api_key=api_key)
            self.primary_model = settings.together_model_planner
            self.fast_model = settings.together_model_executor
            self.provider = "together"
            logger.info("LLMService using Together.ai", model=self.primary_model)
>>>>
```

```python
<<<<
        elif self.provider == "anthropic":
            # Anthropic API
            response = self.client.messages.create(
                model=model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text

        else:
            # OpenAI/Together format
            kwargs: dict[str, Any] = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "temperature": temperature,
            }

            if response_format:
                kwargs["response_format"] = response_format

            response = self.client.chat.completions.create(**kwargs)
            return response.choices[0].message.content
====
        elif self.provider == "anthropic":
            # Anthropic API
            response = await self.client.messages.create(
                model=model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text

        else:
            # OpenAI/Together format
            kwargs: dict[str, Any] = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "temperature": temperature,
            }

            if response_format:
                kwargs["response_format"] = response_format

            response = await self.client.chat.completions.create(**kwargs)
            return response.choices[0].message.content
>>>>
```

*Note: For Google Gemini (`generativeai`), use `await model.generate_content_async(...)`.*

#### B. Fix Google APIs (Gmail/Calendar/Drive)

Since `google-api-python-client` is strictly synchronous, you must wrap calls in `run_in_executor` to prevent blocking.

Example fix for `src/cognitex/services/gmail.py` (apply pattern to Calendar/Drive services too):

```python
# Helper to run sync methods in thread pool
import asyncio
from functools import partial

async def run_sync(func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(func, *args, **kwargs))
```

And update the ingestion calls:

<file path="src/cognitex/services/ingestion.py">
```python
<<<<
    # Fetch full metadata for new messages
    messages = gmail.get_message_batch(list(new_message_ids), format="metadata")
====
    # Fetch full metadata for new messages (run in thread to avoid blocking)
    import asyncio
    messages = await asyncio.to_thread(
        gmail.get_message_batch, list(new_message_ids), format="metadata"
    )
>>>>
```

### 2. Missing Implementation: Tasks & Goals API

The API routes `src/cognitex/api/routes/tasks.py` and `goals.py` contain `NotImplementedError` or return empty lists. Since you have the Service layer (`src/cognitex/services/tasks.py`), you should wire them up.

<file path="src/cognitex/api/routes/tasks.py">
```python
<<<<
@router.get("/")
async def list_tasks(
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[TaskResponse]:
    """List tasks with optional filtering."""
    # TODO: Implement with database
    return []
====
@router.get("/")
async def list_tasks(
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[TaskResponse]:
    """List tasks with optional filtering."""
    from cognitex.services.tasks import get_task_service
    
    service = get_task_service()
    # Note: Service layer currently handles Neo4j session internally
    tasks = await service.list(status=status, limit=limit)
    
    # Convert dicts to Pydantic models
    return [
        TaskResponse(
            id=t["id"],
            title=t["title"],
            description=t.get("description"),
            status=t.get("status", "pending"),
            energy_cost=t.get("energy_cost"),
            due_date=t.get("due"),
            created_at=t.get("created_at"),
            updated_at=t.get("updated_at"),
            source_type=t.get("source_type"),
            source_id=t.get("source_id"),
            project_id=t.get("project_id"),
            completed_at=None # Map if available
        ) for t in tasks
    ]
>>>>
```

### 3. Logic Error: LLM JSON Parsing

In `src/cognitex/agent/autonomous.py`, the JSON parsing logic is brittle. LLMs often include text before/after the JSON block or use markdown code fences inconsistentely.

**Improved Extraction:**

<file path="src/cognitex/agent/autonomous.py">
```python
<<<<
            # Try to find JSON array in the response
            if "[" in response_text:
                start = response_text.find("[")
                end = response_text.rfind("]") + 1
                if end > start:
                    response_text = response_text[start:end]

            decisions = json.loads(response_text)
====
            # Robust JSON extraction
            import re
            
            # 1. Try to find a code block marked as json
            json_block = re.search(r"```json\s*(\[.*?\])\s*```", response_text, re.DOTALL)
            if json_block:
                response_text = json_block.group(1)
            else:
                # 2. Try to find any array-like structure
                array_match = re.search(r"(\[.*\])", response_text, re.DOTALL)
                if array_match:
                    response_text = array_match.group(1)

            decisions = json.loads(response_text)
>>>>
```

### 4. Omission: `graph_query` Tool Security

The `GraphQueryTool` in `tools.py` executes raw Cypher passed by the LLM. While `READONLY` risk level is set, standard Neo4j sessions allow write operations (`CREATE`, `DELETE`, `SET`) unless explicitly restricted.

**Fix:** Ensure the Neo4j session for this tool uses `READ` access mode.

<file path="src/cognitex/agent/tools.py">
```python
<<<<
    async def execute(self, query: str, params: dict | None = None) -> ToolResult:
        from cognitex.db.neo4j import get_neo4j_session

        try:
            async for session in get_neo4j_session():
                result = await session.run(query, params or {})
====
    async def execute(self, query: str, params: dict | None = None) -> ToolResult:
        from cognitex.db.neo4j import get_neo4j_session

        # Simple injection check (LLMs sometimes ignore instructions)
        upper_query = query.upper()
        if any(x in upper_query for x in ["CREATE", "DELETE", "SET", "MERGE", "DETACH", "REMOVE"]):
             return ToolResult(success=False, error="GraphQueryTool is read-only. Write operations not allowed.")

        try:
            # Force READ access mode
            async for session in get_neo4j_session(access_mode="READ"):
                result = await session.run(query, params or {})
>>>>
```

### 5. Docker Volume Omission

In `docker-compose.yml`, the `cognitex-core` service mounts `./src:/app/src`.
However, `cognitex-worker` also needs access to `google_credentials.json` and `client_secret.json` which are typically in `./data`.

Your `docker-compose.yml` mounts `./data:/app/data`, which is correct.
**However**, ensure the user `cognitex` (UID 1000 usually) inside the container has write permissions to `./data` on the host, otherwise token refreshes (`save_credentials`) will fail with `PermissionDenied`.

**Recommendation:** Add a check in your setup script or README to `chmod/chown` the data directory, or set the user in docker-compose.

### 6. Missing Dependencies

The `pyproject.toml` lists `google-generativeai`, `anthropic`, `openai`, `together`.
However, `asyncio.to_thread` requires Python 3.9+ (your project is 3.12, so that's fine).
For the Google fixes, you don't need new libs, just usage changes.
For the Async LLM clients, ensure the library versions support async (e.g. `openai>=1.0.0`, `anthropic>=0.3.0`). Your `pyproject.toml` versions look adequate.

### Summary of Actions Required

1.  **Switch to Async Clients:** Modify `llm.py` to use `AsyncOpenAI`, `AsyncAnthropic`, `AsyncTogether` (or `httpx`).
2.  **Wrap Blocking Calls:** Wrap all Google API calls (Gmail, Calendar, Drive) in `ingestion.py` and `services/*.py` with `asyncio.to_thread`.
3.  **Wire API Routes:** Implement the `list_tasks`, `create_task` etc. in `api/routes/tasks.py` using `TaskService`.
4.  **Harden JSON Parsing:** Apply regex extraction to `autonomous.py` and `planner.py`.
5.  **Secure Graph Tool:** Enforce read-only mode for `GraphQueryTool`.