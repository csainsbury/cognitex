# Cognitex Phase 4: Adaptive Memory & Learning System

**Created:** 2026-01-02
**Status:** Blueprint

This document defines the implementation plan for closing the learning loop in Cognitex. The system has strong foundations for capturing decisions, feedback, and behavioral data - but lacks the machinery to turn observations into adaptations.

---

## Overview

### Current State (What Exists)

| Component | Location | Status |
|-----------|----------|--------|
| Decision Traces | `agent/decision_memory.py` | Full context/action/feedback with embeddings |
| Task Proposals | `agent/action_log.py` | Accept/reject tracking with reasons |
| Operating State | `agent/state_model.py` | Modes, friction, deferral counts |
| Working Memory | `agent/memory.py` | Redis 24hr context |
| Episodic Memory | `agent/memory.py` | PostgreSQL + pgvector long-term |
| Context Packs | `agent/context_pack.py` | JIT compilation with readiness scores |

### The Gap

Data is captured but not used to adapt behavior. The system is reactive, not predictive.

---

## Implementation Tiers

### Tier 1: Quick Wins (Use Existing Data)

These can be implemented with queries on existing tables - no new infrastructure needed.

#### 1.1 Proposal Learning
**Priority:** HIGH | **Effort:** Low | **Files:** `agent/autonomous.py`, `agent/action_log.py`

Learn from task proposal acceptance patterns to improve future proposals.

**Implementation:**
```python
# In action_log.py
async def get_proposal_patterns() -> dict:
    """Analyze proposal acceptance by category/project/source."""
    query = """
    SELECT
        details->>'project_id' as project_id,
        details->>'source' as source,
        COUNT(*) FILTER (WHERE status = 'approved') as approved,
        COUNT(*) FILTER (WHERE status = 'rejected') as rejected,
        COUNT(*) as total,
        ROUND(COUNT(*) FILTER (WHERE status = 'approved')::numeric /
              NULLIF(COUNT(*), 0) * 100, 1) as approval_rate
    FROM task_proposals
    WHERE decision_at IS NOT NULL
    GROUP BY details->>'project_id', details->>'source'
    HAVING COUNT(*) >= 3
    ORDER BY approval_rate DESC
    """
```

**Integration:**
- Before proposing a task, check historical approval rate for similar tasks
- If approval rate < 30% for this category, increase specificity or skip
- If approval rate > 80%, consider auto-approval (with config flag)
- Include approval rate context in proposal notifications

**Success Metric:** Proposal approval rate increases over time

---

#### 1.2 Deadline Completion Analysis
**Priority:** HIGH | **Effort:** Low | **Files:** `agent/graph_observer.py`, `services/tasks.py`

Track when tasks are completed relative to their deadlines.

**Implementation:**
```python
# In graph_observer.py
async def get_deadline_patterns(self) -> dict:
    """Analyze task completion timing relative to deadlines."""
    query = """
    MATCH (t:Task)
    WHERE t.completed_at IS NOT NULL AND t.due_date IS NOT NULL
    WITH t,
         duration.between(t.completed_at, t.due_date).days as days_before_deadline
    RETURN
        CASE
            WHEN days_before_deadline < 0 THEN 'late'
            WHEN days_before_deadline = 0 THEN 'day_of'
            WHEN days_before_deadline <= 1 THEN 'last_minute'
            WHEN days_before_deadline <= 3 THEN 'comfortable'
            ELSE 'early'
        END as timing,
        count(*) as count,
        collect(t.title)[0..3] as examples
    """
```

**Integration:**
- Surface patterns in weekly review ("80% of tasks completed day-of deadline")
- Use pattern to adjust reminder timing (if last-minute, remind earlier)
- Flag tasks at risk based on historical completion patterns
- Inform context pack urgency scoring

**Success Metric:** Reduction in late completions

---

#### 1.3 Deferral Prediction
**Priority:** MEDIUM | **Effort:** Medium | **Files:** `agent/state_model.py`, `agent/autonomous.py`

Predict which tasks the user is likely to defer based on patterns.

