# Proactive Agent: Learning & Intelligence Enhancements

Make Cognitex truly proactive by learning from user behavior, understanding writing style, and providing comprehensive context for decisions.

---

## Overview

Four interconnected enhancement tracks:

| Track | Goal | Key Outcome |
|-------|------|-------------|
| **A. Email Style Learning** | Learn how user writes | Drafts match user's voice |
| **B. Response Pattern Learning** | Learn which emails need responses | Stop drafting for emails that don't need replies |
| **C. External Research** | Web search for context | "What do I need to know" answered |
| **D. Ambient Context** | Track relationship history | "What happened since last meeting with X" |

---

## Track A: Email Writing Style Learning

### Problem
- Drafts are "bland" and generic
- No personalization to recipient
- Agent doesn't learn from user's actual sent emails
- Draft edits aren't analyzed

### Solution Architecture

```
Sent Email → StyleAnalyzer.extract() → StyleMetrics
                                            ↓
                                    EmailStyleProfile (per-recipient)
                                            ↓
                            ExpertiseSystem.update("email_writing")
                                            ↓
                            Inject style guidance into draft prompts
```

### Implementation

#### A.1 EmailStyleAnalyzer Service

**File:** `src/cognitex/services/email_style.py` (NEW)

```python
@dataclass
class StyleMetrics:
    # Structure
    avg_length: int                    # Average email length
    greeting_style: str                # "Hi NAME", "Dear NAME", "NAME,", none
    closing_style: str                 # "Best", "Thanks", "Cheers", none
    signature_present: bool

    # Tone
    formality: float                   # 0 (casual) to 1 (formal)
    directness: float                  # 0 (indirect) to 1 (direct)
    warmth: float                      # 0 (cold) to 1 (warm)

    # Patterns
    uses_bullet_points: bool
    uses_questions: bool
    typical_paragraph_count: int
    sentence_complexity: float         # avg words per sentence

    # Vocabulary
    common_phrases: list[str]          # "Let me know", "Happy to help", etc.
    avoided_words: list[str]           # Words user never uses

class EmailStyleAnalyzer:
    async def extract_style(self, email_body: str, recipient: str) -> StyleMetrics
    async def compare_styles(self, draft: str, target_style: StyleMetrics) -> StyleDiff
    async def generate_style_guidance(self, recipient: str) -> str  # For prompt injection
```

#### A.2 Sent Email Analysis Hook

**File:** `src/cognitex/services/ingestion.py` (MODIFY)

In `process_sent_emails()`:
```python
# After existing task completion check
style_analyzer = get_email_style_analyzer()
style_metrics = await style_analyzer.extract_style(
    email_body=body,
    recipient=to_email,
)

# Store style profile
await style_analyzer.update_recipient_profile(
    recipient=to_email,
    metrics=style_metrics,
)

# Trigger expertise learning
expertise = get_expertise_manager()
await expertise.self_improve(
    domain="email_writing",
    action_type="email_sent",
    context={
        "recipient": to_email,
        "style_metrics": style_metrics.to_dict(),
    }
)
```

#### A.3 Draft Edit Tracking

**File:** `src/cognitex/services/gmail.py` (MODIFY)

Track draft lifecycle:
```python
async def track_draft_lifecycle(
    self,
    draft_id: str,
    original_body: str,
    action: str,  # "created", "edited", "sent", "discarded"
    final_body: str | None = None,
):
    """Track what happens to drafts to learn from edits."""

    if action == "sent" and final_body:
        # Calculate edit distance
        edit_ratio = levenshtein_ratio(original_body, final_body)

        if edit_ratio < 0.8:  # Significant edits
            # Analyze what changed and learn from it
            await self._learn_from_draft_edits(original_body, final_body)
```

**Neo4j Schema Addition:**
```cypher
CREATE (d:EmailDraft {
    id: $draft_id,
    original_body: $original,
    final_body: $final,
    edit_ratio: $ratio,
    recipient: $to,
    sent_at: datetime()
})
```

#### A.4 Style-Aware Draft Generation

**File:** `src/cognitex/agent/triggers.py` (MODIFY)

In `_draft_email_response()`:
```python
# Get recipient style profile
style_analyzer = get_email_style_analyzer()
style_guidance = await style_analyzer.generate_style_guidance(recipient_email)

# Include in prompt
prompt = f"""
Draft a response to this email.

WRITING STYLE (match this):
{style_guidance}

EMAIL TO RESPOND TO:
{email_content}
"""
```

#### A.5 Data Models

