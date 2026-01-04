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
    "CREATE CONSTRAINT repository_id IF NOT EXISTS FOR (r:Repository) REQUIRE r.id IS UNIQUE",
    "CREATE CONSTRAINT repository_full_name IF NOT EXISTS FOR (r:Repository) REQUIRE r.full_name IS UNIQUE",
    "CREATE CONSTRAINT codefile_id IF NOT EXISTS FOR (cf:CodeFile) REQUIRE cf.id IS UNIQUE",
    # Chunk, Topic, Concept constraints for semantic graph
    "CREATE CONSTRAINT chunk_id IF NOT EXISTS FOR (c:Chunk) REQUIRE c.id IS UNIQUE",
    "CREATE CONSTRAINT topic_name IF NOT EXISTS FOR (t:Topic) REQUIRE t.name IS UNIQUE",
    "CREATE CONSTRAINT concept_name IF NOT EXISTS FOR (c:Concept) REQUIRE c.name IS UNIQUE",

    # Additional indexes for common queries
    "CREATE INDEX person_name IF NOT EXISTS FOR (p:Person) ON (p.name)",
    "CREATE INDEX person_org IF NOT EXISTS FOR (p:Person) ON (p.org)",
    "CREATE INDEX email_date IF NOT EXISTS FOR (e:Email) ON (e.date)",
    "CREATE INDEX email_thread_id IF NOT EXISTS FOR (e:Email) ON (e.thread_id)",
    "CREATE INDEX email_action_required IF NOT EXISTS FOR (e:Email) ON (e.action_required)",
    "CREATE INDEX event_start IF NOT EXISTS FOR (ev:Event) ON (ev.start)",
    "CREATE INDEX task_status IF NOT EXISTS FOR (t:Task) ON (t.status)",
    "CREATE INDEX task_due IF NOT EXISTS FOR (t:Task) ON (t.due_date)",
    "CREATE INDEX task_priority IF NOT EXISTS FOR (t:Task) ON (t.priority)",
    "CREATE INDEX project_status IF NOT EXISTS FOR (p:Project) ON (p.status)",
    "CREATE INDEX goal_timeframe IF NOT EXISTS FOR (g:Goal) ON (g.timeframe)",
    "CREATE INDEX goal_status IF NOT EXISTS FOR (g:Goal) ON (g.status)",
    "CREATE INDEX document_name IF NOT EXISTS FOR (d:Document) ON (d.name)",
    "CREATE INDEX document_modified IF NOT EXISTS FOR (d:Document) ON (d.modified_at)",
    "CREATE INDEX document_folder IF NOT EXISTS FOR (d:Document) ON (d.folder_path)",
    "CREATE INDEX document_indexed IF NOT EXISTS FOR (d:Document) ON (d.indexed)",
    "CREATE INDEX repository_name IF NOT EXISTS FOR (r:Repository) ON (r.name)",
    "CREATE INDEX codefile_path IF NOT EXISTS FOR (cf:CodeFile) ON (cf.path)",
    "CREATE INDEX codefile_language IF NOT EXISTS FOR (cf:CodeFile) ON (cf.language)",
    # Chunk indexes for semantic search
    "CREATE INDEX chunk_drive_id IF NOT EXISTS FOR (c:Chunk) ON (c.drive_id)",
    "CREATE INDEX chunk_index IF NOT EXISTS FOR (c:Chunk) ON (c.chunk_index)",
    "CREATE INDEX chunk_content_type IF NOT EXISTS FOR (c:Chunk) ON (c.content_type)",
    "CREATE INDEX chunk_analyzed IF NOT EXISTS FOR (c:Chunk) ON (c.analyzed)",
    # CodingSession constraints and indexes (CLI session ingestion)
    "CREATE CONSTRAINT coding_session_id IF NOT EXISTS FOR (cs:CodingSession) REQUIRE cs.session_id IS UNIQUE",
    "CREATE INDEX coding_session_project IF NOT EXISTS FOR (cs:CodingSession) ON (cs.project_path)",
    "CREATE INDEX coding_session_ended IF NOT EXISTS FOR (cs:CodingSession) ON (cs.ended_at)",
    "CREATE INDEX coding_session_cli IF NOT EXISTS FOR (cs:CodingSession) ON (cs.cli_type)",
]


async def init_graph_schema() -> None:
    """Initialize the Neo4j graph schema with constraints and indexes."""
    from neo4j import WRITE_ACCESS

    driver = get_driver()

    async with driver.session(default_access_mode=WRITE_ACCESS) as session:
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
    is_user: bool = False,
) -> dict:
    """Create or update a Person node.

    Args:
        is_user: If True, marks this person as the authenticated user (for writing style learning)
    """
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
        p.is_user = CASE WHEN $is_user THEN true ELSE p.is_user END,
        p.updated_at = datetime()
    RETURN p
    """
    result = await session.run(query, email=email, name=name, org=org, role=role, is_user=is_user)
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
    # Use parameterized query to prevent Cypher injection
    if date_str:
        query = """
        MATCH (ev:Event)
        WHERE date(ev.start) = date($date_str)
        RETURN
            sum(ev.energy_impact) as total_energy_cost,
            count(ev) as event_count,
            sum(ev.duration_minutes) as total_minutes,
            collect({type: ev.event_type, energy: ev.energy_impact, title: ev.title}) as events
        """
        result = await session.run(query, date_str=date_str)
    else:
        query = """
        MATCH (ev:Event)
        WHERE date(ev.start) = date()
        RETURN
            sum(ev.energy_impact) as total_energy_cost,
            count(ev) as event_count,
            sum(ev.duration_minutes) as total_minutes,
            collect({type: ev.event_type, energy: ev.energy_impact, title: ev.title}) as events
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
    priority: str = "medium",
    due_date: str | None = None,
    effort_estimate: float | None = None,
    energy_cost: str | None = None,
    source_type: str | None = None,
    source_id: str | None = None,
) -> dict:
    """
    Create a Task node in the graph.

    Args:
        task_id: Unique identifier (UUID)
        title: Task title
        description: Optional detailed description
        status: pending, in_progress, completed, cancelled
        priority: high, medium, low
        due_date: Optional deadline (ISO date string)
        effort_estimate: Estimated hours to complete
        energy_cost: high, medium, low - cognitive load
        source_type: Where task originated (email, meeting, manual)
        source_id: ID of source entity
    """
    query = """
    MERGE (t:Task {id: $task_id})
    ON CREATE SET
        t.title = $title,
        t.description = $description,
        t.status = $status,
        t.priority = $priority,
        t.due_date = CASE WHEN $due_date IS NOT NULL THEN datetime($due_date) ELSE null END,
        t.effort_estimate = $effort_estimate,
        t.energy_cost = $energy_cost,
        t.source_type = $source_type,
        t.source_id = $source_id,
        t.created_at = datetime(),
        t.updated_at = datetime()
    ON MATCH SET
        t.title = $title,
        t.description = COALESCE($description, t.description),
        t.status = COALESCE($status, t.status),
        t.priority = COALESCE($priority, t.priority),
        t.due_date = CASE WHEN $due_date IS NOT NULL THEN datetime($due_date) ELSE t.due_date END,
        t.effort_estimate = COALESCE($effort_estimate, t.effort_estimate),
        t.energy_cost = COALESCE($energy_cost, t.energy_cost),
        t.updated_at = datetime()
    RETURN t
    """
    result = await session.run(
        query,
        task_id=task_id,
        title=title,
        description=description,
        status=status,
        priority=priority,
        due_date=due_date,
        effort_estimate=effort_estimate,
        energy_cost=energy_cost,
        source_type=source_type,
        source_id=source_id,
    )
    record = await result.single()
    return dict(record["t"]) if record else {}


