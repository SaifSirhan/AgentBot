"""Manual GUI smoke test for the single-screen shell and Telegram pipeline.

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

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QMainWindow, QSplitter  # noqa: E402

import agent_gui_qt as g  # noqa: E402
import telegram_service as ts  # noqa: E402

app = QApplication([])
app.setStyle("Fusion")
app.setFont(g.terminal_font())
app.setStyleSheet((ROOT / "themes" / "brutalist.qss").read_text(encoding="utf-8"))

w = g.AgentWindow()
w.resize(1480, 820)
w.show()
for _ in range(20):
    app.processEvents()

# --- Single-screen shell -------------------------------------------------
assert isinstance(w, QMainWindow)
assert not w.findChildren(QSplitter)
assert not w.findChildren(g.RightPanel)
assert list(w.nav_rail.buttons) == [
    "Kestrel", "Tasks", "Files", "Voice", "Memory", "Settings"]
assert w.conversation_column.width() == g.TEXT_COLUMN_W == 920
assert w.owl_stage.pet.parentWidget() is w.owl_stage
assert w.owl_stage.hud.pet is w.owl_stage.pet
assert w.composer.mapTo(w, QPoint(0, 0)).x() == w.workspace.mapTo(
    w, QPoint(0, 0)).x()
assert w.send_button.text() == "Send"
assert w.voice_button.text() == "F9 push-to-talk"

# --- Untrusted text ------------------------------------------------------
message = g.UntrustedTextMessage("<b>not markup</b>", source="SRC")
assert message.body_label.textFormat() == Qt.TextFormat.PlainText
assert message.body_label.text() == "<b>not markup</b>"
assert message.source_label.textFormat() == Qt.TextFormat.PlainText
assert message.raw_text.startswith("SRC")

# --- Task activity stays in the integrated workspace --------------------
w.task_workspace.set_task_title("Organize Downloads")
w._on_worker_step("Action: read_file(Downloads) -> Result: 12 files found")
assert w.task_workspace.task_label.text() == "Organize Downloads"
assert "12 files found" in w.task_workspace.steps_text()

# --- Telegram summaries are safe chat messages --------------------------
before = len(w.chat.chat_log)
w._render_telegram_result((
    "summary", 10, 20, "<script>alert(1)</script>",
    {"sender": "Alice", "file_name": "report.pdf"},
))
assert len(w.chat.chat_log) == before + 1
summary = w._telegram_messages[-1]
assert summary.source_label.text() == "TELEGRAM DOCS"
assert "Alice" in summary.body_label.text()
assert "report.pdf" in summary.body_label.text()
assert "<script>alert(1)</script>" in summary.body_label.text()
assert summary.body_label.textFormat() == Qt.TextFormat.PlainText

w._render_telegram_result(("summary", 30, 40, "second summary"))
assert "chat 30" in w._telegram_messages[-1].body_label.text()
assert "msg 40" in w._telegram_messages[-1].body_label.text()
w._handle_slash_command("/tg-clear")
assert w._telegram_messages == []

# Rejections and errors stay in the transcript.
for item in (
    ("rejected", 10, 20, "queue full"),
    ("error", 10, 21, "Encrypted PDF rejected"),
):
    before = len(w.chat.chat_log)
    w._render_telegram_result(item)
    assert len(w.chat.chat_log) == before + 1

# service_error stops the polling path; no timer exists in this test.
w._render_telegram_result(("service_error", None, None, "Not authorized"))
assert w._telegram_timer is None
assert w._telegram_service is None

# --- Idempotent stop -----------------------------------------------------
w._stop_telegram_doc_service()
w._stop_telegram_doc_service()

# --- Disabled config does not start anything ----------------------------
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

# --- Enabled config wires the timer + drain ------------------------------
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
    w._telegram_results.put((
        "summary", 1, 2, "summary text",
        {"sender": "Bob", "file_name": "notes.docx"},
    ))
    w._drain_telegram_results()
    assert len(w.chat.chat_log) == before + 1
    assert "Bob" in w._telegram_messages[-1].body_label.text()
    assert "notes.docx" in w._telegram_messages[-1].body_label.text()
    assert "summary text" in w._telegram_messages[-1].body_label.text()

    service = w._telegram_service
    w._stop_telegram_doc_service()
    assert service.stopped
    assert w._telegram_timer is None
    assert w._telegram_results is None
finally:
    ts.TelegramDocService = real_service_cls
    ts.load_settings = with_unused

# --- Config error is surfaced, not raised --------------------------------
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
