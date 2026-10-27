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
command dispatch) is replicated below as pure Python. scheduler.py is
plain-Python and safe to import; the tray is native QSystemTrayIcon.
"""
from __future__ import annotations
import concurrent.futures
import contextlib
import datetime
import html
import io
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import (
    Qt,
    QEasingCurve,
    QEvent,
    QObject,
    QPropertyAnimation,
    QRect,
    QSize,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QFont,
    QFontDatabase,
    QFontMetrics,
    QGuiApplication,
    QIcon,
    QKeyEvent,
    QPainter,
    QPen,
    QPixmap,
    QTextBlockFormat,
    QTextCursor,
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
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpacerItem,
    QSplitter,
    QSystemTrayIcon,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

try:
    import keyboard
except ImportError:
    keyboard = None

try:
    import scheduler
except Exception:
    scheduler = None

# --------------------------- palette ---------------------------
# Section 1 terminal theme: black surfaces, mint foreground, dim mint
# secondary. Kept as constants because a lot of the existing widgets style
# themselves inline; themes/*.qss handles the structural and color rules.
COLOR_BG         = "#000000"
COLOR_BG_ALT     = "#000000"
COLOR_SIDEBAR    = "#000000"
COLOR_RAISED     = "#0a1a15"
COLOR_CODE_BG    = "#000000"
COLOR_INPUT_BG   = "#000000"
COLOR_BORDER     = "#23483D"
COLOR_HOVER      = "#0e241d"
COLOR_SELECTED   = "#123026"
COLOR_TEXT_HI    = "#70FFD4"
COLOR_TEXT_USER  = "#D5FFF0"
COLOR_TEXT_MID   = "#8AA99F"
COLOR_TEXT_LOW   = "#8AA99F"

COLOR_WARN       = "#ffd166"
COLOR_DANGER     = "#FF8F9C"
COLOR_DANGER_HOVER = "#ff6b7c"

COLOR_ACCENT       = "#70FFD4"
COLOR_ACCENT_HOVER = "#8CFFE0"
COLOR_ACCENT_DIM   = "#23483D"

# Inline labels declare a semantic tone; the theme QSS owns the actual color.
_TONE_BY_COLOR = {
    COLOR_TEXT_HI: "hi",
    COLOR_TEXT_USER: "hi",
    COLOR_ACCENT: "hi",
    COLOR_TEXT_MID: "dim",
    COLOR_TEXT_LOW: "dim",
    COLOR_WARN: "warn",
    COLOR_DANGER: "danger",
}

FONT_UI = "Consolas"
FONT_MONO = "Consolas"

HEADER_H = 48

# Phase 6a — right panel geometry + auto-hide breakpoints. MIN_W is far enough
# above HIDE_BELOW that the hidden->shown transition has hysteresis (a resize
# just under 1100 then a fragment back above cannot flap the panel).
RIGHT_PANEL_W = 320
MIN_RIGHT_PANEL_W = 240
HIDE_PANEL_BELOW = 1100
SHOW_PANEL_AT = 1180
CARD_PAD = 10
CARD_SPACING = 16

# Phase 6b — panel refresh cadence (all timers live on the GUI thread; Phase
# 5a showed a QTimer created off-thread never fires). Sections size to their
# text now; only the Telegram log gets a bounded height.
H_TELEGRAM = 210
TELEGRAM_MAX_ENTRIES = 20
ACTIVITY_MAX_LINES = 6
SYSTEM_REFRESH_MS = 2000
WEATHER_REFRESH_MS = 10 * 60 * 1000
MEDIA_REFRESH_MS = 1000

# Phase 6c — motion timings (ms).
PULSE_READY_MS = 2500     # calm breathing while Ready
PULSE_THINKING_MS = 1200  # faster while Thinking…
MIC_RING_MS = 1200        # outer recording ring expand+fade loop
CARD_FADE_MS = 220        # per-card entrance fade
CARD_STAGGER_MS = 60      # delay between successive cards
ENTRY_FADE_MS = 150       # new activity line (matches Phase 2)
NUMBER_EASE_MS = 400      # SYSTEM value interpolation
PANEL_SLIDE_MS = 200      # right-panel width slide
SPONTANEOUS_INTERVAL_MS = 2 * 60 * 60 * 1000  # proactive thought cadence
OVERLAY_FADE_MS = 150     # settings backdrop dim
OVERLAY_SCALE_MS = 180    # settings panel scale-in
THINKING_DOT_MS = 360     # per-dot step in the 3-dot thinking indicator

FADE_MS = 150

INPUT_LINE_H = 22     # single-line height for the input field
INPUT_MAX_LINES = 6   # auto-grow ceiling (~150px)

SCHEDULED_QUEUE_CAP = 5   # pending scheduled triggers before oldest is dropped

# Continuous listening: a silent room gives the mic back after this long.
CONTINUOUS_IDLE_SECONDS = 60.0

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
    # media transport (Phase 6b follow-up) — solid glyphs, {c} = tint
    "media_prev": '<rect x="6" y="6" width="2.2" height="12" rx="1" '
                  'fill="{c}" stroke="none"/>'
                  '<path d="M19 6.8v10.4L10.5 12z" fill="{c}" stroke="none"/>',
    "media_next": '<rect x="15.8" y="6" width="2.2" height="12" rx="1" '
                  'fill="{c}" stroke="none"/>'
                  '<path d="M5 6.8v10.4L13.5 12z" fill="{c}" stroke="none"/>',
    "media_play": '<path d="M8 5.5v13l10-6.5z" fill="{c}" stroke="none"/>',
    "media_pause": '<rect x="7.5" y="5.5" width="3" height="13" rx="1" '
                   'fill="{c}" stroke="none"/>'
                   '<rect x="13.5" y="5.5" width="3" height="13" rx="1" '
                   'fill="{c}" stroke="none"/>',
    # input row
    "clip": '<path d="M8 12.5l6.5-6.5a3 3 0 0 1 4.2 4.2l-8 8a5 5 0 0 1-7-7'
            'l8-8"/>',
    "send": '<path d="M12 19V6M6.5 11.5L12 6l5.5 5.5"/>',
    "mic": '<rect x="9" y="3" width="6" height="11" rx="3"/>'
           '<path d="M6 11.5a6 6 0 0 0 12 0M12 17.5V21M9 21h6"/>',
}


def _svg_pixmap(name: str, size: int, color: str) -> QPixmap:
    """Rasterise a named icon at `size` px in `color`.

    Line icons inherit the root stroke. Filled glyphs (media transport) use a
    literal `{c}` in their body, substituted for the tint color here so they
    can be solid while still being recoloured on hover.
    """
    body = _ICON_SVGS[name].replace("{c}", color)
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


def terminal_font() -> QFont:
    """Platform-safe monospace font for the terminal theme."""
    available = set(QFontDatabase.families())
    for family in ("Cascadia Mono", "Consolas", "DejaVu Sans Mono"):
        if family in available:
            return QFont(family, 11)
    font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    font.setPointSize(11)
    return font


def zero_layout(layout):
    """Terminal layout: no outer margin, no inter-widget gap."""
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)
    return layout


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
code {{ font-family: "{FONT_MONO}"; font-size: 13px; }}
a {{ color: {COLOR_ACCENT}; }}
"""

AGENT_BANNER = (
    " █████╗  ██████╗ ███████╗███╗   ██╗████████╗\n"
    "██╔══██╗██╔════╝ ██╔════╝████╗  ██║╚══██╔══╝\n"
    "███████║██║  ███╗█████╗  ██╔██╗ ██║   ██║   \n"
    "██╔══██║██║   ██║██╔══╝  ██║╚██╗██║   ██║   \n"
    "██║  ██║╚██████╔╝███████╗██║ ╚████║   ██║   \n"
    "╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝   ╚═╝   "
)


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


def get_psutil():
    """Lazy psutil import (Phase 6b SYSTEM card). Already a project dep."""
    import psutil as _psutil
    return _psutil


def get_weather_news():
    """Lazy weather_news import (Phase 6b WEATHER card). Plain module."""
    import weather_news as _wn
    return _wn


def parse_weather(text: str):
    """Parse weather_news.get_weather()'s human text into (line1, location).

    The source returns a fixed multi-line block:
        Weather for Batu Gugup, Malaysia:
        - Mist, 26°C (feels like 30°C)
        - Humidity: 85%
        ...
    We only want "26°C · Mist" and the place name. Returns (None, None) on
    anything unexpected so the caller can fall back to dim text.
    """
    if not text or not isinstance(text, str) or text.startswith("ERROR"):
        return None, None
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    location = None
    detail = None
    for line in lines:
        if line.startswith("Weather for "):
            location = line[len("Weather for "):].rstrip(":").strip()
        elif line.startswith("- ") and "°C" in line:
            # "- Mist, 26°C (feels like 30°C)" -> ("Mist", "26°C")
            body = line[2:]
            body = body.split(" (feels")[0]
            if "," in body:
                desc, temp = body.rsplit(",", 1)
                detail = f"{temp.strip()} \u00b7 {desc.strip()}"
            else:
                detail = body.strip()
            break
    return detail, location


# ----------------------------------------------------------------------
# Media (Phase 6b follow-up) — Windows media session via winsdk
# ----------------------------------------------------------------------
# winsdk (NOT winrt — a different package) exposes the Windows
# GlobalSystemMediaTransportControls API. Imported lazily so the GUI still
# starts if it is missing; the NOW PLAYING card then shows a placeholder.
_media_manager_module = None
_media_import_error = None


def get_media_manager_cls():
    """Lazy import of MediaManager, or None if winsdk is unavailable."""
    global _media_manager_module, _media_import_error
    if _media_manager_module is not None:
        return _media_manager_module
    if _media_import_error is not None:
        return None
    try:
        from winsdk.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as MediaManager,
        )
        _media_manager_module = MediaManager
        return MediaManager
    except Exception as e:
        _media_import_error = e
        return None


MEDIA_NO_SESSION = "__none__"  # sentinel: nothing is playing


class MediaStatus:
    """GlobalSystemMediaTransportControlsSessionPlaybackStatus values.

    Kept as plain ints so the worker needs no winsdk import to compare.
    """
    CLOSED = 0
    OPENED = 1
    CHANGING = 2
    STOPPED = 3
    PLAYING = 4
    PAUSED = 5


async def _read_thumbnail_bytes(ref):
    from winsdk.windows.storage.streams import DataReader
    stream = await ref.open_read_async()
    size = stream.size
    if not size:
        return None
    reader = DataReader(stream)
    await reader.load_async(size)
    buf = bytearray(size)
    reader.read_bytes(buf)
    return bytes(buf)


def media_poll_once():
    """Blocking media poll for a worker thread.

    Returns a dict describing the current session, or {"state": "none"} when
    nothing is playing / winsdk is unavailable. Never raises into the caller.
    """
    MediaManager = get_media_manager_cls()
    if MediaManager is None:
        return {"state": "unavailable"}
    import asyncio
    try:
        async def _poll():
            mgr = await MediaManager.request_async()
            session = mgr.get_current_session()
            if session is None:
                return {"state": "none"}
            props = await session.try_get_media_properties_async()
            info = session.get_playback_info()
            status = int(info.playback_status)
            art = None
            if props.thumbnail is not None:
                art = await _read_thumbnail_bytes(props.thumbnail)
            return {
                "state": "playing",
                "title": props.title or "",
                "artist": props.artist or "",
                "album": props.album_title or "",
                "status": status,
                "playing": status == MediaStatus.PLAYING,
                "art": art,
            }

        return asyncio.run(_poll())
    except Exception as e:
        return {"state": "error", "error": str(e)}


def media_transport(action: str):
    """Send a transport command ('prev' | 'toggle' | 'next') on a worker thread.

    Returns the winsdk call's boolean, or False on any failure.
    """
    MediaManager = get_media_manager_cls()
    if MediaManager is None:
        return False
    import asyncio
    try:
        async def _do():
            mgr = await MediaManager.request_async()
            session = mgr.get_current_session()
            if session is None:
                return False
            if action == "prev":
                return bool(await session.try_skip_previous_async())
            if action == "next":
                return bool(await session.try_skip_next_async())
            return bool(await session.try_toggle_play_pause_async())

        return asyncio.run(_do())
    except Exception as e:
        print(f"[media] transport {action} failed: {e}")
        return False


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


THEMES_DIR = Path(__file__).resolve().with_name("themes")
THEME_NAMES = ("brutalist", "crt_green")


def read_theme_name() -> str:
    try:
        name = str(read_config_file_raw().get("GUI_THEME", "brutalist")).strip()
    except Exception:
        name = "brutalist"
    return name if name in THEME_NAMES else "brutalist"


def theme_stylesheet(name: str) -> str:
    name = name if name in THEME_NAMES else "brutalist"
    return (THEMES_DIR / f"{name}.qss").read_text(encoding="utf-8")


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
    "Voice:\n\n"
    "F9                 → hold to talk, release to transcribe\n"
    "                     (or say \"Hey Jarvis\" for a hands-free take)\n"
    "/continuous on|off → hands-free loop; 60s silence re-arms wake word\n\n"
    "Interface:\n\n"
    "/settings          → open the settings overlay\n"
    "/tasks             → scheduled tasks\n"
    "/index             → index a folder for RAG\n"
    "/export            → export the conversation\n"
    "/copy              → copy the conversation\n"
    "/activity          → toggle the activity log\n"
    "/theme [name]      → switch theme (brutalist | crt_green)\n"
    "/tg-clear          → clear the Telegram panel\n"
    "/clear             → clear the chat and history\n\n"
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
    "/reindex           → re-index RAG_AUTO_INDEX_FOLDERS\n\n"
    "/reload-skills     → rescan skills/ and the legacy AppData skills folder\n\n"
    "/browse URL | task → run the restricted browser agent (approvals in chat)"
)