**PostgreSQL Table:** `email_style_profiles`
```sql
CREATE TABLE email_style_profiles (
    id SERIAL PRIMARY KEY,
    recipient_email TEXT NOT NULL,
    metrics JSONB NOT NULL,
    sample_count INT DEFAULT 1,
    last_updated TIMESTAMP DEFAULT NOW(),
    UNIQUE(recipient_email)
);
```

**Neo4j Node:** `:EmailStyle`
```cypher
(:Person)-[:HAS_STYLE_PROFILE]->(:EmailStyle {
    formality: 0.7,
    directness: 0.8,
    greeting: "Hi",
    closing: "Best",
    common_phrases: ["Let me know", "Thanks for"]
})
```

---

## Track B: Email Response Pattern Learning

### Problem
- System drafts responses for emails that don't need responses
- No learning from which emails user actually responds to
- Intent classifier is static, doesn't improve from feedback

### Solution Architecture

```
Email Arrives → IntentClassifier → response_likelihood score
                                           ↓
                              Present to user (or skip if low)
                                           ↓
                              User action: respond / skip / delegate
                                           ↓
                              Record in email_response_decisions
                                           ↓
                              Learn patterns → improve classifier
```

### Implementation

#### B.1 Email Response Decision Table

**File:** `src/cognitex/db/phase4_schema.py` (MODIFY)

```sql
CREATE TABLE email_response_decisions (
    id SERIAL PRIMARY KEY,
    email_id TEXT NOT NULL,           -- Gmail ID
    sender_email TEXT NOT NULL,
    sender_domain TEXT,
    subject TEXT,

    -- Classification at time of decision
    intent TEXT,                       -- from EmailIntent enum
    intent_confidence FLOAT,
    predicted_needs_response BOOLEAN,

    -- User's actual decision
    user_decision TEXT,                -- 'responded', 'skipped', 'delegated', 'flagged_later'
    decision_reason TEXT,              -- optional user feedback

    -- Context at decision time
    operating_mode TEXT,               -- focused, available, etc.
    hour_of_day INT,
    day_of_week INT,

    -- Outcome (filled in later)
    did_respond BOOLEAN,
    response_time_minutes INT,

    created_at TIMESTAMP DEFAULT NOW()
);

-- Indexes for pattern learning
CREATE INDEX idx_response_sender ON email_response_decisions(sender_email);
CREATE INDEX idx_response_intent ON email_response_decisions(intent);
CREATE INDEX idx_response_decision ON email_response_decisions(user_decision);
```

#### B.2 Response Likelihood Scorer

**File:** `src/cognitex/services/response_predictor.py` (NEW)

```python
class ResponsePredictor:
    """Predict whether an email needs a response based on learned patterns."""

    async def predict_response_needed(
        self,
        email: dict,
        intent_result: EmailIntentResult,
    ) -> ResponsePrediction:
        """
        Returns:
            ResponsePrediction with:
            - needs_response: bool
            - confidence: float
            - reasoning: str
            - similar_decisions: list  # Past decisions for context
        """

        # Factor 1: Sender history
        sender_stats = await self._get_sender_response_rate(email["from"])

        # Factor 2: Intent-based baseline
        intent_baseline = self._intent_response_rates.get(intent_result.intent, 0.5)

        # Factor 3: Time/context patterns
        context_modifier = await self._get_context_modifier()

        # Factor 4: Similar email decisions (semantic)
        similar = await self._find_similar_decisions(email["subject"], email["snippet"])

        # Combine factors
        score = self._combine_factors(sender_stats, intent_baseline, context_modifier, similar)

        return ResponsePrediction(
            needs_response=score > 0.5,
            confidence=abs(score - 0.5) * 2,
            reasoning=self._explain_prediction(factors),
            similar_decisions=similar[:3],
        )
```

#### B.3 Decision Recording

**File:** `src/cognitex/services/inbox.py` (MODIFY)

Add to email decision handling:
```python
async def record_email_decision(
    self,
    email_id: str,
    email_data: dict,
    intent_result: EmailIntentResult,
    user_decision: str,
    reason: str | None = None,
):
    """Record user's response decision for learning."""

    from cognitex.agent.state_model import get_state_estimator
    state = await get_state_estimator().get_current_state()

    await self._db.execute("""
        INSERT INTO email_response_decisions (
            email_id, sender_email, sender_domain, subject,
            intent, intent_confidence, predicted_needs_response,
            user_decision, decision_reason,
            operating_mode, hour_of_day, day_of_week
        ) VALUES (...)
    """)
```

#### B.4 Pattern Learning Job

**File:** `src/cognitex/agent/learning.py` (MODIFY)

