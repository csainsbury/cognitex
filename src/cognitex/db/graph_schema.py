"""Neo4j graph schema initialization and constraints."""

import structlog
from neo4j import AsyncSession

from cognitex.db.neo4j import get_driver

logger = structlog.get_logger()

# Schema definition: constraints and indexes for the graph
SCHEMA_STATEMENTS = [
    # Node uniqueness constraints (also create indexes)
    "CREATE CONSTRAINT person_email IF NOT EXISTS FOR (p:Person) REQUIRE p.email IS UNIQUE",
    "CREATE CONSTRAINT email_gmail_id IF NOT EXISTS FOR (e:Email) REQUIRE e.gmail_id IS UNIQUE",
    "CREATE CONSTRAINT event_gcal_id IF NOT EXISTS FOR (ev:Event) REQUIRE ev.gcal_id IS UNIQUE",
    "CREATE CONSTRAINT project_id IF NOT EXISTS FOR (p:Project) REQUIRE p.id IS UNIQUE",
    "CREATE CONSTRAINT goal_id IF NOT EXISTS FOR (g:Goal) REQUIRE g.id IS UNIQUE",
    "CREATE CONSTRAINT task_id IF NOT EXISTS FOR (t:Task) REQUIRE t.id IS UNIQUE",
    "CREATE CONSTRAINT document_drive_id IF NOT EXISTS FOR (d:Document) REQUIRE d.drive_id IS UNIQUE",

    # Additional indexes for common queries
    "CREATE INDEX person_name IF NOT EXISTS FOR (p:Person) ON (p.name)",
    "CREATE INDEX person_org IF NOT EXISTS FOR (p:Person) ON (p.org)",
    "CREATE INDEX email_date IF NOT EXISTS FOR (e:Email) ON (e.date)",
    "CREATE INDEX email_thread_id IF NOT EXISTS FOR (e:Email) ON (e.thread_id)",
    "CREATE INDEX email_action_required IF NOT EXISTS FOR (e:Email) ON (e.action_required)",
    "CREATE INDEX event_start IF NOT EXISTS FOR (ev:Event) ON (ev.start)",
    "CREATE INDEX task_status IF NOT EXISTS FOR (t:Task) ON (t.status)",
    "CREATE INDEX task_due IF NOT EXISTS FOR (t:Task) ON (t.due)",
    "CREATE INDEX project_status IF NOT EXISTS FOR (p:Project) ON (p.status)",
    "CREATE INDEX goal_timeframe IF NOT EXISTS FOR (g:Goal) ON (g.timeframe)",
    "CREATE INDEX document_name IF NOT EXISTS FOR (d:Document) ON (d.name)",
    "CREATE INDEX document_modified IF NOT EXISTS FOR (d:Document) ON (d.modified_at)",
    "CREATE INDEX document_folder IF NOT EXISTS FOR (d:Document) ON (d.folder_path)",
    "CREATE INDEX document_indexed IF NOT EXISTS FOR (d:Document) ON (d.indexed)",
]


async def init_graph_schema() -> None:
    """Initialize the Neo4j graph schema with constraints and indexes."""
    driver = get_driver()

    async with driver.session() as session:
        for statement in SCHEMA_STATEMENTS:
            try:
                await session.run(statement)
                logger.debug("Schema statement executed", statement=statement[:50])
            except Exception as e:
                # Log but don't fail - constraint might already exist
                logger.warning("Schema statement failed", statement=statement[:50], error=str(e))

    logger.info("Graph schema initialized", constraints=len(SCHEMA_STATEMENTS))


async def create_person(
    session: AsyncSession,
    email: str,
    name: str | None = None,
    org: str | None = None,
    role: str | None = None,
) -> dict:
    """Create or update a Person node."""
    query = """
    MERGE (p:Person {email: $email})
    ON CREATE SET
        p.name = $name,
        p.org = $org,
        p.role = $role,
        p.created_at = datetime(),
        p.updated_at = datetime()
    ON MATCH SET
        p.name = COALESCE($name, p.name),
        p.org = COALESCE($org, p.org),
        p.role = COALESCE($role, p.role),
        p.updated_at = datetime()
    RETURN p
    """
    result = await session.run(query, email=email, name=name, org=org, role=role)
    record = await result.single()
    return dict(record["p"]) if record else {}


