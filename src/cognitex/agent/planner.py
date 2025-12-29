"""Agent Planner - Multi-provider LLM powered reasoning and planning."""

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any

import structlog

from cognitex.config import get_settings
from cognitex.agent.tools import ToolDefinition, ToolRisk, get_tool_registry

logger = structlog.get_logger()


class AgentMode(Enum):
    """Operating modes for the agent."""
    BRIEFING = "briefing"           # Morning/evening summaries
    REVIEW = "review"               # End of day review
    MONITOR = "monitor"             # Hourly check for urgent items
    PROCESS_EMAIL = "process_email" # Handle new email
    PROCESS_EVENT = "process_event" # Handle calendar change
    CONVERSATION = "conversation"   # Interactive user chat
    ESCALATE = "escalate"           # Handle overdue/urgent items


@dataclass
class PlanStep:
    """A single step in the agent's plan."""
    executor: str  # Which executor handles this (email, task, calendar, notify)
    tool: str      # Tool name to use
    args: dict     # Arguments for the tool
    risk: ToolRisk # Risk level
    reasoning: str # Why this step is needed


@dataclass
class Plan:
    """Complete plan from the planner."""
    reasoning: str           # Overall reasoning
    steps: list[PlanStep]    # Ordered steps to execute
    user_notification: str | None  # Optional message for user
    follow_up: str | None    # Optional scheduled follow-up
    confidence: float        # Confidence in plan (0-1)


# Mode-specific tool access
MODE_TOOL_ACCESS = {
    AgentMode.BRIEFING: [ToolRisk.READONLY, ToolRisk.AUTO],
    AgentMode.REVIEW: [ToolRisk.READONLY, ToolRisk.AUTO],
    AgentMode.MONITOR: [ToolRisk.READONLY, ToolRisk.AUTO],
    AgentMode.PROCESS_EMAIL: [ToolRisk.READONLY, ToolRisk.AUTO, ToolRisk.APPROVAL],
    AgentMode.PROCESS_EVENT: [ToolRisk.READONLY, ToolRisk.AUTO],
    AgentMode.CONVERSATION: [ToolRisk.READONLY, ToolRisk.AUTO, ToolRisk.APPROVAL],
    AgentMode.ESCALATE: [ToolRisk.READONLY, ToolRisk.AUTO, ToolRisk.APPROVAL],
}


def build_tool_descriptions(tools: list[ToolDefinition]) -> str:
    """Build a formatted string describing available tools."""
    lines = []
    for tool in tools:
        risk_label = {
            ToolRisk.READONLY: "[read-only]",
            ToolRisk.AUTO: "[auto-execute]",
            ToolRisk.APPROVAL: "[requires approval]",
        }[tool.risk]

        params_str = ", ".join(
            f"{k}: {v.get('type', 'any')}" + (" (optional)" if v.get("optional") else "")
            for k, v in tool.parameters.items()
        )

        lines.append(f"- **{tool.name}** {risk_label}")
        lines.append(f"  {tool.description}")
        lines.append(f"  Parameters: {params_str}")
        lines.append("")

    return "\n".join(lines)


SYSTEM_PROMPT_TEMPLATE = """You are Cognitex, a personal agent for managing cognitive overhead. Your role is to help the user stay on top of communications, tasks, and commitments while respecting their energy levels and preferences.

## User Profile
{user_profile}

## Current Mode: {mode}
{mode_description}

## Available Tools
{tools}

## Risk Levels
- **read-only**: Always allowed, no side effects
- **auto-execute**: Will execute automatically (creating tasks, sending notifications)
- **requires approval**: Will be staged for user approval before execution (sending emails, calendar changes)

## Current Context
{context}

## Your Task
Given the trigger information below, analyze the situation and create a plan.

Think through:
1. What is happening and why does it matter?
2. What relationships or history are relevant?
3. What actions would genuinely help the user?
4. What is the actual priority/urgency (be honest, not everything is urgent)?

Output your response as JSON:
```json
{{
  "reasoning": "Your analysis of the situation and why you're recommending these actions",
  "steps": [
    {{
      "executor": "task|email|calendar|notify",
      "tool": "tool_name",
      "args": {{}},
      "reasoning": "Why this specific step"
    }}
  ],
  "user_notification": "Optional message to send to user (null if not needed)",
  "follow_up": "Optional: describe if a follow-up check is needed (null if not)",
  "confidence": 0.85
}}
```

Important guidelines:
- Be conservative. Don't create tasks for everything - only genuinely actionable items.
- Don't overwhelm the user with notifications. Only notify if something truly needs attention.
- Respect energy levels. If the user is low energy, defer non-urgent items.
- Be honest about uncertainty. If you're not sure, say so in your reasoning.
- Use the user's communication style when drafting messages.

Tool chaining:
- To update a task by title, FIRST use find_task to search for it, THEN use update_task with the returned task_id
- To mark a task as complete/done, use update_task with status="done"
- When the user refers to "task 8" or similar, use find_task with keywords from that task's title
- Always execute tools in dependency order - don't skip the find step when you need an ID

Trigger:
{trigger}

Additional data:
{trigger_data}"""


