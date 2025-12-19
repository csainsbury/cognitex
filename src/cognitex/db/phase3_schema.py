"""Phase 3 graph schema: Claim ledger, provenance, state model, and research entities.

This module extends the base graph_schema with P0/P1/P2 entities from the Phase 3 blueprint:
- Claim: Atomic statements with evidence grading and provenance
- LiteratureItem: Bibliographic objects with DOI/citation graph
- SpanAnchor: Immutable anchors to document locations
- StateSnapshot: User operating state (focus, overload, etc.)
- ContextPack: Pre-compiled context for events/tasks
- Draft: Writing artifacts linked to claims
- Run: Experiment/analysis registry entries
"""

from datetime import datetime
from enum import Enum
from typing import Any

import structlog
from neo4j import AsyncSession

logger = structlog.get_logger()


# =============================================================================
# Enums for Phase 3 entities
# =============================================================================

class EvidenceGrade(str, Enum):
    """GRADE-style evidence hierarchy for claims."""
    HIGH = "high"           # RCT, systematic review
    MODERATE = "moderate"   # Observational with low bias
    LOW = "low"             # Case series, expert opinion
    VERY_LOW = "very_low"   # Indirect, inconsistent
    UNGRADED = "ungraded"   # Not yet evaluated


class ClaimTier(str, Enum):
    """Epistemic risk tiers for assertions (no-source, no-say)."""
    EXACT_QUOTE = "tier_0"      # Exact quote with anchor
    PARAPHRASE = "tier_1"       # Paraphrase with anchors
    SYNTHESIS = "tier_2"        # Synthesis across multiple anchors
    HYPOTHESIS = "tier_3"       # Speculative, clearly labelled


class OperatingMode(str, Enum):
    """User operating modes for state-aware planning."""
    DEEP_FOCUS = "deep_focus"       # Protect, block interruptions, deep tasks only
    FRAGMENTED = "fragmented"       # Short tasks, batching, context packs
    OVERLOADED = "overloaded"       # Reduce inputs, maintenance + recovery only
    AVOIDANT = "avoidant"           # Micro-commitments, prep tasks, external prompts
    HYPERFOCUS = "hyperfocus"       # Hard stop rails, hydration prompts, time boxing
    TRANSITION = "transition"       # Between states, settling


class DraftType(str, Enum):
    """Types of writing artifacts."""
    GRANT_SECTION = "grant_section"
    PAPER_METHODS = "paper_methods"
    PAPER_RESULTS = "paper_results"
    PAPER_DISCUSSION = "paper_discussion"
    ABSTRACT = "abstract"
    RESPONSE_TO_REVIEWERS = "response_to_reviewers"
    SLIDE_NOTES = "slide_notes"
    EMAIL_DRAFT = "email_draft"
    MEETING_NOTES = "meeting_notes"
    OTHER = "other"


# =============================================================================
# Schema statements for Phase 3 entities
# =============================================================================

