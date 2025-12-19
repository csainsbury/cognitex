# Cognitex: A Multi-Tier Personal Agent Architecture for Cognitive Load Management Through Semantic Knowledge Graphs and Risk-Stratified Autonomous Action

**Authors:** Chris Sainsbury¹

¹Independent Researcher, United Kingdom

**Correspondence:** chris@sainsbury.im

---

## Abstract

We present Cognitex, a novel personal agent architecture designed to reduce cognitive overhead for knowledge workers by unifying email, calendar, document, and code repository data into a hybrid semantic knowledge graph. The system implements a hierarchical large language model (LLM) architecture with distinct reasoning and execution tiers, combined with a three-level risk stratification framework (read-only, auto-execute, approval-required) that balances autonomous operation with human agency. Our approach integrates three complementary knowledge representations: a Neo4j property graph capturing entities and relationships, PostgreSQL with pgvector extensions for 768-dimensional semantic embeddings, and Redis-backed working memory for session context. The agent employs a ReAct-style (Reason + Act) planning loop with decision trace capture, enabling learning from user feedback through retrieval-augmented generation over historical decisions. We describe the complete system architecture, including data ingestion pipelines, the semantic graph schema, the agent's tool ecosystem comprising 27 specialized tools, and the trigger system supporting both scheduled and event-driven activation. Cognitex represents a reference implementation for trustworthy personal AI systems that maintain transparency while providing meaningful cognitive assistance.

**Keywords:** Personal agents, Knowledge graphs, Large language models, Cognitive load management, Semantic search, ReAct agents, Human-AI collaboration

---

## 1. Introduction

### 1.1 Motivation

Knowledge workers face an increasingly fragmented digital environment characterized by multiple communication channels, calendaring systems, document repositories, and project management tools. This fragmentation imposes substantial cognitive overhead: the mental effort required to track commitments, maintain relationship context, and synthesize information across disparate sources. Studies estimate that knowledge workers spend 28% of their workweek managing email alone (McKinsey Global Institute, 2012), with significant additional time lost to context switching between applications.

While general-purpose AI assistants have demonstrated impressive capabilities in natural language understanding and generation, they typically operate without persistent context about the user's specific professional network, commitments, and communication patterns. This limitation necessitates repeated context provision, undermining the efficiency gains such systems might otherwise provide.

### 1.2 Contributions

This paper presents Cognitex, a personal agent system that addresses these challenges through several novel contributions:

1. **Hybrid Knowledge Representation**: We integrate property graphs (Neo4j), vector embeddings (PostgreSQL/pgvector), and key-value caching (Redis) to capture complementary aspects of personal knowledge—relationships, semantic similarity, and session context respectively.

2. **Risk-Stratified Tool Execution**: Our framework categorizes agent capabilities into three risk tiers (READONLY, AUTO, APPROVAL), enabling autonomous operation for safe actions while preserving human oversight for consequential decisions.

3. **Decision Trace Learning**: We capture complete decision contexts including triggers, proposed actions, and user feedback, enabling retrieval-augmented generation from similar historical decisions and providing infrastructure for behavioral fine-tuning.

4. **Hierarchical LLM Architecture**: The system employs specialized models for planning (reasoning about situations) versus execution (structured task completion), optimizing for both cognitive depth and operational efficiency.

5. **Multi-Modal Triggering**: The agent responds to scheduled events (morning briefings, periodic monitoring), external triggers (new emails, calendar changes), and direct user interaction through multiple interfaces (CLI, Discord, REST API).

### 1.3 Paper Organization

Section 2 reviews related work in personal agents, knowledge graphs, and LLM-based systems. Section 3 presents the system architecture, including data models and service integrations. Section 4 details the agent subsystem, covering the planner, executors, tools, and memory systems. Section 5 describes the semantic graph construction and query patterns. Section 6 covers the document chunking and embedding pipeline. Section 7 discusses the trigger and notification infrastructure. Section 8 presents preliminary evaluation results. Section 9 discusses limitations and future directions, and Section 10 concludes.

---

## 2. Related Work

### 2.1 Personal Information Management

Personal information management (PIM) systems have evolved from simple contact databases to sophisticated knowledge organization tools. Semantic desktop initiatives (Sauermann et al., 2005) proposed treating personal data as a knowledge graph, though adoption was limited by the manual effort required for ontology construction. More recent approaches leverage machine learning for automatic relationship extraction (Dong et al., 2014), though typically focusing on enterprise rather than personal contexts.

### 2.2 LLM-Based Agents

The emergence of capable large language models has catalyzed research into autonomous agents. ReAct (Yao et al., 2022) demonstrated the effectiveness of interleaved reasoning and action traces for tool-using agents. Subsequent work has explored tool learning (Qin et al., 2023), self-reflection (Shinn et al., 2023), and multi-agent collaboration (Park et al., 2023). However, most systems target general-purpose assistance rather than the specialized domain of personal productivity with its requirements for persistent context and relationship awareness.

### 2.3 Knowledge Graph Construction

Automatic knowledge graph construction from unstructured text has received extensive attention (Nickel et al., 2016). Entity recognition, relation extraction, and entity linking form the core pipeline. For personal knowledge graphs specifically, challenges include handling the diversity of personal data sources and maintaining privacy constraints. Our approach differs by leveraging LLM capabilities for flexible entity extraction while maintaining structured schemas for core entity types.

### 2.4 Semantic Search and Retrieval

Dense retrieval methods using learned embeddings have largely superseded sparse retrieval for semantic search tasks (Karpukhin et al., 2020). The integration of vector databases with traditional relational systems enables hybrid queries combining structured filters with semantic similarity. Our architecture employs this pattern, using PostgreSQL's pgvector extension for embedding storage alongside structured metadata.

