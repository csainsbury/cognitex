"""
Autonomous Agent Loop - Proactive graph management and orchestration.

This agent runs continuously, observing the knowledge graph and taking
autonomous actions to:
- Monitor and maintain graph health
- Create connections between related entities
- Assess and update task/project/goal status
- Identify actions that will progress goals
- Keep the knowledge graph coherent and actionable
"""

import asyncio
import json
from datetime import datetime
from typing import Any

import structlog

from cognitex.agent.action_log import log_action
from cognitex.agent.graph_observer import GraphObserver
from cognitex.config import get_settings
from cognitex.prompts import format_prompt

logger = structlog.get_logger()

# Default interval in minutes (can be overridden in settings)
DEFAULT_INTERVAL_MINUTES = 15


class AutonomousAgent:
    """
    Autonomous agent that proactively manages the knowledge graph.

    Runs on a configurable interval, observing graph state and taking
    actions to maintain health, create connections, and progress goals.
    """

    def __init__(self):
        self.settings = get_settings()
        self.interval_minutes = self.settings.autonomous_agent_interval_minutes
        self.enabled = self.settings.autonomous_agent_enabled
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the autonomous agent loop."""
        if not self.enabled:
            logger.info("Autonomous agent disabled in settings")
            return

        if self._running:
            logger.warning("Autonomous agent already running")
            return

        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Autonomous agent started", interval_minutes=self.interval_minutes)

        await log_action(
            "autonomous_agent_started",
            "system",
            summary=f"Autonomous agent started with {self.interval_minutes}min interval"
        )

    async def stop(self) -> None:
        """Stop the autonomous agent loop."""
        if not self._running:
            return

        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        logger.info("Autonomous agent stopped")
        await log_action("autonomous_agent_stopped", "system", summary="Autonomous agent stopped")

    async def _run_loop(self) -> None:
        """Main agent loop - observe, reason, act."""
        # Initial delay to let system stabilize
        await asyncio.sleep(30)

        while self._running:
            try:
                await self._run_cycle()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Autonomous agent cycle failed", error=str(e))
                await log_action(
                    "autonomous_cycle",
                    "agent",
                    status="failed",
                    error=str(e)
                )

            # Wait for next cycle
            await asyncio.sleep(self.interval_minutes * 60)

    async def _run_cycle(self) -> None:
        """Run a single observation-reasoning-action cycle."""
        cycle_start = datetime.now()
        logger.info("Starting autonomous agent cycle")

        from cognitex.db.neo4j import get_neo4j_session

        actions_taken = []

        async for session in get_neo4j_session():
            # 1. OBSERVE - Gather graph context
            observer = GraphObserver(session)
            context = await observer.get_full_context()

            logger.info(
                "Graph context gathered",
                changes_24h=context["summary"]["total_changes_24h"],
                stale_tasks=context["summary"]["stale_tasks"],
                orphaned_docs=context["summary"]["orphaned_documents"],
                connection_opps=context["summary"]["connection_opportunities"],
            )

            # 2. ORIENT & DECIDE - Use LLM to reason about what to do
            decisions = await self._reason_about_context(context)

            # 3. ACT - Execute decisions with safety limits
            # Hard limit to prevent runaway cycles
            MAX_ACTIONS_PER_CYCLE = 5
            action_count = 0

            # Track entities flagged in this cycle to prevent duplicates
            flagged_this_cycle: set[str] = set()

            for decision in decisions:
                # Enforce hard limit on actions per cycle
                if action_count >= MAX_ACTIONS_PER_CYCLE:
                    logger.warning(
                        "Hit max actions per cycle limit",
                        limit=MAX_ACTIONS_PER_CYCLE,
                        remaining_decisions=len(decisions) - decisions.index(decision),
                    )
                    break

                try:
                    result = await self._execute_decision(session, decision, flagged_this_cycle)
                    if result and not result.get("skipped"):
                        actions_taken.append({
                            "decision": decision,
                            "result": result
                        })
                        action_count += 1
                except Exception as e:
                    logger.warning(
                        "Decision execution failed",
                        decision_type=decision.get("action"),
                        error=str(e)
                    )

            break

        # Log the cycle results
        cycle_duration = (datetime.now() - cycle_start).total_seconds()
        await log_action(
            "autonomous_cycle",
            "agent",
            summary=f"Cycle completed: {len(actions_taken)} actions taken",
            details={
                "duration_seconds": cycle_duration,
                "context_summary": context["summary"],
                "actions_taken": actions_taken,
            }
        )

        logger.info(
            "Autonomous agent cycle completed",
            duration_seconds=cycle_duration,
            actions_taken=len(actions_taken)
        )

    async def _check_clinical_recovery(self) -> bool:
        """Check if user is currently in post-clinical recovery mode.

        Returns:
            True if in clinical recovery (rest of day mode)
        """
        try:
            from cognitex.db.redis import get_redis
            redis = get_redis()
            recovery_until = await redis.get("cognitex:clinical_recovery_until")
            if recovery_until:
                from datetime import datetime
                recovery_str = recovery_until.decode() if isinstance(recovery_until, bytes) else recovery_until
                recovery_time = datetime.fromisoformat(recovery_str)
                return datetime.now() < recovery_time
            return False
        except Exception as e:
            logger.debug("Could not check clinical recovery", error=str(e))
            return False

    async def _build_state_context(self) -> str:
        """Build state context string for the LLM prompt.

        Returns:
            Formatted string describing current user state and energy level
        """
        try:
            from cognitex.agent.state_model import get_state_estimator, get_temporal_model, ModeRules

            state_estimator = get_state_estimator()
            temporal_model = get_temporal_model()
            state = await state_estimator.get_current_state()

            # Check clinical recovery
            in_clinical_recovery = await self._check_clinical_recovery()

            # Get current time info
            now = datetime.now()
            hour = now.hour
            expected_energy = temporal_model.get_expected_energy(hour)
            peak_hours = temporal_model.get_peak_hours()
            is_peak = hour in peak_hours

            # Get mode rules
            rules = ModeRules.get_rules(state.mode)

            lines = [
                f"- **Mode**: {state.mode.value}",
                f"- **Fatigue**: {state.signals.fatigue_level:.0%}",
                f"- **Hour**: {hour}:00 (expected energy: {expected_energy:.0%})",
                f"- **Peak time**: {'Yes' if is_peak else 'No'} (peak hours: {', '.join(f'{h}:00' for h in peak_hours[:3])})",
            ]

            if state.signals.available_block_minutes:
                lines.append(f"- **Available block**: {state.signals.available_block_minutes} mins")

            if in_clinical_recovery:
                lines.append("")
                lines.append("**POST-CLINICAL RECOVERY MODE ACTIVE**")
                lines.append("- Only low-energy, low-friction tasks allowed")
                lines.append("- Do NOT suggest demanding work")
                lines.append("- Use SUGGEST_RESCHEDULE for high-energy tasks to tomorrow morning")

            # Add mode-specific guidance
            lines.append("")
            lines.append(f"**Mode rules for {state.mode.value}:**")
            lines.append(f"- Max friction: {rules.max_task_friction}/5")
            lines.append(f"- Allowed task types: {', '.join(rules.allowed_task_types)}")
            if state.signals.fatigue_level > 0.7:
                lines.append("- HIGH FATIGUE: Avoid high-energy tasks")

            return "\n".join(lines)

        except Exception as e:
            logger.debug("Could not build state context", error=str(e))
            return "(State context unavailable)"

    async def _reason_about_context(self, context: dict) -> list[dict]:
        """Use LLM to reason about the graph context and decide on actions."""
        from cognitex.services.llm import get_llm_service

        # Build a concise summary for the LLM
        summary = context["summary"]

        # Format inbox items (captured interruptions waiting for triage)
        inbox_items = context.get("inbox_items", [])
        if inbox_items:
            inbox_lines = []
            for item in inbox_items:
                urgency = item.get("urgency", "normal").upper()
                inbox_lines.append(
                    f"  - [{urgency}] [{item.get('source', 'unknown')}] {item.get('subject', 'No subject')}\n"
                    f"    Preview: {item.get('preview', '')[:100]}...\n"
                    f"    Suggestion: {item.get('suggested', 'Review')}\n"
                    f"    ID: {item.get('id')}"
                )
            inbox_text = "\n".join(inbox_lines)
        else:
            inbox_text = "  (Inbox empty)"

        # Format writing samples for style learning
        writing_samples = context.get('writing_samples', [])[:3]
        if writing_samples:
            samples_text = "\n\n".join([
                f"--- Sample {i+1} ---\n{sample[:500]}{'...' if len(sample) > 500 else ''}"
                for i, sample in enumerate(writing_samples)
            ])
        else:
            samples_text = "(No writing samples available yet)"

        # Format pending emails needing response
        pending_emails = context.get('pending_emails', [])[:5]
        if pending_emails:
            email_lines = []
            for e in pending_emails:
                urgency = str(e.get('urgency', 'normal')).upper()
                sender = e.get('sender_name') or e.get('sender_email') or 'Unknown'
                snippet = str(e.get('snippet', '') or '')[:150]
                email_lines.append(
                    f"  - [{urgency}] From: {sender}\n"
                    f"    Subject: {e.get('subject', 'No subject')}\n"
                    f"    ID: {e.get('id')}\n"
                    f"    Snippet: {snippet}..."
                )
            pending_emails_text = "\n".join(email_lines)
        else:
            pending_emails_text = "  None pending"

        # Format upcoming calendar events
        upcoming = context.get('upcoming_calendar', [])[:5]
        if upcoming:
            cal_lines = []
            for c in upcoming:
                needs_prep = "NEEDS CONTEXT" if c.get('needs_context') else "prepared"
                attendees = ", ".join(c.get('attendees', [])[:3]) or "No attendees"
                cal_lines.append(
                    f"  - [{needs_prep}] {c.get('title', 'No title')}\n"
                    f"    ID: {c.get('id')}\n"
                    f"    When: {c.get('start_time')}\n"
                    f"    Attendees: {attendees}"
                )
            upcoming_calendar_text = "\n".join(cal_lines)
        else:
            upcoming_calendar_text = "  No upcoming meetings"

        # Format connection opportunities with explicit action suggestions
        opportunities = context.get('connection_opportunities', [])[:10]
        opp_lines = []
        for o in opportunities:
            opp_type = o.get('opportunity_type', '')
            source_type = o.get('source_type', '')
            source_id = o.get('source_id', '')
            source_name = o.get('source_name', '')
            target_id = o.get('target_id', '')
            target_name = o.get('target_name', '')
            reason = o.get('match_reason', 'match')

            # Map to explicit action with params
            if 'document_project' in opp_type:
                action = f'LINK_DOCUMENT with document_id="{source_id}", document_name="{source_name}", project_id="{target_id}", project_name="{target_name}"'
            elif 'repository_project' in opp_type:
                action = f'LINK_REPOSITORY with repository_id="{source_id}", repository_name="{source_name}", project_id="{target_id}", project_name="{target_name}"'
            elif 'task_project' in opp_type:
                action = f'LINK_TASK with task_id="{source_id}", task_name="{source_name}", project_id="{target_id}", project_name="{target_name}"'
            elif 'project_goal' in opp_type:
                action = f'LINK_PROJECT_TO_GOAL with project_id="{source_id}", project_name="{source_name}", goal_id="{target_id}", goal_name="{target_name}"'
            else:
                action = f'{source_type} "{source_name}" -> {o.get("target_type", "")} "{target_name}"'

            opp_lines.append(f"  - {action} ({reason})")

        opp_text = "\n".join(opp_lines) if opp_lines else "  None found"

        # Format goals needing attention
        goals_text = "\n".join([
            f"  - '{g.get('title')}' (id: {g.get('id')}) - {g.get('status_reason')}"
            for g in context.get('goal_health', []) if g.get('needs_attention')
        ][:5]) or "  None"

        # Format projects needing attention
        projects_text = "\n".join([
            f"  - '{p.get('title')}' (id: {p.get('id')}) - {p.get('status_reason')}, {p.get('total_tasks')} tasks, {p.get('overdue_count', 0)} overdue"
            for p in context.get('project_health', []) if p.get('needs_attention')
        ][:5]) or "  None"

        # Format orphaned items
        orphaned_text = "\n".join([
            f"  - {o.get('type')} '{o.get('label')}' (id: {o.get('id')}) - {o.get('issue')}"
            for o in context.get('orphaned_nodes', [])
        ][:8]) or "  None"

        # Build skip list (items already actioned - don't re-suggest)
        skip_lines = []
        projects_with_blocks = context.get('projects_with_recent_blocks', set())
        if projects_with_blocks:
            skip_lines.append(f"- Projects with recent focus blocks (skip SCHEDULE_BLOCK): {', '.join(list(projects_with_blocks)[:5])}")
        skip_list_text = "\n".join(skip_lines) if skip_lines else "  (none)"

        # Get recent notification history so agent can avoid repetitive notifications
        from cognitex.agent.action_log import get_recent_notifications, get_recent_rejections
        recent_notifications = await get_recent_notifications(hours=48)

        # Format notification history for context
        if recent_notifications:
            notif_lines = []
            for n in recent_notifications[:20]:  # Last 20 notifications
                notif_lines.append(f"  - [{n['timestamp']}] {n['action_type']}: {n['summary'][:100] if n.get('summary') else 'No summary'}")
            notification_history_text = "\n".join(notif_lines)
        else:
            notification_history_text = "  (No recent notifications)"

        # =====================================================================
        # LEARNING CONTEXT - Feed the agent its "report card"
        # This closes the feedback loop: the agent sees what it learned
        # =====================================================================
        learned_lines = []

        # 1. High-level insights from learning system
        try:
            from cognitex.agent.learning import get_learning_system
            ls = get_learning_system()
            if ls:
                learning_summary = await ls.get_learning_summary()
                insights = learning_summary.get("insights", [])
                if insights:
                    learned_lines.append("### General Insights")
                    learned_lines.extend([f"- {i}" for i in insights[:3]])

                # Duration calibration insight
                calibration = learning_summary.get("duration_calibration", {})
                avg_error = calibration.get("average_estimation_error")
                if avg_error is not None and abs(avg_error) > 0.2:
                    direction = "underestimate" if avg_error > 0 else "overestimate"
                    learned_lines.append(f"- Time estimates tend to {direction} by {abs(avg_error):.0%}")
        except Exception as e:
            logger.debug("Failed to get learning insights", error=str(e))

        # 2. Active preference rules
        try:
            from cognitex.agent.decision_memory import get_decision_memory
            dm = get_decision_memory()
            if dm and dm.rules:
                rules = await dm.rules.get_matching_rules(
                    context={"trigger_type": "autonomous_cycle"},
                    rule_type="action_preference"
                )
                if rules:
                    learned_lines.append("\n### Learned Preferences")
                    for rule in rules[:5]:
                        conf = rule.get('confidence', 0)
                        learned_lines.append(f"- {rule['rule_name']} (confidence: {conf:.0%})")
        except Exception as e:
            logger.debug("Failed to get preference rules", error=str(e))

        # 3. Recent rejections - CRITICAL for stopping bad behaviors
        try:
            rejections = await get_recent_rejections(limit=5)
            if rejections:
                learned_lines.append("\n### Recently Rejected (DO NOT REPEAT)")
                for r in rejections:
                    title = r.get('title', 'Unknown')
                    reason = r.get('rejection_reason') or r.get('reason') or 'No reason given'
                    learned_lines.append(f"- Rejected: '{title}' - Reason: {reason}")
        except Exception as e:
            logger.debug("Failed to get recent rejections", error=str(e))

        # 4. Proposal patterns - what gets approved vs rejected
        try:
            from cognitex.agent.action_log import get_proposal_patterns
            patterns = await get_proposal_patterns(min_samples=3)
            low_approval = []
            for priority, data in patterns.get("by_priority", {}).items():
                rate = data.get("approved", 0) / max(data.get("approved", 0) + data.get("rejected", 0), 1)
                if rate < 0.3 and data.get("rejected", 0) >= 2:
                    low_approval.append(f"{priority} priority tasks ({rate:.0%} approval)")
            if low_approval:
                learned_lines.append("\n### Low Approval Categories (avoid proposing)")
                learned_lines.extend([f"- {cat}" for cat in low_approval])
        except Exception as e:
            logger.debug("Failed to get proposal patterns", error=str(e))

        learned_guidelines = "\n".join(learned_lines) if learned_lines else "(No specific guidelines yet - keep learning from feedback)"

        # =====================================================================
        # STATE CONTEXT - Current user state affects what actions are appropriate
        # =====================================================================
        state_context_text = await self._build_state_context()

        prompt = format_prompt(
            "autonomous_agent",
            # Summary stats
            inbox_count=summary.get('inbox_count', 0),
            emails_needing_response=summary.get('emails_needing_response', 0),
            meetings_needing_prep=summary.get('meetings_needing_prep', 0),
            connection_opportunities=summary['connection_opportunities'],
            pending_task_count=summary['pending_task_count'],
            goals_needing_attention=summary['goals_needing_attention'],
            projects_needing_attention=summary['projects_needing_attention'],
            # Formatted content sections
            inbox_text=inbox_text,
            writing_samples_text=samples_text,
            learned_guidelines=learned_guidelines,  # Learning feedback loop
            state_context_text=state_context_text,  # User state/energy context
            pending_emails_text=pending_emails_text,
            upcoming_calendar_text=upcoming_calendar_text,
            opportunities_text=opp_text,
            goals_text=goals_text,
            projects_text=projects_text,
            orphaned_text=orphaned_text,
            skip_list_text=skip_list_text,
            notification_history_text=notification_history_text,
        )

        try:
            llm = get_llm_service()
            logger.info("Calling LLM for autonomous reasoning...")
            response = await llm.complete(prompt, max_tokens=2000)
            logger.info("LLM response received", response_length=len(response) if response else 0)

            if not response:
                logger.warning("LLM returned empty response")
                return []

            # Parse the JSON response
            # Try to extract JSON from the response
            response_text = response.strip()

            # Log first part of response for debugging
            logger.debug("LLM raw response", response=response_text[:500])

            # Robust JSON extraction using regex
            import re

            # 1. Try to find a code block marked as json
            json_block = re.search(r"```json\s*(\[.*?\])\s*```", response_text, re.DOTALL)
            if json_block:
                response_text = json_block.group(1)
            else:
                # 2. Remove any markdown code block markers
                if response_text.startswith("```"):
                    lines = response_text.split("\n")
                    response_text = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])

                # 3. Try to find any array-like structure
                array_match = re.search(r"(\[.*\])", response_text, re.DOTALL)
                if array_match:
                    response_text = array_match.group(1)

            decisions = json.loads(response_text)

            if not isinstance(decisions, list):
                decisions = [decisions]

            # Normalize decision format - handle multiple formats:
            # 1. {"action": "X", "params": {...}} (expected format)
            # 2. {"action": "X", "repository_id": "...", ...} (flat params)
            # 3. {"LINK_X": {...}, "reason": "..."} (action as key)
            normalized = []
            for d in decisions:
                if "action" in d:
                    action = d["action"]
                    if "params" in d:
                        # Already in expected format
                        normalized.append(d)
                    else:
                        # Flat params - extract all non-action/reason keys as params
                        params = {k: v for k, v in d.items() if k not in ("action", "reason")}
                        normalized.append({
                            "action": action,
                            "params": params,
                            "reason": d.get("reason", "")
                        })
                else:
                    # Find action type key (LINK_*, CREATE_*, FLAG_*, DRAFT_*, COMPILE_*, SCHEDULE_*)
                    action_type = None
                    params = {}
                    reason = d.get("reason", "")
                    for key in d:
                        if key.startswith(("LINK_", "CREATE_", "FLAG_", "UPDATE_", "DRAFT_", "COMPILE_", "SCHEDULE_")):
                            action_type = key
                            params = d[key] if isinstance(d[key], dict) else {}
                            break
                    if action_type:
                        normalized.append({
                            "action": action_type,
                            "params": params,
                            "reason": reason
                        })

            logger.info("LLM reasoning complete", decisions_count=len(normalized))
            return normalized

        except json.JSONDecodeError as e:
            logger.warning("Failed to parse LLM response as JSON", error=str(e), response=response[:500] if response else None)
            return []
        except Exception as e:
            logger.error("LLM reasoning failed", error=str(e), exc_info=True)
            return []

    async def _execute_decision(
        self, session, decision: dict, flagged_this_cycle: set[str] | None = None
    ) -> dict | None:
        """Execute a single decision."""
        action = decision.get("action")
        params = decision.get("params", {})
        reason = decision.get("reason", "")

        logger.info("Executing decision", action=action, reason=reason[:100])

        # Inbox processing (Priority 0)
        if action == "PROCESS_INBOX_ITEM":
            return await self._process_inbox_item(session, params, reason)

        # Digital Twin priority actions
        if action == "DRAFT_EMAIL":
            return await self._draft_email(session, params, reason)
        elif action == "COMPILE_CONTEXT_PACK":
            return await self._compile_context_pack(session, params, reason)
        elif action == "SCHEDULE_BLOCK":
            return await self._schedule_block(session, params, reason)
        # Graph maintenance actions
        elif action == "LINK_DOCUMENT":
            return await self._link_document(session, params, reason)
        elif action == "LINK_REPOSITORY":
            return await self._link_repository(session, params, reason)
        elif action == "LINK_TASK":
            return await self._link_task(session, params, reason)
        elif action == "LINK_PROJECT_TO_GOAL":
            return await self._link_project_to_goal(session, params, reason)
        elif action == "UPDATE_PROJECT_STATUS":
            return await self._update_project_status(session, params, reason)
        elif action == "CREATE_TASK":
            return await self._create_task(session, params, reason)
        elif action == "SUGGEST_RESCHEDULE":
            return await self._suggest_reschedule(session, params, reason)
        elif action == "FLAG_FOR_REVIEW":
            return await self._flag_for_review(params, reason, flagged_this_cycle)
        else:
            logger.warning("Unknown action type", action=action)
            return None

    async def _process_inbox_item(self, session, params: dict, reason: str) -> dict | None:
        """
        Process and clear an item from the interruption firewall inbox.

        The agent takes action on the captured item (e.g., creates a task,
        drafts a reply, or dismisses it) and then clears it from the queue.
        """
        item_id = params.get("item_id")
        resolution = params.get("resolution", "dismissed")
        follow_up_action = params.get("follow_up_action")

        if not item_id:
            logger.warning("PROCESS_INBOX_ITEM missing item_id")
            return None

        try:
            from cognitex.agent.interruption_firewall import get_interruption_firewall

            firewall = get_interruption_firewall()

            # Clear the item from the queue
            await firewall.clear_processed_items([item_id])

            await log_action(
                "process_inbox",
                "agent",
                summary=f"Processed inbox item: {resolution}",
                details={
                    "item_id": item_id,
                    "resolution": resolution,
                    "reason": reason,
                    "follow_up_action": follow_up_action,
                }
            )

            logger.info(
                "Inbox item processed",
                item_id=item_id,
                resolution=resolution,
            )

            return {"processed": True, "item_id": item_id, "resolution": resolution}

        except Exception as e:
            logger.warning("Failed to process inbox item", error=str(e), item_id=item_id)
            return None

    async def _link_document(self, session, params: dict, reason: str) -> dict | None:
        """Link a document to a project."""
        doc_id = params.get("document_id")
        project_id = params.get("project_id")

        if not doc_id or not project_id:
            return None

        query = """
        MATCH (d:Document {drive_id: $doc_id})
        MATCH (p:Project {id: $project_id})
        MERGE (d)-[r:BELONGS_TO]->(p)
        SET r.created_at = datetime(),
            r.created_by = 'autonomous_agent',
            r.reason = $reason
        RETURN d.name as doc_name, p.title as project_title
        """
        try:
            result = await session.run(query, {
                "doc_id": doc_id,
                "project_id": project_id,
                "reason": reason
            })
            data = await result.single()
            if data:
                await log_action(
                    "link_document",
                    "agent",
                    summary=f"Linked '{data['doc_name']}' to '{data['project_title']}'",
                    details={"document_id": doc_id, "project_id": project_id, "reason": reason}
                )
                return {"linked": True, "doc": data["doc_name"], "project": data["project_title"]}
        except Exception as e:
            logger.warning("Failed to link document", error=str(e))
        return None

    async def _link_task(self, session, params: dict, reason: str) -> dict | None:
        """Link a task to a project.

        NOTE: Task linking is disabled for autonomous agent.
        Tasks should be linked manually by users or via the task linking UI
        to prevent erroneous automatic associations.
        """
        task_id = params.get("task_id")
        project_id = params.get("project_id")
        task_name = params.get("task_name", "")
        project_name = params.get("project_name", "")

        if not task_id or not project_id:
            return None

        # Check if task is already linked to ANY project
        check_query = """
        MATCH (t:Task {id: $task_id})
        OPTIONAL MATCH (t)-[:BELONGS_TO|PART_OF]->(existing:Project)
        RETURN t.title as task_title, existing.title as existing_project
        """
        try:
            result = await session.run(check_query, {"task_id": task_id})
            data = await result.single()

            if not data:
                logger.warning("Task not found for linking", task_id=task_id)
                return None

            if data.get("existing_project"):
                # Task already linked - don't re-link
                logger.info(
                    "Task already linked to project, skipping",
                    task_id=task_id,
                    task_title=data["task_title"],
                    existing_project=data["existing_project"],
                    proposed_project=project_name,
                )
                return {"skipped": True, "reason": f"Already linked to '{data['existing_project']}'"}

            # For unlinked tasks, flag for human review instead of auto-linking
            await log_action(
                "flag_for_review",
                "agent",
                summary=f"Suggested linking task '{task_name}' to project '{project_name}'",
                details={
                    "type": "task_link_suggestion",
                    "task_id": task_id,
                    "task_name": task_name,
                    "project_id": project_id,
                    "project_name": project_name,
                    "reason": reason,
                }
            )

            # Send notification for review
            from cognitex.services.notifications import publish_notification
            await publish_notification(
                f"**Task Link Suggestion**\n\n"
                f"Link task '{task_name}' to project '{project_name}'?\n"
                f"Reason: {reason}\n\n"
                f"Review at /tasks to approve or dismiss.",
                urgency="low",
            )

            return {"flagged": True, "task": task_name, "project": project_name}

        except Exception as e:
            logger.warning("Failed to check/link task", error=str(e))
        return None

    async def _link_repository(self, session, params: dict, reason: str) -> dict | None:
        """Link a GitHub repository to a project."""
        repo_id = params.get("repository_id")
        project_id = params.get("project_id")
        repo_name = params.get("repository_name", "")
        project_name = params.get("project_name", "")

        if not repo_id or not project_id:
            return None

        query = """
        MATCH (r:Repository {id: $repo_id})
        MATCH (p:Project {id: $project_id})
        MERGE (r)-[rel:BELONGS_TO]->(p)
        SET rel.created_at = datetime(),
            rel.created_by = 'autonomous_agent',
            rel.reason = $reason
        RETURN r.full_name as repo_name, p.title as project_title
        """
        try:
            result = await session.run(query, {
                "repo_id": repo_id,
                "project_id": project_id,
                "reason": reason
            })
            data = await result.single()
            if data:
                await log_action(
                    "link_repository",
                    "agent",
                    summary=f"Linked repo '{data['repo_name']}' to '{data['project_title']}'",
                    details={
                        "repository_id": repo_id,
                        "repository_name": repo_name or data['repo_name'],
                        "project_id": project_id,
                        "project_name": project_name or data['project_title'],
                        "reason": reason
                    }
                )
                return {"linked": True, "repository": data["repo_name"], "project": data["project_title"]}
        except Exception as e:
            logger.warning("Failed to link repository", error=str(e))
        return None

    async def _link_project_to_goal(self, session, params: dict, reason: str) -> dict | None:
        """Link a project to a goal.

        Each project can only have one primary goal. If already linked, skip.
        """
        project_id = params.get("project_id")
        goal_id = params.get("goal_id")
        project_name = params.get("project_name", "")
        goal_name = params.get("goal_name", "")

        if not project_id or not goal_id:
            return None

        # Check if project already has a goal link
        check_query = """
        MATCH (p:Project {id: $project_id})-[r:PART_OF]->(g:Goal)
        RETURN g.id as existing_goal_id, g.title as existing_goal_title
        """
        try:
            check_result = await session.run(check_query, {"project_id": project_id})
            existing = await check_result.single()

            if existing:
                # Already has a goal - skip to prevent duplicates
                logger.debug(
                    "Project already linked to goal, skipping",
                    project_id=project_id,
                    existing_goal=existing["existing_goal_title"],
                )
                return {"skipped": True, "reason": f"Already linked to '{existing['existing_goal_title']}'"}

            # No existing goal - create the link
            query = """
            MATCH (p:Project {id: $project_id})
            MATCH (g:Goal {id: $goal_id})
            CREATE (p)-[rel:PART_OF {
                created_at: datetime(),
                created_by: 'autonomous_agent',
                reason: $reason
            }]->(g)
            RETURN p.title as project_title, g.title as goal_title
            """
            result = await session.run(query, {
                "project_id": project_id,
                "goal_id": goal_id,
                "reason": reason
            })
            data = await result.single()
            if data:
                await log_action(
                    "link_project_to_goal",
                    "agent",
                    summary=f"Linked project '{data['project_title']}' to goal '{data['goal_title']}'",
                    details={
                        "project_id": project_id,
                        "project_name": project_name or data['project_title'],
                        "goal_id": goal_id,
                        "goal_name": goal_name or data['goal_title'],
                        "reason": reason
                    }
                )
                return {"linked": True, "project": data["project_title"], "goal": data["goal_title"]}
        except Exception as e:
            logger.warning("Failed to link project to goal", error=str(e))
        return None

    async def _update_project_status(self, session, params: dict, reason: str) -> dict | None:
        """Update a project's status based on task completion."""
        project_id = params.get("project_id")
        new_status = params.get("new_status")

        if not project_id or not new_status:
            return None

        if new_status not in ["active", "completed", "on_hold", "cancelled"]:
            return None

        query = """
        MATCH (p:Project {id: $project_id})
        SET p.status = $new_status,
            p.updated_at = datetime(),
            p.status_updated_by = 'autonomous_agent',
            p.status_reason = $reason
        RETURN p.title as title, p.status as status
        """
        try:
            result = await session.run(query, {
                "project_id": project_id,
                "new_status": new_status,
                "reason": reason
            })
            data = await result.single()
            if data:
                await log_action(
                    "update_project_status",
                    "agent",
                    summary=f"Updated '{data['title']}' status to '{new_status}'",
                    details={"project_id": project_id, "new_status": new_status, "reason": reason}
                )
                return {"updated": True, "project": data["title"], "status": new_status}
        except Exception as e:
            logger.warning("Failed to update project status", error=str(e))
        return None

    async def _create_task(self, session, params: dict, reason: str) -> dict | None:
        """Create a new task to progress a goal or project.

        In 'propose' mode, sends task for approval instead of creating directly.
        Uses learned patterns to skip proposals likely to be rejected.

        Safety checks:
        1. Deduplication - skip if similar task already exists
        2. Throttling - skip if too many pending proposals for this project
        3. Learning - skip if historical approval rate is too low
        """
        import uuid
        from cognitex.config import get_settings
        from cognitex.agent.action_log import (
            propose_task,
            get_proposal_recommendation,
            get_pending_proposal_count,
        )

        settings = get_settings()

        # Handle both "title" and "task_title" since LLM may use either
        title = params.get("title") or params.get("task_title")
        project_id = params.get("project_id")
        goal_id = params.get("goal_id")
        description = params.get("description") or params.get("task_description", "")
        priority = params.get("priority", "medium")

        if not title:
            return None

        # 1. DEDUPLICATION CHECK
        # Skip if a task with the same title already exists (case-insensitive)
        try:
            check_query = """
            MATCH (t:Task)
            WHERE t.status IN ['pending', 'in_progress']
              AND toLower(t.title) = toLower($title)
            RETURN t.id as id
            """
            existing = await session.run(check_query, {"title": title})
            if await existing.single():
                logger.info("Skipping duplicate task creation", title=title)
                return {"skipped": True, "reason": "duplicate_task_exists"}
        except Exception as e:
            logger.warning("Deduplication check failed", error=str(e))

        # 2. THROTTLING CHECK
        # Don't flood the user with proposals for the same project
        if settings.task_creation_mode == "propose" and project_id:
            try:
                pending_count = await get_pending_proposal_count(project_id=project_id)
                if pending_count >= 3:
                    logger.info(
                        "Skipping proposal - too many pending for project",
                        project_id=project_id,
                        pending_count=pending_count,
                    )
                    return {"skipped": True, "reason": f"{pending_count} pending proposals exist for project"}
            except Exception as e:
                logger.warning("Throttling check failed", error=str(e))

        # 3. LEARNING CHECK - Check learned patterns before proposing (Phase 4)
        if settings.task_creation_mode == "propose":
            try:
                recommendation = await get_proposal_recommendation(
                    project_id=project_id,
                    priority=priority,
                )

                if not recommendation.get("should_propose", True):
                    # Skip proposal based on learned patterns
                    skip_reason = recommendation.get("reason", "low approval rate")
                    logger.info(
                        "Skipping proposal based on learned patterns",
                        title=title,
                        reason=skip_reason,
                        historical_rate=recommendation.get("historical_rate"),
                    )
                    await log_action(
                        "task_proposal_skipped",
                        "agent",
                        summary=f"Skipped proposing '{title}': {skip_reason}",
                        details={
                            "title": title,
                            "project_id": project_id,
                            "priority": priority,
                            "skip_reason": skip_reason,
                            "historical_rate": recommendation.get("historical_rate"),
                        }
                    )
                    return {"skipped": True, "reason": skip_reason}

                # Include historical rate in reason for context
                historical_rate = recommendation.get("historical_rate")
                if historical_rate is not None:
                    reason = f"{reason} (historical approval: {historical_rate:.0f}%)"

            except Exception as e:
                # Don't block proposal if learning check fails
                logger.warning("Failed to check proposal recommendation", error=str(e))

        # In propose mode, send for approval instead of creating
        if settings.task_creation_mode == "propose":
            proposal_id = await propose_task(
                title=title,
                description=description,
                project_id=project_id,
                goal_id=goal_id,
                priority=priority,
                reason=reason,
            )

            # Send Discord notification about the proposal
            await self._send_proposal_notification(title, reason, proposal_id)

            await log_action(
                "task_proposed",
                "agent",
                summary=f"Proposed task '{title}' for approval",
                details={
                    "proposal_id": proposal_id,
                    "title": title,
                    "project_id": project_id,
                    "reason": reason
                }
            )
            return {"proposed": True, "proposal_id": proposal_id, "title": title}

        # Auto mode - create directly
        task_id = f"task_{uuid.uuid4().hex[:12]}"

        query = """
        CREATE (t:Task {
            id: $task_id,
            title: $title,
            description: $description,
            status: 'pending',
            created_at: datetime(),
            created_by: 'autonomous_agent',
            creation_reason: $reason
        })
        RETURN t.id as id, t.title as title
        """
        try:
            result = await session.run(query, {
                "task_id": task_id,
                "title": title,
                "description": description,
                "reason": reason
            })
            data = await result.single()

            # Link to project if specified
            if project_id and data:
                link_query = """
                MATCH (t:Task {id: $task_id})
                MATCH (p:Project {id: $project_id})
                MERGE (t)-[:BELONGS_TO]->(p)
                RETURN p.title as project_title
                """
                link_result = await session.run(link_query, {
                    "task_id": task_id,
                    "project_id": project_id
                })
                await link_result.consume()

            if data:
                await log_action(
                    "create_task",
                    "agent",
                    summary=f"Created task '{title}'",
                    details={
                        "task_id": task_id,
                        "title": title,
                        "project_id": project_id,
                        "reason": reason
                    }
                )
                return {"created": True, "task_id": task_id, "title": title}
        except Exception as e:
            logger.warning("Failed to create task", error=str(e))
        return None

    async def _suggest_reschedule(self, session, params: dict, reason: str) -> dict | None:
        """Suggest rescheduling a high-friction task to a better time.

        Used when current state/energy doesn't match task requirements.
        Sends a notification suggesting the user reschedule to peak hours.
        """
        task_id = params.get("task_id")
        task_title = params.get("task_title", "Task")
        current_issue = params.get("current_issue", "Current state isn't optimal")
        suggested_time = params.get("suggested_time", "tomorrow morning")

        # Get temporal model for better suggestions
        try:
            from cognitex.agent.state_model import get_temporal_model
            temporal = get_temporal_model()
            peak_hour = temporal.get_peak_hour()
            suggested_time = temporal.suggest_reschedule_time(for_high_energy=True)
        except Exception as e:
            logger.debug("Could not get temporal suggestion", error=str(e))

        # Send notification suggesting reschedule
        try:
            from cognitex.agent.tools import SendNotificationTool

            message = (
                f"**Reschedule Suggestion**\n\n"
                f"**Task:** {task_title}\n"
                f"**Issue:** {current_issue}\n"
                f"**Suggested time:** {suggested_time}\n\n"
                f"_{reason}_"
            )

            tool = SendNotificationTool()
            await tool.execute(message=message, urgency="low")

            await log_action(
                "suggest_reschedule",
                "agent",
                summary=f"Suggested rescheduling '{task_title}' to {suggested_time}",
                details={
                    "task_id": task_id,
                    "task_title": task_title,
                    "current_issue": current_issue,
                    "suggested_time": suggested_time,
                    "reason": reason,
                }
            )

            logger.info(
                "Sent reschedule suggestion",
                task_title=task_title[:30],
                suggested_time=suggested_time,
            )

            return {
                "suggested": True,
                "task_id": task_id,
                "task_title": task_title,
                "suggested_time": suggested_time,
            }

        except Exception as e:
            logger.warning("Failed to send reschedule suggestion", error=str(e))
            return None

    async def _send_proposal_notification(
        self, title: str, reason: str, proposal_id: str
    ) -> None:
        """Send a Discord notification for a task proposal."""
        from cognitex.agent.tools import SendNotificationTool

        message = (
            f"**Task Proposal**\n\n"
            f"**Title:** {title}\n"
            f"**Reason:** {reason}\n\n"
            f"_Reply with `/approve {proposal_id}` or `/reject {proposal_id}`_"
        )

        tool = SendNotificationTool()
        await tool.execute(message=message, urgency="low")

    async def _flag_for_review(
        self, params: dict, reason: str, flagged_this_cycle: set[str] | None = None
    ) -> dict | None:
        """Flag an entity for human review and notify via Discord.

        Includes deduplication to avoid sending the same notification repeatedly.
        Uses fuzzy matching on entity names to catch variations.
        Also prevents duplicate flags within the same autonomous cycle.
        """
        from cognitex.agent.action_log import get_recent_notifications

        entity_type = params.get("entity_type")
        entity_name = params.get("entity_name", params.get("entity_id", "Unknown"))
        entity_id = params.get("entity_id")
        issue = params.get("issue", reason)

        # Create a normalized key for this entity
        entity_key = f"{entity_type}:{entity_name}".lower()

        # Check if already flagged in this cycle
        if flagged_this_cycle is not None:
            if entity_key in flagged_this_cycle:
                logger.debug("Skipping duplicate (same cycle)", entity_key=entity_key)
                return {"flagged": False, "reason": "duplicate_in_cycle"}

            # Also check for word overlap with items flagged this cycle
            current_words = set(w.lower() for w in entity_name.replace("/", " ").replace(",", " ").split() if len(w) > 3)
            for flagged_key in flagged_this_cycle:
                flagged_name = flagged_key.split(":", 1)[1] if ":" in flagged_key else flagged_key
                flagged_words = set(w.lower() for w in flagged_name.replace("/", " ").replace(",", " ").split() if len(w) > 3)
                if current_words and flagged_words:
                    overlap = len(current_words & flagged_words)
                    if overlap >= min(len(current_words), len(flagged_words)) * 0.5:
                        logger.debug("Skipping duplicate (cycle word overlap)", entity_name=entity_name)
                        return {"flagged": False, "reason": "duplicate_in_cycle"}

        # Check for recent duplicate notifications (same entity flagged in last 24 hours)
        recent = await get_recent_notifications(hours=24)
        for notif in recent:
            if notif.get("action_type") == "flag_for_review":
                details = notif.get("details", {})
                prev_name = details.get("entity_name", "")
                prev_type = details.get("entity_type")

                # Skip if same entity_id
                if entity_id and details.get("entity_id") == entity_id:
                    logger.debug("Skipping duplicate (same entity_id)", entity_id=entity_id)
                    return {"flagged": False, "reason": "duplicate_notification"}

                # Skip if same type and names share significant overlap
                if prev_type == entity_type and prev_name and entity_name:
                    # Check if any key words from current name appear in previous
                    current_words = set(w.lower() for w in entity_name.replace("/", " ").replace(",", " ").split() if len(w) > 3)
                    prev_words = set(w.lower() for w in prev_name.replace("/", " ").replace(",", " ").split() if len(w) > 3)
                    # If more than half the words overlap, consider it a duplicate
                    if current_words and prev_words:
                        overlap = len(current_words & prev_words)
                        if overlap >= min(len(current_words), len(prev_words)) * 0.5:
                            logger.debug(
                                "Skipping duplicate (name overlap)",
                                entity_name=entity_name,
                                prev_name=prev_name,
                                overlap=overlap,
                            )
                            return {"flagged": False, "reason": "duplicate_notification"}

        # Track that we're flagging this entity in this cycle
        if flagged_this_cycle is not None:
            flagged_this_cycle.add(entity_key)

        await log_action(
            "flag_for_review",
            "agent",
            summary=f"Flagged {entity_type} for review: {issue[:100]}",
            details={
                "entity_type": entity_type,
                "entity_id": entity_id,
                "entity_name": entity_name,
                "issue": issue,
                "reason": reason
            }
        )

        # Send Discord notification
        await self._send_review_notification(entity_type, entity_name, issue)

        return {"flagged": True, "entity_type": entity_type, "entity_id": entity_id}

    async def _send_review_notification(
        self, entity_type: str, entity_name: str, issue: str
    ) -> None:
        """Send a Discord notification for items flagged for review."""
        from cognitex.agent.tools import SendNotificationTool

        message = (
            f"**Review Required**\n\n"
            f"**{entity_type}:** {entity_name}\n"
            f"**Issue:** {issue}\n\n"
            f"_Flagged by autonomous agent_"
        )

        tool = SendNotificationTool()
        await tool.execute(message=message, urgency="normal")

    # =========================================================================
    # Digital Twin Actions
    # =========================================================================

    async def _draft_email(self, session, params: dict, reason: str) -> dict | None:
        """
        Draft an email reply in the user's voice.

        The draft is stored in the graph for user review before sending.
        """
        import uuid

        email_id = params.get("email_id")
        to = params.get("to")
        subject = params.get("subject", "")
        body = params.get("body", "")
        original_subject = params.get("original_subject", "")

        if not email_id or not body:
            logger.warning("DRAFT_EMAIL missing required fields", params=params)
            return None

        draft_id = f"draft_{uuid.uuid4().hex[:12]}"

        # Store the draft in the graph, linked to the original email
        query = """
        MATCH (original:Email {gmail_id: $email_id})
        CREATE (draft:EmailDraft {
            id: $draft_id,
            to: $to,
            subject: $subject,
            body: $body,
            status: 'pending_review',
            created_at: datetime(),
            created_by: 'autonomous_agent',
            reason: $reason
        })
        CREATE (draft)-[:REPLY_TO]->(original)
        RETURN draft.id as id, original.subject as original_subject
        """
        try:
            result = await session.run(query, {
                "email_id": email_id,
                "draft_id": draft_id,
                "to": to or "",
                "subject": subject,
                "body": body,
                "reason": reason
            })
            data = await result.single()

            if data:
                await log_action(
                    "draft_email",
                    "agent",
                    summary=f"Drafted reply to '{original_subject or data.get('original_subject', 'email')}'",
                    details={
                        "draft_id": draft_id,
                        "email_id": email_id,
                        "to": to,
                        "subject": subject,
                        "body_preview": body[:200] + "..." if len(body) > 200 else body,
                        "reason": reason
                    }
                )

                # Notify user about the draft
                await self._send_draft_notification(
                    original_subject or data.get('original_subject', 'email'),
                    to,
                    body[:300]
                )

                return {"drafted": True, "draft_id": draft_id, "email_id": email_id}
        except Exception as e:
            logger.warning("Failed to draft email", error=str(e))
        return None

    async def _send_draft_notification(
        self, original_subject: str, to: str, body_preview: str
    ) -> None:
        """Send a Discord notification about a drafted email."""
        from cognitex.agent.tools import SendNotificationTool

        message = (
            f"**Email Draft Ready for Review**\n\n"
            f"**Replying to:** {original_subject}\n"
            f"**To:** {to}\n\n"
            f"**Preview:**\n{body_preview}...\n\n"
            f"_Review and send from the dashboard_"
        )

        tool = SendNotificationTool()
        await tool.execute(message=message, urgency="normal")

    async def _compile_context_pack(self, session, params: dict, reason: str) -> dict | None:
        """
        Compile a context pack for an upcoming meeting or decision.

        Gathers relevant documents, tasks, and key points into a briefing.
        """
        import uuid

        calendar_id = params.get("calendar_id")
        meeting_title = params.get("meeting_title", "")
        context_summary = params.get("context_summary", "")
        relevant_documents = params.get("relevant_documents", [])
        relevant_tasks = params.get("relevant_tasks", [])
        key_points = params.get("key_points", [])

        if not calendar_id and not meeting_title:
            logger.warning("COMPILE_CONTEXT_PACK missing calendar_id or meeting_title")
            return None

        pack_id = f"context_{uuid.uuid4().hex[:12]}"

        # Create the context pack node
        query = """
        CREATE (cp:ContextPack {
            id: $pack_id,
            title: $meeting_title,
            summary: $context_summary,
            key_points: $key_points,
            status: 'ready',
            created_at: datetime(),
            created_by: 'autonomous_agent',
            reason: $reason
        })
        RETURN cp.id as id
        """
        try:
            result = await session.run(query, {
                "pack_id": pack_id,
                "meeting_title": meeting_title,
                "context_summary": context_summary,
                "key_points": key_points,
                "reason": reason
            })
            data = await result.single()

            if not data:
                return None

            # Link to calendar event if provided
            if calendar_id:
                link_query = """
                MATCH (cp:ContextPack {id: $pack_id})
                MATCH (ce:CalendarEvent {id: $calendar_id})
                MERGE (cp)-[:PREPARED_FOR]->(ce)
                """
                await session.run(link_query, {
                    "pack_id": pack_id,
                    "calendar_id": calendar_id
                })

            # Link to relevant documents
            if relevant_documents:
                doc_query = """
                MATCH (cp:ContextPack {id: $pack_id})
                UNWIND $doc_ids as doc_id
                MATCH (d:Document {drive_id: doc_id})
                MERGE (cp)-[:REFERENCES]->(d)
                """
                await session.run(doc_query, {
                    "pack_id": pack_id,
                    "doc_ids": relevant_documents
                })

            # Link to relevant tasks
            if relevant_tasks:
                task_query = """
                MATCH (cp:ContextPack {id: $pack_id})
                UNWIND $task_ids as task_id
                MATCH (t:Task {id: task_id})
                MERGE (cp)-[:REFERENCES]->(t)
                """
                await session.run(task_query, {
                    "pack_id": pack_id,
                    "task_ids": relevant_tasks
                })

            await log_action(
                "compile_context_pack",
                "agent",
                summary=f"Compiled context pack for '{meeting_title}'",
                details={
                    "pack_id": pack_id,
                    "calendar_id": calendar_id,
                    "meeting_title": meeting_title,
                    "documents_count": len(relevant_documents),
                    "tasks_count": len(relevant_tasks),
                    "key_points": key_points,
                    "reason": reason
                }
            )

            # Notify about the context pack
            await self._send_context_pack_notification(meeting_title, key_points)

            return {
                "compiled": True,
                "pack_id": pack_id,
                "meeting_title": meeting_title,
                "documents": len(relevant_documents),
                "tasks": len(relevant_tasks)
            }
        except Exception as e:
            logger.warning("Failed to compile context pack", error=str(e))
        return None

    async def _send_context_pack_notification(
        self, meeting_title: str, key_points: list[str]
    ) -> None:
        """Send a Discord notification about a compiled context pack."""
        from cognitex.agent.tools import SendNotificationTool

        points_text = "\n".join([f"• {p}" for p in key_points[:5]]) if key_points else "No key points"

        message = (
            f"**Context Pack Ready**\n\n"
            f"**Meeting:** {meeting_title}\n\n"
            f"**Key Points:**\n{points_text}\n\n"
            f"_View full context in the dashboard_"
        )

        tool = SendNotificationTool()
        await tool.execute(message=message, urgency="normal")

    async def _schedule_block(self, session, params: dict, reason: str) -> dict | None:
        """
        Schedule a focus block for a project or task.

        Creates a calendar event suggestion for the user to approve.
        """
        import uuid

        title = params.get("title", "Focus Time")
        project_id = params.get("project_id")
        task_id = params.get("task_id")
        duration_hours = params.get("duration_hours", 2)
        suggested_day = params.get("suggested_day", "tomorrow")

        block_id = f"block_{uuid.uuid4().hex[:12]}"

        # Create a suggested calendar block
        query = """
        CREATE (sb:SuggestedBlock {
            id: $block_id,
            title: $title,
            duration_hours: $duration_hours,
            suggested_day: $suggested_day,
            status: 'pending_approval',
            created_at: datetime(),
            created_by: 'autonomous_agent',
            reason: $reason
        })
        RETURN sb.id as id
        """
        try:
            result = await session.run(query, {
                "block_id": block_id,
                "title": title,
                "duration_hours": duration_hours,
                "suggested_day": suggested_day,
                "reason": reason
            })
            data = await result.single()

            if not data:
                return None

            # Link to project if provided
            if project_id:
                link_query = """
                MATCH (sb:SuggestedBlock {id: $block_id})
                MATCH (p:Project {id: $project_id})
                MERGE (sb)-[:FOR_PROJECT]->(p)
                RETURN p.title as project_title
                """
                link_result = await session.run(link_query, {
                    "block_id": block_id,
                    "project_id": project_id
                })
                link_data = await link_result.single()
                project_title = link_data.get("project_title") if link_data else None
            else:
                project_title = None

            # Link to task if provided
            if task_id:
                link_query = """
                MATCH (sb:SuggestedBlock {id: $block_id})
                MATCH (t:Task {id: $task_id})
                MERGE (sb)-[:FOR_TASK]->(t)
                """
                await session.run(link_query, {
                    "block_id": block_id,
                    "task_id": task_id
                })

            await log_action(
                "schedule_block",
                "agent",
                summary=f"Suggested focus block: '{title}' ({duration_hours}h)",
                details={
                    "block_id": block_id,
                    "title": title,
                    "project_id": project_id,
                    "task_id": task_id,
                    "duration_hours": duration_hours,
                    "suggested_day": suggested_day,
                    "reason": reason
                }
            )

            # Notify about the schedule suggestion
            await self._send_schedule_notification(
                title, duration_hours, suggested_day, project_title, reason
            )

            return {
                "suggested": True,
                "block_id": block_id,
                "title": title,
                "duration_hours": duration_hours
            }
        except Exception as e:
            logger.warning("Failed to schedule block", error=str(e))
        return None

    async def _send_schedule_notification(
        self,
        title: str,
        duration_hours: int,
        suggested_day: str,
        project_title: str | None,
        reason: str
    ) -> None:
        """Send a Discord notification about a suggested calendar block."""
        from cognitex.agent.tools import SendNotificationTool

        project_line = f"**Project:** {project_title}\n" if project_title else ""

        message = (
            f"**Focus Time Suggestion**\n\n"
            f"**Title:** {title}\n"
            f"{project_line}"
            f"**Duration:** {duration_hours} hours\n"
            f"**When:** {suggested_day}\n\n"
            f"**Why:** {reason[:200]}\n\n"
            f"_Approve or modify in the dashboard_"
        )

        tool = SendNotificationTool()
        await tool.execute(message=message, urgency="normal")

    async def run_once(self) -> dict:
        """Run a single cycle manually (for testing/triggering)."""
        await self._run_cycle()
        return {"status": "completed"}


# Singleton instance
_autonomous_agent: AutonomousAgent | None = None


async def get_autonomous_agent() -> AutonomousAgent:
    """Get or create the autonomous agent instance."""
    global _autonomous_agent
    if _autonomous_agent is None:
        _autonomous_agent = AutonomousAgent()
    return _autonomous_agent


async def start_autonomous_agent() -> AutonomousAgent:
    """Start the autonomous agent."""
    agent = await get_autonomous_agent()
    await agent.start()
    return agent


async def stop_autonomous_agent() -> None:
    """Stop the autonomous agent."""
    global _autonomous_agent
    if _autonomous_agent:
        await _autonomous_agent.stop()