PHASE3_SCHEMA_STATEMENTS = [
    # Core Phase 3 node constraints
    "CREATE CONSTRAINT claim_id IF NOT EXISTS FOR (c:Claim) REQUIRE c.id IS UNIQUE",
    "CREATE CONSTRAINT literature_id IF NOT EXISTS FOR (l:LiteratureItem) REQUIRE l.id IS UNIQUE",
    "CREATE CONSTRAINT literature_doi IF NOT EXISTS FOR (l:LiteratureItem) REQUIRE l.doi IS UNIQUE",
    "CREATE CONSTRAINT span_anchor_id IF NOT EXISTS FOR (s:SpanAnchor) REQUIRE s.id IS UNIQUE",
    "CREATE CONSTRAINT state_snapshot_id IF NOT EXISTS FOR (ss:StateSnapshot) REQUIRE ss.id IS UNIQUE",
    "CREATE CONSTRAINT context_pack_id IF NOT EXISTS FOR (cp:ContextPack) REQUIRE cp.id IS UNIQUE",
    "CREATE CONSTRAINT draft_id IF NOT EXISTS FOR (d:Draft) REQUIRE d.id IS UNIQUE",
    "CREATE CONSTRAINT run_id IF NOT EXISTS FOR (r:Run) REQUIRE r.id IS UNIQUE",
    "CREATE CONSTRAINT cohort_id IF NOT EXISTS FOR (c:CohortDefinition) REQUIRE c.id IS UNIQUE",
    "CREATE CONSTRAINT variable_id IF NOT EXISTS FOR (v:VariableDefinition) REQUIRE v.id IS UNIQUE",
    "CREATE CONSTRAINT reviewer_comment_id IF NOT EXISTS FOR (rc:ReviewerComment) REQUIRE rc.id IS UNIQUE",
    "CREATE CONSTRAINT decision_trace_id IF NOT EXISTS FOR (dt:DecisionTrace) REQUIRE dt.id IS UNIQUE",

    # Indexes for common queries
    "CREATE INDEX claim_scope IF NOT EXISTS FOR (c:Claim) ON (c.scope)",
    "CREATE INDEX claim_confidence IF NOT EXISTS FOR (c:Claim) ON (c.confidence)",
    "CREATE INDEX claim_evidence_grade IF NOT EXISTS FOR (c:Claim) ON (c.evidence_grade)",
    "CREATE INDEX claim_tier IF NOT EXISTS FOR (c:Claim) ON (c.tier)",
    "CREATE INDEX literature_venue IF NOT EXISTS FOR (l:LiteratureItem) ON (l.venue)",
    "CREATE INDEX literature_year IF NOT EXISTS FOR (l:LiteratureItem) ON (l.year)",
    "CREATE INDEX state_snapshot_mode IF NOT EXISTS FOR (ss:StateSnapshot) ON (ss.mode)",
    "CREATE INDEX state_snapshot_created IF NOT EXISTS FOR (ss:StateSnapshot) ON (ss.created_at)",
    "CREATE INDEX context_pack_event_id IF NOT EXISTS FOR (cp:ContextPack) ON (cp.event_id)",
    "CREATE INDEX context_pack_task_id IF NOT EXISTS FOR (cp:ContextPack) ON (cp.task_id)",
    "CREATE INDEX draft_type IF NOT EXISTS FOR (d:Draft) ON (d.type)",
    "CREATE INDEX draft_status IF NOT EXISTS FOR (d:Draft) ON (d.status)",
    "CREATE INDEX run_created IF NOT EXISTS FOR (r:Run) ON (r.created_at)",
    "CREATE INDEX run_project_id IF NOT EXISTS FOR (r:Run) ON (r.project_id)",
]


async def init_phase3_schema() -> None:
    """Initialize Phase 3 schema extensions."""
    from neo4j import WRITE_ACCESS
    from cognitex.db.neo4j import get_driver

    driver = get_driver()

    async with driver.session(default_access_mode=WRITE_ACCESS) as session:
        for statement in PHASE3_SCHEMA_STATEMENTS:
            try:
                await session.run(statement)
                logger.debug("Phase 3 schema statement executed", statement=statement[:50])
            except Exception as e:
                logger.warning("Phase 3 schema statement failed", statement=statement[:50], error=str(e))

    logger.info("Phase 3 schema initialized", constraints=len(PHASE3_SCHEMA_STATEMENTS))


# =============================================================================
# P0.1: Claim Ledger
# =============================================================================

