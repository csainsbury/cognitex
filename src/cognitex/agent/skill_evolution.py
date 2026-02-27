"""
Skill Evolution System (Path 3)

Autonomous agent detects recurring patterns in its behaviour and proposes
new skills or refinements to existing ones. All proposals require operator
approval before deployment — nothing is auto-deployed.
"""

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from cognitex.agent.skills import get_skills_loader, parse_frontmatter
from cognitex.prompts import format_prompt
from cognitex.services.llm import get_llm_service

logger = structlog.get_logger()


# =============================================================================
# Constants
# =============================================================================

PROTECTED_FILES = frozenset(
    {
        "SOUL.md",
        "USER.md",
        "AGENTS.md",
        "IDENTITY.md",
        "CONTEXT.md",
        "tools.py",
        "core.py",
        "autonomous.py",
        "config.py",
    }
)

MAX_PROPOSALS_PER_CYCLE = 2
MIN_PATTERN_CONFIDENCE = 0.6
MIN_EVIDENCE_COUNT = 3

DANGEROUS_PATTERNS = frozenset(
    {
        "os.system",
        "subprocess",
        "exec(",
        "eval(",
        "__import__",
        "shutil.rmtree",
        "os.remove",
        "os.unlink",
    }
)


# =============================================================================
# Dataclasses
# =============================================================================


@dataclass
class PatternDescription:
    pattern_type: str  # repeated_rejection | action_cluster | classification_gap | topic_cluster
    description: str
    evidence: list[dict] = field(default_factory=list)
    confidence: float = 0.0


@dataclass
class SafetyCheckResult:
    is_safe: bool
    violations: list[str] = field(default_factory=list)
    modifies_protected_files: bool = False
    modifies_safety_rules: bool = False
    has_side_effects: bool = False


@dataclass
class SkillProposal:
    id: str
    pattern: PatternDescription
    skill_name: str
    skill_content: str
    status: Literal["proposed", "approved", "rejected", "deployed"] = "proposed"
    reviewer_feedback: str | None = None


@dataclass
class FeedbackEntry:
    skill_name: str
    feedback_type: Literal["correction", "missing_case", "false_positive", "suggestion"]
    description: str
    trace_id: str | None = None


@dataclass
class SkillUpdate:
    id: str
    skill_name: str
    current_content: str
    proposed_content: str
    diff_summary: str
    feedback_entries: list[FeedbackEntry] = field(default_factory=list)
    status: Literal["proposed", "approved", "rejected"] = "proposed"


@dataclass
class CodeProposal:
    id: str
    skill_name: str
    limitation: str
    proposed_file: str
    proposed_diff: str
    safety_check: SafetyCheckResult = field(default_factory=lambda: SafetyCheckResult(is_safe=True))
    status: Literal["proposed", "approved", "rejected"] = "proposed"


