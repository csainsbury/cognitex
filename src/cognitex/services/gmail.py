"""Gmail API service for fetching and syncing emails."""

import asyncio
import base64
import email
from datetime import datetime, timedelta
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any

import structlog
from googleapiclient.discovery import build

from cognitex.services.google_auth import get_google_credentials

logger = structlog.get_logger()


class GmailService:
    """Service for interacting with Gmail API."""

    def __init__(self):
        self._service = None

    @property
    def service(self):
        """Lazy-load the Gmail service."""
        if self._service is None:
            credentials = get_google_credentials()
            self._service = build("gmail", "v1", credentials=credentials)
        return self._service

    def get_profile(self) -> dict:
        """Get the authenticated user's profile."""
        return self.service.users().getProfile(userId="me").execute()

    def list_messages(
        self,
        query: str | None = None,
        max_results: int = 100,
        page_token: str | None = None,
        label_ids: list[str] | None = None,
    ) -> dict:
        """
        List messages matching the query.

        Args:
            query: Gmail search query (e.g., 'is:unread', 'from:someone@example.com')
            max_results: Maximum number of results per page
            page_token: Token for pagination
            label_ids: Filter by label IDs

        Returns:
            Dict with 'messages' list and 'nextPageToken' if more results exist
        """
        kwargs: dict[str, Any] = {
            "userId": "me",
            "maxResults": max_results,
        }
        if query:
            kwargs["q"] = query
        if page_token:
            kwargs["pageToken"] = page_token
        if label_ids:
            kwargs["labelIds"] = label_ids

        return self.service.users().messages().list(**kwargs).execute()

    def get_message(self, message_id: str, format: str = "full") -> dict:
        """
        Get a specific message by ID.

        Args:
            message_id: The message ID
            format: 'full', 'metadata', 'minimal', or 'raw'

        Returns:
            Message resource
        """
        return self.service.users().messages().get(
            userId="me",
            id=message_id,
            format=format,
        ).execute()

    def get_message_batch(self, message_ids: list[str], format: str = "metadata") -> list[dict]:
        """
        Get multiple messages with rate limiting.

        Args:
            message_ids: List of message IDs
            format: Message format

        Returns:
            List of message resources
        """
        import time

        messages = []

        # Fetch messages one at a time with rate limiting to avoid 429 errors
        for i, msg_id in enumerate(message_ids):
            for attempt in range(3):
                try:
                    msg = self.get_message(msg_id, format=format)
                    messages.append(msg)
                    break
                except Exception as e:
                    if "429" in str(e) or "rateLimitExceeded" in str(e):
                        # Rate limited - back off exponentially
                        wait_time = 2 ** (attempt + 1)  # 2s, 4s, 8s
                        logger.warning("Rate limited, waiting", wait_time=wait_time, msg_id=msg_id)
                        time.sleep(wait_time)
                    elif attempt == 2:
                        logger.error("Failed to fetch message after retries", msg_id=msg_id, error=str(e))
                        break
                    else:
                        time.sleep(1)

            # Small delay between requests to stay under rate limit
            if i % 10 == 9:
                time.sleep(0.5)

            # Progress logging
            if (i + 1) % 100 == 0:
                logger.info("Fetch progress", fetched=i + 1, total=len(message_ids))

        return messages

    def get_history(
        self,
        start_history_id: str,
        history_types: list[str] | None = None,
        label_id: str | None = None,
        max_results: int = 500,
    ) -> dict:
        """
        Get history of changes since a specific history ID.

        Args:
            start_history_id: The history ID to start from
            history_types: Types to filter ('messageAdded', 'messageDeleted', etc.)
            label_id: Filter by label
            max_results: Maximum results

        Returns:
            History resource with list of changes
        """
        kwargs: dict[str, Any] = {
            "userId": "me",
            "startHistoryId": start_history_id,
            "maxResults": max_results,
        }
        if history_types:
            kwargs["historyTypes"] = history_types
        if label_id:
            kwargs["labelId"] = label_id

        return self.service.users().history().list(**kwargs).execute()

    def get_email_attachments(self, message_id: str) -> list[dict]:
        """
        Extract attachments from a Gmail message.

        Downloads attachment data for each attachment in the message.

        Args:
            message_id: The Gmail message ID

        Returns:
            List of dicts with: filename, mime_type, size, data (bytes)
        """
        message = self.get_message(message_id, format="full")
        attachments = []

        def process_parts(parts: list[dict]) -> None:
            """Recursively process message parts to find attachments."""
            for part in parts:
                filename = part.get("filename")
                body = part.get("body", {})
                attachment_id = body.get("attachmentId")

                # If this part has a filename and attachment ID, it's an attachment
                if filename and attachment_id:
                    try:
                        # Download the attachment data
                        attachment = self.service.users().messages().attachments().get(
                            userId="me",
                            messageId=message_id,
                            id=attachment_id,
                        ).execute()

                        # Decode the attachment data
                        data = base64.urlsafe_b64decode(attachment["data"])

                        attachments.append({
                            "filename": filename,
                            "mime_type": part.get("mimeType", "application/octet-stream"),
                            "size": body.get("size", len(data)),
                            "data": data,
                        })

                        logger.debug(
                            "Extracted attachment",
                            filename=filename,
                            mime_type=part.get("mimeType"),
                            size=body.get("size"),
                        )
                    except Exception as e:
                        logger.error(
                            "Failed to download attachment",
                            filename=filename,
                            message_id=message_id,
                            error=str(e),
                        )

                # Recurse into nested parts
                if part.get("parts"):
                    process_parts(part["parts"])

        payload = message.get("payload", {})
        if payload.get("parts"):
            process_parts(payload["parts"])

        return attachments

    def get_attachment_metadata(self, message_id: str) -> list[dict]:
        """
        Get metadata about attachments without downloading the data.

        Useful for checking what attachments exist before deciding
        whether to download them.

        Args:
            message_id: The Gmail message ID

        Returns:
            List of dicts with: filename, mime_type, size (no data)
        """
        message = self.get_message(message_id, format="full")
        attachments = []

        def process_parts(parts: list[dict]) -> None:
            for part in parts:
                filename = part.get("filename")
                body = part.get("body", {})

                if filename and body.get("attachmentId"):
                    attachments.append({
                        "filename": filename,
                        "mime_type": part.get("mimeType", "application/octet-stream"),
                        "size": body.get("size", 0),
                    })

                if part.get("parts"):
                    process_parts(part["parts"])

        payload = message.get("payload", {})
        if payload.get("parts"):
            process_parts(payload["parts"])

        return attachments


