I have reviewed the implemented code. It is a sophisticated architecture, but there is one **critical bug** breaking the "Just-in-Time" context delivery, and a significant functional gap regarding the "Interruption Firewall" (items go in, but the agent doesn't pull them out).

Here are the fixes and improvements to transition this from a "passive system" to an "anticipatory assistant."

### 1. Fix Critical Redis Channel Mismatch
Your Context Pack system (which generates meeting briefings) is publishing to a channel that the Discord bot is not listening to.

**The Bug:**
*   `ContextPackTriggerSystem` publishes to: `cognitex:events:notification`
*   `CognitexBot` listens to: `cognitex:notifications`

**The Fix:** Align the channels so you actually receive the meeting briefings.

<file_path="src/cognitex/agent/context_pack.py">
```python
    async def _notify_pack_ready(
        self,
        pack: ContextPackContent,
        event: dict,
    ) -> None:
        """Notify user when a context pack is ready (via Discord)."""
        try:
            from cognitex.db.redis import get_redis
            import json

            summary = event.get("summary", "Event")

            # Create a structured message for the Notification Tool format
            # The Discord bot expects "message" and "urgency"
            points = "\n".join([f"- {p}" for p in pack.missing_prerequisites[:3]])
            
            notification_payload = {
                "message": (
                    f"**🧠 Context Pack Ready: {summary}**\n"
                    f"Readiness: {pack.readiness_score:.0%}\n"
                    f"Stage: {pack.build_stage.value}\n\n"
                    f"**Missing / Needs Attention:**\n{points or 'None'}\n\n"
                    f"_Check dashboard for full briefing_"
                ),
                "urgency": "normal",
                "type": "context_pack",
                "pack_id": pack.pack_id
            }

            redis = get_redis()
            # CHANGED: Publish to the main notification channel the bot listens to
            await redis.publish("cognitex:notifications", json.dumps(notification_payload))

        except Exception as e:
            logger.warning("Failed to notify pack ready", error=str(e))
```
</file_path>

### 2. Activate the "Inbox Processor"
You built an `InterruptionFirewall` that captures items (e.g., "Parked: Slack message from Boss"), but currently, those items just sit in the queue. The Autonomous Agent needs to process this queue.

**Functionality Add:** Add `inbox_queue` context to the Autonomous Agent and allow it to process captured items.

**Step A: Expose Queue to GraphObserver**
<file_path="src/cognitex/agent/graph_observer.py">
```python
    async def get_full_context(self) -> dict:
        """Gather comprehensive context about the graph state."""
        # ... existing ...
        
        # NEW: Get firewall inbox items
        from cognitex.agent.interruption_firewall import get_interruption_firewall
        firewall = get_interruption_firewall()
        inbox_items = await firewall.get_queued_items(limit=5)
        
        context = {
            # ... existing fields ...
            "inbox_items": [
                {
                    "id": item.item_id,
                    "source": item.source,
                    "subject": item.subject,
                    "preview": item.preview,
                    "suggested": item.suggested_action
                }
                for item in inbox_items
            ],
            # ... existing fields ...
        }
        
        # Add to summary
        context["summary"]["inbox_count"] = len(inbox_items)
        
        return context
```
</file_path>

**Step B: Update Prompt to handle Inbox**
<file_path="src/cognitex/prompts/autonomous_agent.md">
```md
# ... existing prompt ...

## Current State Summary:
- Inbox items needing triage: {inbox_count}
# ... existing summary items ...

## Priority 0: Firewall Inbox (Triage These First)
{inbox_text}

# ... existing priorities ...

## Available Actions:

### PROCESS_INBOX_ITEM
Clear an item from the firewall queue by taking action on it.
```json
{{"action": "PROCESS_INBOX_ITEM", "item_id": "...", "resolution": "Created task / Drafted reply / dismissed", "reason": "..."}}
```

# ... existing actions ...
```
</file_path>

**Step C: Update Autonomous Agent Logic**
<file_path="src/cognitex/agent/autonomous.py">
```python
    async def _reason_about_context(self, context: dict) -> list[dict]:
        # ... existing setup ...
        
        # Format Inbox Items
        inbox = context.get("inbox_items", [])
        if inbox:
            inbox_lines = []
            for item in inbox:
                inbox_lines.append(
                    f"  - [{item['source']}] {item['subject']}\n"
                    f"    Preview: {item['preview'][:100]}\n"
                    f"    Suggestion: {item['suggested']}\n"
                    f"    ID: {item['id']}"
                )
            inbox_text = "\n".join(inbox_lines)
        else:
            inbox_text = "  (Inbox empty)"

        # ... existing formatting ...

        prompt = format_prompt(
            "autonomous_agent",
            inbox_count=context["summary"].get("inbox_count", 0),
            inbox_text=inbox_text,
            # ... existing params ...
        )
        
        # ... rest of function ...

    async def _execute_decision(self, session, decision: dict, flagged_this_cycle: set[str] | None = None) -> dict | None:
        # ... existing dispatch ...
        if action == "PROCESS_INBOX_ITEM":
            return await self._process_inbox_item(session, params, reason)
        # ... existing dispatch ...

    async def _process_inbox_item(self, session, params: dict, reason: str) -> dict | None:
        """Clear an item from the firewall queue."""
        from cognitex.agent.interruption_firewall import get_interruption_firewall
        
        item_id = params.get("item_id")
        if not item_id:
            return None
            
        firewall = get_interruption_firewall()
        await firewall.clear_processed_items([item_id])
        
        await log_action(
            "process_inbox",
            "agent",
            summary=f"Processed inbox item: {params.get('resolution')}",
            details={"item_id": item_id, "reason": reason}
        )
        return {"processed": True, "item_id": item_id}
```
</file_path>

### 3. Ideas for "Anticipatory" Functionality

To truly become an hour-to-hour assistant, the system needs to **push** context before you ask for it.

**A. Meeting "Whisper" Mode**
Instead of just compiling the context pack at `T-24h` or `T-15m`, the system should DM you the "Cheat Sheet" exactly 5 minutes before the meeting starts.
*   **Implementation:** In `ContextPackTriggerSystem._check_upcoming_events`, add a check for `T-5m`. If found, send a specific high-priority notification with just the 3 bullet points you need to know.

**B. "Stuck" Detection**
If `TaskTiming` shows you have been on a task for > 150% of the estimated time (tracked via `started_at` in Postgres), the agent should ping you:
*   *"You've been on 'Write Report' for 90 mins (Est: 60). Do you want to break this down, or are you in flow?"*
*   **Action:** Add a scheduled job in `triggers.py` running every 30m to query `tasks` where `status='in_progress'` and `NOW() - started_at > estimated_minutes * 1.5`.

**C. The "Transition" Nudge**
When `state_model` detects a mode switch (e.g., calendar event ends), the agent should proactively suggest the next best action based on your current energy.
*   *Event "Board Meeting" ends (High Energy Cost).*
*   *Agent:* "Heavy meeting. I've queued some low-energy admin tasks for the next 30 mins to help you recover. [List Tasks]"

These changes fix the plumbing issues and close the loop on input processing, making the agent actively helpful rather than just a passive observer.