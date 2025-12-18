"""Discord bot entry point with full agent integration."""

import asyncio
import json
from datetime import datetime
from typing import Optional

import discord
import structlog
from discord import app_commands
from discord.ext import commands

from cognitex.config import get_settings

logger = structlog.get_logger()


class CognitexBot(commands.Bot):
    """Cognitex Discord bot for proactive notifications and agent interaction."""

    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.settings = get_settings()
        self.agent = None
        self.trigger_system = None
        self._db_initialized = False
        self._processing_lock = asyncio.Lock()

    async def setup_hook(self) -> None:
        """Called when the bot is starting up."""
        logger.info("Bot setup starting")

        # Initialize databases
        await self._init_databases()

        # Initialize agent
        await self._init_agent()

        # Initialize trigger system (scheduled jobs + event listeners)
        await self._init_triggers()

        # Register slash commands
        self.tree.add_command(tasks_command)
        self.tree.add_command(projects_command)
        self.tree.add_command(goals_command)
        self.tree.add_command(today_command)
        self.tree.add_command(briefing_command)
        self.tree.add_command(approvals_command)
        self.tree.add_command(status_command)
        self.tree.add_command(triggers_command)
        self.tree.add_command(goal_parse_command)

        # Sync commands with Discord
        try:
            synced = await self.tree.sync()
            logger.info("Synced slash commands", count=len(synced))
        except Exception as e:
            logger.error("Failed to sync slash commands", error=str(e))

    async def _init_databases(self) -> None:
        """Initialize database connections."""
        if self._db_initialized:
            return

        try:
            from cognitex.db.neo4j import init_neo4j
            from cognitex.db.postgres import init_postgres
            from cognitex.db.redis import init_redis

            await init_neo4j()
            await init_postgres()
            await init_redis()

            self._db_initialized = True
            logger.info("Databases initialized for Discord bot")
        except Exception as e:
            logger.error("Failed to initialize databases", error=str(e))

    async def _init_agent(self) -> None:
        """Initialize the agent system."""
        try:
            from cognitex.agent.core import get_agent
            self.agent = await get_agent()
            logger.info("Agent initialized for Discord bot")
        except Exception as e:
            logger.error("Failed to initialize agent", error=str(e))

    async def _init_triggers(self) -> None:
        """Initialize the trigger system for scheduled and event-driven actions."""
        try:
            from cognitex.agent.triggers import start_triggers
            self.trigger_system = await start_triggers()
            logger.info("Trigger system started (scheduled jobs active)")
        except Exception as e:
            logger.error("Failed to initialize trigger system", error=str(e))

    async def on_ready(self) -> None:
        """Called when the bot is connected and ready."""
        logger.info("Bot connected", user=str(self.user), guilds=len(self.guilds))

        # Start Redis listener for agent notifications
        self.notification_task = self.loop.create_task(self.listen_for_notifications())

        # Send startup message
        await self.send_notification("Cognitex is online and ready to assist.")

    async def listen_for_notifications(self) -> None:
        """Listen to Redis for notifications from the Agent."""
        from cognitex.db.redis import get_redis

        try:
            redis = get_redis()
            pubsub = redis.pubsub()
            await pubsub.subscribe("cognitex:notifications")

            logger.info("Listening for agent notifications on Redis...")

            async for message in pubsub.listen():
                logger.debug("Redis notification message", message_type=message.get("type"))
                if message["type"] == "message":
                    try:
                        logger.info("Received notification from Redis", data=message["data"][:100] if message.get("data") else None)
                        data = json.loads(message["data"])
                        content = data.get("message")
                        urgency = data.get("urgency", "normal")
                        approval_id = data.get("approval_id")

                        if content:
                            logger.info("Sending notification to Discord", urgency=urgency, length=len(content))
                            await self.send_formatted_notification(
                                content,
                                urgency=urgency,
                                approval_id=approval_id,
                            )
                            logger.info("Notification sent to Discord successfully")

                    except json.JSONDecodeError as e:
                        logger.warning("Failed to parse notification", error=str(e))
                    except Exception as e:
                        logger.error("Failed to process notification", error=str(e), exc_info=True)

        except asyncio.CancelledError:
            logger.info("Notification listener cancelled")
            await pubsub.unsubscribe()
            raise
        except Exception as e:
            logger.error("Redis listener failed", error=str(e))
            await asyncio.sleep(30)
            self.notification_task = self.loop.create_task(self.listen_for_notifications())

    async def on_message(self, message: discord.Message) -> None:
        """Handle incoming messages with natural language processing."""
        # Ignore own messages
        if message.author == self.user:
            return

        # Log all received messages for debugging
        logger.debug(
            "Message received",
            channel_id=str(message.channel.id),
            configured_channel=self.settings.discord_channel_id,
            author=str(message.author),
            content=message.content[:50] if message.content else "(empty)",
            is_dm=isinstance(message.channel, discord.DMChannel),
            bot_mentioned=self.user in message.mentions if self.user else False,
        )

        # Handle DMs directly
        if isinstance(message.channel, discord.DMChannel):
            logger.info("Processing DM", author=str(message.author))
            await self.handle_natural_language(message)
            return

        # In guild channels, respond if:
        # 1. Bot is mentioned, OR
        # 2. Message is in the configured channel
        is_mentioned = self.user in message.mentions
        is_configured_channel = str(message.channel.id) == self.settings.discord_channel_id

        if not is_mentioned and not is_configured_channel:
            logger.debug("Ignoring message - not mentioned and not in configured channel")
            return

        # Process commands first
        await self.process_commands(message)

        # Natural language processing for non-command messages
        if not message.content.startswith(("!", "/")):
            # Strip the bot mention from the message if present
            content = message.content
            if is_mentioned and self.user:
                content = content.replace(f"<@{self.user.id}>", "").strip()
                content = content.replace(f"<@!{self.user.id}>", "").strip()

            if content:
                # Temporarily modify message content for processing
                original_content = message.content
                message.content = content
                await self.handle_natural_language(message)
                message.content = original_content

    async def handle_natural_language(self, message: discord.Message) -> None:
        """Process natural language messages via the agent."""
        content = message.content.strip()

        if not content:
            return

        logger.info("Processing Discord message", content=content[:100], author=str(message.author))

        # Show typing indicator while processing
        async with message.channel.typing():
            async with self._processing_lock:
                try:
                    if not self.agent:
                        await self._init_agent()

                    if not self.agent:
                        await message.channel.send(
                            "Sorry, I'm having trouble connecting to my brain. Please try again in a moment."
                        )
                        return

                    # Get response from agent (with approval IDs from this interaction)
                    response, new_approval_ids = await self.agent.chat_with_approvals(content)

                    # Format and send response
                    await self.send_agent_response(message.channel, response)

                    # Send only the new approvals from this interaction with buttons
                    if new_approval_ids:
                        await self._send_approvals_by_ids(message.channel, new_approval_ids)

                except Exception as e:
                    logger.error("Agent chat failed", error=str(e))
                    await message.channel.send(
                        f"Sorry, I encountered an error: {str(e)[:100]}"
                    )

    async def send_agent_response(
        self,
        channel: discord.TextChannel,
        response: str,
    ) -> None:
        """Send agent response, splitting if necessary."""
        # Discord message limit is 2000 characters
        max_length = 1900

        if len(response) <= max_length:
            await channel.send(response)
        else:
            # Split into chunks at paragraph breaks
            chunks = []
            current_chunk = ""

            for paragraph in response.split("\n\n"):
                if len(current_chunk) + len(paragraph) + 2 <= max_length:
                    current_chunk += paragraph + "\n\n"
                else:
                    if current_chunk:
                        chunks.append(current_chunk.strip())
                    current_chunk = paragraph + "\n\n"

            if current_chunk:
                chunks.append(current_chunk.strip())

            for i, chunk in enumerate(chunks):
                if i > 0:
                    await asyncio.sleep(0.5)  # Rate limiting
                await channel.send(chunk)

    async def _send_approvals_by_ids(self, channel, approval_ids: list[str]) -> None:
        """Send specific approvals with buttons to the given channel."""
        try:
            if not self.agent:
                return

            for approval_id in approval_ids:
                # Get the specific approval
                approval = await self.agent.memory.working.get_approval(approval_id)
                if not approval:
                    continue

                action_type = approval.get("action_type", "")
                params = approval.get("params", {})
                reasoning = approval.get("reasoning", "")

                # Format based on action type
                if action_type == "send_email":
                    content = (
                        f"**📧 Email Draft for Approval**\n\n"
                        f"**To:** {params.get('to', '?')}\n"
                        f"**Subject:** {params.get('subject', '?')}\n\n"
                        f"**Body:**\n```\n{params.get('body', '')[:800]}{'...' if len(params.get('body', '')) > 800 else ''}\n```"
                    )
                    if reasoning:
                        content += f"\n_{reasoning}_"
                elif action_type == "create_event":
                    content = (
                        f"**📅 Calendar Event for Approval**\n\n"
                        f"**Title:** {params.get('title', '?')}\n"
                        f"**Time:** {params.get('start', '?')} - {params.get('end', '?')}\n"
                    )
                    if params.get("attendees"):
                        content += f"**Attendees:** {', '.join(params['attendees'])}\n"
                    if params.get("description"):
                        content += f"\n{params['description']}"
                    if reasoning:
                        content += f"\n\n_{reasoning}_"
                else:
                    content = f"**Action for Approval: {action_type}**\n\n{json.dumps(params, indent=2)[:500]}"

                # Create embed
                embed = discord.Embed(
                    description=content,
                    color=discord.Color.orange(),
                    timestamp=datetime.now(),
                )
                embed.set_footer(text=f"Approval ID: {approval_id}")

                # Add approval buttons
                view = ApprovalView(approval_id, self)

                await channel.send(embed=embed, view=view)
                logger.info("Sent approval request to channel", approval_id=approval_id, action_type=action_type)

        except Exception as e:
            logger.error("Failed to send approvals", error=str(e))

    async def send_notification(self, content: str) -> None:
        """Send a simple notification to the configured channel."""
        if not self.settings.discord_channel_id:
            logger.warning("No Discord channel configured for notifications")
            return

        channel = self.get_channel(int(self.settings.discord_channel_id))
        if channel and isinstance(channel, discord.TextChannel):
            await channel.send(content)

    async def send_formatted_notification(
        self,
        content: str,
        urgency: str = "normal",
        approval_id: Optional[str] = None,
    ) -> None:
        """Send a formatted notification with optional approval buttons."""
        if not self.settings.discord_channel_id:
            logger.warning("No Discord channel ID configured")
            return

        channel = self.get_channel(int(self.settings.discord_channel_id))
        if not channel:
            logger.warning("Could not find Discord channel", channel_id=self.settings.discord_channel_id)
            return
        if not isinstance(channel, discord.TextChannel):
            logger.warning("Channel is not a TextChannel", channel_type=type(channel).__name__)
            return

        # Create embed based on urgency
        color = {
            "high": discord.Color.red(),
            "normal": discord.Color.blue(),
            "low": discord.Color.light_gray(),
        }.get(urgency, discord.Color.blue())

        embed = discord.Embed(
            description=content,
            color=color,
            timestamp=datetime.now(),
        )

        # Add urgency indicator
        if urgency == "high":
            embed.set_author(name="🚨 Urgent")
        elif urgency == "low":
            embed.set_author(name="ℹ️ Info")

        # Add approval buttons if this is an approval request
        view = None
        if approval_id:
            view = ApprovalView(approval_id, self)
            embed.set_footer(text=f"Approval ID: {approval_id}")

        await channel.send(embed=embed, view=view)

    async def on_reaction_add(self, reaction: discord.Reaction, user: discord.User) -> None:
        """Handle reactions for quick approvals."""
        if user == self.user:
            return

        # Check if this is a reaction to a bot message with approval
        if reaction.message.author != self.user:
            return

        # Check for approval reactions
        if str(reaction.emoji) == "✅":
            await self.handle_reaction_approval(reaction.message, approved=True)
        elif str(reaction.emoji) == "❌":
            await self.handle_reaction_approval(reaction.message, approved=False)

    async def handle_reaction_approval(
        self,
        message: discord.Message,
        approved: bool,
    ) -> None:
        """Handle approval via reaction."""
        # Try to extract approval ID from the message
        if not message.embeds:
            return

        embed = message.embeds[0]
        if not embed.footer or not embed.footer.text:
            return

        footer = embed.footer.text
        if not footer.startswith("Approval ID:"):
            return

        approval_id = footer.replace("Approval ID:", "").strip()

        try:
            result = await self.agent.handle_approval(approval_id, approved)

            if result.get("success"):
                status = "approved" if approved else "rejected"
                await message.channel.send(f"Action **{status}**: {result.get('action', 'Unknown')}")
            else:
                await message.channel.send(f"Failed to process approval: {result.get('error', 'Unknown error')}")

        except Exception as e:
            logger.error("Approval handling failed", error=str(e))
            await message.channel.send(f"Error processing approval: {str(e)[:100]}")


