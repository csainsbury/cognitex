# Phase 4b: Learning System Integration

**Created:** 2026-01-02
**Status:** Blueprint
**Prerequisite:** Phase 4 Memory Blueprint (implemented)

This document defines the integration work needed to close the learning feedback loop. The Phase 4 learning functions exist but are not wired into the actual workflows.

---

## Current State

The following functions exist but are never called:

| Function | Location | Purpose |
|----------|----------|---------|
| `get_proposal_recommendation()` | `action_log.py` | Check if task should be proposed based on patterns |
| `record_task_timing()` | `services/tasks.py` | Record actual vs estimated duration |
| `record_deferral()` | `state_model.py` | Track when/why tasks are deferred |
| `init_phase4_schema()` | `db/phase4_schema.py` | Create learning tables |
| `run_policy_update()` | `agent/learning.py` | Validate rules, extract patterns |
| `get_learning_summary()` | `agent/learning.py` | Get insights for briefings |

---

## Integration Points

### 1. Proposal Recommendation Integration
**File:** `agent/autonomous.py`
**Method:** `_create_task()`
**Priority:** HIGH

Before proposing a task, check learned patterns to:
- Skip proposals for categories with <30% approval rate
- Auto-approve categories with >80% approval rate (if configured)
- Add historical context to proposal notifications

**Implementation:**
```python
# In _create_task(), before calling propose_task()
from cognitex.agent.action_log import get_proposal_recommendation

async def _create_task(self, session, params: dict, reason: str) -> dict | None:
    # ... existing code ...

    # Check learned patterns before proposing
    recommendation = await get_proposal_recommendation(
        project_id=project_id,
        priority=priority,
    )

    if not recommendation["should_propose"]:
        logger.info(
            "Skipping proposal based on learned patterns",
            reason=recommendation["reason"],
        )
        return {"skipped": True, "reason": recommendation["reason"]}

    # Include historical rate in notification
    historical_rate = recommendation.get("historical_rate")
    if historical_rate is not None:
        reason += f" (historical approval: {historical_rate:.0f}%)"

    # Continue with proposal...
```

---

### 2. Task Completion Timing
**File:** `agent/tools.py`
**Class:** `UpdateTaskTool`
**Priority:** HIGH

When a task is marked as "done", record the timing for duration calibration.

**Implementation:**
```python
# In UpdateTaskTool.execute(), after successful status update to "done"
from cognitex.services.tasks import record_task_timing
from datetime import datetime

async def execute(self, ...) -> str:
    # ... existing update logic ...

    if status == "done" and result.get("updated"):
        # Record timing for duration calibration
        try:
            # Get task details for timing
            task = result.get("task", {})
            started_at = task.get("started_at")
            estimated_minutes = task.get("estimated_minutes")

            if started_at:
                await record_task_timing(
                    task_id=task_id,
                    started_at=datetime.fromisoformat(started_at),
                    completed_at=datetime.now(),
                    estimated_minutes=estimated_minutes,
                )
        except Exception as e:
            logger.warning("Failed to record task timing", error=str(e))

    return result
```

**Note:** Also need to set `started_at` when task status changes to "in_progress".

---

### 3. Deferral Recording
**File:** `agent/tools.py`
**Class:** `UpdateTaskTool`
**Priority:** MEDIUM

When a task is rescheduled or its due date pushed back, record the deferral.

**Implementation:**
```python
# In UpdateTaskTool.execute(), detect deferrals
from cognitex.agent.state_model import record_deferral

async def execute(self, ...) -> str:
    # Get original task before update
    original_task = await get_task(task_id)
    original_due = original_task.get("due_date") or original_task.get("due")

    # ... perform update ...

    # Check if this is a deferral (due date pushed back)
    new_due = due_date  # from update params
    if original_due and new_due:
        original_dt = parse_date(original_due)
        new_dt = parse_date(new_due)

        if new_dt > original_dt:
            # This is a deferral
            await record_deferral(
                task_id=task_id,
                inferred_reason="due_date_extended",
                friction_at_deferral=None,  # Could get from state model
            )
```

---

### 4. Startup Initialization
**Files:** `discord_bot/__main__.py`, `web/app.py`
**Priority:** HIGH

Initialize Phase 4 schema on startup so tables exist.

**Discord Bot Implementation:**
```python
# In CognitexBot._init_databases()
async def _init_databases(self) -> None:
    await init_neo4j()
    await init_postgres()
    await init_redis()

    # Initialize Phase 4 learning schema
    from cognitex.db.phase4_schema import init_phase4_schema
    await init_phase4_schema()

    logger.info("Phase 4 learning schema initialized")
```