async def update_task(
    session: AsyncSession,
    task_id: str,
    title: str | None = None,
    description: str | None = None,
    status: str | None = None,
    priority: str | None = None,
    due_date: str | None = None,
    effort_estimate: float | None = None,
    energy_cost: str | None = None,
) -> dict:
    """Update specific fields of a task."""
    # Build SET clause dynamically for non-None values
    set_parts = ["t.updated_at = datetime()"]
    params = {"task_id": task_id}

    if title is not None:
        set_parts.append("t.title = $title")
        params["title"] = title
    if description is not None:
        set_parts.append("t.description = $description")
        params["description"] = description
    if status is not None:
        set_parts.append("t.status = $status")
        params["status"] = status
    if priority is not None:
        set_parts.append("t.priority = $priority")
        params["priority"] = priority
    if due_date is not None:
        set_parts.append("t.due_date = datetime($due_date)")
        params["due_date"] = due_date
    if effort_estimate is not None:
        set_parts.append("t.effort_estimate = $effort_estimate")
        params["effort_estimate"] = effort_estimate
    if energy_cost is not None:
        set_parts.append("t.energy_cost = $energy_cost")
        params["energy_cost"] = energy_cost

    query = f"""
    MATCH (t:Task {{id: $task_id}})
    SET {', '.join(set_parts)}
    RETURN t
    """
    result = await session.run(query, **params)
    record = await result.single()
    return dict(record["t"]) if record else {}


async def link_task_to_email(
    session: AsyncSession,
    task_id: str,
    gmail_id: str,
) -> None:
    """Create ORIGINATED_FROM relationship between Task and Email."""
    query = """
    MATCH (t:Task {id: $task_id})
    MATCH (e:Email {gmail_id: $gmail_id})
    MERGE (t)-[:ORIGINATED_FROM]->(e)
    """
    await session.run(query, task_id=task_id, gmail_id=gmail_id)


async def link_task_to_person(
    session: AsyncSession,
    task_id: str,
    person_email: str,
    relationship_type: str = "ASSIGNED_TO",
) -> bool:
    """Create relationship between Task and Person (ASSIGNED_TO, INVOLVES)."""
    query = f"""
    MATCH (t:Task {{id: $task_id}})
    MATCH (p:Person {{email: $person_email}})
    MERGE (t)-[:{relationship_type}]->(p)
    RETURN t.id as id
    """
    result = await session.run(query, task_id=task_id, person_email=person_email)
    record = await result.single()
    return record is not None


async def link_task_to_project(
    session: AsyncSession,
    task_id: str,
    project_id: str,
) -> bool:
    """Create PART_OF relationship between Task and Project.

    Also updates the project's updated_at timestamp to prevent the autonomous
    agent from seeing the project as "stale" immediately after adding a task.
    """
    query = """
    MATCH (t:Task {id: $task_id})
    MATCH (p:Project {id: $project_id})
    MERGE (t)-[:PART_OF]->(p)
    SET p.updated_at = datetime()
    RETURN t.id as id
    """
    result = await session.run(query, task_id=task_id, project_id=project_id)
    record = await result.single()
    return record is not None


async def link_task_to_goal(
    session: AsyncSession,
    task_id: str,
    goal_id: str,
) -> bool:
    """Create CONTRIBUTES_TO relationship between Task and Goal."""
    query = """
    MATCH (t:Task {id: $task_id})
    MATCH (g:Goal {id: $goal_id})
    MERGE (t)-[:CONTRIBUTES_TO]->(g)
    RETURN t.id as id
    """
    result = await session.run(query, task_id=task_id, goal_id=goal_id)
    record = await result.single()
    return record is not None


async def link_task_to_event(
    session: AsyncSession,
    task_id: str,
    gcal_id: str,
) -> bool:
    """Create DISCUSSED_IN relationship between Task and Event."""
    query = """
    MATCH (t:Task {id: $task_id})
    MATCH (e:Event {gcal_id: $gcal_id})
    MERGE (t)-[:DISCUSSED_IN]->(e)
    RETURN t.id as id
    """
    result = await session.run(query, task_id=task_id, gcal_id=gcal_id)
    record = await result.single()
    return record is not None


async def link_task_to_document(
    session: AsyncSession,
    task_id: str,
    drive_id: str,
) -> bool:
    """Create REFERENCES relationship between Task and Document."""
    query = """
    MATCH (t:Task {id: $task_id})
    MATCH (d:Document {drive_id: $drive_id})
    MERGE (t)-[:REFERENCES]->(d)
    RETURN t.id as id
    """
    result = await session.run(query, task_id=task_id, drive_id=drive_id)
    record = await result.single()
    return record is not None


async def link_task_to_codefile(
    session: AsyncSession,
    task_id: str,
    codefile_id: str,
) -> bool:
    """Create INVOLVES_CODE relationship between Task and CodeFile."""
    query = """
    MATCH (t:Task {id: $task_id})
    MATCH (cf:CodeFile {id: $codefile_id})
    MERGE (t)-[:INVOLVES_CODE]->(cf)
    RETURN t.id as id
    """
    result = await session.run(query, task_id=task_id, codefile_id=codefile_id)
    record = await result.single()
    return record is not None


async def link_task_blocked_by(
    session: AsyncSession,
    task_id: str,
    blocking_task_id: str,
) -> bool:
    """Create BLOCKED_BY relationship between Tasks."""
    query = """
    MATCH (t:Task {id: $task_id})
    MATCH (blocker:Task {id: $blocking_task_id})
    MERGE (t)-[:BLOCKED_BY]->(blocker)
    RETURN t.id as id
    """
    result = await session.run(query, task_id=task_id, blocking_task_id=blocking_task_id)
    record = await result.single()
    return record is not None


async def get_tasks_by_email_thread(
    session: AsyncSession,
    thread_id: str,
) -> list[dict]:
    """
    Find all pending tasks that originated from emails in a given thread.

    Args:
        thread_id: Gmail thread ID

    Returns:
        List of tasks linked to emails in this thread
    """
    query = """
    MATCH (e:Email {thread_id: $thread_id})<-[:ORIGINATED_FROM]-(t:Task)
    WHERE t.status IN ['pending', 'in_progress', 'waiting']
    RETURN t.id as id, t.title as title, t.status as status,
           t.priority as priority, t.description as description,
           e.gmail_id as source_email_id, e.subject as source_subject
    ORDER BY t.created_at DESC
    """
    result = await session.run(query, thread_id=thread_id)
    records = await result.data()
    return records


async def get_tasks_by_source_email(
    session: AsyncSession,
    gmail_id: str,
) -> list[dict]:
    """
    Find all pending tasks that originated from a specific email.

    Args:
        gmail_id: Gmail message ID

    Returns:
        List of tasks linked to this email
    """
    query = """
    MATCH (t:Task)
    WHERE t.source_type = 'email' AND t.source_id = $gmail_id
      AND t.status IN ['pending', 'in_progress', 'waiting']
    RETURN t.id as id, t.title as title, t.status as status,
           t.priority as priority, t.description as description
    ORDER BY t.created_at DESC
    """
    result = await session.run(query, gmail_id=gmail_id)
    records = await result.data()
    return records


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