# Canonical commands for inline completion. Aliases are included so Tab can
# cycle through every form the dispatcher accepts.
SLASH_COMMANDS = (
    "/help",
    "/settings",
    "/tasks",
    "/theme",
    "/clear",
    "/new",
    "/copy",
    "/export",
    "/activity",
    "/index",
    "/index-docs",
    "/tg-clear",
    "/continuous",
    "/rag",
    "/find",
    "/move",
    "/mkdir",
    "/symbols",
    "/map",
    "/recipe",
    "/scan",
    "/quarantine",
    "/quarantine-list",
    "/gif-stats",
    "/label-gifs",
    "/eval",
    "/ig",
    "/reindex",
    "/import-chat",
    "/browse",
    "/reload-skills",
    "/send",
    "/edit",
    "/del",
    "/dellast",
    "/log",
    "/delete",
    "/dl",
    "/?",
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
    step = Signal(str)           # one raw step_log line, live

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
                step_log = agent.run_agent_turn(
                    effective_input, self._history, on_step=self.step.emit)
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


class MediaPoller(QObject):
    """Polls the Windows media session off the GUI thread (Phase 6b follow-up).

    The winsdk calls are async and must not run on the Qt event loop, so each
    tick runs on a short-lived daemon thread and posts the result back with a
    signal. The QTimer that drives it lives in AgentWindow (main thread).

    One poll is in flight at a time: `busy` guards against overlap when a call
    takes longer than the 1s interval.
    """

    result = Signal(object)

    def __init__(self):
        super().__init__()
        self._busy = False
        self._stopped = False

    def stop(self):
        """Mark the poller dead so in-flight threads stop emitting.

        Called from AgentWindow.quit_app: a worker finishing after the window
        is torn down would otherwise raise 'Signal source has been deleted'.
        """
        self._stopped = True

    def poll(self):
        if self._busy or self._stopped:
            return
        self._busy = True
        threading.Thread(target=self._run, daemon=True).start()

    def _emit(self, data):
        if self._stopped:
            return
        try:
            self.result.emit(data)
        except RuntimeError:
            pass  # source deleted during teardown — nothing to do

    def _run(self):
        try:
            data = media_poll_once()
        except Exception as e:
            data = {"state": "error", "error": str(e)}
        finally:
            self._busy = False
        self._emit(data)

    def transport(self, action: str):
        """Send a transport command off-thread, then trigger a refresh."""
        def work():
            media_transport(action)
            if self._stopped:
                return
            # Give the OS a moment to apply the change, then re-poll so the
            # play/pause icon reflects reality.
            time.sleep(0.15)
            self._emit(media_poll_once())
        threading.Thread(target=work, daemon=True).start()


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


# ----------------------------------------------------------------------
# Panel card content widgets (Phase 6b)
# ----------------------------------------------------------------------
def _dim_label(text: str, size: int = 11, color: str = COLOR_TEXT_LOW,
               mono: bool = False, wrap: bool = False) -> QLabel:
    lbl = QLabel(text)
    lbl.setProperty("tone", _TONE_BY_COLOR.get(color, "dim"))
    lbl.setStyleSheet(
        f'font-family: "{FONT_MONO if mono else FONT_UI}"; font-size: {size}px;'
    )
    if wrap:
        lbl.setWordWrap(True)
    return lbl


class MeterRow(QWidget):
    """One labelled meter rendered as an ASCII bar, like a TUI."""

    CELLS = 10

    def __init__(self, name: str):
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        self._name = _dim_label(name, size=11, color=COLOR_TEXT_MID)
        self._name.setFixedWidth(34)
        row.addWidget(self._name)

        self._bar = _dim_label("", size=11, color=COLOR_TEXT_MID, mono=True)
        row.addWidget(self._bar)

        self._value = _dim_label("", size=11, color=COLOR_TEXT_HI, mono=True)
        row.addWidget(self._value)
        row.addStretch(1)

    def _bar_text(self, frac: float) -> str:
        frac = max(0.0, min(1.0, frac))
        filled = int(round(self.CELLS * frac))
        return "[" + "#" * filled + "-" * (self.CELLS - filled) + "]"

    def set_value(self, text: str, fraction: float):
        self._value.setText(text)
        self._bar.setText(self._bar_text(fraction))

    def set_value_animated(self, fraction: float, formatter, enabled: bool,
                           threshold: float = 0.01):
        """Phase 6c C — ease the bar + value from the current to a new reading.

        `fraction` is the new 0..1 value; `formatter(frac) -> str` renders the
        label text. Jumps under `threshold` snap instantly (no jitter), and
        when `enabled` is False everything snaps to the final state.
        """
        new = max(0.0, min(1.0, fraction))
        old = getattr(self, "_frac", None)
        ticker = getattr(self, "_ticker", None)
        if ticker is not None:
            ticker.stop()
            ticker = None
        if old is None or not enabled or abs(new - old) < threshold:
            self._frac = new
            self._paint_fraction(new, formatter)
            return

        steps = 20
        state = {"i": 0, "from": old}

        def tick():
            state["i"] += 1
            t = min(1.0, state["i"] / steps)
            # ease-out cubic to match the rest of the motion language
            e = 1 - (1 - t) ** 3
            frac = state["from"] + (new - state["from"]) * e
            self._frac = frac
            self._paint_fraction(frac, formatter)
            if t >= 1.0:
                self._ticker.stop()
                self._ticker = None
                self._frac = new
                self._paint_fraction(new, formatter)

        ticker = QTimer(self)
        ticker.setInterval(max(1, NUMBER_EASE_MS // steps))
        ticker.timeout.connect(tick)
        self._ticker = ticker
        ticker.start()

    def _paint_fraction(self, frac: float, formatter):
        self._value.setText(formatter(frac))
        self._bar.setText(self._bar_text(frac))

    def stop_ticker(self):
        ticker = getattr(self, "_ticker", None)
        if ticker is not None:
            ticker.stop()
            self._ticker = None

    def set_value_text(self, text: str):
        """Value text with no bar (e.g. the no-GPU row)."""
        self.stop_ticker()
        self._frac = None
        self._value.setText(text)
        self._bar.setText("")
        self._bar.setVisible(False)

    def set_bar_visible(self, visible: bool):
        self._bar.setVisible(visible)


def _fmt_percent(frac: float) -> str:
    return f"{frac * 100:4.1f}%"


def _fmt_gb(used_gb: float, total_gb: float):
    return lambda frac: f"{used_gb * frac:.1f}/{total_gb:.1f} GB"


def _fmt_gpu(util_pct: float, used_gb: float, total_gb: float):
    return (lambda frac: f"{frac * 100:.0f}%  "
                         f"{used_gb * frac:.1f}/{total_gb * frac:.1f} GB")


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


_SPEAKER_PREFIX = {
    "user": '<span style="color: #ffffff; font-weight: 700;">You:</span>&#160;',
    # No colour: the label inherits the active theme's bright primary.
    "agent": '<span style="font-weight: 700;">Agent:</span>&#160;',
}


def _inject_speaker(html_text: str, speaker: str) -> str:
    """Put the speaker label inside the first rendered block so the message
    text wraps naturally after it instead of starting on its own row."""
    prefix = _SPEAKER_PREFIX.get(speaker)
    if not prefix:
        return html_text
    match = re.search(
        r"<(p|li|h[1-6]|pre)\b[^>]*>", html_text, flags=re.IGNORECASE)
    if match is None:
        return html_text
    return html_text[:match.end()] + prefix + html_text[match.end():]


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
        self.copy_btn.setStyleSheet("font-size: 14px;")
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
        self._code_scroll.setMinimumWidth(0)
        outer.addWidget(self._code_scroll)

        self._copy_timer = QTimer(self)
        self._copy_timer.setSingleShot(True)
        self._copy_timer.timeout.connect(self._reset_copy_icon)

    def _copy(self):
        QGuiApplication.clipboard().setText(self._code)
        self.copy_btn.setText("\u2713")  # check
        self._copy_timer.start(1500)

    def _reset_copy_icon(self):
        self.copy_btn.setText("\u2398")

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
        head_row = QHBoxLayout(self.header)
        head_row.setContentsMargins(9, 4, 9, 4)
        head_row.setSpacing(6)

        self.chevron = QLabel("\u25b8")
        self.chevron.setProperty("tone", "dim")
        self.chevron.setStyleSheet("font-size: 10px;")
        head_row.addWidget(self.chevron)

        label = QLabel(summary)
        label.setProperty("tone", "dim")
        label.setStyleSheet(
            f'font-family: "{FONT_UI}"; font-size: 11px;'
        )
        head_row.addWidget(label)
        head_row.addStretch(1)
        self.header.mousePressEvent = self._on_click

        outer.addWidget(self.header)

        self.body = QPlainTextEdit(detail)
        self.body.setObjectName("chipBody")
        self.body.setReadOnly(True)
        self.body.setFrameShape(QFrame.Shape.NoFrame)
        self.body.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
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


class InputColumnHost(QWidget):
    """Prompt row stretched to the full terminal width."""

    def __init__(self):
        super().__init__()
        self._column = QWidget()
        self._column.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Minimum)
        row = zero_layout(QHBoxLayout(self))
        row.addStretch(1)
        row.addWidget(self._column, 0)
        row.addStretch(1)

    def resizeEvent(self, event):
        self._column.setFixedWidth(self.width())
        super().resizeEvent(event)


class MessageBase(ShrinkableWidget):
    """One chat message, laid out edge to edge."""

    def __init__(self):
        super().__init__()
        self._column = QWidget()
        self._column.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Minimum
        )
        self._col_layout = QVBoxLayout(self._column)
        self._col_layout.setContentsMargins(0, 0, 0, 0)
        self._col_layout.setSpacing(0)

        row = zero_layout(QHBoxLayout(self))
        row.addStretch(1)
        row.addWidget(self._column, 0)
        row.addStretch(1)

    def minimumSizeHint(self):
        # QWidgetItem promotes minimumSizeHint into the layout's size hint.
        # The wrapped-label minimum is computed before the column gets its real
        # width, which would leave a blank tail in the scroll area.
        return QSize(0, self.sizeHint().height())

    def resizeEvent(self, event):
        # The two stretches above starve the column, so drive its width here.
        self._column.setFixedWidth(self.width())
        super().resizeEvent(event)
        # Word-wrapped labels change height with width; without this the parent
        # layout keeps the pre-resize height and the scroll area grows a blank
        # tail below the last message.
        self.updateGeometry()

    def _prose(self, text: str, speaker: str | None = None) -> QLabel:
        lbl = QLabel()
        lbl.setTextFormat(Qt.TextFormat.RichText)
        rendered = markdown_to_html(text.strip())
        if speaker:
            rendered = _inject_speaker(rendered, speaker)
        lbl.setText(rendered)
        lbl.setWordWrap(True)
        lbl.setMinimumWidth(0)
        lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.LinksAccessibleByMouse
        )
        lbl.setOpenExternalLinks(True)
        lbl.setStyleSheet("font-size: 15px;")
        return lbl

    def _render(self, text: str, speaker: str | None = None):
        pending = speaker
        for seg in _split_segments(text):
            if seg[0] == "prose":
                self._col_layout.addWidget(self._prose(seg[1], pending))
                pending = None
            else:
                if pending:
                    self._col_layout.addWidget(self._prose("", pending))
                    pending = None
                self._col_layout.addWidget(CodeBlock(seg[2]))
        if pending:
            self._col_layout.addWidget(self._prose("", pending))


class UserMessage(MessageBase):
    """Terminal input line: You: prefix, plain text, no bubble or inset."""

    def __init__(self, text: str):
        super().__init__()
        self.raw_text = text
        self._render(text, speaker="user")


class AssistantMessage(MessageBase):
    """Full-width prose, Agent: prefix, optional activity chip below."""

    def __init__(self, text: str, step_log=None):
        super().__init__()
        self.raw_text = text
        self._render(text, speaker="agent")

        summary = activity_summary(step_log)
        if summary:
            chip = ActivityChip(summary, "\n".join(
                l for l in step_log if isinstance(l, str)))
            self._col_layout.addWidget(chip)


class BannerMessage(MessageBase):
    """Startup block-letter banner; inherits the theme's primary colour."""

    def __init__(self, art: str):
        super().__init__()
        # A read-only text view (not a QLabel) lets us pin the line height to
        # the glyph cell so the box-drawing blocks tile without seams.
        view = QTextEdit()
        view.setReadOnly(True)
        view.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        view.setFrameShape(QFrame.Shape.NoFrame)
        view.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        view.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        view.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        view.setContentsMargins(0, 0, 0, 0)
        view.viewport().setContentsMargins(0, 0, 0, 0)
        view.document().setDocumentMargin(8)
        banner_font = QFont("Consolas")
        banner_font.setStyleHint(QFont.StyleHint.Monospace)
        banner_font.setFixedPitch(True)
        banner_font.setPixelSize(18)
        view.setFont(banner_font)
        view.setStyleSheet("background: transparent; border: none; padding: 0px;")
        view.setPlainText(art)
        cursor = view.textCursor()
        cursor.select(QTextCursor.SelectionType.Document)
        fmt = QTextBlockFormat()
        fmt.setLineHeight(
            18.0, QTextBlockFormat.LineHeightTypes.FixedHeight.value)
        cursor.mergeBlockFormat(fmt)
        # Merging leaves the cursor at the end, which can scroll the view and
        # clip the first row even with scrollbars off.
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        view.setTextCursor(cursor)
        view.verticalScrollBar().setValue(0)
        view.setFixedSize(
            int(view.document().idealWidth()) + 4,
            int(view.document().size().height()) + 4)
        self._col_layout.addSpacing(4)
        self._col_layout.addWidget(view)
        self._col_layout.addSpacing(6)


class ReadyMessage(MessageBase):
    """Bold theme-primary status line under the banner."""

    def __init__(self, text: str):
        super().__init__()
        label = QLabel(text)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setProperty("tone", "hi")
        label.setStyleSheet("font-size: 15px; font-weight: 700;")
        label.setWordWrap(True)
        label.setMinimumWidth(0)
        label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self._col_layout.addWidget(label)


class SystemMessage(MessageBase):
    """Dim, bracketed shell notice: [SYSTEM] text."""

    def __init__(self, text: str):
        super().__init__()
        label = QLabel()
        label.setProperty("tone", "dim")
        label.setTextFormat(Qt.TextFormat.RichText)
        safe = html.escape(text).replace("\n", "<br>")
        label.setText(f"<b>[SYSTEM]</b>&#160;{safe}")
        label.setWordWrap(True)
        label.setMinimumWidth(0)
        label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        label.setStyleSheet("font-size: 15px;")
        self._col_layout.addWidget(label)


class HelpMessage(MessageBase):
    """Plain-text terminal manual: newlines are content, not Markdown."""

    def __init__(self, text: str):
        super().__init__()
        self.raw_text = text
        label = QLabel(text)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        label.setMinimumWidth(0)
        label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        label.setStyleSheet(
            f'font-family: "{FONT_MONO}"; font-size: 13px;')
        self._col_layout.addWidget(label)


class UntrustedTextMessage(MessageBase):
    """Plain-text block for untrusted external content (Telegram summaries).

    Never uses markdown or RichText: a document summary is data, not markup.
    """

    def __init__(self, text: str, source: str = ""):
        super().__init__()
        self.raw_text = f"{source}\n{text}" if source else text
        self.source_label = None

        if source:
            caption = QLabel(source)
            caption.setProperty("role", "secondary")
            caption.setProperty("tone", "dim")
            caption.setTextFormat(Qt.TextFormat.PlainText)
            caption.setWordWrap(True)
            caption.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            self._col_layout.addWidget(caption)
            self.source_label = caption

        body = QLabel()
        body.setProperty("role", "log")
        body.setTextFormat(Qt.TextFormat.PlainText)
        body.setText(text)
        body.setWordWrap(True)
        body.setMinimumWidth(0)
        body.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        body.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        body.setStyleSheet("font-size: 14px;")
        self._col_layout.addWidget(body)
        self.body_label = body


