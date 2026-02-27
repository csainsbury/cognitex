# COGNITEX EVOLUTION BLUEPRINT
## Claude Code Implementation Guide

**Version:** 1.0
**Date:** 2026-02-27
**Scope:** Integrate OpenClaw's best architectural patterns into the Cognitex codebase
**Target:** Claude Code instance running in the cognitex repo root

---

## HOW TO USE THIS DOCUMENT

This is the single source of truth for evolving Cognitex. It is designed to be loaded into a Claude Code session as the primary instruction document. Work through the work packages (WPs) in order. Each WP specifies exact files to create/modify, expected behaviour, and acceptance criteria.

**Operating rules:**
- Always create a git branch per WP: `git checkout -b wp1-extended-bootstrap`
- Run existing tests before and after changes: `pytest tests/`
- Never modify files in `~/.cognitex/bootstrap/` without operator approval
- The clinical data firewall (WP2) is non-negotiable — ship it before anything that touches email content
- If a change breaks existing functionality, stop and flag it

**Reference implementation:**
The OpenClaw repo is available at `~/.cognitex/reference/openclaw/` as a read-only reference. Consult it for implementation patterns (skill parser, slash commands, model routing, formatSkillsForPrompt) but NEVER modify files in this directory. Key reference files:
- `docs/tools/skills.md` — AgentSkills format spec and gating rules
- `src/agents/` — system prompt construction, skill injection
- `skills/` — bundled skill examples (real SKILL.md files to test parser against)
- `docs/gateway/configuration.md` — model routing and config structure

To set up: `git clone https://github.com/openclaw/openclaw.git ~/.cognitex/reference/openclaw/`

---

## CODEBASE ORIENTATION

**Repository:** `github.com/csainsbury/cognitex`
**Language:** Python 3.11+
**Lines of code:** ~62,000 across 77 Python files
**Framework:** FastAPI (web), Typer (CLI), structlog (logging)
**Infrastructure:** Neo4j (knowledge graph), PostgreSQL + pgvector (vectors/storage), Redis (pub/sub, state, cache)
**LLM providers:** Google Gemini, Anthropic Claude, OpenAI, Together.ai (runtime-switchable via Redis)

### Key directories

```
src/cognitex/
├── agent/                # Core agent logic
│   ├── autonomous.py     # 15-min autonomous agent loop (OODA cycle)
│   ├── bootstrap.py      # Bootstrap file loader (SOUL.md, IDENTITY.md, CONTEXT.md)
│   ├── skills.py         # Skills loader and parser
│   ├── core.py           # Morning briefing, conversational agent
│   ├── state_model.py    # Operating modes (DEEP_FOCUS, FRAGMENTED, etc.)
│   ├── interruption_firewall.py  # Notification gating
│   ├── context_pack.py   # Meeting prep context packs (T-24h/T-2h/T-15m)
│   ├── decision_memory.py # Decision traces with feedback
│   ├── graph_observer.py # Graph health monitoring queries
│   ├── tools.py          # 30+ agent tools (ReAct loop)
│   ├── triggers.py       # Scheduled trigger system
│   ├── learning.py       # Pattern learning infrastructure
│   ├── feedback_learning.py # Feedback-based adaptation
│   ├── skill_evolution.py   # [TO CREATE - WP3]
│   └── skill_authoring.py   # [TO CREATE - WP3]
├── services/
│   ├── llm.py            # Multi-provider LLM calls (primary_model + fast_model)
│   ├── model_config.py   # Runtime model switching via Redis
│   ├── gmail.py          # Gmail integration
│   ├── calendar.py       # Google Calendar
│   ├── drive.py          # Google Drive
│   ├── github.py         # GitHub integration
│   ├── ingestion.py      # Document ingestion pipeline
│   ├── email_intent.py   # Email classification
│   ├── tasks.py          # Task management
│   ├── hybrid_search.py  # Vector + keyword search
│   ├── memory_files.py   # Daily logs + MEMORY.md management
│   ├── skill_registry.py # [TO CREATE - WP3]
│   └── clinical_firewall.py # [TO CREATE - WP2]
├── skills/               # Bundled skills (3 exist)
│   ├── email-tasks/SKILL.md
│   ├── goal-linking/SKILL.md
│   └── meeting-prep/SKILL.md
├── db/
│   ├── neo4j.py          # Neo4j driver
│   ├── graph_schema.py   # Node/relationship definitions
│   ├── postgres.py       # PostgreSQL driver
│   └── redis.py          # Redis driver
├── web/                  # FastAPI + HTMX web dashboard
│   ├── app.py            # Routes (343K — large file)
│   └── templates/        # Jinja2 templates
├── cli/
│   └── main.py           # Typer CLI (283K — large file)
├── discord_bot/          # Discord bot interface
├── prompts/
│   └── autonomous_agent.md # System prompt for autonomous agent
├── config.py             # Pydantic settings
└── worker.py             # Background worker
```

### Architecture summary

```
User ──→ Web Dashboard / CLI / Discord Bot
              │
              ▼
         Agent Core (core.py)
              │
              ├──→ Bootstrap Loader (bootstrap.py) ──→ ~/.cognitex/bootstrap/*.md
              ├──→ Skills Loader (skills.py) ──→ ~/.cognitex/skills/ + src/cognitex/skills/
              ├──→ LLM Service (llm.py) ──→ Gemini / Claude / OpenAI / Together
              ├──→ Tools (tools.py) ──→ 30+ graph/email/calendar/task tools
              └──→ Autonomous Agent (autonomous.py) ──→ 15-min loop
                        │
                        ├──→ Graph Observer ──→ Neo4j queries
                        ├──→ State Estimator ──→ Operating mode detection
                        ├──→ Context Pack Compiler ──→ Meeting prep
                        └──→ Decision Memory ──→ PostgreSQL traces
```