**Implementation:**
```python
# In state_model.py
@dataclass
class DeferralRisk:
    score: float  # 0-1 probability of deferral
    factors: list[str]  # Contributing factors

    @classmethod
    async def calculate(cls, task: dict) -> "DeferralRisk":
        factors = []
        score = 0.0

        # Factor 1: Task has been deferred before
        if task.get("deferral_count", 0) > 0:
            score += 0.3 * min(task["deferral_count"], 3) / 3
            factors.append(f"deferred {task['deferral_count']}x before")

        # Factor 2: Similar tasks often deferred (by project/type)
        project_deferral_rate = await get_project_deferral_rate(task.get("project_id"))
        if project_deferral_rate > 0.5:
            score += 0.2
            factors.append(f"project has {project_deferral_rate:.0%} deferral rate")

        # Factor 3: High friction tasks
        if task.get("start_friction", 0) >= 4:
            score += 0.2
            factors.append("high start friction")

        # Factor 4: No clear next step
        if not task.get("minimum_viable_start"):
            score += 0.15
            factors.append("no MVS defined")

        # Factor 5: Large estimated time
        if task.get("estimated_minutes", 0) > 120:
            score += 0.15
            factors.append("large time estimate")

        return cls(score=min(score, 1.0), factors=factors)
```

**Integration:**
- Auto-generate MVS for high-deferral-risk tasks
- Proactively decompose tasks before they get deferred
- Surface deferral risk in task prioritization
- Discord notification: "This task has 75% deferral risk - want me to break it down?"

**Success Metric:** Reduction in repeat deferrals

---

### Tier 2: Duration & Timing Learning

Requires tracking actual task timing, which needs minor schema additions.

#### 2.1 Duration Calibration
**Priority:** HIGH | **Effort:** Medium | **Files:** `services/tasks.py`, `agent/decision_memory.py`

Learn personal pace by comparing estimated vs actual duration.

**Schema Addition:**
```sql
-- Add to tasks table or create task_timing table
ALTER TABLE tasks ADD COLUMN started_at TIMESTAMP;
ALTER TABLE tasks ADD COLUMN actual_minutes INTEGER;

-- Or create dedicated timing table
CREATE TABLE task_timing (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    estimated_minutes INTEGER,
    actual_minutes INTEGER,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    interruption_count INTEGER DEFAULT 0,
    context TEXT,  -- e.g., "morning", "after_meeting", "fragmented"
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);
```

**Implementation:**
```python
# In services/tasks.py
async def get_duration_calibration() -> dict:
    """Calculate personal pace factors by task type."""
    query = """
    SELECT
        t.project_id,
        AVG(tt.actual_minutes::float / NULLIF(tt.estimated_minutes, 0)) as pace_factor,
        COUNT(*) as sample_size,
        STDDEV(tt.actual_minutes::float / NULLIF(tt.estimated_minutes, 0)) as variability
    FROM task_timing tt
    JOIN tasks t ON tt.task_id = t.id
    WHERE tt.estimated_minutes > 0 AND tt.actual_minutes > 0
    GROUP BY t.project_id
    HAVING COUNT(*) >= 3
    """

async def calibrate_estimate(task: dict) -> int:
    """Adjust task estimate based on personal pace."""
    base_estimate = task.get("estimated_minutes", 30)
    calibration = await get_duration_calibration()

    project_factor = calibration.get(task.get("project_id"), {}).get("pace_factor", 1.0)

    # Apply factor with dampening for high variability
    adjusted = int(base_estimate * project_factor)
    return adjusted
```

**Integration:**
- Auto-adjust estimates when displaying to user
- Use calibrated estimates in day planning
- Flag systematic underestimation: "You typically take 40% longer on research tasks"
- Improve schedule slack calculation

**Success Metric:** Estimated vs actual correlation improves (target: r > 0.7)

---

#### 2.2 Time-of-Day Preferences
**Priority:** MEDIUM | **Effort:** Medium | **Files:** `agent/state_model.py`, `agent/graph_observer.py`

Learn when different task types are most likely to be completed successfully.

**Implementation:**
```python
# In decision_memory.py
async def get_temporal_patterns() -> dict:
    """Analyze task completion by time of day and day of week."""
    query = """
    SELECT
        EXTRACT(HOUR FROM completed_at) as hour,
        EXTRACT(DOW FROM completed_at) as day_of_week,
        project_id,
        COUNT(*) as completions,
        AVG(CASE WHEN completed_at <= due_date THEN 1 ELSE 0 END) as on_time_rate
    FROM tasks
    WHERE completed_at IS NOT NULL
    GROUP BY hour, day_of_week, project_id
    HAVING COUNT(*) >= 2
    ORDER BY completions DESC
    """
```