class ApprovalView(discord.ui.View):
    """Discord UI view for approval buttons."""

    def __init__(self, approval_id: str, bot: CognitexBot):
        super().__init__(timeout=3600)  # 1 hour timeout
        self.approval_id = approval_id
        self.bot = bot

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.green, emoji="✅")
    async def approve_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        """Handle approve button click."""
        await interaction.response.defer()

        try:
            result = await self.bot.agent.handle_approval(self.approval_id, approved=True)

            if result.get("success"):
                await interaction.followup.send(
                    f"✅ **Approved**: {result.get('action', 'Action')} executed successfully.",
                    ephemeral=False,
                )
                # Disable buttons after action
                self.disable_all_buttons()
                await interaction.message.edit(view=self)
            else:
                await interaction.followup.send(
                    f"❌ Failed: {result.get('error', 'Unknown error')}",
                    ephemeral=True,
                )

        except Exception as e:
            await interaction.followup.send(f"Error: {str(e)[:100]}", ephemeral=True)

    @discord.ui.button(label="Edit & Approve", style=discord.ButtonStyle.blurple, emoji="✏️")
    async def edit_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        """Handle edit button click - opens a modal to edit content."""
        try:
            # Get the approval details
            approval = await self.bot.agent.memory.working.get_approval(self.approval_id)
            if not approval:
                await interaction.response.send_message("Approval not found or expired.", ephemeral=True)
                return

            action_type = approval.get("action_type", "")
            params = approval.get("params", {})

            if action_type == "send_email":
                modal = EmailEditModal(
                    approval_id=self.approval_id,
                    bot=self.bot,
                    view=self,
                    original_to=params.get("to", ""),
                    original_subject=params.get("subject", ""),
                    original_body=params.get("body", ""),
                )
                await interaction.response.send_modal(modal)
            elif action_type == "create_event":
                modal = EventEditModal(
                    approval_id=self.approval_id,
                    bot=self.bot,
                    view=self,
                    original_title=params.get("title", ""),
                    original_start=params.get("start", ""),
                    original_end=params.get("end", ""),
                    original_description=params.get("description", ""),
                )
                await interaction.response.send_modal(modal)
            else:
                await interaction.response.send_message(
                    f"Edit not supported for action type: {action_type}",
                    ephemeral=True,
                )

        except Exception as e:
            logger.error("Edit button failed", error=str(e))
            await interaction.response.send_message(f"Error: {str(e)[:100]}", ephemeral=True)

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.red, emoji="❌")
    async def reject_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        """Handle reject button click."""
        await interaction.response.defer()

        try:
            result = await self.bot.agent.handle_approval(
                self.approval_id,
                approved=False,
                feedback="Rejected via Discord",
            )

            await interaction.followup.send(
                f"❌ **Rejected**: {result.get('action', 'Action')} was not executed.",
                ephemeral=False,
            )
            # Disable buttons after action
            self.disable_all_buttons()
            await interaction.message.edit(view=self)

        except Exception as e:
            await interaction.followup.send(f"Error: {str(e)[:100]}", ephemeral=True)

    def disable_all_buttons(self) -> None:
        """Disable all buttons in the view."""
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True


