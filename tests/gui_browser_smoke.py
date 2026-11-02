"""
Manual GUI smoke test for the Section 4 approval bridge and /browse.

Run directly: python tests/gui_browser_smoke.py
Not named test_*.py so unittest discovery does not import it.
"""
import concurrent.futures
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(r"C:\Users\USER\AgentBot")
sys.path.insert(0, str(ROOT))

from PySide6.QtGui import QColor, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import agent_gui_qt as g  # noqa: E402
import browser_agent as b  # noqa: E402

app = QApplication([])
w = g.AgentWindow()
w.show()
for _ in range(20):
    app.processEvents()

assert "/browse" in g.HELP_TEXT
assert w._browser_bridge is not None

# --- bridge delivers a request and resolves its future -----------------
bridge = g.BrowserApprovalBridge()
received = []
bridge.requested.connect(received.append)
future = bridge({
    "id": "req-1", "summary": 'Click "Sign in"',
    "url": "https://example.com/", "timeout": 5,
})
app.processEvents()
assert received and received[0]["id"] == "req-1"
assert isinstance(received[0]["future"], concurrent.futures.Future)
received[0]["future"].set_result(True)
assert future.result() is True

# --- card Allow/Deny are one-shot --------------------------------------
future = concurrent.futures.Future()
card = g.BrowserApprovalCard({
    "id": "req-2", "summary": 'Click "Sign in"',
    "url": "https://example.com/", "timeout": 5, "future": future,
})
card.show()
card.allow_btn.click()
app.processEvents()
assert future.result() is True
assert not card.allow_btn.isEnabled() and not card.deny_btn.isEnabled()
card._resolve(False, "late denial")
assert future.result() is True  # a late decision never reverses approval

# --- vision card renders a marked screenshot ---------------------------
pix = QPixmap(320, 200)
pix.fill(QColor("#202020"))
tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
tmp.close()
assert pix.save(tmp.name, "PNG")
png = Path(tmp.name).read_bytes()
os.unlink(tmp.name)

vision_future = concurrent.futures.Future()
vision_card = g.BrowserApprovalCard({
    "id": "req-3", "summary": "Click the screenshot at (160, 100)",
    "url": "https://example.com/", "timeout": 5, "future": vision_future,
    "screenshot_png": png, "x": 160, "y": 100,
})
vision_card.show()
vision_card.deny_btn.click()
app.processEvents()
assert vision_future.result() is False
assert any(isinstance(child, g.QLabel) and child.pixmap() is not None
           for child in vision_card.findChildren(g.QLabel))

# --- window slot adds a live card --------------------------------------
live_future = concurrent.futures.Future()
before = w.chat._layout.count()
w._on_browser_approval_request({
    "id": "req-4", "summary": 'Fill "Email"',
    "url": "https://example.com/", "timeout": 5, "future": live_future,
})
app.processEvents()
assert w.chat._layout.count() > before
cards = [
    w.chat._layout.itemAt(i).widget()
    for i in range(w.chat._layout.count())
    if isinstance(w.chat._layout.itemAt(i).widget(), g.BrowserApprovalCard)
]
assert cards
cards[-1].allow_btn.click()
app.processEvents()
assert live_future.result() is True

# --- slash command validation ------------------------------------------
calls = []
w._finish_slash_command = lambda text, result: calls.append((text, result)) or True
assert w._handle_slash_command("/browse") is True
assert calls and "format" in calls[0][1], calls

# --- cleanup -----------------------------------------------------------
b.set_approval_handler(None)
w.close()
print("gui browser smoke: OK")
