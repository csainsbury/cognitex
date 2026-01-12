"""Multi-provider LLM integration for email classification and task inference.

Supports: Together.ai, Google Gemini, Anthropic Claude, OpenAI
"""

import asyncio
import json
from functools import wraps
from typing import Any, Callable, TypeVar

import structlog
from together import Together

from cognitex.config import get_settings

logger = structlog.get_logger()

T = TypeVar("T")


def with_retry(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    exceptions: tuple = (Exception,),
) -> Callable:
    """
    Decorator to retry async functions with exponential backoff.

    Args:
        max_attempts: Maximum number of retry attempts
        base_delay: Initial delay in seconds
        max_delay: Maximum delay between retries
        exceptions: Tuple of exception types to catch
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            last_exception = None

            for attempt in range(max_attempts):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt == max_attempts - 1:
                        logger.error(
                            "LLM call failed after retries",
                            function=func.__name__,
                            attempts=max_attempts,
                            error=str(e),
                        )
                        raise

                    # Exponential backoff with jitter
                    delay = min(base_delay * (2 ** attempt), max_delay)
                    logger.warning(
                        "LLM call failed, retrying",
                        function=func.__name__,
                        attempt=attempt + 1,
                        delay=delay,
                        error=str(e),
                    )
                    await asyncio.sleep(delay)

            raise last_exception
        return wrapper
    return decorator


class LLMService:
    """Service for interacting with LLM APIs (multi-provider support)."""

    def __init__(self):
        settings = get_settings()
        self.provider = settings.llm_provider

        # Initialize the appropriate client based on provider
        if self.provider == "google":
            import google.generativeai as genai
            api_key = settings.google_ai_api_key.get_secret_value()
            if not api_key:
                raise ValueError("GOOGLE_AI_API_KEY not configured")
            genai.configure(api_key=api_key)
            self.client = genai
            self.primary_model = settings.google_model_planner
            self.fast_model = settings.google_model_executor
            logger.info("LLMService using Google Gemini", model=self.primary_model)

        elif self.provider == "anthropic":
            from anthropic import AsyncAnthropic
            api_key = settings.anthropic_api_key.get_secret_value()
            if not api_key:
                raise ValueError("ANTHROPIC_API_KEY not configured")
            self.client = AsyncAnthropic(api_key=api_key)
            self.primary_model = settings.anthropic_model_planner
            self.fast_model = settings.anthropic_model_executor
            logger.info("LLMService using Anthropic Claude (async)", model=self.primary_model)

        elif self.provider == "openai":
            from openai import AsyncOpenAI
            api_key = settings.openai_api_key.get_secret_value()
            if not api_key:
                raise ValueError("OPENAI_API_KEY not configured")
            self.client = AsyncOpenAI(api_key=api_key)
            self.primary_model = settings.openai_model_planner
            self.fast_model = settings.openai_model_executor
            logger.info("LLMService using OpenAI (async)", model=self.primary_model)

        else:  # together (default)
            from together import AsyncTogether
            api_key = settings.together_api_key.get_secret_value()
            if not api_key:
                raise ValueError("TOGETHER_API_KEY not configured")
            self.client = AsyncTogether(api_key=api_key)
            self.primary_model = settings.together_model_planner
            self.fast_model = settings.together_model_executor
            self.provider = "together"
            logger.info("LLMService using Together.ai (async)", model=self.primary_model)

        # Embeddings always use Together.ai (Anthropic/Google don't have compatible embeddings)
        # Use sync client wrapped in asyncio.to_thread for embeddings
        together_key = settings.together_api_key.get_secret_value()
        if together_key:
            self._embedding_client = Together(api_key=together_key)
        else:
            self._embedding_client = None
            logger.warning("Together API key not set - embeddings will not work")
        self.embedding_model = settings.together_model_embedding

    @with_retry(max_attempts=3, base_delay=1.0)
    async def complete(
        self,
        prompt: str,
        model: str | None = None,
        max_tokens: int = 8192,  # Gemini 3 supports much larger outputs
        temperature: float = 0.3,
        response_format: dict | None = None,
    ) -> str:
        """
        Generate a completion from the LLM.

        Args:
            prompt: The prompt to send
            model: Model to use (defaults to primary)
            max_tokens: Maximum tokens in response
            temperature: Sampling temperature
            response_format: Optional JSON schema for structured output

        Returns:
            Generated text response
        """
        model = model or self.primary_model

        if self.provider == "google":
            # Gemini API with minimal safety settings for work content analysis
            import google.generativeai as genai
            safety_settings = [
                {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
            ]
            genai_model = self.client.GenerativeModel(model)

            use_fallback = False
            fallback_reason = None

            try:
                response = await genai_model.generate_content_async(
                    prompt,
                    generation_config={
                        "max_output_tokens": max_tokens,
                        "temperature": temperature,
                    },
                    safety_settings=safety_settings,
                )

                # Handle incomplete or blocked responses
                if not response.parts:
                    finish_reason = None
                    finish_reason_value = None
                    if response.candidates:
                        finish_reason = response.candidates[0].finish_reason
                        finish_reason_value = getattr(finish_reason, 'value', None) or finish_reason
                        # FinishReason enum: 1=STOP, 2=MAX_TOKENS, 3=SAFETY, 4=RECITATION, 5=OTHER, 12=BLOCKLIST/PROHIBITED
                        if finish_reason_value in (2, "MAX_TOKENS"):
                            logger.warning("Gemini response hit max tokens limit")
                            raise ValueError("Response truncated - max tokens reached")
                        else:
                            # Any other blocking reason - use fallback
                            use_fallback = True
                            fallback_reason = f"finish_reason={finish_reason_value}"
                    else:
                        use_fallback = True
                        fallback_reason = "no candidates"
                else:
                    # Try to get text - this can also fail even with parts
                    try:
                        return response.text
                    except Exception as text_err:
                        use_fallback = True
                        fallback_reason = f"response.text failed: {str(text_err)}"

            except Exception as e:
                logger.warning("Gemini API call failed, trying fallback", error=str(e))
                use_fallback = True
                fallback_reason = str(e)

            # Fallback to Together.ai if Gemini failed
            if use_fallback:
                logger.info("Using Together.ai fallback", reason=fallback_reason)
                settings = get_settings()
                api_key = settings.together_api_key
                # Handle SecretStr
                if hasattr(api_key, 'get_secret_value'):
                    api_key = api_key.get_secret_value()
                if api_key:
                    from together import AsyncTogether
                    fallback_client = AsyncTogether(api_key=api_key)
                    fallback_model = "meta-llama/Llama-3.3-70B-Instruct-Turbo"
                    response = await fallback_client.chat.completions.create(
                        model=fallback_model,
                        messages=[{"role": "user", "content": prompt}],
                        max_tokens=max_tokens,
                        temperature=temperature,
                    )
                    return response.choices[0].message.content
                else:
                    raise ValueError(f"Gemini failed ({fallback_reason}) and no fallback API key configured")

        elif self.provider == "anthropic":
            # Anthropic API (async)
            response = await self.client.messages.create(
                model=model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text

        else:
            # OpenAI/Together format
            kwargs: dict[str, Any] = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "temperature": temperature,
            }

            if response_format:
                kwargs["response_format"] = response_format

            response = await self.client.chat.completions.create(**kwargs)
            return response.choices[0].message.content

    async def classify_email(self, email_data: dict) -> dict:
        """
        Classify an email using LLM with enhanced intelligence.

        Args:
            email_data: Email metadata including subject, snippet, sender, body

        Returns:
            Classification result with category, response_type, suggested_action, etc.
        """
        # Use body if available for better classification
        preview = email_data.get('body', email_data.get('snippet', ''))[:800]

        prompt = f"""Analyze this email and classify it. Return a JSON object with the classification.

