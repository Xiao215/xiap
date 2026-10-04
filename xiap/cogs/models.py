"""/models and /model — see which AI models can answer you, and pick one.

The pick is per person: it only changes who answers *your* pings. Claude
models are listed and selectable only for the bot owner, since Claude runs on
the owner's personal subscription. If your pick is down or rate-limited the
bot falls back to the default chain and says so under the reply.
"""

import discord
from discord import app_commands
from discord.ext import commands

from xiap.store import store

PROVIDER_NAMES = {"gemini": "Gemini", "cohere": "Cohere", "claude": "Claude (owner only)"}
DEFAULT = "default"


class ModelsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def _choices(self, user: discord.abc.User) -> dict[str, list[str]] | None:
        """Models this user may pick, by provider; None if AI chat isn't set up."""
        chat = self.bot.get_cog("ChatCog")
        if chat is None or chat.agent is None:
            return None
        lists = await chat.agent.list_models()
        if "claude" in lists and not await chat.is_owner(user):
            lists = {p: models for p, models in lists.items() if p != "claude"}
        return lists

    def _default_label(self, owner: bool) -> str:
        agent = self.bot.get_cog("ChatCog").agent
        chain = agent.chain(owner=owner)
        if not chain:
            return "nothing configured"
        provider, model = chain[0]
        return f"{provider}:{model}" if model else f"{provider} (server default)"

    @app_commands.command(name="models", description="List the AI models that can answer you, and which one you're using")
    async def models(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        lists = await self._choices(interaction.user)
        if not lists:
            await interaction.followup.send("AI chat isn't set up on this bot.", ephemeral=True)
            return
        owner = "claude" in lists
        pick = store.get_model_pick(interaction.user.id)
        embed = discord.Embed(
            title="🤖 AI models",
            color=discord.Color.blurple(),
            description=(
                f"**You're using:** `{pick or self._default_label(owner)}`"
                + ("" if pick else " (default)")
                + "\nSwitch with `/model`, or `/model default` to go back. Only affects replies to you."
            ),
        )
        agent = self.bot.get_cog("ChatCog").agent
        for provider, names in lists.items():
            default = agent.default_model(provider)
            lines = []
            for name in names:
                marks = (" ✅" if pick == f"{provider}:{name}" else "") + (" ⭐" if name == default else "")
                lines.append(f"`{name}`{marks}")
            value = "\n".join(lines) or "(none found)"
            embed.add_field(name=PROVIDER_NAMES.get(provider, provider), value=value[:1024], inline=True)
        embed.set_footer(text="✅ your pick · ⭐ provider default")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="model", description="Choose which AI model answers you")
    @app_commands.describe(model="A model from /models, or 'default'")
    async def model(self, interaction: discord.Interaction, model: str) -> None:
        await interaction.response.defer(ephemeral=True)
        lists = await self._choices(interaction.user)
        if not lists:
            await interaction.followup.send("AI chat isn't set up on this bot.", ephemeral=True)
            return
        if model.strip().lower() == DEFAULT:
            store.set_model_pick(interaction.user.id, None)
            label = self._default_label("claude" in lists)
            await interaction.followup.send(f"🔄 Back to the default: `{label}`.", ephemeral=True)
            return

        provider, _, name = model.strip().partition(":")
        if not name:  # bare model name: find which provider has it
            provider = next((p for p, names in lists.items() if model.strip() in names), "")
            name = model.strip()
        if name not in lists.get(provider, []):
            await interaction.followup.send(
                f"❓ `{model}` isn't one of your options — see `/models`.", ephemeral=True
            )
            return
        store.set_model_pick(interaction.user.id, f"{provider}:{name}")
        await interaction.followup.send(
            f"✅ `{provider}:{name}` will answer you from now on. `/model default` to undo.", ephemeral=True
        )

    @model.autocomplete("model")
    async def _model_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        lists = await self._choices(interaction.user) or {}
        options = [DEFAULT] + [f"{p}:{n}" for p, names in lists.items() for n in names]
        current = current.lower()
        return [app_commands.Choice(name=o, value=o) for o in options if current in o.lower()][:25]


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ModelsCog(bot))
