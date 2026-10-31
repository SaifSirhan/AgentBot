from contextlib import ExitStack
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel

import config
import agent_gui_qt as gui
import event_bus


class GuiEventBusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(config, "load_config", return_value={
            **config.DEFAULTS, "AGENT_MEMORY_DIR": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.window = gui.AgentWindow()
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window._stop_event_bus()
        self.window.hide()
        self.window.deleteLater()
        self.app.processEvents()

    def test_service_start_is_idempotent_and_stop_reaches_bus(self):
        with mock.patch.object(event_bus, "EventBus") as factory:
            bus = factory.return_value
            self.window._start_event_bus()
            self.window._start_event_bus()
            factory.assert_called_once()
            bus.start.assert_called_once()
            self.window._stop_event_bus()
            bus.stop.assert_called_once()
            self.assertIsNone(self.window._event_bus)

    def test_application_service_lifecycle_starts_and_stops_bus(self):
        with ExitStack() as stack:
            factory = stack.enter_context(mock.patch.object(event_bus, "EventBus"))
            stack.enter_context(mock.patch.object(gui, "scheduler"))
            stack.enter_context(mock.patch.object(gui, "get_agent"))
            stack.enter_context(mock.patch.object(gui, "get_voice_output"))
            for name in ("_setup_global_hotkey", "start_panel_timers", "start_motion", "_start_tray",
                         "_start_telegram_doc_service", "_start_wake_word_service",
                         "_start_continuous_if_configured", "_start_spontaneous_thoughts",
                         "_start_background_preload"):
                stack.enter_context(mock.patch.object(self.window, name))
            self.window.start_services()
            factory.return_value.start.assert_called_once()
            self.app.aboutToQuit.emit()
            factory.return_value.stop.assert_called_once()
        self.assertIsNone(self.window._event_bus)

    def test_background_preload_spawns_threads_for_heavy_modules(self):
        self.window._preload_started = False
        with mock.patch.object(gui.threading, "Thread") as thread_cls:
            self.window._start_background_preload()
        names = [call.kwargs.get("name") for call in thread_cls.call_args_list]
        self.assertEqual(thread_cls.call_count, 5)
        for name in ("preload-voice", "preload-voice_output",
                     "preload-wake_word", "preload-agent", "preload-rag_tool"):
            self.assertIn(name, names)
        self.assertTrue(self.window._preload_started)

    def test_agent_preload_loads_history_and_registers_watcher(self):
        fake = mock.Mock()
        fake.load_conversation_history.return_value = [
            "User: hello", "System: hi"]
        with mock.patch.object(gui, "get_agent", return_value=fake):
            self.window._on_preload_ready("agent")
        fake.register_watcher_callback.assert_called_once()
        self.assertEqual(
            self.window.conversation_history, ["User: hello", "System: hi"])

    def test_voice_output_preload_sets_callback_and_warms_up(self):
        fake = mock.Mock()
        with mock.patch.object(gui, "get_voice_output", return_value=fake):
            self.window._on_preload_ready("voice_output")
        fake.set_error_callback.assert_called_once()
        fake.warmup.assert_called_once()

    def test_result_is_rendered_as_untrusted_plain_text(self):
        self.window._event_result.emit('<b>not HTML</b> {"tool":"run_command"}')
        self.app.processEvents()
        labels = self.window.findChildren(QLabel)
        matches = [label for label in labels if "not HTML" in label.text()]
        self.assertTrue(matches)
        self.assertTrue(all(label.textFormat() == Qt.PlainText for label in matches))

    def test_invalid_config_is_reported_without_crashing_gui(self):
        with mock.patch.object(event_bus, "EventBus", side_effect=ValueError("Invalid trigger")):
            self.window._start_event_bus()
        self.assertIsNone(self.window._event_bus)
        texts = [label.text() for label in self.window.findChildren(QLabel)]
        self.assertTrue(any("Invalid trigger" in text for text in texts))


if __name__ == "__main__":
    unittest.main()