---

## 3. System Architecture

### 3.1 Overview

Cognitex implements a multi-tier architecture comprising data services, integration services, agent components, and user interfaces (Figure 1). The system operates as a set of containerized microservices orchestrated via Docker Compose.

```
┌─────────────────────────────────────────────────────────────────────┐
│                         User Interfaces                              │
│  ┌─────────────┐  ┌─────────────────┐  ┌─────────────────────────┐  │
│  │    CLI      │  │  Discord Bot    │  │      REST API           │  │
│  │  (Typer)    │  │  (discord.py)   │  │     (FastAPI)           │  │
│  └──────┬──────┘  └────────┬────────┘  └───────────┬─────────────┘  │
└─────────┼──────────────────┼───────────────────────┼────────────────┘
          │                  │                       │
          └──────────────────┴───────────────────────┘
                             │
┌────────────────────────────┼────────────────────────────────────────┐
│                      Agent System                                    │
│  ┌─────────────────────────┴─────────────────────────────────────┐  │
│  │                      Core Agent                                │  │
│  │   ┌─────────────┐    ┌─────────────┐    ┌─────────────────┐   │  │
│  │   │   Planner   │───▶│  Executors  │───▶│  Tool Registry  │   │  │
│  │   │  (Qwen3)    │    │ (DeepSeek)  │    │   (27 tools)    │   │  │
│  │   └─────────────┘    └─────────────┘    └─────────────────┘   │  │
│  │                                                                │  │
│  │   ┌─────────────────────────────────────────────────────────┐ │  │
│  │   │                    Memory Systems                        │ │  │
│  │   │  Working (Redis)  │  Episodic (PG)  │  Decision (PG)   │ │  │
│  │   └─────────────────────────────────────────────────────────┘ │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                                                                      │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │                    Trigger System                              │  │
│  │  Scheduled (APScheduler)  │  Event-Driven (Redis Pub/Sub)     │  │
│  └───────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
                             │
┌────────────────────────────┼────────────────────────────────────────┐
│                   Integration Services                               │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐  │
│  │  Gmail   │ │ Calendar │ │  Drive   │ │  GitHub  │ │   LLM    │  │
│  │ Service  │ │ Service  │ │ Service  │ │ Service  │ │ Service  │  │
│  └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘  │
└───────┼────────────┼────────────┼────────────┼────────────┼─────────┘
        │            │            │            │            │
┌───────┼────────────┼────────────┼────────────┼────────────┼─────────┐
│       │       External APIs     │            │            │         │
│  ┌────┴────┐ ┌────┴────┐ ┌─────┴────┐ ┌────┴────┐ ┌─────┴────┐    │
│  │ Gmail   │ │ Google  │ │  Google  │ │ GitHub  │ │Together  │    │
│  │  API    │ │Calendar │ │   Drive  │ │   API   │ │   .ai    │    │
│  └─────────┘ └─────────┘ └──────────┘ └─────────┘ └──────────┘    │
└─────────────────────────────────────────────────────────────────────┘
                             │
┌────────────────────────────┼────────────────────────────────────────┐
│                      Data Services                                   │
│  ┌──────────────────┐ ┌─────────────────┐ ┌───────────────────────┐ │
│  │    PostgreSQL    │ │      Neo4j      │ │        Redis          │ │
│  │   + pgvector     │ │  Property Graph │ │   Cache + Pub/Sub     │ │
│  │                  │ │                 │ │                       │ │
│  │ • Embeddings     │ │ • Person        │ │ • Working Memory      │ │
│  │ • Tasks          │ │ • Email         │ │ • Approvals           │ │
│  │ • Goals          │ │ • Event         │ │ • Notifications       │ │
│  │ • Decisions      │ │ • Task          │ │ • Session Context     │ │
│  │ • Documents      │ │ • Document      │ │                       │ │
│  │ • Code Content   │ │ • Chunk         │ │                       │ │
│  │                  │ │ • Topic/Concept │ │                       │ │
│  └──────────────────┘ └─────────────────┘ └───────────────────────┘ │
└─────────────────────────────────────────────────────────────────────┘

Figure 1: Cognitex System Architecture
```

### 3.2 Data Services

#### 3.2.1 PostgreSQL with pgvector

PostgreSQL serves as the primary relational store, extended with the pgvector extension for efficient similarity search over dense embeddings. The schema comprises 15 tables organized into functional groups:

**Task Management:**
- `tasks`: Core task storage with status (pending, in_progress, done), energy cost estimates, due dates, priority levels, and foreign keys to source entities (emails, projects)
- `goals`: Hierarchical goal management supporting quarterly and yearly timeframes with OKR-style key results stored as JSONB
- `projects`: Project containers linking goals to executable tasks

**Semantic Search:**
- `embeddings`: Universal embedding storage with entity_type discriminator, 768-dimensional vectors, and content hashes for deduplication
- `document_content`: Full-text storage for Drive documents enabling keyword search alongside semantic retrieval
- `document_chunks`: Chunked document segments with positional metadata (start_char, end_char, chunk_index)
- `code_content`: GitHub source code with repository and path metadata

**Learning Infrastructure:**
- `decision_traces`: Complete decision context including trigger type, proposed action, final action (post-edit), user feedback, and quality scores
- `communication_patterns`: Learned per-contact preferences (tone, greeting style, response urgency)
- `preference_rules`: Extracted behavioral patterns with conditions and confidence scores

**Synchronization:**
- `sync_state`: Incremental sync tokens for Gmail (history_id) and Calendar (sync_token)
- `audit_log`: Change tracking for debugging and compliance

