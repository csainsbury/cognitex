# Neo4j Graph Queries Reference

## Accessing Neo4j Browser

### Remote Server Access

Tunnel both ports from your local machine:

```bash
ssh -L 7474:localhost:7474 -L 7687:localhost:7687 user@your-server
```

Then open `http://localhost:7474` in your browser.

### Login Credentials

- **Connect URL**: `bolt://localhost:7687`
- **Username**: `neo4j`
- **Password**: (from `.env` file - `NEO4J_AUTH` value after the `/`)

---

## Quick Overview Queries

### Count all nodes by type
```cypher
MATCH (n) RETURN labels(n)[0] as type, count(n) as count ORDER BY count DESC
```

### Count all relationships by type
```cypher
MATCH ()-[r]->() RETURN type(r) as type, count(r) as count ORDER BY count DESC
```

### Full graph statistics
```cypher
CALL apoc.meta.stats() YIELD labels, relTypes
RETURN labels, relTypes
```

---

## Visualization Queries

### See everything (limited for performance)
```cypher
MATCH (n)-[r]->(m) RETURN n, r, m LIMIT 100
```

### Email network - who sends to whom
```cypher
MATCH (e:Email)-[:SENT_BY]->(sender:Person)
MATCH (e)-[:RECEIVED_BY]->(recipient:Person)
RETURN sender, e, recipient LIMIT 50
```

### Calendar events and attendees
```cypher
MATCH (ev:Event)-[r:ATTENDED_BY]->(p:Person)
RETURN ev, r, p LIMIT 50
```

### Tasks and their sources
```cypher
MATCH (t:Task)-[:DERIVED_FROM]->(e:Email)-[:SENT_BY]->(p:Person)
RETURN t, e, p
```

### Your most connected people
```cypher
MATCH (p:Person)-[r]-()
WITH p, count(r) as connections
ORDER BY connections DESC
LIMIT 20
RETURN p.email, p.name, connections
```

---

## Email Queries

### Recent emails
```cypher
MATCH (e:Email)
RETURN e.subject, e.date, e.classification
ORDER BY e.date DESC
LIMIT 20
```

### Actionable emails
```cypher
MATCH (e:Email)
WHERE e.classification = 'actionable' AND e.action_required = true
OPTIONAL MATCH (e)-[:SENT_BY]->(p:Person)
RETURN e.subject, e.urgency, p.email as from
ORDER BY e.urgency DESC
```

### Emails by classification
```cypher
MATCH (e:Email)
RETURN e.classification, count(e) as count
ORDER BY count DESC
```

### Unprocessed emails
```cypher
MATCH (e:Email)
WHERE e.processed = false OR e.processed IS NULL
RETURN e.subject, e.date
ORDER BY e.date DESC
```

### Email threads (emails in same thread)
```cypher
MATCH (e:Email)
WITH e.thread_id as thread, collect(e) as emails
WHERE size(emails) > 1
RETURN thread, size(emails) as email_count, [x IN emails | x.subject][0] as subject
ORDER BY email_count DESC
LIMIT 10
```

---

## Calendar/Event Queries

### Today's events
```cypher
MATCH (ev:Event)
WHERE date(ev.start) = date()
RETURN ev.title, ev.start, ev.event_type, ev.energy_impact
ORDER BY ev.start
```

### This week's events
```cypher
MATCH (ev:Event)
WHERE date(ev.start) >= date() AND date(ev.start) < date() + duration({days: 7})
RETURN ev.title, ev.start, ev.event_type, ev.energy_impact
ORDER BY ev.start
```

### Events by type
```cypher
MATCH (ev:Event)
RETURN ev.event_type, count(ev) as count, avg(ev.energy_impact) as avg_energy
ORDER BY count DESC
```

### High energy cost events
```cypher
MATCH (ev:Event)
WHERE ev.energy_impact >= 5
RETURN ev.title, ev.start, ev.event_type, ev.energy_impact, ev.duration_minutes
ORDER BY ev.energy_impact DESC
LIMIT 20
```