MODE_DESCRIPTIONS = {
    AgentMode.BRIEFING: "Generate a morning or evening briefing. Summarize what's important, highlight priorities, forecast energy needs.",
    AgentMode.REVIEW: "End of day review. What got done, what's rolling over, any concerns for tomorrow.",
    AgentMode.MONITOR: "Hourly monitoring check. Only surface truly urgent items - most checks should result in no action.",
    AgentMode.PROCESS_EMAIL: "Process a new email. Classify it, decide if it needs action, create tasks if appropriate, draft replies if needed.",
    AgentMode.PROCESS_EVENT: "Process a calendar change. Update energy forecasts, check for conflicts, note preparation needs.",
    AgentMode.CONVERSATION: "Interactive conversation with user. Respond helpfully to their query or request.",
    AgentMode.ESCALATE: "Handle an escalation (overdue task, repeated asks, etc). Decide how to address it.",
}


class Planner:
    """
    Agent Planner using Qwen3-30B-A3B.

    Responsible for:
    - Analyzing triggers and context
    - Reasoning about what actions to take
    - Creating structured plans for executors
    """

    def __init__(self):
        settings = get_settings()
        self.provider = settings.llm_provider
        self.registry = get_tool_registry()

        # Initialize the appropriate client based on provider
        if self.provider == "google":
            import google.generativeai as genai
            api_key = settings.google_ai_api_key.get_secret_value()
            if not api_key:
                raise ValueError("GOOGLE_AI_API_KEY not configured")
            genai.configure(api_key=api_key)
            self.client = genai
            self.model = settings.google_model_planner
            logger.info("Planner using Google Gemini", model=self.model)

        elif self.provider == "anthropic":
            from anthropic import Anthropic
            api_key = settings.anthropic_api_key.get_secret_value()
            if not api_key:
                raise ValueError("ANTHROPIC_API_KEY not configured")
            self.client = Anthropic(api_key=api_key)
            self.model = settings.anthropic_model_planner
            logger.info("Planner using Anthropic Claude", model=self.model)

        elif self.provider == "openai":
            from openai import OpenAI
            api_key = settings.openai_api_key.get_secret_value()
            if not api_key:
                raise ValueError("OPENAI_API_KEY not configured")
            self.client = OpenAI(api_key=api_key)
            self.model = settings.openai_model_planner
            logger.info("Planner using OpenAI", model=self.model)

        else:  # together (default)
            from together import Together
            api_key = settings.together_api_key.get_secret_value()
            if not api_key:
                raise ValueError("TOGETHER_API_KEY not configured")
            self.client = Together(api_key=api_key)
            self.model = settings.together_model_planner
            self.provider = "together"
            logger.info("Planner using Together.ai", model=self.model)

        # User profile (could be loaded from config/db)
        self.user_profile = self._load_user_profile()

    def _load_user_profile(self) -> str:
        """Load user profile for personalization."""
        # TODO: Load from database/config
        return """- Communication style: Direct, concise, prefers async
- Energy patterns: Peak in mornings (9-11am), dip after lunch (1-3pm)
- Work style: Prefers deep work blocks, easily overwhelmed by task switching
- Key priorities: Research projects, grant deadlines, student supervision
- Notification preference: Batch updates, not constant interruptions"""

    def _get_available_tools(self, mode: AgentMode) -> list[ToolDefinition]:
        """Get tools available for a given mode."""
        allowed_risks = MODE_TOOL_ACCESS.get(mode, [ToolRisk.READONLY])
        return [
            t.to_definition()
            for t in self.registry.all()
            if t.risk in allowed_risks
        ]

    def _build_prompt(
        self,
        mode: AgentMode,
        context: dict,
        trigger: str,
        trigger_data: dict,
    ) -> str:
        """Build the complete prompt for the planner."""
        tools = self._get_available_tools(mode)

        return SYSTEM_PROMPT_TEMPLATE.format(
            user_profile=self.user_profile,
            mode=mode.value,
            mode_description=MODE_DESCRIPTIONS[mode],
            tools=build_tool_descriptions(tools),
            context=json.dumps(context, indent=2, default=str),
            trigger=trigger,
            trigger_data=json.dumps(trigger_data, indent=2, default=str),
        )

    async def plan(
        self,
        mode: AgentMode,
        context: dict,
        trigger: str,
        trigger_data: dict | None = None,
    ) -> Plan:
        """
        Generate a plan for the given trigger and context.

        Args:
            mode: Agent operating mode
            context: Current context from memory system
            trigger: Description of what triggered this planning
            trigger_data: Additional data about the trigger

        Returns:
            Plan with steps to execute
        """
        prompt = self._build_prompt(mode, context, trigger, trigger_data or {})

        logger.debug("Planning", mode=mode.value, trigger=trigger[:100])

        try:
            # Route to appropriate provider API
            if self.provider == "google":
                model = self.client.GenerativeModel(self.model)
                response = model.generate_content(
                    prompt,
                    generation_config={
                        "max_output_tokens": 2048,
                        "temperature": 0.3,
                    },
                )
                content = response.text

            elif self.provider == "anthropic":
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=2048,
                    messages=[{"role": "user", "content": prompt}],
                )
                content = response.content[0].text

            else:  # openai/together
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=2048,
                    temperature=0.3,
                )
                content = response.choices[0].message.content

            # Parse JSON from response
            content = content.strip()
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0]
            elif "```" in content:
                content = content.split("```")[1].split("```")[0]

            plan_data = json.loads(content)

            # Build plan steps
            steps = []
            for step_data in plan_data.get("steps", []):
                tool = self.registry.get(step_data["tool"])
                risk = tool.risk if tool else ToolRisk.READONLY

                steps.append(PlanStep(
                    executor=step_data.get("executor", "general"),
                    tool=step_data["tool"],
                    args=step_data.get("args", {}),
                    risk=risk,
                    reasoning=step_data.get("reasoning", ""),
                ))

            plan = Plan(
                reasoning=plan_data.get("reasoning", ""),
                steps=steps,
                user_notification=plan_data.get("user_notification"),
                follow_up=plan_data.get("follow_up"),
                confidence=float(plan_data.get("confidence", 0.5)),
            )

            logger.info(
                "Plan generated",
                mode=mode.value,
                steps=len(steps),
                confidence=plan.confidence,
            )

            return plan

        except json.JSONDecodeError as e:
            logger.error("Failed to parse planner response", error=str(e))
            return Plan(
                reasoning=f"Failed to generate plan: {e}",
                steps=[],
                user_notification=None,
                follow_up=None,
                confidence=0.0,
            )
        except Exception as e:
            logger.error("Planner error", error=str(e))
            return Plan(
                reasoning=f"Error during planning: {e}",
                steps=[],
                user_notification=None,
                follow_up=None,
                confidence=0.0,
            )

    async def respond_to_user(
        self,
        context: dict,
        user_message: str,
    ) -> tuple[str, Plan | None]:
        """
        Generate a conversational response to the user.

        Returns both a natural language response and optionally a plan if actions are needed.
        """
        # First, plan any actions needed
        plan = await self.plan(
            mode=AgentMode.CONVERSATION,
            context=context,
            trigger=f"User message: {user_message}",
            trigger_data={"message": user_message},
        )

        # Generate natural language response
        response_prompt = f"""Based on your analysis:
{plan.reasoning}

Generate a natural, helpful response to the user. Be concise but warm.
If you're taking actions, briefly mention what you're doing.
If you need approval for something, explain what and why.

User's message: {user_message}

Your response (just the text, no JSON):"""

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": response_prompt}],
                max_tokens=512,
                temperature=0.5,
            )

            reply = response.choices[0].message.content.strip()
            return reply, plan if plan.steps else None

        except Exception as e:
            logger.error("Response generation failed", error=str(e))
            return "I encountered an error processing your request. Could you try again?", None

    async def generate_response(
        self,
        message: str,
        plan: Plan | None,
        tool_results: list[dict],
    ) -> str:
        """
        Generate a response to the user based on the plan and actual tool results.

        This is called AFTER tools have been executed, so it can include real data.

        Args:
            message: Original user message
            plan: The plan that was executed (or None)
            tool_results: Results from executing tools

        Returns:
            Natural language response for the user
        """
        # Format tool results for the prompt
        results_text = ""
        if tool_results:
            results_parts = []
            for r in tool_results:
                if r["success"] and r["data"]:
                    # Format the data nicely
                    data = r["data"]
                    if isinstance(data, list):
                        results_parts.append(f"**{r['tool']}** returned {len(data)} items:")
                        for item in data[:10]:  # Limit to 10 items
                            if isinstance(item, dict):
                                # Format dict nicely
                                item_str = ", ".join(f"{k}: {v}" for k, v in list(item.items())[:5])
                                results_parts.append(f"  - {item_str}")
                            else:
                                results_parts.append(f"  - {item}")
                        if len(data) > 10:
                            results_parts.append(f"  ... and {len(data) - 10} more")
                    else:
                        results_parts.append(f"**{r['tool']}**: {data}")
                elif r["error"]:
                    results_parts.append(f"**{r['tool']}** failed: {r['error']}")
            results_text = "\n".join(results_parts)

        reasoning = plan.reasoning if plan else "No specific plan needed."

        prompt = f"""You are Cognitex, a helpful personal assistant. Respond to the user's message based on the actual data retrieved.

User's message: {message}

Your analysis: {reasoning}

Tool results (REAL DATA - use this, don't make things up):
{results_text if results_text else "No tools were called."}

Instructions:
- Respond naturally and helpfully
- Use the ACTUAL data from tool results - do not invent or hallucinate information
- If no data was found, say so honestly
- Be concise but complete
- Format lists nicely if presenting multiple items

Your response:"""

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1024,
                temperature=0.4,
            )

            return response.choices[0].message.content.strip()

        except Exception as e:
            logger.error("Response generation failed", error=str(e))
            return "I encountered an error generating a response. Please try again."


# Singleton
_planner: Planner | None = None


def get_planner() -> Planner:
    """Get or create the planner singleton."""
    global _planner
    if _planner is None:
        _planner = Planner()
    return _planner
