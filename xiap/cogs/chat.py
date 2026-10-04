"""AI chat: replies when the bot is mentioned or replied to.

Instead of dumping the whole channel history into every request, the model
gets a small window of recent messages and tools to pull more when it needs
it: older channel history, or a specific message (by id or link) along with
the reply chain above it. Images on the pinging message (or the one it
replies to) are shown to the model too. Everything it can read still obeys
/context and /optout. Provider handling (Gemini/Cohere for everyone, Claude
for the owner only, key rotation, failover) lives in xiap.llm.
"""

import logging
import re
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord.ext import commands

from xiap import config
from xiap.llm import Agent, Image, Tool
from xiap.store import store

log = logging.getLogger(__name__)

TZ = ZoneInfo(config.TIMEZONE)
MESSAGE_LINK_RE = re.compile(r"discord(?:app)?\.com/channels/(\d+|@me)/(\d+)/(\d+)")
MAX_REPLY_CHAIN = 5

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
- You can use Discord markdown to format responses when it helps readability: **bold**, *italics*, `inline code`, code blocks with language tags, > quotes, bullet lists, and ## headers. Casual chat should stay plain; use formatting for explanations, lists, or code.
- When someone asks about websites, docs, tools, papers, or anything with an online reference, include the actual URL so they can click it. Wrap bare URLs in angle brackets so Discord doesn't show a big embed banner (e.g. <https://example.com> or a [masked link](https://example.com)). Only give URLs you are confident actually exist — never invent links.

5. **Chat Context & Tools**:
- You're shown the message that pinged you, the message it replies to (if any), and a few of the most recent messages in the channel (format: `[time] name: message`).
- That is usually enough — most replies need no tools. But when the message refers to something you can't see (an earlier discussion, "what did X say", "summarize the chat", a message link, a reply chain that goes further back), use your tools to look it up instead of guessing.
- `read_channel_history` returns more of the channel's recent messages. `get_message` fetches one message by id or link plus the messages it replies to.
- If a tool says something is off-limits (history turned off, someone opted out), respect that and don't speculate about the hidden content.
- Reference previous conversations when it makes sense.

6. **Your Features (when asked for help)**:
- Besides chatting, you are a utility bot with slash commands. When someone asks what you can do, how to use you, or for help, briefly explain these and tell them `/help` shows the full list in a nice card:
{features}
- People chat with you by @mentioning you or replying to your messages.

