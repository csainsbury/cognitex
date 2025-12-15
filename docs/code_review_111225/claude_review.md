Looking at this codebase against the blueprint, I can see solid foundational work. Let me provide a structured review of errors, omissions, and improvements.

## Critical Issues (Bugs/Errors)

### 1. **Missing Gmail Send Methods**
The agent tools reference methods that don't exist:

```python
# In agent/core.py handle_approval():
sent = gmail.send_reply(...)  # Doesn't exist
sent = gmail.send_message(...)  # Doesn't exist
```

The `GmailService` in `services/gmail.py` only has read operations. You need:

```python
# Add to services/gmail.py
def send_message(self, to: str, subject: str, body: str) -> dict:
    """Send a new email."""
    import base64
    from email.mime.text import MIMEText
    
    message = MIMEText(body)
    message['to'] = to
    message['subject'] = subject
    
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    return self.service.users().messages().send(
        userId='me',
        body={'raw': raw}
    ).execute()

def send_reply(self, thread_id: str, to: str, subject: str, body: str) -> dict:
    """Reply to an existing thread."""
    import base64
    from email.mime.text import MIMEText
    
    message = MIMEText(body)
    message['to'] = to
    message['subject'] = subject if subject.startswith('Re:') else f'Re: {subject}'
    
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    return self.service.users().messages().send(
        userId='me',
        body={'raw': raw, 'threadId': thread_id}
    ).execute()
```

### 2. **Missing Calendar Create Method**
Similar issue - `CalendarService` lacks `create_event()`:

```python
# Add to services/calendar.py
def create_event(
    self,
    title: str,
    start: str,
    end: str,
    attendees: list[str] | None = None,
    description: str | None = None,
    calendar_id: str = "primary",
) -> dict:
    """Create a new calendar event."""
    event = {
        'summary': title,
        'start': {'dateTime': start, 'timeZone': 'UTC'},
        'end': {'dateTime': end, 'timeZone': 'UTC'},
    }
    
    if description:
        event['description'] = description
    
    if attendees:
        event['attendees'] = [{'email': email} for email in attendees]
    
    return self.service.events().insert(
        calendarId=calendar_id,
        body=event,
        sendNotifications=True,
    ).execute()
```

### 3. **Redis Async/Sync Mismatch**
In `db/redis.py`, `get_redis()` is synchronous but called with `await` in multiple places:

```python
# Current (sync):
def get_redis() -> Redis:
    if _redis is None:
        raise RuntimeError("Redis not initialized")
    return _redis

# Used incorrectly in agent/memory.py:
redis = await get_redis()  # Error: can't await non-coroutine
```

Fix either by making it async-compatible or removing the await:

```python
# Option 1: Return directly (since _redis is already async client)
async def get_redis() -> Redis:
    if _redis is None:
        raise RuntimeError("Redis not initialized")
    return _redis
```

### 4. **LLM Service Model Name Mismatch**
`services/llm.py` uses legacy config fields that default to empty strings:

```python
# In llm.py:
self.primary_model = settings.together_model_primary  # Empty string!
self.fast_model = settings.together_model_fast        # Empty string!

# Should use:
self.planner_model = settings.together_model_planner
self.executor_model = settings.together_model_executor
```

---

## Omissions (Missing from Blueprint)

### 1. **Google Workspace Push Notifications**
The blueprint mentions event-driven triggers for new emails/calendar changes, but there's no webhook setup. The current implementation relies on polling. You'd need:

```python
# services/gmail.py - add watch setup
def setup_push_notifications(self, topic_name: str) -> dict:
    """Set up Gmail push notifications via Pub/Sub."""
    return self.service.users().watch(
        userId='me',
        body={
            'topicName': topic_name,
            'labelIds': ['INBOX'],
        }
    ).execute()
```

### 2. **Energy Tracking System**
The PostgreSQL schema has `energy_logs` table, but there's no implementation for:
- Recording actual energy levels
- Predicting energy from calendar
- Adjusting suggestions based on energy

Add a service:

```python
# services/energy.py
class EnergyService:
    async def log_energy(self, level: int, source: str = "manual") -> None:
        """Record current energy level."""
        pass
    
    async def predict_daily_energy(self, date: str) -> dict:
        """Predict energy curve based on calendar load."""
        pass
    
    async def get_available_capacity(self) -> int:
        """Calculate remaining energy for today."""
        pass
```

### 3. **Task API Implementation**
The REST endpoints in `api/routes/tasks.py` and `goals.py` raise `NotImplemented`. Wire them to the graph/postgres:

```python
@router.get("/")
async def list_tasks(...) -> list[TaskResponse]:
    from cognitex.db.neo4j import get_neo4j_session
    from cognitex.db.graph_schema import get_tasks
    
    async for session in get_neo4j_session():
        tasks = await get_tasks(session, status=status, limit=limit)
        return [TaskResponse(**t) for t in tasks]
```

### 4. **Relationship Health Scoring**
Blueprint mentions tracking relationship health with contacts. Add to graph schema:

```python
# In graph_schema.py
async def get_relationship_health(session: AsyncSession) -> list[dict]:
    """Flag contacts you haven't engaged with recently."""
    query = """
    MATCH (p:Person)<-[r:SENT_BY|RECEIVED_BY]-(e:Email)
    WITH p, max(e.date) as last_contact, count(e) as interaction_count
    WHERE last_contact < datetime() - duration({days: 30})
      AND interaction_count > 5  // Only people we regularly talk to
    RETURN p.email, p.name, last_contact, interaction_count
    ORDER BY last_contact ASC
    """
    # ...
```

