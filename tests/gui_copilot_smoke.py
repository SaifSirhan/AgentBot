"""Native screen-copilot GUI smoke; run directly on Windows."""
import os
import sys
import threading
import traceback
from concurrent.futures import Future
from pathlib import Path

os.environ.pop("QT_QPA_PLATFORM", None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QMainWindow  # noqa: E402

import agent_gui_qt as gui  # noqa: E402
import screen_copilot  # noqa: E402


SCREENSHOT = ROOT / "scratch" / "kestrel-copilot-live.png"
PROGRESS_SCREENSHOT = ROOT / "scratch" / "kestrel-copilot-progress.png"
RESULT_SCREENSHOT = ROOT / "scratch" / "kestrel-copilot-result.png"


def save_screenshot(window, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    assert window.grab().save(str(path))
    assert path.stat().st_size > 10_000


def approval_payload():
    return {
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
    }


def main():
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    app.setFont(gui.terminal_font())
    app.setStyleSheet(gui.theme_stylesheet(gui.read_theme_name()))
    window = gui.AgentWindow()
    window.resize(1480, 820)
    window._refresh_copilot_targets = lambda: None
    window.show()
    window._show_copilot_page()
    for _ in range(10):
        app.processEvents()

    assert window.page_stack.currentWidget() is window.copilot_view
    assert sum(isinstance(item, QMainWindow)
               for item in app.topLevelWidgets()) == 1
    assert not any(isinstance(item, QDialog) for item in app.topLevelWidgets())

    payload = approval_payload()
    future = Future()
    payload["future"] = future
    window._show_copilot_approval(payload)
    for _ in range(5):
        app.processEvents()
    cards = window.copilot_view.approval_area.findChildren(
        gui.CopilotApprovalCard)
    assert len(cards) == 1
    assert cards[0].isVisible()
    card_count = len(cards)
    save_screenshot(window, SCREENSHOT)

    cards[0].deny_btn.click()
    assert future.result(timeout=1) is False
    for _ in range(5):
        app.processEvents()
    assert window.copilot_view.approval_area.layout().count() == 0

    before = window.copilot_view.status_label.text()
    progress_worker = gui.CopilotWorker(mode="run")
    progress_worker.progress.connect(window._update_copilot_progress)
    progress_worker.finished.connect(window._on_copilot_finished)
    progress_worker.progress.emit({
        "event": "action",
        "step": 2,
        "action": "type_text",
        "status": "started",
        "goal": "private raw goal",
        "text": "private typed text",
    })
    after = window.copilot_view.status_label.text()
    assert after != before
    assert "2" in after and "type_text" in after
    assert "private" not in after
    save_screenshot(window, PROGRESS_SCREENSHOT)

    result = screen_copilot.CopilotResult(
        status="completed",
        summary=(
            "The planner marked the goal complete; this is not independent "
            "proof of an external effect."
        ),
        task_id=payload["task_id"],
        steps=2,
        changed=True,
        last_action="click",
    )
    progress_worker.finished.emit(result)
    for _ in range(5):
        app.processEvents()
    assert window.header_status.text() == "Copilot complete"
    assert "planner marked the goal complete" in (
        window.copilot_view.status_label.text())
    save_screenshot(window, RESULT_SCREENSHOT)
    progress_worker.deleteLater()

    print(
        "GUI COPILOT OK "
        f"page={type(window.page_stack.currentWidget()).__name__} "
        f"cards={card_count} screenshot={SCREENSHOT} "
        f"progress={PROGRESS_SCREENSHOT} result={RESULT_SCREENSHOT}",
        flush=True,
    )
    window._detach_copilot_bridge()
    assert not window._live_copilot_workers()
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
    watchdog = threading.Timer(30, timeout_handler)
    watchdog.daemon = True
    watchdog.start()
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
    finally:
        watchdog.cancel()