Email:
- From: {email_data.get('sender_name', '')} <{email_data.get('sender_email', '')}>
- Subject: {email_data.get('subject', '')}
- Content: {preview}

Classify this email with the following fields:
- classification: one of "actionable", "fyi", "newsletter", "automated", "spam", "personal"
- urgency: one of "immediate" (needs response today), "today" (should respond today), "this_week", "whenever"
- response_type: one of "reply_needed" (needs email reply), "action_needed" (needs action but not reply), "acknowledge" (quick thanks/confirm), "forward" (delegate to someone), "archive" (no action), "none"
- suggested_action: brief description of what to do (e.g., "Schedule call with Sarah", "Review attached document", "Confirm meeting attendance")
- action_required: boolean - does this email require any response or action?
- sentiment: one of "positive", "neutral", "negative", "urgent"
- suggested_tasks: array of task titles if action is required (empty array if not)
- deadline_mentioned: string or null - any deadline mentioned in the email
- key_points: array of 1-3 key points from the email (for quick scanning)
- needs_research: boolean - does this email reference topics, entities, projects, or concepts that would benefit from background research before responding? (e.g., mentions unfamiliar companies, technical terms, people you should know about, project history)
- research_topics: array of strings - if needs_research is true, list specific topics to research (e.g., ["Acme Corp Q3 results", "Project Aurora timeline", "Jane Smith background"])

