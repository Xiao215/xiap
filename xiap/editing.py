"""Editing the text files and Word documents people attach.

The model never rewrites a file. It asks for small edits through tools —
replace this exact text, add a paragraph after p4, bold this phrase — and the
bot applies them to its own copy and attaches the edited copy to the reply.
Edits go to a copy, so when a provider fails halfway, its edits are thrown
away (reset) before the next provider starts over.

Word documents are shown as numbered blocks: [p3] is a paragraph, [t1.r2] a
table row (its cells are t1.r2.c1, t1.r2.c2, …). Ids are handed out once, when
the document is read, so they don't shift as paragraphs come and go. Every
edit is checked as it's made: the paragraph must read exactly as intended
afterwards, or the edit is undone and the model is told why. Before the file
goes out it's saved, read back and compared: every block must read the same,
and every block no edit touched must be byte-for-byte what it was.

Layout can be changed too — table widths, paragraph spacing and size, page
setup, image size — and the model can look at the result: the document is laid
out as PDF pages (xiap.render) that Claude and Gemini see, when it asks and
again in the final check, so it can catch a table running off the page.

Only the main text is editable; headers, footers, footnotes, comments, text
boxes and equations are listed as things the model can't see or change.
Documents with tracked changes are read-only.
"""

import asyncio
import copy
import difflib
import io
import logging
import os
import re
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager

from lxml import etree

from xiap import config, render
from xiap.llm import PDF, Attachment, Output, Tool

log = logging.getLogger(__name__)

DOCUMENT = "word/document.xml"
MAX_XML = 20 * 1024 * 1024  # bigger document.xml (or other part we read) is refused
# view_file results stay under llm.MAX_TOOL_RESULT_CHARS.
VIEW_CHARS = 5000
# A tool result quotes at most this much of an edited paragraph or text.
ECHO_CHARS = 600
REVIEW_CHARS = 6000

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W = f"{{{W_NS}}}"
MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
M = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
# Runs inside these aren't part of their paragraph's own text: text boxes,
# drawings, and the fallback copy of content Word stores twice.
HIDDEN = {W + "txbxContent", W + "drawing", W + "pict", W + "object", MC + "Fallback", W + "del", W + "moveFrom"}
TRACKED = {W + "ins", W + "del", W + "moveFrom", W + "moveTo"}
# Run properties must appear in this order (CT_RPr), or Word calls the file corrupt.
RPR_ORDER = [
    "rStyle", "rFonts", "b", "bCs", "i", "iCs", "caps", "smallCaps", "strike", "dstrike", "outline",
    "shadow", "emboss", "imprint", "noProof", "snapToGrid", "vanish", "webHidden", "color", "spacing",
    "w", "kern", "position", "sz", "szCs", "highlight", "u", "effect", "bdr", "shd", "fitText",
    "vertAlign", "rtl", "cs", "em", "lang", "eastAsianLayout", "specVanish", "oMath",
]
PPR_ORDER = [
    "pStyle", "keepNext", "keepLines", "pageBreakBefore", "framePr", "widowControl", "numPr",
    "suppressLineNumbers", "pBdr", "shd", "tabs", "suppressAutoHyphens", "kinsoku", "wordWrap",
    "overflowPunct", "topLinePunct", "autoSpaceDE", "autoSpaceDN", "bidi", "adjustRightInd", "snapToGrid",
    "spacing", "ind", "contextualSpacing", "mirrorIndents", "suppressOverlap", "jc", "textDirection",
    "textAlignment", "textboxTightWrap", "outlineLvl", "divId", "cnfStyle", "rPr", "sectPr", "pPrChange",
]
TBLPR_ORDER = [
    "tblStyle", "tblpPr", "tblOverlap", "bidiVisual", "tblStyleRowBandSize", "tblStyleColBandSize", "tblW",
    "jc", "tblCellSpacing", "tblInd", "tblBorders", "shd", "tblLayout", "tblCellMar", "tblLook",
    "tblCaption", "tblDescription", "tblPrChange",
]
TCPR_ORDER = [
    "cnfStyle", "tcW", "gridSpan", "hMerge", "vMerge", "tcBorders", "shd", "noWrap", "tcMar",
    "textDirection", "tcFitText", "vAlign", "hideMark", "headers", "cellIns", "cellDel", "cellMerge", "tcPrChange",
]
SECT_ORDER = [
    "headerReference", "footerReference", "footnotePr", "endnotePr", "type", "pgSz", "pgMar", "paperSrc",
    "pgBorders", "lnNumType", "pgNumType", "cols", "formProt", "vAlign", "noEndnote", "titlePg",
    "textDirection", "bidi", "rtlGutter", "docGrid", "printerSettings", "sectPrChange",
]
WP = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
# Lengths: Word measures pages in twips (1/20 pt) and drawings in EMUs.
TWIPS_PER_CM = 1440 / 2.54
EMU_PER_TWIP = 635
PAPER = {"a4": (11906, 16838), "letter": (12240, 15840), "legal": (12240, 20160), "a5": (8391, 11906)}
ALIGN = {"left": "left", "center": "center", "right": "right", "justify": "both"}

# What format_text can switch, and the element (and value for off) for each.
FORMATS = {"bold": ("b", "0"), "italic": ("i", "0"), "underline": ("u", "none"), "strikethrough": ("strike", "0")}
# Styles worth listing even if the document doesn't use them yet.
COMMON_STYLES = ("Normal", "Title", "Subtitle", "Heading 1", "Heading 2", "Heading 3", "List Paragraph", "Quote")
MAX_STYLES_LISTED = 25

# Straight and curly quotes, and non-breaking spaces, match each other when
# nothing matches exactly: models type ' where documents have ’. Each maps to
# one character, so positions are the same in both texts.
LOOSE = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"', " ": " ",
})

PARSER = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)


class EditError(Exception):
    """An edit that can't be made; the message goes back to the model."""


def _find(text: str, old: str) -> list[int]:
    """Where `old` starts in `text`: exact matches, or failing that, loose ones."""
    for hay, needle in ((text, old), (text.translate(LOOSE), old.translate(LOOSE))):
        starts, i = [], hay.find(needle)
        while i != -1:
            starts.append(i)
            i = hay.find(needle, i + 1)
        if starts:
            return starts
    return []


def _short(text: str) -> str:
    return text if len(text) <= ECHO_CHARS else text[:ECHO_CHARS] + "…"


def edited_name(name: str) -> str:
    """report.docx -> report_edited.docx (once: editing it again keeps the name)."""
    stem, ext = os.path.splitext(name)
    return name if stem.endswith("_edited") else f"{stem}_edited{ext}"


# --- Text files -----------------------------------------------------------------


class TextFile:
    """A UTF-8 text file. Raises UnicodeDecodeError for anything else."""

    kind = "text"

    def __init__(self, name: str, author: str, data: bytes) -> None:
        self.name, self.author = name, author
        text = data.decode("utf-8")
        self.eol = "\r\n" if "\r\n" in text else "\n"
        self._original = text.replace("\r\n", "\n")
        self.reset()

    def reset(self) -> None:
        self.text = self._original
        self.version = 0  # edits made; review() looks for new ones
        self.reviewed = 0

    @property
    def changed(self) -> bool:
        return self.text != self._original

    def view(self, start: str | None = None, limit: int | None = None) -> str:
        lines = self.text.split("\n")
        first = 1
        if start:
            if not str(start).strip().isdigit():
                raise EditError("For a text file, start is a line number.")
            first = max(1, min(int(start), len(lines)))
        text = "\n".join(lines[first - 1:])
        if limit and len(text) > limit:
            shown = text[:limit].count("\n")
            text = text[:limit] + f"\n…(more: view_file with start={first + shown})"
        return text

    def replace_text(self, old: str, new: str, block: str | None = None) -> str:
        if block:
            raise EditError("Blocks are only for Word documents; leave block out for text files.")
        old, new = old.replace("\r\n", "\n"), new.replace("\r\n", "\n")
        if not old:
            if self.text:
                raise EditError("old is empty. Copy the exact text you want to replace from the file.")
            start = 0
        else:
            starts = _find(self.text, old)
            if not starts:
                raise EditError(
                    f"That text isn't in {self.name}. Copy it exactly as the file has it now "
                    "(view_file shows the current text)."
                )
            if len(starts) > 1:
                lines = ", ".join(str(self.text.count("\n", 0, s) + 1) for s in starts[:10])
                raise EditError(
                    f"That text appears {len(starts)} times (lines {lines}). Include more of the "
                    "surrounding text so it matches exactly once."
                )
            start = starts[0]
        self.text = self.text[:start] + new + self.text[start + len(old):]
        self.version += 1
        first = self.text.count("\n", 0, start)
        lines = self.text.split("\n")[max(0, first - 1): first + new.count("\n") + 2]
        return f"Done. From line {max(1, first)} it now reads:\n" + _short("\n".join(lines))

    def diff(self) -> str:
        return "\n".join(
            line for line in difflib.unified_diff(
                self._original.split("\n"), self.text.split("\n"), lineterm="", n=1
            ) if not line.startswith(("---", "+++"))
        )

    def save(self) -> bytes:
        return self.text.replace("\n", self.eol).encode("utf-8")


