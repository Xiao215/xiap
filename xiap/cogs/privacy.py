"""Privacy controls: what the AI is allowed to read.

The AI only ever sends data to its provider when someone pings the bot; these
commands control how much context goes along with that ping.
"""

import discord
from discord import app_commands
from discord.ext import commands

from xiap.store import store


class PrivacyCog(commands.Cog):
    @app_commands.command(
        name="context",
        description="Turn the AI's reading of this channel's recent messages on or off",
    )
    @app_commands.choices(mode=[
        app_commands.Choice(name="on", value="on"),
        app_commands.Choice(name="off", value="off"),
    ])
    async def context(self, interaction: discord.Interaction, mode: app_commands.Choice[str]) -> None:
        enabled = mode.value == "on"
        store.set_context(interaction.channel.id, enabled)
        if enabled:
            msg = "🟢 Context is **on** here — when pinged, I'll read this channel's recent messages to reply better."
        else:
            msg = "🔒 Context is **off** here — when pinged, I'll only see the message that pinged me, nothing else."
        await interaction.response.send_message(msg)

    @app_commands.command(
        name="optout",
        description="Exclude YOUR messages from the AI's context in every channel",
    )
    async def optout(self, interaction: discord.Interaction) -> None:
        store.set_optout(interaction.user.id, True)
        await interaction.response.send_message(
            f"🔒 {interaction.user.display_name}, your messages are now excluded from the AI's "
            "context everywhere. (Pinging me directly still works — that's your own choice.) "
            "Use /optin to undo.",
            ephemeral=True,
        )

    @app_commands.command(name="optin", description="Include your messages in the AI's context again")
    async def optin(self, interaction: discord.Interaction) -> None:
        store.set_optout(interaction.user.id, False)
        await interaction.response.send_message(
            "🟢 Welcome back — your messages can be part of the AI's context again.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PrivacyCog())
