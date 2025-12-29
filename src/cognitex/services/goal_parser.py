"""Goal parser - extracts structured data from goal descriptions using LLM.

Parses natural language goal descriptions and creates appropriate graph connections:
- Identifies related people (stakeholders, owners, collaborators)
- Extracts projects that contribute to the goal
- Identifies tasks/milestones
- Detects timeframes and deadlines
- Finds related domains/themes
"""

import json
import re
from dataclasses import dataclass, field

import structlog

from cognitex.config import get_settings

logger = structlog.get_logger()


@dataclass
class ParsedGoal:
    """Structured representation of a parsed goal."""

    title: str
    description: str | None = None
    timeframe: str | None = None  # quarterly, yearly, multi_year
    target_date: str | None = None

    # Related entities to create/link
    projects: list[dict] = field(default_factory=list)  # [{title, description}]
    tasks: list[dict] = field(default_factory=list)  # [{title, priority}]
    people: list[dict] = field(default_factory=list)  # [{email_hint, name, role}]
    themes: list[str] = field(default_factory=list)  # e.g., ["health", "AI", "revenue"]

    # Metrics/success criteria
    success_criteria: list[str] = field(default_factory=list)

    # Raw extraction confidence
    confidence: float = 0.0


EXTRACTION_PROMPT = """You are an expert at parsing goal descriptions and extracting structured information.

Given a goal description, extract:
1. A clear, concise title (if not obvious from the description)
2. The timeframe (quarterly, yearly, or multi_year based on scope)
3. Any target dates mentioned
4. Projects that would contribute to this goal
5. Specific tasks or milestones
6. People mentioned (with their role: owner, stakeholder, collaborator)
7. Themes or domains (e.g., health, technology, finance, personal)
8. Success criteria or metrics

Respond with JSON only:
```json
{
  "title": "Clear goal title",
  "description": "Expanded description if needed",
  "timeframe": "quarterly|yearly|multi_year",
  "target_date": "YYYY-MM-DD or null",
  "projects": [
    {"title": "Project name", "description": "Brief description"}
  ],
  "tasks": [
    {"title": "Task name", "priority": "high|medium|low"}
  ],
  "people": [
    {"name": "Person name", "email_hint": "email if mentioned or null", "role": "owner|stakeholder|collaborator"}
  ],
  "themes": ["theme1", "theme2"],
  "success_criteria": ["Metric 1", "Metric 2"],
  "confidence": 0.85
}
```

Be conservative - only extract what is clearly stated or strongly implied.
If the goal is simple, return minimal structure.
"""


