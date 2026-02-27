"""Email intent classification for deep semantic analysis.

This module classifies emails by intent to determine the appropriate
workflow for processing. Instead of just asking "does this need a reply?",
it understands what the sender actually wants.

WP4 adds structured triage extraction with tone neutralisation, clinical
bypass, and skill-driven prompting via the email-triage skill.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

import structlog

from cognitex.services.llm import LLMService, get_llm_service

if TYPE_CHECKING:
    from cognitex.services.clinical_firewall import ClinicalScanResult

logger = structlog.get_logger()


class EmailIntent(str, Enum):
    """Classification of email intent."""

    REVIEW_REQUEST = "review_request"  # Please review this document/work
    QUESTION = "question"  # Asking for information
    ACTION_REQUEST = "action_request"  # Do something specific
    FYI = "fyi"  # Just informing, no action required
    DEADLINE = "deadline"  # Time-sensitive request
    CONFIRMATION = "confirmation"  # Confirming or acknowledging something
    FOLLOWUP = "followup"  # Following up on previous conversation
    SCHEDULING = "scheduling"  # Meeting/calendar related
    UNKNOWN = "unknown"  # Could not determine intent


class SuggestedWorkflow(str, Enum):
    """Workflow to use based on email intent."""

    ANALYZE_THEN_RESPOND = "analyze_then_respond"  # Deep analysis needed before responding
    QUICK_REPLY = "quick_reply"  # Simple reply is sufficient
    ARCHIVE = "archive"  # No response needed
    CREATE_TASK = "create_task"  # Create task, maybe acknowledge
    SCHEDULE = "schedule"  # Calendar-related action


class TriageDecision(str, Enum):
    """WP4: Structured triage decision for email routing."""

    ACTION = "action"
    DELEGATE = "delegate"
    TRACK = "track"
    ARCHIVE = "archive"


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

    # WP4: Structured triage fields (all have defaults for backward compat)
    triage_decision: TriageDecision = TriageDecision.ACTION
    action_verb: str = ""
    delegation_candidate: str | None = None
    delegation_reason: str | None = None
    project_context: str | None = None
    factual_summary: str = ""
    emotional_markers: list[str] = field(default_factory=list)
    factual_urgency: int = 3
    deadline_source: str = "none"
    clinical_flag: bool = False

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
            # WP4 triage fields
            "triage_decision": self.triage_decision.value,
            "action_verb": self.action_verb,
            "delegation_candidate": self.delegation_candidate,
            "delegation_reason": self.delegation_reason,
            "project_context": self.project_context,
            "factual_summary": self.factual_summary,
            "emotional_markers": self.emotional_markers,
            "factual_urgency": self.factual_urgency,
            "deadline_source": self.deadline_source,
            "clinical_flag": self.clinical_flag,
        }


# Legacy classification prompt — used as fallback when skill loading fails
_LEGACY_INTENT_CLASSIFICATION_PROMPT = """Analyze this email deeply to understand what the sender actually needs.

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

# Skill-enhanced prompt that produces both legacy and triage fields
_TRIAGE_CLASSIFICATION_PROMPT = """Analyze this email to understand what the sender needs and produce a structured triage decision.

Email:
- From: {sender}
- Subject: {subject}
- Body:
{body}

Attachments: {attachments}
{skill_section}
**Instructions — follow these steps in order:**

1. Produce a `factual_summary`: restate the request stripped of emotional language, hedging, and pleasantries. Keep only: who needs what, by when, why it matters.
2. List any `emotional_markers` (e.g., "urgent-sounding", "apologetic", "frustrated").
3. Score `factual_urgency` (1-5) using ONLY the factual_summary — deadline proximity, business impact, blocked people, regulatory obligations. If the only reason for high urgency is emotional language, cap at 3.
4. Classify intent, determine workflow, and produce the triage decision.

Return ONLY valid JSON with ALL of these fields:
{{
    "intent": "review_request|question|action_request|fyi|deadline|confirmation|followup|scheduling|unknown",
    "confidence": 0.0-1.0,
    "has_attachments": true/false,
    "attachment_types": ["docx", "pdf"],
    "attachment_filenames": ["file1.docx"],
    "requires_document_analysis": true/false,
    "suggested_workflow": "analyze_then_respond|quick_reply|archive|create_task|schedule",
    "key_ask": "One sentence describing what sender wants",
    "deadline": "ISO date or null",
    "response_requirements": ["list", "of", "requirements"],
    "would_acknowledgment_be_unhelpful": true/false,
    "triage_decision": "action|delegate|track|archive",
    "action_verb": "review|respond|schedule|approve|create|forward|research|discuss",
    "deadline_source": "explicit|inferred|none",
    "delegation_candidate": "name/email or null",
    "delegation_reason": "string or null",
    "project_context": "string or null",
    "factual_summary": "tone-neutral restatement",
    "emotional_markers": [],
    "factual_urgency": 1-5,
    "clinical_flag": false
}}"""


