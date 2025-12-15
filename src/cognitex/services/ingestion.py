"""Email, calendar, and Drive ingestion pipeline - processes data and stores in graph."""

import hashlib
from datetime import datetime

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from cognitex.db.neo4j import get_neo4j_session
from cognitex.db.graph_schema import (
    create_document,
    create_email,
    create_event,
    create_person,
    link_document_owner,
    link_document_shared_with,
    link_email_recipient,
    link_email_sender,
    link_event_attendee,
    link_event_organizer,
    mark_document_indexed,
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
    async for session in get_neo4j_session():
        # Create sender Person node
        if email_data["sender_email"]:
            await create_person(
                session,
                email=email_data["sender_email"],
                name=email_data["sender_name"] or None,
            )

        # Create recipient Person nodes
        for name, email_addr in email_data.get("to", []):
            if email_addr:
                await create_person(session, email=email_addr, name=name or None)

        for name, email_addr in email_data.get("cc", []):
            if email_addr:
                await create_person(session, email=email_addr, name=name or None)

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
        await redis.set(redis_key, history_id)
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
        history = gmail.get_history(
            start_history_id=last_history_id,
            history_types=["messageAdded"],
        )
    except Exception as e:
        # History ID might be too old - reset and try again next time
        logger.warning("History sync failed, resetting history ID", error=str(e))
        await redis.set(redis_key, history_id)
        return {"error": str(e), "fallback_needed": True}

    # Extract new message IDs from history
    new_message_ids = set()
    for record in history.get("history", []):
        for msg in record.get("messagesAdded", []):
            new_message_ids.add(msg["message"]["id"])

    # Update stored history ID
    new_history_id = history.get("historyId", history_id)
    await redis.set(redis_key, new_history_id)

    if not new_message_ids:
        logger.info("No new messages found")
        return {
            "total": 0,
            "success": 0,
            "history_id": new_history_id,
        }

    logger.info("Found new messages", count=len(new_message_ids))

    # Fetch full metadata for new messages
    messages = gmail.get_message_batch(list(new_message_ids), format="metadata")
    from cognitex.services.gmail import extract_email_metadata
    email_data = [extract_email_metadata(msg) for msg in messages]

    # Ingest into graph
    stats = await ingest_email_batch(email_data)
    stats["history_id"] = new_history_id
    stats["emails"] = email_data  # Include email data for agent processing

    logger.info("Incremental sync complete", **{k: v for k, v in stats.items() if k != "emails"})

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


async def run_drive_metadata_sync() -> dict:
    """
    Sync all Drive files (metadata only) into the graph.

    Returns:
        Sync result stats
    """
    from cognitex.services.drive import get_drive_service

    logger.info("Starting Drive metadata sync")

    drive = get_drive_service()

    # Collect all files
    files = list(drive.list_all_files())
    logger.info("Fetched Drive files", count=len(files))

    # Ingest into graph
    stats = await ingest_document_batch(files)

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

    # Check if content already indexed with same hash
    check_query = text("""
        SELECT content_hash FROM document_content WHERE drive_id = :drive_id
    """)
    result = await pg_session.execute(check_query, {"drive_id": drive_id})
    existing = result.fetchone()

    if existing and existing.content_hash == content_hash:
        logger.debug("Document content unchanged, skipping", drive_id=drive_id)
        return None

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
        "content": content,
        "content_hash": content_hash,
        "char_count": len(content),
    })

    # Generate embedding (truncate content if too long)
    llm = get_llm_service()
    embedding_text = content[:8000]  # m2-bert supports 8k tokens

    try:
        embedding = await llm.generate_embedding(embedding_text)

        # Store embedding
        embedding_query = text("""
            INSERT INTO embeddings (entity_type, entity_id, content_hash, embedding)
            VALUES ('document', :drive_id, :content_hash, :embedding)
            ON CONFLICT (entity_type, entity_id) DO UPDATE SET
                content_hash = :content_hash,
                embedding = :embedding,
                created_at = NOW()
            RETURNING id
        """)
        result = await pg_session.execute(embedding_query, {
            "drive_id": drive_id,
            "content_hash": content_hash,
            "embedding": embedding,
        })
        row = result.fetchone()
        embedding_id = row.id if row else None

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

            # Try to extract content
            try:
                content = drive.get_file_content(file_data["id"], file_data["mimeType"])

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

        stats["by_folder"][folder_name] = folder_stats

    logger.info("Priority folder indexing complete", **stats)

    return stats


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
    search_query = text("""
        SELECT
            e.entity_id as drive_id,
            dc.content,
            1 - (e.embedding <=> :query_embedding::vector) as similarity
        FROM embeddings e
        JOIN document_content dc ON dc.drive_id = e.entity_id
        WHERE e.entity_type = 'document'
        ORDER BY e.embedding <=> :query_embedding::vector
        LIMIT :limit
    """)

    result = await pg_session.execute(search_query, {
        "query_embedding": query_embedding,
        "limit": limit,
    })

    results = []
    for row in result.fetchall():
        results.append({
            "drive_id": row.drive_id,
            "content_preview": row.content[:500],
            "similarity": float(row.similarity),
        })

    return results
