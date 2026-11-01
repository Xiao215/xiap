"""/play — play a self.mp3 share link or a YouTube link in your voice channel.

A self.mp3 share link (…/l/<id>/<key>) lends its sender's songs to anyone who
has it: the doorman answers with the link's songs and serves each one's audio
and cover from the sender's bucket. A YouTube video or playlist link is read
with yt-dlp, which also fetches each video's audio when its turn comes. The bot
joins the caller's voice channel and plays them in order. Each song gets a
now-playing panel (cover, progress bar, what's next) with buttons to pause,
skip, stop and leave; /pause, /resume, /skip, /stop, /leave and /queue do the
same from the command line.
"""

import asyncio
import contextlib
import logging
import math
import os
import random
import re
import shutil
import tempfile
import time
from collections import deque
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

import aiohttp
import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands

from xiap import config

log = logging.getLogger(__name__)

LINK_RE = re.compile(r"/l/([0-9a-f]{16})/([0-9a-f]{16})/?$")
URL_RE = re.compile(r"https?://\S+")
ENTRY_RE = re.compile(r"[0-9a-f]{12}")
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
VIDEO_RE = re.compile(r"[\w-]{11}")
PLAYLIST_RE = re.compile(r"[\w-]{2,64}")
YOUTUBE_PATH_RE = re.compile(r"/(?:shorts|live|embed)/([\w-]{11})/?")
YOUTUBE_RED = 0xFF0033
UNAVAILABLE = {"[Private video]", "[Deleted video]"}  # how a playlist lists videos it can't show
LIVE = {"is_live", "is_upcoming"}
MAX_PLAYLIST = 200
IDLE_SECONDS = 120  # leave after this long with nothing queued
EMPTY_SECONDS = 60  # leave after this long alone in the channel
PAUSE_SECONDS = 600  # leave after this long paused
PANEL_SECONDS = 15  # how often the panel's progress bar moves
BAR_WIDTH = 16
MAX_SONG_MB = 200
QUEUE_SHOWN = 10
PLAYED = "✅ Played"


@dataclass(frozen=True)
class ShareRef:
    id: str
    key: str
    start: int | None  # seconds into the song, from the link's ?t=
    song: str | None = None  # the entry a list opens on, from the link's ?s=


@dataclass(frozen=True)
class YouTubeRef:
    url: str  # what yt-dlp reads: the video's or the playlist's page
    video: str | None  # the video a playlist opens on, or the video itself
    playlist: str | None
    start: int | None  # seconds into the video, from the link's ?t=


@dataclass(frozen=True)
class Song:
    title: str
    artist: str
    duration: int  # seconds; 0 = unknown
    audio_url: str  # for YouTube, the video's page, which yt-dlp reads the audio from
    sender: str  # whoever shared the self.mp3 link, or queued the YouTube one
    start: int | None = None
    album: str = ""
    cover_url: str | None = None
    page_url: str | None = None  # the link's own page
    link_name: str = ""
    color: int | None = None  # the cover's main colour, for the panel's stripe
    source: str = "selfmp3"  # or "youtube"


class LinkError(Exception):
    """Something to tell whoever ran /play, as is."""


def parse_start(value: str | None) -> int | None:
    """A link's ?t=: plain seconds, m:ss or h:mm:ss (self.mp3's parseShareStart)."""
    given = (value or "").strip()
    if re.fullmatch(r"\d{1,5}", given):
        return int(given)
    first, *rest = given.split(":")
    if not re.fullmatch(r"\d{1,3}", first) or not 1 <= len(rest) <= 2:
        return None
    if not all(re.fullmatch(r"[0-5]\d", part) for part in rest):
        return None
    seconds = 0
    for part in (first, *rest):
        seconds = seconds * 60 + int(part)
    return seconds


def parse_link(text: str) -> ShareRef | None:
    """The share link in `text`, on github.io or the doorman: both end in /l/<id>/<key>."""
    for url in URL_RE.findall(text):
        parsed = urlparse(url.rstrip(">)"))
        if match := LINK_RE.search(parsed.path):
            query = parse_qs(parsed.query)
            start = parse_start(query.get("t", [None])[0])
            song = query.get("s", [""])[0]
            return ShareRef(match[1], match[2], start, song if ENTRY_RE.fullmatch(song) else None)
    return None


