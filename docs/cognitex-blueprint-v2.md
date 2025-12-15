# Cognitex: Personal Agent System
## Blueprint v2 - Implementation Status & Roadmap

**Last Updated:** 2025-12-11
**Project Status:** Phase 3 In Progress (Agent System Complete, Interfaces Pending)

---

## 1. Executive Summary

Cognitex (formerly "Life OS") is a personal agent system designed to manage cognitive overhead for professional and personal administration. It monitors email, calendar, and documents, builds a relationship graph, infers actionable tasks, and proactively assists through Discord and CLI.

### Core Design Principles
- **Neurodivergent-friendly:** Energy-aware scheduling, overwhelm prevention, low-friction interaction
- **Graph-centric:** All entities connected in a queryable knowledge graph (Neo4j)
- **Staged autonomy:** Low-risk actions auto-execute, high-risk require approval
- **Hierarchical LLM:** Planner (reasoning) + Executors (task-specific) architecture
- **Dual interface:** CLI for deep work, Discord for mobile/proactive notifications

---

## 2. Implementation Status

### Phase 1: Foundation ✅ COMPLETE

| Component | Status | Notes |
|-----------|--------|-------|
| Docker Compose stack | ✅ | Neo4j, PostgreSQL (pgvector), Redis |
| Gmail API integration | ✅ | OAuth2, historical sync, read + send |
| Google Calendar integration | ✅ | Read events, create/update/delete |
| Google Drive integration | ✅ | Metadata sync, content indexing |
| Graph schema (Neo4j) | ✅ | Person, Email, Event, Task, Document nodes |
| PostgreSQL schema | ✅ | tasks, goals, energy_logs, embeddings, agent_memory |
| Together.ai integration | ✅ | DeepSeek V3 for planner + executor |
| Email classification | ✅ | LLM-based classification pipeline |

### Phase 2: Intelligence ✅ COMPLETE

| Component | Status | Notes |
|-----------|--------|-------|
| Task inference pipeline | ✅ | Extracts tasks from actionable emails |
| Approval workflow | ✅ | Redis-based staging for high-risk actions |
| Calendar event analysis | ✅ | Energy impact estimation, event type inference |
| Contact enrichment | ✅ | Org, role, communication style inference |
| Goal/project data model | ✅ | PostgreSQL schema ready, API stubs |

### Phase 3: Agent System ✅ COMPLETE (Added beyond original blueprint)

| Component | Status | Notes |
|-----------|--------|-------|
| Agent Core | ✅ | Observe → Think → Plan → Act loop |
| Planner (DeepSeek V3) | ✅ | Mode-specific prompts, ReAct-style reasoning |
| Executors | ✅ | Email, Task, Calendar, Notify executors |
| Working Memory (Redis) | ✅ | 24h context, approval staging |
| Episodic Memory (Postgres) | ✅ | Long-term decisions, feedback storage |
| Tool Registry | ✅ | 13 tools with risk levels |
| Trigger System | ✅ | Scheduled (APScheduler) + Event (Redis pub/sub) |
| Agent Chat | ✅ | Fast path (queries) + Planning path (actions) |

### Phase 3: Interfaces ✅ MOSTLY COMPLETE

| Component | Status | Notes |
|-----------|--------|-------|
| CLI commands | ✅ | status, sync, tasks, calendar, agent-chat, etc. |
| CLI TUI dashboard | ❌ | Textual-based dashboard not started |
| Discord bot basic | ✅ | Connected, database initialized on startup |
| Discord Redis listener | ✅ | Receives agent notifications with embeds |
| Discord NL interaction | ✅ | Full agent chat via natural language |
| Discord slash commands | ✅ | /tasks, /today, /briefing, /approvals, /status |
| Discord approval buttons | ✅ | Approve/Reject buttons + reaction support |
| Notification throttling | ❌ | Config exists, not enforced |

### Phase 4: Energy & Polish ❌ NOT STARTED

| Component | Status | Notes |
|-----------|--------|-------|
| Energy tracking service | ❌ | Schema exists, no implementation |
| Daily energy profile | ❌ | Not implemented |
| Dynamic adjustment | ❌ | Calendar load → availability |
| Relationship health | ❌ | Query exists, no automation |
| Morning briefing | ✅ | Agent mode implemented |
| Evening review | ✅ | Agent mode implemented |

