"""Custom title bar wiring (offscreen Qt).

The bar is the only way to move, minimise, maximise or close the window once the
OS caption is gone, so its buttons are driven for real here rather than mocked:
each one is clicked and the resulting window state is asserted. The native
message plumbing is covered by tests/test_window_chrome.py and, on a live
desktop, by scratch/probe_native.py.

Run: python -m unittest tests.test_gui_chrome -v
"""
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).parent.parent))

from PySide6.QtCore import QEvent, QPoint, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import agent_gui_qt as g  # noqa: E402


def mouse_event(event_type, buttons, position=None):
    position = position or QPoint(12, 12)
    # The 6-argument form: the 5-argument overload is deprecated in Qt 6.
    return QMouseEvent(event_type, position.toPointF(), position.toPointF(),
                       Qt.MouseButton.LeftButton, buttons,
                       Qt.KeyboardModifier.NoModifier)


class TitleBarPresenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = g.AgentWindow()
        self.window.show()
        for _ in range(10):
            self.app.processEvents()
        self.addCleanup(self.window.deleteLater)

    def tearDown(self):
        self.window.close()
        self.app.processEvents()

    def test_bar_exists_only_where_the_chrome_is_implemented(self):
        if g._FRAMELESS_SUPPORTED:
            self.assertIsInstance(self.window._title_bar, g.TitleBar)
            self.assertEqual(self.window._title_bar.height(), g.TITLEBAR_H)
        else:
            self.assertIsNone(self.window._title_bar)

    def test_bar_sits_above_the_chat(self):
        bar = self.window._title_bar
        if bar is None:
            self.skipTest("frameless chrome is Windows-only")
        self.assertEqual(bar.mapTo(self.window, QPoint(0, 0)).y(), 0)

    def test_bar_carries_settings_and_the_three_window_controls(self):
        bar = self.window._title_bar
        if bar is None:
            self.skipTest("frameless chrome is Windows-only")
        for name in ("settings_button", "min_button", "max_button",
                     "close_button"):
            self.assertTrue(hasattr(bar, name), name)
        self.assertEqual(bar.close_button.objectName(), "titleBarClose")

    def test_window_still_has_its_controls_when_the_bar_is_absent(self):
        # The toggle is used by the bar and must exist regardless of platform.
        self.assertTrue(callable(self.window.toggle_maximize))
        self.assertTrue(callable(self.window.nativeEvent))


class TitleBarButtonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = g.AgentWindow()
        self.window.resize(1100, 700)
        self.window.show()
        for _ in range(10):
            self.app.processEvents()
        self.bar = self.window._title_bar
        if self.bar is None:
            self.skipTest("frameless chrome is Windows-only")
        self.addCleanup(self.window.deleteLater)

    def tearDown(self):
        self.window.showNormal()
        self.window.close()
        self.app.processEvents()

    def test_minimise_button_minimises(self):
        self.bar.min_button.click()
        self.app.processEvents()
        self.assertTrue(self.window.isMinimized())

    def test_maximise_button_maximises_then_restores(self):
        self.bar.max_button.click()
        self.app.processEvents()
        self.assertTrue(self.window.isMaximized())
        self.bar.max_button.click()
        self.app.processEvents()
        self.assertFalse(self.window.isMaximized())

    def test_maximise_glyph_tracks_the_window_state(self):
        self.assertEqual(self.bar.max_button._icon_name, "win_max")
        self.window.showMaximized()
        self.app.processEvents()
        self.assertEqual(self.bar.max_button._icon_name, "win_restore")
        self.assertEqual(self.bar.max_button.toolTip(), "Restore")
        self.window.showNormal()
        self.app.processEvents()
        self.assertEqual(self.bar.max_button._icon_name, "win_max")

    def test_close_button_withdraws_without_quitting(self):
        # closeEvent ignores the close and withdraws to the tray; the window
        # object must survive, because a tray-resident app never exits here.
        self.bar.close_button.click()
        self.app.processEvents()
        self.assertTrue(self.window.isMinimized() or self.window.isHidden())

    def test_close_falls_back_to_minimise_when_there_is_no_tray(self):
        self.assertIsNone(self.window._tray_icon)
        self.bar.close_button.click()
        self.app.processEvents()
        self.assertTrue(self.window.isMinimized())
        self.assertFalse(self.window.isHidden())

    def test_close_hides_when_a_tray_icon_is_available(self):
        class FakeTray:
            def isVisible(self):
                return True

        self.window._tray_icon = FakeTray()
        self.bar.close_button.click()
        self.app.processEvents()
        self.assertTrue(self.window.isHidden())

    def test_settings_button_opens_settings(self):
        calls = []
        self.window.open_settings = lambda: calls.append(True)
        self.bar.settings_button.click()
        self.assertEqual(calls, [True])

    def test_double_click_on_the_bar_toggles_maximise(self):
        self.bar.mouseDoubleClickEvent(
            mouse_event(QEvent.Type.MouseButtonDblClick,
                        Qt.MouseButton.LeftButton))
        self.app.processEvents()
        self.assertTrue(self.window.isMaximized())

    def test_press_with_no_native_move_loop_is_ignored_not_crashed(self):
        # Offscreen cannot start the native move loop; startSystemMove() returns
        # False and the press must fall through instead of eating the event.
        self.bar.mousePressEvent(
            mouse_event(QEvent.Type.MouseButtonPress,
                        Qt.MouseButton.LeftButton))
        self.app.processEvents()
        self.assertFalse(self.window.isMaximized())


class TitleBarTitleSyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = g.AgentWindow()
        self.window.resize(900, 600)
        self.window.show()
        for _ in range(10):
            self.app.processEvents()
        self.bar = self.window._title_bar
        if self.bar is None:
            self.skipTest("frameless chrome is Windows-only")
        self.addCleanup(self.window.deleteLater)

    def tearDown(self):
        self.window.close()
        self.app.processEvents()

    def test_window_title_change_reaches_the_bar(self):
        self.window.setWindowTitle("Agent — Thinking")
        self.app.processEvents()
        self.assertIn("Thinking", self.bar.title_label.text())

    def test_status_updates_are_mirrored(self):
        self.window.set_status("Ready")
        self.app.processEvents()
        self.assertIn("Ready", self.bar.title_label.text())

    def test_a_long_title_is_elided_not_spilled_onto_the_buttons(self):
        self.window.setWindowTitle("Agent — " + "x" * 400)
        self.app.processEvents()
        self.assertLessEqual(self.bar.title_label.width(),
                             self.bar.width())
        self.assertTrue(self.bar.close_button.isVisible())


if __name__ == "__main__":
    unittest.main(verbosity=2)