**Neo4j Pattern Storage:**
```cypher
// Store learned temporal patterns
CREATE (p:TemporalPattern {
    project_id: $project_id,
    best_hours: [9, 10, 14],  // Hours with highest completion
    worst_hours: [16, 17],     // Hours with lowest completion
    best_days: [1, 2, 3],      // Mon-Wed
    sample_size: 45,
    last_updated: datetime()
})
```

**Integration:**
- Suggest task scheduling based on learned patterns
- Context packs factor in temporal fit
- "You complete research tasks 3x more often before noon"
- Decision policy weights temporal fit in utility function

**Success Metric:** Task completion rate increases when scheduled in preferred times

---

### Tier 3: Behavioral Prediction & Adaptation

These require more sophisticated analysis and model building.

#### 3.1 Procrastination Root Cause Analysis
**Priority:** MEDIUM | **Effort:** High | **Files:** `agent/decision_memory.py`, `agent/autonomous.py`

Understand WHY tasks are avoided, not just that they are.

**Schema Addition:**
```sql
CREATE TABLE deferral_analysis (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    deferred_at TIMESTAMP DEFAULT NOW(),
    inferred_reason TEXT,  -- 'unclear_next_step', 'too_large', 'boring', 'anxiety', 'dependency'
    context JSONB,  -- State at deferral time
    friction_at_deferral FLOAT,
    eventually_completed BOOLEAN DEFAULT FALSE,
    completion_trigger TEXT  -- What finally got it done: 'deadline', 'decomposition', 'energy_spike'
);
```

**Implementation:**
```python
# In decision_memory.py
class DeferralAnalyzer:
    REASON_SIGNALS = {
        "unclear_next_step": ["no_mvs", "vague_title", "no_description"],
        "too_large": ["high_estimate", "no_subtasks", "scope_creep"],
        "boring": ["repeated_deferral", "low_energy_task", "admin_category"],
        "anxiety": ["high_stakes", "external_deadline", "reviewer_involved"],
        "dependency": ["blocked_by", "waiting_for", "needs_input"],
    }

    async def analyze_deferral(self, task: dict, state: UserState) -> str:
        """Infer why a task was deferred."""
        signals = []

        if not task.get("minimum_viable_start"):
            signals.append("no_mvs")
        if task.get("estimated_minutes", 0) > 120:
            signals.append("high_estimate")
        if task.get("deferral_count", 0) >= 3:
            signals.append("repeated_deferral")
        # ... more signal detection

        # Match signals to reasons
        for reason, required_signals in self.REASON_SIGNALS.items():
            if any(s in signals for s in required_signals):
                return reason

        return "unknown"

    async def suggest_intervention(self, reason: str, task: dict) -> dict:
        """Suggest intervention based on deferral reason."""
        interventions = {
            "unclear_next_step": {"action": "generate_mvs", "prompt": "Break into first step"},
            "too_large": {"action": "decompose", "prompt": "Split into 3-5 subtasks"},
            "boring": {"action": "pair_with_reward", "prompt": "Combine with preferred task"},
            "anxiety": {"action": "reduce_stakes", "prompt": "Identify minimum viable outcome"},
            "dependency": {"action": "identify_blocker", "prompt": "What's blocking this?"},
        }
        return interventions.get(reason, {"action": "flag_for_review"})
```

**Integration:**
- Auto-apply interventions when deferral pattern detected
- Track which interventions work (task eventually completed)
- Surface insights: "Tasks deferred due to 'unclear next step' complete 80% faster after MVS generation"
- Feed successful intervention patterns back into proposal generation

**Success Metric:** Tasks with interventions complete at higher rate than control

---

#### 3.2 Interruption Analytics
**Priority:** MEDIUM | **Effort:** Medium | **Files:** `agent/interruption_firewall.py`, `agent/state_model.py`

Learn interruptibility patterns and recovery times.

**Schema Addition:**
```sql
CREATE TABLE interruption_events (
    id TEXT PRIMARY KEY,
    timestamp TIMESTAMP DEFAULT NOW(),
    source TEXT NOT NULL,  -- 'email', 'calendar', 'discord', 'slack'
    urgency TEXT,  -- 'low', 'medium', 'high', 'critical'
    mode_at_interrupt TEXT,  -- Operating mode when interrupted
    task_in_progress TEXT,  -- What was being worked on
    response_action TEXT,  -- 'engaged', 'batched', 'ignored'
    recovery_minutes INTEGER,  -- Time to return to original task
    context_lost BOOLEAN DEFAULT FALSE
);
```