async def create_claim(
    session: AsyncSession,
    claim_id: str,
    statement: str,
    scope: str | None = None,
    population: str | None = None,
    setting: str | None = None,
    effect_direction: str | None = None,
    effect_size: str | None = None,
    confidence: float = 0.5,
    evidence_grade: str = EvidenceGrade.UNGRADED.value,
    tier: str = ClaimTier.HYPOTHESIS.value,
) -> dict:
    """Create a Claim node in the claim ledger.

    Args:
        claim_id: Unique identifier
        statement: The atomic claim statement
        scope: Domain/context scope
        population: Target population (if applicable)
        setting: Clinical/research setting
        effect_direction: positive, negative, null, mixed
        effect_size: Quantified effect if available
        confidence: 0-1 confidence score
        evidence_grade: GRADE-style evidence quality
        tier: Epistemic tier (quote, paraphrase, synthesis, hypothesis)
    """
    query = """
    CREATE (c:Claim {
        id: $claim_id,
        statement: $statement,
        scope: $scope,
        population: $population,
        setting: $setting,
        effect_direction: $effect_direction,
        effect_size: $effect_size,
        confidence: $confidence,
        evidence_grade: $evidence_grade,
        tier: $tier,
        created_at: datetime(),
        updated_at: datetime()
    })
    RETURN c
    """
    result = await session.run(
        query,
        claim_id=claim_id,
        statement=statement,
        scope=scope,
        population=population,
        setting=setting,
        effect_direction=effect_direction,
        effect_size=effect_size,
        confidence=confidence,
        evidence_grade=evidence_grade,
        tier=tier,
    )
    record = await result.single()
    return dict(record["c"]) if record else {}


async def link_claim_supported_by(
    session: AsyncSession,
    claim_id: str,
    span_anchor_id: str,
    strength: float = 1.0,
) -> bool:
    """Link a claim to supporting evidence via span anchor."""
    query = """
    MATCH (c:Claim {id: $claim_id})
    MATCH (s:SpanAnchor {id: $span_anchor_id})
    MERGE (c)-[r:SUPPORTED_BY]->(s)
    SET r.strength = $strength, r.created_at = datetime()
    RETURN c, s
    """
    result = await session.run(query, claim_id=claim_id, span_anchor_id=span_anchor_id, strength=strength)
    record = await result.single()
    return record is not None


async def link_claim_contradicts(
    session: AsyncSession,
    claim_id: str,
    contradicting_claim_id: str,
    note: str | None = None,
) -> bool:
    """Link two contradicting claims."""
    query = """
    MATCH (c1:Claim {id: $claim_id})
    MATCH (c2:Claim {id: $contradicting_claim_id})
    MERGE (c1)-[r:CONTRADICTS]->(c2)
    SET r.note = $note, r.created_at = datetime()
    RETURN c1, c2
    """
    result = await session.run(query, claim_id=claim_id, contradicting_claim_id=contradicting_claim_id, note=note)
    record = await result.single()
    return record is not None


async def link_claim_used_in(
    session: AsyncSession,
    claim_id: str,
    draft_id: str,
    paragraph_index: int | None = None,
) -> bool:
    """Link a claim to a draft where it's used."""
    query = """
    MATCH (c:Claim {id: $claim_id})
    MATCH (d:Draft {id: $draft_id})
    MERGE (c)-[r:USED_IN]->(d)
    SET r.paragraph_index = $paragraph_index, r.created_at = datetime()
    RETURN c, d
    """
    result = await session.run(query, claim_id=claim_id, draft_id=draft_id, paragraph_index=paragraph_index)
    record = await result.single()
    return record is not None


async def get_claim(session: AsyncSession, claim_id: str) -> dict | None:
    """Get a claim with all its relationships."""
    query = """
    MATCH (c:Claim {id: $claim_id})
    OPTIONAL MATCH (c)-[:SUPPORTED_BY]->(s:SpanAnchor)
    OPTIONAL MATCH (c)-[:CONTRADICTS]-(other:Claim)
    OPTIONAL MATCH (c)-[:USED_IN]->(d:Draft)
    RETURN c {
        .*,
        created_at: toString(c.created_at),
        updated_at: toString(c.updated_at),
        supports: collect(DISTINCT s.id),
        contradictions: collect(DISTINCT other.id),
        used_in_drafts: collect(DISTINCT d.id)
    } as claim
    """
    result = await session.run(query, claim_id=claim_id)
    record = await result.single()
    return record["claim"] if record else None


