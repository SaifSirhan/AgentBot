"""
Manual GUI smoke test for the OS-control hide/show bridge and kill switch.

Run directly: python tests/gui_os_smoke.py
Not named test_*.py so unittest discovery does not import it.
"""
import os
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(r"C:\Users\USER\AgentBot")
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

import agent_gui_qt as g  # noqa: E402
import os_control  # noqa: E402

app = QApplication([])
w = g.AgentWindow()
w.show()
for _ in range(20):
    app.processEvents()

assert w._os_control is not None, "OS control failed to initialize"
assert w.isVisible()


def run_worker(fn, timeout=3.0):
    """Run a bridge call on a worker thread while pumping the Qt loop."""
    thread = threading.Thread(target=fn)
    thread.start()
    deadline = time.time() + timeout
    while thread.is_alive() and time.time() < deadline:
        app.processEvents()
        thread.join(0.02)
    assert not thread.is_alive(), f"worker did not finish: {fn}"


run_worker(w._hide_gui_for_screenshot)
assert not w.isVisible(), "window was not hidden"

run_worker(w._show_gui_after_screenshot)
assert w.isVisible(), "window was not restored"

# Kill switch latches and blocks OS tools before any screenshot dependency.
os_control.trigger_kill_switch()
out = os_control.screenshot_background()
assert out.startswith("ABORTED:"), out
assert os_control.kill_switch_active()
os_control.reset_kill_switch()
assert not os_control.kill_switch_active()

# A hidden-in-tray style state must not be re-shown by a failed hide.
w.hide()
for _ in range(5):
    app.processEvents()
w._gui_was_visible = False
w._apply_gui_visibility(True, None)
assert not w.isVisible(), "restore popped a window that was never visible"

os_control.stop_kill_switch()
os_control.set_gui_visibility_handlers(None, None)
w.close()
print("gui OS-control smoke: OK")
