import io
import unittest
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
from PIL import Image

from xiap.cogs.chat import DOCX, ChatCog, FileReader, Files, _embed_images, _to_png
from xiap.editing import WordFile


def gif(frames: int) -> bytes:
    images = [Image.new("RGB", (40, 30), (i * 60, 0, 0)) for i in range(frames)]
    out = io.BytesIO()
    images[0].save(out, "GIF", save_all=True, append_images=images[1:])
    return out.getvalue()


def png() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (10, 10)).save(out, "PNG")
    return out.getvalue()


def docx(body: str) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("word/document.xml", (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body>{body}<w:sectPr/></w:body></w:document>"
        ))
    return out.getvalue()


REPORT = docx(
    "<w:p><w:r><w:t>Trip </w:t></w:r><w:r><w:t>plan</w:t></w:r></w:p>"
    "<w:p><w:r><w:t>Day 1</w:t><w:tab/><w:t>Hike</w:t><w:br/><w:t>bring water</w:t></w:r></w:p>"
    "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Who</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Pays</w:t></w:r></w:p></w:tc></w:tr>"
    "<w:tr><w:tc><w:p><w:r><w:t>Bob</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>$20</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
)


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

    async def test_recent_messages_add_every_readable_file_and_never_report_skips(self):
        reader = FileReader(Mock())
        old = message("Xiao", attachments=[
            attachment("image.png", "image/png", png()),
            attachment("plan.pdf", "application/pdf", b"%PDF-1.7"),
            attachment("notes.txt", "text/plain", b"hi"),
            attachment("trip.docx", DOCX, REPORT),
            attachment("big.png", "image/png", b"x" * 50 * 1024 * 1024),
            attachment("song.mp3", "audio/mpeg", b"ID3"),
        ])
        await reader.read(old, "earlier in the chat", quiet=True)
        self.assertEqual(reader.files.labels, [
            "image.png, attached by Xiao earlier in the chat",
            "plan.pdf, attached by Xiao earlier in the chat",
        ])
        self.assertEqual(len(reader.files.texts), 2)
        self.assertEqual(reader.files.skipped, [])

    async def test_read_into_adds_only_files_not_taken_yet(self):
        with patch("xiap.config.CHAT_MAX_PDFS", 1):
            reader = FileReader(Mock())
        ping = message("Will", attachments=[attachment("ping.pdf", "application/pdf", b"%PDF-1")], id=1)
        old = message("Amy", attachments=[
            attachment("a.png", "image/png", png()),
            attachment("plan.pdf", "application/pdf", b"%PDF-2"),
        ], id=2)
        await reader.read(ping, "in the message that pinged you")
        await reader.read(old, "earlier in the chat", quiet=True)
        self.assertEqual([a.filename for a in reader.files.media], ["ping.pdf", "a.png"])

        later = Files()
        await reader.read_into(later, old, "in message 2")
        self.assertEqual((later.media, later.skipped), ([], ["plan.pdf"]))  # PDF limit already used
        reader.counts["pdf"] = 0
        await reader.read_into(later, old, "in message 2")
        self.assertEqual([a.filename for a in later.media], ["plan.pdf"])
        self.assertEqual(len(reader.files.media), 2)

    async def test_image_cap_skips_the_rest(self):
        with patch("xiap.config.CHAT_MAX_IMAGES", 1):
            reader = FileReader(Mock())
        ping = message("Bob", attachments=[attachment("a.png", "image/png", png()), attachment("b.png", "image/png", png())])
        await reader.read(ping, "in the message that pinged you")
        self.assertEqual(len(reader.files.media), 1)
        self.assertEqual(reader.files.skipped, ["b.png"])


class DocxTests(unittest.IsolatedAsyncioTestCase):
    def test_paragraphs_tabs_breaks_and_tables(self):
        self.assertEqual(
            WordFile("trip.docx", "Bob", REPORT).view(),
            "[p1] Trip plan\n[p2] Day 1\tHike\nbring water\n[t1.r1] Who | Pays\n[t1.r2] Bob | $20",
        )

    async def test_docx_is_pasted_as_text_and_broken_ones_are_skipped(self):
        reader = FileReader(Mock())
        ping = message("Bob", attachments=[
            attachment("trip.docx", DOCX, REPORT),
            attachment("broken.docx", "application/octet-stream", b"not a zip"),
        ])
        await reader.read(ping, "in the message that pinged you")
        self.assertEqual(len(reader.files.texts), 1)
        self.assertIn("=== trip.docx (attached by Bob) ===\n[p1] Trip plan", reader.files.texts[0])
        self.assertEqual((reader.files.media, reader.files.skipped), ([], ["broken.docx"]))
        self.assertEqual([f.name for f in reader.files.editable], ["trip.docx"])

    async def test_utf8_text_files_are_editable_and_same_names_are_told_apart(self):
        reader = FileReader(Mock())
        ping = message("Bob", attachments=[
            attachment("notes.txt", "text/plain", b"hi"),
            attachment("old.txt", "text/plain", b"caf\xe9"),  # Latin-1: shown, but not editable
        ])
        replied = message("Bob", attachments=[attachment("notes.txt", "text/plain", b"there")], id=2)
        await reader.read(ping, "in the message that pinged you")
        await reader.read(replied, "in the message being replied to")
        self.assertEqual([f.name for f in reader.files.editable], ["notes.txt", "notes (2).txt"])
        self.assertEqual(len(reader.files.texts), 3)
        self.assertIn("=== notes (2).txt (attached by Bob) ===\nthere", reader.files.texts[2])


class ReadFilesTests(unittest.IsolatedAsyncioTestCase):
    async def test_ping_images_come_first_then_recent_newest_first(self):
        cog = ChatCog(Mock())
        cog.http = Mock()
        older = message("A", attachments=[attachment("old.png", "image/png", png())], id=10)
        newer = message("B", attachments=[attachment("new.png", "image/png", png())], id=11)
        ctx = SimpleNamespace(limit=30, recent=AsyncMock(return_value=[older, newer]))
        ping = message("C", attachments=[attachment("ping.png", "image/png", png())], id=12)
        ping.content = "what's this"
        reader = await cog._read_files(ctx, ping, None)
        self.assertEqual([a.filename for a in reader.files.media], ["ping.png", "new.png", "old.png"])

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
            reader = await cog._read_files(ctx, ping, None)
        ping.channel.fetch_message.assert_awaited_once_with(12)
        self.assertEqual([a.filename for a in reader.files.media], ["IMG_8081.jpg"])


if __name__ == "__main__":
    unittest.main()