async def get_task(
    session: AsyncSession,
    task_id: str,
) -> dict | None:
    """Get a single task by ID with all its relationships and full context."""
    query = """
    MATCH (t:Task {id: $task_id})
    // Try relationship first, then fall back to source_id property
    OPTIONAL MATCH (t)-[:ORIGINATED_FROM]->(e_rel:Email)
    OPTIONAL MATCH (e_prop:Email {gmail_id: t.source_id})
    WHERE t.source_type = 'email'
    WITH t, COALESCE(e_rel, e_prop) as e
    OPTIONAL MATCH (e)-[:SENT_BY]->(sender:Person)
    // Event source (relationship or property)
    OPTIONAL MATCH (t)-[:DISCUSSED_IN]->(ev_rel:Event)
    OPTIONAL MATCH (ev_prop:Event {gcal_id: t.source_id})
    WHERE t.source_type = 'event'
    WITH t, e, sender, COALESCE(ev_rel, ev_prop) as ev
    // Other relationships
    OPTIONAL MATCH (t)-[pr:PART_OF|BELONGS_TO]->(p:Project)
    OPTIONAL MATCH (t)-[:CONTRIBUTES_TO]->(g:Goal)
    OPTIONAL MATCH (t)-[assigned:ASSIGNED_TO|INVOLVES]->(assignee:Person)
    OPTIONAL MATCH (t)-[:REFERENCES]->(d:Document)
    OPTIONAL MATCH (t)-[:INVOLVES_CODE]->(cf:CodeFile)
    OPTIONAL MATCH (cf)-[:CONTAINED_IN]->(repo:Repository)
    OPTIONAL MATCH (t)-[:BLOCKED_BY]->(blocker:Task)
    RETURN t,
           e {
               .gmail_id, .subject, .snippet, .date,
               sender_email: sender.email,
               sender_name: sender.name
           } as source_email,
           ev {.gcal_id, .title, .start, .end} as source_event,
           collect(DISTINCT {id: p.id, title: p.title, status: p.status, created_by: pr.created_by}) as projects,
           collect(DISTINCT g {.id, .title, .timeframe}) as goals,
           collect(DISTINCT {
               email: assignee.email,
               name: assignee.name,
               role: assigned.role
           }) as people,
           collect(DISTINCT d {.drive_id, .name, .mime_type}) as documents,
           collect(DISTINCT {
               id: cf.id,
               path: cf.path,
               name: cf.name,
               language: cf.language,
               repo: repo.full_name
           }) as codefiles,
           collect(DISTINCT blocker {.id, .title, .status}) as blocked_by
    """
    result = await session.run(query, task_id=task_id)
    record = await result.single()
    if not record:
        return None

    task_data = dict(record["t"])

    # Clean up the collected data (remove empty entries)
    projects = [p for p in record["projects"] if p.get("id")]
    goals = [g for g in record["goals"] if g.get("id")]
    people = [p for p in record["people"] if p.get("email")]
    documents = [d for d in record["documents"] if d.get("drive_id")]
    codefiles = [c for c in record["codefiles"] if c.get("id")]
    blocked_by = [b for b in record["blocked_by"] if b.get("id")]

    # Create display-friendly people list (names or emails)
    people_display = [p.get("name") or p.get("email") for p in people]
    people_emails = [p.get("email") for p in people if p.get("email")]

    # Add due date alias (templates use 'due')
    due_date = task_data.get("due_date")
    due_str = str(due_date) if due_date else None

    return {
        **task_data,
        "due": due_str,  # Alias for template compatibility
        "source_email": record["source_email"] if record["source_email"] and record["source_email"].get("gmail_id") else None,
        "source_event": record["source_event"] if record["source_event"] and record["source_event"].get("gcal_id") else None,
        "projects": projects,
        "goals": goals,
        "people": people_display,  # Display-friendly names/emails
        "people_emails": people_emails,  # For form selection
        "people_full": people,  # Full info including roles
        "documents": documents,
        "codefiles": codefiles,
        "blocked_by": blocked_by,
        # Convenience fields for single project/goal
        "project": projects[0]["title"] if projects else None,
        "project_id": projects[0]["id"] if projects else None,
        "goal": goals[0]["title"] if goals else None,
        "goal_id": goals[0]["id"] if goals else None,
    }


async def get_tasks(
    session: AsyncSession,
    status: str | None = None,
    priority: str | None = None,
    project_id: str | None = None,
    include_completed: bool = False,
    limit: int = 50,
) -> list[dict]:
    """Get tasks with optional filters."""
    filters = []
    params = {"limit": limit}

    if status:
        filters.append("t.status = $status")
        params["status"] = status
    elif not include_completed:
        # Filter out completed tasks - handle both 'done' and 'completed' status values
        filters.append("NOT t.status IN ['done', 'completed', 'cancelled']")

    if priority:
        filters.append("t.priority = $priority")
        params["priority"] = priority

    where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""

    # Add project filter via relationship if needed
    project_match = ""
    if project_id:
        project_match = "MATCH (t)-[:PART_OF]->(proj:Project {id: $project_id})"
        params["project_id"] = project_id

    query = f"""
    MATCH (t:Task)
    {project_match}
    {where_clause}
    OPTIONAL MATCH (t)-[:ORIGINATED_FROM]->(e:Email)
    OPTIONAL MATCH (t)-[:PART_OF]->(p:Project)
    OPTIONAL MATCH (t)-[:ASSIGNED_TO]->(assignee:Person)
    RETURN t,
           e.subject as source_subject,
           collect(DISTINCT p.title)[0] as project_name,
           collect(DISTINCT assignee.email) as assignees
    ORDER BY
        CASE t.priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
        t.due_date ASC,
        t.created_at DESC
    LIMIT $limit
    """
    result = await session.run(query, **params)
    records = await result.data()

    tasks = []
    for r in records:
        task_data = dict(r["t"])
        # Add convenience alias for due_date (templates use 'due')
        due_date = task_data.get("due_date")
        if due_date:
            # Convert Neo4j datetime to string if needed
            due_str = str(due_date) if due_date else None
        else:
            due_str = None

        tasks.append({
            **task_data,
            "due": due_str,  # Alias for template compatibility
            "source_subject": r["source_subject"],
            "project": r["project_name"],  # Alias for template compatibility
            "project_name": r["project_name"],
            "people": [a for a in r["assignees"] if a],  # Alias for template
            "assignees": [a for a in r["assignees"] if a],
        })

    return tasks


async def delete_task(
    session: AsyncSession,
    task_id: str,
) -> bool:
    """Delete a task and all its relationships."""
    query = """
    MATCH (t:Task {id: $task_id})
    DETACH DELETE t
    RETURN count(t) as deleted
    """
    result = await session.run(query, task_id=task_id)
    record = await result.single()
    return record["deleted"] > 0 if record else False


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


async def get_todays_events(session: AsyncSession) -> list[dict]:
    """Get all events scheduled for today."""
    query = """
    MATCH (ev:Event)
    WHERE date(ev.start) = date()
    RETURN ev.gcal_id as gcal_id,
           ev.title as title,
           ev.start as start,
           ev.end as end,
           ev.duration_minutes as duration_minutes,
           ev.event_type as event_type,
           ev.energy_impact as energy_impact,
           ev.location as location,
           ev.attendee_count as attendee_count
    ORDER BY ev.start ASC
    """
    result = await session.run(query)
    records = await result.data()
    return records


async def get_upcoming_events(
    session: AsyncSession,
    days_ahead: int = 7,
    limit: int = 50,
) -> list[dict]:
    """Get upcoming events for the next N days."""
    query = """
    MATCH (ev:Event)
    WHERE date(ev.start) >= date()
      AND date(ev.start) <= date() + duration({days: $days_ahead})
    RETURN ev.gcal_id as gcal_id,
           ev.title as title,
           ev.start as start,
           ev.end as end,
           ev.duration_minutes as duration_minutes,
           ev.event_type as event_type,
           ev.energy_impact as energy_impact,
           ev.location as location,
           ev.attendee_count as attendee_count
    ORDER BY ev.start ASC
    LIMIT $limit
    """
    result = await session.run(query, days_ahead=days_ahead, limit=limit)
    records = await result.data()
    return records


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
    # Build parameterized query to prevent Cypher injection
    params = {"limit": limit}
    filters = []

    if folder_path:
        filters.append("d.folder_path STARTS WITH $folder_path")
        params["folder_path"] = folder_path
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
    result = await session.run(query, **params)
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
    # Use parameterized query with list unwind to prevent Cypher injection
    query = """
    MATCH (d:Document)
    WHERE (d.indexed IS NULL OR d.indexed = false)
      AND any(prefix IN $folder_prefixes WHERE d.folder_path STARTS WITH prefix)
    RETURN d
    ORDER BY d.modified_at DESC
    LIMIT $limit
    """
    result = await session.run(query, folder_prefixes=folder_prefixes, limit=limit)
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


# ============================================================================
# Project operations
# ============================================================================