### Phase 5: Autonomy 🔄 PARTIAL

| Component | Status | Notes |
|-----------|--------|-------|
| Risk-based tool execution | ✅ | READONLY/AUTO/APPROVAL levels |
| Email sending (approved) | ✅ | GmailSender class |
| Calendar creation (approved) | ✅ | CalendarService.create_event() |
| Confidence scoring | ✅ | Plan confidence 0-1 |
| Learning from corrections | ❌ | Feedback stored, not used |

---

## 3. Current Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        USER INTERFACES                          │
├─────────────────────┬───────────────────────┬───────────────────┤
│    CLI (Typer)      │   Discord Bot         │   FastAPI         │
│  - agent-chat       │  - Notifications      │  - /health        │
│  - tasks, calendar  │  - Redis listener     │  - /tasks (stub)  │
│  - sync, status     │  - NL chat (basic)    │  - /goals (stub)  │
└─────────────────────┴───────────────────────┴───────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      AGENT SYSTEM                               │
├─────────────────────────────────────────────────────────────────┤
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────────────┐ │
│  │   Planner   │───▶│  Executors  │───▶│   Tool Registry     │ │
│  │ (DeepSeek)  │    │ (DeepSeek)  │    │  13 tools by risk   │ │
│  └─────────────┘    └─────────────┘    └─────────────────────┘ │
│         │                                        │              │
│         ▼                                        ▼              │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │                    MEMORY SYSTEM                         │   │
│  │  Working (Redis 24h)  │  Episodic (Postgres)  │ Graph   │   │
│  └─────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      TRIGGER SYSTEM                             │
├─────────────────────────────────────────────────────────────────┤
│  Scheduled (APScheduler)     │    Event-Driven (Redis Pub/Sub)  │
│  - 8am Morning briefing      │    - cognitex:events:email       │
│  - 6pm Evening review        │    - cognitex:events:calendar    │
│  - Hourly monitoring         │    - cognitex:events:task        │
│  - Overdue task check        │                                  │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                         SERVICES                                │
├────────────┬────────────┬──────────────┬────────────┬───────────┤
│   Gmail    │  Calendar  │    Drive     │    LLM     │ Ingestion │
│  read/send │ read/write │ sync/index   │ Together.ai│  pipeline │
└────────────┴────────────┴──────────────┴────────────┴───────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                        DATA STORES                              │
├─────────────────┬───────────────────────┬───────────────────────┤
│     Neo4j       │      PostgreSQL       │        Redis          │
│  Graph data     │  Structured data      │  Cache, queues        │
│  - Person       │  - tasks              │  - Working memory     │
│  - Email        │  - goals              │  - Approvals          │
│  - Event        │  - energy_logs        │  - Pub/sub events     │
│  - Task         │  - embeddings         │                       │
│  - Document     │  - agent_memory       │                       │
└─────────────────┴───────────────────────┴───────────────────────┘
```

---

## 4. Tool Inventory

### Read-Only Tools (Always Allowed)
| Tool | Description |
|------|-------------|
| `graph_query` | Execute Cypher queries on Neo4j |
| `search_documents` | Semantic search over indexed documents |
| `read_document` | Read full content of a Drive document |
| `get_calendar` | Fetch upcoming calendar events |
| `get_tasks` | Query tasks by status/filters |
| `get_contact` | Get person details from graph |
| `recall_memory` | Search episodic memory |

### Auto-Execute Tools (Low Risk)
| Tool | Description |
|------|-------------|
| `create_task` | Create a new task in Neo4j |
| `update_task` | Update task status/details |
| `send_notification` | Send Discord notification |
| `add_memory` | Store to episodic memory |

### Approval-Required Tools (High Risk)
| Tool | Description |
|------|-------------|
| `draft_email` | Draft email for review |
| `send_email` | Send email (after approval) |
| `create_event` | Create calendar event |

---

## 5. CLI Commands Reference

```bash
# System
cognitex status              # Show system status
cognitex auth                # Google OAuth flow
cognitex graph               # Neo4j statistics

