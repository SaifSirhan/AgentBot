"""
AgentBot GUI — PySide6 port.

Phase 4a: slash commands, settings overlay, memory reset — faithful ports of
the same features in agent_gui.py. Phase 4b: per-turn collapsible activity
chips showing the step log. Phase 5a: system tray, F9 hotkey (stub) and
scheduler notifications.

This file will eventually replace agent_gui.py. Until the port is complete,
agent_gui.py remains the live GUI and this file is only run manually.

Palette values are inlined from gui_widgets.py (the mint accent has been
shifted greener for this port). That module imports tkinter, so it must NOT
be imported here — the Qt port stays free of tkinter. agent_gui.py is NOT
imported either; the logic shared with it (the hallucination guard, the slash
command dispatch) is replicated below as pure Python. tray.py and scheduler.py
are plain-Python and safe to import.
"""
from __future__ import annotations
import contextlib
import io
import json
import os
import re
import sys
import threading

from PySide6.QtCore import (
    Qt,
    QEasingCurve,
    QObject,
    QParallelAnimationGroup,
    QPropertyAnimation,
    QSize,
    QTimer,
    Signal,
)
from PySide6.QtGui import QGuiApplication, QKeyEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

try:
    import keyboard
except ImportError:
    keyboard = None

try:
    import tray
except Exception:
    tray = None

try:
    import scheduler
except Exception:
    scheduler = None

# --------------------------- palette ---------------------------
# Mirrors gui_widgets.py, with the mint accent shifted greener (hue 153->147,
# saturation 60%->70%). Keep backgrounds/borders/text in sync with the theme.
COLOR_BG         = "#0d0f14"
COLOR_BG_ALT     = "#141720"
COLOR_SIDEBAR    = "#111319"
COLOR_RAISED     = "#171a22"
COLOR_CODE_BG    = "#080a0e"
COLOR_INPUT_BG   = "#14171f"
COLOR_BORDER     = "#23262f"
COLOR_HOVER      = "#1c2029"
COLOR_SELECTED   = "#16241e"
COLOR_USER_PILL  = "#232838"
COLOR_TEXT_HI    = "#e8eaef"
COLOR_TEXT_MID   = "#9aa1af"
COLOR_TEXT_LOW   = "#5f6673"

COLOR_WARN       = "#eab308"
COLOR_DANGER     = "#dc2626"
COLOR_DANGER_HOVER = "#b91c1c"

# mint accent — shifted greener (was #3ecf8e / #2fb87c / #1f4d3a)
COLOR_ACCENT       = "#4fe08f"
COLOR_ACCENT_HOVER = "#3bc476"
COLOR_ACCENT_DIM   = "#1c5a3f"

FONT_UI = "Segoe UI"
FONT_MONO = "Consolas"

SIDEBAR_W = 226
HEADER_H = 48

CONTENT_MAX_W = 720
CONTENT_PAD = 24
MSG_SPACING = 28
FADE_MS = 150

INPUT_LINE_H = 22     # single-line height for the input field
INPUT_MAX_LINES = 6   # auto-grow ceiling (~150px)

SCHEDULED_QUEUE_CAP = 5   # pending scheduled triggers before oldest is dropped

_CODE_FENCE_RE = re.compile(r"```([a-zA-Z0-9_+.-]*)\n?(.*?)```", re.DOTALL)

# CSS injected into QTextDocument HTML so the markdown typography matches the
# design spec (body 15px/1.6, scaled headers, inline code pill).
_MD_CSS = f"""
p {{ margin: 0 0 10px 0; }}
h1 {{ font-size: 22px; font-weight: 600; margin: 18px 0 10px 0; }}
h2 {{ font-size: 18px; font-weight: 600; margin: 16px 0 8px 0; }}
h3 {{ font-size: 16px; font-weight: 600; margin: 14px 0 6px 0; }}
ul, ol {{ margin: 0 0 10px 0; -qt-list-indent: 1; }}
li {{ margin: 2px 0; }}
code {{ font-family: "{FONT_MONO}"; font-size: 13px;
        background-color: {COLOR_RAISED}; color: {COLOR_ACCENT}; }}
a {{ color: {COLOR_ACCENT}; }}
"""


# ----------------------------------------------------------------------
# Agent bridge — extraction + hallucination guard (pure Python)
# ----------------------------------------------------------------------
_agent_module = None
_agent_import_error = None


def get_agent():
    """Import agent.py lazily. Heavy (ChromaDB, sentence-transformers, Kokoro)
    and must never break GUI startup — failures surface on first send."""
    global _agent_module, _agent_import_error
    if _agent_module is not None:
        return _agent_module
    if _agent_import_error is not None:
        raise _agent_import_error
    try:
        import agent as _agent
        _agent_module = _agent
        return _agent_module
    except Exception as e:  # remember so repeated sends fail fast and visibly
        _agent_import_error = e
        raise


_voice_module = None
_voice_output_module = None


def get_voice():
    """Import voice.py lazily. It pulls whisper/numpy/sounddevice (~4s), so it
    must not run at startup — only on the first F9 press."""
    global _voice_module
    if _voice_module is None:
        import voice as _voice
        _voice_module = _voice
    return _voice_module


def get_voice_output():
    """Import voice_output.py lazily (Kokoro). Failure is non-fatal."""
    global _voice_output_module
    if _voice_output_module is None:
        import voice_output as _vo
        _voice_output_module = _vo
    return _voice_output_module


def extract_final_message(step_log):
    """Pull the reply out of run_agent_turn's step_log (list[str]).

    Mirrors agent_gui.handle_result's scan order: chat() result, Done:, any
    'AI: ' line, then the last non-status line.
    """
    if not step_log:
        return None
    for line in reversed(step_log):
        if not isinstance(line, str):
            continue
        if line.startswith("Action: chat(") and "-> Result: " in line:
            candidate = line.split("-> Result: ", 1)[-1]
            if candidate.startswith("AI: "):
                candidate = candidate[4:]
            if candidate.strip():
                return candidate
        if line.startswith("Done:"):
            return line[5:].strip()
    for line in reversed(step_log):
        if isinstance(line, str) and "AI: " in line:
            candidate = line.split("AI: ", 1)[-1].strip()
            if candidate:
                return candidate
    for line in reversed(step_log):
        if not isinstance(line, str):
            continue
        s = line.strip()
        if s and not s.startswith(("Action:", "System:", "Done:")):
            return s
    return None


def apply_hallucination_guard(final_message, step_log):
    """Replicate agent_gui.handle_result's guard verbatim (pure Python).

    The LLM sometimes claims it performed an action when no tool ran. If the
    reply asserts a completed action and step_log shows no non-chat tool ran,
    replace it with a warning.
    """
    if not final_message:
        return final_message

    tools_ran = any(
        isinstance(l, str) and l.startswith("Action: ") and "-> Result:" in l
        and not l.startswith("Action: chat(")
        for l in (step_log or [])
    )
    claim_words = (
        "i've moved", "i moved", "i've created", "i created",
        "i've sent", "i sent", "i've deleted", "i deleted",
        "i've found", "i found ", "i've opened", "i opened",
        "files moved", "folder created",
        "relabeled", "relabelled", "i've relabeled",
        "i've removed", "i removed", "i've labeled", "i labeled",
        "now labeled", "just labeled",
        "i've updated", "i updated", "i've edited", "i edited",
        "i've added", "i added", "i've changed", "i changed",
    )
    claim_verbs = (
        "moved", "created", "sent", "deleted", "removed", "relabeled",
        "relabelled", "labeled", "updated", "edited", "added",
        "changed", "cleared", "saved", "scheduled",
    )
    low = final_message.lower()
    stripped = low.lstrip()
    claims_done = stripped.startswith(("done", "did it", "all set"))
    claims_verb = stripped.startswith(claim_verbs)
    if (any(w in low for w in claim_words) or claims_done or claims_verb) and not tools_ran:
        return (
            "⚠️ I didn't actually do that — no tool ran. "
            "Say it again or use /find, /move, /mkdir to run it directly."
        )
    return final_message


# ----------------------------------------------------------------------
# Config access — safe single-key writes
# ----------------------------------------------------------------------
_config_module = None


def get_config_module():
    global _config_module
    if _config_module is None:
        import config as _config
        _config_module = _config
    return _config_module


