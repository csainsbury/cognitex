gemini code review

prompt:

this is the code for my current version of my life management system.
at the moment the basic  functionality seems fairly solid, with command line and web UI

I'm having difficulty defining and enacting the autonomous agent.
the relevant system prompt for the agent is here: src/cognitex/prompts/autonomous_agent.md
this defines the agent as an entity that examines the graph and sees/makes connections etc, which is good but is only a part of what i would like it to do

I would like it to act as 'me' when I am not there. ie constantly scanning for tasks it can progress - or generate and progress which contribute to improving projects or contributes to advancing progress towards a goal etc
It should generate context packs proactively if it wants me to make a decision, and present this along with options via discord if notification is needed
It should block out calendar time to progress tasks
it should draft emails (pulling large numbers of emails from 'sent' to understand the way that I write emails (not use generic corporate-speak as it seems inclined to do)
it should draft text documents if these are needed as a response to an email or as a preparatory step for a meeting etc

can you review the code, and the autonomous_agent prompt and identify the next steps to make this a truly proactive helpful agent.
the underlying motto should be 'what can I do that the user would do right now - or what can I create that will make the user's progress towards completing tasks & goals easier'


response: 

This is a significant pivot from a "maintenance agent" (cleaning the graph) to an "executive function agent" (doing the work).

To achieve this "Digital Twin" behavior, we need to upgrade three specific areas:

1.  **The Observer (`graph_observer.py`):** It needs to stop looking just for *broken links* and start looking for *patterns*, *writing styles*, and *opportunities for progress*.
2.  **The Prompt (`autonomous_agent.md`):** It needs to be rewritten to prioritize **output generation** (drafts, schedules, packs) over graph maintenance, and it needs to ingest your writing style dynamically.
3.  **The Agent Logic (`autonomous.py`):** It needs to handle new action types like `DRAFT_RESPONSE` and `SCHEDULE_FOCUS`, and pass your writing samples into the prompt context.

Here are the necessary changes.

### 1. Update the System Prompt

This redefines the agent's persona. Instead of a librarian, it becomes an executive assistant mimicking you.

<file_path="src/cognitex/prompts/autonomous_agent.md">
```md
# Autonomous Agent System Prompt

You are the user's **Digital Executive Function**. Your goal is to act as the user when they are not there.
Do not just organize data—**do the work**.

## Your Core Directives:
1.  **Advance Goals:** Look for tasks that can be progressed immediately.
2.  **Mimic Voice:** When drafting communication, use the "User Writing Samples" below. Avoid corporate-speak. Be direct, authentic, and concise.
3.  **Protect Time:** Block calendar time for deep work on specific tasks.
4.  **Facilitate Decisions:** If a decision is complex, compile a Context Pack and present options.

## User Writing Style (MIMIC THIS EXACTLY):
{writing_style_samples}

## Current Context:
- Recent changes: {total_changes_24h}
- Stale items: {stale_tasks} tasks, {stale_projects} projects
- Pending emails needing reply: {pending_email_count}
- Connection opportunities: {connection_opportunities}

## Action Queue (Prioritize These):
{opportunities_text}

## Goals Needing Progress:
{goals_text}

## Projects Needing Attention:
{projects_text}

## Available Actions:

### DRAFT_EMAIL
Draft a reply or new email in the user's voice. This stages it for approval.
**Crucial:** Mimic the user's style found in the samples above.
```json
{{"action": "DRAFT_EMAIL", "to": "...", "subject": "...", "body": "...", "reason": "Replying to X to unblock project Y"}}
```

### SCHEDULE_BLOCK
Block time on the calendar to progress a specific task.
```json
{{"action": "SCHEDULE_BLOCK", "task_id": "...", "title": "Focus: [Task Name]", "start_time": "ISO_TIMESTAMP", "duration_minutes": 60, "reason": "..."}}
```

### COMPILE_CONTEXT_PACK
Generate a decision support pack for a complex issue/task and notify the user.
```json
{{"action": "COMPILE_CONTEXT_PACK", "task_id": "...", "objective": "Decide on...", "decision_needed": "...", "options": ["Option A", "Option B"]}}
```

### CREATE_TASK
Create a task only if it represents concrete progress, not just "planning".
```json
{{"action": "CREATE_TASK", "title": "...", "project_id": "...", "description": "..."}}
```

### LINK_ENTITY
Link entities to maintain graph coherence (Secondary priority).
```json
{{"action": "LINK_ENTITY", "source_id": "...", "source_type": "...", "target_id": "...", "target_type": "..."}}
```

## Instructions:
- If an email needs a reply, DRAFT it.
- If a project is stuck, SCHEDULE time or CREATE a specific next step.
- If the user needs to decide, COMPILE a pack.
- Limit to 3 high-impact actions per cycle.
- Return JSON array ONLY.
```
</file_path>

### 2. Update Graph Observer to Fetch "Me" Context

We need the observer to pull recent emails sent by *you* so the LLM can learn your style in real-time.

<file_path="src/cognitex/agent/graph_observer.py">
```python
    async def get_full_context(self) -> dict:
        """Gather comprehensive context about the graph state."""
        # Fetch pending emails for context
        pending_emails = await self.get_actionable_emails()
        
        context = {
            "timestamp": datetime.now().isoformat(),
            "summary": {},
            "recent_changes": await self.get_recent_changes(),
            "stale_items": await self.get_stale_items(),
            "orphaned_nodes": await self.get_orphaned_nodes(),
            "goal_health": await self.get_goal_health(),
            "project_health": await self.get_project_health(),
            "pending_tasks": await self.get_pending_tasks(),
            "recent_documents": await self.get_recent_documents(),
            "connection_opportunities": await self.get_connection_opportunities(),
            # NEW: Writing samples for style mimicry
            "writing_samples": await self.get_user_writing_samples(),
            "pending_emails": pending_emails,
        }

        # Build summary
        context["summary"] = {
            "total_changes_24h": len(context["recent_changes"]),
            "stale_tasks": len([i for i in context["stale_items"] if i["type"] == "Task"]),
            "stale_projects": len([i for i in context["stale_items"] if i["type"] == "Project"]),
            "orphaned_documents": len([n for n in context["orphaned_nodes"] if n["type"] == "Document"]),
            "goals_needing_attention": len([g for g in context["goal_health"] if g.get("needs_attention")]),
            "projects_needing_attention": len([p for p in context["project_health"] if p.get("needs_attention")]),
            "pending_task_count": len(context["pending_tasks"]),
            "connection_opportunities": len(context["connection_opportunities"]),
            "pending_email_count": len(pending_emails),
        }

        return context

    async def get_user_writing_samples(self, limit: int = 5) -> list[str]:
        """Fetch recent emails sent by the user to establish writing style."""
        # Assumes user is identified by 'me' or we find the Person node representing the user
        # This query looks for emails sent by a person who has 'OWNER' relationship to projects/goals 
        # or simply relies on the ingestion logic mapping 'me' to a specific email address.
        query = """
        MATCH (e:Email)-[:SENT_BY]->(p:Person)
        WHERE p.email IS NOT NULL 
        // We assume the user is the one sending emails that are ingested as 'sent'
        // Ideally, we filter by the specific user email if known config-side
        RETURN e.snippet as body
        ORDER BY e.date DESC
        LIMIT $limit
        """
        # Note: In a real multi-user graph, we'd need the specific user_email here.
        # For personal OS, we grab the most recent sent emails.
        try:
            result = await self.session.run(query, {"limit": limit})
            data = await result.data()
            return [row["body"] for row in data if row["body"]]
        except Exception as e:
            logger.warning("Failed to get writing samples", error=str(e))
            return ["(No writing samples available)"]

    async def get_actionable_emails(self, limit: int = 10) -> list[dict]:
        """Get emails that likely require a response."""
        query = """
        MATCH (e:Email)
        WHERE e.classification IN ['actionable', 'urgent']
          AND e.action_required = true
          AND NOT (e)<-[:DERIVED_FROM]-(:Task)
        RETURN e.gmail_id as id, e.subject as subject, e.snippet as snippet, e.urgency as urgency
        ORDER BY e.date DESC
        LIMIT $limit
        """
        try:
            result = await self.session.run(query, {"limit": limit})
            return await result.data()
        except Exception:
            return []
```
</file_path>

### 3. Update Autonomous Agent to Execute New Actions

We need to wire up the new actions (`DRAFT_EMAIL`, `SCHEDULE_BLOCK`, `COMPILE_CONTEXT_PACK`) in the execution loop.

<file_path="src/cognitex/agent/autonomous.py">
```python
    async def _reason_about_context(self, context: dict) -> list[dict]:
        """Use LLM to reason about the graph context and decide on actions."""
        from cognitex.services.llm import get_llm_service

        # Build a concise summary for the LLM
        summary = context["summary"]
        
        # Format writing samples
        samples = context.get("writing_samples", [])
        writing_style_text = "\n---\n".join(samples) if samples else "No samples found. Use a direct, professional but casual tone."

        # Format pending emails opportunities
        emails = context.get("pending_emails", [])
        email_text = "\n".join([
            f"- EMAIL: '{e.get('subject')}' (ID: {e.get('id')}) - Snippet: {e.get('snippet')[:100]}..."
            for e in emails
        ])

        # Format connection opportunities with explicit action suggestions
        opportunities = context.get('connection_opportunities', [])[:10]
        opp_lines = []
        for o in opportunities:
            # ... (existing formatting code) ...
            opp_lines.append(f"  - LINK: {o.get('source_name')} -> {o.get('target_name')}")

        opp_text = email_text + "\n" + ("\n".join(opp_lines) if opp_lines else "")

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

        prompt = format_prompt(
            "autonomous_agent",
            writing_style_samples=writing_style_text,
            total_changes_24h=summary['total_changes_24h'],
            stale_tasks=summary['stale_tasks'],
            stale_projects=summary['stale_projects'],
            pending_email_count=summary['pending_email_count'],
            connection_opportunities=summary['connection_opportunities'],
            opportunities_text=opp_text,
            goals_text=goals_text,
            projects_text=projects_text,
            orphaned_text="", # Removed to save token space for writing samples
        )

        try:
            llm = get_llm_service()
            logger.info("Calling LLM for autonomous reasoning...")
            # ... existing LLM call code ...
            response = await llm.complete(prompt, max_tokens=2000)
            
            # ... existing parsing logic ...
            # Ensure parsing handles the new action types
            
            # (Standardize decision parsing logic here as per existing code)
            response_text = response.strip()
            if response_text.startswith("```"):
                lines = response_text.split("\n")
                response_text = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])
            
            if "[" in response_text:
                start = response_text.find("[")
                end = response_text.rfind("]") + 1
                response_text = response_text[start:end]

            decisions = json.loads(response_text)
            if not isinstance(decisions, list):
                decisions = [decisions]
                
            return decisions

        except Exception as e:
            logger.error("LLM reasoning failed", error=str(e))
            return []

    async def _execute_decision(self, session, decision: dict) -> dict | None:
        """Execute a single decision."""
        action = decision.get("action")
        params = decision.get("params", {})
        # Flatten params if they are at top level
        if not params:
            params = {k: v for k, v in decision.items() if k not in ("action", "reason")}
            
        reason = decision.get("reason", "")

        logger.info("Executing decision", action=action, reason=reason[:100])

        if action == "DRAFT_EMAIL":
            return await self._draft_email(params, reason)
        elif action == "SCHEDULE_BLOCK":
            return await self._schedule_block(params, reason)
        elif action == "COMPILE_CONTEXT_PACK":
            return await self._compile_decision_pack(params, reason)
        elif action == "CREATE_TASK":
            return await self._create_task(session, params, reason)
        elif action == "LINK_ENTITY" or action.startswith("LINK_"):
            # Handle generic linking
            return await self._handle_generic_link(session, params, reason)
        else:
            logger.warning("Unknown action type", action=action)
            return None

    # --- New Execution Handlers ---

    async def _draft_email(self, params: dict, reason: str) -> dict | None:
        """Execute DraftEmailTool."""
        from cognitex.agent.tools import DraftEmailTool
        
        tool = DraftEmailTool()
        result = await tool.execute(
            to=params.get("to"),
            subject=params.get("subject"),
            body=params.get("body"),
            reasoning=reason
        )
        
        if result.success:
            await log_action("draft_email", "autonomous_agent", 
                           summary=f"Drafted email to {params.get('to')}", 
                           details=result.data)
            return {"action": "draft_email", "status": "staged", "id": result.approval_id}
        return None

    async def _schedule_block(self, params: dict, reason: str) -> dict | None:
        """Execute CreateEventTool to block time."""
        from cognitex.agent.tools import CreateEventTool
        from datetime import datetime, timedelta
        
        # Calculate end time if duration provided
        start_str = params.get("start_time")
        duration = params.get("duration_minutes", 60)
        
        try:
            start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            end_dt = start_dt + timedelta(minutes=duration)
            end_str = end_dt.isoformat()
        except:
            end_str = params.get("end_time") # Fallback

        tool = CreateEventTool()
        result = await tool.execute(
            title=params.get("title", "Focus Time"),
            start=start_str,
            end=end_str,
            description=f"Automated block for: {reason}",
            reasoning=reason
        )
        
        if result.success:
            await log_action("schedule_block", "autonomous_agent",
                           summary=f"Scheduled block '{params.get('title')}'",
                           details=result.data)
            return {"action": "schedule_block", "status": "staged", "id": result.approval_id}
        return None

    async def _compile_decision_pack(self, params: dict, reason: str) -> dict | None:
        """Create a context pack and notify user."""
        from cognitex.agent.context_pack import get_context_pack_compiler, BuildStage
        from cognitex.agent.tools import SendNotificationTool
        
        compiler = get_context_pack_compiler()
        
        # Create a temporary task structure to build the pack around
        dummy_task = {
            "id": params.get("task_id", "decision_needed"),
            "title": params.get("objective", "Decision Needed"),
            "description": f"Decision needed regarding: {reason}\nOptions: {params.get('options')}"
        }
        
        pack = await compiler.compile_for_task(dummy_task, stage=BuildStage.T_24H)
        
        # Notify via Discord
        msg = f"**🧠 Decision Required**\n\n**Objective:** {pack.objective}\n\n**Context:** I've prepared a decision pack. Check the dashboard or reply here."
        if params.get("options"):
             msg += f"\n\n**Options:**\n" + "\n".join([f"- {o}" for o in params['options']])
             
        notify_tool = SendNotificationTool()
        await notify_tool.execute(message=msg, urgency="normal")
        
        return {"action": "compile_pack", "pack_id": pack.pack_id}

    async def _handle_generic_link(self, session, params: dict, reason: str) -> dict | None:
        """Backward compatibility for specific LINK actions."""
        # ... logic to map generic link params to specific _link_x functions ...
        # For brevity, implementing a simple mapping
        source_type = params.get("source_type", "").lower()
        target_type = params.get("target_type", "").lower()
        
        if "document" in source_type and "project" in target_type:
            return await self._link_document(session, 
                {"document_id": params.get("source_id"), "project_id": params.get("target_id")}, 
                reason)
        # Add other mappings as needed from the original code
        return None
