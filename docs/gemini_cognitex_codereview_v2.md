gemini code review version 2

This looks like a massive step forward. You've successfully pivoted the agent from a "janitor" (cleaning up the graph) to a "chief of staff" (drafting, scheduling, and preparing context). The integration of the `Twin` review dashboard in the web UI is particularly strong—it solves the "human-in-the-loop" safety problem elegantly.

However, I've identified **two specific data gaps** in the implementation that will prevent the "Writing Style Mimicry" from working as intended right now.

### 1. The "Self" Identity Problem
The query in `graph_observer.py` relies on a property `p.is_user = true` to find your sent emails:
```cypher
MATCH (e:Email)-[:SENT_BY]->(p:Person)
WHERE p.is_user = true
```
**Issue:** The ingestion pipeline (`ingest_email_to_graph` in `ingestion.py`) creates Person nodes but never sets `is_user = true`. The agent won't find any writing samples because it doesn't know who "you" are in the graph.

### 2. The Missing Email Body Problem
The observer queries for `e.body`:
```cypher
WHERE e.body IS NOT NULL ... RETURN e.body
```
**Issue:** The `Email` nodes in your graph schema currently only store `snippet` (short preview) and `body_preview`. The full body isn't being stored in Neo4j (likely to keep the graph light), so `e.body` will always be null, and the agent will have zero writing samples to learn from.

---

### Recommended Fixes

Here are the two patches to make the Digital Twin functional.

#### Fix 1: Identify "Self" during Ingestion
Update `ingestion.py` to flag the Person node that matches your authenticated email address.

<file_path="src/cognitex/services/ingestion.py">
```python
# In ingest_email_to_graph function

async def ingest_email_to_graph(email_data: dict) -> None:
    """Ingest a single email into the graph database."""
    # Get authenticated user email to mark 'self'
    user_email = await get_user_email()
    
    async for session in get_neo4j_session():
        # Create sender Person node
        if email_data["sender_email"]:
            is_self = (email_data["sender_email"].lower() == user_email)
            await create_person(
                session,
                email=email_data["sender_email"],
                name=email_data["sender_name"] or None,
                is_user=is_self  # Pass this new param
            )
            
        # ... rest of function ...
```
</file_path>

You'll need to update `create_person` in `graph_schema.py` to accept and set `is_user`:

<file_path="src/cognitex/db/graph_schema.py">
```python
async def create_person(
    session: AsyncSession,
    email: str,
    name: str | None = None,
    org: str | None = None,
    role: str | None = None,
    is_user: bool = False,  # New param
) -> dict:
    """Create or update a Person node."""
    query = """
    MERGE (p:Person {email: $email})
    ON CREATE SET
        p.name = $name,
        p.org = $org,
        p.role = $role,
        p.is_user = $is_user,
        p.created_at = datetime(),
        p.updated_at = datetime()
    ON MATCH SET
        p.name = COALESCE($name, p.name),
        p.org = COALESCE($org, p.org),
        p.role = COALESCE($role, p.role),
        p.is_user = COALESCE($is_user, p.is_user),  # Update if discovered
        p.updated_at = datetime()
    RETURN p
    """
    result = await session.run(query, email=email, name=name, org=org, role=role, is_user=is_user)
    # ...
```
</file_path>

#### Fix 2: Fallback for Writing Samples
Since storing full email bodies in the graph is heavy, a quick fix is to use the `snippet` property in the observer for now. It's usually enough to capture tone (formal vs casual).

<file_path="src/cognitex/agent/graph_observer.py">
```python
    async def get_user_writing_samples(self, limit: int = 5) -> list[str]:
        """Fetch recent emails sent by the user to establish writing style."""
        # Modified to use 'snippet' if 'body' is missing, and fallback for is_user
        query = """
        MATCH (e:Email)-[:SENT_BY]->(p:Person)
        WHERE (p.is_user = true OR p.email = $user_email) 
        RETURN coalesce(e.body, e.snippet) as body
        ORDER BY e.date DESC
        LIMIT $limit
        """
        
        # Get user email for fallback
        from cognitex.services.ingestion import get_user_email
        user_email = await get_user_email()
        
        try:
            result = await self.session.run(query, {"limit": limit, "user_email": user_email})
            data = await result.data()
            samples = [row["body"] for row in data if row["body"] and len(row["body"]) > 20]
            
            if not samples:
                return ["(No recent sent emails found to learn style from)"]
            return samples
        except Exception as e:
            logger.warning("Failed to get writing samples", error=str(e))
            return ["(Error fetching writing samples)"]
```
</file_path>

With these two fixes, your Digital Twin will be able to identify you and start mimicking your voice immediately. The rest of the architecture looks excellent.

<chatName="Fix Digital Twin Identity and Data Gaps"/>