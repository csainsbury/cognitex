"""Agent Core - ReAct-style agent with iterative reasoning and tool use."""

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import structlog
from together import Together

from cognitex.config import get_settings
from cognitex.agent.memory import Memory, init_memory, get_memory
from cognitex.agent.tools import ToolRisk, ToolResult, get_tool_registry

logger = structlog.get_logger()

# Maximum iterations to prevent infinite loops
MAX_REACT_ITERATIONS = 8


@dataclass
class ThoughtAction:
    """A single thought-action pair in the ReAct loop."""
    thought: str
    action: str | None = None  # Tool name, or None if ready to respond
    action_input: dict = field(default_factory=dict)
    observation: str | None = None


@dataclass
class ReactTrace:
    """Complete trace of a ReAct execution."""
    steps: list[ThoughtAction] = field(default_factory=list)
    final_response: str = ""
    pending_approvals: list[str] = field(default_factory=list)


class Agent:
    """
    ReAct-style agent for Cognitex.

    Uses an iterative Thought → Action → Observation loop to:
    - Freely explore the knowledge graph
    - Make connections across emails, tasks, people, events, documents
    - Take actions when needed
    - Respond naturally to any query
    """

    def __init__(self):
        self.memory: Memory | None = None
        self.tool_registry = get_tool_registry()
        self._initialized = False
        self._client = None
        self._model = None

    async def initialize(self) -> None:
        """Initialize the agent and all subsystems."""
        if self._initialized:
            return

        logger.info("Initializing agent")

        settings = get_settings()
        api_key = settings.together_api_key.get_secret_value()
        if not api_key:
            raise ValueError("TOGETHER_API_KEY not configured")

        self._client = Together(api_key=api_key)
        self._model = settings.together_model_planner

        # Initialize memory
        self.memory = await init_memory()

        self._initialized = True
        logger.info("Agent initialized")

    def _ensure_initialized(self) -> None:
        """Ensure agent is initialized."""
        if not self._initialized:
            raise RuntimeError("Agent not initialized. Call initialize() first.")

    def _build_system_prompt(self) -> str:
        """Build the system prompt with available tools."""
        tool_descriptions = []
        for tool in self.tool_registry.all():
            risk_label = {
                ToolRisk.READONLY: "(read-only)",
                ToolRisk.AUTO: "(auto-execute)",
                ToolRisk.APPROVAL: "(requires approval)",
            }[tool.risk]

            params = ", ".join(
                f"{k}: {v.get('type', 'any')}" + (" [optional]" if v.get("optional") else "")
                for k, v in tool.parameters.items()
            )

            tool_descriptions.append(
                f"- {tool.name} {risk_label}: {tool.description}\n  Parameters: {params}"
            )

        tools_text = "\n".join(tool_descriptions)

        return f"""You are Cognitex, a personal assistant with access to a knowledge graph containing emails, tasks, calendar events, contacts, and documents.

Your job is to help the user by reasoning through their request, gathering information, and taking actions when needed.

## Available Tools
{tools_text}

## How to Respond

You use a Thought → Action → Observation loop. For each step:

1. **Thought**: Reason about what you know and what you need to find out
2. **Action**: Call a tool to get information or take an action (or respond if ready)
3. **Observation**: See the result and continue reasoning

Output your response as JSON:
```json
{{
  "thought": "Your reasoning about the current state and what to do next",
  "action": "tool_name or null if ready to give final response",
  "action_input": {{}},
  "response": "Your final response to the user (only if action is null)"
}}
```

## Guidelines

- **Explore freely**: Query the graph to understand context before acting
- **Make connections**: Link information across emails, tasks, people, events
- **Be thorough**: If the user asks about something, find the actual data
- **Chain queries**: Use results from one query to inform the next
- **Take action when asked**: Update tasks, draft emails, create events as requested
- **Be honest**: If you can't find something, say so
- **Respond promptly**: Once you have enough information, set action to null and provide your response. Don't keep querying unnecessarily - 2-4 tool calls is usually enough.

## Graph Query Tips (for graph_query tool)

The Neo4j graph has these node types:
- Person (email, name, org, role, communication_style)
- Email (gmail_id, subject, snippet, date, classification, urgency)
- Task (id, title, status, energy_cost, due, source_type)
- Event (gcal_id, title, start, end, event_type, energy_impact)
- Document (drive_id, name, mime_type, folder_path)

Common relationships:
- (Email)-[:SENT_BY]->(Person)
- (Email)-[:RECEIVED_BY]->(Person)
- (Task)-[:DERIVED_FROM]->(Email)
- (Task)-[:REQUESTED_BY]->(Person)
- (Event)-[:ATTENDED_BY]->(Person)
- (Document)-[:OWNED_BY]->(Person)

Example queries:
- Tasks from a person: MATCH (t:Task)-[:REQUESTED_BY]->(p:Person {{email: $email}}) RETURN t
- Recent emails: MATCH (e:Email) WHERE e.date > datetime() - duration('P7D') RETURN e ORDER BY e.date DESC LIMIT 10
- Person's communication history: MATCH (p:Person {{email: $email}})<-[:SENT_BY]-(e:Email) RETURN e ORDER BY e.date DESC LIMIT 5"""

    async def chat(self, message: str) -> str:
        """
        Handle a conversational message using ReAct loop.

        Args:
            message: User's message

        Returns:
            Agent's response
        """
        self._ensure_initialized()

        logger.info("ReAct chat starting", message=message[:100])

        # Record user message
        await self.memory.working.add_interaction(role="user", content=message)

        # Run ReAct loop
        trace = await self._react_loop(message)

        # Record response
        await self.memory.working.add_interaction(role="agent", content=trace.final_response)

        # Add approval notice if any
        response = trace.final_response
        if trace.pending_approvals:
            response += f"\n\n_(Staged {len(trace.pending_approvals)} action(s) for your approval)_"

        return response

    async def _react_loop(self, message: str) -> ReactTrace:
        """Execute the ReAct reasoning loop."""
        trace = ReactTrace()

        # Build conversation for the LLM
        system_prompt = self._build_system_prompt()

        conversation = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"User message: {message}"},
        ]

        for iteration in range(MAX_REACT_ITERATIONS):
            logger.debug("ReAct iteration", iteration=iteration)

            # Get next thought/action from LLM
            try:
                response = self._client.chat.completions.create(
                    model=self._model,
                    messages=conversation,
                    max_tokens=2048,
                    temperature=0.3,
                )

                content = response.choices[0].message.content.strip()

                # Parse JSON response
                parsed = self._parse_react_response(content)

                step = ThoughtAction(
                    thought=parsed.get("thought", ""),
                    action=parsed.get("action"),
                    action_input=parsed.get("action_input", {}),
                )

                logger.debug(
                    "ReAct step",
                    thought=step.thought[:100],
                    action=step.action,
                )

                # If no action, we have the final response
                if step.action is None:
                    trace.final_response = parsed.get("response", step.thought)
                    trace.steps.append(step)
                    break

                # Execute the action
                observation, approval_id = await self._execute_action(
                    step.action,
                    step.action_input
                )
                step.observation = observation

                if approval_id:
                    trace.pending_approvals.append(approval_id)

                trace.steps.append(step)

                # Add to conversation for next iteration
                conversation.append({
                    "role": "assistant",
                    "content": content,
                })
                conversation.append({
                    "role": "user",
                    "content": f"Observation: {observation}",
                })

            except Exception as e:
                logger.error("ReAct iteration failed", error=str(e), iteration=iteration)
                trace.final_response = f"I encountered an error while processing your request: {str(e)[:100]}"
                break

        else:
            # Hit max iterations - ask LLM to summarize what we found
            logger.warning("ReAct hit max iterations", iterations=MAX_REACT_ITERATIONS)
            trace.final_response = await self._generate_summary(message, trace)

        logger.info(
            "ReAct complete",
            iterations=len(trace.steps),
            approvals=len(trace.pending_approvals),
        )

        return trace

    def _parse_react_response(self, content: str) -> dict:
        """Parse the LLM's JSON response."""
        # Handle markdown code blocks
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0]
        elif "```" in content:
            content = content.split("```")[1].split("```")[0]

        try:
            return json.loads(content.strip())
        except json.JSONDecodeError:
            # Try to extract key fields manually
            logger.warning("Failed to parse ReAct JSON, attempting manual extraction")
            result = {"thought": content}

            # Look for response patterns
            if "final response" in content.lower() or "my response" in content.lower():
                result["action"] = None
                result["response"] = content

            return result

    async def _execute_action(self, action: str, action_input: dict) -> tuple[str, str | None]:
        """
        Execute a tool action and return the observation.

        Returns:
            Tuple of (observation string, approval_id if approval needed)
        """
        tool = self.tool_registry.get(action)
        if not tool:
            return f"Error: Unknown tool '{action}'. Available tools: {[t.name for t in self.tool_registry.all()]}", None

        try:
            result = await tool.execute(**action_input)

            if result.success:
                # Format the observation
                if result.needs_approval:
                    return f"Action staged for approval (ID: {result.approval_id}). Details: {result.data}", result.approval_id

                # Format data nicely for observation
                if result.data is None:
                    return "Success (no data returned)", None
                elif isinstance(result.data, list):
                    if len(result.data) == 0:
                        return "No results found", None
                    # Format list results
                    items = []
                    for item in result.data[:15]:  # Limit to 15 items
                        if isinstance(item, dict):
                            # Pick key fields
                            item_str = ", ".join(f"{k}: {v}" for k, v in list(item.items())[:6] if v is not None)
                            items.append(f"  - {item_str}")
                        else:
                            items.append(f"  - {item}")

                    obs = f"Found {len(result.data)} results:\n" + "\n".join(items)
                    if len(result.data) > 15:
                        obs += f"\n  ... and {len(result.data) - 15} more"
                    return obs, None
                elif isinstance(result.data, dict):
                    return f"Result: {json.dumps(result.data, indent=2, default=str)}", None
                else:
                    return f"Result: {result.data}", None
            else:
                return f"Error: {result.error}", None

        except Exception as e:
            logger.error("Tool execution failed", tool=action, error=str(e))
            return f"Error executing {action}: {str(e)}", None

    async def _generate_summary(self, original_message: str, trace: ReactTrace) -> str:
        """Generate a natural summary when hitting max iterations."""
        # Collect all observations
        observations = []
        for step in trace.steps:
            if step.observation:
                observations.append(f"- {step.action}: {step.observation[:500]}")

        observations_text = "\n".join(observations) if observations else "No data retrieved."

        prompt = f"""Based on the user's question and the data I gathered, provide a helpful, natural response.

User's question: {original_message}

Data gathered:
{observations_text}

Instructions:
- Summarize the key information naturally, as if talking to the user
- Don't dump raw data - interpret it helpfully
- If there are calendar events, format times nicely
- If there are tasks, list them clearly
- Be concise but complete

Your response:"""

        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1024,
                temperature=0.4,
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            logger.error("Summary generation failed", error=str(e))
            return "I found some information but had trouble summarizing it. Please try asking again."

    # =========================================================================
    # APPROVAL HANDLING
    # =========================================================================

    async def handle_approval(self, approval_id: str, approved: bool, feedback: str | None = None) -> dict:
        """Handle user approval or rejection of a staged action."""
        self._ensure_initialized()

        logger.info("Handling approval", approval_id=approval_id, approved=approved)

        approval = await self.memory.working.resolve_approval(approval_id, approved, feedback)

        if not approval:
            return {"success": False, "error": "Approval not found or expired"}

        result = {"success": True, "approval_id": approval_id, "action": approval["action_type"]}

        if approved:
            action_type = approval["action_type"]
            params = approval["params"]

            if action_type == "send_email":
                from cognitex.services.gmail import GmailSender
                gmail = GmailSender()

                try:
                    if params.get("reply_to_id"):
                        sent = gmail.send_reply(
                            thread_id=params["reply_to_id"],
                            to=params["to"],
                            subject=params["subject"],
                            body=params["body"],
                        )
                    else:
                        sent = gmail.send_message(
                            to=params["to"],
                            subject=params["subject"],
                            body=params["body"],
                        )
                    result["sent"] = True
                    result["message_id"] = sent.get("id")

                    await self.memory.episodic.store(
                        content=f"Sent email to {params['to']}: {params['subject']}",
                        memory_type="interaction",
                        importance=4,
                        entities=[params["to"]],
                    )

                except Exception as e:
                    result["success"] = False
                    result["error"] = str(e)

            elif action_type == "create_event":
                from cognitex.services.calendar import CalendarService
                calendar = CalendarService()

                try:
                    event = calendar.create_event(
                        title=params["title"],
                        start=params["start"],
                        end=params["end"],
                        attendees=params.get("attendees"),
                        description=params.get("description"),
                    )
                    result["created"] = True
                    result["event_id"] = event.get("id")

                    await self.memory.episodic.store(
                        content=f"Created event: {params['title']} at {params['start']}",
                        memory_type="interaction",
                        importance=3,
                    )

                except Exception as e:
                    result["success"] = False
                    result["error"] = str(e)

        else:
            if feedback:
                await self.memory.episodic.store(
                    content=f"User rejected {approval['action_type']}: {feedback}",
                    memory_type="feedback",
                    importance=4,
                    metadata={"approval_id": approval_id, "action_type": approval["action_type"]},
                )

        return result

    async def get_pending_approvals(self) -> list[dict]:
        """Get all pending approval requests."""
        self._ensure_initialized()
        return await self.memory.working.get_pending_approvals()

    # =========================================================================
    # SCHEDULED MODES (briefing, review, etc.)
    # =========================================================================

    async def morning_briefing(self) -> str:
        """Generate a morning briefing using ReAct."""
        return await self.chat(
            "Give me a morning briefing: what's on my calendar today, what are my top priority tasks, "
            "any urgent emails I should know about, and any important deadlines coming up this week."
        )

    async def evening_review(self) -> str:
        """Generate an evening review using ReAct."""
        return await self.chat(
            "Give me an end-of-day review: what did I have scheduled today, "
            "what tasks might need attention tomorrow, and any emails that came in today that need responses."
        )


# Singleton
_agent: Agent | None = None


async def get_agent() -> Agent:
    """Get or create the agent singleton."""
    global _agent
    if _agent is None:
        _agent = Agent()
        await _agent.initialize()
    return _agent


# Keep AgentMode for backward compatibility with existing code
from enum import Enum

class AgentMode(Enum):
    """Operating modes for the agent (legacy, kept for compatibility)."""
    BRIEFING = "briefing"
    REVIEW = "review"
    MONITOR = "monitor"
    PROCESS_EMAIL = "process_email"
    PROCESS_EVENT = "process_event"
    CONVERSATION = "conversation"
    ESCALATE = "escalate"


__all__ = ["Agent", "AgentMode", "get_agent"]