async def create_project(
    session: AsyncSession,
    project_id: str,
    title: str,
    description: str | None = None,
    status: str = "active",
    target_date: str | None = None,
    local_path: str | None = None,
) -> dict:
    """
    Create a Project node in the graph.

    Args:
        project_id: Unique identifier (UUID)
        title: Project title
        description: Detailed description
        status: active, paused, completed, archived
        target_date: Target completion date (ISO datetime string)
        local_path: Local filesystem path for auto-linking coding sessions
    """
    query = """
    MERGE (p:Project {id: $project_id})
    ON CREATE SET
        p.title = $title,
        p.description = $description,
        p.status = $status,
        p.target_date = CASE WHEN $target_date IS NOT NULL THEN datetime($target_date) ELSE null END,
        p.local_path = $local_path,
        p.created_at = datetime(),
        p.updated_at = datetime()
    ON MATCH SET
        p.title = $title,
        p.description = COALESCE($description, p.description),
        p.status = COALESCE($status, p.status),
        p.target_date = CASE WHEN $target_date IS NOT NULL THEN datetime($target_date) ELSE p.target_date END,
        p.local_path = COALESCE($local_path, p.local_path),
        p.updated_at = datetime()
    RETURN p
    """
    result = await session.run(
        query,
        project_id=project_id,
        title=title,
        description=description,
        status=status,
        target_date=target_date,
        local_path=local_path,
    )
    record = await result.single()
    return dict(record["p"]) if record else {}


async def update_project(
    session: AsyncSession,
    project_id: str,
    title: str | None = None,
    description: str | None = None,
    status: str | None = None,
    target_date: str | None = None,
    local_path: str | None = None,
) -> dict:
    """Update specific fields of a project."""
    set_parts = ["p.updated_at = datetime()"]
    params = {"project_id": project_id}

    if title is not None:
        set_parts.append("p.title = $title")
        params["title"] = title
    if description is not None:
        set_parts.append("p.description = $description")
        params["description"] = description
    if status is not None:
        set_parts.append("p.status = $status")
        params["status"] = status
    if target_date is not None:
        set_parts.append("p.target_date = datetime($target_date)")
        params["target_date"] = target_date
    if local_path is not None:
        set_parts.append("p.local_path = $local_path")
        params["local_path"] = local_path

    query = f"""
    MATCH (p:Project {{id: $project_id}})
    SET {', '.join(set_parts)}
    RETURN p
    """
    result = await session.run(query, **params)
    record = await result.single()
    return dict(record["p"]) if record else {}


async def get_project(
    session: AsyncSession,
    project_id: str,
) -> dict | None:
    """Get a single project by ID with all its relationships."""
    query = """
    MATCH (p:Project {id: $project_id})
    OPTIONAL MATCH (p)-[:ACHIEVES]->(g:Goal)
    OPTIONAL MATCH (p)-[:OWNED_BY]->(owner:Person)
    OPTIONAL MATCH (p)<-[:STAKEHOLDER]-(stakeholder:Person)
    OPTIONAL MATCH (p)-[:USES_REPO]->(r:Repository)
    OPTIONAL MATCH (p)-[:DOCUMENTED_IN]->(d:Document)
    OPTIONAL MATCH (p)-[:RELATED_TO]-(related:Project)
    OPTIONAL MATCH (t:Task)-[:PART_OF]->(p)
    RETURN p,
           collect(DISTINCT g.id) as goal_ids,
           owner.email as owner_email,
           owner.name as owner_name,
           collect(DISTINCT {email: stakeholder.email, name: stakeholder.name}) as stakeholders,
           collect(DISTINCT r.full_name) as repositories,
           collect(DISTINCT d.drive_id) as document_ids,
           collect(DISTINCT related.id) as related_project_ids,
           count(DISTINCT t) as task_count,
           sum(CASE WHEN t.status = 'completed' THEN 1 ELSE 0 END) as completed_tasks
    """
    result = await session.run(query, project_id=project_id)
    record = await result.single()
    if not record:
        return None

    # Combine owner and stakeholders for display
    owner_email = record["owner_email"]
    owner_name = record.get("owner_name")
    stakeholders = [s for s in record["stakeholders"] if s.get("email")]
    all_people = ([{"email": owner_email, "name": owner_name}] if owner_email else []) + stakeholders
    # Deduplicate by email while preserving order
    seen = set()
    people_full = []
    for p in all_people:
        if p["email"] and p["email"] not in seen:
            seen.add(p["email"])
            people_full.append(p)
    people_emails = [p["email"] for p in people_full]

    project_data = dict(record["p"])
    # Convert target_date DateTime to string for template compatibility
    target_date = project_data.get("target_date")
    if target_date:
        project_data["target_date"] = str(target_date)

    return {
        **project_data,
        "goal_ids": [g for g in record["goal_ids"] if g],
        "owner_email": owner_email,
        "stakeholders": [s["email"] for s in stakeholders],
        "people": people_emails,  # For display
        "people_emails": people_emails,  # For form selection
        "people_full": people_full,  # For people picker (with names)
        "repositories": [r for r in record["repositories"] if r],
        "document_ids": [d for d in record["document_ids"] if d],
        "related_project_ids": [r for r in record["related_project_ids"] if r],
        "task_count": record["task_count"],
        "completed_tasks": record["completed_tasks"],
    }


async def get_projects(
    session: AsyncSession,
    status: str | None = None,
    include_archived: bool = False,
    limit: int = 50,
) -> list[dict]:
    """Get projects with optional filters."""
    filters = []
    params = {"limit": limit}

    if status:
        filters.append("p.status = $status")
        params["status"] = status
    elif not include_archived:
        filters.append("p.status <> 'archived'")

    where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""

    query = f"""
    MATCH (p:Project)
    {where_clause}
    OPTIONAL MATCH (t:Task)-[:PART_OF]->(p)
    OPTIONAL MATCH (p)-[:ACHIEVES]->(g:Goal)
    OPTIONAL MATCH (p)-[:OWNED_BY]->(owner:Person)
    OPTIONAL MATCH (p)<-[:STAKEHOLDER]-(stakeholder:Person)
    OPTIONAL MATCH (p)-[:USES_REPO]->(r:Repository)
    WITH p, g, owner, r,
         count(DISTINCT t) as task_count,
         sum(CASE WHEN t.status = 'completed' THEN 1 ELSE 0 END) as completed_tasks,
         collect(DISTINCT stakeholder) as stakeholders
    RETURN p,
           task_count,
           completed_tasks,
           collect(DISTINCT g.title)[0] as goal_name,
           owner.email as owner_email,
           owner.name as owner_name,
           [s IN stakeholders | {{email: s.email, name: s.name}}] as stakeholder_list,
           collect(DISTINCT r.full_name) as repositories
    ORDER BY
        CASE p.status WHEN 'active' THEN 0 WHEN 'paused' THEN 1 ELSE 2 END,
        p.target_date ASC,
        p.created_at DESC
    LIMIT $limit
    """
    result = await session.run(query, **params)
    records = await result.data()

    projects = []
    for r in records:
        # Combine owner and stakeholders for display
        owner_email = r.get("owner_email")
        owner_name = r.get("owner_name")
        stakeholders = [s for s in (r.get("stakeholder_list") or []) if s.get("email")]
        all_people = ([{"email": owner_email, "name": owner_name}] if owner_email else []) + stakeholders

        # Deduplicate by email while preserving order
        seen = set()
        people_full = []
        for person in all_people:
            if person.get("email") and person["email"] not in seen:
                seen.add(person["email"])
                people_full.append(person)
        people_emails = [person["email"] for person in people_full]

        project_data = dict(r["p"])
        # Convert target_date DateTime to string for template compatibility
        target_date = project_data.get("target_date")
        if target_date:
            project_data["target_date"] = str(target_date)

        projects.append({
            **project_data,
            "task_count": r["task_count"],
            "completed_tasks": r["completed_tasks"],
            "goal_name": r["goal_name"],
            "goal": r["goal_name"],  # Alias for template compatibility
            "people": people_emails,
            "people_full": people_full,
            "repositories": [repo for repo in (r.get("repositories") or []) if repo],
        })

    return projects


