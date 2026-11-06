"""Crash log wiring tests (main/thread excepthooks and faulthandler)."""
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import agent_gui_qt as gui


class CrashLoggingTests(unittest.TestCase):
    def test_install_logs_main_and_thread_exceptions(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = mock.Mock()
            cfg.memory_path.side_effect = (
                lambda *parts: str(Path(directory).joinpath(*parts)))
            old_main = sys.excepthook
            old_thread = threading.excepthook
            old_handle = gui._crash_log_handle
            sys.excepthook = lambda *args: None
            threading.excepthook = lambda args: None
            try:
                gui._crash_log_handle = None
                with mock.patch.object(
                        gui, "get_config_module", return_value=cfg), \
                        mock.patch.object(gui, "faulthandler") as fake_fault:
                    path = gui.install_crash_logging()
                    self.assertIsNotNone(path)
                    try:
                        raise ValueError("main boom")
                    except ValueError:
                        sys.excepthook(*sys.exc_info())
                    threading.excepthook(SimpleNamespace(
                        exc_type=ValueError,
                        exc_value=ValueError("thread boom"),
                        exc_traceback=None,
                        thread=SimpleNamespace(name="worker-test"),
                    ))
                    gui._crash_log_handle.flush()
                    text = path.read_text(encoding="utf-8")
                self.assertIn("=== Kestrel start", text)
                self.assertIn("main boom", text)
                self.assertIn("thread boom", text)
                self.assertIn("worker-test", text)
                fake_fault.enable.assert_called_once()
            finally:
                handle = gui._crash_log_handle
                gui._crash_log_handle = old_handle
                if handle is not None and handle is not old_handle:
                    handle.close()
                sys.excepthook = old_main
                threading.excepthook = old_thread


if __name__ == "__main__":
    unittest.main()