# --- Word documents ----------------------------------------------------------------


def _parse(xml: bytes) -> etree._Element:
    if b"<!DOCTYPE" in xml[:4096]:  # never in a real .docx
        raise ValueError("XML has a DTD")
    return etree.fromstring(xml, PARSER)


def _hidden(el: etree._Element, stop: etree._Element) -> bool:
    """Whether el sits inside a text box, drawing or the like, below `stop`."""
    el = el.getparent()
    while el is not None and el is not stop:
        if el.tag in HIDDEN:
            return True
        el = el.getparent()
    return False


def _unwrap(container: etree._Element, tag: str) -> list[etree._Element]:
    """container's w:<tag> children, including ones Word wrapped in content controls."""
    out = []
    for el in container:
        if el.tag == W + tag:
            out.append(el)
        elif el.tag in (W + "sdt", W + "customXml"):
            inner = el.find(W + "sdtContent") if el.tag == W + "sdt" else el
            if inner is not None:
                out += _unwrap(inner, tag)
    return out


def _rows(tbl: etree._Element) -> list[etree._Element]:
    return _unwrap(tbl, "tr")


def _cells(tr: etree._Element) -> list[etree._Element]:
    return _unwrap(tr, "tc")


def _cell_text(tc: etree._Element) -> str:
    """A cell's paragraphs (not those of tables nested in it), joined by " / "."""
    return " / ".join(_text(p) + _marks(p) for p in _unwrap(tc, "p"))


def _piece_text(child: etree._Element) -> str | None:
    """The text a run's child adds to its paragraph, or None if it adds none."""
    if child.tag == W + "t":
        return child.text or ""
    if child.tag == W + "tab":
        return "\t"
    # Page and column breaks aren't text; they stay wherever they are.
    if child.tag in (W + "br", W + "cr") and child.get(W + "type") in (None, "textWrapping"):
        return "\n"
    return None


def _pieces(p: etree._Element) -> list[tuple[etree._Element, etree._Element, int, int]]:
    """(run, child, start, end) for every piece of text in paragraph p."""
    out, pos = [], 0
    for r in p.iter(W + "r"):
        if _hidden(r, p):
            continue
        for child in r:
            text = _piece_text(child)
            if text is not None:
                out.append((r, child, pos, pos + len(text)))
                pos += len(text)
    return out


def _text(p: etree._Element) -> str:
    return "".join(_piece_text(c) for _, c, _, _ in _pieces(p))


def _w(tag: str, **attrs: str) -> etree._Element:
    el = etree.Element(W + tag)
    for k, v in attrs.items():
        el.set(W + k, v)
    return el


def _t(text: str) -> etree._Element:
    t = _w("t")
    t.text = text
    t.set(XML_SPACE, "preserve")
    return t


def _content(text: str) -> list[etree._Element]:
    """Run children for text: \\t becomes a tab, \\n a line break."""
    out = []
    for part in re.split(r"(\t|\n)", text):
        if part == "\t":
            out.append(_w("tab"))
        elif part == "\n":
            out.append(_w("br"))
        elif part:
            out.append(_t(part))
    return out


def _has_content(r: etree._Element) -> bool:
    return any(c.tag != W + "rPr" for c in r)


def _split(p: etree._Element, k: int) -> None:
    """Make sure a run boundary falls at text offset k of paragraph p."""
    for r, child, start, end in _pieces(p):
        if start < k < end:  # inside a w:t: cut it in two
            rest = _t(child.text[k - start:])
            child.text = child.text[: k - start]
            child.set(XML_SPACE, "preserve")
            child.addnext(rest)
            boundary = rest
            break
        if start == k:
            boundary = child
            break
    else:
        return  # k is the end of the text
    run = boundary.getparent()
    prev = boundary.getprevious()
    if prev is None or prev.tag == W + "rPr":
        return  # already the start of its run
    twin = etree.Element(run.tag, run.attrib)
    if (rpr := run.find(W + "rPr")) is not None:
        twin.append(copy.deepcopy(rpr))
    moving = [boundary, *boundary.itersiblings()]
    run.addnext(twin)
    for c in moving:
        twin.append(c)


def _span_runs(p: etree._Element, a: int, b: int) -> list[etree._Element]:
    """Split p's runs so text [a, b) is exactly a set of whole runs; return them."""
    _split(p, b)
    _split(p, a)
    runs = []
    for r, _, start, end in _pieces(p):
        if a <= start and end <= b and end > start and r not in runs:
            runs.append(r)
    return runs


def _set_prop(parent: etree._Element, name: str, order: list[str], el: etree._Element | None) -> None:
    """Replace (or remove, with el=None) parent's w:<name>, keeping schema order."""
    if (old := parent.find(W + name)) is not None:
        parent.remove(old)
    if el is None:
        return
    rank = order.index(name)
    for i, child in enumerate(parent):
        local = etree.QName(child).localname
        if local in order and order.index(local) > rank or local.endswith("Change"):
            parent.insert(i, el)
            return
    parent.append(el)


def _prop(parent: etree._Element, name: str, order: list[str]) -> etree._Element:
    """parent's w:<name>, added in schema order if it isn't there yet."""
    if (el := parent.find(W + name)) is None:
        _set_prop(parent, name, order, el := _w(name))
    return el


def _props(el: etree._Element, name: str) -> etree._Element:
    """A paragraph's pPr, a table's tblPr or a cell's tcPr: always their first child."""
    if (props := el.find(W + name)) is None:
        props = _w(name)
        el.insert(0, props)
    return props


def _twips(value: str | None, default: int) -> int:
    try:
        return int(float(value)) if value else default
    except ValueError:
        return default


def _cm(twips: float) -> str:
    return f"{twips / TWIPS_PER_CM:.1f}"


def _drawings(el: etree._Element) -> list[etree._Element]:
    """Pictures, charts and shapes in el (not Word's fallback copies), in order."""
    return [d for d in el.iter(W + "drawing") if not _inside(d, MC + "Fallback") and not _inside(d, W + "txbxContent")]


def _drawing_kind(d: etree._Element) -> str:
    tags = {etree.QName(e).localname for e in d.iter(etree.Element)}
    return "chart" if "chart" in tags else "text box" if "txbxContent" in tags else "image" if "pic" in tags else "shape"


def _drawing_size(d: etree._Element) -> tuple[int, int] | None:
    """(width, height) in EMUs."""
    extent = d.find(f"*/{WP}extent")
    if extent is None:
        return None
    return _twips(extent.get("cx"), 0), _twips(extent.get("cy"), 0)


def _marks(p: etree._Element) -> str:
    """What the model sees of the drawings in a paragraph, e.g. ⟦image 1: 15.2×8.0 cm⟧."""
    out = []
    for i, d in enumerate(_drawings(p), 1):
        size = _drawing_size(d)
        dims = f": {size[0] / 360000:.1f}×{size[1] / 360000:.1f} cm" if size else ""
        out.append(f" ⟦{_drawing_kind(d)} {i}{dims}⟧")
    return "".join(out)


def _rpr(r: etree._Element) -> etree._Element:
    if (rpr := r.find(W + "rPr")) is None:
        rpr = _w("rPr")
        r.insert(0, rpr)
    return rpr


def _first_rpr(p: etree._Element) -> etree._Element | None:
    """A copy of the formatting of p's first run, for new text written like it."""
    for r, _, _, _ in _pieces(p):
        if (rpr := r.find(W + "rPr")) is not None:
            return copy.deepcopy(rpr)
        return None
    # An empty paragraph: its paragraph mark's formatting is what typing there gets.
    mark = p.find(f"{W}pPr/{W}rPr")
    if mark is None:
        return None
    rpr = copy.deepcopy(mark)
    for c in list(rpr):
        if c.tag in TRACKED or etree.QName(c).localname.endswith("Change"):
            rpr.remove(c)
    rpr.tag = W + "rPr"
    return rpr


def _run(rpr: etree._Element | None, text: str) -> etree._Element:
    r = _w("r")
    if rpr is not None:
        r.append(copy.deepcopy(rpr))
    for c in _content(text):
        r.append(c)
    return r


def _restore(el: etree._Element, snapshot: etree._Element) -> None:
    """Put el back the way snapshot (a deepcopy of it) was, keeping el itself."""
    el.attrib.clear()
    el.attrib.update(snapshot.attrib)
    el[:] = list(snapshot)


