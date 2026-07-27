"""Button-based polls with live results."""

import discord
from discord import app_commands
from discord.ext import commands

BAR_LENGTH = 12


class PollView(discord.ui.View):
    def __init__(self, question: str, options: list[str], author: discord.User):
        super().__init__(timeout=24 * 3600)
        self.question = question
        self.options = options
        self.author = author
        self.votes: dict[int, int] = {}  # user_id -> option index
        self.message: discord.Message | None = None
        for i, option in enumerate(options):
            self.add_item(self._make_button(i, option))

    def _make_button(self, index: int, option: str) -> discord.ui.Button:
        button = discord.ui.Button(label=option[:80], style=discord.ButtonStyle.primary)

        async def callback(interaction: discord.Interaction) -> None:
            self.votes[interaction.user.id] = index
            await interaction.response.edit_message(embed=self.build_embed(), view=self)

        button.callback = callback
        return button

    def build_embed(self, closed: bool = False) -> discord.Embed:
        total = len(self.votes)
        lines = []
        for i, option in enumerate(self.options):
            count = sum(1 for v in self.votes.values() if v == i)
            filled = round(BAR_LENGTH * count / total) if total else 0
            bar = "█" * filled + "░" * (BAR_LENGTH - filled)
            lines.append(f"**{option}**\n{bar} {count} vote{'s' if count != 1 else ''}")
        embed = discord.Embed(
            title=("📊 " if not closed else "📊 [CLOSED] ") + self.question,
            description="\n".join(lines),
            color=discord.Color.blurple() if not closed else discord.Color.greyple(),
        )
        embed.set_footer(text=f"{total} vote{'s' if total != 1 else ''} • started by {self.author.display_name}")
        return embed

    async def on_timeout(self) -> None:
        if self.message:
            await self.message.edit(embed=self.build_embed(closed=True), view=None)


class PollCog(commands.Cog):
    @app_commands.command(name="poll", description="Start a poll with up to 5 options")
    @app_commands.describe(
        question="What are you asking?",
        option1="First option",
        option2="Second option",
        option3="Third option (optional)",
        option4="Fourth option (optional)",
        option5="Fifth option (optional)",
    )
    async def poll(
        self,
        interaction: discord.Interaction,
        question: str,
        option1: str,
        option2: str,
        option3: str | None = None,
        option4: str | None = None,
        option5: str | None = None,
    ) -> None:
        options = [o for o in (option1, option2, option3, option4, option5) if o]
        view = PollView(question, options, interaction.user)
        await interaction.response.send_message(embed=view.build_embed(), view=view)
        view.message = await interaction.original_response()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PollCog())
