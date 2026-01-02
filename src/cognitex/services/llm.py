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
            from anthropic import Anthropic
            api_key = settings.anthropic_api_key.get_secret_value()
            if not api_key:
                raise ValueError("ANTHROPIC_API_KEY not configured")
            self.client = Anthropic(api_key=api_key)
            self.primary_model = settings.anthropic_model_planner
            self.fast_model = settings.anthropic_model_executor
            logger.info("LLMService using Anthropic Claude", model=self.primary_model)

        elif self.provider == "openai":
            from openai import OpenAI
            api_key = settings.openai_api_key.get_secret_value()
            if not api_key:
                raise ValueError("OPENAI_API_KEY not configured")
            self.client = OpenAI(api_key=api_key)
            self.primary_model = settings.openai_model_planner
            self.fast_model = settings.openai_model_executor
            logger.info("LLMService using OpenAI", model=self.primary_model)

        else:  # together (default)
            api_key = settings.together_api_key.get_secret_value()
            if not api_key:
                raise ValueError("TOGETHER_API_KEY not configured")
            self.client = Together(api_key=api_key)
            self.primary_model = settings.together_model_planner
            self.fast_model = settings.together_model_executor
            self.provider = "together"
            logger.info("LLMService using Together.ai", model=self.primary_model)

        # Embeddings always use Together.ai (Anthropic/Google don't have compatible embeddings)
        # Initialize a separate Together client for embeddings if using a different provider
        if self.provider != "together":
            together_key = settings.together_api_key.get_secret_value()
            if together_key:
                self._together_client = Together(api_key=together_key)
            else:
                self._together_client = None
                logger.warning("Together API key not set - embeddings will not work")
        else:
            self._together_client = self.client
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
                response = genai_model.generate_content(
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
                    fallback_client = Together(api_key=api_key)
                    fallback_model = "meta-llama/Llama-3.3-70B-Instruct-Turbo"
                    response = fallback_client.chat.completions.create(
                        model=fallback_model,
                        messages=[{"role": "user", "content": prompt}],
                        max_tokens=max_tokens,
                        temperature=temperature,
                    )
                    return response.choices[0].message.content
                else:
                    raise ValueError(f"Gemini failed ({fallback_reason}) and no fallback API key configured")

        elif self.provider == "anthropic":
            # Anthropic API
            response = self.client.messages.create(
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

            response = self.client.chat.completions.create(**kwargs)
            return response.choices[0].message.content

    async def classify_email(self, email_data: dict) -> dict:
        """
        Classify an email using LLM.

        Args:
            email_data: Email metadata including subject, snippet, sender

        Returns:
            Classification result with category, action_required, urgency, etc.
        """
        prompt = f"""Analyze this email and classify it. Return a JSON object with the classification.

Email:
- From: {email_data.get('sender_name', '')} <{email_data.get('sender_email', '')}>
- Subject: {email_data.get('subject', '')}
- Preview: {email_data.get('snippet', '')[:500]}

Classify this email with the following fields:
- classification: one of "actionable", "fyi", "newsletter", "automated", "spam", "personal"
- action_required: boolean - does this email require a response or action from me?
- urgency: integer 1-5 (1=low, 5=critical)
- sentiment: one of "positive", "neutral", "negative", "urgent"
- suggested_tasks: array of task titles if action is required (empty array if not)
- reply_needed: boolean - does this specifically need a reply?
- deadline_mentioned: string or null - any deadline mentioned in the email

Return ONLY valid JSON, no other text."""

        try:
            response = await self.complete(
                prompt,
                model=self.fast_model,  # Use fast model for classification
                max_tokens=512,
                temperature=0.1,
            )

            # Parse JSON response
            # Handle potential markdown code blocks
            response = response.strip()
            if response.startswith("```"):
                response = response.split("\n", 1)[1]
                response = response.rsplit("```", 1)[0]

            result = json.loads(response)

            # Validate and normalize
            return {
                "classification": result.get("classification", "fyi"),
                "action_required": bool(result.get("action_required", False)),
                "urgency": min(5, max(1, int(result.get("urgency", 2)))),
                "sentiment": result.get("sentiment", "neutral"),
                "suggested_tasks": result.get("suggested_tasks", []),
                "reply_needed": bool(result.get("reply_needed", False)),
                "deadline_mentioned": result.get("deadline_mentioned"),
            }

        except json.JSONDecodeError as e:
            logger.warning("Failed to parse LLM classification response", error=str(e))
            return {
                "classification": "unknown",
                "action_required": False,
                "urgency": 2,
                "sentiment": "neutral",
                "suggested_tasks": [],
                "reply_needed": False,
                "deadline_mentioned": None,
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

        Args:
            text: Text to embed (max ~8k tokens for m2-bert)

        Returns:
            Embedding vector as list of floats
        """
        if self._together_client is None:
            raise ValueError("Together.ai client not configured - embeddings unavailable")

        response = self._together_client.embeddings.create(
            model=self.embedding_model,
            input=text,
        )
        return response.data[0].embedding

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

3. "topics": List of 3-7 semantic topics/themes discussed.
   - Be specific and use domain language: "type 2 diabetes management", "continuous glucose monitoring"
   - Include both primary topic and subtopics

4. "concepts": List of domain concepts with definitions.
   - Format: {{"term": "...", "domain": "...", "definition": "..."}}
   - Domains: medical, technical, business, legal, scientific, etc.
   - Example: {{"term": "HbA1c", "domain": "medical", "definition": "glycated hemoglobin measure of blood sugar over 3 months"}}

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


# Singleton instance
_llm_service: LLMService | None = None


def get_llm_service() -> LLMService:
    """Get or create the LLM service singleton."""
    global _llm_service
    if _llm_service is None:
        _llm_service = LLMService()
    return _llm_service