def parse_youtube_start(value: str | None) -> int | None:
    """A YouTube link's ?t=: plain seconds, or 1h2m3s and any part of it."""
    given = (value or "").strip().lower()
    match = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s?)?", given)
    if not given or not match:
        return None
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def parse_youtube(text: str) -> YouTubeRef | None:
    """The YouTube video or playlist link in `text`.

    A video opened from a playlist (watch?v=…&list=…) means the playlist, starting on that video.
    A Mix (list=RD…) is YouTube's endless radio rather than a list someone made, so it's just the video."""
    for url in URL_RE.findall(text):
        parsed = urlparse(url.rstrip(">)"))
        host = (parsed.hostname or "").lower()
        if host not in YOUTUBE_HOSTS:
            continue
        query = parse_qs(parsed.query)
        if host == "youtu.be":
            video = parsed.path.strip("/")
        elif match := YOUTUBE_PATH_RE.fullmatch(parsed.path):
            video = match[1]
        else:
            video = query.get("v", [""])[0]
        video = video if VIDEO_RE.fullmatch(video) else None
        playlist = query.get("list", [""])[0]
        playlist = playlist if PLAYLIST_RE.fullmatch(playlist) and not playlist.startswith("RD") else None
        if playlist:
            return YouTubeRef(f"https://www.youtube.com/playlist?list={playlist}", video, playlist, None)
        if video:
            start = parse_youtube_start(query.get("t", query.get("start", [None]))[0])
            return YouTubeRef(f"https://www.youtube.com/watch?v={video}", video, None, start)
    return None


