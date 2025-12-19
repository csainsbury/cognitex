Section A - General comments:

Cognitex already has the right skeleton for an academic-grade assistant: persistent context across Gmail/Calendar/Drive/GitHub, hybrid graph+vector memory, and risk-stratified execution with an approval queue plus decision traces for learning.  ￼  ￼  ￼ The missing pieces are mostly “research substrate” features: citation integrity, evidence accounting, writing pipeline integration, and mechanisms that prevent the system from becoming a confident bullshitter.

Literature substrate and citation integrity
	1.	First-class reference manager integration
Cognitex indexes Drive documents and chunks them, but it does not treat “a paper” as a canonical bibliographic object with DOI, authors, venue, year, BibTeX/CSL, citation graph, and a stable identifier beyond a Drive file ID.  ￼
Feature gap: a LiteratureItem node type with DOI/arXiv/PubMed IDs, plus edges CITES, SUPPORTS, CONTRADICTS, EXTENDS.
Why it matters: academic work is anchored in citation objects, not files.
	2.	Page-line anchored provenance
Chunk-level retrieval is good, but academics need “page 7, paragraph 2, table 1 row 3” anchors, especially when you later draft grant text or rebut a reviewer. Current chunking is character-based with overlap.  ￼
Feature gap: immutable “span anchors” into PDF text extraction and (ideally) coordinates for PDF page regions.
	3.	Annotation ingestion as a data source
Highlights, margin notes, and your own summaries are often more valuable than the paper text. Cognitex currently indexes content and does “deep semantic analysis” on a limited subset, but it does not explicitly model user annotations as privileged evidence.  ￼
Feature gap: Annotation entities linked to LiteratureItem and to your projects, with timestamps and confidence.

Evidence accounting and anti-hallucination mechanics
	4.	Claim ledger
This is the big missing primitive. A claim ledger is a table of atomic statements, each with supporting citations, contradiction flags, and uncertainty. Cognitex has decision traces for actions, not for epistemic claims.  ￼
Feature gap: Claim nodes with fields {statement, scope, population, setting, effect direction/size if applicable, confidence}, edges SUPPORTED_BY (to spans/annotations) and USED_IN (to drafts, grants, slides).
	5.	Disagreement and contradiction detection
Research is adversarial by nature. The system needs an explicit representation of disagreements so you can reason about them without losing track. Current graph inference focuses on people/work relationships and communication patterns.  ￼  ￼
Feature gap: detect and store CONTRADICTS relations between claims, not just between documents.
	6.	Evidence grading
Academics and clinicians think in evidence hierarchies. The tool needs a built-in mechanism to grade evidence strength, not just retrieve it.
Feature gap: EvidenceGrade attached to Claim, with schemas compatible with GRADE-style reasoning in biomedical contexts.
	7.	“No-source, no-say” mode
You already have risk stratification for actions.  ￼
Feature gap: an epistemic risk stratification for assertions. Example tiers:

	•	Tier 0: exact quote with anchor
	•	Tier 1: paraphrase with anchors
	•	Tier 2: synthesis across anchors
	•	Tier 3: speculative hypothesis, clearly labelled
This prevents accidental laundering of model priors into your knowledge base.

Writing pipeline integration
	8.	Draft objects as first-class graph entities
Cognitex has tasks/projects/goals, and it can draft emails and create events with approval.  ￼  ￼
Feature gap: Draft nodes for “grant section”, “paper methods”, “response to reviewers”, “abstract”, each linked to Claim nodes and citations, with versioning.
	9.	Citation-aware drafting and formatting
Not “write prose”, but “write prose with valid citations that round-trip into Word/Overleaf”.
Feature gap: CSL/BibTeX output and a tool that emits citekeys plus a generated .bib subset per draft.
	10.	Consistency checking across documents
Academics die by inconsistency: sample sizes differ between abstract and methods, outcomes mutate between protocol and paper, variable names drift between code and manuscript. GitHub indexing exists, but there’s no explicit cross-artifact consistency engine.  ￼
Feature gap: consistency checks that traverse Draft ↔ Code ↔ Tables/Figures ↔ Claims.
	11.	Reviewer-response management
Feature gap: represent each reviewer comment as an object, link to the exact edits, link to supporting evidence, track status. This maps tightly to your existing task system but requires dedicated structure.

Research execution substrate
	12.	Experiment and analysis registry
GitHub indexing helps retrieval.  ￼
Feature gap: “runs” as objects: dataset snapshot, code commit hash, environment, parameters, outputs, metrics, figures. Without this, the assistant cannot reliably answer “what produced figure 2” six months later.
	13.	Data dictionary and cohort definition objects
Clinical/epi work needs durable cohort specs, variable definitions, and phenotypes. Feature gap: CohortDefinition nodes and VariableDefinition nodes, linked to code, drafts, and claims.
	14.	Computational resource awareness