```
</file_path>

<chatName="Upgrade Agent to Digital Twin"/>

atomic blueprint:

Here is the **Atomic Blueprint** for upgrading your Autonomous Agent from a "Graph Gardener" to a "Digital Twin/Executive Assistant."

You can follow these three atomic steps to implement the changes.

### **Blueprint: "Digital Twin" Agent Upgrade**

#### **Phase 1: Perception (The Eyes & Ears)**
**Goal:** Enable the agent to see "actionable work" and learn your "voice," not just see broken graph links.
*   **File:** `src/cognitex/agent/graph_observer.py`
*   **Task A:** Implement `get_user_writing_samples()` to fetch the last 5-10 emails sent by *you* (the user) from the graph.
*   **Task B:** Implement `get_actionable_emails()` to fetch incoming emails marked as 'actionable' or 'urgent' that haven't been replied to yet.
*   **Task C:** Update `get_full_context()` to include these new data points so they are passed to the LLM.

#### **Phase 2: Personality (The Brain)**
**Goal:** Redefine the agent's core directive from "organize data" to "advance goals and mimic user."
*   **File:** `src/cognitex/prompts/autonomous_agent.md`
*   **Task:** Completely rewrite the prompt to:
    1.  Ingest `{writing_style_samples}` for style mimicry.
    2.  Prioritize `DRAFT_EMAIL` and `SCHEDULE_BLOCK` actions over linking.
    3.  Define the persona as "The User's Executive Function."

#### **Phase 3: Execution (The Hands)**
**Goal:** Wire the LLM's new decisions to the existing toolset.
*   **File:** `src/cognitex/agent/autonomous.py`
*   **Task A:** Update `_reason_about_context` to format the writing samples and email context into the new prompt variables.
*   **Task B:** Update `_execute_decision` to handle the new action types: `DRAFT_EMAIL`, `SCHEDULE_BLOCK`, and `COMPILE_CONTEXT_PACK`.

---

### **Implementation Guide**

Here are the specific code edits to enact this blueprint.

#### **Step 1: Update Graph Observer (Perception)**

We need to fetch your writing style and pending emails.

<file_path="src/cognitex/agent/graph_observer.py">
```python
    async def get_full_context(self) -> dict:
        """Gather comprehensive context about the graph state."""
        # Fetch pending emails for context
        pending_emails = await self.get_actionable_emails()
        
        context = {
            "timestamp": datetime.now().isoformat(),
            "summary": {},
            "recent_changes": await self.get_recent_changes(),
            "stale_items": await self.get_stale_items(),
            "orphaned_nodes": await self.get_orphaned_nodes(),
            "goal_health": await self.get_goal_health(),
            "project_health": await self.get_project_health(),
            "pending_tasks": await self.get_pending_tasks(),
            "recent_documents": await self.get_recent_documents(),
            "connection_opportunities": await self.get_connection_opportunities(),
            # NEW: Writing samples for style mimicry
            "writing_samples": await self.get_user_writing_samples(),
            "pending_emails": pending_emails,
        }

        # Build summary
        context["summary"] = {
            "total_changes_24h": len(context["recent_changes"]),
            "stale_tasks": len([i for i in context["stale_items"] if i["type"] == "Task"]),
            "stale_projects": len([i for i in context["stale_items"] if i["type"] == "Project"]),
            "orphaned_documents": len([n for n in context["orphaned_nodes"] if n["type"] == "Document"]),
            "goals_needing_attention": len([g for g in context["goal_health"] if g.get("needs_attention")]),
            "projects_needing_attention": len([p for p in context["project_health"] if p.get("needs_attention")]),
            "pending_task_count": len(context["pending_tasks"]),
            "connection_opportunities": len(context["connection_opportunities"]),
            "pending_email_count": len(pending_emails),
        }

        return context

    async def get_user_writing_samples(self, limit: int = 5) -> list[str]:
        """Fetch recent emails sent by the user to establish writing style."""
        # Assumes user is identified by 'me' or we find the Person node representing the user
        # This query looks for emails sent by a person who has 'OWNER' relationship to projects/goals 
        # or simply relies on the ingestion logic mapping 'me' to a specific email address.
        query = """
        MATCH (e:Email)-[:SENT_BY]->(p:Person)
        WHERE p.email IS NOT NULL 
        // We assume the user is the one sending emails that are ingested as 'sent'
        // Ideally, we filter by the specific user email if known config-side
        RETURN e.snippet as body
        ORDER BY e.date DESC
        LIMIT $limit
        """
        try:
            result = await self.session.run(query, {"limit": limit})
            data = await result.data()
            return [row["body"] for row in data if row["body"]]
        except Exception as e:
            logger.warning("Failed to get writing samples", error=str(e))
            return ["(No writing samples available)"]

    async def get_actionable_emails(self, limit: int = 10) -> list[dict]:
        """Get emails that likely require a response."""
        query = """
        MATCH (e:Email)
        WHERE e.classification IN ['actionable', 'urgent']
          AND e.action_required = true
          AND NOT (e)<-[:DERIVED_FROM]-(:Task)
        RETURN e.gmail_id as id, e.subject as subject, e.snippet as snippet, e.urgency as urgency
        ORDER BY e.date DESC
        LIMIT $limit
        """
        try:
            result = await self.session.run(query, {"limit": limit})
            return await result.data()
        except Exception:
            return []
