"""Web authentication using Google OAuth2."""

from __future__ import annotations

import json
import secrets
from datetime import datetime

import structlog
from google_auth_oauthlib.flow import Flow
from itsdangerous import URLSafeTimedSerializer, BadSignature

from cognitex.config import get_settings
from cognitex.db.redis import get_redis

logger = structlog.get_logger()

# Session configuration
SESSION_COOKIE_NAME = "cognitex_session"
SESSION_MAX_AGE = 7 * 24 * 60 * 60  # 7 days in seconds
REDIS_SESSION_PREFIX = "cognitex:auth:session:"
REDIS_STATE_PREFIX = "cognitex:auth:state:"

# OAuth scopes - only need email/profile for login
LOGIN_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]


def get_serializer() -> URLSafeTimedSerializer:
    """Get serializer for signing session tokens."""
    settings = get_settings()
    # Use google_client_secret as signing key (already a secret)
    secret = settings.google_client_secret.get_secret_value()
    if not secret:
        raise ValueError("google_client_secret must be set for web authentication")
    return URLSafeTimedSerializer(secret)


def get_allowed_emails() -> set[str]:
    """Get set of allowed email addresses."""
    settings = get_settings()
    emails_str = settings.web_allowed_emails
    if not emails_str:
        return set()
    return {e.strip().lower() for e in emails_str.split(",") if e.strip()}


def create_oauth_flow(redirect_uri: str) -> Flow:
    """Create OAuth flow for web authentication."""
    settings = get_settings()

    if not settings.google_client_id or not settings.google_client_secret.get_secret_value():
        raise ValueError("Google OAuth credentials not configured")

    client_config = {
        "web": {
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret.get_secret_value(),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri],
        }
    }

    flow = Flow.from_client_config(
        client_config,
        scopes=LOGIN_SCOPES,
        redirect_uri=redirect_uri,
    )
    return flow


async def create_session(user_email: str, user_name: str | None = None) -> str:
    """Create a new session and return signed session ID."""
    redis = get_redis()
    serializer = get_serializer()

    # Generate session ID
    session_id = secrets.token_urlsafe(32)

    # Store session data in Redis
    session_data = {
        "email": user_email,
        "name": user_name,
        "created_at": datetime.utcnow().isoformat(),
    }

    await redis.setex(
        f"{REDIS_SESSION_PREFIX}{session_id}",
        SESSION_MAX_AGE,
        json.dumps(session_data),
    )

    logger.debug("Session created", email=user_email, session_id=session_id[:8])

    # Return signed session ID
    return serializer.dumps(session_id)


async def verify_session(signed_session_id: str, refresh: bool = True) -> dict | None:
    """Verify session and return user data or None.

    Args:
        signed_session_id: The signed session cookie value
        refresh: If True, extends the Redis TTL on successful verification (sliding window)
    """
    if not signed_session_id:
        return None

    serializer = get_serializer()
    redis = get_redis()

    try:
        # Verify signature and extract session ID
        session_id = serializer.loads(signed_session_id, max_age=SESSION_MAX_AGE)
    except BadSignature:
        logger.warning("Invalid session signature")
        return None
    except Exception as e:
        logger.warning("Session verification failed", error=str(e))
        return None

    # Get session data from Redis
    redis_key = f"{REDIS_SESSION_PREFIX}{session_id}"
    session_json = await redis.get(redis_key)
    if not session_json:
        logger.debug("Session not found in Redis", session_id=session_id[:8])
        return None

    try:
        session_data = json.loads(session_json)

        # Refresh the session TTL on each successful verification (sliding window)
        # This keeps active users logged in
        if refresh:
            await redis.expire(redis_key, SESSION_MAX_AGE)

        return session_data
    except json.JSONDecodeError:
        logger.warning("Invalid session data in Redis")
        return None


async def destroy_session(signed_session_id: str) -> None:
    """Destroy a session."""
    if not signed_session_id:
        return

    serializer = get_serializer()
    redis = get_redis()

    try:
        session_id = serializer.loads(signed_session_id, max_age=SESSION_MAX_AGE)
        await redis.delete(f"{REDIS_SESSION_PREFIX}{session_id}")
        logger.debug("Session destroyed", session_id=session_id[:8])
    except BadSignature:
        pass  # Invalid session, nothing to destroy


async def store_oauth_state(state: str, next_url: str) -> None:
    """Store OAuth state for callback verification."""
    redis = get_redis()
    # 10 minute TTL for state
    await redis.setex(f"{REDIS_STATE_PREFIX}{state}", 600, next_url)


async def verify_oauth_state(state: str) -> str | None:
    """Verify OAuth state and return next URL, then delete state."""
    redis = get_redis()
    next_url = await redis.get(f"{REDIS_STATE_PREFIX}{state}")
    if next_url:
        await redis.delete(f"{REDIS_STATE_PREFIX}{state}")
        return next_url
    return None