Add to daily learning cycle:
```python
async def _learn_email_response_patterns(self):
    """Extract patterns from email response decisions."""

    # Query recent decisions
    decisions = await self._get_recent_email_decisions(days=30)

    # Calculate sender response rates
    sender_rates = self._calculate_sender_rates(decisions)

    # Calculate intent response rates
    intent_rates = self._calculate_intent_rates(decisions)

    # Extract time-based patterns
    time_patterns = self._extract_time_patterns(decisions)

    # Store as preference rules
    for pattern in self._significant_patterns(sender_rates, intent_rates, time_patterns):
        await self._store_preference_rule(
            rule_type="email_response",
            condition=pattern.condition,
            action=pattern.action,
            confidence=pattern.confidence,
        )
```

#### B.5 Feedback Integration

**File:** `src/cognitex/web/app.py` (MODIFY)

When user dismisses/skips email inbox items:
```python
@app.post("/api/inbox/{item_id}/skip")
async def skip_inbox_email(item_id: str, reason: str = None):
    """User indicates they won't respond to this email."""

    item = await inbox.get_item(item_id)

    if item.item_type in ["email_draft", "email_review"]:
        await inbox.record_email_decision(
            email_id=item.payload.get("email_id"),
            email_data=item.payload,
            intent_result=item.payload.get("intent"),
            user_decision="skipped",
            reason=reason,
        )

    await inbox.dismiss_item(item_id)
```

---

## Track C: External Research for Context Packs

### Problem
- Context packs only use internal data (emails, docs, graph)
- No web search for meeting topics or attendee backgrounds
- Can't answer "what do I need to know about X company"

### Solution Architecture

```
Context Pack Build Trigger
           ↓
    Extract research targets:
    - Attendee names + companies
    - Topics from event description
    - Companies/products mentioned
           ↓
    WebSearchTool.execute() for each target
           ↓
    Cache results in graph (ExternalContext nodes)
           ↓
    Include in pack's external_research field
```

### Implementation

#### C.1 Research Target Extractor

**File:** `src/cognitex/services/research_extractor.py` (NEW)

```python
@dataclass
class ResearchTarget:
    target_type: str  # "person", "company", "topic", "product"
    query: str        # Search query
    priority: int     # 1-5, higher = more important
    context: str      # Why this matters

class ResearchTargetExtractor:
    async def extract_targets(
        self,
        event: dict,
        attendees: list[dict],
    ) -> list[ResearchTarget]:
        """Extract research targets from event context."""

        targets = []

        # 1. Attendee research
        for att in attendees:
            email = att.get("email", "")
            name = att.get("displayName") or email.split("@")[0]
            domain = email.split("@")[1] if "@" in email else None

            # Person search
            targets.append(ResearchTarget(
                target_type="person",
                query=f"{name} {domain or ''}",
                priority=3,
                context=f"Meeting attendee: {name}",
            ))

            # Company search (if external)
            if domain and not self._is_internal_domain(domain):
                targets.append(ResearchTarget(
                    target_type="company",
                    query=f"{domain.replace('.com', '')} company",
                    priority=2,
                    context=f"Attendee's company",
                ))

        # 2. Topic extraction from event description
        topics = await self._extract_topics(event.get("description", ""))
        for topic in topics:
            targets.append(ResearchTarget(
                target_type="topic",
                query=topic,
                priority=2,
                context="Event topic",
            ))

        return sorted(targets, key=lambda t: t.priority, reverse=True)[:10]
```

#### C.2 External Research Gatherer

**File:** `src/cognitex/agent/context_pack.py` (MODIFY)

Add new method:
```python
async def _gather_external_research(
    self,
    event: dict,
    attendees: list[dict],
) -> list[dict]:
    """Gather external research via web search."""

    from cognitex.services.research_extractor import ResearchTargetExtractor
    from cognitex.agent.tools import WebSearchTool

    extractor = ResearchTargetExtractor()
    targets = await extractor.extract_targets(event, attendees)

    web_search = WebSearchTool()
    research_results = []

    for target in targets[:5]:  # Limit to 5 searches per pack
        # Check cache first
        cached = await self._get_cached_research(target.query)
        if cached and self._is_fresh(cached):
            research_results.append(cached)
            continue

        # Perform web search
        result = await web_search.execute(query=target.query, num_results=3)

        if result.success:
            research = {
                "target_type": target.target_type,
                "query": target.query,
                "context": target.context,
                "results": result.data,
                "fetched_at": datetime.now().isoformat(),
            }
            research_results.append(research)

            # Cache in graph
            await self._cache_research(target.query, research)

    return research_results
```

