import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from xiap.cogs import music
from xiap.cogs.music import (
    PLAYED, YOUTUBE_RED, LinkError, Player, ShareRef, Song, YouTubeRef, cover_color, oklch_to_rgb, panel_embed,
    parse_link, parse_start, parse_youtube, parse_youtube_start, progress_bar, songs_from, youtube_songs_from,
)

ID, KEY = "bc4be89979795352", "49244069fcb5646a"


def entry(n: int, *, gone: bool = False, audio: bool = True, duration: int = 200) -> dict:
    return {
        "id": f"e{n}",
        "gone": gone,
        "song": {
            "title": f"Song {n}",
            "artist": "五月天",
            "duration": duration,
            "album": "",
            "audio": {"key": f"audio/{n}.m4a", "size": 1, "mime": "audio/mp4"} if audio else None,
            "cover": {"key": f"covers/{n}.jpg", "size": 1} if n == 1 else None,
            "coverTone": {"hue": 0, "chroma": 0, "palette": [{"l": 1, "c": 0, "h": 0, "share": 1}]},
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

    def test_reads_the_song_a_list_opens_on(self):
        ref = parse_link(f"https://xiao215.github.io/selfmp3/l/{ID}/{KEY}?s=a1b2c3d4e5f6&t=0:42")
        self.assertEqual(ref, ShareRef(ID, KEY, 42, "a1b2c3d4e5f6"))
        for wrong in ("", "nope", "a1b2c3d4e5f60"):
            self.assertIsNone(parse_link(f"https://xiao215.github.io/selfmp3/l/{ID}/{KEY}?s={wrong}").song)

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
        self.assertTrue(songs[0].cover_url.endswith(f"/v1/share/{ID}/files/e1/cover?k={KEY}"))
        self.assertTrue(songs[0].page_url.endswith(f"/l/{ID}/{KEY}"))
        self.assertEqual(songs[0].color, 0xFFFFFF)

    def test_start_applies_to_a_song_unless_past_its_end(self):
        link = {"kind": "song", "from": {"name": "Xiao"}, "entries": [entry(1, duration=200)]}
        self.assertEqual(songs_from(link, ShareRef(ID, KEY, 42))[0].start, 42)
        self.assertIsNone(songs_from(link, ShareRef(ID, KEY, 500))[0].start)


    def test_a_list_opened_on_a_song_starts_there_then_wraps(self):
        link = {"kind": "list", "from": {"name": "Xiao"}, "entries": [entry(1), entry(2), entry(3), entry(4)]}
        titles = lambda song: [s.title for s in songs_from(link, ShareRef(ID, KEY, None, song))]
        self.assertEqual(titles("e3"), ["Song 3", "Song 4", "Song 1", "Song 2"])
        self.assertEqual(titles("e9"), ["Song 1", "Song 2", "Song 3", "Song 4"])  # not in the list
        self.assertEqual(titles(None), ["Song 1", "Song 2", "Song 3", "Song 4"])


VIDEO, LIST = "dQw4w9WgXcQ", "PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI"


def video(n: int, **more) -> dict:
    return {"id": f"video{n:06d}", "title": f"Video {n}", "channel": "Ado", "duration": 200, **more}


class YouTubeParseTests(unittest.TestCase):
    def test_reads_video_links(self):
        watch = f"https://www.youtube.com/watch?v={VIDEO}"
        for url in (
            watch, f"https://youtu.be/{VIDEO}?si=abc", f"https://m.youtube.com/watch?v={VIDEO}&feature=share",
            f"https://music.youtube.com/watch?v={VIDEO}", f"https://youtube.com/shorts/{VIDEO}",
            f"<https://www.youtube.com/live/{VIDEO}>",
        ):
            self.assertEqual(parse_youtube(f"this one {url}"), YouTubeRef(watch, VIDEO, None, None), url)

    def test_reads_a_start(self):
        self.assertEqual(parse_youtube(f"https://youtu.be/{VIDEO}?t=1m30s").start, 90)
        self.assertEqual(parse_youtube(f"https://www.youtube.com/watch?v={VIDEO}&t=42").start, 42)
        self.assertEqual(parse_youtube_start("1h2m3s"), 3723)
        self.assertEqual(parse_youtube_start("90s"), 90)
        for bad in (None, "", "abc", "1:30", "s"):
            self.assertIsNone(parse_youtube_start(bad), bad)

    def test_reads_playlists_and_the_video_they_open_on(self):
        page = f"https://www.youtube.com/playlist?list={LIST}"
        self.assertEqual(parse_youtube(page), YouTubeRef(page, None, LIST, None))
        opened = parse_youtube(f"https://www.youtube.com/watch?v={VIDEO}&list={LIST}&index=3&t=40")
        self.assertEqual(opened, YouTubeRef(page, VIDEO, LIST, None))  # a start only belongs to a lone video

    def test_a_mix_is_just_its_video(self):
        ref = parse_youtube(f"https://www.youtube.com/watch?v={VIDEO}&list=RD{VIDEO}&start_radio=1")
        self.assertEqual(ref, YouTubeRef(f"https://www.youtube.com/watch?v={VIDEO}", VIDEO, None, None))

    def test_rejects_other_links(self):
        for url in (
            "https://www.youtube.com/watch?v=short", "https://www.youtube.com/@ado", "https://notyoutube.com/watch?v="
            + VIDEO, f"https://xiao215.github.io/selfmp3/l/{ID}/{KEY}", "no link here",
        ):
            self.assertIsNone(parse_youtube(url), url)


class YouTubeSongsTests(unittest.TestCase):
    def test_a_video(self):
        info = {**video(1), "id": VIDEO}
        [s] = youtube_songs_from(info, YouTubeRef("u", VIDEO, None, 30), "Ken")
        self.assertEqual((s.title, s.artist, s.duration, s.start, s.sender), ("Video 1", "Ado", 200, 30, "Ken"))
        self.assertEqual(s.audio_url, f"https://www.youtube.com/watch?v={VIDEO}")
        self.assertEqual(s.cover_url, f"https://i.ytimg.com/vi/{VIDEO}/hqdefault.jpg")
        self.assertEqual((s.source, s.color, s.link_name), ("youtube", YOUTUBE_RED, ""))
        self.assertIsNone(youtube_songs_from(info, YouTubeRef("u", VIDEO, None, 500), "Ken")[0].start)

    def test_a_playlist_skips_what_cant_play_and_starts_where_it_was_opened(self):
        info = {"_type": "playlist", "title": "J-pop", "entries": [
            video(1), video(2, title="[Private video]"), video(3), video(4, live_status="is_live"), None, video(5),
        ]}
        songs = youtube_songs_from(info, YouTubeRef("u", "video000003", LIST, None), "Ken")
        self.assertEqual([s.title for s in songs], ["Video 3", "Video 5", "Video 1"])
        self.assertEqual(songs[0].link_name, "J-pop")
        self.assertEqual([s.title for s in youtube_songs_from(info, YouTubeRef("u", None, LIST, None), "Ken")],
                         ["Video 1", "Video 3", "Video 5"])

    def test_youtube_errors_read_plainly(self):
        error = music._youtube_error(Exception(f"ERROR: [youtube] {VIDEO}: Video unavailable\nmore"))
        self.assertEqual(str(error), "YouTube said: Video unavailable")
        error = music._youtube_error(Exception(f"ERROR: [youtube] {VIDEO}: Sign in to confirm you’re not a bot."))
        self.assertIn("blocking me", str(error))


class PanelTests(unittest.TestCase):
    def test_progress_bar(self):
        self.assertEqual(progress_bar(0, 100, width=5), "●────  0:00 / 1:40")
        self.assertEqual(progress_bar(50, 100, width=5), "━━●──  0:50 / 1:40")
        self.assertEqual(progress_bar(100, 100, width=5), "━━━━●  1:40 / 1:40")
        self.assertEqual(progress_bar(83, 0), "1:23")  # length unknown

    def test_cover_colours(self):
        self.assertEqual(oklch_to_rgb(1, 0, 0), 0xFFFFFF)
        self.assertEqual(oklch_to_rgb(0, 0, 0), 0x000000)
        red = oklch_to_rgb(0.628, 0.2577, 29.23)  # sRGB red in OKLCH
        self.assertTrue(red >> 16 > 0xF0 and red & 0xFFFF < 0x0808, hex(red))
        tone = {"palette": [{"l": 0, "c": 0, "h": 0, "share": 0.2}, {"l": 1, "c": 0, "h": 0, "share": 0.8}]}
        self.assertEqual(cover_color(tone), 0xFFFFFF)
        self.assertIsNone(cover_color(None))

    def test_panel(self):
        s = Song("恋風", "幾田りら", 182, "https://x/a", "Xiao", album="Sketch", cover_url="https://x/c",
                 page_url="https://x/l", link_name="jpop", color=0x123456)
        embed = panel_embed(s, 60, "▶️ Playing", up_next=song(2), more=3)
        self.assertEqual(embed.title, "恋風")
        self.assertIn("幾田りら · Sketch", embed.description)
        self.assertIn("1:00 / 3:02", embed.description)
        self.assertEqual(embed.fields[0].value, "**Song 2** · and 3 more")
        self.assertEqual(embed.thumbnail.url, "https://x/c")
        self.assertEqual(embed.footer.text, "▶️ Playing · from Xiao's link · jpop")

    def test_youtube_panel(self):
        s = Song("Usseewa", "Ado", 205, "https://y/v", "Ken", link_name="J-pop", source="youtube")
        self.assertEqual(panel_embed(s, 0, "▶️ Playing").footer.text, "▶️ Playing · YouTube, queued by Ken · J-pop")


class FakeVoice:
    """Stands in for discord.VoiceClient: plays until stop() or finish()."""

    def __init__(self):
        self.guild = MagicMock(id=1)
        self.played: list[str] = []
        self.disconnect = AsyncMock()
        self.pause = MagicMock()
        self.resume = MagicMock()
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
        self.panels: list[MagicMock] = []
        self.channel = MagicMock(send=AsyncMock(side_effect=self._send))
        patcher = patch.object(music.discord, "FFmpegOpusAudio", side_effect=lambda path, **kw: path)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def _send(self, content=None, **kwargs):
        message = MagicMock(edit=AsyncMock(), content=content, kwargs=kwargs)
        if "embed" in kwargs:
            self.panels.append(message)
        return message

    async def _download(self, s: Song) -> str:
        fd, path = tempfile.mkstemp()
        os.close(fd)
        return path

    def new_player(self) -> Player:
        player = Player(self.cog, self.voice, self.channel)
        self.cog.players[1] = player
        return player

    async def settle(self):
        for _ in range(10):
            await asyncio.sleep(0)

    def last_edit(self, panel: MagicMock) -> dict:
        return panel.edit.await_args.kwargs

    async def test_plays_in_order_and_skips(self):
        player = self.new_player()
        player.add([song(1), song(2)])
        await self.settle()
        self.assertEqual(player.current.title, "Song 1")
        player.skip("Ken")
        await self.settle()
        self.assertEqual(player.current.title, "Song 2")
        self.assertEqual(len(self.voice.played), 2)
        self.assertFalse(os.path.exists(self.voice.played[0]))  # the temp file goes once played
        await player.leave()

    async def test_each_song_gets_a_panel_that_loses_its_buttons_when_it_ends(self):
        player = self.new_player()
        player.add([song(1), song(2)])
        await self.settle()
        self.assertEqual(len(self.panels), 1)
        self.assertIsNotNone(self.panels[0].kwargs["view"])
        self.assertEqual(self.panels[0].kwargs["embed"].fields[0].value, "**Song 2**")  # up next
        self.voice.finish()
        await self.settle()
        closed = self.last_edit(self.panels[0])
        self.assertIsNone(closed["view"])
        self.assertTrue(closed["embed"].footer.text.startswith(PLAYED))
        self.assertEqual(len(self.panels), 2)
        player.skip("Ken")
        await self.settle()
        self.assertTrue(self.last_edit(self.panels[1])["embed"].footer.text.startswith("⏭️ Skipped by Ken"))
        await player.leave()

    async def test_pause_and_resume_hold_the_position(self):
        now = [100.0]
        with patch.object(music, "time", SimpleNamespace(monotonic=lambda: now[0])):
            player = self.new_player()
            player.add([Song("A", "", 300, "https://x/a", "Xiao")])
            await self.settle()
            now[0] += 30
            self.assertTrue(player.pause("Ken"))
            self.assertFalse(player.pause("Ken"))  # already paused
            now[0] += 500
            self.assertEqual(player.position(), 30)
            self.assertIn("Paused by Ken", player.panel_embed().footer.text)
            self.assertTrue(player.resume())
            now[0] += 10
            self.assertEqual(player.position(), 40)
            self.voice.pause.assert_called_once()
            self.voice.resume.assert_called_once()
            await player.leave()

    async def test_shuffle_mixes_the_queue_and_puts_it_back(self):
        player = self.new_player()
        player.add([song(n) for n in range(1, 21)])
        await self.settle()
        with patch.object(music.random, "sample", side_effect=lambda seq, k: list(reversed(seq))):
            player.shuffle(True)
        self.assertEqual(player.queue[0].title, "Song 20")
        self.assertIn("Shuffle", player.panel_embed().footer.text)
        player.skip("Ken")
        await self.settle()
        self.assertEqual(player.current.title, "Song 20")
        player.add([song(21)])  # lands somewhere in the shuffled queue
        player.shuffle(False)
        self.assertEqual([s.title for s in player.queue], [f"Song {n}" for n in range(2, 20)] + ["Song 21"])
        self.assertNotIn("Shuffle", player.panel_embed().footer.text)
        await player.leave()

    async def test_leaves_after_a_long_pause(self):
        with patch.object(music, "PAUSE_SECONDS", 0), patch.object(music, "PANEL_SECONDS", 0.01):
            player = self.new_player()
            player.add([song(1), song(2)])
            await self.settle()
            player.pause("Ken")
            await asyncio.wait_for(player.task, 1)
        self.voice.disconnect.assert_awaited()
        self.assertNotIn(1, self.cog.players)
        self.assertIn("💤", self.last_edit(self.panels[0])["embed"].footer.text)

    async def test_progress_moves_while_playing(self):
        with patch.object(music, "PANEL_SECONDS", 0.01):
            player = self.new_player()
            player.add([song(1)])
            await self.settle()
            await asyncio.sleep(0.05)
            self.assertTrue(self.panels[0].edit.await_count >= 2)
            self.assertIsNotNone(self.last_edit(self.panels[0])["view"])
            await player.leave("👋 Ken sent me away")
        self.assertIn("Ken sent me away", self.last_edit(self.panels[0])["embed"].footer.text)

    async def test_stop_clears_the_queue_and_leave_disconnects(self):
        player = self.new_player()
        player.add([song(1), song(2), song(3)])
        await self.settle()
        player.stop("Ken")
        await self.settle()
        self.assertFalse(player.busy)
        self.assertIn("Stopped by Ken", self.last_edit(self.panels[0])["embed"].footer.text)
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
