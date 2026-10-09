"""
AgentBot GUI — PySide6 port.

Phase 4a: slash commands, settings overlay, memory reset — faithful ports of
the same features in agent_gui.py. Phase 4b: per-turn collapsible activity
chips showing the step log. Phase 5a: system tray, F9 hotkey (stub) and
scheduler notifications. Phase 5b: voice (F9 push-to-talk STT, auto-speak TTS)
and a scheduler queue so overlapping turns no longer race. Phase 5c: TTS
warmup at startup, proactive file-watcher callbacks, and file attachments
(paperclip picker, chip strip, contents inlined into the agent turn). Phase
5d: wired the previously-dead sidebar/header buttons (Tasks, Index docs,
Activity log, Copy all, Export chat, New chat), added a visible mic button
(click-to-toggle, in sync with F9), and replaced Unicode glyphs with SVG
icons rendered via QtSvg.

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
import datetime
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
from PySide6.QtGui import (
    QGuiApplication,
    QIcon,
    QKeyEvent,
    QPainter,
    QPixmap,
)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QInputDialog,
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

# Phase 6a — right info panel
# A subtle step up from the chat bg (#0d0f14) and the input pill (#14171f), so
# the cards read as raised panels without a border-heavy look. Reuses the
# existing border colour (no new border token needed).
COLOR_CARD         = "#14171d"
# Card header accent: the mint dimmed toward the background. Base mint #4fe08f
# at ~40% lightness -> #2a6b52; nudged one step to #2f7d5e so it stays legible
# against the card bg at 11px.
COLOR_HEAD_ACCENT  = "#2f7d5e"

FONT_UI = "Segoe UI"
FONT_MONO = "Consolas"

SIDEBAR_W = 226
HEADER_H = 48

# Phase 6a — right panel geometry + auto-hide breakpoints. MIN_W is far enough
# above HIDE_BELOW that the hidden->shown transition has hysteresis (a resize
# just under 1100 then a fragment back above cannot flap the panel); at 1100
# the 720px chat column still gets its full width (1100-226-320 = 554 < 720 it
# clamps, 1380-226-320 = 834 > 720 so the default already clears it).
RIGHT_PANEL_W = 320
HIDE_PANEL_BELOW = 1100
SHOW_PANEL_AT = 1180
CARD_RADIUS = 10
CARD_PAD = 14
CARD_SPACING = 12

CONTENT_MAX_W = 720
CONTENT_PAD = 24
MSG_SPACING = 28
FADE_MS = 150

INPUT_LINE_H = 22     # single-line height for the input field
INPUT_MAX_LINES = 6   # auto-grow ceiling (~150px)

SCHEDULED_QUEUE_CAP = 5   # pending scheduled triggers before oldest is dropped

# ----------------------------------------------------------------------
# SVG icon set (Phase 5d)
# ----------------------------------------------------------------------
# Stroke-based 24x24 line icons drawn as inline SVG strings and rasterised at
# runtime with QtSvg. No new dependency (PySide6 ships QtSvg) and no PIL.
# Each SVG uses stroke="currentColor" so the caller can tint it; `_svg_pixmap`
# substitutes the colour and renders at the requested pixel size.
_ICON_SVGS = {
    # sidebar / nav
    "settings": '<circle cx="12" cy="12" r="3.2"/><path d="M12 2.5v3M12 18.5v3'
                'M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2.5 12h3M18.5 12h3'
                'M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1"/>',
    "tasks": '<path d="M4 6.5l1.6 1.6L8.5 5M4 12.5l1.6 1.6L8.5 11'
             'M4 18.5l1.6 1.6L8.5 17M11 7h9M11 13h9M11 19h9"/>',
    "book": '<path d="M4 5.5A1.5 1.5 0 0 1 5.5 4H11v15H5.5A1.5 1.5 0 0 0 4 20.5z'
            'M20 5.5A1.5 1.5 0 0 0 18.5 4H13v15h5.5A1.5 1.5 0 0 1 20 20.5z"/>',
    "pulse": '<path d="M3 12h4l2.5-6 5 12 2.5-6h4"/>',
    "clipboard": '<rect x="6" y="4.5" width="12" height="15" rx="2"/>'
                 '<path d="M9 4.5a3 3 0 0 1 6 0M9.5 11h5M9.5 14.5h5"/>',
    "export": '<path d="M12 15V4M8.5 7.5L12 4l3.5 3.5'
              'M5 14v4.5A1.5 1.5 0 0 0 6.5 20h11a1.5 1.5 0 0 0 1.5-1.5V14"/>',
    "chat": '<path d="M4 6.5A2.5 2.5 0 0 1 6.5 4h11A2.5 2.5 0 0 1 20 6.5v7'
            'A2.5 2.5 0 0 1 17.5 16H10l-4.5 4v-4A2.5 2.5 0 0 1 4 13.5z"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    # header
    "menu": '<path d="M4 7h16M4 12h16M4 17h16"/>',
    "newchat": '<path d="M20 11.5A8 8 0 1 1 12 3.5"/>'
               '<path d="M20 4l-7.5 7.5"/>',
    "back": '<path d="M15 5l-7 7 7 7"/>',
    # right-panel toggle (Phase 6a): double chevrons pointing toward the panel
    "chevrons_right": '<path d="M8 6.5l6 6-6 6"/><path d="M15 6.5l6 6-6 6"/>',
    "chevrons_left": '<path d="M16 6.5l-6 6 6 6"/><path d="M9 6.5l-6 6 6 6"/>',
    # input row
    "clip": '<path d="M8 12.5l6.5-6.5a3 3 0 0 1 4.2 4.2l-8 8a5 5 0 0 1-7-7'
            'l8-8"/>',
    "send": '<path d="M12 19V6M6.5 11.5L12 6l5.5 5.5"/>',
    "mic": '<rect x="9" y="3" width="6" height="11" rx="3"/>'
           '<path d="M6 11.5a6 6 0 0 0 12 0M12 17.5V21M9 21h6"/>',
}


def _svg_pixmap(name: str, size: int, color: str) -> QPixmap:
    """Rasterise a named line icon at `size` px in `color`."""
    body = _ICON_SVGS[name]
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
        f'fill="none" stroke="{color}" stroke-width="1.9" '
        f'stroke-linecap="round" stroke-linejoin="round">{body}</svg>'
    )
    renderer = QSvgRenderer(svg.encode("utf-8"))
    dpr = QApplication.instance().devicePixelRatio() if QApplication.instance() else 1.0
    pm = QPixmap(int(size * dpr), int(size * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    renderer.render(painter)
    painter.end()
    return pm


def _svg_icon(name: str, size: int, color: str) -> QIcon:
    return QIcon(_svg_pixmap(name, size, color))


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

    def __init__(self, user_input, conversation_history, attached_paths=None):
        super().__init__()
        self._user_input = user_input
        self._history = conversation_history
        self._attached_paths = list(attached_paths or [])

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        log_capture = io.StringIO()
        try:
            agent = get_agent()
            effective_input = self._user_input
            if self._attached_paths:
                effective_input = self._build_attachment_input()
            suffix = (f" [attached: {len(self._attached_paths)} file(s)]"
                      if self._attached_paths else "")
            self._history.append(f"User: {self._user_input}{suffix}")
            with contextlib.redirect_stdout(log_capture):
                step_log = agent.run_agent_turn(effective_input, self._history)
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

    def _build_attachment_input(self):
        """Prefix the user message with file contents, exactly as
        agent_gui.run_turn_background does."""
        try:
            import file_tools
            block = file_tools.build_attachment_block(self._attached_paths)
            question = self._user_input or "(none — analyze the attached files)"
            return f"{block}\n\nUser question: {question}"
        except Exception as e:
            return f"[attachment read failed: {e}]\n\n{self._user_input}"


def _icon_button(icon: str, tooltip: str = "", size: int = 30,
                 icon_size: int = 18) -> QPushButton:
    """Flat icon button drawn from the SVG set (Phase 5d).

    `icon` is a name in _ICON_SVGS. The pixmap is tinted by hand on hover so
    the glyph lightens along with the background (Phase 2 hover rule).
    """
    btn = QPushButton()
    btn.setToolTip(tooltip)
    btn.setFixedSize(size + 2, size)
    btn.setIconSize(QSize(icon_size, icon_size))
    btn.setIcon(_svg_icon(icon, icon_size, COLOR_TEXT_MID))
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setStyleSheet(
        f"""
        QPushButton {{
            background: transparent;
            border: none;
        }}
        QPushButton:hover {{
            background: {COLOR_HOVER};
            border-radius: 8px;
        }}
        QPushButton:pressed {{ background: {COLOR_SELECTED}; }}
        """
    )
    # Swap the tint on hover: QPushButton can't recolour a QIcon via QSS.
    _orig_enter = btn.enterEvent
    _orig_leave = btn.leaveEvent

    def _enter(e):
        btn.setIcon(_svg_icon(icon, icon_size, COLOR_TEXT_HI))
        _orig_enter(e)

    def _leave(e):
        btn.setIcon(_svg_icon(icon, icon_size, COLOR_TEXT_MID))
        _orig_leave(e)

    btn.enterEvent = _enter
    btn.leaveEvent = _leave
    return btn


def _nav_button(icon: str, label: str, active: bool = False) -> QPushButton:
    """Sidebar nav row: SVG icon + text label, left aligned with hover."""
    btn = QPushButton(f"   {label}")
    btn.setFixedHeight(34)
    btn.setIcon(_svg_icon(icon, 16, COLOR_ACCENT if active else COLOR_TEXT_MID))
    btn.setIconSize(QSize(16, 16))
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
            padding-left: 10px;
        }}
        QPushButton:hover {{ background: {COLOR_HOVER}; }}
        """
    )
    if not active:
        _orig_enter = btn.enterEvent
        _orig_leave = btn.leaveEvent

        def _enter(e):
            btn.setIcon(_svg_icon(icon, 16, COLOR_TEXT_HI))
            _orig_enter(e)

        def _leave(e):
            btn.setIcon(_svg_icon(icon, 16, COLOR_TEXT_MID))
            _orig_leave(e)

        btn.enterEvent = _enter
        btn.leaveEvent = _leave
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
        self.raw_text = text
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
        self.raw_text = text
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
        self.chat_log = []  # (kind, text) record for Copy all / Export

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

        # Record for Copy all / Export chat (Phase 5d), mirroring the old GUI's
        # bubble_log. kind is "user" or "agent".
        raw = getattr(widget, "raw_text", None)
        if raw is not None:
            kind = "user" if isinstance(widget, UserMessage) else "agent"
            self.chat_log.append((kind, raw))

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

    def clear(self):
        """Remove every message widget and reset the recorded log."""
        while self._layout.count() > 1:
            item = self._layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self.chat_log.clear()


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

        self.new_chat = QPushButton("   New chat")
        self.new_chat.setFixedHeight(36)
        self.new_chat.setIcon(_svg_icon("plus", 16, "#06281a"))
        self.new_chat.setIconSize(QSize(16, 16))
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
        self.current_chat_btn = _nav_button("chat", "Current chat", active=True)
        nav_layout.addWidget(self.current_chat_btn)
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
        # Named buttons so AgentWindow can wire each to its handler (Phase 5d).
        self.nav_buttons = {}
        for icon, label in (
            ("settings", "Settings"),
            ("tasks", "Tasks"),
            ("book", "Index docs"),
            ("pulse", "Activity log"),
            ("clipboard", "Copy all"),
            ("export", "Export chat"),
        ):
            b = _nav_button(icon, label)
            self.nav_buttons[label] = b
            bottom_layout.addWidget(b)
        self.settings_btn = self.nav_buttons["Settings"]
        layout.addWidget(bottom)


