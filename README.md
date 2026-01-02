# Cognitex

A personal cognitive assistant that manages your digital life through a semantic knowledge graph. Cognitex integrates with Gmail, Google Calendar, Google Drive, and GitHub to build a unified view of your work, then uses an autonomous agent to help manage tasks, draft emails, and prepare for meetings.

## Features

### Knowledge Graph Integration
- **Gmail**: Syncs emails, classifies them (actionable/informational/urgent), extracts tasks, and tracks communication patterns
- **Google Calendar**: Syncs events, identifies meetings needing preparation
- **Google Drive**: Indexes documents with semantic embeddings, extracts topics and concepts
- **GitHub**: Syncs repositories and code files with semantic search

### Autonomous Digital Twin Agent
An LLM-powered agent that acts on your behalf when you're not available:
- **Drafts email replies** in your voice (learns from your sent emails)
- **Compiles context packs** for upcoming meetings
- **Suggests focus blocks** for projects needing attention
- **Auto-links** documents, tasks, and projects in the knowledge graph
- **Flags items** requiring human judgment
- **Task proposals** with approval workflow (configurable auto vs propose mode)
- **Decision memory** for learning from accepted/rejected proposals

### Executive Function Layer
- **Operating Modes**: Deep Focus, Fragmented, Overloaded, etc.
- **Interruption Firewall**: Captures incoming items based on current mode
- **Decision Policy**: Recommends next actions based on energy and task fit
- **Context Packs**: Auto-generated briefings for meetings and decisions

### Web Dashboard
- Task, Project, and Goal management with inline editing
- Document search with semantic similarity
- Agent log with action history
- Digital Twin review page for approving drafts and suggestions
- Real-time state and mode visualization

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Web Dashboard (FastAPI)                  │
├─────────────────────────────────────────────────────────────┤
│                    Autonomous Agent (LLM)                    │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐       │
│  │ Graph        │  │ Digital Twin │  │ Decision     │       │
│  │ Observer     │  │ Actions      │  │ Memory       │       │
│  └──────────────┘  └──────────────┘  └──────────────┘       │
├─────────────────────────────────────────────────────────────┤
│                    Knowledge Graph (Neo4j)                   │
│  Nodes: Email, Person, Task, Project, Goal, Document, etc.  │
├─────────────────────────────────────────────────────────────┤
│                  Vector Store (PostgreSQL + pgvector)        │
├─────────────────────────────────────────────────────────────┤
│  Redis (pub/sub)  │  Gmail API  │  Calendar  │  Drive  │ GH │
└─────────────────────────────────────────────────────────────┘
```

## Installation

### Prerequisites
- Python 3.11+
- Neo4j 5.x
- PostgreSQL 15+ with pgvector extension
- Redis 7+
- Google Cloud project with Gmail, Calendar, Drive APIs enabled

### Setup

1. Clone the repository:
```bash
git clone https://github.com/yourusername/cognitex.git
cd cognitex
```

2. Create and activate a conda environment:
```bash
conda create -n cognitex python=3.12
conda activate cognitex
```

3. Install dependencies:
```bash
pip install -e .
```

4. Copy environment template and configure:
```bash
cp .env.example .env
# Edit .env with your API keys and database credentials
```

5. Set up Google OAuth:
   - Create OAuth credentials in Google Cloud Console
   - Download `client_secret.json` to `data/` directory
   - Run `cognitex auth` to authenticate

6. Initialize databases:
```bash
# Start Neo4j, PostgreSQL, Redis (via Docker or native)
cognitex init-phase3  # Initialize schema
```

## Configuration

Key environment variables in `.env`:

```bash
# LLM Provider
TOGETHER_API_KEY=your_key_here

# Neo4j
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=password

# PostgreSQL
DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/cognitex

# Redis
REDIS_URL=redis://localhost:6379

# Google APIs
GOOGLE_PUBSUB_TOPIC=projects/your-project/topics/gmail-push

# Discord (optional, for notifications)
DISCORD_BOT_TOKEN=your_token
DISCORD_CHANNEL_ID=your_channel_id

# Autonomous Agent
AUTONOMOUS_AGENT_ENABLED=true
AUTONOMOUS_AGENT_INTERVAL_MINUTES=15
TASK_CREATION_MODE=propose  # 'auto' or 'propose' (requires approval)
```

## Usage

### CLI Commands

```bash
# Authentication
cognitex auth              # Authenticate with Google

