"""
Manual GUI smoke test for the Telegram document pipeline.

Run directly: python tests/telegram_gui_smoke.py
Not named test_*.py so unittest discovery does not import it.
"""
import os
import queue
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(r"C:\Users\USER\AgentBot")
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import agent_gui_qt as g  # noqa: E402
import telegram_service as ts  # noqa: E402

app = QApplication([])
app.setStyle("Fusion")
app.setFont(g.terminal_font())
app.setStyleSheet((ROOT / "agentbot.qss").read_text(encoding="utf-8"))

w = g.AgentWindow()
w.show()
for _ in range(20):
    app.processEvents()

# --- PlainText widget -------------------------------------------------
message = g.UntrustedTextMessage("<b>not markup</b>", source="SRC")
assert message.body_label.textFormat() == Qt.TextFormat.PlainText
assert message.body_label.text() == "<b>not markup</b>"
assert message.source_label.textFormat() == Qt.TextFormat.PlainText
assert message.raw_text.startswith("SRC")

# --- Render results ---------------------------------------------------
before = len(w.chat.chat_log)
w._render_telegram_result(("summary", 10, 20, "<script>alert(1)</script>"))
added = w.chat.chat_log[before:]
assert len(added) == 1, added
assert added[0][0] == "agent"
assert "<script>" in added[0][1]
last = w.chat._layout.itemAt(w.chat._layout.count() - 2).widget()
assert isinstance(last, g.UntrustedTextMessage), type(last)
assert last.body_label.textFormat() == Qt.TextFormat.PlainText

for item in (
    ("rejected", 10, 20, "queue full"),
    ("error", 10, 21, "Encrypted PDF rejected"),
):
    w._render_telegram_result(item)
    app.processEvents()

# service_error stops the polling path; no timer exists in this test.
w._render_telegram_result(("service_error", None, None, "Not authorized"))
assert w._telegram_timer is None
assert w._telegram_service is None

# --- Idempotent stop --------------------------------------------------
w._stop_telegram_doc_service()
w._stop_telegram_doc_service()

# --- Disabled config does not start anything --------------------------
disabled = ts.DocSettings(
    enabled=False, api_id=0, api_hash="", session_name="x",
    session_path=str(ROOT / "x.session"),
    allowed_chats=frozenset(), allowed_senders=frozenset(),
    llm_priority=("deepseek",), local_only=False,
)
with_unused = ts.load_settings
ts.load_settings = lambda: disabled
try:
    w._start_telegram_doc_service()
    assert w._telegram_service is None
    assert w._telegram_timer is None
finally:
    ts.load_settings = with_unused

# --- Enabled config wires the timer + drain ---------------------------
enabled = ts.DocSettings(
    enabled=True, api_id=1, api_hash="h", session_name="x",
    session_path=str(ROOT / "x.session"),
    allowed_chats=frozenset({1}), allowed_senders=frozenset({2}),
    llm_priority=("deepseek",), local_only=False,
)


class FakeService:
    def __init__(self, settings):
        self.settings = settings
        self.results = queue.Queue()
        self.stopped = False

    def start(self):
        return True

    def stop(self):
        self.stopped = True


real_service_cls = ts.TelegramDocService
ts.TelegramDocService = FakeService
ts.load_settings = lambda: enabled
try:
    w._start_telegram_doc_service()
    assert isinstance(w._telegram_service, FakeService)
    assert w._telegram_timer is not None
    assert w._telegram_results is w._telegram_service.results

    before = len(w.chat.chat_log)
    w._telegram_results.put(("summary", 1, 2, "summary text"))
    w._drain_telegram_results()
    assert len(w.chat.chat_log) == before + 1
    assert w.chat.chat_log[-1][1].endswith("summary text")

    service = w._telegram_service
    w._stop_telegram_doc_service()
    assert service.stopped
    assert w._telegram_timer is None
    assert w._telegram_results is None
finally:
    ts.TelegramDocService = real_service_cls
    ts.load_settings = with_unused

# --- Config error is surfaced, not raised ----------------------------
w._telegram_service = None
w._telegram_timer = None
w._telegram_results = None


def raise_config():
    raise ts.TelegramDocConfigError("allowlists empty")


ts.load_settings = raise_config
try:
    before = len(w.chat.chat_log)
    w._start_telegram_doc_service()
    assert len(w.chat.chat_log) == before + 1
    assert "allowlists empty" in w.chat.chat_log[-1][1]
    assert w._telegram_service is None
finally:
    ts.load_settings = with_unused

print("GUI TELEGRAM TESTS OK")
w._stop_telegram_doc_service()
w.close()