# ----------------------------------------------------------------------
# Right info panel (Phase 6a)
# ----------------------------------------------------------------------
class InfoCard(QFrame):
    """A titled, empty card for the right panel.

    Phase 6a renders structure only: a small uppercase header and a dim
    placeholder body. Phase 6b fills `body` with live widgets. The body is a
    real QWidget with its own layout so 6b can just addWidget into it.
    """

    def __init__(self, title: str, placeholder: str = "\u2014"):
        super().__init__()
        self.title = title
        self.setObjectName("infoCard")
        self.setStyleSheet(
            f"""
            QFrame#infoCard {{
                background: {COLOR_CARD};
                border: 1px solid {COLOR_BORDER};
                border-radius: {CARD_RADIUS}px;
            }}
            QFrame#infoCard QLabel {{ background: transparent; }}
            """
        )
        outer = QVBoxLayout(self)
        outer.setContentsMargins(CARD_PAD, CARD_PAD, CARD_PAD, CARD_PAD)
        outer.setSpacing(8)

        head = QLabel(title)
        head.setStyleSheet(
            f'color: {COLOR_HEAD_ACCENT}; font-family: "{FONT_UI}"; '
            f"font-size: 11px; font-weight: bold; letter-spacing: 0.5px;"
        )
        outer.addWidget(head)

        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(4)
        self.body_layout.addStretch(1)

        self.placeholder = QLabel(placeholder)
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.placeholder.setStyleSheet(
            f'color: {COLOR_TEXT_LOW}; font-family: "{FONT_UI}"; font-size: 13px;'
        )
        self.body_layout.insertWidget(0, self.placeholder)
        outer.addWidget(self.body, 1)


