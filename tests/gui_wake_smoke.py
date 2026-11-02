"""
Manual GUI smoke test for the wake-word wiring (no real microphone).

Run directly: python tests/gui_wake_smoke.py
Not named test_*.py so unittest discovery does not import it.
"""
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(r"C:\Users\USER\AgentBot")
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

import agent_gui_qt as g  # noqa: E402
import wake_word  # noqa: E402

app = QApplication([])
w = g.AgentWindow()
w.show()
for _ in range(20):
    app.processEvents()

# Explicitly disabled: no listener, no error.
assert w._wake_word_service is None
with mock.patch.object(g, "read_config_file_raw", return_value={
    "WAKE_WORD_ENABLED": False,
}):
    w._start_wake_word_service()
assert w._wake_word_service is None

# Enabled but no custom "agent" model: clear error in the transcript.
tmp = Path(tempfile.mkdtemp(prefix="wake_gui_"))
before = len(w.chat.chat_log)
with mock.patch.object(g, "read_config_file_raw", return_value={
    "WAKE_WORD_ENABLED": True,
    "WAKE_WORD_MODEL": "agent",
}), mock.patch.object(wake_word, "MODEL_DIRS", (tmp / "empty",)):
    w._start_wake_word_service()

assert w._wake_word_service is None
added = w.chat.chat_log[before:]
assert added and any("Wake word disabled" in text for _, text in added), added
shutil.rmtree(tmp, True)

# Detection presses F9, shows a notice, then auto-releases.
calls = []
w._hotkey_press = lambda event=None: calls.append("press")
w._hotkey_release = lambda event=None: calls.append("release")
w._speak_wake_ack = lambda: False  # no real TTS in the smoke
w._processing = False
w._recording = False
with mock.patch.object(
        g, "read_config_file_raw", return_value={"WAKE_RECORD_MS": 40}):
    w._on_wake_word(0.9)
assert calls == ["press"], calls
assert w._wake_triggered
last = None
for i in range(w.chat._layout.count() - 1, -1, -1):
    widget = w.chat._layout.itemAt(i).widget()
    if widget is not None:
        last = widget
        break
assert isinstance(last, g.SystemMessage), type(last)
assert any("Wake word detected" in lbl.text()
           for lbl in last.findChildren(g.QLabel))
deadline = time.time() + 2.0
while time.time() < deadline and calls == ["press"]:
    app.processEvents()
    time.sleep(0.01)
assert calls == ["press", "release"], calls
assert not w._wake_triggered

resumes = []
real_resume = w._resume_wake_word
w._resume_wake_word = lambda: resumes.append("resume")
w._processing = True
w._on_wake_word(0.9)
assert calls == ["press", "release"], calls
assert resumes == ["resume"], resumes
w._processing = False
w._resume_wake_word = real_resume

# Pause/resume proxy calls the service.
class FakeService:
    def __init__(self):
        self.paused = 0
        self.resumed = 0

    def pause(self):
        self.paused += 1

    def resume(self):
        self.resumed += 1


fake = FakeService()
w._wake_word_service = fake
w._pause_wake_word()
w._resume_wake_word()
assert fake.paused == 1 and fake.resumed == 1
w._wake_word_service = None

# Runtime errors are shown once.
w._wake_word_error_shown = False
before = len(w.chat.chat_log)
w._on_wake_word_error("mic unavailable")
w._on_wake_word_error("mic unavailable")
assert w._wake_word_error_shown
added = w.chat.chat_log[before:]
assert len(added) == 1, added
assert "Wake word unavailable" in added[0][1]

w.close()
print("gui wake-word smoke: OK")
