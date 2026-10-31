"""UIA control tests, including a live WinForms window.

Run: python -m unittest tests.test_uia_control -v
"""
import json
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import uia_control


DUMMY_SCRIPT = """
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$form = New-Object System.Windows.Forms.Form
$form.Text = "{title}"
$form.Width = 420
$form.Height = 180
$form.StartPosition = "CenterScreen"
$form.TopMost = $true
$inputBox = New-Object System.Windows.Forms.TextBox
$inputBox.Name = "uiaTestInput"
$inputBox.AccessibleName = "UIA Test Input"
$inputBox.Width = 360
$inputBox.Top = 40
$inputBox.Left = 20
$form.Controls.Add($inputBox)
$form.Add_Shown({{ $inputBox.Focus() }})
[System.Windows.Forms.Application]::Run($form)
"""


class MissingWindowTests(unittest.TestCase):
    def test_uia_dump_tree_returns_an_error_instead_of_crashing(self):
        result = uia_control.uia_dump_tree(
            "Definitely Not An Open Window 987654")
        self.assertTrue(result.startswith("ERROR:"), result)


@unittest.skipUnless(sys.platform == "win32", "Windows only")
@unittest.skipUnless(
    getattr(uia_control, "PYWINAUTO_AVAILABLE", False),
    "pywinauto is not installed")
class LiveWindowTests(unittest.TestCase):
    def setUp(self):
        self.title = "UIA Test Window " + uuid.uuid4().hex[:8]
        script = DUMMY_SCRIPT.format(title=self.title)
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".ps1", delete=False, encoding="utf-8")
        handle.write(script)
        handle.close()
        self.script_path = Path(handle.name)
        try:
            self.proc = subprocess.Popen([
                "powershell.exe", "-NoProfile", "-STA",
                "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
                "-File", str(self.script_path),
            ])
        except Exception:
            self.script_path.unlink(missing_ok=True)
            raise
        self.addCleanup(self._cleanup)
        self.window = self._wait_for_window()
    def _cleanup(self):
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            pass
        self.script_path.unlink(missing_ok=True)

    def _wait_for_window(self):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                return uia_control.get_app_window(self.title)
            except Exception:
                time.sleep(0.25)
        self.fail("dummy UIA window did not appear")

    def test_dump_tree_lists_the_text_box(self):
        elements = uia_control.dump_tree(self.window)
        text_boxes = [
            item for item in elements
            if item.get("automation_id") == "uiaTestInput"
            or item.get("name") == "UIA Test Input"
        ]
        self.assertTrue(text_boxes, json.dumps(elements))
        self.assertEqual(text_boxes[0]["type"], "Edit")

    def test_uia_type_writes_text_that_can_be_read_back(self):
        message = uia_control.uia_type(
            f"{self.title}|UIA Test Input|hello from UIA")

        self.assertTrue(message.startswith("Typed"), message)
        self.window = uia_control.get_app_window(self.title)
        element = uia_control.find_element(self.window, "uiaTestInput")
        try:
            value = element.get_value()
        except Exception:
            value = element.window_text()
        self.assertIn("hello from UIA", value)


if __name__ == "__main__":
    unittest.main()