**Implementation:**
```python
# In interruption_firewall.py
async def get_interruptibility_model() -> dict:
    """Learn personal interruptibility patterns."""
    query = """
    SELECT
        source,
        mode_at_interrupt,
        EXTRACT(HOUR FROM timestamp) as hour,
        AVG(recovery_minutes) as avg_recovery,
        COUNT(*) FILTER (WHERE context_lost) as context_losses,
        COUNT(*) as total
    FROM interruption_events
    GROUP BY source, mode_at_interrupt, hour
    HAVING COUNT(*) >= 3
    """

    # Build model: (source, mode, hour) -> expected_cost
    # Use for notification gating decisions
```

**Integration:**
- Adjust notification gates based on learned recovery costs
- Protect high-cost interruption periods more aggressively
- "Slack messages during DEEP_FOCUS cost you 23 minutes on average"
- Inform focus block suggestions

**Success Metric:** Average recovery time decreases, context losses decrease

---

### Tier 4: Continuous Learning Infrastructure

These provide the foundation for ongoing adaptation.

#### 4.1 Preference Rule Validation
**Priority:** HIGH | **Effort:** Medium | **Files:** `agent/decision_memory.py`

Validate extracted rules and deprecate ineffective ones.

**Implementation:**
```python
# In decision_memory.py
async def validate_preference_rules():
    """Score preference rules by their predictive accuracy."""
    query = """
    SELECT
        pr.id,
        pr.pattern,
        pr.confidence,
        COUNT(dt.*) as applications,
        AVG(dt.quality_score) as avg_outcome_quality,
        COUNT(*) FILTER (WHERE dt.quality_score >= 0.7) as successful,
        COUNT(*) FILTER (WHERE dt.quality_score < 0.3) as failed
    FROM preference_rules pr
    LEFT JOIN decision_traces dt ON dt.context @> pr.pattern::jsonb
    WHERE dt.created_at > pr.created_at
    GROUP BY pr.id
    """

    # Deprecate rules with low success rate
    # Boost confidence of high-performing rules
    # Flag conflicting rules for review
```

**Rule Lifecycle:**
```
CANDIDATE (< 3 applications)
    → ACTIVE (3+ applications, > 50% success)
    → VALIDATED (10+ applications, > 70% success)
    → DEPRECATED (< 30% success after 5+ applications)
```

**Integration:**
- Show rule performance in dashboard
- Allow user to confirm/reject rules
- Automatically deprecate poor performers
- Use validated rules with higher weight in decisions

**Success Metric:** Decision quality improves when validated rules apply

---

#### 4.2 Feedback Loop Closure
**Priority:** CRITICAL | **Effort:** High | **Files:** `agent/autonomous.py`, `agent/decision_memory.py`

Connect outcomes back to decision parameters.

**Implementation:**
```python
# In decision_memory.py
class AdaptivePolicyUpdater:
    """Update decision parameters based on outcomes."""

    async def update_from_traces(self, lookback_days: int = 30):
        """Analyze recent traces and adjust policy parameters."""

        # 1. Proposal acceptance patterns
        proposal_stats = await get_proposal_patterns()
        for project_id, stats in proposal_stats.items():
            if stats["approval_rate"] < 30 and stats["total"] >= 5:
                # Reduce proposal frequency for this project
                await self.adjust_proposal_threshold(project_id, increase=True)

        # 2. Duration calibration
        duration_factors = await get_duration_calibration()
        for project_id, factor in duration_factors.items():
            if factor["pace_factor"] > 1.3:  # Consistently underestimating
                await self.store_pace_adjustment(project_id, factor["pace_factor"])

        # 3. Temporal patterns
        temporal = await get_temporal_patterns()
        for pattern in temporal:
            await self.update_scheduling_weights(pattern)

        # 4. Deferral patterns
        deferral_reasons = await get_deferral_patterns()
        for reason, count in deferral_reasons.items():
            if count >= 3:
                await self.enable_auto_intervention(reason)
```

**Integration:**
- Run policy update daily (or on-demand)
- Log all parameter changes for transparency
- Allow rollback of changes
- A/B test policy changes where possible

**Success Metric:** Key metrics (completion rate, on-time rate, approval rate) improve over time

---

#### 4.3 Fine-Tuning Pipeline
**Priority:** LOW | **Effort:** Very High | **Files:** New module

Use decision traces for model fine-tuning (future consideration).

**Approach:**
1. Export high-quality traces (quality_score >= 0.8) as training data
2. Format for preference learning (DPO/RLHF)
3. Fine-tune executor model on personal preferences
4. A/B test fine-tuned vs base model
5. Iterate based on quality scores

