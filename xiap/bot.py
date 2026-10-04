"""XiapBot: a friendly AI-powered Discord bot for the Sunday Social group."""

import logging

import discord
from discord.ext import commands

from xiap import config
from xiap.web import start_health_server

log = logging.getLogger(__name__)

COGS = (
    "xiap.cogs.chat",
    "xiap.cogs.fun",
    "xiap.cogs.help",
    "xiap.cogs.models",
    "xiap.cogs.privacy",
    "xiap.cogs.stocks",
    "xiap.cogs.papers",
    "xiap.cogs.polls",
    "xiap.cogs.reminders",
)


class XiapBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        # Never let user- or AI-written text ping @everyone/@here or roles.
        super().__init__(
            command_prefix="!",
            intents=intents,
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
        )
        self._synced = False
        self._all_commands: list = []

    async def setup_hook(self) -> None:
        await start_health_server(config.PORT)
        for cog in COGS:
            try:
                await self.load_extension(cog)
            except Exception:
                log.exception("Failed to load extension %s", cog)

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%d guilds)", self.user, len(self.guilds))
        if self._synced:
            return
        self._synced = True
        # Register commands per guild: guild commands update instantly, while
        # global ones can take up to an hour to propagate to clients.
        self._all_commands = self.tree.get_commands()
        for guild in self.guilds:
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %d commands to %s", len(synced), guild.name)
        # Remove the old global registrations so commands don't appear twice.
        self.tree.clear_commands(guild=None)
        await self.tree.sync()

    async def on_guild_join(self, guild: discord.Guild) -> None:
        for cmd in self._all_commands:
            self.tree.add_command(cmd, guild=guild)
        await self.tree.sync(guild=guild)
        log.info("Joined %s, synced commands", guild.name)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not config.DISCORD_TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")
    async with XiapBot() as bot:
        await bot.start(config.DISCORD_TOKEN)