### Existing model role architecture

The LLM service (`services/llm.py`) uses two model roles:
- **primary_model** (planner): Used for reasoning, planning, autonomous agent decisions
- **fast_model** (executor): Used for structured tasks — email drafting, classification, extraction

These map to provider-specific models in `config.py`:
- `google_model_planner` / `google_model_executor`
- `anthropic_model_planner` / `anthropic_model_executor`
- `openai_model_planner` / `openai_model_executor`
- `together_model_planner` / `together_model_executor`

Runtime switching is via `model_config.py` which stores active config in Redis key `cognitex:model_config`.

### Existing skills architecture

`agent/skills.py` provides:
- `SkillsLoader` with async discovery, caching, file-watch invalidation
- Loads from `BUNDLED_SKILLS_DIR` (src/cognitex/skills/) and `USER_SKILLS_DIR` (~/.cognitex/skills/)
- User skills override bundled by name
- `save_skill()`, `delete_skill()` exist but are never called by the autonomous agent
- 3 bundled skills: email-tasks, goal-linking, meeting-prep
- Parser handles `## Section` headers only (no YAML frontmatter)

### Existing bootstrap architecture

`agent/bootstrap.py` provides:
- `BootstrapLoader` with async loading, caching, file-watch invalidation
- Loads 3 files: SOUL.md (communication style), IDENTITY.md (user context), CONTEXT.md (ambient state, agent-maintained)
- `format_for_prompt()` injects into system prompt
- `update_context()` allows agent to write to CONTEXT.md
- `save_file()` restricted to the 3 known filenames

---

## WORK PACKAGE OVERVIEW

| WP | Name | Est. Days | Dependencies |
|----|------|-----------|--------------|
| WP1 | Extended Bootstrap System | 3-5 | None |
| WP2 | Clinical Data Firewall | 2-3 | None |
| WP3 | AgentSkills-Compatible Self-Evolving Skills | 8-12 | WP1, WP2 |
| WP4 | Structured Triage Extraction | 2-3 | WP2, WP3-A |
| WP5 | Commitment Ledger | 3-4 | WP1 |
| WP6 | Memory Curation & Distillation | 2-3 | WP1 |
| WP7 | Slash Commands & Model Routing | 3-4 | WP3-A |
| WP8 | AgentMail Integration | 3-4 | WP2 |
| **Total** | | **~6-7 weeks** | |

**Execution order:**
```
Week 1:    WP1 (Extended Bootstrap) + WP2 (Clinical Firewall)
Week 2:    WP8 (AgentMail) + WP3-A (AgentSkills Format Compatibility)
Week 2-3:  WP4 (Triage Extraction) + WP5 (Commitment Ledger)
Week 3:    WP6 (Memory Curation) + WP7 (Slash Commands & Model Routing)
Week 3-4:  WP3-B (Three-Path Skill Creation)
Week 4-5:  WP3-C (Feedback Loop & Continuous Refinement)
```

WP8 is scheduled in Week 2, immediately after the clinical firewall (WP2) ships. This is deliberate: AgentMail content still passes through the firewall as a second line of defence. Getting AgentMail in early also means all subsequent WPs that touch email (WP4 triage extraction, WP5 commitment extraction from email) build on the cleaner AgentMail pipeline rather than the fragile Gmail OAuth flow.

---

## WP1: EXTENDED BOOTSTRAP SYSTEM (3-5 days)

**Branch:** `wp1-extended-bootstrap`
**Goal:** Make Cognitex load and use the full OpenClaw-style configuration file set.

### 1.1 Extend BootstrapLoader

**File:** `src/cognitex/agent/bootstrap.py`

Currently loads: SOUL.md, IDENTITY.md, CONTEXT.md
Must load: SOUL.md, USER.md, AGENTS.md, TOOLS.md, MEMORY.md (curated), IDENTITY.md (fallback), CONTEXT.md (auto-updated)

**Changes:**

1. Add new file constants:
```python
BOOTSTRAP_FILES = {
    "SOUL.md": {"writable_by_agent": False, "required": True},
    "USER.md": {"writable_by_agent": False, "required": True},
    "AGENTS.md": {"writable_by_agent": False, "required": True},
    "TOOLS.md": {"writable_by_agent": False, "required": False},
    "MEMORY.md": {"writable_by_agent": True, "required": False},
    "IDENTITY.md": {"writable_by_agent": False, "required": False},  # Legacy fallback
    "CONTEXT.md": {"writable_by_agent": True, "required": False},
}
```

2. Add methods: `get_user()`, `get_agents()`, `get_tools()`, `get_memory_file()`

3. Extend `get_all()` to return the full file set

4. Extend `format_for_prompt()`:
   - SOUL.md → agent behaviour rules (full injection)
   - USER.md → operator context (key sections only — keep token budget manageable)
   - AGENTS.md §5 (Safety and Action Boundaries) → hard constraints (full injection)
   - AGENTS.md other sections → summarised
   - TOOLS.md → SSH hosts, workspace paths, key infrastructure facts
   - MEMORY.md → curated operational memory (full injection — this is the durable context)
   - CONTEXT.md → ambient state (full injection)

5. Add `get_safety_rules()` method that extracts safety section from AGENTS.md for injection into ALL LLM calls (not just autonomous agent)

6. Extend `save_file()` to accept the new filenames (not just the original 3)

7. Keep backward compatibility: if USER.md doesn't exist but IDENTITY.md does, fall back to IDENTITY.md

### 1.2 Update autonomous agent prompt

**File:** `src/cognitex/prompts/autonomous_agent.md`

