"""Lightweight reminders: /remind 1h30m take the pizza out of the oven."""

import asyncio
import re

import discord
from discord import app_commands
from discord.ext import commands

DURATION_RE = re.compile(r"(\d+)\s*([dhms])", re.IGNORECASE)
UNIT_SECONDS = {"d": 86400, "h": 3600, "m": 60, "s": 1}
MAX_SECONDS = 7 * 86400


def parse_duration(text: str) -> int | None:
    matches = DURATION_RE.findall(text)
    if not matches or DURATION_RE.sub("", text).strip():
        return None
    return sum(int(amount) * UNIT_SECONDS[unit.lower()] for amount, unit in matches)


class ReminderCog(commands.Cog):
    """In-memory reminders — they survive as long as the bot stays up."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.tasks: set[asyncio.Task] = set()

    def cog_unload(self) -> None:
        for task in self.tasks:
            task.cancel()

    @app_commands.command(name="remind", description="Set a reminder, e.g. duration '1h30m'")
    @app_commands.describe(
        duration="How long from now, e.g. 10m, 2h, 1d, 1h30m",
        text="What should I remind you about?",
    )
    async def remind(self, interaction: discord.Interaction, duration: str, text: str) -> None:
        seconds = parse_duration(duration)
        if seconds is None or seconds <= 0:
            await interaction.response.send_message(
                "I couldn't parse that duration — try something like `10m`, `2h`, or `1d12h`.",
                ephemeral=True,
            )
            return
        if seconds > MAX_SECONDS:
            await interaction.response.send_message("Max reminder is 7 days!", ephemeral=True)
            return

        await interaction.response.send_message(f"⏰ Got it! I'll remind you in **{duration}**: {text}")
        task = asyncio.create_task(self._fire(interaction, seconds, text))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _fire(self, interaction: discord.Interaction, seconds: int, text: str) -> None:
        await asyncio.sleep(seconds)
        try:
            await interaction.channel.send(f"⏰ {interaction.user.mention} Reminder: {text}")
        except discord.HTTPException:
            pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ReminderCog(bot))