# =============================================================================
# DB Schema
# =============================================================================

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS skill_proposals (
    id TEXT PRIMARY KEY,
    pattern_type TEXT NOT NULL,
    pattern_description TEXT,
    pattern_evidence JSONB DEFAULT '[]',
    pattern_confidence FLOAT DEFAULT 0.0,
    skill_name TEXT NOT NULL,
    skill_content TEXT NOT NULL,
    status TEXT DEFAULT 'proposed',
    proposal_type TEXT DEFAULT 'new_skill',
    diff_summary TEXT,
    proposed_diff TEXT,
    reviewer_feedback TEXT,
    created_at TIMESTAMP DEFAULT NOW(),
    reviewed_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_skill_proposals_status ON skill_proposals(status);
CREATE INDEX IF NOT EXISTS idx_skill_proposals_created ON skill_proposals(created_at DESC);

CREATE TABLE IF NOT EXISTS skill_feedback (
    id TEXT PRIMARY KEY,
    skill_name TEXT NOT NULL,
    feedback_type TEXT NOT NULL,
    description TEXT NOT NULL,
    trace_id TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_skill_feedback_name ON skill_feedback(skill_name);
CREATE INDEX IF NOT EXISTS idx_skill_feedback_created ON skill_feedback(created_at DESC);
"""


async def ensure_schema(session: AsyncSession) -> None:
    """Initialize skill evolution tables."""
    statements = [s.strip() for s in SCHEMA_SQL.split(";") if s.strip()]
    for stmt in statements:
        try:
            await session.execute(text(stmt))
        except Exception as e:
            logger.debug("Schema statement skipped", error=str(e)[:100])
    await session.commit()
    logger.info("Skill evolution schema initialized")


# =============================================================================
# SkillEvolution
# =============================================================================


class SkillEvolution:
    """Detect patterns, propose skills, and manage the evolution lifecycle."""

    def __init__(self, get_session_func):
        self._get_session = get_session_func
        self._initialized = False
        self._llm = get_llm_service()
        self._loader = get_skills_loader()

    async def _ensure_schema(self, session: AsyncSession) -> None:
        if not self._initialized:
            await ensure_schema(session)
            self._initialized = True

    # -----------------------------------------------------------------
    # Pattern Detection
    # -----------------------------------------------------------------

    async def detect_skill_opportunity(self) -> list[PatternDescription]:
        """Run all sub-analyses and return top patterns above threshold."""
        results = await asyncio.gather(
            self._analyze_rejection_patterns(),
            self._analyze_action_clusters(),
            self._analyze_classification_gaps(),
            self._analyze_topic_clusters(),
            return_exceptions=True,
        )

        patterns = []
        for r in results:
            if isinstance(r, Exception):
                logger.warning("Pattern analysis failed", error=str(r)[:100])
                continue
            if isinstance(r, list):
                patterns.extend(r)

        # Filter by confidence threshold
        patterns = [p for p in patterns if p.confidence >= MIN_PATTERN_CONFIDENCE]
        # Sort by confidence descending, cap
        patterns.sort(key=lambda p: p.confidence, reverse=True)
        return patterns[:MAX_PROPOSALS_PER_CYCLE]

    async def _analyze_rejection_patterns(self) -> list[PatternDescription]:
        """Find action types that get repeatedly rejected."""
        patterns = []
        try:
            async for session in self._get_session():
                await self._ensure_schema(session)
                result = await session.execute(
                    text("""
                    SELECT action_type, COUNT(*) as cnt,
                           json_agg(json_build_object(
                               'id', id, 'summary', trigger_summary,
                               'reasoning', LEFT(reasoning, 200)
                           ) ORDER BY created_at DESC) as evidence
                    FROM decision_traces
                    WHERE status = 'rejected'
                      AND created_at > NOW() - INTERVAL '14 days'
                    GROUP BY action_type
                    HAVING COUNT(*) >= :min_count
                    ORDER BY cnt DESC
                    LIMIT 5
                """),
                    {"min_count": MIN_EVIDENCE_COUNT},
                )

                for row in result.mappings():
                    evidence = row["evidence"] if isinstance(row["evidence"], list) else []
                    confidence = min(1.0, row["cnt"] / 10.0)
                    patterns.append(
                        PatternDescription(
                            pattern_type="repeated_rejection",
                            description=(
                                f"Action type '{row['action_type']}' rejected "
                                f"{row['cnt']} times in the last 14 days"
                            ),
                            evidence=evidence[:5],
                            confidence=confidence,
                        )
                    )
                break
        except Exception as e:
            logger.debug("Rejection pattern analysis failed", error=str(e)[:100])
        return patterns

    async def _analyze_action_clusters(self) -> list[PatternDescription]:
        """Find repeated manual corrections in agent actions."""
        patterns = []
        try:
            async for session in self._get_session():
                await self._ensure_schema(session)
                result = await session.execute(
                    text("""
                    SELECT action_type, COUNT(*) as cnt,
                           json_agg(json_build_object(
                               'id', id, 'summary', summary,
                               'source', source
                           ) ORDER BY timestamp DESC) as evidence
                    FROM agent_actions
                    WHERE source = 'user'
                      AND timestamp > NOW() - INTERVAL '14 days'
                    GROUP BY action_type
                    HAVING COUNT(*) >= :min_count
                    ORDER BY cnt DESC
                    LIMIT 5
                """),
                    {"min_count": MIN_EVIDENCE_COUNT},
                )

                for row in result.mappings():
                    evidence = row["evidence"] if isinstance(row["evidence"], list) else []
                    confidence = min(1.0, row["cnt"] / 8.0)
                    patterns.append(
                        PatternDescription(
                            pattern_type="action_cluster",
                            description=(
                                f"User manually performed '{row['action_type']}' "
                                f"{row['cnt']} times — candidate for automation"
                            ),
                            evidence=evidence[:5],
                            confidence=confidence,
                        )
                    )
                break
        except Exception as e:
            logger.debug("Action cluster analysis failed", error=str(e)[:100])
        return patterns

    async def _analyze_classification_gaps(self) -> list[PatternDescription]:
        """Find decision traces with low quality scores."""
        patterns = []
        try:
            async for session in self._get_session():
                await self._ensure_schema(session)
                result = await session.execute(
                    text("""
                    SELECT action_type, COUNT(*) as cnt,
                           AVG(quality_score) as avg_score,
                           json_agg(json_build_object(
                               'id', id, 'summary', trigger_summary,
                               'score', quality_score
                           ) ORDER BY quality_score ASC) as evidence
                    FROM decision_traces
                    WHERE quality_score IS NOT NULL
                      AND quality_score < 0.4
                      AND created_at > NOW() - INTERVAL '14 days'
                    GROUP BY action_type
                    HAVING COUNT(*) >= :min_count
                    ORDER BY avg_score ASC
                    LIMIT 5
                """),
                    {"min_count": MIN_EVIDENCE_COUNT},
                )

                for row in result.mappings():
                    evidence = row["evidence"] if isinstance(row["evidence"], list) else []
                    confidence = min(1.0, (0.5 - (row["avg_score"] or 0)) * 2 + 0.3)
                    patterns.append(
                        PatternDescription(
                            pattern_type="classification_gap",
                            description=(
                                f"Action type '{row['action_type']}' has average quality "
                                f"score {row['avg_score']:.2f} — may need a skill to improve"
                            ),
                            evidence=evidence[:5],
                            confidence=max(0.0, min(1.0, confidence)),
                        )
                    )
                break
        except Exception as e:
            logger.debug("Classification gap analysis failed", error=str(e)[:100])
        return patterns

    async def _analyze_topic_clusters(self) -> list[PatternDescription]:
        """Find new Topic nodes with high connectivity in the graph."""
        patterns = []
        try:
            from cognitex.db.neo4j import get_neo4j_session

            async for session in get_neo4j_session():
                result = await session.run("""
                    MATCH (t:Topic)
                    WHERE t.created_at > datetime() - duration('P14D')
                    WITH t, size((t)--()) AS connections
                    WHERE connections >= 5
                    RETURN t.name AS name, connections
                    ORDER BY connections DESC
                    LIMIT 5
                """)
                records = await result.data()

                if len(records) >= MIN_EVIDENCE_COUNT:
                    evidence = [
                        {"name": r["name"], "connections": r["connections"]} for r in records
                    ]
                    confidence = min(1.0, len(records) / 8.0)
                    patterns.append(
                        PatternDescription(
                            pattern_type="topic_cluster",
                            description=(
                                f"Emerging topic cluster: {', '.join(r['name'] for r in records[:3])} "
                                f"with high graph connectivity"
                            ),
                            evidence=evidence,
                            confidence=confidence,
                        )
                    )
                break
        except Exception as e:
            logger.debug("Topic cluster analysis failed", error=str(e)[:100])
        return patterns

    # -----------------------------------------------------------------
    # Skill Proposal
    # -----------------------------------------------------------------

    async def propose_new_skill(self, pattern: PatternDescription) -> SkillProposal:
        """Generate a new skill from a detected pattern. Never auto-deploys."""
        # Gather existing skill names to avoid overlap
        all_skills = await self._loader.list_skills()
        existing_names = ", ".join(s["name"] for s in all_skills) if all_skills else "(none)"

        evidence_text = ""
        for i, ev in enumerate(pattern.evidence[:5], 1):
            evidence_text += f"\n{i}. {json.dumps(ev, default=str)[:300]}"

        prompt = format_prompt(
            "skill_proposal",
            pattern_type=pattern.pattern_type,
            pattern_description=pattern.description,
            pattern_confidence=f"{pattern.confidence:.2f}",
            evidence_text=evidence_text or "(no evidence details)",
            existing_skills=existing_names,
        )

        response = await self._llm.complete(
            prompt=prompt,
            max_tokens=4096,
            temperature=0.4,
        )
        content = response.strip()
        if content.startswith("```"):
            first_nl = content.find("\n")
            if first_nl != -1:
                content = content[first_nl + 1 :]
            content = content.rsplit("```", 1)[0].strip()

        # Extract name from frontmatter
        fm, _ = parse_frontmatter(content)
        skill_name = fm.get("name", f"auto-{pattern.pattern_type}")

        proposal_id = f"proposal_{uuid.uuid4().hex[:12]}"

        proposal = SkillProposal(
            id=proposal_id,
            pattern=pattern,
            skill_name=skill_name,
            skill_content=content,
            status="proposed",
        )

        # Persist to DB
        async for session in self._get_session():
            await self._ensure_schema(session)
            await session.execute(
                text("""
                INSERT INTO skill_proposals
                    (id, pattern_type, pattern_description, pattern_evidence,
                     pattern_confidence, skill_name, skill_content, status, proposal_type)
                VALUES
                    (:id, :pattern_type, :pattern_description, :pattern_evidence,
                     :pattern_confidence, :skill_name, :skill_content, 'proposed', 'new_skill')
            """),
                {
                    "id": proposal_id,
                    "pattern_type": pattern.pattern_type,
                    "pattern_description": pattern.description,
                    "pattern_evidence": json.dumps(pattern.evidence, default=str),
                    "pattern_confidence": pattern.confidence,
                    "skill_name": skill_name,
                    "skill_content": content,
                },
            )
            await session.commit()
            break

        logger.info(
            "Skill proposed",
            proposal_id=proposal_id,
            skill_name=skill_name,
            pattern_type=pattern.pattern_type,
        )
        return proposal

    # -----------------------------------------------------------------
    # Skill Refinement
    # -----------------------------------------------------------------

    async def refine_skill(
        self,
        skill_name: str,
        feedback_entries: list[FeedbackEntry],
    ) -> SkillUpdate:
        """Refine an existing skill based on accumulated feedback."""
        skill = await self._loader.get_skill(skill_name)
        if not skill:
            raise ValueError(f"Skill '{skill_name}' not found")

        current_content = skill.raw_content

        feedback_text = "\n".join(
            f"- [{f.feedback_type}] {f.description}" for f in feedback_entries
        )

        prompt = format_prompt(
            "skill_refine",
            current_content=current_content,
            feedback=feedback_text,
        )

        response = await self._llm.complete(
            prompt=prompt,
            max_tokens=4096,
            temperature=0.3,
        )
        proposed_content = response.strip()
        if proposed_content.startswith("```"):
            first_nl = proposed_content.find("\n")
            if first_nl != -1:
                proposed_content = proposed_content[first_nl + 1 :]
            proposed_content = proposed_content.rsplit("```", 1)[0].strip()

        # Generate diff summary
        diff_summary = self._generate_diff_summary(current_content, proposed_content)

        update_id = f"update_{uuid.uuid4().hex[:12]}"

        update = SkillUpdate(
            id=update_id,
            skill_name=skill_name,
            current_content=current_content,
            proposed_content=proposed_content,
            diff_summary=diff_summary,
            feedback_entries=feedback_entries,
        )

        # Persist
        async for session in self._get_session():
            await self._ensure_schema(session)
            await session.execute(
                text("""
                INSERT INTO skill_proposals
                    (id, pattern_type, pattern_description, skill_name,
                     skill_content, status, proposal_type, diff_summary)
                VALUES
                    (:id, 'feedback_refinement', :description, :skill_name,
                     :skill_content, 'proposed', 'update', :diff_summary)
            """),
                {
                    "id": update_id,
                    "description": f"Refinement from {len(feedback_entries)} feedback entries",
                    "skill_name": skill_name,
                    "skill_content": proposed_content,
                    "diff_summary": diff_summary,
                },
            )
            await session.commit()
            break

        logger.info("Skill update proposed", update_id=update_id, skill_name=skill_name)
        return update

    def _generate_diff_summary(self, old: str, new: str) -> str:
        """Generate a brief summary of changes between two versions."""
        old_lines = set(old.strip().splitlines())
        new_lines = set(new.strip().splitlines())
        added = len(new_lines - old_lines)
        removed = len(old_lines - new_lines)
        return f"+{added} lines, -{removed} lines"

    # -----------------------------------------------------------------
    # Code Proposals (most restricted)
    # -----------------------------------------------------------------

    async def generate_code_proposal(
        self,
        skill_name: str,
        limitation: str,
    ) -> CodeProposal:
        """Propose code changes for a skill's limitations. Never auto-executes."""
        prompt = (
            f"A skill '{skill_name}' has the following limitation:\n\n"
            f"{limitation}\n\n"
            f"Suggest a minimal code change (single file, < 50 lines) to address this. "
            f"Return the file path and a unified diff. Do not modify core agent files."
        )

        response = await self._llm.complete(
            prompt=prompt,
            max_tokens=2048,
            temperature=0.2,
        )

        # Extract file path and diff from response
        proposed_file = ""
        proposed_diff = response.strip()
        for line in response.strip().splitlines():
            if line.startswith("File:") or line.startswith("Path:"):
                proposed_file = line.split(":", 1)[1].strip()
                break
            if line.startswith("--- ") or line.startswith("+++ "):
                proposed_file = line.split()[-1] if len(line.split()) > 1 else ""
                break

        safety = self._check_safety(proposed_file, proposed_diff)

        proposal_id = f"code_{uuid.uuid4().hex[:12]}"

        return CodeProposal(
            id=proposal_id,
            skill_name=skill_name,
            limitation=limitation,
            proposed_file=proposed_file,
            proposed_diff=proposed_diff,
            safety_check=safety,
        )

    def _check_safety(self, proposed_file: str, diff: str) -> SafetyCheckResult:
        """Check whether a code proposal is safe."""
        violations = []
        modifies_protected = False
        modifies_safety = False
        has_side_effects = False

        # Check protected files
        for protected in PROTECTED_FILES:
            if protected in proposed_file:
                violations.append(f"Modifies protected file: {protected}")
                modifies_protected = True

        # Check for safety-rule modifications
        safety_keywords = ["risk_level", "APPROVAL", "approval_required", "require_approval"]
        for keyword in safety_keywords:
            if keyword in diff:
                violations.append(f"Modifies safety-related code: {keyword}")
                modifies_safety = True

        # Check for dangerous patterns
        for pattern in DANGEROUS_PATTERNS:
            if pattern in diff:
                violations.append(f"Contains dangerous pattern: {pattern}")
                has_side_effects = True

        return SafetyCheckResult(
            is_safe=len(violations) == 0,
            violations=violations,
            modifies_protected_files=modifies_protected,
            modifies_safety_rules=modifies_safety,
            has_side_effects=has_side_effects,
        )

    # -----------------------------------------------------------------
    # Evolution Cycle (entry point)
    # -----------------------------------------------------------------

    async def run_evolution_cycle(self) -> list[SkillProposal | SkillUpdate]:
        """Run a full evolution cycle: refine from feedback, then detect new patterns."""
        results: list[SkillProposal | SkillUpdate] = []

        # Phase 1: Refine skills with accumulated feedback
        try:
            refinements = await self._process_pending_feedback()
            results.extend(refinements)
        except Exception as e:
            logger.warning("Feedback processing failed", error=str(e)[:100])

        # Phase 2: Detect new patterns and propose skills
        remaining = MAX_PROPOSALS_PER_CYCLE - len(results)
        if remaining > 0:
            try:
                patterns = await self.detect_skill_opportunity()
                for pattern in patterns[:remaining]:
                    proposal = await self.propose_new_skill(pattern)
                    results.append(proposal)
            except Exception as e:
                logger.warning("Pattern detection failed", error=str(e)[:100])

        logger.info("Evolution cycle completed", proposals=len(results))
        return results

    async def _process_pending_feedback(self) -> list[SkillUpdate]:
        """Find skills with 3+ feedback entries and propose refinements."""
        updates = []
        try:
            async for session in self._get_session():
                await self._ensure_schema(session)
                result = await session.execute(
                    text("""
                    SELECT skill_name, COUNT(*) as cnt
                    FROM skill_feedback
                    WHERE created_at > NOW() - INTERVAL '30 days'
                    GROUP BY skill_name
                    HAVING COUNT(*) >= :min_count
                    ORDER BY cnt DESC
                    LIMIT :max_limit
                """),
                    {"min_count": MIN_EVIDENCE_COUNT, "max_limit": MAX_PROPOSALS_PER_CYCLE},
                )

                skill_names = [row["skill_name"] for row in result.mappings()]
                break

            for skill_name in skill_names:
                async for session in self._get_session():
                    fb_result = await session.execute(
                        text("""
                        SELECT id, skill_name, feedback_type, description, trace_id
                        FROM skill_feedback
                        WHERE skill_name = :name
                          AND created_at > NOW() - INTERVAL '30 days'
                        ORDER BY created_at DESC
                        LIMIT 10
                    """),
                        {"name": skill_name},
                    )

                    entries = [
                        FeedbackEntry(
                            skill_name=row["skill_name"],
                            feedback_type=row["feedback_type"],
                            description=row["description"],
                            trace_id=row["trace_id"],
                        )
                        for row in fb_result.mappings()
                    ]
                    break

                if entries:
                    update = await self.refine_skill(skill_name, entries)
                    updates.append(update)

        except Exception as e:
            logger.warning("Pending feedback processing failed", error=str(e)[:100])
        return updates

    # -----------------------------------------------------------------
    # Review & Deploy
    # -----------------------------------------------------------------

    async def review_proposal(
        self,
        proposal_id: str,
        decision: Literal["approved", "rejected"],
        feedback: str | None = None,
    ) -> bool:
        """Review a pending proposal."""
        async for session in self._get_session():
            await self._ensure_schema(session)
            result = await session.execute(
                text("""
                UPDATE skill_proposals
                SET status = :status,
                    reviewer_feedback = :feedback,
                    reviewed_at = NOW()
                WHERE id = :id AND status = 'proposed'
            """),
                {
                    "id": proposal_id,
                    "status": decision,
                    "feedback": feedback,
                },
            )
            await session.commit()
            updated = result.rowcount > 0
            break

        if updated:
            logger.info("Proposal reviewed", id=proposal_id, decision=decision)
        return updated

    async def deploy_proposal(self, proposal_id: str) -> bool:
        """Deploy an approved proposal by saving the skill."""
        async for session in self._get_session():
            await self._ensure_schema(session)
            result = await session.execute(
                text("""
                SELECT skill_name, skill_content, status
                FROM skill_proposals
                WHERE id = :id
            """),
                {"id": proposal_id},
            )
            row = result.mappings().first()
            break

        if not row:
            logger.warning("Proposal not found", id=proposal_id)
            return False

        if row["status"] != "approved":
            logger.warning("Proposal not approved", id=proposal_id, status=row["status"])
            return False

        success = await self._loader.save_skill(row["skill_name"], row["skill_content"])
        if success:
            async for session in self._get_session():
                await session.execute(
                    text("""
                    UPDATE skill_proposals SET status = 'deployed' WHERE id = :id
                """),
                    {"id": proposal_id},
                )
                await session.commit()
                break

            try:
                from cognitex.agent.action_log import log_action

                await log_action(
                    "skill_evolution_deployed",
                    "agent",
                    summary=f"Deployed evolved skill '{row['skill_name']}'",
                    details={"proposal_id": proposal_id},
                )
            except Exception:
                pass

            logger.info("Proposal deployed", id=proposal_id, skill=row["skill_name"])
        return success

    # -----------------------------------------------------------------
    # Queries
    # -----------------------------------------------------------------

    async def _get_pending_proposals(self) -> list[dict[str, Any]]:
        """Get all pending proposals."""
        async for session in self._get_session():
            await self._ensure_schema(session)
            result = await session.execute(
                text("""
                SELECT id, pattern_type, pattern_description, pattern_confidence,
                       skill_name, skill_content, status, proposal_type,
                       diff_summary, created_at
                FROM skill_proposals
                WHERE status = 'proposed'
                ORDER BY created_at DESC
            """)
            )
            rows = [dict(r) for r in result.mappings()]
            break
        return rows

    async def get_feedback_summary(self) -> list[dict]:
        """Get feedback counts per skill for the dashboard (last 30 days)."""
        rows: list[dict] = []
        try:
            async for session in self._get_session():
                await self._ensure_schema(session)
                result = await session.execute(
                    text("""
                    SELECT skill_name, feedback_type, COUNT(*) as count
                    FROM skill_feedback
                    WHERE created_at > NOW() - INTERVAL '30 days'
                    GROUP BY skill_name, feedback_type
                    ORDER BY skill_name, count DESC
                """)
                )
                rows = [dict(r) for r in result.mappings()]
                break
        except Exception as e:
            logger.warning("Failed to get feedback summary", error=str(e)[:100])
        return rows

    async def _get_all_proposals(self, limit: int = 50) -> list[dict[str, Any]]:
        """Get all proposals for history view."""
        async for session in self._get_session():
            await self._ensure_schema(session)
            result = await session.execute(
                text("""
                SELECT id, pattern_type, pattern_description, pattern_confidence,
                       skill_name, skill_content, status, proposal_type,
                       diff_summary, reviewer_feedback, created_at, reviewed_at
                FROM skill_proposals
                ORDER BY created_at DESC
                LIMIT :limit
            """),
                {"limit": limit},
            )
            rows = [dict(r) for r in result.mappings()]
            break
        return rows

    async def add_feedback(self, entry: FeedbackEntry) -> str:
        """Record feedback about a skill."""
        feedback_id = f"fb_{uuid.uuid4().hex[:12]}"
        async for session in self._get_session():
            await self._ensure_schema(session)
            await session.execute(
                text("""
                INSERT INTO skill_feedback (id, skill_name, feedback_type, description, trace_id)
                VALUES (:id, :skill_name, :feedback_type, :description, :trace_id)
            """),
                {
                    "id": feedback_id,
                    "skill_name": entry.skill_name,
                    "feedback_type": entry.feedback_type,
                    "description": entry.description,
                    "trace_id": entry.trace_id,
                },
            )
            await session.commit()
            break
        return feedback_id


# =============================================================================
# Singleton
# =============================================================================

_skill_evolution: SkillEvolution | None = None


def get_skill_evolution() -> SkillEvolution:
    """Get or create the SkillEvolution singleton."""
    global _skill_evolution
    if _skill_evolution is None:
        from cognitex.db.postgres import get_session

        _skill_evolution = SkillEvolution(get_session)
    return _skill_evolution