Return ONLY valid JSON, no other text."""

        try:
            response = await self.complete(
                prompt,
                model=self.fast_model,  # Use fast model for classification
                max_tokens=600,
                temperature=0.1,
            )

            # Parse JSON response
            # Handle potential markdown code blocks
            response = response.strip()
            if response.startswith("```"):
                response = response.split("\n", 1)[1]
                response = response.rsplit("```", 1)[0]

            result = json.loads(response)

            # Validate and normalize urgency
            urgency_map = {"immediate": 5, "today": 4, "this_week": 3, "whenever": 2}
            urgency_str = result.get("urgency", "whenever")
            if isinstance(urgency_str, int):
                urgency_int = min(5, max(1, urgency_str))
            else:
                urgency_int = urgency_map.get(urgency_str, 2)

            return {
                "classification": result.get("classification", "fyi"),
                "urgency": urgency_int,
                "urgency_label": urgency_str if isinstance(urgency_str, str) else "whenever",
                "response_type": result.get("response_type", "none"),
                "suggested_action": result.get("suggested_action"),
                "action_required": bool(result.get("action_required", False)),
                "sentiment": result.get("sentiment", "neutral"),
                "suggested_tasks": result.get("suggested_tasks", []),
                "reply_needed": result.get("response_type") in ("reply_needed", "acknowledge"),
                "deadline_mentioned": result.get("deadline_mentioned"),
                "key_points": result.get("key_points", []),
                "needs_research": bool(result.get("needs_research", False)),
                "research_topics": result.get("research_topics", []),
            }

        except json.JSONDecodeError as e:
            logger.warning("Failed to parse LLM classification response", error=str(e))
            return {
                "classification": "unknown",
                "urgency": 2,
                "urgency_label": "whenever",
                "response_type": "none",
                "suggested_action": None,
                "action_required": False,
                "sentiment": "neutral",
                "suggested_tasks": [],
                "reply_needed": False,
                "deadline_mentioned": None,
                "key_points": [],
                "needs_research": False,
                "research_topics": [],
                "parse_error": str(e),
            }

    async def infer_tasks_from_email(self, email_data: dict, body: str | None = None) -> list[dict]:
        """
        Infer actionable tasks from an email.

        Args:
            email_data: Email metadata
            body: Optional full email body for better context

        Returns:
            List of inferred tasks with title, description, energy_cost, due_date
        """
        content = body[:2000] if body else email_data.get("snippet", "")

        prompt = f"""Analyze this email and extract any actionable tasks for the recipient.

Email:
- From: {email_data.get('sender_name', '')} <{email_data.get('sender_email', '')}>
- Subject: {email_data.get('subject', '')}
- Content: {content}

For each task, provide:
- title: concise task title (action verb + object)
- description: brief context
- energy_cost: estimated effort 1-10 (1=trivial, 10=full day)
- due_date: ISO date if mentioned, null otherwise
- source_context: what in the email indicates this task

Return a JSON array of tasks. If no tasks, return empty array [].
Return ONLY valid JSON, no other text."""

        try:
            response = await self.complete(
                prompt,
                model=self.primary_model,  # Use primary model for task inference
                max_tokens=1024,
                temperature=0.2,
            )

            # Parse JSON response
            response = response.strip()
            if response.startswith("```"):
                response = response.split("\n", 1)[1]
                response = response.rsplit("```", 1)[0]

            tasks = json.loads(response)

            if not isinstance(tasks, list):
                return []

            # Normalize task structure
            normalized = []
            for task in tasks:
                normalized.append({
                    "title": task.get("title", "Untitled task"),
                    "description": task.get("description", ""),
                    "energy_cost": min(10, max(1, int(task.get("energy_cost", 3)))),
                    "due_date": task.get("due_date"),
                    "source_context": task.get("source_context", ""),
                })

            return normalized

        except json.JSONDecodeError as e:
            logger.warning("Failed to parse LLM task inference response", error=str(e))
            return []

    async def draft_reply(
        self,
        email_data: dict,
        body: str | None = None,
        tone: str = "professional",
        instructions: str | None = None,
    ) -> str:
        """
        Draft a reply to an email.

        Args:
            email_data: Email metadata
            body: Original email body
            tone: Desired tone (professional, friendly, brief)
            instructions: Additional instructions for the reply

        Returns:
            Draft reply text
        """
        content = body[:3000] if body else email_data.get("snippet", "")

        prompt = f"""Draft a reply to this email.

