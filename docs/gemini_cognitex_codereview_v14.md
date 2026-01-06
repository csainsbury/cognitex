I've reviewed the Cognitex codebase (v14). It is a highly sophisticated implementation of a Graph-RAG Agentic architecture. You have successfully integrated the "Digital Twin" concepts (Phase 3) and the "Learning System" (Phase 4).

However, I have identified **2 logic conflicts**, **3 architectural risks**, and **5 concrete opportunities** for functional improvement.

### 1. Logic Errors & Conflicts

#### A. Route Conflict & Logic Duplication (Sync API)
**Files:** `src/cognitex/web/app.py` vs `src/cognitex/api/routes/sync.py`
You have defined the `/api/sync/sessions` endpoint logic **twice**:
1.  Inside `web/app.py` (lines ~1740-1845)
2.  Inside `api/routes/sync.py`

**The Issue:** If you run `cognitex web`, it uses the logic in `app.py`. If you run `cognitex api`, it uses `routes/sync.py`. If you update one (e.g., changing how LLM summarization works), the other becomes stale.
**Fix:** Remove the endpoint definition from `web/app.py` and mount the API router into the Web App instance, or have `web/app.py` delegate strictly to a service class without re-implementing the HTTP handler logic.

#### B. Postgres vs Neo4j "Split Brain" on Projects
**Files:** `docker/postgres/init.sql` vs `src/cognitex/db/graph_schema.py`
*   **Neo4j:** Stores `Project` nodes as the source of truth.
*   **Postgres:** The `tasks` table has a `project_id` column (Line 17), but there is no `projects` table in Postgres.
**The Risk:** The Learning System (Phase 4) relies heavily on SQL queries for analytics (e.g., `get_duration_calibration`). If you try to join `tasks` to project details (like `project_title`) in SQL, it will fail because project metadata only lives in Neo4j.
**Fix:** Create a read-only mirror of the `projects` table in Postgres, or ensure all analytics queries only group by `project_id` and fetch titles from Neo4j later.

### 2. Architectural Omissions

#### A. Missing "Cleanup" for Vector Store
**File:** `src/cognitex/services/ingestion.py`
When `run_drive_metadata_sync` runs with `cleanup_deleted=True`, it deletes `Document` nodes from Neo4j.
**The Omission:** It does **not** appear to delete the corresponding rows from the Postgres `embeddings` or `document_chunks` tables. Over time, your vector search results will return "ghost" documents that no longer exist in Drive or Graph.
**Fix:** Add a cleanup step in `ingestion.py` to delete from Postgres where `drive_id` no longer exists in the active set.

#### B. Hardcoded Model Config in Executors
**File:** `src/cognitex/agent/executors.py`
The `EmailExecutor` and others use `get_llm_service().fast_model`.
**The Issue:** If the user changes the model configuration in `settings.html` (stored in Redis), `get_llm_service()` might still be using the environment variable defaults depending on when it was initialized.
**Fix:** Ensure `LLMService` checks the dynamic config (Redis) on every request or implements a refresh mechanism, rather than caching the model name at startup.

### 3. Functional Improvements

#### Domain 1: User Experience (Web UI)
**Feature:** **"Focus Mode" Toggle**
*   **Context:** You have `state.html` to view state, but the UI doesn't visually react to it.
*   **Suggestion:** When `state.mode == DEEP_FOCUS`, inject a CSS class into `base.html` that hides the sidebar navigation, the "Ideas" input, and non-critical metrics. Make the interface minimalist to reduce visual cognitive load.

#### Domain 2: Input Capture
**Feature:** **Voice Note Transcription**
*   **Context:** You have the "Digital Twin" logic but input is text-only.
*   **Suggestion:** Add an endpoint `/api/voice` that accepts audio blobs. Use OpenAI Whisper (via `LLMService`) to transcribe, then feed the text directly into the `InterruptionFirewall` as an `IncomingItem`. This allows "walking and talking" to your second brain.

#### Domain 3: Agent Intelligence
**Feature:** **"Project Context" Injection**
*   **Context:** When the agent drafts an email, it looks at `writing_samples`.
*   **Suggestion:** Also inject the **Project Context**. If an email is about "Project X", perform a RAG search specifically for "Project X status/blockers" and inject that into the prompt.
*   **Why:** This prevents the agent from sounding confident but factually empty about project details.

#### Domain 4: Graph Visualization
**Feature:** **Interactive Graph Filtering**
*   **Context:** `graph.html` loads nodes based on checkbox filters.
*   **Suggestion:** Add a "Cluster" view. Use Neo4j Graph Data Science (GDS) or simple community detection to color-code nodes by their computed cluster (e.g., "Work/Marketing", "Personal/Health"). This makes the graph view useful for seeing disconnected islands of information.

### 4. Code Edits

Here is the fix for the **Vector Store Cleanup** omission.

<file path="src/cognitex/services/ingestion.py">
```python
<<<<
            if to_delete:
                # Delete orphaned documents
                await session.run("""
                    MATCH (d:Document)
                    WHERE d.drive_id IN $ids
                    DETACH DELETE d
                """, {"ids": list(to_delete)})
                deleted_count = len(to_delete)
                logger.info("Deleted orphaned documents", count=deleted_count)

        stats["deleted"] = deleted_count
====
            if to_delete:
                ids_list = list(to_delete)
                # Delete from Neo4j
                await session.run("""
                    MATCH (d:Document)
                    WHERE d.drive_id IN $ids
                    DETACH DELETE d
                """, {"ids": ids_list})
                deleted_count = len(ids_list)
                
                # ALSO Delete from Postgres (Vector Store)
                from cognitex.db.postgres import get_session
                from sqlalchemy import text
                
                async for pg_session in get_session():
                    # Delete chunks
                    await pg_session.execute(
                        text("DELETE FROM document_chunks WHERE drive_id = ANY(:ids)"),
                        {"ids": ids_list}
                    )
                    # Delete content
                    await pg_session.execute(
                        text("DELETE FROM document_content WHERE drive_id = ANY(:ids)"),
                        {"ids": ids_list}
                    )
                    # Delete embeddings (linked by entity_id which contains drive_id)
                    # Note: entity_id for chunks is "drive_id:index", for docs is "drive_id"
                    # We use a regex or similar approach, or strict matching if possible.
                    # Simpler to delete where entity_type='document' AND entity_id in ids
                    await pg_session.execute(
                        text("DELETE FROM embeddings WHERE entity_type = 'document' AND entity_id = ANY(:ids)"),
                        {"ids": ids_list}
                    )
                    # For chunks, we need a LIKE query or regex, or select IDs first.
                    # Simplified approach:
                    for drive_id in ids_list:
                         await pg_session.execute(
                            text("DELETE FROM embeddings WHERE entity_type = 'chunk' AND entity_id LIKE :pattern"),
                            {"pattern": f"{drive_id}:%"}
                         )
                    await pg_session.commit()

                logger.info("Deleted orphaned documents and embeddings", count=deleted_count)

        stats["deleted"] = deleted_count
>>>>
```

<chatName="Review Cognitex v14"/>