The embedding index employs HNSW (Hierarchical Navigable Small World) for approximate nearest neighbor search:

```sql
CREATE INDEX idx_embeddings_vector
ON embeddings USING hnsw (embedding vector_cosine_ops)
WITH (m = 16, ef_construction = 64);
```

This configuration provides O(log n) query complexity with high recall at the cost of O(n log n) index construction time.

#### 3.2.2 Neo4j Property Graph

Neo4j stores the semantic knowledge graph with 10 primary node types and 15 relationship types. The graph captures:

**Entity Nodes:**
- `Person`: Contacts with email (unique), name, organization, role, communication style
- `Email`: Messages with gmail_id (unique), thread_id, subject, date, classification, urgency
- `Event`: Calendar entries with gcal_id (unique), title, start/end times, energy_impact
- `Task`: Action items with status, energy_cost, priority, due_date
- `Document`: Drive files with drive_id (unique), name, mime_type, folder_path
- `Project`, `Goal`: Organizational hierarchy
- `Repository`, `CodeFile`: GitHub entities
- `Chunk`, `Topic`, `Concept`: Semantic analysis outputs

**Relationship Types:**
- Communication: `SENT_BY`, `RECEIVED_BY`, `ATTENDED_BY`, `ORGANIZED_BY`
- Ownership: `OWNS`, `SHARED_WITH`, `REQUESTED_BY`
- Derivation: `DERIVED_FROM`, `LINKED_TO`, `BLOCKED_BY`
- Inference: `WORKS_WITH` (computed from email interactions)
- Semantic: `HAS_CHUNK`, `DISCUSSES`, `REFERENCES`, `MENTIONS`

Constraints ensure data integrity:

```cypher
CREATE CONSTRAINT person_email_unique
FOR (p:Person) REQUIRE p.email IS UNIQUE;

CREATE CONSTRAINT email_gmail_id_unique
FOR (e:Email) REQUIRE e.gmail_id IS UNIQUE;
```

#### 3.2.3 Redis

Redis provides three functions:

1. **Working Memory**: Session context with 24-hour TTL, storing recent interactions (last 50 messages), current focus topics, and session metadata
2. **Approval Queue**: Staged high-risk actions awaiting user confirmation, with pending set for efficient listing
3. **Pub/Sub Messaging**: Event channels for email notifications, calendar changes, and cross-service communication

### 3.3 Integration Services

#### 3.3.1 Gmail Service

The Gmail integration implements OAuth2 authentication with incremental synchronization:

```python
class GmailService:
    def list_messages(query, max_results, page_token, label_ids)
    def get_message(message_id, format)  # full, metadata, raw
    def send_message(to, subject, body, reply_to)
    def extract_email_metadata(message)  # headers, body, attachments
```

Initial synchronization fetches historical messages within a configurable date range. Subsequent syncs use Gmail's history API with stored history_id to retrieve only new messages, achieving efficient incremental updates.

#### 3.3.2 Calendar Service

Google Calendar integration supports full CRUD operations:

```python
class CalendarService:
    def get_events(start, end, time_zone)
    def create_event(title, start, end, attendees, description)
    def update_event(event_id, **kwargs)
    def delete_event(event_id)
```

Events are enriched with computed properties including energy_impact (1-10 scale based on event type, duration, and attendee count) and event_type classification (standup, presentation, discussion, focus_time).

#### 3.3.3 Drive Service

Google Drive integration indexes document metadata and content:

```python
class DriveService:
    def list_files_in_folder(folder_id, recursive)
    def get_file_metadata(file_id)
    def get_file_content(file_id, mime_type)  # text extraction
    def list_all_files()  # paginated traversal
```

Content extraction supports Google Docs (export as plain text), PDFs (via pypdf), Microsoft Office documents (via python-docx, openpyxl), and plain text formats.

#### 3.3.4 GitHub Service

GitHub integration enables code repository indexing:

```python
class GitHubService:
    def list_user_repos()
    def get_repo_tree(owner, repo, ref)
    def get_file_content(owner, repo, path, ref)
```

Selective indexing targets meaningful files (source code, configuration, documentation) while excluding generated artifacts (node_modules, .git, build outputs).

#### 3.3.5 LLM Service

The LLM service provides a unified interface to Together.ai's inference API:

```python
class LLMService:
    async def generate(prompt, model, temperature, max_tokens)
    async def generate_embedding(text)  # 768-dim vector
    async def classify_email(email_data)
    async def extract_entities_from_chunk(content, document_name)
```

Model selection is task-specific:
- **Planning**: DeepSeek V3 (671B parameters, MoE) for complex reasoning
- **Execution**: DeepSeek V3 for structured task completion
- **Embedding**: BAAI/bge-base-en-v1.5 for 768-dimensional semantic vectors

---

## 4. Agent Subsystem

### 4.1 Architecture Overview

The agent subsystem implements a ReAct-style (Yao et al., 2022) architecture with hierarchical control flow. The design separates high-level reasoning (what to do and why) from low-level execution (how to accomplish specific tasks), enabling specialized optimization of each tier.

### 4.2 Core Agent

The core agent orchestrates the reasoning loop, managing interactions between the planner, executors, tools, and memory systems:

```python
class Agent:
    async def chat(message: str) -> str
    async def chat_with_approvals(message: str) -> tuple[str, list[str]]
    async def morning_briefing() -> str
    async def evening_review() -> str
    async def handle_approval(approval_id, approved, feedback) -> str
```

The primary interaction loop (`chat_with_approvals`) proceeds as follows:

1. Record user message to working memory
2. Construct system prompt with available tools
3. Retrieve relevant historical decisions via RAG
4. Execute ReAct iterations (maximum 8):
   - Generate LLM response with thought/action/observation structure
   - If action specified, execute tool and capture observation
   - If response specified, return to user