def oklch_to_rgb(lightness: float, chroma: float, hue: float) -> int:
    """An OKLCH colour (how self.mp3 keeps a cover's swatches) as 0xRRGGBB, clipped to sRGB."""
    a, b = chroma * math.cos(math.radians(hue)), chroma * math.sin(math.radians(hue))
    l_ = (lightness + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m_ = (lightness - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s_ = (lightness - 0.0894841775 * a - 1.2914855480 * b) ** 3
    linear = (
        4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_,
        -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_,
        -0.0041960863 * l_ - 0.7034186147 * m_ + 1.7076147010 * s_,
    )
    value = 0
    for x in linear:
        x = min(max(x, 0.0), 1.0)
        x = 12.92 * x if x <= 0.0031308 else 1.055 * x ** (1 / 2.4) - 0.055
        value = value << 8 | round(x * 255)
    return value


def cover_color(tone: dict | None) -> int | None:
    """The swatch that covers most of the cover."""
    palette = (tone or {}).get("palette") or []
    if not palette:
        return None
    main = max(palette, key=lambda swatch: swatch.get("share", 0))
    return oklch_to_rgb(main["l"], main["c"], main["h"])


def songs_from(link: dict, ref: ShareRef) -> list[Song]:
    """The link's songs that can still play. A start only applies to a one-song link.

    A list opened on one of its songs (?s=) plays from that song to the end, then the ones before it."""
    base = config.SELFMP3_DOORMAN_URL
    sender = link.get("from", {}).get("name") or "someone"
    songs = []
    first = 0
    for entry in link.get("entries", []):
        song = entry.get("song") or {}
        if entry.get("gone") or not song.get("audio"):
            continue
        files = f"{base}/v1/share/{ref.id}/files/{entry['id']}"
        duration = int(song.get("duration") or 0)
        start = ref.start if link.get("kind") == "song" else None
        if start is not None and duration and start >= duration:
            start = None
        if entry["id"] == ref.song and link.get("kind") == "list":
            first = len(songs)
        songs.append(Song(
            title=song.get("title") or "Untitled",
            artist=song.get("artist") or "",
            duration=duration,
            audio_url=f"{files}/audio?k={ref.key}",
            sender=sender,
            start=start,
            album=song.get("album") or "",
            cover_url=f"{files}/cover?k={ref.key}" if song.get("cover") else None,
            page_url=f"{base}/l/{ref.id}/{ref.key}",
            link_name=link.get("name") or "",
            color=cover_color(song.get("coverTone")),
        ))
    return songs[first:] + songs[:first]


def youtube_songs_from(info: dict, ref: YouTubeRef, by: str) -> list[Song]:
    """A video, or a playlist's videos that can play. A start only applies to a lone video.

    A playlist opened on one of its videos plays from there to the end, then the ones before it."""
    is_list = info.get("_type") == "playlist"
    songs = []
    first = 0
    for video in (info.get("entries") or []) if is_list else [info]:
        if not video or not VIDEO_RE.fullmatch(video.get("id") or ""):
            continue
        if video.get("title") in UNAVAILABLE or video.get("live_status") in LIVE:
            continue
        duration = int(video.get("duration") or 0)
        start = None if is_list else ref.start
        if start is not None and duration and start >= duration:
            start = None
        if video["id"] == ref.video:
            first = len(songs)
        page = f"https://www.youtube.com/watch?v={video['id']}"
        songs.append(Song(
            title=video.get("title") or "Untitled",
            artist=video.get("channel") or video.get("uploader") or "",
            duration=duration,
            audio_url=page,
            sender=by,
            start=start,
            cover_url=f"https://i.ytimg.com/vi/{video['id']}/hqdefault.jpg",
            page_url=page,
            link_name=(info.get("title") or "") if is_list else "",
            color=YOUTUBE_RED,
            source="youtube",
        ))
    return songs[first:] + songs[:first]


def _youtube_options(**more) -> dict:
    options = {"quiet": True, "no_warnings": True, "noprogress": True, "logger": log, **more}
    if config.YOUTUBE_COOKIES:
        options["cookiefile"] = config.YOUTUBE_COOKIES
    return options


def _youtube_error(e: Exception) -> LinkError:
    """yt-dlp's error, without its `ERROR: [youtube] <id>: ` prefix."""
    text = re.sub(r"^ERROR: (\[[\w:]+\] [\w-]+: )?", "", str(e)).split("\n")[0]
    if "confirm you" in text and "not a bot" in text:
        log.warning("YouTube wants a sign-in; set YOUTUBE_COOKIES to a cookies.txt: %s", text)
        return LinkError("YouTube is blocking me right now (it wants me to sign in).")
    return LinkError(f"YouTube said: {text}")


def youtube_info(url: str) -> dict:
    """A video's details, or a playlist's videos without opening each one. Blocks; run it in a thread."""
    options = _youtube_options(extract_flat="in_playlist", playlistend=MAX_PLAYLIST)
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            return ydl.extract_info(url, download=False)
    except yt_dlp.utils.YoutubeDLError as e:
        raise _youtube_error(e) from e


def youtube_download(url: str) -> str:
    """Fetch a video's audio to a temp file. Blocks; run it in a thread."""
    folder = tempfile.mkdtemp(prefix="xiap-")
    options = _youtube_options(
        format="bestaudio/best",
        outtmpl=os.path.join(folder, "audio.%(ext)s"),
        noplaylist=True,
        max_filesize=MAX_SONG_MB * 1024 * 1024,
    )
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
            if info.get("live_status") in LIVE:
                raise LinkError("it's a live stream.")
            ydl.process_ie_result(info, download=True)
        files = [name for name in os.listdir(folder) if not name.endswith((".part", ".ytdl"))]
        if not files:  # yt-dlp skips a file over max_filesize rather than failing
            raise LinkError(f"it's over {MAX_SONG_MB} MB.")
        fd, path = tempfile.mkstemp(prefix="xiap-", suffix=".audio")
        os.close(fd)
        os.replace(os.path.join(folder, files[0]), path)
        return path
    except yt_dlp.utils.YoutubeDLError as e:
        raise _youtube_error(e) from e
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def fmt_time(seconds: int) -> str:
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def describe(song: Song) -> str:
    line = f"**{song.title}**"
    if song.artist:
        line += f" — {song.artist}"
    if song.duration:
        line += f" ({fmt_time(song.duration)})"
    return line


def progress_bar(position: float, duration: int, width: int = BAR_WIDTH) -> str:
    """`━━━━●───────  1:23 / 3:02`, or just the time played when the length is unknown."""
    if not duration:
        return fmt_time(int(position))
    done = min(width - 1, int(position / duration * width))
    return f"{'━' * done}●{'─' * (width - 1 - done)}  {fmt_time(int(position))} / {fmt_time(duration)}"


def panel_embed(song: Song, position: float, status: str, up_next: Song | None = None, more: int = 0) -> discord.Embed:
    byline = " · ".join(part for part in (song.artist, song.album) if part)
    embed = discord.Embed(
        title=song.title[:256],
        url=song.page_url,
        description=f"{byline}\n\n{progress_bar(position, song.duration)}".strip(),
        color=song.color,
    )
    if up_next:
        embed.add_field(name="Up next", value=describe(up_next) + (f" · and {more} more" if more else ""), inline=False)
    if song.cover_url:
        embed.set_thumbnail(url=song.cover_url)
    if song.source == "youtube":
        footer = f"{status} · YouTube, queued by {song.sender}"
    else:
        footer = f"{status} · from {song.sender}'s link"
    if song.link_name and song.link_name != song.title:
        footer += f" · {song.link_name}"
    embed.set_footer(text=footer[:2048])
    return embed


async def _error_text(resp: aiohttp.ClientResponse) -> str:
    with contextlib.suppress(Exception):
        return (await resp.json()).get("error") or resp.reason
    return resp.reason or f"HTTP {resp.status}"


class Controls(discord.ui.View):
    """A panel's buttons. Only people in the bot's voice channel can use them."""

    def __init__(self, player: "Player") -> None:
        super().__init__(timeout=None)
        self.player = player
        self.sync()

    def sync(self) -> None:
        paused = self.player.paused
        self.pause_button.label = "Resume" if paused else "Pause"
        self.pause_button.emoji = "▶️" if paused else "⏸️"
        self.pause_button.style = discord.ButtonStyle.success if paused else discord.ButtonStyle.secondary
        self.shuffle_button.label = "Shuffle on" if self.player.shuffled else "Shuffle off"
        self.shuffle_button.style = discord.ButtonStyle.success if self.player.shuffled else discord.ButtonStyle.secondary

    async def _allowed(self, interaction: discord.Interaction) -> bool:
        return await self.player.cog.controller(interaction, self.player) is not None

    @discord.ui.button(label="Pause", emoji="⏸️", style=discord.ButtonStyle.secondary)
    async def pause_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._allowed(interaction):
            return
        if self.player.paused:
            self.player.resume()
        else:
            self.player.pause(interaction.user.display_name)
        self.sync()
        await interaction.response.edit_message(embed=self.player.panel_embed(), view=self)

    @discord.ui.button(label="Skip", emoji="⏭️", style=discord.ButtonStyle.secondary)
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if await self._allowed(interaction):
            await interaction.response.defer()
            self.player.skip(interaction.user.display_name)

    @discord.ui.button(label="Shuffle off", emoji="🔀", style=discord.ButtonStyle.secondary)
    async def shuffle_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._allowed(interaction):
            return
        self.player.shuffle(not self.player.shuffled)
        self.sync()
        await interaction.response.edit_message(embed=self.player.panel_embed(), view=self)

    @discord.ui.button(label="Stop", emoji="⏹️", style=discord.ButtonStyle.secondary)
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if await self._allowed(interaction):
            await interaction.response.defer()
            self.player.stop(interaction.user.display_name)

    @discord.ui.button(label="Leave", emoji="👋", style=discord.ButtonStyle.danger)
    async def leave_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if await self._allowed(interaction):
            await interaction.response.defer()
            await self.player.leave(f"👋 {interaction.user.display_name} sent me away")


class Player:
    """One guild's voice connection and queue. It leaves when idle, stopped, paused too long or left alone."""

    def __init__(self, cog: "MusicCog", voice: discord.VoiceClient, channel: discord.abc.Messageable) -> None:
        self.cog = cog
        self.voice = voice
        self.channel = channel  # where /play was used; panels go here
        self.queue: deque[Song] = deque()
        self.current: Song | None = None
        self.panel: discord.Message | None = None
        self.controls: Controls | None = None
        self.outcome: str | None = None  # how the current song ended, for its panel
        self.paused_by: str | None = None
        self.shuffled = False
        self._order: list[Song] = []  # the queue as it was added, to go back to when shuffle's turned off
        self._played = 0.0  # seconds of the current song played before the last resume
        self._resumed_at = 0.0
        self._paused_at: float | None = None
        self._wake = asyncio.Event()
        self._done = asyncio.Event()
        self.task = asyncio.create_task(self._run())

    @property
    def busy(self) -> bool:
        return self.current is not None or bool(self.queue)

    @property
    def paused(self) -> bool:
        return self._paused_at is not None

    def position(self) -> float:
        """Seconds into the current song."""
        if self.current is None:
            return 0.0
        running = 0.0 if self.paused else time.monotonic() - self._resumed_at
        position = (self.current.start or 0) + self._played + running
        return min(position, self.current.duration) if self.current.duration else position

    def add(self, songs: list[Song]) -> None:
        if self.shuffled:
            self._order.extend(songs)
            for song in songs:
                self.queue.insert(random.randint(0, len(self.queue)), song)
        else:
            self.queue.extend(songs)
        self._wake.set()

    def shuffle(self, on: bool) -> None:
        """Shuffle what's queued, or put what's left back in the order it was added."""
        if on and not self.shuffled:
            self._order = list(self.queue)
            self.queue = deque(random.sample(self._order, len(self._order)))
        elif not on and self.shuffled:
            left = {id(song) for song in self.queue}
            self.queue = deque(song for song in self._order if id(song) in left)
            self._order = []
        self.shuffled = on

    def pause(self, by: str) -> bool:
        if self.current is None or self.paused:
            return False
        self.voice.pause()
        now = time.monotonic()
        self._played += now - self._resumed_at
        self._paused_at, self.paused_by = now, by
        return True

    def resume(self) -> bool:
        if not self.paused:
            return False
        self.voice.resume()
        self._resumed_at, self._paused_at, self.paused_by = time.monotonic(), None, None
        return True

    def skip(self, by: str) -> None:
        self.outcome = f"⏭️ Skipped by {by}"
        self.voice.stop()  # the `after` callback moves on to the next song

    def stop(self, by: str) -> None:
        self.queue.clear()
        self._order = []
        self.outcome = f"⏹️ Stopped by {by}"
        self.voice.stop()

    async def leave(self, outcome: str = "👋 Left the channel") -> None:
        self.outcome = self.outcome or outcome
        self.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.task

    def panel_embed(self, ended: str | None = None) -> discord.Embed:
        if ended:
            position = self.current.duration if ended == PLAYED and self.current.duration else self.position()
            return panel_embed(self.current, position, ended)
        status = f"⏸️ Paused by {self.paused_by}" if self.paused else "▶️ Playing"
        if self.shuffled:
            status += " · 🔀 Shuffle"
        up_next = self.queue[0] if self.queue else None
        return panel_embed(self.current, self.position(), status, up_next, max(len(self.queue) - 1, 0))

    async def refresh_panel(self) -> None:
        if self.panel is None or self.current is None:
            return
        if self.controls:
            self.controls.sync()
        try:
            await self.panel.edit(embed=self.panel_embed(), view=self.controls)
        except discord.NotFound:
            self.panel = None  # someone deleted it; keep playing without one
        except discord.HTTPException as e:
            log.warning("Couldn't update the panel: %s", e)

    async def _post_panel(self) -> None:
        self.controls = Controls(self)
        try:
            self.panel = await self.channel.send(embed=self.panel_embed(), view=self.controls)
        except discord.HTTPException as e:
            log.warning("Couldn't post the panel: %s", e)
            self.panel = None

    async def _close_panel(self) -> None:
        """The panel keeps the song and how it ended, without buttons; the next song gets a new one."""
        if self.controls:
            self.controls.stop()
        if self.panel is not None:
            with contextlib.suppress(discord.HTTPException):
                await self.panel.edit(embed=self.panel_embed(self.outcome or PLAYED), view=None)
        self.panel = self.controls = None

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            while True:
                if not self.queue:
                    self._wake.clear()
                    try:
                        await asyncio.wait_for(self._wake.wait(), IDLE_SECONDS)
                    except TimeoutError:
                        return
                    continue
                self.current = self.queue.popleft()
                self.outcome = self.paused_by = self._paused_at = None
                try:
                    path = await self.cog.download(self.current)
                except LinkError as e:
                    await self._say(f"⚠️ Skipping {describe(self.current)}: {e}")
                    self.current = None
                    continue
                try:
                    before = f"-ss {self.current.start}" if self.current.start else None
                    # -vn: m4a files carry their cover as a video stream, which Ogg can't hold.
                    source = discord.FFmpegOpusAudio(path, before_options=before, options="-vn")
                    self._done.clear()
                    self.voice.play(source, after=lambda e: self._finished(loop, e))
                    self._played, self._resumed_at = 0.0, time.monotonic()
                    await self._post_panel()
                    while not self._done.is_set():
                        with contextlib.suppress(TimeoutError):
                            await asyncio.wait_for(self._done.wait(), PANEL_SECONDS)
                        if self.paused and time.monotonic() - self._paused_at >= PAUSE_SECONDS:
                            self.outcome = f"💤 Paused for {PAUSE_SECONDS // 60} minutes, so I left"
                            await self._say(f"{self.outcome}.")
                            return
                        if not self._done.is_set() and not self.paused:
                            await self.refresh_panel()
                finally:
                    with contextlib.suppress(OSError):
                        os.remove(path)
                    await self._close_panel()
                    self.current = None
        except Exception:
            log.exception("Player crashed")
            await self._say("💥 Something broke while playing — leaving the channel.")
        finally:
            self.queue.clear()
            self.cog.players.pop(self.voice.guild.id, None)
            with contextlib.suppress(Exception):
                await self.voice.disconnect()

    def _finished(self, loop: asyncio.AbstractEventLoop, error: Exception | None) -> None:
        # Runs on discord.py's audio thread.
        if error:
            log.warning("Playback error: %s", error)
        loop.call_soon_threadsafe(self._done.set)

    async def _say(self, text: str) -> None:
        with contextlib.suppress(discord.HTTPException):
            await self.channel.send(text)


class MusicCog(commands.Cog):
    """Plays self.mp3 share links and YouTube links in voice channels, one queue per server."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.http: aiohttp.ClientSession | None = None
        self.players: dict[int, Player] = {}

    async def cog_load(self) -> None:
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=60))

    async def cog_unload(self) -> None:
        for player in list(self.players.values()):
            await player.leave()
        if self.http:
            await self.http.close()

    async def fetch_link(self, ref: ShareRef) -> dict:
        # `v` makes the bot count as one guest on the sender's side, however often it opens a link.
        url = f"{config.SELFMP3_DOORMAN_URL}/v1/share/{ref.id}"
        params = {"k": ref.key, "v": f"xiap-{self.bot.user.id}"}
        try:
            async with self.http.get(url, params=params) as resp:
                if resp.status in (403, 404):
                    raise LinkError("that link doesn't open — it may be gone, or cut short when it was copied.")
                if resp.status != 200:
                    raise LinkError(f"self.mp3 said: {await _error_text(resp)}")
                return await resp.json()
        except aiohttp.ClientError as e:
            raise LinkError(f"couldn't reach self.mp3 ({e.__class__.__name__}).") from e

    async def download(self, song: Song) -> str:
        """Fetch a song's audio to a temp file, so a failed read can be told apart from a bad file."""
        if song.source == "youtube":
            task = asyncio.ensure_future(asyncio.to_thread(youtube_download, song.audio_url))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # The thread can't be stopped; drop its file once it's done.
                task.add_done_callback(lambda t: t.cancelled() or t.exception() or os.remove(t.result()))
                raise
        fd, path = tempfile.mkstemp(prefix="xiap-", suffix=".audio")
        try:
            with os.fdopen(fd, "wb") as f:
                async with self.http.get(song.audio_url) as resp:
                    if resp.status == 410:
                        raise LinkError("the link has ended.")
                    if resp.status != 200:
                        raise LinkError(await _error_text(resp))
                    size = 0
                    async for chunk in resp.content.iter_chunked(1 << 16):
                        size += len(chunk)
                        if size > MAX_SONG_MB * 1024 * 1024:
                            raise LinkError(f"it's over {MAX_SONG_MB} MB.")
                        f.write(chunk)
            return path
        except BaseException as e:
            with contextlib.suppress(OSError):
                os.remove(path)
            if isinstance(e, aiohttp.ClientError):
                raise LinkError(f"couldn't download it ({e.__class__.__name__}).") from e
            raise

    async def controller(self, interaction: discord.Interaction, player: Player | None = None) -> Player | None:
        """The guild's player, if the caller is in its voice channel; otherwise says why not.

        `player` is the one a panel belongs to: an old panel's buttons don't steer a newer player."""
        current = self.players.get(interaction.guild_id)
        if current is None or (player is not None and player is not current):
            await interaction.response.send_message("I'm not playing anything right now.", ephemeral=True)
            return None
        voice = getattr(interaction.user, "voice", None)
        if not voice or voice.channel != current.voice.channel:
            await interaction.response.send_message(
                f"Join {current.voice.channel.mention} to control the music.", ephemeral=True
            )
            return None
        return current

    async def selfmp3_songs(self, ref: ShareRef) -> tuple[list[Song], str, str]:
        """A share link's songs, what /play calls them, and who they're from. LinkError says why not, as is."""
        try:
            data = await self.fetch_link(ref)
        except LinkError as e:
            raise LinkError(f"😕 Couldn't play that: {e}") from e
        sender = data.get("from", {}).get("name") or "whoever sent it"
        if data.get("ended"):
            raise LinkError(f"⌛ **{data.get('name', 'That link')}** has ended — ask {sender} for it again.")
        songs = songs_from(data, ref)
        if not songs:
            raise LinkError(f"😕 None of the songs in that link can play any more — {sender} removed them.")
        what = describe(songs[0]) if data.get("kind") == "song" else f"**{data.get('name')}** · {len(songs)} songs"
        if data.get("kind") == "list" and ref.song and f"/files/{ref.song}/" in songs[0].audio_url:
            what += f", starting on {describe(songs[0])}"
        return songs, what, f"shared by {sender}"

    async def youtube_songs(self, ref: YouTubeRef, by: str) -> tuple[list[Song], str, str]:
        """A YouTube link's videos, what /play calls them, and where they're from. LinkError says why not, as is."""
        try:
            info = await asyncio.to_thread(youtube_info, ref.url)
        except LinkError as e:
            raise LinkError(f"😕 Couldn't play that: {e}") from e
        if info.get("live_status") in LIVE:
            raise LinkError("😕 That's a live stream — I can only play videos.")
        songs = youtube_songs_from(info, ref, by)
        if not songs:
            raise LinkError("😕 Nothing in that playlist can play.")
        if not ref.playlist:
            return songs, describe(songs[0]), "from YouTube"
        what = f"**{info.get('title') or 'That playlist'}** · {len(songs)} videos"
        if ref.video and songs[0].page_url.endswith(ref.video):
            what += f", starting on {describe(songs[0])}"
        return songs, what, "from YouTube"

    @app_commands.command(name="play", description="Play a self.mp3 share link or a YouTube link in your voice channel")
    @app_commands.describe(link="A self.mp3 share link (…/selfmp3/l/…/…), or a YouTube video or playlist link")
    @app_commands.guild_only()
    async def play(self, interaction: discord.Interaction, link: str) -> None:
        ref = parse_link(link)
        youtube = parse_youtube(link) if ref is None else None
        if ref is None and youtube is None:
            await interaction.response.send_message(
                "That doesn't look like a self.mp3 share link (they end in `/l/<id>/<key>`) "
                "or a YouTube video or playlist link.",
                ephemeral=True,
            )
            return
        voice_state = interaction.user.voice
        if not voice_state or not voice_state.channel:
            await interaction.response.send_message("Join a voice channel first, then `/play` again.", ephemeral=True)
            return
        channel = voice_state.channel
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.connect and perms.speak):
            await interaction.response.send_message(
                f"I need the **Connect** and **Speak** permissions in {channel.mention}.", ephemeral=True
            )
            return
        player = self.players.get(interaction.guild_id)
        if player and player.busy and player.voice.channel != channel:
            await interaction.response.send_message(
                f"I'm already playing in {player.voice.channel.mention} — join there to add to the queue.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)
        try:
            if ref is not None:
                songs, what, credit = await self.selfmp3_songs(ref)
            else:
                songs, what, credit = await self.youtube_songs(youtube, interaction.user.display_name)
        except LinkError as e:
            await interaction.followup.send(str(e))
            return

        try:
            player = self.players.get(interaction.guild_id)
            if player is None:
                voice = await channel.connect(self_deaf=True)
                player = self.players[interaction.guild_id] = Player(self, voice, interaction.channel)
            elif player.voice.channel != channel:
                await player.voice.move_to(channel)
        except (discord.ClientException, TimeoutError) as e:
            log.warning("Couldn't join %s: %s", channel, e)
            await interaction.followup.send(f"😕 I couldn't join {channel.mention}.")
            return
        player.channel = interaction.channel
        was_busy = player.busy
        player.add(songs)

        if player.shuffled:
            what += " (shuffled in)"
        if songs[0].start:
            what += f" from {fmt_time(songs[0].start)}"
        verb = "Queued" if was_busy else "Playing"
        await interaction.followup.send(f"🎧 {verb} {what}, {credit}, in {channel.mention}")
        if was_busy:
            await player.refresh_panel()  # its "Up next" may have changed

    @app_commands.command(name="pause", description="Pause the music")
    @app_commands.guild_only()
    async def pause(self, interaction: discord.Interaction) -> None:
        if not (player := await self.controller(interaction)):
            return
        if not player.pause(interaction.user.display_name):
            await interaction.response.send_message("Nothing's playing.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"⏸️ Paused. I'll leave if nobody resumes within {PAUSE_SECONDS // 60} minutes."
        )
        await player.refresh_panel()

    @app_commands.command(name="resume", description="Carry on playing after a pause")
    @app_commands.guild_only()
    async def resume(self, interaction: discord.Interaction) -> None:
        if not (player := await self.controller(interaction)):
            return
        if not player.resume():
            await interaction.response.send_message("It isn't paused.", ephemeral=True)
            return
        await interaction.response.send_message("▶️ Resumed.")
        await player.refresh_panel()

    @app_commands.command(name="skip", description="Skip the song that's playing")
    @app_commands.guild_only()
    async def skip(self, interaction: discord.Interaction) -> None:
        if not (player := await self.controller(interaction)):
            return
        if player.current is None:
            await interaction.response.send_message("Nothing's playing.", ephemeral=True)
            return
        song = player.current
        player.skip(interaction.user.display_name)
        await interaction.response.send_message(f"⏭️ Skipped {describe(song)}")

    @app_commands.command(name="stop", description="Stop the music and clear the queue")
    @app_commands.guild_only()
    async def stop(self, interaction: discord.Interaction) -> None:
        if not (player := await self.controller(interaction)):
            return
        player.stop(interaction.user.display_name)
        await interaction.response.send_message("⏹️ Stopped and cleared the queue.")

    @app_commands.command(name="leave", description="Stop the music and leave the voice channel")
    @app_commands.guild_only()
    async def leave(self, interaction: discord.Interaction) -> None:
        if not (player := await self.controller(interaction)):
            return
        await interaction.response.send_message(f"👋 Left {player.voice.channel.mention}.")
        await player.leave(f"👋 {interaction.user.display_name} sent me away")

    @app_commands.command(name="queue", description="What's playing and what's up next")
    @app_commands.guild_only()
    async def queue(self, interaction: discord.Interaction) -> None:
        player = self.players.get(interaction.guild_id)
        if not player or not player.busy:
            await interaction.response.send_message("The queue is empty.", ephemeral=True)
            return
        lines = []
        if player.current:
            lines.append(f"🎶 Now: {describe(player.current)}")
        lines += [f"`{i}.` {describe(s)}" for i, s in enumerate(list(player.queue)[:QUEUE_SHOWN], 1)]
        if len(player.queue) > QUEUE_SHOWN:
            lines.append(f"…and {len(player.queue) - QUEUE_SHOWN} more")
        await interaction.response.send_message("\n".join(lines)[:2000])

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        player = self.players.get(member.guild.id)
        if player is None:
            return
        if member.id == self.bot.user.id:
            if after.channel is None:  # disconnected by someone else
                await player.leave("👋 Disconnected")
            return
        if before.channel != player.voice.channel or after.channel == before.channel:
            return
        # Someone left the bot's channel: go too if nobody's back after a while.
        if not any(not m.bot for m in player.voice.channel.members):
            await asyncio.sleep(EMPTY_SECONDS)
            if self.players.get(member.guild.id) is player and not any(not m.bot for m in player.voice.channel.members):
                await player._say("👋 Everyone left, so I did too.")
                await player.leave("👋 Everyone left")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MusicCog(bot))