Add at the top, before existing content:
```
## Operating Constitution
{agents_safety_rules}

## Operator Profile
{user_context}

## Operational Memory
{memory_content}

## Tools & Infrastructure
{tools_context}
```

**File:** `src/cognitex/agent/autonomous.py`

In `_reason_about_context()`, load the new bootstrap sections and inject them into the prompt template.

### 1.3 Update web dashboard

**File:** `src/cognitex/web/templates/bootstrap.html`

Add tabs for USER.md, AGENTS.md, TOOLS.md, MEMORY.md alongside existing editors.

### 1.4 Deploy the .md files

The operator (Chris) will populate these files. Create default templates with `[FILL]` markers:

**File:** `~/.cognitex/bootstrap/USER.md` (default template)
**File:** `~/.cognitex/bootstrap/AGENTS.md` (default template)
**File:** `~/.cognitex/bootstrap/TOOLS.md` (default template)
**File:** `~/.cognitex/bootstrap/MEMORY.md` (default template)

NOTE: Rich versions of these files already exist from a prior session. The operator may provide them. The defaults are fallbacks only.

### Acceptance criteria
- [ ] `cognitex` boots and loads all 7 files without error
- [ ] `get_formatted_prompt_section()` returns content from all loaded files
- [ ] Safety rules from AGENTS.md are present in autonomous agent prompt
- [ ] Missing files degrade gracefully (warning logged, system continues)
- [ ] Web dashboard shows editors for all files
- [ ] Existing tests pass

---

## WP2: CLINICAL DATA FIREWALL (2-3 days)

**Branch:** `wp2-clinical-firewall`
**Goal:** Ensure no PHI or clinical data reaches the LLM context window.

### 2.1 Create clinical firewall module

**New file:** `src/cognitex/services/clinical_firewall.py`

```python
"""Pre-LLM clinical data filter.

Scans text for patterns indicating clinical/patient data (CHI numbers,
NHS numbers, clinical urgency language, ward/inpatient context, etc.)
and prevents this content from reaching the LLM context window.

This filter runs OUTSIDE the LLM — it is not a prompt instruction.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import structlog

logger = structlog.get_logger()

@dataclass
class ClinicalScanResult:
    is_clinical: bool
    matched_categories: list[str] = field(default_factory=list)
    matched_patterns: list[str] = field(default_factory=list)
    sanitised_text: str = ""  # Text with clinical content redacted
    bypass_action: Literal["block", "redact", "flag"] = "block"

class ClinicalDataFirewall:
    """Pre-LLM filter for clinical data in email/document content."""

    def __init__(self, patterns_path: Path | None = None):
        self.patterns_path = patterns_path or Path.home() / ".cognitex" / "config" / "clinical_bypass_regex.txt"
        self.patterns: dict[str, list[re.Pattern]] = {}
        self._load_patterns()

    def _load_patterns(self) -> None:
        """Load regex patterns from config file.
        File format: category headers as '# Category Name', patterns as lines below."""
        # ... implementation

    def scan(self, text: str) -> ClinicalScanResult:
        """Scan text for clinical data patterns."""
        # ... implementation

    def filter_email(self, email: dict) -> dict | None:
        """Filter email before LLM processing.
        Returns None if clinical data detected (triggers CLINICAL_BYPASS).
        Returns email dict with sanitised content if redaction mode."""
        # ... implementation

    def filter_text(self, text: str) -> str:
        """Filter arbitrary text, replacing clinical content with [CLINICAL_REDACTED]."""
        # ... implementation
```

Pattern categories to detect (from the clinical_bypass_regex.txt already created):
- Patient identifiers (CHI numbers, NHS numbers, hospital numbers)
- Clinical urgency language (MDT, adverse event, DATIX, safeguarding)
- Ward/inpatient context (ward names, bed numbers, discharge)
- Clinical results (HbA1c, eGFR, blood results)
- Prescribing (insulin, medication changes, dose adjustments)
- Clinic/consultation patterns (clinic letters, referrals)
- NHS-specific systems (SCI-Store, TrakCare, EMIS, BadgerNet)

### 2.2 Add config setting

**File:** `src/cognitex/config.py`

```python
# Clinical Data Firewall
clinical_firewall_enabled: bool = Field(
    default=True,
    description="Enable pre-LLM clinical data filtering",
)
clinical_firewall_mode: Literal["block", "redact", "flag"] = Field(
    default="block",
    description="block=skip LLM entirely, redact=remove PHI then process, flag=process but warn",
)
clinical_firewall_patterns_path: str = Field(
    default="~/.cognitex/config/clinical_bypass_regex.txt",
    description="Path to clinical data regex patterns file",
)
```

### 2.3 Integrate into Gmail pipeline

**File:** `src/cognitex/services/gmail.py`

In the message processing pipeline, add firewall check BEFORE any LLM classification:

```python
from cognitex.services.clinical_firewall import ClinicalDataFirewall

# In _process_message() or equivalent:
if settings.clinical_firewall_enabled:
    firewall = ClinicalDataFirewall()
    result = firewall.scan(email_body + " " + email_subject)
    if result.is_clinical:
        if settings.clinical_firewall_mode == "block":
            # Tag as CLINICAL in graph, skip LLM processing entirely
            await self._tag_clinical_email(message_id, result.matched_categories)
            logger.info("Clinical email bypassed", id=message_id, categories=result.matched_categories)
            return
        elif settings.clinical_firewall_mode == "redact":
            email_body = result.sanitised_text
```

### 2.4 Integrate into autonomous agent

**File:** `src/cognitex/agent/autonomous.py`

In `_reason_about_context()`, filter all email snippets through the firewall before injecting into the LLM prompt.