def read_config_file_raw():
    """Read config.json as-is (NO env-var overlay).

    config.load_config() overlays environment variables over file values. If
    we saved that merged dict back to disk it would bake env-var secrets into
    the plaintext file. Settings must only ever persist what the user typed,
    so we read the raw file here and write only edited keys back.
    """
    cfg = get_config_module()
    if not os.path.exists(cfg.CONFIG_PATH):
        return {}
    try:
        with open(cfg.CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_config_keys(updates):
    """Merge `updates` into the raw config file and write it back.

    Unknown keys already in the file are preserved. Values not present are
    never introduced, so nothing from the environment leaks in.
    """
    cfg = get_config_module()
    raw = read_config_file_raw()
    raw.update(updates)
    os.makedirs(cfg.APP_DIR, exist_ok=True)
    with open(cfg.CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(raw, f, indent=2)
    return True


# ----------------------------------------------------------------------
# Activity summary — what the chip shows when collapsed
# ----------------------------------------------------------------------
def activity_summary(step_log):
    """Human summary of the tool steps in a step_log, or None if none ran."""
    tool_lines = [
        l for l in (step_log or [])
        if isinstance(l, str) and l.startswith("Action: ")
        and not l.startswith("Action: chat(")
    ]
    if not tool_lines:
        return None
    names = []
    for l in tool_lines:
        name = l[len("Action: "):].split("(")[0].strip()
        if name and name not in names:
            names.append(name)
    steps = len(tool_lines)
    return (f"used {', '.join(names)}  \u00b7  {steps} step"
            f"{'s' if steps != 1 else ''}")


# ----------------------------------------------------------------------
# Slash command help (verbatim from agent_gui._handle_slash_command)
# ----------------------------------------------------------------------
HELP_TEXT = (
    "Slash commands — bypass the AI and go straight to the tool:\n\n"
    "/send  Contact|message\n"
    "       e.g.  /send JEE|helo\n\n"
    "/edit  Contact|old text|new text\n"
    "       e.g.  /edit JEE|helo|hello there\n\n"
    "/del   Contact|text             → preview\n"
    "/del   Contact|text|confirm     → delete\n"
    "/del   Contact|text|all         → preview all matches\n"
    "/del   Contact|text|all|confirm → delete all matches\n"
    "       e.g.  /del JEE|helo\n\n"
    "/dellast Contact       → delete latest outgoing message\n\n"
    "Use 'me' as contact for Saved Messages.\n\n"
    "File commands — no AI, straight to the tool:\n\n"
    "/find  Folder|pat1,pat2\n"
    "       e.g.  /find telegram desktop|SPM,CTU,LCC\n"
    "       Folder alone works too:  /find SPM,CTU\n\n"
    "/move  Source|pat1,pat2|Dest            → preview\n"
    "/move  Source|pat1,pat2|Dest|confirm    → execute\n"
    "       e.g.  /move downloads|SPM,CTU|semester1\n\n"
    "/mkdir Name       → creates under Downloads (or give a full path)\n\n"
    "Code inspection — read-only, cannot modify anything:\n\n"
    "/symbols file.py  → every class/function with line numbers\n"
    "       e.g.  /symbols config.py\n\n"
    "/map   [Folder|max_files]  → symbol map of a whole folder\n"
    "       e.g.  /map agentbot|40     (no args = the AgentBot folder)\n\n"
    "Browser recipes — deterministic click/type scripts:\n\n"
    "/recipe Name      → run a recipe from recipes.json\n"
    "       e.g.  /recipe clock_out\n\n"
    "Security scanning — static analysis, no AI:\n\n"
    "/scan  File        → scan a file (hash, entropy, verdict)\n"
    "       e.g.  /scan C:\\Users\\USER\\Downloads\\file.exe\n\n"
    "/quarantine File   → move a file to Downloads\\Quarantine\\\n\n"
    "/quarantine-list   → list quarantined files\n\n"
    "/rag   query       → search your indexed documents\n\n"
    "Group GIF library — memes the bot stored from Telegram:\n\n"
    "/gif-stats         → how many GIFs, occurrences and labels\n\n"
    "/label-gifs        → label every unlabeled GIF (costs API calls)\n"
    "/label-gifs 50     → label the 50 most-reused only\n"
    "/label-gifs min 2  → only GIFs sent 2+ times (cheapest start)\n\n"
    "Instagram gateway (needs INSTAGRAM_ENABLED in config.json):\n\n"
    "/ig status         → is the DM gateway running?\n"
    "/ig restart        → stop and re-login the gateway\n\n"
    "Evaluation harness (developer surface, selection-only):\n\n"
    "/eval run Suite [provider] → run a suite, compare tool choice\n"
    "/eval list         → list suites\n"
    "/eval traces [n]   → recent turn traces\n"
    "/eval label Id good|bad    → label a trace\n"
    "/eval diff RunA RunB       → which cases changed\n\n"
    "Telegram group export (private, stays outside the repo):\n\n"
    "/import-chat <result.json>\n"
    "       → parse a Telegram Desktop export into scrubbed monthly\n"
    "         files under C:\\Users\\USER\\PrivateExport\n\n"
    "/reindex           → re-index RAG_AUTO_INDEX_FOLDERS"
)


# ----------------------------------------------------------------------
# Worker — runs the blocking agent call off the GUI thread
# ----------------------------------------------------------------------
class AgentWorker(QObject):
    """Runs one agent turn on a plain Python thread and emits Qt signals.

    threading.Thread + a QObject signal emitter is used instead of
    QThread/moveToThread: the agent call is a single blocking function with no
    event loop of its own, so a plain thread is simpler and the queued
    connection (worker living in the main thread, unlike a QThread) still
    delivers signals to the GUI thread automatically.
    """

    completed = Signal(object)   # (final_message, step_log)
    failed = Signal(str)         # error text

    def __init__(self, user_input, conversation_history):
        super().__init__()
        self._user_input = user_input
        self._history = conversation_history

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        log_capture = io.StringIO()
        try:
            agent = get_agent()
            self._history.append(f"User: {self._user_input}")
            with contextlib.redirect_stdout(log_capture):
                step_log = agent.run_agent_turn(self._user_input, self._history)
        except Exception as e:
            self.failed.emit(f"⚠️ Agent error: {e}")
            return

        if step_log is None:
            step_log = ["System: agent returned None — check the terminal for a traceback."]

        message = extract_final_message(step_log)
        if message:
            message = apply_hallucination_guard(message, step_log)

        if not message:
            message = "(no reply produced — check the terminal log)"

        try:
            agent = get_agent()
            agent.save_conversation_history(self._history)
        except Exception:
            pass

        self.completed.emit((message, step_log))


def _icon_button(glyph: str, tooltip: str = "", size: int = 30) -> QPushButton:
    btn = QPushButton(glyph)
    btn.setToolTip(tooltip)
    btn.setFixedSize(size + 2, size)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setStyleSheet(
        f"""
        QPushButton {{
            background: transparent;
            border: none;
            color: {COLOR_TEXT_MID};
            font-family: "{FONT_UI}";
            font-size: 14px;
        }}
        QPushButton:hover {{
            background: {COLOR_HOVER};
            border-radius: 8px;
            color: {COLOR_TEXT_HI};
        }}
        QPushButton:pressed {{ background: {COLOR_SELECTED}; }}
        """
    )
    return btn


def _nav_button(glyph: str, label: str, active: bool = False) -> QPushButton:
    btn = QPushButton(f"  {glyph}   {label}")
    btn.setFixedHeight(34)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setStyleSheet(
        f"""
        QPushButton {{
            background: {COLOR_SELECTED if active else "transparent"};
            border: none;
            border-radius: 9px;
            color: {COLOR_TEXT_HI if active else COLOR_TEXT_MID};
            font-family: "{FONT_UI}";
            font-size: 12px;
            text-align: left;
            padding-left: 8px;
        }}
        QPushButton:hover {{ background: {COLOR_HOVER}; }}
        """
    )
    return btn


# ----------------------------------------------------------------------
# Markdown
# ----------------------------------------------------------------------
def markdown_to_html(text: str) -> str:
    from PySide6.QtGui import QTextDocument

    doc = QTextDocument()
    doc.setMarkdown(text)
    html = doc.toHtml()
    # Replace the generated <style> block with our own so typography matches
    # the design spec instead of QTextDocument's 9pt defaults.
    html = re.sub(r"<style type=\"text/css\">.*?</style>", f"<style>{_MD_CSS}</style>",
                  html, count=1, flags=re.DOTALL)
    return html


def _split_segments(text: str):
    """Split text into ('prose', str) and ('code', lang, str) segments."""
    segments = []
    pos = 0
    for m in _CODE_FENCE_RE.finditer(text):
        before = text[pos:m.start()]
        if before.strip():
            segments.append(("prose", before))
        segments.append(("code", m.group(1), m.group(2).rstrip("\n")))
        pos = m.end()
    tail = text[pos:]
    if tail.strip():
        segments.append(("prose", tail))
    return segments


# ----------------------------------------------------------------------
# Code block with hover copy button
# ----------------------------------------------------------------------
class CodeBlock(QFrame):
    """Monospace code panel with a copy button that appears on hover."""

    def __init__(self, code: str):
        super().__init__()
        self._code = code
        self.setObjectName("codeBlock")
        self.setStyleSheet(
            f"""
            QFrame#codeBlock {{
                background: {COLOR_CODE_BG};
                border: 1px solid {COLOR_BORDER};
                border-radius: 8px;
            }}
            """
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        bar = QWidget()
        bar.setFixedHeight(34)
        bar_row = QHBoxLayout(bar)
        bar_row.setContentsMargins(10, 4, 8, 0)
        bar_row.setSpacing(6)
        bar_row.addStretch(1)

        self.copy_btn = QPushButton("\u2398")  # copy glyph
        self.copy_btn.setToolTip("Copy")
        self.copy_btn.setFixedSize(28, 28)
        self.copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copy_btn.setStyleSheet(
            f"""
            QPushButton {{
                background: transparent;
                border: none;
                border-radius: 6px;
                color: {COLOR_TEXT_MID};
                font-size: 14px;
            }}
            QPushButton:hover {{
                background: {COLOR_HOVER};
                color: {COLOR_TEXT_HI};
            }}
            """
        )
        self.copy_btn.clicked.connect(self._copy)
        self.copy_btn.setVisible(False)
        bar_row.addWidget(self.copy_btn)
        outer.addWidget(bar)

        self.code_label = QLabel(code)
        self.code_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.code_label.setWordWrap(False)
        self.code_label.setStyleSheet(
            f'background: transparent; color: {COLOR_TEXT_HI}; '
            f'font-family: "{FONT_MONO}"; font-size: 13px;'
        )
        self.code_label.setContentsMargins(14, 0, 14, 12)
        # Long code lines must scroll inside the block, not widen the whole
        # chat column (an unwrapped QLabel reports its full text width as a
        # minimum, which bled content past the window edges).
        self._code_scroll = QScrollArea()
        self._code_scroll.setWidget(self.code_label)
        self._code_scroll.setWidgetResizable(True)
        self._code_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._code_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._code_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._code_scroll.setStyleSheet("background: transparent; border: none;")
        self._code_scroll.setMinimumWidth(0)
        outer.addWidget(self._code_scroll)

        self._copy_timer = QTimer(self)
        self._copy_timer.setSingleShot(True)
        self._copy_timer.timeout.connect(self._reset_copy_icon)

    def _copy(self):
        QGuiApplication.clipboard().setText(self._code)
        self.copy_btn.setText("\u2713")  # check
        self.copy_btn.setStyleSheet(
            self.copy_btn.styleSheet().replace(
                f"color: {COLOR_TEXT_MID};", f"color: {COLOR_ACCENT};"
            )
        )
        self._copy_timer.start(1500)

    def _reset_copy_icon(self):
        self.copy_btn.setText("\u2398")
        self.copy_btn.setStyleSheet(
            self.copy_btn.styleSheet().replace(
                f"color: {COLOR_ACCENT};", f"color: {COLOR_TEXT_MID};"
            )
        )

    def enterEvent(self, event):
        self.copy_btn.setVisible(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.copy_btn.setVisible(False)
        super().leaveEvent(event)


# ----------------------------------------------------------------------
# Activity chip — per-turn collapsible step log
# ----------------------------------------------------------------------
class ActivityChip(QWidget):
    """Collapsed one-line tool summary; click to reveal the raw step log."""

    def __init__(self, summary: str, detail: str):
        super().__init__()
        self._detail = detail
        self._open = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.header = QFrame()
        self.header.setObjectName("chipHeader")
        self.header.setCursor(Qt.CursorShape.PointingHandCursor)
        self.header.setStyleSheet(
            f"""
            QFrame#chipHeader {{
                background: {COLOR_RAISED};
                border: 1px solid {COLOR_BORDER};
                border-radius: 6px;
            }}
            QFrame#chipHeader:hover {{ background: {COLOR_HOVER}; }}
            """
        )
        head_row = QHBoxLayout(self.header)
        head_row.setContentsMargins(9, 4, 9, 4)
        head_row.setSpacing(6)

        self.chevron = QLabel("\u25b8")
        self.chevron.setStyleSheet(
            f"color: {COLOR_TEXT_LOW}; font-size: 10px; background: transparent;"
        )
        head_row.addWidget(self.chevron)

        label = QLabel(summary)
        label.setStyleSheet(
            f'color: {COLOR_TEXT_MID}; font-family: "{FONT_UI}"; '
            f"font-size: 11px; background: transparent;"
        )
        head_row.addWidget(label)
        head_row.addStretch(1)
        self.header.mousePressEvent = self._on_click

        outer.addWidget(self.header)

        self.body = QPlainTextEdit(detail)
        self.body.setReadOnly(True)
        self.body.setFrameShape(QFrame.Shape.NoFrame)
        self.body.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.body.setStyleSheet(
            f'background: {COLOR_RAISED}; color: {COLOR_TEXT_LOW}; '
            f'border: 1px solid {COLOR_BORDER}; border-radius: 6px; '
            f'font-family: "{FONT_MONO}"; font-size: 11px; padding: 6px;'
        )
        self.body.setVisible(False)
        outer.addWidget(self.body)

        self._anim = None

    def _on_click(self, _event):
        self._open = not self._open
        self.chevron.setText("\u25be" if self._open else "\u25b8")
        self.body.setVisible(True)  # ensure mapped before animating height
        lines = max(1, self._detail.count("\n") + 1)
        target = min(lines, 14) * 18 + 16
        self._anim = QPropertyAnimation(self.body, b"maximumHeight", self)
        self._anim.setDuration(FADE_MS)
        self._anim.setStartValue(0 if self._open else max(0, self.body.height()))
        self._anim.setEndValue(target if self._open else 0)
        self._anim.setEasingCurve(
            QEasingCurve.Type.OutCubic if self._open else QEasingCurve.Type.InCubic
        )
        if not self._open:
            self._anim.finished.connect(lambda: self.body.setVisible(False))
        self._anim.start()


# ----------------------------------------------------------------------
# Messages
# ----------------------------------------------------------------------
class ShrinkableWidget(QWidget):
    """A widget that never imposes a horizontal minimum on its parent.

    Word-wrapped QLabels report their full unwrapped width as a minimum size
    hint, which would otherwise force this widget wider than its viewport and
    let content bleed past both window edges.
    """

    def minimumSizeHint(self):
        return QSize(0, super().minimumSizeHint().height())

    def sizeHint(self):
        return QSize(0, super().sizeHint().height())


class MessageBase(ShrinkableWidget):
    """One chat message, laid out in a centred max-width column."""

    def __init__(self):
        super().__init__()
        self._column = QWidget()
        self._column.setMaximumWidth(CONTENT_MAX_W)
        self._column.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Minimum
        )
        self._col_layout = QVBoxLayout(self._column)
        self._col_layout.setContentsMargins(0, 0, 0, 0)
        self._col_layout.setSpacing(10)

        row = QHBoxLayout(self)
        row.setContentsMargins(CONTENT_PAD, 0, CONTENT_PAD, 0)
        row.setSpacing(0)
        row.addStretch(1)
        row.addWidget(self._column, 0)
        row.addStretch(1)

    def resizeEvent(self, event):
        # The two stretches above starve the column, so drive its width here:
        # fill the target width when there is room, shrink when there is not.
        avail = self.width() - 2 * CONTENT_PAD
        self._column.setFixedWidth(max(0, min(CONTENT_MAX_W, avail)))
        super().resizeEvent(event)

    def _prose(self, text: str) -> QLabel:
        lbl = QLabel()
        lbl.setTextFormat(Qt.TextFormat.RichText)
        lbl.setText(markdown_to_html(text))
        lbl.setWordWrap(True)
        lbl.setMinimumWidth(0)
        lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.LinksAccessibleByMouse
        )
        lbl.setOpenExternalLinks(True)
        lbl.setStyleSheet(
            f'background: transparent; color: {COLOR_TEXT_HI}; font-size: 15px;'
        )
        return lbl

    def _render(self, text: str):
        for seg in _split_segments(text):
            if seg[0] == "prose":
                self._col_layout.addWidget(self._prose(seg[1]))
            else:
                self._col_layout.addWidget(CodeBlock(seg[2]))


class UserMessage(MessageBase):
    """Subtle filled block behind the text. No bubble tail."""

    def __init__(self, text: str):
        super().__init__()
        holder = QFrame()
        holder.setObjectName("userMsg")
        holder.setStyleSheet(
            f"""
            QFrame#userMsg {{
                background: {COLOR_USER_PILL};
                border-radius: 8px;
            }}
            QFrame#userMsg QLabel {{ background: transparent; }}
            """
        )
        hl = QVBoxLayout(holder)
        hl.setContentsMargins(14, 12, 14, 12)
        holder_layout = QVBoxLayout()
        hl.addLayout(holder_layout)

        for seg in _split_segments(text):
            if seg[0] == "prose":
                lbl = QLabel()
                lbl.setTextFormat(Qt.TextFormat.RichText)
                lbl.setText(markdown_to_html(seg[1]))
                lbl.setWordWrap(True)
                lbl.setMinimumWidth(0)
                lbl.setTextInteractionFlags(
                    Qt.TextInteractionFlag.TextSelectableByMouse
                )
                lbl.setStyleSheet(
                    f"background: transparent; color: {COLOR_TEXT_HI}; "
                    f"font-size: 15px;"
                )
                holder_layout.addWidget(lbl)
            else:
                holder_layout.addWidget(CodeBlock(seg[2]))

        # Keep the filled block hugging its content, left-aligned in the column.
        wrap = QWidget()
        wrap_row = QHBoxLayout(wrap)
        wrap_row.setContentsMargins(0, 0, 0, 0)
        wrap_row.setSpacing(0)
        wrap_row.addWidget(holder, 0, Qt.AlignmentFlag.AlignLeft)
        wrap_row.addStretch(1)
        self._col_layout.addWidget(wrap)


class AssistantMessage(MessageBase):
    """Full-width prose, no background. Optional activity chip below."""

    def __init__(self, text: str, step_log=None):
        super().__init__()
        self._render(text)

        summary = activity_summary(step_log)
        if summary:
            chip = ActivityChip(summary, "\n".join(
                l for l in step_log if isinstance(l, str)))
            self._col_layout.addWidget(chip)


# ----------------------------------------------------------------------
# Chat view with scroll-lock
# ----------------------------------------------------------------------
class ChatView(QScrollArea):
    """
    Scrollable chat column.

    Scroll-lock rule (reproduced from the CustomTkinter fix in commits
    b554f75 / 51e9229):
      - new content is followed down only when the user is already at the
        bottom (within ~5px of the true pixel bottom);
      - if the user has scrolled up, new content must NOT yank them down;
      - scrolling back to the bottom re-arms the follow behaviour.
    """

    BOTTOM_EPSILON = 5

    def __init__(self):
        super().__init__()
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setStyleSheet(
            f"""
            QScrollArea {{ background: {COLOR_BG}; border: none; }}
            QScrollBar:vertical {{
                background: {COLOR_BG}; width: 10px; margin: 0;
            }}
            QScrollBar::handle:vertical {{
                background: #22262f; border-radius: 5px; min-height: 30px;
            }}
            QScrollBar::handle:vertical:hover {{ background: #313745; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0;
            }}
            """
        )

        self._body = QWidget()
        self._body.setStyleSheet(f"background: {COLOR_BG};")
        self._layout = QVBoxLayout(self._body)
        self._layout.setContentsMargins(0, 24, 0, 24)
        self._layout.setSpacing(MSG_SPACING)
        self._layout.addStretch(1)

        self.setWidget(self._body)

        self._pinned = True  # follow new content while at the bottom
        self.verticalScrollBar().valueChanged.connect(self._on_scroll)
        self.verticalScrollBar().rangeChanged.connect(self._on_range_changed)

    # -- scroll-lock ----------------------------------------------------
    def _at_bottom(self) -> bool:
        bar = self.verticalScrollBar()
        return (bar.maximum() - bar.value()) <= self.BOTTOM_EPSILON

    def _on_scroll(self, _value):
        # Any user/environment scroll re-evaluates whether we're pinned. A
        # programmatic scroll to the bottom also lands us pinned, which is the
        # desired re-arm behaviour.
        self._pinned = self._at_bottom()

    def _on_range_changed(self, _min, _max):
        if self._pinned:
            self._scroll_to_bottom()

    def _scroll_to_bottom(self):
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())

    def force_scroll_bottom(self):
        self._pinned = True
        QTimer.singleShot(0, self._scroll_to_bottom)

    # -- messages -------------------------------------------------------
    def add_message(self, widget: QWidget, fade: bool = True):
        was_pinned = self._pinned
        # Insert before the trailing stretch so messages stay top-aligned.
        self._layout.insertWidget(self._layout.count() - 1, widget)

        if fade:
            effect = QGraphicsOpacityEffect(widget)
            widget.setGraphicsEffect(effect)
            anim = QPropertyAnimation(effect, b"opacity", widget)
            anim.setDuration(FADE_MS)
            anim.setStartValue(0.0)
            anim.setEndValue(1.0)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            anim.finished.connect(lambda: widget.setGraphicsEffect(None))
            anim.start()
            widget._fade_anim = anim  # keep a reference alive

        if was_pinned:
            self._pinned = True
            QTimer.singleShot(0, self._scroll_to_bottom)


# ----------------------------------------------------------------------
# Sidebar
# ----------------------------------------------------------------------
class Sidebar(QFrame):
    """Left collapsible drawer. Width animates on toggle."""

    def __init__(self):
        super().__init__()
        # Fixed at rest so the layout never shrinks it below its content
        # (which clipped the nav labels). toggle_sidebar drives min+max together.
        self.setFixedWidth(SIDEBAR_W)
        self.setStyleSheet(f"background: {COLOR_SIDEBAR};")
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        title = QLabel("  AgentBot")
        title.setStyleSheet(
            f'color: {COLOR_TEXT_HI}; font-family: "{FONT_UI}"; '
            f"font-size: 14px; font-weight: bold;"
        )
        title.setFixedHeight(44)
        layout.addWidget(title)

        self.new_chat = QPushButton("  +   New chat")
        self.new_chat.setFixedHeight(36)
        self.new_chat.setCursor(Qt.CursorShape.PointingHandCursor)
        self.new_chat.setStyleSheet(
            f"""
            QPushButton {{
                background: {COLOR_ACCENT};
                border: none;
                border-radius: 10px;
                color: #06281a;
                font-family: "{FONT_UI}";
                font-size: 12px;
                font-weight: bold;
                text-align: left;
                padding-left: 10px;
            }}
            QPushButton:hover {{ background: {COLOR_ACCENT_HOVER}; }}
            """
        )
        layout.addWidget(self.new_chat)
        layout.addSpacing(14)

        section = QLabel("  CONVERSATION")
        section.setStyleSheet(
            f'color: {COLOR_TEXT_LOW}; font-family: "{FONT_UI}"; '
            f"font-size: 10px; font-weight: bold;"
        )
        layout.addWidget(section)

        nav = QWidget()
        nav_layout = QVBoxLayout(nav)
        nav_layout.setContentsMargins(10, 0, 10, 0)
        nav_layout.setSpacing(2)
        nav_layout.addWidget(_nav_button("\U0001f4ac", "Current chat", active=True))
        layout.addWidget(nav)

        layout.addStretch(1)

        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setStyleSheet(f"background: {COLOR_BORDER};")
        layout.addSpacing(12)
        layout.addWidget(divider)
        layout.addSpacing(12)

        bottom = QWidget()
        bottom_layout = QVBoxLayout(bottom)
        bottom_layout.setContentsMargins(10, 0, 10, 10)
        bottom_layout.setSpacing(2)
        self.settings_btn = None
        for glyph, label in (
            ("\u2699", "Settings"),
            ("\U0001f4c5", "Tasks"),
            ("\U0001f4da", "Index docs"),
            ("\U0001f50d", "Activity log"),
            ("\U0001f4cb", "Copy all"),
            ("\U0001f4e4", "Export chat"),
        ):
            b = _nav_button(glyph, label)
            if label == "Settings":
                self.settings_btn = b
            bottom_layout.addWidget(b)
        layout.addWidget(bottom)


# ----------------------------------------------------------------------
# Main window
# ----------------------------------------------------------------------
class AgentWindow(QMainWindow):
    _command_done = Signal(str)      # background slash-command result
    _scheduled_triggered = Signal(str)  # scheduler fired (from its own thread)
    _reminder_result = Signal(str, str)  # (result, reminder_text)
    _hotkey_changed = Signal(bool)   # F9 pressed/released (from keyboard thread)
    _voice_event = Signal(str)       # recording/transcription status from worker

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Agent")
        self.resize(1100, 720)
        self.setStyleSheet(f"background: {COLOR_BG};")

        self._sidebar_anim = None
        self._worker = None
        self._processing = False
        self._settings_overlay = None
        self._input_outer = None
        self._tray_icon = None
        self._hotkey_installed = False
        self._recording = False
        self._scheduled_queue = []
        self._command_done.connect(self._on_command_done)
        self._scheduled_triggered.connect(self._on_scheduled_trigger)
        self._reminder_result.connect(self._reminder_done)
        self._hotkey_changed.connect(self._on_hotkey_changed)
        self._voice_event.connect(self._on_voice_event)

        # Conversation history shared with agent.run_agent_turn, loaded once.
        self.conversation_history = []
        try:
            self.conversation_history = get_agent().load_conversation_history()
        except Exception:
            self.conversation_history = []

        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.sidebar = Sidebar()
        if self.sidebar.settings_btn is not None:
            self.sidebar.settings_btn.clicked.connect(self.open_settings)
        root.addWidget(self.sidebar)

        root.addWidget(self._build_main_column(), 1)
        self.setCentralWidget(central)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._settings_overlay is not None:
            self._settings_overlay.setGeometry(self.rect())
        self._sync_input_column_width()

    def _sync_input_column_width(self):
        # Match the input column to the message column: cap at CONTENT_MAX_W,
        # shrink when the window is narrower than that.
        if self._input_outer is None:
            return
        avail = self._input_outer.width() - 2 * CONTENT_PAD
        self._input_column.setFixedWidth(max(0, min(CONTENT_MAX_W, avail)))

    def keyPressEvent(self, event):
        if (event.key() == Qt.Key.Key_Escape
                and self._settings_overlay is not None):
            self._close_settings()
            return
        super().keyPressEvent(event)

    # ------------------------------------------------------------------
    def _build_main_column(self) -> QWidget:
        col = QWidget()
        col.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout = QVBoxLayout(col)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(self._build_header())

        border = QFrame()
        border.setFixedHeight(1)
        border.setStyleSheet(f"background: {COLOR_BORDER};")
        layout.addWidget(border)

        self.chat = ChatView()
        layout.addWidget(self.chat, 1)
        layout.addWidget(self._build_input_area())
        return col

    def _build_header(self) -> QWidget:
        header = QWidget()
        header.setFixedHeight(HEADER_H)
        header.setStyleSheet(f"background: {COLOR_BG};")

        row = QHBoxLayout(header)
        row.setContentsMargins(10, 0, 10, 0)
        row.setSpacing(4)

        self.sidebar_btn = _icon_button("\u2630", "Toggle sidebar")
        self.sidebar_btn.clicked.connect(self.toggle_sidebar)
        row.addWidget(self.sidebar_btn)

        title = QLabel("Agent")
        title.setStyleSheet(
            f'color: {COLOR_TEXT_HI}; font-family: "{FONT_UI}"; '
            f"font-size: 15px; font-weight: bold;"
        )
        row.addWidget(title)
        row.addSpacing(6)

        self.status_dot = QLabel("\u25cf")
        self.status_dot.setStyleSheet(f"color: {COLOR_ACCENT}; font-size: 10px;")
        row.addWidget(self.status_dot)

        self.status_label = QLabel("Ready")
        self.status_label.setStyleSheet(
            f'color: {COLOR_TEXT_LOW}; font-family: "{FONT_UI}"; font-size: 11px;'
        )
        row.addWidget(self.status_label)
        row.addStretch(1)

        row.addWidget(_icon_button("\u21bb", "New chat", size=28))
        gear = _icon_button("\u2699", "Settings")
        gear.clicked.connect(self.open_settings)
        row.addWidget(gear)

        return header

    def _build_input_area(self) -> QWidget:
        outer = QWidget()
        outer.setStyleSheet(f"background: {COLOR_BG};")
        col = QVBoxLayout(outer)
        col.setContentsMargins(CONTENT_PAD, 8, CONTENT_PAD, 16)
        col.setSpacing(0)

        # Centre the pill in the same max-width column as the messages.
        holder = ShrinkableWidget()
        holder_row = QHBoxLayout(holder)
        holder_row.setContentsMargins(0, 0, 0, 0)
        holder_row.setSpacing(0)
        holder_row.addStretch(1)
        self._input_column = QWidget()
        self._input_column.setMaximumWidth(CONTENT_MAX_W)
        self._input_column.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Minimum)
        self._input_col_layout = QVBoxLayout(self._input_column)
        self._input_col_layout.setContentsMargins(0, 0, 0, 0)
        self._input_col_layout.setSpacing(0)
        holder_row.addWidget(self._input_column, 0)
        holder_row.addStretch(1)

        self.input_pill = QFrame()
        self.input_pill.setObjectName("inputPill")
        self.input_pill.setStyleSheet(
            f"""
            QFrame#inputPill {{
                background: {COLOR_INPUT_BG};
                border: 1px solid {COLOR_BORDER};
                border-radius: 18px;
            }}
            """
        )
        pill_row = QHBoxLayout(self.input_pill)
        pill_row.setContentsMargins(14, 8, 8, 8)
        pill_row.setSpacing(8)

        self.input_field = QPlainTextEdit()
        self.input_field.setPlaceholderText("Message AgentBot…")
        self.input_field.setFrameShape(QFrame.Shape.NoFrame)
        self.input_field.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.input_field.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.input_field.setFixedHeight(INPUT_LINE_H)
        self.input_field.setStyleSheet(
            f'background: transparent; color: {COLOR_TEXT_HI}; '
            f'border: none; font-size: 14px;'
        )
        self.input_field.textChanged.connect(self._on_input_changed)
        self.input_field.installEventFilter(self)
        pill_row.addWidget(self.input_field, 1)

        self.send_btn = QPushButton("\u2191")
        self.send_btn.setToolTip("Send")
        self.send_btn.setFixedSize(32, 32)
        self.send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.send_btn.clicked.connect(self.send)
        pill_row.addWidget(self.send_btn, 0, Qt.AlignmentFlag.AlignBottom)

        self._input_col_layout.addWidget(self.input_pill)
        col.addWidget(holder)
        self._input_outer = holder
        self._refresh_send_btn()
        return outer

    def _refresh_send_btn(self):
        has_text = bool(self._get_input_text())
        enabled = has_text and not self._processing
        self.send_btn.setEnabled(enabled)
        self.send_btn.setStyleSheet(
            f"""
            QPushButton {{
                background: {COLOR_ACCENT if enabled else COLOR_RAISED};
                border: none;
                border-radius: 16px;
                color: {COLOR_BG if enabled else COLOR_TEXT_LOW};
                font-size: 16px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background: {COLOR_ACCENT_HOVER if enabled else COLOR_RAISED};
            }}
            """
        )

    # ------------------------------------------------------------------
    # Input helpers
    # ------------------------------------------------------------------
    def _get_input_text(self) -> str:
        return self.input_field.toPlainText().strip()

    def _clear_input(self):
        self.input_field.clear()

    def _on_input_changed(self):
        self._autogrow_input()
        self._refresh_send_btn()

    def _autogrow_input(self):
        doc = self.input_field.document()
        lines = int(doc.size().height())
        target = max(1, min(INPUT_MAX_LINES, lines))
        height = target * INPUT_LINE_H + 4
        if abs(self.input_field.height() - height) > 2:
            self.input_field.setFixedHeight(height)

    def eventFilter(self, obj, event):
        # Enter sends, Shift+Enter inserts a newline.
        if obj is self.input_field and event.type() == QKeyEvent.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                    return False  # let QPlainTextEdit insert the newline
                self.send()
                return True
        return super().eventFilter(obj, event)

    # ------------------------------------------------------------------
    # Send / receive
    # ------------------------------------------------------------------
    def set_status(self, text, busy=False):
        self.status_label.setText(text)
        self.status_dot.setStyleSheet(
            f"color: {COLOR_ACCENT if not busy else COLOR_WARN}; font-size: 10px;"
        )

    def send(self):
        if self._processing:
            return
        text = self._get_input_text()
        if not text:
            return

        self._clear_input()

        if text.startswith("/"):
            self._handle_slash_command(text)
            return

        self.chat.add_message(UserMessage(text))
        self._start_agent_turn(text)

    def _start_agent_turn(self, text):
        """Kick off a background agent turn for `text`."""
        self._processing = True
        self.input_field.setEnabled(False)
        self._refresh_send_btn()
        self.set_status("Thinking…", busy=True)

        self._worker = AgentWorker(text, self.conversation_history)
        self._worker.completed.connect(self._on_turn_completed)
        self._worker.failed.connect(self._on_turn_failed)
        self._worker.start()

    def _end_turn(self):
        self._processing = False
        self.input_field.setEnabled(True)
        self.input_field.setFocus()
        self._refresh_send_btn()
        self.set_status("Ready", busy=False)
        # A scheduled task may have fired while this turn was running.
        self._drain_scheduled_queue()

    def _on_turn_completed(self, payload):
        message, step_log = payload
        self.chat.add_message(AssistantMessage(message, step_log=step_log))
        self._speak_reply(message)
        self._end_turn()

    def _on_turn_failed(self, error_text):
        self.chat.add_message(AssistantMessage(error_text))
        self._end_turn()

    def _speak_reply(self, message):
        """Speak a reply if TTS_AUTO_SPEAK is on. Matches agent_gui.handle_result.

        speak() queues to its own worker thread and returns immediately, so no
        extra threading is needed here.
        """
        if not message:
            return
        try:
            auto_speak = str(
                read_config_file_raw().get("TTS_AUTO_SPEAK", "true")
            ).strip().lower() in ("true", "1", "yes", "on")
        except Exception:
            auto_speak = True
        if not auto_speak:
            return
        try:
            get_voice_output().speak(message)
        except Exception as e:
            print(f"[tts] speak failed: {e}")

    # ------------------------------------------------------------------
    # Slash commands — faithful port of agent_gui._handle_slash_command
    # ------------------------------------------------------------------
    def _finish_slash_command(self, text, result):
        """Mirror agent_gui._finish_slash_command: bubble the result and
        record the exchange in history."""
        if not isinstance(result, str):
            result = str(result) if result is not None else "(no result)"
        self.chat.add_message(UserMessage(text))
        self.chat.add_message(AssistantMessage(result))
        self.conversation_history.append(f"User: {text}")
        self.conversation_history.append(f"System: {result}")
        try:
            get_agent().save_conversation_history(self.conversation_history)
        except Exception:
            pass

    def _handle_slash_command(self, text):
        """Route a '/'-prefixed input to its tool. Returns True if handled.

        Non-Telegram commands are handled before importing telegram_user so
        they keep working when Telethon is missing or not logged in.
        """
        if not text.startswith("/"):
            return False

        parts = text.split(None, 1)
        cmd = parts[0].lower()
        args = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("/help", "/?"):
            self.chat.add_message(AssistantMessage(HELP_TEXT))
            self.conversation_history.append(f"User: {text}")
            self.conversation_history.append(f"System: {HELP_TEXT}")
            return True

        try:
            agent = get_agent()
        except Exception as e:
            self._finish_slash_command(text, f"ERROR: {e}")
            return True

        if cmd == "/find":
            result = ("ERROR: format is /find Folder|pat1,pat2" if not args
                      else agent.find_files(args))
            return self._finish_slash_command(text, result)

        if cmd == "/move":
            result = ("ERROR: format is /move Source|pat1,pat2|Dest (add |confirm to execute)"
                      if args.count("|") < 2 else agent.move_files(args))
            return self._finish_slash_command(text, result)

        if cmd == "/mkdir":
            result = ("ERROR: format is /mkdir Name" if not args
                      else agent.make_folder(args))
            return self._finish_slash_command(text, result)

        if cmd == "/symbols":
            result = ("ERROR: format is /symbols file.py" if not args
                      else agent.list_symbols(args))
            return self._finish_slash_command(text, result)

        if cmd == "/map":
            target = args or os.path.dirname(os.path.abspath(agent.__file__))
            return self._finish_slash_command(text, agent.repo_map(target))

        if cmd == "/recipe":
            if not args:
                result = "ERROR: format is /recipe Name"
            else:
                from recipes import run_recipe
                result = run_recipe(args.strip())
            return self._finish_slash_command(text, result)

        if cmd == "/scan":
            if not args:
                result = "ERROR: format is /scan <file_path>"
            else:
                import security_tools
                result = security_tools.scan_file(args.strip())
            return self._finish_slash_command(text, result)

        if cmd == "/quarantine":
            if not args:
                result = "ERROR: format is /quarantine <file_path>"
            else:
                import security_tools
                result = security_tools.quarantine_file(args.strip())
            return self._finish_slash_command(text, result)

        if cmd == "/quarantine-list":
            import security_tools
            return self._finish_slash_command(text, security_tools.list_quarantine())

        if cmd == "/rag":
            if not args:
                result = "ERROR: format is /rag <query>"
            else:
                from rag_tool import search_documents
                result = search_documents(args.strip())
            return self._finish_slash_command(text, result)

        if cmd == "/gif-stats":
            try:
                import gif_library
                result = gif_library.library_stats()
            except Exception as e:
                result = f"ERROR: {e}"
            return self._finish_slash_command(text, result)

        if cmd == "/eval":
            return self._handle_eval(text, args)

        if cmd == "/ig":
            return self._handle_ig(text, args)

        if cmd == "/reindex":
            self._run_background_command(
                text, "🔍 Re-indexing configured folders…", self._reindex_work)
            return True

        if cmd == "/import-chat":
            if not args.strip():
                self.chat.add_message(AssistantMessage(
                    "Usage: /import-chat <path to result.json>\n"
                    "Parses a Telegram Desktop export into scrubbed monthly "
                    "files under C:\\Users\\USER\\PrivateExport."))
                return True
            try:
                import telegram_export_parser  # noqa: F401
            except Exception as e:
                self.chat.add_message(UserMessage(text))
                self.chat.add_message(AssistantMessage(f"ERROR: {e}"))
                return True
            self.chat.add_message(UserMessage(text))
            self._run_background_command(
                text, "📥 Parsing and scrubbing Telegram export…",
                lambda: self._import_chat_work(args.strip()))
            return True

        if cmd == "/label-gifs":
            return self._handle_label_gifs(text, args)

        # Telegram commands come last: they require telethon and a login.
        try:
            import telegram_user
        except ImportError:
            self.chat.add_message(AssistantMessage(
                "ERROR: telegram_user module not found."))
            return True
        if cmd == "/send":
            result = ("ERROR: format is /send Contact|message" if "|" not in args
                      else telegram_user.send_telegram_tool(args))
        elif cmd == "/edit":
            result = ("ERROR: format is /edit Contact|old text|new text"
                      if args.count("|") < 2 else telegram_user.edit_tool(args))
        elif cmd in ("/del", "/delete"):
            result = ("ERROR: format is /del Contact|text (add |confirm to delete)"
                      if "|" not in args else telegram_user.delete_tool(args))
        elif cmd in ("/dellast", "/dl"):
            if not args:
                result = "ERROR: format is /dellast Contact"
            else:
                try:
                    result = telegram_user.delete_latest_tool(args.strip())
                except AttributeError:
                    result = "ERROR: telegram_user.delete_latest_tool not available."
        else:
            return False

        return self._finish_slash_command(text, result)

    def _handle_eval(self, text, args):
        parts = (args or "").strip().split()
        sub = parts[0] if parts else "help"
        try:
            import evals
        except Exception as e:
            return self._finish_slash_command(text, f"ERROR: {e}")

        if sub == "run":
            if len(parts) < 2:
                return self._finish_slash_command(text, "Usage: /eval run <suite> [<provider>]")
            suite = parts[1]
            provider = parts[2] if len(parts) > 2 else None
            run = evals.run_suite(suite, force_provider=provider)
            passed = sum(1 for r in run["results"] if r["passed"])
            return self._finish_slash_command(
                text,
                f"Suite {suite} — pass rate {run['pass_rate']:.2f} "
                f"({passed}/{len(run['results'])}) "
                f"provider={provider or 'chain'} run={run['run_id']}")

        if sub == "list":
            suites = sorted(p.stem for p in evals.SUITES_DIR.glob("*.json"))
            return self._finish_slash_command(
                text, "Suites: " + (", ".join(suites) if suites else "(none)"))

        if sub == "traces":
            try:
                n = int(parts[1]) if len(parts) > 1 else 20
            except ValueError:
                n = 20
            rows = evals.list_traces(n)
            if not rows:
                return self._finish_slash_command(text, "No traces.")
            return self._finish_slash_command(text, "\n".join(
                f"[{r['id']}] {r['provider']} parse={r['parse_ok']} "
                f"empty={r['empty_reply']} — {r['request']}" for r in rows))

        if sub == "label":
            if len(parts) < 3:
                return self._finish_slash_command(text, "Usage: /eval label <trace_id> <good|bad>")
            return self._finish_slash_command(
                text, evals.label_trace(parts[1], "user_feedback", parts[2]))

        if sub == "diff":
            if len(parts) < 3:
                return self._finish_slash_command(text, "Usage: /eval diff <run_a> <run_b>")
            return self._finish_slash_command(text, evals.diff_runs(parts[1], parts[2]))

        return self._finish_slash_command(
            text,
            "Usage: /eval run <suite> [<provider>] | list | traces [n] | "
            "label <trace_id> <good|bad> | diff <run_a> <run_b>")

    def _handle_ig(self, text, args):
        try:
            import gateway
        except Exception as e:
            return self._finish_slash_command(text, f"ERROR: {e}")
        sub = (args or "status").strip().lower()
        if sub == "status":
            gw = gateway.get_active_adapter()
            msg = ("Instagram gateway: not running." if gw is None
                   else f"Instagram gateway: running, poll every {gw.poll_interval}s.")
            return self._finish_slash_command(text, msg)
        if sub == "restart":
            gw = gateway.get_active_adapter()
            if gw:
                gw.stop()
            from config import load_config
            result = gateway.start_gateway(load_config())
            msg = str(result) if result else "Disabled in config."
            return self._finish_slash_command(text, msg)
        return self._finish_slash_command(text, "Usage: /ig status | /ig restart")

    def _handle_label_gifs(self, text, args):
        parts_l = args.split()
        limit = None
        min_occ = 1
        if len(parts_l) == 1 and parts_l[0].isdigit():
            limit = int(parts_l[0])
        elif len(parts_l) == 2 and parts_l[0].lower() == "min":
            if not parts_l[1].isdigit():
                self.chat.add_message(UserMessage(text))
                self.chat.add_message(AssistantMessage(
                    "ERROR: /label-gifs min N — N must be a number"))
                return True
            min_occ = int(parts_l[1])

        try:
            import gif_library  # noqa: F401
        except Exception as e:
            self.chat.add_message(UserMessage(text))
            self.chat.add_message(AssistantMessage(f"ERROR: {e}"))
            return True

        def work():
            try:
                import gif_library
                msg = gif_library.label_gifs(min_occurrences=min_occ, limit=limit)
            except Exception as e:
                msg = f"❌ Labeling failed: {e}"
            return msg

        self.chat.add_message(UserMessage(text))
        self._run_background_command(
            text, "🏷️ Labeling GIFs… this can take a while.", work)
        return True

    def _run_background_command(self, text, busy_msg, work):
        """Run a blocking command off the GUI thread; post the result back."""
        self._processing = True
        self.input_field.setEnabled(False)
        self._refresh_send_btn()
        self.chat.add_message(AssistantMessage(busy_msg))
        self.set_status("Working…", busy=True)

        def runner():
            try:
                result = work()
            except Exception as e:
                result = f"ERROR: {e}"
            self._command_done.emit(result)

        threading.Thread(target=runner, daemon=True).start()

    def _on_command_done(self, result):
        self.chat.add_message(AssistantMessage(result))
        self.conversation_history.append(f"System: {result}")
        try:
            get_agent().save_conversation_history(self.conversation_history)
        except Exception:
            pass
        self._end_turn()

    def _reindex_work(self):
        import rag_tool
        cfg = read_config_file_raw()
        merged = dict(get_config_module().DEFAULTS)
        merged.update(cfg)
        folders = merged.get("RAG_AUTO_INDEX_FOLDERS", [])
        if isinstance(folders, str):
            folders = [f.strip() for f in folders.split(",") if f.strip()]
        lines = []
        skipped = 0
        for folder in folders:
            if not os.path.isdir(folder):
                skipped += 1
                continue
            try:
                res = rag_tool.index_documents(folder)
                first = (res or "done").splitlines()[0] if res else "done"
            except Exception as e:
                first = f"failed: {e}"
            lines.append(f"  {os.path.basename(folder) or folder}: {first}")
        msg = "✅ Reindex complete:\n" + "\n".join(lines)
        if skipped:
            msg += f"\n  ({skipped} configured folder(s) not found, skipped)"
        return msg

    def _import_chat_work(self, path):
        import telegram_export_parser as tep
        r = tep.parse_json_export(path)
        return (
            f"✅ Chat import complete:\n"
            f"  Chat: {r['chat_name']}\n"
            f"  Files written: {r['files_written']}\n"
            f"  Messages indexed: {r['total_messages']}\n"
            f"  Dropped (address/blocklist): {r['dropped_lines']}\n"
            f"  Redacted: {r['modified_lines']}\n"
            f"  Output: {r['output_dir']}\n\n"
            f"Review the scrub report before indexing:\n"
            f"  {r['report_path']}\n\n"
            f"Run /reindex to add it to RAG."
        )

    # ------------------------------------------------------------------
    # Settings overlay — in-window, dim backdrop, centred panel
    # ------------------------------------------------------------------
    def open_settings(self):
        try:
            cfgmod = get_config_module()
        except Exception as e:
            self.chat.add_message(AssistantMessage(f"ERROR: config.py not found — {e}"))
            return
        cfg = read_config_file_raw()

        overlay = QWidget(self)
        overlay.setGeometry(self.rect())
        overlay.setStyleSheet("background: rgba(0, 0, 0, 170);")
        overlay.mousePressEvent = lambda e: None  # swallow clicks on backdrop

        panel = QFrame(overlay)
        panel.setObjectName("settingsPanel")
        panel.setStyleSheet(
            f"""
            QFrame#settingsPanel {{
                background: {COLOR_BG};
                border: 1px solid {COLOR_BORDER};
                border-radius: 12px;
            }}
            QFrame#settingsPanel QLabel {{
                background: transparent;
                color: {COLOR_TEXT_HI};
                font-family: "{FONT_UI}";
                font-size: 12px;
            }}
            QLineEdit, QComboBox {{
                background: {COLOR_INPUT_BG};
                border: 1px solid {COLOR_BORDER};
                border-radius: 6px;
                color: {COLOR_TEXT_HI};
                padding: 5px 8px;
                font-family: "{FONT_MONO}";
                font-size: 11px;
            }}
            QComboBox:hover, QLineEdit:hover {{ border-color: {COLOR_ACCENT_DIM}; }}
            QComboBox::drop-down {{ border: none; width: 18px; }}
            QComboBox QAbstractItemView {{
                background: {COLOR_BG_ALT};
                color: {COLOR_TEXT_HI};
                selection-background-color: {COLOR_SELECTED};
            }}
            """
        )
        outer = QVBoxLayout(overlay)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(panel, 0, Qt.AlignmentFlag.AlignCenter)
        panel.setFixedSize(760, 620)

        playout = QVBoxLayout(panel)
        playout.setContentsMargins(0, 0, 0, 0)
        playout.setSpacing(0)

        # Header bar
        bar = QWidget()
        bar.setFixedHeight(HEADER_H)
        bar.setStyleSheet(f"background: {COLOR_SIDEBAR}; border-top-left-radius: 12px;"
                          f" border-top-right-radius: 12px;")
        bar_row = QHBoxLayout(bar)
        bar_row.setContentsMargins(10, 0, 10, 0)
        back = _icon_button("\u2190", "Close settings")
        back.clicked.connect(self._close_settings)
        bar_row.addWidget(back)
        bar_title = QLabel("Settings")
        bar_title.setStyleSheet(
            f'color: {COLOR_TEXT_HI}; font-family: "{FONT_UI}"; '
            f"font-size: 15px; font-weight: bold; background: transparent;"
        )
        bar_row.addWidget(bar_title)
        path_lbl = QLabel(f"saved to {cfgmod.CONFIG_PATH}")
        path_lbl.setStyleSheet(
            f'color: {COLOR_TEXT_LOW}; font-family: "{FONT_UI}"; '
            f"font-size: 10px; background: transparent;"
        )
        bar_row.addWidget(path_lbl)
        bar_row.addStretch(1)
        playout.addWidget(bar)

        # Tabs
        tabs = QTabWidget()
        tabs.setStyleSheet(
            f"""
            QTabWidget::pane {{ border: none; background: {COLOR_BG_ALT}; }}
            QTabBar::tab {{
                background: {COLOR_RAISED}; color: {COLOR_TEXT_MID};
                padding: 7px 14px; border-top-left-radius: 6px;
                border-top-right-radius: 6px; margin-right: 2px;
                font-family: "{FONT_UI}"; font-size: 11px;
            }}
            QTabBar::tab:hover {{ background: {COLOR_HOVER}; color: {COLOR_TEXT_HI}; }}
            QTabBar::tab:selected {{ background: {COLOR_ACCENT_DIM}; color: {COLOR_TEXT_HI}; }}
            """
        )
        playout.addWidget(tabs, 1)

        entries = {}

        def make_tab(name):
            page = QWidget()
            lay = QVBoxLayout(page)
            lay.setContentsMargins(12, 10, 12, 10)
            lay.setSpacing(6)
            tabs.addTab(page, name)
            return page, lay

        def section(lay, text):
            lbl = QLabel(text)
            lbl.setStyleSheet(
                f'color: {COLOR_TEXT_MID}; font-family: "{FONT_UI}"; '
                f"font-size: 13px; font-weight: bold; background: transparent;"
            )
            lay.addWidget(lbl)

        def field(lay, label, key, secret=False):
            row = QWidget()
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            lab = QLabel(label)
            lab.setFixedWidth(210)
            rl.addWidget(lab)
            edit = QLineEdit(str(cfg.get(key, cfgmod.DEFAULTS.get(key, ""))))
            if secret:
                edit.setEchoMode(QLineEdit.EchoMode.Password)
            rl.addWidget(edit, 1)
            lay.addWidget(row)
            entries[key] = ("text", edit)
            return edit

        def dropdown(lay, label, key, choices):
            row = QWidget()
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            lab = QLabel(label)
            lab.setFixedWidth(210)
            rl.addWidget(lab)
            saved = str(cfg.get(key, choices[0]))
            values = list(choices)
            if saved not in values:
                values.append(saved)
            combo = QComboBox()
            combo.addItems(values)
            combo.setCurrentText(saved)
            combo.setFixedWidth(220)
            rl.addWidget(combo)
            rl.addStretch(1)
            lay.addWidget(row)
            entries[key] = ("combo", combo)
            return combo

        # --- API Keys ---
        page, lay = make_tab("API Keys")
        section(lay, "Groq"); field(lay, "API key", "GROQ_API_KEY", secret=True)
        section(lay, "Google Gemini"); field(lay, "API key", "GEMINI_API_KEY", secret=True)
        section(lay, "OpenRouter"); field(lay, "API key", "OPENROUTER_API_KEY", secret=True)
        section(lay, "Cerebras"); field(lay, "API key", "CEREBRAS_API_KEY", secret=True)
        section(lay, "Mistral"); field(lay, "API key", "MISTRAL_API_KEY", secret=True)
        section(lay, "Cloudflare Workers AI")
        field(lay, "API key", "CLOUDFLARE_API_KEY", secret=True)
        field(lay, "Account ID", "CLOUDFLARE_ACCOUNT_ID")
        section(lay, "Cohere"); field(lay, "API key", "COHERE_API_KEY", secret=True)
        section(lay, "HuggingFace"); field(lay, "API key", "HUGGINGFACE_API_KEY", secret=True)
        section(lay, "DeepSeek"); field(lay, "API key", "DEEPSEEK_API_KEY", secret=True)
        lay.addStretch(1)

        # --- Chat ---
        page, lay = make_tab("Chat")
        section(lay, "Input")
        dropdown(lay, "Enter key behaviour", "ENTER_SENDS", ["true", "false"])
        section(lay, "Display")
        dropdown(lay, "Typing indicator", "SHOW_TYPING_INDICATOR", ["true", "false"])
        dropdown(lay, "Show system messages", "SHOW_SYSTEM_MESSAGES", ["true", "false"])
        dropdown(lay, "Show timestamps", "SHOW_TIMESTAMPS", ["true", "false"])
        dropdown(lay, "Autocorrect outgoing messages", "AUTOCORRECT_ENABLED", ["true", "false"])
        section(lay, "History")
        field(lay, "Conversation history length", "HISTORY_LENGTH")
        reset_btn = QPushButton("Reset conversation memory")
        reset_btn.setFixedHeight(32)
        reset_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        reset_btn.setStyleSheet(
            f"""
            QPushButton {{
                background: {COLOR_DANGER}; border: none; border-radius: 8px;
                color: #ffffff; font-family: "{FONT_UI}"; font-size: 12px;
                font-weight: bold;
            }}
            QPushButton:hover {{ background: {COLOR_DANGER_HOVER}; }}
            """
        )
        reset_btn.clicked.connect(self.reset_memory)
        lay.addWidget(reset_btn)
        note = QLabel("Forgets the saved history only — the chat on screen is kept.")
        note.setStyleSheet(
            f'color: {COLOR_TEXT_LOW}; font-family: "{FONT_UI}"; '
            f"font-size: 10px; background: transparent;"
        )
        note.setWordWrap(True)
        lay.addWidget(note)
        lay.addStretch(1)

        # --- Voice ---
        page, lay = make_tab("Voice")
        section(lay, "Voice output")
        dropdown(lay, "Voice on", "TTS_ENABLED", ["true", "false"])
        dropdown(lay, "Auto-speak replies", "TTS_AUTO_SPEAK", ["true", "false"])
        dropdown(lay, "Voice", "TTS_VOICE", [
            "af_heart", "af_bella", "af_nicole", "af_sarah", "am_michael",
            "am_adam", "bf_emma", "bf_isabella", "bm_george", "bm_lewis"])
        dropdown(lay, "Speed", "TTS_SPEED", ["0.75", "0.9", "1.0", "1.1", "1.25", "1.5"])
        dropdown(lay, "Language", "TTS_LANG", ["a", "b"])
        field(lay, "Max characters per reply", "TTS_MAX_CHARS")
        lay.addStretch(1)

        # --- Appearance ---
        page, lay = make_tab("Appearance")
        section(lay, "Theme")
        dropdown(lay, "Appearance mode", "THEME", ["dark", "light", "system"])
        section(lay, "Chat bubbles")
        dropdown(lay, "Font size", "FONT_SIZE", ["10", "11", "12", "13", "14", "15", "16"])
        dropdown(lay, "Max width (% of window)", "BUBBLE_WIDTH", ["50", "60", "65", "72", "80", "90"])
        dropdown(lay, "Corner radius", "BUBBLE_RADIUS", ["0", "8", "12", "16", "20", "24"])
        note = QLabel("Appearance changes require a restart.")
        note.setStyleSheet(
            f'color: {COLOR_TEXT_LOW}; font-family: "{FONT_UI}"; '
            f"font-size: 10px; background: transparent;"
        )
        lay.addWidget(note)
        lay.addStretch(1)

        # --- Telegram ---
        page, lay = make_tab("Telegram")
        section(lay, "Bot API (notifications to you)")
        field(lay, "Bot token", "TELEGRAM_BOT_TOKEN", secret=True)
        field(lay, "Your user ID", "TELEGRAM_USER_ID")
        section(lay, "Personal account (message contacts)")
        field(lay, "API ID", "TELEGRAM_API_ID")
        field(lay, "API hash", "TELEGRAM_API_HASH", secret=True)
        lay.addStretch(1)

        # --- Advanced ---
        page, lay = make_tab("Advanced")
        section(lay, "Brain")
        field(lay, "Provider priority", "AGENT_BRAIN_PRIORITY")
        field(lay, "Max steps per turn", "MAX_STEPS_PER_TURN")
        field(lay, "Temperature (0.0-1.0)", "TEMPERATURE")
        section(lay, "Ollama (local)")
        field(lay, "Server URL", "OLLAMA_URL")
        field(lay, "Model name", "OLLAMA_MODEL")
        field(lay, "Timeout (seconds)", "OLLAMA_TIMEOUT")
        section(lay, "Other")
        dropdown(lay, "Check for updates", "CHECK_UPDATES", ["true", "false"])
        dropdown(lay, "Log level", "LOG_LEVEL", ["info", "debug", "off"])
        lay.addStretch(1)

        # --- Bottom buttons ---
        btn_row = QWidget()
        br = QHBoxLayout(btn_row)
        br.setContentsMargins(14, 8, 14, 14)
        br.setSpacing(8)

        def footer_button(text, primary=False, danger=False):
            b = QPushButton(text)
            b.setFixedHeight(34)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            bg = COLOR_ACCENT if primary else (COLOR_DANGER if danger else COLOR_RAISED)
            hover = (COLOR_ACCENT_HOVER if primary
                     else (COLOR_DANGER_HOVER if danger else COLOR_HOVER))
            fg = COLOR_BG if primary else COLOR_TEXT_HI
            b.setStyleSheet(
                f"""
                QPushButton {{
                    background: {bg}; border: none; border-radius: 8px;
                    color: {fg}; font-family: "{FONT_UI}"; font-size: 12px;
                    font-weight: bold; padding: 0 16px;
                }}
                QPushButton:hover {{ background: {hover}; }}
                """
            )
            return b

        save_btn = footer_button("Save", primary=True)
        save_btn.clicked.connect(lambda: self._save_settings(entries))
        cancel_btn = footer_button("Cancel")
        cancel_btn.clicked.connect(self._close_settings)
        default_btn = footer_button("Reset to defaults", danger=True)
        default_btn.clicked.connect(self._reset_settings_defaults)
        br.addWidget(default_btn)
        br.addStretch(1)
        br.addWidget(cancel_btn)
        br.addWidget(save_btn)
        playout.addWidget(btn_row)

        self._settings_overlay = overlay
        overlay.show()
        overlay.raise_()

    def _close_settings(self):
        if self._settings_overlay is not None:
            self._settings_overlay.deleteLater()
            self._settings_overlay = None

    def _save_settings(self, entries):
        updates = {}
        for key, (kind, widget) in entries.items():
            updates[key] = (widget.currentText() if kind == "combo"
                            else widget.text().strip())
        try:
            write_config_keys(updates)
        except Exception as e:
            self.chat.add_message(AssistantMessage(f"ERROR: save failed — {e}"))
            return
        self._close_settings()
        self.chat.add_message(AssistantMessage(
            "✅ Settings saved. Restart AgentBot for changes to take effect."))

    def _reset_settings_defaults(self):
        cfgmod = get_config_module()
        try:
            write_config_keys(dict(cfgmod.DEFAULTS))
        except Exception as e:
            self.chat.add_message(AssistantMessage(f"ERROR: reset failed — {e}"))
            return
        self._close_settings()
        self.chat.add_message(AssistantMessage("Settings reset to defaults."))

    # ------------------------------------------------------------------
    # Memory reset — conversation history only, never facts.json
    # ------------------------------------------------------------------
    def reset_memory(self):
        """Forget saved conversation history; leave the on-screen chat alone.

        Touches AgentMemory/conversation.json only — NOT facts.json.
        """
        reply = QMessageBox.question(
            self, "Reset memory",
            "Forget the saved conversation (memory)?\n\n"
            "The chat on screen stays as-is.\n"
            "The next session starts fresh.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self.conversation_history.clear()
        try:
            get_agent().save_conversation_history(self.conversation_history)
        except Exception:
            pass
        self.chat.add_message(AssistantMessage(
            "Memory reset — saved history cleared, chat kept."))

    # ------------------------------------------------------------------
    def toggle_sidebar(self):
        visible = self.sidebar.isVisible()
        if visible:
            start, end = SIDEBAR_W, 0
        else:
            self.sidebar.setVisible(True)
            start, end = 0, SIDEBAR_W

        # Drive min and max together: the sidebar is fixed-width at rest, so
        # animating only one bound would leave the other pinning the width.
        group = QParallelAnimationGroup(self)
        for prop in (b"minimumWidth", b"maximumWidth"):
            anim = QPropertyAnimation(self.sidebar, prop, self)
            anim.setDuration(200)
            anim.setStartValue(start)
            anim.setEndValue(end)
            anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
            group.addAnimation(anim)
        if visible:
            group.finished.connect(lambda: self.sidebar.setVisible(False))
        group.start()
        self._sidebar_anim = group

    def show_greeting(self):
        if self.conversation_history:
            self.chat.add_message(AssistantMessage(
                f"Resumed — {len(self.conversation_history)} messages from last session."
            ), fade=False)
        else:
            self.chat.add_message(AssistantMessage(
                "Hi! Ask me anything. Type a message below and press Enter."
            ), fade=False)

    # ------------------------------------------------------------------
    # Background services — tray, hotkey, scheduler
    # ------------------------------------------------------------------
    def start_services(self):
        """Wire tray, hotkey and scheduler. Call after the window is shown.

        The scheduler's callback fires on its own daemon thread, so it routes
        through a Qt signal rather than touching widgets directly. The tray
        callbacks likewise arrive off the GUI thread.
        """
        if scheduler is not None:
            try:
                scheduler.start_scheduler(self._scheduled_triggered.emit)
            except Exception as e:
                print(f"[scheduler] failed to start: {e}")
        else:
            print("[scheduler] module unavailable; skipped")

        self._setup_global_hotkey()

        # Delay the tray like the old GUI (root.after(800, _start_tray)):
        # pystray needs the Qt event loop to be up and running first.
        QTimer.singleShot(800, self._start_tray)

    # -- tray -----------------------------------------------------------
    def _start_tray(self):
        if tray is None:
            print("[tray] module unavailable; skipped")
            return
        try:
            self._tray_icon = tray.start_tray(
                on_show=self._tray_show,
                on_quit=self._tray_quit,
            )
            print(f"[tray] icon = {self._tray_icon}")
        except Exception as e:
            print(f"[tray] failed: {e}")
            self._tray_icon = None

    def _tray_show(self):
        # Runs on the pystray thread — hop back to the GUI thread.
        if threading.current_thread() is threading.main_thread():
            self.show_window()
        else:
            QTimer.singleShot(0, self.show_window)

    def _tray_quit(self):
        if threading.current_thread() is threading.main_thread():
            self.quit_app()
        else:
            QTimer.singleShot(0, self.quit_app)

    def hide_to_tray(self):
        """Close button: save history and withdraw (match the old GUI)."""
        try:
            get_agent().save_conversation_history(self.conversation_history)
        except Exception:
            pass
        self.hide()

    def show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def quit_app(self):
        try:
            get_agent().save_conversation_history(self.conversation_history)
        except Exception:
            pass
        # Stop any in-flight recording / speech before tearing down.
        if self._recording and _voice_module is not None:
            try:
                _voice_module.stop_and_transcribe()
            except Exception:
                pass
            self._recording = False
        if _voice_output_module is not None:
            try:
                _voice_output_module.stop()
            except Exception:
                pass
        if self._tray_icon is not None:
            try:
                self._tray_icon.stop()
            except Exception:
                pass
        if self._hotkey_installed and keyboard is not None:
            try:
                keyboard.unhook_all()
            except Exception:
                pass
            self._hotkey_installed = False
        QApplication.instance().quit()

    def closeEvent(self, event):
        # Match the old GUI: closing the window hides to tray, it does not exit.
        event.ignore()
        self.hide_to_tray()

    # -- global hotkey (F9) → push-to-talk ------------------------------
    def _setup_global_hotkey(self):
        if keyboard is None:
            print("[hotkey] 'keyboard' not installed. Run: pip install keyboard")
            return
        try:
            keyboard.on_press_key("f9", self._hotkey_press, suppress=False)
            keyboard.on_release_key("f9", self._hotkey_release, suppress=False)
            self._hotkey_installed = True
            print("[hotkey] F9 registered (hold to talk, release to send)")
        except Exception as e:
            print(f"[hotkey] failed to register: {e}")

    def _hotkey_press(self, event=None):
        # Runs on the keyboard library's thread — never touch widgets here.
        self._hotkey_changed.emit(True)

    def _hotkey_release(self, event=None):
        self._hotkey_changed.emit(False)

    def _on_hotkey_changed(self, pressed):
        """GUI-thread slot for F9 press/release."""
        if pressed:
            self._start_recording()
        else:
            self._stop_recording()

    def _start_recording(self):
        if self._recording or self._processing:
            return
        self._recording = True
        self.set_status("Recording… release to send", busy=True)

        def work():
            try:
                get_voice().start_recording()
                self._voice_event.emit("recording_started")
            except Exception as e:
                self._voice_event.emit(f"error:Mic error: {e}")

        threading.Thread(target=work, daemon=True).start()

    def _stop_recording(self):
        if not self._recording:
            return
        self._recording = False
        self.set_status("Transcribing…", busy=True)

        def work():
            try:
                text = get_voice().stop_and_transcribe()
                self._voice_event.emit(f"transcript:{text}")
            except Exception as e:
                self._voice_event.emit(f"error:Transcribe error: {e}")

        threading.Thread(target=work, daemon=True).start()

    def _on_voice_event(self, payload):
        """GUI-thread slot for recording/transcription results."""
        if payload.startswith("error:"):
            self._recording = False
            self.set_status(payload[len("error:"):], busy=False)
            return
        if payload == "recording_started":
            return
        if payload.startswith("transcript:"):
            self._handle_transcript(payload[len("transcript:"):])

    def _handle_transcript(self, text):
        # Match the old GUI: type it into the input, then send immediately.
        self._recording = False
        if text and text.strip():
            self.input_field.setPlainText(text.strip())
            self.send()
        else:
            self.set_status("No speech detected — try again", busy=False)

    # -- scheduler notifications ----------------------------------------
    def _on_scheduled_trigger(self, request_text):
        """Slot for _scheduled_triggered. Runs on the GUI thread."""
        if request_text.startswith("REMINDER:"):
            reminder_text = request_text[len("REMINDER:"):].strip()
            self._fire_reminder_direct(reminder_text)
            return
        # A scheduled task runs a full agent turn. If one is already running,
        # queue it (the old GUI overlapped two racing turns instead — that is
        # the bug this fixes).
        self.chat.add_message(AssistantMessage(f"[scheduled] {request_text}"))
        if self._processing:
            self._queue_scheduled(request_text)
        else:
            self._start_agent_turn(request_text)

    def _queue_scheduled(self, request_text):
        if len(self._scheduled_queue) >= SCHEDULED_QUEUE_CAP:
            dropped = self._scheduled_queue.pop(0)
            print(f"[scheduler] queue full — dropped oldest: {dropped[:60]}")
        self._scheduled_queue.append(request_text)

    def _drain_scheduled_queue(self):
        if self._processing or not self._scheduled_queue:
            return
        nxt = self._scheduled_queue.pop(0)
        self._start_agent_turn(nxt)

    def _fire_reminder_direct(self, reminder_text):
        self.chat.add_message(AssistantMessage(f"[REMINDER] {reminder_text}"))

        def work():
            try:
                result = get_agent().execute_reminder(reminder_text)
            except Exception as e:
                result = f"ERROR: {e}"
            # Emit rather than QTimer.singleShot: a timer created on a worker
            # thread has no event loop and never fires. A signal is delivered
            # to the GUI thread by Qt's queued connection.
            self._reminder_result.emit(result, reminder_text)

        threading.Thread(target=work, daemon=True).start()

    def _reminder_done(self, result, reminder_text):
        try:
            from winotify import Notification
            Notification(
                app_id="Agent",
                title="⏰ Reminder",
                msg=reminder_text,
                duration="long",
            ).show()
        except Exception:
            pass


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)  # app lives in the tray
    window = AgentWindow()
    window.show_greeting()
    window.show()
    window.start_services()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