async def create_email(
    session: AsyncSession,
    gmail_id: str,
    thread_id: str,
    subject: str,
    date: str,
    snippet: str | None = None,
    body_preview: str | None = None,
    sentiment: str | None = None,
    action_required: bool = False,
    classification: str | None = None,
    urgency: int | None = None,
) -> dict:
    """Create an Email node."""
    query = """
    MERGE (e:Email {gmail_id: $gmail_id})
    ON CREATE SET
        e.thread_id = $thread_id,
        e.subject = $subject,
        e.date = datetime($date),
        e.snippet = $snippet,
        e.body_preview = $body_preview,
        e.sentiment = $sentiment,
        e.action_required = $action_required,
        e.classification = $classification,
        e.urgency = $urgency,
        e.created_at = datetime(),
        e.processed = false
    ON MATCH SET
        e.sentiment = COALESCE($sentiment, e.sentiment),
        e.action_required = COALESCE($action_required, e.action_required),
        e.classification = COALESCE($classification, e.classification),
        e.urgency = COALESCE($urgency, e.urgency)
    RETURN e
    """
    result = await session.run(
        query,
        gmail_id=gmail_id,
        thread_id=thread_id,
        subject=subject,
        date=date,
        snippet=snippet,
        body_preview=body_preview,
        sentiment=sentiment,
        action_required=action_required,
        classification=classification,
        urgency=urgency,
    )
    record = await result.single()
    return dict(record["e"]) if record else {}


async def link_email_sender(
    session: AsyncSession,
    gmail_id: str,
    sender_email: str,
) -> None:
    """Create SENT_BY relationship between Email and Person."""
    query = """
    MATCH (e:Email {gmail_id: $gmail_id})
    MATCH (p:Person {email: $sender_email})
    MERGE (e)-[:SENT_BY]->(p)
    """
    await session.run(query, gmail_id=gmail_id, sender_email=sender_email)


async def link_email_recipient(
    session: AsyncSession,
    gmail_id: str,
    recipient_email: str,
    recipient_type: str = "to",  # 'to', 'cc', 'bcc'
) -> None:
    """Create RECEIVED_BY relationship between Email and Person."""
    query = """
    MATCH (e:Email {gmail_id: $gmail_id})
    MATCH (p:Person {email: $recipient_email})
    MERGE (e)-[:RECEIVED_BY {type: $recipient_type}]->(p)
    """
    await session.run(query, gmail_id=gmail_id, recipient_email=recipient_email, recipient_type=recipient_type)


async def link_email_mentions(
    session: AsyncSession,
    gmail_id: str,
    mentioned_email: str,
) -> None:
    """Create MENTIONED_IN relationship when a person is mentioned in an email."""
    query = """
    MATCH (e:Email {gmail_id: $gmail_id})
    MATCH (p:Person {email: $mentioned_email})
    MERGE (p)-[:MENTIONED_IN]->(e)
    """
    await session.run(query, gmail_id=gmail_id, mentioned_email=mentioned_email)


async def get_person_emails(
    session: AsyncSession,
    person_email: str,
    limit: int = 50,
) -> list[dict]:
    """Get emails sent by or received by a person."""
    query = """
    MATCH (p:Person {email: $person_email})
    OPTIONAL MATCH (e:Email)-[:SENT_BY]->(p)
    OPTIONAL MATCH (e2:Email)-[:RECEIVED_BY]->(p)
    WITH COLLECT(DISTINCT e) + COLLECT(DISTINCT e2) AS emails
    UNWIND emails AS email
    RETURN DISTINCT email
    ORDER BY email.date DESC
    LIMIT $limit
    """
    result = await session.run(query, person_email=person_email, limit=limit)
    records = await result.data()
    return [dict(r["email"]) for r in records if r["email"]]