class EmailEditModal(discord.ui.Modal, title="Edit Email"):
    """Modal for editing email before approval."""

    def __init__(
        self,
        approval_id: str,
        bot: CognitexBot,
        view: ApprovalView,
        original_to: str,
        original_subject: str,
        original_body: str,
    ):
        super().__init__()
        self.approval_id = approval_id
        self.bot = bot
        self.view = view
        self.original_params = {
            "to": original_to,
            "subject": original_subject,
            "body": original_body,
        }

        # Add input fields with original values
        self.to_field = discord.ui.TextInput(
            label="To",
            default=original_to,
            required=True,
            max_length=200,
        )
        self.add_item(self.to_field)

        self.subject_field = discord.ui.TextInput(
            label="Subject",
            default=original_subject,
            required=True,
            max_length=200,
        )
        self.add_item(self.subject_field)

        self.body_field = discord.ui.TextInput(
            label="Body",
            style=discord.TextStyle.paragraph,
            default=original_body[:4000],  # Discord limit
            required=True,
            max_length=4000,
        )
        self.add_item(self.body_field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Handle form submission - approve with edited content."""
        await interaction.response.defer()

        try:
            # Build edited action with new values
            edited_action = {
                "to": self.to_field.value,
                "subject": self.subject_field.value,
                "body": self.body_field.value,
            }

            # Check what was changed for learning
            changes = []
            if self.to_field.value != self.original_params["to"]:
                changes.append("recipient")
            if self.subject_field.value != self.original_params["subject"]:
                changes.append("subject")
            if self.body_field.value != self.original_params["body"]:
                changes.append("body")

            # Approve with edited content
            result = await self.bot.agent.handle_approval(
                self.approval_id,
                approved=True,
                edited_action=edited_action,
                feedback=f"User edited: {', '.join(changes)}" if changes else None,
            )

            if result.get("success"):
                change_note = f" (edited: {', '.join(changes)})" if changes else ""
                await interaction.followup.send(
                    f"✅ **Approved with edits{change_note}**: Email sent successfully.",
                    ephemeral=False,
                )
                # Disable buttons
                self.view.disable_all_buttons()
                await interaction.message.edit(view=self.view)
            else:
                await interaction.followup.send(
                    f"❌ Failed: {result.get('error', 'Unknown error')}",
                    ephemeral=True,
                )

        except Exception as e:
            logger.error("Email edit submit failed", error=str(e))
            await interaction.followup.send(f"Error: {str(e)[:100]}", ephemeral=True)


class EventEditModal(discord.ui.Modal, title="Edit Calendar Event"):
    """Modal for editing calendar event before approval."""

    def __init__(
        self,
        approval_id: str,
        bot: CognitexBot,
        view: ApprovalView,
        original_title: str,
        original_start: str,
        original_end: str,
        original_description: str,
    ):
        super().__init__()
        self.approval_id = approval_id
        self.bot = bot
        self.view = view
        self.original_params = {
            "title": original_title,
            "start": original_start,
            "end": original_end,
            "description": original_description,
        }

        self.title_field = discord.ui.TextInput(
            label="Title",
            default=original_title,
            required=True,
            max_length=200,
        )
        self.add_item(self.title_field)

        self.start_field = discord.ui.TextInput(
            label="Start (ISO format: 2025-12-18T09:00:00)",
            default=original_start,
            required=True,
            max_length=30,
        )
        self.add_item(self.start_field)

        self.end_field = discord.ui.TextInput(
            label="End (ISO format: 2025-12-18T10:00:00)",
            default=original_end,
            required=True,
            max_length=30,
        )
        self.add_item(self.end_field)

        self.description_field = discord.ui.TextInput(
            label="Description",
            style=discord.TextStyle.paragraph,
            default=original_description[:1000] if original_description else "",
            required=False,
            max_length=1000,
        )
        self.add_item(self.description_field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Handle form submission - approve with edited content."""
        await interaction.response.defer()

        try:
            edited_action = {
                "title": self.title_field.value,
                "start": self.start_field.value,
                "end": self.end_field.value,
                "description": self.description_field.value,
            }

            changes = []
            if self.title_field.value != self.original_params["title"]:
                changes.append("title")
            if self.start_field.value != self.original_params["start"]:
                changes.append("start time")
            if self.end_field.value != self.original_params["end"]:
                changes.append("end time")
            if self.description_field.value != self.original_params.get("description", ""):
                changes.append("description")

            result = await self.bot.agent.handle_approval(
                self.approval_id,
                approved=True,
                edited_action=edited_action,
                feedback=f"User edited: {', '.join(changes)}" if changes else None,
            )

            if result.get("success"):
                change_note = f" (edited: {', '.join(changes)})" if changes else ""
                await interaction.followup.send(
                    f"✅ **Approved with edits{change_note}**: Event created successfully.",
                    ephemeral=False,
                )
                self.view.disable_all_buttons()
                await interaction.message.edit(view=self.view)
            else:
                await interaction.followup.send(
                    f"❌ Failed: {result.get('error', 'Unknown error')}",
                    ephemeral=True,
                )

        except Exception as e:
            logger.error("Event edit submit failed", error=str(e))
            await interaction.followup.send(f"Error: {str(e)[:100]}", ephemeral=True)


# =============================================================================
# SLASH COMMANDS
# =============================================================================

@app_commands.command(name="tasks", description="Show your pending tasks")
@app_commands.describe(limit="Number of tasks to show (default: 10)")
async def tasks_command(interaction: discord.Interaction, limit: int = 10) -> None:
    """Show pending tasks."""
    await interaction.response.defer()

    try:
        from cognitex.db.neo4j import init_neo4j, get_neo4j_session
        from cognitex.db.graph_schema import get_tasks

        await init_neo4j()

        async for session in get_neo4j_session():
            tasks = await get_tasks(session, status="pending", limit=limit)

            if not tasks:
                await interaction.followup.send("No pending tasks found.")
                return

            # Format tasks
            lines = [f"**📋 Pending Tasks ({len(tasks)})**\n"]
            for i, task in enumerate(tasks, 1):
                title = task.get("title", "Untitled")
                energy = task.get("energy_cost", "?")
                due = task.get("due", "No due date")

                lines.append(f"{i}. **{title}**")
                lines.append(f"   Energy: {energy}/10 | Due: {due}")

            await interaction.followup.send("\n".join(lines))
            return

    except Exception as e:
        logger.error("Tasks command failed", error=str(e))
        await interaction.followup.send(f"Error fetching tasks: {str(e)[:100]}")


@app_commands.command(name="projects", description="Show your active projects")
@app_commands.describe(limit="Number of projects to show (default: 10)")
async def projects_command(interaction: discord.Interaction, limit: int = 10) -> None:
    """Show active projects."""
    await interaction.response.defer()

    try:
        from cognitex.services.tasks import get_project_service

        project_service = get_project_service()
        projects = await project_service.list(status="active", limit=limit)

        if not projects:
            await interaction.followup.send("No active projects found.")
            return

        lines = [f"**📁 Active Projects ({len(projects)})**\n"]
        for i, project in enumerate(projects, 1):
            title = project.get("title", "Untitled")
            task_count = project.get("task_count", 0)
            done_count = project.get("done_count", 0)
            lines.append(f"{i}. **{title}** ({done_count}/{task_count} tasks)")

        await interaction.followup.send("\n".join(lines))

    except Exception as e:
        logger.error("Projects command failed", error=str(e))
        await interaction.followup.send(f"Error fetching projects: {str(e)[:100]}")


@app_commands.command(name="goals", description="Show your goals")
@app_commands.describe(limit="Number of goals to show (default: 10)")
async def goals_command(interaction: discord.Interaction, limit: int = 10) -> None:
    """Show goals."""
    await interaction.response.defer()

    try:
        from cognitex.services.tasks import get_goal_service

        goal_service = get_goal_service()
        goals = await goal_service.list(limit=limit)

        if not goals:
            await interaction.followup.send("No goals found.")
            return

        lines = [f"**🎯 Goals ({len(goals)})**\n"]
        for i, goal in enumerate(goals, 1):
            title = goal.get("title", "Untitled")
            timeframe = goal.get("timeframe", "")
            status = goal.get("status", "active")
            tf_str = f" [{timeframe}]" if timeframe else ""
            lines.append(f"{i}. **{title}**{tf_str} - {status}")

        await interaction.followup.send("\n".join(lines))

    except Exception as e:
        logger.error("Goals command failed", error=str(e))
        await interaction.followup.send(f"Error fetching goals: {str(e)[:100]}")


@app_commands.command(name="goal-parse", description="Parse and create a goal from natural language")
@app_commands.describe(description="Natural language goal description")
async def goal_parse_command(interaction: discord.Interaction, description: str) -> None:
    """Parse a goal description and create structured entities."""
    await interaction.response.defer()

    try:
        from cognitex.services.goal_parser import parse_and_create_goal

        result = await parse_and_create_goal(
            description,
            create_projects=True,
            create_tasks=True,
            link_people=True,
            dry_run=False,
        )

        parsed = result["parsed"]
        created = result["created"]

        embed = discord.Embed(
            title=f"🎯 Goal Created: {parsed['title'][:50]}",
            color=discord.Color.green(),
            timestamp=datetime.now(),
        )

        if parsed.get("timeframe"):
            embed.add_field(name="Timeframe", value=parsed["timeframe"], inline=True)
        if parsed.get("target_date"):
            embed.add_field(name="Target", value=parsed["target_date"], inline=True)
        if parsed.get("themes"):
            embed.add_field(name="Themes", value=", ".join(parsed["themes"]), inline=True)

        # Created summary
        summary = []
        if created.get("goal"):
            summary.append(f"✅ Goal: `{created['goal']['id'][:12]}...`")
        if created.get("projects"):
            summary.append(f"📁 {len(created['projects'])} project(s)")
        if created.get("tasks"):
            summary.append(f"📋 {len(created['tasks'])} task(s)")
        if created.get("people_linked"):
            summary.append(f"👥 {len(created['people_linked'])} person(s) linked")

        embed.add_field(name="Created", value="\n".join(summary) or "Goal only", inline=False)

        if parsed.get("projects"):
            proj_names = [p["title"] for p in parsed["projects"][:5]]
            embed.add_field(name="Projects", value="\n".join(f"• {p}" for p in proj_names), inline=False)

        embed.set_footer(text=f"Confidence: {parsed.get('confidence', 0):.0%}")

        await interaction.followup.send(embed=embed)

    except Exception as e:
        logger.error("Goal parse command failed", error=str(e))
        await interaction.followup.send(f"Error parsing goal: {str(e)[:100]}")


@app_commands.command(name="today", description="Show today's schedule and priorities")
async def today_command(interaction: discord.Interaction) -> None:
    """Show today's schedule."""
    await interaction.response.defer()

    try:
        from cognitex.db.neo4j import init_neo4j, get_neo4j_session
        from cognitex.db.graph_schema import get_todays_events, get_tasks
        from datetime import datetime

        await init_neo4j()

        async for session in get_neo4j_session():
            events = await get_todays_events(session)
            tasks = await get_tasks(session, status="pending", limit=5)

            embed = discord.Embed(
                title=f"📅 Today - {datetime.now().strftime('%A, %B %d')}",
                color=discord.Color.blue(),
            )

            # Events section
            if events:
                event_lines = []
                total_energy = 0
                for event in events[:8]:
                    time = event.get("start", "")[:5] if event.get("start") else "?"
                    title = event.get("title", "Untitled")[:40]
                    energy = event.get("energy_impact", 0)
                    total_energy += energy
                    event_lines.append(f"`{time}` {title} (⚡{energy})")

                embed.add_field(
                    name=f"📆 Events ({len(events)})",
                    value="\n".join(event_lines) or "No events",
                    inline=False,
                )
                embed.add_field(
                    name="⚡ Energy Forecast",
                    value=f"Total event energy: {total_energy}",
                    inline=True,
                )
            else:
                embed.add_field(
                    name="📆 Events",
                    value="No events scheduled",
                    inline=False,
                )

            # Tasks section
            if tasks:
                task_lines = []
                for task in tasks:
                    title = task.get("title", "Untitled")[:35]
                    energy = task.get("energy_cost", "?")
                    task_lines.append(f"• {title} (⚡{energy})")

                embed.add_field(
                    name=f"✅ Top Tasks ({len(tasks)})",
                    value="\n".join(task_lines),
                    inline=False,
                )

            await interaction.followup.send(embed=embed)
            return

    except Exception as e:
        logger.error("Today command failed", error=str(e))
        await interaction.followup.send(f"Error: {str(e)[:100]}")


@app_commands.command(name="briefing", description="Get your morning briefing")
async def briefing_command(interaction: discord.Interaction) -> None:
    """Generate and show morning briefing."""
    await interaction.response.defer()

    try:
        from cognitex.agent.core import get_agent

        agent = await get_agent()
        briefing = await agent.morning_briefing()

        embed = discord.Embed(
            title="☀️ Morning Briefing",
            description=briefing,
            color=discord.Color.gold(),
            timestamp=datetime.now(),
        )

        await interaction.followup.send(embed=embed)

    except Exception as e:
        logger.error("Briefing command failed", error=str(e))
        await interaction.followup.send(f"Error generating briefing: {str(e)[:100]}")


@app_commands.command(name="approvals", description="Show pending approval requests")
async def approvals_command(interaction: discord.Interaction) -> None:
    """Show pending approvals."""
    await interaction.response.defer()

    try:
        from cognitex.agent.core import get_agent

        agent = await get_agent()
        approvals = await agent.get_pending_approvals()

        if not approvals:
            await interaction.followup.send("No pending approvals.")
            return

        embed = discord.Embed(
            title=f"📝 Pending Approvals ({len(approvals)})",
            color=discord.Color.orange(),
        )

        for approval in approvals[:10]:
            action_type = approval.get("action_type", "Unknown")
            params = approval.get("params", {})
            approval_id = approval.get("id", "?")

            # Format based on action type
            if action_type == "send_email":
                desc = f"To: {params.get('to', '?')}\nSubject: {params.get('subject', '?')[:30]}"
            elif action_type == "create_event":
                desc = f"Event: {params.get('title', '?')}\nTime: {params.get('start', '?')}"
            else:
                desc = str(params)[:100]

            embed.add_field(
                name=f"{action_type} (`{approval_id[:8]}...`)",
                value=desc,
                inline=False,
            )

        await interaction.followup.send(embed=embed)

    except Exception as e:
        logger.error("Approvals command failed", error=str(e))
        await interaction.followup.send(f"Error: {str(e)[:100]}")


@app_commands.command(name="status", description="Show system status")
async def status_command(interaction: discord.Interaction) -> None:
    """Show system status."""
    await interaction.response.defer()

    checks = {}

    # Check Neo4j
    try:
        from cognitex.db.neo4j import get_neo4j_session
        async for session in get_neo4j_session():
            result = await session.run("MATCH (n) RETURN count(n) as count")
            record = await result.single()
            checks["Neo4j"] = f"✅ {record['count']} nodes"
    except Exception as e:
        checks["Neo4j"] = f"❌ {str(e)[:30]}"

    # Check Redis
    try:
        from cognitex.db.redis import get_redis
        redis = get_redis()
        await redis.ping()
        checks["Redis"] = "✅ Connected"
    except Exception as e:
        checks["Redis"] = f"❌ {str(e)[:30]}"

    # Check Postgres
    try:
        from cognitex.db.postgres import get_session
        from sqlalchemy import text
        async for session in get_session():
            await session.execute(text("SELECT 1"))
            checks["PostgreSQL"] = "✅ Connected"
    except Exception as e:
        checks["PostgreSQL"] = f"❌ {str(e)[:30]}"

    # Check Trigger System
    try:
        from cognitex.agent.triggers import get_trigger_system
        trigger_system = await get_trigger_system()
        jobs = trigger_system.list_scheduled()
        checks["Triggers"] = f"✅ {len(jobs)} scheduled"
    except Exception as e:
        checks["Triggers"] = f"❌ {str(e)[:30]}"

    embed = discord.Embed(
        title="🔧 System Status",
        color=discord.Color.green() if all("✅" in v for v in checks.values()) else discord.Color.orange(),
        timestamp=datetime.now(),
    )

    for service, status in checks.items():
        embed.add_field(name=service, value=status, inline=True)

    await interaction.followup.send(embed=embed)


@app_commands.command(name="triggers", description="Show scheduled triggers and next run times")
async def triggers_command(interaction: discord.Interaction) -> None:
    """Show scheduled triggers."""
    await interaction.response.defer()

    try:
        from cognitex.agent.triggers import get_trigger_system

        trigger_system = await get_trigger_system()
        jobs = trigger_system.list_scheduled()

        if not jobs:
            await interaction.followup.send("No scheduled triggers configured.")
            return

        embed = discord.Embed(
            title="⏰ Scheduled Triggers",
            color=discord.Color.purple(),
            timestamp=datetime.now(),
        )

        for job in jobs:
            next_run = job.get("next_run", "Not scheduled")
            if next_run and next_run != "Not scheduled":
                # Parse and format the time nicely
                try:
                    from datetime import datetime as dt
                    next_dt = dt.fromisoformat(next_run.replace("Z", "+00:00"))
                    next_run = next_dt.strftime("%Y-%m-%d %H:%M")
                except Exception:
                    pass

            embed.add_field(
                name=job.get("name", job.get("id", "Unknown")),
                value=f"Next: `{next_run}`",
                inline=True,
            )

        await interaction.followup.send(embed=embed)

    except Exception as e:
        logger.error("Triggers command failed", error=str(e))
        await interaction.followup.send(f"Error: {str(e)[:100]}")


# =============================================================================
# MAIN
# =============================================================================

def main() -> None:
    """Run the Discord bot."""
    settings = get_settings()

    if not settings.discord_bot_token.get_secret_value():
        logger.error("DISCORD_BOT_TOKEN not configured")
        return

    bot = CognitexBot()
    bot.run(settings.discord_bot_token.get_secret_value())


if __name__ == "__main__":
    main()
