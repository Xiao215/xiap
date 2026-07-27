"""Quick games and toys: rock-paper-scissors (PvP!), dice, 8-ball, and more."""

import hashlib
import random

import discord
from discord import app_commands
from discord.ext import commands

RPS_EMOJI = {"rock": "🪨", "paper": "📄", "scissors": "✂️"}
RPS_BEATS = {"rock": "scissors", "paper": "rock", "scissors": "paper"}

EIGHT_BALL_ANSWERS = [
    "It is certain.", "Without a doubt.", "Yes, definitely.", "Most likely.",
    "Outlook good.", "Signs point to yes.", "Reply hazy, try again.",
    "Ask again later.", "Better not tell you now.", "Don't count on it.",
    "My reply is no.", "My sources say no.", "Outlook not so good.",
    "Very doubtful.", "Absolutely not.",
]


class RPSDuelView(discord.ui.View):
    """Both players secretly pick, then the result is revealed."""

    def __init__(self, challenger: discord.Member, opponent: discord.Member):
        super().__init__(timeout=120)
        self.players = {challenger.id: None, opponent.id: None}
        self.challenger = challenger
        self.opponent = opponent
        self.message: discord.Message | None = None
        for choice in ("rock", "paper", "scissors"):
            self.add_item(self._make_button(choice))

    def _make_button(self, choice: str) -> discord.ui.Button:
        button = discord.ui.Button(label=choice.title(), emoji=RPS_EMOJI[choice])

        async def callback(interaction: discord.Interaction) -> None:
            if interaction.user.id not in self.players:
                await interaction.response.send_message("This duel isn't yours 👀", ephemeral=True)
                return
            if self.players[interaction.user.id] is not None:
                await interaction.response.send_message("You already picked!", ephemeral=True)
                return
            self.players[interaction.user.id] = choice
            await interaction.response.send_message(f"You picked {RPS_EMOJI[choice]}", ephemeral=True)
            if all(self.players.values()):
                await self._reveal()

        button.callback = callback
        return button

    async def _reveal(self) -> None:
        c, o = self.players[self.challenger.id], self.players[self.opponent.id]
        if c == o:
            result = "It's a tie! 🤝"
        elif RPS_BEATS[c] == o:
            result = f"**{self.challenger.display_name}** wins! 🏆"
        else:
            result = f"**{self.opponent.display_name}** wins! 🏆"
        for item in self.children:
            item.disabled = True
        self.stop()
        if self.message:
            await self.message.edit(
                content=(
                    f"{self.challenger.display_name} {RPS_EMOJI[c]} vs "
                    f"{RPS_EMOJI[o]} {self.opponent.display_name}\n{result}"
                ),
                view=self,
            )

    async def on_timeout(self) -> None:
        if self.message:
            await self.message.edit(content="Duel expired — someone chickened out 🐔", view=None)


class FunCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="rps", description="Rock-paper-scissors — vs the bot or duel a friend")
    @app_commands.describe(opponent="Challenge a friend (leave empty to play the bot)")
    async def rps(self, interaction: discord.Interaction, opponent: discord.Member | None = None) -> None:
        if opponent is None or opponent.bot:
            view = discord.ui.View(timeout=60)
            for choice in ("rock", "paper", "scissors"):
                view.add_item(self._bot_rps_button(choice, interaction.user))
            await interaction.response.send_message("Pick your weapon:", view=view)
            return
        if opponent == interaction.user:
            await interaction.response.send_message("You can't duel yourself 🤨", ephemeral=True)
            return
        view = RPSDuelView(interaction.user, opponent)
        await interaction.response.send_message(
            f"{opponent.mention}, {interaction.user.display_name} challenges you to "
            "rock-paper-scissors! Both of you, pick secretly:",
            view=view,
        )
        view.message = await interaction.original_response()

    def _bot_rps_button(self, choice: str, player: discord.User) -> discord.ui.Button:
        button = discord.ui.Button(label=choice.title(), emoji=RPS_EMOJI[choice])

        async def callback(interaction: discord.Interaction) -> None:
            if interaction.user != player:
                await interaction.response.send_message("Start your own game with /rps!", ephemeral=True)
                return
            bot_choice = random.choice(list(RPS_EMOJI))
            if choice == bot_choice:
                result = "Tie! 🤝"
            elif RPS_BEATS[choice] == bot_choice:
                result = "You win! 🏆"
            else:
                result = "I win! 😎"
            await interaction.response.edit_message(
                content=f"You {RPS_EMOJI[choice]} vs {RPS_EMOJI[bot_choice]} me — {result}",
                view=None,
            )

        button.callback = callback
        return button

    @app_commands.command(name="roll", description="Roll dice, e.g. 2d6 or d20")
    @app_commands.describe(dice="Dice notation like 2d6, d20 (default: d6)")
    async def roll(self, interaction: discord.Interaction, dice: str = "d6") -> None:
        try:
            count_str, _, sides_str = dice.lower().partition("d")
            count = int(count_str) if count_str else 1
            sides = int(sides_str)
            if not (1 <= count <= 100 and 2 <= sides <= 1000):
                raise ValueError
        except ValueError:
            await interaction.response.send_message(
                "Use dice notation like `2d6` or `d20`.", ephemeral=True
            )
            return
        rolls = [random.randint(1, sides) for _ in range(count)]
        detail = f" ({' + '.join(map(str, rolls))})" if count > 1 else ""
        await interaction.response.send_message(f"🎲 Rolled **{sum(rolls)}**{detail} on {dice.lower()}")

    @app_commands.command(name="8ball", description="Ask the magic 8-ball a question")
    @app_commands.describe(question="Your yes/no question")
    async def eight_ball(self, interaction: discord.Interaction, question: str) -> None:
        await interaction.response.send_message(
            f"❓ *{question}*\n🎱 {random.choice(EIGHT_BALL_ANSWERS)}"
        )

    @app_commands.command(name="coinflip", description="Flip a coin")
    async def coinflip(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(f"🪙 **{random.choice(['Heads', 'Tails'])}**!")

    @app_commands.command(name="choose", description="Can't decide? Let me pick for you")
    @app_commands.describe(options="Comma-separated options, e.g. 'pizza, sushi, ramen'")
    async def choose(self, interaction: discord.Interaction, options: str) -> None:
        choices = [c.strip() for c in options.split(",") if c.strip()]
        if len(choices) < 2:
            await interaction.response.send_message("Give me at least two options! 😤", ephemeral=True)
            return
        await interaction.response.send_message(f"🤔 I choose... **{random.choice(choices)}**!")

    @app_commands.command(name="ship", description="Check the compatibility between two people 💘")
    async def ship(
        self, interaction: discord.Interaction, person1: discord.Member, person2: discord.Member
    ) -> None:
        # Deterministic per pair so the "result" is consistent — order doesn't matter.
        pair = "-".join(sorted([str(person1.id), str(person2.id)]))
        score = int(hashlib.sha256(pair.encode()).hexdigest(), 16) % 101
        bar = "❤️" * (score // 10) + "🖤" * (10 - score // 10)
        verdict = (
            "Soulmates!! 💍" if score > 90 else
            "Sparks are flying ✨" if score > 70 else
            "There's potential 👀" if score > 40 else
            "Better as friends 🫂" if score > 15 else
            "Run. 🏃"
        )
        await interaction.response.send_message(
            f"💘 {person1.display_name} × {person2.display_name}\n{bar} **{score}%** — {verdict}"
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(FunCog(bot))