# Sync data
cognitex sync              # Sync Gmail
cognitex calendar          # Sync Calendar
cognitex drive-sync        # Sync Drive
cognitex github-sync owner/repo  # Sync GitHub repo

# Classify and process
cognitex classify          # Classify emails with LLM
cognitex infer-tasks       # Extract tasks from emails
cognitex analyze-chunks    # Build semantic graph from documents

# Task management
cognitex tasks             # List tasks
cognitex task-add "Title"  # Add task
cognitex task-done 1       # Complete task

# Projects and Goals
cognitex projects          # List projects
cognitex goals             # List goals
cognitex goal-parse "..."  # Parse goal into projects/tasks

# Agent
cognitex agent-chat        # Interactive chat with agent
cognitex briefing          # Get daily briefing

# Web interfaces
cognitex web               # Start web dashboard (port 8080)
cognitex api               # Start API server (port 8000)
```

### Web Dashboard

Start the dashboard:
```bash
cognitex web
```

Navigate to `http://localhost:8080`:
- **Dashboard**: Overview of tasks, projects, goals
- **Today**: Today's schedule and energy forecast
- **Tasks/Projects/Goals**: CRUD management
- **Documents**: Semantic search
- **Twin**: Review agent-drafted emails and suggestions
- **Agent Log**: Action history
- **State**: Current operating mode

### Autonomous Agent

The agent runs automatically every 15 minutes (configurable) and:
1. Observes graph state (stale items, orphaned nodes, actionable emails)
2. Reasons about what actions to take
3. Executes actions (drafts, links, tasks) or proposes them for approval
4. Logs all decisions for review and learning

Review agent outputs at `/twin`:
- Approve/edit/discard email drafts
- Archive context packs
- Accept/dismiss focus block suggestions

### Discord Bot

The Discord bot provides notifications and commands:

```
/briefing          - Get daily briefing
/state             - Show current operating state
/proposals         - List pending task proposals
/approve <id>      - Approve a task proposal
/reject <id>       - Reject a task proposal
/sync              - Trigger data sync
/calendar          - Show upcoming events
```

## Project Structure

```
cognitex/
├── src/cognitex/
│   ├── agent/           # Autonomous agent
│   │   ├── autonomous.py    # Main agent loop
│   │   ├── graph_observer.py # Graph state monitoring
│   │   ├── tools.py         # Agent tools
│   │   ├── decision_memory.py # Learning from decisions
│   │   ├── memory.py        # Working + episodic memory
│   │   └── action_log.py    # Action logging + task proposals
│   ├── api/             # REST API
│   ├── cli/             # CLI commands
│   ├── db/              # Database models
│   │   ├── neo4j.py
│   │   ├── postgres.py
│   │   └── graph_schema.py
│   ├── prompts/         # LLM prompt templates
│   ├── services/        # Business logic
│   │   ├── gmail.py
│   │   ├── calendar.py
│   │   ├── drive.py
│   │   ├── github.py
│   │   └── llm.py
│   └── web/             # Web dashboard
│       ├── app.py
│       └── templates/
├── docs/                # Documentation
├── data/                # Local data (credentials, etc.)
└── tests/
```

## Node Types in Knowledge Graph

| Node | Description |
|------|-------------|
| Email | Gmail messages with classification |
| Person | Contacts with communication patterns |
| Task | Action items with priority and energy cost |
| Project | Collections of related tasks |
| Goal | High-level objectives |
| Document | Drive files with embeddings |
| CalendarEvent | Calendar entries |
| Repository | GitHub repos |
| CodeFile | Source code with embeddings |
| Topic | Extracted topics from documents |
| Concept | Extracted concepts |
| EmailDraft | Agent-drafted replies |
| ContextPack | Meeting preparation briefings |
| SuggestedBlock | Focus time suggestions |

## Documentation

Implementation blueprints and status tracking:
- `docs/cognitex_phase_3_blueprint.md` - Phase 3: Executive Function Layer
- `docs/PHASE3_STATUS.md` - Phase 3 implementation status
- `docs/PHASE4_MEMORY_BLUEPRINT.md` - Phase 4: Adaptive Memory & Learning System

## Development

### Running Tests
```bash
pytest tests/
```

### Code Style
```bash
ruff check src/
ruff format src/
```

## License

MIT License - see LICENSE file for details.

## Acknowledgments

- Built with Claude (Anthropic) for LLM capabilities
- Uses Together.ai for embeddings
- Neo4j for graph database
- FastAPI for web framework
