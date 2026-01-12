"""Email intent classification for deep semantic analysis.

This module classifies emails by intent to determine the appropriate
workflow for processing. Instead of just asking "does this need a reply?",
it understands what the sender actually wants.
"""

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import structlog

from cognitex.services.llm import LLMService, get_llm_service

logger = structlog.get_logger()


class EmailIntent(str, Enum):
    """Classification of email intent."""

    REVIEW_REQUEST = "review_request"      # Please review this document/work
    QUESTION = "question"                   # Asking for information
    ACTION_REQUEST = "action_request"       # Do something specific
    FYI = "fyi"                            # Just informing, no action required
    DEADLINE = "deadline"                   # Time-sensitive request
    CONFIRMATION = "confirmation"           # Confirming or acknowledging something
    FOLLOWUP = "followup"                  # Following up on previous conversation
    SCHEDULING = "scheduling"              # Meeting/calendar related
    UNKNOWN = "unknown"                    # Could not determine intent


class SuggestedWorkflow(str, Enum):
    """Workflow to use based on email intent."""

    ANALYZE_THEN_RESPOND = "analyze_then_respond"  # Deep analysis needed before responding
    QUICK_REPLY = "quick_reply"                     # Simple reply is sufficient
    ARCHIVE = "archive"                             # No response needed
    CREATE_TASK = "create_task"                     # Create task, maybe acknowledge
    SCHEDULE = "schedule"                           # Calendar-related action


@dataclass
class EmailIntentResult:
    """Result of email intent classification."""

    intent: EmailIntent
    confidence: float
    has_attachments: bool
    attachment_types: list[str] = field(default_factory=list)
    attachment_filenames: list[str] = field(default_factory=list)
    requires_document_analysis: bool = False
    suggested_workflow: SuggestedWorkflow = SuggestedWorkflow.QUICK_REPLY
    key_ask: str = ""  # What is the sender specifically asking for?
    deadline: str | None = None
    response_requirements: list[str] = field(default_factory=list)
    would_acknowledgment_be_unhelpful: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "intent": self.intent.value,
            "confidence": self.confidence,
            "has_attachments": self.has_attachments,
            "attachment_types": self.attachment_types,
            "attachment_filenames": self.attachment_filenames,
            "requires_document_analysis": self.requires_document_analysis,
            "suggested_workflow": self.suggested_workflow.value,
            "key_ask": self.key_ask,
            "deadline": self.deadline,
            "response_requirements": self.response_requirements,
            "would_acknowledgment_be_unhelpful": self.would_acknowledgment_be_unhelpful,
        }


# Classification prompt for deep intent analysis
INTENT_CLASSIFICATION_PROMPT = """Analyze this email deeply to understand what the sender actually needs.

Email:
- From: {sender}
- Subject: {subject}
- Body:
{body}

Attachments: {attachments}

Classify the email intent by answering these questions:

1. **Primary Intent**: What does the sender want?
   - review_request: They want feedback on a document/work (e.g., "please review", "take a look at", "feedback on")
   - question: They're asking for information or clarification
   - action_request: They need you to do something specific (not just review)
   - fyi: Just informing you, no action required
   - deadline: Time-sensitive request with a specific deadline
   - confirmation: Confirming or acknowledging something
   - followup: Following up on a previous conversation
   - scheduling: Meeting/calendar related request
   - unknown: Cannot determine intent

2. **Attachment Analysis**:
   - Are there attachments mentioned or referenced?
   - What types of documents are mentioned (docx, pdf, spreadsheet, etc.)?
   - Does the email explicitly reference the attachment content?
   - Is document analysis REQUIRED to respond meaningfully?

3. **Key Ask**: In one sentence, what specifically is the sender asking for?

4. **Workflow Determination**:
   - analyze_then_respond: Attachments need to be analyzed before a meaningful response can be drafted
   - quick_reply: A simple acknowledgment or brief answer suffices
   - archive: No response needed (FYI only)
   - create_task: Should create a task to track, maybe send quick acknowledgment
   - schedule: Needs calendar action

5. **Response Requirements**:
   - What information do you need to respond substantively?
   - Would a generic "I'll review this" be unhelpful given the context?
   - List specific things needed to craft a good response

6. **Deadline**: Is there any deadline mentioned? (extract the date/time if so)

Return ONLY valid JSON with these fields:
{{
    "intent": "review_request|question|action_request|fyi|deadline|confirmation|followup|scheduling|unknown",
    "confidence": 0.0-1.0,
    "has_attachments": true/false,
    "attachment_types": ["docx", "pdf", etc.],
    "attachment_filenames": ["file1.docx", etc.],
    "requires_document_analysis": true/false,
    "suggested_workflow": "analyze_then_respond|quick_reply|archive|create_task|schedule",
    "key_ask": "One sentence describing what sender wants",
    "deadline": "2024-01-15" or null,
    "response_requirements": ["list", "of", "requirements"],
    "would_acknowledgment_be_unhelpful": true/false
}}"""


