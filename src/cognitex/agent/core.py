"""Agent Core - ReAct-style agent with iterative reasoning and tool use."""

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime

import structlog
from together import Together

from cognitex.agent.decision_memory import DecisionMemory, init_decision_memory
from cognitex.agent.memory import Memory, init_memory
from cognitex.agent.tool_filter import get_tool_filter
from cognitex.agent.tools import ToolRisk, get_tool_registry
from cognitex.config import get_settings

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
    decision_trace_ids: list[str] = field(default_factory=list)  # IDs of decision traces created


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
        self.decision_memory: DecisionMemory | None = None
        self.tool_registry = get_tool_registry()
        self.tool_filter = get_tool_filter()
        self._initialized = False
        self._client = None
        self._model = None
        self._provider = None
        # Cache for filtered tools (set during _build_system_prompt)
        self._current_mode = None
        self._filtered_tools: list[str] = []

    async def initialize(self) -> None:
        """Initialize the agent and all subsystems."""
        if self._initialized:
            return

        logger.info("Initializing agent")

        settings = get_settings()

        # Load model config from Redis (runtime overrides), falling back to env settings
        try:
            from cognitex.services.model_config import get_model_config_service
            model_config = await get_model_config_service().get_config()
            self._provider = model_config.provider
            self._model = model_config.planner_model
            logger.info("Loaded model config from Redis", provider=self._provider, model=self._model)
        except Exception as e:
            logger.warning("Failed to load model config from Redis, using env", error=str(e))
            self._provider = settings.llm_provider
            self._model = None  # Will be set below per provider

        # Initialize the appropriate LLM client based on provider
        if self._provider == "google":
            import google.generativeai as genai

            api_key = settings.google_ai_api_key.get_secret_value()
            if not api_key:
                raise ValueError("GOOGLE_AI_API_KEY not configured")
            genai.configure(api_key=api_key)
            self._client = genai
            self._model = self._model or settings.google_model_planner
            logger.info("Using Google Gemini", model=self._model)
        elif self._provider == "anthropic":
            from anthropic import Anthropic

            api_key = settings.anthropic_api_key.get_secret_value()
            if not api_key:
                raise ValueError("ANTHROPIC_API_KEY not configured")
            self._client = Anthropic(api_key=api_key)
            self._model = self._model or settings.anthropic_model_planner
            logger.info("Using Anthropic Claude", model=self._model)
        elif self._provider == "openai":
            from openai import OpenAI

            api_key = settings.openai_api_key.get_secret_value()
            if not api_key:
                raise ValueError("OPENAI_API_KEY not configured")
            self._client = OpenAI(api_key=api_key)
            self._model = self._model or settings.openai_model_planner
            logger.info("Using OpenAI", model=self._model)
        elif self._provider == "openrouter":
            from openai import OpenAI

            api_key = settings.openrouter_api_key.get_secret_value()
            if not api_key:
                raise ValueError("OPENROUTER_API_KEY not configured")
            self._client = OpenAI(
                api_key=api_key,
                base_url="https://openrouter.ai/api/v1",
            )
            self._model = self._model or settings.openrouter_model_planner
            logger.info("Using OpenRouter", model=self._model)
        else:  # together (default)
            api_key = settings.together_api_key.get_secret_value()
            if not api_key:
                raise ValueError("TOGETHER_API_KEY not configured")
            self._client = Together(api_key=api_key)
            self._model = self._model or settings.together_model_planner
            self._provider = "together"
            logger.info("Using Together.ai", model=self._model)

        # Initialize memory
        self.memory = await init_memory()

        # Initialize decision memory for behavioral learning
        self.decision_memory = await init_decision_memory()

        self._initialized = True
        logger.info("Agent initialized", provider=self._provider, model=self._model)

    def _ensure_initialized(self) -> None:
        """Ensure agent is initialized."""
        if not self._initialized:
            raise RuntimeError("Agent not initialized. Call initialize() first.")

    def _llm_chat(
        self, messages: list[dict], max_tokens: int = 2048, temperature: float = 0.3
    ) -> str:
        """Call the LLM with provider-specific API handling."""
        if self._provider == "google":
            # Gemini API format with fallback to Together.ai
            model = self._client.GenerativeModel(self._model)
            # Convert OpenAI format to Gemini format
            gemini_messages = []
            system_prompt = None
            for msg in messages:
                role = msg["role"]
                content = msg["content"]
                if role == "system":
                    system_prompt = content
                elif role == "assistant":
                    gemini_messages.append({"role": "model", "parts": [content]})
                else:
                    gemini_messages.append({"role": "user", "parts": [content]})

            # Prepend system prompt to first user message if present
            if system_prompt and gemini_messages:
                first_content = gemini_messages[0]["parts"][0]
                gemini_messages[0]["parts"][0] = f"{system_prompt}\n\n{first_content}"

            use_fallback = False
            fallback_reason = None

            try:
                safety_settings = [
                    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
                ]
                response = model.generate_content(
                    gemini_messages,
                    generation_config={
                        "max_output_tokens": max_tokens,
                        "temperature": temperature,
                    },
                    safety_settings=safety_settings,
                )
                # Try to get text - can fail even with response
                try:
                    return response.text
                except Exception as text_err:
                    use_fallback = True
                    fallback_reason = f"response.text failed: {text_err}"
            except Exception as e:
                use_fallback = True
                fallback_reason = str(e)

            # Fallback to Together.ai if Gemini failed
            if use_fallback:
                logger.warning("Gemini failed, using Together.ai fallback", reason=fallback_reason)
                from together import Together

                settings = get_settings()
                api_key = settings.together_api_key
                # Handle SecretStr
                if hasattr(api_key, "get_secret_value"):
                    api_key = api_key.get_secret_value()
                if api_key:
                    fallback_client = Together(api_key=api_key)
                    fallback_response = fallback_client.chat.completions.create(
                        model="meta-llama/Llama-3.3-70B-Instruct-Turbo",
                        messages=messages,
                        max_tokens=max_tokens,
                        temperature=temperature,
                    )
                    return fallback_response.choices[0].message.content
                else:
                    raise ValueError(f"Gemini failed and no fallback: {fallback_reason}")

        elif self._provider == "anthropic":
            # Anthropic API format
            system_prompt = None
            anthropic_messages = []
            for msg in messages:
                if msg["role"] == "system":
                    system_prompt = msg["content"]
                else:
                    anthropic_messages.append(msg)

            response = self._client.messages.create(
                model=self._model,
                max_tokens=max_tokens,
                system=system_prompt or "",
                messages=anthropic_messages,
            )
            return response.content[0].text

        else:
            # OpenAI/Together format
            response = self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            return response.choices[0].message.content

    async def _build_system_prompt(self) -> str:
        """Build the system prompt with available tools, filtered by operating mode."""
        # Filter tools based on current operating mode
        all_tools = self.tool_registry.all()
        eligible_tools, filtered_tools, current_mode = await self.tool_filter.get_eligible_tools(
            all_tools
        )

        # Cache for reference
        self._current_mode = current_mode
        self._filtered_tools = filtered_tools

        tool_descriptions = []
        for tool in eligible_tools:
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

        # Add filter notice if tools were filtered
        filter_notice = ""
        if filtered_tools:
            filter_notice = self.tool_filter.format_filter_notice(filtered_tools, current_mode)

        # Get bootstrap context (personality, identity, context)
        bootstrap_context = await self._get_bootstrap_context()

        # Get memory context (recent observations, curated knowledge)
        memory_context = await self._get_memory_context()

        # Get current date/time for context
        now = datetime.now()
        current_date = now.strftime("%A, %B %d, %Y")
        current_time = now.strftime("%H:%M")

        return f"""You are Cognitex, a personal assistant with access to a knowledge graph containing emails, tasks, calendar events, contacts, and documents.
{bootstrap_context}

**Current date and time: {current_date} at {current_time}**

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
- **Respond promptly**: Once you have enough information, respond. For simple queries, 2-4 tool calls is usually enough.
- **Research deeply**: For questions requiring web research (travel, prices, comparisons, current events, finding specific information), use the `research` tool. Break the question into specific, independent sub-questions. The tool handles parallel execution and iterative refinement automatically. Never give generic advice like "check Google" or "try Skyscanner" — actually find the information. If the research tool reports it hit its cycle limit, tell the user the results may be incomplete.

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
- Person's communication history: MATCH (p:Person {{email: $email}})<-[:SENT_BY]-(e:Email) RETURN e ORDER BY e.date DESC LIMIT 5
{filter_notice}
{memory_context}"""

    async def _get_bootstrap_context(self) -> str:
        """
        Get bootstrap context from SOUL.md, IDENTITY.md, CONTEXT.md files.

        Bootstrap files provide explicit personality and user context,
        replacing algorithmic learning with human-editable files.
        """
        try:
            from cognitex.agent.bootstrap import get_bootstrap_loader

            loader = get_bootstrap_loader()
            return await loader.get_formatted_prompt_section()
        except Exception as e:
            logger.debug("Failed to load bootstrap context", error=str(e))
            return ""

    async def _get_memory_context(self) -> str:
        """
        Get memory context from daily logs and curated memory.

        Memory files provide human-readable, persistent observations
        that the agent should remember across sessions.
        """
        try:
            from cognitex.services.memory_files import get_memory_file_service

            service = get_memory_file_service()
            return await service.get_context_for_prompt(max_entries=10)
        except Exception as e:
            logger.debug("Failed to load memory context", error=str(e))
            return ""

    async def _get_learned_context(self, message: str) -> str | None:
        """
        Build learned context from past decisions, patterns, and rules.

        This queries the decision memory to find:
        1. Similar past decisions (RAG retrieval)
        2. Active preference rules
        3. Communication patterns for mentioned people

        Returns formatted context to append to system prompt.
        """
        if not self.decision_memory:
            return None

        sections = []

        try:
            # Find similar past decisions via RAG
            similar_decisions = await self.decision_memory.traces.find_similar_decisions(
                query_text=message,
                min_quality=0.6,
                limit=3,
            )

            if similar_decisions:
                examples = []
                for d in similar_decisions:
                    if d["similarity"] > 0.3:  # Only include reasonably similar
                        status_note = ""
                        if d["status"] == "edited":
                            status_note = " (user edited this)"
                        elif d["status"] == "rejected":
                            status_note = " (user rejected this)"

                        examples.append(
                            f"- Similar request: {d.get('trigger_summary', 'N/A')}\n"
                            f"  Action taken: {d['action_type']}{status_note}\n"
                            f"  Quality: {d['quality_score']:.0%}"
                        )

                if examples:
                    sections.append(
                        "## Relevant Past Decisions\n"
                        "Use these as reference for how to handle similar requests:\n\n"
                        + "\n".join(examples)
                    )

            # Get active preference rules
            matching_rules = await self.decision_memory.rules.get_matching_rules(
                context={"trigger_type": "user_request"},
                rule_type=None,
            )

            if matching_rules:
                rules_text = []
                for rule in matching_rules[:5]:  # Top 5 rules
                    if rule["confidence"] >= 0.3:
                        pref = rule.get("preference", {})
                        rules_text.append(
                            f"- {rule['rule_name']}: {pref} (confidence: {rule['confidence']:.0%})"
                        )

                if rules_text:
                    sections.append(
                        "## User Preferences\n"
                        "Learned preferences from past interactions:\n\n" + "\n".join(rules_text)
                    )

        except Exception as e:
            logger.warning("Failed to get learned context", error=str(e))
            return None

        if sections:
            return "\n\n".join(sections)
        return None

    async def chat(self, message: str) -> str:
        """
        Handle a conversational message using ReAct loop.

        Args:
            message: User's message

        Returns:
            Agent's response
        """
        response, _ = await self.chat_with_approvals(message)
        return response

    async def chat_with_approvals(self, message: str) -> tuple[str, list[str]]:
        """
        Handle a conversational message and return both response and new approval IDs.

        Args:
            message: User's message

        Returns:
            Tuple of (response text, list of new approval IDs created in this interaction)
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

        return response, trace.pending_approvals

    async def _react_loop(self, message: str) -> ReactTrace:
        """Execute the ReAct reasoning loop."""
        trace = ReactTrace()

        # Build conversation for the LLM
        system_prompt = await self._build_system_prompt()

        # Add learned context from past decisions
        learned_context = await self._get_learned_context(message)
        if learned_context:
            system_prompt += f"\n\n{learned_context}"

        # Start with system prompt
        conversation = [
            {"role": "system", "content": system_prompt},
        ]

        # Include recent conversation history from working memory
        context = await self.memory.working.get_context()
        recent_interactions = context.get("interactions", [])

        # Check if we need to summarize older interactions
        summary_prefix = ""
        settings = get_settings()
        if (
            settings.context_summarization_enabled
            and len(recent_interactions) > settings.recent_turns_to_keep
        ):
            from cognitex.agent.summarization import format_summary_for_prompt, get_summarizer

            summarizer = get_summarizer()
            if summarizer.should_summarize(recent_interactions):
                # Get session ID from working memory
                session_id = context.get("session_id", "default")
                result = await summarizer.summarize_older_messages(
                    messages=recent_interactions,
                    session_id=session_id,
                )
                summary_prefix = format_summary_for_prompt(result.summary)
                recent_interactions = result.recent_messages
                logger.debug(
                    "Summarized conversation history",
                    messages_summarized=result.messages_summarized,
                    tokens_saved=result.estimated_tokens_saved,
                )
            else:
                # Just keep recent interactions
                recent_interactions = recent_interactions[-settings.recent_turns_to_keep :]
        else:
            # Fallback: keep last 10 if summarization disabled
            recent_interactions = recent_interactions[-10:]

        # Add summary to system prompt if generated
        if summary_prefix:
            system_prompt = summary_prefix + system_prompt

        for interaction in recent_interactions:
            role = interaction.get("role", "user")
            content = interaction.get("content", "")
            # Map our roles to OpenAI roles
            if role == "agent":
                conversation.append({"role": "assistant", "content": content})
            else:
                conversation.append({"role": "user", "content": content})

        # Add current message
        conversation.append({"role": "user", "content": f"User message: {message}"})

        for iteration in range(MAX_REACT_ITERATIONS):
            logger.debug("ReAct iteration", iteration=iteration)

            # Get next thought/action from LLM
            try:
                content = self._llm_chat(conversation, max_tokens=2048, temperature=0.3)
                content = content.strip()

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
                observation, approval_id, trace_id = await self._execute_action(
                    step.action,
                    step.action_input,
                    thought=step.thought,
                    message_context=message,
                )
                step.observation = observation

                if approval_id:
                    trace.pending_approvals.append(approval_id)

                if trace_id:
                    trace.decision_trace_ids.append(trace_id)

                trace.steps.append(step)

                # Add to conversation for next iteration
                conversation.append(
                    {
                        "role": "assistant",
                        "content": content,
                    }
                )
                conversation.append(
                    {
                        "role": "user",
                        "content": f"Observation: {observation}",
                    }
                )

            except Exception as e:
                logger.error("ReAct iteration failed", error=str(e), iteration=iteration)
                trace.final_response = (
                    f"I encountered an error while processing your request: {str(e)[:100]}"
                )
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

    async def _execute_action(
        self,
        action: str,
        action_input: dict,
        thought: str = "",
        message_context: str = "",
    ) -> tuple[str, str | None, str | None]:
        """
        Execute a tool action and return the observation.

        Args:
            action: Tool name to execute
            action_input: Parameters for the tool
            thought: Agent's reasoning for this action (for decision trace)
            message_context: Original user message (for decision trace)

        Returns:
            Tuple of (observation string, approval_id if approval needed, trace_id if decision traced)
        """
        tool = self.tool_registry.get(action)
        if not tool:
            return (
                f"Error: Unknown tool '{action}'. Available tools: {[t.name for t in self.tool_registry.all()]}",
                None,
                None,
            )

        # Determine if this action should be traced
        # Trace: approval-required actions, task/event creation, email drafting
        should_trace = tool.risk == ToolRisk.APPROVAL or action in [
            "create_task",
            "update_task",
            "complete_task",
            "create_event",
            "draft_email",
            "send_email",
        ]
        trace_id = None

        # Set depth/model context for sub-agent spawning
        if action in ("spawn_subagent", "research"):
            tool._current_depth = 0  # orchestrator is depth 0
            tool._parent_model = self._model
            tool._parent_provider = self._provider

        try:
            result = await tool.execute(**action_input)

            if result.success:
                # Create decision trace for significant actions
                if should_trace and self.decision_memory:
                    try:
                        trace_id = await self.decision_memory.traces.create_trace(
                            trigger_type="user_request",
                            action_type=action,
                            proposed_action=action_input,
                            context={
                                "user_message": message_context,
                                "tool_parameters": action_input,
                            },
                            trigger_summary=message_context[:100] if message_context else None,
                            reasoning=thought,
                            metadata={
                                "tool_name": action,
                                "tool_risk": tool.risk.value,
                                "needs_approval": result.needs_approval,
                                "approval_id": result.approval_id,  # Link trace to approval
                            },
                        )

                        # If auto-executed, record immediate feedback
                        if not result.needs_approval:
                            await self.decision_memory.traces.record_feedback(
                                trace_id,
                                status="auto_executed",
                                final_action=action_input,
                                implicit_signals={"auto_executed": True},
                            )
                    except Exception as e:
                        logger.warning("Failed to create decision trace", error=str(e))

                # Format the observation
                if result.needs_approval:
                    return (
                        f"Action staged for approval (ID: {result.approval_id}). Details: {result.data}",
                        result.approval_id,
                        trace_id,
                    )

                # Format data nicely for observation
                if result.data is None:
                    return "Success (no data returned)", None, trace_id
                elif isinstance(result.data, list):
                    if len(result.data) == 0:
                        return "No results found", None, trace_id
                    # Format list results
                    items = []
                    for item in result.data[:15]:  # Limit to 15 items
                        if isinstance(item, dict):
                            # Pick key fields
                            item_str = ", ".join(
                                f"{k}: {v}" for k, v in list(item.items())[:6] if v is not None
                            )
                            items.append(f"  - {item_str}")
                        else:
                            items.append(f"  - {item}")

                    obs = f"Found {len(result.data)} results:\n" + "\n".join(items)
                    if len(result.data) > 15:
                        obs += f"\n  ... and {len(result.data) - 15} more"
                    return obs, None, trace_id
                elif isinstance(result.data, dict):
                    return (
                        f"Result: {json.dumps(result.data, indent=2, default=str)}",
                        None,
                        trace_id,
                    )
                else:
                    return f"Result: {result.data}", None, trace_id
            else:
                return f"Error: {result.error}", None, None

        except Exception as e:
            logger.error("Tool execution failed", tool=action, error=str(e))
            return f"Error executing {action}: {str(e)}", None, None

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
            response = self._llm_chat(
                [{"role": "user", "content": prompt}],
                max_tokens=1024,
                temperature=0.4,
            )
            return response.strip()
        except Exception as e:
            logger.error("Summary generation failed", error=str(e))
            return (
                "I found some information but had trouble summarizing it. Please try asking again."
            )

    # =========================================================================
    # APPROVAL HANDLING
    # =========================================================================

    async def handle_approval(
        self,
        approval_id: str,
        approved: bool,
        feedback: str | None = None,
        edited_action: dict | None = None,
    ) -> dict:
        """
        Handle user approval or rejection of a staged action.

        Args:
            approval_id: The approval to resolve
            approved: Whether the action was approved
            feedback: Optional explicit feedback from user
            edited_action: If the user edited the action before approving
        """
        self._ensure_initialized()

        logger.info("Handling approval", approval_id=approval_id, approved=approved)

        approval = await self.memory.working.resolve_approval(approval_id, approved, feedback)

        if not approval:
            return {"success": False, "error": "Approval not found or expired"}

        result = {"success": True, "approval_id": approval_id, "action": approval["action_type"]}

        # Record decision feedback for learning
        if self.decision_memory:
            try:
                trace = await self.decision_memory.traces.find_by_approval_id(approval_id)
                if trace:
                    status = ("edited" if edited_action else "approved") if approved else "rejected"

                    await self.decision_memory.traces.record_feedback(
                        trace["id"],
                        status=status,
                        final_action=edited_action or approval["params"],
                        user_edits={"edited": bool(edited_action)} if edited_action else None,
                        explicit_feedback=feedback,
                        implicit_signals={
                            "was_edited": bool(edited_action),
                            "had_explicit_feedback": bool(feedback),
                        },
                    )
                    logger.info("Recorded decision feedback", trace_id=trace["id"], status=status)

                    # Learn from approved/edited actions
                    if approved and approval["action_type"] == "send_email":
                        await self._learn_from_email_approval(
                            approval["params"],
                            edited_action,
                            trace["id"],
                        )
            except Exception as e:
                logger.warning("Failed to record decision feedback", error=str(e))

        if approved:
            action_type = approval["action_type"]
            # Use edited action if provided, otherwise use original params
            params = edited_action if edited_action else approval["params"]
            # Preserve reply_to_id from original if not in edited_action
            if edited_action and approval["params"].get("reply_to_id"):
                params["reply_to_id"] = approval["params"]["reply_to_id"]

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
                    if edited_action:
                        result["was_edited"] = True

                    # Track draft lifecycle (learn from edits)
                    try:
                        draft_id = approval["params"].get("draft_node_id")
                        if draft_id:
                            from cognitex.services.email_style import track_draft_sent

                            await track_draft_sent(
                                draft_id=draft_id,
                                final_body=params["body"],
                            )
                    except Exception as track_e:
                        logger.debug("Failed to track draft sent", error=str(track_e))

                    # Store to episodic memory (non-critical)
                    try:
                        await self.memory.episodic.store(
                            content=f"Sent email to {params['to']}: {params['subject']}",
                            memory_type="interaction",
                            importance=4,
                            entities=[params["to"]],
                        )
                    except Exception as mem_e:
                        logger.warning("Failed to store email to episodic memory", error=str(mem_e))

                except Exception as e:
                    result["success"] = False
                    result["error"] = str(e)

            elif action_type == "create_event":
                from cognitex.services.calendar import CalendarService

                calendar = CalendarService()

                try:
                    # Preserve attendees from original if not in edited_action
                    if (
                        edited_action
                        and approval["params"].get("attendees")
                        and not params.get("attendees")
                    ):
                        params["attendees"] = approval["params"]["attendees"]

                    event = calendar.create_event(
                        title=params["title"],
                        start=params["start"],
                        end=params["end"],
                        attendees=params.get("attendees"),
                        description=params.get("description"),
                    )
                    result["created"] = True
                    result["event_id"] = event.get("id")
                    if edited_action:
                        result["was_edited"] = True

                    # Store to episodic memory (non-critical)
                    try:
                        await self.memory.episodic.store(
                            content=f"Created event: {params['title']} at {params['start']}",
                            memory_type="interaction",
                            importance=3,
                        )
                    except Exception as mem_e:
                        logger.warning("Failed to store event to episodic memory", error=str(mem_e))

                except Exception as e:
                    result["success"] = False
                    result["error"] = str(e)

        else:
            # Track draft discarded if this was an email draft
            if approval["action_type"] == "send_email":
                try:
                    draft_id = approval["params"].get("draft_node_id")
                    if draft_id:
                        from cognitex.services.email_style import track_draft_discarded

                        await track_draft_discarded(draft_id)
                except Exception as track_e:
                    logger.debug("Failed to track draft discarded", error=str(track_e))

            if feedback:
                try:
                    await self.memory.episodic.store(
                        content=f"User rejected {approval['action_type']}: {feedback}",
                        memory_type="feedback",
                        importance=4,
                        metadata={
                            "approval_id": approval_id,
                            "action_type": approval["action_type"],
                        },
                    )
                except Exception as mem_e:
                    logger.warning("Failed to store rejection to episodic memory", error=str(mem_e))

        return result

    async def get_pending_approvals(self) -> list[dict]:
        """Get all pending approval requests."""
        self._ensure_initialized()
        return await self.memory.working.get_pending_approvals()

    # =========================================================================
    # LEARNING FROM FEEDBACK
    # =========================================================================

    async def _learn_from_email_approval(
        self,
        original_params: dict,
        edited_action: dict | None,
        trace_id: str,
    ) -> None:
        """
        Learn communication patterns from approved/edited email actions.

        This extracts patterns like:
        - Preferred tone for this recipient
        - Typical response length
        - Greeting/sign-off styles
        """
        if not self.decision_memory:
            return

        try:
            recipient_email = original_params.get("to")
            if not recipient_email:
                return

            # Use the final approved content
            final_content = edited_action or original_params
            body = final_content.get("body", "")

            if not body:
                return

            # Analyze the email content to extract patterns
            patterns = self._analyze_email_patterns(body)

            if patterns:
                # Update communication pattern for this recipient
                await self.decision_memory.patterns.update_pattern(
                    person_email=recipient_email,
                    updates=patterns,
                    increment_interaction=True,
                )

                # Add this trace as an example
                await self.decision_memory.patterns.add_example_trace(
                    person_email=recipient_email,
                    trace_id=trace_id,
                )

                logger.info(
                    "Learned communication pattern",
                    recipient=recipient_email,
                    patterns=list(patterns.keys()),
                )

        except Exception as e:
            logger.warning("Failed to learn from email approval", error=str(e))

    def _analyze_email_patterns(self, body: str) -> dict:
        """
        Analyze email body to extract communication patterns.

        Returns dict with detected patterns like tone, greeting style, etc.
        """
        patterns = {}
        body_lower = body.lower()
        body_lines = body.strip().split("\n")

        # Detect greeting style
        first_line = body_lines[0].strip().lower() if body_lines else ""
        if first_line.startswith(("dear ", "hello ", "good morning", "good afternoon")):
            patterns["greeting_style"] = "formal_greeting"
        elif first_line.startswith(("hi ", "hey ")):
            patterns["greeting_style"] = "casual_greeting"
        elif any(name in first_line for name in ["dr.", "mr.", "ms.", "mrs."]):
            patterns["greeting_style"] = "formal_title"

        # Detect sign-off style
        last_lines = "\n".join(body_lines[-3:]).lower() if len(body_lines) >= 3 else body_lower
        if any(
            s in last_lines for s in ["best regards", "kind regards", "sincerely", "yours truly"]
        ):
            patterns["sign_off_style"] = "formal"
        elif any(s in last_lines for s in ["thanks", "cheers", "best", "talk soon"]):
            patterns["sign_off_style"] = "casual"

        # Detect response length preference
        word_count = len(body.split())
        if word_count < 50:
            patterns["typical_response_length"] = "brief"
        elif word_count < 150:
            patterns["typical_response_length"] = "moderate"
        else:
            patterns["typical_response_length"] = "detailed"

        # Detect tone (simple heuristics)
        formal_indicators = ["please", "kindly", "would you", "i would appreciate", "thank you for"]
        casual_indicators = ["!", "awesome", "great", "cool", "sounds good", "no worries"]

        formal_count = sum(1 for ind in formal_indicators if ind in body_lower)
        casual_count = sum(1 for ind in casual_indicators if ind in body_lower)

        if formal_count > casual_count + 1:
            patterns["preferred_tone"] = "formal"
        elif casual_count > formal_count + 1:
            patterns["preferred_tone"] = "casual"
        else:
            patterns["preferred_tone"] = "professional"

        return patterns

    # =========================================================================
    # SCHEDULED MODES (briefing, review, etc.)
    # =========================================================================

    async def morning_briefing(self) -> str:
        """Generate a morning briefing using ReAct, including context packs and learning insights."""
        # Get the base briefing from ReAct
        briefing = await self.chat(
            "Give me a morning briefing: what's on my calendar today, what are my top priority tasks, "
            "any urgent emails I should know about, and any important deadlines coming up this week."
        )

        # Add context packs for today's events
        context_section = await self._get_todays_context_packs()
        if context_section:
            briefing += f"\n\n{context_section}"

        # Add commitment summary (WP5)
        commitment_section = await self._get_commitment_summary()
        if commitment_section:
            briefing += f"\n\n{commitment_section}"

        # Add learning insights (Phase 4 integration)
        learning_section = await self._get_learning_insights()
        if learning_section:
            briefing += f"\n\n{learning_section}"

        return briefing

    async def _get_commitment_summary(self) -> str | None:
        """Get commitment ledger summary for the morning briefing.

        Includes overdue and approaching commitments.
        """
        try:
            from cognitex.agent.graph_observer import GraphObserver

            observer = GraphObserver()
            overdue, approaching = await asyncio.gather(
                observer.get_overdue_commitments(),
                observer.get_approaching_commitments(hours=48),
                return_exceptions=True,
            )

            if isinstance(overdue, Exception):
                overdue = []
            if isinstance(approaching, Exception):
                approaching = []

            if not overdue and not approaching:
                return None

            sections = []

            if overdue:
                lines = []
                for c in overdue[:5]:
                    deadline_str = ""
                    if c.get("deadline"):
                        deadline_str = f" (due: {str(c['deadline'])[:10]})"
                    lines.append(f"- {c.get('description', 'Unknown')}{deadline_str}")
                sections.append(f"**Overdue Commitments** ({len(overdue)})\n" + "\n".join(lines))

            if approaching:
                lines = []
                for c in approaching[:5]:
                    deadline_str = ""
                    if c.get("deadline"):
                        deadline_str = f" (due: {str(c['deadline'])[:10]})"
                    lines.append(f"- {c.get('description', 'Unknown')}{deadline_str}")
                sections.append(
                    f"**Approaching Deadlines** ({len(approaching)})\n" + "\n".join(lines)
                )

            if sections:
                return "---\n**Commitment Ledger**\n\n" + "\n\n".join(sections)

            return None

        except Exception as e:
            logger.warning("Failed to get commitment summary for briefing", error=str(e))
            return None

    async def _get_learning_insights(self) -> str | None:
        """Get learning system insights for the morning briefing.

        Includes:
        - Recent patterns learned from task proposals
        - Tasks at risk of deferral
        - Duration calibration insights
        """
        try:
            from cognitex.agent.learning import get_learning_system

            ls = get_learning_system()
            if ls is None:
                return None

            summary = await ls.get_learning_summary()

            sections = []

            # Learning insights
            insights = summary.get("insights", [])
            if insights:
                insight_lines = [f"• {insight}" for insight in insights[:3]]
                sections.append("**📊 Learning Insights**\n" + "\n".join(insight_lines))

            # High-risk tasks (deferral prediction)
            deferrals = summary.get("deferrals", {})
            high_risk = deferrals.get("high_risk_tasks", [])
            if high_risk:
                risk_lines = []
                for task in high_risk[:3]:
                    name = task.get("title", task.get("task_id", "Unknown"))
                    score = task.get("risk_score", 0)
                    factors = task.get("factors", [])
                    factor_text = f" ({', '.join(factors[:2])})" if factors else ""
                    risk_lines.append(f"• {name} - {score:.0%} risk{factor_text}")

                sections.append(
                    f"**⚠️ Deferral Risk** ({len(high_risk)} tasks at risk)\n"
                    + "\n".join(risk_lines)
                )

            # Duration calibration (if patterns exist)
            duration = summary.get("duration_calibration", {})
            avg_error = duration.get("average_estimation_error")
            if avg_error is not None and abs(avg_error) > 0.2:
                direction = "underestimating" if avg_error > 0 else "overestimating"
                sections.append(
                    f"**⏱️ Time Estimation**: You tend to {direction} task durations by "
                    f"{abs(avg_error):.0%} on average. Consider adjusting estimates accordingly."
                )

            # Proposal approval rate trends
            proposals = summary.get("proposal_patterns", {})
            overall_rate = proposals.get("overall_approval_rate")
            if overall_rate is not None:
                if overall_rate < 0.3:
                    sections.append(
                        f"**📋 Proposal Quality**: Low approval rate ({overall_rate:.0%}). "
                        "I'm learning from your feedback to improve suggestions."
                    )
                elif overall_rate > 0.8:
                    sections.append(
                        f"**📋 Proposal Quality**: High alignment ({overall_rate:.0%} approved). "
                        "My suggestions are matching your preferences well."
                    )

            if sections:
                return "---\n" + "\n\n".join(sections)

            return None

        except Exception as e:
            logger.warning("Failed to get learning insights for briefing", error=str(e))
            return None

    async def _get_todays_context_packs(self) -> str | None:
        """Get context packs for today's events to include in briefing."""
        try:
            from datetime import datetime

            from cognitex.agent.context_pack import BuildStage, get_context_pack_compiler
            from cognitex.services.calendar import CalendarService

            calendar = CalendarService()
            now = datetime.now()
            end_of_day = now.replace(hour=23, minute=59, second=59)

            # Get today's events
            events = calendar.get_events(
                time_min=now.isoformat() + "Z",
                time_max=end_of_day.isoformat() + "Z",
                max_results=10,
            )

            if not events:
                return None

            compiler = get_context_pack_compiler()
            context_sections = []

            for event in events[:5]:  # Limit to first 5 events
                title = event.get("summary", "Untitled")
                description = event.get("description", "")
                start = event.get("start", {}).get("dateTime", "")
                attendees = event.get("attendees", [])

                # Only build packs for events with attendees (meetings)
                if not attendees:
                    continue

                try:
                    pack = await compiler.build_pack(
                        event_id=event.get("id", ""),
                        event_title=title,
                        event_description=description,
                        attendees=attendees,
                        event_start=start,
                        stage=BuildStage.T_24H,
                    )

                    if pack and (pack.attendee_briefs or pack.artifacts or pack.last_interaction):
                        # Format a concise version for briefing
                        pack_text = f"**{title}** ({start[:16] if start else 'TBD'})"

                        if pack.last_interaction:
                            pack_text += f"\n  • Last interaction: {pack.last_interaction[:100]}..."

                        if pack.attendee_briefs:
                            attendee_names = [a.name for a in pack.attendee_briefs[:3]]
                            pack_text += f"\n  • With: {', '.join(attendee_names)}"

                        if pack.artifacts:
                            artifact_names = [
                                a.get("title", a.get("name", "doc"))[:30]
                                for a in pack.artifacts[:3]
                            ]
                            pack_text += f"\n  • Related docs: {', '.join(artifact_names)}"

                        context_sections.append(pack_text)

                except Exception as e:
                    logger.debug(
                        "Failed to build context pack for event", event_title=title, error=str(e)
                    )
                    continue

            if context_sections:
                return "---\n**📋 Meeting Context Packs**\n\n" + "\n\n".join(context_sections)

            return None

        except Exception as e:
            logger.warning("Failed to get context packs for briefing", error=str(e))
            return None

    async def evening_review(self) -> str:
        """Generate an evening review using ReAct."""
        return await self.chat(
            "Give me an end-of-day review: what did I have scheduled today, "
            "what tasks might need attention tomorrow, and any emails that came in today that need responses."
        )


# Singleton with async lock to prevent race conditions
_agent: Agent | None = None
_agent_lock = asyncio.Lock()


async def get_agent() -> Agent:
    """Get or create the agent singleton (thread-safe)."""
    global _agent
    if _agent is not None:
        return _agent
    async with _agent_lock:
        # Double-check after acquiring lock
        if _agent is None:
            _agent = Agent()
            await _agent.initialize()
    return _agent


# Keep AgentMode for backward compatibility with existing code
from enum import Enum  # noqa: E402


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
