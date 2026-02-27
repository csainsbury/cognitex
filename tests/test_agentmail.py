"""Tests for WP8: AgentMail Integration."""

from __future__ import annotations

import hmac
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------


def test_config_defaults():
    """AgentMail is disabled by default with empty credentials."""
    from cognitex.config import Settings

    s = Settings(environment="testing")
    assert s.agentmail_enabled is False
    assert s.agentmail_api_key.get_secret_value() == ""
    assert s.agentmail_inbox_id == ""
    assert s.agentmail_webhook_secret.get_secret_value() == ""


# ---------------------------------------------------------------------------
# _to_email_data conversion
# ---------------------------------------------------------------------------


def _make_message(**overrides):
    """Create a mock Message with sensible defaults."""
    msg = MagicMock()
    msg.message_id = overrides.get("message_id", "msg_123")
    msg.thread_id = overrides.get("thread_id", "thd_456")
    msg.subject = overrides.get("subject", "Hello World")
    msg.timestamp = overrides.get("timestamp", datetime(2026, 1, 15, 10, 30))
    msg.from_ = overrides.get("from_", "Alice <alice@example.com>")
    msg.to = overrides.get("to", ["bob@example.com"])
    msg.cc = overrides.get("cc", None)
    msg.bcc = overrides.get("bcc", None)
    msg.preview = overrides.get("preview", "Hello, just checking in...")
    msg.labels = overrides.get("labels", ["inbox"])
    msg.size = overrides.get("size", 1234)
    msg.text = overrides.get("text", "Hello body")
    return msg


def test_to_email_data_basic():
    """Conversion produces correct dict format matching extract_email_metadata."""
    from cognitex.services.agentmail import _to_email_data

    msg = _make_message()
    data = _to_email_data(msg)

    assert data["gmail_id"] == "msg_123"
    assert data["thread_id"] == "thd_456"
    assert data["subject"] == "Hello World"
    assert data["sender_name"] == "Alice"
    assert data["sender_email"] == "alice@example.com"
    assert data["to"] == [("", "bob@example.com")]
    assert data["cc"] == []
    assert data["bcc"] == []
    assert data["snippet"] == "Hello, just checking in..."
    assert data["labels"] == ["inbox"]
    assert data["size_estimate"] == 1234


def test_to_email_data_no_name():
    """Handles plain email address without display name."""
    from cognitex.services.agentmail import _to_email_data

    msg = _make_message(from_="plain@example.com")
    data = _to_email_data(msg)
    assert data["sender_name"] == ""
    assert data["sender_email"] == "plain@example.com"


def test_to_email_data_no_subject():
    """Missing subject gets default."""
    from cognitex.services.agentmail import _to_email_data

    msg = _make_message(subject=None)
    data = _to_email_data(msg)
    assert data["subject"] == "(no subject)"


def test_to_email_data_cc_bcc():
    """CC and BCC recipients are parsed correctly."""
    from cognitex.services.agentmail import _to_email_data

    msg = _make_message(
        cc=["Carol <carol@example.com>", "dave@example.com"],
        bcc=["eve@secret.com"],
    )
    data = _to_email_data(msg)
    assert len(data["cc"]) == 2
    assert data["cc"][0] == ("Carol", "carol@example.com")
    assert data["cc"][1] == ("", "dave@example.com")
    assert data["bcc"] == [("", "eve@secret.com")]


