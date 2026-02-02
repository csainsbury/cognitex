# Cognitex vs OpenClaw Comparison

## What They Are

| Aspect | Cognitex | OpenClaw |
|--------|----------|----------|
| **Purpose** | Unified life graph with cognitive assistance | Multi-channel messaging gateway with AI agent |
| **Primary Input** | Gmail, Calendar, Drive, GitHub | WhatsApp, Telegram, Discord, iMessage, Slack |
| **Storage** | Neo4j graph + PostgreSQL + Redis | JSONL transcripts + flat files |
| **Agent** | ReAct loop with 30+ tools | Embedded Pi agent runtime |
| **UI** | Web dashboard (FastAPI + HTMX) | CLI + WebSocket control plane |

## Key Similarities

1. Multi-provider LLM support with fallback
2. Tool-augmented reasoning
3. Message routing and classification
4. Approval workflows for sensitive actions
5. Session/conversation context management

## Key Differences

| Feature | Cognitex | OpenClaw |
|---------|----------|----------|
| **Prompt building** | Dynamic from learned context | Bootstrap files (SOUL.md, IDENTITY.md) |
| **Voice/style** | Algorithmic extraction from sent emails | User-defined in markdown files |
| **Task handling** | LLM inference | Skills teach specific behaviors |
| **Memory** | Episodic (pgvector) + Decision traces | Daily logs + curated MEMORY.md |
| **Context management** | Neo4j queries | Session pruning + compaction |
| **Extensibility** | Monolithic Python | Plugin architecture + hooks |

## What OpenClaw Does Better

1. **Explicit personality** - Bootstrap files make voice controllable
2. **Teachable behaviors** - Skills define rules through examples
3. **Human-readable memory** - Daily logs are editable
4. **Simpler context** - Files over databases for some things

## What Cognitex Does Better

1. **Semantic relationships** - Graph connects all life domains
2. **Cross-domain queries** - Find connections others miss
3. **Learning infrastructure** - Phase 4 closes feedback loops
4. **Rich integrations** - Gmail, Calendar, Drive, GitHub unified

## Adoption Strategy

Take OpenClaw's **user-facing patterns** (bootstrap, skills, memory) while keeping Cognitex's **backend architecture** (graph, learning, integrations).

### Phase 1: Bootstrap Files for Voice & Personality

Replace algorithmic learning with explicit, human-editable markdown files:

- `~/.cognitex/bootstrap/SOUL.md` - Core personality and voice
- `~/.cognitex/bootstrap/IDENTITY.md` - User context
- `~/.cognitex/bootstrap/CONTEXT.md` - Agent-maintained ambient context

### Phase 2: Skills-Based Task Recognition

Adopt OpenClaw's skills pattern - markdown files that teach the agent specific behaviors through examples and rules:

- `~/.cognitex/skills/` (user) + `src/cognitex/skills/` (bundled)
- User skills override bundled skills

### Phase 3: Graph-Integrated Memory System

Daily memory logs that are both human-readable AND queryable:

- `~/.cognitex/memory/YYYY-MM-DD.md` - Daily logs
- `~/.cognitex/memory/MEMORY.md` - Curated long-term memory
- MemoryEntry nodes in Neo4j for graph queries

### Phase 4: Integration & Polish

- Bootstrap ↔ Graph bidirectional sync
- Skills ↔ Learning integration
- Memory ↔ Existing systems integration

## Architecture Principles

1. **Graph remains source of truth** - Bootstrap/skills/memory enhance, don't replace
2. **Human-editable first** - All files are markdown, user can always edit
3. **Explicit over implicit** - Clear rules beat learned patterns
4. **Bidirectional sync** - Changes flow both directions (files ↔ graph)
5. **Graceful degradation** - System works without bootstrap files, just worse