class BrowserApprovalCard(QFrame):
    """Inline approval for one browser action (never blocks the Qt thread)."""

    resolved = Signal(bool)

    def __init__(self, payload: dict):
        super().__init__()
        self.setObjectName("browserApproval")
        self._payload = payload
        self._future = payload.get("future")
        self._resolved = False
        self._timer = None
        self.setStyleSheet(
            f"QFrame#browserApproval {{ background: #0c0f0e; "
            f"border: 1px solid {COLOR_BORDER}; border-radius: 6px; }}"
        )

        col = QVBoxLayout(self)
        col.setContentsMargins(14, 10, 14, 10)
        col.setSpacing(6)
        self._col = col

        header = QLabel(f"BROWSER APPROVAL \u2014 {payload.get('summary', 'action')}")
        header.setWordWrap(True)
        header.setTextFormat(Qt.TextFormat.PlainText)
        header.setStyleSheet(
            f"color: {COLOR_ACCENT}; font-family: '{FONT_MONO}'; "
            "font-size: 12px; font-weight: bold;"
        )
        col.addWidget(header)

        url = QLabel(str(payload.get("url") or ""))
        url.setWordWrap(True)
        url.setTextFormat(Qt.TextFormat.PlainText)
        url.setStyleSheet(
            f"color: {COLOR_TEXT_LOW}; font-family: '{FONT_MONO}'; font-size: 11px;"
        )
        col.addWidget(url)

        if payload.get("screenshot_png"):
            self._add_marked_screenshot(
                payload["screenshot_png"], payload.get("x"), payload.get("y"))

        self.status_label = QLabel("Waiting for your decision\u2026")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet(
            f"color: {COLOR_TEXT_LOW}; font-size: 11px;")
        col.addWidget(self.status_label)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.allow_btn = QPushButton("Allow")
        self.deny_btn = QPushButton("Deny & stop")
        for button in (self.allow_btn, self.deny_btn):
            button.setFixedHeight(26)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.allow_btn.clicked.connect(
            lambda: self._resolve(True, "Allowed"))
        self.deny_btn.clicked.connect(
            lambda: self._resolve(False, "Denied \u2014 task stopped"))
        row.addWidget(self.allow_btn)
        row.addWidget(self.deny_btn)
        row.addStretch(1)
        col.addLayout(row)

        try:
            timeout_ms = max(1_000, int(float(payload.get("timeout") or 120) * 1000))
        except (TypeError, ValueError):
            timeout_ms = 120_000
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._expire)
        self._timer.start(timeout_ms)

    def _add_marked_screenshot(self, png, x, y):
        pix = QPixmap()
        if not pix.loadFromData(png):
            return
        original_width = pix.width()
        if original_width > 560:
            pix = pix.scaledToWidth(
                560, Qt.TransformationMode.SmoothTransformation)
        scale = pix.width() / max(1, original_width)
        try:
            px = int(float(x) * scale)
            py = int(float(y) * scale)
        except (TypeError, ValueError):
            px = py = 0

        painter = QPainter(pix)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(COLOR_ACCENT), 3))
        painter.drawEllipse(px - 14, py - 14, 28, 28)
        painter.drawLine(px - 22, py, px + 22, py)
        painter.drawLine(px, py - 22, px, py + 22)
        painter.end()

        image = QLabel()
        image.setPixmap(pix)
        image.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self._col.addWidget(image)

    def _expire(self):
        self._resolve(False, "Expired \u2014 denied")

    def _resolve(self, approved: bool, note: str):
        if self._resolved:
            return
        self._resolved = True
        if self._timer is not None:
            self._timer.stop()
        self.allow_btn.setEnabled(False)
        self.deny_btn.setEnabled(False)
        if self._future is not None and not self._future.done():
            with contextlib.suppress(Exception):
                self._future.set_result(bool(approved))
        self.status_label.setText(note)
        self.resolved.emit(bool(approved))


class BrowserApprovalBridge(QObject):
    """Thread-safe bridge: service loop -> Qt main thread -> future."""

    requested = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._lock = threading.Lock()
        self._pending = {}

    def __call__(self, request: dict):
        future = concurrent.futures.Future()
        request_id = str(request.get("id") or "")
        with self._lock:
            self._pending[request_id] = future
        payload = dict(request)
        payload["future"] = future
        self.requested.emit(payload)

        def cleanup(_):
            with self._lock:
                self._pending.pop(request_id, None)

        future.add_done_callback(cleanup)
        return future


# ----------------------------------------------------------------------
# Chat view with scroll-lock
# ----------------------------------------------------------------------
class TerminalInput(QPlainTextEdit):
    """Prompt editor with inline ghost completion.

    Prefix matches paint the missing suffix after the caret, so Right accepts
    by appending it. Subsequence ("fuzzy") matches cannot be appended without
    corrupting the typed query, so they paint the full command as a dim hint
    and Right replaces the prompt with it. Tab cycles either kind.
    """

    GHOST_OPACITY = 0.6
    MAX_FUZZY_MATCHES = 8

    def __init__(self, parent=None):
        super().__init__(parent)
        self._ghost = ""
        self._matches = []
        self._match_index = 0
        self._replace = False
        self.textChanged.connect(lambda: self._refresh_ghost(reset=True))
        self.cursorPositionChanged.connect(
            lambda: self._refresh_ghost(reset=False))
        # selectAll() changes the selection without moving the cursor.
        self.selectionChanged.connect(
            lambda: self._refresh_ghost(reset=False))

    @staticmethod
    def _is_subsequence(needle, haystack):
        chars = iter(haystack)
        return all(ch in chars for ch in needle)

    def _completions(self, text):
        query = text.lower()
        prefix = [
            command for command in SLASH_COMMANDS
            if command.lower().startswith(query) and len(command) > len(text)
        ]
        fuzzy = [
            command for command in SLASH_COMMANDS
            if command not in prefix
            and command.lower() != query
            and self._is_subsequence(query, command.lower())
        ][:self.MAX_FUZZY_MATCHES]
        return prefix + fuzzy

    def _apply_selection(self, text):
        command = self._matches[self._match_index]
        if command.lower().startswith(text.lower()):
            self._ghost = command[len(text):]
            self._replace = False
        else:
            self._ghost = f"  \u2192 {command}"
            self._replace = True
        self.viewport().update()

    def _refresh_ghost(self, reset=False):
        text = self.toPlainText()
        cursor = self.textCursor()
        if reset:
            self._match_index = 0
        if (cursor.hasSelection() or "\n" in text or " " in text
                or not text.startswith("/")
                or cursor.position() != len(text)):
            self._set_completion("", [])
            return
        matches = self._completions(text)
        if not matches:
            self._set_completion("", [])
            return
        if matches != self._matches:
            self._matches = matches
            self._match_index = min(self._match_index, len(matches) - 1)
        self._apply_selection(text)

    def _set_completion(self, ghost, matches):
        changed = ghost != self._ghost
        self._ghost = ghost
        self._matches = matches
        self._replace = False
        if changed:
            self.viewport().update()

    def _cycle_completion(self):
        if not self._ghost or not self._matches:
            return
        self._match_index = (self._match_index + 1) % len(self._matches)
        self._apply_selection(self.toPlainText())

    def _accept_completion(self):
        if not self._ghost:
            return
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.setTextCursor(cursor)
        if self._replace:
            self.setPlainText(self._matches[self._match_index])
            cursor = self.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            self.setTextCursor(cursor)
        else:
            self.insertPlainText(self._ghost)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Right and self._ghost:
            self._accept_completion()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Tab:
            # Tab is completion-only in this prompt; never insert a literal tab.
            if self._ghost:
                self._cycle_completion()
            event.accept()
            return
        super().keyPressEvent(event)

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self._ghost:
            return
        metrics = QFontMetrics(self.font())
        caret = self.cursorRect()
        x = caret.left() + max(
            self.cursorWidth(), metrics.horizontalAdvance(" "))
        baseline = caret.top() + metrics.ascent()
        painter = QPainter(self.viewport())
        painter.setFont(self.font())
        painter.setPen(QColor(COLOR_TEXT_LOW))
        painter.setOpacity(self.GHOST_OPACITY)
        painter.drawText(x, baseline, self._ghost)
        painter.end()

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self._refresh_ghost()

    def focusOutEvent(self, event):
        self._set_completion("", [])
        super().focusOutEvent(event)


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
    MESSAGE_GAP = 12

    def __init__(self):
        super().__init__()
        self.setObjectName("chatScroll")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setContentsMargins(0, 0, 0, 0)
        self.viewport().setContentsMargins(0, 0, 0, 0)
        self.viewport().setObjectName("chatViewport")

        self._body = QWidget()
        self._body.setObjectName("chatContent")
        self._layout = zero_layout(QVBoxLayout(self._body))
        # The trailing stretch absorbs spare viewport height so no message is
        # stretched. It never adds scroll range: when content is short the body
        # equals the viewport, and when content overflows the stretch is 0px.
        self._layout.addStretch(1)
        self._gap_items = {}
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
        # One consistent 12px block gap, never a trailing gap.
        has_messages = self._layout.count() > 1
        if has_messages:
            gap = QSpacerItem(
                0, self.MESSAGE_GAP,
                QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
            self._layout.insertSpacerItem(
                self._layout.count() - 1, gap)
            self._gap_items[id(widget)] = gap
        self._layout.insertWidget(
            self._layout.count() - 1, widget)

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

    def remove_message(self, widget: QWidget):
        """Drop a transient widget (e.g. the [SYSTEM] Thinking… line)."""
        if widget is None:
            return
        gap = self._gap_items.pop(id(widget), None)
        if gap is not None:
            self._layout.removeItem(gap)
        self._layout.removeWidget(widget)
        widget.setParent(None)
        widget.deleteLater()

    def clear(self):
        """Remove every message widget and reset the recorded log."""
        while self._layout.count() > 1:
            item = self._layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._gap_items.clear()
        self.chat_log.clear()


# ----------------------------------------------------------------------
# Right info panel (Phase 6a)
# ----------------------------------------------------------------------
class InfoCard(QFrame):
    """A titled, empty card for the right panel.

    Phase 6a renders structure only: a small uppercase header and a dim
    placeholder body. Phase 6b fills `body` with live widgets. The body is a
    real QWidget with its own layout so 6b can just addWidget into it.
    """

    def __init__(self, title: str, placeholder: str = "\u2014",
                 object_name: str = "infoCard"):
        super().__init__()
        self.title = title
        self.setObjectName(object_name)
        self.setProperty("panel", True)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(CARD_PAD, CARD_PAD, CARD_PAD, CARD_PAD)
        outer.setSpacing(8)

        # Header is a row (not a bare label) so a card can add a trailing
        # status label without rebuilding the layout.
        head_row = QHBoxLayout()
        head_row.setContentsMargins(0, 0, 0, 0)
        head_row.setSpacing(6)
        head = QLabel(title.lower())
        head.setProperty("role", "heading")
        head_row.addWidget(head)
        head_row.addStretch(1)
        self.header_layout = head_row
        outer.addLayout(head_row)

        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(4)
        self.body_layout.addStretch(1)

        self.placeholder = QLabel(placeholder)
        self.placeholder.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.placeholder.setWordWrap(True)
        self.placeholder.setProperty("role", "secondary")
        self.placeholder.setProperty("tone", "dim")
        self.body_layout.insertWidget(0, self.placeholder)
        outer.addWidget(self.body, 1)

    def set_placeholder(self, text: str):
        """Replace the body with a single dim message.

        Used both for the idle state and the "unavailable" states, so callers
        never have to juggle the placeholder widget's visibility themselves.
        """
        if self.placeholder is None:
            self.placeholder = QLabel()
            self.placeholder.setAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            self.placeholder.setWordWrap(True)
            self.placeholder.setProperty("role", "secondary")
            self.placeholder.setProperty("tone", "dim")
            self.body_layout.insertWidget(0, self.placeholder)
        self.placeholder.setText(text)
        self.placeholder.setVisible(True)


class TelegramEntry(QFrame):
    """One Telegram document summary.

    Sender, file name and summary come from Telegram and are untrusted, so
    every label is PlainText and is only ever changed with setText().
    """

    def __init__(self, sender: str, file_name: str, summary: str):
        super().__init__()
        self.setObjectName("telegramEntry")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 2, 0, 2)
        lay.setSpacing(2)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(6)
        self.sender_label = _dim_label(
            sender, size=10, mono=True, color=COLOR_ACCENT)
        self.file_label = _dim_label(
            file_name, size=10, mono=True, color=COLOR_TEXT_LOW)
        for lbl in (self.sender_label, self.file_label):
            lbl.setTextFormat(Qt.TextFormat.PlainText)
            lbl.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse)
        head.addWidget(self.sender_label)
        head.addWidget(self.file_label, 1)
        lay.addLayout(head)

        self.summary_label = _dim_label(
            summary, size=11, mono=True, color=COLOR_TEXT_MID, wrap=True)
        self.summary_label.setTextFormat(Qt.TextFormat.PlainText)
        self.summary_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.summary_label)


class TelegramPanel(InfoCard):
    """Right-panel card listing Telegram document summaries.

    Newest first, capped at TELEGRAM_MAX_ENTRIES, with an internal scroll area
    so long summaries don't stretch the whole card column.
    """

    def __init__(self):
        super().__init__("TELEGRAM", placeholder="no documents yet")
        self.setMaximumHeight(H_TELEGRAM)
        self.entries = []

        self.list_scroll = QScrollArea()
        self.list_scroll.setWidgetResizable(True)
        self.list_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.list_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list_scroll.setVisible(False)

        self.list_host = QWidget()
        self.list_layout = QVBoxLayout(self.list_host)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(8)
        self.list_layout.addStretch(1)
        self.list_scroll.setWidget(self.list_host)
        self.body_layout.insertWidget(0, self.list_scroll, 1)

    def add_summary(self, sender: str, file_name: str, summary: str):
        entry = TelegramEntry(str(sender), str(file_name), str(summary))
        self.entries.insert(0, entry)
        self.list_layout.insertWidget(0, entry)
        while len(self.entries) > TELEGRAM_MAX_ENTRIES:
            old = self.entries.pop()
            self.list_layout.removeWidget(old)
            old.deleteLater()
        self.placeholder.setVisible(False)
        self.list_scroll.setVisible(True)

    def clear(self):
        for entry in self.entries:
            self.list_layout.removeWidget(entry)
            entry.deleteLater()
        self.entries = []
        self.list_scroll.setVisible(False)
        self.placeholder.setText("no documents yet")
        self.placeholder.setVisible(True)


