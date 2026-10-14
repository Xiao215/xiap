import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from xiap import config
from xiap.cogs.chat import ChatCog
from xiap.cogs.models import ModelsCog
from xiap.llm import Agent


class ModelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        settings = patch.multiple(
            config,
            COHERE_API_KEYS=[],
            CLI_BRIDGE_URL="http://localhost:8787",
            CLAUDE_MODEL="",
            GEMINI_MODEL="",
            CHAT_PROVIDER="auto",
        )
        settings.start()
        self.addCleanup(settings.stop)
        self.agent = Agent(Mock())
        self.agent._list_claude = AsyncMock(return_value=["sonnet", "opus", "haiku"])
        self.agent._list_gemini = AsyncMock(return_value=["gemini-3.8-flash-high"])
        self.chat = SimpleNamespace(agent=self.agent, is_owner=AsyncMock(return_value=False))
        self.bot = Mock()
        self.bot.get_cog.return_value = self.chat
        self.cog = ModelsCog(self.bot)
        self.interaction = SimpleNamespace(
            user=SimpleNamespace(id=42),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

    async def test_every_user_sees_claude_in_autocomplete(self):
        ordinary = await self.cog._model_autocomplete(self.interaction, "")
        self.chat.is_owner.return_value = True
        owner = await self.cog._model_autocomplete(self.interaction, "")
        self.assertEqual([c.value for c in ordinary], [c.value for c in owner])
        self.assertIn("claude:sonnet", [c.value for c in ordinary])

    async def test_non_owner_can_select_claude(self):
        with patch("xiap.cogs.models.store") as store:
            await self.cog.model.callback(self.cog, self.interaction, "claude:sonnet")
        store.set_model_pick.assert_called_once_with(42, "claude:sonnet")
        self.assertEqual(self.agent.chain(pick="claude:sonnet")[0], ("claude", "sonnet"))

    async def test_bare_claude_name_can_be_selected(self):
        with patch("xiap.cogs.models.store") as store:
            await self.cog.model.callback(self.cog, self.interaction, "opus")
        store.set_model_pick.assert_called_once_with(42, "claude:opus")

    async def test_non_owner_claude_pick_is_used_to_answer(self):
        claude = AsyncMock(return_value=("Hello", "claude-sonnet"))
        gemini = AsyncMock(return_value=("Fallback", "gemini-3.8-flash-high"))
        self.agent._runners.update(claude=claude, gemini=gemini)
        result = await self.agent.run("system", "prompt", [], pick="claude:sonnet")
        self.assertEqual(result, ("Hello", "claude:sonnet", "claude-sonnet"))
        gemini.assert_not_awaited()

    async def test_claude_failure_falls_back_to_gemini(self):
        claude = AsyncMock(side_effect=RuntimeError("unavailable"))
        gemini = AsyncMock(return_value=("Fallback", "gemini-3.8-flash-high"))
        self.agent._runners.update(claude=claude, gemini=gemini)
        result = await self.agent.run("system", "prompt", [])
        self.assertEqual(result[1], "gemini")
        claude.assert_awaited()
        gemini.assert_awaited_once()

    async def test_auto_uses_claude_by_default(self):
        claude = AsyncMock(return_value=("Hello", "claude-sonnet"))
        gemini = AsyncMock()
        self.agent._runners.update(claude=claude, gemini=gemini)
        result = await self.agent.run("system", "prompt", [])
        self.assertEqual(result, ("Hello", "claude", "claude-sonnet"))
        gemini.assert_not_awaited()
        self.assertEqual(self.cog._default_label(), "claude (server default)")

    async def test_gemini_remains_selectable_and_overrides_default(self):
        pick = "gemini:gemini-3.8-flash-high"
        with patch("xiap.cogs.models.store") as store:
            await self.cog.model.callback(self.cog, self.interaction, pick)
        store.set_model_pick.assert_called_once_with(42, pick)
        claude = AsyncMock()
        gemini = AsyncMock(return_value=("Hello", "gemini-3.8-flash-high"))
        self.agent._runners.update(claude=claude, gemini=gemini)
        result = await self.agent.run("system", "prompt", [], pick=pick)
        self.assertEqual(result[1], pick)
        claude.assert_not_awaited()

    async def test_model_default_resets_to_claude(self):
        with patch("xiap.cogs.models.store") as store:
            await self.cog.model.callback(self.cog, self.interaction, "default")
        store.set_model_pick.assert_called_once_with(42, None)
        self.assertIn("claude", self.interaction.followup.send.call_args.args[0])

    async def test_bridge_only_setup_answers_non_owner(self):
        agent = Agent(Mock())
        bot = Mock(user=SimpleNamespace(id=99, display_name="xiap"))
        chat = ChatCog(bot)
        chat.agent = agent
        chat.is_owner = AsyncMock(return_value=False)
        chat._answer = AsyncMock()
        message = SimpleNamespace(
            author=SimpleNamespace(id=42, bot=False, display_name="User"),
            reference=None,
            guild=None,
            raw_mentions=[99],
            clean_content="@xiap hello",
            attachments=[],
            channel=SimpleNamespace(name="test"),
        )
        await chat.on_message(message)
        chat._answer.assert_awaited_once()

    async def test_failed_refresh_preserves_last_known_claude_models(self):
        await self.agent.list_models()
        self.agent._model_lists["claude"] = (0, ["sonnet", "opus", "haiku"])
        self.agent._list_claude.side_effect = RuntimeError("offline")
        result = await self.agent.list_models()
        self.assertEqual(result["claude"], ["sonnet", "opus", "haiku"])
        await self.agent.list_models()
        self.assertEqual(self.agent._list_claude.await_count, 2)
        self.assertLess(self.agent._model_lists["claude"][0] - time.monotonic(), 61)

    async def test_cold_start_offline_still_lists_claude_aliases(self):
        self.agent._list_claude.side_effect = RuntimeError("offline")
        options = await self.cog._model_autocomplete(self.interaction, "claude")
        self.assertTrue({"claude:sonnet", "claude:opus", "claude:haiku"} <= {c.value for c in options})

    async def test_empty_claude_catalogue_does_not_erase_cached_models(self):
        self.agent._model_lists["claude"] = (0, ["sonnet", "opus"])
        self.agent._list_claude.return_value = []
        self.assertEqual((await self.agent.list_models())["claude"], ["sonnet", "opus"])

    async def test_slow_provider_does_not_block_autocomplete(self):
        async def stalled():
            await asyncio.Event().wait()

        self.agent._list_gemini.side_effect = stalled
        with patch("xiap.llm.MODEL_LIST_TIMEOUT", 0.02):
            choices = await asyncio.wait_for(self.cog._model_autocomplete(self.interaction, "claude"), 0.3)
        self.assertIn("claude:sonnet", [c.value for c in choices])

    async def test_search_finds_models_beyond_first_25_choices(self):
        self.agent._list_gemini.return_value = [f"gemini-test-{i}" for i in range(30)]
        self.assertEqual(len(await self.cog._model_autocomplete(self.interaction, "")), 25)
        choices = await self.cog._model_autocomplete(self.interaction, "gemini-test-29")
        self.assertEqual([c.value for c in choices], ["gemini:gemini-test-29"])

    async def test_disabled_bridge_is_not_listed_or_used(self):
        with patch.multiple(config, CLI_BRIDGE_URL="", COHERE_API_KEYS=["test-key"]):
            agent = Agent(Mock())
        agent._list_cohere = AsyncMock(return_value=["command-test"])
        self.assertEqual(list(await agent.list_models()), ["cohere"])
        self.assertEqual([p for p, _ in agent.chain(pick="claude:sonnet")], ["cohere"])

    async def test_bridge_models_split_into_claude_and_gemini(self):
        agent = Agent(MagicMock())
        resp = AsyncMock()
        resp.status = 200
        resp.json.return_value = {"data": [{"id": "sonnet"}, {"id": "gemini-3.8-flash-high"}, {"id": "opus"}]}
        agent.http.get.return_value.__aenter__.return_value = resp
        self.assertEqual(await agent.list_models(), {"claude": ["sonnet", "opus"], "gemini": ["gemini-3.8-flash-high"]})

    async def test_gemini_without_a_model_asks_the_bridge_for_gemini(self):
        self.agent._bridge_request = AsyncMock(return_value={"content": [{"type": "text", "text": "Hi"}], "model": "gemini-3.8-flash-high"})
        result = await self.agent.run("system", "prompt", [], pick="gemini:")
        self.assertEqual(result, ("Hi", "gemini", "gemini-3.8-flash-high"))
        self.assertEqual(self.agent._bridge_request.call_args.args[0]["model"], "gemini")

    def test_shared_primary_can_be_configured_as_claude(self):
        with patch.object(config, "CHAT_PROVIDER", "claude"):
            agent = Agent(Mock())
        self.assertEqual(agent.chain()[0][0], "claude")


if __name__ == "__main__":
    unittest.main()
