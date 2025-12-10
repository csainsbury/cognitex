# Cognitex Agent Architecture

## Overview

The Cognitex agent is a hierarchical LLM-based system that manages cognitive overhead by:
- Observing incoming data (emails, calendar, tasks)
- Reasoning about what needs attention
- Planning appropriate actions
- Executing with appropriate approval gates

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         COGNITEX AGENT                               │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  TRIGGERS                    PLANNER                 EXECUTORS       │
│  ────────                    ───────                 ─────────       │
│  Scheduled:                  Qwen3-30B-A3B           DeepSeek V3     │
│  • 8am morning briefing      (MoE, 3B active)        (fast, capable) │
│  • 6pm evening review                                                │
│  • Hourly monitoring         Responsibilities:       Responsibilities:│
│                              • Analyze context       • Draft emails   │
│  Event-driven:               • Reason about needs    • Create tasks   │
│  • New email                 • Create action plans   • Schedule events│
│  • Calendar change           • Assign to executors   • Notify user    │
│  • Task overdue                                                      │
│                                                                      │
│  User-initiated:                                                     │
│  • Discord message                                                   │
│  • CLI command                                                       │
│                                                                      │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  MEMORY SYSTEM                                                       │
│  ─────────────                                                       │
│                                                                      │
│  Working Memory (Redis) - 24h TTL                                   │
│  ├── context         Current conversation/session state              │
│  ├── pending_approvals  Staged actions awaiting user OK             │
│  ├── observations    Recent things the agent noticed                 │
│  └── scratch_pad     Intermediate reasoning                          │
│                                                                      │
│  Episodic Memory (Postgres + pgvector) - Permanent                  │
│  ├── interactions    Past conversations + outcomes                   │
│  ├── decisions       What the agent decided + reasoning              │
│  ├── feedback        User corrections and preferences                │
│  └── observations    Patterns noticed over time                      │
│                                                                      │
│  Semantic Memory (Neo4j) - The knowledge graph                      │
│  └── People, emails, events, tasks, documents, relationships        │
│                                                                      │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  TOOL SYSTEM                                                         │
│  ───────────                                                         │
│                                                                      │
│  Read-only (always allowed):                                        │
│  • graph_query      - Cypher queries against Neo4j                  │
│  • search_documents - Semantic search via pgvector                  │
│  • get_calendar     - Fetch events for date range                   │
│  • get_tasks        - Fetch tasks with filters                      │
│  • get_contact      - Get person profile + history                  │
│  • recall_memory    - Search episodic memory                        │
│                                                                      │
│  Auto-execute (low risk):                                           │
│  • create_task      - Add task to graph                             │
│  • update_task      - Change task status/details                    │
│  • send_notification - Discord notification                         │
│  • add_memory       - Store to episodic memory                      │
│                                                                      │
│  Approval required (high risk):                                     │
│  • draft_email      - Stage email for review                        │
│  • send_email       - Actually send (after approval)                │
│  • create_event     - Add calendar event                            │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

## Agent Modes

| Mode | Trigger | Purpose |
|------|---------|---------|
| `BRIEFING` | 8am daily | Morning summary with priorities |
| `REVIEW` | 6pm daily | End of day recap, rollover items |
| `MONITOR` | Hourly 9-6 | Check for urgent items only |
| `PROCESS_EMAIL` | New email | Classify, create tasks, draft replies |
| `PROCESS_EVENT` | Calendar change | Update energy forecast, note prep |
| `CONVERSATION` | User message | Interactive chat, handle requests |
| `ESCALATE` | Overdue/urgent | Handle escalations |

## Execution Flow

```
1. TRIGGER arrives (scheduled/event/user)
         │
         v
2. CONTEXT ASSEMBLY
   • Load working memory (recent context)
   • Query relevant graph data
   • Recall related episodic memories
   • Get current state (energy, pending items)
         │
         v
3. PLANNER (Qwen3-30B-A3B)
   • Receives: trigger + context + user profile
   • Outputs: reasoning + action plan as JSON
   • Each step assigned to an executor
         │
         v
4. EXECUTOR DISPATCH
   For each step in plan:
   • Check tool risk level
   • If readonly/auto: execute immediately
   • If approval: stage and notify user
   • Collect results
         │
         v
5. RESULT SYNTHESIS
   • Update working memory
   • Store decision in episodic memory
   • Send notification if needed
   • Schedule follow-up if needed
```