async def link_project_to_goal(
    session: AsyncSession,
    project_id: str,
    goal_id: str,
) -> None:
    """Create ACHIEVES relationship between Project and Goal."""
    query = """
    MATCH (p:Project {id: $project_id})
    MATCH (g:Goal {id: $goal_id})
    MERGE (p)-[:ACHIEVES]->(g)
    """
    await session.run(query, project_id=project_id, goal_id=goal_id)


async def link_project_to_person(
    session: AsyncSession,
    project_id: str,
    person_email: str,
    role: str = "stakeholder",
) -> None:
    """Create relationship between Project and Person (OWNED_BY or STAKEHOLDER)."""
    if role == "owner":
        query = """
        MATCH (p:Project {id: $project_id})
        MATCH (person:Person {email: $person_email})
        MERGE (p)-[:OWNED_BY]->(person)
        """
    else:
        query = """
        MATCH (p:Project {id: $project_id})
        MATCH (person:Person {email: $person_email})
        MERGE (p)<-[:STAKEHOLDER {role: $role}]-(person)
        """
    await session.run(query, project_id=project_id, person_email=person_email, role=role)


async def link_goal_to_person(
    session: AsyncSession,
    goal_id: str,
    person_email: str,
    role: str = "stakeholder",
) -> None:
    """Create relationship between Goal and Person (OWNED_BY or STAKEHOLDER)."""
    if role == "owner":
        query = """
        MATCH (g:Goal {id: $goal_id})
        MATCH (person:Person {email: $person_email})
        MERGE (g)-[:OWNED_BY]->(person)
        """
    else:
        query = """
        MATCH (g:Goal {id: $goal_id})
        MATCH (person:Person {email: $person_email})
        MERGE (g)<-[:STAKEHOLDER {role: $role}]-(person)
        """
    await session.run(query, goal_id=goal_id, person_email=person_email, role=role)


async def link_project_to_repository(
    session: AsyncSession,
    project_id: str,
    repository_id: str,
) -> None:
    """Create USES_REPO relationship between Project and Repository."""
    query = """
    MATCH (p:Project {id: $project_id})
    MATCH (r:Repository {id: $repository_id})
    MERGE (p)-[:USES_REPO]->(r)
    """
    await session.run(query, project_id=project_id, repository_id=repository_id)


async def link_project_to_document(
    session: AsyncSession,
    project_id: str,
    drive_id: str,
) -> None:
    """Create DOCUMENTED_IN relationship between Project and Document."""
    query = """
    MATCH (p:Project {id: $project_id})
    MATCH (d:Document {drive_id: $drive_id})
    MERGE (p)-[:DOCUMENTED_IN]->(d)
    """
    await session.run(query, project_id=project_id, drive_id=drive_id)


async def link_projects_related(
    session: AsyncSession,
    project_id_1: str,
    project_id_2: str,
) -> None:
    """Create RELATED_TO relationship between two Projects."""
    query = """
    MATCH (p1:Project {id: $project_id_1})
    MATCH (p2:Project {id: $project_id_2})
    MERGE (p1)-[:RELATED_TO]-(p2)
    """
    await session.run(query, project_id_1=project_id_1, project_id_2=project_id_2)


async def delete_project(
    session: AsyncSession,
    project_id: str,
) -> bool:
    """Delete a project and all its relationships (tasks remain orphaned)."""
    query = """
    MATCH (p:Project {id: $project_id})
    DETACH DELETE p
    RETURN count(p) as deleted
    """
    result = await session.run(query, project_id=project_id)
    record = await result.single()
    return record["deleted"] > 0 if record else False


# ============================================================================
# Goal operations
# ============================================================================

async def create_goal(
    session: AsyncSession,
    goal_id: str,
    title: str,
    description: str | None = None,
    timeframe: str = "ongoing",
    status: str = "active",
) -> dict:
    """
    Create a Goal node in the graph.

    Args:
        goal_id: Unique identifier (UUID)
        title: Goal title
        description: Detailed description
        timeframe: Q1_2025, Q2_2025, 2025, ongoing, etc.
        status: active, achieved, abandoned
    """
    query = """
    MERGE (g:Goal {id: $goal_id})
    ON CREATE SET
        g.title = $title,
        g.description = $description,
        g.timeframe = $timeframe,
        g.status = $status,
        g.created_at = datetime(),
        g.updated_at = datetime()
    ON MATCH SET
        g.title = $title,
        g.description = COALESCE($description, g.description),
        g.timeframe = COALESCE($timeframe, g.timeframe),
        g.status = COALESCE($status, g.status),
        g.updated_at = datetime()
    RETURN g
    """
    result = await session.run(
        query,
        goal_id=goal_id,
        title=title,
        description=description,
        timeframe=timeframe,
        status=status,
    )
    record = await result.single()
    return dict(record["g"]) if record else {}


async def update_goal(
    session: AsyncSession,
    goal_id: str,
    title: str | None = None,
    description: str | None = None,
    timeframe: str | None = None,
    status: str | None = None,
) -> dict:
    """Update specific fields of a goal."""
    set_parts = ["g.updated_at = datetime()"]
    params = {"goal_id": goal_id}

    if title is not None:
        set_parts.append("g.title = $title")
        params["title"] = title
    if description is not None:
        set_parts.append("g.description = $description")
        params["description"] = description
    if timeframe is not None:
        set_parts.append("g.timeframe = $timeframe")
        params["timeframe"] = timeframe
    if status is not None:
        set_parts.append("g.status = $status")
        params["status"] = status

    query = f"""
    MATCH (g:Goal {{id: $goal_id}})
    SET {', '.join(set_parts)}
    RETURN g
    """
    result = await session.run(query, **params)
    record = await result.single()
    return dict(record["g"]) if record else {}


async def get_goal(
    session: AsyncSession,
    goal_id: str,
) -> dict | None:
    """Get a single goal by ID with all its relationships."""
    query = """
    MATCH (g:Goal {id: $goal_id})
    OPTIONAL MATCH (p:Project)-[:ACHIEVES]->(g)
    OPTIONAL MATCH (t:Task)-[:CONTRIBUTES_TO]->(g)
    OPTIONAL MATCH (g)-[:PARENT_OF]->(child:Goal)
    OPTIONAL MATCH (parent:Goal)-[:PARENT_OF]->(g)
    OPTIONAL MATCH (g)-[:OWNED_BY]->(owner:Person)
    OPTIONAL MATCH (g)<-[:STAKEHOLDER]-(stakeholder:Person)
    RETURN g,
           collect(DISTINCT p.id) as project_ids,
           count(DISTINCT t) as task_count,
           sum(CASE WHEN t.status = 'completed' THEN 1 ELSE 0 END) as completed_tasks,
           collect(DISTINCT child.id) as child_goal_ids,
           parent.id as parent_goal_id,
           owner.email as owner_email,
           owner.name as owner_name,
           collect(DISTINCT {email: stakeholder.email, name: stakeholder.name}) as stakeholders
    """
    result = await session.run(query, goal_id=goal_id)
    record = await result.single()
    if not record:
        return None

    # Combine owner and stakeholders for display
    owner_email = record["owner_email"]
    owner_name = record.get("owner_name")
    stakeholders = [s for s in record["stakeholders"] if s.get("email")]
    all_people = ([{"email": owner_email, "name": owner_name}] if owner_email else []) + stakeholders
    # Deduplicate by email while preserving order
    seen = set()
    people_full = []
    for p in all_people:
        if p["email"] and p["email"] not in seen:
            seen.add(p["email"])
            people_full.append(p)
    people_emails = [p["email"] for p in people_full]

    return {
        **dict(record["g"]),
        "project_ids": [p for p in record["project_ids"] if p],
        "project_count": len([p for p in record["project_ids"] if p]),
        "task_count": record["task_count"],
        "completed_tasks": record["completed_tasks"],
        "child_goal_ids": [c for c in record["child_goal_ids"] if c],
        "parent_goal_id": record["parent_goal_id"],
        "owner_email": owner_email,
        "stakeholders": [s["email"] for s in stakeholders],
        "people": people_emails,  # For display
        "people_emails": people_emails,  # For form selection
        "people_full": people_full,  # For people picker (with names)
    }