async def get_claims(
    session: AsyncSession,
    scope: str | None = None,
    evidence_grade: str | None = None,
    tier: str | None = None,
    min_confidence: float | None = None,
    limit: int = 50,
) -> list[dict]:
    """List claims with filters."""
    filters = []
    params = {"limit": limit}

    if scope:
        filters.append("c.scope = $scope")
        params["scope"] = scope
    if evidence_grade:
        filters.append("c.evidence_grade = $evidence_grade")
        params["evidence_grade"] = evidence_grade
    if tier:
        filters.append("c.tier = $tier")
        params["tier"] = tier
    if min_confidence is not None:
        filters.append("c.confidence >= $min_confidence")
        params["min_confidence"] = min_confidence

    where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""

    query = f"""
    MATCH (c:Claim)
    {where_clause}
    RETURN c {{
        .*,
        created_at: toString(c.created_at),
        updated_at: toString(c.updated_at)
    }} as claim
    ORDER BY c.confidence DESC, c.created_at DESC
    LIMIT $limit
    """
    result = await session.run(query, params)
    data = await result.data()
    return [r["claim"] for r in data]


# =============================================================================
# P0.2: Span Anchors (Provenance)
# =============================================================================

async def create_span_anchor(
    session: AsyncSession,
    anchor_id: str,
    document_id: str,
    document_type: str,  # 'drive', 'literature', 'chunk'
    start_char: int | None = None,
    end_char: int | None = None,
    page: int | None = None,
    paragraph: int | None = None,
    text_content: str | None = None,
    stable_locator: str | None = None,
) -> dict:
    """Create an immutable span anchor for provenance tracking.

    Args:
        anchor_id: Unique identifier
        document_id: Source document ID (drive_id, literature_id, or chunk_id)
        document_type: Type of source document
        start_char: Character offset start
        end_char: Character offset end
        page: Page number (for PDFs)
        paragraph: Paragraph index
        text_content: Extracted text at this anchor
        stable_locator: Stable reference string (e.g., "page:7:para:2")
    """
    query = """
    CREATE (s:SpanAnchor {
        id: $anchor_id,
        document_id: $document_id,
        document_type: $document_type,
        start_char: $start_char,
        end_char: $end_char,
        page: $page,
        paragraph: $paragraph,
        text_content: $text_content,
        stable_locator: $stable_locator,
        created_at: datetime()
    })
    RETURN s
    """
    result = await session.run(
        query,
        anchor_id=anchor_id,
        document_id=document_id,
        document_type=document_type,
        start_char=start_char,
        end_char=end_char,
        page=page,
        paragraph=paragraph,
        text_content=text_content,
        stable_locator=stable_locator,
    )
    record = await result.single()
    return dict(record["s"]) if record else {}


async def link_anchor_to_document(
    session: AsyncSession,
    anchor_id: str,
    drive_id: str,
) -> bool:
    """Link a span anchor to a Drive document."""
    query = """
    MATCH (s:SpanAnchor {id: $anchor_id})
    MATCH (d:Document {drive_id: $drive_id})
    MERGE (s)-[:ANCHORED_IN]->(d)
    RETURN s, d
    """
    result = await session.run(query, anchor_id=anchor_id, drive_id=drive_id)
    record = await result.single()
    return record is not None


async def link_anchor_to_literature(
    session: AsyncSession,
    anchor_id: str,
    literature_id: str,
) -> bool:
    """Link a span anchor to a literature item."""
    query = """
    MATCH (s:SpanAnchor {id: $anchor_id})
    MATCH (l:LiteratureItem {id: $literature_id})
    MERGE (s)-[:ANCHORED_IN]->(l)
    RETURN s, l
    """
    result = await session.run(query, anchor_id=anchor_id, literature_id=literature_id)
    record = await result.single()
    return record is not None


# =============================================================================
# P0.3: Literature Items
# =============================================================================