#### C.3 Context Pack Enhancement

**File:** `src/cognitex/agent/context_pack.py` (MODIFY)

Add to `ContextPackContent`:
```python
@dataclass
class ContextPackContent:
    # ... existing fields ...

    # NEW: External research
    external_research: list[dict] = field(default_factory=list)
    attendee_profiles: list[dict] = field(default_factory=list)
```

Modify `compile_for_event()`:
```python
async def compile_for_event(self, event: dict, stage: BuildStage) -> ContextPackContent:
    # ... existing code ...

    # NEW: Gather external research (only at T-24h, cache for later stages)
    external_research = []
    attendee_profiles = []

    if stage == BuildStage.T_24H:
        external_research = await self._gather_external_research(event, attendees)
        attendee_profiles = await self._build_attendee_profiles(attendees, external_research)
    else:
        # Use cached research from earlier build
        external_research = await self._get_cached_event_research(event_id)
        attendee_profiles = await self._get_cached_attendee_profiles(event_id)

    pack = ContextPackContent(
        # ... existing fields ...
        external_research=external_research,
        attendee_profiles=attendee_profiles,
    )
```

#### C.4 Research Cache in Neo4j

**Schema Addition:**
```cypher
// External research cache
CREATE (r:ExternalResearch {
    query: $query,
    target_type: $type,
    results: $results,      // JSON array
    fetched_at: datetime(),
    expires_at: datetime() + duration('P7D')  // 7-day cache
})

// Link to events that used it
MATCH (e:CalendarEvent {id: $event_id})
MATCH (r:ExternalResearch {query: $query})
MERGE (e)-[:USED_RESEARCH]->(r)

// Link to people
MATCH (p:Person {email: $email})
MATCH (r:ExternalResearch {query: $query, target_type: 'person'})
MERGE (p)-[:HAS_EXTERNAL_PROFILE]->(r)
```

---

## Track D: Ambient Context ("What happened since...")

### Problem
- Context packs don't summarize recent relationship history
- No "since we last met" context
- Can't quickly recall pending items with a person

### Solution Architecture

```
Context Pack for Meeting with Person X
              ↓
    Query: Last meeting with X
    Query: Emails since last meeting
    Query: Tasks involving X
    Query: Documents mentioning X
              ↓
    LLM Summary: "Since your last call on Jan 10..."
              ↓
    Include in ambient_context field
```

### Implementation

#### D.1 Relationship Timeline Builder

**File:** `src/cognitex/services/relationship_timeline.py` (NEW)

```python
@dataclass
class RelationshipEvent:
    event_type: str        # "email_sent", "email_received", "meeting", "task_created"
    timestamp: datetime
    summary: str
    sentiment: str | None  # positive, neutral, negative
    outcome: str | None    # for meetings/tasks

@dataclass
class AmbientContext:
    person_email: str
    last_interaction: datetime
    last_meeting: datetime | None

    # Since last meeting
    emails_exchanged: int
    emails_from_them: int
    emails_to_them: int
    key_topics: list[str]

    # Pending items
    open_tasks: list[dict]
    pending_requests: list[str]
    awaiting_their_response: bool

    # Narrative summary
    summary: str  # "Since your call on Jan 10, you've exchanged 5 emails about the proposal..."

class RelationshipTimelineBuilder:
    async def build_ambient_context(
        self,
        person_email: str,
        reference_event_id: str | None = None,
    ) -> AmbientContext:
        """Build ambient context for a relationship."""

        # Find last meeting
        last_meeting = await self._get_last_meeting(person_email)
        reference_date = last_meeting.get("end") if last_meeting else None

        # Get events since then
        events = await self._get_relationship_events(
            person_email,
            since=reference_date,
        )

        # Analyze events
        email_stats = self._analyze_email_flow(events)
        topics = await self._extract_key_topics(events)
        pending = await self._get_pending_items(person_email)

        # Generate narrative summary
        summary = await self._generate_summary(
            person_email, last_meeting, events, pending
        )

        return AmbientContext(
            person_email=person_email,
            last_interaction=events[0].timestamp if events else None,
            last_meeting=last_meeting.get("start") if last_meeting else None,
            emails_exchanged=email_stats["total"],
            emails_from_them=email_stats["received"],
            emails_to_them=email_stats["sent"],
            key_topics=topics,
            open_tasks=pending.get("tasks", []),
            pending_requests=pending.get("requests", []),
            awaiting_their_response=pending.get("awaiting_response", False),
            summary=summary,
        )
```

#### D.2 Neo4j Timeline Queries