5. Record final response to episodic memory
6. Return response and any approval IDs created

### 4.3 Planner

The planner generates structured action plans from triggers:

```python
@dataclass
class Plan:
    reasoning: str           # Analysis of situation
    steps: list[PlanStep]    # Ordered actions
    user_notification: str   # Optional message
    follow_up: str          # Scheduled check-in
    confidence: float       # Quality score (0-1)

@dataclass
class PlanStep:
    tool: str               # Tool name
    params: dict            # Tool parameters
    reasoning: str          # Why this step
    risk_level: ToolRisk    # READONLY, AUTO, APPROVAL
```

The planner operates in distinct modes with mode-specific tool access:

| Mode | Description | Tool Access |
|------|-------------|-------------|
| BRIEFING | Morning summary | READONLY, AUTO |
| REVIEW | Evening reflection | READONLY, AUTO |
| MONITOR | Hourly check | READONLY, AUTO |
| PROCESS_EMAIL | New email analysis | READONLY, AUTO, APPROVAL |
| PROCESS_EVENT | Calendar change | READONLY, AUTO |
| CONVERSATION | User interaction | READONLY, AUTO, APPROVAL |
| ESCALATE | Urgent notification | READONLY, AUTO, APPROVAL |

### 4.4 Executors

Specialized executors handle domain-specific tool execution:

```python
class EmailExecutor:
    async def draft_email(to, subject, body, context)
    # Learns communication patterns (tone, greeting, length)

class TaskExecutor:
    async def create_task(title, description, context)
    async def update_task(task_id, updates)

class CalendarExecutor:
    async def create_event(title, start, end, attendees)
    # Validates scheduling constraints

class NotifyExecutor:
    async def send_notification(message, urgency)
    # Formats Discord messages
```

Executors encapsulate domain knowledge, enabling the planner to reason at a higher abstraction level while delegating implementation details.

### 4.5 Tool Registry

The system provides 27 tools organized by risk level:

**READONLY Tools (10)** - No side effects, always allowed:
- `graph_query`: Execute Cypher queries on Neo4j
- `search_documents`: Semantic search over document embeddings
- `search_code`: Semantic search over code embeddings
- `read_document`: Retrieve full document content
- `read_code_file`: Retrieve source code content
- `get_calendar`: Query calendar events
- `get_tasks`: List tasks with filters
- `find_task`: Search tasks by title
- `get_contact`: Retrieve person profile
- `recall_memory`: Query episodic memory

**AUTO Tools (14)** - Low-risk side effects, executed automatically:
- `create_task`, `update_task`, `complete_task`
- `create_project`, `update_project`, `link_project_to_person`
- `create_goal`, `update_goal`, `parse_goal`
- `link_task`: Connect task to goal/project/document
- `send_notification`: Discord message
- `add_memory`: Store to episodic memory
- `get_projects`, `get_goals`, `get_project`, `get_goal`

**APPROVAL Tools (3)** - High-risk, require user confirmation:
- `draft_email`: Stage email for review
- `send_email`: Execute after approval
- `create_event`: Stage calendar event for review

The risk stratification enables safe autonomous operation while preserving human agency for consequential actions.

### 4.6 Memory Systems

#### 4.6.1 Working Memory

Redis-backed short-term storage with 24-hour TTL:

```python
class WorkingMemory:
    async def get_context() -> dict
    async def add_interaction(role, content)
    async def stage_approval(approval_id, action_type, params, reasoning)
    async def resolve_approval(approval_id, approved, feedback)
```

Structure:
```json
{
  "interactions": [{"role": "user|assistant", "content": "...", "timestamp": "..."}],
  "session_start": "2024-01-15T09:00:00Z",
  "focus": "email triage",
  "updated_at": "2024-01-15T09:45:00Z"
}
```

#### 4.6.2 Episodic Memory

PostgreSQL-backed long-term storage:

```python
class EpisodicMemory:
    async def add(content, memory_type, importance, entities)
    async def search(query, memory_type, limit) -> list[Memory]
    async def get_recent(hours, memory_type, limit) -> list[Memory]
```

Memory types include `interaction` (conversations), `decision` (agent actions), and `observation` (external events).

#### 4.6.3 Decision Memory

Specialized storage for learning from feedback:

```python
class DecisionMemory:
    async def record_decision(trigger, context, proposed_action, reasoning)
    async def record_feedback(decision_id, status, feedback, edits)
    async def find_similar_decisions(query, min_quality, limit)
    async def extract_patterns(decision_ids) -> list[PreferenceRule]
```

Decision traces capture:
- **Trigger**: What prompted the decision (email, user request, scheduled)
- **Context**: Full situation snapshot (JSONB)
- **Proposed Action**: What the agent intended to do
- **Final Action**: What was actually executed (after edits)
- **Feedback**: User's explicit feedback (if any)
- **Quality Score**: Computed from approval status

Quality scoring:
- Auto-executed: 0.7 (no user signal)
- Approved unchanged: 0.9 (user satisfied)
- Approved with minor edits: 0.85 (mostly correct)
- Rejected: 0.3 (incorrect)

---

## 5. Semantic Graph Construction

### 5.1 Entity Extraction Pipeline

The ingestion pipeline transforms raw data from external sources into graph entities:

```
Gmail Message → Email Node + Person Nodes + Relationships
Calendar Event → Event Node + Person Nodes + Relationships
Drive File → Document Node + Chunk Nodes + Embeddings
GitHub Repo → Repository Node + CodeFile Nodes + Embeddings
```

#### 5.1.1 Email Processing

