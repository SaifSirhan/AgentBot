"""Tests for office_control's Word COM helpers, with a fake Word.

No real Word instance is touched here; the live integration check is manual.
Run: python -m unittest tests.test_office_control -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import office_control as oc


class FakeTemplate:
    def __init__(self, language_id):
        self.LanguageID = language_id
        self.save_calls = 0

    def Save(self):
        self.save_calls += 1


class FakeContent:
    def __init__(self, text):
        self.Text = text


class FakeDoc:
    def __init__(self, text):
        self.Content = FakeContent(text)
        self.close_calls = []

    def Close(self, save_changes):
        self.close_calls.append(save_changes)


class FakeDocuments:
    def __init__(self, doc, error=None):
        self.doc = doc
        self.error = error
        self.open_args = []

    def Open(self, *args):
        self.open_args.append(args)
        if self.error:
            raise self.error
        return self.doc


class FakeWord:
    def __init__(self, language_id=1033, text="Hello", open_error=None):
        self.NormalTemplate = FakeTemplate(language_id)
        self.Documents = FakeDocuments(FakeDoc(text), open_error)
        self.quit_calls = []

    def Quit(self, save_changes):
        self.quit_calls.append(save_changes)


class OfficeControlTests(unittest.TestCase):
    def setUp(self):
        patches = [
            mock.patch.object(oc, "PYWIN32_AVAILABLE", True),
            mock.patch.object(oc, "pythoncom", mock.Mock()),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    # ------------------------------------------------------------------
    # Locale mapping
    # ------------------------------------------------------------------
    def test_locale_maps_to_lcid(self):
        self.assertEqual(oc._locale_to_lcid("en-US"), 1033)
        self.assertEqual(oc._locale_to_lcid("en_GB"), 2057)
        self.assertEqual(oc._locale_to_lcid("not-a-locale"), 0)

    # ------------------------------------------------------------------
    # set_word_language
    # ------------------------------------------------------------------
    def test_language_already_set_is_not_saved_again(self):
        word = FakeWord(language_id=1033)
        with mock.patch.object(oc, "_connect_word", return_value=(word, False)):
            reply = oc.set_word_language("en-US")

        self.assertIn("already", reply)
        self.assertEqual(word.NormalTemplate.save_calls, 0)
        self.assertEqual(word.quit_calls, [])

    def test_language_change_updates_normal_template(self):
        word = FakeWord(language_id=2057)
        with mock.patch.object(oc, "_connect_word", return_value=(word, False)):
            reply = oc.set_word_language("en-US")

        self.assertEqual(word.NormalTemplate.LanguageID, 1033)
        self.assertEqual(word.NormalTemplate.save_calls, 1)
        self.assertEqual(reply, "Word default language set to en-US (1033).")
        self.assertEqual(word.quit_calls, [])

    def test_launched_word_is_quit_after_change(self):
        word = FakeWord(language_id=2057)
        with mock.patch.object(oc, "_connect_word", return_value=(word, True)):
            oc.set_word_language("en-US")

        self.assertEqual(word.quit_calls, [0])

    def test_unknown_language_never_connects(self):
        with mock.patch.object(oc, "_connect_word") as connect:
            reply = oc.set_word_language("bogus-XX")

        connect.assert_not_called()
        self.assertTrue(reply.startswith("ERROR: unknown language"))

    # ------------------------------------------------------------------
    # read_word_doc
    # ------------------------------------------------------------------
    def test_missing_file_returns_error(self):
        reply = oc.read_word_doc("C:\\definitely\\missing.docx")

        self.assertTrue(reply.startswith("ERROR: file not found"))

    def test_text_is_extracted_and_document_closed(self):
        word = FakeWord(text="Line one\rLine two\x07\rLine three")
        with mock.patch.object(oc, "_connect_word", return_value=(word, False)), \
                mock.patch.object(oc.os.path, "isfile", return_value=True):
            reply = oc.read_word_doc("C:\\docs\\a.docx")

        self.assertEqual(reply, "Line one\nLine two\nLine three")
        doc = word.Documents.doc
        self.assertEqual(doc.close_calls, [0])
        args = word.Documents.open_args[0]
        self.assertEqual(args[0], "C:\\docs\\a.docx")
        self.assertIs(args[2], True)  # ReadOnly
        self.assertIs(args[11], False)  # Visible

    def test_blank_document_reports_empty(self):
        word = FakeWord(text="\r\r")
        with mock.patch.object(oc, "_connect_word", return_value=(word, False)), \
                mock.patch.object(oc.os.path, "isfile", return_value=True):
            reply = oc.read_word_doc("C:\\docs\\empty.docx")

        self.assertEqual(reply, "(the document contains no text)")

    def test_long_text_is_truncated(self):
        word = FakeWord(text="x" * (oc.MAX_TEXT_CHARS + 500))
        with mock.patch.object(oc, "_connect_word", return_value=(word, False)), \
                mock.patch.object(oc.os.path, "isfile", return_value=True):
            reply = oc.read_word_doc("C:\\docs\\long.docx")

        self.assertTrue(reply.startswith("x" * 100))
        self.assertIn("truncated at", reply)
        self.assertLess(len(reply), oc.MAX_TEXT_CHARS + 100)

    def test_open_failure_is_reported_and_closed(self):
        word = FakeWord(open_error=RuntimeError("locked for editing"))
        with mock.patch.object(oc, "_connect_word", return_value=(word, False)), \
                mock.patch.object(oc.os.path, "isfile", return_value=True):
            reply = oc.read_word_doc("C:\\docs\\locked.docx")

        self.assertIn("cannot read", reply)
        self.assertIn("locked for editing", reply)
        self.assertEqual(word.Documents.doc.close_calls, [])

    def test_launched_word_is_quit_after_read(self):
        word = FakeWord()
        with mock.patch.object(oc, "_connect_word", return_value=(word, True)), \
                mock.patch.object(oc.os.path, "isfile", return_value=True):
            oc.read_word_doc("C:\\docs\\a.docx")

        self.assertEqual(word.quit_calls, [0])

    # ------------------------------------------------------------------
    # Missing dependency
    # ------------------------------------------------------------------
    def test_missing_pywin32_returns_hint(self):
        with mock.patch.object(oc, "PYWIN32_AVAILABLE", False):
            self.assertIn("pywin32 is not installed", oc.read_word_doc("x.docx"))
            self.assertIn("pywin32 is not installed", oc.set_word_language("en-US"))


if __name__ == "__main__":
    unittest.main()