def _map_triage_to_workflow(
    triage_decision: TriageDecision,
    factual_urgency: int,
    original_workflow: SuggestedWorkflow,
) -> SuggestedWorkflow:
    """Deterministic mapping from triage decision to suggested workflow.

    Ensures suggested_workflow stays consistent with the triage decision
    while preserving the original workflow for ACTION decisions (derived
    from intent as before).
    """
    if triage_decision == TriageDecision.DELEGATE:
        return SuggestedWorkflow.CREATE_TASK
    if triage_decision == TriageDecision.TRACK:
        if factual_urgency >= 4:
            return SuggestedWorkflow.CREATE_TASK
        return SuggestedWorkflow.ARCHIVE
    if triage_decision == TriageDecision.ARCHIVE:
        return SuggestedWorkflow.ARCHIVE
    # ACTION — use the intent-derived workflow
    return original_workflow


class EmailIntentClassifier:
    """
    Classifies email intent for deep semantic understanding.

    This classifier goes beyond simple "needs reply?" classification to
    understand what the sender actually wants and what workflow should
    be used to process the email effectively.

    WP4 adds skill-driven triage extraction with tone neutralisation.
    """

    def __init__(self, llm_service: LLMService | None = None):
        self._llm = llm_service

    @property
    def llm(self) -> LLMService:
        """Lazy-load LLM service to avoid circular imports."""
        if self._llm is None:
            self._llm = get_llm_service()
        return self._llm

    async def _get_triage_skill(self) -> str:
        """Load the email-triage skill for prompt injection.

        Returns formatted skill content or empty string if not available.
        """
        try:
            from cognitex.agent.skills import get_skills_loader

            loader = get_skills_loader()
            skill = await loader.get_skill("email-triage")

            if skill:
                return f"\n## Email Triage Guidelines\n\n{loader.format_skill_for_prompt(skill)}\n"
            return ""
        except Exception as e:
            logger.debug("Failed to load email-triage skill", error=str(e))
            return ""

    async def classify(
        self,
        sender: str,
        subject: str,
        body: str,
        attachments: list[dict[str, str]] | None = None,
        clinical_scan_result: ClinicalScanResult | None = None,
    ) -> EmailIntentResult:
        """
        Classify an email's intent and produce structured triage.

        Args:
            sender: Email sender (name and/or address)
            subject: Email subject line
            body: Email body text
            attachments: List of attachment info dicts with 'filename' and 'mime_type'
            clinical_scan_result: Optional pre-computed clinical scan (WP2 firewall)

        Returns:
            EmailIntentResult with classification and triage details
        """
        # Clinical short-circuit — no LLM call needed
        if clinical_scan_result and clinical_scan_result.is_clinical:
            logger.info(
                "Clinical content detected, short-circuiting triage",
                subject=subject[:50],
                categories=clinical_scan_result.matched_categories,
            )
            return EmailIntentResult(
                intent=EmailIntent.FYI,
                confidence=1.0,
                has_attachments=bool(attachments),
                suggested_workflow=SuggestedWorkflow.ARCHIVE,
                key_ask="Clinical content — deferred to clinical firewall",
                triage_decision=TriageDecision.TRACK,
                factual_summary="Clinical content detected — deferred to clinical firewall",
                factual_urgency=1,
                clinical_flag=True,
            )

        # Format attachments for prompt
        if attachments:
            attachment_str = "\n".join(
                f"- {a.get('filename', 'unknown')} ({a.get('mime_type', 'unknown type')})"
                for a in attachments
            )
        else:
            attachment_str = "None mentioned"

        # Try skill-enhanced triage prompt first
        skill_section = await self._get_triage_skill()
        use_triage_prompt = bool(skill_section)

        if use_triage_prompt:
            prompt = _TRIAGE_CLASSIFICATION_PROMPT.format(
                sender=sender,
                subject=subject,
                body=body[:2000],
                attachments=attachment_str,
                skill_section=skill_section,
            )
        else:
            logger.debug("Triage skill not available, using legacy prompt")
            prompt = _LEGACY_INTENT_CLASSIFICATION_PROMPT.format(
                sender=sender,
                subject=subject,
                body=body[:2000],
                attachments=attachment_str,
            )

        try:
            response = await self.llm.complete(
                prompt,
                max_tokens=1000,
                temperature=0.1,
                task="triage",
            )

            # Parse JSON response
            response = response.strip()
            if response.startswith("```"):
                response = response.split("\n", 1)[1]
                response = response.rsplit("```", 1)[0]

            data = json.loads(response)
            return self._parse_result(data, use_triage_prompt)

        except json.JSONDecodeError as e:
            logger.warning("Failed to parse intent classification JSON", error=str(e))
            return self._fallback_classification(subject, body, attachments)
        except Exception as e:
            logger.error("Email intent classification failed", error=str(e))
            return self._fallback_classification(subject, body, attachments)

    def _parse_result(self, data: dict, has_triage: bool) -> EmailIntentResult:
        """Parse LLM JSON response into EmailIntentResult."""
        # Legacy fields
        intent = EmailIntent(data.get("intent", "unknown"))
        original_workflow = SuggestedWorkflow(data.get("suggested_workflow", "quick_reply"))

        # Triage fields (with safe defaults when absent)
        triage_decision_str = data.get("triage_decision", "action")
        try:
            triage_decision = TriageDecision(triage_decision_str)
        except ValueError:
            triage_decision = TriageDecision.ACTION

        factual_urgency = min(5, max(1, int(data.get("factual_urgency", 3))))

        # If triage fields present, apply deterministic workflow mapping
        if has_triage:
            suggested_workflow = _map_triage_to_workflow(
                triage_decision, factual_urgency, original_workflow
            )
        else:
            suggested_workflow = original_workflow

        return EmailIntentResult(
            intent=intent,
            confidence=float(data.get("confidence", 0.5)),
            has_attachments=bool(data.get("has_attachments", False)),
            attachment_types=data.get("attachment_types", []),
            attachment_filenames=data.get("attachment_filenames", []),
            requires_document_analysis=bool(data.get("requires_document_analysis", False)),
            suggested_workflow=suggested_workflow,
            key_ask=data.get("key_ask", ""),
            deadline=data.get("deadline"),
            response_requirements=data.get("response_requirements", []),
            would_acknowledgment_be_unhelpful=bool(
                data.get("would_acknowledgment_be_unhelpful", False)
            ),
            # WP4 triage
            triage_decision=triage_decision,
            action_verb=data.get("action_verb", ""),
            delegation_candidate=data.get("delegation_candidate"),
            delegation_reason=data.get("delegation_reason"),
            project_context=data.get("project_context"),
            factual_summary=data.get("factual_summary", ""),
            emotional_markers=data.get("emotional_markers", []),
            factual_urgency=factual_urgency,
            deadline_source=data.get("deadline_source", "none"),
            clinical_flag=bool(data.get("clinical_flag", False)),
        )

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
        triage_decision = TriageDecision.ACTION

        if any(
            kw in combined for kw in ["please review", "take a look", "feedback on", "comments on"]
        ):
            intent = EmailIntent.REVIEW_REQUEST
            workflow = SuggestedWorkflow.ANALYZE_THEN_RESPOND
        elif any(
            kw in combined for kw in ["?", "what", "how", "when", "where", "who", "can you tell"]
        ):
            intent = EmailIntent.QUESTION
            workflow = SuggestedWorkflow.QUICK_REPLY
        elif any(
            kw in combined for kw in ["fyi", "for your information", "just wanted to let you know"]
        ):
            intent = EmailIntent.FYI
            workflow = SuggestedWorkflow.ARCHIVE
            triage_decision = TriageDecision.ARCHIVE
        elif any(kw in combined for kw in ["deadline", "due by", "by eod", "urgent", "asap"]):
            intent = EmailIntent.DEADLINE
            workflow = SuggestedWorkflow.CREATE_TASK
        elif any(
            kw in combined for kw in ["schedule", "meeting", "calendar", "call", "let's meet"]
        ):
            intent = EmailIntent.SCHEDULING
            workflow = SuggestedWorkflow.SCHEDULE

        # Check for attachments
        has_attachments = bool(attachments)
        requires_doc_analysis = has_attachments and intent == EmailIntent.REVIEW_REQUEST

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
            key_ask=(f"Review: {subject}" if intent == EmailIntent.REVIEW_REQUEST else subject),
            deadline=None,
            response_requirements=[],
            would_acknowledgment_be_unhelpful=requires_doc_analysis,
            triage_decision=triage_decision,
            factual_summary=subject,
        )


# Singleton instance
_classifier: EmailIntentClassifier | None = None


def get_email_intent_classifier() -> EmailIntentClassifier:
    """Get or create the email intent classifier singleton."""
    global _classifier
    if _classifier is None:
        _classifier = EmailIntentClassifier()
    return _classifier