```python
async def ingest_email_to_graph(email_data: dict):
    # 1. Create sender Person node
    await create_person(sender_email, sender_name)

    # 2. Create recipient Person nodes
    for recipient in to + cc:
        await create_person(recipient_email, recipient_name)

    # 3. Create Email node with classification
    classification = await llm.classify_email(email_data)
    await create_email(gmail_id, subject, date, classification)

    # 4. Create relationships
    await link_email_sender(gmail_id, sender_email)
    for recipient in recipients:
        await link_email_recipient(gmail_id, recipient_email)

    # 5. Infer WORKS_WITH relationships
    await infer_works_with(sender_email, recipient_emails)
```

#### 5.1.2 Email Classification

The LLM classifies emails into four categories:
- **actionable**: Requires user response or action
- **delegable**: Could be handled by someone else
- **fyi**: Informational only
- **spam**: Unwanted/promotional

Additional properties extracted:
- `urgency`: low, medium, high
- `action_required`: boolean
- `suggested_action`: brief description

#### 5.1.3 Task Inference

For actionable emails, the system optionally infers tasks:

```python
async def infer_tasks_from_email(email_data: dict):
    if not email_data.get('action_required'):
        return

    # LLM extracts implicit tasks
    tasks = await llm.extract_tasks(email_data)

    for task in tasks:
        task_id = await create_task(
            title=task['title'],
            source_type='email',
            source_id=email_data['gmail_id']
        )
        await link_task_derived_from_email(task_id, email_data['gmail_id'])
```

### 5.2 Relationship Inference

Beyond explicit relationships, the system infers latent connections:

#### 5.2.1 WORKS_WITH Inference

```cypher
MATCH (e:Email)-[:SENT_BY]->(sender:Person)
MATCH (e)-[:RECEIVED_BY]->(recipient:Person)
WHERE sender <> recipient
MERGE (sender)-[r:WORKS_WITH]->(recipient)
ON CREATE SET
    r.interaction_count = 1,
    r.first_interaction = e.date
ON MATCH SET
    r.interaction_count = r.interaction_count + 1,
    r.last_interaction = e.date
```

#### 5.2.2 Entity Enrichment

Person nodes are enriched from email metadata:

```python
async def enrich_contact(email: str):
    # Extract organization from email domain
    domain = email.split('@')[1]
    org = await lookup_organization(domain)

    # Infer role from email patterns
    patterns = await get_communication_patterns(email)
    role = infer_role(patterns)

    # Update Person node
    await update_person(email, org=org, role=role)
```

### 5.3 Semantic Chunk Analysis

Document chunks are analyzed for semantic content:

```python
async def analyze_chunk_for_graph(chunk_id: str, document_name: str):
    # 1. Get chunk content
    content = await get_chunk_content(chunk_id)

    # 2. LLM extraction
    entities = await llm.extract_entities_from_chunk(content, document_name)
    # Returns: {topics: [], concepts: [], people: [], summary: str, key_facts: []}

    # 3. Create Chunk node
    await create_chunk(chunk_id, summary=entities['summary'])

    # 4. Create Topic/Concept nodes and relationships
    for topic in entities['topics']:
        await create_topic(topic)
        await link_chunk_to_topic(chunk_id, topic)

    for concept in entities['concepts']:
        await create_concept(concept)
        await link_chunk_to_concept(chunk_id, concept)

    # 5. Link to mentioned people
    for person in entities['people']:
        await link_chunk_to_person(chunk_id, person)
```

### 5.4 Graph Query Patterns

The agent employs various query patterns:

**Contact Network:**
```cypher
MATCH (p:Person)<-[:SENT_BY]-(e:Email)
WITH p, count(e) as emails_sent
ORDER BY emails_sent DESC
LIMIT 20
RETURN p.email, p.name, emails_sent
```

**Actionable Items:**
```cypher
MATCH (e:Email)
WHERE e.action_required = true AND e.processed = false
OPTIONAL MATCH (e)-[:SENT_BY]->(sender:Person)
RETURN e.subject, sender.email, e.urgency
ORDER BY e.urgency DESC, e.date DESC
```

**Topic Exploration:**
```cypher
MATCH (t:Topic {name: $topic_name})<-[:DISCUSSES]-(c:Chunk)
MATCH (c)<-[:HAS_CHUNK]-(d:Document)
RETURN DISTINCT d.name, count(c) as relevance
ORDER BY relevance DESC
```

**Energy Forecast:**
```cypher
MATCH (ev:Event)
WHERE date(ev.start) = date()
RETURN
    count(ev) as event_count,
    sum(ev.energy_impact) as total_energy,
    avg(ev.duration_minutes) as avg_duration
```

---

## 6. Document Chunking and Embedding Pipeline

### 6.1 Chunking Strategy

Documents are split into overlapping chunks optimized for embedding model context windows:

```python
CHUNK_SIZE = 1200       # characters (~300 tokens)
CHUNK_OVERLAP = 200     # characters (~50 tokens)
MIN_CHUNK_SIZE = 100    # minimum viable chunk
```

The chunking algorithm preserves semantic boundaries:

```python
def chunk_document(content: str) -> list[DocumentChunk]:
    # 1. Split on paragraph boundaries
    paragraphs = content.split('\n\n')

    chunks = []
    current_chunk = []
    current_length = 0

    for para in paragraphs:
        if current_length + len(para) > CHUNK_SIZE:
            if current_length >= MIN_CHUNK_SIZE:
                # Finalize current chunk
                chunks.append(create_chunk(current_chunk))

                # Start new chunk with overlap
                overlap_text = get_overlap(current_chunk, CHUNK_OVERLAP)
                current_chunk = [overlap_text, para]
                current_length = len(overlap_text) + len(para)
            else:
                # Chunk too small, continue accumulating
                current_chunk.append(para)
                current_length += len(para)
        else:
            current_chunk.append(para)
            current_length += len(para)

    # Handle final chunk
    if current_chunk:
        chunks.append(create_chunk(current_chunk))

    return chunks
```