async def get_unprocessed_emails(
    session: AsyncSession,
    limit: int = 100,
) -> list[dict]:
    """Get emails that haven't been processed by LLM yet."""
    query = """
    MATCH (e:Email)
    WHERE e.processed = false OR e.processed IS NULL
    RETURN e
    ORDER BY e.date DESC
    LIMIT $limit
    """
    result = await session.run(query, limit=limit)
    records = await result.data()
    return [dict(r["e"]) for r in records]


async def mark_email_processed(
    session: AsyncSession,
    gmail_id: str,
    classification: str,
    action_required: bool,
    urgency: int | None = None,
    sentiment: str | None = None,
    inferred_tasks: list[str] | None = None,
) -> None:
    """Mark an email as processed with LLM analysis results."""
    query = """
    MATCH (e:Email {gmail_id: $gmail_id})
    SET e.processed = true,
        e.classification = $classification,
        e.action_required = $action_required,
        e.urgency = $urgency,
        e.sentiment = $sentiment,
        e.inferred_tasks = $inferred_tasks,
        e.processed_at = datetime()
    """
    await session.run(
        query,
        gmail_id=gmail_id,
        classification=classification,
        action_required=action_required,
        urgency=urgency,
        sentiment=sentiment,
        inferred_tasks=inferred_tasks or [],
    )


async def infer_works_with_relationships(session: AsyncSession) -> int:
    """
    Infer WORKS_WITH relationships between people who appear in the same email threads.
    Returns the number of relationships created.
    """
    query = """
    MATCH (e:Email)-[:SENT_BY]->(sender:Person)
    MATCH (e)-[:RECEIVED_BY]->(recipient:Person)
    WHERE sender <> recipient
    MERGE (sender)-[r:WORKS_WITH]->(recipient)
    ON CREATE SET r.first_interaction = e.date, r.interaction_count = 1
    ON MATCH SET r.interaction_count = r.interaction_count + 1,
                 r.last_interaction = e.date
    RETURN COUNT(r) as created
    """
    result = await session.run(query)
    record = await result.single()
    return record["created"] if record else 0


# ============================================================================
# Event (Calendar) operations
# ============================================================================

async def create_event(
    session: AsyncSession,
    gcal_id: str,
    title: str,
    start: str,
    end: str,
    duration_minutes: int,
    event_type: str,
    energy_impact: int,
    is_all_day: bool = False,
    location: str | None = None,
    description: str | None = None,
    organizer_email: str | None = None,
    attendee_count: int = 0,
    is_recurring: bool = False,
    conference_data: bool = False,
) -> dict:
    """Create or update an Event node."""
    query = """
    MERGE (ev:Event {gcal_id: $gcal_id})
    ON CREATE SET
        ev.title = $title,
        ev.start = datetime($start),
        ev.end = datetime($end),
        ev.duration_minutes = $duration_minutes,
        ev.event_type = $event_type,
        ev.energy_impact = $energy_impact,
        ev.is_all_day = $is_all_day,
        ev.location = $location,
        ev.description = $description,
        ev.organizer_email = $organizer_email,
        ev.attendee_count = $attendee_count,
        ev.is_recurring = $is_recurring,
        ev.conference_data = $conference_data,
        ev.created_at = datetime()
    ON MATCH SET
        ev.title = $title,
        ev.start = datetime($start),
        ev.end = datetime($end),
        ev.duration_minutes = $duration_minutes,
        ev.event_type = $event_type,
        ev.energy_impact = $energy_impact,
        ev.updated_at = datetime()
    RETURN ev
    """
    result = await session.run(
        query,
        gcal_id=gcal_id,
        title=title,
        start=start,
        end=end,
        duration_minutes=duration_minutes,
        event_type=event_type,
        energy_impact=energy_impact,
        is_all_day=is_all_day,
        location=location,
        description=description,
        organizer_email=organizer_email,
        attendee_count=attendee_count,
        is_recurring=is_recurring,
        conference_data=conference_data,
    )
    record = await result.single()
    return dict(record["ev"]) if record else {}