Original Email:
- From: {email_data.get('sender_name', '')} <{email_data.get('sender_email', '')}>
- Subject: {email_data.get('subject', '')}
- Content: {content}

Requirements:
- Tone: {tone}
- Keep it concise but complete
{f'- Additional instructions: {instructions}' if instructions else ''}

Write ONLY the email body (no subject line, no greeting like "Dear X" unless appropriate).
Start directly with the response content."""

        response = await self.complete(
            prompt,
            model=self.primary_model,
            max_tokens=1024,
            temperature=0.5,
        )

        return response.strip()

    async def extract_contact_info(self, email_data: dict, body: str | None = None) -> dict:
        """
        Extract contact information and infer traits from email communication.

        Args:
            email_data: Email metadata
            body: Email body content

        Returns:
            Dict with extracted/inferred contact info
        """
        content = body[:1500] if body else email_data.get("snippet", "")

        prompt = f"""Analyze this email to extract information about the sender.

Email:
- From: {email_data.get('sender_name', '')} <{email_data.get('sender_email', '')}>
- Subject: {email_data.get('subject', '')}
- Content: {content}

Extract and infer:
- organization: their company/org if apparent
- role: their job role if apparent
- communication_style: one of "formal", "casual", "terse", "verbose"
- urgency_tendency: do they tend to mark things urgent? "high", "normal", "low"

Return ONLY valid JSON, no other text. Use null for unknown fields."""

        try:
            response = await self.complete(
                prompt,
                model=self.fast_model,
                max_tokens=256,
                temperature=0.1,
            )

            response = response.strip()
            if response.startswith("```"):
                response = response.split("\n", 1)[1]
                response = response.rsplit("```", 1)[0]

            return json.loads(response)

        except json.JSONDecodeError:
            return {
                "organization": None,
                "role": None,
                "communication_style": None,
                "urgency_tendency": None,
            }

    async def generate_embedding(self, text: str) -> list[float]:
        """
        Generate an embedding vector for the given text.

        Uses Together.ai for embeddings regardless of primary LLM provider.
        Sync client is wrapped in asyncio.to_thread to avoid blocking.

        Args:
            text: Text to embed (max ~8k tokens for m2-bert)

        Returns:
            Embedding vector as list of floats
        """
        if self._embedding_client is None:
            raise ValueError("Together.ai client not configured - embeddings unavailable")

        # Wrap sync embedding call in thread with timeout to prevent freezing
        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    self._embedding_client.embeddings.create,
                    model=self.embedding_model,
                    input=text,
                ),
                timeout=30.0  # 30s timeout per embedding
            )
            return response.data[0].embedding
        except asyncio.TimeoutError:
            logger.error("Embedding generation timed out", text_length=len(text))
            raise

    async def extract_entities_from_chunk(
        self,
        content: str,
        document_name: str,
        chunk_index: int,
        use_deep_model: bool = True,
    ) -> dict:
        """
        Extract entities and semantic information from a document chunk.

        Uses the planner model (DeepSeek-V3) for deep semantic understanding.

        Args:
            content: The chunk text content
            document_name: Name of the source document
            chunk_index: Position of chunk in document
            use_deep_model: Use planner model for better extraction (default True)

        Returns:
            Dict with people, topics, concepts, relationships, summary, and key_facts
        """
        # Use more content for deep understanding (5000 chars vs 3000)
        content = content[:5000]

        prompt = f"""Analyze this document chunk deeply and extract structured semantic information.

Document: {document_name}
Chunk #{chunk_index}
Content:
{content}

Perform deep analysis and extract as JSON:

1. "people": List of people mentioned with context.
   - Include full names, email addresses, job titles/roles when available
   - Format as objects: {{"name": "...", "role": "...", "context": "..."}}
   - Example: {{"name": "Dr. Sarah Chen", "role": "Principal Investigator", "context": "leading the HbA1c study"}}