### 6.2 Embedding Generation

Chunks are embedded using BAAI/bge-base-en-v1.5:

```python
async def embed_chunks(chunks: list[DocumentChunk], drive_id: str):
    for chunk in chunks:
        # Generate embedding
        embedding = await llm.generate_embedding(chunk.content)

        # Store with deduplication
        await store_embedding(
            entity_type='chunk',
            entity_id=f"{drive_id}:{chunk.chunk_index}",
            content_hash=chunk.content_hash,
            embedding=embedding  # 768 dimensions
        )
```

### 6.3 Semantic Search

Search combines vector similarity with metadata filtering:

```python
async def search_documents(query: str, limit: int = 10) -> list[dict]:
    # 1. Embed query
    query_embedding = await llm.generate_embedding(query)

    # 2. Vector similarity search
    results = await pg_session.execute("""
        SELECT
            e.entity_id,
            dc.content,
            d.name as document_name,
            1 - (e.embedding <=> $1::vector) as similarity
        FROM embeddings e
        JOIN document_chunks dc ON dc.drive_id || ':' || dc.chunk_index = e.entity_id
        JOIN documents d ON d.drive_id = dc.drive_id
        WHERE e.entity_type = 'chunk'
        ORDER BY e.embedding <=> $1::vector
        LIMIT $2
    """, [query_embedding, limit])

    return [format_result(r) for r in results]
```

### 6.4 Auto-Indexing Pipeline

When Drive files change, the system automatically re-indexes:

```python
async def auto_index_drive_file(file_id: str, file_name: str, mime_type: str):
    # 1. Extract content
    content = drive.get_file_content(file_id, mime_type)

    # 2. Delete old chunks (for updates)
    await delete_chunks(file_id)
    await delete_embeddings(file_id)

    # 3. Chunk and embed
    chunks = chunk_document(content)
    await embed_chunks(chunks, file_id)

    # 4. Analyze for graph integration
    for chunk in chunks:
        await analyze_chunk_for_graph(
            chunk_id=f"{file_id}:{chunk.chunk_index}",
            document_name=file_name
        )
```

---

## 7. Trigger and Notification System

### 7.1 Trigger Types

The system supports three trigger categories:

#### 7.1.1 Scheduled Triggers

APScheduler provides cron-based scheduling:

```python
class TriggerSystem:
    def _setup_scheduled_triggers(self):
        # Morning briefing at 8:00 AM
        self.scheduler.add_job(
            self._morning_briefing,
            CronTrigger(hour=8, minute=0),
            id="morning_briefing"
        )

        # Evening review at 6:00 PM
        self.scheduler.add_job(
            self._evening_review,
            CronTrigger(hour=18, minute=0),
            id="evening_review"
        )

        # Hourly monitoring (9 AM - 6 PM)
        self.scheduler.add_job(
            self._hourly_check,
            CronTrigger(hour="9-18", minute=0),
            id="hourly_check"
        )
```

#### 7.1.2 Event-Driven Triggers

Redis pub/sub enables reactive processing:

```python
async def _start_event_listeners(self):
    pubsub = redis.pubsub()
    await pubsub.subscribe(
        "cognitex:events:email",
        "cognitex:events:calendar",
        "cognitex:events:task",
        "cognitex:events:drive"
    )

    async for message in pubsub.listen():
        if message["type"] == "message":
            await self._handle_event(message["channel"], message["data"])
```

Event handlers process specific triggers:

```python
async def _on_new_email(self, email_data: dict):
    # Sync new emails
    await run_incremental_sync(email_data['history_id'])

    # Agent analysis
    response = await self.agent.chat(
        f"New email received from {email_data['sender']}. "
        "Analyze for urgency and required actions."
    )

    # Notify if important
    if is_actionable(response):
        await self._send_notification(response)

async def _on_drive_change(self, event_data: dict):
    # Auto-index changed files
    for file in event_data['changed_files']:
        await auto_index_drive_file(
            file['id'], file['name'], file['mime_type']
        )

    # Agent notification
    await self.agent.chat(
        f"Files updated: {[f['name'] for f in event_data['changed_files']]}"
    )
```

#### 7.1.3 User-Initiated Triggers

Direct interaction via CLI, Discord, or API:

```python
# CLI
$ cognitex agent-chat "What should I focus on today?"

# Discord
@cognitex What are my urgent tasks?

# API
POST /api/chat {"message": "Summarize my pending items"}
```

### 7.2 Approval Workflow

High-risk actions follow a staged approval process:

```
┌─────────────────┐
│  Agent decides  │
│  to send email  │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ stage_approval()│
│ to Redis        │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Publish to      │
│ notifications   │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Discord Bot     │
│ displays embed  │
│ [Approve][Edit] │
│ [Skip]          │
└────────┬────────┘
         │
    ┌────┴────┐
    │         │
    ▼         ▼
┌───────┐ ┌───────┐
│Approve│ │Reject │
└───┬───┘ └───┬───┘
    │         │
    ▼         ▼
┌───────┐ ┌───────┐
│Execute│ │ Store │
│Action │ │Feedback│
└───────┘ └───────┘
```

Approval storage:

