"""Manual GUI smoke test for the native tray and background mode.

Run directly: python tests/gui_tray_smoke.py
Not named test_*.py so unittest discovery does not import it.
"""
import os
import sys
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(r"C:\Users\USER\AgentBot")
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QSystemTrayIcon,
)

import agent_gui_qt as g  # noqa: E402


class FakeAgent:
    def load_conversation_history(self):
        return []

    def save_conversation_history(self, _history):
        pass


app = QApplication([])
app.setQuitOnLastWindowClosed(False)
g.get_agent = lambda: FakeAgent()

w = g.AgentWindow()
w.show()
for _ in range(20):
    app.processEvents()


def pump(n=20):
    for _ in range(n):
        app.processEvents()


# --- native icon created on startup -----------------------------------
with mock.patch.object(
        QSystemTrayIcon, "isSystemTrayAvailable", return_value=True):
    w._start_tray()
pump()
assert isinstance(w._tray_icon, QSystemTrayIcon)
assert w._tray_icon.isVisible()
labels = [a.text() for a in w._tray_menu.actions() if a.text()]
assert labels == ["Show", "Quit"], labels

# --- close hides to the tray (does not exit) --------------------------
w.close()
pump()
assert not w.isVisible(), "close did not hide to the tray"

# --- double-click / Show restore --------------------------------------
w._on_tray_activated(QSystemTrayIcon.ActivationReason.DoubleClick)
pump()
assert w.isVisible(), "double-click did not restore the window"

w.hide()
pump()
show_action = next(
    a for a in w._tray_menu.actions() if a.text() == "Show")
show_action.trigger()
pump()
assert w.isVisible(), "tray Show did not restore the window"

# --- minimise hides to the tray ---------------------------------------
w.showNormal()
pump()
w.showMinimized()
pump(40)
assert not w.isVisible(), "minimise did not hide to the tray"
assert w.isMinimized()
w.show_window()
pump()
assert w.isVisible() and not w.isMinimized()

# --- no tray: close falls back to the taskbar -------------------------
w._tray_icon.hide()
w.hide_to_tray()
pump()
assert w.isVisible() and w.isMinimized(), "fallback did not minimise"

w.show_window()
pump()
w._tray_icon.hide()
w.close()
app.quit()
print("gui tray smoke: OK")
