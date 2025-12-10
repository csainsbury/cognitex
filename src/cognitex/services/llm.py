"""Together.ai LLM integration for email classification and task inference."""

import json
from typing import Any

import structlog
from together import Together

from cognitex.config import get_settings

logger = structlog.get_logger()


class LLMService:
    """Service for interacting with Together.ai LLM APIs."""

    def __init__(self):
        settings = get_settings()
        api_key = settings.together_api_key.get_secret_value()
        if not api_key:
            raise ValueError("TOGETHER_API_KEY not configured")

        self.client = Together(api_key=api_key)
        self.primary_model = settings.together_model_primary
        self.fast_model = settings.together_model_fast
        self.embedding_model = settings.together_model_embedding

    async def complete(
        self,
        prompt: str,
        model: str | None = None,
        max_tokens: int = 1024,
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

        Args:
            text: Text to embed (max ~8k tokens for m2-bert)

        Returns:
            Embedding vector as list of floats
        """
        response = self.client.embeddings.create(
            model=self.embedding_model,
            input=text,
        )
        return response.data[0].embedding

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
