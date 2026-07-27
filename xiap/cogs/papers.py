"""/papers — trending AI papers from Hugging Face's daily papers list.

`/papers today` posts the current digest on demand; `/papers subscribe` signs a
channel up for an automatic digest every morning (optionally filtered by topic).
"""

import datetime
import logging
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from xiap import config
from xiap.store import store

log = logging.getLogger(__name__)

API_URL = "https://huggingface.co/api/daily_papers?limit=50"
DIGEST_SIZE = 5
POST_TIME = datetime.time(hour=config.PAPERS_HOUR, tzinfo=ZoneInfo(config.TIMEZONE))


class PapersCog(commands.Cog):
    papers = app_commands.Group(name="papers", description="Trending AI papers, daily")

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.http: aiohttp.ClientSession | None = None

    async def cog_load(self) -> None:
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        self.daily_digest.start()

    async def cog_unload(self) -> None:
        self.daily_digest.cancel()
        if self.http:
            await self.http.close()

    async def _fetch(self) -> list[dict]:
        async with self.http.get(API_URL) as resp:
            data = await resp.json()
        papers = [entry["paper"] for entry in data if "paper" in entry]
        return sorted(papers, key=lambda p: p.get("upvotes", 0), reverse=True)

    @staticmethod
    def _matches(paper: dict, topic: str) -> bool:
        if not topic:
            return True
        haystack = (paper.get("title", "") + " " + paper.get("summary", "")).lower()
        return all(word in haystack for word in topic.lower().split())

    def _build_embed(self, papers: list[dict], topic: str) -> discord.Embed | None:
        selected = [p for p in papers if self._matches(p, topic)][:DIGEST_SIZE]
        if not selected:
            return None
        title = "📚 Today's trending papers"
        if topic:
            title += f" · {topic}"
        embed = discord.Embed(
            title=title,
            color=discord.Color.orange(),
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        for p in selected:
            authors = ", ".join(a.get("name", "?") for a in p.get("authors", [])[:3])
            if len(p.get("authors", [])) > 3:
                authors += " et al."
            summary = (p.get("summary") or "").replace("\n", " ")
            if len(summary) > 400:
                summary = summary[:400].rsplit(" ", 1)[0] + "…"
            embed.add_field(
                name=f"▲ {p.get('upvotes', 0)} · {p.get('title', 'Untitled')[:230]}",
                value=(
                    f"{summary}\n"
                    f"[arXiv](https://arxiv.org/abs/{p['id']}) · "
                    f"[HF discussion](https://huggingface.co/papers/{p['id']}) · {authors}"
                )[:1024],
                inline=False,
            )
        embed.set_footer(text="Source: huggingface.co/papers · ▲ = community upvotes")
        return embed

    @papers.command(name="today", description="Show today's trending AI papers right now")
    @app_commands.describe(topic="Optional topic filter, e.g. 'diffusion', 'LLM agents'")
    async def today(self, interaction: discord.Interaction, topic: str = "") -> None:
        await interaction.response.defer()
        try:
            papers = await self._fetch()
        except Exception:
            log.exception("Papers fetch failed")
            papers = []
        embed = self._build_embed(papers, topic) if papers else None
        if embed is None:
            await interaction.followup.send(
                f"No trending papers found{f' for `{topic}`' if topic else ''} today 😕"
            )
            return
        await interaction.followup.send(embed=embed)

    @papers.command(name="subscribe", description="Post the trending-papers digest here every morning")
    @app_commands.describe(topic="Optional topic filter, e.g. 'diffusion', 'LLM agents'")
    async def subscribe(self, interaction: discord.Interaction, topic: str = "") -> None:
        store.set_paper_sub(interaction.channel.id, topic)
        scope = f" filtered by **{topic}**" if topic else ""
        await interaction.response.send_message(
            f"📬 Subscribed! I'll post the top {DIGEST_SIZE} trending papers{scope} here "
            f"every morning around {POST_TIME.hour}:00 ({config.TIMEZONE})."
        )

    @papers.command(name="unsubscribe", description="Stop the daily papers digest in this channel")
    async def unsubscribe(self, interaction: discord.Interaction) -> None:
        if store.remove_paper_sub(interaction.channel.id):
            await interaction.response.send_message("📭 Unsubscribed — no more daily papers here.")
        else:
            await interaction.response.send_message(
                "This channel wasn't subscribed. Use `/papers subscribe` to start.", ephemeral=True
            )

    @tasks.loop(time=POST_TIME)
    async def daily_digest(self) -> None:
        if not store.paper_subs:
            return
        try:
            papers = await self._fetch()
        except Exception:
            log.exception("Daily papers fetch failed, skipping today")
            return
        for channel_id, topic in list(store.paper_subs.items()):
            channel = self.bot.get_channel(channel_id)
            if channel is None:  # kicked from server or channel deleted
                log.info("Dropping papers subscription for missing channel %d", channel_id)
                store.remove_paper_sub(channel_id)
                continue
            embed = self._build_embed(papers, topic)
            try:
                if embed is not None:
                    await channel.send(embed=embed)
            except discord.HTTPException:
                log.exception("Failed to post papers digest in %d", channel_id)

    @daily_digest.before_loop
    async def before_daily_digest(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PapersCog(bot))