async def link_event_attendee(
    session: AsyncSession,
    gcal_id: str,
    attendee_email: str,
    response_status: str = "needsAction",
    is_organizer: bool = False,
) -> None:
    """Create ATTENDED_BY relationship between Event and Person."""
    query = """
    MATCH (ev:Event {gcal_id: $gcal_id})
    MATCH (p:Person {email: $attendee_email})
    MERGE (ev)-[r:ATTENDED_BY]->(p)
    SET r.response_status = $response_status,
        r.is_organizer = $is_organizer
    """
    await session.run(
        query,
        gcal_id=gcal_id,
        attendee_email=attendee_email,
        response_status=response_status,
        is_organizer=is_organizer,
    )


async def link_event_organizer(
    session: AsyncSession,
    gcal_id: str,
    organizer_email: str,
) -> None:
    """Create ORGANIZED_BY relationship between Event and Person."""
    query = """
    MATCH (ev:Event {gcal_id: $gcal_id})
    MATCH (p:Person {email: $organizer_email})
    MERGE (ev)-[:ORGANIZED_BY]->(p)
    """
    await session.run(query, gcal_id=gcal_id, organizer_email=organizer_email)


async def get_upcoming_events(
    session: AsyncSession,
    days_ahead: int = 7,
    limit: int = 50,
) -> list[dict]:
    """Get upcoming events from the graph."""
    query = """
    MATCH (ev:Event)
    WHERE ev.start >= datetime() AND ev.start <= datetime() + duration({days: $days_ahead})
    RETURN ev
    ORDER BY ev.start ASC
    LIMIT $limit
    """
    result = await session.run(query, days_ahead=days_ahead, limit=limit)
    records = await result.data()
    return [dict(r["ev"]) for r in records]


async def get_today_events(session: AsyncSession) -> list[dict]:
    """Get today's events from the graph."""
    query = """
    MATCH (ev:Event)
    WHERE date(ev.start) = date()
    RETURN ev
    ORDER BY ev.start ASC
    """
    result = await session.run(query)
    records = await result.data()
    return [dict(r["ev"]) for r in records]


async def get_daily_energy_forecast(session: AsyncSession, date_str: str | None = None) -> dict:
    """
    Calculate energy forecast for a day based on scheduled events.

    Returns dict with total_energy_cost, event_count, and breakdown by type.
    """
    date_filter = f"date(ev.start) = date('{date_str}')" if date_str else "date(ev.start) = date()"

    query = f"""
    MATCH (ev:Event)
    WHERE {date_filter}
    RETURN
        sum(ev.energy_impact) as total_energy_cost,
        count(ev) as event_count,
        sum(ev.duration_minutes) as total_minutes,
        collect({{type: ev.event_type, energy: ev.energy_impact, title: ev.title}}) as events
    """
    result = await session.run(query)
    record = await result.single()

    if record:
        return {
            "total_energy_cost": record["total_energy_cost"] or 0,
            "event_count": record["event_count"] or 0,
            "total_minutes": record["total_minutes"] or 0,
            "events": record["events"] or [],
        }
    return {"total_energy_cost": 0, "event_count": 0, "total_minutes": 0, "events": []}


# ============================================================================
# Task operations
# ============================================================================

async def create_task(
    session: AsyncSession,
    task_id: str,
    title: str,
    description: str | None = None,
    status: str = "pending",
    energy_cost: int = 3,
    due_date: str | None = None,
    source_type: str | None = None,
    source_id: str | None = None,
) -> dict:
    """Create a Task node in the graph."""
    query = """
    MERGE (t:Task {id: $task_id})
    ON CREATE SET
        t.title = $title,
        t.description = $description,
        t.status = $status,
        t.energy_cost = $energy_cost,
        t.due = CASE WHEN $due_date IS NOT NULL THEN date($due_date) ELSE null END,
        t.source_type = $source_type,
        t.source_id = $source_id,
        t.created_at = datetime()
    ON MATCH SET
        t.title = $title,
        t.description = COALESCE($description, t.description),
        t.updated_at = datetime()
    RETURN t
    """
    result = await session.run(
        query,
        task_id=task_id,
        title=title,
        description=description,
        status=status,
        energy_cost=energy_cost,
        due_date=due_date,
        source_type=source_type,
        source_id=source_id,
    )
    record = await result.single()
    return dict(record["t"]) if record else {}


