"""
Manual GUI smoke test for the Telegram document pipeline.

Run directly: python tests/telegram_gui_smoke.py
Not named test_*.py so unittest discovery does not import it.
"""
import os
import queue
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(r"C:\Users\USER\AgentBot")
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QSplitter  # noqa: E402

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

# --- Splitter + panel replacement --------------------------------------
assert isinstance(w._main_splitter, QSplitter)
assert "NOTIFICATIONS" not in w.panel.cards
assert "TELEGRAM" in w.panel.cards
panel = w.panel.cards["TELEGRAM"]
assert isinstance(panel, g.TelegramPanel)

total = w._main_splitter.width()
w._main_splitter.setSizes([total - 280, 280])
for _ in range(10):
    app.processEvents()
assert abs(w.right_panel.width() - 280) <= 10, w.right_panel.width()
w._main_splitter.setSizes([total - 340, 340])
for _ in range(10):
    app.processEvents()
assert abs(w.right_panel.width() - 340) <= 10, w.right_panel.width()

# Hide/show still collapses and restores the splitter pane.
w._animations_enabled = False
w._set_right_panel_visible(False, animate=False)
assert not w.right_panel.isVisible()
w._set_right_panel_visible(True, animate=False)
for _ in range(10):
    app.processEvents()
assert w.right_panel.isVisible()
assert w.right_panel.width() >= g.MIN_RIGHT_PANEL_W - 1
w._animations_enabled = True


def wait_events(ms=500):
    end = time.time() + ms / 1000
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


# Animated hide/show must also restore the splitter pane.
w._set_right_panel_visible(False, animate=True)
wait_events(400)
assert not w.right_panel.isVisible()
w._set_right_panel_visible(True, animate=True)
wait_events(400)
assert w.right_panel.isVisible()
assert w.right_panel.width() >= g.MIN_RIGHT_PANEL_W - 1

# --- Summaries go to the panel, not the chat ---------------------------
before = len(w.chat.chat_log)
w._render_telegram_result((
    "summary", 10, 20, "<script>alert(1)</script>",
    {"sender": "Alice", "file_name": "report.pdf"},
))
assert len(w.chat.chat_log) == before
assert len(panel.entries) == 1
entry = panel.entries[0]
assert entry.sender_label.text() == "Alice"
assert entry.file_label.text() == "report.pdf"
assert entry.summary_label.text() == "<script>alert(1)</script>"
for lbl in (entry.sender_label, entry.file_label, entry.summary_label):
    assert lbl.textFormat() == Qt.TextFormat.PlainText
assert panel.list_scroll.isVisible()

# Older 4-tuple results still render with fallback labels.
w._render_telegram_result(("summary", 30, 40, "second summary"))
assert len(panel.entries) == 2
assert panel.entries[0].sender_label.text() == "chat 30"
assert panel.entries[0].file_label.text() == "msg 40"

# The clear button empties the list and restores the placeholder.
panel.clear_btn.click()
assert panel.entries == []
assert not panel.list_scroll.isVisible()
assert panel.placeholder.isVisible()

# Rejections and errors stay in the transcript.
before = len(w.chat.chat_log)
for item in (
    ("rejected", 10, 20, "queue full"),
    ("error", 10, 21, "Encrypted PDF rejected"),
):
    w._render_telegram_result(item)
    app.processEvents()
assert len(w.chat.chat_log) == before + 2

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
    entries_before = len(panel.entries)
    w._telegram_results.put((
        "summary", 1, 2, "summary text",
        {"sender": "Bob", "file_name": "notes.docx"},
    ))
    w._drain_telegram_results()
    assert len(w.chat.chat_log) == before
    assert len(panel.entries) == entries_before + 1
    assert panel.entries[0].summary_label.text() == "summary text"
    assert panel.entries[0].sender_label.text() == "Bob"

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