2. "organizations": List of organizations, companies, institutions mentioned.
   - Format: {{"name": "...", "type": "...", "context": "..."}}
   - Types: company, university, hospital, government, nonprofit, team

3. "topics": List of 3-7 semantic topics/themes discussed WITH RELEVANCE SCORES.
   - Be specific and use domain language: "type 2 diabetes management", "continuous glucose monitoring"
   - Format: {{"name": "...", "relevance": 0.0-1.0, "is_primary": true/false}}
   - relevance: 0.9-1.0 = central theme, 0.7-0.9 = important, 0.5-0.7 = relevant, <0.5 = tangential
   - Mark 1-2 topics as is_primary: true (the main focus of this chunk)

4. "concepts": List of domain concepts with definitions AND CONFIDENCE SCORES.
   - Format: {{"term": "...", "domain": "...", "definition": "...", "confidence": 0.0-1.0}}
   - Domains: medical, technical, business, legal, scientific, etc.
   - confidence: how certain you are about the extraction (0.9+ = very certain)
   - Example: {{"term": "HbA1c", "domain": "medical", "definition": "glycated hemoglobin measure of blood sugar over 3 months", "confidence": 0.95}}

5. "relationships": List of relationships between entities.
   - Format: {{"from": "...", "relationship": "...", "to": "..."}}
   - Example: {{"from": "Dr. Chen", "relationship": "supervises", "to": "research team"}}
   - Example: {{"from": "Study A", "relationship": "funded_by", "to": "NHS"}}

6. "summary": A 2-3 sentence comprehensive summary capturing the key information.

7. "key_facts": List of specific, verifiable facts.
   - Include dates, numbers, decisions, outcomes, metrics
   - Format: {{"fact": "...", "category": "...", "confidence": "high|medium"}}
   - Categories: metric, date, decision, outcome, requirement

8. "content_type": Primary type: "research", "correspondence", "data", "narrative", "code", "meeting_notes", "planning", "report"

9. "semantic_tags": List of 3-5 high-level tags for clustering similar content.
   - Examples: "healthcare_research", "project_management", "financial_planning", "technical_documentation"

10. "actionable_items": Any tasks, to-dos, or action items mentioned.
    - Format: {{"action": "...", "assignee": "...", "deadline": "..."}}

Return ONLY valid JSON, no markdown formatting or explanation."""

        # Use planner model for deep understanding
        model = self.primary_model if use_deep_model else self.fast_model

        try:
            response = await self.complete(
                prompt,
                model=model,
                max_tokens=2048,
                temperature=0.1,
            )

            response = response.strip()
            if response.startswith("```"):
                response = response.split("\n", 1)[1]
                response = response.rsplit("```", 1)[0]

            result = json.loads(response)

            # Normalize people format (support both old string format and new object format)
            people = result.get("people", [])
            normalized_people = []
            for p in people:
                if isinstance(p, str):
                    normalized_people.append({"name": p, "role": "", "context": ""})
                elif isinstance(p, dict):
                    normalized_people.append(p)

            # Normalize concepts format
            concepts = result.get("concepts", [])
            normalized_concepts = []
            for c in concepts:
                if isinstance(c, str):
                    normalized_concepts.append({"term": c, "domain": "", "definition": ""})
                elif isinstance(c, dict):
                    normalized_concepts.append(c)

            return {
                "people": normalized_people,
                "organizations": result.get("organizations", []),
                "topics": result.get("topics", []),
                "concepts": normalized_concepts,
                "relationships": result.get("relationships", []),
                "summary": result.get("summary", ""),
                "key_facts": result.get("key_facts", []),
                "content_type": result.get("content_type", "mixed"),
                "semantic_tags": result.get("semantic_tags", []),
                "actionable_items": result.get("actionable_items", []),
            }

        except json.JSONDecodeError as e:
            logger.warning("Failed to parse entity extraction response", error=str(e))
            return {
                "people": [],
                "organizations": [],
                "topics": [],
                "concepts": [],
                "relationships": [],
                "summary": "",
                "key_facts": [],
                "content_type": "unknown",
                "semantic_tags": [],
                "actionable_items": [],
                "parse_error": str(e),
            }

    async def enrich_contact(
        self,
        email_address: str,
        name: str | None = None,
        sample_snippets: list[str] | None = None,
        interaction_summary: str | None = None,
    ) -> dict:
        """
        Enrich contact information based on email address, name, and sample communications.

        Args:
            email_address: Contact's email address
            name: Contact's name if known
            sample_snippets: Sample email snippets from this contact
            interaction_summary: Summary of interaction patterns

        Returns:
            Dict with inferred org, role, communication_style, urgency_tendency
        """
        # Extract domain for org inference
        domain = email_address.split("@")[1] if "@" in email_address else ""

        snippets_text = "\n".join(sample_snippets[:3]) if sample_snippets else "No samples available"

        prompt = f"""Analyze this contact and infer information about them.

