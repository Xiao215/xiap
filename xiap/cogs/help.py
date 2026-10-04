"""/help — what xiap can do and how to use it."""

import discord
from discord import app_commands
from discord.ext import commands


class HelpCog(commands.Cog):
    @app_commands.command(name="help", description="Everything xiap can do and how to use it")
    async def help(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="👋 What xiap can do",
            color=discord.Color.blurple(),
            description=(
                "**💬 AI chat** — just @mention me or reply to one of my messages "
                "and I'll chat back. I look back through the channel (or a message you link) "
                "when I need more context, and I know this server's custom emojis."
            ),
        )
        tree = interaction.client.tree
        commands_list = sorted(
            tree.get_commands(guild=interaction.guild) or tree.get_commands(),
            key=lambda c: c.name,
        )
        lines = [
            f"`/{c.name}` — {c.description}" for c in commands_list if c.name != "help"
        ]
        embed.add_field(name="⚡ Slash commands", value="\n".join(lines), inline=False)
        embed.set_footer(text="Tip: type / in the message box to see every command with its options")
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(HelpCog())
