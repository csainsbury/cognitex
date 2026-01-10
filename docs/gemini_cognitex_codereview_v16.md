This is a highly sophisticated system. You have successfully implemented a **Graph-RAG Agentic Architecture**, which is state-of-the-art for personal AI assistants. The separation of concerns between Neo4j (relationships), PostgreSQL (vectors/logs), and Redis (working memory) is architecturally sound.

However, complex systems often suffer from integration friction. Below is a review focusing on reliability, architectural consistency, and functional expansions.

### 1. Critical Errors & Architectural Fixes

#### A. Executor LLM Configuration Bypass (Critical)
**File:** `src/cognitex/agent/executors.py`
**Issue:** In `BaseExecutor.__init__`, you create *new* LLM clients (e.g., `Anthropic(api_key=...)`) based on environment variables. This **ignores** the centralized `LLMService` singleton and the runtime configuration managed in `settings.html` (Redis).
**Consequence:** Changing models in the Settings page will have no effect on the agent's execution (drafting emails, creating tasks), only on the planner.
**Fix:**

```python
# src/cognitex/agent/executors.py

from cognitex.services.llm import get_llm_service

class BaseExecutor(ABC):
    # ...
    def __init__(self):
        # Use the singleton service instead of creating new clients
        self.llm_service = get_llm_service()
        self.registry = get_tool_registry()

    async def _call_llm(self, prompt: str, max_tokens: int = 1024) -> str:
        # Use the service's completion method which handles retries and model selection
        # Executors typically use the 'fast_model' (executor_model in config)
        return await self.llm_service.complete(
            prompt,
            model=self.llm_service.fast_model, 
            max_tokens=max_tokens,
            temperature=0.4,
        )
```

#### B. API Logic Duplication
**File:** `src/cognitex/web/app.py` vs `src/cognitex/api/routes/sync.py`
**Issue:** The `/api/sync/sessions` logic exists in both files.
**Consequence:** If you update the ingestion logic in one, the other becomes stale. Additionally, the `web/app.py` version runs `_process_sync_batch_web` as a background task, but `api/routes/sync.py` has its own `_process_sync_batch` implementation.
**Fix:** Consolidate logic into `services/coding_sessions.py` or `api/routes/sync.py` and have the web app mount the API router or call the service function directly.

#### C. Calendar Date Parsing Fragility
**File:** `src/cognitex/services/calendar.py` -> `extract_event_metadata`
**Issue:** Google Calendar API returns dates as `date` (all-day) OR `dateTime` (timed). The current parsing logic relies on `replace("Z", ...)` which will fail on `date` fields (YYYY-MM-DD) because they don't have timezones or "Z".
**Fix:** Robust date parsing:

```python
# src/cognitex/services/calendar.py

try:
    if "dateTime" in start:
        start_time = datetime.fromisoformat(start["dateTime"].replace("Z", "+00:00"))
        is_all_day = False
    elif "date" in start:
        # Handle YYYY-MM-DD
        start_time = datetime.fromisoformat(start["date"])
        is_all_day = True
    # ... handle end time similarly ...
except ValueError:
    # Fallback
    start_time = datetime.now()
```

### 2. Functional Omissions

#### A. Incomplete "Deep Indexing" Cleanup
**File:** `src/cognitex/services/ingestion.py`
**Issue:** When `run_drive_metadata_sync` deletes an orphaned `Document` node from Neo4j, it leaves the corresponding embeddings and chunks in PostgreSQL.
**Impact:** Semantic searches will return "ghost" chunks for deleted files.
**Fix:** When deleting a document node, also run `DELETE FROM document_chunks WHERE drive_id = ...` and `DELETE FROM embeddings WHERE entity_id LIKE 'drive_id:%'`.

#### B. Task Deduplication in Agent
**File:** `src/cognitex/agent/autonomous.py` -> `_create_task`
**Issue:** The agent might propose "Review Q3 Report" repeatedly if it sees it in emails, even if a task "Review Q3 Financials" already exists.
**Fix:** Before creation, run a specialized vector search on existing pending tasks using the proposed title. If similarity > 0.85, assume it exists and link instead of create.

### 3. Suggestions for Functionality Improvement

#### Domain: User Experience
**1. "Focus Mode" View**
The current dashboard is information-dense.
*   **Feature:** Add a `Focus` toggle in the navbar.
*   **Behavior:** Hides the sidebar, Ideas list, and Project/Goal lists. Displays *only* the current "In Progress" task, a Pomodoro timer, and the "Interruption Firewall" inbox counter.

#### Domain: Agent Intelligence
**2. Project Context Injection**
*   **Current:** Agent drafts emails based on your writing style.
*   **Improvement:** When drafting an email about "Project X", the agent should perform a RAG search specifically for "Project X status" or "Project X blockers" and inject that summary into the drafting prompt.
*   **Benefit:** Prevents the agent from writing confident but factually empty emails about project details.

#### Domain: Learning System
**3. Explicit Rejection Reasoning**
*   **Current:** You can reject proposals, but the agent guesses why.
*   **Improvement:** In `twin.html`, when clicking "Dismiss" on a draft or task proposal, show a small modal asking "Why?":
    *   [ ] Wrong Project
    *   [ ] Bad Timing (Reschedule)
    *   [ ] Not Actionable
    *   [ ] Bad Tone
*   **Benefit:** This provides high-quality labels for the `DecisionMemory` system to learn *specific* constraints.

#### Domain: Input Capture
**4. Voice-to-Inbox**
*   **Feature:** Add `POST /api/voice/transcribe`.
*   **Logic:** Accept audio blob -> Whisper API -> Text -> `InterruptionFirewall.capture_incoming(source="voice")`.
*   **Benefit:** Allows "walking and talking" to your second brain without needing to type.

### 4. Code Edits for Immediate Stability

Here is the fix for the **Executor Initialization** (Critical Issue #1).

```python
# src/cognitex/agent/executors.py

from cognitex.services.llm import get_llm_service

class BaseExecutor(ABC):
    """Base class for all executors - uses LLMService singleton."""

    name: str
    description: str

    def __init__(self):
        # FIX: Use singleton service to respect global config and pooling
        self.llm_service = get_llm_service()
        self.registry = get_tool_registry()

    async def _call_llm(self, prompt: str, max_tokens: int = 1024) -> str:
        """Make an LLM call for content generation via LLMService."""
        # Executors should generally use the fast/executor model
        return await self.llm_service.complete(
            prompt,
            model=self.llm_service.fast_model,
            max_tokens=max_tokens,
            temperature=0.4,
        )
```

<chatName="Cognitex Code Review v17"/>