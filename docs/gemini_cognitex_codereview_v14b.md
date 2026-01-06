This is an impressive, production-grade system. You have successfully implemented a **Graph-RAG Agentic Architecture**, combining Neo4j (relationships), PostgreSQL/pgvector (semantics), and Redis (context).

I have reviewed the codebase (v14b) and identified **2 Critical Blocking Issues**, **3 Logic/Architectural Omissions**, and **4 High-Value Feature Suggestions**.

### 1. Critical Errors (Stability & Performance)

#### A. Blocking I/O in Autonomous Loop
**File:** `src/cognitex/agent/autonomous.py`
In `_check_scheduling_conflict`, the agent calls the synchronous `CalendarService.list_events` method directly.
*   **Impact:** Because this runs inside the main asyncio loop, every time the agent considers scheduling a block, it **freezes the entire application** (Web UI and Discord bot) for 0.5–2 seconds while waiting for Google's API.
*   **Fix:** Wrap the call in `asyncio.to_thread`.

#### B. API Logic Duplication
**File:** `src/cognitex/web/app.py` vs `src/cognitex/api/routes/sync.py`
You have defined the `/api/sync/sessions` endpoint logic **twice**.
1.  `web/app.py`: Lines ~3590 (`api_sync_sessions`)
2.  `api/routes/sync.py`: Entire file
*   **Impact:** If you update the sync logic (e.g., changing how LLM summaries work), one endpoint will remain stale. Additionally, the version in `web/app.py` processes sessions **synchronously** in the request, which will cause HTTP 504 Timeouts for large batches.
*   **Fix:** Delete the endpoint from `web/app.py` and rely on the mounted `api` router, or update `web/app.py` to use `BackgroundTasks` like the API route does.

---

### 2. Omissions & Data Integrity

#### A. Vector Store "Ghosting"
**File:** `src/cognitex/services/ingestion.py` -> `run_drive_metadata_sync`
When files are deleted from Drive, you correctly remove the `Document` node from Neo4j.
*   **Omission:** You do **not** delete the corresponding rows from PostgreSQL (`document_content`, `document_chunks`, `embeddings`).
*   **Result:** Vector searches will return results for deleted files, leading to "File not found" errors when the agent tries to read them.

#### B. Missing Project Foreign Key
**File:** `docker/postgres/init.sql`
The `tasks` table has a `project_id` column, but no `projects` table exists in PostgreSQL (projects live in Neo4j).
*   **Risk:** While this is a valid polyglot pattern, if you try to perform SQL-based analytics (like `get_duration_calibration` in Phase 4), you cannot join tasks to project names.
*   **Fix:** Ensure analytics queries perform IDs-only aggregation in SQL and fetch titles from Neo4j in the application layer (your current `services/tasks.py` handles this correctly, just be aware of the schema constraint).

---

### 3. Suggested Improvements

#### Domain: User Interface
**1. "Focus Mode" Toggle**
The dashboard is information-dense. Add a simple toggle in the navbar that hides the sidebar, ideas list, and graph visualization, leaving only the **Current Task** and a **Pomodoro Timer**. This reduces visual cognitive load during execution.

#### Domain: Input
**2. Voice Note Transcription**
You have the "Digital Twin" logic but input is text-only.
*   **Implementation:** Add `POST /api/voice/transcribe`. Use OpenAI Whisper (via `LLMService`) to transcribe audio blobs, then feed the text directly into `InterruptionFirewall` as an `IncomingItem`. This enables "walking and talking" to your second brain.

#### Domain: Learning
**3. Rejection Reason Capture**
In `agent/action_log.py`, you have `reject_proposal(..., reason=...)`.
*   **Improvement:** In the Web UI (`twin.html`), when clicking "Dismiss" or "Discard", pop up a small modal asking "Why?" (e.g., "Wrong project", "Not now", "Bad tone").
*   **Why:** Without this explicit signal, the Learning System (Phase 4) struggles to differentiate between "Bad timing" (reschedule) and "Bad idea" (never suggest again).

#### Domain: Resilience
**4. Hard Context Limit**
In `agent/autonomous.py`, you inject email snippets and writing samples into the prompt.
*   **Risk:** A massive email thread could overflow the context window or cost $$$.
*   **Improvement:** Enforce a hard character limit on dynamic inputs (e.g., `snippet[:500]`) before formatting the prompt.

---

### 4. Code Patches

#### Fix 1: Non-Blocking Calendar Check (`src/cognitex/agent/autonomous.py`)

```python
    async def _check_scheduling_conflict(
        self,
        start_time: str | None,
        duration_minutes: int,
    ) -> tuple[bool, str | None]:
        # ... (keep existing setup) ...

            # Check existing events (wrap blocking API call in thread)
            cal = CalendarService()
            
            # FIX: Use asyncio.to_thread
            events_result = await asyncio.to_thread(
                cal.list_events,
                time_min=start_dt - timedelta(hours=1),
                time_max=end_dt + timedelta(hours=1),
            )

            for event in events_result.get("items", []):
                # ... (rest of logic)
```

#### Fix 2: Vector Store Cleanup (`src/cognitex/services/ingestion.py`)

Update `run_drive_metadata_sync` to clean PostgreSQL:

```python
            if to_delete:
                ids_list = list(to_delete)

                # Delete from Neo4j
                await session.run("""
                    MATCH (d:Document)
                    WHERE d.drive_id IN $ids
                    DETACH DELETE d
                """, {"ids": ids_list})
                
                # FIX: Delete from Postgres
                from cognitex.db.postgres import get_session
                from sqlalchemy import text
                
                async for pg_session in get_session():
                    # Delete content and chunks
                    await pg_session.execute(
                        text("DELETE FROM document_content WHERE drive_id = ANY(:ids)"),
                        {"ids": ids_list}
                    )
                    await pg_session.execute(
                        text("DELETE FROM document_chunks WHERE drive_id = ANY(:ids)"),
                        {"ids": ids_list}
                    )
                    # Delete embeddings (requires identifying them by entity_id)
                    # This is complex because entity_id for chunks is "drive_id:index"
                    # Simplified: Delete embeddings where entity_type='document' AND entity_id in list
                    await pg_session.execute(
                        text("DELETE FROM embeddings WHERE entity_type = 'document' AND entity_id = ANY(:ids)"),
                        {"ids": ids_list}
                    )
                    # For chunks, we'd need a LIKE query loop or cleaner ID structure
                    for drive_id in ids_list:
                         await pg_session.execute(
                            text("DELETE FROM embeddings WHERE entity_type = 'chunk' AND entity_id LIKE :pattern"),
                            {"pattern": f"{drive_id}:%"}
                         )
                    await pg_session.commit()
                    break

                logger.info("Deleted orphaned documents and vectors", count=len(ids_list))
```

<chatName="Review Cognitex v14b - Async fixes and Vector cleanup"/>