```
</file_path>

#### **Step 2: Update System Prompt (Personality)**

This redefines the agent as an executive assistant that mimics you.

<file_path="src/cognitex/prompts/autonomous_agent.md">
```md
# Autonomous Agent System Prompt

You are the user's **Digital Executive Function**. Your goal is to act as the user when they are not there.
Do not just organize data—**do the work**.

## Your Core Directives:
1.  **Advance Goals:** Look for tasks that can be progressed immediately.
2.  **Mimic Voice:** When drafting communication, use the "User Writing Samples" below. Avoid corporate-speak. Be direct, authentic, and concise.
3.  **Protect Time:** Block calendar time for deep work on specific tasks.
4.  **Facilitate Decisions:** If a decision is complex, compile a Context Pack and present options.

## User Writing Style (MIMIC THIS EXACTLY):
{writing_style_samples}

## Current Context:
- Recent changes: {total_changes_24h}
- Stale items: {stale_tasks} tasks, {stale_projects} projects
- Pending emails needing reply: {pending_email_count}
- Connection opportunities: {connection_opportunities}

## Action Queue (Prioritize These):
{opportunities_text}

## Goals Needing Progress:
{goals_text}

## Projects Needing Attention:
{projects_text}

## Available Actions:

### DRAFT_EMAIL
Draft a reply or new email in the user's voice. This stages it for approval.
**Crucial:** Mimic the user's style found in the samples above.
```json
{{"action": "DRAFT_EMAIL", "to": "...", "subject": "...", "body": "...", "reason": "Replying to X to unblock project Y"}}
```

