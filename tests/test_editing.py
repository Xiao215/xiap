import io
import unittest
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from lxml import etree

from xiap import config, render
from xiap.cogs.chat import ChatCog, Files
from xiap.editing import EditError, EditSession, TextFile, WordFile, edited_name
from xiap.llm import PDF, Agent, Attachment, Output

NS = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
    'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" '
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
    'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" mc:Ignorable="w14"'
)
STYLES = (
    '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>'
    '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/></w:style>'
    '<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/></w:style>'
    '<w:style w:type="paragraph" w:styleId="ListParagraph"><w:name w:val="List Paragraph"/></w:style>'
    '<w:style w:type="character" w:styleId="Hyperlink"><w:name w:val="Hyperlink"/></w:style>'
    "</w:styles>"
)
TEXT_BOX = (
    '<w:r><mc:AlternateContent><mc:Choice Requires="wps"><w:drawing><wp:inline>'
    "<w:txbxContent><w:p><w:r><w:t>Boxed</w:t></w:r></w:p></w:txbxContent>"
    "</wp:inline></w:drawing></mc:Choice><mc:Fallback><w:pict>"
    "<w:txbxContent><w:p><w:r><w:t>Boxed</w:t></w:r></w:p></w:txbxContent>"
    "</w:pict></mc:Fallback></mc:AlternateContent></w:r>"
)
# Shaped like what Word writes: rsids, proofing marks and bookmarks between
# runs, a word split across runs with different formatting, a hyperlink, a
# list, a header row, a merged cell, a text box and a section break.
BODY = (
    '<w:p w14:paraId="0A"><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Trip plan</w:t></w:r></w:p>'
    '<w:p w14:paraId="0B" w:rsidR="00AB"><w:r w:rsidR="00AB"><w:rPr><w:b/><w:sz w:val="28"/></w:rPr>'
    '<w:t xml:space="preserve">Meet at </w:t></w:r><w:proofErr w:type="spellStart"/><w:bookmarkStart w:id="0" w:name="x"/>'
    '<w:r><w:rPr><w:i/></w:rPr><w:t>Uni</w:t></w:r><w:r><w:t>on</w:t></w:r><w:bookmarkEnd w:id="0"/>'
    '<w:proofErr w:type="spellEnd"/><w:r><w:t xml:space="preserve"> at 9</w:t><w:tab/><w:t>sharp</w:t></w:r></w:p>'
    '<w:p><w:r><w:t xml:space="preserve">Map: </w:t></w:r><w:hyperlink r:id="rId5"><w:r><w:rPr>'
    '<w:rStyle w:val="Hyperlink"/></w:rPr><w:t>trail map</w:t></w:r></w:hyperlink></w:p>'
    '<w:p><w:pPr><w:pStyle w:val="ListParagraph"/><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr>'
    "<w:r><w:t>water</w:t></w:r></w:p>"
    '<w:p><w:pPr><w:pStyle w:val="ListParagraph"/><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr>'
    "<w:r><w:t>snacks</w:t></w:r></w:p>"
    "<w:tbl><w:tblPr/>"
    '<w:tr><w:trPr><w:tblHeader/></w:trPr><w:tc><w:p><w:r><w:rPr><w:b/></w:rPr><w:t>Who</w:t></w:r></w:p></w:tc>'
    '<w:tc><w:p><w:r><w:rPr><w:b/></w:rPr><w:t>Pays</w:t></w:r></w:p></w:tc></w:tr>'
    '<w:tr><w:tc><w:p><w:r><w:t>Bob</w:t></w:r></w:p></w:tc><w:tc><w:tcPr><w:vMerge w:val="restart"/></w:tcPr>'
    "<w:p><w:r><w:t>$20</w:t></w:r></w:p></w:tc></w:tr>"
    '<w:tr><w:tc><w:p><w:r><w:t>Amy</w:t></w:r></w:p></w:tc><w:tc><w:tcPr><w:vMerge/></w:tcPr><w:p/></w:tc></w:tr>'
    "</w:tbl>"
    f"<w:p>{TEXT_BOX}<w:r><w:t>See the box</w:t></w:r></w:p>"
    "<w:p/>"
    '<w:p><w:pPr><w:sectPr><w:pgSz w:w="12240"/></w:sectPr></w:pPr><w:r><w:t>It’s going to be fun</w:t></w:r></w:p>'
    "<w:p><w:r><w:t>Bring a hat</w:t></w:r></w:p>"
    '<w:sectPr><w:pgSz w:w="12240"/></w:sectPr>'
)