# ---------------------------------------------------------------------------
# EmailProvider routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_routes_to_gmail_when_disabled():
    """EmailProvider uses Gmail when agentmail_enabled=False."""
    with patch("cognitex.services.email_provider.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(agentmail_enabled=False)

        from cognitex.services.email_provider import EmailProvider

        provider = EmailProvider()
        assert provider.provider_name == "gmail"


@pytest.mark.asyncio
async def test_provider_routes_to_agentmail_when_enabled():
    """EmailProvider uses AgentMail when agentmail_enabled=True."""
    with patch("cognitex.services.email_provider.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(agentmail_enabled=True)

        from cognitex.services.email_provider import EmailProvider

        provider = EmailProvider()
        assert provider.provider_name == "agentmail"


@pytest.mark.asyncio
async def test_provider_get_profile_agentmail():
    """get_profile delegates to AgentMail when enabled."""
    with (
        patch("cognitex.services.email_provider.get_settings") as mock_settings,
        patch("cognitex.services.agentmail.get_agentmail_service") as mock_get_svc,
    ):
        mock_settings.return_value = MagicMock(agentmail_enabled=True)
        mock_svc = AsyncMock()
        mock_svc.get_inbox.return_value = {
            "inbox_id": "inbox_test",
            "display_name": "Test Inbox",
        }
        mock_get_svc.return_value = mock_svc

        from cognitex.services.email_provider import EmailProvider

        provider = EmailProvider()
        profile = await provider.get_profile()

        assert profile["provider"] == "agentmail"
        assert profile["inbox_id"] == "inbox_test"


@pytest.mark.asyncio
async def test_provider_create_draft_agentmail():
    """create_draft delegates to AgentMail when enabled."""
    with (
        patch("cognitex.services.email_provider.get_settings") as mock_settings,
        patch("cognitex.services.agentmail.get_agentmail_service") as mock_get_svc,
    ):
        mock_settings.return_value = MagicMock(agentmail_enabled=True)
        mock_svc = AsyncMock()
        mock_svc.create_draft.return_value = {
            "id": "draft_1", "thread_id": "thd_1", "to": ["a@b.com"], "subject": "Hi",
        }
        mock_get_svc.return_value = mock_svc

        from cognitex.services.email_provider import EmailProvider

        provider = EmailProvider()
        result = await provider.create_draft(to="a@b.com", subject="Hi", body="Hello")

        mock_svc.create_draft.assert_awaited_once()
        assert result["id"] == "draft_1"


# ---------------------------------------------------------------------------
# Webhook handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_rejects_bad_secret():
    """Webhook returns 401 when secret doesn't match."""
    from fastapi.testclient import TestClient
    from fastapi import FastAPI

    # Build a mini app with the webhook router
    from cognitex.api.routes.webhooks import router

    test_app = FastAPI()
    test_app.include_router(router, prefix="/webhooks")
    client = TestClient(test_app)

    with patch("cognitex.config.get_settings") as mock_settings:
        mock_s = MagicMock()
        mock_s.agentmail_webhook_secret.get_secret_value.return_value = "real_secret"
        mock_settings.return_value = mock_s

        response = client.post(
            "/webhooks/agentmail",
            json={"event_type": "message.received", "data": {"message_id": "m1"}},
            headers={"X-Webhook-Secret": "wrong_secret"},
        )
        assert response.status_code == 401


@pytest.mark.asyncio
async def test_webhook_accepts_valid_secret():
    """Webhook returns 200 when secret matches."""
    from fastapi.testclient import TestClient
    from fastapi import FastAPI

    from cognitex.api.routes.webhooks import router

    test_app = FastAPI()
    test_app.include_router(router, prefix="/webhooks")
    client = TestClient(test_app)

    with patch("cognitex.config.get_settings") as mock_settings:
        mock_s = MagicMock()
        mock_s.agentmail_webhook_secret.get_secret_value.return_value = "real_secret"
        mock_settings.return_value = mock_s

        response = client.post(
            "/webhooks/agentmail",
            json={"event_type": "message.received", "data": {"message_id": "m1"}},
            headers={"X-Webhook-Secret": "real_secret"},
        )
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_webhook_returns_200_on_error():
    """Webhook always returns 200 even on processing errors."""
    from fastapi.testclient import TestClient
    from fastapi import FastAPI

    from cognitex.api.routes.webhooks import router

    test_app = FastAPI()
    test_app.include_router(router, prefix="/webhooks")
    client = TestClient(test_app)

    with patch("cognitex.config.get_settings") as mock_settings:
        mock_s = MagicMock()
        mock_s.agentmail_webhook_secret.get_secret_value.return_value = ""
        mock_settings.return_value = mock_s

        # Send garbage that will fail to parse properly but should still 200
        response = client.post(
            "/webhooks/agentmail",
            json={"event_type": "message.received", "data": {}},
        )
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_webhook_no_secret_required_when_unconfigured():
    """Webhook skips auth when no secret is configured."""
    from fastapi.testclient import TestClient
    from fastapi import FastAPI

    from cognitex.api.routes.webhooks import router

    test_app = FastAPI()
    test_app.include_router(router, prefix="/webhooks")
    client = TestClient(test_app)

    with patch("cognitex.config.get_settings") as mock_settings:
        mock_s = MagicMock()
        mock_s.agentmail_webhook_secret.get_secret_value.return_value = ""
        mock_settings.return_value = mock_s

        response = client.post(
            "/webhooks/agentmail",
            json={"event_type": "message.sent", "data": {"message_id": "m2"}},
        )
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# AgentMailService singleton
# ---------------------------------------------------------------------------


def test_get_agentmail_service_returns_none_when_disabled():
    """Singleton returns None when agentmail is disabled."""
    import cognitex.services.agentmail as mod

    mod._agentmail_service = None  # reset singleton

    with patch.object(mod, "get_settings") as mock_settings:
        mock_s = MagicMock()
        mock_s.agentmail_enabled = False
        mock_settings.return_value = mock_s

        result = mod.get_agentmail_service()
        assert result is None

    mod._agentmail_service = None  # cleanup


def test_get_agentmail_service_returns_none_without_key():
    """Singleton returns None when enabled but API key is empty."""
    import cognitex.services.agentmail as mod

    mod._agentmail_service = None

    with patch.object(mod, "get_settings") as mock_settings:
        mock_s = MagicMock()
        mock_s.agentmail_enabled = True
        mock_s.agentmail_api_key.get_secret_value.return_value = ""
        mock_settings.return_value = mock_s

        result = mod.get_agentmail_service()
        assert result is None

    mod._agentmail_service = None


# ---------------------------------------------------------------------------
# /email slash command
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_email_command_gmail():
    """/email shows Gmail status when agentmail disabled."""
    with (
        patch("cognitex.services.email_provider.get_email_provider") as mock_get,
    ):
        mock_provider = AsyncMock()
        mock_provider.provider_name = "gmail"
        mock_provider.get_profile.return_value = {
            "provider": "gmail",
            "email": "user@gmail.com",
            "messages_total": 42,
        }
        mock_get.return_value = mock_provider

        from cognitex.agent.slash_commands import _handle_email

        result = await _handle_email("")
        assert "Gmail" in result
        assert "user@gmail.com" in result


@pytest.mark.asyncio
async def test_email_command_agentmail():
    """/email shows AgentMail status when enabled."""
    with (
        patch("cognitex.services.email_provider.get_email_provider") as mock_get,
    ):
        mock_provider = AsyncMock()
        mock_provider.provider_name = "agentmail"
        mock_provider.get_profile.return_value = {
            "provider": "agentmail",
            "inbox_id": "inbox_abc",
            "display_name": "My Agent",
        }
        mock_get.return_value = mock_provider

        from cognitex.agent.slash_commands import _handle_email

        result = await _handle_email("")
        assert "AgentMail" in result
        assert "inbox_abc" in result