class EmailIntentClassifier:
    """
    Classifies email intent for deep semantic understanding.

    This classifier goes beyond simple "needs reply?" classification to
    understand what the sender actually wants and what workflow should
    be used to process the email effectively.
    """

    def __init__(self, llm_service: LLMService | None = None):
        """
        Initialize the classifier.

        Args:
            llm_service: LLM service for classification (uses singleton if not provided)
        """
        self._llm = llm_service

    @property
    def llm(self) -> LLMService:
        """Lazy-load LLM service to avoid circular imports."""
        if self._llm is None:
            self._llm = get_llm_service()
        return self._llm

    async def classify(
        self,
        sender: str,
        subject: str,
        body: str,
        attachments: list[dict[str, str]] | None = None,
    ) -> EmailIntentResult:
        """
        Classify an email's intent.

        Args:
            sender: Email sender (name and/or address)
            subject: Email subject line
            body: Email body text
            attachments: List of attachment info dicts with 'filename' and 'mime_type'

        Returns:
            EmailIntentResult with classification details
        """
        # Format attachments for prompt
        if attachments:
            attachment_str = "\n".join(
                f"- {a.get('filename', 'unknown')} ({a.get('mime_type', 'unknown type')})"
                for a in attachments
            )
        else:
            attachment_str = "None mentioned"

        # Build prompt
        prompt = INTENT_CLASSIFICATION_PROMPT.format(
            sender=sender,
            subject=subject,
            body=body[:2000],  # Limit body length
            attachments=attachment_str,
        )

        try:
            response = await self.llm.complete(
                prompt,
                model=self.llm.fast_model,  # Use fast model for classification
                max_tokens=800,
                temperature=0.1,
            )

            # Parse JSON response
            response = response.strip()
            if response.startswith("```"):
                response = response.split("\n", 1)[1]
                response = response.rsplit("```", 1)[0]

            data = json.loads(response)

            return EmailIntentResult(
                intent=EmailIntent(data.get("intent", "unknown")),
                confidence=float(data.get("confidence", 0.5)),
                has_attachments=bool(data.get("has_attachments", False)),
                attachment_types=data.get("attachment_types", []),
                attachment_filenames=data.get("attachment_filenames", []),
                requires_document_analysis=bool(data.get("requires_document_analysis", False)),
                suggested_workflow=SuggestedWorkflow(
                    data.get("suggested_workflow", "quick_reply")
                ),
                key_ask=data.get("key_ask", ""),
                deadline=data.get("deadline"),
                response_requirements=data.get("response_requirements", []),
                would_acknowledgment_be_unhelpful=bool(
                    data.get("would_acknowledgment_be_unhelpful", False)
                ),
            )

        except json.JSONDecodeError as e:
            logger.warning("Failed to parse intent classification JSON", error=str(e))
            return self._fallback_classification(subject, body, attachments)
        except Exception as e:
            logger.error("Email intent classification failed", error=str(e))
            return self._fallback_classification(subject, body, attachments)

    def _fallback_classification(
        self,
        subject: str,
        body: str,
        attachments: list[dict[str, str]] | None,
    ) -> EmailIntentResult:
        """
        Fallback classification using simple heuristics.

        Used when LLM classification fails.
        """
        subject_lower = subject.lower()
        body_lower = body.lower()
        combined = f"{subject_lower} {body_lower}"

        # Detect intent from keywords
        intent = EmailIntent.UNKNOWN
        workflow = SuggestedWorkflow.QUICK_REPLY

        if any(kw in combined for kw in ["please review", "take a look", "feedback on", "comments on"]):
            intent = EmailIntent.REVIEW_REQUEST
            workflow = SuggestedWorkflow.ANALYZE_THEN_RESPOND
        elif any(kw in combined for kw in ["?", "what", "how", "when", "where", "who", "can you tell"]):
            intent = EmailIntent.QUESTION
            workflow = SuggestedWorkflow.QUICK_REPLY
        elif any(kw in combined for kw in ["fyi", "for your information", "just wanted to let you know"]):
            intent = EmailIntent.FYI
            workflow = SuggestedWorkflow.ARCHIVE
        elif any(kw in combined for kw in ["deadline", "due by", "by eod", "urgent", "asap"]):
            intent = EmailIntent.DEADLINE
            workflow = SuggestedWorkflow.CREATE_TASK
        elif any(kw in combined for kw in ["schedule", "meeting", "calendar", "call", "let's meet"]):
            intent = EmailIntent.SCHEDULING
            workflow = SuggestedWorkflow.SCHEDULE

        # Check for attachments
        has_attachments = bool(attachments)
        requires_doc_analysis = (
            has_attachments and
            intent == EmailIntent.REVIEW_REQUEST
        )

        if requires_doc_analysis:
            workflow = SuggestedWorkflow.ANALYZE_THEN_RESPOND

        return EmailIntentResult(
            intent=intent,
            confidence=0.5,  # Low confidence for fallback
            has_attachments=has_attachments,
            attachment_types=[a.get("mime_type", "").split("/")[-1] for a in (attachments or [])],
            attachment_filenames=[a.get("filename", "") for a in (attachments or [])],
            requires_document_analysis=requires_doc_analysis,
            suggested_workflow=workflow,
            key_ask=f"Review: {subject}" if intent == EmailIntent.REVIEW_REQUEST else subject,
            deadline=None,
            response_requirements=[],
            would_acknowledgment_be_unhelpful=requires_doc_analysis,
        )


# Singleton instance
_classifier: EmailIntentClassifier | None = None


def get_email_intent_classifier() -> EmailIntentClassifier:
    """Get or create the email intent classifier singleton."""
    global _classifier
    if _classifier is None:
        _classifier = EmailIntentClassifier()
    return _classifier
