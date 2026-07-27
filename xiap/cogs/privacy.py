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
        description="Set how many recent messages the AI may read in this channel (0 = none)",
    )
    @app_commands.describe(messages="Max past messages the AI sees when pinged here, 0-30 (default 30)")
    async def context(
        self, interaction: discord.Interaction, messages: app_commands.Range[int, 0, 30]
    ) -> None:
        store.set_context_limit(interaction.channel.id, messages)
        if messages == 0:
            msg = "🔒 Context is **off** here — when pinged, I'll only see the message that pinged me, nothing else."
        else:
            msg = (
                f"🟢 When pinged here, I'll read up to the last **{messages}** message"
                f"{'s' if messages != 1 else ''} of this channel for context."
            )
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