async def create_literature_item(
    session: AsyncSession,
    item_id: str,
    title: str,
    authors: list[str] | None = None,
    year: int | None = None,
    venue: str | None = None,
    doi: str | None = None,
    arxiv_id: str | None = None,
    pubmed_id: str | None = None,
    abstract: str | None = None,
    bibtex_key: str | None = None,
    drive_id: str | None = None,
) -> dict:
    """Create a LiteratureItem node for bibliographic tracking.

    Args:
        item_id: Unique identifier
        title: Paper/article title
        authors: List of author names
        year: Publication year
        venue: Journal/conference name
        doi: DOI identifier
        arxiv_id: arXiv identifier
        pubmed_id: PubMed identifier
        abstract: Paper abstract
        bibtex_key: BibTeX citation key
        drive_id: Linked Drive document if available
    """
    query = """
    CREATE (l:LiteratureItem {
        id: $item_id,
        title: $title,
        authors: $authors,
        year: $year,
        venue: $venue,
        doi: $doi,
        arxiv_id: $arxiv_id,
        pubmed_id: $pubmed_id,
        abstract: $abstract,
        bibtex_key: $bibtex_key,
        drive_id: $drive_id,
        created_at: datetime(),
        updated_at: datetime()
    })
    RETURN l
    """
    result = await session.run(
        query,
        item_id=item_id,
        title=title,
        authors=authors or [],
        year=year,
        venue=venue,
        doi=doi,
        arxiv_id=arxiv_id,
        pubmed_id=pubmed_id,
        abstract=abstract,
        bibtex_key=bibtex_key,
        drive_id=drive_id,
    )
    record = await result.single()
    return dict(record["l"]) if record else {}


async def link_literature_cites(
    session: AsyncSession,
    citing_id: str,
    cited_id: str,
) -> bool:
    """Create CITES relationship between literature items."""
    query = """
    MATCH (citing:LiteratureItem {id: $citing_id})
    MATCH (cited:LiteratureItem {id: $cited_id})
    MERGE (citing)-[:CITES]->(cited)
    RETURN citing, cited
    """
    result = await session.run(query, citing_id=citing_id, cited_id=cited_id)
    record = await result.single()
    return record is not None


async def link_literature_extends(
    session: AsyncSession,
    extending_id: str,
    base_id: str,
) -> bool:
    """Mark that one paper extends another's work."""
    query = """
    MATCH (extending:LiteratureItem {id: $extending_id})
    MATCH (base:LiteratureItem {id: $base_id})
    MERGE (extending)-[:EXTENDS]->(base)
    RETURN extending, base
    """
    result = await session.run(query, extending_id=extending_id, base_id=base_id)
    record = await result.single()
    return record is not None


# =============================================================================
# P1.1: State Snapshots (User Operating State)
# =============================================================================

async def create_state_snapshot(
    session: AsyncSession,
    snapshot_id: str,
    mode: str = OperatingMode.FRAGMENTED.value,
    available_block_minutes: int | None = None,
    interruption_pressure: float = 0.5,
    fatigue_level: float = 0.5,
    fatigue_slope: float = 0.0,
    time_to_next_commitment_minutes: int | None = None,
    focus_score: float | None = None,
    context_notes: str | None = None,
) -> dict:
    """Create a StateSnapshot capturing current operating state.

    Args:
        snapshot_id: Unique identifier
        mode: Current operating mode (deep_focus, fragmented, etc.)
        available_block_minutes: True uninterrupted time available
        interruption_pressure: 0-1 scale of incoming demand
        fatigue_level: 0-1 current tiredness
        fatigue_slope: Rate of fatigue change (-1 to 1)
        time_to_next_commitment_minutes: Minutes until next hard commitment
        focus_score: 0-1 attention bandwidth
        context_notes: Free-form notes about current context
    """
    query = """
    CREATE (ss:StateSnapshot {
        id: $snapshot_id,
        mode: $mode,
        available_block_minutes: $available_block_minutes,
        interruption_pressure: $interruption_pressure,
        fatigue_level: $fatigue_level,
        fatigue_slope: $fatigue_slope,
        time_to_next_commitment_minutes: $time_to_next_commitment_minutes,
        focus_score: $focus_score,
        context_notes: $context_notes,
        created_at: datetime()
    })
    RETURN ss
    """
    result = await session.run(
        query,
        snapshot_id=snapshot_id,
        mode=mode,
        available_block_minutes=available_block_minutes,
        interruption_pressure=interruption_pressure,
        fatigue_level=fatigue_level,
        fatigue_slope=fatigue_slope,
        time_to_next_commitment_minutes=time_to_next_commitment_minutes,
        focus_score=focus_score,
        context_notes=context_notes,
    )
    record = await result.single()
    return dict(record["ss"]) if record else {}