You already model “energy impact” for calendar events as cognitive load.  ￼
Feature gap: compute load as a planning constraint for research runs, tied into scheduling and task planning.

Active memory shaping for researchers
	15.	“Research state” snapshots
Cognitex has episodic memory and decision traces.  ￼
Feature gap: periodic “state snapshots” per project: current hypothesis, key claims, open questions, next analyses, blockers, and who/what depends on what. This prevents the slow entropy drift that kills multi-strand programmes.
	16.	Idea ledger with decay and testing hooks
Cross-domain idea generation becomes useful only when ideas are tracked, pruned, and tested.
Feature gap: Idea nodes with predicted value, required evidence, falsification tests, and expiry. Otherwise the system becomes an idea fountain that you never drink from.

Safety, governance, and robustness for academic use
	17.	Reproducible prompt+tool traces for intellectual audit
You capture decision traces for actions and approvals.  ￼
Feature gap: analogous traces for knowledge work outputs, especially syntheses and drafts: retrieved spans, intermediate reasoning artifacts, and final text mappings.
	18.	Tests and evaluation harness for research tasks
Your paper already lists limited automated testing as a limitation.  ￼
Feature gap: a fixed benchmark suite of your own recurring academic tasks with regression testing across model/version changes.
	19.	Per-project privacy and compartmentalisation
Granular privacy controls are listed as future work.  ￼
Feature gap: project-level “knowledge firewalls” so sensitive drafts, embargoed results, or reviewer comments cannot bleed into other contexts.

Opinion: the single highest-leverage missing feature is the claim ledger plus anchored provenance. Everything else becomes easier once the system has a native representation of “what is believed, why it is believed, and exactly where that belief came from”.  ￼  ￼

--

Section B - Feature Suggestions:

What’s missing is not more “features” in the usual sense. It’s the decision machinery that turns your graph + tasks + calendar into a minute-by-minute policy that reliably chooses what to do next, prepares the right context pack, and keeps the whole thing aligned across timescales without you babysitting it.

1) A formal decision policy that can arbitrate conflicts

Right now you have representations (tasks/projects/goals) and execution scaffolding (approval tiers). What’s missing is an explicit choice rule.

You need a utility function (or equivalent) that can trade off:
	•	deadline pressure (hard vs soft)
	•	long-horizon goal progress (career / family / health)
	•	project critical paths (blocking dependencies)
	•	cognitive state cost (deep work vs admin)
	•	risk/irreversibility (sending an email, committing to a meeting)
	•	opportunity value (rare windows: collaborator availability, bursts of motivation)

Without this, the system will either be reactive (inbox-driven) or brittle (over-planned). The core object here is: “Given the next N hours, what is the best next action right now?” computed repeatedly, not assumed.

Opinion: this is the single most important missing component.

2) State estimation of “you, right now”

Minute-to-minute support depends on knowing your current operating mode. You already gesture at “energy impact” and cognitive load. The missing piece is a continuously updated latent-state model that infers:
	•	attention bandwidth (focused vs fragmented)
	•	fatigue trajectory (not just current tiredness; slope matters)
	•	stress/avoidance signatures (procrastination detection)
	•	context constraints (location, device, connectivity, childcare windows)
	•	available block length (true uninterrupted time, not calendar time)

Then the planner uses that state to select tasks with the right “activation energy.” Otherwise it will keep proposing “write grant section” at times when you only have 12 jagged minutes and a brain full of bees.

3) Lookahead planning with slack, not just scheduling

Calendars are deterministic; life isn’t. The missing capability is stochastic planning:
	•	buffers automatically inserted around meetings/travel/context switching
	•	detection of overcommitment using realistic task durations (and uncertainty)
	•	“if this slips by 30 minutes, what breaks?” analysis
	•	dynamic re-planning when interruptions occur, without collapsing into chaos

This is where most productivity systems die: they don’t model variance, so they create fragile plans that generate self-disgust when reality happens.

4) Just-in-time “context pack compiler” for every event/task

You described the goal precisely: have everything pre-extracted, summarised, drafted at the time it’s needed. The missing feature is a dedicated pipeline that treats each upcoming commitment as a build artifact.

For a calendar event (or a task start time), compile a pack containing:
	•	purpose/objectives (one line, explicit)
	•	last-touch recap (what happened last time; what’s pending)
	•	relevant threads/docs/code links (ranked)
	•	pre-written drafts (emails, agenda, messages, update notes)
	•	a “decision list” (what you must decide/ask during the event)
	•	a “risk list” (what could go wrong; what must not be forgotten)
	•	post-event follow-ups pre-created as tasks with owners and deadlines

This needs to run on a timer (e.g., T-24h, T-2h, T-15m) and refresh based on new email/Slack/Drive changes.

5) A dependency-aware project spine (critical path, not just lists)