Contact:
- Email: {email_address}
- Name: {name or 'Unknown'}
- Email domain: {domain}
{f'- Interaction: {interaction_summary}' if interaction_summary else ''}

Sample communications from them:
{snippets_text}

Based on their email domain, name, and communication samples, infer:
- organization: their company/organization (use domain hints, e.g., @google.com = Google)
- role: their likely job role/title if apparent from context or signature patterns
- communication_style: one of "formal", "casual", "terse", "verbose" based on their writing
- urgency_tendency: based on their communication patterns, "high", "normal", or "low"

Return ONLY valid JSON. Use null for fields you cannot reasonably infer."""

        try:
            response = await self.complete(
                prompt,
                model=self.fast_model,
                max_tokens=256,
                temperature=0.1,
            )

            response = response.strip()
            if response.startswith("```"):
                response = response.split("\n", 1)[1]
                response = response.rsplit("```", 1)[0]

            result = json.loads(response)

            return {
                "organization": result.get("organization"),
                "role": result.get("role"),
                "communication_style": result.get("communication_style"),
                "urgency_tendency": result.get("urgency_tendency"),
            }

        except json.JSONDecodeError:
            # Fall back to domain-based org inference
            org = None
            if domain and not domain.endswith(("gmail.com", "yahoo.com", "hotmail.com", "outlook.com")):
                # Use domain as org hint
                org = domain.split(".")[0].title()

            return {
                "organization": org,
                "role": None,
                "communication_style": None,
                "urgency_tendency": None,
            }

    async def analyze_with_skills(
        self,
        prompt: str,
        files: list[tuple[str, bytes, str]],  # (filename, content, mime_type)
        skills: list[str] | None = None,
    ) -> dict:
        """
        Analyze content using Anthropic Skills with code execution.

        This method uses Anthropic's beta Skills API to analyze documents
        with specialized capabilities (DOCX, PDF, XLSX, PPTX processing).

        Args:
            prompt: Analysis prompt describing what to extract
            files: List of (filename, content_bytes, mime_type) tuples
            skills: List of skill IDs to use (e.g., ["docx", "pdf"])
                   Defaults to ["docx", "pdf"] if not specified

        Returns:
            Dict with analysis results parsed from the response

        Raises:
            ValueError: If provider is not Anthropic or Skills are not enabled
            Exception: If Skills API call fails
        """
        settings = get_settings()

        if self.provider != "anthropic":
            raise ValueError("Skills analysis requires Anthropic provider")

        if not settings.skills_enabled:
            raise ValueError("Skills are disabled in configuration")

        skills = skills or ["docx", "pdf"]

        try:
            # Upload files to Anthropic Files API
            file_ids = []
            for filename, content, mime_type in files:
                # Use beta files API to upload
                file_response = await self.client.beta.files.upload(
                    file=(filename, content, mime_type),
                    betas=["files-api-2025-04-14"]
                )
                file_ids.append(file_response.id)
                logger.debug("Uploaded file for Skills analysis", filename=filename, file_id=file_response.id)

            # Build message content with files
            message_content = [{"type": "text", "text": prompt}]
            for file_id in file_ids:
                message_content.append({"type": "file", "file_id": file_id})

            # Call with Skills and code execution
            response = await self.client.beta.messages.create(
                model=self.primary_model,
                max_tokens=8192,
                betas=["code-execution-2025-08-25", "skills-2025-10-02"],
                container={
                    "skills": [
                        {"type": "anthropic", "skill_id": skill, "version": "latest"}
                        for skill in skills
                    ]
                },
                messages=[{
                    "role": "user",
                    "content": message_content
                }],
                tools=[{"type": "code_execution_20250825", "name": "code_execution"}]
            )

            # Handle pause_turn for long-running operations
            max_continuations = 5
            for _ in range(max_continuations):
                if response.stop_reason != "pause_turn":
                    break

                logger.debug("Skills operation paused, continuing...")
                response = await self.client.beta.messages.create(
                    model=self.primary_model,
                    max_tokens=8192,
                    betas=["code-execution-2025-08-25", "skills-2025-10-02"],
                    container={
                        "id": response.container.id,
                        "skills": [
                            {"type": "anthropic", "skill_id": skill, "version": "latest"}
                            for skill in skills
                        ]
                    },
                    messages=[
                        {"role": "user", "content": message_content},
                        {"role": "assistant", "content": response.content}
                    ],
                    tools=[{"type": "code_execution_20250825", "name": "code_execution"}]
                )

            # Parse response content
            result = self._parse_skills_response(response)

            # Clean up uploaded files
            for file_id in file_ids:
                try:
                    await self.client.beta.files.delete(
                        file_id=file_id,
                        betas=["files-api-2025-04-14"]
                    )
                except Exception as e:
                    logger.warning("Failed to delete uploaded file", file_id=file_id, error=str(e))

            return result

        except Exception as e:
            logger.error("Skills analysis failed", error=str(e), skills=skills)
            raise

    def _parse_skills_response(self, response) -> dict:
        """
        Parse response from Skills API call.

        Extracts text content and any structured data from the response.
        """
        result = {
            "raw_text": "",
            "summary": "",
            "changes": [],
            "review_items": [],
            "questions": [],
            "method": "skills",
        }

        for block in response.content:
            if hasattr(block, "text"):
                result["raw_text"] += block.text + "\n"
            elif hasattr(block, "type") and block.type == "tool_result":
                # Handle code execution results
                if hasattr(block, "content"):
                    result["raw_text"] += str(block.content) + "\n"

        # Parse structured sections from raw text
        raw = result["raw_text"]

        # Extract SUMMARY section
        if "SUMMARY:" in raw:
            summary_start = raw.index("SUMMARY:") + len("SUMMARY:")
            summary_end = raw.find("\n\n", summary_start)
            if summary_end == -1:
                summary_end = raw.find("CHANGES:", summary_start)
            if summary_end == -1:
                summary_end = len(raw)
            result["summary"] = raw[summary_start:summary_end].strip()

        # Extract CHANGES section
        if "CHANGES:" in raw:
            changes_start = raw.index("CHANGES:") + len("CHANGES:")
            changes_end = raw.find("REVIEW_ITEMS:", changes_start)
            if changes_end == -1:
                changes_end = raw.find("QUESTIONS:", changes_start)
            if changes_end == -1:
                changes_end = raw.find("\n\n", changes_start + 10)
            if changes_end == -1:
                changes_end = len(raw)
            changes_text = raw[changes_start:changes_end].strip()
            # Parse bullet points
            for line in changes_text.split("\n"):
                line = line.strip()
                if line.startswith(("-", "*", "•")):
                    result["changes"].append(line.lstrip("-*• ").strip())

        # Extract REVIEW_ITEMS section
        if "REVIEW_ITEMS:" in raw:
            items_start = raw.index("REVIEW_ITEMS:") + len("REVIEW_ITEMS:")
            items_end = raw.find("QUESTIONS:", items_start)
            if items_end == -1:
                items_end = raw.find("\n\n", items_start + 10)
            if items_end == -1:
                items_end = len(raw)
            items_text = raw[items_start:items_end].strip()
            for line in items_text.split("\n"):
                line = line.strip()
                if line.startswith(("-", "*", "•", "1", "2", "3", "4", "5")):
                    result["review_items"].append(line.lstrip("-*•0123456789. ").strip())

        # Extract QUESTIONS section
        if "QUESTIONS:" in raw:
            questions_start = raw.index("QUESTIONS:") + len("QUESTIONS:")
            questions_end = len(raw)
            questions_text = raw[questions_start:questions_end].strip()
            for line in questions_text.split("\n"):
                line = line.strip()
                if line.startswith(("-", "*", "•", "?")):
                    result["questions"].append(line.lstrip("-*•? ").strip())
                elif "?" in line and len(line) > 5:
                    result["questions"].append(line.strip())

        return result


# Singleton instance
_llm_service: LLMService | None = None


def get_llm_service() -> LLMService:
    """Get or create the LLM service singleton."""
    global _llm_service
    if _llm_service is None:
        _llm_service = LLMService()
    return _llm_service