async def get_latest_state_snapshot(session: AsyncSession) -> dict | None:
    """Get the most recent state snapshot."""
    query = """
    MATCH (ss:StateSnapshot)
    RETURN ss {
        .*,
        created_at: toString(ss.created_at)
    } as snapshot
    ORDER BY ss.created_at DESC
    LIMIT 1
    """
    result = await session.run(query)
    record = await result.single()
    return record["snapshot"] if record else None


async def get_state_history(
    session: AsyncSession,
    hours: int = 24,
    limit: int = 100,
) -> list[dict]:
    """Get state snapshots from the past N hours."""
    query = """
    MATCH (ss:StateSnapshot)
    WHERE ss.created_at >= datetime() - duration({hours: $hours})
    RETURN ss {
        .*,
        created_at: toString(ss.created_at)
    } as snapshot
    ORDER BY ss.created_at DESC
    LIMIT $limit
    """
    result = await session.run(query, hours=hours, limit=limit)
    data = await result.data()
    return [r["snapshot"] for r in data]


# =============================================================================
# P2.1: Context Packs
# =============================================================================

async def create_context_pack(
    session: AsyncSession,
    pack_id: str,
    event_id: str | None = None,
    task_id: str | None = None,
    objective: str | None = None,
    last_touch_recap: str | None = None,
    decision_list: list[str] | None = None,
    dont_forget: list[str] | None = None,
    readiness_score: float | None = None,
    missing_prerequisites: list[str] | None = None,
    pre_drafted_content: dict | None = None,
    artifact_links: list[str] | None = None,
    build_stage: str = "T-24h",
) -> dict:
    """Create a ContextPack for an upcoming event or task.

    Args:
        pack_id: Unique identifier
        event_id: Linked calendar event (if applicable)
        task_id: Linked task (if applicable)
        objective: One-line purpose statement
        last_touch_recap: What happened last time
        decision_list: Decisions to make
        dont_forget: Critical reminders
        readiness_score: 0-1 preparation level
        missing_prerequisites: What's needed before start
        pre_drafted_content: Dict of drafts (emails, agenda, etc.)
        artifact_links: Ranked list of relevant doc/code links
        build_stage: When this pack was compiled (T-24h, T-2h, T-15m)
    """
    query = """
    CREATE (cp:ContextPack {
        id: $pack_id,
        event_id: $event_id,
        task_id: $task_id,
        objective: $objective,
        last_touch_recap: $last_touch_recap,
        decision_list: $decision_list,
        dont_forget: $dont_forget,
        readiness_score: $readiness_score,
        missing_prerequisites: $missing_prerequisites,
        pre_drafted_content: $pre_drafted_content,
        artifact_links: $artifact_links,
        build_stage: $build_stage,
        created_at: datetime(),
        updated_at: datetime()
    })
    RETURN cp
    """
    result = await session.run(
        query,
        pack_id=pack_id,
        event_id=event_id,
        task_id=task_id,
        objective=objective,
        last_touch_recap=last_touch_recap,
        decision_list=decision_list or [],
        dont_forget=dont_forget or [],
        readiness_score=readiness_score,
        missing_prerequisites=missing_prerequisites or [],
        pre_drafted_content=str(pre_drafted_content) if pre_drafted_content else None,
        artifact_links=artifact_links or [],
        build_stage=build_stage,
    )
    record = await result.single()
    return dict(record["cp"]) if record else {}


