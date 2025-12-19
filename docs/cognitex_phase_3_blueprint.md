Cognitex Phase 3 (Next-phase) integrated modifications and features

P0 Foundations
	1.	Unified object model (single spine)

	•	Canonical entities: Person, Project, Goal (multi-timescale), Task, Event, Draft, LiteratureItem, Claim, DecisionTrace, ContextPack, StateSnapshot, Run/Experiment, Cohort/VariableDefinition.
	•	Mandatory IDs and cross-links so everything is addressable and composable (no “just text in a note”).

	2.	Span-anchored provenance

	•	Every extracted fact, summary sentence, and draft paragraph maps to immutable anchors (document → page → span).
	•	Store both: (a) extracted text span and (b) a stable locator.

	3.	Claim ledger

	•	Atomic claims with fields: statement, scope, confidence, evidence grade, supported_by spans, contradicts links, used_in drafts.
	•	Enforce “no-source, no-say” tiers for any output that pretends to be factual.

⸻

P1 The minute-to-minute control system
	4.	State estimation of “you, right now”

	•	Discrete modes (e.g., Deep Focus, Fragmented, Overloaded, Avoidant, Hyperfocus) with deterministic rules.
	•	Continuous signals: available block length, interruption pressure, fatigue slope, time-to-next-hard-commitment.
	•	Mode-aware task selection and UI simplification.

	5.	Decision policy (explicit arbitration)

	•	A scored utility function that selects the next action repeatedly using:
	•	urgency/deadlines
	•	critical-path impact and blockers
	•	long-horizon goal budgets
	•	start friction and cognitive cost
	•	risk/irreversibility (ties into approval tiers)
	•	context-switch penalties
	•	Outputs: 1–3 next actions, not a buffet.

	6.	Activation energy model (neurodivergent-first execution)

	•	Per task: Start Friction (0–5) + Minimum Viable Start (MVS) + “prep ladder” auto-generated.
	•	Failure-to-start logic: repeated deferrals trigger decomposition until MVS becomes trivial.

	7.	Lookahead planning with slack

	•	Schedule buffers and transition ramps as default, not optional.
	•	Uncertainty-aware duration estimates that learn your actual pace.
	•	Automatic re-plan on disruption without collapsing the day.

	8.	Interruption firewall

	•	Mode-gated notifications.
	•	Inbound capture without engagement (messages triaged into queues with suggested next action).
	•	Fixed inbox windows with prebuilt reply drafts.

⸻

P2 Just-in-time “context pack compiler”
	9.	ContextPack as a build artifact

	•	For every upcoming event and selected task, compile a single-screen pack:
	•	objective (one line)
	•	last-touch recap
	•	required links/artifacts (ranked)
	•	decision list + “don’t forget”
	•	pre-drafted agenda / emails / messages
	•	readiness score and missing prerequisites
	•	Build schedule: T–24h, T–2h, T–15m, plus rebuild on new relevant inputs (email/doc changes).

	10.	Readiness scoring + auto-prep tasks

	•	Each meeting/task has a readiness threshold.
	•	System schedules micro-prep tasks to hit the threshold (or intentionally accepts “unprepared” with consequences made explicit).

	11.	Two-track day plan

	•	Plan A (normal capacity) and Plan B (minimum viable day).
	•	Plan B protects critical commitments and preserves future function; activated automatically when overload signals rise.

⸻

P3 Research-grade workflow (only the parts that matter minute-to-minute)
	12.	Draft objects with citation-aware output

	•	Draft nodes for grant sections, papers, rebuttals, slides.
	•	Each paragraph linked to Claim nodes; exports produce valid citekeys and BibTeX/CSL subsets.

	13.	Consistency checker across artifacts

	•	Detect drift between abstract/methods/tables/code/protocol.
	•	Flag mismatched sample sizes, variable definitions, outcome names, and version inconsistencies.

	14.	Reviewer-response manager

	•	Reviewer comment objects → linked edits → linked claims/evidence → status tracking.
	•	Generates response scaffolds and ensures each claim is backed by anchors.

	15.	Experiment/analysis registry

	•	Run objects: dataset snapshot, commit hash, params, environment, outputs, metrics, figures.
	•	Enables “what produced figure 2?” retrieval with zero archaeology.

⸻

P4 Learning loop and governance
	16.	Closed-loop calibration (plan vs reality)

	•	Track predicted vs actual duration, interruption causes, deferral reasons.
	•	Update personal pace priors and friction estimates automatically.
	•	Detect systematic overfill (e.g., weekly plan exceeds real capacity by X%).

	17.	Multi-timescale budgets and reconciliation

	•	Explicit hour/energy budgets per domain (research, clinical, family, health, admin).
	•	Daily contract (top priorities + one “not doing today”).
	•	Weekly reconciliation that adjusts goal weights based on reality, not aspiration.

	18.	Policy layer (values + guardrails)

	•	Hard constraints: family blocks, sleep minimums, no-meeting zones.
	•	Soft preferences: protect morning deep work, avoid late meetings.
	•	Irreversible actions require stricter approval; reversible actions can be faster.

	19.	Graceful degradation modes

	•	If connectors fail or data is missing: conservative mode, fewer autonomous actions, more capture and scheduling.
	•	Explicit uncertainty reporting for recommendations and syntheses.

⸻

Implementation order (dependency-aware)
	1.	Unified object model + provenance anchors + claim ledger
	2.	State model + decision policy + activation energy/MVS + interruption firewall
	3.	Context pack compiler + readiness scoring + slack planning + two-track day
	4.	Draft/citation pipeline + consistency checking + reviewer manager
	5.	Run registry + calibration learning loop + budgets/policy + graceful degradation