```cypher
// Get last meeting with person
MATCH (e:CalendarEvent)-[:ATTENDED_BY]->(p:Person {email: $email})
WHERE e.start < datetime()
RETURN e ORDER BY e.start DESC LIMIT 1

// Get emails since date
MATCH (email:Email)
WHERE email.date > datetime($since)
  AND (
    (email)-[:SENT_BY]->(:Person {email: $person_email})
    OR (email)-[:RECEIVED_BY]->(:Person {email: $person_email})
  )
RETURN email ORDER BY email.date

// Get open tasks involving person
MATCH (t:Task)-[:ASSIGNED_TO|MENTIONED_IN]->(p:Person {email: $email})
WHERE t.status IN ['pending', 'in_progress']
RETURN t

// Get pending requests (emails awaiting response)
MATCH (e:Email)-[:SENT_BY]->(p:Person {email: $email})
WHERE e.needs_response = true
  AND NOT EXISTS { (reply:Email)-[:REPLY_TO]->(e) }
RETURN e
```

#### D.3 Context Pack Integration

**File:** `src/cognitex/agent/context_pack.py` (MODIFY)

Add to `ContextPackContent`:
```python
@dataclass
class ContextPackContent:
    # ... existing fields ...

    # NEW: Ambient context per attendee
    ambient_context: list[AmbientContext] = field(default_factory=list)
```

Modify `compile_for_event()`:
```python
# Build ambient context for each attendee
timeline_builder = get_relationship_timeline_builder()
ambient_contexts = []

for att in attendees[:5]:  # Limit to top 5 attendees
    email = att.get("email")
    if email and not self._is_internal(email):
        context = await timeline_builder.build_ambient_context(
            person_email=email,
            reference_event_id=event_id,
        )
        ambient_contexts.append(context)

pack.ambient_context = ambient_contexts
```

---

## Files to Create/Modify

### New Files

| File | Purpose |
|------|---------|
| `src/cognitex/services/email_style.py` | Style extraction and profile management |
| `src/cognitex/services/response_predictor.py` | Predict which emails need responses |
| `src/cognitex/services/research_extractor.py` | Extract research targets from events |
| `src/cognitex/services/relationship_timeline.py` | Build ambient context for relationships |

### Modified Files

| File | Changes |
|------|---------|
| `src/cognitex/services/ingestion.py` | Hook sent email analysis for style |
| `src/cognitex/services/gmail.py` | Track draft lifecycle |
| `src/cognitex/services/inbox.py` | Record email response decisions |
| `src/cognitex/agent/context_pack.py` | Add external research + ambient context |
| `src/cognitex/agent/triggers.py` | Style-aware draft generation |
| `src/cognitex/agent/learning.py` | Email response pattern learning |
| `src/cognitex/db/phase4_schema.py` | New tables for decisions + style |
| `src/cognitex/web/app.py` | Skip/delegate email endpoints |

---

## Implementation Order

### Phase 1: Foundation (Style + Response Tracking)
1. Create `email_style.py` with StyleMetrics extraction
2. Create `email_response_decisions` table
3. Hook style analysis into sent email processing
4. Add skip/delegate endpoints to inbox

### Phase 2: Learning Loops
5. Create `response_predictor.py`
6. Add pattern learning to daily learning cycle
7. Implement style-aware draft generation
8. Add draft edit tracking

### Phase 3: External Research
9. Create `research_extractor.py`
10. Add web search to context pack compilation
11. Implement research caching in Neo4j
12. Add attendee profile building

### Phase 4: Ambient Context
13. Create `relationship_timeline.py`
14. Add timeline queries to Neo4j
15. Integrate ambient context into context packs
16. Generate narrative summaries

### Phase 5: UI Integration
17. Display external research in context pack UI
18. Show response likelihood in email inbox
19. Add style feedback buttons
20. Display ambient context in meeting prep view

---

## Success Metrics

| Metric | Current | Target |
|--------|---------|--------|
| Email drafts matching user style | ~20% | >80% |
| Correct "needs response" prediction | ~60% | >90% |
| Context packs with external research | 0% | 100% |
| Ambient context available before meetings | 0% | 100% |
| User draft edit rate (lower = better) | Unknown | <20% |

---

## Data Privacy Considerations

1. **Style profiles** stored locally, not shared
2. **External research** cached with 7-day expiry
3. **Response patterns** aggregated, no PII in rules
4. **Web searches** logged but not stored long-term
5. User can delete style/pattern data on request

---

## Dependencies

- Existing: Neo4j, PostgreSQL, LLM service, WebSearchTool
- New: None (all uses existing infrastructure)