### Events with specific person
```cypher
MATCH (ev:Event)-[:ATTENDED_BY]->(p:Person)
WHERE p.email CONTAINS 'someone@example.com'
RETURN ev.title, ev.start, ev.event_type
ORDER BY ev.start DESC
```

### External meetings (multiple domains)
```cypher
MATCH (ev:Event)
WHERE ev.event_type = 'external'
RETURN ev.title, ev.start, ev.attendee_count
ORDER BY ev.start DESC
LIMIT 20
```

---

## Task Queries

### All pending tasks
```cypher
MATCH (t:Task)
WHERE t.status = 'pending'
OPTIONAL MATCH (t)-[:DERIVED_FROM]->(e:Email)
RETURN t.title, t.energy_cost, t.due, e.subject as source
ORDER BY t.due ASC, t.energy_cost DESC
```

### Tasks by energy cost
```cypher
MATCH (t:Task)
RETURN t.status, count(t) as count, sum(t.energy_cost) as total_energy
```

### Tasks from a specific person
```cypher
MATCH (t:Task)-[:REQUESTED_BY]->(p:Person)
WHERE p.email CONTAINS 'someone@example.com'
RETURN t.title, t.status, t.energy_cost
```

### Overdue tasks
```cypher
MATCH (t:Task)
WHERE t.due < date() AND t.status <> 'done'
RETURN t.title, t.due, t.energy_cost
ORDER BY t.due
```

---

## Person/Contact Queries

### All contacts
```cypher
MATCH (p:Person)
RETURN p.email, p.name, p.org, p.role
ORDER BY p.email
LIMIT 50
```

### Most frequent correspondents
```cypher
MATCH (p:Person)<-[:SENT_BY]-(e:Email)
WITH p, count(e) as emails_sent
ORDER BY emails_sent DESC
LIMIT 20
RETURN p.email, p.name, emails_sent
```

### People you meet with most
```cypher
MATCH (p:Person)<-[:ATTENDED_BY]-(ev:Event)
WITH p, count(ev) as meetings
ORDER BY meetings DESC
LIMIT 20
RETURN p.email, p.name, meetings
```

### Contact network (who knows who via shared emails)
```cypher
MATCH (p1:Person)<-[:SENT_BY]-(e:Email)-[:RECEIVED_BY]->(p2:Person)
WHERE p1 <> p2
WITH p1, p2, count(e) as interactions
WHERE interactions > 2
RETURN p1.email, p2.email, interactions
ORDER BY interactions DESC
LIMIT 50
```

---

## Energy & Workload Analysis

### Daily energy forecast
```cypher
MATCH (ev:Event)
WHERE date(ev.start) = date()
RETURN
    count(ev) as events,
    sum(ev.duration_minutes) as total_minutes,
    sum(ev.energy_impact) as total_energy_cost
```

### Weekly energy breakdown
```cypher
MATCH (ev:Event)
WHERE date(ev.start) >= date() AND date(ev.start) < date() + duration({days: 7})
WITH date(ev.start) as day, sum(ev.energy_impact) as energy
RETURN day, energy
ORDER BY day
```

### Pending task load
```cypher
MATCH (t:Task)
WHERE t.status = 'pending'
RETURN count(t) as pending_tasks, sum(t.energy_cost) as total_energy_needed
```

---

## Relationship Inference

### Infer WORKS_WITH relationships (run once after sync)
```cypher
MATCH (e:Email)-[:SENT_BY]->(sender:Person)
MATCH (e)-[:RECEIVED_BY]->(recipient:Person)
WHERE sender <> recipient
MERGE (sender)-[r:WORKS_WITH]->(recipient)
ON CREATE SET r.first_interaction = e.date, r.interaction_count = 1
ON MATCH SET r.interaction_count = r.interaction_count + 1, r.last_interaction = e.date
RETURN count(r) as relationships_created
```

### View work relationships
```cypher
MATCH (p1:Person)-[r:WORKS_WITH]->(p2:Person)
RETURN p1.email, p2.email, r.interaction_count
ORDER BY r.interaction_count DESC
LIMIT 30
```

---

## Data Maintenance

### Delete all emails (before re-sync)
```cypher
MATCH (e:Email) DETACH DELETE e
```