# Data Sync
cognitex sync                # Sync Gmail (incremental)
cognitex sync --full         # Full Gmail sync
cognitex calendar            # Sync calendar events
cognitex drive-sync          # Sync Drive metadata
cognitex drive-sync --index-priority  # Index priority folders

# Queries
cognitex today               # Today's schedule + energy
cognitex tasks               # List tasks
cognitex tasks --status pending
cognitex contacts            # List contacts
cognitex documents           # List documents
cognitex doc-search "query"  # Semantic document search

# Agent
cognitex agent-chat          # Interactive agent chat
cognitex agent-chat "query"  # Single query
cognitex briefing            # Morning briefing
cognitex approvals           # List pending approvals
cognitex approvals approve <id>
cognitex approvals reject <id>
cognitex agent-run <mode>    # Run agent mode
cognitex agent-status        # Agent configuration

# Processing
cognitex classify            # Classify unprocessed emails
cognitex infer-tasks         # Infer tasks from emails
cognitex enrich              # Enrich contact profiles
```

---

## 6. Roadmap: Next Steps

### Immediate (This Week)

#### 6.1 ~~Complete Discord Natural Language~~ ✅ DONE
- [x] Full agent chat integration via natural language
- [x] Slash commands: /tasks, /today, /briefing, /approvals, /status
- [x] Approval buttons and reaction support
- [x] Formatted embeds with urgency indicators

#### 6.2 Wire Up REST API Endpoints
- [ ] Implement `GET /tasks` with proper Neo4j queries
- [ ] Implement `POST /tasks` for task creation
- [ ] Implement `GET /goals`, `POST /goals`
- [ ] Add authentication (API key or JWT)

#### 6.3 Push Notifications (Gmail/Calendar)
- [ ] Set up Google Cloud Pub/Sub topic
- [ ] Implement Gmail watch API for real-time email notifications
- [ ] Implement Calendar watch for event changes
- [ ] Connect to Redis pub/sub for agent triggers

### Short-Term (Next 2 Weeks)

#### 6.4 Energy Tracking System
- [ ] Create `EnergyService` class
- [ ] Implement `log_energy()` - manual logging
- [ ] Implement `predict_daily_energy()` - calendar-based prediction
- [ ] Implement `get_available_capacity()` - remaining energy
- [ ] Add CLI commands: `cognitex energy`, `cognitex energy set 5`
- [ ] Integrate energy into agent planning prompts

#### 6.5 Textual TUI Dashboard
- [ ] Create main dashboard with panels:
  - Today's energy + forecast
  - Upcoming events (next 3)
  - Top 5 pending tasks
  - Recent agent actions
- [ ] Inbox view for processing emails
- [ ] Graph explorer with natural language queries

#### 6.6 Relationship Health
- [ ] Implement `get_relationship_health()` query
- [ ] Add to morning briefing: "You haven't replied to X in 2 weeks"
- [ ] Create CLI command: `cognitex relationships`

### Medium-Term (Next Month)

#### 6.7 Learning from Feedback
- [ ] Track approval/rejection patterns per action type
- [ ] Adjust confidence thresholds based on track record
- [ ] Store rejection feedback for prompt refinement
- [ ] Implement confidence decay for unused patterns

#### 6.8 Advanced Calendar Features
- [ ] Suggest optimal meeting times based on energy
- [ ] Detect scheduling conflicts
- [ ] Auto-suggest prep time before important meetings
- [ ] Focus time protection (block suggestions)

#### 6.9 Project/Goal Management
- [ ] Create Project nodes in Neo4j
- [ ] Link tasks to projects automatically (LLM inference)
- [ ] OKR tracking with progress indicators
- [ ] Weekly goal review automation

### Long-Term (Future)

#### 6.10 Full Autonomy Mode
- [ ] Configurable auto-approval thresholds per action type
- [ ] "Vacation mode" - handle routine items autonomously
- [ ] Escalation rules based on sender importance
- [ ] Undo/rollback for autonomous actions

#### 6.11 Multi-Modal Input
- [ ] Voice input via Discord or Whisper API
- [ ] Screenshot/image understanding for task context
- [ ] Calendar screenshot parsing

#### 6.12 External Integrations
- [ ] Slack workspace integration
- [ ] Linear/Jira task sync
- [ ] Notion page monitoring
- [ ] Bank transaction categorization

---

## 7. Configuration Reference

### Environment Variables (.env)

```bash
# Database
POSTGRES_PASSWORD=<secure_password>
NEO4J_PASSWORD=<secure_password>
DATABASE_URL=postgresql://cognitex:<url_encoded_password>@localhost:5432/cognitex