## Approval Flow

```
Agent decides to send email
         │
         v
┌─────────────────────────┐
│  STAGE FOR APPROVAL     │
│                         │
│  Stored in Redis:       │
│  - approval_id          │
│  - action_type          │
│  - params (to, body)    │
│  - reasoning            │
│  - expires: 24h         │
└───────────┬─────────────┘
            │
            v
┌─────────────────────────┐
│  NOTIFY USER            │
│                         │
│  Via Discord/CLI:       │
│  "I'd like to send      │
│   this to Sarah..."     │
│                         │
│  [Approve] [Edit] [Skip]│
└───────────┬─────────────┘
            │
    ┌───────┴───────┐
    v               v
 Approved        Rejected
    │               │
    v               v
 Execute        Store feedback
 action         for learning
```

## Models

| Role | Model | Provider | Notes |
|------|-------|----------|-------|
| Planner | Qwen3-30B-A3B | Together.ai | MoE architecture, only 3B active params |
| Executors | DeepSeek V3 | Together.ai | Fast, excellent at structured tasks |
| Embeddings | m2-bert-80M | Together.ai | For semantic search |

## CLI Commands

```bash
# Interactive chat
cognitex agent-chat                    # Start interactive session
cognitex agent-chat "What's urgent?"   # Single message

# Briefings
cognitex briefing                      # Get morning summary

# Approvals
cognitex approvals                     # List pending
cognitex approvals approve apr_xxx     # Approve action
cognitex approvals reject apr_xxx      # Reject action

# Manual mode execution
cognitex agent-run briefing            # Run briefing mode
cognitex agent-run monitor             # Run monitoring check
cognitex agent-run escalate            # Check overdue items

# Status
cognitex agent-status                  # Show config + memory stats
```

## Configuration

In `.env`:

```bash
# Required
TOGETHER_API_KEY=your-key

# Optional (defaults shown)
TOGETHER_MODEL_PLANNER=Qwen/Qwen3-30B-A3B
TOGETHER_MODEL_EXECUTOR=deepseek-ai/DeepSeek-V3
TOGETHER_MODEL_EMBEDDING=togethercomputer/m2-bert-80M-8k-retrieval
```

## File Structure

```
src/cognitex/agent/
├── __init__.py      # Module exports
├── core.py          # Agent orchestrator
├── planner.py       # Qwen3 planner with mode-specific prompts
├── executors.py     # DeepSeek executors (email, task, calendar, notify)
├── memory.py        # Working (Redis) + Episodic (Postgres) memory
├── tools.py         # 12 tools with risk levels
└── triggers.py      # Scheduled + event-driven trigger system
```

## Extending the Agent

### Adding a new tool

```python
# In tools.py

class MyNewTool(BaseTool):
    name = "my_tool"
    description = "What this tool does"
    risk = ToolRisk.AUTO  # or READONLY, APPROVAL
    parameters = {
        "param1": {"type": "string", "description": "..."},
    }

    async def execute(self, param1: str) -> ToolResult:
        # Do the thing
        return ToolResult(success=True, data={"result": "..."})

# Register in ToolRegistry._register_defaults()
```

### Adding a new executor

```python
# In executors.py

class MyExecutor(BaseExecutor):
    name = "my_executor"
    description = "Handles specific domain"

    async def execute(self, tool: str, args: dict, reasoning: str) -> ToolResult:
        # Custom logic or delegate to tool
        return await self.registry.execute(tool, **args)

# Register in ExecutorRegistry._register_defaults()
```

### Adding a new trigger

```python
# In triggers.py, add to _setup_scheduled_triggers():

self.scheduler.add_job(
    self._my_handler,
    CronTrigger(hour=12, minute=0),  # noon daily
    id="my_trigger",
    name="My Custom Trigger",
)

async def _my_handler(self):
    await self.agent.run(
        mode=AgentMode.MONITOR,
        trigger="My custom trigger fired",
    )
```