### 5. **OAuth Scopes for Write Operations**
The current scopes include write permissions, but they're not used. Ensure `SCOPES` in `google_auth.py` includes what you need:

```python
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",  # Add for sending
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",    # Full access, not just readonly
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/drive.readonly",
]
```

---

## Improvements

### 1. **Better Email Classification Context**
Currently classification only uses metadata. Include body preview:

```python
# In services/ingestion.py or llm.py
async def classify_with_body(self, email_data: dict) -> dict:
    # Fetch full email body for better classification
    gmail = GmailService()
    full_msg = gmail.get_message(email_data["gmail_id"], format="full")
    body = extract_email_body(full_msg, max_length=2000)
    
    # Include body in classification prompt
    email_data["body_preview"] = body
    return await self.classify_email(email_data)
```

### 2. **Batch Operations for Performance**
The email sync fetches messages one-by-one with rate limiting. Use batch API:

```python
# Use Google's batch API for better performance
from googleapiclient.http import BatchHttpRequest

def get_message_batch_efficient(self, message_ids: list[str]) -> list[dict]:
    """Fetch multiple messages using batch API."""
    results = []
    
    def callback(request_id, response, exception):
        if exception:
            logger.warning(f"Batch request {request_id} failed: {exception}")
        else:
            results.append(response)
    
    batch = self.service.new_batch_http_request(callback=callback)
    for msg_id in message_ids:
        batch.add(self.service.users().messages().get(userId='me', id=msg_id, format='metadata'))
    
    batch.execute()
    return results
```

### 3. **Agent Context Window Management**
The planner prompt can get very large. Add token counting and truncation:

```python
# In planner.py
def _truncate_context(self, context: dict, max_tokens: int = 4000) -> dict:
    """Ensure context fits in model context window."""
    import json
    
    context_str = json.dumps(context, default=str)
    # Rough estimate: 4 chars per token
    if len(context_str) > max_tokens * 4:
        # Prioritize recent items, truncate older
        if "recent_memories" in context:
            context["recent_memories"] = context["recent_memories"][:5]
        if "working" in context and "interactions" in context["working"]:
            context["working"]["interactions"] = context["working"]["interactions"][-10:]
    
    return context
```

### 4. **Graceful Degradation**
Add fallbacks when services are unavailable:

```python
# In agent/core.py
async def run(self, mode: AgentMode, trigger: str, ...) -> ExecutionResult:
    try:
        context = await self.memory.build_context(trigger)
    except Exception as e:
        logger.warning("Memory unavailable, using minimal context", error=str(e))
        context = {"trigger": trigger, "timestamp": datetime.now().isoformat()}
    
    # Continue with degraded context...
```

### 5. **Add Health Check Depth**
The current health endpoint is shallow. Add dependency checks:

```python
# In api/routes/health.py
@router.get("/health/deep")
async def deep_health_check() -> dict:
    """Check all dependencies."""
    checks = {}
    
    # Check Neo4j
    try:
        from cognitex.db.neo4j import get_neo4j_session
        async for session in get_neo4j_session():
            await session.run("RETURN 1")
        checks["neo4j"] = "healthy"
    except Exception as e:
        checks["neo4j"] = f"unhealthy: {e}"
    
    # Check Redis, Postgres similarly...
    
    all_healthy = all(v == "healthy" for v in checks.values())
    return {"status": "healthy" if all_healthy else "degraded", "checks": checks}
```

### 6. **Discord Bot - Natural Language Intent Detection**
The bot currently only handles greetings. Add basic intent routing:

```python
# In discord_bot/__main__.py
async def handle_natural_language(self, message: discord.Message) -> None:
    content = message.content.lower()
    
    # Use LLM for intent classification
    from cognitex.services.llm import get_llm_service
    llm = get_llm_service()
    
    intent = await llm.classify_intent(content)
    
    if intent == "status_query":
        # Handle "what's urgent?", "what do I have today?"
        pass
    elif intent == "task_action":
        # Handle "mark X as done", "create task for..."
        pass
    elif intent == "approval_action":
        # Handle "approve that", "reject the email"
        pass
```

### 7. **Add Retry Logic to LLM Calls**
Together.ai can have transient failures. Add retries:

```python
# In services/llm.py
import asyncio
from functools import wraps

def with_retry(max_attempts: int = 3, backoff: float = 1.0):
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            for attempt in range(max_attempts):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    if attempt == max_attempts - 1:
                        raise
                    await asyncio.sleep(backoff * (2 ** attempt))
        return wrapper
    return decorator

@with_retry(max_attempts=3)
async def complete(self, prompt: str, ...) -> str:
    # existing implementation
```

---

## Summary Priority List

**Critical (Fix Now):**
1. Add Gmail send methods
2. Add Calendar create_event method  
3. Fix Redis async/sync mismatch
4. Fix LLM model config field names

**High Priority (Phase 2):**
5. Implement REST API endpoints
6. Add energy tracking service
7. Add relationship health scoring

**Medium Priority (Polish):**
8. Push notification webhooks
9. Better email body context for classification
10. Discord NL intent detection
11. Context window management
12. Retry logic on LLM calls

Would you like me to generate complete implementations for any of these fixes?