"""Webhook endpoints for Google Push Notifications and AgentMail."""

import hmac
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
# AgentMail Webhook
# =============================================================================


async def _process_agentmail_event(event_type: str, data: dict) -> None:
    """Process an AgentMail webhook event in the background."""
    try:
        if event_type != "message.received":
            logger.debug("Ignoring AgentMail event", event_type=event_type)
            return

        message_id = data.get("message_id") or data.get("messageId")
        if not message_id:
            logger.warning("AgentMail event missing message_id", data=data)
            return

        from cognitex.services.agentmail import get_agentmail_service

        svc = get_agentmail_service()
        if not svc:
            logger.error("AgentMail service unavailable for webhook processing")
            return

        email_data = await svc.get_message(message_id)
        body = await svc.get_message_body(message_id)

        # Clinical firewall — runs BEFORE any LLM call
        from cognitex.config import get_settings

        settings = get_settings()
        if settings.clinical_firewall_enabled:
            from cognitex.services.clinical_firewall import get_firewall

            firewall = get_firewall()
            scan_text = f"{email_data.get('subject', '')} {body}"
            result = firewall.scan(scan_text)
            if result.is_clinical and settings.clinical_firewall_mode == "block":
                logger.info(
                    "AgentMail message blocked by clinical firewall",
                    message_id=message_id,
                    reason=result.reason,
                )
                return

        from cognitex.services.ingestion import ingest_email_to_graph

        await ingest_email_to_graph(email_data, classify=True, body=body)
        logger.info("AgentMail message ingested", message_id=message_id)

        # Publish to Redis for real-time dashboard updates
        await publish_event(
            "cognitex:events:email",
            {
                "type": "agentmail_received",
                "message_id": message_id,
                "subject": email_data.get("subject", ""),
                "sender": email_data.get("sender_email", ""),
            },
        )

    except Exception as e:
        logger.error("AgentMail webhook processing error", error=str(e), event_type=event_type)


@router.post("/agentmail")
async def agentmail_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_webhook_secret: str | None = Header(None, alias="X-Webhook-Secret"),
) -> Response:
    """
    Receive AgentMail webhook events.

    Events: message.received, message.sent, message.delivered, message.bounced
    """
    try:
        # Verify webhook secret
        from cognitex.config import get_settings

        settings = get_settings()
        expected_secret = settings.agentmail_webhook_secret.get_secret_value()
        if expected_secret and (
            not x_webhook_secret
            or not hmac.compare_digest(x_webhook_secret, expected_secret)
        ):
            logger.warning("AgentMail webhook: invalid secret")
            return Response(status_code=401)

        body = await request.json()
        event_type = body.get("event_type") or body.get("type", "")
        data = body.get("data", body)

        logger.info("AgentMail webhook received", event_type=event_type)

        background_tasks.add_task(_process_agentmail_event, event_type, data)

        return Response(status_code=200)

    except Exception as e:
        logger.error("AgentMail webhook error", error=str(e))
        # Return 200 to prevent retry storms
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
            "/webhooks/agentmail",
        ]
    }
