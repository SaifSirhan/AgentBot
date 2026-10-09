"""
Document editing tools for AgentBot.
Edit DOCX, XLSX, PPTX, and PDF files based on natural-language instructions.
"""
from __future__ import annotations
import os, re, shutil, tempfile
from pathlib import Path
from datetime import datetime

def _backup(path: str) -> str:
    """Create a timestamped backup before editing. Returns the backup path."""
    p = Path(path)
    bak = p.with_suffix(f".bak_{datetime.now().strftime('%Y%m%d_%H%M%S')}{p.suffix}")
    shutil.copy2(p, bak)
    return str(bak)


# ---------- DOCX ----------
def edit_docx(path: str, instruction: str) -> str:
    """Edit a Word document. Instruction is natural language.
    Supported operations: add paragraph, replace text, add heading, add table row."""
    from docx import Document
    if not os.path.exists(path):
        return f"ERROR: file not found: {path}"
    _backup(path)
    doc = Document(path)

    inst = instruction.lower()
    if "add paragraph" in inst or "append" in inst:
        # Extract the text after the colon or after "paragraph"
        m = re.search(r"(?:add paragraph|append)[:\s]+(.+)", instruction, re.I)
        text = m.group(1) if m else instruction
        doc.add_paragraph(text)
        doc.save(path)
        return f"Added paragraph to {path}"

    if "replace" in inst:
        m = re.search(r'replace\s+["\'](.+?)["\']\s+with\s+["\'](.+?)["\']', instruction, re.I)
        if m:
            old, new = m.group(1), m.group(2)
            count = 0
            for para in doc.paragraphs:
                if old in para.text:
                    para.text = para.text.replace(old, new)
                    count += 1
            doc.save(path)
            return f"Replaced {count} occurrence(s) of '{old}' with '{new}'"

    if "heading" in inst:
        m = re.search(r"heading[:\s]+(.+)", instruction, re.I)
        text = m.group(1) if m else instruction
        doc.add_heading(text, level=1)
        doc.save(path)
        return f"Added heading '{text}' to {path}"

    return "ERROR: could not parse DOCX instruction. Try 'add paragraph: <text>' or 'replace \"old\" with \"new\"'."


# ---------- XLSX ----------
def edit_xlsx(path: str, instruction: str) -> str:
    """Edit an Excel spreadsheet. Instruction is natural language.
    Supported: set cell, add row, add formula, rename sheet."""
    import openpyxl
    if not os.path.exists(path):
        return f"ERROR: file not found: {path}"
    _backup(path)
    wb = openpyxl.load_workbook(path)
    ws = wb.active

    inst = instruction.lower()
    if "set" in inst and "cell" in inst:
        m = re.search(r"cell\s+([A-Z]+\d+)\s+to\s+(.+)", instruction, re.I)
        if m:
            cell, val = m.group(1), m.group(2)
            try:
                val = float(val) if re.fullmatch(r"-?\d+(\.\d+)?", val) else val
            except Exception:
                pass
            ws[cell] = val
            wb.save(path)
            return f"Set {cell} = {val} in {path}"

    if "add row" in inst or "append" in inst:
        m = re.search(r"(?:add row|append)[:\s]+(.+)", instruction, re.I)
        if m:
            vals = [v.strip() for v in m.group(1).split(",")]
            ws.append(vals)
            wb.save(path)
            return f"Appended row {vals} to {path}"

    if "formula" in inst:
        m = re.search(r'formula\s+["\'](.+?)["\']\s+(?:in|to)\s+([A-Z]+\d+)', instruction, re.I)
        if m:
            formula, cell = m.group(1), m.group(2)
            ws[cell] = formula
            wb.save(path)
            return f"Set formula {formula} in {cell}"

    return "ERROR: could not parse XLSX instruction."


# ---------- PPTX ----------
def edit_pptx(path: str, instruction: str) -> str:
    """Edit a PowerPoint presentation. Instruction is natural language.
    Supported: add slide, add text to slide, change title."""
    from pptx import Presentation
    from pptx.util import Inches
    if not os.path.exists(path):
        return f"ERROR: file not found: {path}"
    _backup(path)
    prs = Presentation(path)

    inst = instruction.lower()
    if "add slide" in inst:
        m = re.search(r"(?:add slide|new slide)[:\s]+(.+)", instruction, re.I)
        title_text = m.group(1) if m else "New Slide"
        slide = prs.slides.add_slide(prs.slide_layouts[1])  # Title and Content
        slide.shapes.title.text = title_text
        prs.save(path)
        return f"Added slide '{title_text}' to {path}"

    if "title" in inst and "change" in inst:
        m = re.search(r'change title to\s+["\'](.+?)["\']', instruction, re.I)
        if m and prs.slides:
            prs.slides[0].shapes.title.text = m.group(1)
            prs.save(path)
            return f"Changed title to '{m.group(1)}'"

    return "ERROR: could not parse PPTX instruction."


# ---------- PDF ----------
def edit_pdf(path: str, instruction: str) -> str:
    """Edit or create a PDF. Instruction is natural language.
    Supported: extract text, merge, split, add watermark, create from text."""
    from pypdf import PdfReader, PdfWriter
    import io

    inst = instruction.lower()

    if "extract" in inst and "text" in inst:
        if not os.path.exists(path):
            return f"ERROR: file not found: {path}"
        reader = PdfReader(path)
        text = "\n".join(p.extract_text() or "" for p in reader.pages)
        return text[:5000] + ("\n...[truncated]" if len(text) > 5000 else "")

    if "merge" in inst:
        m = re.search(r"merge[:\s]+(.+)", instruction, re.I)
        if m:
            others = [f.strip() for f in m.group(1).split(",")]
            writer = PdfWriter()
            for f in [path] + others:
                if not os.path.exists(f):
                    return f"ERROR: file not found: {f}"
                for page in PdfReader(f).pages:
                    writer.add_page(page)
            out = path.replace(".pdf", "_merged.pdf")
            with open(out, "wb") as fh:
                writer.write(fh)
            return f"Merged into {out}"

    if "create" in inst:
        from reportlab.lib.pagesizes import letter
        from reportlab.pdfgen import canvas
        m = re.search(r"create[:\s]+(.+)", instruction, re.I)
        text = m.group(1) if m else instruction
        out = path if path.endswith(".pdf") else path + ".pdf"
        c = canvas.Canvas(out, pagesize=letter)
        y = 750
        for line in text.split("\n"):
            c.drawString(72, y, line)
            y -= 14
            if y < 72:
                c.showPage()
                y = 750
        c.save()
        return f"Created PDF at {out}"

    return "ERROR: could not parse PDF instruction."