def docx(body: str = BODY, styles: str = STYLES) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", (
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'
        ))
        z.writestr("_rels/.rels", (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship '
            'Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="word/document.xml"/></Relationships>'
        ))
        z.writestr("word/document.xml", (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
            f"<w:document {NS}><w:body>{body}</w:body></w:document>"
        ))
        z.writestr("word/styles.xml", styles)
        z.writestr("word/header1.xml", '<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                   "<w:p><w:r><w:t>Draft</w:t></w:r></w:p></w:hdr>")
        z.writestr("word/footnotes.xml", '<w:footnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                   '<w:footnote w:type="separator" w:id="-1"/><w:footnote w:id="1"><w:p/></w:footnote></w:footnotes>')
        z.writestr("word/media/image1.png", b"\x89PNG not really")
    return out.getvalue()


def document_xml(data: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return z.read("word/document.xml")


def paragraph(doc: WordFile, block: str) -> str:
    return etree.tostring(doc.ids[block]).decode()


class TextFileTests(unittest.TestCase):
    def test_replaces_one_exact_match_and_keeps_line_endings(self):
        f = TextFile("a.txt", "Bob", b"one\r\ntwo\r\nthree\r\n")
        result = f.replace_text("two\nthree", "2\n3")
        self.assertIn("2\n3", result)
        self.assertEqual(f.save(), b"one\r\n2\r\n3\r\n")

    def test_refuses_missing_and_repeated_text(self):
        f = TextFile("a.py", "Bob", b"x = 1\ny = 1\n")
        with self.assertRaisesRegex(EditError, "appears 2 times \\(lines 1, 2\\)"):
            f.replace_text("= 1", "= 2")
        with self.assertRaisesRegex(EditError, "isn't in a.py"):
            f.replace_text("z = 1", "z = 2")
        self.assertFalse(f.changed)

    def test_straight_quotes_match_curly_ones(self):
        f = TextFile("a.md", "Bob", "It’s “fine”".encode())
        f.replace_text('It\'s "fine"', "It is fine")
        self.assertEqual(f.save(), b"It is fine")

    def test_rejects_non_utf8(self):
        with self.assertRaises(UnicodeDecodeError):
            TextFile("a.txt", "Bob", b"caf\xe9")

    def test_reset_drops_edits(self):
        f = TextFile("a.txt", "Bob", b"hello")
        f.replace_text("hello", "bye")
        f.reset()
        self.assertEqual((f.save(), f.changed), (b"hello", False))

    def test_view_from_a_line(self):
        f = TextFile("a.txt", "Bob", b"a\nb\nc")
        self.assertEqual(f.view("2"), "b\nc")


class WordViewTests(unittest.TestCase):
    def test_numbered_blocks_styles_and_what_cant_be_edited(self):
        doc = WordFile("trip.docx", "Bob", docx())
        self.assertEqual(doc.view().split("\n"), [
            "(Paragraph styles: Normal, Heading 1, Heading 2, List Paragraph)",
            "(Not shown and can't be edited: 1 header, 1 footnote, 1 text box)",
            "[p1 Heading 1] Trip plan",
            "[p2] Meet at Union at 9\tsharp",
            "[p3] Map: trail map",
            "[p4 List Paragraph, list] water",
            "[p5 List Paragraph, list] snacks",
            "[t1.r1] Who | Pays",
            "[t1.r2] Bob | $20",
            "[t1.r3] Amy | ",
            "[p6] See the box ⟦text box 1⟧",
            "[p7] ",
            "[p8] It’s going to be fun",
            "[p9] Bring a hat",
        ])

    def test_view_from_a_block(self):
        doc = WordFile("trip.docx", "Bob", docx())
        self.assertEqual(doc.view("t1.r3", limit=45), "[t1.r3] Amy | \n[p6] See the box ⟦text box 1⟧\n…(more: view_file with start=p7)")

    def test_cells_and_rows_wrapped_in_content_controls(self):
        # Word's header and footer layouts wrap cells (and repeating rows) in w:sdt.
        doc = WordFile("t.docx", "Bob", docx(
            "<w:tbl><w:sdt><w:sdtContent><w:tr>"
            "<w:sdt><w:sdtContent><w:tc><w:p><w:r><w:t>[Author]</w:t></w:r></w:p></w:tc></w:sdtContent></w:sdt>"
            "<w:tc><w:p><w:r><w:t>[Date]</w:t></w:r></w:p></w:tc>"
            "</w:tr></w:sdtContent></w:sdt></w:tbl><w:p/><w:sectPr/>"
        ))
        self.assertIn("[t1.r1] [Author] | [Date]", doc.view())
        doc.replace_text("[Author]", "Xiao")
        doc.insert_row("t1.r1", ["Bob", "today"])
        self.assertIn("[t1.r1] Xiao | [Date]\n[t1.r2] Bob | today", doc.view())
        WordFile("t.docx", "Bob", doc.save())
        doc.delete_block("t1.r2")
        doc.delete_block("t1.r1")  # the last row takes its table with it
        self.assertNotIn("t1", doc.ids)

    def test_tracked_changes_make_it_read_only(self):
        doc = WordFile("t.docx", "Bob", docx('<w:p><w:ins w:id="1"><w:r><w:t>new</w:t></w:r></w:ins></w:p><w:sectPr/>'))
        self.assertIn("tracked changes", doc.view())
        with self.assertRaisesRegex(EditError, "tracked changes"):
            doc.replace_text("new", "old")

    def test_refuses_a_dtd(self):
        out = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(docx())) as src, zipfile.ZipFile(out, "w") as dst:
            for info in src.infolist():
                data = src.read(info)
                if info.filename == "word/document.xml":
                    data = b'<!DOCTYPE x [<!ENTITY a "b">]>' + data.split(b"?>", 1)[1]
                dst.writestr(info, data)
        with self.assertRaises(ValueError):
            WordFile("t.docx", "Bob", out.getvalue())


class WordEditTests(unittest.TestCase):
    def setUp(self):
        self.doc = WordFile("trip.docx", "Bob", docx())

    def text(self, block: str) -> str:
        line = next(line for bid, line in self.doc._lines() if bid == block)
        return line.split("] ", 1)[1]

    def test_replace_across_runs_keeps_the_first_runs_formatting(self):
        self.doc.replace_text("at Union", "by the Library")
        self.assertEqual(self.text("p2"), "Meet by the Library at 9\tsharp")
        xml = paragraph(self.doc, "p2")
        self.assertIn('<w:rPr><w:b/><w:sz w:val="28"/></w:rPr><w:t xml:space="preserve">by the Library</w:t>', xml)
        self.assertNotIn("Uni", xml)
        self.assertIn('<w:bookmarkStart w:id="0" w:name="x"/>', xml)  # markers between runs stay

    def test_replace_inside_a_hyperlink_and_with_tabs_and_breaks(self):
        self.doc.replace_text("trail map", "the map")
        self.assertIn('<w:hyperlink r:id="rId5"><w:r><w:rPr><w:rStyle w:val="Hyperlink"/></w:rPr><w:t xml:space="preserve">the map', paragraph(self.doc, "p3"))
        self.doc.replace_text("9\tsharp", "9:30\nno later")
        self.assertEqual(self.text("p2"), "Meet at Union at 9:30\nno later")
        self.assertIn("<w:br/>", paragraph(self.doc, "p2"))

    def test_deleting_text(self):
        self.doc.replace_text("\tsharp", "")
        self.assertEqual(self.text("p2"), "Meet at Union at 9")

    def test_curly_quotes_match_straight_ones(self):
        self.doc.replace_text("It's going", "It is going")
        self.assertEqual(self.text("p8"), "It is going to be fun")

    def test_ambiguous_missing_and_spanning_text_are_refused(self):
        with self.assertRaisesRegex(EditError, "appears 2 times \\(in p2\\)"):
            self.doc.replace_text("at ", "by ")
        with self.assertRaisesRegex(EditError, "more than one paragraph or table cell"):
            self.doc.replace_text("Who | Pays", "x")
        with self.assertRaisesRegex(EditError, "more than one paragraph"):
            self.doc.replace_text("water\nsnacks", "x")
        with self.assertRaisesRegex(EditError, "isn't in p9; it's in p1"):
            self.doc.replace_text("Trip", "x", block="p9")
        with self.assertRaisesRegex(EditError, "There's no block 'p99'"):
            self.doc.replace_text("Trip", "x", block="p99")
        self.assertFalse(self.doc.changed)

    def test_block_narrows_the_search(self):
        self.doc.replace_text("$20", "$25", block="t1.r2")
        self.doc.replace_text("Amy", "Ann", block="t1.r3.c1")
        self.assertEqual((self.text("t1.r2"), self.text("t1.r3")), ("Bob | $25", "Ann | "))

    def test_fill_an_empty_cell_and_paragraph(self):
        self.doc.replace_text("", "$15", block="t1.r3.c2")
        self.doc.replace_text("", "Notes", block="p7")
        self.assertEqual((self.text("t1.r3"), self.text("p7")), ("Amy | $15", "Notes"))
        with self.assertRaisesRegex(EditError, "isn't empty"):
            self.doc.replace_text("", "x", block="p9")

    def test_format_text_splits_runs_and_keeps_schema_order(self):
        self.doc.format_text("Union", bold=True, underline=True)
        xml = paragraph(self.doc, "p2")
        self.assertIn('<w:rPr><w:b/><w:i/><w:u w:val="single"/></w:rPr><w:t>Uni</w:t>', xml)
        self.assertIn('<w:rPr><w:b/><w:u w:val="single"/></w:rPr><w:t>on</w:t>', xml)
        self.doc.format_text("Meet", bold=False)
        self.assertIn('<w:rPr><w:b w:val="0"/><w:sz w:val="28"/></w:rPr><w:t xml:space="preserve">Meet</w:t>', paragraph(self.doc, "p2"))
        self.assertEqual(self.text("p2"), "Meet at Union at 9\tsharp")
        self.assertIn("* p2: made 'Union' bold, underline", self.doc.diff())

    def test_ids_stay_put_as_paragraphs_come_and_go(self):
        result = self.doc.insert_paragraphs("p5", "sunscreen\nhat")
        self.assertIn("Added p10, p11", result)
        self.doc.delete_block("p4")
        self.doc.replace_text("snacks", "trail mix", block="p5")
        lines = [line for _, line in self.doc._lines()]
        self.assertEqual(lines[3:6], [
            "[p5 List Paragraph, list] trail mix",
            "[p10 List Paragraph, list] sunscreen",
            "[p11 List Paragraph, list] hat",
        ])

    def test_insert_after_a_heading_like_another_paragraph_or_with_a_style(self):
        self.doc.insert_paragraphs("p1", "Day one", like="p9")
        self.doc.insert_paragraphs("end", "Extras", style="Heading 2")
        self.doc.insert_paragraphs("start", "Draft")
        lines = [line for _, line in self.doc._lines()]
        self.assertEqual(lines[:3], ["[p12] Draft", "[p1 Heading 1] Trip plan", "[p10] Day one"])
        self.assertEqual(lines[-1], "[p11 Heading 2] Extras")
        with self.assertRaisesRegex(EditError, "no paragraph style 'Fancy'"):
            self.doc.insert_paragraphs("p9", "x", style="Fancy")
        self.assertEqual(len(lines), len(self.doc._lines()))  # the failed insert left nothing behind

    def test_insert_row_after_a_header_copies_a_data_row(self):
        self.doc.insert_row("t1.r1", ["Cat", "$5"])
        self.assertEqual(self.text("t1.r4"), "Cat | $5")
        xml = etree.tostring(self.doc.ids["t1.r4"]).decode()
        self.assertNotIn("tblHeader", xml)
        self.assertNotIn("vMerge", xml)
        self.assertNotIn("<w:b/>", xml)
        with self.assertRaisesRegex(EditError, "has 2 cells"):
            self.doc.insert_row("t1.r2", ["only one"])

    def test_delete_rows_tables_and_paragraphs(self):
        self.doc.delete_block("t1.r3")
        self.assertNotIn("t1.r3", self.doc.ids)
        with self.assertRaisesRegex(EditError, "not a single cell"):
            self.doc.delete_block("t1.r2.c1")
        with self.assertRaisesRegex(EditError, "ends a section"):
            self.doc.delete_block("p8")
        result = self.doc.delete_block("p6")
        self.assertIn("also held 1 image", result)
        self.doc.delete_block("t1")
        self.assertFalse(any(bid.startswith("t1") for bid in self.doc.ids))

    def test_set_style(self):
        self.doc.set_style("p9", "heading 2")
        self.assertEqual(next(line for bid, line in self.doc._lines() if bid == "p9"), "[p9 Heading 2] Bring a hat")
        with self.assertRaisesRegex(EditError, "isn't a paragraph"):
            self.doc.set_style("t1", "Heading 2")


class WordSaveTests(unittest.TestCase):
    def test_saved_file_reads_back_and_only_edited_blocks_change(self):
        original = docx()
        doc = WordFile("trip.docx", "Bob", original)
        doc.replace_text("Union", "the Library")
        doc.insert_row("t1.r2", ["Cat", "$5"])
        data = doc.save()

        before, after = document_xml(original), document_xml(data)
        # Word's namespace declarations (including the mc:Ignorable ones) survive.
        self.assertIn(b'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"', after)
        self.assertIn(b'mc:Ignorable="w14"', after)
        for untouched in (b"<w:t>Trip plan</w:t>", b"<w:t>Bring a hat</w:t>", b"<w:txbxContent>", b'<w:pgSz w:w="12240"/>'):
            self.assertEqual(before.count(untouched), after.count(untouched))
        again = WordFile("trip_edited.docx", "Bob", data)
        self.assertIn("[p2] Meet at the Library at 9\tsharp", again.view())
        self.assertIn("[t1.r3] Cat | $5", again.view())
        with zipfile.ZipFile(io.BytesIO(original)) as a, zipfile.ZipFile(io.BytesIO(data)) as b:
            self.assertEqual(a.namelist(), b.namelist())
            for name in a.namelist():
                if name != "word/document.xml":
                    self.assertEqual(a.read(name), b.read(name))

    def test_a_block_changed_behind_the_tools_back_fails_the_check(self):
        doc = WordFile("trip.docx", "Bob", docx())
        doc.replace_text("Union", "the Library")
        doc.ids["p9"].find(".//{*}t").text = "Bring a cap"
        with self.assertRaisesRegex(ValueError, "p9 changed without being edited"):
            doc.save()

    def test_edited_name(self):
        self.assertEqual(edited_name("trip.docx"), "trip_edited.docx")
        self.assertEqual(edited_name("trip_edited.docx"), "trip_edited.docx")


class SessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.doc = WordFile("trip.docx", "Bob", docx())
        self.txt = TextFile("notes.txt", "Bob", b"buy milk\n")
        self.session = EditSession([self.doc, self.txt])
        self.tools = {t.name: t for t in self.session.tools()}

    async def test_tools_edit_and_review_once_per_change(self):
        self.assertIsNone(await self.session.review())
        await self.tools["replace_text"].run(file="notes.txt", old="milk", new="eggs")
        await self.tools["format_text"].run(file="trip.docx", text="Trip plan", italic=True)
        with patch("xiap.editing.render.soffice", return_value=None):  # pages are tested in RenderTests
            note = await self.session.review()
            self.assertIn("-buy milk\n+buy eggs", note.text)
            self.assertIn("* p1: made 'Trip plan' italic", note.text)
            self.assertIsNone(await self.session.review())
        saved, failed = await self.session.outputs()
        self.assertEqual(([name for name, _ in saved], failed), (["trip_edited.docx", "notes_edited.txt"], []))

    async def test_word_only_tools_refuse_text_files(self):
        with self.assertRaisesRegex(EditError, "is a text file"):
            await self.tools["delete_block"].run(file="notes.txt", block="p1")
        with self.assertRaisesRegex(EditError, "no editable file"):
            await self.tools["replace_text"].run(file="other.txt", old="a", new="b")

    async def test_text_files_alone_get_only_the_basic_tools(self):
        names = [t.name for t in EditSession([self.txt]).tools()]
        self.assertEqual(names, ["replace_text", "view_file"])


class AgentWorkspaceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        settings = patch.multiple(config, COHERE_API_KEYS=[], CLI_BRIDGE_URL="http://localhost:8787", CHAT_PROVIDER="auto")
        settings.start()
        self.addCleanup(settings.stop)
        self.agent = Agent(Mock())
        self.txt = TextFile("notes.txt", "Bob", b"buy milk\n")
        self.session = EditSession([self.txt])

    async def test_model_checks_the_diff_before_answering(self):
        edit = {"type": "tool_use", "id": "1", "name": "replace_text",
                "input": {"file": "notes.txt", "old": "milk", "new": "eggs"}}
        self.agent._bridge_request = AsyncMock(side_effect=[
            {"content": [edit]},
            {"content": [{"type": "text", "text": "draft"}]},
            {"content": [{"type": "text", "text": "Swapped milk for eggs"}], "model": "claude-x"},
        ])
        result = await self.agent.run("system", "prompt", self.session.tools(), workspace=self.session, rounds=8)
        self.assertEqual(result[0], "Swapped milk for eggs")
        review = self.agent._bridge_request.call_args.args[0]["messages"][-1]["content"][0]["text"]
        self.assertIn("+buy eggs", review)
        self.assertEqual(self.txt.save(), b"buy eggs\n")

    async def test_failover_starts_from_the_original_file(self):
        async def half_done(*args, **kwargs):
            self.txt.replace_text("milk", "oops")
            raise RuntimeError("down")

        async def gemini(*args, **kwargs):
            self.assertEqual(self.txt.text, "buy milk\n")
            return "ok", "gemini-x"

        self.agent._runners.update(claude=half_done, gemini=gemini)
        result = await self.agent.run("system", "prompt", [], workspace=self.session)
        self.assertEqual(result[1], "gemini")
        self.assertFalse(self.txt.changed)


PAGE = (
    '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" '
    'w:left="1440" w:header="720" w:footer="720" w:gutter="0"/></w:sectPr>'
)
WIDE_TABLE = (  # 15000 twips wide on a page with 9360 between the margins
    '<w:tbl><w:tblPr><w:tblW w:w="15000" w:type="dxa"/><w:tblInd w:w="500" w:type="dxa"/></w:tblPr>'
    '<w:tblGrid><w:gridCol w:w="5000"/><w:gridCol w:w="10000"/></w:tblGrid>'
    '<w:tr><w:tc><w:tcPr><w:tcW w:w="5000" w:type="dxa"/></w:tcPr><w:p><w:r><w:t>Day</w:t></w:r></w:p></w:tc>'
    '<w:tc><w:p><w:r><w:t>Route</w:t></w:r></w:p></w:tc></w:tr>'
    '<w:tr><w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr><w:p><w:r><w:t>Both days</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
)
EMU_CM = 360000
IMAGE = (
    '<w:p><w:r><w:drawing><wp:inline><wp:extent cx="9000000" cy="4500000"/><a:graphic '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:graphicData><pic:pic '
    'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"><pic:spPr><a:xfrm>'
    '<a:ext cx="9000000" cy="4500000"/></a:xfrm></pic:spPr></pic:pic></a:graphicData></a:graphic>'
    "</wp:inline></w:drawing></w:r></w:p>"
)
TEXT = (
    '<w:p><w:pPr><w:spacing w:before="0" w:beforeAutospacing="1"/></w:pPr><w:r><w:rPr><w:b/><w:sz w:val="40"/>'
    '</w:rPr><w:t xml:space="preserve">Big </w:t></w:r><w:r><w:t>small</w:t></w:r></w:p>'
    "<w:p><w:r><w:t>Second</w:t></w:r></w:p>"
)


class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.doc = WordFile("trip.docx", "Bob", docx(TEXT + WIDE_TABLE + IMAGE + PAGE))

    def xml(self, block: str) -> str:
        return etree.tostring(self.doc.ids[block]).decode()

    def test_table_fits_the_page_keeping_proportions(self):
        result = self.doc.set_table_layout("t1")
        self.assertIn("16.5 cm wide", result)
        xml = self.xml("t1")
        self.assertIn('<w:gridCol w:w="3120"/><w:gridCol w:w="6240"/>', xml)
        self.assertIn('<w:tblW w:w="9360" w:type="dxa"/><w:tblInd w:w="0" w:type="dxa"/><w:tblLayout w:type="fixed"/>', xml)
        self.assertIn('<w:tcW w:w="3120" w:type="dxa"/>', xml)
        self.assertIn('<w:tcPr><w:tcW w:w="9360" w:type="dxa"/><w:gridSpan w:val="2"/></w:tcPr>', xml)  # spans both
        WordFile("x.docx", "Bob", self.doc.save())

    def test_table_widths_and_mistakes(self):
        self.doc.set_table_layout("t1", [1, 3])
        self.assertIn('<w:gridCol w:w="2340"/><w:gridCol w:w="7020"/>', self.xml("t1"))
        with self.assertRaisesRegex(EditError, "t1 has 2 columns; pass 2 widths"):
            self.doc.set_table_layout("t1", [1, 2, 3])
        with self.assertRaisesRegex(EditError, "isn't a table"):
            self.doc.set_table_layout("p1")

    def test_paragraph_size_spacing_and_alignment(self):
        self.doc.format_paragraphs("p1-p2", size=11, space_before=6, line_spacing=1.15, align="justify")
        xml = self.xml("p1")
        self.assertIn('<w:spacing w:before="120" w:line="276" w:lineRule="auto"/><w:jc w:val="both"/>', xml)
        self.assertNotIn("Autospacing", xml)
        self.assertIn('<w:rPr><w:b/><w:sz w:val="22"/><w:szCs w:val="22"/></w:rPr>', xml)
        self.assertEqual(xml.count('<w:sz w:val="22"/>'), 3)  # both runs and the paragraph mark
        self.assertIn('<w:sz w:val="22"/>', self.xml("p2"))
        self.assertIn("* p1, p2: size 11 pt, space before 6 pt, line spacing 1.15×, align justify", self.doc.diff())
        with self.assertRaisesRegex(EditError, "align is one of"):
            self.doc.format_paragraphs("p1", align="middle")
        with self.assertRaisesRegex(EditError, "Say what to change"):
            self.doc.format_paragraphs("p1")
        WordFile("x.docx", "Bob", self.doc.save())

    def test_page_breaks_indents_and_table_text(self):
        self.doc.format_paragraphs("p2", page_break_before=True, keep_with_next=False, first_line_indent=-1)
        self.assertIn('<w:pPr><w:keepNext w:val="0"/><w:pageBreakBefore/><w:ind w:hanging="567"/></w:pPr>', self.xml("p2"))
        self.doc.format_paragraphs("t1", size=9)
        self.assertEqual(self.xml("t1").count('<w:sz w:val="18"/>'), 6)  # 3 cells: their runs and marks

    def test_page_setup(self):
        result = self.doc.set_page(orientation="landscape", size="a4", margins=[2])
        self.assertIn("landscape", result)
        sect = etree.tostring(self.doc.body.find("{*}sectPr")).decode()
        self.assertIn('<w:pgSz w:w="16838" w:h="11906" w:orient="landscape"/>', sect)
        self.assertIn('w:top="1134" w:right="1134" w:bottom="1134" w:left="1134" w:header="720"', sect)
        self.doc.set_page(orientation="portrait")
        self.assertIn('<w:pgSz w:w="11906" w:h="16838"/>', etree.tostring(self.doc.body.find("{*}sectPr")).decode())
        self.assertIn("16.5 cm wide", WordFile("t.docx", "Bob", docx(WIDE_TABLE + PAGE)).set_table_layout("t1"))
        WordFile("x.docx", "Bob", self.doc.save())

    def test_images_are_shown_and_shrink_to_fit(self):
        self.assertIn("[p3]  ⟦image 1: 25.0×12.5 cm⟧", self.doc.view())
        result = self.doc.resize_image("p3")
        self.assertIn("16.5×8.3 cm", result)
        xml = self.xml("p3")
        self.assertIn('<wp:extent cx="5943600" cy="2971800"/>', xml)
        self.assertIn('<a:ext cx="5943600" cy="2971800"/>', xml)
        self.doc.resize_image("p3", width_cm=5)
        self.assertIn("⟦image 1: 5.0×2.5 cm⟧", self.doc.view())
        with self.assertRaisesRegex(EditError, "p1 has no images"):
            self.doc.resize_image("p1")
        WordFile("x.docx", "Bob", self.doc.save())


class PagePickTests(unittest.TestCase):
    def test_pick_and_spans(self):
        self.assertEqual(render.pick(None, 12, 5), [1, 2, 3, 4, 5])
        self.assertEqual(render.pick("2, 7-9", 8, 5), [2, 7, 8])
        self.assertEqual(render.pick("1-20", 30, 5), [1, 2, 3, 4, 5])
        self.assertEqual(render.spans([1, 2, 3, 5, 7, 8]), "1-3, 5, 7-8")
        with self.assertRaisesRegex(render.RenderError, "has 3 pages"):
            render.pick("9", 3, 5)
        with self.assertRaisesRegex(render.RenderError, "look like"):
            render.pick("first", 3, 5)


class PagesToModelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        settings = patch.multiple(config, COHERE_API_KEYS=[], CLI_BRIDGE_URL="http://localhost:8787", CHAT_PROVIDER="auto")
        settings.start()
        self.addCleanup(settings.stop)
        self.agent = Agent(Mock())
        pages = Output("page 1 of 1", [Attachment(PDF, b"%PDF-1.7 fake", "trip p1.pdf")])
        self.tools = [SimpleNamespace(name="render", description="", parameters={}, run=AsyncMock(return_value=pages))]

    async def test_claude_and_gemini_get_the_pdf_inside_the_tool_result(self):
        call = {"type": "tool_use", "id": "1", "name": "render", "input": {"file": "trip.docx"}}
        self.agent._bridge_request = AsyncMock(side_effect=[
            {"content": [call]}, {"content": [{"type": "text", "text": "looks fine"}]},
        ])
        await self.agent.run("system", "prompt", self.tools)
        result = self.agent._bridge_request.call_args.args[0]["messages"][-1]["content"][0]
        self.assertEqual(result["content"][0], {"type": "text", "text": "page 1 of 1"})
        self.assertEqual(result["content"][1]["type"], "document")
        self.assertEqual(result["content"][1]["title"], "trip p1.pdf")

    async def test_the_final_check_shows_pages(self):
        session = EditSession([WordFile("trip.docx", "Bob", docx())])
        session.files["trip.docx"].replace_text("Union", "the Library")
        pages = Output("pages 1 of 1 after your edits", [Attachment(PDF, b"%PDF fake", "trip p1.pdf")])
        with patch("xiap.editing.render.soffice", return_value="/usr/bin/soffice"), \
                patch.object(WordFile, "changed_pages", AsyncMock(return_value=pages)):
            note = await session.review()
        self.assertIn("after your edits", note.text)
        self.assertIn("nothing should run off the page", note.text)
        self.assertEqual(len(note.attachments), 1)

    async def test_cohere_is_told_it_cant_see_pages(self):
        with patch.object(config, "COHERE_API_KEYS", ["k"]), patch.object(config, "CLI_BRIDGE_URL", ""):
            agent = Agent(Mock())
        call = SimpleNamespace(id="1", function=SimpleNamespace(name="render", arguments="{}"))
        agent._cohere_request = AsyncMock(side_effect=[
            SimpleNamespace(tool_calls=[call], tool_plan=None, content=None),
            SimpleNamespace(tool_calls=None, content=[SimpleNamespace(type="text", text="ok")]),
        ])
        await agent.run("system", "prompt", self.tools)
        sent = agent._cohere_request.call_args.kwargs["messages"][-1]["content"]
        self.assertIn("page 1 of 1", sent)
        self.assertIn("couldn't be shown to you", sent)

    def test_editing_puts_models_that_see_pages_first(self):
        with patch.object(config, "COHERE_API_KEYS", ["k"]), patch.object(config, "CHAT_PROVIDER", "cohere"):
            agent = Agent(Mock())
        self.assertEqual(agent.chain(pick="cohere:", pdf=True)[0][0], "claude")


@unittest.skipUnless(render.soffice(), "LibreOffice isn't installed")
class RenderTests(unittest.IsolatedAsyncioTestCase):
    async def test_pages_of_the_document_as_it_is_now(self):
        doc = WordFile("trip.docx", "Bob", docx(TEXT + WIDE_TABLE + IMAGE + PAGE))
        out = await doc.render()
        self.assertIn("page 1 of 1", out.text)
        self.assertTrue(out.attachments[0].data.startswith(b"%PDF"))
        doc.replace_text("Second", "Second paragraph, now longer")
        out = await doc.changed_pages()
        self.assertIn("after your edits: page 1 of 1", out.text)
        self.assertIn("Second paragraph, now longer", render.page_texts(out.attachments[0].data)[0])


class AnswerTests(unittest.IsolatedAsyncioTestCase):
    async def test_edited_file_is_attached_to_the_reply(self):
        cog = ChatCog(Mock())
        txt = TextFile("notes.txt", "Bob", b"buy milk\n")

        async def run(system, prompt, tools, media, pick=None, workspace=None, rounds=None, visual=False):
            self.assertIn("You can edit these attached files: notes.txt", prompt)
            self.assertEqual(rounds, config.CHAT_MAX_EDIT_ROUNDS)
            await next(t for t in tools if t.name == "replace_text").run(file="notes.txt", old="milk", new="eggs")
            return "Done, swapped it", "claude", "claude-x"

        cog.agent = SimpleNamespace(run=run)
        cog._read_files = AsyncMock(return_value=Files(editable=[txt]))
        cog._system_prompt = Mock(return_value="system")
        message = MagicMock()
        message.author.display_name = "Bob"
        message.guild.filesize_limit = 10 * 1024 * 1024
        message.reply = AsyncMock()
        with patch("xiap.cogs.chat.store") as store, patch("xiap.cogs.chat.ChatContext.prompt", AsyncMock(
            return_value="...\n\nYou can edit these attached files: notes.txt."
        )):
            store.get_context_limit.return_value = 0
            store.get_model_pick.return_value = None
            await cog._answer(message, None, "swap milk for eggs")
        files = message.reply.call_args.kwargs["files"]
        self.assertEqual([f.filename for f in files], ["notes_edited.txt"])
        self.assertEqual(files[0].fp.read(), b"buy eggs\n")


if __name__ == "__main__":
    unittest.main()