```json
{
  "id": "apr_abc123",
  "action_type": "send_email",
  "params": {
    "to": "colleague@example.com",
    "subject": "Re: Project Update",
    "body": "..."
  },
  "reasoning": "User asked to reply confirming availability",
  "status": "pending",
  "created_at": "2024-01-15T10:30:00Z",
  "expires_at": "2024-01-16T10:30:00Z"
}
```

### 7.3 Notification Delivery

Notifications are delivered via Discord with rich formatting:

```python
async def send_notification(message: str, urgency: str = "normal"):
    embed = discord.Embed(
        title="Cognitex Notification",
        description=message,
        color=URGENCY_COLORS[urgency]
    )

    if urgency == "high":
        embed.set_footer(text="⚠️ Requires immediate attention")

    await channel.send(embed=embed)
```

Rate limiting prevents notification fatigue:

```python
MAX_NOTIFICATIONS_PER_HOUR = 3  # configurable
```

---

## 8. Evaluation

### 8.1 System Metrics

The current deployment demonstrates the following characteristics:

**Data Scale:**
- 153,108 documents indexed from Google Drive
- 3,372 document chunks with embeddings
- 50 documents with deep semantic analysis
- ~8,000 lines of application code

**Performance:**
- Embedding generation: ~0.5s per chunk (Together.ai API)
- Semantic search: <100ms for top-10 results (HNSW index)
- Graph queries: <50ms for typical traversals
- Agent response: 3-15s depending on tool usage

**Memory Footprint:**
- PostgreSQL: ~500MB (embeddings dominate)
- Neo4j: ~100MB (graph structure)
- Redis: ~10MB (working memory)

### 8.2 Qualitative Assessment

The system successfully demonstrates:

1. **Context Persistence**: The agent maintains awareness of email threads, contacts, and commitments across sessions
2. **Relationship Inference**: WORKS_WITH relationships accurately reflect collaboration patterns
3. **Risk Stratification**: Approval workflow prevents unintended actions while allowing autonomous task creation
4. **Semantic Search**: Document retrieval surfaces relevant content across diverse file types

### 8.3 Limitations

Current limitations include:

1. **Learning Pipeline**: Decision traces are captured but not yet used for fine-tuning
2. **Push Notifications**: Gmail and Calendar use polling rather than push
3. **Test Coverage**: Limited automated testing
4. **Multi-User**: Designed for single-user deployment

---

## 9. Discussion and Future Work

### 9.1 Design Decisions

Several architectural decisions warrant discussion:

**Hybrid Storage**: The combination of Neo4j, PostgreSQL, and Redis adds operational complexity but enables optimal data models for each use case. Graph traversal, vector similarity, and low-latency caching each have distinct requirements poorly served by a single database.

**Risk Stratification**: The three-tier risk model (READONLY, AUTO, APPROVAL) represents a pragmatic balance between autonomy and control. Future work may explore dynamic risk assessment based on context and learned user preferences.

**LLM Selection**: Using Together.ai provides cost-effective access to capable models. The hierarchical architecture (separate planner and executor models) enables future experimentation with model specialization.

### 9.2 Future Directions

Several extensions are planned:

1. **Behavioral Fine-Tuning**: Using captured decision traces to fine-tune models on user preferences
2. **Energy Management**: Implementing the energy tracking schema for cognitive load awareness
3. **Push Notifications**: Google Cloud Pub/Sub for real-time Gmail and Calendar updates
4. **Multi-Modal Input**: Processing images and attachments within emails
5. **Collaborative Features**: Shared graphs for team environments
6. **Privacy Controls**: Granular permissions for data access and retention

### 9.3 Ethical Considerations

Personal agent systems raise important ethical questions:

- **Data Privacy**: The system has broad access to personal communications; responsible deployment requires careful data handling
- **Autonomy vs Control**: The approval mechanism preserves human agency but may create friction; calibration is ongoing
- **Transparency**: Decision traces provide auditability but may not be accessible to average users
- **Dependency**: Over-reliance on agent recommendations could reduce cognitive engagement

---

## 10. Conclusion

Cognitex demonstrates a viable architecture for personal AI agents that balance autonomous capability with human oversight. The hybrid knowledge representation—combining property graphs, vector embeddings, and session caching—provides a robust foundation for personal information management. The risk-stratified tool system enables meaningful automation while preserving user agency for consequential actions.

Key contributions include the decision trace framework for learning from user feedback, the semantic graph construction pipeline for automatic relationship inference, and the multi-modal trigger system supporting both proactive and reactive agent behavior.

While significant work remains—particularly in leveraging captured feedback for behavioral improvement—the current implementation provides a functional reference architecture for trustworthy personal AI systems. We release the complete codebase to support further research in this domain.

---

## Acknowledgments

This work was developed independently with assistance from Claude (Anthropic) for code generation and architectural refinement.

---

## References

Dong, X., Gabrilovich, E., Heitz, G., Horn, W., Lao, N., Murphy, K., ... & Zhang, W. (2014). Knowledge vault: A web-scale approach to probabilistic knowledge fusion. In *Proceedings of the 20th ACM SIGKDD International Conference on Knowledge Discovery and Data Mining* (pp. 601-610).

Karpukhin, V., Oguz, B., Min, S., Lewis, P., Wu, L., Edunov, S., ... & Yih, W. T. (2020). Dense passage retrieval for open-domain question answering. In *Proceedings of the 2020 Conference on Empirical Methods in Natural Language Processing* (pp. 6769-6781).

McKinsey Global Institute. (2012). The social economy: Unlocking value and productivity through social technologies. *McKinsey & Company*.

Nickel, M., Murphy, K., Tresp, V., & Gabrilovich, E. (2016). A review of relational machine learning for knowledge graphs. *Proceedings of the IEEE*, 104(1), 11-33.

