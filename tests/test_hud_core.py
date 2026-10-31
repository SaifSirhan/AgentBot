"""ReactorHud and chat-background wiring tests (offscreen Qt).

Run: python -m unittest tests.test_hud_core -v
"""
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QSizePolicy  # noqa: E402

from audio_bus import AudioBus  # noqa: E402
from hud_core import ReactorHud  # noqa: E402

STATES = ("IDLE", "LISTENING", "THINKING", "SPEAKING", "ERROR")


class ReactorHudTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.bus = AudioBus()
        self.hud = ReactorHud(bus=self.bus)
        self.hud.show()
        self.app.processEvents()

    def tearDown(self):
        self.hud.hide()
        self.hud.deleteLater()
        self.app.processEvents()

    def test_defaults_to_240_square_and_idle(self):
        self.assertEqual(self.hud.width(), 240)
        self.assertEqual(self.hud.height(), 240)
        self.assertEqual(self.hud.state, "IDLE")

    def test_set_state_accepts_every_state_and_renders(self):
        for state in STATES:
            with self.subTest(state=state):
                self.hud.set_state(state)
                self.app.processEvents()
                self.assertEqual(self.hud.state, state)
                pixmap = self.hud.grab()
                self.assertFalse(pixmap.isNull())
                self.assertEqual((pixmap.width(), pixmap.height()), (240, 240))

    def test_unknown_state_falls_back_to_idle(self):
        self.hud.set_state("NOPE")
        self.assertEqual(self.hud.state, "IDLE")

    def test_published_audio_raises_the_voice_level_while_speaking(self):
        self.hud.set_state("SPEAKING")
        self.bus.publish(np.full(1024, 0.5, dtype=np.float32), "tts")
        QTest.qWait(140)
        self.assertGreater(self.hud.level, 0.05)


class CoreCardIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_right_panel_keeps_core_first_without_an_inline_hud(self):
        import agent_gui_qt as gui

        panel = gui.RightPanel()
        try:
            self.assertEqual(panel.CARD_TITLES[0], "CORE")
            self.assertIn("CORE", panel.cards)
            self.assertFalse(panel.cards["CORE"].findChildren(ReactorHud))
            self.assertGreaterEqual(panel.minimumWidth(), 250)
            for card in panel.cards.values():
                self.assertEqual(
                    card.sizePolicy().horizontalPolicy(),
                    QSizePolicy.Policy.Ignored)
        finally:
            panel.deleteLater()
            self.app.processEvents()

    def test_chat_has_a_centered_large_translucent_background_hud(self):
        import agent_gui_qt as gui

        window = gui.AgentWindow()
        try:
            hud = window.chat.background_hud
            self.assertIsInstance(hud, ReactorHud)
            self.assertEqual((hud.width(), hud.height()), (800, 800))
            self.assertIsNotNone(hud.graphicsEffect())
            self.assertAlmostEqual(
                hud.graphicsEffect().opacity(), 0.15, places=3)
            self.assertGreaterEqual(
                window.right_panel.minimumWidth(), 250)
        finally:
            window.close()
            window.deleteLater()
            self.app.processEvents()

    def test_agent_status_maps_to_hud_states(self):
        import agent_gui_qt as gui

        window = gui.AgentWindow()
        try:
            hud = window.chat.background_hud

            window.set_status("Thinking…", busy=True)
            self.assertEqual(hud.state, "THINKING")

            window._recording = True
            window.set_status("Listening…", busy=True)
            self.assertEqual(hud.state, "LISTENING")

            window._recording = False
            with mock.patch.object(gui, "_tts_is_speaking", return_value=True):
                window.set_status("Ready", busy=False)
                self.assertEqual(hud.state, "SPEAKING")

            with mock.patch.object(gui, "_tts_is_speaking", return_value=False):
                window.set_status("Ready", busy=False)
                self.assertEqual(hud.state, "IDLE")

            window._hud_error_until = time.monotonic() + 5.0
            window.set_status("Ready", busy=False)
            self.assertEqual(hud.state, "ERROR")
        finally:
            window.close()
            window.deleteLater()
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
