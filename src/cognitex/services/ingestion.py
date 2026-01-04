"""Email, calendar, and Drive ingestion pipeline - processes data and stores in graph."""

import asyncio
import hashlib
from datetime import datetime

import structlog
from sqlalchemy import text, bindparam
from sqlalchemy.ext.asyncio import AsyncSession

from cognitex.db.neo4j import get_neo4j_session
from cognitex.db.graph_schema import (
    create_document,
    create_email,
    create_event,
    create_person,
    get_tasks_by_email_thread,
    link_document_owner,
    link_document_shared_with,
    link_email_recipient,
    link_email_sender,
    link_event_attendee,
    link_event_organizer,
    mark_document_indexed,
    update_task_status,
)
from cognitex.services.gmail import GmailService, fetch_all_messages, build_historical_query

logger = structlog.get_logger()


async def ingest_email_to_graph(email_data: dict) -> None:
    """
    Ingest a single email into the graph database.

    Creates Person nodes for sender/recipients and Email node,
    with appropriate relationships.

    Args:
        email_data: Email metadata dict from extract_email_metadata()
    """
    # Get authenticated user's email to mark "self" in the graph
    user_email = await get_user_email()
    user_email_lower = user_email.lower() if user_email else None

    async for session in get_neo4j_session():
        # Create sender Person node
        if email_data["sender_email"]:
            sender_email_lower = email_data["sender_email"].lower()
            is_self = (sender_email_lower == user_email_lower) if user_email_lower else False
            await create_person(
                session,
                email=email_data["sender_email"],
                name=email_data["sender_name"] or None,
                is_user=is_self,
            )

        # Create recipient Person nodes
        for name, email_addr in email_data.get("to", []):
            if email_addr:
                is_self = (email_addr.lower() == user_email_lower) if user_email_lower else False
                await create_person(session, email=email_addr, name=name or None, is_user=is_self)

        for name, email_addr in email_data.get("cc", []):
            if email_addr:
                is_self = (email_addr.lower() == user_email_lower) if user_email_lower else False
                await create_person(session, email=email_addr, name=name or None, is_user=is_self)

        # Create Email node
        await create_email(
            session,
            gmail_id=email_data["gmail_id"],
            thread_id=email_data["thread_id"],
            subject=email_data["subject"],
            date=email_data["date"],
            snippet=email_data.get("snippet"),
        )

        # Create relationships
        if email_data["sender_email"]:
            await link_email_sender(
                session,
                gmail_id=email_data["gmail_id"],
                sender_email=email_data["sender_email"],
            )

        for name, email_addr in email_data.get("to", []):
            if email_addr:
                await link_email_recipient(
                    session,
                    gmail_id=email_data["gmail_id"],
                    recipient_email=email_addr,
                    recipient_type="to",
                )

        for name, email_addr in email_data.get("cc", []):
            if email_addr:
                await link_email_recipient(
                    session,
                    gmail_id=email_data["gmail_id"],
                    recipient_email=email_addr,
                    recipient_type="cc",
                )

        logger.debug(
            "Ingested email",
            gmail_id=email_data["gmail_id"],
            subject=email_data["subject"][:50],
        )


async def get_user_email() -> str | None:
    """Get the authenticated user's email address."""
    try:
        gmail = GmailService()
        # Wrap sync Google API call to avoid blocking event loop
        profile = await asyncio.to_thread(gmail.get_profile)
        return profile.get("emailAddress", "").lower()
    except Exception as e:
        logger.warning("Failed to get user email", error=str(e))
        return None


async def check_sent_email_for_task_completion(
    email_data: dict,
    user_email: str,
) -> list[dict]:
    """
    Check if a sent email might complete any pending tasks.

    When the user sends a reply in a thread that has associated tasks,
    analyze whether the reply resolves those tasks.

    Args:
        email_data: Email metadata dict
        user_email: The authenticated user's email address

    Returns:
        List of tasks that were identified for potential completion
    """
    # Check if this email was sent by the user
    sender_email = email_data.get("sender_email", "").lower()
    if sender_email != user_email:
        return []

    thread_id = email_data.get("thread_id")
    if not thread_id:
        return []

    # Find tasks linked to this email thread
    tasks_to_check = []
    async for session in get_neo4j_session():
        tasks_to_check = await get_tasks_by_email_thread(session, thread_id)
        break

    if not tasks_to_check:
        return []

    logger.info(
        "Found tasks linked to sent email thread",
        thread_id=thread_id,
        task_count=len(tasks_to_check),
        subject=email_data.get("subject", "")[:50],
    )

    return tasks_to_check


async def auto_complete_tasks_from_reply(
    email_data: dict,
    tasks: list[dict],
) -> list[str]:
    """
    Use LLM to determine if a sent email completes any tasks.

    Args:
        email_data: The sent email metadata
        tasks: List of tasks potentially completed by this email

    Returns:
        List of task IDs that were marked as complete
    """
    from cognitex.services.llm import get_llm_service

    if not tasks:
        return []

    llm = get_llm_service()

    # Build context for LLM
    task_descriptions = "\n".join([
        f"- Task #{i+1}: {t['title']} (status: {t['status']})"
        + (f"\n  Description: {t['description']}" if t.get('description') else "")
        + (f"\n  Original email subject: {t.get('source_subject', 'N/A')}")
        for i, t in enumerate(tasks)
    ])

    prompt = f"""Analyze whether the following sent email reply resolves any of the listed tasks.

SENT EMAIL:
Subject: {email_data.get('subject', '(no subject)')}
To: {', '.join([e for _, e in email_data.get('to', [])])}
Snippet: {email_data.get('snippet', '')}

PENDING TASKS FROM THIS EMAIL THREAD:
{task_descriptions}

For each task, determine if the sent email effectively completes or resolves it.
Consider:
- Did the user respond to a request? (e.g., confirming a meeting time, answering a question)
- Did the user take the action implied by the task?
- A simple acknowledgment or reply usually completes "respond to..." tasks

Respond with a JSON object:
{{"completed_tasks": [1, 2], "reasoning": "Task 1 was to respond about meeting time, user confirmed 2:30pm..."}}

Use task numbers (1-indexed) from the list above. Return empty array if no tasks are completed."""

    try:
        response = await llm.complete(prompt)

        # Parse JSON response
        import json
        import re

        # Extract JSON from response
        json_match = re.search(r'\{.*\}', response, re.DOTALL)
        if not json_match:
            logger.warning("No JSON found in LLM response", response=response[:200])
            return []

        result = json.loads(json_match.group())
        completed_indices = result.get("completed_tasks", [])
        reasoning = result.get("reasoning", "")

        if not completed_indices:
            logger.info("LLM determined no tasks completed", reasoning=reasoning[:200])
            return []

        # Mark tasks as complete
        completed_ids = []
        async for session in get_neo4j_session():
            for idx in completed_indices:
                if 1 <= idx <= len(tasks):
                    task = tasks[idx - 1]
                    task_id = task["id"]
                    await update_task_status(session, task_id, "done")
                    completed_ids.append(task_id)
                    logger.info(
                        "Auto-completed task from email reply",
                        task_id=task_id,
                        task_title=task["title"],
                        reasoning=reasoning[:200],
                    )
            break

        return completed_ids

    except Exception as e:
        logger.warning("Failed to analyze email for task completion", error=str(e))
        return []


async def process_sent_emails(emails: list[dict], user_email: str) -> dict:
    """
    Process sent emails to check for task auto-completion.

    Args:
        emails: List of email metadata dicts
        user_email: The authenticated user's email

    Returns:
        Dict with processing stats
    """
    stats = {
        "sent_emails_found": 0,
        "tasks_checked": 0,
        "tasks_completed": [],
    }

    for email_data in emails:
        # Check if sent by user
        sender = email_data.get("sender_email", "").lower()
        if sender != user_email:
            continue

        stats["sent_emails_found"] += 1

        # Find related tasks
        tasks = await check_sent_email_for_task_completion(email_data, user_email)
        stats["tasks_checked"] += len(tasks)

        if tasks:
            # Use LLM to determine completion
            completed = await auto_complete_tasks_from_reply(email_data, tasks)
            stats["tasks_completed"].extend(completed)

    if stats["tasks_completed"]:
        logger.info(
            "Auto-completed tasks from sent emails",
            completed_count=len(stats["tasks_completed"]),
            task_ids=stats["tasks_completed"],
        )

    return stats


