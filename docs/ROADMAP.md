# Cognitex Future Development Roadmap

Planned features and improvements for future development.

---

## Graph & Visualization

### Graph Clustering / Community Detection
**Priority:** Low
**Complexity:** Medium
**Dependencies:** Neo4j GDS plugin (free Community Edition)

Add automatic clustering to the graph visualization using community detection algorithms (Louvain, Label Propagation). Would:
- Color-code nodes by detected cluster
- Reveal hidden project groupings
- Identify knowledge islands (disconnected areas)
- Show people clusters (actual working groups)

**Implementation notes:**
- Install Neo4j GDS plugin in docker-compose
- Run community detection algorithm on graph
- Store cluster IDs on nodes
- Update graph.html to color by cluster

---

## User Experience

### Focus Mode UI
**Priority:** Low
**Complexity:** Low

When `state.mode == DEEP_FOCUS`, inject CSS to hide sidebar navigation, Ideas input, and non-critical metrics. Minimalist interface to reduce cognitive load.

---

## Agent Intelligence

### Project Context Injection for Emails
**Priority:** Medium
**Complexity:** Medium

When agent drafts emails about a specific project, perform RAG search for "Project X status/blockers" and inject into prompt. Prevents confident but factually empty responses about project details.

---

## Learning System

### Rejection Reason Capture
**Priority:** Medium
**Complexity:** Low

When dismissing agent proposals in twin.html, show a modal asking "Why?" with options like:
- "Wrong project"
- "Not now / bad timing"
- "Bad tone / style"
- "Never suggest this again"

This explicit signal helps the Learning System (Phase 4) differentiate between:
- Bad timing (reschedule later)
- Bad idea (never suggest again)
- Style mismatch (adjust writing approach)

**Implementation notes:**
- Add modal to twin.html dismiss/discard buttons
- Pass reason to `reject_proposal(..., reason=...)` in action_log.py
- Store reason in learned_patterns table
- Use reason to improve suppression logic in `should_suppress_proposal()`

---

## Infrastructure

### LLM Config Hot Reload
**Priority:** Low
**Complexity:** Medium

Currently `LLMService` caches model config at startup. Changes via settings page require server restart. Should check Redis config dynamically or implement refresh mechanism.

### Sync API Consolidation
**Priority:** Low
**Complexity:** Low

`/api/sync/sessions` endpoint is duplicated in both `web/app.py` and `api/routes/sync.py`. Should consolidate to single implementation.

---

## Completed

- [x] Vector store cleanup when documents deleted from Drive (v14 review)
- [x] Shared indexing check between basic and deep indexing
- [x] Ideas scratch pad with web form, API, and email capture
- [x] Task subtasks with drag-and-drop reordering
