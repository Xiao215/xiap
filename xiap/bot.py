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
    "xiap.cogs.polls",
    "xiap.cogs.reminders",
    "xiap.cogs.newsletter",
)


class XiapBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self) -> None:
        await start_health_server(config.PORT)
        for cog in COGS:
            try:
                await self.load_extension(cog)
            except Exception:
                log.exception("Failed to load extension %s", cog)
        synced = await self.tree.sync()
        log.info("Synced %d slash commands", len(synced))

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%d guilds)", self.user, len(self.guilds))


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not config.DISCORD_TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")
    async with XiapBot() as bot:
        await bot.start(config.DISCORD_TOKEN)