class RightPanel(QFrame):
    """Column of stacked info cards, scrollable and splitter-resizable.

    Wrapped in an internal QScrollArea so the cards can be filled without any
    layout restructuring.
    """

    CARD_TITLES = ("ACTIVITY", "NOW PLAYING", "SYSTEM", "WEATHER",
                   "TELEGRAM")

    def __init__(self):
        super().__init__()
        self.setObjectName("rightPanel")
        self.setMinimumWidth(200)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        # Phase 6c: set by AgentWindow; gates the per-entry fade-in.
        self.animations_enabled = True

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        body = QWidget()
        body.setObjectName("panelContent")
        self.cards_layout = QVBoxLayout(body)
        # The scroll area itself is inset 16px on every side, so the cards
        # float in the void; 16px between cards keeps the stack airy.
        self.cards_layout.setContentsMargins(0, 0, 0, 0)
        self.cards_layout.setSpacing(CARD_SPACING)

        self.cards = {}
        for title in self.CARD_TITLES:
            if title == "TELEGRAM":
                card = TelegramPanel()
            else:
                card = InfoCard(title)
            self.cards[title] = card
            self.cards_layout.addWidget(card)
        self.cards_layout.addStretch(1)

        # ACTIVITY shows at most ACTIVITY_MAX_LINES single-line steps, so cap it
        # rather than letting the scroll area stretch it into a tall log pane.
        self.cards["ACTIVITY"].setMaximumHeight(
            CARD_PAD * 2 + 16 + ACTIVITY_MAX_LINES * 17 + 12)

        scroll.setWidget(body)
        outer.addWidget(scroll)
        self.scroll_area = scroll

        self._build_activity()
        self._build_now_playing()
        self._build_system()
        self._build_weather()

    # -- ACTIVITY -------------------------------------------------------
    def _build_activity(self):
        card = self.cards["ACTIVITY"]
        card.placeholder.setVisible(False)
        self.activity_lines = []
        self.activity_earlier = _dim_label("", size=10, color=COLOR_TEXT_LOW)
        self.activity_earlier.setVisible(False)
        card.body_layout.insertWidget(0, self.activity_earlier)
        # Labels are created once and only re-texted, so no widget churn/flicker.
        self.activity_labels = []
        for _ in range(ACTIVITY_MAX_LINES):
            lbl = _dim_label("", size=11, mono=True, color=COLOR_TEXT_MID)
            lbl.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse)
            lbl.setVisible(False)
            card.body_layout.insertWidget(card.body_layout.count() - 1, lbl)
            self.activity_labels.append(lbl)
        self.set_activity_idle()

    def set_activity_idle(self):
        self.activity_lines = []
        self._render_activity()

    def set_activity(self, step_log):
        """Show the turn's tool steps, most recent ACTIVITY_MAX_LINES."""
        lines = [l for l in (step_log or []) if isinstance(l, str)]
        lines = [l for l in lines if l.strip()]
        self.activity_lines = lines
        self._render_activity()

    def _render_activity(self):
        total = len(self.activity_lines)
        shown = self.activity_lines[-ACTIVITY_MAX_LINES:]
        more = total - len(shown)
        if total == 0:
            self.activity_earlier.setVisible(False)
            for lbl in self.activity_labels:
                lbl.setVisible(False)
            # Idle text lives in the placeholder slot.
            self.cards["ACTIVITY"].placeholder.setText("Idle")
            self.cards["ACTIVITY"].placeholder.setVisible(True)
            return
        self.cards["ACTIVITY"].placeholder.setVisible(False)
        if more > 0:
            self.activity_earlier.setText(f"\u2026{more} earlier")
            self.activity_earlier.setVisible(True)
        else:
            self.activity_earlier.setVisible(False)
        for i, lbl in enumerate(self.activity_labels):
            if i < len(shown):
                newly = not lbl.isVisible() or lbl.text() != self._ellipsize(shown[i])
                lbl.setText(self._ellipsize(shown[i]))
                lbl.setVisible(True)
                if newly:
                    self._fade_in(lbl)
            else:
                lbl.setVisible(False)

    @staticmethod
    def _ellipsize(text: str, limit: int = 46) -> str:
        text = " ".join(text.split())
        return text if len(text) <= limit else text[:limit - 1] + "\u2026"

    def _fade_in(self, widget: QWidget, duration: int = ENTRY_FADE_MS):
        """Phase 6c E — fade a freshly shown line/entry in (matches Phase 2).

        A plain opacity fade, no slide/bounce. No-op when animations are off,
        and it clears the effect on finish so nothing lingers on the widget.
        """
        if not self.animations_enabled:
            return
        effect = QGraphicsOpacityEffect(widget)
        widget.setGraphicsEffect(effect)
        anim = QPropertyAnimation(effect, b"opacity", widget)
        anim.setDuration(duration)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.finished.connect(lambda w=widget: w.setGraphicsEffect(None))
        anim.start()
        widget._fade_anim = anim  # keep a reference alive

    # -- NOW PLAYING ----------------------------------------------------
    def _build_now_playing(self):
        card = self.cards["NOW PLAYING"]
        self.media_available = get_media_manager_cls() is not None
        card.placeholder.setVisible(False)
        self._art_cache = {}  # key -> QPixmap (avoids re-decoding every second)

        # Idle placeholder (shown when nothing plays or the dep is missing).
        self.media_placeholder = _dim_label(
            "" if self.media_available else "Requires winsdk \u2014 ask user",
            size=13, color=COLOR_TEXT_LOW)
        self.media_placeholder.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.media_placeholder.setWordWrap(True)
        card.body_layout.insertWidget(0, self.media_placeholder)

        # Row: album art + (title / artist / album).
        self.media_row = QWidget()
        mrow = QHBoxLayout(self.media_row)
        mrow.setContentsMargins(0, 0, 0, 0)
        mrow.setSpacing(10)

        self.media_art = QLabel()
        self.media_art.setObjectName("mediaArt")
        self.media_art.setProperty("tone", "dim")
        self.media_art.setFixedSize(60, 60)
        self.media_art.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mrow.addWidget(self.media_art)

        text_col = QWidget()
        tcol = QVBoxLayout(text_col)
        tcol.setContentsMargins(0, 2, 0, 2)
        tcol.setSpacing(2)
        self.media_title = _dim_label("", size=12, color=COLOR_TEXT_HI)
        self.media_title.setStyleSheet(
            f'font-family: "{FONT_MONO}"; font-size: 12px; font-weight: bold;')
        self.media_artist = _dim_label("", size=11, color=COLOR_TEXT_MID)
        self.media_album = _dim_label("", size=10, color=COLOR_TEXT_LOW)
        for w in (self.media_title, self.media_artist, self.media_album):
            w.setMinimumWidth(0)
            tcol.addWidget(w)
        tcol.addStretch(1)
        mrow.addWidget(text_col, 1)
        card.body_layout.insertWidget(1, self.media_row)

        # Transport row: previous / play-pause / next.
        self.media_controls = QWidget()
        ct = QHBoxLayout(self.media_controls)
        ct.setContentsMargins(0, 2, 0, 0)
        ct.setSpacing(6)
        ct.addStretch(1)
        self.media_prev_btn = _icon_button("media_prev", "Previous", size=26,
                                           icon_size=16)
        self.media_prev_btn.clicked.connect(lambda: self._media_command("prev"))
        self.media_play_btn = _icon_button("media_play", "Play/pause", size=26,
                                           icon_size=16)
        self.media_play_btn.clicked.connect(
            lambda: self._media_command("toggle"))
        self.media_next_btn = _icon_button("media_next", "Next", size=26,
                                           icon_size=16)
        self.media_next_btn.clicked.connect(lambda: self._media_command("next"))
        for b in (self.media_prev_btn, self.media_play_btn, self.media_next_btn):
            ct.addWidget(b)
        ct.addStretch(1)
        card.body_layout.insertWidget(2, self.media_controls)

        self._media_command_cb = None  # set by AgentWindow (transport hook)
        self._render_media(None)

    def set_media_command_callback(self, cb):
        """AgentWindow injects the off-thread transport sender."""
        self._media_command_cb = cb

    def _media_command(self, action: str):
        if self._media_command_cb is not None:
            self._media_command_cb(action)

    def update_media(self, data):
        """Slot for MediaPoller.result — repaint the card from a poll dict."""
        self._render_media(data)

    def _render_media(self, data):
        available = self.media_available
        state = (data or {}).get("state") if data else "none"
        if not available or state in (None, "none", "unavailable", "error"):
            if not available:
                self.media_placeholder.setText(
                    "Requires winsdk \u2014 ask user")
            else:
                self.media_placeholder.setText("Nothing playing")
            self.media_placeholder.setVisible(True)
            self.media_row.setVisible(False)
            self.media_controls.setVisible(False)
            return

        self.media_placeholder.setVisible(False)
        self.media_row.setVisible(True)
        self.media_controls.setVisible(True)

        title = data.get("title") or "(unknown title)"
        artist = data.get("artist") or ""
        album = data.get("album") or ""
        self.media_title.setText(self._ellipsize(title, 26))
        self.media_title.setToolTip(title)
        self.media_artist.setText(self._ellipsize(artist, 28) if artist else "")
        self.media_album.setText(self._ellipsize(album, 30) if album else "")

        # Play/pause glyph reflects real playback state.
        playing = bool(data.get("playing"))
        self.media_play_btn.setIcon(_svg_icon(
            "media_pause" if playing else "media_play", 16, COLOR_TEXT_MID))
        self.media_play_btn.setToolTip("Pause" if playing else "Play")

        self._set_media_art(data.get("art"), title, artist)

    def _set_media_art(self, art_bytes, title, artist):
        """Show album art, caching the decoded pixmap by (title, artist)."""
        key = f"{title}\x00{artist}"
        if art_bytes:
            if key not in self._art_cache:
                pm = QPixmap()
                if pm.loadFromData(art_bytes):
                    self._art_cache[key] = pm.scaled(
                        60, 60, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                        Qt.TransformationMode.SmoothTransformation)
                else:
                    self._art_cache[key] = None
            # Keep the cache small — only the current track matters.
            if len(self._art_cache) > 8:
                self._art_cache = {key: self._art_cache.get(key)}
            pm = self._art_cache.get(key)
            if pm is not None:
                self.media_art.setPixmap(pm)
                self.media_art.setText("")
                return
        self.media_art.setPixmap(QPixmap())
        self.media_art.setText("\u266b")  # music note fallback
        self.media_art.setStyleSheet("font-size: 22px;")

    # -- SYSTEM ---------------------------------------------------------
    def _build_system(self):
        card = self.cards["SYSTEM"]
        card.placeholder.setVisible(False)
        self.cpu_row = MeterRow("CPU")
        self.ram_row = MeterRow("RAM")
        self.gpu_row = MeterRow("GPU")
        for row in (self.cpu_row, self.ram_row, self.gpu_row):
            card.body_layout.insertWidget(card.body_layout.count() - 1, row)
        self.cpu_row.set_value("\u2014", 0.0)
        self.ram_row.set_value("\u2014", 0.0)
        self.gpu_row.set_value_text("no GPU detected")

    def update_system(self, cpu_pct, ram_used, ram_total, gpu=None):
        anim = self.animations_enabled
        self.cpu_row.set_value_animated(
            cpu_pct / 100.0, _fmt_percent, anim)
        used_gb = ram_used / (1024 ** 3)
        total_gb = ram_total / (1024 ** 3)
        frac = (ram_used / ram_total) if ram_total else 0.0
        self.ram_row.set_value_animated(
            frac, _fmt_gb(used_gb, total_gb), anim)
        if gpu is None:
            self.gpu_row.set_value_text("no GPU")
        else:
            util, mem_used, mem_total = gpu
            self.gpu_row.set_bar_visible(True)
            self.gpu_row.set_value_animated(
                util / 100.0,
                _fmt_gpu(util, mem_used / 1024, mem_total / 1024),
                anim)

    # -- WEATHER --------------------------------------------------------
    def _build_weather(self):
        card = self.cards["WEATHER"]
        card.placeholder.setVisible(False)
        self.weather_main = _dim_label("\u2014", size=14, color=COLOR_TEXT_HI)
        self.weather_loc = _dim_label("", size=11, color=COLOR_TEXT_LOW)
        card.body_layout.insertWidget(card.body_layout.count() - 1, self.weather_main)
        card.body_layout.insertWidget(card.body_layout.count() - 1, self.weather_loc)
        self._weather_last = None  # last successful (main, loc)

    def update_weather(self, main_text, location_text):
        if main_text:
            self._weather_last = (main_text, location_text or "")
            self.weather_main.setText(main_text)
            self.weather_loc.setText(location_text or "")
        elif self._weather_last:
            # Keep the last good reading on a failed refresh.
            self.weather_main.setText(self._weather_last[0])
            self.weather_loc.setText(self._weather_last[1] + "  (stale)")
        else:
            self.weather_main.setText("unavailable")
            self.weather_loc.setText("")

    # -- TELEGRAM -------------------------------------------------------
    def add_telegram_summary(self, sender: str, file_name: str, summary: str):
        card = self.cards["TELEGRAM"]
        card.add_summary(sender, file_name, summary)
        # The card is last in the column; bring it into view when a document
        # arrives instead of leaving it below the fold.
        self.scroll_area.ensureWidgetVisible(card)

    def clear_telegram(self):
        self.cards["TELEGRAM"].clear()