### Important Notes:
- Avoid starting messages awkwardly (e.g., avoid "Hi" or "Hello" as standalone replies).
- Always strive to make your responses relevant to the conversation and group dynamics.
"""


class ChatContext:
    """What the agent may read while answering one ping, and the tools to read it.

    The channel's /context limit caps how far back history goes; opted-out
    users' messages are always hidden.
    """

    def __init__(self, bot: commands.Bot, message: discord.Message) -> None:
        self.bot = bot
        self.message = message
        self.channel = message.channel
        self.limit = store.get_context_limit(self.channel.id, config.CHAT_HISTORY_LIMIT)
        self._history: list[discord.Message] = []  # newest first, unfiltered
        self._fetched = 0  # how many messages back we've asked Discord for

    def readable(self, msg: discord.Message, *, other_bots: bool = False) -> bool:
        if msg.author == self.bot.user:
            return True
        if msg.author.bot:
            return other_bots
        return not store.is_opted_out(msg.author.id)

    @staticmethod
    def format(msg: discord.Message) -> str:
        when = msg.created_at.astimezone(TZ).strftime("%b %d %H:%M")
        line = f"[{when}] {msg.author.display_name}"
        ref = msg.reference
        if ref and ref.message_id:
            target = ref.resolved
            who = target.author.display_name if isinstance(target, discord.Message) else "a message"
            line += f" (replying to {who}, message id {ref.message_id})"
        line += f": {msg.clean_content.strip()}"
        if msg.attachments:
            line += f" [attached: {', '.join(a.filename for a in msg.attachments)}]"
        if titles := [e.title for e in msg.embeds if e.title]:
            line += f" [embeds: {'; '.join(titles)}]"
        return line

    async def recent(self, count: int) -> list[discord.Message]:
        """Up to `count` readable messages before the ping (oldest first), capped by /context."""
        count = min(count, self.limit)
        if count > self._fetched:
            self._history = [m async for m in self.channel.history(limit=count, before=self.message)]
            self._fetched = count
        msgs = [
            m for m in self._history[:count]
            if self.readable(m) and (m.content.strip() or m.attachments)
        ]
        return msgs[::-1]

    # --- Tools ------------------------------------------------------------------

    async def read_channel_history(self, count: int = 20) -> str:
        if self.limit <= 0:
            return "Channel history is turned off here via /context, so you can't read it."
        count = max(1, min(int(count), self.limit))
        msgs = await self.recent(count)
        lines = [self.format(m) for m in msgs] or ["(no readable messages)"]
        return (
            f"The {count} most recent messages before the ping (oldest first; "
            f"/context lets you read at most {self.limit} here):\n" + "\n".join(lines)
        )

    async def get_message(self, message: str) -> str:
        if link := MESSAGE_LINK_RE.search(message):
            guild_id, channel_id, message_id = link.groups()
            here = str(self.message.guild.id) if self.message.guild else "@me"
            if guild_id != here:
                return "That message is in a different server or DM, so you can't read it."
            channel_id, message_id = int(channel_id), int(message_id)
        elif message.strip().isdigit():
            channel_id, message_id = self.channel.id, int(message.strip())
        else:
            return "Pass a message id or a Discord message link."

        if channel_id == self.channel.id:
            channel = self.channel
        elif self.message.guild:
            channel = self.message.guild.get_channel_or_thread(channel_id)
            if channel is None:
                return "I can't find or access that channel."
            # Don't leak channels the person asking can't see themselves.
            if not channel.permissions_for(self.message.author).read_message_history:
                return "The person asking can't see that channel, so you can't read it for them."
        else:
            return "I can't find or access that channel."
        if store.get_context_limit(channel.id, config.CHAT_HISTORY_LIMIT) <= 0:
            return "AI context is turned off in that channel via /context, so you can't read it."

        msg = await channel.fetch_message(message_id)
        chain: list[str] = []
        for _ in range(MAX_REPLY_CHAIN):
            if self.readable(msg, other_bots=True):
                chain.append(self.format(msg))
            else:
                chain.append(f"(hidden: {msg.author.display_name} opted out of AI context)")
            ref = msg.reference
            if not (ref and ref.message_id and ref.channel_id == channel.id):
                break
            parent = ref.resolved if isinstance(ref.resolved, discord.Message) else None
            msg = parent or await channel.fetch_message(ref.message_id)
        where = f" in #{channel.name}" if getattr(channel, "name", None) else ""
        header = f"The message{where}" + (", preceded by the messages it replies to" if len(chain) > 1 else "")
        return f"{header} (oldest first):\n" + "\n".join(reversed(chain))

    def tools(self) -> list[Tool]:
        tools = [
            Tool(
                name="get_message",
                description=(
                    "Fetch one Discord message by its id (in this channel) or by a message link, "
                    "together with up to 5 messages above it in its reply chain."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "message": {"type": "string", "description": "A message id or a discord.com/channels/... link"},
                    },
                    "required": ["message"],
                },
                run=self.get_message,
            ),
        ]
        if self.limit > config.CHAT_BASE_CONTEXT:
            tools.append(Tool(
                name="read_channel_history",
                description=(
                    "Read the most recent messages in this channel, further back than the few you "
                    f"were shown. Returns up to `count` messages (max {self.limit})."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "count": {"type": "integer", "description": f"How many messages back to read, 1-{self.limit}"},
                    },
                    "required": ["count"],
                },
                run=self.read_channel_history,
            ))
        return tools


class ChatCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.http: aiohttp.ClientSession | None = None
        self.agent: Agent | None = None

    async def cog_load(self) -> None:
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
        self.agent = Agent(self.http)

    async def cog_unload(self) -> None:
        if self.http:
            await self.http.close()

    async def _is_owner(self, user: discord.abc.User) -> bool:
        """OWNER_ID if set, else whoever owns the bot in the Developer Portal."""
        if config.OWNER_ID:
            return user.id == config.OWNER_ID
        return await self.bot.is_owner(user)

    @staticmethod
    async def _images(*messages: discord.Message | None) -> list[Image]:
        """Image attachments from the given messages, within the count/size limits."""
        images: list[Image] = []
        for msg in messages:
            for a in msg.attachments if msg else ():
                mime = (a.content_type or "").split(";")[0]
                if not mime.startswith("image/") or a.size > config.CHAT_IMAGE_MAX_MB * 1024 * 1024:
                    continue
                if len(images) >= config.CHAT_MAX_IMAGES:
                    return images
                try:
                    images.append(Image(mime, await a.read()))
                except discord.HTTPException as e:
                    log.warning("Couldn't download attachment %s: %s", a.filename, e)
        return images

    @staticmethod
    def _split_message(text: str, limit: int = 2000) -> list[str]:
        """Split into Discord-sized chunks, preferring paragraph, then line, then word breaks."""
        chunks: list[str] = []
        text = text.strip()
        while len(text) > limit:
            window = text[:limit]
            cut = max(window.rfind("\n\n"), window.rfind("\n"))
            if cut < limit // 2:
                cut = window.rfind(" ")
            if cut <= 0:
                cut = limit
            chunks.append(text[:cut].rstrip())
            text = text[cut:].lstrip()
        return chunks + [text] if text else chunks or ["…"]

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

    def _system_prompt(self, guild: discord.Guild | None) -> str:
        # str(emoji) yields the exact sendable token: <:name:id> or <a:name:id> for animated.
        emojis = "\n".join(str(e) for e in (guild.emojis if guild else ()))
        commands_list = self.bot.tree.get_commands(guild=guild) or self.bot.tree.get_commands()
        features = "\n".join(
            f"/{c.name} — {c.description}"
            for c in sorted(commands_list, key=lambda c: c.name)
        )
        return SYSTEM_PROMPT.format(
            name=self.bot.user.name,
            emojis=emojis or "(no custom emojis)",
            features=features,
        )

    async def _user_prompt(self, ctx: ChatContext, replied: discord.Message | None, content: str) -> str:
        sections: list[str] = []
        if ctx.limit <= 0:
            sections.append(
                "(Reading channel history is turned off in this channel via /context, so you only "
                "see the message that pinged you. Don't guess at prior conversation.)"
            )
        else:
            recent = await ctx.recent(config.CHAT_BASE_CONTEXT)
            if recent:
                more = " — use read_channel_history for more" if ctx.limit > config.CHAT_BASE_CONTEXT else ""
                sections.append(
                    f"Most recent messages in this channel (oldest first{more}):\n"
                    + "\n".join(ctx.format(m) for m in recent)
                )

        author = ctx.message.author.display_name
        if replied is not None:
            sections.append(f"{author} is replying to this message:\n{ctx.format(replied)}")
        sections.append(f"{author} says to you: {content}")
        return "\n\n".join(sections)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or self.bot.user is None or self.agent is None:
            return

        mention = f"<@{self.bot.user.id}>"
        replied = message.reference.resolved if message.reference else None
        if not isinstance(replied, discord.Message):
            replied = None
        is_reply_to_me = replied is not None and replied.author == self.bot.user
        is_mention = mention in message.content or f"<@!{self.bot.user.id}>" in message.content

        if not (is_reply_to_me or is_mention):
            return
        owner = bool(self.agent.claude) and await self._is_owner(message.author)
        if not (self.agent.providers or owner):
            return

        log.info(
            "Chat trigger from %s in #%s (%s)",
            message.author.display_name,
            getattr(message.channel, "name", "?"),
            "reply" if is_reply_to_me else "mention",
        )

        # clean_content turns <@id> mentions into readable @names; drop the ping to ourselves.
        me = message.guild.me if message.guild else self.bot.user
        content = message.clean_content.replace(f"@{me.display_name}", "").strip()
        if not content and not message.attachments:
            await message.channel.send("What do you mean?")
            return
        if message.attachments:
            content += f" [attached: {', '.join(a.filename for a in message.attachments)}]"

        ctx = ChatContext(self.bot, message)
        # The replied-to message's images count too ("@xiap what's this?" on a photo),
        # as long as its text would be readable.
        replied_ok = replied is not None and (
            replied.author == self.bot.user or (ctx.limit > 0 and ctx.readable(replied, other_bots=True))
        )
        async with message.channel.typing():
            prompt = await self._user_prompt(ctx, replied if replied_ok else None, content)
            images = await self._images(message, replied if replied_ok else None)
            response = await self.agent.run(
                self._system_prompt(message.guild), prompt, ctx.tools(), images, owner=owner
            )
        response = response or "My brain is fried right now, try again in a bit 😵"
        chunks = self._split_message(self._fix_emojis(response, message.guild))
        await message.reply(chunks[0], mention_author=False)
        for chunk in chunks[1:]:
            await message.channel.send(chunk)


async def setup(bot: commands.Bot) -> None:
    if not (config.GEMINI_API_KEYS or config.COHERE_API_KEYS):
        log.warning("No Gemini or Cohere API keys configured — AI chat disabled for non-owners")
    await bot.add_cog(ChatCog(bot))