async def get_context_pack(
    session: AsyncSession,
    pack_id: str | None = None,
    event_id: str | None = None,
    task_id: str | None = None,
) -> dict | None:
    """Get a context pack by ID, event, or task."""
    if pack_id:
        match_clause = "MATCH (cp:ContextPack {id: $pack_id})"
        params = {"pack_id": pack_id}
    elif event_id:
        match_clause = "MATCH (cp:ContextPack {event_id: $event_id})"
        params = {"event_id": event_id}
    elif task_id:
        match_clause = "MATCH (cp:ContextPack {task_id: $task_id})"
        params = {"task_id": task_id}
    else:
        return None

    query = f"""
    {match_clause}
    RETURN cp {{
        .*,
        created_at: toString(cp.created_at),
        updated_at: toString(cp.updated_at)
    }} as pack
    ORDER BY cp.updated_at DESC
    LIMIT 1
    """
    result = await session.run(query, params)
    record = await result.single()
    return record["pack"] if record else None


async def update_context_pack(
    session: AsyncSession,
    pack_id: str,
    **updates,
) -> dict | None:
    """Update a context pack with new information."""
    set_clauses = []
    params = {"pack_id": pack_id}

    for key, value in updates.items():
        if value is not None:
            set_clauses.append(f"cp.{key} = ${key}")
            params[key] = value

    if not set_clauses:
        return await get_context_pack(session, pack_id=pack_id)

    set_clauses.append("cp.updated_at = datetime()")

    query = f"""
    MATCH (cp:ContextPack {{id: $pack_id}})
    SET {', '.join(set_clauses)}
    RETURN cp {{
        .*,
        created_at: toString(cp.created_at),
        updated_at: toString(cp.updated_at)
    }} as pack
    """
    result = await session.run(query, params)
    record = await result.single()
    return record["pack"] if record else None


# =============================================================================
# P3.1: Drafts (Writing Pipeline)
# =============================================================================

async def create_draft(
    session: AsyncSession,
    draft_id: str,
    title: str,
    draft_type: str = DraftType.OTHER.value,
    content: str | None = None,
    version: int = 1,
    status: str = "draft",
    project_id: str | None = None,
    parent_draft_id: str | None = None,
) -> dict:
    """Create a Draft node for writing artifacts.

    Args:
        draft_id: Unique identifier
        title: Draft title
        draft_type: Type of writing (grant_section, paper_methods, etc.)
        content: Current draft content
        version: Version number
        status: draft, review, final
        project_id: Linked project
        parent_draft_id: Parent draft for versioning
    """
    query = """
    CREATE (d:Draft {
        id: $draft_id,
        title: $title,
        type: $draft_type,
        content: $content,
        version: $version,
        status: $status,
        project_id: $project_id,
        parent_draft_id: $parent_draft_id,
        created_at: datetime(),
        updated_at: datetime()
    })
    RETURN d
    """
    result = await session.run(
        query,
        draft_id=draft_id,
        title=title,
        draft_type=draft_type,
        content=content,
        version=version,
        status=status,
        project_id=project_id,
        parent_draft_id=parent_draft_id,
    )
    record = await result.single()
    return dict(record["d"]) if record else {}


async def get_draft(session: AsyncSession, draft_id: str) -> dict | None:
    """Get a draft with linked claims."""
    query = """
    MATCH (d:Draft {id: $draft_id})
    OPTIONAL MATCH (c:Claim)-[:USED_IN]->(d)
    OPTIONAL MATCH (d)-[:PART_OF]->(p:Project)
    RETURN d {
        .*,
        created_at: toString(d.created_at),
        updated_at: toString(d.updated_at),
        claims: collect(DISTINCT c.id),
        project_title: p.title
    } as draft
    """
    result = await session.run(query, draft_id=draft_id)
    record = await result.single()
    return record["draft"] if record else None


# =============================================================================
# P3.2: Experiment/Analysis Registry
# =============================================================================