### Delete orphaned people (no relationships)
```cypher
MATCH (p:Person)
WHERE NOT (p)--()
DELETE p
```

### Delete all events (before re-sync)
```cypher
MATCH (ev:Event) DETACH DELETE ev
```

### Delete all tasks
```cypher
MATCH (t:Task) DETACH DELETE t
```

### Full reset (delete everything)
```cypher
MATCH (n) DETACH DELETE n
```

---

## Document Queries

### All documents
```cypher
MATCH (d:Document)
RETURN d.name, d.mime_type, d.folder_path, d.indexed
ORDER BY d.modified_at DESC
LIMIT 30
```

### Documents by folder
```cypher
MATCH (d:Document)
WHERE d.folder_path STARTS WITH 'dundee'
RETURN d.name, d.mime_type, d.indexed
ORDER BY d.modified_at DESC
```

### Document ownership network
```cypher
MATCH (d:Document)-[:OWNED_BY]->(p:Person)
RETURN p.email, count(d) as docs_owned
ORDER BY docs_owned DESC
LIMIT 20
```

### Documents shared with me
```cypher
MATCH (d:Document)-[:SHARED_WITH]->(p:Person {email: 'your@email.com'})
RETURN d.name, d.folder_path, d.modified_at
ORDER BY d.modified_at DESC
```

### Indexed documents (full text available)
```cypher
MATCH (d:Document)
WHERE d.indexed = true
RETURN d.name, d.folder_path, d.indexed_at
ORDER BY d.indexed_at DESC
```

### Document statistics
```cypher
MATCH (d:Document)
RETURN
    count(d) as total,
    sum(CASE WHEN d.indexed = true THEN 1 ELSE 0 END) as indexed,
    sum(CASE WHEN d.is_shared = true THEN 1 ELSE 0 END) as shared
```

### Documents by person (owned + shared)
```cypher
MATCH (p:Person {email: 'someone@example.com'})
OPTIONAL MATCH (d1:Document)-[:OWNED_BY]->(p)
OPTIONAL MATCH (d2:Document)-[:SHARED_WITH]->(p)
WITH p, collect(DISTINCT d1) + collect(DISTINCT d2) as docs
UNWIND docs as d
RETURN DISTINCT d.name, d.folder_path
ORDER BY d.modified_at DESC
```

### Priority folder contents
```cypher
MATCH (d:Document)
WHERE d.folder_path STARTS WITH 'dundee'
   OR d.folder_path STARTS WITH 'myWayDigitalHealth'
   OR d.folder_path STARTS WITH 'glucose.ai'
   OR d.folder_path STARTS WITH 'birmingham'
RETURN d.folder_path, d.name, d.indexed
ORDER BY d.folder_path, d.modified_at DESC
```

---

## CLI Equivalents

These queries can also be accessed via CLI:

```bash
# Graph statistics
cognitex graph

# View tasks
cognitex tasks
cognitex tasks --status pending

# Today's schedule
cognitex today

# View documents
cognitex documents
cognitex documents --folder dundee
cognitex documents --indexed

# Sync Drive files
cognitex drive-sync                    # Metadata only
cognitex drive-sync -f dundee          # Specific folder
cognitex drive-sync --index-priority   # With content indexing

# Semantic document search
cognitex doc-search "grant proposal"

# System status
cognitex status
```

---

## Agent Commands

The agent system provides intelligent interaction with your data:

```bash
# Chat with the agent
cognitex agent-chat                    # Interactive mode
cognitex agent-chat "What's urgent?"   # Single query

# Morning briefing
cognitex briefing

# Manage pending approvals (emails, events the agent wants to create)
cognitex approvals                     # List pending
cognitex approvals approve apr_xxx     # Approve an action
cognitex approvals reject apr_xxx      # Reject an action

# Run agent in specific mode
cognitex agent-run briefing            # Morning summary
cognitex agent-run review              # Evening review
cognitex agent-run monitor             # Check for urgent
cognitex agent-run escalate            # Handle overdue

# Agent status
cognitex agent-status                  # Config + memory stats
```

See `docs/agent-architecture.md` for full agent documentation.
