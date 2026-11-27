"""Native main-chat Screen Copilot GUI smoke; run directly on Windows."""
import os
import sys
import threading
import time
import traceback
from pathlib import Path
from unittest import mock

os.environ.pop("QT_QPA_PLATFORM", None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QMainWindow  # noqa: E402

import agent_gui_qt as gui  # noqa: E402
import screen_copilot  # noqa: E402


SCREENSHOT = ROOT / "scratch" / "kestrel-copilot-live.png"
PROGRESS_SCREENSHOT = ROOT / "scratch" / "kestrel-copilot-progress.png"
RESULT_SCREENSHOT = ROOT / "scratch" / "kestrel-copilot-result.png"


class SmokeAgent:
    def __init__(self):
        self.progress_ready = threading.Event()
        self.allow_finish = threading.Event()
        self.approved = None

    def run_agent_turn(self, text, history, on_step=None, stop_event=None,
                       copilot_approval_handler=None):
        assert callable(copilot_approval_handler)
        assert stop_event is not None
        on_step("Copilot: Planning step 1…")
        future = copilot_approval_handler({
            "task_id": "gui-smoke-task",
            "request_id": "gui-smoke-request",
            "goal": "Search for Dune and open it",
            "target": {
                "title": "Smoke fixture",
                "process": "fixture.exe",
                "pid": 202,
                "hwnd": 101,
            },
            "max_steps": 5,
            "allowed_actions": ["click", "type_text", "vision_click"],
            "typing_rule": "Typed text must be part of the approved goal",
            "screenshot_disclosure": "Only the selected window may be captured",
            "timeout_seconds": 60,
        })
        self.approved = future.result(timeout=15)
        if not self.approved:
            return ["Done: Plan denied; no actions were executed."]
        on_step("Copilot: Performing step 2…")
        self.progress_ready.set()
        if not self.allow_finish.wait(timeout=15):
            raise TimeoutError("GUI smoke did not release the completed turn")
        return [
            "Copilot: Step 2 finished; checking the window…",
            "Done: Screen Copilot status: completed — the planner marked the "
            "goal done; this is not independent proof of an external effect.",
        ]

    def save_conversation_history(self, history):
        pass


def save_screenshot(window, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    assert window.grab().save(str(path))
    assert path.stat().st_size > 10_000


def wait_until(app, predicate, description, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        QTest.qWait(10)
    raise AssertionError(f"Timed out waiting for {description}")


def main():
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    app.setFont(gui.terminal_font())
    app.setStyleSheet(gui.theme_stylesheet(gui.read_theme_name()))
    window = gui.AgentWindow()
    window.resize(1480, 820)
    window.show()
    smoke_agent = SmokeAgent()

    with mock.patch.object(gui, "get_agent", return_value=smoke_agent), \
            mock.patch.object(
                window, "_speak_reply",
                side_effect=lambda _message: window._set_voice_state("idle")), \
            mock.patch.object(window, "_schedule_continuous_listen"):
        try:
            assert not hasattr(gui, "CopilotView")
            assert not hasattr(gui, "CopilotWorker")
            assert not hasattr(window, "copilot_view")
            assert window.page_stack.currentWidget() is window.chat
            assert sum(isinstance(item, QMainWindow)
                       for item in app.topLevelWidgets()) == 1
            assert not any(isinstance(item, QDialog)
                           for item in app.topLevelWidgets())

            window._start_agent_turn(
                "Search for Dune and open it", persist_history=False)
            bridge = window._copilot_bridge
            assert callable(bridge)
            wait_until(
                app,
                lambda: any(
                    card.isVisible()
                    for card in window.chat._body.findChildren(
                        gui.CopilotApprovalCard)),
                "the visible inline approval card")
            cards = window.chat._body.findChildren(gui.CopilotApprovalCard)
            assert len(cards) == 1
            card = cards[0]
            assert card.isVisible()
            assert window.page_stack.currentWidget() is window.chat
            save_screenshot(window, SCREENSHOT)

            card.approve_btn.click()
            wait_until(
                app,
                lambda: smoke_agent.progress_ready.is_set()
                and "Performing step 2" in window.task_workspace.steps_text(),
                "approved Copilot progress")
            assert smoke_agent.approved is True
            assert not window.chat._body.findChildren(gui.CopilotApprovalCard)
            assert id(card) not in window.chat._gap_items
            activity = window.task_workspace.steps_text()
            assert "Planning step 1" in activity
            assert "Performing step 2" in activity
            assert "private raw goal" not in activity
            assert "private typed text" not in activity
            save_screenshot(window, PROGRESS_SCREENSHOT)

            smoke_agent.allow_finish.set()
            wait_until(app, lambda: not window._processing,
                       "the completed main turn")
            assert window._copilot_bridge is None
            wait_until(
                app,
                lambda: any(
                    "Screen Copilot status: completed" in getattr(
                        message, "raw_text", "")
                    and getattr(message, "_fade_anim", None) is None
                    for message in window.chat._body.findChildren(
                        gui.AssistantMessage)),
                "the visible completion reply")
            assert "Screen Copilot status: completed" in (
                window.task_workspace.steps_text())
            assert "planner marked the goal done" in (
                window.task_workspace.steps_text())
            save_screenshot(window, RESULT_SCREENSHOT)
        finally:
            smoke_agent.allow_finish.set()
            window._detach_copilot_bridge()
            worker = window._worker
            if worker is not None and getattr(worker, "_thread", None):
                worker._thread.join(timeout=5)
            screen_copilot.shutdown()

    assert not window._live_agent_workers()
    print(
        "GUI COPILOT OK "
        f"page={type(window.page_stack.currentWidget()).__name__} "
        f"approved={smoke_agent.approved} screenshot={SCREENSHOT} "
        f"progress={PROGRESS_SCREENSHOT} result={RESULT_SCREENSHOT}",
        flush=True)
    window.deleteLater()
    app.processEvents()
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    app.quit()


def timeout_handler():
    print("GUI COPILOT TIMEOUT", flush=True)
    os._exit(2)


if __name__ == "__main__":
    if sys.platform != "win32":
        print("SKIP: native copilot GUI smoke is Windows-only")
        raise SystemExit(0)
    watchdog = threading.Timer(45, timeout_handler)
    watchdog.daemon = True
    watchdog.start()
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
    finally:
        watchdog.cancel()