class RightPanel(QFrame):
    """Fixed-width column of stacked info cards, scrollable.

    Wrapped in an internal QScrollArea so Phase 6b can fill the cards without
    any layout restructuring — at five empty cards it never actually scrolls.
    """

    CARD_TITLES = ("ACTIVITY", "NOW PLAYING", "SYSTEM", "WEATHER",
                   "NOTIFICATIONS")

    def __init__(self):
        super().__init__()
        self.setFixedWidth(RIGHT_PANEL_W)
        self.setStyleSheet(f"background: {COLOR_BG};")
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet(
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

        body = QWidget()
        body.setStyleSheet(f"background: {COLOR_BG};")
        self.cards_layout = QVBoxLayout(body)
        # 0 left / 16 right: the chat column already carries its own 24px right
        # padding, so this keeps a total 40px gutter without over-padding.
        self.cards_layout.setContentsMargins(0, 16, 16, 16)
        self.cards_layout.setSpacing(CARD_SPACING)

        self.cards = {}
        for title in self.CARD_TITLES:
            card = InfoCard(title)
            self.cards[title] = card
            self.cards_layout.addWidget(card)
        self.cards_layout.addStretch(1)

        scroll.setWidget(body)
        outer.addWidget(scroll)


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
        self.resize(1380, 720)
        self.setMinimumWidth(900)
        self.setStyleSheet(f"background: {COLOR_BG};")

        self._sidebar_anim = None
        self._worker = None
        self._processing = False
        self._settings_overlay = None
        self._input_outer = None
        self._tray_icon = None
        self._hotkey_installed = False
        self._recording = False
        self._mic_state = "idle"
        self._mic_pulse = None
        self._scheduled_queue = []
        self.attachments = []
        self._log_panel = None
        self._main_col_layout = None
        self._last_step_log = None
        # Phase 6a: right panel state is in-memory only (never persisted), so a
        # fresh launch always opens with the panel visible.
        self._right_panel_visible = True
        self._right_panel_auto = True  # not user-toggled — safe to auto-hide
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
        self._wire_sidebar()
        root.addWidget(self.sidebar)

        root.addWidget(self._build_main_column(), 1)

        # Phase 6a — right info panel. Added after the chat column so the chat
        # owns the flex (stretch 1) and the panel is a fixed 320px at the far
        # right. The border divider lives on the panel's inner wrapper (in
        # _build_right_panel), not on the panel itself, so it survives a
        # hide/show toggle.
        self.right_panel = self._build_right_panel()
        root.addWidget(self.right_panel)

        self.setCentralWidget(central)

    def _build_right_panel(self) -> QWidget:
        self._right_panel_inner = RightPanel()
        wrapper = QWidget()
        wrapper.setFixedWidth(RIGHT_PANEL_W)
        wrap_layout = QVBoxLayout(wrapper)
        wrap_layout.setContentsMargins(0, 0, 0, 0)
        wrap_layout.setSpacing(0)
        border = QFrame()
        border.setFixedWidth(1)
        border.setStyleSheet(f"background: {COLOR_BORDER};")
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addWidget(border)
        row.addWidget(self._right_panel_inner, 1)
        wrap_layout.addLayout(row)
        return wrapper

    def _wire_sidebar(self):
        """Connect the sidebar nav + New chat buttons (Phase 5d).

        Each mirrors its handler in agent_gui.py: Settings -> open_settings,
        Tasks -> schedule dialog, Index docs -> RAG indexing, Activity log ->
        toggle the in-window log, Copy all -> clipboard, Export chat -> save
        dialog, New chat -> clear the conversation.
        """
        nb = self.sidebar.nav_buttons
        nb["Settings"].clicked.connect(self.open_settings)
        nb["Tasks"].clicked.connect(self.open_schedule_dialog)
        nb["Index docs"].clicked.connect(self.on_index_documents)
        nb["Activity log"].clicked.connect(self.toggle_log)
        nb["Copy all"].clicked.connect(self.copy_all)
        nb["Export chat"].clicked.connect(self.export_conversation)
        self.sidebar.new_chat.clicked.connect(self.new_chat)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._settings_overlay is not None:
            self._settings_overlay.setGeometry(self.rect())
        self._sync_input_column_width()
        self._apply_panel_autohide()

    def _apply_panel_autohide(self):
        """Auto-hide the right panel below HIDE_PANEL_BELOW, re-show above
        SHOW_PANEL_AT (hysteresis so a resize can't flap it).

        Only acts while the panel is in auto mode — once the user toggles it by
        hand, their choice wins until they toggle back.
        """
        if not self._right_panel_auto:
            return
        w = self.width()
        if self._right_panel_visible and w < HIDE_PANEL_BELOW:
            self._set_right_panel_visible(False)
        elif not self._right_panel_visible and w >= SHOW_PANEL_AT:
            self._set_right_panel_visible(True)

    def _set_right_panel_visible(self, visible: bool):
        self._right_panel_visible = visible
        self.right_panel.setVisible(visible)
        self.panel_toggle_btn.setIcon(_svg_icon(
            "chevrons_left" if visible else "chevrons_right", 18, COLOR_TEXT_MID))
        self.panel_toggle_btn.setToolTip(
            "Hide info panel" if visible else "Show info panel")

    def toggle_right_panel(self):
        """Header chevron -> show/hide the right info panel (Phase 6a).

        The first manual toggle switches the panel into manual mode; a manual
        toggle that re-shows it at a very narrow width just flips back to auto
        so a subsequent resize can still hide it (otherwise a stray click at
        900px would pin an overlapping panel).
        """
        show = not self._right_panel_visible
        self._set_right_panel_visible(show)
        self._right_panel_auto = not (show and self.width() < HIDE_PANEL_BELOW)

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
        self._main_col_layout = layout
        return col

    def _build_header(self) -> QWidget:
        header = QWidget()
        header.setFixedHeight(HEADER_H)
        header.setStyleSheet(f"background: {COLOR_BG};")

        row = QHBoxLayout(header)
        row.setContentsMargins(10, 0, 10, 0)
        row.setSpacing(4)

        self.sidebar_btn = _icon_button("menu", "Toggle sidebar")
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

        # The old GUI's \u21bb header button is "New chat" (clear_chat), not a
        # refresh — see agent_gui.py:284 -> _new_chat.
        self.header_new_chat = _icon_button("newchat", "New chat", size=28)
        self.header_new_chat.clicked.connect(self.new_chat)
        row.addWidget(self.header_new_chat)
        # Phase 6a — right-panel toggle, sitting between New chat and the gear.
        self.panel_toggle_btn = _icon_button("chevrons_left", "Hide info panel")
        self.panel_toggle_btn.clicked.connect(self.toggle_right_panel)
        row.addWidget(self.panel_toggle_btn)
        gear = _icon_button("settings", "Settings")
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

        # Attachment chips sit above the pill, inside the same column.
        self.attach_strip = QWidget()
        self.attach_strip.setStyleSheet("background: transparent;")
        self.attach_strip_row = QHBoxLayout(self.attach_strip)
        self.attach_strip_row.setContentsMargins(4, 0, 4, 6)
        self.attach_strip_row.setSpacing(6)
        self.attach_strip_row.addStretch(1)
        self.attach_strip.setVisible(False)
        self._input_col_layout.addWidget(self.attach_strip)

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

        self.attach_btn = _icon_button("clip", "Attach files")
        self.attach_btn.clicked.connect(self._pick_files)
        pill_row.addWidget(self.attach_btn, 0, Qt.AlignmentFlag.AlignBottom)

        # Mic button (Phase 5d): click to START recording, click again to STOP
        # and transcribe (click-to-toggle). F9 remains press-and-hold and drives
        # the same state, so the button reflects F9 activity too.
        self.mic_btn = QPushButton()
        self.mic_btn.setToolTip("Record voice message")
        self.mic_btn.setFixedSize(32, 32)
        self.mic_btn.setIconSize(QSize(18, 18))
        self.mic_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mic_btn.clicked.connect(self._toggle_recording)
        pill_row.addWidget(self.mic_btn, 0, Qt.AlignmentFlag.AlignBottom)

        self.send_btn = QPushButton()
        self.send_btn.setToolTip("Send")
        self.send_btn.setFixedSize(32, 32)
        self.send_btn.setIconSize(QSize(16, 16))
        self.send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.send_btn.clicked.connect(self.send)
        pill_row.addWidget(self.send_btn, 0, Qt.AlignmentFlag.AlignBottom)

        self._input_col_layout.addWidget(self.input_pill)
        col.addWidget(holder)
        self._input_outer = holder
        self._set_mic_state("idle")   # paint the initial mic look
        self._refresh_send_btn()
        return outer

    def _refresh_send_btn(self):
        has_text = bool(self._get_input_text()) or bool(self.attachments)
        enabled = has_text and not self._processing
        self.send_btn.setEnabled(enabled)
        self.send_btn.setIcon(
            _svg_icon("send", 16, COLOR_BG if enabled else COLOR_TEXT_LOW))
        self.send_btn.setStyleSheet(
            f"""
            QPushButton {{
                background: {COLOR_ACCENT if enabled else COLOR_RAISED};
                border: none;
                border-radius: 16px;
            }}
            QPushButton:hover {{
                background: {COLOR_ACCENT_HOVER if enabled else COLOR_RAISED};
            }}
            """
        )

    # ------------------------------------------------------------------
    # Mic button — click-to-toggle (Phase 5d)
    # ------------------------------------------------------------------
    def _set_mic_state(self, state: str):
        """Paint the mic button for 'idle' | 'recording' | 'transcribing'.

        F9 (press-and-hold) and the button (click-toggle) both route through
        _start_recording/_stop_recording, and those call this, so the button
        always mirrors the true recording state.
        """
        self._mic_state = state
        if state == "recording":
            self.mic_btn.setIcon(_svg_icon("mic", 18, "#ffffff"))
            self.mic_btn.setStyleSheet(
                f"""
                QPushButton {{
                    background: {COLOR_DANGER}; border: 1px solid {COLOR_DANGER};
                    border-radius: 16px;
                }}
                QPushButton:hover {{ background: {COLOR_DANGER_HOVER}; }}
                """
            )
            self._start_mic_pulse()
        elif state == "transcribing":
            self._stop_mic_pulse()
            self.mic_btn.setIcon(_svg_icon("mic", 18, COLOR_WARN))
            self.mic_btn.setStyleSheet(
                f"""
                QPushButton {{
                    background: transparent; border: 1px solid {COLOR_WARN};
                    border-radius: 16px;
                }}
                """
            )
        else:  # idle
            self._stop_mic_pulse()
            self.mic_btn.setIcon(_svg_icon("mic", 18, COLOR_TEXT_MID))
            self.mic_btn.setStyleSheet(
                f"""
                QPushButton {{
                    background: transparent; border: none; border-radius: 16px;
                }}
                QPushButton:hover {{ background: {COLOR_HOVER}; }}
                """
            )

    def _start_mic_pulse(self):
        """Opacity breathing 0.7 -> 1.0 over ~1.2s, looping, while recording."""
        self._stop_mic_pulse()
        effect = QGraphicsOpacityEffect(self.mic_btn)
        self.mic_btn.setGraphicsEffect(effect)
        anim = QPropertyAnimation(effect, b"opacity", self.mic_btn)
        anim.setDuration(1200)
        anim.setStartValue(1.0)
        anim.setKeyValueAt(0.5, 0.7)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.Type.InOutSine)
        anim.setLoopCount(-1)  # loop forever until recording stops
        anim.start()
        self._mic_pulse = anim

    def _stop_mic_pulse(self):
        anim = getattr(self, "_mic_pulse", None)
        if anim is not None:
            anim.stop()
            self._mic_pulse = None
        self.mic_btn.setGraphicsEffect(None)

    def _toggle_recording(self):
        """Click-to-toggle: start recording, or stop + transcribe if already on."""
        if self._recording:
            self._stop_recording()
        else:
            self._start_recording()

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

    # ------------------------------------------------------------------
    # Attachments — faithful port of agent_gui._pick_files /
    # _render_attachments / _remove_attachment
    # ------------------------------------------------------------------
    def _pick_files(self):
        from PySide6.QtWidgets import QFileDialog
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Attach files", "",
            "All supported (*.txt *.md *.py *.js *.ts *.json *.csv *.log "
            "*.html *.xml *.yaml *.yml *.ini *.cfg *.sh *.bat *.ps1 *.sql "
            "*.pdf *.docx *.xlsx *.png *.jpg *.jpeg *.bmp *.gif *.webp);;"
            "Documents (*.pdf *.docx *.xlsx);;"
            "Text / code (*.txt *.md *.py *.js *.ts *.json *.csv *.log "
            "*.html *.xml *.yaml *.yml);;"
            "Images (*.png *.jpg *.jpeg *.bmp *.gif *.webp);;"
            "All files (*.*)",
        )
        if not paths:
            return
        for p in paths:
            if p not in self.attachments:
                self.attachments.append(p)
        self._render_attachments()

    def _render_attachments(self):
        # Rebuild the chip strip. Unlike the old GUI we do not draw thumbnails
        # (Phase 4a keeps the input area simple); the name + remove button is
        # enough and avoids a PIL dependency in the GUI.
        while self.attach_strip_row.count() > 1:
            item = self.attach_strip_row.takeAt(0)
            w = item.widget()
            if w is not None:
                # Reparent off the strip before deleting: deleteLater() is
                # deferred, and a widget that is still a child of attach_strip
                # keeps showing up (and rendering) until the event loop runs.
                w.setParent(None)
                w.deleteLater()

        if not self.attachments:
            self.attach_strip.setVisible(False)
            self._refresh_send_btn()
            return

        for i, path in enumerate(self.attachments):
            chip = QFrame()
            chip.setObjectName("attachChip")
            chip.setStyleSheet(
                f"""
                QFrame#attachChip {{
                    background: {COLOR_HOVER};
                    border-radius: 14px;
                }}
                """
            )
            ch = QHBoxLayout(chip)
            ch.setContentsMargins(10, 3, 4, 3)
            ch.setSpacing(4)

            name = os.path.basename(path)
            if len(name) > 32:
                name = name[:29] + "…"
            label = QLabel(f"\U0001f4ce {name}")
            label.setToolTip(path)
            label.setStyleSheet(
                f'background: transparent; color: {COLOR_TEXT_MID}; '
                f'font-family: "{FONT_UI}"; font-size: 11px;'
            )
            ch.addWidget(label)

            remove = QPushButton("\u2715")
            remove.setFixedSize(20, 20)
            remove.setCursor(Qt.CursorShape.PointingHandCursor)
            remove.setStyleSheet(
                f"""
                QPushButton {{
                    background: transparent; border: none; border-radius: 10px;
                    color: {COLOR_TEXT_LOW}; font-size: 11px;
                }}
                QPushButton:hover {{ background: {COLOR_SELECTED}; color: {COLOR_TEXT_HI}; }}
                """
            )
            remove.clicked.connect(lambda _=False, idx=i: self._remove_attachment(idx))
            ch.addWidget(remove)

            self.attach_strip_row.insertWidget(self.attach_strip_row.count() - 1, chip)

        self.attach_strip.setVisible(True)
        self._refresh_send_btn()

    def _remove_attachment(self, idx):
        # Only drops from the list — the file on disk is left alone.
        if 0 <= idx < len(self.attachments):
            self.attachments.pop(idx)
        self._render_attachments()

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
        attached = list(self.attachments)
        if not text and not attached:
            return

        # Slash commands bypass attachments (match agent_gui.send).
        if text.startswith("/") and not attached:
            self._clear_input()
            self._handle_slash_command(text)
            return

        self._clear_input()
        self.attachments.clear()
        self._render_attachments()

        # Show the attachment names in the user bubble, as the old GUI does.
        if attached:
            names = ", ".join(os.path.basename(p) for p in attached)
            display = f"\U0001f4ce {names}\n{text}" if text else f"\U0001f4ce {names}"
        else:
            display = text
        self.chat.add_message(UserMessage(display))
        self._start_agent_turn(text, attached)

    def _start_agent_turn(self, text, attached_paths=None):
        """Kick off a background agent turn for `text`."""
        self._processing = True
        self.input_field.setEnabled(False)
        self._refresh_send_btn()
        self.set_status("Reading attachments…" if attached_paths else "Thinking…",
                        busy=True)

        self._worker = AgentWorker(text, self.conversation_history, attached_paths)
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
        self._last_step_log = step_log
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
        back = _icon_button("back", "Close settings")
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
    # Phase 5d — sidebar/header button handlers
    # (faithful ports of agent_gui.py: _new_chat, _copy_all,
    #  export_conversation, on_index_documents, toggle_log,
    #  open_schedule_dialog)
    # ------------------------------------------------------------------
    def new_chat(self):
        """Header \u21bb / sidebar New chat -> clear_chat (agent_gui.py:577)."""
        reply = QMessageBox.question(
            self, "Clear chat", "Clear conversation and start fresh?",
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
        self.chat.clear()
        self.chat.add_message(AssistantMessage("Chat cleared. Fresh start \u2728"))

    def _chat_as_text(self) -> str:
        """Format the recorded chat like agent_gui._copy_all does."""
        lines = []
        for kind, text in self.chat.chat_log:
            if kind == "user":
                lines.append(f"You: {text}")
            elif kind == "agent":
                lines.append(f"Agent: {text}")
            else:
                lines.append(f"— {text}")
        return "\n\n".join(lines)

    def copy_all(self):
        """Sidebar Copy all -> clipboard + confirmation bubble (:2269)."""
        if not self.chat.chat_log:
            self.chat.add_message(AssistantMessage("Nothing to copy"))
            return
        QApplication.clipboard().setText(self._chat_as_text())
        self.chat.add_message(AssistantMessage("\U0001f4cb Copied to clipboard"))

    def export_conversation(self):
        """Sidebar Export chat -> save dialog + export_tools (:2301)."""
        if not self.chat.chat_log:
            QMessageBox.information(self, "Export", "Nothing to export yet.")
            return

        default_name = "conversation_" + datetime.datetime.now().strftime(
            "%Y%m%d_%H%M%S")
        path, _ = QFileDialog.getSaveFileName(
            self, "Export conversation", default_name,
            "PDF document (*.pdf);;Word document (*.docx);;"
            "Markdown (*.md);;Text file (*.txt);;All files (*.*)",
        )
        if not path:
            return

        ext = os.path.splitext(path)[1].lower()
        title = "Agent Conversation"
        try:
            import export_tools
            if ext == ".pdf":
                export_tools.export_pdf(self.chat.chat_log, path, title)
            elif ext == ".docx":
                export_tools.export_docx(self.chat.chat_log, path, title)
            elif ext == ".md":
                export_tools.export_md(self.chat.chat_log, path, title)
            else:
                export_tools.export_txt(self.chat.chat_log, path, title)
            QMessageBox.information(self, "Exported", f"Saved to:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "Export failed", str(e))

    def on_index_documents(self):
        """Sidebar Index docs -> pick folder, index in the background (:2481)."""
        folder = QFileDialog.getExistingDirectory(self, "Pick a folder to index")
        if not folder:
            return
        self._run_background_command(
            f"Index documents: {folder}",
            f"\U0001f4da Indexing {folder} … this can take a minute.",
            lambda: self._index_documents_work(folder),
        )

    def _index_documents_work(self, folder):
        # Stop any ongoing speech so replies don't overlap (matches old GUI).
        try:
            get_voice_output().stop()
        except Exception:
            pass
        from rag_tool import index_documents
        result = index_documents(folder)
        return f"✅ Indexing done:\n{result}"

    def toggle_log(self):
        """Sidebar Activity log -> show/hide a bottom activity-log panel (:2870).

        The old GUI packed/unpacked a tk log box; here we show/hide a Qt panel
        docked under the chat. The panel mirrors the step log of the last turn.
        """
        # Use isHidden() (the widget's own flag) rather than isVisible(): the
        # latter is False whenever any ancestor is hidden, which would make the
        # toggle re-show instead of hide.
        if self._log_panel is not None and not self._log_panel.isHidden():
            self._log_panel.setVisible(False)
            return
        if self._log_panel is None:
            self._log_panel = self._build_log_panel()
            self._main_col_layout.addWidget(self._log_panel)
        self._log_panel.setVisible(True)
        self._refresh_log_panel()

    def _build_log_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("logPanel")
        panel.setFixedHeight(140)
        panel.setStyleSheet(
            f"""
            QFrame#logPanel {{
                background: {COLOR_BG_ALT};
                border-top: 1px solid {COLOR_BORDER};
            }}
            """
        )
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(12, 8, 12, 8)
        head = QLabel("ACTIVITY LOG")
        head.setStyleSheet(
            f'color: {COLOR_TEXT_LOW}; font-family: "{FONT_UI}"; '
            f"font-size: 10px; font-weight: bold;"
        )
        lay.addWidget(head)
        self._log_box = QPlainTextEdit()
        self._log_box.setReadOnly(True)
        self._log_box.setStyleSheet(
            f'background: {COLOR_CODE_BG}; color: {COLOR_TEXT_MID}; '
            f'border: none; font-family: "{FONT_MONO}"; font-size: 11px;'
        )
        lay.addWidget(self._log_box)
        return panel

    def _refresh_log_panel(self):
        if self._log_panel is None:
            return
        lines = getattr(self, "_last_step_log", None) or ["(no activity yet)"]
        self._log_box.setPlainText("\n".join(
            l for l in lines if isinstance(l, str)))
        self._log_box.verticalScrollBar().setValue(
            self._log_box.verticalScrollBar().maximum())

    def open_schedule_dialog(self):
        """Sidebar Tasks -> read-only scheduled tasks view (:2885).

        The old GUI auto-refreshed every 3s; this port rebuilds the listing on
        open and offers the same Add / Remove / Clear actions via dialogs.
        """
        dlg = QDialog(self)
        dlg.setWindowTitle("Scheduled tasks")
        dlg.resize(520, 480)
        dlg.setStyleSheet(f"background: {COLOR_BG};")

        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(16, 16, 16, 16)

        title = QLabel("Scheduled tasks")
        title.setStyleSheet(
            f'color: {COLOR_TEXT_HI}; font-family: "{FONT_UI}"; '
            f"font-size: 16px; font-weight: bold;"
        )
        lay.addWidget(title)

        sub = QLabel("Reminders are one-shot; daily tasks repeat.")
        sub.setStyleSheet(
            f'color: {COLOR_TEXT_MID}; font-family: "{FONT_UI}"; font-size: 12px;'
        )
        lay.addWidget(sub)

        box = QPlainTextEdit()
        box.setReadOnly(True)
        box.setStyleSheet(
            f'background: {COLOR_INPUT_BG}; color: {COLOR_TEXT_HI}; '
            f'border: 1px solid {COLOR_BORDER}; border-radius: 10px; '
            f'font-family: "{FONT_MONO}"; font-size: 12px;'
        )
        lay.addWidget(box, 1)

        def build_listing():
            lines = []
            try:
                reminders = scheduler.load_reminders()
            except Exception:
                reminders = []
            lines.append("── PENDING REMINDERS (one-shot) ──")
            if reminders:
                for i, r in enumerate(reminders):
                    when = datetime.datetime.fromtimestamp(
                        r["fire_at"]).strftime("%Y-%m-%d %H:%M")
                    lines.append(f"  [{i}] {when}  →  {r['text']}")
            else:
                lines.append("  (none)")
            lines.append("")
            try:
                tasks = scheduler.load_tasks()
            except Exception:
                tasks = []
            lines.append("── DAILY TASKS (repeat) ──")
            if tasks:
                for i, t in enumerate(tasks):
                    lines.append(f"  [{i}] {t['time']}  →  {t['request']}")
            else:
                lines.append("  (none)")
            return "\n".join(lines)

        box.setPlainText(build_listing())

        btn_row = QHBoxLayout()
        add_btn = QPushButton("Add task")
        rem_task_btn = QPushButton("Remove task")
        clear_btn = QPushButton("Clear reminders")
        close_btn = QPushButton("Close")
        for b in (add_btn, rem_task_btn, clear_btn, close_btn):
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(
                f"""
                QPushButton {{
                    background: {COLOR_RAISED}; border: 1px solid {COLOR_BORDER};
                    border-radius: 8px; color: {COLOR_TEXT_HI}; padding: 6px 12px;
                    font-family: "{FONT_UI}"; font-size: 12px;
                }}
                QPushButton:hover {{ background: {COLOR_HOVER}; }}
                """
            )
            btn_row.addWidget(b)
        lay.addLayout(btn_row)

        def do_add():
            time_str, ok = QInputDialog.getText(
                dlg, "Add task", "Time (24-hour, e.g. 08:00):")
            if not ok or not time_str.strip():
                return
            time_str = time_str.strip()
            if len(time_str) != 5 or time_str[2] != ":":
                QMessageBox.critical(dlg, "Invalid time", "Use HH:MM format.")
                return
            req, ok = QInputDialog.getText(
                dlg, "Add task", "What should the agent do at that time?")
            if not ok or not req.strip():
                return
            scheduler.add_task(time_str, req.strip())
            box.setPlainText(build_listing())

        def do_remove_task():
            idx_str, ok = QInputDialog.getText(
                dlg, "Remove task", "Daily task number to remove:")
            if not ok or idx_str is None:
                return
            try:
                idx = int(idx_str.strip())
            except ValueError:
                QMessageBox.critical(dlg, "Invalid", "Enter a number.")
                return
            if scheduler.remove_task(idx):
                box.setPlainText(build_listing())
            else:
                QMessageBox.critical(dlg, "Not found", f"No daily task {idx}.")

        def do_clear_reminders():
            reply = QMessageBox.question(
                dlg, "Clear reminders",
                "Remove ALL pending one-shot reminders?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
            try:
                scheduler.save_reminders([])
            except Exception as e:
                QMessageBox.critical(dlg, "Failed", str(e))
                return
            box.setPlainText(build_listing())

        add_btn.clicked.connect(do_add)
        rem_task_btn.clicked.connect(do_remove_task)
        clear_btn.clicked.connect(do_clear_reminders)
        close_btn.clicked.connect(dlg.accept)
        dlg.exec()

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
        """Wire tray, hotkey, scheduler, watchers and TTS warmup. Call after
        the window is shown.

        The scheduler and watcher callbacks fire on their own daemon threads,
        so they route through a Qt signal rather than touching widgets. The
        tray callbacks likewise arrive off the GUI thread.
        """
        if scheduler is not None:
            try:
                scheduler.start_scheduler(self._scheduled_triggered.emit)
            except Exception as e:
                print(f"[scheduler] failed to start: {e}")
        else:
            print("[scheduler] module unavailable; skipped")

        # Proactive watchers reuse the same handler as the scheduler, exactly
        # as the old GUI does (agent_gui.py:459).
        try:
            get_agent().register_watcher_callback(self._scheduled_triggered.emit)
        except Exception as e:
            print(f"[watcher] registration failed: {e}")

        self._setup_global_hotkey()

        # Delay the tray like the old GUI (root.after(800, _start_tray)):
        # pystray needs the Qt event loop to be up and running first.
        QTimer.singleShot(800, self._start_tray)

        # Warm up TTS so the first spoken reply is instant. warmup() starts its
        # own daemon thread and returns immediately, so this never blocks the
        # window; failures are logged, not fatal.
        try:
            get_voice_output().warmup()
            print("[tts] warmup requested")
        except Exception as e:
            print(f"[tts] warmup skipped: {e}")

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
        self._set_mic_state("recording")
        self.set_status("Recording… click the mic or release F9 to send", busy=True)

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
        self._set_mic_state("transcribing")
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
            self._set_mic_state("idle")
            self.set_status(payload[len("error:"):], busy=False)
            return
        if payload == "recording_started":
            return
        if payload.startswith("transcript:"):
            self._handle_transcript(payload[len("transcript:"):])

    def _handle_transcript(self, text):
        # Match the old GUI: type it into the input, then send immediately.
        self._recording = False
        self._set_mic_state("idle")
        if text and text.strip():
            self.input_field.setPlainText(text.strip())
            self.send()
        else:
            self.set_status("No speech detected — try again", busy=False)

    # -- scheduler notifications ----------------------------------------
    def _on_scheduled_trigger(self, request_text):
        """Slot for _scheduled_triggered. Runs on the GUI thread.

        Handles both scheduler fires and proactive watcher events; watchers
        send "FILE WATCHER: <event> -> <path>" strings.
        """
        if request_text.startswith("REMINDER:"):
            reminder_text = request_text[len("REMINDER:"):].strip()
            self._fire_reminder_direct(reminder_text)
            return
        # A scheduled task or watcher event runs a full agent turn. If one is
        # already running, queue it (the old GUI overlapped two racing turns
        # instead — that is the bug this fixes).
        label = "[watcher]" if request_text.startswith("FILE WATCHER:") else "[scheduled]"
        self.chat.add_message(AssistantMessage(f"{label} {request_text}"))
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
