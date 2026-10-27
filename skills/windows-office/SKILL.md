---
name: windows-office
description: "Word/Excel tasks: COM tools first, OS Control second, script last."
---

# Windows Office tasks

Work down this ladder and stop at the first rung that covers the request.
Never refuse an Office task just because rung 1 has no matching tool.

## 1. Native COM / Office tools (first)

- `read_word_doc("C:\path\file.docx")` — every paragraph of text, read-only.
  It attaches to the running Word or starts a hidden one, then closes the
  document without saving.
- `set_word_language("en-US")` — Word's default language for NEW documents.
  It changes the Normal template, not the text already in an open file.
- `edit_document("C:\path\file.xlsx|set cell B2 to 1500")` — python-docx,
  openpyxl, python-pptx and PDF edits; it backs the file up first.

Prefer these when they exist: fast, reversible, and they leave the user's
screen alone.

## 2. OS Control (second)

When the request is a setting or command with no API tool, drive the real UI:

1. `screenshot_background()` — see the current screen.
2. `click_coordinates("x,y")` or `click_coordinates("x,y | App title")`.
3. `type_text("text{enter}")` — type, then submit.

Say what you are about to click before clicking, and confirm first when the
step is destructive or irreversible.

## 3. Suggested tool (last)

If the native route is missing and OS Control would be fragile — complex file
manipulation, missing dependencies, or a long chain of fragile clicks — stop
and hand the user a script:

```
[SUGGESTED_TOOL] Rename every sheet in a workbook to its creation date.
```python
import openpyxl, datetime
wb = openpyxl.load_workbook(r"C:\path\book.xlsx")
for ws in wb.worksheets:
    ws.title = datetime.date.today().isoformat()
wb.save(r"C:\path\book.xlsx")
```
```

Rules for the script:

- Start the answer with `[SUGGESTED_TOOL]` exactly, so the user can find it.
- Include imports and a runnable path; no `<TODO>` placeholders.
- Explain in one line what it changes and where.
- Never claim the script was run or that the task is done.

## Reporting

Quote the tool result in the reply. If it starts with `ERROR`, report the
failure plainly; only say "done" after a successful result.