Tasks/projects/goals aren’t enough. Research work is a dependency graph:
	•	“can’t do B until A exists”
	•	“C is blocked by reviewer response from X”
	•	“D must happen before the meeting on Tuesday”
	•	“E unlocks 3 downstream tasks”

Missing feature: critical-path computation per project + cross-project arbitration so the system knows what actually moves the world versus what just feels busy.

6) A motivation model that treats avoidance as first-class data

You said: “consider all the different levels of motivation at all times.” That implies the system must model misalignment between stated goals and observed behaviour without moralising and without giving up.

Missing feature set:
	•	avoidance detectors (repeated deferral patterns, inbox-fleeing, tool-hopping)
	•	“micro-commitments” as planned interventions (5-minute entry tasks that reliably start the engine)
	•	escalation logic: when deep work fails repeatedly, the system automatically switches to preparatory sub-tasks that reduce resistance (collect refs, outline, open notebook, draft headings, precompute figure skeleton)
	•	reward scheduling that is reality-based (not gamification; actual replenishment blocks)

Opinion: if you don’t explicitly model avoidance, the system will optimise for “urgent easy things” because that’s what the data will show.

7) An interruption firewall and context switching manager

Minute-to-minute support is mostly interruption management.

Missing features:
	•	focus modes that gate inbox/notifications based on current plan state (with safe exceptions)
	•	batching rules (email windows, admin windows)
	•	automatic triage deferral: “capture and park” incoming requests into the right project lane with a suggested next action and a scheduled revisit time
	•	context switch cost accounting: the system penalises task sequences that thrash attention

8) Closed-loop execution monitoring (plan vs reality) with learning

You have decision traces for actions. What’s missing is the control loop:
	•	predicted duration vs actual duration tracking
	•	reasons for deviation (interruption, fatigue, underestimated scope)
	•	parameter updates (your personal speed on “grant writing”, “email backlog”, “coding”, “supervision”)
	•	drift detection: “your weekly plan is consistently overfilled by 30%”

This turns the planner from aspirational to calibrated.

9) Multi-timescale governance: daily/weekly/quarterly coordination

Task-level optimisation will destroy goal-level intent unless you enforce review cycles.

Missing features:
	•	daily “contract”: top priorities, hard constraints, and one explicit “not doing today”
	•	weekly reconciliation: commitments vs goals vs reality, with automatic goal re-weighting based on time actually available
	•	quarterly goal allocation: explicit budgets (hours/week) per life domain, treated as constraints in scheduling

Opinion: without budgets, goals are just decorative text that gets eaten by the urgent.

10) A “policy layer” for values and guardrails

This is how you stop the system from turning you into a hyper-efficient husk.

Missing features:
	•	hard guardrails (family blocks, sleep minimums, no-meetings zones)
	•	soft preferences (avoid late meetings, protect morning deep work)
	•	ethical constraints (what the system must never do autonomously)
	•	reversible vs irreversible actions classification (tied to approval tiers)

This is the alignment layer between long-term goals and short-term behaviour.

11) A readiness model for meetings and commitments

Not just “meeting exists,” but “meeting is prepared enough.”

Missing feature: readiness scoring per event:
	•	pre-reads reviewed?
	•	agenda drafted?
	•	decision points identified?
	•	required materials available offline?
	•	drafts queued?

The system schedules prep tasks automatically to hit a readiness threshold, rather than hoping you’ll “get around to it.”

12) A failure mode framework: graceful degradation

When the system is wrong, it must fail in predictable ways.

Missing features:
	•	“degraded mode” when data feeds break (calendar/email permissions, outages)
	•	conservative mode under uncertainty (fewer autonomous actions, more capture)
	•	explicit uncertainty reporting for recommendations (not vibes; quantified confidence bands where possible)

Opinion: reliability beats cleverness for a minute-to-minute exoskeleton.

Net: the missing core is a control system: state estimation → lookahead planning → just-in-time context compilation → execution monitoring → learning, all under a values/policy layer that arbiters conflicts across task/project/goal timescales.

--

Section C - neurodivergent focus:

Neurodivergence changes the design target from “optimal planning” to “optimal execution under variable friction.” The same plan can be either effortless or impossible depending on state, context, and stimulus load. Build Cognitex to treat that variability as the primary reality, not an edge case.

1) Replace “priority” with “activation energy”

Neurodivergent failure mode: you know the top priority and still cannot start.

Add a first-class property on every task: Start Friction (0–5) plus Minimum Viable Start (MVS).
	•	MVS is the smallest action that counts as “started” (open file + write heading; run a notebook cell; paste reviewer comment into template).
	•	Planner chooses by: value × urgency ÷ friction, constrained by available block length and current state.
	•	If a task fails to start twice, the system auto-demotes it into prep tasks until friction drops.

Opinion: this matters more than any clever long-horizon optimizer.