class GoalParser:
    """Parses goal descriptions into structured data using LLM (multi-provider)."""

    def __init__(self):
        settings = get_settings()
        self._provider = settings.llm_provider

        # Initialize the appropriate client based on provider
        if self._provider == "google":
            import google.generativeai as genai
            api_key = settings.google_ai_api_key.get_secret_value()
            if not api_key:
                raise ValueError("GOOGLE_AI_API_KEY not configured")
            genai.configure(api_key=api_key)
            self._client = genai
            self._model = settings.google_model_executor
            logger.debug("GoalParser using Google Gemini", model=self._model)

        elif self._provider == "anthropic":
            from anthropic import Anthropic
            api_key = settings.anthropic_api_key.get_secret_value()
            if not api_key:
                raise ValueError("ANTHROPIC_API_KEY not configured")
            self._client = Anthropic(api_key=api_key)
            self._model = settings.anthropic_model_executor
            logger.debug("GoalParser using Anthropic Claude", model=self._model)

        elif self._provider == "openai":
            from openai import OpenAI
            api_key = settings.openai_api_key.get_secret_value()
            if not api_key:
                raise ValueError("OPENAI_API_KEY not configured")
            self._client = OpenAI(api_key=api_key)
            self._model = settings.openai_model_executor
            logger.debug("GoalParser using OpenAI", model=self._model)

        else:  # together (default)
            from together import Together
            api_key = settings.together_api_key.get_secret_value()
            if not api_key:
                raise ValueError("TOGETHER_API_KEY not configured")
            self._client = Together(api_key=api_key)
            self._model = settings.together_model_executor
            self._provider = "together"
            logger.debug("GoalParser using Together.ai", model=self._model)

    async def parse(self, goal_text: str, context: str | None = None) -> ParsedGoal:
        """
        Parse a goal description into structured data.

        Args:
            goal_text: The goal description to parse
            context: Optional additional context (e.g., user's current projects)

        Returns:
            ParsedGoal with extracted structure
        """
        prompt = f"Goal description:\n{goal_text}"
        if context:
            prompt += f"\n\nAdditional context:\n{context}"

        try:
            # Route to appropriate provider API
            if self._provider == "google":
                model = self._client.GenerativeModel(self._model)
                response = model.generate_content(
                    f"{EXTRACTION_PROMPT}\n\n{prompt}",
                    generation_config={
                        "max_output_tokens": 1500,
                        "temperature": 0.2,
                    },
                )
                content = response.text.strip()

            elif self._provider == "anthropic":
                response = self._client.messages.create(
                    model=self._model,
                    max_tokens=1500,
                    system=EXTRACTION_PROMPT,
                    messages=[{"role": "user", "content": prompt}],
                )
                content = response.content[0].text.strip()

            else:  # openai/together
                response = self._client.chat.completions.create(
                    model=self._model,
                    messages=[
                        {"role": "system", "content": EXTRACTION_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    max_tokens=1500,
                    temperature=0.2,
                )
                content = response.choices[0].message.content.strip()

            # Extract JSON from response
            parsed = self._extract_json(content)
            if not parsed:
                logger.warning("Failed to parse goal extraction response", content=content[:200])
                return ParsedGoal(title=goal_text[:100], description=goal_text)

            return ParsedGoal(
                title=parsed.get("title", goal_text[:100]),
                description=parsed.get("description"),
                timeframe=parsed.get("timeframe"),
                target_date=parsed.get("target_date"),
                projects=parsed.get("projects", []),
                tasks=parsed.get("tasks", []),
                people=parsed.get("people", []),
                themes=parsed.get("themes", []),
                success_criteria=parsed.get("success_criteria", []),
                confidence=parsed.get("confidence", 0.5),
            )

        except Exception as e:
            logger.error("Goal parsing failed", error=str(e))
            return ParsedGoal(title=goal_text[:100], description=goal_text)

    def _extract_json(self, content: str) -> dict | None:
        """Extract JSON from LLM response."""
        # Try to find JSON block
        json_match = re.search(r"```json\s*(.*?)\s*```", content, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group(1))
            except json.JSONDecodeError:
                pass

        # Try direct JSON parse
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            pass

        # Try to find JSON object in content
        json_match = re.search(r"\{.*\}", content, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group(0))
            except json.JSONDecodeError:
                pass

        return None


async def parse_and_create_goal(
    goal_text: str,
    create_projects: bool = True,
    create_tasks: bool = True,
    link_people: bool = True,
    dry_run: bool = False,
) -> dict:
    """
    Parse a goal description and create all related graph entities.

    Args:
        goal_text: Natural language goal description
        create_projects: Whether to create extracted projects
        create_tasks: Whether to create extracted tasks
        link_people: Whether to link mentioned people
        dry_run: If True, return what would be created without creating

    Returns:
        Dict with created goal and related entities
    """
    from cognitex.db.neo4j import get_neo4j_session
    from cognitex.db.graph_schema import (
        link_goal_to_person,
        link_project_to_goal,
        link_task_to_goal,
    )
    from cognitex.services.tasks import get_goal_service, get_project_service, get_task_service

    parser = GoalParser()
    parsed = await parser.parse(goal_text)

    result = {
        "parsed": {
            "title": parsed.title,
            "description": parsed.description,
            "timeframe": parsed.timeframe,
            "target_date": parsed.target_date,
            "projects": parsed.projects,
            "tasks": parsed.tasks,
            "people": parsed.people,
            "themes": parsed.themes,
            "success_criteria": parsed.success_criteria,
            "confidence": parsed.confidence,
        },
        "created": {
            "goal": None,
            "projects": [],
            "tasks": [],
            "people_linked": [],
        },
    }

    if dry_run:
        return result

    # Create the goal
    goal_service = get_goal_service()
    goal = await goal_service.create(
        title=parsed.title,
        description=parsed.description or goal_text,
        timeframe=parsed.timeframe,
    )
    result["created"]["goal"] = goal

    # Create projects
    if create_projects and parsed.projects:
        project_service = get_project_service()
        for proj_data in parsed.projects:
            try:
                project = await project_service.create(
                    title=proj_data["title"],
                    description=proj_data.get("description"),
                    status="planning",
                    goal_id=goal["id"],
                )
                result["created"]["projects"].append(project)
            except Exception as e:
                logger.warning("Failed to create project", title=proj_data["title"], error=str(e))

    # Create tasks
    if create_tasks and parsed.tasks:
        task_service = get_task_service()
        for task_data in parsed.tasks:
            try:
                task = await task_service.create(
                    title=task_data["title"],
                    priority=task_data.get("priority", "medium"),
                    goal_id=goal["id"],
                )
                result["created"]["tasks"].append(task)
            except Exception as e:
                logger.warning("Failed to create task", title=task_data["title"], error=str(e))

    # Link people (try to match by email hint or name)
    if link_people and parsed.people:
        async for session in get_neo4j_session():
            for person_data in parsed.people:
                try:
                    # Try to find person by email hint or name
                    email = person_data.get("email_hint")
                    name = person_data.get("name")
                    role = person_data.get("role", "stakeholder")

                    if email:
                        # Direct email match
                        query = "MATCH (p:Person {email: $email}) RETURN p.email as email LIMIT 1"
                        r = await session.run(query, {"email": email})
                        rec = await r.single()
                        if rec:
                            await link_goal_to_person(session, goal["id"], rec["email"], role)
                            result["created"]["people_linked"].append({"email": rec["email"], "role": role})
                    elif name:
                        # Fuzzy name match
                        query = """
                        MATCH (p:Person)
                        WHERE toLower(p.name) CONTAINS toLower($name)
                           OR toLower(p.email) CONTAINS toLower($name)
                        RETURN p.email as email, p.name as name
                        LIMIT 1
                        """
                        r = await session.run(query, {"name": name})
                        rec = await r.single()
                        if rec:
                            await link_goal_to_person(session, goal["id"], rec["email"], role)
                            result["created"]["people_linked"].append({
                                "email": rec["email"],
                                "name": rec["name"],
                                "role": role,
                            })

                except Exception as e:
                    logger.warning("Failed to link person", person=person_data, error=str(e))
            break

    return result
