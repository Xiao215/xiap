"""AI chat: replies when the bot is mentioned or replied to.

Supports two providers — Gemini (free tier, resets daily) and Cohere — with
round-robin rotation across however many API keys are configured for each,
and automatic failover between providers.
"""

import logging
import re

import aiohttp
import cohere
import discord
from discord.ext import commands

from xiap import config
from xiap.store import store

log = logging.getLogger(__name__)

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

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
- Copy the emoji token EXACTLY as written above, including the angle brackets and the number, e.g. "<:name:123456789>" or "<a:name:123456789>" for animated ones. Never write the ":name:" shorthand — it will not render.

3. **Engagement**:
- Respond directly to mentions or replies.
- Address users by their names to show familiarity.

4. **Response Style**:
- Be concise unless explicitly asked to elaborate.
- Always make your responses conversational and context-aware. Avoid sounding robotic or repetitive.

5. **Chat Context**:
- Use the provided chat history (format: `username: message`) to craft relevant and engaging replies.
- Reference previous conversations when it makes sense.

6. **Your Features (when asked for help)**:
- Besides chatting, you are a utility bot with slash commands. When someone asks what you can do, how to use you, or for help, briefly explain these and tell them `/help` shows the full list in a nice card:
{features}
- People chat with you by @mentioning you or replying to your messages.

### Important Notes:
- Avoid starting messages awkwardly (e.g., avoid "Hi" or "Hello" as standalone replies).
- Always strive to make your responses relevant to the conversation and group dynamics.
"""


class KeyRing:
    """Round-robin over any number of keys/clients, starting each request one
    position later than the last so load spreads evenly."""

    def __init__(self, items: list):
        self.items = items
        self._start = 0

    def rotation(self) -> list:
        """All items, beginning at the current start position, then advance it."""
        ordered = self.items[self._start:] + self.items[: self._start]
        self._start = (self._start + 1) % len(self.items)
        return ordered


class ChatCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.http: aiohttp.ClientSession | None = None

        self.gemini_keys = KeyRing(config.GEMINI_API_KEYS) if config.GEMINI_API_KEYS else None
        self.cohere_clients = (
            KeyRing([cohere.AsyncClientV2(api_key=k) for k in config.COHERE_API_KEYS])
            if config.COHERE_API_KEYS
            else None
        )

        # Ordered provider list: primary first, the other as fallback.
        providers = {"gemini": self._query_gemini, "cohere": self._query_cohere}
        available = {
            name: fn
            for name, fn in providers.items()
            if (name == "gemini" and self.gemini_keys) or (name == "cohere" and self.cohere_clients)
        }
        if config.CHAT_PROVIDER in available:
            order = [config.CHAT_PROVIDER] + [n for n in available if n != config.CHAT_PROVIDER]
        else:  # "auto": prefer Gemini's daily-resetting free tier
            order = list(available)
        self.providers = [(name, available[name]) for name in order]
        log.info("Chat providers (in order): %s", [n for n, _ in self.providers] or "NONE")

    async def cog_load(self) -> None:
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))

    async def cog_unload(self) -> None:
        if self.http:
            await self.http.close()

    async def _query_gemini(self, system: str, user_prompt: str) -> str:
        url = GEMINI_URL.format(model=config.GEMINI_MODEL)
        payload = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        }
        last_error: Exception | None = None
        for key in self.gemini_keys.rotation():
            try:
                async with self.http.post(url, headers={"x-goog-api-key": key}, json=payload) as resp:
                    data = await resp.json()
                    if resp.status != 200:
                        raise RuntimeError(data.get("error", {}).get("message", f"HTTP {resp.status}"))
                    parts = data["candidates"][0]["content"]["parts"]
                    return "".join(p.get("text", "") for p in parts)
            except Exception as e:  # noqa: BLE001 — rotate to the next key on any API failure
                last_error = e
                log.warning("Gemini request failed, rotating key: %s", e)
        raise RuntimeError(f"all Gemini keys failed: {last_error}")

    async def _query_cohere(self, system: str, user_prompt: str) -> str:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_prompt},
        ]
        last_error: Exception | None = None
        for client in self.cohere_clients.rotation():
            try:
                res = await client.chat(model=config.COHERE_MODEL, messages=messages)
                if res.message and res.message.content:
                    return res.message.content[0].text
                raise RuntimeError("empty response")
            except Exception as e:  # noqa: BLE001 — rotate to the next key on any API failure
                last_error = e
                log.warning("Cohere request failed, rotating key: %s", e)
        raise RuntimeError(f"all Cohere keys failed: {last_error}")

    async def _query(self, system: str, user_prompt: str) -> str:
        for name, provider in self.providers:
            try:
                return await provider(system, user_prompt)
            except Exception as e:  # noqa: BLE001 — fall through to the next provider
                log.error("Provider %s failed: %s", name, e)
        return "My brain is fried right now, try again in a bit 😵"

    # Matches a full emoji token (kept/canonicalized) or bare :name: shorthand (fixed up).
    EMOJI_RE = re.compile(r"<a?:(\w+):\d+>|:(\w+):")

    @classmethod
    def _fix_emojis(cls, text: str, guild: discord.Guild | None) -> str:
        """Repair model emoji mistakes: ':name:' shorthand and wrong ids/animated prefixes."""
        if guild is None:
            return text
        by_name = {e.name: e for e in guild.emojis}

        def repl(m: re.Match) -> str:
            emoji = by_name.get(m.group(1) or m.group(2))
            return str(emoji) if emoji else m.group(0)

        return cls.EMOJI_RE.sub(repl, text)

    async def _build_context(self, channel: discord.abc.Messageable, guild: discord.Guild | None) -> str:
        # str(emoji) yields the exact sendable token: <:name:id> or <a:name:id> for animated.
        emojis = "\n".join(str(e) for e in (guild.emojis if guild else ()))
        commands_list = self.bot.tree.get_commands(guild=guild) or self.bot.tree.get_commands()
        features = "\n".join(
            f"/{c.name} — {c.description}"
            for c in sorted(commands_list, key=lambda c: c.name)
        )
        prompt = SYSTEM_PROMPT.format(
            name=self.bot.user.name,
            emojis=emojis or "(no custom emojis)",
            features=features,
        )

        if not store.context_enabled(channel.id):
            return (
                prompt
                + "\n\n(Reading channel history is turned off in this channel via /context, "
                + "so you only see the message that pinged you. Don't guess at prior conversation.)"
            )

        history: list[str] = []
        async for msg in channel.history(limit=config.CHAT_HISTORY_LIMIT):
            if not msg.content.strip():
                continue
            if msg.author.bot and msg.author != self.bot.user:
                continue
            if store.is_opted_out(msg.author.id):
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

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or self.bot.user is None or not self.providers:
            return

        mention = f"<@{self.bot.user.id}>"
        content = message.content.replace(mention, "").strip()

        replied = message.reference.resolved if message.reference else None
        is_reply_to_me = isinstance(replied, discord.Message) and replied.author == self.bot.user
        is_mention = mention in message.content

        if not (is_reply_to_me or is_mention):
            return

        log.info(
            "Chat trigger from %s in #%s (%s)",
            message.author.display_name,
            getattr(message.channel, "name", "?"),
            "reply" if is_reply_to_me else "mention",
        )

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
        response = self._fix_emojis(response, message.guild)
        await message.reply(response[:2000], mention_author=False)


async def setup(bot: commands.Bot) -> None:
    if not (config.GEMINI_API_KEYS or config.COHERE_API_KEYS):
        log.warning("No Gemini or Cohere API keys configured — AI chat disabled")
    await bot.add_cog(ChatCog(bot))
