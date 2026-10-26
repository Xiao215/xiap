import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from xiap.cogs import music
from xiap.cogs.music import LinkError, Player, ShareRef, Song, parse_link, parse_start, songs_from

ID, KEY = "bc4be89979795352", "49244069fcb5646a"


def entry(n: int, *, gone: bool = False, audio: bool = True, duration: int = 200) -> dict:
    return {
        "id": f"e{n}",
        "gone": gone,
        "song": {
            "title": f"Song {n}",
            "artist": "五月天",
            "duration": duration,
            "audio": {"key": f"audio/{n}.m4a", "size": 1, "mime": "audio/mp4"} if audio else None,
        },
    }


def song(n: int) -> Song:
    return Song(title=f"Song {n}", artist="", duration=0, audio_url=f"https://x/{n}", sender="Xiao")


class ParseTests(unittest.TestCase):
    def test_reads_github_and_doorman_links(self):
        for host in ("https://xiao215.github.io/selfmp3", "https://selfmp3-doorman.example.workers.dev"):
            self.assertEqual(parse_link(f"{host}/l/{ID}/{KEY}"), ShareRef(ID, KEY, None))

    def test_reads_a_start_and_text_around_the_link(self):
        ref = parse_link(f"listen to this <https://xiao215.github.io/selfmp3/l/{ID}/{KEY}?t=1:02:03> ok")
        self.assertEqual(ref, ShareRef(ID, KEY, 3723))

    def test_rejects_other_links(self):
        self.assertIsNone(parse_link("https://youtube.com/watch?v=abc"))
        self.assertIsNone(parse_link(f"https://xiao215.github.io/selfmp3/l/{ID}"))  # cut short
        self.assertIsNone(parse_link(f"https://xiao215.github.io/selfmp3/l/{ID}/{KEY}/extra"))

    def test_start_formats(self):
        self.assertEqual(parse_start("42"), 42)
        self.assertEqual(parse_start("0:42"), 42)
        self.assertEqual(parse_start("1:02:03"), 3723)
        for bad in (None, "", "0:7", "0:60", "abc", "1:2:3:4"):
            self.assertIsNone(parse_start(bad), bad)


class SongsFromTests(unittest.TestCase):
    def test_skips_gone_and_unstored_songs(self):
        link = {"kind": "list", "from": {"name": "Xiao"}, "entries": [entry(1), entry(2, gone=True), entry(3, audio=False)]}
        songs = songs_from(link, ShareRef(ID, KEY, 30))
        self.assertEqual([s.title for s in songs], ["Song 1"])
        self.assertEqual(songs[0].sender, "Xiao")
        self.assertIsNone(songs[0].start)  # a start only belongs to a one-song link
        self.assertTrue(songs[0].audio_url.endswith(f"/v1/share/{ID}/files/e1/audio?k={KEY}"))

    def test_start_applies_to_a_song_unless_past_its_end(self):
        link = {"kind": "song", "from": {"name": "Xiao"}, "entries": [entry(1, duration=200)]}
        self.assertEqual(songs_from(link, ShareRef(ID, KEY, 42))[0].start, 42)
        self.assertIsNone(songs_from(link, ShareRef(ID, KEY, 500))[0].start)


class FakeVoice:
    """Stands in for discord.VoiceClient: plays until stop() or finish()."""

    def __init__(self):
        self.guild = MagicMock(id=1)
        self.played: list[str] = []
        self.disconnect = AsyncMock()
        self._after = None

    def play(self, source, after):
        self.played.append(source)
        self._after = after

    def stop(self):
        self.finish()

    def finish(self):
        if after := self._after:
            self._after = None
            after(None)


class PlayerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cog = MagicMock(players={})
        self.cog.download = AsyncMock(side_effect=self._download)
        self.voice = FakeVoice()
        self.channel = MagicMock(send=AsyncMock())
        patcher = patch.object(music.discord, "FFmpegOpusAudio", side_effect=lambda path, **kw: path)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def _download(self, s: Song) -> str:
        fd, path = tempfile.mkstemp()
        os.close(fd)
        return path

    def new_player(self) -> Player:
        player = Player(self.cog, self.voice, self.channel)
        self.cog.players[1] = player
        return player

    async def settle(self):
        for _ in range(5):
            await asyncio.sleep(0)

    async def test_plays_in_order_and_skips(self):
        player = self.new_player()
        player.add([song(1), song(2)])
        await self.settle()
        self.assertEqual(player.current.title, "Song 1")
        player.skip()
        await self.settle()
        self.assertEqual(player.current.title, "Song 2")
        self.assertEqual(len(self.voice.played), 2)
        self.assertFalse(os.path.exists(self.voice.played[0]))  # the temp file goes once played
        await player.leave()

    async def test_stop_clears_the_queue_and_leave_disconnects(self):
        player = self.new_player()
        player.add([song(1), song(2), song(3)])
        await self.settle()
        player.stop()
        await self.settle()
        self.assertFalse(player.busy)
        await player.leave()
        self.voice.disconnect.assert_awaited()
        self.assertNotIn(1, self.cog.players)

    async def test_a_song_that_wont_download_is_skipped(self):
        async def download(s: Song) -> str:
            if s.title == "Song 1":
                raise LinkError("the link has ended.")
            return await self._download(s)

        self.cog.download = AsyncMock(side_effect=download)
        player = self.new_player()
        player.add([song(1), song(2)])
        await self.settle()
        self.assertEqual(player.current.title, "Song 2")
        self.assertIn("the link has ended", self.channel.send.await_args_list[0].args[0])
        await player.leave()

    async def test_leaves_when_idle(self):
        with patch.object(music, "IDLE_SECONDS", 0.01):
            player = self.new_player()
            await asyncio.wait_for(player.task, 1)
        self.voice.disconnect.assert_awaited()
        self.assertNotIn(1, self.cog.players)


if __name__ == "__main__":
    unittest.main()
