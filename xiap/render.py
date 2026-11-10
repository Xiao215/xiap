"""Laying out Word documents as PDF pages, so the model can see what it edits.

Headless LibreOffice does the layout. It isn't Word, but with fonts that take
the same space as Word's (see the Dockerfile) the pages come out close: what
overflows in Word overflows here too. Each layout is a LibreOffice process
using a few hundred MB, so only one runs at a time, and they share one
profile directory (LibreOffice locks it per process).
"""

import asyncio
import io
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from xiap import config

log = logging.getLogger(__name__)

MAC_SOFFICE = "/Applications/LibreOffice.app/Contents/MacOS/soffice"
PROFILE = Path(tempfile.gettempdir()) / "xiap-soffice-profile"
_lock = asyncio.Lock()


class RenderError(Exception):
    """The document couldn't be laid out; the message goes back to the model."""


def soffice() -> str | None:
    """The LibreOffice binary to use, or None if it isn't installed."""
    for exe in (config.SOFFICE, "soffice", "libreoffice"):
        if exe and (found := shutil.which(exe)):
            return found
    return MAC_SOFFICE if not config.SOFFICE and os.path.exists(MAC_SOFFICE) else None


async def to_pdf(docx: bytes) -> bytes:
    """A .docx laid out as a PDF."""
    exe = soffice()
    if exe is None:
        raise RenderError("LibreOffice isn't installed, so pages can't be shown")
    async with _lock:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "document.docx"
            src.write_bytes(docx)
            proc = await asyncio.create_subprocess_exec(
                exe, f"-env:UserInstallation={PROFILE.as_uri()}", "--headless", "--norestore",
                "--nolockcheck", "--convert-to", "pdf", "--outdir", tmp, str(src),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
            try:
                async with asyncio.timeout(config.RENDER_TIMEOUT):
                    _, err = await proc.communicate()
            except TimeoutError:
                raise RenderError("laying out the pages took too long") from None
            finally:
                if proc.returncode is None:  # timed out, or the reply was cancelled
                    proc.kill()
                    await proc.wait()
            out = Path(tmp) / "document.pdf"
            if not out.exists():
                log.warning("LibreOffice failed (%s): %s", proc.returncode, err.decode(errors="replace").strip())
                raise RenderError("LibreOffice couldn't lay the document out")
            return out.read_bytes()


def page_texts(pdf: bytes) -> list[str]:
    """Each page's text, for finding which pages an edit landed on."""
    return [page.extract_text() or "" for page in PdfReader(io.BytesIO(pdf)).pages]


def pick(spec: str | None, total: int, limit: int) -> list[int]:
    """1-based page numbers from a spec like "2", "3-5" or "1,4-6", at most
    `limit` of them; the first `limit` pages when there's no spec."""
    if not spec or not str(spec).strip():
        return list(range(1, min(total, limit) + 1))
    wanted: list[int] = []
    for part in str(spec).replace(" ", "").split(","):
        if not (m := re.fullmatch(r"(\d+)(?:-(\d+))?", part)):
            raise RenderError(f"Pages look like '2', '3-5' or '1,4-6', not {spec!r}.")
        first, last = int(m[1]), int(m[2] or m[1])
        wanted += [n for n in range(first, last + 1) if 1 <= n <= total and n not in wanted]
    if not wanted:
        raise RenderError(f"The document has {total} page{'s' * (total != 1)}.")
    return wanted[:limit]


def only(pdf: bytes, pages: list[int]) -> bytes:
    """The PDF cut down to the given 1-based pages."""
    reader = PdfReader(io.BytesIO(pdf))
    writer = PdfWriter()
    for n in pages:
        writer.add_page(reader.pages[n - 1])
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def spans(pages: list[int]) -> str:
    """[1, 2, 3, 5] -> "1-3, 5"."""
    out: list[str] = []
    for n in pages:
        if out and (m := re.fullmatch(r"(\d+)(?:-(\d+))?", out[-1])) and int(m[2] or m[1]) == n - 1:
            out[-1] = f"{m[1]}-{n}"
        else:
            out.append(str(n))
    return ", ".join(out)