async def link_task_to_email(
    session: AsyncSession,
    task_id: str,
    gmail_id: str,
) -> None:
    """Create DERIVED_FROM relationship between Task and Email."""
    query = """
    MATCH (t:Task {id: $task_id})
    MATCH (e:Email {gmail_id: $gmail_id})
    MERGE (t)-[:DERIVED_FROM]->(e)
    """
    await session.run(query, task_id=task_id, gmail_id=gmail_id)


async def link_task_to_person(
    session: AsyncSession,
    task_id: str,
    person_email: str,
    relationship_type: str = "ASSIGNED_TO",
) -> None:
    """Create relationship between Task and Person."""
    query = f"""
    MATCH (t:Task {{id: $task_id}})
    MATCH (p:Person {{email: $person_email}})
    MERGE (t)-[:{relationship_type}]->(p)
    """
    await session.run(query, task_id=task_id, person_email=person_email)


async def get_actionable_emails(
    session: AsyncSession,
    limit: int = 50,
) -> list[dict]:
    """Get emails marked as actionable that haven't had tasks created yet."""
    query = """
    MATCH (e:Email)
    WHERE e.classification = 'actionable'
      AND e.action_required = true
      AND NOT (e)<-[:DERIVED_FROM]-(:Task)
    OPTIONAL MATCH (e)-[:SENT_BY]->(sender:Person)
    RETURN e, sender.email as sender_email, sender.name as sender_name
    ORDER BY e.date DESC
    LIMIT $limit
    """
    result = await session.run(query, limit=limit)
    records = await result.data()
    return [
        {
            **dict(r["e"]),
            "sender_email": r["sender_email"],
            "sender_name": r["sender_name"],
        }
        for r in records
    ]


