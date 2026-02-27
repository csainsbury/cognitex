"""
Skill Authoring System (Path 2)

Operator describes a skill in natural language and the LLM generates it.
Supports create → refine → test → deploy workflow.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

import structlog

from cognitex.agent.skills import get_skills_loader, parse_frontmatter
from cognitex.prompts import format_prompt
from cognitex.services.llm import get_llm_service

logger = structlog.get_logger()


@dataclass
class SkillTestResult:
    input_text: str
    output_text: str
    success: bool
    error: str | None = None


@dataclass
class SkillDraft:
    name: str
    description: str
    content: str
    version: int = 1
    created_at: datetime = field(default_factory=datetime.now)
    status: Literal["draft", "tested", "deployed"] = "draft"
    test_results: list[SkillTestResult] = field(default_factory=list)
    feedback_history: list[str] = field(default_factory=list)


def _slugify(text: str) -> str:
    """Convert text to a lowercase-hyphen slug suitable for skill names."""
    slug = text.lower().strip()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug[:50]


def _strip_code_fences(text: str) -> str:
    """Remove markdown code fences if the LLM wrapped the output."""
    text = text.strip()
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl + 1 :]
        text = text.rsplit("```", 1)[0]
    return text.strip()


class SkillAuthoring:
    """Create, refine, test, and deploy skills from natural language descriptions."""

    def __init__(self):
        self._llm = get_llm_service()
        self._loader = get_skills_loader()

    async def create_from_description(
        self,
        description: str,
        name: str | None = None,
        examples: list[str] | None = None,
    ) -> SkillDraft:
        """Generate a skill from a natural language description.

        Args:
            description: What the skill should do
            name: Optional skill name (auto-derived if not given)
            examples: Optional input/output examples to guide generation

        Returns:
            A SkillDraft with the generated content
        """
        # Load reference skill
        reference_skill = ""
        ref = await self._loader.get_skill("email-tasks")
        if ref:
            reference_skill = ref.raw_content

        # Build examples section
        examples_section = ""
        if examples:
            examples_section = "**Additional examples to incorporate:**\n"
            for i, ex in enumerate(examples, 1):
                examples_section += f"\n{i}. {ex}"

        # Derive name
        skill_name = name or _slugify(description[:50])

        prompt = format_prompt(
            "skill_authoring",
            reference_skill=reference_skill,
            description=description,
            examples_section=examples_section,
            skill_name=skill_name,
        )

        response = await self._llm.complete(
            prompt=prompt,
            max_tokens=4096,
            temperature=0.4,
            task="skill_evolution",
        )
        content = _strip_code_fences(response)

        # Try to extract name from frontmatter if we auto-generated
        if not name:
            fm, _ = parse_frontmatter(content)
            if fm.get("name"):
                skill_name = fm["name"]

        # Extract description from frontmatter
        fm, _ = parse_frontmatter(content)
        skill_description = fm.get("description", description[:100])

        logger.info("Skill draft created", name=skill_name, description=skill_description[:60])

        return SkillDraft(
            name=skill_name,
            description=skill_description,
            content=content,
        )

    async def refine_draft(self, draft: SkillDraft, feedback: str) -> SkillDraft:
        """Refine an existing draft with feedback.

        Args:
            draft: The current draft to refine
            feedback: Change requests or corrections

        Returns:
            Updated SkillDraft with incremented version
        """
        prompt = format_prompt(
            "skill_refine",
            current_content=draft.content,
            feedback=feedback,
        )

        response = await self._llm.complete(
            prompt=prompt,
            max_tokens=4096,
            temperature=0.3,
            task="skill_evolution",
        )
        content = _strip_code_fences(response)

        fm, _ = parse_frontmatter(content)
        description = fm.get("description", draft.description)

        new_draft = SkillDraft(
            name=draft.name,
            description=description,
            content=content,
            version=draft.version + 1,
            created_at=draft.created_at,
            status="draft",
            test_results=[],
            feedback_history=[*draft.feedback_history, feedback],
        )

        logger.info("Skill draft refined", name=draft.name, version=new_draft.version)
        return new_draft

    async def test_skill(
        self,
        draft: SkillDraft,
        test_inputs: list[str],
    ) -> list[SkillTestResult]:
        """Test a skill draft against sample inputs.

        Args:
            draft: The skill draft to test
            test_inputs: Sample inputs to run through the skill

        Returns:
            List of test results
        """
        # Validate that the content parses
        fm, body = parse_frontmatter(draft.content)
        if not fm and not body.strip():
            return [
                SkillTestResult(
                    input_text="<frontmatter parse>",
                    output_text="",
                    success=False,
                    error="Skill content is empty or could not be parsed",
                )
            ]

        results = []
        for input_text in test_inputs:
            try:
                prompt = (
                    f"You are applying the following skill to process input.\n\n"
                    f"## Skill Definition\n\n{draft.content}\n\n"
                    f"## Input\n\n{input_text}\n\n"
                    f"## Instructions\n\n"
                    f"Apply the skill rules to the input and produce the expected output. "
                    f"Be specific and follow the skill's format."
                )

                output = await self._llm.complete(
                    prompt=prompt,
                    max_tokens=2048,
                    temperature=0.2,
                    task="skill_evolution",
                )

                results.append(
                    SkillTestResult(
                        input_text=input_text,
                        output_text=output.strip(),
                        success=bool(output.strip()),
                    )
                )
            except Exception as e:
                results.append(
                    SkillTestResult(
                        input_text=input_text,
                        output_text="",
                        success=False,
                        error=str(e),
                    )
                )

        draft.test_results = results
        if results:
            draft.status = "tested"

        logger.info(
            "Skill tested",
            name=draft.name,
            total=len(results),
            passed=sum(1 for r in results if r.success),
        )
        return results

    async def deploy_skill(self, draft: SkillDraft) -> bool:
        """Deploy a skill draft to the user skills directory.

        Args:
            draft: The skill draft to deploy

        Returns:
            True if deployment succeeded
        """
        success = await self._loader.save_skill(draft.name, draft.content)

        if success:
            draft.status = "deployed"
            try:
                from cognitex.agent.action_log import log_action

                await log_action(
                    "skill_deployed",
                    "user",
                    summary=f"Deployed skill '{draft.name}' v{draft.version}",
                    details={
                        "name": draft.name,
                        "version": draft.version,
                        "description": draft.description,
                    },
                )
            except Exception:
                pass  # Action log is optional
            logger.info("Skill deployed", name=draft.name, version=draft.version)

        return success


_skill_authoring: SkillAuthoring | None = None


def get_skill_authoring() -> SkillAuthoring:
    """Get or create the SkillAuthoring singleton."""
    global _skill_authoring
    if _skill_authoring is None:
        _skill_authoring = SkillAuthoring()
    return _skill_authoring
