"""
Screen awareness for the agent - OCR-based, deliberately NOT a vision model.

Captures a screenshot and extracts any visible TEXT via Tesseract OCR. This
does not use a vision-capable model (LLaVA, Qwen2-VL, etc.) - those need
several extra GB of RAM loaded alongside the planning model, which given
an already-tight RAM budget would likely cause more problems than it
solves. OCR only reads text characters - it can't describe a photo, a
chart's shape, or a UI's visual layout - but it covers the actual use case
of reading an error message, a document, or any text currently on screen.

Requires:
- pip install pytesseract Pillow
- Tesseract-OCR itself, installed SEPARATELY - pytesseract is just a thin
  wrapper around the real tesseract.exe, it doesn't include it. Get the
  Windows installer from: https://github.com/UB-Mannheim/tesseract/wiki
  If it's not automatically found on PATH after installing, uncomment and
  set TESSERACT_CMD below to wherever the installer put it (commonly
  C:\\Program Files\\Tesseract-OCR\\tesseract.exe).
"""

import pytesseract
from PIL import ImageGrab

# Uncomment and adjust if pytesseract can't find tesseract.exe on its own:
pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

MAX_SCREEN_CHARS = 4000  # cap how much OCR'd text goes into a single prompt


def capture_and_read_screen():
    """
    Takes a screenshot of the whole screen and OCRs it. Returns the
    extracted text (capped in length), or an ERROR string explaining what
    went wrong - most commonly, Tesseract not being installed/found.
    """
    try:
        screenshot = ImageGrab.grab()
    except Exception as e:
        return f"ERROR: Could not capture the screen: {e}"

    try:
        text = pytesseract.image_to_string(screenshot)
    except pytesseract.TesseractNotFoundError:
        return ("ERROR: Tesseract-OCR isn't installed or isn't on PATH. "
                "Install it from https://github.com/UB-Mannheim/tesseract/wiki "
                "- if it's still not found afterward, set "
                "pytesseract.pytesseract.tesseract_cmd in screen.py to its "
                "install path.")
    except Exception as e:
        return f"ERROR: OCR failed: {e}"

    text = text.strip()
    if not text:
        return "ERROR: No readable text found on screen (it may be mostly images/video)."

    if len(text) > MAX_SCREEN_CHARS:
        text = text[:MAX_SCREEN_CHARS] + "\n...[truncated]"

    return text


if __name__ == "__main__":
    # Quick manual test: python screen.py
    # Put some text on your screen first (a browser, an error dialog, etc.)
    print(capture_and_read_screen())