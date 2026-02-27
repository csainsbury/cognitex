# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Cognitex is a personal cognitive assistant that builds a semantic knowledge graph from Gmail, Google Calendar, Google Drive, and GitHub. An autonomous LLM-powered agent acts on the user's behalf — drafting emails, compiling meeting context packs, extracting tasks, and managing the knowledge graph.

## Commands

```bash
# Install
pip install -e ".[dev]"

# Run web dashboard (port 8080)
cognitex web

# Run API server (port 8000)
cognitex api

# Lint & format
ruff check src/
ruff format src/

# Type checking
mypy src/cognitex

# Tests
pytest tests/
pytest tests/ -k "test_name"       # single test
pytest tests/ --cov=src/cognitex   # with coverage

# Infrastructure (PostgreSQL + Neo4j + Redis)
docker compose up -d postgres neo4j redis
```

## Architecture

**Three-database system:**
- **PostgreSQL + pgvector** (`db/postgres.py`): Relational data (tasks, projects, goals, documents) and vector embeddings
- **Neo4j** (`db/neo4j.py`): Semantic knowledge graph — nodes for Email, Person, Task, Project, Goal, Document, Topic, etc. with typed relationships
- **Redis** (`db/redis.py`): Pub/sub event streaming, ARQ background job queue, working memory cache

**Source layout** (`src/cognitex/`):

| Directory | Purpose |
|-----------|---------|
| `agent/` | Autonomous ReAct-style agent: core loop, graph observer, decision memory, context packs, state model, tools, triggers, skills, bootstrap |
| `services/` | Business logic: Gmail sync/classify, Calendar sync, Drive indexing, GitHub sync, LLM abstraction, ingestion/chunking, hybrid search, memory files, notifications |
| `web/` | FastAPI + Jinja2 + HTMX dashboard (`app.py` is the main monolith — routes are inline, not split into separate files) |
| `api/` | REST API routes (health, tasks, goals, webhooks, sync) |
| `cli/` | Typer CLI (`main.py` — entry point registered as `cognitex` script) |
| `db/` | Database connections + schema definitions (postgres.py, neo4j.py, redis.py, graph_schema.py, phase3/4 schemas) |
| `prompts/` | LLM prompt templates |
| `skills/` | Bundled agent skills (email-tasks, meeting-prep, goal-linking) |
| `discord_bot/` | Discord integration |

**Agent system** (`agent/`):
- `core.py`: ReAct loop (Thought → Action → Observation, max 8 iterations)
- `graph_observer.py`: Scans graph for actionable items (stale emails, upcoming meetings, orphan nodes)
- `autonomous.py`: 15-minute interval proactive loop
- `tools.py`: 20+ tools with risk levels (READONLY, AUTO, APPROVAL)
- `state_model.py`: 6 operating modes (Deep Focus, Fragmented, Overloaded, etc.)
- `context_pack.py`: Multi-stage meeting prep (T-24h, T-2h, T-15m, T-5m)
- `decision_memory.py` + `learning.py`: Stores reasoning traces, learns from accepted/rejected proposals
- `bootstrap.py`: Loads user voice/personality from `~/.cognitex/bootstrap/` (SOUL.md, IDENTITY.md, CONTEXT.md)
- `skills.py`: Teachable behaviors from `~/.cognitex/skills/` + bundled skills

**LLM multi-provider** (`services/llm.py`, `services/model_config.py`):
- Supports Anthropic, Google, OpenAI, Together.ai
- Two-tier model system: planner (reasoning) + executor (structured tasks)
- Automatic fallback on 529 overload errors
- Provider selected via `LLM_PROVIDER` env var, switchable at runtime from Settings page

**Configuration** (`config.py`): Pydantic Settings loading from `.env` file. All env vars documented there.

## Code Style

- Python 3.12+, async throughout (asyncpg, neo4j async driver, httpx)
- Ruff: line length 100, rules E/W/F/I/B/C4/UP/ARG/SIM
- MyPy strict mode with Pydantic plugin
- Structured logging via `structlog`
- Database access via async context managers (`async for session in get_neo4j_session()`)
- Background jobs via ARQ (Redis-backed)

## User Data Directories

- `~/.cognitex/bootstrap/` — SOUL.md, IDENTITY.md, CONTEXT.md (agent personality)
- `~/.cognitex/skills/` — User-defined skills (override bundled by name)
- `~/.cognitex/memory/` — Daily logs (YYYY-MM-DD.md) and MEMORY.md (curated knowledge)