async def ingest_email_batch(emails: list[dict]) -> dict:
    """
    Ingest a batch of emails into the graph.

    Args:
        emails: List of email metadata dicts

    Returns:
        Dict with ingestion stats
    """
    stats = {
        "total": len(emails),
        "success": 0,
        "failed": 0,
        "errors": [],
    }

    for email_data in emails:
        try:
            await ingest_email_to_graph(email_data)
            stats["success"] += 1
        except Exception as e:
            stats["failed"] += 1
            stats["errors"].append({
                "gmail_id": email_data.get("gmail_id"),
                "error": str(e),
            })
            logger.warning(
                "Failed to ingest email",
                gmail_id=email_data.get("gmail_id"),
                error=str(e),
            )

    logger.info(
        "Batch ingestion complete",
        total=stats["total"],
        success=stats["success"],
        failed=stats["failed"],
    )

    return stats


async def update_sync_state(
    pg_session: AsyncSession,
    sync_id: str,
    history_id: str | None = None,
    metadata: dict | None = None,
) -> None:
    """
    Update the sync state in PostgreSQL.

    Args:
        pg_session: PostgreSQL async session
        sync_id: Sync identifier (e.g., 'gmail')
        history_id: Gmail history ID for incremental sync
        metadata: Additional metadata to store
    """
    query = text("""
        INSERT INTO sync_state (id, last_sync_at, history_id, metadata)
        VALUES (:id, NOW(), :history_id, :metadata)
        ON CONFLICT (id) DO UPDATE SET
            last_sync_at = NOW(),
            history_id = COALESCE(:history_id, sync_state.history_id),
            metadata = COALESCE(:metadata, sync_state.metadata)
    """)

    await pg_session.execute(
        query,
        {
            "id": sync_id,
            "history_id": history_id,
            "metadata": metadata or {},
        },
    )
    await pg_session.commit()


async def get_sync_state(pg_session: AsyncSession, sync_id: str) -> dict | None:
    """
    Get the current sync state from PostgreSQL.

    Args:
        pg_session: PostgreSQL async session
        sync_id: Sync identifier

    Returns:
        Sync state dict or None if not found
    """
    query = text("""
        SELECT id, last_sync_at, history_id, sync_token, metadata
        FROM sync_state
        WHERE id = :id
    """)

    result = await pg_session.execute(query, {"id": sync_id})
    row = result.fetchone()

    if row:
        return {
            "id": row.id,
            "last_sync_at": row.last_sync_at,
            "history_id": row.history_id,
            "sync_token": row.sync_token,
            "metadata": row.metadata,
        }

    return None


async def run_historical_sync(months: int = 6, inbox_only: bool = True) -> dict:
    """
    Run a historical sync of emails.

    Fetches emails from the past N months and ingests them into the graph.

    Args:
        months: Number of months to sync
        inbox_only: Only sync emails that hit the inbox (not filtered/spam)

    Returns:
        Sync result stats
    """
    logger.info("Starting historical Gmail sync", months=months, inbox_only=inbox_only)

    gmail = GmailService()

    # Get user profile for history ID
    profile = gmail.get_profile()
    history_id = profile.get("historyId")

    # Build query and fetch messages
    query = build_historical_query(months, inbox_only=inbox_only)
    messages = await fetch_all_messages(gmail, query)

    logger.info("Fetched messages for historical sync", count=len(messages))

    # Ingest into graph
    stats = await ingest_email_batch(messages)
    stats["history_id"] = history_id

    logger.info("Historical sync complete", **stats)

    return stats


async def run_incremental_sync(history_id: str) -> dict:
    """
    Run an incremental sync using Gmail history API.

    The incoming history_id is the CURRENT state from Gmail push notification.
    We need to query from our LAST KNOWN history_id to find what's new.

    Args:
        history_id: The current history ID from Gmail push notification

    Returns:
        Sync result stats including emails list for further processing
    """
    from cognitex.db.redis import get_redis

    redis = get_redis()
    redis_key = "cognitex:gmail:last_history_id"

    # Get the last known history ID
    last_history_id = await redis.get(redis_key)

    if not last_history_id:
        # First time - store current ID and return (nothing to sync yet)
        # TTL of 30 days - if we don't sync for a month, reset
        await redis.set(redis_key, history_id, ex=2592000)
        logger.info("First Gmail sync - storing initial history ID", history_id=history_id)
        return {
            "total": 0,
            "success": 0,
            "history_id": history_id,
            "first_sync": True,
        }

    logger.info("Starting incremental Gmail sync", from_history_id=last_history_id, to_history_id=history_id)

    gmail = GmailService()

    try:
        # Wrap sync Google API call to avoid blocking event loop
        history = await asyncio.to_thread(
            gmail.get_history,
            start_history_id=last_history_id,
            history_types=["messageAdded"],
        )
    except Exception as e:
        # History ID might be too old - reset and try again next time
        logger.warning("History sync failed, resetting history ID", error=str(e))
        await redis.set(redis_key, history_id, ex=2592000)  # 30 day TTL
        return {"error": str(e), "fallback_needed": True}

    # Extract new message IDs from history
    new_message_ids = set()
    for record in history.get("history", []):
        for msg in record.get("messagesAdded", []):
            new_message_ids.add(msg["message"]["id"])

    # Update stored history ID
    new_history_id = history.get("historyId", history_id)
    await redis.set(redis_key, new_history_id, ex=2592000)  # 30 day TTL

    if not new_message_ids:
        logger.info("No new messages found")
        return {
            "total": 0,
            "success": 0,
            "history_id": new_history_id,
        }

    logger.info("Found new messages", count=len(new_message_ids))

    # Fetch full metadata for new messages (run in thread to avoid blocking)
    messages = await asyncio.to_thread(
        gmail.get_message_batch, list(new_message_ids), format="metadata"
    )
    from cognitex.services.gmail import extract_email_metadata
    email_data = [extract_email_metadata(msg) for msg in messages]

    # Ingest into graph
    stats = await ingest_email_batch(email_data)
    stats["history_id"] = new_history_id
    stats["emails"] = email_data  # Include email data for agent processing

    # Check for task auto-completion from sent emails
    user_email = await get_user_email()
    if user_email:
        sent_stats = await process_sent_emails(email_data, user_email)
        stats["sent_email_stats"] = sent_stats
        if sent_stats["tasks_completed"]:
            stats["auto_completed_tasks"] = sent_stats["tasks_completed"]

    logger.info("Incremental sync complete", **{k: v for k, v in stats.items() if k not in ["emails", "sent_email_stats"]})

    return stats


# ============================================================================
# Calendar ingestion
# ============================================================================

