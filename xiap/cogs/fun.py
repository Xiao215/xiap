"""Little utilities for the group chat."""

import random

import discord
from discord import app_commands
from discord.ext import commands


class FunCog(commands.Cog):
    @app_commands.command(name="choose", description="Can't decide? Let me pick for you")
    @app_commands.describe(options="Comma-separated options, e.g. 'pizza, sushi, ramen'")
    async def choose(self, interaction: discord.Interaction, options: str) -> None:
        choices = [c.strip() for c in options.split(",") if c.strip()]
        if len(choices) < 2:
            await interaction.response.send_message("Give me at least two options! 😤", ephemeral=True)
            return
        await interaction.response.send_message(f"🤔 I choose... **{random.choice(choices)}**!")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(FunCog())