Park, J. S., O'Brien, J. C., Cai, C. J., Morris, M. R., Liang, P., & Bernstein, M. S. (2023). Generative agents: Interactive simulacra of human behavior. In *Proceedings of the 36th Annual ACM Symposium on User Interface Software and Technology* (pp. 1-22).

Qin, Y., Liang, S., Ye, Y., Zhu, K., Yan, L., Lu, Y., ... & Sun, M. (2023). Toolllm: Facilitating large language models to master 16000+ real-world apis. *arXiv preprint arXiv:2307.16789*.

Sauermann, L., Bernardi, A., & Dengel, A. (2005). Overview and outlook on the semantic desktop. In *Proceedings of the ISWC 2005 Workshop on the Semantic Desktop*.

Shinn, N., Cassano, F., Gopinath, A., Narasimhan, K., & Yao, S. (2023). Reflexion: Language agents with verbal reinforcement learning. *Advances in Neural Information Processing Systems*, 36.

Yao, S., Zhao, J., Yu, D., Du, N., Shafran, I., Narasimhan, K., & Cao, Y. (2022). React: Synergizing reasoning and acting in language models. *arXiv preprint arXiv:2210.03629*.

---

## Appendix A: Configuration Reference

```ini
# Database URLs
DATABASE_URL=postgresql://cognitex:password@localhost:5432/cognitex
NEO4J_URI=bolt://localhost:7687
NEO4J_PASSWORD=<secret>
REDIS_URL=redis://localhost:6379/0

# Together.ai LLM
TOGETHER_API_KEY=<secret>
TOGETHER_MODEL_PLANNER=deepseek-ai/DeepSeek-V3
TOGETHER_MODEL_EXECUTOR=deepseek-ai/DeepSeek-V3
TOGETHER_MODEL_EMBEDDING=BAAI/bge-base-en-v1.5

# Google APIs
GOOGLE_CLIENT_ID=<client_id>
GOOGLE_CLIENT_SECRET=<secret>
GOOGLE_CREDENTIALS_PATH=data/google_credentials.json

# Discord
DISCORD_BOT_TOKEN=<secret>
DISCORD_CHANNEL_ID=<channel_id>

# GitHub
GITHUB_TOKEN=<secret>

# Application
ENVIRONMENT=development
LOG_LEVEL=INFO
MAX_NOTIFICATIONS_PER_HOUR=3
DEFAULT_ENERGY_LEVEL=7
```

---

## Appendix B: CLI Command Reference

```bash
# System
cognitex status              # Configuration and memory stats
cognitex auth                # OAuth2 authentication flow
cognitex graph               # Neo4j statistics

# Data Synchronization
cognitex sync                # Incremental Gmail sync
cognitex sync --full         # Full historical sync
cognitex calendar            # Google Calendar sync
cognitex drive-sync          # Drive metadata sync
cognitex deep-index          # Document chunking and embedding

# Queries
cognitex tasks               # List tasks
cognitex tasks --status pending
cognitex contacts            # List people
cognitex documents           # List documents
cognitex doc-search "query"  # Semantic search
cognitex chunk-search "query" # Chunk-level search

# Agent
cognitex agent-chat          # Interactive mode
cognitex agent-chat "Query"  # Single message
cognitex briefing            # Morning briefing
cognitex approvals           # Pending approvals
cognitex approvals approve <id>
cognitex approvals reject <id> --feedback "reason"

# Semantic Analysis
cognitex analyze-chunks      # LLM analysis of chunks
cognitex topics              # List extracted topics
cognitex concepts            # List extracted concepts
cognitex semantic-stats      # Analysis progress
```

---

## Appendix C: Neo4j Schema

```cypher
// Constraints
CREATE CONSTRAINT person_email_unique FOR (p:Person) REQUIRE p.email IS UNIQUE;
CREATE CONSTRAINT email_gmail_id_unique FOR (e:Email) REQUIRE e.gmail_id IS UNIQUE;
CREATE CONSTRAINT event_gcal_id_unique FOR (ev:Event) REQUIRE ev.gcal_id IS UNIQUE;
CREATE CONSTRAINT task_id_unique FOR (t:Task) REQUIRE t.id IS UNIQUE;
CREATE CONSTRAINT document_drive_id_unique FOR (d:Document) REQUIRE d.drive_id IS UNIQUE;
CREATE CONSTRAINT project_id_unique FOR (p:Project) REQUIRE p.id IS UNIQUE;
CREATE CONSTRAINT goal_id_unique FOR (g:Goal) REQUIRE g.id IS UNIQUE;
CREATE CONSTRAINT repository_id_unique FOR (r:Repository) REQUIRE r.id IS UNIQUE;
CREATE CONSTRAINT chunk_id_unique FOR (c:Chunk) REQUIRE c.id IS UNIQUE;
CREATE CONSTRAINT topic_name_unique FOR (t:Topic) REQUIRE t.name IS UNIQUE;
CREATE CONSTRAINT concept_name_unique FOR (c:Concept) REQUIRE c.name IS UNIQUE;

// Indexes
CREATE INDEX person_name FOR (p:Person) ON (p.name);
CREATE INDEX email_date FOR (e:Email) ON (e.date);
CREATE INDEX email_classification FOR (e:Email) ON (e.classification);
CREATE INDEX event_start FOR (ev:Event) ON (ev.start);
CREATE INDEX task_status FOR (t:Task) ON (t.status);
CREATE INDEX task_priority FOR (t:Task) ON (t.priority);
CREATE INDEX document_indexed FOR (d:Document) ON (d.indexed);
CREATE INDEX chunk_analyzed FOR (c:Chunk) ON (c.analyzed);
```

---

*Preprint submitted to arXiv, December 2024*
