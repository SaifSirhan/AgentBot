"""Crash log wiring tests (main/thread excepthooks and faulthandler)."""
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Slot  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import agent_gui_qt as gui  # noqa: E402


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


class _FailedSink(QObject):
    """A real receiver object: a bare lambda can be dropped before the emit.

    Same reason tests/test_kali_gui_relay.py uses one — the assertion that a
    signal fired is only worth making against something Qt will keep alive.
    """

    def __init__(self, calls):
        super().__init__()
        self.calls = calls

    @Slot(str)
    def on_failed(self, text):
        self.calls.append("failed")


class _FaultRecorder:
    """Stands in for the faulthandler module: records instead of scheduling."""

    def __init__(self, calls):
        self.calls = calls
        self.arm = None

    def enable(self, *args, **kwargs):
        pass

    def dump_traceback_later(self, timeout, repeat=False, file=None):
        self.arm = (timeout, repeat, file)
        self.calls.append("arm")

    def cancel_dump_traceback_later(self):
        self.calls.append("cancel")


class _RecordingAgent:
    """One turn that reports where it sits in the arm/turn/cancel sequence."""

    def __init__(self, calls, error=None):
        self.calls = calls
        self.error = error

    def run_agent_turn(self, text, history, on_step=None, stop_event=None):
        self.calls.append("turn")
        if self.error is not None:
            raise self.error
        return ["Done: ok"]

    def save_conversation_history(self, history):
        pass


class TurnWatchdogTests(unittest.TestCase):
    """A turn that never returns has to write its own stacks to crash.log.

    The hang this exists for left nothing behind: no reply, no dump, and a GUI
    stuck on "Thinking…" until the app was closed. These tests use the real
    faulthandler where they can, because the wiring that matters is that the
    stacks land in the crash log rather than in the void a windowed process
    calls its stderr.
    """

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.path = Path(self._directory.name) / "crash.log"
        self.handle = open(self.path, "a", encoding="utf-8")
        # Cleanups run in reverse: the timer is disarmed before the handle
        # closes, because faulthandler writes through the raw descriptor and a
        # timer left armed on a closed fd can land on whatever descriptor
        # reuses that number.
        self.addCleanup(self.handle.close)
        self.addCleanup(gui.end_turn_watchdog)
        patcher = mock.patch.object(gui, "_crash_log_handle", self.handle)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_an_over_budget_turn_writes_every_thread_stack_to_the_log(self):
        gui.arm_turn_watchdog(0.05)
        time.sleep(0.6)
        text = self.path.read_text(encoding="utf-8")
        # faulthandler heads each dump with `Timeout (<budget>!` and prints one
        # `Thread 0x...` block per thread; the dumping thread is its own
        # watchdog, so the only way this file appears is a cross-thread dump.
        self.assertEqual(text.count("Timeout ("), 1)
        self.assertIn("test_crash_logging.py", text)

    def test_one_dump_per_turn_not_one_per_interval(self):
        gui.arm_turn_watchdog(0.05)
        time.sleep(0.6)
        gui.end_turn_watchdog()
        text = self.path.read_text(encoding="utf-8")
        self.assertEqual(text.count("Timeout ("), 1)

    def test_cancelling_disarms_it_so_an_idle_app_never_dumps(self):
        gui.arm_turn_watchdog(0.05)
        gui.end_turn_watchdog()
        time.sleep(0.3)
        self.assertEqual(self.path.read_text(encoding="utf-8").strip(), "")

    def test_watchdog_stays_off_without_a_crash_log(self):
        calls = []
        recorder = _FaultRecorder(calls)
        with mock.patch.object(gui, "_crash_log_handle", None), \
                mock.patch.object(gui, "faulthandler", recorder):
            gui.arm_turn_watchdog(0.05)
        self.assertEqual(calls, [])

    def test_a_turn_arms_the_watchdog_and_disarms_it_when_it_returns(self):
        calls = []
        recorder = _FaultRecorder(calls)
        agent = _RecordingAgent(calls)
        with mock.patch.object(gui, "get_agent", return_value=agent), \
                mock.patch.object(gui, "faulthandler", recorder):
            worker = gui.AgentWorker("hello", [])
            worker.start()
            worker._thread.join(10)
        self.assertFalse(worker._thread.is_alive())
        self.assertEqual(calls, ["arm", "turn", "cancel"])
        self.assertEqual(recorder.arm,
                         (gui.TURN_WATCHDOG_SECONDS, False, self.handle))

    def test_a_turn_that_raises_still_disarms(self):
        calls = []
        recorder = _FaultRecorder(calls)
        agent = _RecordingAgent(calls, error=RuntimeError("boom"))
        with mock.patch.object(gui, "get_agent", return_value=agent), \
                mock.patch.object(gui, "faulthandler", recorder):
            worker = gui.AgentWorker("hello", [])
            sink = _FailedSink(calls)
            self._sink = sink
            worker.failed.connect(sink.on_failed)
            worker.start()
            worker._thread.join(10)
        # AgentWorker lives in the main thread and emits from the worker
        # thread, so every one of its signals is queued: the GUI event loop
        # that normally drains it is not running here.
        self.assertEqual([call for call in calls
                          if call in ("arm", "turn", "cancel")],
                         ["arm", "turn", "cancel"])
        self.app.processEvents()
        self.assertIn("failed", calls)

    def test_the_budget_outlives_one_provider_request(self):
        """A single slow request must not look like a hung turn."""
        import agent

        self.assertGreater(gui.TURN_WATCHDOG_SECONDS, agent.LLM_TIMEOUT)


if __name__ == "__main__":
    unittest.main()
