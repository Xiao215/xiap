import io
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
from PIL import Image

from xiap.cogs.chat import ChatCog, FileReader, _embed_images, _to_png


def gif(frames: int) -> bytes:
    images = [Image.new("RGB", (40, 30), (i * 60, 0, 0)) for i in range(frames)]
    out = io.BytesIO()
    images[0].save(out, "GIF", save_all=True, append_images=images[1:])
    return out.getvalue()


def png() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (10, 10)).save(out, "PNG")
    return out.getvalue()


def attachment(filename: str, mime: str, data: bytes) -> SimpleNamespace:
    return SimpleNamespace(filename=filename, content_type=mime, size=len(data), read=AsyncMock(return_value=data))


def message(name: str, attachments=(), embeds=(), id=1) -> SimpleNamespace:
    return SimpleNamespace(
        id=id, author=SimpleNamespace(display_name=name), attachments=list(attachments), embeds=list(embeds)
    )


TENOR = discord.Embed.from_dict({
    "type": "gifv",
    "url": "https://tenor.com/view/cat-vibing-gif-123",
    "thumbnail": {"url": "https://media.tenor.com/x/cat.png", "proxy_url": "https://images-ext-1.discordapp.net/cat.png"},
})
IMAGE_LINK = discord.Embed.from_dict({
    "type": "image",
    "url": "https://cdn.discordapp.com/attachments/1/2/IMG_8081.jpg?ex=1",
    "thumbnail": {"url": "https://cdn.discordapp.com/attachments/1/2/IMG_8081.jpg", "proxy_url": "https://media.discordapp.net/IMG_8081.jpg"},
})
ARTICLE = discord.Embed.from_dict({
    "type": "article", "title": "News", "url": "https://example.com/a",
    "thumbnail": {"url": "https://example.com/t.png", "proxy_url": "https://images-ext-1.discordapp.net/t.png"},
})


class EmbedTests(unittest.TestCase):
    def test_image_links_and_gifs_use_discords_proxy(self):
        images = _embed_images(message("Xiao", embeds=[IMAGE_LINK, TENOR, ARTICLE]))
        self.assertEqual(images, [
            ("https://media.discordapp.net/IMG_8081.jpg", "IMG_8081.jpg", False),
            ("https://images-ext-1.discordapp.net/cat.png", "cat-vibing-gif-123", True),
        ])


class GifTests(unittest.TestCase):
    def test_animated_gif_becomes_frame_grid(self):
        data, frames = _to_png(gif(8))
        self.assertEqual(frames, 4)
        with Image.open(io.BytesIO(data)) as im:
            self.assertEqual((im.format, im.size), ("PNG", (80, 60)))

    def test_still_gif_stays_one_frame(self):
        data, frames = _to_png(gif(1))
        self.assertEqual(frames, 1)
        with Image.open(io.BytesIO(data)) as im:
            self.assertEqual(im.size, (40, 30))


class FileReaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_attachments_embeds_and_converts_gifs(self):
        reader = FileReader(Mock())
        reader._download = AsyncMock(return_value=(png(), "image/png"))
        ping = message("Bob", attachments=[attachment("party.gif", "image/gif", gif(5))], embeds=[TENOR])
        await reader.read(ping, "in the message that pinged you")
        self.assertEqual([a.mime for a in reader.files.media], ["image/png", "image/png"])
        self.assertIn("4 frames in a grid", reader.files.labels[0])
        self.assertIn("cat-vibing-gif-123, linked by Bob", reader.files.labels[1])
        self.assertIn("one still frame", reader.files.labels[1])

    async def test_recent_messages_only_add_images_and_never_report_skips(self):
        reader = FileReader(Mock())
        old = message("Xiao", attachments=[
            attachment("image.png", "image/png", png()),
            attachment("notes.txt", "text/plain", b"hi"),
            attachment("big.png", "image/png", b"x" * 50 * 1024 * 1024),
        ])
        await reader.read(old, "earlier in the chat", images_only=True)
        self.assertEqual(reader.files.labels, ["image.png, attached by Xiao earlier in the chat"])
        self.assertEqual((reader.files.texts, reader.files.skipped), ([], []))

    async def test_image_cap_skips_the_rest(self):
        with patch("xiap.config.CHAT_MAX_IMAGES", 1):
            reader = FileReader(Mock())
        ping = message("Bob", attachments=[attachment("a.png", "image/png", png()), attachment("b.png", "image/png", png())])
        await reader.read(ping, "in the message that pinged you")
        self.assertEqual(len(reader.files.media), 1)
        self.assertEqual(reader.files.skipped, ["b.png"])


class ReadFilesTests(unittest.IsolatedAsyncioTestCase):
    async def test_ping_images_come_first_then_recent_newest_first(self):
        cog = ChatCog(Mock())
        cog.http = Mock()
        older = message("A", attachments=[attachment("old.png", "image/png", png())], id=10)
        newer = message("B", attachments=[attachment("new.png", "image/png", png())], id=11)
        ctx = SimpleNamespace(limit=30, recent=AsyncMock(return_value=[older, newer]))
        ping = message("C", attachments=[attachment("ping.png", "image/png", png())], id=12)
        ping.content = "what's this"
        files = await cog._read_files(ctx, ping, None)
        self.assertEqual([a.filename for a in files.media], ["ping.png", "new.png", "old.png"])

    async def test_waits_for_embed_on_pasted_link(self):
        cog = ChatCog(Mock())
        cog.http = Mock()
        ctx = SimpleNamespace(limit=0)
        ping = message("C", id=12)
        ping.content = "@xiap https://cdn.discordapp.com/attachments/1/2/IMG_8081.jpg"
        refetched = message("C", embeds=[IMAGE_LINK], id=12)
        ping.channel = SimpleNamespace(fetch_message=AsyncMock(return_value=refetched))
        with patch("xiap.cogs.chat.EMBED_WAIT", 0), \
                patch.object(FileReader, "_download", AsyncMock(return_value=(png(), "image/png"))):
            files = await cog._read_files(ctx, ping, None)
        ping.channel.fetch_message.assert_awaited_once_with(12)
        self.assertEqual([a.filename for a in files.media], ["IMG_8081.jpg"])


if __name__ == "__main__":
    unittest.main()