async def ingest_event_to_graph(event_data: dict) -> None:
    """
    Ingest a single calendar event into the graph database.

    Creates Person nodes for attendees and Event node with relationships.

    Args:
        event_data: Event metadata dict from extract_event_metadata()
    """
    async for session in get_neo4j_session():
        # Create organizer Person node
        if event_data.get("organizer_email"):
            await create_person(
                session,
                email=event_data["organizer_email"],
            )

        # Create attendee Person nodes
        for attendee in event_data.get("attendees", []):
            if attendee.get("email"):
                await create_person(
                    session,
                    email=attendee["email"],
                    name=attendee.get("name") or None,
                )

        # Create Event node
        await create_event(
            session,
            gcal_id=event_data["gcal_id"],
            title=event_data["title"],
            start=event_data["start"],
            end=event_data["end"],
            duration_minutes=event_data["duration_minutes"],
            event_type=event_data["event_type"],
            energy_impact=event_data["energy_impact"],
            is_all_day=event_data.get("is_all_day", False),
            location=event_data.get("location"),
            description=event_data.get("description"),
            organizer_email=event_data.get("organizer_email"),
            attendee_count=event_data.get("attendee_count", 0),
            is_recurring=event_data.get("is_recurring", False),
            conference_data=event_data.get("conference_data", False),
        )

        # Create organizer relationship
        if event_data.get("organizer_email"):
            await link_event_organizer(
                session,
                gcal_id=event_data["gcal_id"],
                organizer_email=event_data["organizer_email"],
            )

        # Create attendee relationships
        for attendee in event_data.get("attendees", []):
            if attendee.get("email"):
                await link_event_attendee(
                    session,
                    gcal_id=event_data["gcal_id"],
                    attendee_email=attendee["email"],
                    response_status=attendee.get("response_status", "needsAction"),
                    is_organizer=attendee.get("is_organizer", False),
                )

        logger.debug(
            "Ingested event",
            gcal_id=event_data["gcal_id"],
            title=event_data["title"][:50],
        )


async def ingest_event_batch(events: list[dict]) -> dict:
    """
    Ingest a batch of calendar events into the graph.

    Args:
        events: List of event metadata dicts

    Returns:
        Dict with ingestion stats
    """
    stats = {
        "total": len(events),
        "success": 0,
        "failed": 0,
        "errors": [],
    }

    for event_data in events:
        try:
            await ingest_event_to_graph(event_data)
            stats["success"] += 1
        except Exception as e:
            stats["failed"] += 1
            stats["errors"].append({
                "gcal_id": event_data.get("gcal_id"),
                "error": str(e),
            })
            logger.warning(
                "Failed to ingest event",
                gcal_id=event_data.get("gcal_id"),
                error=str(e),
            )

    logger.info(
        "Event batch ingestion complete",
        total=stats["total"],
        success=stats["success"],
        failed=stats["failed"],
    )

    return stats