async def get_tasks(
    session: AsyncSession,
    status: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Get tasks, optionally filtered by status."""
    status_filter = "WHERE t.status = $status" if status else ""
    query = f"""
    MATCH (t:Task)
    {status_filter}
    OPTIONAL MATCH (t)-[:DERIVED_FROM]->(e:Email)
    OPTIONAL MATCH (e)-[:SENT_BY]->(sender:Person)
    RETURN t, e.subject as source_subject, sender.email as from_email
    ORDER BY t.due ASC, t.energy_cost DESC
    LIMIT $limit
    """
    result = await session.run(query, status=status, limit=limit)
    records = await result.data()
    return [
        {
            **dict(r["t"]),
            "source_subject": r["source_subject"],
            "from_email": r["from_email"],
        }
        for r in records
    ]


async def update_task_status(
    session: AsyncSession,
    task_id: str,
    status: str,
) -> None:
    """Update task status."""
    query = """
    MATCH (t:Task {id: $task_id})
    SET t.status = $status, t.updated_at = datetime()
    """
    await session.run(query, task_id=task_id, status=status)


async def get_contacts_for_enrichment(
    session: AsyncSession,
    limit: int = 50,
) -> list[dict]:
    """Get contacts that haven't been enriched yet, prioritized by interaction count."""
    query = """
    MATCH (p:Person)
    WHERE p.enriched IS NULL OR p.enriched = false
    OPTIONAL MATCH (p)<-[:SENT_BY]-(sent:Email)
    OPTIONAL MATCH (p)<-[:RECEIVED_BY]-(received:Email)
    OPTIONAL MATCH (p)<-[:ATTENDED_BY]-(ev:Event)
    WITH p,
         count(DISTINCT sent) as emails_sent,
         count(DISTINCT received) as emails_received,
         count(DISTINCT ev) as events_attended,
         collect(DISTINCT sent.snippet)[0..3] as sample_snippets
    WHERE emails_sent > 0 OR emails_received > 0 OR events_attended > 0
    RETURN p.email as email, p.name as name,
           emails_sent, emails_received, events_attended,
           sample_snippets
    ORDER BY emails_sent + events_attended DESC
    LIMIT $limit
    """
    result = await session.run(query, limit=limit)
    records = await result.data()
    return records


async def update_person_enrichment(
    session: AsyncSession,
    email: str,
    org: str | None = None,
    role: str | None = None,
    communication_style: str | None = None,
    urgency_tendency: str | None = None,
) -> None:
    """Update a Person node with enrichment data."""
    query = """
    MATCH (p:Person {email: $email})
    SET p.org = COALESCE($org, p.org),
        p.role = COALESCE($role, p.role),
        p.communication_style = $communication_style,
        p.urgency_tendency = $urgency_tendency,
        p.enriched = true,
        p.enriched_at = datetime()
    """
    await session.run(
        query,
        email=email,
        org=org,
        role=role,
        communication_style=communication_style,
        urgency_tendency=urgency_tendency,
    )


async def get_graph_stats(session: AsyncSession) -> dict:
    """Get statistics about the graph."""
    query = """
    MATCH (n)
    WITH labels(n)[0] as label, count(n) as count
    RETURN collect({label: label, count: count}) as node_counts
    """
    result = await session.run(query)
    record = await result.single()
    node_counts = {item["label"]: item["count"] for item in record["node_counts"]} if record else {}

    rel_query = """
    MATCH ()-[r]->()
    WITH type(r) as rel_type, count(r) as count
    RETURN collect({type: rel_type, count: count}) as rel_counts
    """
    rel_result = await session.run(rel_query)
    rel_record = await rel_result.single()
    rel_counts = {item["type"]: item["count"] for item in rel_record["rel_counts"]} if rel_record else {}

    return {"nodes": node_counts, "relationships": rel_counts}


# ============================================================================
# Document (Drive) operations
# ============================================================================

async def create_document(
    session: AsyncSession,
    drive_id: str,
    name: str,
    mime_type: str,
    modified_at: str,
    folder_path: str | None = None,
    size_bytes: int | None = None,
    web_link: str | None = None,
    owner_email: str | None = None,
    is_shared: bool = False,
    indexed: bool = False,
    content_hash: str | None = None,
) -> dict:
    """Create or update a Document node."""
    query = """
    MERGE (d:Document {drive_id: $drive_id})
    ON CREATE SET
        d.name = $name,
        d.mime_type = $mime_type,
        d.modified_at = datetime($modified_at),
        d.folder_path = $folder_path,
        d.size_bytes = $size_bytes,
        d.web_link = $web_link,
        d.owner_email = $owner_email,
        d.is_shared = $is_shared,
        d.indexed = $indexed,
        d.content_hash = $content_hash,
        d.created_at = datetime()
    ON MATCH SET
        d.name = $name,
        d.mime_type = $mime_type,
        d.modified_at = datetime($modified_at),
        d.folder_path = $folder_path,
        d.size_bytes = $size_bytes,
        d.web_link = $web_link,
        d.is_shared = $is_shared,
        d.updated_at = datetime()
    RETURN d
    """
    result = await session.run(
        query,
        drive_id=drive_id,
        name=name,
        mime_type=mime_type,
        modified_at=modified_at,
        folder_path=folder_path,
        size_bytes=size_bytes,
        web_link=web_link,
        owner_email=owner_email,
        is_shared=is_shared,
        indexed=indexed,
        content_hash=content_hash,
    )
    record = await result.single()
    return dict(record["d"]) if record else {}


async def link_document_owner(
    session: AsyncSession,
    drive_id: str,
    owner_email: str,
) -> None:
    """Create OWNED_BY relationship between Document and Person."""
    query = """
    MATCH (d:Document {drive_id: $drive_id})
    MERGE (p:Person {email: $owner_email})
    MERGE (d)-[:OWNED_BY]->(p)
    """
    await session.run(query, drive_id=drive_id, owner_email=owner_email)


async def link_document_shared_with(
    session: AsyncSession,
    drive_id: str,
    shared_with_email: str,
    role: str = "reader",
) -> None:
    """Create SHARED_WITH relationship between Document and Person."""
    query = """
    MATCH (d:Document {drive_id: $drive_id})
    MERGE (p:Person {email: $shared_with_email})
    MERGE (d)-[r:SHARED_WITH]->(p)
    SET r.role = $role
    """
    await session.run(query, drive_id=drive_id, shared_with_email=shared_with_email, role=role)


async def mark_document_indexed(
    session: AsyncSession,
    drive_id: str,
    content_hash: str | None = None,
    embedding_id: int | None = None,
) -> None:
    """Mark a document as indexed with optional embedding reference."""
    query = """
    MATCH (d:Document {drive_id: $drive_id})
    SET d.indexed = true,
        d.content_hash = $content_hash,
        d.embedding_id = $embedding_id,
        d.indexed_at = datetime()
    """
    await session.run(query, drive_id=drive_id, content_hash=content_hash, embedding_id=embedding_id)


async def get_documents(
    session: AsyncSession,
    folder_path: str | None = None,
    indexed_only: bool = False,
    limit: int = 50,
) -> list[dict]:
    """Get documents, optionally filtered by folder or indexed status."""
    filters = []
    if folder_path:
        filters.append(f"d.folder_path STARTS WITH '{folder_path}'")
    if indexed_only:
        filters.append("d.indexed = true")

    where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""

    query = f"""
    MATCH (d:Document)
    {where_clause}
    OPTIONAL MATCH (d)-[:OWNED_BY]->(owner:Person)
    RETURN d, owner.email as owner_email
    ORDER BY d.modified_at DESC
    LIMIT $limit
    """
    result = await session.run(query, limit=limit)
    records = await result.data()
    return [
        {
            **dict(r["d"]),
            "owner_email": r["owner_email"],
        }
        for r in records
    ]


async def get_documents_for_person(
    session: AsyncSession,
    person_email: str,
    limit: int = 50,
) -> list[dict]:
    """Get documents owned by or shared with a person."""
    query = """
    MATCH (p:Person {email: $person_email})
    OPTIONAL MATCH (d1:Document)-[:OWNED_BY]->(p)
    OPTIONAL MATCH (d2:Document)-[:SHARED_WITH]->(p)
    WITH COLLECT(DISTINCT d1) + COLLECT(DISTINCT d2) AS docs
    UNWIND docs AS d
    WHERE d IS NOT NULL
    RETURN DISTINCT d
    ORDER BY d.modified_at DESC
    LIMIT $limit
    """
    result = await session.run(query, person_email=person_email, limit=limit)
    records = await result.data()
    return [dict(r["d"]) for r in records if r["d"]]


async def get_unindexed_documents_in_folders(
    session: AsyncSession,
    folder_prefixes: list[str],
    limit: int = 100,
) -> list[dict]:
    """Get documents in priority folders that haven't been indexed yet."""
    # Build OR conditions for folder paths
    path_conditions = " OR ".join([f"d.folder_path STARTS WITH '{fp}'" for fp in folder_prefixes])

    query = f"""
    MATCH (d:Document)
    WHERE (d.indexed IS NULL OR d.indexed = false)
      AND ({path_conditions})
    RETURN d
    ORDER BY d.modified_at DESC
    LIMIT $limit
    """
    result = await session.run(query, limit=limit)
    records = await result.data()
    return [dict(r["d"]) for r in records]


async def get_document_stats(session: AsyncSession) -> dict:
    """Get statistics about documents in the graph."""
    query = """
    MATCH (d:Document)
    WITH count(d) as total,
         sum(CASE WHEN d.indexed = true THEN 1 ELSE 0 END) as indexed_count,
         sum(CASE WHEN d.is_shared = true THEN 1 ELSE 0 END) as shared_count
    RETURN total, indexed_count, shared_count
    """
    result = await session.run(query)
    record = await result.single()

    folder_query = """
    MATCH (d:Document)
    WHERE d.folder_path IS NOT NULL
    WITH split(d.folder_path, '/')[0] as root_folder, count(d) as count
    RETURN collect({folder: root_folder, count: count}) as folder_counts
    """
    folder_result = await session.run(folder_query)
    folder_record = await folder_result.single()
    folder_counts = {
        item["folder"]: item["count"]
        for item in (folder_record["folder_counts"] if folder_record else [])
        if item["folder"]
    }

    return {
        "total": record["total"] if record else 0,
        "indexed": record["indexed_count"] if record else 0,
        "shared": record["shared_count"] if record else 0,
        "by_folder": folder_counts,
    }