def parse_email_address(raw: str) -> tuple[str, str]:
    """
    Parse an email address string into (name, email).

    Args:
        raw: Raw email string like 'John Doe <john@example.com>'

    Returns:
        Tuple of (name, email_address)
    """
    name, addr = parseaddr(raw)
    return name or "", addr.lower()


def extract_email_metadata(message: dict) -> dict:
    """
    Extract useful metadata from a Gmail message resource.

    Args:
        message: Gmail message resource (format='metadata' or 'full')

    Returns:
        Dict with extracted metadata
    """
    headers = {h["name"].lower(): h["value"] for h in message.get("payload", {}).get("headers", [])}

    # Parse sender
    sender_name, sender_email = parse_email_address(headers.get("from", ""))

    # Parse recipients
    to_raw = headers.get("to", "")
    cc_raw = headers.get("cc", "")
    bcc_raw = headers.get("bcc", "")

    def parse_recipients(raw: str) -> list[tuple[str, str]]:
        if not raw:
            return []
        # Split by comma, handling quoted names
        parts = raw.split(",")
        return [parse_email_address(p.strip()) for p in parts if p.strip()]

    # Parse date
    date_str = headers.get("date", "")
    try:
        date = parsedate_to_datetime(date_str)
    except Exception:
        # Fall back to internal date
        internal_date = message.get("internalDate")
        if internal_date:
            date = datetime.fromtimestamp(int(internal_date) / 1000)
        else:
            date = datetime.now()

    return {
        "gmail_id": message["id"],
        "thread_id": message["threadId"],
        "subject": headers.get("subject", "(no subject)"),
        "date": date.isoformat(),
        "sender_name": sender_name,
        "sender_email": sender_email,
        "to": parse_recipients(to_raw),
        "cc": parse_recipients(cc_raw),
        "bcc": parse_recipients(bcc_raw),
        "snippet": message.get("snippet", ""),
        "labels": message.get("labelIds", []),
        "size_estimate": message.get("sizeEstimate", 0),
    }


def extract_email_body(message: dict, max_length: int = 5000) -> str:
    """
    Extract the plain text body from a Gmail message.

    Args:
        message: Gmail message resource (format='full')
        max_length: Maximum body length to return

    Returns:
        Plain text body content
    """
    payload = message.get("payload", {})

    def get_body_from_part(part: dict) -> str | None:
        mime_type = part.get("mimeType", "")
        body = part.get("body", {})
        data = body.get("data")

        if mime_type == "text/plain" and data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

        # Recurse into multipart
        parts = part.get("parts", [])
        for sub_part in parts:
            result = get_body_from_part(sub_part)
            if result:
                return result

        return None

    body = get_body_from_part(payload)

    if body:
        return body[:max_length]

    return ""


