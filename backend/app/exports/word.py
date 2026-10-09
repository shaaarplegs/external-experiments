"""Word (.docx) export. Arabic needs explicit right-to-left and complex-script settings."""

import io

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor
from docx.text.paragraph import Paragraph

from app.exports.view import MeetingView, looks_arabic

FONT = "Arial"  # ships with Arabic glyphs on Windows, macOS, Android and Office online
ACCENT = RGBColor(0x1F, 0x6F, 0xEB)
MUTED = RGBColor(0x57, 0x60, 0x6A)
# Column widths of the action items table: #, task, owner, due, priority, status.
ACTION_COLUMNS = (Cm(0.8), Cm(6.6), Cm(2.6), Cm(2.4), Cm(1.8), Cm(1.8))


def _format_run(run, *, size: float, bold: bool, color: RGBColor | None, rtl: bool) -> None:
    run.font.name = FONT
    run.font.size = Pt(size)
    run.font.bold = bold
    if color is not None:
        run.font.color.rgb = color
    rpr = run._r.get_or_add_rPr()
    fonts = rpr.get_or_add_rFonts()
    for attr in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
        fonts.set(qn(attr), FONT)
    # Arabic is a "complex script": Word reads its size and bold from szCs/bCs.
    size_cs = OxmlElement("w:szCs")
    size_cs.set(qn("w:val"), str(int(size * 2)))
    rpr.insert_element_before(
        size_cs, "w:highlight", "w:u", "w:effect", "w:bdr", "w:shd", "w:fitText", "w:vertAlign",
        "w:rtl", "w:cs", "w:em", "w:lang", "w:eastAsianLayout", "w:specVanish", "w:oMath",
    )
    if bold:
        rpr.get_or_add_bCs()
    if rtl:
        rpr.get_or_add_rtl()


def _set_direction(paragraph: Paragraph, rtl: bool) -> None:
    if rtl:
        ppr = paragraph._p.get_or_add_pPr()
        bidi = OxmlElement("w:bidi")
        ppr.insert_element_before(
            bidi, "w:adjustRightInd", "w:snapToGrid", "w:spacing", "w:ind", "w:contextualSpacing",
            "w:mirrorIndents", "w:suppressOverlap", "w:jc", "w:textDirection", "w:textAlignment",
            "w:textboxTightWrap", "w:outlineLvl", "w:divId", "w:cnfStyle", "w:rPr", "w:sectPr",
            "w:pPrChange",
        )
        # No explicit alignment: a bidi paragraph starts at the right by default, and
        # Word/LibreOffice read jc="right" as "end", which is the left edge in RTL.


class _Writer:
    def __init__(self, view: MeetingView):
        self.view = view
        self.doc = Document()
        section = self.doc.sections[0]
        if view.rtl:
            sect_bidi = OxmlElement("w:bidi")
            section._sectPr.insert_element_before(
                sect_bidi, "w:rtlGutter", "w:docGrid", "w:printerSettings", "w:sectPrChange"
            )

    def text(self, container, value: str, *, size=10.5, bold=False, color=None, style=None) -> Paragraph:
        paragraph = container.add_paragraph(style=style)
        _set_direction(paragraph, self.view.rtl)
        if value:
            run = paragraph.add_run(value)
            _format_run(run, size=size, bold=bold, color=color, rtl=looks_arabic(value))
        return paragraph

    def labeled(self, label: str, value: str) -> None:
        paragraph = self.doc.add_paragraph()
        _set_direction(paragraph, self.view.rtl)
        paragraph.paragraph_format.space_after = Pt(2)
        _format_run(paragraph.add_run(f"{label}: "), size=10, bold=True, color=MUTED, rtl=self.view.rtl)
        _format_run(paragraph.add_run(value), size=10, bold=False, color=None, rtl=looks_arabic(value))

    def heading(self, value: str) -> None:
        paragraph = self.text(self.doc, value, size=13, bold=True, color=ACCENT, style="Heading 2")
        paragraph.paragraph_format.space_before = Pt(12)

    def bullets(self, items: list[str]) -> None:
        for item in items:
            self.text(self.doc, item, style="List Bullet")

    def actions_table(self) -> None:
        labels = self.view.labels
        headers = ["#", labels["task"], labels["owner"], labels["due"], labels["priority"], labels["status"]]
        rows = [headers] + [
            [str(index), row.task, row.owner, row.due, row.priority, row.status]
            for index, row in enumerate(self.view.actions, start=1)
        ]
        table = self.doc.add_table(rows=len(rows), cols=len(headers))
        table.style = "Table Grid"
        table.autofit = False
        for column, width in zip(table.columns, ACTION_COLUMNS, strict=True):
            column.width = width  # the table grid (LibreOffice); cell widths below are for Word
        if self.view.rtl:
            table._tbl.tblPr.insert_element_before(
                OxmlElement("w:bidiVisual"),
                "w:tblStyleRowBandSize", "w:tblStyleColBandSize", "w:tblW", "w:jc",
                "w:tblCellSpacing", "w:tblInd", "w:tblBorders", "w:shd", "w:tblLayout",
                "w:tblCellMar", "w:tblLook", "w:tblCaption", "w:tblDescription", "w:tblPrChange",
            )
        for row_index, values in enumerate(rows):
            for cell, value, width in zip(table.rows[row_index].cells, values, ACTION_COLUMNS, strict=True):
                cell.width = width
                cell._tc.remove(cell.paragraphs[0]._p)
                self.text(cell, value, size=9.5, bold=row_index == 0)
                if row_index == 0:
                    shading = OxmlElement("w:shd")
                    shading.set(qn("w:val"), "clear")
                    shading.set(qn("w:color"), "auto")
                    shading.set(qn("w:fill"), "F1F5FB")
                    cell._tc.get_or_add_tcPr().append(shading)

    def build(self) -> bytes:
        v, labels, s = self.view, self.view.labels, self.view.summary
        self.text(self.doc, labels["doc_title"], size=9, bold=True, color=ACCENT)
        self.text(self.doc, v.title, size=18, bold=True, style="Title")
        for key, value in (
            ("date", v.date),
            ("duration", v.duration),
            ("category", v.category),
            ("attendees", ("، " if v.rtl else ", ").join(v.attendees)),
        ):
            if value:
                self.labeled(labels[key], value)

        if s:
            if s.overview:
                self.heading(labels["overview"])
                self.text(self.doc, s.overview)
            if s.what_happened:
                self.heading(labels["what_happened"])
                self.bullets(s.what_happened)
            if s.decisions:
                self.heading(labels["decisions"])
                self.bullets(s.decisions)
        else:
            self.text(self.doc, labels["no_summary"], color=MUTED)

        if v.actions:
            self.heading(labels["action_items"])
            self.actions_table()

        if s:
            if s.parties:
                self.heading(labels["parties"])
                self.bullets([f"{p.name}: {p.role}" if p.role else p.name for p in s.parties])
            if s.open_questions:
                self.heading(labels["open_questions"])
                self.bullets(s.open_questions)
            if s.topics:
                self.heading(labels["topics"])
                self.text(self.doc, " • ".join(s.topics))

        if v.transcript:
            self.doc.add_page_break()
            self.heading(labels["transcript"])
            for line in v.transcript.splitlines():
                if line.strip():
                    self.text(self.doc, line.strip(), size=9.5)

        buffer = io.BytesIO()
        self.doc.save(buffer)
        return buffer.getvalue()


def render_docx(view: MeetingView) -> bytes:
    return _Writer(view).build()