@contextmanager
def _undo_on_error(*els: etree._Element) -> Iterator[None]:
    snapshots = [copy.deepcopy(el) for el in els]
    try:
        yield
    except Exception:
        for el, snapshot in zip(els, snapshots, strict=True):
            _restore(el, snapshot)
        raise


def _style_names(styles: bytes | None) -> tuple[dict[str, str], str | None]:
    """({style id: name} for paragraph styles, the default paragraph style's id)."""
    if not styles:
        return {}, None
    root = _parse(styles)
    names, default = {}, None
    for s in root.iterchildren(W + "style"):
        if s.get(W + "type") != "paragraph" or not (sid := s.get(W + "styleId")):
            continue
        name = s.find(W + "name")
        names[sid] = name.get(W + "val") if name is not None else sid
        if s.get(W + "default") in ("1", "true"):
            default = sid
    # Word stores built-in names in lower case ("heading 1"); show them as Word does.
    for sid, name in names.items():
        if re.fullmatch(r"heading \d|title|subtitle|normal|quote", name):
            names[sid] = name.capitalize()
    return names, default


def _extras(z: zipfile.ZipFile, root: etree._Element) -> list[str]:
    """What's in the document besides the main text: none of it is shown or editable."""
    def part(name: str) -> etree._Element | None:
        try:
            if z.getinfo(name).file_size > MAX_XML:
                return None
            return _parse(z.read(name))
        except (KeyError, ValueError, etree.XMLSyntaxError):
            return None

    def has_text(el: etree._Element | None) -> bool:
        return el is not None and any((t.text or "").strip() for t in el.iter(W + "t"))

    def say(n: int, one: str, many: str) -> None:
        if n:
            found.append(f"{n} {one if n == 1 else many}")

    found: list[str] = []
    names = z.namelist()
    for kind in ("header", "footer"):
        say(sum(has_text(part(n)) for n in names if re.fullmatch(rf"word/{kind}\d*\.xml", n)), kind, kind + "s")
    for kind in ("footnote", "endnote", "comment"):
        el = part(f"word/{kind}s.xml")
        n = 0 if el is None else sum(
            1 for note in el.iterchildren(W + kind) if note.get(W + "type") in (None, "normal")
        )
        say(n, kind, kind + "s")
    body = root.find(W + "body")
    # Word keeps a second, older copy of text boxes and drawings in mc:Fallback.
    say(sum(not _inside(el, MC + "Fallback") for el in body.iter(W + "txbxContent")), "text box", "text boxes")
    # Drawings are shown in the text (⟦image 1: …⟧); old VML ones aren't.
    say(sum(not _inside(el, MC + "Fallback") for el in body.iter(W + "pict")),
        "older-format image or shape", "older-format images or shapes")
    say(sum(1 for _ in body.iter(M + "oMath")), "equation", "equations")
    return found


def _inside(el: etree._Element, tag: str) -> bool:
    return any(a.tag == tag for a in el.iterancestors())