async def run_calendar_sync(months_back: int = 1, days_ahead: int = 30) -> dict:
    """
    Sync calendar events (past and upcoming) into the graph.

    Args:
        months_back: Number of months of historical events to sync
        days_ahead: Number of days of upcoming events to sync

    Returns:
        Sync result stats
    """
    from datetime import datetime, timedelta, timezone
    from cognitex.services.calendar import (
        CalendarService,
        fetch_historical_events,
        fetch_upcoming_events,
    )

    logger.info("Starting calendar sync", months_back=months_back, days_ahead=days_ahead)

    calendar = CalendarService()

    # Fetch historical events
    historical = await fetch_historical_events(calendar, months_back=months_back)

    # Fetch upcoming events
    upcoming = await fetch_upcoming_events(calendar, days_ahead=days_ahead)

    # Deduplicate (upcoming might overlap with historical)
    all_events = {e["gcal_id"]: e for e in historical + upcoming}
    events = list(all_events.values())
    gcal_ids = set(all_events.keys())

    logger.info("Fetched calendar events", historical=len(historical), upcoming=len(upcoming), total=len(events))

    # Ingest into graph
    stats = await ingest_event_batch(events)

    # Delete events from Neo4j that are no longer in Google Calendar (within the sync window)
    now = datetime.now(timezone.utc)
    start_date = now - timedelta(days=months_back * 30)
    end_date = now + timedelta(days=days_ahead)

    deleted_count = 0
    async for session in get_neo4j_session():
        # Find events in Neo4j within the sync window that aren't in the fetched events
        result = await session.run("""
            MATCH (e:Event)
            WHERE e.start >= datetime($start) AND e.start <= datetime($end)
            RETURN e.gcal_id as gcal_id
        """, {
            "start": start_date.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": end_date.strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
        existing = await result.data()
        existing_ids = {e["gcal_id"] for e in existing}

        # Find IDs to delete (in Neo4j but not in Google Calendar)
        to_delete = existing_ids - gcal_ids

        if to_delete:
            # Delete orphaned events
            await session.run("""
                MATCH (e:Event)
                WHERE e.gcal_id IN $ids
                DETACH DELETE e
            """, {"ids": list(to_delete)})
            deleted_count = len(to_delete)
            logger.info("Deleted orphaned events", count=deleted_count)

    stats["deleted"] = deleted_count
    logger.info("Calendar sync complete", **stats)

    return stats


# ============================================================================
# Drive ingestion
# ============================================================================

async def ingest_document_to_graph(file_data: dict) -> None:
    """
    Ingest a single Drive file into the graph database.

    Creates Document node with owner/sharing relationships.

    Args:
        file_data: File metadata dict from Drive API
    """
    async for session in get_neo4j_session():
        # Extract owner email
        owner_email = None
        owners = file_data.get("owners", [])
        if owners:
            owner_email = owners[0].get("emailAddress")

        # Create Document node
        await create_document(
            session,
            drive_id=file_data["id"],
            name=file_data["name"],
            mime_type=file_data["mimeType"],
            modified_at=file_data["modifiedTime"],
            folder_path=file_data.get("_path"),
            size_bytes=int(file_data.get("size", 0)) if file_data.get("size") else None,
            web_link=file_data.get("webViewLink"),
            owner_email=owner_email,
            is_shared=file_data.get("shared", False),
        )

        # Create owner relationship
        if owner_email:
            await link_document_owner(
                session,
                drive_id=file_data["id"],
                owner_email=owner_email,
            )

        logger.debug(
            "Ingested document",
            drive_id=file_data["id"],
            name=file_data["name"][:50],
        )


async def ingest_document_batch(files: list[dict]) -> dict:
    """
    Ingest a batch of Drive files into the graph.

    Args:
        files: List of file metadata dicts

    Returns:
        Dict with ingestion stats
    """
    stats = {
        "total": len(files),
        "success": 0,
        "failed": 0,
        "errors": [],
    }

    for file_data in files:
        try:
            await ingest_document_to_graph(file_data)
            stats["success"] += 1
        except Exception as e:
            stats["failed"] += 1
            stats["errors"].append({
                "drive_id": file_data.get("id"),
                "name": file_data.get("name"),
                "error": str(e),
            })
            logger.warning(
                "Failed to ingest document",
                drive_id=file_data.get("id"),
                error=str(e),
            )

    logger.info(
        "Document batch ingestion complete",
        total=stats["total"],
        success=stats["success"],
        failed=stats["failed"],
    )

    return stats


async def run_drive_metadata_sync(cleanup_deleted: bool = True) -> dict:
    """
    Sync all Drive files (metadata only) into the graph.

    Args:
        cleanup_deleted: If True, remove documents from Neo4j that no longer exist in Drive

    Returns:
        Sync result stats
    """
    from cognitex.services.drive import get_drive_service

    logger.info("Starting Drive metadata sync")

    drive = get_drive_service()

    # Collect all files (blocking generator, wrap in thread)
    files = await asyncio.to_thread(lambda: list(drive.list_all_files()))
    drive_ids = {f["id"] for f in files}
    logger.info("Fetched Drive files", count=len(files))

    # Ingest into graph
    stats = await ingest_document_batch(files)

    # Clean up deleted files from Neo4j
    if cleanup_deleted:
        deleted_count = 0
        async for session in get_neo4j_session():
            # Find documents in Neo4j that aren't in Drive
            result = await session.run("""
                MATCH (d:Document)
                WHERE d.drive_id IS NOT NULL
                RETURN d.drive_id as drive_id
            """)
            existing = await result.data()
            existing_ids = {e["drive_id"] for e in existing}

            # Find IDs to delete (in Neo4j but not in Drive)
            to_delete = existing_ids - drive_ids

            if to_delete:
                # Delete orphaned documents
                await session.run("""
                    MATCH (d:Document)
                    WHERE d.drive_id IN $ids
                    DETACH DELETE d
                """, {"ids": list(to_delete)})
                deleted_count = len(to_delete)
                logger.info("Deleted orphaned documents", count=deleted_count)

        stats["deleted"] = deleted_count

    logger.info("Drive metadata sync complete", **stats)

    return stats


async def run_drive_folder_sync(folder_name: str) -> dict:
    """
    Sync all files in a specific folder (with path tracking).

    Args:
        folder_name: Name of the folder to sync

    Returns:
        Sync result stats
    """
    from cognitex.services.drive import get_drive_service

    logger.info("Starting Drive folder sync", folder=folder_name)

    drive = get_drive_service()

    # Find the folder
    folder_id = drive.get_folder_id_by_name(folder_name)
    if not folder_id:
        logger.warning("Folder not found", folder=folder_name)
        return {"error": f"Folder '{folder_name}' not found", "total": 0}

    # List files recursively
    files = list(drive.list_files_in_folder(folder_id, recursive=True))
    logger.info("Found files in folder", folder=folder_name, count=len(files))

    # Ingest into graph
    stats = await ingest_document_batch(files)
    stats["folder"] = folder_name

    logger.info("Drive folder sync complete", **stats)

    return stats


async def index_document_content(
    pg_session: AsyncSession,
    drive_id: str,
    content: str,
) -> int | None:
    """
    Store document content and generate embedding.

    Args:
        pg_session: PostgreSQL async session
        drive_id: Drive file ID
        content: Extracted text content

    Returns:
        Embedding ID if successful, None otherwise
    """
    from cognitex.services.llm import get_llm_service

    # Calculate content hash
    content_hash = hashlib.sha256(content.encode()).hexdigest()[:64]

    # Check if content already indexed with same hash AND embedding exists
    check_query = text("""
        SELECT dc.content_hash, e.id as embedding_id
        FROM document_content dc
        LEFT JOIN embeddings e ON e.entity_type = 'document' AND e.entity_id = dc.drive_id
        WHERE dc.drive_id = :drive_id
    """)
    result = await pg_session.execute(check_query, {"drive_id": drive_id})
    existing = result.fetchone()

    if existing and existing.content_hash == content_hash and existing.embedding_id:
        logger.debug("Document content unchanged and embedding exists, skipping", drive_id=drive_id)
        return str(existing.embedding_id)

    # Truncate content to stay under PostgreSQL tsvector limit (~1MB)
    # The FTS index trigger will fail for strings > 1048575 bytes
    MAX_CONTENT_LENGTH = 500_000  # ~500KB to be safe with multi-byte chars
    stored_content = content[:MAX_CONTENT_LENGTH] if len(content) > MAX_CONTENT_LENGTH else content

    # Store content
    upsert_query = text("""
        INSERT INTO document_content (drive_id, content, content_hash, char_count)
        VALUES (:drive_id, :content, :content_hash, :char_count)
        ON CONFLICT (drive_id) DO UPDATE SET
            content = :content,
            content_hash = :content_hash,
            char_count = :char_count,
            updated_at = NOW()
    """)
    await pg_session.execute(upsert_query, {
        "drive_id": drive_id,
        "content": stored_content,
        "content_hash": content_hash,
        "char_count": len(content),  # Store original length for reference
    })

    # Generate embedding (truncate to ~400 chars for bge-base-en-v1.5 512 token limit)
    # CSV/code files have many small tokens, so we need a smaller char limit
    llm = get_llm_service()
    embedding_text = content[:400]  # Conservative limit to stay under 512 tokens

    try:
        embedding = await llm.generate_embedding(embedding_text)
        # Convert list to pgvector string format for asyncpg
        embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"

        # Store embedding
        embedding_query = text("""
            INSERT INTO embeddings (entity_type, entity_id, content_hash, embedding)
            VALUES ('document', :drive_id, :content_hash, CAST(:embedding AS vector))
            ON CONFLICT (entity_type, entity_id) DO UPDATE SET
                content_hash = EXCLUDED.content_hash,
                embedding = EXCLUDED.embedding,
                created_at = NOW()
            RETURNING id
        """)
        result = await pg_session.execute(embedding_query, {
            "drive_id": drive_id,
            "content_hash": content_hash,
            "embedding": embedding_str,
        })
        row = result.fetchone()
        # Convert UUID to string to avoid asyncpg type issues
        embedding_id = str(row.id) if row else None

        await pg_session.commit()

        # Mark document as indexed in Neo4j
        async for neo_session in get_neo4j_session():
            await mark_document_indexed(
                neo_session,
                drive_id=drive_id,
                content_hash=content_hash,
                embedding_id=embedding_id,
            )

        logger.debug("Indexed document content", drive_id=drive_id, chars=len(content))
        return embedding_id

    except Exception as e:
        logger.warning("Failed to generate embedding", drive_id=drive_id, error=str(e))
        await pg_session.commit()  # Still save the content
        return None


async def run_priority_folder_indexing(
    pg_session: AsyncSession,
    folder_names: list[str] | None = None,
    limit: int = 100,
) -> dict:
    """
    Index content from priority folders (full text + embeddings).

    Args:
        pg_session: PostgreSQL async session
        folder_names: Folders to index (defaults to PRIORITY_FOLDERS)
        limit: Maximum documents to process

    Returns:
        Indexing stats
    """
    from cognitex.services.drive import get_drive_service, PRIORITY_FOLDERS

    folder_names = folder_names or PRIORITY_FOLDERS
    logger.info("Starting priority folder indexing", folders=folder_names)

    drive = get_drive_service()

    stats = {
        "total": 0,
        "indexed": 0,
        "skipped": 0,
        "failed": 0,
        "by_folder": {},
    }

    for folder_name in folder_names:
        folder_stats = {"indexed": 0, "skipped": 0, "failed": 0}

        folder_id = drive.get_folder_id_by_name(folder_name)
        if not folder_id:
            logger.warning("Priority folder not found", folder=folder_name)
            continue

        # Get files in folder
        for file_data in drive.list_files_in_folder(folder_id, recursive=True):
            if stats["total"] >= limit:
                break

            stats["total"] += 1

            # Skip folders
            if file_data["mimeType"] == "application/vnd.google-apps.folder":
                continue

            # Try to extract content (run in thread to avoid blocking)
            try:
                content = await asyncio.to_thread(
                    drive.get_file_content,
                    file_data["id"],
                    file_data["mimeType"]
                )

                if not content or len(content.strip()) < 100:
                    folder_stats["skipped"] += 1
                    stats["skipped"] += 1
                    continue

                # Index the content
                embedding_id = await index_document_content(
                    pg_session,
                    drive_id=file_data["id"],
                    content=content,
                )

                if embedding_id:
                    folder_stats["indexed"] += 1
                    stats["indexed"] += 1
                else:
                    folder_stats["skipped"] += 1
                    stats["skipped"] += 1

            except Exception as e:
                folder_stats["failed"] += 1
                stats["failed"] += 1
                logger.warning(
                    "Failed to index document",
                    drive_id=file_data["id"],
                    name=file_data["name"],
                    error=str(e),
                )
                # Rollback to recover from failed transaction
                await pg_session.rollback()

        stats["by_folder"][folder_name] = folder_stats

    logger.info("Priority folder indexing complete", **stats)

    return stats


# ============================================================================
# Deep document indexing with chunking
# ============================================================================


async def index_document_chunked(
    pg_session: AsyncSession,
    drive_id: str,
    content: str,
    mime_type: str | None = None,
) -> dict:
    """
    Index a document using semantic chunking for deep understanding.

    Splits the document into overlapping chunks, generates embeddings for each,
    and stores them for retrieval. This enables finding relevant passages
    within large documents.

    Args:
        pg_session: PostgreSQL async session
        drive_id: Drive file ID
        content: Full document content
        mime_type: MIME type hint for chunking strategy

    Returns:
        Dict with indexing stats (chunks_created, embeddings_created)
    """
    from cognitex.services.chunking import smart_chunk, compute_hash
    from cognitex.services.llm import get_llm_service

    stats = {"chunks_created": 0, "embeddings_created": 0, "skipped": 0}

    # Calculate content hash for the full document
    full_hash = compute_hash(content)

    # Check if already indexed with same hash
    check_query = text("""
        SELECT COUNT(*) as chunk_count FROM document_chunks
        WHERE drive_id = :drive_id
    """)
    result = await pg_session.execute(check_query, {"drive_id": drive_id})
    existing = result.fetchone()

    if existing and existing.chunk_count > 0:
        # Check if content changed by comparing first chunk hash
        hash_query = text("""
            SELECT content_hash FROM document_chunks
            WHERE drive_id = :drive_id AND chunk_index = 0
        """)
        hash_result = await pg_session.execute(hash_query, {"drive_id": drive_id})
        first_chunk = hash_result.fetchone()

        # Generate first chunk to compare
        chunks = smart_chunk(content, mime_type)
        if chunks and first_chunk and first_chunk.content_hash == chunks[0].content_hash:
            logger.debug("Document unchanged, skipping", drive_id=drive_id, chunks=existing.chunk_count)
            stats["skipped"] = existing.chunk_count
            return stats

        # Content changed - delete old chunks
        delete_query = text("DELETE FROM document_chunks WHERE drive_id = :drive_id")
        await pg_session.execute(delete_query, {"drive_id": drive_id})
        delete_embeddings = text("""
            DELETE FROM embeddings
            WHERE entity_type = 'chunk' AND entity_id LIKE :pattern
        """)
        await pg_session.execute(delete_embeddings, {"pattern": f"{drive_id}:%"})
    else:
        chunks = smart_chunk(content, mime_type)

    if not chunks:
        logger.debug("No chunks generated", drive_id=drive_id)
        return stats

    llm = get_llm_service()

    # Process chunks
    for chunk in chunks:
        # Store chunk
        insert_chunk = text("""
            INSERT INTO document_chunks
                (drive_id, chunk_index, content, content_hash, start_char, end_char, char_count)
            VALUES
                (:drive_id, :chunk_index, :content, :content_hash, :start_char, :end_char, :char_count)
            ON CONFLICT (drive_id, chunk_index) DO UPDATE SET
                content = EXCLUDED.content,
                content_hash = EXCLUDED.content_hash,
                start_char = EXCLUDED.start_char,
                end_char = EXCLUDED.end_char,
                char_count = EXCLUDED.char_count
        """)
        await pg_session.execute(insert_chunk, {
            "drive_id": drive_id,
            "chunk_index": chunk.chunk_index,
            "content": chunk.content,
            "content_hash": chunk.content_hash,
            "start_char": chunk.start_char,
            "end_char": chunk.end_char,
            "char_count": len(chunk.content),
        })
        stats["chunks_created"] += 1

        # Generate embedding for chunk
        # Use first 400 chars to stay within token limit
        embedding_text = chunk.content[:400]
        chunk_entity_id = f"{drive_id}:{chunk.chunk_index}"

        try:
            embedding = await llm.generate_embedding(embedding_text)
            embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"

            insert_embedding = text("""
                INSERT INTO embeddings (entity_type, entity_id, content_hash, embedding)
                VALUES ('chunk', :entity_id, :content_hash, CAST(:embedding AS vector))
                ON CONFLICT (entity_type, entity_id) DO UPDATE SET
                    content_hash = EXCLUDED.content_hash,
                    embedding = EXCLUDED.embedding,
                    created_at = NOW()
            """)
            await pg_session.execute(insert_embedding, {
                "entity_id": chunk_entity_id,
                "content_hash": chunk.content_hash,
                "embedding": embedding_str,
            })
            stats["embeddings_created"] += 1

        except Exception as e:
            logger.warning(
                "Failed to embed chunk",
                drive_id=drive_id,
                chunk_index=chunk.chunk_index,
                error=str(e),
            )

    await pg_session.commit()

    logger.info(
        "Indexed document with chunks",
        drive_id=drive_id,
        chunks=stats["chunks_created"],
        embeddings=stats["embeddings_created"],
    )

    return stats


async def run_deep_document_indexing(
    pg_session: AsyncSession,
    folder_names: list[str] | None = None,
    limit: int = 100,
    max_file_size: int = 10_000_000,  # 10MB default
) -> dict:
    """
    Index documents with deep chunking for comprehensive understanding.

    Memory-efficient: processes one document at a time, uses streaming.

    Args:
        pg_session: PostgreSQL async session
        folder_names: Folders to index (defaults to PRIORITY_FOLDERS)
        limit: Maximum documents to process
        max_file_size: Skip files larger than this (bytes)

    Returns:
        Indexing stats
    """
    from cognitex.services.drive import get_drive_service, PRIORITY_FOLDERS

    folder_names = folder_names or PRIORITY_FOLDERS
    logger.info("Starting deep document indexing", folders=folder_names, limit=limit)

    drive = get_drive_service()

    stats = {
        "documents_processed": 0,
        "chunks_total": 0,
        "embeddings_total": 0,
        "skipped_size": 0,
        "skipped_type": 0,
        "failed": 0,
        "by_folder": {},
    }

    # MIME types we can meaningfully index
    indexable_types = {
        'application/vnd.google-apps.document',
        'application/vnd.google-apps.spreadsheet',
        'text/plain',
        'text/csv',
        'text/markdown',
        'application/pdf',
        'application/json',
        'text/x-python',
        'application/javascript',
    }

    for folder_name in folder_names:
        folder_stats = {"docs": 0, "chunks": 0, "skipped": 0, "failed": 0}

        folder_id = drive.get_folder_id_by_name(folder_name)
        if not folder_id:
            logger.warning("Priority folder not found", folder=folder_name)
            continue

        for file_data in drive.list_files_in_folder(folder_id, recursive=True):
            if stats["documents_processed"] >= limit:
                break

            # Skip folders
            if file_data["mimeType"] == "application/vnd.google-apps.folder":
                continue

            # Skip large files
            file_size = int(file_data.get("size", 0))
            if file_size > max_file_size:
                logger.debug("Skipping large file", name=file_data["name"], size=file_size)
                stats["skipped_size"] += 1
                folder_stats["skipped"] += 1
                continue

            # Skip non-indexable types
            mime_type = file_data["mimeType"]
            if mime_type not in indexable_types and not mime_type.startswith('text/'):
                stats["skipped_type"] += 1
                folder_stats["skipped"] += 1
                continue

            try:
                # Extract content (run in thread to avoid blocking)
                content = await asyncio.to_thread(
                    drive.get_file_content,
                    file_data["id"],
                    mime_type
                )

                if not content or len(content.strip()) < 100:
                    folder_stats["skipped"] += 1
                    continue

                # Index with chunking
                result = await index_document_chunked(
                    pg_session,
                    drive_id=file_data["id"],
                    content=content,
                    mime_type=mime_type,
                )

                # Only count as processed if new work was done (not skipped)
                if result["chunks_created"] > 0:
                    stats["documents_processed"] += 1
                    stats["chunks_total"] += result["chunks_created"]
                    stats["embeddings_total"] += result["embeddings_created"]
                    folder_stats["docs"] += 1
                    folder_stats["chunks"] += result["chunks_created"]
                else:
                    # Document was already indexed with same content
                    folder_stats["skipped"] += 1

                # Free memory
                del content

            except Exception as e:
                folder_stats["failed"] += 1
                stats["failed"] += 1
                logger.warning(
                    "Failed to index document",
                    drive_id=file_data["id"],
                    name=file_data["name"],
                    error=str(e),
                )
                await pg_session.rollback()

        stats["by_folder"][folder_name] = folder_stats

    logger.info("Deep document indexing complete", **stats)

    return stats


async def search_chunks_semantic(
    pg_session: AsyncSession,
    query: str,
    limit: int = 10,
) -> list[dict]:
    """
    Search document chunks using semantic similarity.

    Returns the most relevant passages from across all indexed documents.

    Args:
        pg_session: PostgreSQL async session
        query: Search query text
        limit: Maximum results to return

    Returns:
        List of matching chunks with document info and similarity scores
    """
    from cognitex.services.llm import get_llm_service

    llm = get_llm_service()

    # Generate query embedding
    query_embedding = await llm.generate_embedding(query)
    query_embedding_str = "[" + ",".join(str(x) for x in query_embedding) + "]"

    # Search chunks
    search_query = text("""
        SELECT
            e.entity_id,
            dc.drive_id,
            dc.chunk_index,
            dc.content,
            dc.start_char,
            dc.end_char,
            1 - (e.embedding <=> CAST(:query_embedding AS vector)) as similarity
        FROM embeddings e
        JOIN document_chunks dc ON dc.drive_id || ':' || dc.chunk_index = e.entity_id
        WHERE e.entity_type = 'chunk'
        ORDER BY e.embedding <=> CAST(:query_embedding AS vector)
        LIMIT :limit
    """)

    result = await pg_session.execute(search_query, {
        "query_embedding": query_embedding_str,
        "limit": limit,
    })

    results = []
    for row in result.fetchall():
        results.append({
            "drive_id": row.drive_id,
            "chunk_index": row.chunk_index,
            "content": row.content,
            "start_char": row.start_char,
            "end_char": row.end_char,
            "similarity": float(row.similarity),
        })

    return results


# ============================================================================
# Code indexing (GitHub)
# ============================================================================


async def index_code_content(
    pg_session: AsyncSession,
    file_id: str,
    path: str,
    content: str,
    repo_name: str,
    skip_embedding: bool = False,
) -> int | None:
    """
    Store code file content and generate embedding for semantic search.

    Args:
        pg_session: PostgreSQL async session
        file_id: Unique file identifier (repo_id:path)
        path: File path within repository
        content: File content
        repo_name: Repository full name (owner/repo)

    Returns:
        Embedding ID if successful, None otherwise
    """
    from cognitex.services.llm import get_llm_service

    # Calculate content hash
    content_hash = hashlib.sha256(content.encode()).hexdigest()[:64]

    # Check if content already indexed with same hash
    check_query = text("""
        SELECT content_hash FROM code_content WHERE file_id = :file_id
    """)
    result = await pg_session.execute(check_query, {"file_id": file_id})
    existing = result.fetchone()

    if existing and existing.content_hash == content_hash:
        logger.debug("Code content unchanged, skipping", file_id=file_id)
        return None

    # Store content
    upsert_query = text("""
        INSERT INTO code_content (file_id, repo_name, path, content, content_hash, char_count)
        VALUES (:file_id, :repo_name, :path, :content, :content_hash, :char_count)
        ON CONFLICT (file_id) DO UPDATE SET
            content = :content,
            content_hash = :content_hash,
            char_count = :char_count,
            repo_name = :repo_name,
            path = :path,
            updated_at = NOW()
    """)
    await pg_session.execute(upsert_query, {
        "file_id": file_id,
        "repo_name": repo_name,
        "path": path,
        "content": content,
        "content_hash": content_hash,
        "char_count": len(content),
    })

    # Skip embedding generation if requested
    if skip_embedding:
        await pg_session.commit()
        logger.debug("Indexed code content (no embedding)", file_id=file_id, chars=len(content))
        return None

    # Generate embedding (truncate content if too long)
    llm = get_llm_service()

    # Create a summary for embedding that includes path context
    # Truncate to ~350 tokens (~1200 chars) for bge-base-en-v1.5 (512 token limit)
    embedding_text = f"File: {path}\n\n{content[:1100]}"

    try:
        embedding = await llm.generate_embedding(embedding_text)
        # Convert list to pgvector string format
        embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"

        # Store embedding
        embedding_query = text("""
            INSERT INTO embeddings (entity_type, entity_id, content_hash, embedding)
            VALUES ('code', :file_id, :content_hash, CAST(:embedding AS vector))
            ON CONFLICT (entity_type, entity_id) DO UPDATE SET
                content_hash = EXCLUDED.content_hash,
                embedding = EXCLUDED.embedding,
                created_at = NOW()
            RETURNING id
        """)
        result = await pg_session.execute(embedding_query, {
            "file_id": file_id,
            "content_hash": content_hash,
            "embedding": embedding_str,
        })
        row = result.fetchone()
        embedding_id = row.id if row else None

        await pg_session.commit()

        logger.debug("Indexed code content", file_id=file_id, chars=len(content))
        return embedding_id

    except Exception as e:
        logger.warning("Failed to generate code embedding", file_id=file_id, error=str(e))
        await pg_session.commit()  # Still save the content
        return None


async def search_code_semantic(
    pg_session: AsyncSession,
    query: str,
    repo_filter: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """
    Search code files using semantic similarity.

    Args:
        pg_session: PostgreSQL async session
        query: Search query text
        repo_filter: Optional repository name to filter results
        limit: Maximum results to return

    Returns:
        List of matching code files with similarity scores
    """
    from cognitex.services.llm import get_llm_service

    llm = get_llm_service()

    # Generate query embedding
    query_embedding = await llm.generate_embedding(query)
    # Convert list to pgvector string format
    query_embedding_str = "[" + ",".join(str(x) for x in query_embedding) + "]"

    # Build search query - use CAST for asyncpg compatibility
    if repo_filter:
        search_query = text("""
            SELECT
                e.entity_id as file_id,
                cc.repo_name,
                cc.path,
                cc.content,
                1 - (e.embedding <=> CAST(:query_embedding AS vector)) as similarity
            FROM embeddings e
            JOIN code_content cc ON cc.file_id = e.entity_id
            WHERE e.entity_type = 'code'
              AND cc.repo_name = :repo_filter
            ORDER BY e.embedding <=> CAST(:query_embedding AS vector)
            LIMIT :limit
        """)
        params = {
            "query_embedding": query_embedding_str,
            "repo_filter": repo_filter,
            "limit": limit,
        }
    else:
        search_query = text("""
            SELECT
                e.entity_id as file_id,
                cc.repo_name,
                cc.path,
                cc.content,
                1 - (e.embedding <=> CAST(:query_embedding AS vector)) as similarity
            FROM embeddings e
            JOIN code_content cc ON cc.file_id = e.entity_id
            WHERE e.entity_type = 'code'
            ORDER BY e.embedding <=> CAST(:query_embedding AS vector)
            LIMIT :limit
        """)
        params = {
            "query_embedding": query_embedding_str,
            "limit": limit,
        }

    result = await pg_session.execute(search_query, params)

    results = []
    for row in result.fetchall():
        results.append({
            "file_id": row.file_id,
            "repo_name": row.repo_name,
            "path": row.path,
            "content_preview": row.content[:500] if row.content else "",
            "similarity": float(row.similarity),
        })

    return results


async def search_documents_semantic(
    pg_session: AsyncSession,
    query: str,
    limit: int = 10,
) -> list[dict]:
    """
    Search documents using semantic similarity.

    Args:
        pg_session: PostgreSQL async session
        query: Search query text
        limit: Maximum results to return

    Returns:
        List of matching documents with similarity scores
    """
    from cognitex.services.llm import get_llm_service

    llm = get_llm_service()

    # Generate query embedding
    query_embedding = await llm.generate_embedding(query)

    # Search for similar documents
    # Format embedding as PostgreSQL array literal for pgvector
    embedding_str = "[" + ",".join(str(x) for x in query_embedding) + "]"

    search_query = text("""
        SELECT
            e.entity_id as drive_id,
            dc.content,
            1 - (e.embedding <=> CAST(:query_embedding AS vector)) as similarity
        FROM embeddings e
        JOIN document_content dc ON dc.drive_id = e.entity_id
        WHERE e.entity_type = 'document'
        ORDER BY e.embedding <=> CAST(:query_embedding AS vector)
        LIMIT :limit
    """).bindparams(
        bindparam("query_embedding", value=embedding_str),
        bindparam("limit", value=limit),
    )

    result = await pg_session.execute(search_query)

    results = []
    for row in result.fetchall():
        results.append({
            "drive_id": row.drive_id,
            "content_preview": row.content[:500],
            "similarity": float(row.similarity),
        })

    return results


# ============================================================================
# Chunk analysis and graph integration
# ============================================================================


async def analyze_chunk_for_graph(
    pg_session: AsyncSession,
    neo4j_session,
    chunk_id: str,
    document_name: str,
) -> dict:
    """
    Analyze a chunk using LLM and create graph relationships.

    Uses the planner model for deep semantic understanding and extracts:
    - People (with roles and context)
    - Organizations
    - Topics and concepts (with domains)
    - Semantic tags for clustering
    - Relationships between entities

    Args:
        pg_session: PostgreSQL async session
        neo4j_session: Neo4j async session
        chunk_id: The chunk ID (drive_id:chunk_index)
        document_name: Name of the parent document

    Returns:
        Dict with extraction results and counts
    """
    from cognitex.services.llm import get_llm_service
    from cognitex.db.graph_schema import (
        create_chunk,
        link_chunk_to_topic,
        link_chunk_to_concept,
        link_chunk_to_person,
        link_chunk_to_organization,
        link_chunk_to_semantic_tag,
    )

    # Parse chunk_id
    parts = chunk_id.rsplit(":", 1)
    if len(parts) != 2:
        logger.error("Invalid chunk_id format", chunk_id=chunk_id)
        return {"error": "Invalid chunk_id format"}

    drive_id, chunk_index_str = parts
    chunk_index = int(chunk_index_str)

    # Get chunk content from PostgreSQL
    query = text("""
        SELECT content FROM document_chunks
        WHERE drive_id = :drive_id AND chunk_index = :chunk_index
    """)
    result = await pg_session.execute(query, {"drive_id": drive_id, "chunk_index": chunk_index})
    row = result.fetchone()

    if not row:
        logger.warning("Chunk not found in database", chunk_id=chunk_id)
        return {"error": "Chunk not found"}

    content = row.content

    # Extract entities using LLM (uses planner model for deep understanding)
    llm = get_llm_service()
    entities = await llm.extract_entities_from_chunk(content, document_name, chunk_index)

    # Normalize key_facts to list of strings (handle both string and dict formats)
    # New format: [{"fact": "...", "category": "...", "confidence": "..."}]
    raw_key_facts = entities.get("key_facts", [])
    key_facts = []
    for kf in raw_key_facts:
        if isinstance(kf, dict):
            # Extract just the fact text from dict format
            fact_text = kf.get("fact", "")
            if fact_text:
                key_facts.append(fact_text)
        elif isinstance(kf, str) and kf:
            key_facts.append(kf)

    # Create chunk node in graph
    await create_chunk(
        neo4j_session,
        chunk_id=chunk_id,
        drive_id=drive_id,
        chunk_index=chunk_index,
        summary=entities.get("summary"),
        content_type=entities.get("content_type"),
        key_facts=key_facts,
    )

    # Link to topics
    topics = entities.get("topics", [])
    for topic in topics:
        if topic:
            await link_chunk_to_topic(neo4j_session, chunk_id, topic)

    # Link to concepts (handle both string and dict formats)
    concepts = entities.get("concepts", [])
    for concept in concepts:
        if concept:
            # New format: {"term": "...", "domain": "...", "definition": "..."}
            if isinstance(concept, dict):
                term = concept.get("term", "")
                if term:
                    await link_chunk_to_concept(neo4j_session, chunk_id, term)
            else:
                await link_chunk_to_concept(neo4j_session, chunk_id, concept)

    # Link to people (handle both string and dict formats)
    people = entities.get("people", [])
    for person in people:
        if person:
            # New format: {"name": "...", "role": "...", "context": "..."}
            if isinstance(person, dict):
                name = person.get("name", "")
                if name:
                    await link_chunk_to_person(neo4j_session, chunk_id, name)
            else:
                await link_chunk_to_person(neo4j_session, chunk_id, person)

    # Link to organizations (new in deep extraction)
    organizations = entities.get("organizations", [])
    for org in organizations:
        if org:
            if isinstance(org, dict):
                org_name = org.get("name", "")
                org_type = org.get("type", "")
                if org_name:
                    await link_chunk_to_organization(neo4j_session, chunk_id, org_name, org_type)
            else:
                await link_chunk_to_organization(neo4j_session, chunk_id, org)

    # Link to semantic tags (for clustering similar content)
    semantic_tags = entities.get("semantic_tags", [])
    for tag in semantic_tags:
        if tag:
            await link_chunk_to_semantic_tag(neo4j_session, chunk_id, tag)

    return {
        "chunk_id": chunk_id,
        "summary": entities.get("summary", ""),
        "content_type": entities.get("content_type"),
        "topics": len(topics),
        "concepts": len(concepts),
        "people": len(people),
        "organizations": len(organizations),
        "semantic_tags": len(semantic_tags),
        "key_facts": len(entities.get("key_facts", [])),
        "actionable_items": len(entities.get("actionable_items", [])),
    }


async def analyze_chunks_batch(
    pg_session: AsyncSession,
    neo4j_session,
    limit: int = 50,
) -> dict:
    """
    Analyze unanalyzed chunks in batch and create graph relationships.

    Args:
        pg_session: PostgreSQL async session
        neo4j_session: Neo4j async session
        limit: Maximum chunks to process

    Returns:
        Dict with processing statistics
    """
    # Get chunks that have embeddings - we'll check Neo4j for already-analyzed ones
    simple_query = text("""
        SELECT dc.drive_id, dc.chunk_index
        FROM document_chunks dc
        JOIN embeddings e ON e.entity_id = dc.drive_id || ':' || dc.chunk_index
            AND e.entity_type = 'chunk'
        ORDER BY dc.drive_id, dc.chunk_index
        LIMIT :limit
    """)

    result = await pg_session.execute(simple_query, {"limit": limit * 2})  # Fetch extra to account for already-analyzed
    chunks = result.fetchall()

    # Get document names from Neo4j
    doc_names = {}
    stats = {
        "processed": 0,
        "skipped": 0,
        "topics_created": 0,
        "concepts_created": 0,
        "people_linked": 0,
        "errors": 0,
    }

    for row in chunks:
        if stats["processed"] >= limit:
            break

        drive_id = row.drive_id
        chunk_index = row.chunk_index
        chunk_id = f"{drive_id}:{chunk_index}"

        # Check if already analyzed in Neo4j
        check_query = """
        MATCH (c:Chunk {id: $chunk_id})
        WHERE c.analyzed = true
        RETURN c.id
        """
        check_result = await neo4j_session.run(check_query, chunk_id=chunk_id)
        existing = await check_result.single()
        if existing:
            stats["skipped"] += 1
            continue

        # Get document name
        if drive_id not in doc_names:
            doc_query = """
            MATCH (d:Document {drive_id: $drive_id})
            RETURN d.name as name
            """
            doc_result = await neo4j_session.run(doc_query, drive_id=drive_id)
            doc_record = await doc_result.single()
            doc_names[drive_id] = doc_record["name"] if doc_record else "Unknown"

        document_name = doc_names[drive_id]

        try:
            analysis = await analyze_chunk_for_graph(
                pg_session, neo4j_session, chunk_id, document_name
            )

            if "error" not in analysis:
                stats["processed"] += 1
                stats["topics_created"] += analysis.get("topics", 0)
                stats["concepts_created"] += analysis.get("concepts", 0)
                stats["people_linked"] += analysis.get("people", 0)
                logger.info(
                    "Analyzed chunk",
                    chunk_id=chunk_id,
                    topics=analysis.get("topics", 0),
                    concepts=analysis.get("concepts", 0),
                )
            else:
                stats["errors"] += 1

        except Exception as e:
            logger.error("Error analyzing chunk", chunk_id=chunk_id, error=str(e))
            stats["errors"] += 1

    return stats


async def auto_index_drive_file(
    file_id: str,
    file_name: str,
    mime_type: str,
    max_file_size: int = 10_000_000,
) -> dict:
    """
    Automatically index a single Drive file with chunking and graph analysis.

    Called by Drive change webhooks when files are added or modified.

    Args:
        file_id: Google Drive file ID
        file_name: File name for logging
        mime_type: MIME type of the file
        max_file_size: Skip files larger than this (bytes)

    Returns:
        Indexing stats
    """
    from cognitex.services.drive import get_drive_service
    from cognitex.db.postgres import init_postgres, close_postgres, get_session
    from cognitex.db.neo4j import init_neo4j, close_neo4j, get_neo4j_session

    # MIME types we can meaningfully index
    indexable_types = {
        'application/vnd.google-apps.document',
        'application/vnd.google-apps.spreadsheet',
        'text/plain',
        'text/csv',
        'text/markdown',
        'application/pdf',
        'application/json',
        'text/x-python',
        'application/javascript',
    }

    stats = {
        "file_id": file_id,
        "file_name": file_name,
        "indexed": False,
        "chunks_created": 0,
        "embeddings_created": 0,
        "chunks_analyzed": 0,
        "topics_created": 0,
        "concepts_created": 0,
        "error": None,
    }

    # Check if file type is indexable
    if mime_type not in indexable_types and not mime_type.startswith('text/'):
        stats["error"] = f"Non-indexable MIME type: {mime_type}"
        logger.info("Skipping non-indexable file", file=file_name, mime_type=mime_type)
        return stats

    try:
        drive = get_drive_service()

        # Get file metadata to check size
        file_data = drive.get_file_metadata(file_id)
        if not file_data:
            stats["error"] = "File not found in Drive"
            return stats

        file_size = int(file_data.get("size", 0))
        if file_size > max_file_size:
            stats["error"] = f"File too large: {file_size} bytes"
            logger.info("Skipping large file", file=file_name, size=file_size)
            return stats

        # Extract content (run in thread to avoid blocking)
        content = await asyncio.to_thread(drive.get_file_content, file_id, mime_type)
        if not content or len(content.strip()) < 100:
            stats["error"] = "No meaningful content found"
            return stats

        logger.info("Auto-indexing Drive file", file=file_name, chars=len(content))

        # Initialize database connections
        await init_postgres()
        await init_neo4j()

        try:
            # Index with chunking
            async for pg_session in get_session():
                # First, delete old chunks for this file (in case of update)
                delete_query = text("""
                    DELETE FROM document_chunks WHERE drive_id = :drive_id
                """)
                await pg_session.execute(delete_query, {"drive_id": file_id})

                # Also delete old embeddings for those chunks
                delete_emb_query = text("""
                    DELETE FROM embeddings
                    WHERE entity_type = 'chunk'
                    AND entity_id LIKE :pattern
                """)
                await pg_session.execute(delete_emb_query, {"pattern": f"{file_id}:%"})
                await pg_session.commit()

                # Now index with chunking
                result = await index_document_chunked(
                    pg_session,
                    drive_id=file_id,
                    content=content,
                    mime_type=mime_type,
                )

                stats["indexed"] = True
                stats["chunks_created"] = result["chunks_created"]
                stats["embeddings_created"] = result["embeddings_created"]

                # Also update the document node in Neo4j
                async for neo4j_session in get_neo4j_session():
                    # Mark document as indexed
                    await mark_document_indexed(neo4j_session, file_id)

                    # Clean up old chunk nodes for this document
                    cleanup_query = """
                    MATCH (d:Document {drive_id: $drive_id})-[:HAS_CHUNK]->(c:Chunk)
                    DETACH DELETE c
                    """
                    await neo4j_session.run(cleanup_query, drive_id=file_id)

                    # Analyze chunks and create graph relationships
                    chunks_query = text("""
                        SELECT chunk_index FROM document_chunks
                        WHERE drive_id = :drive_id
                        ORDER BY chunk_index
                    """)
                    chunks_result = await pg_session.execute(chunks_query, {"drive_id": file_id})

                    for row in chunks_result.fetchall():
                        chunk_id = f"{file_id}:{row.chunk_index}"
                        try:
                            analysis = await analyze_chunk_for_graph(
                                pg_session, neo4j_session, chunk_id, file_name
                            )
                            if "error" not in analysis:
                                stats["chunks_analyzed"] += 1
                                stats["topics_created"] += analysis.get("topics", 0)
                                stats["concepts_created"] += analysis.get("concepts", 0)
                        except Exception as e:
                            logger.warning("Chunk analysis failed", chunk_id=chunk_id, error=str(e))

                    break
                break

        finally:
            await close_postgres()
            await close_neo4j()

        logger.info(
            "Auto-indexing complete",
            file=file_name,
            chunks=stats["chunks_created"],
            analyzed=stats["chunks_analyzed"],
            topics=stats["topics_created"],
        )

    except Exception as e:
        stats["error"] = str(e)
        logger.error("Auto-indexing failed", file=file_name, error=str(e))

    return stats


async def sync_github_repo(repo_name: str) -> dict:
    """
    Sync a GitHub repository to the graph and index code files.

    This is the programmatic API for the CLI's github-sync command,
    designed for use by the trigger system for automated daily syncs.

    Args:
        repo_name: Repository in 'owner/repo' format

    Returns:
        dict with keys: files_synced, files_total, repo_id, error (if any)
    """
    from pathlib import Path

    from cognitex.db.neo4j import init_neo4j, close_neo4j, get_neo4j_session
    from cognitex.db.postgres import init_postgres, close_postgres, get_session
    from cognitex.services.github import get_github_service
    from cognitex.db.graph_schema import create_repository, create_codefile

    result = {"files_synced": 0, "files_total": 0, "repo_id": None}

    try:
        await init_neo4j()
        github = get_github_service()

        # Get repo info
        repo = github.get_repo(repo_name)
        if not repo:
            result["error"] = f"Repository not found: {repo_name}"
            return result

        result["repo_id"] = repo["id"]

        # Create/update repository node
        async for session in get_neo4j_session():
            await create_repository(
                session,
                repo_id=repo["id"],
                name=repo["name"],
                full_name=repo["full_name"],
                url=repo["url"],
                description=repo["description"],
                primary_language=repo["language"],
                default_branch=repo["default_branch"],
            )
            break

        # Get indexable files
        files = list(github.get_indexable_files(repo_name))
        result["files_total"] = len(files)

        if files:
            await init_postgres()

            for file_info in files:
                try:
                    # Get file content
                    content = github.get_file_content(repo_name, file_info["path"])
                    if not content:
                        continue

                    # Create CodeFile node
                    async for session in get_neo4j_session():
                        file_id = f"{repo['id']}:{file_info['path']}"
                        # Detect language from extension
                        ext = Path(file_info["name"]).suffix.lower()
                        lang_map = {
                            ".py": "Python", ".js": "JavaScript", ".ts": "TypeScript",
                            ".tsx": "TypeScript", ".jsx": "JavaScript", ".go": "Go",
                            ".rs": "Rust", ".java": "Java", ".kt": "Kotlin",
                            ".rb": "Ruby", ".php": "PHP", ".swift": "Swift",
                            ".sql": "SQL", ".md": "Markdown", ".yml": "YAML",
                            ".yaml": "YAML", ".json": "JSON", ".toml": "TOML",
                            ".sh": "Shell", ".bash": "Shell",
                        }
                        language = lang_map.get(ext, ext[1:].upper() if ext else None)

                        await create_codefile(
                            session,
                            codefile_id=file_id,
                            path=file_info["path"],
                            name=file_info["name"],
                            repository_id=repo["id"],
                            language=language,
                        )
                        break

                    # Store content and generate embedding
                    async for pg_session in get_session():
                        await index_code_content(
                            pg_session,
                            file_id=file_id,
                            path=file_info["path"],
                            content=content,
                            repo_name=repo_name,
                            skip_embedding=False,
                        )
                        break

                    result["files_synced"] += 1

                except Exception as e:
                    logger.debug("Failed to sync file", path=file_info.get("path"), error=str(e))

            await close_postgres()

        await close_neo4j()
        logger.info("GitHub repo synced", repo=repo_name, files=result["files_synced"])

    except Exception as e:
        result["error"] = str(e)
        logger.error("GitHub sync failed", repo=repo_name, error=str(e))

    return result