2) Make “state” explicit and operational

Neurodivergent day-to-day is stateful: focus, overload, avoidance, hyperfocus.

Define 5–7 discrete operating modes, each with default rules:
	•	Deep Focus: protect, block interruptions, only deep tasks.
	•	Fragmented: short tasks only, batching, context packs.
	•	Overloaded: reduce inputs, only maintenance + recovery.
	•	Avoidant: micro-commitments, prep tasks, external prompts.
	•	Hyperfocus: hard stop rails, hydration/food prompts, time boxing.

Planner outputs must always be conditioned on current mode. Otherwise it will repeatedly recommend actions you cannot execute.

3) Aggressive interruption firewall

Neurodivergent cost of context switching is often catastrophic.

Implement:
	•	Inbound capture without engagement: messages get triaged into a queue with a one-line suggested next action, but you are not pulled into them.
	•	Notification gating tied to mode and block type.
	•	Scheduled inbox windows with auto-generated “reply drafts” ready at the start of the window.

This turns the world from a live firehose into a batched workstream.

4) Time blindness countermeasures

Many neurodivergent people misperceive time and transitions.

Add:
	•	Pre-start ramps: “T-10 minutes: open docs; T-2: agenda; T-0: join link.”
	•	Transition buffers between meetings and task blocks (default 5–15 minutes).
	•	Duration realism model that learns your actual pace, not ideal pace.
	•	Hard stop mechanism when hyperfocus threatens the next commitment.

5) Working memory externalization as a primary goal

The cognitive tax is “holding the whole project in head.”

Per task/event, compile a single screen context pack:
	•	objective (one line)
	•	current status (one paragraph)
	•	next 3 actions
	•	blockers
	•	required links/artifacts
	•	draft outputs (email, agenda, code snippet placeholders)
	•	“what not to forget” list

Neurodivergent-friendly detail: keep the pack stable. Frequent reformatting becomes friction.

6) Scripts for socially ambiguous or high-friction communications

Email/Slack/meeting dynamics can be disproportionately draining.

Add:
	•	response templates (decline, defer, ask for clarification, request deadline, set boundary, follow-up, summarize decisions)
	•	meeting scripts (opening, agenda, decision check, action assignment, close)
	•	tone control as a dial (direct / neutral / warm) with predictable outputs

This converts social ambiguity into a repeatable protocol.

7) Anti-avoidance design without moralism

Avoidance is not laziness; it is often threat response to ambiguity, scale, or uncertain reward.

Implement:
	•	ambiguity reduction steps as defaults: “define success; list inputs; choose first action; set timer.”
	•	binary commit prompts inside the system: commit to 5 minutes or consciously defer with a scheduled revisit and reason captured.
	•	automatic decomposition when a task is deferred repeatedly.

Capture “reason for deferral” as structured data (fatigue, unclear, too big, missing input, dread) and use it to adjust planning.

8) Sensory and cognitive load management

UI design becomes neurological ergonomics.

Rules:
	•	low visual density, minimal simultaneous stimuli
	•	stable layout, consistent controls
	•	limit choices to 1–3 “next actions”
	•	one canonical inbox per category (work, home, research) to prevent scatter
	•	dark mode and typography choices that reduce strain

Opinion: cluttered dashboards are productivity cosplay; they punish neurodivergent cognition.

9) Hyperfocus harness with guardrails

Hyperfocus is a superpower with a crash landing.

Add:
	•	hyperfocus contracts: allowed window + stop conditions + “parking note” generated automatically to preserve context for later restart.
	•	interruption exceptions: only truly urgent items pierce the bubble.
	•	re-entry support: after interruption, the system restores the exact state (files, notes, “next line to write”).

10) Energy budgeting across life domains

Neurodivergent burnout often comes from overcommitting cognitive energy, not time.

Track:
	•	cognitive cost of tasks (deep reasoning, social, admin, sensory exposure)
	•	recovery requirements (short walk, quiet time, low-demand block)
	•	enforce minimum recovery like a hard constraint, not optional advice

11) “Two-track day” planning

Many neurodivergent days bifurcate: either high-functioning or barely-functional.

Plan two versions automatically:
	•	Plan A: normal capacity.
	•	Plan B: minimum viable day that protects critical commitments and preserves future function.

This prevents the common failure cascade: one disruption → whole plan collapses → self-reproach → worse execution.

12) Reduce the number of “places where decisions happen”

Executive dysfunction worsens when the system asks you to decide repeatedly.

Design principle: default everything, allow override rarely.
	•	default block types
	•	default prep windows
	•	default meeting pack generation
	•	default triage rules
	•	default “next action” selection

The system should feel like rails, not a cockpit.

Net: neurodivergent optimization is not “more intelligence,” it’s “less friction, fewer decisions, tighter guardrails, and state-aware planning.”