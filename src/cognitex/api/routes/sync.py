"""Sync API routes for remote session ingestion."""

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request

from cognitex.config import get_settings

logger = structlog.get_logger()
router = APIRouter()


def verify_sync_api_key(authorization: str = Header(None)) -> bool:
    """Verify the sync API key from Authorization header."""
    import hmac

    settings = get_settings()
    expected_key = settings.sync_api_key.get_secret_value()

    if not expected_key:
        raise HTTPException(
            status_code=503,
            detail="Sync API not configured. Set SYNC_API_KEY in environment.",
        )

    if not authorization:
        raise HTTPException(
            status_code=401,
            detail="Missing Authorization header. Use: Authorization: Bearer <api_key>",
        )

    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Invalid Authorization format. Use: Bearer <api_key>",
        )

    provided_key = authorization[7:]  # Remove "Bearer " prefix
    # Use timing-safe comparison to prevent timing attacks
    if not hmac.compare_digest(provided_key, expected_key):
        raise HTTPException(status_code=403, detail="Invalid API key")

    return True


async def _process_sync_batch(
    machine_id: str,
    cli_type: str,
    sessions: list[dict],
) -> None:
    """Process session sync in background - delegates to shared service method."""
    from cognitex.services.coding_sessions import get_session_ingester

    ingester = get_session_ingester()
    await ingester.process_sync_batch(machine_id, cli_type, sessions)


@router.post("/sessions")
async def sync_sessions(
    request: Request,
    background_tasks: BackgroundTasks,
    _auth: bool = Depends(verify_sync_api_key),
):
    """
    Ingest coding sessions from remote machines.

    Processing happens in background to avoid HTTP timeouts on large batches.
    Returns immediately with 'accepted' status.

    Accepts JSON with session data:
    {
        "machine_id": "laptop-chris",
        "cli_type": "claude",
        "sessions": [
            {
                "session_id": "abc123",
                "project_path": "/Users/chris/projects/myapp",
                "git_branch": "main",
                "started_at": "2025-01-03T10:00:00Z",
                "ended_at": "2025-01-03T11:30:00Z",
                "messages": [...],  # Optional: raw messages for LLM extraction
                "summary": "...",   # Optional: pre-extracted summary
                "decisions": [...],
                "next_steps": [...],
                "topics": [...],
            }
        ]
    }
    """
    try:
        data = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")

    machine_id = data.get("machine_id", "unknown")
    cli_type = data.get("cli_type", "claude")
    sessions = data.get("sessions", [])

    if not sessions:
        return {"status": "ok", "message": "No sessions to ingest", "queued": 0}

    # Offload processing to background task to prevent HTTP timeout
    background_tasks.add_task(_process_sync_batch, machine_id, cli_type, sessions)

    logger.info(
        "Session sync accepted",
        machine_id=machine_id,
        session_count=len(sessions),
    )

    return {
        "status": "accepted",
        "message": "Processing started in background",
        "machine_id": machine_id,
        "queued": len(sessions),
    }


@router.get("/status")
async def sync_status(_auth: bool = Depends(verify_sync_api_key)):
    """Check sync API status and get server info."""
    from cognitex.db.neo4j import get_driver

    driver = get_driver()
    async with driver.session() as session:
        result = await session.run(
            "MATCH (cs:CodingSession) RETURN count(cs) as count"
        )
        record = await result.single()
        session_count = record["count"] if record else 0

    return {
        "status": "ok",
        "version": "1.0.0",
        "total_sessions": session_count,
    }
