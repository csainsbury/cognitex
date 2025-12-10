"""Discord bot entry point."""

import asyncio
import structlog
import discord
from discord.ext import commands

from cognitex.config import get_settings

logger = structlog.get_logger()


class CognitexBot(commands.Bot):
    """Cognitex Discord bot for proactive notifications and interaction."""

    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.settings = get_settings()

    async def setup_hook(self) -> None:
        """Called when the bot is starting up."""
        logger.info("Bot setup starting")

    async def on_ready(self) -> None:
        """Called when the bot is connected and ready."""
        logger.info("Bot connected", user=str(self.user), guilds=len(self.guilds))

    async def on_message(self, message: discord.Message) -> None:
        """Handle incoming messages with natural language processing."""
        if message.author == self.user:
            return

        # Only respond in the configured channel
        if str(message.channel.id) != self.settings.discord_channel_id:
            return

        # Process commands first
        await self.process_commands(message)

        # Natural language processing for non-command messages
        if not message.content.startswith("!"):
            await self.handle_natural_language(message)

    async def handle_natural_language(self, message: discord.Message) -> None:
        """Process natural language messages."""
        content = message.content.lower()
        logger.info("Received message", content=content, author=str(message.author))

        # TODO: Implement natural language processing with Together.ai
        # For now, just acknowledge
        if any(word in content for word in ["hello", "hi", "hey"]):
            await message.channel.send("Hello! I'm Cognitex, your personal assistant. How can I help?")

    async def send_notification(self, content: str) -> None:
        """Send a proactive notification to the configured channel."""
        if not self.settings.discord_channel_id:
            logger.warning("No Discord channel configured for notifications")
            return

        channel = self.get_channel(int(self.settings.discord_channel_id))
        if channel and isinstance(channel, discord.TextChannel):
            await channel.send(content)


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