### 2.5 Deploy regex patterns

Create default pattern file at `~/.cognitex/config/clinical_bypass_regex.txt` if not present.

### Acceptance criteria
- [ ] Clinical email with CHI number is blocked from LLM processing
- [ ] Clinical email is tagged as CLINICAL in the graph
- [ ] Non-clinical emails pass through unchanged
- [ ] Firewall can be disabled via config
- [ ] Autonomous agent prompt never contains clinical content
- [ ] Existing tests pass

---

## WP3: AGENTSKILLS-COMPATIBLE SELF-EVOLVING SKILLS (8-12 days)

**Branch:** `wp3-agentskills` (then `wp3-skill-creation`, `wp3-feedback-loop`)

### Phase A: AgentSkills Format Compatibility (3-4 days)

**Goal:** Adopt the AgentSkills standard (used by OpenClaw's 3,000+ community skills), while keeping existing Cognitex skills working.

#### 3A.1 Extend SKILL.md parser for YAML frontmatter

**File:** `src/cognitex/agent/skills.py`

The OpenClaw / AgentSkills format:
```yaml
---
name: my-skill
description: Short description of what this skill does.
version: 1.0.0
metadata: { "openclaw": { "requires": { "bins": ["curl"], "env": ["API_KEY"] }, "emoji": "🔧" } }
---

# Instructions
Markdown body teaching the agent how to use this skill...
```

Changes to `skills.py`:

1. **Add YAML frontmatter parser** — detect `---` delimiters at top of file, parse with `yaml.safe_load()`
2. **Extend `Skill` dataclass:**
   ```python
   version: str = "1.0.0"
   metadata: dict = field(default_factory=dict)
   format: Literal["agentskills", "cognitex_legacy"] = "cognitex_legacy"
   requires_bins: list[str] = field(default_factory=list)
   requires_env: list[str] = field(default_factory=list)
   requires_config: list[str] = field(default_factory=list)
   eligible: bool = True
   ineligibility_reason: str = ""
   ```
3. **Add `_check_eligibility()` method** — verify bins on PATH, env vars set, config keys truthy
4. **Backward compatibility** — if no frontmatter detected, fall back to existing section-header parsing
5. **Extend `format_skill_for_prompt()`:**
   - AgentSkills format: compact XML summary in skills list, full body loaded on-demand
   - Legacy format: existing direct injection (purpose, rules, examples)

#### 3A.2 Build community skill registry client

**New file:** `src/cognitex/services/skill_registry.py`

```python
class SkillRegistry:
    """Interface to the OpenClaw/ClawHub community skill ecosystem."""

    SKILLS_REPO_URL = "https://github.com/openclaw/skills.git"
    CACHE_DIR = Path.home() / ".cognitex" / "cache" / "community-skills"

    async def sync_registry(self) -> int:
        """Clone or pull the community skills repo. Returns skill count."""

    async def search(self, query: str) -> list[SkillListing]:
        """Search community skills by name/description."""

    async def install(self, skill_slug: str) -> bool:
        """Copy skill from cache to ~/.cognitex/skills/."""

    async def update(self, skill_slug: str | None = None) -> list[str]:
        """Update installed community skills. None = update all."""

    async def list_installed(self) -> list[dict]:
        """List community skills currently installed."""
```

#### 3A.3 CLI integration

**File:** `src/cognitex/cli/main.py`

Add commands:
```bash
cognitex skills list                    # All skills with eligibility status
cognitex skills search <query>          # Search community registry
cognitex skills install <slug>          # Install from community
cognitex skills info <name>             # Show skill details
cognitex skills update [--all]          # Update community skills
cognitex skills sync                    # Sync community registry
```

#### 3A.4 Web dashboard integration

**File:** `src/cognitex/web/app.py` + new template `templates/skills.html`

Skills page showing: installed skills (source, version, eligibility), community browser with search, one-click install.

#### 3A.5 Migrate existing bundled skills to AgentSkills format

Add YAML frontmatter to:
- `src/cognitex/skills/email-tasks/SKILL.md`
- `src/cognitex/skills/goal-linking/SKILL.md`
- `src/cognitex/skills/meeting-prep/SKILL.md`

Keep existing body content unchanged.

### Phase B: Three-Path Skill Creation (3-4 days)

**Path 1: Operator writes directly** — already works, now with AgentSkills format preferred.

**Path 2: Operator describes, agent writes**

**New file:** `src/cognitex/agent/skill_authoring.py`

```python
class SkillAuthoring:
    """Conversational skill creation — operator describes, agent generates."""

    async def create_from_description(self, description: str, examples: list[dict] | None = None) -> SkillDraft:
        """Generate a SKILL.md from natural language description."""

    async def refine_draft(self, draft: SkillDraft, feedback: str) -> SkillDraft:
        """Iteratively refine based on operator feedback."""

    async def test_skill(self, draft: SkillDraft, test_inputs: list[str]) -> list[SkillTestResult]:
        """Test draft against sample inputs."""

    async def deploy_skill(self, draft: SkillDraft) -> bool:
        """Save approved draft and reload skills."""
```

CLI: `cognitex skills create` (interactive), `cognitex skills create --from "description"`
Web: "Create Skill" form with preview, test, and deploy.

**Path 3: Agent detects pattern and proposes**

**New file:** `src/cognitex/agent/skill_evolution.py`

```python
class SkillEvolution:
    """Agent-driven skill creation and refinement."""

    async def detect_skill_opportunity(self, context: dict) -> SkillProposal | None:
        """Analyse recent actions for recurring codifiable patterns."""

    async def propose_new_skill(self, pattern: PatternDescription) -> SkillProposal:
        """Generate a new SKILL.md from detected pattern. Requires operator approval."""

    async def refine_skill(self, skill_name: str, feedback: list[FeedbackEntry]) -> SkillUpdate:
        """Propose updates based on accumulated feedback."""

    async def generate_code_proposal(self, skill_name: str, limitation: str) -> CodeProposal:
        """Propose code changes when skill can't be expressed as prompt rules.
        ALWAYS requires operator approval. NEVER auto-execute."""
```

Integration with autonomous agent — add EVOLVE phase to `_run_cycle()`, running every 10th cycle (~2.5 hours). Pattern detection from: decision memory (repeated rejections), task proposal accuracy, email classification accuracy, new topic clusters.

Web dashboard: "Evolution" tab with skill proposals, code proposal diffs, approval workflow.

**Safety rules for code proposals:**
- Never auto-execute. Diff review required.
- Cannot modify SOUL.md, USER.md, AGENTS.md, safety rules
- All proposals logged in agent audit trail
- Operator can approve, modify, or reject

### Phase C: Feedback Loop (2-3 days)

Add `SkillFeedback` tracking to `skills.py`. Route rejected/corrected proposals to relevant skill. After N feedback entries, trigger skill evolution refinement proposal.

Optional: publish clinical-domain skills back to ClawHub via `clawhub sync` or PR to `openclaw/skills`.

### Acceptance criteria (all phases)
- [ ] Skills with YAML frontmatter load correctly
- [ ] Existing 3 bundled skills still work (backward compatibility)
- [ ] Community skills can be searched, installed, listed
- [ ] Ineligible skills are loaded but marked as such (missing bins/env)
- [ ] `cognitex skills create --from "..."` generates valid SKILL.md
- [ ] Agent proposes skill improvements based on feedback patterns
- [ ] Code proposals shown in dashboard with diff view
- [ ] No auto-execution of code proposals

---

## WP4: STRUCTURED TRIAGE EXTRACTION (2-3 days)

**Branch:** `wp4-triage-extraction`
**Goal:** Replace conversational email classification with deterministic JSON extraction + tone neutralisation.

### 4.1 Create email-triage skill

**New skill:** `src/cognitex/skills/email-triage/SKILL.md`

AgentSkills format with:
- Structured JSON output schema (action_verb, deadline, delegation_candidate, confidence, project_context)
- Tone neutralisation rules (strip emotional valence before priority scoring)
- Clinical bypass trigger (defer to WP2 firewall)
- Worked examples

### 4.2 Integrate with email_intent.py

**File:** `src/cognitex/services/email_intent.py`

Replace current classification approach:
- Load email-triage skill via SkillsLoader
- Use structured JSON schema for output parsing
- Store extracted fields as properties on Email node in Neo4j
- Route triage decision (Action/Delegate/Track/Archive) into existing inbox/task pipeline

### 4.3 Add tone neutralisation pre-processing

Before priority scoring, extract factual content and deadlines separately from emotional tone. This prevents LLM prioritisation bias toward urgent-sounding language.

### Acceptance criteria
- [ ] Email classification produces structured JSON output
- [ ] Tone-neutral emails are not deprioritised vs urgent-sounding ones with same factual content
- [ ] Extracted fields visible on Email nodes in Neo4j
- [ ] Existing task creation pipeline still works

---

## WP5: COMMITMENT LEDGER (3-4 days)

**Branch:** `wp5-commitment-ledger`
**Goal:** Add commitment tracking as a first-class graph concept with bidirectional LEDGER.yaml sync.

### 5.1 Add Commitment node type

**File:** `src/cognitex/db/graph_schema.py`

New node type with properties: commitment_id, task_description, owner, deadline, status (pending → accepted → in_progress → blocked → waiting_on → complete → abandoned), cognitive_load, source (provenance), dependencies, date_logged.

Relationships: COMMITTED_TO (Project), DEPENDS_ON (Commitment), EXTRACTED_FROM (Email), OWNED_BY (Person), WAITING_ON (Person).

### 5.2 Commitment extraction in autonomous agent

Detect commitment language in emails ("I will send by", "Chris to review", "Let's submit on") and create pending Commitment nodes for operator approval.

### 5.3 Commitment monitoring in graph observer

**File:** `src/cognitex/agent/graph_observer.py`

Add queries: commitments within 48h of deadline, overdue commitments, blocked/waiting >7 days. Surface in morning briefing.

### 5.4 LEDGER.yaml bidirectional sync

**File:** `src/cognitex/services/memory_files.py` (extend)

- Graph → File: agent writes current commitment state to `~/.cognitex/bootstrap/LEDGER.yaml` each cycle
- File → Graph: if operator edits LEDGER.yaml directly, sync to graph on next boot

### Acceptance criteria
- [ ] Commitment nodes created from email content
- [ ] Pending commitments shown in dashboard for approval
- [ ] Approaching deadlines surfaced in morning briefing
- [ ] LEDGER.yaml reflects current graph state
- [ ] Manual LEDGER.yaml edits sync back to graph

---

## WP6: MEMORY CURATION & DISTILLATION (2-3 days)

**Branch:** `wp6-memory-curation`
**Goal:** Add active memory management with distillation and forgetting.

### 6.1 Weekly distillation in autonomous agent

**File:** `src/cognitex/agent/autonomous.py`

Triggered by day-of-week check (e.g., Sunday evening):
- Load recent daily memory logs from `~/.cognitex/memory/`
- Run memory distillation prompt (extract: project state updates, decisions with rationale, new commitments, preferences, lessons learned)
- Propose MEMORY.md updates (shown in dashboard for approval)
- Archive processed daily logs

### 6.2 Forgetting policy

**File:** `src/cognitex/services/memory_files.py`

- Daily: trivial updates not carried forward
- Weekly: distilled content promoted to MEMORY.md, raw detail deprioritised
- Monthly: daily logs >30 days moved to `~/.cognitex/memory/archive/`, not loaded by agent

### 6.3 Memory curation skill

**New skill:** `~/.cognitex/skills/memory-curation/SKILL.md`

AgentSkills format. Rules for what gets promoted to MEMORY.md vs discarded. This becomes a teachable, evolvable behaviour via WP3.

### Acceptance criteria
- [ ] Weekly distillation runs and proposes MEMORY.md updates
- [ ] Old daily logs archived automatically
- [ ] Memory curation skill loaded and used by distillation process
- [ ] MEMORY.md content available in agent prompt via WP1 bootstrap

---

## WP7: SLASH COMMANDS & MODEL ROUTING (3-4 days)

**Branch:** `wp7-slash-commands`
**Goal:** Add OpenClaw-style slash commands for runtime model switching and operational control, plus per-task model routing.

### 7.1 Slash command framework

**New file:** `src/cognitex/agent/slash_commands.py`

```python
"""Slash command system for runtime control.

Inspired by OpenClaw's user-invocable skill commands.
Commands are parsed before LLM processing — they are
direct operations, not conversational requests.
"""

from dataclasses import dataclass
from typing import Callable, Awaitable

@dataclass
class SlashCommand:
    name: str              # e.g., "model"
    description: str
    handler: Callable[..., Awaitable[str]]
    aliases: list[str] = field(default_factory=list)

class SlashCommandRegistry:
    """Registry for slash commands."""

    def __init__(self):
        self._commands: dict[str, SlashCommand] = {}

    def register(self, command: SlashCommand) -> None:
        self._commands[command.name] = command
        for alias in command.aliases:
            self._commands[alias] = command

    async def dispatch(self, input_text: str) -> str | None:
        """Parse and dispatch a slash command.
        Returns response string if command found, None if not a command."""
        if not input_text.startswith("/"):
            return None
        parts = input_text[1:].split(maxsplit=1)
        cmd_name = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""
        if cmd_name in self._commands:
            return await self._commands[cmd_name].handler(args)
        return None

    def list_commands(self) -> list[dict]:
        """List all registered commands."""
        seen = set()
        result = []
        for name, cmd in self._commands.items():
            if cmd.name not in seen:
                seen.add(cmd.name)
                result.append({"name": cmd.name, "description": cmd.description, "aliases": cmd.aliases})
        return result
```

### 7.2 Built-in commands

Register these commands at startup:

```
/model <name>              Switch active model (aliases: sonnet, opus, gemini, deepseek, haiku, flash)
/model                     Show current model configuration
/provider <name>           Switch LLM provider (google, anthropic, openai, together)
/status                    Show system status (mode, energy, active skills, model, pending items)
/mode <mode>               Set operating mode (deep_focus, fragmented, overloaded, avoidant, transition)
/skills                    List active skills with eligibility
/skill install <slug>      Install community skill
/skill create              Start interactive skill creation
/briefing                  Generate morning briefing now
/next                      Get recommended next action
/approve <id>              Approve a pending proposal
/reject <id>               Reject a pending proposal
/help                      List all available commands
```

**Model shorthand aliases:**

```python
MODEL_ALIASES = {
    # Anthropic
    "sonnet": ("anthropic", "claude-sonnet-4-20250514"),
    "opus": ("anthropic", "claude-opus-4-20250514"),
    "haiku": ("anthropic", "claude-3-5-haiku-20241022"),
    # Google
    "gemini": ("google", "gemini-3-pro-preview"),
    "flash": ("google", "gemini-3-flash-preview"),
    # Together
    "deepseek": ("together", "deepseek-ai/DeepSeek-V3"),
    "r1": ("together", "deepseek-ai/DeepSeek-R1"),
    "qwen": ("together", "Qwen/Qwen3-235B-A22B-fp8"),
    "llama": ("together", "meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8"),
    # OpenAI
    "gpt4o": ("openai", "gpt-4o"),
    "o3": ("openai", "o3-mini"),
}
```

The `/model` command updates the Redis-backed model config, so the change takes effect immediately for the next LLM call without restart.

### 7.3 Per-task model routing (subagent models)

**File:** `src/cognitex/services/model_config.py`

Extend `ModelConfig` to support task-specific model overrides:

```python
@dataclass
class ModelConfig:
    provider: str
    planner_model: str
    executor_model: str
    embedding_model: str
    embedding_provider: str = "together"
    # NEW: Task-specific overrides
    autonomous_model: str = ""      # Model for autonomous agent loop (defaults to planner)
    triage_model: str = ""          # Model for email triage (defaults to executor)
    draft_model: str = ""           # Model for email drafting (defaults to executor)
    context_pack_model: str = ""    # Model for context pack compilation (defaults to executor)
    skill_evolution_model: str = "" # Model for skill creation/refinement (defaults to planner)
```

This allows: "use Opus for autonomous reasoning, Sonnet for email drafting, Flash for triage extraction" — similar to OpenClaw's planner/executor model split but with finer granularity.

**Slash command:**
```
/model autonomous opus       Set autonomous agent model to Opus
/model triage flash          Set triage model to Flash
/model draft sonnet          Set draft model to Sonnet
/model                       Show all model assignments
```

### 7.4 Integrate slash commands into all interfaces

**Discord bot:** `src/cognitex/discord_bot/__main__.py` — parse messages starting with `/` through the SlashCommandRegistry before sending to agent

**Web dashboard:** `src/cognitex/web/app.py` — parse chat input through SlashCommandRegistry; show command suggestions on `/` keypress

**CLI:** `src/cognitex/cli/main.py` — in shell mode, parse through SlashCommandRegistry

### 7.5 Skill-based slash commands

When a skill has `user-invocable: true` in its frontmatter (the AgentSkills default), register it as a slash command:

```yaml
---
name: meeting-prep
description: Compile context pack for upcoming meeting
user-invocable: true
---
```

This becomes available as `/meeting-prep [args]`. The command loads the full SKILL.md, formats it into a prompt, and runs it with the specified arguments.

### 7.6 Settings page for model routing

**File:** `src/cognitex/web/templates/settings.html` (extend)

Add a "Model Routing" section to the settings page showing:
- Current provider and models for each task role
- Dropdown selectors for each role
- Quick-switch buttons for common configurations (e.g., "All Claude", "All Gemini", "Hybrid: Claude reasoning + Gemini execution")

### Acceptance criteria
- [ ] `/model sonnet` switches active model immediately
- [ ] `/model autonomous opus` sets per-task model override
- [ ] `/status` shows current model config, mode, pending items
- [ ] `/skills` lists installed skills with eligibility
- [ ] Slash commands work in Discord, web chat, and CLI shell
- [ ] Skills with `user-invocable: true` registered as slash commands
- [ ] Settings page shows model routing configuration
- [ ] Existing tests pass

---

---

## WP8: AGENTMAIL INTEGRATION (3-4 days)

**Branch:** `wp8-agentmail`
**Goal:** Replace the fragile Gmail OAuth integration with AgentMail's API-first email infrastructure, giving the agent its own inbox separate from your personal/NHS email.

### Why this matters

The current Gmail integration (`services/gmail.py` + `services/google_auth.py`) uses Google OAuth2 with client secrets, manual consent flow, and token refresh. This has been unreliable. Google's rate limits, complex OAuth, and token expiry cause operational failures. Additionally, the direct Gmail connection creates governance risk: the agent has read/modify access to your actual inbox, including clinical emails.

AgentMail (agentmail.to) solves both problems:
- **API key auth** — no OAuth dance, no token refresh, no client secrets
- **Separate inbox** — agent gets its own address (e.g., `cognitex@agentmail.to` or custom domain), your Gmail stays untouched
- **Webhooks + WebSockets** — real-time email arrival notification (replaces Gmail Pub/Sub)
- **Structured extraction** — emails arrive as parsed JSON with built-in semantic search
- **Clinical boundary enforcement** — NHS email never touches the agent. Only forwarded non-clinical email reaches the AgentMail inbox.

Python SDK: `pip install agentmail`
Docs: https://docs.agentmail.to
Pricing: Free tier (3 inboxes, 3K emails), Developer $20/mo (10 inboxes, 10K emails)

### Architecture

```
Your Gmail ──[forwarding rule]──→ cognitex@agentmail.to
                                        │
                                        ▼
                                  AgentMail API
                                        │
                        ┌───────────────┼───────────────┐
                        ▼               ▼               ▼
                   Webhook         REST polling     WebSocket
                   (real-time)     (batch sync)     (live feed)
                        │               │               │
                        └───────────────┼───────────────┘
                                        ▼
                              Clinical Firewall (WP2)
                                        ▼
                              Cognitex Ingestion Pipeline
                                        ▼
                              Knowledge Graph (Neo4j)
```

**Email flow:**
1. Gmail forwarding rule forwards non-clinical email to `cognitex@agentmail.to` (manual setup, filtering at Gmail level before agent sees anything)
2. AgentMail receives email, stores it, triggers webhook
3. Cognitex webhook handler fetches full email via API
4. Clinical firewall (WP2) scans as second line of defence
5. Email enters existing ingestion pipeline (classification, task extraction, graph storage)

**Draft flow (agent → outbound):**
1. Autonomous agent generates draft via existing DRAFT_EMAIL action
2. Draft saved to AgentMail (not sent)
3. Operator reviews in web dashboard
4. Operator approves → sends via AgentMail API
5. Reply goes from `cognitex@agentmail.to`, not operator's personal Gmail

### 8.1 Add AgentMail service

**New file:** `src/cognitex/services/agentmail.py`

```python
"""AgentMail integration — API-first email for the agent.

Replaces direct Gmail API access with AgentMail's cleaner API.
Docs: https://docs.agentmail.to
"""

from agentmail import AgentMail

class AgentMailService:
    def __init__(self, api_key: str, inbox_id: str):
        self.client = AgentMail(api_key=api_key)
        self.inbox_id = inbox_id

    async def get_messages(self, limit: int = 50, label: str | None = None) -> list[dict]: ...
    async def get_message(self, message_id: str) -> dict: ...
    async def get_threads(self, limit: int = 20) -> list[dict]: ...
    async def send_message(self, to: str, subject: str, body: str, thread_id: str | None = None) -> dict: ...
    async def create_draft(self, to: str, subject: str, body: str, thread_id: str | None = None) -> dict: ...
    async def send_draft(self, draft_id: str) -> dict: ...
    async def search(self, query: str) -> list[dict]: ...
    async def apply_label(self, message_id: str, label: str) -> None: ...
```

### 8.2 Add config settings

**File:** `src/cognitex/config.py`

```python
agentmail_enabled: bool = Field(default=False, description="Use AgentMail instead of direct Gmail API")
agentmail_api_key: SecretStr = Field(default=SecretStr(""), description="AgentMail API key")
agentmail_inbox_id: str = Field(default="", description="AgentMail inbox ID")
agentmail_webhook_secret: SecretStr = Field(default=SecretStr(""), description="Webhook verification secret")
```

### 8.3 Add webhook endpoint

**File:** `src/cognitex/api/routes/webhooks.py` (new)

POST `/api/webhooks/agentmail` — verify signature, parse event, fetch full message, pass through clinical firewall, feed into ingestion pipeline.

### 8.4 Create email provider abstraction

**New file:** `src/cognitex/services/email_provider.py`

```python
"""Email provider abstraction — supports Gmail and AgentMail.

When agentmail_enabled=True, routes through AgentMail.
When False, falls back to existing Gmail integration.
Allows gradual migration without breaking existing functionality.
"""

class EmailProvider:
    def __init__(self):
        settings = get_settings()
        if settings.agentmail_enabled:
            self._provider = AgentMailProvider(...)
        else:
            self._provider = GmailProvider(...)  # Wraps existing gmail.py
    # Unified interface: get_messages, send_draft, search, etc.
```

### 8.5 Update email references

Update these files to use `EmailProvider` instead of direct `GmailService`:
- `src/cognitex/services/ingestion.py`
- `src/cognitex/agent/autonomous.py` (email drafting)
- `src/cognitex/agent/tools.py` (email tools)
- `src/cognitex/cli/main.py` (sync commands)

### 8.6 Setup procedure

```bash
pip install agentmail
# Create account at console.agentmail.to, get API key
# Create inbox via API or console
# Set up Gmail forwarding rule (exclude clinical/NHS patterns)
# Configure: AGENTMAIL_ENABLED=true, AGENTMAIL_API_KEY=..., AGENTMAIL_INBOX_ID=...
# Register webhook: https://your-server/api/webhooks/agentmail
```

### Acceptance criteria
- [ ] AgentMail inbox receives forwarded emails
- [ ] Webhook triggers ingestion pipeline on new email
- [ ] Clinical firewall scans AgentMail content before LLM processing
- [ ] Agent creates drafts in AgentMail for operator review
- [ ] Sent emails go from agent inbox, not operator's Gmail
- [ ] `agentmail_enabled=false` falls back to existing Gmail flow
- [ ] Existing Gmail integration not removed (backward compatible)

---

## WHAT NOT TO BUILD YET

- **Two-Track Day Plan** (Phase 3 P2.11) — useful but not essential until core integration stable
- **Research Workflow** (Phase 3 P3) — claim ledger, citation management, reviewer manager. High value but high complexity. Park until agent integration proven.
- **Multi-Timescale Budgets** (Phase 4 P4.17) — needs more usage data
- **n8n integration** — Cognitex's autonomous agent loop replaces n8n heartbeats
- **Full ClawHub publishing pipeline** — defer until skills are stable and proven useful

---

## RISK REGISTER

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| FM-1: Overbuilding before proving value | High | High | Strict WP sequencing. WP1+WP2 must run before WP3. |
| Clinical data leak via Gmail sync | Medium | Critical | WP2 is Week 1, non-negotiable. Pre-LLM filter. |
| Skills evolution generates noise | Medium | Medium | All proposals require approval. Throttle to 1 per 10 cycles. |
| Code proposals introduce bugs | Low | High | Never auto-execute. Diff review in dashboard. |
| Community skills inject unsafe behaviour | Medium | High | Clinical firewall applies to all content regardless of source. Skill review before enabling. |
| AgentSkills format drift | Low | Medium | Pin to current spec. Format is simple YAML + markdown. |
| Token budget from many skills | Medium | Medium | Compact XML summary list; full SKILL.md on-demand. Cap ~20 active skills. |
| Bootstrap file bloat consuming context | Medium | Medium | Safety rules injected fully; other files summarised. Token budget monitoring. |
| Slash command conflicts with agent chat | Low | Low | Commands parsed before LLM — clear `/` prefix. Unknown commands passed to agent. |
| Model routing complexity | Medium | Medium | Sensible defaults (task models default to planner/executor). Override only when needed. |
| AgentMail vendor dependency | Medium | Medium | Email provider abstraction layer (WP8.4) means Gmail fallback always available. No lock-in. |
| Gmail forwarding rule lets clinical email through | Low | Critical | Two-layer defence: Gmail filter (first line) + clinical firewall WP2 (second line). Both must fail for leak. |

---

## TESTING STRATEGY

Each WP should include:
1. **Unit tests** for new modules (clinical firewall patterns, skill parser, slash command dispatch)
2. **Integration tests** for modified pipelines (email processing with firewall, agent prompt with bootstrap)
3. **Manual verification** documented in PR description

Test files go in `tests/` following existing patterns (`tests/conftest.py` exists).

---

## DEPLOYMENT CHECKLIST

Before merging each WP:
- [ ] All existing tests pass: `pytest tests/`
- [ ] New tests pass
- [ ] No breaking changes to existing CLI commands
- [ ] No breaking changes to web dashboard
- [ ] No breaking changes to Discord bot
- [ ] Docker compose still works: `docker-compose up -d`
- [ ] Config changes documented (new env vars, new settings)
- [ ] Default files created on first boot if missing

---

## CONTEXT FOR THE OPERATOR

Chris is a Consultant Physician in Endocrinology who uniquely combines clinical practice with hands-on ML development. He writes his own code. He has neurodivergent traits and benefits from structured approaches over open-ended questions. The system must work even when the operator doesn't show up for a daily check-in — routine is the one thing that can't be relied on.

**NHS/Caldicott compliance is non-negotiable.** NHS email must never be connected to this pipeline. Clinical data must never reach the LLM context window. The clinical firewall is not optional.

**Key projects in flight:** ASCENDgpt (clinical LM for EHR), CAIRN (conversational AI for NHS 24), ValidAct (clinical decision support), PRIMA-HD, ARTEMIS.

**Infrastructure:** Remote server (srv1055607), titan-kre (GPU compute), Docker-based deployment, multiple Python environments.
