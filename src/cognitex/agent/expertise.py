"""Domain Expertise System - Self-improving agent mental models.

This module implements the "Agent Expert" pattern where agents maintain
and automatically update their own expertise/mental models for specific domains.

Key concepts:
- Expertise File: Structured knowledge about a domain (project, skill, workflow)
- Self-Improve: After successful actions, agents update their expertise
- Read-First: Before acting, agents load relevant expertise to bootstrap context

The difference between a generic agent and an agent expert:
- Generic agent: executes and forgets
- Agent expert: executes and learns
"""

import json
import uuid
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import text

from cognitex.db.postgres import get_session

logger = structlog.get_logger()


# Default expertise structure templates
EXPERTISE_TEMPLATES = {
    "project": {
        "overview": "",
        "key_contacts": [],  # [{name, email, role, preferences}]
        "key_files": [],  # [{path, purpose, notes}]
        "patterns": [],  # Coding/communication patterns observed
        "preferences": [],  # User preferences for this project
        "common_tasks": [],  # Types of tasks that get created
        "approval_patterns": [],  # What gets approved vs rejected
        "notes": [],  # General observations
    },
    "skill": {
        "description": "",
        "best_practices": [],
        "common_pitfalls": [],
        "examples": [],  # Successful examples
        "preferences": [],
        "notes": [],
    },
    "entity": {
        "name": "",
        "type": "",  # person, company, etc.
        "relationships": [],
        "communication_style": "",
        "preferences": [],
        "history": [],  # Key interactions
        "notes": [],
    },
    "workflow": {
        "description": "",
        "steps": [],
        "triggers": [],  # When to use this workflow
        "success_criteria": [],
        "learned_optimizations": [],
        "notes": [],
    },
}


