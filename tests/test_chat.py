import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from xiap.cogs.chat import ChatCog, ChatContext

NOW = datetime(2026, 11, 9, 18, 55, tzinfo=timezone.utc)
BOT = SimpleNamespace(id=1, display_name="xiap", bot=True)
WILL = SimpleNamespace(id=2, display_name="Will", bot=False)
AMY = SimpleNamespace(id=3, display_name="Amy", bot=False)


def message(author, content, minutes_ago=0, id=None) -> SimpleNamespace:
    return SimpleNamespace(
        id=id or hash((author.id, content)), author=author, content=content, clean_content=content,
        created_at=NOW - timedelta(minutes=minutes_ago), reference=None, attachments=[], embeds=[],
    )


class History:
    """channel.history(limit=…, before=…): newest first, like Discord."""

    def __init__(self, msgs):
        self.msgs = msgs[::-1]

    def __call__(self, limit, before):
        async def gen():
            for m in self.msgs[:limit]:
                yield m
        return gen()


def context(history, limit=30) -> ChatContext:
    ping = message(WILL, "@xiap", id=99)
    ping.channel = SimpleNamespace(id=5, history=History(history))
    with patch("xiap.cogs.chat.store.get_context_limit", return_value=limit):
        return ChatContext(SimpleNamespace(user=BOT), ping)


class LeadInTests(unittest.IsolatedAsyncioTestCase):
    async def test_takes_the_pingers_own_messages_right_before(self):
        ctx = context([
            message(AMY, "the companies are VITURE, Looki…", minutes_ago=4),
            message(WILL, "你可以概括一下", minutes_ago=1),
            message(WILL, "他们都是什么级别的公司", minutes_ago=0),
        ])
        lead_in = await ctx.lead_in()
        self.assertEqual([m.content for m in lead_in], ["你可以概括一下", "他们都是什么级别的公司"])
        prompt = await ctx.prompt(None, "", lead_in=lead_in)
        self.assertIn("Will pinged you with just an @mention right after sending this", prompt)
        self.assertTrue(prompt.endswith("Will: 他们都是什么级别的公司"))

    async def test_stops_at_someone_else_or_an_old_message(self):
        ctx = context([
            message(WILL, "an old thought", minutes_ago=20),
            message(BOT, "my last answer", minutes_ago=1),
        ])
        self.assertEqual(await ctx.lead_in(), [])
        ctx = context([message(WILL, "an old thought", minutes_ago=20)])
        self.assertEqual(await ctx.lead_in(), [])

    async def test_nothing_when_history_is_off(self):
        ctx = context([message(WILL, "question", minutes_ago=0)], limit=0)
        self.assertEqual(await ctx.lead_in(), [])

    async def test_bare_ping_without_lead_in_asks_the_model_to_infer(self):
        ctx = context([message(AMY, "anyone up for food", minutes_ago=1)])
        prompt = await ctx.prompt(None, "")
        self.assertIn("Will pinged you with just an @mention and nothing else", prompt)

    async def test_bare_ping_on_a_reply_points_at_that_message(self):
        ctx = context([])
        prompt = await ctx.prompt(message(AMY, "is this legit?"), "")
        self.assertIn("Will is replying to this message:\n[", prompt)
        self.assertIn("pinged you with just an @mention on the message they're replying to", prompt)


class BarePingTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_asks_what_they_mean_when_there_is_nothing_to_go_on(self):
        cog = ChatCog(Mock())
        cog.agent = Mock(run=AsyncMock())
        ping = message(WILL, "@xiap", id=99)
        ping.channel = SimpleNamespace(id=5, history=History([]), send=AsyncMock())
        ping.guild = None
        with patch("xiap.cogs.chat.store.get_context_limit", return_value=0):
            await cog._answer(ping, None, "")
        ping.channel.send.assert_awaited_once_with("What do you mean?")
        cog.agent.run.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