async def create_run(
    session: AsyncSession,
    run_id: str,
    name: str,
    project_id: str | None = None,
    dataset_snapshot: str | None = None,
    commit_hash: str | None = None,
    environment: dict | None = None,
    parameters: dict | None = None,
    outputs: dict | None = None,
    metrics: dict | None = None,
    figures: list[str] | None = None,
    notes: str | None = None,
) -> dict:
    """Create a Run node for experiment tracking.

    Args:
        run_id: Unique identifier
        name: Run name/description
        project_id: Linked project
        dataset_snapshot: Dataset version/hash
        commit_hash: Git commit hash
        environment: Environment details (Python version, packages)
        parameters: Hyperparameters/settings
        outputs: Output file paths/hashes
        metrics: Performance metrics
        figures: List of figure IDs/paths
        notes: Free-form notes
    """
    query = """
    CREATE (r:Run {
        id: $run_id,
        name: $name,
        project_id: $project_id,
        dataset_snapshot: $dataset_snapshot,
        commit_hash: $commit_hash,
        environment: $environment,
        parameters: $parameters,
        outputs: $outputs,
        metrics: $metrics,
        figures: $figures,
        notes: $notes,
        created_at: datetime()
    })
    RETURN r
    """
    result = await session.run(
        query,
        run_id=run_id,
        name=name,
        project_id=project_id,
        dataset_snapshot=dataset_snapshot,
        commit_hash=commit_hash,
        environment=str(environment) if environment else None,
        parameters=str(parameters) if parameters else None,
        outputs=str(outputs) if outputs else None,
        metrics=str(metrics) if metrics else None,
        figures=figures or [],
        notes=notes,
    )
    record = await result.single()
    return dict(record["r"]) if record else {}


async def get_run(session: AsyncSession, run_id: str) -> dict | None:
    """Get a run by ID."""
    query = """
    MATCH (r:Run {id: $run_id})
    OPTIONAL MATCH (r)-[:PART_OF]->(p:Project)
    RETURN r {
        .*,
        created_at: toString(r.created_at),
        project_title: p.title
    } as run
    """
    result = await session.run(query, run_id=run_id)
    record = await result.single()
    return record["run"] if record else None


async def get_runs_for_project(
    session: AsyncSession,
    project_id: str,
    limit: int = 50,
) -> list[dict]:
    """Get all runs for a project."""
    query = """
    MATCH (r:Run {project_id: $project_id})
    RETURN r {
        .*,
        created_at: toString(r.created_at)
    } as run
    ORDER BY r.created_at DESC
    LIMIT $limit
    """
    result = await session.run(query, project_id=project_id, limit=limit)
    data = await result.data()
    return [r["run"] for r in data]


# =============================================================================
# P3.3: Reviewer Comments
# =============================================================================

async def create_reviewer_comment(
    session: AsyncSession,
    comment_id: str,
    content: str,
    reviewer: str | None = None,
    draft_id: str | None = None,
    status: str = "pending",
    response: str | None = None,
) -> dict:
    """Create a ReviewerComment node.

    Args:
        comment_id: Unique identifier
        content: The reviewer's comment
        reviewer: Reviewer identifier (e.g., "Reviewer 1")
        draft_id: Linked draft being reviewed
        status: pending, addressed, rejected
        response: Our response to the comment
    """
    query = """
    CREATE (rc:ReviewerComment {
        id: $comment_id,
        content: $content,
        reviewer: $reviewer,
        draft_id: $draft_id,
        status: $status,
        response: $response,
        created_at: datetime(),
        updated_at: datetime()
    })
    RETURN rc
    """
    result = await session.run(
        query,
        comment_id=comment_id,
        content=content,
        reviewer=reviewer,
        draft_id=draft_id,
        status=status,
        response=response,
    )
    record = await result.single()
    return dict(record["rc"]) if record else {}


async def link_comment_to_claim(
    session: AsyncSession,
    comment_id: str,
    claim_id: str,
) -> bool:
    """Link a reviewer comment to a supporting claim."""
    query = """
    MATCH (rc:ReviewerComment {id: $comment_id})
    MATCH (c:Claim {id: $claim_id})
    MERGE (rc)-[:ADDRESSED_BY]->(c)
    RETURN rc, c
    """
    result = await session.run(query, comment_id=comment_id, claim_id=claim_id)
    record = await result.single()
    return record is not None
