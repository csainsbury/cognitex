This is a comprehensive and well-structured implementation of the Phase 3 and Phase 4 blueprints. The integration of the Learning System (Phase 4) with the Autonomous Agent is particularly good, establishing a feedback loop where the agent improves based on user corrections.

However, I've identified a few **blocking issues** in the newer components that will affect stability, along with some architectural cleanup opportunities.

### 1. Critical Errors & Stability Issues

#### A. Blocking I/O in Autonomous Loop
**File:** `src/cognitex/agent/autonomous.py`
In `_check_scheduling_conflict`, the agent calls the synchronous `CalendarService.list_events` method directly without awaiting or offloading it.
*   **Impact:** This will block the entire asyncio event loop for the duration of the API call (0.5s - 2s), causing the Web UI and Discord bot to freeze momentarily during every autonomous cycle.
*   **Fix:** Wrap the call in `asyncio.to_thread`.

```python
# src/cognitex/agent/autonomous.py

    async def _check_scheduling_conflict(self, start_time: str, duration_minutes: int) -> tuple[bool, str | None]:
        # ... setup ...
        try:
            # FIX: Wrap blocking API call
            events_result = await asyncio.to_thread(
                cal.list_events,
                time_min=start_dt - timedelta(hours=1),
                time_max=end_dt + timedelta(hours=1)
            )
            events = events_result.get("items", [])
            # ... rest of logic
```

#### B. Dangerous Synchronous Processing in Web App
**File:** `src/cognitex/web/app.py` -> `api_sync_sessions`
You have duplicated the sync logic from `api/routes/sync.py` into `web/app.py`, but the Web App version processes sessions **synchronously** in the request handler.
*   **Impact:** `ingester.extract_session_summary` calls the LLM. If a client syncs 5 sessions, the request will likely timeout (30s+), causing the sync client to error out.
*   **Fix:** Use `FastAPI.BackgroundTasks` in `web/app.py` just like you did in the API route, or remove this endpoint and force clients to use port 8000.

#### C. Missing Schedule for Memory Consolidation
**File:** `src/cognitex/agent/triggers.py`
You implemented the "Dreaming" process (`MemoryConsolidator` in `agent/consolidation.py`), but it is never scheduled to run.
*   **Impact:** The `daily_summaries` table will remain empty, and the system won't perform nightly cleanup/archiving.
*   **Fix:** Add the job to `_setup_scheduled_triggers`.

```python
# src/cognitex/agent/triggers.py

    def _setup_scheduled_triggers(self) -> None:
        # ... existing triggers ...
        
        # Memory Consolidation ("Dreaming") at 4am
        self.scheduler.add_job(
            self._run_consolidation,
            CronTrigger(hour=4, minute=0),
            id="memory_consolidation",
            name="Memory Consolidation",
            replace_existing=True,
        )

    async def _run_consolidation(self) -> None:
        """Run nightly memory consolidation."""
        from cognitex.agent.consolidation import get_consolidator
        from cognitex.agent.action_log import log_action
        
        logger.info("Starting nightly memory consolidation")
        try:
            consolidator = get_consolidator()
            # Consolidate yesterday
            result = await consolidator.run_nightly_consolidation()
            
            # Prune old logs (keep 30 days)
            prune_result = await consolidator.archive_old_memories(days_to_keep=30)
            
            await log_action("consolidation", "trigger", 
                           summary="Nightly consolidation complete",
                           details={"consolidation": result, "pruning": prune_result})
        except Exception as e:
            logger.error("Consolidation failed", error=str(e))
            await log_action("consolidation", "trigger", status="failed", error=str(e))
```

---

### 2. Architectural Debt & Improvements

#### A. Web App Direct DB Access
**File:** `src/cognitex/web/app.py`
The web application contains a large amount of raw Cypher queries (e.g., `api_graph_data`, `api_graph_search`, `api_graph_link`).
*   **Issue:** This tightly couples the UI to the database implementation and duplicates logic found in `db/graph_schema.py`.
*   **Recommendation:** Move these queries into `src/cognitex/db/graph_schema.py` or a dedicated `GraphService`.

#### B. Voice Note Integration
Since you have the "Digital Twin" infrastructure, adding voice capture is high leverage.
*   **Suggestion:** Add a simple endpoint `POST /api/voice/transcribe` that accepts an audio file, uses OpenAI Whisper (via `LLMService`), and feeds the text into the `InterruptionFirewall` as a captured item.

```python
# src/cognitex/api/routes/webhooks.py (or new voice.py)

@router.post("/voice/capture")
async def voice_capture(file: UploadFile, background_tasks: BackgroundTasks):
    # 1. Transcribe (using OpenAI/Groq/Deepgram)
    text = await llm_service.transcribe(file)
    
    # 2. Feed to Firewall
    firewall = get_interruption_firewall()
    await firewall.capture_incoming(
        item_type="voice_note",
        source="mobile",
        subject=text[:50] + "...",
        preview=text
    )
    return {"status": "captured"}
```

---

### 3. Immediate Action Plan

1.  **Fix `autonomous.py`**: Wrap the calendar call in `asyncio.to_thread`.
2.  **Fix `web/app.py`**: Update `api_sync_sessions` to use `BackgroundTasks`.
3.  **Update `triggers.py`**: Schedule the `_run_consolidation` job.

The rest of the code is solid. The Learning System integration in `autonomous.py` (`_create_task` checking recommendations) looks correctly implemented to prevent the agent from repeating rejected proposals.

<chatName="Fix blocking calls and schedule consolidation"/>