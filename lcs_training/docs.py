"""Branded Word output (LCS-Training Style Guide: Montserrat headings in Purple,
Calibri body, logo top-left, footer contact block)."""
from __future__ import annotations

import io

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

from .config import APP_DIR, BRAND, FOOTER_LINE

PURPLE = RGBColor.from_string(BRAND["purple"])
CHARCOAL = RGBColor.from_string(BRAND["charcoal"])
GRAY = RGBColor.from_string(BRAND["gray"])


def _shade(cell, hex_fill: str) -> None:
    tcPr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_fill)
    tcPr.append(shd)


def _font(run, name="Calibri", size=11, bold=False, color=CHARCOAL, italic=False):
    run.font.name = name
    run._element.rPr.rFonts.set(qn("w:eastAsia"), name)
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.italic = italic
    run.font.color.rgb = color


def _heading(doc, text, size=16):
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(10)
    p.paragraph_format.space_after = Pt(4)
    _font(p.add_run(text), "Montserrat", size, True, PURPLE)
    return p


def _para(doc_or_cell, text, **kw):
    p = doc_or_cell.add_paragraph()
    _font(p.add_run(text or ""), **kw)
    return p


def _base_document(title: str) -> Document:
    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Inches(8.5), Inches(11)
    sec.left_margin = sec.right_margin = Inches(0.75)
    sec.top_margin, sec.bottom_margin = Inches(0.75), Inches(0.9)
    st = doc.styles["Normal"]
    st.font.name = "Calibri"
    st.font.size = Pt(11)
    st.element.rPr.rFonts.set(qn("w:eastAsia"), "Calibri")
    head = sec.header.paragraphs[0]
    head.add_run().add_picture(str(APP_DIR / "static" / "logo.png"), width=Inches(2.4))
    rule = sec.header.add_paragraph()
    pPr = rule._p.get_or_add_pPr()
    bdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    for k, v in (("w:val", "single"), ("w:sz", "8"), ("w:space", "1"), ("w:color", BRAND["purple"])):
        bottom.set(qn(k), v)
    bdr.append(bottom)
    pPr.append(bdr)
    foot = sec.footer.paragraphs[0]
    foot.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _font(foot.add_run(FOOTER_LINE), size=8.5, color=GRAY)
    t = doc.add_paragraph()
    _font(t.add_run(title), "Montserrat", 22, True, PURPLE)
    return doc


def _kv_table(doc, rows):
    tbl = doc.add_table(rows=0, cols=2)
    tbl.alignment = WD_TABLE_ALIGNMENT.LEFT
    tbl.style = "Table Grid"
    for k, v in rows:
        cells = tbl.add_row().cells
        cells[0].width, cells[1].width = Inches(2.0), Inches(5.0)
        _shade(cells[0], BRAND["light_gray"])
        cells[0].paragraphs[0].text = ""
        _font(cells[0].paragraphs[0].add_run(k), "Montserrat", 10, True, PURPLE)
        _font(cells[1].paragraphs[0].add_run(v or ""), size=11)
    return tbl


def consult_plan_docx(plan, client_name: str, coach_name: str, hours: dict) -> bytes:
    doc = _base_document("Pre-Consult Meeting Set Up Sheet")
    _kv_table(doc, [
        ("Client", client_name),
        ("Consult date", plan.consult_date.strftime("%B %d, %Y") if plan.consult_date else ""),
        ("Hours in contract", f"{hours['contracted']:g}"),
        ("Hours planned below", f"{hours['booked']:g}"),
        ("Coach", coach_name or ""),
    ])
    _heading(doc, "Description of the Structure of the Sessions")
    _para(doc, plan.structure_description or "")
    _heading(doc, "Consult Baseline, Goals, and Objectives")
    _kv_table(doc, [
        ("Baseline at beginning of consult program", plan.baseline),
        ("Goals for consult program (what are we trying to achieve?)", plan.goals),
        ("Objectives (tangible ways to tell the goal has been met)", plan.objectives),
    ])
    _heading(doc, "Session Structure")
    tbl = doc.add_table(rows=1, cols=5)
    tbl.style = "Table Grid"
    widths = [Inches(0.8), Inches(1.0), Inches(1.2), Inches(1.3), Inches(2.7)]
    for i, h in enumerate(["Session #", "Time duration", "Virtual or In-Person", "Date / approx.", "Topic / focus"]):
        c = tbl.rows[0].cells[i]
        c.width = widths[i]
        _shade(c, BRAND["purple"])
        _font(c.paragraphs[0].add_run(h), "Montserrat", 9.5, True, RGBColor(255, 255, 255))
    for n, s in enumerate(plan.sessions):
        cells = tbl.add_row().cells
        vals = [str(s.session_no), f"{s.duration:g} hr" if s.duration else "", s.mode or "",
                s.session_date.strftime("%m/%d/%Y") if s.session_date else (s.date_text or ""), s.topic or ""]
        for i, v in enumerate(vals):
            cells[i].width = widths[i]
            if n % 2:
                _shade(cells[i], BRAND["light_gray"])
            _font(cells[i].paragraphs[0].add_run(v), size=10.5)
    if not plan.sessions:
        _para(doc, "No sessions planned yet.", italic=True, color=GRAY, size=10)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
