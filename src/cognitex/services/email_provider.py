"""Unified email provider — routes to AgentMail or Gmail based on config."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import structlog

from cognitex.config import get_settings

logger = structlog.get_logger()


class EmailProvider:
    """Unified email interface that delegates to AgentMail or Gmail."""

    def __init__(self) -> None:
        settings = get_settings()
        self._use_agentmail = settings.agentmail_enabled

    @property
    def provider_name(self) -> str:
        return "agentmail" if self._use_agentmail else "gmail"

    async def get_messages(
        self,
        limit: int = 50,
        after: datetime | None = None,
        labels: list[str] | None = None,
    ) -> list[dict]:
        if self._use_agentmail:
            from cognitex.services.agentmail import get_agentmail_service

            svc = get_agentmail_service()
            if not svc:
                raise RuntimeError("AgentMail enabled but service unavailable")
            return await svc.get_messages(limit=limit, after=after, labels=labels)

        from cognitex.services.gmail import GmailService, extract_email_metadata

        gmail = GmailService()
        query = ""
        if labels:
            query = " ".join(f"label:{lbl}" for lbl in labels)
        result = await asyncio.to_thread(
            gmail.list_messages, query=query or None, max_results=limit
        )
        messages = result.get("messages", [])
        if not messages:
            return []
        ids = [m["id"] for m in messages]
        full = await asyncio.to_thread(gmail.get_message_batch, ids, format="metadata")
        return [extract_email_metadata(m) for m in full]

    async def get_message(self, message_id: str) -> dict:
        if self._use_agentmail:
            from cognitex.services.agentmail import get_agentmail_service

            svc = get_agentmail_service()
            if not svc:
                raise RuntimeError("AgentMail enabled but service unavailable")
            return await svc.get_message(message_id)

        from cognitex.services.gmail import GmailService, extract_email_metadata

        gmail = GmailService()
        msg = await asyncio.to_thread(gmail.get_message, message_id, format="metadata")
        return extract_email_metadata(msg)

    async def get_threads(self, limit: int = 50) -> list[dict]:
        if self._use_agentmail:
            from cognitex.services.agentmail import get_agentmail_service

            svc = get_agentmail_service()
            if not svc:
                raise RuntimeError("AgentMail enabled but service unavailable")
            return await svc.get_threads(limit=limit)

        # Gmail doesn't have a direct thread list — return empty
        return []

    async def send_message(
        self,
        to: str,
        subject: str,
        body: str,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
    ) -> dict:
        if self._use_agentmail:
            from cognitex.services.agentmail import get_agentmail_service

            svc = get_agentmail_service()
            if not svc:
                raise RuntimeError("AgentMail enabled but service unavailable")
            return await svc.send_message(to=to, subject=subject, body=body, cc=cc, bcc=bcc)

        from cognitex.services.gmail import GmailSender

        sender = GmailSender()
        return await asyncio.to_thread(
            sender.send_message, to=to, subject=subject, body=body, cc=cc, bcc=bcc
        )

    async def reply_to_message(
        self,
        thread_id: str,
        to: str,
        subject: str,
        body: str,
        in_reply_to: str | None = None,
    ) -> dict:
        if self._use_agentmail:
            from cognitex.services.agentmail import get_agentmail_service

            svc = get_agentmail_service()
            if not svc:
                raise RuntimeError("AgentMail enabled but service unavailable")
            # AgentMail reply uses message_id, not thread_id
            message_id = in_reply_to or thread_id
            return await svc.reply_to_message(message_id=message_id, body=body, to=to)

        from cognitex.services.gmail import GmailSender

        sender = GmailSender()
        return await asyncio.to_thread(
            sender.send_reply,
            thread_id=thread_id,
            to=to,
            subject=subject,
            body=body,
            in_reply_to=in_reply_to,
        )

    async def create_draft(
        self,
        to: str,
        subject: str,
        body: str,
        thread_id: str | None = None,
        in_reply_to: str | None = None,
    ) -> dict:
        if self._use_agentmail:
            from cognitex.services.agentmail import get_agentmail_service

            svc = get_agentmail_service()
            if not svc:
                raise RuntimeError("AgentMail enabled but service unavailable")
            return await svc.create_draft(
                to=to, subject=subject, body=body,
                thread_id=thread_id, in_reply_to=in_reply_to,
            )

        from cognitex.services.gmail import GmailSender

        sender = GmailSender()
        return await asyncio.to_thread(
            sender.create_draft, to=to, subject=subject, body=body, thread_id=thread_id
        )

    async def send_draft(self, draft_id: str) -> dict:
        if self._use_agentmail:
            from cognitex.services.agentmail import get_agentmail_service

            svc = get_agentmail_service()
            if not svc:
                raise RuntimeError("AgentMail enabled but service unavailable")
            return await svc.send_draft(draft_id)

        # Gmail drafts are sent via the Gmail API directly
        raise NotImplementedError("Gmail draft sending not supported via EmailProvider")

    async def get_profile(self) -> dict[str, Any]:
        """Get current inbox/profile info."""
        if self._use_agentmail:
            from cognitex.services.agentmail import get_agentmail_service

            svc = get_agentmail_service()
            if not svc:
                raise RuntimeError("AgentMail enabled but service unavailable")
            inbox = await svc.get_inbox()
            return {
                "provider": "agentmail",
                "inbox_id": inbox.get("inbox_id", ""),
                "display_name": inbox.get("display_name", ""),
            }

        from cognitex.services.gmail import GmailService

        gmail = GmailService()
        profile = await asyncio.to_thread(gmail.get_profile)
        return {
            "provider": "gmail",
            "email": profile.get("emailAddress", ""),
            "messages_total": profile.get("messagesTotal", 0),
        }


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_email_provider: EmailProvider | None = None


def get_email_provider() -> EmailProvider:
    """Get the email provider singleton."""
    global _email_provider
    if _email_provider is None:
        _email_provider = EmailProvider()
    return _email_provider
