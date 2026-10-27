---
name: office-master
description: Restructure Word documents and build formatted Excel workbooks from Python. Use for complex .docx re-layout, section merging, or bulk document generation (docxtpl templates with Jinja2 tags, row loops, rich text, subdocs) and for Excel reports/exports that need grouped data or cell formatting (pandas groupby plus xlsxwriter formats, freeze panes, autofilter, charts). Triggers include "restructure this Word doc", "generate N documents from a template", "group this CSV by region into a formatted xlsx", or "build an Excel report".
---

# Office Master

## Overview

Produce Word and Excel deliverables with two fixed toolchains instead of hand-editing document XML:

- **Word, complex restructuring** → `docxtpl`: author a template, render it. Never rebuild layout paragraph-by-paragraph.
- **Excel, grouping and formatting** → `pandas` for computation, `xlsxwriter` for every cell/sheet style.

Use `python-docx` only for small surgical edits (one header string, one paragraph). If the task reorders sections, repeats blocks, or generates many documents, switch to docxtpl.

## Word: template first, then render

1. Copy the target layout into `template.docx`. Keep styles, headers/footers, page setup, and images; replace only the changing text with tags.
2. Insert Jinja2 tags where content varies:
   - `{{ name }}` for values, `{% if %}` / `{% endif %}` for optional blocks.
   - `{%tr for row in rows %}` / `{%tr endfor %}` to repeat whole table rows. Keep each `tr` tag alone in its own unmerged row; docxtpl deletes the tag row and repeats what lies between.
   - `{%tc %}` for cell-level loops, `{%p %}` when the tag shares a paragraph with text, `{%r %}` for run-level.
3. Format inline text with `RichText`, not post-processing:

```python
from docxtpl import DocxTemplate, RichText, InlineImage
from docx.shared import Inches

doc = DocxTemplate("template.docx")
doc.render(
    {
        "title": "Q3 Summary",
        "items": [{"label": "Revenue", "value": 42}],
        "note": RichText("Urgent", bold=True, color="FF0000"),
        "logo": InlineImage(doc, "logo.png", width=Inches(1.2)),
    },
    autoescape=True,
)
doc.save("out.docx")
```

4. Use `doc.new_subdoc()` for repeated sections that need their own headers/footers, and `autoescape=True` whenever user data may contain `{`, `}`, `<`, `>`.
5. Keep it declarative: if a layout cannot be expressed with tags, change the template, not the code.

Pitfalls:

- Row loops fail silently when the `tr` tag is merged into a data cell. Give the tag row room of its own.
- `RichText` does not create new paragraphs. For line breaks, loop over a list of paragraphs in the template.
- Re-rendering an already rendered document leaves tags resolved; always render from the clean template.

## Excel: aggregate in pandas, format in xlsxwriter

1. Load and normalize with pandas, then group and aggregate:

```python
import pandas as pd

df = pd.read_csv("raw.csv")
summary = (
    df.groupby(["region", "product"], dropna=False)
      .agg(units=("units", "sum"), revenue=("revenue", "sum"))
      .reset_index()
      .sort_values(["region", "revenue"], ascending=[True, False])
)
```

2. Open one writer and reuse its handles:

```python
with pd.ExcelWriter("report.xlsx", engine="xlsxwriter") as writer:
    workbook = writer.book
    sheet = workbook.add_worksheet("Summary")
    writer.sheets["Summary"] = sheet
    header = workbook.add_format({"bold": True, "bg_color": "#DDEBF7", "border": 1})
    money = workbook.add_format({"num_format": "#,##0.00"})
    sheet.write_row(0, 0, list(summary.columns), header)
    summary.to_excel(writer, sheet_name="Summary", startrow=1,
                     index=False, header=False, float_format=None)
    sheet.set_column("A:B", 18)
    sheet.set_column("C:D", 14, money)
    sheet.freeze_panes(1, 0)
    sheet.autofilter(0, 0, len(summary), len(summary.columns) - 1)
```

3. Write group blocks yourself when the workbook needs section headers or subtotals: iterate groups in sorted order, track `startrow`, and write section rows with a bold format. Reserve `to_excel` for flat ranges.
4. Create each `add_format` once, before cells that use it. Reusing one format object across thousands of cells is what keeps file size small.
5. Do not mix `add_table()` with manual formats on the same range: a table overwrites existing formatting and later writes inside it raise `OverwriteError`. Choose tables for flat unformatted data, manual writes for styled reports.
6. Reach for `conditional_format()` for scale/heat rules and `workbook.add_chart()` for charts; never hand-compute colors per cell.
7. For very large outputs, pass `engine_kwargs={"options": {"constant_memory": True}}` and write strictly row by row — with constant memory, out-of-order writes are dropped.

## Verify before delivering

Word — no tags survived:

```python
import zipfile
xml = zipfile.ZipFile("out.docx").read("word/document.xml").decode("utf-8")
assert "{{" not in xml and "{%" not in xml, "unrendered tags"
```

Also confirm the repeated sections have the expected count (open the docx or count marker strings in the XML), since an empty loop renders cleanly and silently.

Excel — data round-trips:

```python
sheets = pd.read_excel("report.xlsx", sheet_name=None, engine="openpyxl")
assert "Summary" in sheets and not sheets["Summary"].empty
```

`read_excel` verifies values only, not styling. If formatting is part of the requirement, open the file or convert a sheet to an image and inspect it; say so when you could not.

## Libraries

Install only with the user's approval if missing:

```
pip install docxtpl xlsxwriter pandas
```

`docxtpl` pulls in `python-docx` and `jinja2`. `xlsxwriter` cannot read or modify existing files — for editing a workbook that already exists with styles, use `openpyxl` and state that the rule above does not apply.