# Redis
REDIS_URL=redis://localhost:6379/0

# Together.ai
TOGETHER_API_KEY=<api_key>
TOGETHER_MODEL_PLANNER=deepseek-ai/DeepSeek-V3
TOGETHER_MODEL_EXECUTOR=deepseek-ai/DeepSeek-V3
TOGETHER_MODEL_EMBEDDING=togethercomputer/m2-bert-80M-8k-retrieval

# Google API
GOOGLE_CLIENT_ID=<client_id>
GOOGLE_CLIENT_SECRET=<client_secret>

# Discord
DISCORD_BOT_TOKEN=<bot_token>
DISCORD_CHANNEL_ID=<channel_id>

# Application
ENVIRONMENT=development
LOG_LEVEL=INFO
```

### Agent Modes

| Mode | Trigger | Behavior |
|------|---------|----------|
| `BRIEFING` | 8am daily | Summarize day, priorities, energy forecast |
| `REVIEW` | 6pm daily | What got done, rollover items, tomorrow preview |
| `MONITOR` | Hourly | Check for urgent items only |
| `PROCESS_EMAIL` | New email event | Classify, infer tasks, draft reply if needed |
| `PROCESS_EVENT` | Calendar change | Update energy forecast, check conflicts |
| `CONVERSATION` | User chat | Interactive assistance |
| `ESCALATE` | Overdue trigger | Handle overdue tasks, repeated follow-ups |

---

## 8. Development Commands

```bash
# Start services
docker compose up -d

# Install package
pip install -e .

# Run CLI
cognitex --help

# Run API server
uvicorn cognitex.api.main:app --reload

# Run worker (with triggers)
python -m cognitex.worker

# Run Discord bot
python -m cognitex.discord_bot

# Run tests
pytest tests/
```

---

## 9. Files Changed Since v1

### New Files (Agent System)
- `src/cognitex/agent/core.py` - Main orchestrator
- `src/cognitex/agent/planner.py` - LLM planner
- `src/cognitex/agent/executors.py` - Task executors
- `src/cognitex/agent/memory.py` - Working + episodic memory
- `src/cognitex/agent/tools.py` - 13 tools with registry
- `src/cognitex/agent/triggers.py` - Scheduled + event triggers
- `docs/agent-architecture.md` - Agent documentation

### Modified Files (Code Review Fixes)
- `src/cognitex/services/gmail.py` - Added GmailSender class
- `src/cognitex/services/calendar.py` - Added create/update/delete
- `src/cognitex/services/llm.py` - Fixed model config, added retry
- `src/cognitex/worker.py` - Added trigger system startup
- `src/cognitex/discord_bot/__main__.py` - Added Redis listener
- `src/cognitex/api/routes/health.py` - Added deep health check
- `docker/postgres/init.sql` - Added agent_memory table

---

## 10. Known Issues & Technical Debt

1. **API endpoints are stubs** - `/tasks` and `/goals` return empty/NotImplemented
2. **No authentication** - API has no auth, only local use assumed
3. **Notification throttling not enforced** - Config exists but not checked
4. **Energy system not implemented** - Schema only, no service
5. **TUI not started** - CLI is command-based only
6. **Gmail/Calendar push notifications not set up** - Polling only
7. **Tests minimal** - Need comprehensive test coverage
8. **No CI/CD pipeline** - Manual deployment only

---

## 11. Success Metrics (Future)

- **Response time**: Agent chat < 5s for queries, < 15s for planning
- **Task inference accuracy**: > 80% of suggested tasks are approved
- **Email draft quality**: > 70% sent without major edits
- **Energy prediction accuracy**: Within 2 points of actual
- **Notification relevance**: < 10% dismissed as irrelevant
- **Relationship health**: 0 contacts with > 30 day response gap

---

*This blueprint is a living document. Update as implementation progresses.*
