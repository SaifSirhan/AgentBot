"""
Manual GUI smoke test for the /reload-skills command.

Run directly: python tests/gui_skills_smoke.py
Not named test_*.py so unittest discovery does not import it.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(r"C:\Users\USER\AgentBot")
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

import agent_gui_qt as g  # noqa: E402

app = QApplication([])
w = g.AgentWindow()
w.show()
for _ in range(20):
    app.processEvents()

assert "/reload-skills" in g.HELP_TEXT

# Avoid writing to the real conversation history in this smoke test.
calls = []
w._finish_slash_command = lambda text, result: calls.append((text, result)) or True

assert w._handle_slash_command("/reload-skills") is True
assert calls, "the slash command was not handled"
assert calls[0][0] == "/reload-skills"
assert "Skills:" in calls[0][1], calls[0][1]
assert "loaded" in calls[0][1], calls[0][1]
print("reload output:", calls[0][1].splitlines()[0])

# A turn in flight refuses the command without scanning.
w._processing = True
try:
    before = len(w.chat.chat_log)
    assert w._handle_slash_command("/reload-skills") is True
    added = w.chat.chat_log[before:]
    assert len(added) == 1, added
    assert "unavailable while a turn is running" in added[0][1], added[0][1]
finally:
    w._processing = False

# Case-insensitive routing still works.
calls.clear()
assert w._handle_slash_command("/RELOAD-SKILLS") is True
assert calls and "Skills:" in calls[0][1]

w.close()
print("gui skills smoke: OK")