**Web App Implementation:**
```python
# In lifespan() context manager
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_neo4j()
    await init_graph_schema()
    await init_postgres()
    await init_redis()

    # Initialize Phase 4 learning schema
    from cognitex.db.phase4_schema import init_phase4_schema
    await init_phase4_schema()

    yield
    # ... cleanup ...
```

---

### 5. Scheduled Policy Updates
**File:** `agent/triggers.py`
**Method:** `_setup_scheduled_triggers()`
**Priority:** MEDIUM

Schedule daily policy update to validate rules and extract patterns.

**Implementation:**
```python
# In _setup_scheduled_triggers(), add new job
async def _setup_scheduled_triggers(self) -> None:
    # ... existing triggers ...

    # Daily learning policy update (2am - low activity time)
    self.scheduler.add_job(
        self._run_policy_update,
        trigger=CronTrigger(hour=2, minute=0),
        id="policy_update",
        name="Learning Policy Update",
        replace_existing=True,
    )

async def _run_policy_update(self) -> None:
    """Run learning policy update cycle."""
    from cognitex.agent.learning import init_learning_system, get_learning_system
    from cognitex.agent.action_log import log_action

    try:
        await init_learning_system()
        ls = get_learning_system()
        results = await ls.run_policy_update()

        logger.info("Policy update completed", **results)

    except Exception as e:
        logger.error("Policy update failed", error=str(e))
        await log_action(
            action_type="policy_update",
            source="trigger",
            status="failed",
            error=str(e),
        )
```

---

### 6. Morning Briefing Integration
**File:** `agent/core.py`
**Method:** `morning_briefing()`
**Priority:** MEDIUM

Include learning insights in the morning briefing.

**Implementation:**
```python
# In Agent.morning_briefing()
async def morning_briefing(self) -> str:
    # ... existing briefing generation ...

    # Add learning insights section
    learning_section = await self._get_learning_insights()

    return briefing + context_section + learning_section

async def _get_learning_insights(self) -> str:
    """Get learning system insights for briefing."""
    try:
        from cognitex.agent.learning import get_learning_system

        ls = get_learning_system()
        summary = await ls.get_learning_summary()

        insights = summary.get("insights", [])
        if not insights:
            return ""

        section = "\n\n**Learning Insights:**\n"
        for insight in insights[:3]:
            section += f"- {insight}\n"

        # Add high-risk tasks if any
        deferrals = summary.get("deferrals", {})
        high_risk = deferrals.get("high_risk_tasks", [])
        if high_risk:
            section += f"\n**Deferral Risk:** {len(high_risk)} tasks at risk of being deferred\n"

        return section

    except Exception as e:
        logger.warning("Failed to get learning insights", error=str(e))
        return ""
```

---

### 7. Task Start Time Recording
**File:** `agent/tools.py` or `services/tasks.py`
**Priority:** HIGH

When task status changes to "in_progress", record the start time.

**Implementation:**
```python
# In UpdateTaskTool.execute() or TaskService.update()
if status == "in_progress":
    # Record start time for duration calibration
    await session.execute(text("""
        UPDATE tasks
        SET started_at = NOW()
        WHERE id = :task_id AND started_at IS NULL
    """), {"task_id": task_id})
```

---

## Implementation Order

1. **Startup Initialization** (4) - Tables must exist first
2. **Task Start Time Recording** (7) - Needed for timing to work
3. **Task Completion Timing** (2) - Core duration calibration
4. **Proposal Recommendation** (1) - Use learned patterns
5. **Deferral Recording** (3) - Track procrastination
6. **Scheduled Policy Updates** (5) - Periodic learning
7. **Morning Briefing Integration** (6) - Surface insights

---

## Files to Modify

| File | Changes |
|------|---------|
| `agent/autonomous.py` | Add proposal recommendation check |
| `agent/tools.py` | Add timing recording, deferral tracking, start time |
| `agent/core.py` | Add learning insights to briefing |
| `agent/triggers.py` | Add policy update schedule |
| `discord_bot/__main__.py` | Add Phase 4 schema init |
| `web/app.py` | Add Phase 4 schema init |
| `services/tasks.py` | Add started_at handling in update |

---

## Success Criteria

After implementation:

1. **`cognitex learning-stats`** shows non-zero timing records after completing tasks
2. **`cognitex deferral-risk`** updates when tasks are rescheduled
3. **Morning briefing** includes learning insights when patterns exist
4. **Task proposals** respect learned approval patterns
5. **Rules** are automatically validated daily at 2am

---

## Testing Plan

1. Create a task with estimate, mark in_progress, then done → verify timing recorded
2. Reschedule a task's due date → verify deferral recorded
3. Approve/reject several proposals → verify patterns affect future recommendations
4. Wait for 2am trigger → verify rules validated (or run `cognitex learning-update`)
5. Run `/briefing` → verify learning insights section appears