class WordFile:
    """A .docx whose main text can be edited. Raises on anything that isn't a readable .docx."""

    kind = "docx"

    def __init__(self, name: str, author: str, data: bytes) -> None:
        self.name, self.author, self.data = name, author, data
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if z.getinfo(DOCUMENT).file_size > MAX_XML:
                raise ValueError("document.xml is too big")
            self._xml = z.read(DOCUMENT)
            try:
                styles = z.read("word/styles.xml") if z.getinfo("word/styles.xml").file_size <= MAX_XML else None
            except KeyError:
                styles = None
            self.styles, self.default_style = _style_names(styles)
            self.reset()
            self.extras = _extras(z, self.root)
        tracked = any(el.tag in TRACKED or el.tag.endswith("Change") for el in self.body.iter(etree.Element))
        self.readonly = "it has tracked changes; accept or reject them in Word first" if tracked else None

    def reset(self) -> None:
        self.root = _parse(self._xml)
        self.body = self.root.find(W + "body")
        if self.body is None:
            raise ValueError("document.xml has no body")
        self.ids: dict[str, etree._Element] = {}
        self.of: dict[etree._Element, str] = {}
        self.paragraphs = self.tables = 0
        self.rows: dict[str, int] = {}  # table id -> rows numbered so far
        self._index(self.body)
        self.original = {bid: etree.tostring(el, with_tail=False) for bid, el in self.ids.items()}
        self.start = self._lines()
        self.touched: set[str] = set()
        self.notes: list[str] = []  # formatting changes, which the text diff doesn't show
        self.version = 0
        self.reviewed = 0

    @property
    def changed(self) -> bool:
        return self.version > 0

    # --- ids -------------------------------------------------------------------

    def _name(self, bid: str, el: etree._Element) -> None:
        self.ids[bid] = el
        self.of[el] = bid

    def _new_paragraph_id(self, p: etree._Element) -> str:
        self.paragraphs += 1
        self._name(f"p{self.paragraphs}", p)
        return f"p{self.paragraphs}"

    def _index(self, container: etree._Element) -> None:
        for el in container:
            if el.tag == W + "p":
                self._new_paragraph_id(el)
            elif el.tag == W + "tbl":
                self._index_table(el)
            elif el.tag in (W + "sdt", W + "customXml"):  # content controls: index what's inside
                inner = el.find(W + "sdtContent") if el.tag == W + "sdt" else el
                if inner is not None:
                    self._index(inner)

    def _index_table(self, tbl: etree._Element) -> None:
        self.tables += 1
        tid = f"t{self.tables}"
        self._name(tid, tbl)
        self.rows[tid] = 0
        for tr in _rows(tbl):
            self.rows[tid] += 1
            self._name(f"{tid}.r{self.rows[tid]}", tr)
            for tc in _cells(tr):
                for nested in _unwrap(tc, "tbl"):
                    self._index_table(nested)

    def _forget(self, el: etree._Element) -> None:
        for sub in el.iter(etree.Element):
            if (bid := self.of.pop(sub, None)) is not None:
                del self.ids[bid]

    def _touch(self, el: etree._Element, deep: bool = False) -> None:
        """Mark el and the blocks around it (and with deep, inside it) as changed on purpose."""
        for a in (el, *el.iterancestors(), *(el.iter(etree.Element) if deep else ())):
            if (bid := self.of.get(a)) is not None:
                self.touched.add(bid)
        self.version += 1

    def _el(self, block: str) -> etree._Element:
        bid = block.strip().strip("[]").split(" ")[0].lower()
        if m := re.fullmatch(r"(t\d+\.r\d+)\.c(\d+)", bid):
            cells = _cells(self._el(m[1]))
            if not 1 <= int(m[2]) <= len(cells):
                raise EditError(f"Row {m[1]} has {len(cells)} cells, so there's no {bid}.")
            return cells[int(m[2]) - 1]
        if (el := self.ids.get(bid)) is None:
            raise EditError(
                f"There's no block {block!r} (it may have been deleted). "
                "Ids look like p3, t1, t1.r2 or t1.r2.c1."
            )
        return el

    def _label(self, p: etree._Element) -> str:
        """A paragraph's id, or the id of the table cell it's in."""
        if (bid := self.of.get(p)) is not None:
            return bid
        tc = next(p.iterancestors(W + "tc"), None)
        tr = next(tc.iterancestors(W + "tr"), None) if tc is not None else None
        if tr is not None and (row := self.of.get(tr)):
            return f"{row}.c{_cells(tr).index(tc) + 1}"
        return "?"

    def _paragraphs(self, scope: etree._Element | None) -> list[tuple[str, etree._Element]]:
        root = self.body if scope is None else scope
        return [(self._label(p), p) for p in root.iter(W + "p") if not _hidden(p, root)]

    # --- viewing --------------------------------------------------------------------

    def _style(self, p: etree._Element) -> str:
        """A paragraph's style name (blank for the default) and whether it's a list item."""
        parts = []
        sid = p.find(f"{W}pPr/{W}pStyle")
        if sid is not None and sid.get(W + "val") != self.default_style:
            parts.append(self.styles.get(sid.get(W + "val"), sid.get(W + "val")))
        if p.find(f"{W}pPr/{W}numPr") is not None:
            parts.append("list")
        return ", ".join(parts)

    def _lines(self) -> list[tuple[str, str]]:
        """(block id, "[id style] text") for every block, in document order."""
        out: list[tuple[str, str]] = []

        def rows(tbl: etree._Element) -> None:
            for tr in _rows(tbl):
                bid = self.of.get(tr, "?")
                out.append((bid, f"[{bid}] {' | '.join(_cell_text(tc) for tc in _cells(tr))}"))
                for tc in _cells(tr):
                    for nested in _unwrap(tc, "tbl"):
                        rows(nested)

        def walk(container: etree._Element) -> None:
            for el in container:
                if el.tag == W + "p" and el in self.of:
                    style = self._style(el)
                    out.append((self.of[el], f"[{self.of[el]}{' ' + style if style else ''}] {_text(el)}{_marks(el)}"))
                elif el.tag == W + "tbl":
                    rows(el)
                elif el.tag in (W + "sdt", W + "customXml"):
                    inner = el.find(W + "sdtContent") if el.tag == W + "sdt" else el
                    if inner is not None:
                        walk(inner)

        walk(self.body)
        return out

    def _header(self) -> list[str]:
        used = {self._style(p).split(", ")[0] for p in self.body.iter(W + "p")} - {"", "list"}
        names = list(dict.fromkeys(
            [n for n in self.styles.values() if n in used or n in COMMON_STYLES]
            or list(self.styles.values())
        ))[:MAX_STYLES_LISTED]
        header = []
        if names:
            header.append(f"(Paragraph styles: {', '.join(names)})")
        if self.extras:
            header.append(f"(Not shown and can't be edited: {', '.join(self.extras)})")
        if self.readonly:
            header.append(f"(Can't be edited: {self.readonly})")
        return header

    def view(self, start: str | None = None, limit: int | None = None) -> str:
        lines = self._lines()
        if start:
            bid = re.sub(r"\.c\d+$", "", start.strip().strip("[]").split(" ")[0].lower())
            at = next((i for i, (b, _) in enumerate(lines) if b == bid or b.startswith(bid + ".")), None)
            if at is None:
                raise EditError(f"There's no block {start!r}. Ids look like p3, t1 or t1.r2.")
            lines = lines[at:]
        out = [] if start else self._header()
        used = sum(len(line) + 1 for line in out)
        for i, (_, line) in enumerate(lines):
            if limit and used + len(line) > limit and i:
                out.append(f"…(more: view_file with start={lines[i][0]})")
                break
            out.append(line)
            used += len(line) + 1
        return "\n".join(out)

    # --- edits ---------------------------------------------------------------------

    def _writable(self) -> None:
        if self.readonly:
            raise EditError(f"{self.name} can't be edited: {self.readonly}.")

    def _match(self, old: str, block: str | None) -> tuple[str, etree._Element, int]:
        """The one paragraph (label, element) and offset where `old` is."""
        scope = self._el(block) if block else None
        paragraphs = self._paragraphs(scope)
        hits = [(label, p, s) for label, p in paragraphs for s in _find(_text(p), old)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            where = ", ".join(dict.fromkeys(label for label, _, _ in hits))
            raise EditError(
                f"That text appears {len(hits)} times (in {where}). Include more of the surrounding "
                "text, or pass block, so it matches exactly once."
            )
        if block and (elsewhere := [label for label, p in self._paragraphs(None) if _find(_text(p), old)]):
            raise EditError(f"That text isn't in {block}; it's in {', '.join(elsewhere[:5])}.")
        rows = "\n".join(line.split("] ", 1)[1] for _, line in self._lines())
        if _find("\n".join(_text(p) for _, p in paragraphs), old) or _find(rows, old):
            raise EditError(
                "That text runs across more than one paragraph or table cell. Edit each one "
                "separately (insert_paragraphs and delete_block add and remove whole paragraphs)."
            )
        raise EditError(
            f"That text isn't in {block or self.name}. Copy it exactly as the document has it now "
            "(view_file shows the current text)."
        )

    def replace_text(self, old: str, new: str, block: str | None = None) -> str:
        self._writable()
        new = new.replace("\r\n", "\n")
        if not old:
            if not block:
                raise EditError("old is empty. To fill an empty paragraph or cell, pass its block id too.")
            paragraphs = self._paragraphs(self._el(block))
            if not paragraphs or any(_text(p) for _, p in paragraphs):
                raise EditError(f"{block} isn't empty; pass the text to replace as old.")
            label, p = paragraphs[0]
            with _undo_on_error(p):
                if new:
                    rpr = _first_rpr(p)
                    ppr = p.find(W + "pPr")
                    p.insert(0 if ppr is None else 1, _run(rpr, new))
                if _text(p) != new:
                    raise EditError("Couldn't write that cleanly, so nothing was changed.")
            self._touch(p)
            return f"Done. {label} now reads: {_short(new)}"

        label, p, a = self._match(old, block)
        before = _text(p)
        b = a + len(old)
        expected = before[:a] + new + before[b:]
        with _undo_on_error(p):
            runs = _span_runs(p, a, b)
            template = runs[0]
            spot = next(i for i, c in enumerate(template) if _piece_text(c) is not None)
            for r in runs:
                for c in [c for c in r if _piece_text(c) is not None]:
                    r.remove(c)
            for i, c in enumerate(_content(new)):
                template.insert(spot + i, c)
            for r in runs:
                if not _has_content(r):
                    r.getparent().remove(r)
            if _text(p) != expected:
                raise EditError("Couldn't make that edit cleanly, so nothing was changed.")
        self._touch(p)
        return f"Done. {label} now reads: {_short(expected)}"

    def format_text(self, text: str, block: str | None = None, **formats: bool | None) -> str:
        self._writable()
        wanted = {k: v for k, v in formats.items() if v is not None}
        if not wanted:
            raise EditError("Say which formatting to change: bold, italic, underline or strikethrough (true or false).")
        if not text:
            raise EditError("Pass the exact text to format.")
        label, p, a = self._match(text, block)
        before = _text(p)
        with _undo_on_error(p):
            for r in _span_runs(p, a, a + len(text)):
                rpr = _rpr(r)
                for key, on in wanted.items():
                    tag, off = FORMATS[key]
                    value = ("single" if tag == "u" else None) if on else off
                    _set_prop(rpr, tag, RPR_ORDER, _w(tag, **({"val": value} if value else {})))
            if _text(p) != before:
                raise EditError("Couldn't format that cleanly, so nothing was changed.")
        self._touch(p)
        how = ", ".join(f"{'' if on else 'not '}{key}" for key, on in wanted.items())
        self.notes.append(f"{label}: made {_short(text)!r} {how}")
        return f"Done. In {label}, {_short(text)!r} is now {how}."

    def _paragraph(self, block: str) -> etree._Element:
        el = self._el(block)
        if el.tag != W + "p":
            raise EditError(f"{block} isn't a paragraph; paragraph ids look like p3.")
        return el

    def set_style(self, block: str, style: str) -> str:
        self._writable()
        p = self._paragraph(block)
        wanted = style.strip().lower()
        sid = next((sid for sid, name in self.styles.items() if wanted in (sid.lower(), name.lower())), None)
        if sid is None:
            raise EditError(
                f"This document has no paragraph style {style!r}. It has: "
                f"{', '.join(list(self.styles.values())[:40]) or 'none'}."
            )
        with _undo_on_error(p):
            if (ppr := p.find(W + "pPr")) is None:
                ppr = _w("pPr")
                p.insert(0, ppr)
            if (ps := ppr.find(W + "pStyle")) is None:
                ps = _w("pStyle")
                ppr.insert(0, ps)
            ps.set(W + "val", sid)
        self._touch(p)
        return f"Done. {self.of[p]} is now {self.styles[sid]}."

    def insert_paragraphs(self, after: str, text: str, style: str | None = None, like: str | None = None) -> str:
        self._writable()
        where = after.strip().lower()
        if where in ("start", "end"):
            anchor = None
        else:
            anchor = self._el(after)
            if anchor.tag not in (W + "p", W + "tbl") or anchor not in self.of or _inside(anchor, W + "tc"):
                raise EditError("after must be a paragraph id (p3), a table id (t1), 'start' or 'end'.")
        template = self._paragraph(like) if like else anchor if anchor is not None and anchor.tag == W + "p" else None
        ppr = None
        if template is not None and (ppr := template.find(W + "pPr")) is not None:
            ppr = copy.deepcopy(ppr)
            for c in list(ppr):  # never copy a section break or tracked-change marks
                if c.tag == W + "sectPr" or etree.QName(c).localname.endswith("Change"):
                    ppr.remove(c)
        rpr = _first_rpr(template) if template is not None else None

        made = []
        for line in text.replace("\r\n", "\n").split("\n"):
            p = _w("p")
            if ppr is not None:
                p.append(copy.deepcopy(ppr))
            if line:
                p.append(_run(rpr, line))
            made.append(p)
        if where == "start":
            for i, p in enumerate(made):
                self.body.insert(i, p)
        elif where == "end":
            last = self.body[-1] if len(self.body) else None
            for p in made:
                if last is not None and last.tag == W + "sectPr":
                    last.addprevious(p)
                else:
                    self.body.append(p)
        else:
            spot = anchor
            for p in made:
                spot.addnext(p)
                spot = p
        ids = [self._new_paragraph_id(p) for p in made]
        try:
            if style:
                for bid in ids:
                    self.set_style(bid, style)
        except EditError:
            for p in made:
                self._forget(p)
                p.getparent().remove(p)
            raise
        for p, line in zip(made, text.replace("\r\n", "\n").split("\n"), strict=True):
            self._touch(p)
            if _text(p) != line:  # can't happen unless _content is broken
                raise EditError("Couldn't write those paragraphs cleanly.")
        shown = ", ".join(ids) if len(ids) <= 5 else f"{ids[0]}–{ids[-1]}"
        return f"Done. Added {shown}:\n" + self.view(ids[0], limit=ECHO_CHARS)

    def insert_row(self, after: str, cells: list[str]) -> str:
        self._writable()
        row = self._el(after)
        if row.tag != W + "tr":
            raise EditError("after must be a table row id, like t1.r2.")
        tbl = next(row.iterancestors(W + "tbl"))
        tid = self.of[tbl]
        rows = _rows(tbl)
        # A header row (the first of several) isn't a good model for a data row.
        template = rows[1] if row is rows[0] and len(rows) > 1 else row
        n = len(_cells(template))
        if not isinstance(cells, list) or len(cells) != n:
            copied = "" if template is row else f"New rows copy {self.of[template]}, which has {n} cells; "
            raise EditError(
                f"{copied or f'That row has {n} cells; '}pass exactly {n} values (\"\" for an empty cell)."
            )
        values = [str(v).replace("\r\n", "\n") for v in cells]
        new = copy.deepcopy(template)
        if (trpr := new.find(W + "trPr")) is not None:
            for c in trpr.findall(W + "tblHeader"):
                trpr.remove(c)
        for tc, value in zip(_cells(new), values, strict=True):
            if (tcpr := tc.find(W + "tcPr")) is not None:
                for c in tcpr.findall(W + "vMerge"):  # don't merge into the cell above
                    tcpr.remove(c)
            paragraphs = _unwrap(tc, "p")
            keep = paragraphs[0] if paragraphs else _w("p")
            rpr = _first_rpr(keep) if paragraphs else None
            for c in list(tc):
                if c.tag != W + "tcPr":
                    tc.remove(c)
            for c in list(keep):
                if c.tag != W + "pPr":
                    keep.remove(c)
            if value:
                keep.append(_run(rpr, value))
            tc.append(keep)
        row.addnext(new)
        self.rows[tid] += 1
        bid = f"{tid}.r{self.rows[tid]}"
        self._name(bid, new)
        if [_cell_text(tc) for tc in _cells(new)] != values:
            self._forget(new)
            new.getparent().remove(new)
            raise EditError("Couldn't write that row cleanly, so nothing was changed.")
        self._touch(new)
        return f"Done. Added row {bid}:\n" + self.view(bid, limit=ECHO_CHARS).split("\n")[0]

    def delete_block(self, block: str) -> str:
        self._writable()
        el = self._el(block)
        bid = self.of.get(el)
        if el.tag == W + "tc" or bid is None:
            raise EditError("You can delete a paragraph (p3), a table row (t1.r2) or a table (t1), not a single cell.")
        if el.tag == W + "p" and el.find(f"{W}pPr/{W}sectPr") is not None:
            raise EditError(
                f"{bid} ends a section of the document (it holds page layout), so it can't be deleted. "
                "Clear its text with replace_text instead."
            )
        if el.tag == W + "tr" and len(_rows(tbl := next(el.iterancestors(W + "tbl")))) == 1:
            el = tbl  # the last row goes with its table
        parent = el.getparent()
        images = sum(1 for d in el.iter(W + "drawing", W + "pict") if not _inside(d, MC + "Fallback"))
        self._touch(parent)
        self._forget(el)
        parent.remove(el)
        cell = next((a for a in (parent, *parent.iterancestors()) if a.tag == W + "tc"), None)
        if cell is not None and not _unwrap(cell, "p"):
            cell.append(_w("p"))  # a cell must hold a paragraph
        blocks = [c for c in self.body if c.tag != W + "sectPr"]
        if blocks and blocks[-1].tag == W + "tbl":  # Word wants a paragraph after a closing table
            p = _w("p")
            blocks[-1].addnext(p)
            self._new_paragraph_id(p)
        extra = f" (it also held {images} image{'s' * (images > 1)}, now gone)" if images else ""
        return f"Done. Deleted {block.strip()}{extra}."

    # --- layout -------------------------------------------------------------------

    def _section(self, el: etree._Element) -> etree._Element | None:
        """The page setup (sectPr) that applies to el: the next one after it."""
        top = el
        while top.getparent() is not None and top.getparent() is not self.body:
            top = top.getparent()
        for sib in (top, *top.itersiblings()):
            if sib.tag == W + "sectPr":
                return sib
            if sib.tag == W + "p" and (sect := sib.find(f"{W}pPr/{W}sectPr")) is not None:
                return sect
        return self.body.find(W + "sectPr")

    def _room(self, el: etree._Element) -> int:
        """How wide el can be, in twips: its table cell's width, or the page's between the margins."""
        tc = next(el.iterancestors(W + "tc"), None)
        w = tc.find(f"{W}tcPr/{W}tcW") if tc is not None else None
        if w is not None and w.get(W + "type") == "dxa" and (width := _twips(w.get(W + "w"), 0)):
            return width
        sect = self._section(el)
        size = sect.find(W + "pgSz") if sect is not None else None
        mar = sect.find(W + "pgMar") if sect is not None else None
        width = _twips(size.get(W + "w") if size is not None else None, 12240)
        if mar is None:
            return width - 2 * 1440
        return width - sum(_twips(mar.get(W + side), d) for side, d in (("left", 1440), ("right", 1440), ("gutter", 0)))

    def _block_paragraphs(self, spec: str) -> list[tuple[str, etree._Element]]:
        """Paragraphs picked by a spec like "p3", "p3-p9", "t1", "t1.r2.c1", "all" or a mix ("p2, p5-p7")."""
        order = [bid for bid, _ in self._lines()]
        found: list[tuple[str, etree._Element]] = []
        for part in [x.strip().lower() for x in str(spec).split(",") if x.strip()]:
            if part == "all":
                found += self._paragraphs(None)
            elif m := re.fullmatch(r"(p\d+)\s*-\s*(p\d+)", part):
                for bid in m.groups():
                    self._el(bid)  # unknown ids raise
                i, j = sorted((order.index(m[1]), order.index(m[2])))
                for bid in order[i: j + 1]:
                    found += self._paragraphs(self.ids[bid])
            else:
                found += self._paragraphs(self._el(part))
        seen: set[etree._Element] = set()
        found = [(label, p) for label, p in found if not (p in seen or seen.add(p))]
        if not found:
            raise EditError(f"There are no paragraphs in {spec!r}.")
        return found

    @staticmethod
    def _where(targets: list[tuple[str, etree._Element]]) -> str:
        labels = list(dict.fromkeys(label for label, _ in targets))
        return ", ".join(labels) if len(labels) <= 4 else f"{labels[0]}–{labels[-1]} ({len(labels)} blocks)"

    def format_paragraphs(
        self, blocks: str, size: float | None = None, space_before: float | None = None,
        space_after: float | None = None, line_spacing: float | None = None, align: str | None = None,
        indent_left: float | None = None, first_line_indent: float | None = None,
        page_break_before: bool | None = None, keep_with_next: bool | None = None,
    ) -> str:
        self._writable()
        if align is not None and align.lower() not in ALIGN:
            raise EditError(f"align is one of {', '.join(ALIGN)}.")
        if size is not None and not 1 <= float(size) <= 400:
            raise EditError("size is in points, between 1 and 400.")
        what = []
        for key, value, unit in (
            ("size", size, " pt"), ("space before", space_before, " pt"), ("space after", space_after, " pt"),
            ("line spacing", line_spacing, "×"), ("align", align, ""), ("left indent", indent_left, " cm"),
            ("first-line indent", first_line_indent, " cm"), ("page break before", page_break_before, ""),
            ("keep with next", keep_with_next, ""),
        ):
            if value is not None:
                what.append(f"{key} {str(value).lower()}{unit}")
        if not what:
            raise EditError("Say what to change: size, space_before, space_after, line_spacing, align, indents or page breaks.")
        targets = self._block_paragraphs(blocks)
        paragraphs = [p for _, p in targets]
        before = [_text(p) for p in paragraphs]

        def switch(ppr: etree._Element, name: str, on: bool) -> None:
            _set_prop(ppr, name, PPR_ORDER, _w(name) if on else _w(name, val="0"))

        with _undo_on_error(*paragraphs):
            for p in paragraphs:
                ppr = _props(p, "pPr")
                if space_before is not None or space_after is not None or line_spacing is not None:
                    spacing = _prop(ppr, "spacing", PPR_ORDER)
                    for side, value in (("before", space_before), ("after", space_after)):
                        if value is not None:
                            spacing.set(W + side, str(round(float(value) * 20)))
                            spacing.attrib.pop(f"{W}{side}Autospacing", None)  # it would override the value
                            spacing.attrib.pop(f"{W}{side}Lines", None)
                    if line_spacing is not None:
                        spacing.set(W + "line", str(round(float(line_spacing) * 240)))
                        spacing.set(W + "lineRule", "auto")
                if indent_left is not None or first_line_indent is not None:
                    ind = _prop(ppr, "ind", PPR_ORDER)
                    if indent_left is not None:
                        ind.attrib.pop(W + "start", None)
                        ind.set(W + "left", str(round(float(indent_left) * TWIPS_PER_CM)))
                    if first_line_indent is not None:
                        for key in ("firstLine", "hanging", "firstLineChars", "hangingChars"):
                            ind.attrib.pop(W + key, None)
                        value = round(float(first_line_indent) * TWIPS_PER_CM)
                        ind.set(W + ("firstLine" if value >= 0 else "hanging"), str(abs(value)))
                if align is not None:
                    _prop(ppr, "jc", PPR_ORDER).set(W + "val", ALIGN[align.lower()])
                if page_break_before is not None:
                    switch(ppr, "pageBreakBefore", page_break_before)
                if keep_with_next is not None:
                    switch(ppr, "keepNext", keep_with_next)
                if size is not None:
                    half_points = str(round(float(size) * 2))
                    rprs = [_rpr(r) for r in p.iter(W + "r") if not _hidden(r, p)]
                    for rpr in [*rprs, _prop(ppr, "rPr", PPR_ORDER)]:  # the runs, and the paragraph mark
                        _prop(rpr, "sz", RPR_ORDER).set(W + "val", half_points)
                        _prop(rpr, "szCs", RPR_ORDER).set(W + "val", half_points)
            if [_text(p) for p in paragraphs] != before:
                raise EditError("Couldn't change that cleanly, so nothing was changed.")
        for p in paragraphs:
            self._touch(p)
        where = self._where(targets)
        self.notes.append(f"{where}: {', '.join(what)}")
        return f"Done. {where}: {', '.join(what)}."

    def set_table_layout(self, table: str, widths: list[float] | None = None) -> str:
        """Fit a table to the page (or its cell), keeping its column proportions or using `widths`."""
        self._writable()
        tbl = self._el(table)
        if tbl.tag != W + "tbl":
            raise EditError(f"{table} isn't a table; table ids look like t1.")
        grid = tbl.find(W + "tblGrid")
        cols = grid.findall(W + "gridCol") if grid is not None else []
        if not cols:
            raise EditError(f"{table} has no column grid, so its widths can't be set.")
        room = self._room(tbl)
        if widths:
            if not isinstance(widths, list) or len(widths) != len(cols):
                raise EditError(
                    f"{table} has {len(cols)} columns; pass {len(cols)} widths, as percentages of the "
                    f"{_cm(room)} cm there is room for."
                )
            weights = [float(w) for w in widths]
            if min(weights) <= 0:
                raise EditError("Widths must be more than 0.")
        else:
            weights = [_twips(c.get(W + "w"), 0) for c in cols]
            if min(weights) <= 0:
                weights = [1.0] * len(cols)
        new = [round(room * w / sum(weights)) for w in weights]
        rows_before = [_cell_text(tc) for tr in _rows(tbl) for tc in _cells(tr)]
        with _undo_on_error(tbl):
            for col, width in zip(cols, new, strict=True):
                col.set(W + "w", str(width))
            tblpr = _props(tbl, "tblPr")
            tbl_w = _prop(tblpr, "tblW", TBLPR_ORDER)
            tbl_w.set(W + "w", str(sum(new)))
            tbl_w.set(W + "type", "dxa")
            if (ind := tblpr.find(W + "tblInd")) is not None:  # an indent pushes it past the margin
                ind.set(W + "w", "0")
                ind.set(W + "type", "dxa")
            _prop(tblpr, "tblLayout", TBLPR_ORDER).set(W + "type", "fixed")  # don't let content widen it
            for tr in _rows(tbl):
                before = tr.find(f"{W}trPr/{W}gridBefore")
                at = _twips(before.get(W + "val"), 0) if before is not None else 0
                for tc in _cells(tr):
                    span_el = tc.find(f"{W}tcPr/{W}gridSpan")
                    span = _twips(span_el.get(W + "val"), 1) if span_el is not None else 1
                    tc_w = _prop(_props(tc, "tcPr"), "tcW", TCPR_ORDER)
                    tc_w.set(W + "w", str(sum(new[at: at + span]) or new[-1]))
                    tc_w.set(W + "type", "dxa")
                    at += span
            if [_cell_text(tc) for tr in _rows(tbl) for tc in _cells(tr)] != rows_before:
                raise EditError("Couldn't change that cleanly, so nothing was changed.")
        self._touch(tbl, deep=True)
        cm = " / ".join(_cm(w) for w in new)
        self.notes.append(f"{table}: {_cm(sum(new))} cm wide, columns {cm} cm")
        return f"Done. {table} is now {_cm(sum(new))} cm wide (all the room there is), columns {cm} cm."

    def set_page(
        self, orientation: str | None = None, size: str | None = None, margins: list[float] | None = None
    ) -> str:
        self._writable()
        if orientation is not None and orientation.lower() not in ("portrait", "landscape"):
            raise EditError("orientation is portrait or landscape.")
        if size is not None and size.lower() not in PAPER:
            raise EditError(f"size is one of {', '.join(PAPER)}.")
        if margins is not None:
            margins = [margins] if isinstance(margins, (int, float)) else list(margins)
            if len(margins) not in (1, 2, 4) or min(float(m) for m in margins) < 0:
                raise EditError("margins: 1 number (cm) for all sides, 2 (top/bottom, left/right) or 4 (top, right, bottom, left).")
            top, right, bottom, left = (margins * 4)[:4] if len(margins) == 1 else (
                [margins[0], margins[1]] * 2 if len(margins) == 2 else margins
            )
        if orientation is None and size is None and margins is None:
            raise EditError("Say what to change: orientation, size or margins.")
        sections = list(self.body.iter(W + "sectPr"))
        if not sections:
            self.body.append(sections := _w("sectPr"))
            sections = [sections]
        for sect in sections:
            page = _prop(sect, "pgSz", SECT_ORDER)
            w, h = _twips(page.get(W + "w"), 12240), _twips(page.get(W + "h"), 15840)
            landscape = w > h if orientation is None else orientation.lower() == "landscape"
            if size is not None:
                w, h = PAPER[size.lower()]
            w, h = (max(w, h), min(w, h)) if landscape else (min(w, h), max(w, h))
            page.set(W + "w", str(w))
            page.set(W + "h", str(h))
            if landscape:
                page.set(W + "orient", "landscape")
            else:
                page.attrib.pop(W + "orient", None)
            if margins is not None:
                mar = _prop(sect, "pgMar", SECT_ORDER)
                for side, cm in (("top", top), ("right", right), ("bottom", bottom), ("left", left)):
                    mar.set(W + side, str(round(float(cm) * TWIPS_PER_CM)))
                for side, default in (("header", "720"), ("footer", "720"), ("gutter", "0")):
                    mar.set(W + side, mar.get(W + side) or default)  # Word requires all seven
            owner = sect.getparent()
            self._touch(owner.getparent() if owner.tag == W + "pPr" else sect)
        what = [x for x in (
            orientation and orientation.lower(), size and size.upper() if size and size.lower() != "letter" else size,
            margins is not None and f"margins {top}/{right}/{bottom}/{left} cm (top/right/bottom/left)",
        ) if x]
        self.notes.append(f"page: {', '.join(what)}")
        return f"Done. Pages are now {', '.join(what)}."

    def resize_image(self, block: str, width_cm: float | None = None, image: int | None = None) -> str:
        """Set an image's width (keeping its shape), or without width_cm shrink it to fit."""
        self._writable()
        el = self._el(block)
        drawings = _drawings(el)
        if not drawings:
            raise EditError(f"{block} has no images.")
        if image is not None:
            if not 1 <= int(image) <= len(drawings):
                raise EditError(f"{block} has {len(drawings)} image{'s' * (len(drawings) > 1)}.")
            drawings = [drawings[int(image) - 1]]
        paragraphs = list(dict.fromkeys(next(d.iterancestors(W + "p")) for d in drawings))
        done = []
        with _undo_on_error(*paragraphs):
            for d in drawings:
                if (size := _drawing_size(d)) is None or not size[0]:
                    continue
                cx, cy = size
                room = self._room(d) * EMU_PER_TWIP
                target = round(float(width_cm) * 360000) if width_cm else min(cx, room)
                ratio = target / cx
                extent = d.find(f"*/{WP}extent")
                extent.set("cx", str(target))
                extent.set("cy", str(round(cy * ratio)))
                for ext in d.iterfind(f".//{A}xfrm/{A}ext"):
                    ext.set("cx", str(round(_twips(ext.get("cx"), 0) * ratio)))
                    ext.set("cy", str(round(_twips(ext.get("cy"), 0) * ratio)))
                done.append(f"{target / 360000:.1f}×{cy * ratio / 360000:.1f} cm")
        for p in paragraphs:
            self._touch(p)
        if not done:
            raise EditError(f"Couldn't read the size of the image in {block}.")
        self.notes.append(f"{block}: image now {', '.join(done)}")
        return f"Done. In {block}, the image is now {', '.join(done)}."

    # --- seeing the pages -------------------------------------------------------------

    def _pages(self, pdf: bytes, pages: list[int], total: int, why: str = "") -> Output:
        more = f" Ask for pages='{pages[-1] + 1}-{min(total, pages[-1] + config.CHAT_RENDER_PAGES)}' to see more." if pages[-1] < total else ""
        shown = render.spans(pages)
        return Output(
            f"{self.name} as it is now{why}: page{'s' * (len(pages) > 1)} {shown} of {total}, laid out "
            f"by LibreOffice (very close to Word, not identical).{more}",
            [Attachment(PDF, render.only(pdf, pages), f"{os.path.splitext(self.name)[0]} p{shown}.pdf")],
        )

    async def render(self, pages: str | None = None) -> Output:
        pdf = await render.to_pdf(await asyncio.to_thread(self._pack))
        total = len(render.page_texts(pdf))
        return self._pages(pdf, render.pick(pages, total, config.CHAT_RENDER_PAGES), total)

    async def changed_pages(self) -> Output | None:
        """The pages the edits landed on (found by their text), for the final check."""
        try:
            pdf = await render.to_pdf(await asyncio.to_thread(self._pack))
        except render.RenderError as e:
            return Output(f"(Couldn't lay {self.name} out to show you the pages: {e}.)")
        texts = [re.sub(r"\s+", "", t) for t in await asyncio.to_thread(render.page_texts, pdf)]
        found = []
        for bid, line in self._lines():
            if bid not in self.touched:
                continue
            sample = re.sub(r"\s+|⟦[^⟧]*⟧", "", line.split("] ", 1)[1].replace(" | ", ""))[:30]
            if len(sample) >= 6 and (page := next((n for n, t in enumerate(texts, 1) if sample in t), None)):
                found.append(page)
        pages = sorted(set(found))[: config.CHAT_RENDER_PAGES] or render.pick(None, len(texts), config.CHAT_RENDER_PAGES)
        return self._pages(pdf, pages, len(texts), " after your edits")

    # --- checking and saving ------------------------------------------------------

    def diff(self) -> str:
        lines = [
            line for line in difflib.unified_diff(
                [line for _, line in self.start], [line for _, line in self._lines()], lineterm="", n=0
            ) if not line.startswith(("---", "+++", "@@"))
        ]
        return "\n".join(lines + [f"* {note}" for note in self.notes])

    def _pack(self) -> bytes:
        """The document as it is now, as .docx bytes (unchecked: for laying out)."""
        xml = etree.tostring(self.root, xml_declaration=True, encoding="UTF-8", standalone=True)
        out = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(self.data)) as src, zipfile.ZipFile(out, "w") as dst:
            for info in src.infolist():
                # writestr updates the ZipInfo it's given; keep src's own intact.
                dst.writestr(copy.copy(info), xml if info.filename == DOCUMENT else src.read(info))
        return out.getvalue()

    def save(self) -> bytes:
        data = self._pack()
        self._check(data)
        return data

    def _check(self, data: bytes) -> None:
        """Raise unless `data` reads back as this document, untouched blocks unchanged."""
        for bid, xml in self.original.items():
            if bid not in self.touched and bid in self.ids and etree.tostring(self.ids[bid], with_tail=False) != xml:
                raise ValueError(f"{bid} changed without being edited")
        again = WordFile(self.name, self.author, data)
        if [line.split("] ", 1)[1] for _, line in again._lines()] != [
            line.split("] ", 1)[1] for _, line in self._lines()
        ]:
            raise ValueError("the saved document doesn't read back the same")
        with zipfile.ZipFile(io.BytesIO(self.data)) as a, zipfile.ZipFile(io.BytesIO(data)) as b:
            if a.namelist() != b.namelist():
                raise ValueError("the saved document lost or gained parts")


