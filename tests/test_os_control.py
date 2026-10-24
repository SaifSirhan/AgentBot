"""Tests for os_control.py using fake MSS/PyAutoGUI surfaces.

No real screen capture, mouse movement, or typing occurs.

Run: python -m pytest tests/test_os_control.py -v
Or:  python tests/test_os_control.py
"""
import re
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import os_control as oc


class FakeShot:
    def __init__(self, size=(100, 50), pixel=(1, 2, 3)):
        self.size = size
        self._pixel = pixel
        self.rgb = b"\x00\x00\x00\x00" * (size[0] * size[1])

    def pixel(self, x, y):
        return self._pixel


class FakeSCT:
    def __init__(self, shot):
        self.shot = shot
        self.monitors = [
            {},
            {"left": 0, "top": 0, "width": 100, "height": 50},
        ]
        self.grabs = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def grab(self, monitor):
        self.grabs.append(dict(monitor))
        return self.shot


class FakeMSSModule:
    def __init__(self):
        self.sct = FakeSCT(FakeShot())
        self.tools = types.SimpleNamespace(
            to_png=lambda rgb, size: b"PNG:" + str(size).encode())

    def mss(self):
        return self.sct


class OSControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="os_control_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.fake_mss = FakeMSSModule()

        self.patches = [
            mock.patch.object(oc, "_load_mss", return_value=self.fake_mss),
            mock.patch.object(oc, "SCREENSHOT_DIR", self.tmp),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

        oc.set_gui_visibility_handlers(None, None)
        oc.reset_kill_switch()
        with oc._kill_lock:
            oc._kill_callbacks.clear()

    def tearDown(self):
        oc.reset_kill_switch()
        oc.set_gui_visibility_handlers(None, None)
        oc.stop_kill_switch()
        with oc._kill_lock:
            oc._kill_callbacks.clear()

    def mock_gui(self):
        calls = []
        oc.set_gui_visibility_handlers(
            lambda: calls.append("hide"),
            lambda: calls.append("show"),
        )
        return calls

    # ------------------------------------------------------------------
    # DPI / coordinates
    # ------------------------------------------------------------------
    def test_dpi_info(self):
        info = oc.dpi_info()
        self.assertGreaterEqual(info["scale"], 1.0)
        self.assertIn(info["awareness"], (-1, 0, 1, 2))

    def test_click_uses_monitor_offset(self):
        self.fake_mss.sct.monitors[1] = {
            "left": 100, "top": 50, "width": 100, "height": 50}
        with mock.patch.object(oc.pyautogui, "moveTo") as move, \
             mock.patch.object(oc.pyautogui, "click") as click:
            out = oc.click_coordinates("10, 20")

        move.assert_called_once_with(110, 70, duration=0.1)
        click.assert_called_once()
        self.assertIn("Clicked (10, 20)", out)
        self.assertIn("(110, 70)", out)

    def test_click_rejects_bad_coordinates(self):
        with mock.patch.object(oc.pyautogui, "moveTo") as move:
            for value in ("abc", "10", "1,2,3", "-1,0", "100,0", "0,50"):
                out = oc.click_coordinates(value)
                self.assertTrue(out.startswith("ERROR:"), (value, out))
        move.assert_not_called()

    def test_failsafe_reports_aborted(self):
        with mock.patch.object(oc.pyautogui, "moveTo",
                               side_effect=oc.pyautogui.FailSafeException):
            out = oc.click_coordinates("1,1")
        self.assertTrue(out.startswith("ABORTED:"), out)
        self.assertIn("failsafe", out.lower())

    # ------------------------------------------------------------------
    # Screenshot
    # ------------------------------------------------------------------
    def test_screenshot_hides_restores_and_saves_png(self):
        calls = self.mock_gui()
        with mock.patch.object(oc.time, "sleep"):
            out = oc.screenshot_background()

        self.assertEqual(calls, ["hide", "show"])
        self.assertIn("GUI: hidden", out)
        self.assertIn("Image: 100x50", out)
        self.assertIn("DPI:", out)
        match = re.search(r"Screenshot saved: (.+)", out)
        self.assertIsNotNone(match, out)
        path = Path(match.group(1))
        self.assertTrue(path.exists())
        self.assertEqual(path.read_bytes(), b"PNG:(100, 50)")
        self.assertEqual(self.fake_mss.sct.grabs[0]["width"], 100)

    def test_screenshot_abort_still_restores_gui(self):
        calls = []

        def hide():
            calls.append("hide")
            oc.trigger_kill_switch()

        oc.set_gui_visibility_handlers(hide, lambda: calls.append("show"))
        with mock.patch.object(oc.time, "sleep"):
            out = oc.screenshot_background()

        self.assertTrue(out.startswith("ABORTED:"), out)
        self.assertEqual(calls, ["hide", "show"])

    def test_screenshot_without_mss_does_not_touch_gui(self):
        calls = self.mock_gui()
        with mock.patch.object(
            oc, "_load_mss",
            side_effect=oc.OSControlUnavailable("mss missing"),
        ):
            out = oc.screenshot_background()

        self.assertTrue(out.startswith("ERROR:"), out)
        self.assertIn("mss missing", out)
        self.assertEqual(calls, [])

    def test_hide_failure_is_reported_but_capture_continues(self):
        calls = []

        def hide():
            calls.append("hide")
            raise RuntimeError("timeout")

        oc.set_gui_visibility_handlers(hide, lambda: calls.append("show"))
        with mock.patch.object(oc.time, "sleep"):
            out = oc.screenshot_background()

        self.assertIn("GUI: hide failed", out)
        self.assertEqual(calls, ["hide", "show"])

    # ------------------------------------------------------------------
    # Pixel colour
    # ------------------------------------------------------------------
    def test_pixel_color_hex(self):
        self.fake_mss.sct.shot._pixel = (0x12, 0x34, 0xAB)
        with mock.patch.object(oc.pyautogui, "moveTo") as move:
            out = oc.get_pixel_color("5,6")
        self.assertEqual(out, "#1234AB at (5, 6)")
        self.assertEqual(self.fake_mss.sct.grabs[-1],
                         {"left": 5, "top": 6, "width": 1, "height": 1})
        move.assert_not_called()

    def test_pixel_color_bounds(self):
        out = oc.get_pixel_color("100,10")
        self.assertTrue(out.startswith("ERROR:"), out)

    # ------------------------------------------------------------------
    # Typing
    # ------------------------------------------------------------------
    def test_type_ascii_uses_write(self):
        with mock.patch.object(oc.pyautogui, "write") as write:
            out = oc.type_text("hello")
        write.assert_called_once_with("hello", interval=0.01)
        self.assertIn("Typed 5", out)

    def test_type_long_ascii_is_chunked(self):
        with mock.patch.object(oc.pyautogui, "write") as write:
            out = oc.type_text("a" * 100)
        self.assertEqual([c.args[0] for c in write.call_args_list],
                         ["a" * 40, "a" * 40, "a" * 20])
        self.assertIn("Typed 100", out)

    def test_type_unicode_uses_clipboard_and_restores(self):
        copied = []
        with mock.patch.object(oc.pyperclip, "paste", return_value="previous"), \
             mock.patch.object(oc.pyperclip, "copy", side_effect=copied.append), \
             mock.patch.object(oc.pyautogui, "hotkey") as hotkey, \
             mock.patch.object(oc.time, "sleep"):
            out = oc.type_text("h\u00e9llo\nworld")

        self.assertEqual(copied, ["h\u00e9llo\nworld", "previous"])
        hotkey.assert_called_once_with("ctrl", "v")
        self.assertIn("Pasted", out)

    def test_type_rejects_empty_and_oversized(self):
        self.assertTrue(oc.type_text("").startswith("ERROR:"))
        self.assertTrue(oc.type_text("x" * (oc.MAX_TYPE_CHARS + 1)).startswith("ERROR:"))

    # ------------------------------------------------------------------
    # Kill switch
    # ------------------------------------------------------------------
    def test_kill_switch_blocks_every_tool(self):
        oc.trigger_kill_switch()
        self.assertTrue(oc.screenshot_background().startswith("ABORTED:"))
        self.assertTrue(oc.click_coordinates("1,1").startswith("ABORTED:"))
        self.assertTrue(oc.type_text("hello").startswith("ABORTED:"))
        self.assertTrue(oc.get_pixel_color("1,1").startswith("ABORTED:"))

        oc.reset_kill_switch()
        with mock.patch.object(oc.pyautogui, "moveTo"), \
             mock.patch.object(oc.pyautogui, "click"):
            self.assertIn("Clicked", oc.click_coordinates("1,1"))

    def test_kill_callbacks_fire(self):
        called = []
        callback = lambda: called.append("stop")
        oc.add_kill_callback(callback)
        oc.add_kill_callback(callback)  # deduplicated by identity
        oc.trigger_kill_switch()
        self.assertEqual(called, ["stop"])

    def test_watcher_starts_and_stops(self):
        self.assertTrue(oc.ensure_kill_switch())
        self.assertTrue(oc.ensure_kill_switch())  # idempotent
        oc.stop_kill_switch()
        self.assertIsNone(oc._kill_thread)


if __name__ == "__main__":
    unittest.main(verbosity=2)
