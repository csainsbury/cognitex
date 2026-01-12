"""Unified document analysis service with Skills + local fallback.

This module provides deep document analysis using Anthropic Skills for
enhanced capabilities (tracked changes, comments, semantic understanding),
with automatic fallback to local parsing when Skills are unavailable.
"""

import mimetypes
from typing import Any

import structlog

from cognitex.config import get_settings
from cognitex.services.document_parser import (
    DocumentAnalysis,
    LocalDocumentParser,
    UnsupportedDocumentType,
    get_document_parser,
)
from cognitex.services.llm import LLMService, get_llm_service

logger = structlog.get_logger()


# MIME type to Skill ID mapping
SKILL_FOR_MIME = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/msword": "docx",
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.ms-excel": "xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
}


class DocumentAnalyzer:
    """
    Unified document analyzer using Anthropic Skills with local fallback.

    The analyzer tries to use Anthropic Skills first for enhanced document
    understanding (semantic analysis, change detection, question extraction).
    If Skills are unavailable or fail, it falls back to local parsing.

    Usage:
        analyzer = get_document_analyzer()
        result = await analyzer.analyze(
            filename="report.docx",
            content=file_bytes,
            context="Sent for review with changes highlighted"
        )
    """

    def __init__(
        self,
        llm_service: LLMService | None = None,
        local_parser: LocalDocumentParser | None = None,
    ):
        """
        Initialize the document analyzer.

        Args:
            llm_service: LLM service for Skills analysis (uses singleton if not provided)
            local_parser: Local parser for fallback (uses singleton if not provided)
        """
        self._llm = llm_service
        self._local_parser = local_parser or get_document_parser()
        self._settings = get_settings()

    @property
    def llm(self) -> LLMService:
        """Lazy-load LLM service to avoid circular imports."""
        if self._llm is None:
            self._llm = get_llm_service()
        return self._llm

    def _get_mime_type(self, filename: str) -> str:
        """Get MIME type from filename."""
        mime_type, _ = mimetypes.guess_type(filename)
        return mime_type or "application/octet-stream"

    def _get_skill_for_type(self, mime_type: str) -> str | None:
        """Get the appropriate Skill ID for a MIME type."""
        return SKILL_FOR_MIME.get(mime_type)

    def is_supported(self, filename: str, mime_type: str | None = None) -> bool:
        """Check if document type is supported for analysis."""
        if mime_type is None:
            mime_type = self._get_mime_type(filename)

        # Supported if we have a Skill OR local parser can handle it
        has_skill = mime_type in SKILL_FOR_MIME
        has_local = self._local_parser.is_supported(filename, mime_type)

        return has_skill or has_local

    async def analyze(
        self,
        filename: str,
        content: bytes,
        context: str = "",
        mime_type: str | None = None,
        prefer_skills: bool = True,
    ) -> DocumentAnalysis:
        """
        Analyze a document, using Skills with local fallback.

        Args:
            filename: Name of the file
            content: Raw file bytes
            context: Optional context about the document (e.g., email subject)
            mime_type: Optional MIME type (guessed from filename if not provided)
            prefer_skills: Whether to try Skills first (default True)

        Returns:
            DocumentAnalysis with extracted content

        Raises:
            UnsupportedDocumentType: If document type not supported
        """
        if mime_type is None:
            mime_type = self._get_mime_type(filename)

        if not self.is_supported(filename, mime_type):
            raise UnsupportedDocumentType(
                f"Document type not supported: {filename} ({mime_type})"
            )

        # Try Skills first if enabled and preferred
        skills_enabled = self._settings.skills_enabled and prefer_skills
        skill_id = self._get_skill_for_type(mime_type)

        if skills_enabled and skill_id:
            try:
                logger.info(
                    "Analyzing document with Skills",
                    filename=filename,
                    skill=skill_id,
                )
                result = await self._analyze_with_skills(
                    filename, content, context, mime_type, skill_id
                )
                result.filename = filename
                return result
            except Exception as e:
                logger.warning(
                    "Skills analysis failed, falling back to local",
                    filename=filename,
                    error=str(e),
                )

        # Fallback to local parsing
        logger.info("Analyzing document locally", filename=filename)
        result = await self._local_parser.parse(filename, content, mime_type)
        result.filename = filename
        return result

    async def _analyze_with_skills(
        self,
        filename: str,
        content: bytes,
        context: str,
        mime_type: str,
        skill_id: str,
    ) -> DocumentAnalysis:
        """
        Analyze document using Anthropic Skills.

        Constructs a specialized prompt based on the document type and context.
        """
        # Build analysis prompt
        prompt = self._build_analysis_prompt(filename, context, skill_id)

        # Call Skills API
        result = await self.llm.analyze_with_skills(
            prompt=prompt,
            files=[(filename, content, mime_type)],
            skills=[skill_id],
        )

        # Convert to DocumentAnalysis with enhanced fields
        return DocumentAnalysis(
            summary=result.get("summary", ""),
            changes=result.get("changes", []),
            review_items=result.get("review_items", []),
            questions=[
                {"author": "document", "text": q}
                for q in result.get("questions", [])
            ],
            raw_text=result.get("raw_text", ""),
            method="skills",
            # Enhanced fields
            document_type=result.get("document_type", ""),
            maturity=result.get("maturity", ""),
            purpose=result.get("purpose", ""),
            key_decisions=result.get("key_decisions", []),
            action_items=result.get("action_items", []),
            risks=result.get("risks", []),
            key_findings=result.get("key_findings", []),
            recommendations=result.get("recommendations", []),
            sections=result.get("sections", []),
            key_entities=result.get("key_entities", {}),
            doc_author=result.get("doc_author", ""),
            doc_date=result.get("doc_date", ""),
            doc_version=result.get("doc_version", ""),
        )

    def _build_analysis_prompt(
        self,
        filename: str,
        context: str,
        skill_id: str,
    ) -> str:
        """Build the analysis prompt based on document type and context."""

        # Common deep analysis instructions
        deep_analysis_instructions = """
Additionally, perform deep semantic analysis:

1. **Document Classification**:
   - document_type: What kind of document is this? (proposal, report, specification, meeting_notes, status_update, decision_doc, contract, policy, guide, analysis, letter, other)
   - maturity: What stage is this document? (draft, in_review, final, archived)
   - purpose: In one sentence, what is this document for?

2. **Semantic Extraction**:
   - key_decisions: List any decisions made OR decisions that need to be made
   - action_items: List action items with format {item, assignee (if mentioned), deadline (if mentioned)}
   - risks: List any risks, concerns, blockers, or warnings mentioned
   - key_findings: List the main findings, conclusions, or results
   - recommendations: List any recommendations or suggestions made

3. **Structure**:
   - sections: List main sections with format {title, summary (1-2 sentences)}

4. **Key Entities**:
   - key_entities: Extract {people: [names], organizations: [org names], projects: [project names]}

5. **Metadata** (if found in document):
   - doc_author: Author name if mentioned
   - doc_date: Document date if mentioned
   - doc_version: Version number if mentioned

Return your response as JSON with ALL these fields."""

        base_prompt = f"""Analyze this document: "{filename}"
"""

        if context:
            base_prompt += f"""
Context: {context}
"""

        # Add type-specific instructions
        if skill_id == "docx":
            base_prompt += """
This is a Word document. Please:
1. Identify any tracked changes (insertions and deletions) with their authors if available
2. Find all comments in the document with author attribution
3. Locate highlighted or emphasized text (bold, underlined, colored)
4. Summarize the main content
5. List any questions or items that need review/decision
6. Identify the document structure (sections, headings)

Format your response as JSON:
{
    "summary": "Brief overview of the document content",
    "changes": ["Change 1: ...", "Change 2: ..."],
    "review_items": ["Item needing review 1", "Item 2"],
    "questions": ["Question 1", "Question 2"],
    "raw_text": "First 500 chars of document text...",
    ... (additional fields below)
}
"""

        elif skill_id == "pdf":
            base_prompt += """
This is a PDF document. Please:
1. Extract and summarize the main content
2. Identify the document structure (sections, chapters, key areas)
3. Note any items that appear to need review or action
4. List any questions or unclear items
5. Identify tables, figures, and their key data
6. Extract any form fields and their values

Format your response as JSON:
{
    "summary": "Brief overview of the document content",
    "changes": [],
    "review_items": ["Key point needing attention 1", "Point 2"],
    "questions": ["Question or unclear item 1"],
    "raw_text": "First 500 chars of document text...",
    ... (additional fields below)
}
"""

        elif skill_id == "xlsx":
            base_prompt += """
This is an Excel spreadsheet. Please:
1. Describe the structure (sheets, columns, data types, row counts)
2. Summarize what data this spreadsheet contains and its purpose
3. Identify key formulas, calculations, and their results
4. Note any validation rules, conditional formatting, or data constraints
5. Find any issues: missing data, errors, inconsistencies
6. Identify named ranges and their purposes

Format your response as JSON:
{
    "summary": "Overview of the spreadsheet structure and content",
    "changes": ["Notable data pattern or issue 1"],
    "review_items": ["Item needing verification 1"],
    "questions": ["Unclear data point 1"],
    "raw_text": "Sheet structure overview...",
    ... (additional fields below)
}
"""

        elif skill_id == "pptx":
            base_prompt += """
This is a PowerPoint presentation. Please:
1. Summarize the presentation topic, narrative arc, and key message
2. List each slide with its title and key points
3. Identify any items needing review or that seem incomplete
4. Note speaker notes if present
5. List any questions, action items, or next steps mentioned

Format your response as JSON:
{
    "summary": "Overview of the presentation",
    "changes": [],
    "review_items": ["Slide or content needing review 1"],
    "questions": ["Question raised in presentation 1"],
    "raw_text": "Slide titles and key content...",
    ... (additional fields below)
}
"""

        # Add deep analysis instructions
        base_prompt += deep_analysis_instructions

        return base_prompt

    async def analyze_for_review(
        self,
        filename: str,
        content: bytes,
        context: str,
        mime_type: str | None = None,
    ) -> DocumentAnalysis:
        """
        Analyze a document specifically for review requests.

        This is a convenience method that emphasizes finding changes,
        highlights, and items needing decision.

        Args:
            filename: Name of the file
            content: Raw file bytes
            context: Context about the review request (e.g., email content)
            mime_type: Optional MIME type

        Returns:
            DocumentAnalysis focused on review items
        """
        # Enhance context for review-focused analysis
        review_context = f"Document sent for review. {context}"
        if "highlight" in context.lower() or "change" in context.lower():
            review_context += " Focus on finding highlighted sections and changes."

        return await self.analyze(
            filename=filename,
            content=content,
            context=review_context,
            mime_type=mime_type,
        )


# Singleton instance
_analyzer: DocumentAnalyzer | None = None


def get_document_analyzer() -> DocumentAnalyzer:
    """Get or create the document analyzer singleton."""
    global _analyzer
    if _analyzer is None:
        _analyzer = DocumentAnalyzer()
    return _analyzer
