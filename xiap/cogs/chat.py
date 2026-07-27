"""AI chat: replies when the bot is mentioned or replied to, powered by Cohere."""

import logging
import random

import cohere
import discord
from discord.ext import commands

from xiap import config

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a friendly, witty, and conversational member of the Sunday Social Discord group chat. \
Your name is {name}, and you actively engage with others like a close friend.

### Guidelines for Behavior:
1. **Tone**:
- Be warm, friendly, and natural.
- Use wit and humor sparingly to add personality but ensure it fits the context.

2. **Custom Emoji Usage (VERY IMPORTANT)**:
- Always include emojis from the list below when they fit the context of your response:
{emojis}
- You **must** integrate at least one emoji in every response unless it feels completely inappropriate.
- Note that the emoji is in the format "<:name:emoji_id>" and you should include the entire thing, brackets included, in your response.

3. **Engagement**:
- Respond directly to mentions or replies.
- Address users by their names to show familiarity.

4. **Response Style**:
- Be concise unless explicitly asked to elaborate.
- Always make your responses conversational and context-aware. Avoid sounding robotic or repetitive.

5. **Chat Context**:
- Use the provided chat history (format: `username: message`) to craft relevant and engaging replies.
- Reference previous conversations when it makes sense.

### Important Notes:
- Avoid starting messages awkwardly (e.g., avoid "Hi" or "Hello" as standalone replies).
- Always strive to make your responses relevant to the conversation and group dynamics.
"""


class ChatCog(commands.Cog):
    """Responds to mentions/replies using Cohere, with recent channel history as context."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.clients = [cohere.AsyncClientV2(api_key=key) for key in config.COHERE_API_KEYS]

    async def _build_context(self, channel: discord.abc.Messageable, guild: discord.Guild | None) -> str:
        emojis = "\n".join(f"name: {e.name} emoji_id: {e.id}" for e in (guild.emojis if guild else ()))
        prompt = SYSTEM_PROMPT.format(name=self.bot.user.name, emojis=emojis or "(no custom emojis)")

        history: list[str] = []
        async for msg in channel.history(limit=config.CHAT_HISTORY_LIMIT):
            if not msg.content.strip():
                continue
            if msg.author.bot and msg.author != self.bot.user:
                continue
            content = msg.content.replace(f"<@{self.bot.user.id}>", f"@{self.bot.user.name}")
            history.append(f"{msg.author.display_name}: {content}")
        history.reverse()

        return (
            prompt
            + f"\n\nHere are the last {len(history)} messages from this channel, "
            + "spoken by members in this discord group:\n"
            + "\n".join(history)
        )

    async def _query(self, system: str, user_prompt: str) -> str:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_prompt},
        ]
        # Try each API key in random order so a rate-limited key doesn't take the bot down.
        clients = random.sample(self.clients, k=len(self.clients))
        last_error: Exception | None = None
        for client in clients:
            try:
                res = await client.chat(model=config.COHERE_MODEL, messages=messages)
                if res.message and res.message.content:
                    return res.message.content[0].text
                return "I'm sorry, I couldn't generate a response."
            except Exception as e:  # noqa: BLE001 — rotate to the next key on any API failure
                last_error = e
                log.warning("Cohere request failed, rotating key: %s", e)
        log.error("All Cohere keys failed: %s", last_error)
        return "My brain is fried right now, try again in a bit 😵"

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or self.bot.user is None:
            return
        if not self.clients:
            return

        mention = f"<@{self.bot.user.id}>"
        content = message.content.replace(mention, "").strip()

        replied = message.reference.resolved if message.reference else None
        is_reply_to_me = isinstance(replied, discord.Message) and replied.author == self.bot.user
        is_mention = mention in message.content

        if not (is_reply_to_me or is_mention):
            return

        if not content:
            await message.channel.send("What do you mean?")
            return

        if is_reply_to_me:
            user_prompt = (
                f"{message.author.display_name} replies to '{replied.content}' "
                f"from {self.bot.user.name} with '{content}'"
            )
        else:
            user_prompt = f"{message.author.display_name}: {content}"

        system = await self._build_context(message.channel, message.guild)
        async with message.channel.typing():
            response = await self._query(system, user_prompt)
        await message.reply(response[:2000], mention_author=False)


async def setup(bot: commands.Bot) -> None:
    if not config.COHERE_API_KEYS:
        log.warning("No Cohere API keys configured — AI chat disabled")
    await bot.add_cog(ChatCog(bot))