async def get_goals(
    session: AsyncSession,
    status: str | None = None,
    timeframe: str | None = None,
    include_achieved: bool = False,
    limit: int = 50,
) -> list[dict]:
    """Get goals with optional filters."""
    filters = []
    params = {"limit": limit}

    if status:
        filters.append("g.status = $status")
        params["status"] = status
    elif not include_achieved:
        filters.append("g.status = 'active'")

    if timeframe:
        filters.append("g.timeframe = $timeframe")
        params["timeframe"] = timeframe

    where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""

    query = f"""
    MATCH (g:Goal)
    {where_clause}
    OPTIONAL MATCH (p:Project)-[:ACHIEVES]->(g)
    OPTIONAL MATCH (t:Task)-[:CONTRIBUTES_TO]->(g)
    RETURN g,
           count(DISTINCT p) as project_count,
           count(DISTINCT t) as task_count,
           sum(CASE WHEN t.status = 'completed' THEN 1 ELSE 0 END) as completed_tasks
    ORDER BY
        CASE g.status WHEN 'active' THEN 0 ELSE 1 END,
        g.timeframe ASC,
        g.created_at DESC
    LIMIT $limit
    """
    result = await session.run(query, **params)
    records = await result.data()
    return [
        {
            **dict(r["g"]),
            "project_count": r["project_count"],
            "task_count": r["task_count"],
            "completed_tasks": r["completed_tasks"],
        }
        for r in records
    ]


async def link_goal_parent(
    session: AsyncSession,
    child_goal_id: str,
    parent_goal_id: str,
) -> None:
    """Create PARENT_OF relationship for goal hierarchy."""
    query = """
    MATCH (parent:Goal {id: $parent_goal_id})
    MATCH (child:Goal {id: $child_goal_id})
    MERGE (parent)-[:PARENT_OF]->(child)
    """
    await session.run(query, parent_goal_id=parent_goal_id, child_goal_id=child_goal_id)


async def delete_goal(
    session: AsyncSession,
    goal_id: str,
) -> bool:
    """Delete a goal and all its relationships."""
    query = """
    MATCH (g:Goal {id: $goal_id})
    DETACH DELETE g
    RETURN count(g) as deleted
    """
    result = await session.run(query, goal_id=goal_id)
    record = await result.single()
    return record["deleted"] > 0 if record else False


# ============================================================================
# Repository operations (GitHub integration)
# ============================================================================

async def create_repository(
    session: AsyncSession,
    repo_id: str,
    name: str,
    full_name: str,
    url: str,
    description: str | None = None,
    primary_language: str | None = None,
    default_branch: str = "main",
) -> dict:
    """
    Create a Repository node in the graph.

    Args:
        repo_id: Unique identifier (UUID or GitHub ID)
        name: Repository name
        full_name: owner/repo format
        url: GitHub URL
        description: Repository description
        primary_language: Main programming language
        default_branch: Default branch name
    """
    query = """
    MERGE (r:Repository {id: $repo_id})
    ON CREATE SET
        r.name = $name,
        r.full_name = $full_name,
        r.url = $url,
        r.description = $description,
        r.primary_language = $primary_language,
        r.default_branch = $default_branch,
        r.created_at = datetime(),
        r.last_synced = datetime()
    ON MATCH SET
        r.name = $name,
        r.full_name = $full_name,
        r.url = $url,
        r.description = COALESCE($description, r.description),
        r.primary_language = COALESCE($primary_language, r.primary_language),
        r.default_branch = COALESCE($default_branch, r.default_branch),
        r.last_synced = datetime()
    RETURN r
    """
    result = await session.run(
        query,
        repo_id=repo_id,
        name=name,
        full_name=full_name,
        url=url,
        description=description,
        primary_language=primary_language,
        default_branch=default_branch,
    )
    record = await result.single()
    return dict(record["r"]) if record else {}


async def get_repository(
    session: AsyncSession,
    repo_id: str | None = None,
    full_name: str | None = None,
) -> dict | None:
    """Get a repository by ID or full_name."""
    if repo_id:
        query = """
        MATCH (r:Repository {id: $repo_id})
        OPTIONAL MATCH (r)<-[:USES_REPO]-(p:Project)
        OPTIONAL MATCH (r)-[:CONTAINS]->(cf:CodeFile)
        RETURN r,
               collect(DISTINCT p.id) as project_ids,
               count(DISTINCT cf) as file_count
        """
        result = await session.run(query, repo_id=repo_id)
    elif full_name:
        query = """
        MATCH (r:Repository {full_name: $full_name})
        OPTIONAL MATCH (r)<-[:USES_REPO]-(p:Project)
        OPTIONAL MATCH (r)-[:CONTAINS]->(cf:CodeFile)
        RETURN r,
               collect(DISTINCT p.id) as project_ids,
               count(DISTINCT cf) as file_count
        """
        result = await session.run(query, full_name=full_name)
    else:
        return None

    record = await result.single()
    if not record:
        return None
    return {
        **dict(record["r"]),
        "project_ids": [p for p in record["project_ids"] if p],
        "file_count": record["file_count"],
    }


async def get_repositories(
    session: AsyncSession,
    limit: int = 50,
) -> list[dict]:
    """Get all repositories."""
    query = """
    MATCH (r:Repository)
    OPTIONAL MATCH (r)<-[:USES_REPO]-(p:Project)
    OPTIONAL MATCH (r)-[:CONTAINS]->(cf:CodeFile)
    RETURN r,
           collect(DISTINCT p.title) as project_names,
           count(DISTINCT cf) as file_count
    ORDER BY r.last_synced DESC
    LIMIT $limit
    """
    result = await session.run(query, limit=limit)
    records = await result.data()
    return [
        {
            **dict(r["r"]),
            "project_names": [p for p in r["project_names"] if p],
            "file_count": r["file_count"],
        }
        for r in records
    ]


async def delete_repository(
    session: AsyncSession,
    repo_id: str,
) -> bool:
    """Delete a repository and all its code files."""
    query = """
    MATCH (r:Repository {id: $repo_id})
    OPTIONAL MATCH (r)-[:CONTAINS]->(cf:CodeFile)
    DETACH DELETE r, cf
    RETURN count(r) as deleted
    """
    result = await session.run(query, repo_id=repo_id)
    record = await result.single()
    return record["deleted"] > 0 if record else False


# ============================================================================
# CodeFile operations
# ============================================================================

async def create_codefile(
    session: AsyncSession,
    codefile_id: str,
    path: str,
    name: str,
    repository_id: str,
    language: str | None = None,
    summary: str | None = None,
    last_modified: str | None = None,
) -> dict:
    """
    Create a CodeFile node in the graph.

    Args:
        codefile_id: Unique identifier (UUID)
        path: Full path in repository
        name: File name
        repository_id: Parent repository ID
        language: Programming language
        summary: LLM-generated description
        last_modified: Last modification date
    """
    query = """
    MERGE (cf:CodeFile {id: $codefile_id})
    ON CREATE SET
        cf.path = $path,
        cf.name = $name,
        cf.language = $language,
        cf.summary = $summary,
        cf.last_modified = CASE WHEN $last_modified IS NOT NULL THEN datetime($last_modified) ELSE null END,
        cf.created_at = datetime()
    ON MATCH SET
        cf.path = $path,
        cf.name = $name,
        cf.language = COALESCE($language, cf.language),
        cf.summary = COALESCE($summary, cf.summary),
        cf.last_modified = CASE WHEN $last_modified IS NOT NULL THEN datetime($last_modified) ELSE cf.last_modified END,
        cf.updated_at = datetime()
    RETURN cf
    """
    result = await session.run(
        query,
        codefile_id=codefile_id,
        path=path,
        name=name,
        language=language,
        summary=summary,
        last_modified=last_modified,
    )
    record = await result.single()

    # Link to repository
    if record:
        link_query = """
        MATCH (r:Repository {id: $repository_id})
        MATCH (cf:CodeFile {id: $codefile_id})
        MERGE (r)-[:CONTAINS]->(cf)
        """
        await session.run(link_query, repository_id=repository_id, codefile_id=codefile_id)

    return dict(record["cf"]) if record else {}


