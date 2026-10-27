"""Native Microsoft Office automation through COM (pywin32).

Attaches to a running Word instance when there is one; otherwise starts a
hidden instance and quits it afterwards. This module is for things the
python-docx tooling in document_tools cannot do (Word settings, exact text
extraction), not a replacement for it.
"""
from __future__ import annotations

import ctypes
import gc
import os
import re

try:
    import pythoncom
    import win32com.client
    PYWIN32_AVAILABLE = True
except ImportError:
    PYWIN32_AVAILABLE = False

MAX_TEXT_CHARS = 20000
_WD_DO_NOT_SAVE = 0
_MISSING = "ERROR: pywin32 is not installed (pip install pywin32)."


def _short(exc, limit=300):
    text = str(exc).strip() or type(exc).__name__
    return text[:limit]


def _connect_word():
    """Return (word_app, launched_here) for the running or a new Word."""
    try:
        return win32com.client.GetActiveObject("Word.Application"), False
    except Exception:
        app = win32com.client.Dispatch("Word.Application")
        app.Visible = False
        return app, True


def _release_word(word, launched):
    """Quit only the instance we started, then drop the COM references."""
    try:
        if launched:
            word.Quit(_WD_DO_NOT_SAVE)
    except Exception:
        pass
    del word
    gc.collect()


_CUSTOM_LCIDS = {0x1000, 0x2000, 0x0C00}  # LOCALE_CUSTOM_* placeholders


def _locale_to_lcid(code):
    """Map a locale such as "en-US" to a WdLanguageID; 0 when unknown."""
    # Unknown but well-formed names map to LOCALE_CUSTOM_UNSPECIFIED, not 0.
    lcid = int(ctypes.windll.kernel32.LocaleNameToLCID(code.replace("_", "-"), 0))
    return 0 if lcid in _CUSTOM_LCIDS else lcid


def set_word_language(language_code="en-US"):
    """Set Word's default language for new documents (Normal template)."""
    if not PYWIN32_AVAILABLE:
        return _MISSING
    code = (language_code or "en-US").strip()
    lcid = _locale_to_lcid(code)
    if not lcid:
        return (f"ERROR: unknown language '{code}'. Use a locale code such as "
                f"en-US, en-GB, fr-FR, or ar-SA.")

    pythoncom.CoInitialize()
    try:
        try:
            word, launched = _connect_word()
        except Exception as e:
            return f"ERROR: cannot start Word: {_short(e)}"

        try:
            template = word.NormalTemplate
            current = int(template.LanguageID)
            if current == lcid:
                return f"Word's default language is already {code} ({lcid})."
            template.LanguageID = lcid
            template.Save()
            applied = int(template.LanguageID)
        except Exception as e:
            return f"ERROR: Word refused the language change: {_short(e)}"
        finally:
            _release_word(word, launched)
    finally:
        pythoncom.CoUninitialize()

    if applied != lcid:
        return f"ERROR: Word reports language {applied} instead of {lcid} ({code})."
    return f"Word default language set to {code} ({lcid})."


def _clean_text(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x07", "")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    return "\n".join(line.rstrip() for line in text.split("\n")).strip()


def read_word_doc(filepath):
    """Return all text from a Word file through Word's COM API."""
    if not PYWIN32_AVAILABLE:
        return _MISSING
    raw = (filepath or "").strip().strip('"')
    if not raw:
        return "ERROR: read_word_doc needs a file path."
    path = os.path.abspath(os.path.expanduser(raw))
    if not os.path.isfile(path):
        return f"ERROR: file not found: {path}"

    pythoncom.CoInitialize()
    word = None
    launched = False
    try:
        try:
            word, launched = _connect_word()
        except Exception as e:
            return f"ERROR: cannot start Word: {_short(e)}"

        doc = None
        try:
            # Open(FileName, ConfirmConversions, ReadOnly, AddToRecentFiles,
            #      PasswordDocument, PasswordTemplate, Revert, WritePasswordDocument,
            #      WritePasswordTemplate, Format, Encoding, Visible)
            doc = word.Documents.Open(
                path, False, True, False, "", "", True, "", "", 0, 0, False)
            text = doc.Content.Text or ""
        except Exception as e:
            return f"ERROR: cannot read '{path}': {_short(e)}"
        finally:
            if doc is not None:
                try:
                    doc.Close(_WD_DO_NOT_SAVE)
                except Exception:
                    pass
    finally:
        if word is not None:
            _release_word(word, launched)
        pythoncom.CoUninitialize()

    text = _clean_text(text)
    if len(text) > MAX_TEXT_CHARS:
        text = (text[:MAX_TEXT_CHARS]
                + f"\n[... truncated at {MAX_TEXT_CHARS} characters]")
    return text or "(the document contains no text)"