**Note:** This is a longer-term goal. The immediate priority is to use the data for rule-based adaptation before investing in model fine-tuning.

---

## Implementation Order

### Phase 4a: Quick Wins (Week 1-2)
1. **1.1 Proposal Learning** - Query existing data, adjust proposal behavior
2. **1.2 Deadline Completion Analysis** - Query Neo4j, surface in briefings
3. **4.1 Preference Rule Validation** - Add lifecycle to existing rules

### Phase 4b: Duration Learning (Week 3-4)
4. **2.1 Duration Calibration** - Add timing fields, build pace model
5. **1.3 Deferral Prediction** - Calculate risk scores, trigger interventions

### Phase 4c: Temporal Patterns (Week 5-6)
6. **2.2 Time-of-Day Preferences** - Analyze patterns, inform scheduling
7. **3.2 Interruption Analytics** - Track interruptions, learn costs

### Phase 4d: Behavioral Prediction (Week 7-8)
8. **3.1 Procrastination Root Cause** - Infer reasons, auto-intervene
9. **4.2 Feedback Loop Closure** - Connect all learnings to policy updates

---

## Database Changes Summary

### PostgreSQL Additions
```sql
-- Task timing
ALTER TABLE tasks ADD COLUMN started_at TIMESTAMP;
ALTER TABLE tasks ADD COLUMN actual_minutes INTEGER;

-- Deferral analysis
CREATE TABLE deferral_analysis (...);

-- Interruption tracking
CREATE TABLE interruption_events (...);

-- Rule lifecycle
ALTER TABLE preference_rules ADD COLUMN lifecycle TEXT DEFAULT 'candidate';
ALTER TABLE preference_rules ADD COLUMN applications INTEGER DEFAULT 0;
ALTER TABLE preference_rules ADD COLUMN success_rate FLOAT;
```

### Neo4j Additions
```cypher
// Temporal patterns
CREATE CONSTRAINT temporal_pattern_id IF NOT EXISTS
FOR (p:TemporalPattern) REQUIRE p.id IS UNIQUE;

// Learned pace factors
CREATE CONSTRAINT pace_factor_id IF NOT EXISTS
FOR (p:PaceFactor) REQUIRE p.project_id IS UNIQUE;
```

---

## Success Metrics

| Metric | Current | Target | Measurement |
|--------|---------|--------|-------------|
| Proposal approval rate | ~50% | >75% | `get_proposal_stats()` |
| On-time task completion | Unknown | >80% | Deadline analysis query |
| Duration estimate accuracy | Unknown | r > 0.7 | Correlation of estimated vs actual |
| Deferral rate | Unknown | <20% | Tasks deferred / tasks created |
| Repeat deferral rate | Unknown | <10% | Tasks deferred 2+ times |
| Rule validation rate | 0% | >60% rules validated | Rule lifecycle tracking |

---

## Files Reference

| File | New/Modified | Purpose |
|------|--------------|---------|
| `agent/action_log.py` | Modified | Add `get_proposal_patterns()` |
| `agent/decision_memory.py` | Modified | Add pattern analysis, rule validation |
| `agent/state_model.py` | Modified | Add `DeferralRisk`, temporal patterns |
| `agent/autonomous.py` | Modified | Use learned patterns in decisions |
| `agent/graph_observer.py` | Modified | Add deadline/deferral analysis |
| `services/tasks.py` | Modified | Add timing fields, calibration |
| `agent/interruption_firewall.py` | Modified | Add interruption tracking |
| `agent/learning.py` | New | Central learning loop coordination |
| `db/phase4_schema.py` | New | Schema migrations for Phase 4 |

---

## CLI Commands (Proposed)

```bash
# Learning analysis
cognitex learning-stats        # Show learning metrics
cognitex learning-update       # Run policy update cycle
cognitex calibration           # Show duration calibration factors

# Pattern inspection
cognitex patterns temporal     # Show time-of-day patterns
cognitex patterns deferral     # Show deferral patterns
cognitex patterns proposals    # Show proposal acceptance patterns

# Rule management
cognitex rules list            # List preference rules with lifecycle
cognitex rules validate        # Run rule validation
cognitex rules deprecate <id>  # Manually deprecate a rule
```

---

## Discord Commands (Proposed)

```
/learning           - Show learning summary
/patterns           - Show key behavioral patterns
/calibration        - Show personal pace factors
/rules              - List active preference rules
```