async def get_codefiles(
    session: AsyncSession,
    repository_id: str | None = None,
    language: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Get code files with optional filters."""
    filters = []
    params = {"limit": limit}

    if repository_id:
        filters.append("r.id = $repository_id")
        params["repository_id"] = repository_id

    if language:
        filters.append("cf.language = $language")
        params["language"] = language

    repo_match = "MATCH (r:Repository)-[:CONTAINS]->(cf:CodeFile)" if repository_id else "MATCH (cf:CodeFile)"
    where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""

    query = f"""
    {repo_match}
    {where_clause}
    OPTIONAL MATCH (r:Repository)-[:CONTAINS]->(cf)
    RETURN cf, r.full_name as repository
    ORDER BY cf.path ASC
    LIMIT $limit
    """
    result = await session.run(query, **params)
    records = await result.data()
    return [
        {
            **dict(r["cf"]),
            "repository": r["repository"],
        }
        for r in records
    ]


async def link_codefiles_import(
    session: AsyncSession,
    importer_id: str,
    imported_id: str,
) -> None:
    """Create IMPORTS relationship between CodeFiles."""
    query = """
    MATCH (importer:CodeFile {id: $importer_id})
    MATCH (imported:CodeFile {id: $imported_id})
    MERGE (importer)-[:IMPORTS]->(imported)
    """
    await session.run(query, importer_id=importer_id, imported_id=imported_id)


# ============================================================================
# Chunk operations (Document semantic graph)
# ============================================================================

async def create_chunk(
    session: AsyncSession,
    chunk_id: str,
    drive_id: str,
    chunk_index: int,
    summary: str | None = None,
    content_type: str | None = None,
    key_facts: list[str] | None = None,
) -> dict:
    """
    Create a Chunk node in the graph linked to its parent Document.

    Args:
        chunk_id: Unique identifier (drive_id:chunk_index)
        drive_id: Parent document's Drive ID
        chunk_index: Position in document (0-indexed)
        summary: LLM-generated summary
        content_type: Type of content (data, narrative, code, etc.)
        key_facts: List of key facts extracted
    """
    query = """
    MERGE (c:Chunk {id: $chunk_id})
    ON CREATE SET
        c.drive_id = $drive_id,
        c.chunk_index = $chunk_index,
        c.summary = $summary,
        c.content_type = $content_type,
        c.key_facts = $key_facts,
        c.analyzed = true,
        c.created_at = datetime()
    ON MATCH SET
        c.summary = COALESCE($summary, c.summary),
        c.content_type = COALESCE($content_type, c.content_type),
        c.key_facts = COALESCE($key_facts, c.key_facts),
        c.analyzed = true,
        c.updated_at = datetime()
    RETURN c
    """
    result = await session.run(
        query,
        chunk_id=chunk_id,
        drive_id=drive_id,
        chunk_index=chunk_index,
        summary=summary,
        content_type=content_type,
        key_facts=key_facts or [],
    )
    record = await result.single()

    # Link to parent document
    if record:
        link_query = """
        MATCH (d:Document {drive_id: $drive_id})
        MATCH (c:Chunk {id: $chunk_id})
        MERGE (d)-[:HAS_CHUNK]->(c)
        """
        await session.run(link_query, drive_id=drive_id, chunk_id=chunk_id)

        # Link to previous chunk for sequence
        if chunk_index > 0:
            prev_chunk_id = f"{drive_id}:{chunk_index - 1}"
            seq_query = """
            MATCH (prev:Chunk {id: $prev_chunk_id})
            MATCH (curr:Chunk {id: $chunk_id})
            MERGE (prev)-[:NEXT_CHUNK]->(curr)
            """
            await session.run(seq_query, prev_chunk_id=prev_chunk_id, chunk_id=chunk_id)

    return dict(record["c"]) if record else {}


async def create_topic(
    session: AsyncSession,
    name: str,
) -> dict:
    """
    Create or get a Topic node (normalized label for themes/subjects).

    Args:
        name: Topic name (lowercase, normalized)
    """
    # Normalize topic name
    normalized = name.lower().strip()

    query = """
    MERGE (t:Topic {name: $name})
    ON CREATE SET
        t.created_at = datetime(),
        t.mention_count = 1
    ON MATCH SET
        t.mention_count = t.mention_count + 1
    RETURN t
    """
    result = await session.run(query, name=normalized)
    record = await result.single()
    return dict(record["t"]) if record else {}


async def create_concept(
    session: AsyncSession,
    name: str,
) -> dict:
    """
    Create or get a Concept node (domain-specific terms, entities).

    Args:
        name: Concept name (preserved case for proper nouns)
    """
    # Light normalization - preserve case but trim
    normalized = name.strip()

    query = """
    MERGE (c:Concept {name: $name})
    ON CREATE SET
        c.created_at = datetime(),
        c.mention_count = 1
    ON MATCH SET
        c.mention_count = c.mention_count + 1
    RETURN c
    """
    result = await session.run(query, name=normalized)
    record = await result.single()
    return dict(record["c"]) if record else {}


async def link_chunk_to_topic(
    session: AsyncSession,
    chunk_id: str,
    topic_name: str,
) -> None:
    """Create DISCUSSES relationship between Chunk and Topic."""
    normalized = topic_name.lower().strip()
    query = """
    MATCH (c:Chunk {id: $chunk_id})
    MERGE (t:Topic {name: $topic_name})
    MERGE (c)-[:DISCUSSES]->(t)
    """
    await session.run(query, chunk_id=chunk_id, topic_name=normalized)


async def link_chunk_to_concept(
    session: AsyncSession,
    chunk_id: str,
    concept_name: str,
) -> None:
    """Create REFERENCES relationship between Chunk and Concept."""
    normalized = concept_name.strip()
    query = """
    MATCH (c:Chunk {id: $chunk_id})
    MERGE (con:Concept {name: $concept_name})
    MERGE (c)-[:REFERENCES]->(con)
    """
    await session.run(query, chunk_id=chunk_id, concept_name=normalized)


async def link_chunk_to_person(
    session: AsyncSession,
    chunk_id: str,
    person_identifier: str,
) -> None:
    """
    Create MENTIONS relationship between Chunk and Person.

    Args:
        chunk_id: The chunk ID
        person_identifier: Email address or name to match/create person
    """
    # Check if it looks like an email
    if "@" in person_identifier:
        query = """
        MATCH (c:Chunk {id: $chunk_id})
        MERGE (p:Person {email: $identifier})
        MERGE (c)-[:MENTIONS]->(p)
        """
    else:
        # Try to match by name, create with generated email if not found
        query = """
        MATCH (c:Chunk {id: $chunk_id})
        MERGE (p:Person {name: $identifier})
        ON CREATE SET p.email = $identifier + '@unknown'
        MERGE (c)-[:MENTIONS]->(p)
        """
    await session.run(query, chunk_id=chunk_id, identifier=person_identifier)


async def link_chunk_to_organization(
    session: AsyncSession,
    chunk_id: str,
    org_name: str,
    org_type: str = "",
) -> None:
    """Create INVOLVES relationship between Chunk and Organization."""
    normalized = org_name.strip()
    query = """
    MATCH (c:Chunk {id: $chunk_id})
    MERGE (o:Organization {name: $org_name})
    ON CREATE SET o.type = $org_type
    MERGE (c)-[:INVOLVES]->(o)
    """
    await session.run(query, chunk_id=chunk_id, org_name=normalized, org_type=org_type)


async def link_chunk_to_semantic_tag(
    session: AsyncSession,
    chunk_id: str,
    tag_name: str,
) -> None:
    """Create TAGGED_AS relationship between Chunk and SemanticTag for clustering."""
    normalized = tag_name.lower().strip().replace(" ", "_")
    query = """
    MATCH (c:Chunk {id: $chunk_id})
    MERGE (t:SemanticTag {name: $tag_name})
    MERGE (c)-[:TAGGED_AS]->(t)
    """
    await session.run(query, chunk_id=chunk_id, tag_name=normalized)


async def get_unanalyzed_chunks(
    session: AsyncSession,
    limit: int = 100,
) -> list[dict]:
    """Get chunks that haven't been analyzed for entities yet."""
    query = """
    MATCH (d:Document)-[:HAS_CHUNK]->(c:Chunk)
    WHERE c.analyzed IS NULL OR c.analyzed = false
    RETURN c.id as chunk_id, c.drive_id as drive_id, c.chunk_index as chunk_index,
           d.name as document_name
    ORDER BY c.drive_id, c.chunk_index
    LIMIT $limit
    """
    result = await session.run(query, limit=limit)
    records = await result.data()
    return records


async def get_chunks_for_document(
    session: AsyncSession,
    drive_id: str,
) -> list[dict]:
    """Get all chunks for a document with their relationships."""
    query = """
    MATCH (d:Document {drive_id: $drive_id})-[:HAS_CHUNK]->(c:Chunk)
    OPTIONAL MATCH (c)-[:DISCUSSES]->(t:Topic)
    OPTIONAL MATCH (c)-[:REFERENCES]->(con:Concept)
    OPTIONAL MATCH (c)-[:MENTIONS]->(p:Person)
    RETURN c,
           collect(DISTINCT t.name) as topics,
           collect(DISTINCT con.name) as concepts,
           collect(DISTINCT p.name) as people
    ORDER BY c.chunk_index ASC
    """
    result = await session.run(query, drive_id=drive_id)
    records = await result.data()
    return [
        {
            **dict(r["c"]),
            "topics": [t for t in r["topics"] if t],
            "concepts": [c for c in r["concepts"] if c],
            "people": [p for p in r["people"] if p],
        }
        for r in records
    ]


async def get_topics(
    session: AsyncSession,
    limit: int = 50,
) -> list[dict]:
    """Get all topics ordered by mention count."""
    query = """
    MATCH (t:Topic)
    OPTIONAL MATCH (c:Chunk)-[:DISCUSSES]->(t)
    OPTIONAL MATCH (c)-[:HAS_CHUNK]-(d:Document)
    RETURN t.name as name,
           t.mention_count as mention_count,
           count(DISTINCT c) as chunk_count,
           count(DISTINCT d) as document_count
    ORDER BY t.mention_count DESC
    LIMIT $limit
    """
    result = await session.run(query, limit=limit)
    records = await result.data()
    return records


async def get_concepts(
    session: AsyncSession,
    limit: int = 50,
) -> list[dict]:
    """Get all concepts ordered by mention count."""
    query = """
    MATCH (c:Concept)
    OPTIONAL MATCH (ch:Chunk)-[:REFERENCES]->(c)
    OPTIONAL MATCH (ch)-[:HAS_CHUNK]-(d:Document)
    RETURN c.name as name,
           c.mention_count as mention_count,
           count(DISTINCT ch) as chunk_count,
           count(DISTINCT d) as document_count
    ORDER BY c.mention_count DESC
    LIMIT $limit
    """
    result = await session.run(query, limit=limit)
    records = await result.data()
    return records


async def find_chunks_by_topic(
    session: AsyncSession,
    topic_name: str,
    limit: int = 20,
) -> list[dict]:
    """Find chunks that discuss a specific topic."""
    normalized = topic_name.lower().strip()
    query = """
    MATCH (c:Chunk)-[:DISCUSSES]->(t:Topic {name: $topic_name})
    MATCH (d:Document)-[:HAS_CHUNK]->(c)
    RETURN c.id as chunk_id,
           c.summary as summary,
           c.chunk_index as chunk_index,
           d.drive_id as drive_id,
           d.name as document_name
    ORDER BY c.chunk_index ASC
    LIMIT $limit
    """
    result = await session.run(query, topic_name=normalized, limit=limit)
    records = await result.data()
    return records


async def find_chunks_by_concept(
    session: AsyncSession,
    concept_name: str,
    limit: int = 20,
) -> list[dict]:
    """Find chunks that reference a specific concept."""
    query = """
    MATCH (c:Chunk)-[:REFERENCES]->(con:Concept {name: $concept_name})
    MATCH (d:Document)-[:HAS_CHUNK]->(c)
    RETURN c.id as chunk_id,
           c.summary as summary,
           c.chunk_index as chunk_index,
           d.drive_id as drive_id,
           d.name as document_name
    ORDER BY c.chunk_index ASC
    LIMIT $limit
    """
    result = await session.run(query, concept_name=concept_name, limit=limit)
    records = await result.data()
    return records


async def find_chunks_mentioning_person(
    session: AsyncSession,
    person_email: str,
    limit: int = 20,
) -> list[dict]:
    """Find chunks that mention a specific person."""
    query = """
    MATCH (c:Chunk)-[:MENTIONS]->(p:Person {email: $person_email})
    MATCH (d:Document)-[:HAS_CHUNK]->(c)
    RETURN c.id as chunk_id,
           c.summary as summary,
           c.chunk_index as chunk_index,
           d.drive_id as drive_id,
           d.name as document_name
    ORDER BY c.chunk_index ASC
    LIMIT $limit
    """
    result = await session.run(query, person_email=person_email, limit=limit)
    records = await result.data()
    return records


async def get_topic_connections(
    session: AsyncSession,
    topic_name: str,
) -> dict:
    """Get all entities connected to a topic (for graph exploration)."""
    normalized = topic_name.lower().strip()
    query = """
    MATCH (t:Topic {name: $topic_name})<-[:DISCUSSES]-(c:Chunk)
    OPTIONAL MATCH (c)-[:REFERENCES]->(con:Concept)
    OPTIONAL MATCH (c)-[:MENTIONS]->(p:Person)
    OPTIONAL MATCH (c)-[:DISCUSSES]->(other_topic:Topic)
    WHERE other_topic.name <> $topic_name
    OPTIONAL MATCH (d:Document)-[:HAS_CHUNK]->(c)
    RETURN
        collect(DISTINCT con.name) as related_concepts,
        collect(DISTINCT p.email) as mentioned_people,
        collect(DISTINCT other_topic.name) as related_topics,
        collect(DISTINCT d.name) as documents
    """
    result = await session.run(query, topic_name=normalized)
    record = await result.single()
    if not record:
        return {"related_concepts": [], "mentioned_people": [], "related_topics": [], "documents": []}
    return {
        "related_concepts": [c for c in record["related_concepts"] if c],
        "mentioned_people": [p for p in record["mentioned_people"] if p],
        "related_topics": [t for t in record["related_topics"] if t],
        "documents": [d for d in record["documents"] if d],
    }


async def get_semantic_graph_stats(session: AsyncSession) -> dict:
    """Get statistics about the semantic graph (chunks, topics, concepts)."""
    query = """
    MATCH (c:Chunk)
    WITH count(c) as chunk_count
    MATCH (t:Topic)
    WITH chunk_count, count(t) as topic_count
    MATCH (con:Concept)
    RETURN chunk_count, topic_count, count(con) as concept_count
    """
    result = await session.run(query)
    record = await result.single()

    if not record:
        return {"chunks": 0, "topics": 0, "concepts": 0, "analyzed": 0}

    # Get analyzed count
    analyzed_query = """
    MATCH (c:Chunk)
    WHERE c.analyzed = true
    RETURN count(c) as analyzed_count
    """
    analyzed_result = await session.run(analyzed_query)
    analyzed_record = await analyzed_result.single()

    return {
        "chunks": record["chunk_count"],
        "topics": record["topic_count"],
        "concepts": record["concept_count"],
        "analyzed": analyzed_record["analyzed_count"] if analyzed_record else 0,
    }
