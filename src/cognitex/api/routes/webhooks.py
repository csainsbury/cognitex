"""Webhook endpoints for Google Push Notifications."""

import json
from typing import Optional

import structlog
from fastapi import APIRouter, Request, Response, Header, BackgroundTasks

from cognitex.db.redis import get_redis

logger = structlog.get_logger()

router = APIRouter()


async def publish_event(channel: str, data: dict) -> None:
    """Publish an event to Redis pub/sub for the agent to handle."""
    redis = get_redis()
    await redis.publish(channel, json.dumps(data))
    logger.info("Published event to Redis", channel=channel)


# =============================================================================
# Gmail Webhook (via Pub/Sub push subscription)
# =============================================================================

@router.post("/google/gmail")
async def gmail_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
) -> Response:
    """
    Receive Gmail push notifications from Google Cloud Pub/Sub.

    Pub/Sub sends a POST with JSON body containing:
    {
        "message": {
            "data": "<base64 encoded>",  // Contains emailAddress and historyId
            "messageId": "...",
            "publishTime": "..."
        },
        "subscription": "projects/.../subscriptions/..."
    }
    """
    try:
        body = await request.json()
        message = body.get("message", {})

        # Decode the data
        import base64
        data_b64 = message.get("data", "")
        if data_b64:
            data_json = base64.b64decode(data_b64).decode("utf-8")
            data = json.loads(data_json)
        else:
            data = {}

        email_address = data.get("emailAddress")
        history_id = data.get("historyId")

        logger.info(
            "Gmail push notification received",
            email_address=email_address,
            history_id=history_id,
        )

        # Publish to Redis for agent processing
        background_tasks.add_task(
            publish_event,
            "cognitex:events:email",
            {
                "type": "gmail_push",
                "email_address": email_address,
                "history_id": history_id,
            }
        )

        # Acknowledge the message
        return Response(status_code=200)

    except Exception as e:
        logger.error("Gmail webhook error", error=str(e))
        # Return 200 anyway to avoid Pub/Sub retries for malformed messages
        return Response(status_code=200)


# =============================================================================
# Calendar Webhook
# =============================================================================

@router.post("/google/calendar")
async def calendar_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_goog_channel_id: Optional[str] = Header(None, alias="X-Goog-Channel-ID"),
    x_goog_channel_token: Optional[str] = Header(None, alias="X-Goog-Channel-Token"),
    x_goog_resource_id: Optional[str] = Header(None, alias="X-Goog-Resource-ID"),
    x_goog_resource_state: Optional[str] = Header(None, alias="X-Goog-Resource-State"),
    x_goog_resource_uri: Optional[str] = Header(None, alias="X-Goog-Resource-URI"),
    x_goog_message_number: Optional[str] = Header(None, alias="X-Goog-Message-Number"),
) -> Response:
    """
    Receive Calendar push notifications.

    Google sends notifications via HTTP headers (no body).
    Resource states: sync, exists, not_exists
    """
    logger.info(
        "Calendar push notification received",
        channel_id=x_goog_channel_id,
        resource_state=x_goog_resource_state,
        message_number=x_goog_message_number,
    )

    # Skip sync messages (initial notification)
    if x_goog_resource_state == "sync":
        logger.debug("Calendar sync message received, ignoring")
        return Response(status_code=200)

    # Extract calendar ID from token if present
    calendar_id = "primary"
    if x_goog_channel_token and x_goog_channel_token.startswith("calendar:"):
        parts = x_goog_channel_token.split(":")
        if len(parts) >= 2:
            calendar_id = parts[1]

    # Publish to Redis for agent processing
    background_tasks.add_task(
        publish_event,
        "cognitex:events:calendar",
        {
            "type": "calendar_push",
            "calendar_id": calendar_id,
            "channel_id": x_goog_channel_id,
            "resource_id": x_goog_resource_id,
            "resource_state": x_goog_resource_state,
            "resource_uri": x_goog_resource_uri,
        }
    )

    return Response(status_code=200)


# =============================================================================
# Drive Webhook
# =============================================================================

@router.post("/google/drive")
async def drive_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_goog_channel_id: Optional[str] = Header(None, alias="X-Goog-Channel-ID"),
    x_goog_channel_token: Optional[str] = Header(None, alias="X-Goog-Channel-Token"),
    x_goog_resource_id: Optional[str] = Header(None, alias="X-Goog-Resource-ID"),
    x_goog_resource_state: Optional[str] = Header(None, alias="X-Goog-Resource-State"),
    x_goog_resource_uri: Optional[str] = Header(None, alias="X-Goog-Resource-URI"),
    x_goog_message_number: Optional[str] = Header(None, alias="X-Goog-Message-Number"),
    x_goog_changed: Optional[str] = Header(None, alias="X-Goog-Changed"),
) -> Response:
    """
    Receive Drive push notifications.

    Google sends notifications via HTTP headers.
    Resource states: sync, add, update, remove, trash, untrash, change
    X-Goog-Changed: comma-separated list of change types
    """
    logger.info(
        "Drive push notification received",
        channel_id=x_goog_channel_id,
        resource_state=x_goog_resource_state,
        changed=x_goog_changed,
        message_number=x_goog_message_number,
    )

    # Skip sync messages
    if x_goog_resource_state == "sync":
        logger.debug("Drive sync message received, ignoring")
        return Response(status_code=200)

    # Publish to Redis for agent processing
    background_tasks.add_task(
        publish_event,
        "cognitex:events:drive",
        {
            "type": "drive_push",
            "channel_id": x_goog_channel_id,
            "resource_id": x_goog_resource_id,
            "resource_state": x_goog_resource_state,
            "changed": x_goog_changed.split(",") if x_goog_changed else [],
        }
    )

    return Response(status_code=200)


# =============================================================================
# Health check for webhook endpoint
# =============================================================================

@router.get("/health")
async def webhooks_health() -> dict:
    """Health check for webhook endpoints."""
    return {
        "status": "ok",
        "endpoints": [
            "/webhooks/google/gmail",
            "/webhooks/google/calendar",
            "/webhooks/google/drive",
        ]
    }
