"""AgentMail service — wraps the AgentMail SDK for agent-specific email."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import structlog
from agentmail import AsyncAgentMail, Message

from cognitex.config import get_settings

logger = structlog.get_logger()


class AgentMailService:
    """Service for interacting with AgentMail API.

    Provides the same email operations as Gmail but via a dedicated
    agent inbox, removing direct access to the operator's personal email.
    """

    def __init__(self, api_key: str, inbox_id: str) -> None:
        self._client = AsyncAgentMail(api_key=api_key)
        self._inbox_id = inbox_id

    @property
    def inbox_id(self) -> str:
        return self._inbox_id

    async def get_inbox(self) -> dict:
        """Get inbox metadata (address, timestamps)."""
        inbox = await self._client.inboxes.get(self._inbox_id)
        return {
            "inbox_id": inbox.inbox_id,
            "display_name": inbox.display_name,
            "created_at": inbox.created_at.isoformat() if inbox.created_at else None,
        }

    async def get_messages(
        self,
        limit: int = 50,
        after: datetime | None = None,
        labels: list[str] | None = None,
    ) -> list[dict]:
        """List messages, converting each to the standard email_data format."""
        response = await self._client.inboxes.messages.list(
            self._inbox_id,
            limit=limit,
            after=after.isoformat() if after else None,
            labels=labels,
        )
        results = []
        for msg in response.messages:
            full = await self._client.inboxes.messages.get(self._inbox_id, msg.message_id)
            results.append(_to_email_data(full))
        return results

    async def get_message(self, message_id: str) -> dict:
        """Get a single message in standard email_data format."""
        msg = await self._client.inboxes.messages.get(self._inbox_id, message_id)
        return _to_email_data(msg)

    async def get_message_body(self, message_id: str) -> str:
        """Get the plain-text body of a message."""
        msg = await self._client.inboxes.messages.get(self._inbox_id, message_id)
        return msg.text or ""

    async def get_threads(self, limit: int = 50) -> list[dict]:
        """List threads with basic metadata."""
        response = await self._client.inboxes.threads.list(self._inbox_id, limit=limit)
        return [
            {
                "thread_id": t.thread_id,
                "subject": t.subject,
                "preview": t.preview,
                "message_count": t.message_count,
                "senders": t.senders,
                "timestamp": t.timestamp.isoformat() if t.timestamp else None,
            }
            for t in response.threads
        ]

    async def send_message(
        self,
        to: str | list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
    ) -> dict:
        """Send a new message."""
        to_list = [to] if isinstance(to, str) else to
        response = await self._client.inboxes.messages.send(
            self._inbox_id,
            to=to_list,
            subject=subject,
            text=body,
            cc=cc,
            bcc=bcc,
        )
        logger.info(
            "AgentMail message sent",
            to=to_list,
            subject=subject[:50],
            message_id=response.message_id,
        )
        return {"id": response.message_id, "thread_id": response.thread_id}

    async def reply_to_message(
        self,
        message_id: str,
        body: str,
        to: str | list[str] | None = None,
        cc: list[str] | None = None,
    ) -> dict:
        """Reply to an existing message."""
        to_list = [to] if isinstance(to, str) else to
        response = await self._client.inboxes.messages.reply(
            self._inbox_id,
            message_id,
            to=to_list,
            text=body,
            cc=cc,
        )
        logger.info(
            "AgentMail reply sent",
            message_id=message_id,
            reply_message_id=response.message_id,
        )
        return {"id": response.message_id, "thread_id": response.thread_id}

    async def create_draft(
        self,
        to: str | list[str],
        subject: str,
        body: str,
        thread_id: str | None = None,  # noqa: ARG002
        in_reply_to: str | None = None,
    ) -> dict:
        """Create a draft email."""
        to_list = [to] if isinstance(to, str) else to
        draft = await self._client.inboxes.drafts.create(
            self._inbox_id,
            to=to_list,
            subject=subject,
            text=body,
            in_reply_to=in_reply_to,
        )
        logger.info("AgentMail draft created", draft_id=draft.draft_id, to=to_list)
        return {
            "id": draft.draft_id,
            "thread_id": draft.thread_id,
            "to": to_list,
            "subject": subject,
        }

    async def send_draft(self, draft_id: str) -> dict:
        """Send an existing draft."""
        response = await self._client.inboxes.drafts.send(self._inbox_id, draft_id)
        logger.info("AgentMail draft sent", draft_id=draft_id, message_id=response.message_id)
        return {"id": response.message_id, "thread_id": response.thread_id}

    async def update_message_labels(
        self,
        message_id: str,
        add_labels: list[str] | None = None,
        remove_labels: list[str] | None = None,
    ) -> None:
        """Update labels on a message."""
        await self._client.inboxes.messages.update(
            self._inbox_id,
            message_id,
            add_labels=add_labels,
            remove_labels=remove_labels,
        )


def _parse_address(raw: str) -> tuple[str, str]:
    """Parse 'Name <email>' or plain 'email' into (name, email)."""
    if "<" in raw and ">" in raw:
        name = raw.split("<")[0].strip().strip('"')
        addr = raw.split("<")[1].split(">")[0].strip()
        return name, addr.lower()
    return "", raw.strip().lower()


def _to_email_data(message: Message) -> dict[str, Any]:
    """Convert an AgentMail Message to the email_data dict format.

    Matches the structure produced by ``extract_email_metadata()`` in gmail.py
    so the ingestion pipeline works unchanged.
    """
    sender_name, sender_email = _parse_address(message.from_)

    def parse_recipients(addrs: list[str] | None) -> list[tuple[str, str]]:
        if not addrs:
            return []
        return [_parse_address(a) for a in addrs]

    return {
        "gmail_id": message.message_id,
        "thread_id": message.thread_id,
        "subject": message.subject or "(no subject)",
        "date": message.timestamp.isoformat() if message.timestamp else datetime.now().isoformat(),
        "sender_name": sender_name,
        "sender_email": sender_email,
        "to": parse_recipients(message.to),
        "cc": parse_recipients(message.cc),
        "bcc": parse_recipients(message.bcc),
        "snippet": message.preview or "",
        "labels": message.labels or [],
        "size_estimate": message.size or 0,
    }


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_agentmail_service: AgentMailService | None = None


def get_agentmail_service() -> AgentMailService | None:
    """Get the AgentMail service singleton. Returns None if not enabled."""
    global _agentmail_service
    settings = get_settings()
    if not settings.agentmail_enabled:
        return None
    if _agentmail_service is None:
        api_key = settings.agentmail_api_key.get_secret_value()
        if not api_key:
            logger.warning("AgentMail enabled but no API key configured")
            return None
        _agentmail_service = AgentMailService(
            api_key=api_key,
            inbox_id=settings.agentmail_inbox_id,
        )
    return _agentmail_service