# --- The edit tools -----------------------------------------------------------------

REVIEW = """\
Before your reply goes out, check your edits. Everything that changed in the files so far \
("-" was there before, "+" is there now, "*" is a formatting change):

{diffs}
{pages}
If anything is wrong, missing or changed by accident, fix it now with the tools. Otherwise \
write your reply again (your draft above isn't sent): say briefly what you changed — the \
edited file is attached to your reply automatically — and say plainly if any part of the \
request couldn't be done."""


class EditSession:
    """The editable files around one ping and the tools to edit them.

    Doubles as the agent's workspace: reset() before each provider's attempt,
    review() when the model is about to answer."""

    def __init__(self, files: list[TextFile | WordFile]) -> None:
        self.files = {f.name: f for f in files}

    def reset(self) -> None:
        for f in self.files.values():
            f.reset()

    @property
    def visual(self) -> bool:
        """Whether the model can be shown pages: a Word document, and LibreOffice to lay it out."""
        return any(isinstance(f, WordFile) for f in self.files.values()) and render.soffice() is not None

    async def review(self) -> Output | None:
        """The diff to check, and the pages the edits landed on, if anything
        changed since the last review."""
        if not any(f.version > f.reviewed for f in self.files.values()):
            return None
        diffs, notes, attachments = [], [], []
        for f in self.files.values():
            fresh = f.version > f.reviewed
            f.reviewed = f.version
            if not f.changed:
                continue
            diffs.append(f"=== {f.name} ===\n{f.diff() or '(no change in the end)'}")
            if fresh and isinstance(f, WordFile) and self.visual and (pages := await f.changed_pages()):
                notes.append(pages.text)
                attachments += pages.attachments
        text = "\n\n".join(diffs)
        if len(text) > REVIEW_CHARS:
            text = text[:REVIEW_CHARS] + "\n…(cut off; view_file shows the files in full)"
        if attachments:
            notes.append(
                "Look at these pages too: nothing should run off the page or overlap, and spacing, "
                "sizes and alignment should look even and deliberate."
            )
        pages = "\n" + "\n".join(notes) + "\n" if notes else ""
        return Output(REVIEW.format(diffs=text, pages=pages), attachments)

    async def outputs(self) -> tuple[list[tuple[str, bytes]], list[str]]:
        """(edited name, data) for every edited file, and names of ones that failed to save."""
        saved, failed = [], []
        for f in self.files.values():
            if not f.changed:
                continue
            try:
                saved.append((edited_name(f.name), await asyncio.to_thread(f.save)))
            except Exception as e:  # noqa: BLE001 — a bad save must not lose the reply
                log.error("Couldn't save edited %s: %s", f.name, e)
                failed.append(f.name)
        return saved, failed

    def _file(self, name: str) -> TextFile | WordFile:
        if (f := self.files.get(name)) is None:
            raise EditError(f"There's no editable file {name!r}; the files are: {', '.join(self.files)}.")
        return f

    def _word(self, name: str) -> WordFile:
        f = self._file(name)
        if not isinstance(f, WordFile):
            raise EditError(f"{name} is a text file: use replace_text to change it.")
        return f

    def tools(self) -> list[Tool]:
        file = {"type": "string", "enum": list(self.files), "description": "Which attached file"}
        block = {"type": "string", "description": "Word documents only: a block id (p3, t1.r2, t1.r2.c1) to look in"}

        async def replace_text(file: str, old: str, new: str, block: str | None = None) -> str:
            return self._file(file).replace_text(old, new, block or None)

        async def view_file(file: str, start: str | int | None = None) -> str:
            return self._file(file).view(str(start) if start not in (None, "") else None, limit=VIEW_CHARS)

        tools = [
            Tool(
                name="replace_text",
                description=(
                    "Edit an attached file by replacing text. `old` must be copied exactly from the "
                    "file's current text and match exactly once (within `block`, if given): include "
                    "a few words around it to make it unique. In a Word document it must lie inside "
                    "one paragraph or table cell; to fill an empty paragraph or cell, pass its block "
                    "id and an empty `old`. `new` takes its place (empty to delete it) and keeps its "
                    "formatting; \\n is a new line (a line break in Word) and \\t a tab."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "file": file,
                        "old": {"type": "string", "description": "The exact current text to replace"},
                        "new": {"type": "string", "description": "What to put there instead"},
                        "block": block,
                    },
                    "required": ["file", "old", "new"],
                },
                run=replace_text,
            ),
            Tool(
                name="view_file",
                description=(
                    "Show an attached file as it is now, with your edits so far. `start` is where to "
                    "begin: a block id in a Word document, a line number in a text file."
                ),
                parameters={
                    "type": "object",
                    "properties": {"file": file, "start": {"type": "string", "description": "Block id or line number"}},
                    "required": ["file"],
                },
                run=view_file,
            ),
        ]
        if not any(isinstance(f, WordFile) for f in self.files.values()):
            return tools

        async def insert_paragraphs(
            file: str, after: str, text: str, style: str | None = None, like: str | None = None
        ) -> str:
            return self._word(file).insert_paragraphs(after, text, style or None, like or None)

        async def insert_row(file: str, after: str, cells: list[str]) -> str:
            return self._word(file).insert_row(after, cells)

        async def delete_block(file: str, block: str) -> str:
            return self._word(file).delete_block(block)

        async def set_style(file: str, block: str, style: str) -> str:
            return self._word(file).set_style(block, style)

        async def format_text(
            file: str, text: str, block: str | None = None, bold: bool | None = None,
            italic: bool | None = None, underline: bool | None = None, strikethrough: bool | None = None,
        ) -> str:
            return self._word(file).format_text(
                text, block or None, bold=bold, italic=italic, underline=underline, strikethrough=strikethrough
            )

        word = {"type": "string", "enum": [n for n, f in self.files.items() if isinstance(f, WordFile)],
                "description": "Which attached Word document"}
        switch = {"type": "boolean", "description": "true to turn on, false to turn off; leave out to keep"}
        tools += [
            Tool(
                name="insert_paragraphs",
                description=(
                    "Word documents: add paragraphs after a block (a paragraph id like p4, or a table "
                    "id like t1), or at the 'start' or 'end'. Each line of `text` becomes a paragraph. "
                    "They copy the formatting of the paragraph they follow; after a heading or list "
                    "item, pass `like` (a paragraph whose look you want) or `style` instead."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "file": word,
                        "after": {"type": "string", "description": "p4, t1, 'start' or 'end'"},
                        "text": {"type": "string", "description": "The new paragraphs, one per line"},
                        "style": {"type": "string", "description": "A paragraph style the document has, e.g. 'Heading 2'"},
                        "like": {"type": "string", "description": "Copy the formatting of this paragraph id"},
                    },
                    "required": ["file", "after", "text"],
                },
                run=insert_paragraphs,
            ),
            Tool(
                name="insert_row",
                description=(
                    "Word documents: add a table row after row `after` (e.g. t1.r3), one value per "
                    "cell. It copies that row's formatting (the second row's, after a header row)."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "file": word,
                        "after": {"type": "string", "description": "A row id, e.g. t1.r3"},
                        "cells": {"type": "array", "items": {"type": "string"}, "description": "One value per cell"},
                    },
                    "required": ["file", "after", "cells"],
                },
                run=insert_row,
            ),
            Tool(
                name="delete_block",
                description="Word documents: delete a paragraph (p4), a table row (t1.r2) or a whole table (t1).",
                parameters={
                    "type": "object",
                    "properties": {"file": word, "block": {"type": "string", "description": "p4, t1.r2 or t1"}},
                    "required": ["file", "block"],
                },
                run=delete_block,
            ),
            Tool(
                name="set_style",
                description=(
                    "Word documents: change a paragraph's style, e.g. to 'Heading 2' or 'Normal'. Only "
                    "styles the document has work; they're listed at the top of the document."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "file": word,
                        "block": {"type": "string", "description": "A paragraph id, e.g. p4"},
                        "style": {"type": "string", "description": "The style's name"},
                    },
                    "required": ["file", "block", "style"],
                },
                run=set_style,
            ),
            Tool(
                name="format_text",
                description=(
                    "Word documents: make text bold, italic, underlined or struck through (true), or "
                    "not (false). `text` must match exactly once (within `block`, if given), inside "
                    "one paragraph or table cell."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "file": word,
                        "text": {"type": "string", "description": "The exact text to format"},
                        "block": block,
                        "bold": switch, "italic": switch, "underline": switch, "strikethrough": switch,
                    },
                    "required": ["file", "text"],
                },
                run=format_text,
            ),
        ]

        async def format_paragraphs(file: str, blocks: str, **changes: float | str | bool | None) -> str:
            return self._word(file).format_paragraphs(blocks, **{k: v for k, v in changes.items() if v is not None})

        async def set_table_layout(file: str, table: str, widths: list[float] | None = None) -> str:
            return self._word(file).set_table_layout(table, widths or None)

        async def set_page(
            file: str, orientation: str | None = None, size: str | None = None, margins: list[float] | None = None
        ) -> str:
            return self._word(file).set_page(orientation or None, size or None, margins)

        async def resize_image(file: str, block: str, width_cm: float | None = None, image: int | None = None) -> str:
            return self._word(file).resize_image(block, width_cm, image)

        number = {"type": "number"}
        tools += [
            Tool(
                name="format_paragraphs",
                description=(
                    "Word documents: change how paragraphs look. `blocks` picks them: a paragraph "
                    "(p4), a range (p4-p9), a table or row or cell (t1, t1.r2, t1.r2.c1, for the "
                    "text in it), 'all', or several (\"p2, p5-p7\"). Sizes and spacing are in "
                    "points, indents in cm; leave out what you aren't changing. `size` sets every "
                    "run in them to that font size."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "file": word,
                        "blocks": {"type": "string", "description": "Which paragraphs, e.g. p4, p4-p9, t1, all"},
                        "size": {**number, "description": "Font size in points"},
                        "space_before": {**number, "description": "Points of space above"},
                        "space_after": {**number, "description": "Points of space below"},
                        "line_spacing": {**number, "description": "Line spacing: 1 single, 1.15, 1.5, 2 double"},
                        "align": {"type": "string", "enum": list(ALIGN)},
                        "indent_left": {**number, "description": "Left indent in cm"},
                        "first_line_indent": {**number, "description": "First-line indent in cm (negative hangs)"},
                        "page_break_before": {**switch, "description": "Start each on a new page"},
                        "keep_with_next": {**switch, "description": "Keep on the same page as the next paragraph"},
                    },
                    "required": ["file", "blocks"],
                },
                run=format_paragraphs,
            ),
            Tool(
                name="set_table_layout",
                description=(
                    "Word documents: make a table exactly as wide as the page between its margins (or "
                    "its cell, for a table in a table) — the fix for a table running off the page. "
                    "Columns keep their proportions, or pass `widths`, one per column, as relative "
                    "sizes (e.g. [20, 50, 30]). Long text then wraps inside the cells."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "file": word,
                        "table": {"type": "string", "description": "A table id, e.g. t1"},
                        "widths": {"type": "array", "items": number, "description": "Relative column widths"},
                    },
                    "required": ["file", "table"],
                },
                run=set_table_layout,
            ),
            Tool(
                name="set_page",
                description=(
                    "Word documents: change page orientation, paper size or margins for the whole "
                    "document (landscape gives wide tables room)."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "file": word,
                        "orientation": {"type": "string", "enum": ["portrait", "landscape"]},
                        "size": {"type": "string", "enum": list(PAPER)},
                        "margins": {
                            "type": "array", "items": number,
                            "description": "In cm: [all sides], [top/bottom, left/right] or [top, right, bottom, left]",
                        },
                    },
                    "required": ["file"],
                },
                run=set_page,
            ),
            Tool(
                name="resize_image",
                description=(
                    "Word documents: resize the images (shown as ⟦image 1: 15.2×8.0 cm⟧) in a block, "
                    "keeping their shape. Give `width_cm`, or leave it out to shrink them to fit the "
                    "page (or their cell). `image` picks one when the block has several."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "file": word,
                        "block": {"type": "string", "description": "The paragraph or cell holding it, e.g. p7"},
                        "width_cm": {**number, "description": "New width in cm"},
                        "image": {"type": "integer", "description": "Which image in the block, from 1"},
                    },
                    "required": ["file", "block"],
                },
                run=resize_image,
            ),
        ]
        if not self.visual:
            return tools

        async def render_pages(file: str, pages: str | None = None) -> Output:
            return await self._word(file).render(pages or None)

        tools.append(Tool(
            name="render",
            description=(
                "Word documents: see pages as they'd print, laid out as they are now (with your "
                f"edits so far), up to {config.CHAT_RENDER_PAGES} at a time. Look before fixing layout "
                "or design (tables running off the page, cramped or uneven spacing, oversized "
                "images), and again after, to check the fix worked."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "file": word,
                    "pages": {"type": "string", "description": "e.g. '1', '2-4' or '1,5'; default the first pages"},
                },
                "required": ["file"],
            },
            run=render_pages,
        ))
        return tools
