Yes, the learning system you have implemented provides the **foundation** for improvement (capturing data), but currently, the autonomous agent is not fully **closing the loop**—it's not actually *looking* at that learned data when making decisions.

To achieve "sensible autonomous functioning" that evolves without explicit teaching, the agent needs to see its own report card before it acts.

Here are the specific changes to inject your learning system into the agent's reasoning process.

### 1. Update System Prompt to Accept Guidelines
First, we need to make space in the prompt for the agent to see what it has learned.

<file_path="src/cognitex/prompts/autonomous_agent.md">
```md
# Digital Twin Autonomous Agent

You are a Digital Twin - an autonomous agent that acts on behalf of the user when they're not available. Your role is to advance their work, respond to incoming requests in their voice, and maintain their knowledge graph.

## Your Mission:
1. **RESPOND** - Draft email replies in the user's voice for actionable messages
2. **PREPARE** - Compile context packs for upcoming meetings and decisions
3. **ORGANIZE** - Link documents, tasks, and projects to maintain the knowledge graph
4. **SURFACE** - Flag truly ambiguous items that need human judgment

## The User's Writing Style:
Learn from these recent emails sent by the user. Match their tone, formality level, and patterns:
{writing_samples_text}

## LEARNED GUIDELINES & FEEDBACK:
Apply these lessons from previous interactions to your decisions today:
{learned_guidelines}

## Current State Summary:
- Emails awaiting response: {emails_needing_response}
- Meetings needing prep: {meetings_needing_prep}
- Connection opportunities: {connection_opportunities}
- Pending tasks: {pending_task_count}
- Goals needing attention: {goals_needing_attention}
- Projects needing attention: {projects_needing_attention}
```
</file_path>

### 2. Inject Learning Context into the Loop
Now update the autonomous agent to fetch three key pieces of learning data and feed them into that new prompt section:
1.  **General Insights:** High-level observations (e.g., "You tend to underestimate task duration").
2.  **Preference Rules:** Rules extracted from consistent patterns (e.g., "Always draft replies to Client X").
3.  **Recent Rejections:** Specific mistakes to avoid repeating (e.g., "Don't create tasks for 'lunch'").

<file_path="src/cognitex/agent/autonomous.py">
```python
    async def _reason_about_context(self, context: dict) -> list[dict]:
        """Use LLM to reason about the graph context and decide on actions."""
        from cognitex.services.llm import get_llm_service
        # NEW: Imports for learning context
        from cognitex.agent.learning import get_learning_system
        from cognitex.agent.action_log import get_recent_rejections
        from cognitex.agent.decision_memory import get_decision_memory

        # Build learned guidelines context
        learned_lines = []
        
        # 1. High-level insights
        try:
            ls = get_learning_system()
            if ls:
                summary = await ls.get_learning_summary()
                if summary.get("insights"):
                    learned_lines.append("### General Insights")
                    learned_lines.extend([f"- {i}" for i in summary["insights"][:3]])
        except Exception:
            pass

        # 2. Preference Rules (Active)
        try:
            dm = get_decision_memory()
            rules = await dm.rules.get_matching_rules(
                context={"trigger_type": "autonomous_cycle"},
                rule_type="action_preference"
            )
            if rules:
                learned_lines.append("\n### Learned Preferences")
                for rule in rules[:5]:
                    learned_lines.append(f"- {rule['rule_name']} (confidence: {rule['confidence']:.0%})")
        except Exception:
            pass

        # 3. Recent Rejections (Negative constraints)
        # This is CRITICAL for stopping bad behaviors
        try:
            rejections = await get_recent_rejections(limit=5)
            if rejections:
                learned_lines.append("\n### Recently Rejected (DO NOT REPEAT)")
                for r in rejections:
                    reason = r.get('rejection_reason') or 'No reason given'
                    learned_lines.append(f"- Rejected task '{r['title']}': {reason}")
        except Exception:
            pass

        learned_guidelines = "\n".join(learned_lines) if learned_lines else "(No specific guidelines yet)"

        # ... existing summary extraction ...
        summary = context["summary"]
        
        # ... existing formatting code ...

        prompt = format_prompt(
            "autonomous_agent",
            # ... existing params ...
            writing_samples_text=samples_text,
            learned_guidelines=learned_guidelines, # Inject the new section
            pending_emails_text=pending_emails_text,
            # ... existing params ...
        )

        # ... rest of function ...
```
</file_path>

### 3. Ensure Rejections Record Reasons
For the system to learn *why* something was rejected, ensure the rejection action captures the reason. The `reject_proposal` function in `src/cognitex/agent/action_log.py` already supports a `reason` parameter.

**Workflow Suggestion:**
When you reject a proposal via Discord or the Web UI, always try to provide a brief reason (e.g., "Too vague", "Duplicate", "I do this manually").
*   **Discord:** `/reject proposal_id reason:"Too vague"`
*   **Web:** Ensure the reject button prompts for a reason or allows one.

### How this improves functionality over time:
1.  **Feedback Loop:** If the agent proposes "Follow up with John" and you reject it saying "I handle this in Slack", that rejection appears in the prompt next time. The LLM sees "Rejected: Follow up... Reason: I handle this in Slack" and will self-correct.
2.  **Pattern Recognition:** If you accept 5 scheduling suggestions for "Deep Work" in the morning, the `PreferenceRuleMemory` will extract a rule: "Prefer scheduling Deep Work in mornings". This rule then gets injected into the prompt, making the agent *start* with that assumption.
3.  **Style Drift:** By constantly feeding in the last 3 sent emails (`writing_samples`), the agent's drafting style will drift to match yours as your own style changes over time.

This moves the system from "random trial and error" to "context-aware adaptation."

<chatName="Inject learning loop into autonomous agent"/>