# ----------------------------------------------------------------------
# Main window
# ----------------------------------------------------------------------
class AgentWindow(QMainWindow):
    _command_done = Signal(str)      # background slash-command result
    _scheduled_triggered = Signal(str)  # scheduler fired (from its own thread)
    _reminder_result = Signal(str, str)  # (result, reminder_text)
    _hotkey_changed = Signal(bool)   # F9 pressed/released (from keyboard thread)
    _voice_event = Signal(str)       # recording/transcription status from worker
    _weather_ready = Signal(str)     # parsed weather text (from worker thread)
    _gui_visibility_request = Signal(bool, object)  # (show, completion event)
    _wake_word_detected = Signal(float)   # wake word score (from mic thread)
    _wake_word_error = Signal(str)        # listener startup/runtime error
    _tts_error = Signal(str)              # speech engine error (TTS thread)
    _spontaneous_message = Signal(str)    # proactive observation (worker thread)
    _spontaneous_finished = Signal()      # worker done; clear the busy flag

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Agent")
        self.resize(1380, 720)
        self.setMinimumWidth(900)
        self.setContentsMargins(0, 0, 0, 0)

        self._worker = None
        self._processing = False
        self._settings_overlay = None
        self._tray_icon = None
        self._tray_menu = None
        self._hotkey_installed = False
        self._recording = False
        self._scheduled_queue = []
        self.attachments = []
        self._log_panel = None
        self._main_col_layout = None
        self._last_step_log = None
        self._live_steps = []
        self._thinking_message = None
        # Phase 6a: right panel state is in-memory only (never persisted), so a
        # fresh launch always opens with the panel visible.
        self._right_panel_visible = True
        self._right_panel_auto = True  # not user-toggled — safe to auto-hide
        self._right_panel_last_width = RIGHT_PANEL_W
        # Phase 6c: read the motion flag once and cache it. Toggling it in
        # settings applies on next launch (no mid-session hot-reload).
        self._animations_enabled = self._read_animations_flag()
        self._theme = read_theme_name()
        # Phase 6c animation state (all anims live on the main thread).
        self._minimized = False
        self._card_anims = []
        self._entry_anims = []
        self._panel_anim = None
        # Section 2 — Telegram document listener (started in start_services).
        self._telegram_service = None
        self._telegram_results = None
        self._telegram_timer = None
        # Section 4 — browser approval bridge (created after the chat view).
        self._browser_bridge = None
        # Option 2 — OS control (hide/show bridge and kill switch).
        self._os_control = None
        self._gui_was_visible = True
        self._gui_was_minimized = False
        # Option 3 — wake-word listener (started in start_services).
        self._wake_word_service = None
        self._wake_word_error_shown = False
        self._wake_triggered = False
        self._wake_record_ms = 3500
        self._wake_ack_started = 0.0
        # Continuous listening (opt-in config, toggled by /continuous).
        self._continuous_enabled = self._read_continuous_flag()
        self._continuous_paused = False
        self._continuous_listening = False
        self._continuous_timer = None
        self._tts_error_shown = False
        # Option A — proactive thoughts (opt-in QTimer, started on show).
        self._spontaneous_timer = None
        self._spontaneous_busy = False
        # Phase 6b panel timers (all created here, on the main thread) + state.
        self._system_timer = None
        self._weather_timer = None
        self._media_timer = None
        self._media_poller = None
        self._gpu_unavailable = True
        self._command_done.connect(self._on_command_done)
        self._scheduled_triggered.connect(self._on_scheduled_trigger)
        self._reminder_result.connect(self._reminder_done)
        self._hotkey_changed.connect(self._on_hotkey_changed)
        self._voice_event.connect(self._on_voice_event)
        self._weather_ready.connect(self._on_weather_ready)
        self._gui_visibility_request.connect(self._apply_gui_visibility)
        self._wake_word_detected.connect(self._on_wake_word)
        self._wake_word_error.connect(self._on_wake_word_error)
        self._tts_error.connect(self._on_tts_error)
        self._spontaneous_message.connect(self._on_spontaneous_message)
        self._spontaneous_finished.connect(self._on_spontaneous_finished)

        # Conversation history shared with agent.run_agent_turn, loaded once.
        self.conversation_history = []
        try:
            self.conversation_history = get_agent().load_conversation_history()
        except Exception:
            self.conversation_history = []

        central = QWidget()
        central.setObjectName("central")
        self.setAcceptDrops(True)
        self._drop_highlighted = False
        self._central_widget = central
        # Drag highlight lives on a transparent overlay: the stacked children
        # would paint over a border drawn by the central widget itself.
        self._drop_overlay = QFrame(central)
        self._drop_overlay.setObjectName("dropOverlay")
        self._drop_overlay.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._drop_overlay.setStyleSheet(
            "QFrame#dropOverlay { background: transparent; "
            "border: 2px solid transparent; }")
        self._drop_overlay.setGeometry(central.rect())
        self._drop_overlay.hide()
        root = zero_layout(QHBoxLayout(central))

        # Chat and the info panel share a splitter; the chat keeps the stretch.
        self.right_panel = self._build_right_panel()
        self._main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._main_splitter.setObjectName("mainSplitter")
        self._main_splitter.setHandleWidth(6)
        self._main_splitter.setChildrenCollapsible(False)
        self._main_splitter.setStyleSheet(
            "QSplitter::handle:horizontal { background: transparent; }"
            f"QSplitter::handle:horizontal:hover "
            f"{{ background: {COLOR_ACCENT_DIM}; }}"
        )
        self._main_splitter.addWidget(self._build_main_column())
        self._main_splitter.addWidget(self.right_panel)
        self._main_splitter.setStretchFactor(0, 1)
        self._main_splitter.setStretchFactor(1, 0)
        self._main_splitter.setSizes([900, RIGHT_PANEL_W])
        self._main_splitter.splitterMoved.connect(self._on_splitter_moved)
        root.addWidget(self._main_splitter, 1)

        self.setCentralWidget(central)

        self._setup_browser_bridge()
        self._setup_os_control()

    def _build_right_panel(self) -> QWidget:
        self._right_panel_inner = RightPanel()
        self._right_panel_inner.animations_enabled = self._animations_enabled
        wrapper = QWidget()
        wrapper.setMinimumWidth(MIN_RIGHT_PANEL_W)
        wrapper.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        wrap_layout = zero_layout(QVBoxLayout(wrapper))
        wrap_layout.addWidget(self._right_panel_inner)
        return wrapper

    @property
    def panel(self) -> "RightPanel":
        """The cards' controller (the RightPanel inside the fixed wrapper)."""
        return self._right_panel_inner

    # ------------------------------------------------------------------
    # Phase 6b — panel data wiring (SYSTEM / WEATHER / NOW PLAYING)
    # ------------------------------------------------------------------
    def start_panel_timers(self):
        """Start the panel polling timers. Called once the window is shown.

        All timers are created here (main thread) — Phase 5a established that a
        QTimer made on a worker thread never fires.
        """
        try:
            psutil = get_psutil()
            psutil.cpu_percent(interval=None)  # prime: first call returns 0.0
            self._gpu_unavailable = not self._nvidia_available()
        except Exception as e:
            print(f"[system] psutil unavailable: {e}")
            psutil = None
        self._psutil = psutil

        self._system_timer = QTimer(self)
        self._system_timer.setInterval(SYSTEM_REFRESH_MS)
        self._system_timer.timeout.connect(self._refresh_system)
        self._system_timer.start()
        self._refresh_system()  # paint immediately, don't wait 2s

        self._weather_timer = QTimer(self)
        self._weather_timer.setInterval(WEATHER_REFRESH_MS)
        self._weather_timer.timeout.connect(self._refresh_weather)
        self._weather_timer.start()
        QTimer.singleShot(200, self._refresh_weather)  # first fetch soon after show

        # Phase 6b follow-up — NOW PLAYING media poll (1s). The poll runs on a
        # worker thread (winsdk calls are async); the timer just kicks it.
        self.panel.set_media_command_callback(self._media_command)
        self._media_poller = MediaPoller()
        self._media_poller.result.connect(self._on_media_result)
        self._media_timer = QTimer(self)
        self._media_timer.setInterval(MEDIA_REFRESH_MS)
        self._media_timer.timeout.connect(self._refresh_media)
        self._media_timer.start()
        self._refresh_media()

    def _refresh_media(self):
        # Skip polling (and its thread) while the panel is hidden or minimized.
        if not self._right_panel_visible or self._minimized:
            return
        if getattr(self, "_media_poller", None) is not None:
            self._media_poller.poll()

    def _on_media_result(self, data):
        self.panel.update_media(data)

    def _media_command(self, action: str):
        """Transport button -> off-thread winsdk command (from the card)."""
        if getattr(self, "_media_poller", None) is not None:
            self._media_poller.transport(action)

    # ------------------------------------------------------------------
    # Phase 6c — motion startup
    # ------------------------------------------------------------------
    def start_motion(self):
        """Begin the ambient motion (card entrance).

        Called once after the window is shown. When ANIMATIONS_ENABLED is
        false this is a no-op: every element already renders in its final
        state.
        """
        # Card entrance: only when the panel is actually visible at launch.
        if self._right_panel_visible:
            QTimer.singleShot(0, self._animate_card_entrance)

    # ------------------------------------------------------------------
    # Phase 6c D — staggered card entrance
    # ------------------------------------------------------------------
    def _animate_card_entrance(self):
        """Fade each card in with a 60ms stagger (220ms ease-out each).

        Only runs on first show and on an explicit toggle-back-on — never on
        focus/resize. No-op when animations are disabled.
        """
        if not self._animations_enabled or not self._right_panel_visible:
            return
        self._stop_card_anims()
        for i, title in enumerate(RightPanel.CARD_TITLES):
            card = self.panel.cards.get(title)
            if card is None:
                continue
            effect = QGraphicsOpacityEffect(card)
            card.setGraphicsEffect(effect)
            anim = QPropertyAnimation(effect, b"opacity", card)
            anim.setDuration(CARD_FADE_MS)
            anim.setStartValue(0.0)
            anim.setEndValue(1.0)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            anim.finished.connect(lambda c=card: c.setGraphicsEffect(None))
            QTimer.singleShot(i * CARD_STAGGER_MS, anim.start)
            self._card_anims.append(anim)

    def _stop_card_anims(self):
        for anim in list(self._card_anims):
            anim.stop()
        self._card_anims = []
        for card in self.panel.cards.values():
            card.setGraphicsEffect(None)

    @staticmethod
    def _nvidia_available() -> bool:
        import shutil
        return shutil.which("nvidia-smi") is not None

    def _refresh_system(self):
        psutil = getattr(self, "_psutil", None)
        if psutil is None:
            return
        # Performance guard: no point sampling (or animating) a hidden panel.
        # cpu_percent keeps its own baseline, so skipping calls is harmless.
        if not self._right_panel_visible or self._minimized:
            return
        try:
            cpu = psutil.cpu_percent(interval=None)
            vm = psutil.virtual_memory()
            gpu = None
            if not self._gpu_unavailable:
                gpu = self._read_gpu()
            self.panel.update_system(cpu, vm.used, vm.total, gpu)
        except Exception as e:
            print(f"[system] refresh failed: {e}")

    @staticmethod
    def _read_gpu():
        """Query nvidia-smi for (util%, mem_used_MB, mem_total_MB), or None."""
        import subprocess
        try:
            out = subprocess.run(
                ["nvidia-smi",
                 "--query-gpu=utilization.gpu,memory.used,memory.total",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=3,
            )
            if out.returncode != 0 or not out.stdout.strip():
                return None
            util_s, used_s, total_s = [
                p.strip() for p in out.stdout.strip().splitlines()[0].split(",")
            ]
            return float(util_s), float(used_s), float(total_s)
        except Exception:
            return None

    def _refresh_weather(self):
        """Fetch weather off the GUI thread; result posted via _weather_ready."""
        def work():
            try:
                text = get_weather_news().get_weather("Shah Alam")
            except Exception as e:
                text = f"ERROR: {e}"
            self._weather_ready.emit(text)

        threading.Thread(target=work, daemon=True).start()

    def _on_weather_ready(self, raw_text):
        detail, location = parse_weather(raw_text)
        self.panel.update_weather(detail, location)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._settings_overlay is not None:
            self._settings_overlay.setGeometry(self.rect())
        if getattr(self, "_central_widget", None) is not None:
            self._drop_overlay.setGeometry(self._central_widget.rect())
            if self._drop_overlay.isVisible():
                self._drop_overlay.raise_()
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
        # Autohide is a reactive resize response — do it instantly (animate=
        # False) so the panel cannot lag behind a fast drag.
        if self._right_panel_visible and w < HIDE_PANEL_BELOW:
            self._set_right_panel_visible(False, animate=False)
        elif not self._right_panel_visible and w >= SHOW_PANEL_AT:
            self._set_right_panel_visible(True, animate=False)

    def _on_splitter_moved(self, _pos, _index):
        if self._right_panel_visible:
            width = self.right_panel.width()
            if width >= MIN_RIGHT_PANEL_W:
                self._right_panel_last_width = width

    def _restore_panel_width(self):
        splitter = getattr(self, "_main_splitter", None)
        if splitter is None or splitter.width() <= 0:
            return
        total = splitter.width()
        target = max(MIN_RIGHT_PANEL_W, self._right_panel_last_width)
        target = min(target, max(MIN_RIGHT_PANEL_W, total - 200))
        splitter.setSizes([max(1, total - target), target])

    def _set_right_panel_visible(self, visible: bool, animate: bool = True):
        was_visible = self._right_panel_visible
        if not visible and self.right_panel.width() >= MIN_RIGHT_PANEL_W:
            self._right_panel_last_width = self.right_panel.width()

        self._right_panel_visible = visible

        # Slide the splitter pane between its remembered width and zero.
        # Minimum width is relaxed during the animation and restored after.
        self._stop_panel_anim()
        if animate and self._animations_enabled:
            self.right_panel.setMinimumWidth(0)
            self.right_panel.setMaximumWidth(16777215)
            self.right_panel.setVisible(True)
            anim = QPropertyAnimation(self.right_panel, b"maximumWidth",
                                      self.right_panel)
            anim.setDuration(PANEL_SLIDE_MS)
            anim.setStartValue(self.right_panel.width() if was_visible else 0)
            anim.setEndValue(
                max(MIN_RIGHT_PANEL_W, self._right_panel_last_width)
                if visible else 0)
            anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
            if visible:
                anim.finished.connect(self._after_panel_shown)
            else:
                anim.finished.connect(self._after_panel_hidden)
            anim.start()
            self._panel_anim = anim
        elif visible:
            self.right_panel.setMinimumWidth(MIN_RIGHT_PANEL_W)
            self.right_panel.setMaximumWidth(16777215)
            self.right_panel.setVisible(True)
            QTimer.singleShot(0, self._restore_panel_width)
            QTimer.singleShot(0, self._animate_card_entrance)
        else:
            self.right_panel.setMinimumWidth(0)
            self.right_panel.setMaximumWidth(0)
            self.right_panel.setVisible(False)

        # Guardrails: stop panel motion while hidden; entrance on re-show.
        if not visible:
            self._stop_card_anims()
            for row in (self.panel.cpu_row, self.panel.ram_row,
                        self.panel.gpu_row):
                row.stop_ticker()

    def _stop_panel_anim(self):
        anim = getattr(self, "_panel_anim", None)
        if anim is not None:
            anim.stop()
            self._panel_anim = None

    def _after_panel_shown(self):
        # Release the width caps and re-apply the remembered splitter size.
        self._panel_anim = None
        self.right_panel.setMaximumWidth(16777215)
        self.right_panel.setMinimumWidth(MIN_RIGHT_PANEL_W)
        self._restore_panel_width()
        self._animate_card_entrance()

    def _after_panel_hidden(self):
        self._panel_anim = None
        self.right_panel.setMinimumWidth(0)
        self.right_panel.setMaximumWidth(0)
        self.right_panel.setVisible(False)

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

    # ------------------------------------------------------------------
    # Drag & drop file input
    # ------------------------------------------------------------------
    @staticmethod
    def _dropped_paths(mime):
        if mime is None or not mime.hasUrls():
            return []
        paths = []
        for url in mime.urls():
            path = url.toLocalFile()
            if path:
                paths.append(os.path.normpath(path))
        return paths

    def dragEnterEvent(self, event):
        if self._dropped_paths(event.mimeData()):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
            self._set_drop_highlight(True)
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if self._dropped_paths(event.mimeData()):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self._set_drop_highlight(False)
        event.accept()

    def dropEvent(self, event):
        paths = self._dropped_paths(event.mimeData())
        if not paths:
            event.ignore()
            return
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()
        self._set_drop_highlight(False)
        self._insert_dropped_paths(paths)

    def _set_drop_highlight(self, active):
        if self._drop_highlighted == active:
            return
        self._drop_highlighted = active
        color = COLOR_ACCENT if active else "transparent"
        self._drop_overlay.setStyleSheet(
            f"QFrame#dropOverlay {{ background: transparent; "
            f"border: 2px solid {color}; }}")
        self._drop_overlay.setGeometry(self._central_widget.rect())
        self._drop_overlay.setVisible(active)
        if active:
            self._drop_overlay.raise_()

    def _insert_dropped_paths(self, paths):
        text = ", ".join(paths)
        existing = self.input_field.toPlainText().strip()
        if existing:
            text = f"{existing}, {text}"
        self.input_field.setPlainText(text)
        cursor = self.input_field.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.input_field.setTextCursor(cursor)
        self.input_field.ensureCursorVisible()
        self.input_field.setFocus()

    def showEvent(self, event):
        super().showEvent(event)
        # A terminal always has the prompt focused and the cursor visible.
        QTimer.singleShot(0, self.input_field.setFocus)

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
        layout = zero_layout(QVBoxLayout(col))
        # Exactly two terminal columns of breathing room off the left wall.
        layout.setContentsMargins(
            QFontMetrics(self.font()).horizontalAdvance("  "), 0, 0, 0)

        self.chat = ChatView()
        layout.addWidget(self.chat, 1)
        layout.addWidget(self._build_input_area())
        self._main_col_layout = layout
        return col

    def _build_input_area(self) -> QWidget:
        outer = QWidget()
        col = zero_layout(QVBoxLayout(outer))

        # Prompt fills the terminal width edge to edge.
        holder = InputColumnHost()
        self._input_column = holder._column
        self._input_col_layout = zero_layout(QVBoxLayout(self._input_column))

        # Attachment chips sit above the prompt, inside the same column.
        self.attach_strip = QWidget()
        self.attach_strip.setStyleSheet("background: transparent;")
        self.attach_strip_row = zero_layout(QHBoxLayout(self.attach_strip))
        self.attach_strip_row.addStretch(1)
        self.attach_strip.setVisible(False)
        self._input_col_layout.addWidget(self.attach_strip)

        # Permanent shell prefix + borderless editor. Enter is the only
        # submit path; the row carries no controls, padding, or separator.
        self.input_pill = QFrame()
        self.input_pill.setObjectName("promptRow")
        self.input_pill.setContentsMargins(0, 0, 0, 0)
        pill_row = zero_layout(QHBoxLayout(self.input_pill))

        self.prompt_prefix = QLabel("root@agent:~# ")
        self.prompt_prefix.setObjectName("promptPrefix")
        self.prompt_prefix.setMargin(0)
        self.prompt_prefix.setIndent(0)
        self.prompt_prefix.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)

        self.input_field = TerminalInput()
        self.input_field.setObjectName("commandInput")
        # Let file drops bubble to AgentWindow instead of the editor.
        self.input_field.setAcceptDrops(False)
        self.input_field.viewport().setAcceptDrops(False)
        self.input_field.setCursorWidth(8)
        self.input_field.setFrameShape(QFrame.Shape.NoFrame)
        # QPlainTextEdit has no QLineEdit-style setTextMargins(); zero the
        # document, viewport, and contents margins for the same result.
        self.input_field.setContentsMargins(0, 0, 0, 0)
        self.input_field.viewport().setContentsMargins(0, 0, 0, 0)
        self.input_field.setViewportMargins(0, 0, 0, 0)
        self.input_field.document().setDocumentMargin(0)
        self.input_field.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.input_field.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.input_field.setFixedHeight(INPUT_LINE_H)
        self.input_field.textChanged.connect(self._on_input_changed)
        self.input_field.installEventFilter(self)

        # Prefix and editor share one top edge.
        text_host = QWidget()
        text_col = zero_layout(QVBoxLayout(text_host))
        text_col.addStretch(1)
        text_row = zero_layout(QHBoxLayout())
        text_row.addWidget(self.prompt_prefix, 0, Qt.AlignmentFlag.AlignTop)
        text_row.addWidget(self.input_field, 1, Qt.AlignmentFlag.AlignTop)
        text_col.addLayout(text_row)
        text_col.addStretch(1)
        pill_row.addWidget(text_host, 1)

        self._input_col_layout.addWidget(self.input_pill)
        col.addWidget(holder)
        return outer

    # ------------------------------------------------------------------
    # Input helpers
    # ------------------------------------------------------------------
    def _get_input_text(self) -> str:
        return self.input_field.toPlainText().strip()

    def _clear_input(self):
        self.input_field.clear()

    def _on_input_changed(self):
        self._autogrow_input()

    def _autogrow_input(self):
        doc = self.input_field.document()
        lines = int(doc.size().height())
        target = max(1, min(INPUT_MAX_LINES, lines))
        height = target * INPUT_LINE_H + 4
        if abs(self.input_field.height() - height) > 2:
            self.input_field.setFixedHeight(height)

    # ------------------------------------------------------------------
    # Attachments — drag & drop only; the paperclip button was removed.
    # ------------------------------------------------------------------
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
        # The terminal canvas carries no status chrome; the window title is
        # the only place a shell-like status belongs.
        label = " ".join((text or "").split())
        self.setWindowTitle(f"Agent — {label}" if label else "Agent")

    # ------------------------------------------------------------------
    # Phase 6c performance guard — pause motion while minimized
    # ------------------------------------------------------------------
    def changeEvent(self, event):
        if event.type() == QEvent.Type.WindowStateChange:
            self._minimized = self.isMinimized()
            if self._minimized:
                self._stop_pass_a_anims()
                # Minimise to the tray, not the taskbar.
                QTimer.singleShot(0, self._hide_minimized_to_tray)
        super().changeEvent(event)

    def _hide_minimized_to_tray(self):
        tray_icon = self._tray_icon
        if (self.isMinimized() and self.isVisible()
                and tray_icon is not None and tray_icon.isVisible()):
            self.hide_to_tray()

    def _stop_pass_a_anims(self):
        """Stop looping card/entry anims (called when minimized)."""
        for anim in list(self._card_anims):
            anim.stop()
        self._card_anims = []

    @staticmethod
    def _read_animations_flag() -> bool:
        """Read ANIMATIONS_ENABLED once (raw config, default True)."""
        try:
            val = read_config_file_raw().get("ANIMATIONS_ENABLED", True)
        except Exception:
            val = True
        if isinstance(val, bool):
            return val
        return str(val).strip().lower() in ("true", "1", "yes", "on")

    # ------------------------------------------------------------------
    # Theme engine
    # ------------------------------------------------------------------
    def _cycle_theme(self):
        try:
            idx = THEME_NAMES.index(self._theme)
        except ValueError:
            idx = 0
        self._apply_theme(THEME_NAMES[(idx + 1) % len(THEME_NAMES)])

    def _apply_theme(self, name: str):
        """Swap the app stylesheet instantly; no restart or window rebuild."""
        if name not in THEME_NAMES:
            return
        try:
            qss = theme_stylesheet(name)
        except OSError as exc:
            self.set_status(f"Theme error: {exc}", busy=False)
            return
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(qss)
        self._theme = name

    def _cmd_theme(self, args: str):
        name = args.strip().lower()
        if name:
            if name not in THEME_NAMES:
                self.set_status(f"unknown theme: {name}", busy=False)
                return
            self._apply_theme(name)
        else:
            self._cycle_theme()
        self.set_status(f"theme: {self._theme}", busy=False)

    def send(self):
        if self._processing:
            return
        text = self._get_input_text()
        attached = list(self.attachments)
        if not text and not attached:
            return

        # A new command cuts the assistant off mid-sentence and stops the
        # hands-free mic; the reply will re-arm it when the turn finishes.
        self._interrupt_speech()
        self._cancel_continuous_listening(resume_wake=False)

        # Slash commands bypass attachments (match agent_gui.send).
        if text.startswith("/") and not attached:
            self._clear_input()
            self._handle_slash_command(text)
            return

        if self._continuous_enabled:
            self._continuous_paused = False

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

    def _interrupt_speech(self):
        """Cut off TTS immediately; a missing backend is not fatal."""
        try:
            get_voice_output().stop()
        except Exception as e:
            print(f"[tts] stop failed: {e}")

    def _reset_task_context(self):
        """Drop previous task details so a new task cannot inherit them."""
        if not self.conversation_history:
            return
        self.conversation_history.clear()
        try:
            get_agent().save_conversation_history(self.conversation_history)
        except Exception:
            pass

    def _reset_task_context_if_new(self, text):
        try:
            import agent as _agent
            if not _agent.starts_new_task(text):
                return
        except Exception:
            return
        self._reset_task_context()

    def _start_agent_turn(self, text, attached_paths=None):
        """Kick off a background agent turn for `text`."""
        self._reset_task_context_if_new(text)
        self._processing = True
        self.input_field.setEnabled(False)
        self.set_status("Reading attachments…" if attached_paths else "Thinking…",
                        busy=True)
        self._live_steps = []
        self.panel.set_activity(["Thinking…"])
        self._thinking_message = SystemMessage("Thinking…")
        self.chat.add_message(self._thinking_message, fade=False)

        self._worker = AgentWorker(text, self.conversation_history, attached_paths)
        self._worker.completed.connect(self._on_turn_completed)
        self._worker.failed.connect(self._on_turn_failed)
        self._worker.step.connect(self._on_worker_step)
        self._worker.start()

    def _on_worker_step(self, line):
        """Stream one raw step into the ACTIVITY panel while the turn runs."""
        text = " ".join(str(line).split())
        if not text:
            return
        self._live_steps.append(text)
        self._live_steps = self._live_steps[-50:]
        self.panel.set_activity(self._live_steps)

    def _clear_thinking_message(self):
        message = self._thinking_message
        self._thinking_message = None
        self.chat.remove_message(message)

    def _end_turn(self):
        self._processing = False
        self.input_field.setEnabled(True)
        self.input_field.setFocus()
        self.set_status("Ready", busy=False)
        # A scheduled task may have fired while this turn was running.
        self._drain_scheduled_queue()

    def _on_turn_completed(self, payload):
        message, step_log = payload
        self._last_step_log = step_log
        self._clear_thinking_message()
        self.chat.add_message(AssistantMessage(message, step_log=step_log))
        # Keep the finished turn's steps on the ACTIVITY card until the next
        # turn starts.
        self.panel.set_activity(step_log)
        self._speak_reply(message)
        self._end_turn()
        self._schedule_continuous_listen()

    def _on_turn_failed(self, error_text):
        self._clear_thinking_message()
        self.chat.add_message(AssistantMessage(error_text))
        self._end_turn()
        self._schedule_continuous_listen()

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
            queued = get_voice_output().speak(message)
            if not queued:
                error = get_voice_output().last_error()
                if error:
                    self._on_tts_error(error)
        except Exception as e:
            self._on_tts_error(f"speak failed: {e}")

    def _on_tts_error(self, message):
        """Show the first speech failure as a dim system line."""
        print(f"[tts] {message}")
        if self._tts_error_shown:
            return
        self._tts_error_shown = True
        self.chat.add_message(SystemMessage(f"Voice unavailable: {message}"))

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
            self.chat.add_message(HelpMessage(HELP_TEXT))
            self.conversation_history.append(f"User: {text}")
            self.conversation_history.append(f"System: {HELP_TEXT}")
            return True

        # Interface commands never need the agent; keep them working even if
        # the brain is unavailable.
        if cmd == "/theme":
            self._cmd_theme(args)
            return True

        if cmd == "/continuous":
            self._cmd_continuous(args)
            return True

        ui_commands = {
            "/settings": self.open_settings,
            "/tasks": self.open_schedule_dialog,
            "/index": self.on_index_documents,
            "/index-docs": self.on_index_documents,
            "/export": self.export_conversation,
            "/copy": self.copy_all,
            "/activity": self.toggle_log,
            "/log": self.toggle_log,
            "/tg-clear": self.panel.clear_telegram,
            "/clear": self.new_chat,
            "/new": self.new_chat,
        }
        handler = ui_commands.get(cmd)
        if handler is not None:
            handler()
            return True

        # A new tool command starts a new task: do not inherit the previous
        # task's titles, screenshots, or tool results.
        self._reset_task_context()

        try:
            agent = get_agent()
        except Exception as e:
            self._finish_slash_command(text, f"ERROR: {e}")
            return True

        if cmd == "/reload-skills":
            if self._processing:
                self.chat.add_message(AssistantMessage(
                    "ERROR: /reload-skills is unavailable while a turn is running."))
                return True
            try:
                manager = agent.reload_skills()
                result = manager.summary()
                warnings = manager.warnings
                if warnings:
                    result += "\nWarnings:\n" + "\n".join(
                        f"- {w}" for w in warnings)
            except Exception as e:
                result = f"ERROR: {e}"
            return self._finish_slash_command(text, result)

        if cmd == "/browse":
            if "|" not in args:
                return self._finish_slash_command(
                    text, "ERROR: format is /browse https://site | task")
            self._run_background_command(
                text, "Starting browser task\u2026",
                lambda: agent.browse_web(args))
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
            QCheckBox {{
                color: {COLOR_TEXT_HI};
                font-family: "{FONT_MONO}";
                font-size: 12px;
                spacing: 8px;
                background: transparent;
            }}
            QCheckBox::indicator {{
                width: 14px;
                height: 14px;
                border: 1px solid {COLOR_ACCENT};
                background: {COLOR_BG};
            }}
            QCheckBox::indicator:checked {{
                background: {COLOR_ACCENT};
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

        # Phase 6c H — fade the newly shown tab page in briefly.
        def _fade_tab(_idx):
            if not self._animations_enabled:
                return
            page = tabs.currentWidget()
            if page is None:
                return
            eff = QGraphicsOpacityEffect(page)
            page.setGraphicsEffect(eff)
            a = QPropertyAnimation(eff, b"opacity", page)
            a.setDuration(ENTRY_FADE_MS)
            a.setStartValue(0.0)
            a.setEndValue(1.0)
            a.setEasingCurve(QEasingCurve.Type.OutCubic)
            a.finished.connect(lambda p=page: p.setGraphicsEffect(None))
            a.start()
            self._tab_fade = a

        tabs.currentChanged.connect(_fade_tab)

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

        def checkbox(lay, label, key):
            row = QWidget()
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            lab = QLabel(label)
            lab.setFixedWidth(210)
            rl.addWidget(lab)
            box = QCheckBox()
            saved = cfg.get(key, cfgmod.DEFAULTS.get(key, False))
            if isinstance(saved, str):
                checked = saved.strip().lower() in ("true", "1", "yes", "on")
            else:
                checked = bool(saved)
            box.setChecked(checked)
            rl.addWidget(box)
            rl.addStretch(1)
            lay.addWidget(row)
            entries[key] = ("check", box)
            return box

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
        section(lay, "Motion")
        dropdown(lay, "Animations", "ANIMATIONS_ENABLED", ["true", "false"])
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
        section(lay, "Proactive thoughts")
        checkbox(lay, "Spontaneous thoughts (2h)",
                 "SPONTANEOUS_THOUGHTS_ENABLED")
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
        # Phase 6c H — backdrop fades in (0 -> 1 effect over the 170-alpha
        # backdrop) and the panel scales 0.97 -> 1.0.
        self._animate_settings_open(overlay, panel)

    def _animate_settings_open(self, overlay, panel):
        if not self._animations_enabled:
            return
        fade = QGraphicsOpacityEffect(overlay)
        overlay.setGraphicsEffect(fade)
        a1 = QPropertyAnimation(fade, b"opacity", overlay)
        a1.setDuration(OVERLAY_FADE_MS)
        a1.setStartValue(0.0)
        a1.setEndValue(1.0)
        a1.setEasingCurve(QEasingCurve.Type.OutCubic)
        a1.finished.connect(lambda: overlay.setGraphicsEffect(None))
        a1.start()
        self._settings_fade = a1

        pw, ph = panel.width(), panel.height()
        cx, cy = (overlay.width() - pw) // 2, (overlay.height() - ph) // 2
        start = QRect(cx + int(pw * 0.015), cy + int(ph * 0.015),
                      int(pw * 0.97), int(ph * 0.97))
        end = QRect(cx, cy, pw, ph)
        panel.setGeometry(start)
        a2 = QPropertyAnimation(panel, b"geometry", panel)
        a2.setDuration(OVERLAY_SCALE_MS)
        a2.setStartValue(start)
        a2.setEndValue(end)
        a2.setEasingCurve(QEasingCurve.Type.OutCubic)
        a2.start()
        self._settings_scale = a2

    def _close_settings(self):
        if self._settings_overlay is not None:
            self._settings_overlay.deleteLater()
            self._settings_overlay = None

    def _save_settings(self, entries):
        updates = {}
        for key, (kind, widget) in entries.items():
            if kind == "combo":
                updates[key] = widget.currentText()
            elif kind == "check":
                updates[key] = widget.isChecked()
            else:
                updates[key] = widget.text().strip()
        try:
            write_config_keys(updates)
        except Exception as e:
            self.chat.add_message(AssistantMessage(f"ERROR: save failed — {e}"))
            return
        self._apply_spontaneous_setting()
        self._close_settings()
        self.chat.add_message(AssistantMessage(
            "✅ Settings saved. Proactive thoughts apply immediately; "
            "other changes apply on restart."))

    def _reset_settings_defaults(self):
        cfgmod = get_config_module()
        try:
            write_config_keys(dict(cfgmod.DEFAULTS))
        except Exception as e:
            self.chat.add_message(AssistantMessage(f"ERROR: reset failed — {e}"))
            return
        self._apply_spontaneous_setting()
        self._apply_theme(read_theme_name())
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
    # Chat / UI handlers (formerly driven by the sidebar)
    # ------------------------------------------------------------------
    def new_chat(self):
        """Clear the on-screen chat and the saved conversation."""
        self.conversation_history.clear()
        try:
            get_agent().save_conversation_history(self.conversation_history)
        except Exception:
            pass
        self.chat.clear()
        self._last_step_log = None
        self._live_steps = []
        self._thinking_message = None
        self.panel.set_activity_idle()
        if self._log_panel is not None:
            self._refresh_log_panel()
        self.input_field.setFocus()

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
        """Copy the whole conversation to the clipboard."""
        if not self.chat.chat_log:
            self.chat.add_message(AssistantMessage("Nothing to copy"))
            return
        QApplication.clipboard().setText(self._chat_as_text())
        self.chat.add_message(AssistantMessage("\U0001f4cb Copied to clipboard"))

    def export_conversation(self):
        """Export the conversation through export_tools."""
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
        """Pick a folder and index it for RAG in the background."""
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
        """Show/hide the bottom activity-log panel.

        The first call creates it; subsequent calls toggle visibility.
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
        """Read-only scheduled tasks view.

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
    def show_greeting(self):
        self.chat.add_message(BannerMessage(AGENT_BANNER), fade=False)
        self.chat.add_message(ReadyMessage("Agent Ready..."), fade=False)
        self.chat.add_message(SystemMessage(
            "System Online. Type /help for commands."), fade=False)
        if self.conversation_history:
            self.chat.add_message(SystemMessage(
                f"Resumed — {len(self.conversation_history)} messages from last session."
            ), fade=False)

    # ------------------------------------------------------------------
    # Section 4 — browser approvals
    # ------------------------------------------------------------------
    def _setup_browser_bridge(self):
        """Register the Qt approval bridge with the browser service."""
        try:
            import browser_agent
            bridge = BrowserApprovalBridge(self)
            bridge.requested.connect(self._on_browser_approval_request)
            browser_agent.set_approval_handler(bridge)
            self._browser_bridge = bridge
        except Exception as e:
            print(f"[browser] approval bridge unavailable: {e}")

    def _on_browser_approval_request(self, payload):
        card = BrowserApprovalCard(payload)
        self.chat.add_message(card, fade=False)
        card.resolved.connect(self._on_browser_approval_resolved)
        self.set_status("Waiting for browser approval\u2026", busy=True)

    def _on_browser_approval_resolved(self, approved):
        if self._processing:
            self.set_status("Browsing\u2026", busy=True)

    # ------------------------------------------------------------------
    # Option 2 — OS control (hide/show + kill switch)
    # ------------------------------------------------------------------
    def _setup_os_control(self):
        """Install the hide/show bridge and start the kill-switch watcher."""
        try:
            import os_control
            os_control.set_gui_visibility_handlers(
                self._hide_gui_for_screenshot,
                self._show_gui_after_screenshot,
            )
            os_control.ensure_kill_switch()
            try:
                import browser_agent
                os_control.add_kill_callback(
                    browser_agent.cancel_current_task)
            except Exception:
                pass
            self._os_control = os_control
        except Exception as e:
            print(f"[os] OS control unavailable: {e}")

    def _hide_gui_for_screenshot(self):
        """Called from the agent worker; blocks until Qt has hidden the window."""
        event = threading.Event()
        self._gui_visibility_request.emit(False, event)
        if not event.wait(timeout=1.5):
            raise RuntimeError("GUI hide timed out")

    def _show_gui_after_screenshot(self):
        event = threading.Event()
        self._gui_visibility_request.emit(True, event)
        event.wait(timeout=1.5)

    def _apply_gui_visibility(self, visible: bool, event):
        try:
            if not visible:
                self._gui_was_visible = self.isVisible()
                self._gui_was_minimized = self.isMinimized()
                self.hide()
            elif self._gui_was_visible:
                if self._gui_was_minimized:
                    self.showMinimized()
                else:
                    self.show()
                    self.raise_()
                    self.activateWindow()
        finally:
            if event is not None:
                event.set()

    # ------------------------------------------------------------------
    # Option 3 — wake word
    # ------------------------------------------------------------------
    def _start_wake_word_service(self):
        """Start the openWakeWord listener when WAKE_WORD_ENABLED is true."""
        if self._wake_word_service is not None:
            return
        try:
            cfg = dict(get_config_module().DEFAULTS)
            try:
                cfg.update(read_config_file_raw())
            except Exception:
                pass

            enabled = cfg.get("WAKE_WORD_ENABLED", False)
            if isinstance(enabled, str):
                enabled = enabled.strip().lower() in ("true", "1", "yes", "on")
            if not enabled:
                return

            model = str(cfg.get("WAKE_WORD_MODEL") or "agent")
            threshold = float(cfg.get("WAKE_WORD_THRESHOLD", 0.5))
            cooldown = float(cfg.get("WAKE_WORD_COOLDOWN", 1.5))
            device_raw = str(cfg.get("WAKE_WORD_DEVICE", "") or "").strip()
            device_index = int(device_raw) if device_raw else None

            import wake_word
            service = wake_word.WakeWordService(
                on_detect=self._wake_word_detected.emit,
                model=model,
                threshold=threshold,
                device_index=device_index,
                cooldown=cooldown,
                on_error=self._wake_word_error.emit,
            )
            service.start()
            self._wake_word_service = service
            print(
                f"[wake] listening ({service.model_spec}, "
                f"{service.inference_framework})")
        except Exception as e:
            self._wake_word_service = None
            print(f"[wake] failed to start: {e}")
            self.chat.add_message(AssistantMessage(f"Wake word disabled: {e}"))

    def _on_wake_word(self, score):
        """GUI-thread slot: acknowledge, open the mic, auto-stop after a window."""
        if self._processing or self._recording:
            self._resume_wake_word()
            return
        try:
            record_ms = int(str(
                read_config_file_raw().get("WAKE_RECORD_MS", 3500)).strip())
        except (TypeError, ValueError):
            record_ms = 3500
        record_ms = max(1000, min(30000, record_ms))
        self._wake_record_ms = record_ms
        self.chat.add_message(SystemMessage(
            f"Wake word detected ({float(score):.2f}) \u2014 "
            f"listening {record_ms / 1000:g}s"))
        self._wake_triggered = True
        self._continuous_paused = False
        # "Yeah?" first, then the mic. Opening it immediately records the ack.
        if self._speak_wake_ack():
            self._wake_ack_started = time.monotonic()
            QTimer.singleShot(100, self._open_mic_after_ack)
        else:
            self._open_mic_for_wake()

    def _speak_wake_ack(self) -> bool:
        """Queue a short spoken acknowledgement. False when TTS is silent."""
        try:
            voice = get_voice_output()
            voice.stop()  # the acknowledgement starts immediately
            return bool(voice.speak("Yeah?"))
        except Exception as e:
            print(f"[tts] wake acknowledgement skipped: {e}")
            return False

    def _open_mic_after_ack(self):
        """Wait for the acknowledgement to finish, then start listening."""
        if not self._wake_triggered:
            return
        try:
            speaking = bool(get_voice_output().is_speaking())
        except Exception:
            speaking = False
        if speaking and time.monotonic() - self._wake_ack_started < 3.0:
            QTimer.singleShot(100, self._open_mic_after_ack)
            return
        self._open_mic_for_wake()

    def _open_mic_for_wake(self):
        if not self._wake_triggered:
            return
        self._hotkey_press()
        # The wake path has no physical key release, so stop it ourselves.
        QTimer.singleShot(self._wake_record_ms, self._wake_word_auto_stop)

    def _wake_word_auto_stop(self):
        if not self._wake_triggered:
            return
        self._wake_triggered = False
        self._hotkey_release()

    def _on_wake_word_error(self, message):
        print(f"[wake] {message}")
        if self._wake_word_error_shown:
            return
        self._wake_word_error_shown = True
        self.chat.add_message(
            AssistantMessage(f"Wake word unavailable: {message}"))

    def _pause_wake_word(self):
        service = self._wake_word_service
        if service is not None:
            try:
                service.pause()
            except Exception:
                pass

    def _resume_wake_word(self):
        service = self._wake_word_service
        if service is not None:
            try:
                service.resume()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Continuous listening (/continuous on|off)
    # ------------------------------------------------------------------
    @staticmethod
    def _read_continuous_flag() -> bool:
        try:
            val = read_config_file_raw().get("CONTINUOUS_LISTENING", False)
        except Exception:
            val = False
        if isinstance(val, bool):
            return val
        return str(val).strip().lower() in ("true", "1", "yes", "on")

    def _cmd_continuous(self, args):
        mode = (args or "").strip().lower()
        if mode not in ("on", "off"):
            self.chat.add_message(AssistantMessage(
                "Usage: /continuous on|off\n"
                "ON keeps the mic open after each reply; 60s of silence "
                "returns to the wake word."))
            return

        self._continuous_enabled = mode == "on"
        self._continuous_paused = False
        try:
            write_config_keys(
                {"CONTINUOUS_LISTENING": self._continuous_enabled})
        except Exception as e:
            print(f"[config] continuous save failed: {e}")

        if self._continuous_enabled:
            self.chat.add_message(SystemMessage(
                "Continuous listening ON \u2014 speak without the wake word. "
                "60s of silence returns to the wake word."))
            self._schedule_continuous_listen()
        else:
            self._cancel_continuous_listening()
            self.chat.add_message(SystemMessage(
                "Continuous listening OFF \u2014 wake word required."))

    def _start_continuous_if_configured(self):
        if not self._continuous_enabled:
            return
        self.chat.add_message(SystemMessage(
            "Continuous listening ON \u2014 speak without the wake word. "
            "60s of silence returns to the wake word."))
        self._schedule_continuous_listen(1000)

    def _schedule_continuous_listen(self, delay_ms=300):
        """Re-arm hands-free listening once the turn and its speech are done."""
        if not self._continuous_enabled or self._continuous_paused:
            return
        if self._processing or self._recording:
            return
        if self._continuous_timer is None:
            self._continuous_timer = QTimer(self)
            self._continuous_timer.setSingleShot(True)
            self._continuous_timer.timeout.connect(self._try_continuous_listen)
        self._continuous_timer.start(max(0, int(delay_ms)))

    def _try_continuous_listen(self):
        if not self._continuous_enabled or self._continuous_paused:
            return
        if self._processing or self._recording:
            return
        try:
            if get_voice_output().is_speaking():
                self._schedule_continuous_listen(300)
                return
        except Exception:
            pass
        self._start_recording(continuous=True)

    def _cancel_continuous_listening(self, resume_wake=True):
        if self._continuous_timer is not None:
            self._continuous_timer.stop()
        if self._continuous_listening:
            self._continuous_listening = False
            self._recording = False
            try:
                get_voice().cancel_recording()
            except Exception:
                pass
        if resume_wake:
            self._resume_wake_word()

    # ------------------------------------------------------------------
    # Option A — proactive thoughts
    # ------------------------------------------------------------------
    @staticmethod
    def _read_spontaneous_flag() -> bool:
        try:
            val = read_config_file_raw().get(
                "SPONTANEOUS_THOUGHTS_ENABLED", False)
        except Exception:
            val = False
        if isinstance(val, bool):
            return val
        return str(val).strip().lower() in ("true", "1", "yes", "on")

    def _start_spontaneous_thoughts(self):
        """Start the 2-hour thought timer when the config flag is on."""
        if self._spontaneous_timer is not None:
            return
        try:
            if not self._read_spontaneous_flag():
                return
            if self._os_control is None:
                print("[thoughts] OS control unavailable; disabled")
                return

            timer = QTimer(self)
            timer.setInterval(SPONTANEOUS_INTERVAL_MS)
            timer.timeout.connect(self._spontaneous_tick)
            timer.start()
            self._spontaneous_timer = timer
            print("[thoughts] enabled (every 2h)")
        except Exception as e:
            print(f"[thoughts] failed to start: {e}")

    def _stop_spontaneous_thoughts(self):
        timer = self._spontaneous_timer
        self._spontaneous_timer = None
        if timer is not None:
            timer.stop()
            timer.deleteLater()

    def _apply_spontaneous_setting(self):
        """React to the settings checkbox without a restart."""
        if self._read_spontaneous_flag():
            self._start_spontaneous_thoughts()
        else:
            self._stop_spontaneous_thoughts()

    def _spontaneous_tick(self):
        """GUI-thread timer slot: start one background capture+vision run."""
        if self._spontaneous_busy or self._processing or self._recording:
            return
        if self._os_control is None:
            return
        self._spontaneous_busy = True
        threading.Thread(
            target=self._spontaneous_work, daemon=True).start()

    def _spontaneous_work(self):
        """Capture the screen, ask the vision model, emit one observation."""
        path = None
        try:
            result = self._os_control.screenshot_background()
            if not result.startswith("Screenshot saved:"):
                print(
                    f"[thoughts] screenshot skipped: "
                    f"{result.splitlines()[0][:120]}")
                return
            path = result.splitlines()[0].split(
                "Screenshot saved:", 1)[1].strip()
            text = get_agent().spontaneous_thought(path)
            if text:
                self._spontaneous_message.emit(text)
        except Exception as exc:
            print(f"[thoughts] failed: {exc}")
        finally:
            # Do not accumulate private screenshots on disk.
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass
            self._spontaneous_finished.emit()

    def _on_spontaneous_message(self, text):
        if not text:
            return
        self.chat.add_message(AssistantMessage(f"[proactive] {text}"))

    def _on_spontaneous_finished(self):
        self._spontaneous_busy = False

    # ------------------------------------------------------------------
    # Background services — tray, hotkey, scheduler
    # ------------------------------------------------------------------
    def start_services(self):
        """Wire tray, hotkey, scheduler, watchers and TTS warmup. Call after
        the window is shown.

        The scheduler and watcher callbacks fire on their own daemon threads,
        so they route through a Qt signal rather than touching widgets. Tray
        actions are native Qt signals and therefore already run on the GUI
        thread.
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

        # Phase 6b — start the panel polling timers (SYSTEM 2s, WEATHER 10min).
        # Guarded so a panel-data hiccup can never stop the app from booting.
        try:
            self.start_panel_timers()
        except Exception as e:
            print(f"[panel] timers failed to start: {e}")

        # Phase 6c — ambient motion (status pulse, card entrance).
        try:
            self.start_motion()
        except Exception as e:
            print(f"[motion] failed to start: {e}")

        # Native Qt tray: no pystray thread and no cross-thread timer hop.
        self._start_tray()

        # Warm up TTS so the first spoken reply is instant. warmup() starts its
        # own daemon thread and returns immediately, so this never blocks the
        # window; failures are logged, not fatal.
        try:
            voice = get_voice_output()
            voice.set_error_callback(self._tts_error.emit)
            voice.warmup()
            print("[tts] warmup requested")
        except Exception as e:
            print(f"[tts] warmup skipped: {e}")

        # Section 2 — Telegram document listener. Opt-in via config; config
        # and authorisation problems surface in the transcript, not as a
        # startup exception.
        self._start_telegram_doc_service()

        # Option 3 — wake word listener. Opt-in via config; missing models or
        # dependencies surface in the transcript, never as a startup crash.
        self._start_wake_word_service()

        # Hands-free loop when CONTINUOUS_LISTENING is on at launch.
        self._start_continuous_if_configured()

        # Option A — proactive thoughts. Off by default (screenshots leave the
        # machine); a missing vision key simply produces no messages.
        self._start_spontaneous_thoughts()

    # -- Telegram document listener (Section 2) -------------------------
    def _start_telegram_doc_service(self):
        """Start the dedicated Telegram listener and its GUI drain timer.

        Called once the window is shown. The service runs on its own daemon
        thread with its own asyncio loop; this method only creates the
        main-thread QTimer that drains its result queue.
        """
        if self._telegram_service is not None:
            return

        try:
            import telegram_service
        except Exception as exc:
            self.chat.add_message(UntrustedTextMessage(
                f"listener unavailable: {type(exc).__name__}",
                source="TELEGRAM DOCS"))
            return

        try:
            settings = telegram_service.load_settings()
            if not settings.enabled:
                return
            service = telegram_service.TelegramDocService(settings)
            if not service.start():
                return
        except telegram_service.TelegramDocConfigError as exc:
            self.chat.add_message(UntrustedTextMessage(
                f"listener not started: {exc}", source="TELEGRAM DOCS"))
            return
        except Exception as exc:
            self.chat.add_message(UntrustedTextMessage(
                f"listener failed to start: {type(exc).__name__}",
                source="TELEGRAM DOCS"))
            return

        self._telegram_service = service
        self._telegram_results = service.results
        timer = QTimer(self)
        timer.setInterval(telegram_service.RESULT_POLL_MS)
        timer.timeout.connect(self._drain_telegram_results)
        timer.start()
        self._telegram_timer = timer

    def _drain_telegram_results(self):
        """Main-thread drain: the only place Telegram results touch widgets."""
        if self._telegram_results is None:
            return
        try:
            import telegram_service
            telegram_service.drain_results(
                self._telegram_results, self._render_telegram_result, limit=20)
        except Exception as exc:
            self._stop_telegram_doc_service()
            self.chat.add_message(UntrustedTextMessage(
                f"polling stopped: {type(exc).__name__}",
                source="TELEGRAM DOCS"))

    def _render_telegram_result(self, item):
        try:
            kind, chat_id, message_id, payload, *rest = item
        except (TypeError, ValueError):
            return
        meta = rest[0] if rest and isinstance(rest[0], dict) else {}

        if kind == "summary":
            sender = str(meta.get("sender") or f"chat {chat_id}")
            file_name = str(meta.get("file_name") or f"msg {message_id}")
            self.panel.add_telegram_summary(
                sender, file_name, str(payload))
        elif kind in ("rejected", "error"):
            self.chat.add_message(UntrustedTextMessage(
                f"{kind}: {payload}", source="TELEGRAM DOCS"))
        elif kind == "service_error":
            self.chat.add_message(UntrustedTextMessage(
                f"listener stopped: {payload}", source="TELEGRAM DOCS"))
            self._stop_telegram_doc_service()

    def _stop_telegram_doc_service(self):
        """Cooperative shutdown. Never joins the thread on the GUI thread."""
        timer = self._telegram_timer
        if timer is not None:
            timer.stop()
            self._telegram_timer = None
        service = self._telegram_service
        if service is not None:
            service.stop()
            self._telegram_service = None
        self._telegram_results = None

    # -- tray -----------------------------------------------------------
    def _make_tray_icon(self) -> QIcon:
        pix = QPixmap(64, 64)
        pix.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(COLOR_ACCENT))
        painter.drawEllipse(4, 4, 56, 56)
        font = QFont("Consolas")
        font.setPixelSize(34)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QPen(QColor(COLOR_BG)))
        painter.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, "A")
        painter.end()
        return QIcon(pix)

    def _start_tray(self):
        """Create the native QSystemTrayIcon once, at startup."""
        if self._tray_icon is not None:
            return
        if not QSystemTrayIcon.isSystemTrayAvailable():
            print("[tray] system tray unavailable; close will minimise")
            return
        try:
            icon = QSystemTrayIcon(self._make_tray_icon(), self)
            icon.setToolTip("AgentBot")

            menu = QMenu(self)
            show_action = QAction("Show", menu)
            show_action.triggered.connect(self.show_window)
            quit_action = QAction("Quit", menu)
            quit_action.triggered.connect(self.quit_app)
            menu.addAction(show_action)
            menu.addSeparator()
            menu.addAction(quit_action)

            icon.setContextMenu(menu)
            icon.activated.connect(self._on_tray_activated)
            icon.show()
            self._tray_icon = icon
            self._tray_menu = menu
            print("[tray] QSystemTrayIcon shown")
        except Exception as e:
            print(f"[tray] failed: {e}")
            self._tray_icon = None
            self._tray_menu = None

    def _on_tray_activated(self, reason):
        if reason in (
            QSystemTrayIcon.ActivationReason.DoubleClick,
            QSystemTrayIcon.ActivationReason.Trigger,
        ):
            self.show_window()

    def hide_to_tray(self):
        """Close/minimise button: withdraw to the tray (or taskbar fallback)."""
        try:
            get_agent().save_conversation_history(self.conversation_history)
        except Exception:
            pass
        tray_icon = self._tray_icon
        if tray_icon is not None and tray_icon.isVisible():
            # Windows keeps a ghost taskbar button when a window is hidden
            # while still in the normal state; minimize first, then withdraw.
            self.setWindowState(Qt.WindowState.WindowMinimized)
            self.hide()
        else:
            # Never strand the window when no tray icon could be created.
            self.showMinimized()

    def show_window(self):
        # showNormal clears the minimized state so the taskbar button returns;
        # show() covers a window that was hidden while minimized.
        self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()

    def quit_app(self):
        try:
            get_agent().save_conversation_history(self.conversation_history)
        except Exception:
            pass
        # Section 2 — ask the Telegram listener to disconnect. Cooperative:
        # the daemon thread exits on its own; never join on the GUI thread.
        telegram_service = self._telegram_service
        self._stop_telegram_doc_service()
        if telegram_service is not None:
            # A normal quit must not leave a stale session lock behind.
            telegram_service.release_lock()
        # Option 3 — close the wake-word mic stream so the mic light turns off.
        wake_service = self._wake_word_service
        self._wake_word_service = None
        if wake_service is not None:
            try:
                wake_service.stop()
            except Exception:
                pass
        # Option A — stop the proactive-thought timer.
        spontaneous_timer = self._spontaneous_timer
        self._spontaneous_timer = None
        if spontaneous_timer is not None:
            spontaneous_timer.stop()
        # Stop the media poller so an in-flight thread can't emit into a
        # deleted window during teardown (Phase 6b follow-up).
        if getattr(self, "_media_poller", None) is not None:
            self._media_poller.stop()
        if getattr(self, "_media_timer", None) is not None:
            self._media_timer.stop()
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
        tray_icon = self._tray_icon
        self._tray_icon = None
        self._tray_menu = None
        if tray_icon is not None:
            try:
                tray_icon.hide()
            except Exception:
                pass
        # Option 2 — stop the OS kill-switch watcher and detach the GUI bridge.
        try:
            import os_control
            os_control.stop_kill_switch()
            os_control.set_gui_visibility_handlers(None, None)
        except Exception:
            pass
        self._os_control = None
        if self._hotkey_installed and keyboard is not None:
            try:
                keyboard.unhook_all()
            except Exception:
                pass
            self._hotkey_installed = False
        # Section 4 — cancel any browser task and detach the approval bridge.
        try:
            import browser_agent
            browser_agent.shutdown()
            browser_agent.set_approval_handler(None)
        except Exception:
            pass
        self._browser_bridge = None
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
            self._wake_triggered = False
            self._stop_recording()

    def _start_recording(self, continuous=False):
        if self._recording or self._processing:
            return
        self._recording = True
        self._continuous_listening = bool(continuous)
        self._pause_wake_word()
        self.set_status(
            "Listening… (continuous)" if continuous
            else "Recording… release F9 to transcribe",
            busy=True)

        def work():
            try:
                if continuous:
                    get_voice().start_recording(
                        on_transcript=lambda text, reason: self._voice_event.emit(
                            f"continuous:{reason}:{text}"),
                        idle_timeout=CONTINUOUS_IDLE_SECONDS)
                else:
                    get_voice().start_recording()
                self._voice_event.emit("recording_started")
            except Exception as e:
                self._voice_event.emit(f"error:Mic error: {e}")

        threading.Thread(target=work, daemon=True).start()

    def _stop_recording(self):
        if not self._recording:
            return
        self._recording = False
        self._continuous_listening = False
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
            self._continuous_listening = False
            self.set_status(payload[len("error:"):], busy=False)
            self._resume_wake_word()
            return
        if payload == "recording_started":
            return
        if payload.startswith("continuous:"):
            _, reason, text = payload.split(":", 2)
            self._recording = False
            self._continuous_listening = False
            if reason == "error":
                self._continuous_paused = True
                self._resume_wake_word()
                self.set_status(
                    "Transcription failed \u2014 say the wake word",
                    busy=False)
                self.chat.add_message(SystemMessage(
                    "Continuous listening paused \u2014 transcription "
                    "failed; say the wake word to resume."))
                return
            if reason == "idle":
                # Nobody spoke for 60s: hand the mic back to the wake word.
                self._continuous_paused = True
                self._resume_wake_word()
                self.set_status(
                    "Continuous listening paused \u2014 say the wake word",
                    busy=False)
                self.chat.add_message(SystemMessage(
                    "Continuous listening paused after 60s of silence \u2014 "
                    "say the wake word to resume."))
                return
            self._handle_transcript(text)
            return
        if payload.startswith("transcript:"):
            self._handle_transcript(payload[len("transcript:"):])
            if not self._continuous_enabled:
                self._resume_wake_word()

    def _handle_transcript(self, text):
        # A finished transcript is a command: send it, don't stage it.
        self._recording = False
        text = (text or "").strip()
        if not text:
            self.set_status("No speech detected — try again", busy=False)
            if self._continuous_enabled and not self._continuous_paused:
                self._schedule_continuous_listen()
            return
        self.input_field.setPlainText(text)
        self.input_field.setFocus()
        self.send()

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
        is_watcher = request_text.startswith("FILE WATCHER:")
        label = "[watcher]" if is_watcher else "[scheduled]"
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
    app.setStyle("Fusion")
    # A terminal cursor is a steady block, not a blinking hairline.
    app.setCursorFlashTime(0)
    app.setFont(terminal_font())
    theme = read_theme_name()
    try:
        app.setStyleSheet(theme_stylesheet(theme))
    except OSError as exc:
        print(f"[gui] theme {theme!r} not loaded: {exc}")
    app.setQuitOnLastWindowClosed(False)  # app lives in the tray
    window = AgentWindow()
    window.show_greeting()
    window.show()
    window.start_services()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
