"""Make model-written markdown render properly in Discord.

Discord only speaks a subset of markdown: no tables, no horizontal rules, and
headers stop at ###. Models write all of these anyway, so replies are rewritten
here before sending: tables become an aligned monospace block (or a list when
too wide for a phone screen), rules are dropped, and deep headers turn bold.
Long replies are split without breaking code blocks across messages.
"""

import re
import unicodedata

# Wider tables wrap badly on phones, so they're rendered as a list instead.
TABLE_MAX_WIDTH = 60

HR_RE = re.compile(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
DEEP_HEADER_RE = re.compile(r"^ {0,3}#{4,6}[ \t]+(.+?)[ \t#]*$")
SEPARATOR_CELL_RE = re.compile(r"^:?-+:?$")
CELL_SPLIT_RE = re.compile(r"(?<!\\)\|")
EMOJI_TOKEN_RE = re.compile(r"<a?(:\w+:)\d+>")
LINK_RE = re.compile(r"\[([^\]]+)\]\(<?[^)\s>]+>?\)")
EMPHASIS_RE = re.compile(r"(\*\*|__|~~|\|\||`|(?<!\w)[*_](?=\S)|(?<=\S)[*_](?!\w))")
FENCE_LANG_RE = re.compile(r"```(\w*)")


def to_discord(text: str) -> str:
    """Rewrite markdown Discord can't render; code blocks are left untouched."""
    lines = text.split("\n")
    out: list[str] = []
    in_code = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if in_code or line.count("```") % 2:
            if line.count("```") % 2:
                in_code = not in_code
            out.append(line)
            i += 1
            continue
        if "|" in line and i + 1 < len(lines) and _is_separator(lines[i + 1]):
            header, align = _cells(line), _alignments(lines[i + 1])
            rows: list[list[str]] = []
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(_cells(lines[i]))
                i += 1
            out.extend(_render_table(header, align, rows))
            continue
        i += 1
        if HR_RE.match(line):
            continue
        if m := DEEP_HEADER_RE.match(line):
            line = f"**{m.group(1)}**"
        # Collapse runs of blank lines (often left behind by dropped rules).
        if not line.strip() and (not out or not out[-1].strip()):
            continue
        out.append(line)
    return "\n".join(out).strip()


def split(text: str, limit: int = 2000) -> list[str]:
    """Split into Discord-sized chunks at paragraph, line, then word breaks.

    A code block cut in half is closed at the end of one chunk and reopened
    (same language) at the start of the next, so both halves still render.
    """
    chunks: list[str] = []
    text = text.strip()
    while len(text) > limit:
        window = text[: limit - 4]  # room to close an open code block
        cut = window.rfind("\n\n")
        if cut < limit // 2:
            cut = window.rfind("\n")
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = len(window)
        chunk, rest = text[:cut].rstrip(), text[cut:]
        rest = rest.lstrip("\n") if rest.startswith("\n") else rest.lstrip(" ")
        if (fence := _open_fence(chunk)) is not None:
            chunk += "\n```"
            rest = f"{fence}\n{rest}"
        chunks.append(chunk)
        text = rest
    return chunks + [text] if text else chunks or ["…"]


def _open_fence(text: str) -> str | None:
    """The opening fence (e.g. "```py") if `text` ends inside a code block."""
    fence = None
    for line in text.split("\n"):
        if line.count("```") % 2:
            m = FENCE_LANG_RE.search(line)
            fence = None if fence is not None else f"```{m.group(1) if m else ''}"
    return fence


# --- Tables --------------------------------------------------------------------

def _cells(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return [c.strip().replace("\\|", "|") for c in CELL_SPLIT_RE.split(s)]


def _is_separator(line: str) -> bool:
    if "|" not in line or "-" not in line:
        return False
    return all(SEPARATOR_CELL_RE.match(c.replace(" ", "")) for c in _cells(line))


def _alignments(line: str) -> list[str]:
    def align(cell: str) -> str:
        cell = cell.replace(" ", "")
        if cell.startswith(":") and cell.endswith(":"):
            return "center"
        return "right" if cell.endswith(":") else "left"
    return [align(c) for c in _cells(line)]


def _plain(cell: str) -> str:
    """Strip inline markdown, which would show literally inside a code block."""
    cell = LINK_RE.sub(r"\1", cell)
    cell = EMOJI_TOKEN_RE.sub(r"\1", cell)
    cell = re.sub(r"<(https?://[^>]+)>", r"\1", cell)
    return EMPHASIS_RE.sub("", cell)


def _width(s: str) -> int:
    """Monospace display width: CJK and most emoji take two columns."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def _pad(s: str, width: int, align: str) -> str:
    gap = width - _width(s)
    if align == "right":
        return " " * gap + s
    if align == "center":
        return " " * (gap // 2) + s + " " * (gap - gap // 2)
    return s + " " * gap


def _render_table(header: list[str], align: list[str], rows: list[list[str]]) -> list[str]:
    n = len(header)
    rows = [(r + [""] * n)[:n] for r in rows]
    align = (align + ["left"] * n)[:n]
    plain = [[_plain(c) for c in r] for r in [header, *rows]]
    widths = [max(_width(r[j]) for r in plain) for j in range(n)]

    if sum(widths) + 2 * (n - 1) <= TABLE_MAX_WIDTH:
        def fmt(r: list[str]) -> str:
            return "  ".join(_pad(c, widths[j], align[j]) for j, c in enumerate(r)).rstrip()
        rule = "  ".join("-" * w for w in widths)
        return ["```", fmt(plain[0]), rule, *(fmt(r) for r in plain[1:]), "```"]

    # Too wide: one bullet per row, keeping the cells' own markdown.
    out = []
    for r in rows:
        title = r[0].strip("*") if r[0].startswith("**") and r[0].endswith("**") else r[0]
        fields = [f"{h}: {c}" if _plain(h) else c for h, c in zip(header[1:], r[1:]) if c]
        title = f"**{title}**" if title else ""
        out.append(f"- {title}" + (" — " * bool(title) + " · ".join(fields) if fields else ""))
    return out