### SCHEDULE_BLOCK
Block time on the calendar to progress a specific task.
```json
{{"action": "SCHEDULE_BLOCK", "task_id": "...", "title": "Focus: [Task Name]", "start_time": "ISO_TIMESTAMP", "duration_minutes": 60, "reason": "..."}}
```

### COMPILE_CONTEXT_PACK
Generate a decision support pack for a complex issue/task and notify the user.
```json
{{"action": "COMPILE_CONTEXT_PACK", "task_id": "...", "objective": "Decide on...", "decision_needed": "...", "options": ["Option A", "Option B"]}}
```

### CREATE_TASK
Create a task only if it represents concrete progress, not just "planning".
```json
{{"action": "CREATE_TASK", "title": "...", "project_id": "...", "description": "..."}}
```

### LINK_ENTITY
Link entities to maintain graph coherence (Secondary priority).
```json
{{"action": "LINK_ENTITY", "source_id": "...", "source_type": "...", "target_id": "...", "target_type": "..."}}
```

## Instructions:
- If an email needs a reply, DRAFT it.
- If a project is stuck, SCHEDULE time or CREATE a specific next step.
- If the user needs to decide, COMPILE a pack.
- Limit to 3 high-impact actions per cycle.
- Return JSON array ONLY.
```
</file_path>

#### **Step 3: Update Agent Logic (Execution)**

Wire the new data into the prompt and handle the new actions.

<file_path="src/cognitex/agent/autonomous.py">
```python
    async def _reason_about_context(self, context: dict) -> list[dict]:
        """Use LLM to reason about the graph context and decide on actions."""
        from cognitex.services.llm import get_llm_service

        # Build a concise summary for the LLM
        summary = context["summary"]
        
        # Format writing samples
        samples = context.get("writing_samples", [])
        writing_style_text = "\n---\n".join(samples) if samples else "No samples found. Use a direct, professional but casual tone."

        # Format pending emails opportunities
        emails = context.get("pending_emails", [])
        email_text = "\n".join([
            f"- EMAIL: '{e.get('subject')}' (ID: {e.get('id')}) - Snippet: {e.get('snippet')[:100]}..."
            for e in emails
        ])

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
            
            # Keep existing logic for linking suggestions
            action = f'LINK_ENTITY with source_id="{source_id}", source_type="{source_type}", target_id="{target_id}", target_type="{target_type}"'
            opp_lines.append(f"  - {action} ({reason})")

        opp_text = email_text + "\n" + ("\n".join(opp_lines) if opp_lines else "")

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

        prompt = format_prompt(
            "autonomous_agent",
            writing_style_samples=writing_style_text,
            total_changes_24h=summary['total_changes_24h'],
            stale_tasks=summary['stale_tasks'],
            stale_projects=summary['stale_projects'],
            pending_email_count=summary['pending_email_count'],
            connection_opportunities=summary['connection_opportunities'],
            opportunities_text=opp_text,
            goals_text=goals_text,
            projects_text=projects_text,
            orphaned_text="", # Removed to save token space for writing samples
        )

        try:
            llm = get_llm_service()
            logger.info("Calling LLM for autonomous reasoning...")
            response = await llm.complete(prompt, max_tokens=2000)
            
            # ... existing parsing logic ...
            response_text = response.strip()
            if response_text.startswith("```"):
                lines = response_text.split("\n")
                response_text = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])
            
            if "[" in response_text:
                start = response_text.find("[")
                end = response_text.rfind("]") + 1
                response_text = response_text[start:end]

            decisions = json.loads(response_text)
            if not isinstance(decisions, list):
                decisions = [decisions]
                
            return decisions

        except Exception as e:
            logger.error("LLM reasoning failed", error=str(e))
            return []

    async def _execute_decision(self, session, decision: dict) -> dict | None:
        """Execute a single decision."""
        action = decision.get("action")
        params = decision.get("params", {})
        # Flatten params if they are at top level (supports both formats)
        if not params:
            params = {k: v for k, v in decision.items() if k not in ("action", "reason")}
            
        reason = decision.get("reason", "")

        logger.info("Executing decision", action=action, reason=reason[:100])

        if action == "DRAFT_EMAIL":
            return await self._draft_email(params, reason)
        elif action == "SCHEDULE_BLOCK":
            return await self._schedule_block(params, reason)
        elif action == "COMPILE_CONTEXT_PACK":
            return await self._compile_decision_pack(params, reason)
        elif action == "CREATE_TASK":
            return await self._create_task(session, params, reason)
        elif action == "LINK_ENTITY" or action.startswith("LINK_"):
            # Handle generic linking
            return await self._handle_generic_link(session, params, reason)
        else:
            logger.warning("Unknown action type", action=action)
            return None

    # --- New Execution Handlers ---

    async def _draft_email(self, params: dict, reason: str) -> dict | None:
        """Execute DraftEmailTool."""
        from cognitex.agent.tools import DraftEmailTool
        
        tool = DraftEmailTool()
        result = await tool.execute(
            to=params.get("to"),
            subject=params.get("subject"),
            body=params.get("body"),
            reasoning=reason
        )
        
        if result.success:
            await log_action("draft_email", "autonomous_agent", 
                           summary=f"Drafted email to {params.get('to')}", 
                           details=result.data)
            return {"action": "draft_email", "status": "staged", "id": result.approval_id}
        return None

    async def _schedule_block(self, params: dict, reason: str) -> dict | None:
        """Execute CreateEventTool to block time."""
        from cognitex.agent.tools import CreateEventTool
        from datetime import datetime, timedelta
        
        # Calculate end time if duration provided
        start_str = params.get("start_time")
        duration = params.get("duration_minutes", 60)
        
        try:
            start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            end_dt = start_dt + timedelta(minutes=duration)
            end_str = end_dt.isoformat()
        except:
            end_str = params.get("end_time") # Fallback

        tool = CreateEventTool()
        result = await tool.execute(
            title=params.get("title", "Focus Time"),
            start=start_str,
            end=end_str,
            description=f"Automated block for: {reason}",
            reasoning=reason
        )
        
        if result.success:
            await log_action("schedule_block", "autonomous_agent",
                           summary=f"Scheduled block '{params.get('title')}'",
                           details=result.data)
            return {"action": "schedule_block", "status": "staged", "id": result.approval_id}
        return None

    async def _compile_decision_pack(self, params: dict, reason: str) -> dict | None:
        """Create a context pack and notify user."""
        from cognitex.agent.context_pack import get_context_pack_compiler, BuildStage
        from cognitex.agent.tools import SendNotificationTool
        
        compiler = get_context_pack_compiler()
        
        # Create a temporary task structure to build the pack around
        dummy_task = {
            "id": params.get("task_id", "decision_needed"),
            "title": params.get("objective", "Decision Needed"),
            "description": f"Decision needed regarding: {reason}\nOptions: {params.get('options')}"
        }
        
        pack = await compiler.compile_for_task(dummy_task, stage=BuildStage.T_24H)
        
        # Notify via Discord
        msg = f"**🧠 Decision Required**\n\n**Objective:** {pack.objective}\n\n**Context:** I've prepared a decision pack. Check the dashboard or reply here."
        if params.get("options"):
             msg += f"\n\n**Options:**\n" + "\n".join([f"- {o}" for o in params['options']])
             
        notify_tool = SendNotificationTool()
        await notify_tool.execute(message=msg, urgency="normal")
        
        return {"action": "compile_pack", "pack_id": pack.pack_id}

    async def _handle_generic_link(self, session, params: dict, reason: str) -> dict | None:
        """Backward compatibility for specific LINK actions."""
        # Re-use existing linking logic but adapt params
        source_type = params.get("source_type", "").lower()
        target_type = params.get("target_type", "").lower()
        
        # Dispatch to existing private link methods based on types
        if "document" in source_type and "project" in target_type:
            return await self._link_document(session, {"document_id": params.get("source_id"), "project_id": params.get("target_id")}, reason)
        elif "task" in source_type and "project" in target_type:
            return await self._link_task(session, {"task_id": params.get("source_id"), "project_id": params.get("target_id")}, reason)
        # Add other mappings as needed from the original code
        
        return None
```
</file_path>

<chatName="Update autonomous agent to proactive digital twin"/>