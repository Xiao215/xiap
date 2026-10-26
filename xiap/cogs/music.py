"""/play — play a self.mp3 share link in your voice channel.

A self.mp3 share link (…/l/<id>/<key>) lends its sender's songs to anyone who
has it: the doorman answers with the link's songs and serves each one's audio
from the sender's bucket. The bot joins the caller's voice channel and plays
them in order; /skip, /stop and /leave control it, and /queue shows what's next.
"""

import asyncio
import contextlib
import logging
import os
import re
import tempfile
from collections import deque
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from xiap import config

log = logging.getLogger(__name__)

LINK_RE = re.compile(r"/l/([0-9a-f]{16})/([0-9a-f]{16})/?$")
URL_RE = re.compile(r"https?://\S+")
IDLE_SECONDS = 120  # leave after this long with nothing queued
EMPTY_SECONDS = 60  # leave after this long alone in the channel
MAX_SONG_MB = 200
QUEUE_SHOWN = 10


@dataclass(frozen=True)
class ShareRef:
    id: str
    key: str
    start: int | None  # seconds into the song, from the link's ?t=


@dataclass(frozen=True)
class Song:
    title: str
    artist: str
    duration: int  # seconds; 0 = unknown
    audio_url: str
    sender: str
    start: int | None = None


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
            start = parse_start(parse_qs(parsed.query).get("t", [None])[0])
            return ShareRef(match[1], match[2], start)
    return None


def songs_from(link: dict, ref: ShareRef) -> list[Song]:
    """The link's songs that can still play. A start only applies to a one-song link."""
    sender = link.get("from", {}).get("name") or "someone"
    songs = []
    for entry in link.get("entries", []):
        song = entry.get("song") or {}
        if entry.get("gone") or not song.get("audio"):
            continue
        duration = int(song.get("duration") or 0)
        start = ref.start if link.get("kind") == "song" else None
        if start is not None and duration and start >= duration:
            start = None
        songs.append(Song(
            title=song.get("title") or "Untitled",
            artist=song.get("artist") or "",
            duration=duration,
            audio_url=f"{config.SELFMP3_DOORMAN_URL}/v1/share/{ref.id}/files/{entry['id']}/audio?k={ref.key}",
            sender=sender,
            start=start,
        ))
    return songs


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


async def _error_text(resp: aiohttp.ClientResponse) -> str:
    with contextlib.suppress(Exception):
        return (await resp.json()).get("error") or resp.reason
    return resp.reason or f"HTTP {resp.status}"


class Player:
    """One guild's voice connection and queue. It leaves when idle, stopped or left alone."""

    def __init__(self, cog: "MusicCog", voice: discord.VoiceClient, channel: discord.abc.Messageable) -> None:
        self.cog = cog
        self.voice = voice
        self.channel = channel  # where /play was used; now-playing lines go here
        self.queue: deque[Song] = deque()
        self.current: Song | None = None
        self._wake = asyncio.Event()
        self._done = asyncio.Event()
        self.task = asyncio.create_task(self._run())

    @property
    def busy(self) -> bool:
        return self.current is not None or bool(self.queue)

    def add(self, songs: list[Song]) -> None:
        self.queue.extend(songs)
        self._wake.set()

    def skip(self) -> None:
        self.voice.stop()  # the `after` callback moves on to the next song

    def stop(self) -> None:
        self.queue.clear()
        self.voice.stop()

    async def leave(self) -> None:
        self.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.task

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
                    await self._say(f"🎶 Now playing {describe(self.current)} · from {self.current.sender}'s link")
                    await self._done.wait()
                finally:
                    with contextlib.suppress(OSError):
                        os.remove(path)
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
    """Plays self.mp3 share links in voice channels, one queue per server."""

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

    async def _player_for(self, interaction: discord.Interaction) -> Player | None:
        """The caller's player, if they're in the voice channel it plays in; otherwise says why not."""
        player = self.players.get(interaction.guild_id)
        if player is None:
            await interaction.response.send_message("I'm not playing anything right now.", ephemeral=True)
            return None
        voice = getattr(interaction.user, "voice", None)
        if not voice or voice.channel != player.voice.channel:
            await interaction.response.send_message(
                f"Join {player.voice.channel.mention} to control the music.", ephemeral=True
            )
            return None
        return player

    @app_commands.command(name="play", description="Play a self.mp3 share link in your voice channel")
    @app_commands.describe(link="A self.mp3 share link, e.g. https://xiao215.github.io/selfmp3/l/…/…")
    @app_commands.guild_only()
    async def play(self, interaction: discord.Interaction, link: str) -> None:
        ref = parse_link(link)
        if ref is None:
            await interaction.response.send_message(
                "That doesn't look like a self.mp3 share link — they end in `/l/<id>/<key>`.", ephemeral=True
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
            data = await self.fetch_link(ref)
        except LinkError as e:
            await interaction.followup.send(f"😕 Couldn't play that: {e}")
            return
        sender = data.get("from", {}).get("name") or "whoever sent it"
        if data.get("ended"):
            await interaction.followup.send(f"⌛ **{data.get('name', 'That link')}** has ended — ask {sender} for it again.")
            return
        songs = songs_from(data, ref)
        if not songs:
            await interaction.followup.send(f"😕 None of the songs in that link can play any more — {sender} removed them.")
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

        what = describe(songs[0]) if data.get("kind") == "song" else f"**{data.get('name')}** · {len(songs)} songs"
        if songs[0].start:
            what += f" from {fmt_time(songs[0].start)}"
        verb = "Queued" if was_busy else "Playing"
        await interaction.followup.send(f"🎧 {verb} {what}, shared by {sender}, in {channel.mention}")

    @app_commands.command(name="skip", description="Skip the song that's playing")
    @app_commands.guild_only()
    async def skip(self, interaction: discord.Interaction) -> None:
        if not (player := await self._player_for(interaction)):
            return
        if player.current is None:
            await interaction.response.send_message("Nothing's playing.", ephemeral=True)
            return
        song = player.current
        player.skip()
        await interaction.response.send_message(f"⏭️ Skipped {describe(song)}")

    @app_commands.command(name="stop", description="Stop the music and clear the queue")
    @app_commands.guild_only()
    async def stop(self, interaction: discord.Interaction) -> None:
        if not (player := await self._player_for(interaction)):
            return
        player.stop()
        await interaction.response.send_message("⏹️ Stopped and cleared the queue.")

    @app_commands.command(name="leave", description="Stop the music and leave the voice channel")
    @app_commands.guild_only()
    async def leave(self, interaction: discord.Interaction) -> None:
        if not (player := await self._player_for(interaction)):
            return
        await interaction.response.send_message(f"👋 Left {player.voice.channel.mention}.")
        await player.leave()

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
                await player.leave()
            return
        if before.channel != player.voice.channel or after.channel == before.channel:
            return
        # Someone left the bot's channel: go too if nobody's back after a while.
        if not any(not m.bot for m in player.voice.channel.members):
            await asyncio.sleep(EMPTY_SECONDS)
            if self.players.get(member.guild.id) is player and not any(not m.bot for m in player.voice.channel.members):
                await player._say("👋 Everyone left, so I did too.")
                await player.leave()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MusicCog(bot))