class ExpertiseManager:
    """Manages domain expertise - the agent's self-improving mental models."""

    def __init__(self):
        self._cache: dict[str, dict] = {}

    async def get_expertise(self, domain: str) -> dict | None:
        """Load expertise for a specific domain.

        Args:
            domain: Domain identifier (e.g., 'project:cognitex', 'email_drafting')

        Returns:
            Expertise dict with content and metadata, or None if not found
        """
        # Check cache first
        if domain in self._cache:
            # Update last_used_at in background
            await self._update_last_used(domain)
            return self._cache[domain]

        async for session in get_session():
            try:
                result = await session.execute(
                    text("""
                        SELECT id, domain, domain_type, title, expertise_content,
                               version, learnings_count, last_improved_at, created_at
                        FROM domain_expertise
                        WHERE domain = :domain
                    """),
                    {"domain": domain}
                )
                row = result.fetchone()

                if row:
                    expertise = {
                        "id": row.id,
                        "domain": row.domain,
                        "domain_type": row.domain_type,
                        "title": row.title,
                        "content": row.expertise_content if isinstance(row.expertise_content, dict)
                                   else json.loads(row.expertise_content) if row.expertise_content else {},
                        "version": row.version,
                        "learnings_count": row.learnings_count,
                        "last_improved_at": row.last_improved_at.isoformat() if row.last_improved_at else None,
                        "created_at": row.created_at.isoformat() if row.created_at else None,
                    }
                    self._cache[domain] = expertise

                    # Update last_used_at
                    await session.execute(
                        text("UPDATE domain_expertise SET last_used_at = NOW() WHERE domain = :domain"),
                        {"domain": domain}
                    )
                    await session.commit()

                    return expertise
            except Exception as e:
                logger.warning("Failed to get expertise", domain=domain, error=str(e))
            break

        return None

    async def _update_last_used(self, domain: str) -> None:
        """Update the last_used_at timestamp."""
        async for session in get_session():
            try:
                await session.execute(
                    text("UPDATE domain_expertise SET last_used_at = NOW() WHERE domain = :domain"),
                    {"domain": domain}
                )
                await session.commit()
            except Exception:
                pass
            break

    async def create_expertise(
        self,
        domain: str,
        domain_type: str,
        title: str,
        initial_content: dict | None = None,
    ) -> str:
        """Create a new expertise domain.

        Args:
            domain: Unique domain identifier
            domain_type: Type of expertise ('project', 'skill', 'entity', 'workflow')
            title: Human-readable title
            initial_content: Optional initial content, otherwise uses template

        Returns:
            Expertise ID
        """
        expertise_id = f"exp_{uuid.uuid4().hex[:12]}"

        # Use template if no initial content
        content = initial_content or EXPERTISE_TEMPLATES.get(domain_type, {}).copy()

        # Generate embedding for the domain
        embedding_str = None
        try:
            from cognitex.services.llm import get_llm_service
            llm = get_llm_service()
            embed_text = f"{title} {domain} {domain_type}"
            embedding = await llm.generate_embedding(embed_text[:500])
            embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"
        except Exception as e:
            logger.warning("Failed to generate expertise embedding", error=str(e))

        async for session in get_session():
            try:
                await session.execute(
                    text("""
                        INSERT INTO domain_expertise (
                            id, domain, domain_type, title, expertise_content,
                            expertise_embedding, created_at
                        ) VALUES (
                            :id, :domain, :domain_type, :title, :content,
                            CAST(:embedding AS vector), NOW()
                        )
                        ON CONFLICT (domain) DO UPDATE SET
                            title = EXCLUDED.title,
                            expertise_content = EXCLUDED.expertise_content
                    """),
                    {
                        "id": expertise_id,
                        "domain": domain,
                        "domain_type": domain_type,
                        "title": title,
                        "content": json.dumps(content),
                        "embedding": embedding_str,
                    }
                )
                await session.commit()

                logger.info("Created expertise", expertise_id=expertise_id, domain=domain)
                return expertise_id

            except Exception as e:
                logger.error("Failed to create expertise", error=str(e))
                await session.rollback()
            break

        return expertise_id

    async def self_improve(
        self,
        domain: str,
        action_type: str,
        action_result: dict,
        context: dict | None = None,
    ) -> dict:
        """Update expertise after a successful action - the core learning loop.

        This is called after approved tasks, sent emails, etc. to capture
        what worked and update the mental model.

        Args:
            domain: Domain to update
            action_type: Type of action ('task_approved', 'email_sent', 'draft_approved')
            action_result: Details of what happened
            context: Additional context

        Returns:
            Dict with update results
        """
        from cognitex.services.llm import get_llm_service

        # Get current expertise
        expertise = await self.get_expertise(domain)
        if not expertise:
            # Auto-create if doesn't exist
            domain_type = domain.split(":")[0] if ":" in domain else "skill"
            await self.create_expertise(domain, domain_type, domain)
            expertise = await self.get_expertise(domain)

        if not expertise:
            return {"error": "Could not create expertise"}

        current_content = expertise.get("content", {})
        context = context or {}

        # Use LLM to extract learnings from the action
        llm = get_llm_service()

        extract_prompt = f"""Analyze this successful action and extract learnings for the agent's expertise.

Domain: {domain}
Action Type: {action_type}
Action Result: {json.dumps(action_result, indent=2)[:1500]}
Context: {json.dumps(context, indent=2)[:500]}

Current Expertise:
{json.dumps(current_content, indent=2)[:2000]}

Extract learnings in these categories:
1. **Patterns**: Recurring patterns worth remembering
2. **Preferences**: User preferences revealed by this action
3. **Facts**: Concrete facts to remember (names, paths, relationships)
4. **Optimizations**: Ways to do this better next time

Return a JSON object with:
{{
    "learnings": [
        {{"type": "pattern|preference|fact|optimization", "content": "...", "confidence": 0.0-1.0}}
    ],
    "content_updates": {{
        "key_to_update": "new_value_or_append_item"
    }},
    "summary": "One sentence summary of what was learned"
}}

Only include meaningful learnings. Return empty arrays if nothing significant to learn.
Return ONLY valid JSON."""

        try:
            response = await llm.complete(
                extract_prompt,
                model=llm.fast_model,
                max_tokens=800,
                temperature=0.3,
            )

            # Parse response
            response = response.strip()
            if response.startswith("```"):
                response = response.split("\n", 1)[1]
                response = response.rsplit("```", 1)[0]

            learning_data = json.loads(response)
            learnings = learning_data.get("learnings", [])
            content_updates = learning_data.get("content_updates", {})
            summary = learning_data.get("summary", "")

            # Record individual learnings
            for learning in learnings:
                await self._record_learning(
                    expertise_id=expertise["id"],
                    learning_type=learning.get("type", "pattern"),
                    learning_content=learning,
                    source_action=action_type,
                    source_id=action_result.get("id"),
                    confidence=learning.get("confidence", 0.7),
                )

            # Apply content updates
            if content_updates:
                updated_content = self._merge_content_updates(current_content, content_updates)
                await self._update_expertise_content(expertise["id"], updated_content)

            # Clear cache
            if domain in self._cache:
                del self._cache[domain]

            logger.info(
                "Self-improved expertise",
                domain=domain,
                action_type=action_type,
                learnings_count=len(learnings),
                summary=summary[:100],
            )

            return {
                "success": True,
                "learnings_count": len(learnings),
                "summary": summary,
                "learnings": learnings,
            }

        except json.JSONDecodeError as e:
            logger.warning("Failed to parse learning extraction", error=str(e))
            return {"error": "Failed to parse learnings"}
        except Exception as e:
            logger.error("Self-improve failed", error=str(e))
            return {"error": str(e)}

    def _merge_content_updates(self, current: dict, updates: dict) -> dict:
        """Merge content updates into current content."""
        result = current.copy()

        for key, value in updates.items():
            if key in result:
                if isinstance(result[key], list) and isinstance(value, str):
                    # Append to list
                    if value not in result[key]:
                        result[key].append(value)
                elif isinstance(result[key], list) and isinstance(value, list):
                    # Extend list, avoiding duplicates
                    for item in value:
                        if item not in result[key]:
                            result[key].append(item)
                else:
                    # Replace value
                    result[key] = value
            else:
                result[key] = value

        return result

    async def _record_learning(
        self,
        expertise_id: str,
        learning_type: str,
        learning_content: dict,
        source_action: str,
        source_id: str | None,
        confidence: float,
    ) -> str:
        """Record an individual learning."""
        learning_id = f"learn_{uuid.uuid4().hex[:12]}"

        async for session in get_session():
            try:
                await session.execute(
                    text("""
                        INSERT INTO expertise_learnings (
                            id, expertise_id, learning_type, learning_content,
                            source_action, source_id, confidence, created_at
                        ) VALUES (
                            :id, :expertise_id, :learning_type, :content,
                            :source_action, :source_id, :confidence, NOW()
                        )
                    """),
                    {
                        "id": learning_id,
                        "expertise_id": expertise_id,
                        "learning_type": learning_type,
                        "content": json.dumps(learning_content),
                        "source_action": source_action,
                        "source_id": source_id,
                        "confidence": confidence,
                    }
                )

                # Update expertise metadata
                await session.execute(
                    text("""
                        UPDATE domain_expertise
                        SET learnings_count = learnings_count + 1,
                            last_improved_at = NOW(),
                            version = version + 1
                        WHERE id = :expertise_id
                    """),
                    {"expertise_id": expertise_id}
                )

                await session.commit()
            except Exception as e:
                logger.warning("Failed to record learning", error=str(e))
                await session.rollback()
            break

        return learning_id

    async def _update_expertise_content(self, expertise_id: str, content: dict) -> None:
        """Update the expertise content."""
        async for session in get_session():
            try:
                await session.execute(
                    text("""
                        UPDATE domain_expertise
                        SET expertise_content = :content,
                            version = version + 1,
                            last_improved_at = NOW()
                        WHERE id = :id
                    """),
                    {"id": expertise_id, "content": json.dumps(content)}
                )
                await session.commit()
            except Exception as e:
                logger.warning("Failed to update expertise content", error=str(e))
            break

    async def get_relevant_expertise(
        self,
        context_text: str,
        domain_types: list[str] | None = None,
        limit: int = 3,
        min_similarity: float = 0.5,
    ) -> list[dict]:
        """Find expertise relevant to a given context using semantic search.

        Args:
            context_text: Text describing the current context
            domain_types: Optional filter by domain type
            limit: Max results
            min_similarity: Minimum similarity threshold

        Returns:
            List of relevant expertise dicts
        """
        from cognitex.services.llm import get_llm_service

        try:
            llm = get_llm_service()
            query_embedding = await llm.generate_embedding(context_text[:500])
            query_emb_str = "[" + ",".join(str(x) for x in query_embedding) + "]"
        except Exception as e:
            logger.warning("Failed to generate query embedding", error=str(e))
            return []

        results = []
        async for session in get_session():
            try:
                type_filter = ""
                params = {
                    "query_embedding": query_emb_str,
                    "min_similarity": min_similarity,
                    "limit": limit,
                }

                if domain_types:
                    type_filter = "AND domain_type = ANY(:domain_types)"
                    params["domain_types"] = domain_types

                result = await session.execute(
                    text(f"""
                        SELECT id, domain, domain_type, title, expertise_content,
                               version, learnings_count,
                               1 - (expertise_embedding <=> CAST(:query_embedding AS vector)) as similarity
                        FROM domain_expertise
                        WHERE expertise_embedding IS NOT NULL
                          AND 1 - (expertise_embedding <=> CAST(:query_embedding AS vector)) >= :min_similarity
                          {type_filter}
                        ORDER BY expertise_embedding <=> CAST(:query_embedding AS vector)
                        LIMIT :limit
                    """),
                    params
                )

                for row in result.fetchall():
                    content = row.expertise_content
                    if isinstance(content, str):
                        content = json.loads(content)

                    results.append({
                        "id": row.id,
                        "domain": row.domain,
                        "domain_type": row.domain_type,
                        "title": row.title,
                        "content": content,
                        "version": row.version,
                        "learnings_count": row.learnings_count,
                        "similarity": float(row.similarity),
                    })

            except Exception as e:
                logger.warning("Failed to search expertise", error=str(e))
            break

        return results

    async def get_expertise_for_prompt(
        self,
        domains: list[str] | None = None,
        context_text: str | None = None,
        max_length: int = 2000,
    ) -> str:
        """Format expertise for injection into agent prompts.

        Args:
            domains: Specific domains to include
            context_text: If provided, find relevant expertise
            max_length: Max characters to return

        Returns:
            Formatted expertise text for prompt injection
        """
        expertise_items = []

        # Get specific domains
        if domains:
            for domain in domains:
                exp = await self.get_expertise(domain)
                if exp:
                    expertise_items.append(exp)

        # Get contextually relevant expertise
        if context_text:
            relevant = await self.get_relevant_expertise(context_text, limit=2)
            for exp in relevant:
                if exp["domain"] not in [e["domain"] for e in expertise_items]:
                    expertise_items.append(exp)

        if not expertise_items:
            return ""

        # Format for prompt
        lines = ["## Agent Expertise (Mental Models)\n"]

        for exp in expertise_items[:5]:  # Max 5 expertise items
            lines.append(f"### {exp.get('title', exp['domain'])}")
            lines.append(f"_Domain: {exp['domain']} | Version: {exp.get('version', 1)} | Learnings: {exp.get('learnings_count', 0)}_\n")

            content = exp.get("content", {})

            # Format key sections concisely
            if content.get("overview"):
                lines.append(f"**Overview:** {content['overview'][:300]}")

            if content.get("key_contacts"):
                contacts = ", ".join([c.get("name", c.get("email", "")) for c in content["key_contacts"][:5]])
                lines.append(f"**Key Contacts:** {contacts}")

            if content.get("preferences"):
                prefs = content["preferences"][:3]
                lines.append("**Preferences:**")
                for pref in prefs:
                    if isinstance(pref, str):
                        lines.append(f"  - {pref[:100]}")
                    elif isinstance(pref, dict):
                        lines.append(f"  - {pref.get('content', str(pref))[:100]}")

            if content.get("patterns"):
                patterns = content["patterns"][:3]
                lines.append("**Patterns:**")
                for pattern in patterns:
                    if isinstance(pattern, str):
                        lines.append(f"  - {pattern[:100]}")
                    elif isinstance(pattern, dict):
                        lines.append(f"  - {pattern.get('content', str(pattern))[:100]}")

            if content.get("notes"):
                notes = content["notes"][:2]
                lines.append("**Notes:**")
                for note in notes:
                    if isinstance(note, str):
                        lines.append(f"  - {note[:100]}")

            lines.append("")

        result = "\n".join(lines)

        # Truncate if too long
        if len(result) > max_length:
            result = result[:max_length] + "\n\n_(expertise truncated)_"

        return result

    async def get_recent_learnings(
        self,
        expertise_id: str | None = None,
        limit: int = 10,
    ) -> list[dict]:
        """Get recent learnings, optionally filtered by expertise."""
        results = []

        async for session in get_session():
            try:
                if expertise_id:
                    query = text("""
                        SELECT el.id, el.learning_type, el.learning_content,
                               el.source_action, el.confidence, el.created_at,
                               de.domain, de.title
                        FROM expertise_learnings el
                        JOIN domain_expertise de ON el.expertise_id = de.id
                        WHERE el.expertise_id = :expertise_id
                        ORDER BY el.created_at DESC
                        LIMIT :limit
                    """)
                    params = {"expertise_id": expertise_id, "limit": limit}
                else:
                    query = text("""
                        SELECT el.id, el.learning_type, el.learning_content,
                               el.source_action, el.confidence, el.created_at,
                               de.domain, de.title
                        FROM expertise_learnings el
                        JOIN domain_expertise de ON el.expertise_id = de.id
                        ORDER BY el.created_at DESC
                        LIMIT :limit
                    """)
                    params = {"limit": limit}

                result = await session.execute(query, params)

                for row in result.fetchall():
                    content = row.learning_content
                    if isinstance(content, str):
                        content = json.loads(content)

                    results.append({
                        "id": row.id,
                        "type": row.learning_type,
                        "content": content,
                        "source_action": row.source_action,
                        "confidence": row.confidence,
                        "domain": row.domain,
                        "title": row.title,
                        "created_at": row.created_at.isoformat() if row.created_at else None,
                    })
            except Exception as e:
                logger.warning("Failed to get recent learnings", error=str(e))
            break

        return results

    async def list_expertise(self, domain_type: str | None = None) -> list[dict]:
        """List all expertise domains."""
        results = []

        async for session in get_session():
            try:
                if domain_type:
                    query = text("""
                        SELECT id, domain, domain_type, title, version,
                               learnings_count, last_improved_at, last_used_at, created_at
                        FROM domain_expertise
                        WHERE domain_type = :domain_type
                        ORDER BY last_used_at DESC NULLS LAST
                    """)
                    result = await session.execute(query, {"domain_type": domain_type})
                else:
                    query = text("""
                        SELECT id, domain, domain_type, title, version,
                               learnings_count, last_improved_at, last_used_at, created_at
                        FROM domain_expertise
                        ORDER BY last_used_at DESC NULLS LAST
                    """)
                    result = await session.execute(query)

                for row in result.fetchall():
                    results.append({
                        "id": row.id,
                        "domain": row.domain,
                        "domain_type": row.domain_type,
                        "title": row.title,
                        "version": row.version,
                        "learnings_count": row.learnings_count,
                        "last_improved_at": row.last_improved_at.isoformat() if row.last_improved_at else None,
                        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
                        "created_at": row.created_at.isoformat() if row.created_at else None,
                    })
            except Exception as e:
                logger.warning("Failed to list expertise", error=str(e))
            break

        return results


# Module-level singleton
_expertise_manager: ExpertiseManager | None = None


def get_expertise_manager() -> ExpertiseManager:
    """Get or create the expertise manager singleton."""
    global _expertise_manager
    if _expertise_manager is None:
        _expertise_manager = ExpertiseManager()
    return _expertise_manager