def build_historical_query(months: int = 6, inbox_only: bool = True) -> str:
    """
    Build a Gmail query for historical sync.

    Args:
        months: Number of months to look back
        inbox_only: Only include emails that hit the inbox (not filtered/spam)

    Returns:
        Gmail search query string
    """
    cutoff = datetime.now() - timedelta(days=months * 30)
    query = f"after:{cutoff.strftime('%Y/%m/%d')}"

    if inbox_only:
        # Only emails that were delivered to inbox (not filtered to skip inbox)
        query += " in:inbox"

    return query


def build_incremental_query(since: datetime) -> str:
    """
    Build a Gmail query for incremental sync.

    Args:
        since: Datetime to sync from

    Returns:
        Gmail search query string
    """
    return f"after:{since.strftime('%Y/%m/%d')}"


class GmailSender:
    """Service for sending emails via Gmail API."""

    def __init__(self):
        self._service = None

    @property
    def service(self):
        """Lazy-load the Gmail service."""
        if self._service is None:
            credentials = get_google_credentials()
            self._service = build("gmail", "v1", credentials=credentials)
        return self._service

    def send_message(
        self,
        to: str,
        subject: str,
        body: str,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
    ) -> dict:
        """
        Send a new email.

        Args:
            to: Recipient email address
            subject: Email subject
            body: Email body (plain text)
            cc: Optional CC recipients
            bcc: Optional BCC recipients

        Returns:
            Sent message resource with 'id' and 'threadId'
        """
        from email.mime.text import MIMEText

        message = MIMEText(body)
        message["to"] = to
        message["subject"] = subject

        if cc:
            message["cc"] = ", ".join(cc)
        if bcc:
            message["bcc"] = ", ".join(bcc)

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

        result = self.service.users().messages().send(
            userId="me",
            body={"raw": raw}
        ).execute()

        logger.info("Email sent", to=to, subject=subject[:50], message_id=result.get("id"))
        return result

    def send_reply(
        self,
        thread_id: str,
        to: str,
        subject: str,
        body: str,
        in_reply_to: str | None = None,
        references: str | None = None,
    ) -> dict:
        """
        Reply to an existing email thread.

        Args:
            thread_id: Gmail thread ID to reply to
            to: Recipient email address
            subject: Email subject (will add Re: if not present)
            body: Email body (plain text)
            in_reply_to: Message-ID header of the message being replied to
            references: References header for threading

        Returns:
            Sent message resource with 'id' and 'threadId'
        """
        from email.mime.text import MIMEText

        # Ensure subject has Re: prefix for replies
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"

        message = MIMEText(body)
        message["to"] = to
        message["subject"] = subject

        # Add threading headers if provided
        if in_reply_to:
            message["In-Reply-To"] = in_reply_to
        if references:
            message["References"] = references

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

        result = self.service.users().messages().send(
            userId="me",
            body={"raw": raw, "threadId": thread_id}
        ).execute()

        logger.info("Reply sent", thread_id=thread_id, to=to, message_id=result.get("id"))
        return result

    def create_draft(
        self,
        to: str,
        subject: str,
        body: str,
        thread_id: str | None = None,
    ) -> dict:
        """
        Create a draft email (for review before sending).

        Args:
            to: Recipient email address
            subject: Email subject
            body: Email body
            thread_id: Optional thread ID if this is a reply draft

        Returns:
            Draft resource with 'id' and 'message'
        """
        from email.mime.text import MIMEText

        message = MIMEText(body)
        message["to"] = to
        message["subject"] = subject

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

        draft_body = {"message": {"raw": raw}}
        if thread_id:
            draft_body["message"]["threadId"] = thread_id

        result = self.service.users().drafts().create(
            userId="me",
            body=draft_body
        ).execute()

        logger.info("Draft created", to=to, draft_id=result.get("id"))
        return result


async def fetch_all_messages(
    gmail: GmailService,
    query: str,
    max_messages: int = 10000,
) -> list[dict]:
    """
    Fetch all messages matching a query, handling pagination.
    Wraps blocking calls in threads to prevent event loop blocking.

    Args:
        gmail: GmailService instance
        query: Gmail search query
        max_messages: Maximum total messages to fetch

    Returns:
        List of message metadata dicts
    """
    all_messages = []
    page_token = None

    while len(all_messages) < max_messages:
        # Run blocking list_messages in thread
        result = await asyncio.to_thread(
            gmail.list_messages,
            query=query,
            max_results=min(500, max_messages - len(all_messages)),
            page_token=page_token,
        )

        messages = result.get("messages", [])
        if not messages:
            break

        # Get full metadata (also blocking, so wrap it)
        message_ids = [m["id"] for m in messages]
        full_messages = await asyncio.to_thread(
            gmail.get_message_batch,
            message_ids,
            format="metadata",
        )

        for msg in full_messages:
            all_messages.append(extract_email_metadata(msg))

        logger.info("Fetched messages", count=len(all_messages), total_in_batch=len(messages))

        page_token = result.get("nextPageToken")
        if not page_token:
            break

    return all_messages
