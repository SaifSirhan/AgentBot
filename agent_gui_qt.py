"""
Kestrel GUI — PySide6 port.

Phase 4a: slash commands, settings overlay, memory reset — ported from
the old Tkinter GUI. Phase 4b: per-turn collapsible activity
chips showing the step log. Phase 5a: system tray, F9 hotkey (stub) and
scheduler notifications. Phase 5b: voice (F9 push-to-talk STT, auto-speak TTS)
and a scheduler queue so overlapping turns no longer race. Phase 5c: TTS
warmup at startup, proactive file-watcher callbacks, and file attachments
(paperclip picker, chip strip, contents inlined into the agent turn). Phase
5d: wired the previously-dead sidebar/header buttons (Tasks, Index docs,
Activity log, Copy all, Export chat, New chat), added a visible mic button
(click-to-toggle, in sync with F9), and replaced Unicode glyphs with SVG
icons rendered via QtSvg.

This is the live GUI. The old Tkinter GUI (agent_gui.py) has been removed.

Palette values are inlined from gui_widgets.py (the mint accent has been
shifted greener for this port). That module imports tkinter, so it must NOT
be imported here — the Qt port stays free of tkinter. The old Tkinter GUI is
not imported either; the logic shared with it (the hallucination guard, the
slash command dispatch) is replicated below as pure Python. scheduler.py is
plain-Python and safe to import; the tray is native QSystemTrayIcon.
"""
from __future__ import annotations
import concurrent.futures
import contextlib
import datetime
import faulthandler
import html
import importlib
import io
import json
import math
import os
import re
import sys
import threading
import time
import traceback
from pathlib import Path
from PySide6.QtCore import (
    Qt,
    QBuffer,
    QEasingCurve,
    QEvent,
    QIODevice,
    QObject,
    QPropertyAnimation,
    QRect,
    QRectF,
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
    QImage,
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
    QStackedWidget,
    QSystemTrayIcon,
    QTabWidget,
    QTextEdit,
    QToolButton,
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

import memory
import kali_bus
import screen_copilot
import sprite_bus
import task_ledger
from hud_core import PetWidget, ReactorHud
from window_chrome import FramelessChrome

# --------------------------- palette ---------------------------
# Pure-terminal palette: matte black canvas, lifted slate surfaces, neon cyan
# accent. Kept as constants because a lot of the existing widgets style
# themselves inline (icon tints can't come from QSS); themes/*.qss owns the
# structural and tone rules. These values track
# themes/holographic_dark.qss — the default.
COLOR_BG         = "#0B0E13"
COLOR_BG_ALT     = "#0E1116"
COLOR_SIDEBAR    = "#0B0E13"
COLOR_RAISED     = "#1A1D24"
COLOR_CODE_BG    = "#12161D"
COLOR_INPUT_BG   = "#1A1D24"
COLOR_BORDER     = "#263041"
COLOR_HOVER      = "#1E2632"
COLOR_SELECTED   = "#24303F"
COLOR_TEXT_HI    = "#00E5FF"
COLOR_TEXT_USER  = "#E8F1F7"
COLOR_TEXT_MID   = "#8FA0B0"
COLOR_TEXT_LOW   = "#8FA0B0"

COLOR_WARN       = "#FFC857"
COLOR_DANGER     = "#FF6B81"
COLOR_DANGER_HOVER = "#ff8ea0"

COLOR_ACCENT       = "#00E5FF"
COLOR_ACCENT_HOVER = "#7DF3FF"
COLOR_ACCENT_DIM   = "#0E3A44"

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

# Preferred families, re-pinned by resolve_fonts() once a QApplication exists.
# They are read by inline-stylesheet f-strings at call time, so a name set here
# propagates everywhere; the literal values are only the pre-resolution guess.
FONT_UI = "Inter"
FONT_MONO = "JetBrains Mono"

_UI_FAMILIES = ("Inter", "SF Pro Text", "Noto Sans", "DejaVu Sans", "Roboto")
_MONO_FAMILIES = ("JetBrains Mono", "Cascadia Mono", "Consolas",
                  "DejaVu Sans Mono")
_fonts_resolved = False

HEADER_H = 48
TITLEBAR_H = 34
# Qt has no reverse-video block cursor in QPlainTextEdit; a wide blinking bar at
# the caret is the closest terminal look without custom painting.
CURSOR_FLASH_MS = 530

# The frameless shell is implemented with Win32 native messages, so it exists
# only where those messages do: on Linux/macOS the OS title bar stays and no
# custom bar is built (a half-ported chrome would leave the window undraggable).
_FRAMELESS_SUPPORTED = sys.platform == "win32"

# Phase 6a — right panel geometry + auto-hide breakpoints. MIN_W is far enough
# above HIDE_BELOW that the hidden->shown transition has hysteresis (a resize
# just under 1100 then a fragment back above cannot flap the panel).
RIGHT_PANEL_W = 320
MIN_RIGHT_PANEL_W = 250
HIDE_PANEL_BELOW = 1100
SHOW_PANEL_AT = 1180
TEXT_COLUMN_BASELINE_W = 900
TEXT_COLUMN_W = TEXT_COLUMN_BASELINE_W + 20
NAV_RAIL_W = 104
TASK_WORKSPACE_H = 184
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
LESSONS_INTERVAL_MS = 24 * 60 * 60 * 1000
LESSONS_STARTUP_DELAY_MS = 10 * 60 * 1000

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
    "target": '<circle cx="12" cy="12" r="7.5"/><circle cx="12" cy="12" r="2.5"/>'
              '<path d="M12 1.5v3M12 19.5v3M1.5 12h3M19.5 12h3"/>',
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
    "stop": '<rect x="7" y="7" width="10" height="10" rx="2.2" '
            'fill="{c}" stroke="none"/>',
    "mic": '<rect x="9" y="3" width="6" height="11" rx="3"/>'
           '<path d="M6 11.5a6 6 0 0 0 12 0M12 17.5V21M9 21h6"/>',
    # custom title-bar window controls (frameless chrome)
    "win_min": '<path d="M7 12.5h10"/>',
    "win_max": '<rect x="7" y="7" width="10" height="10" rx="1.6"/>',
    "win_restore": '<rect x="5.5" y="8.5" width="8.5" height="8.5" rx="1.6"/>'
                   '<path d="M9 8.5V6.6a1.1 1.1 0 0 1 1.1-1.1h7.3'
                   'a1.1 1.1 0 0 1 1.1 1.1v7.3a1.1 1.1 0 0 1-1.1 1.1H14"/>',
    "win_close": '<path d="M7.5 7.5l9 9M16.5 7.5l-9 9"/>',
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


def _first_family(candidates, available):
    for family in candidates:
        if family in available:
            return family
    return None


def resolve_fonts() -> tuple[str, str]:
    """Pin FONT_UI / FONT_MONO to families that are actually installed.

    Needs a QApplication (QFontDatabase is app-scoped), so main() and
    AgentWindow call it before any widget is built. A desktop without any
    preferred face downgrades to the platform fixed font, never to a named
    family Qt cannot resolve. With no font database at all — the offscreen
    platform reports none — the preferred names stay, because an empty list
    says nothing about what a real display would resolve.
    """
    global FONT_UI, FONT_MONO, _fonts_resolved
    if _fonts_resolved:
        return FONT_UI, FONT_MONO
    available = set(QFontDatabase.families())
    if available:
        FONT_MONO = (_first_family(_MONO_FAMILIES, available)
                     or QFontDatabase.systemFont(
                         QFontDatabase.SystemFont.FixedFont).family())
        # Terminal-first: with no Inter-class face, keep the monospace body.
        FONT_UI = _first_family(_UI_FAMILIES, available) or FONT_MONO
    _fonts_resolved = True
    return FONT_UI, FONT_MONO


def terminal_font() -> QFont:
    """Platform-safe monospace font for the terminal theme."""
    resolve_fonts()
    font = QFont(FONT_MONO, 11)
    font.setStyleHint(QFont.StyleHint.Monospace)
    return font


def zero_layout(layout):
    """Terminal layout: no outer margin, no inter-widget gap."""
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)
    return layout


_CODE_FENCE_RE = re.compile(r"```([a-zA-Z0-9_+.-]*)\n?(.*?)```", re.DOTALL)


def md_css() -> str:
    """CSS injected into QTextDocument HTML so markdown typography matches the
    design spec (body 15px with breathing room, scaled headers, inline code).

    Built per call, not at import: it interpolates the font family, which only
    becomes final once resolve_fonts() has run against a live QApplication.
    """
    return f"""
p {{ margin: 0 0 12px 0; }}
h1 {{ font-size: 22px; font-weight: 600; margin: 18px 0 10px 0; }}
h2 {{ font-size: 18px; font-weight: 600; margin: 16px 0 8px 0; }}
h3 {{ font-size: 16px; font-weight: 600; margin: 14px 0 6px 0; }}
ul, ol {{ margin: 0 0 12px 0; -qt-list-indent: 1; }}
li {{ margin: 3px 0; }}
code {{ font-family: "{FONT_MONO}"; font-size: 13px; }}
a {{ color: {COLOR_ACCENT}; }}
"""

AGENT_BANNER = (
    "██╗  ██╗███████╗███████╗████████╗██████╗ ███████╗██╗\n"
    "██║ ██╔╝██╔════╝██╔════╝╚══██╔══╝██╔══██╗██╔════╝██║\n"
    "█████╔╝ █████╗  ███████╗   ██║   ██████╔╝█████╗  ██║\n"
    "██╔═██╗ ██╔══╝  ╚════██║   ██║   ██╔══██╗██╔══╝  ██║\n"
    "██║  ██╗███████╗███████║   ██║   ██║  ██║███████╗███████╗\n"
    "╚═╝  ╚═╝╚══════╝╚══════╝   ╚═╝   ╚═╝  ╚═╝╚══════╝╚══════╝"
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


def _tts_is_speaking():
    """Check TTS state without importing Kokoro on a status-only poll."""
    module = sys.modules.get("voice_output")
    if module is None:
        return False
    try:
        return bool(module.is_speaking())
    except Exception:
        return False


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
# holographic_dark is the default; brutalist and crt_green stay selectable and
# are untouched by the new look.
DEFAULT_THEME = "holographic_dark"
THEME_NAMES = ("holographic_dark", "brutalist", "crt_green")


def read_theme_name() -> str:
    try:
        name = str(read_config_file_raw().get("GUI_THEME", DEFAULT_THEME)).strip()
    except Exception:
        name = DEFAULT_THEME
    return name if name in THEME_NAMES else DEFAULT_THEME


def theme_stylesheet(name: str) -> str:
    name = name if name in THEME_NAMES else DEFAULT_THEME
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
    "                     (wake word off until a VEX model is installed)\n"
    "/continuous on|off → hands-free loop; 60s silence pauses it\n\n"
    "Interface:\n\n"
    "/settings          → open the settings overlay\n"
    "/memories          → manage saved memories (global + project)\n"
    "/tasks             → scheduled tasks\n"
    "/index             → index a folder for RAG\n"
    "/export            → export the conversation\n"
    "/copy              → copy the conversation\n"
    "/activity          → toggle the activity log\n"
    "/theme [name]      → switch theme (brutalist | crt_green)\n"
    "/tg-clear          → clear the Telegram panel\n"
    "/clear             → clear the chat and history\n"
    "/task goal         → run a multi-step task with the autonomous loop\n"
    "/autonomous goal   → alias for /task\n\n"
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
    "       e.g.  /map .|40            (no args = the project folder)\n\n"
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
    "/memories",
    "/tasks",
    "/theme",
    "/clear",
    "/new",
    "/task",
    "/autonomous",
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

    def __init__(self, user_input, conversation_history, attached_paths=None,
                 stop_event=None, persist_history=True,
                 copilot_approval_handler=None):
        super().__init__()
        self._user_input = user_input
        self._history = (conversation_history if persist_history
                         else list(conversation_history))
        self._attached_paths = list(attached_paths or [])
        self._stop_event = stop_event
        self._persist_history = persist_history
        self._copilot_approval_handler = copilot_approval_handler

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        arm_turn_watchdog()
        try:
            self._run_turn()
        finally:
            end_turn_watchdog()

    def _run_turn(self):
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
                    effective_input, self._history, on_step=self.step.emit,
                    stop_event=self._stop_event,
                    copilot_approval_handler=self._copilot_approval_handler)
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
            if self._persist_history:
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
    # Callers that swap the glyph (send <-> stop) update these instead of the
    # closure so hover recolours the current icon, not the original one.
    btn._icon_name = icon
    btn._icon_color = COLOR_TEXT_MID
    btn._icon_hover = COLOR_TEXT_HI
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
        btn.setIcon(_svg_icon(btn._icon_name, icon_size, btn._icon_hover))
        _orig_enter(e)

    def _leave(e):
        btn.setIcon(_svg_icon(btn._icon_name, icon_size, btn._icon_color))
        _orig_leave(e)

    btn.enterEvent = _enter
    btn.leaveEvent = _leave
    return btn


class ScanlineLayer(QWidget):
    """Faint horizontal holographic lines behind the chat body.

    Parented to the chat viewport beside the background HUD and lowered under
    the message body, which is how the HUD already stays behind text.
    """

    PERIOD = 4
    ALPHA = 10
    WIDTH = 1

    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("scanlineLayer")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    def paintEvent(self, event):
        painter = QPainter(self)
        if not painter.isActive():
            return
        color = QColor(COLOR_ACCENT)
        color.setAlpha(self.ALPHA)
        pen = QPen(color)
        pen.setWidth(self.WIDTH)
        painter.setPen(pen)
        y = 0
        while y < self.height():
            painter.drawLine(0, y, self.width(), y)
            y += self.PERIOD
        painter.end()


class TitleBar(QFrame):
    """Thin custom window bar for the frameless shell: drag surface, title and
    the three window controls.

    Dragging runs through the native move loop (`startSystemMove`), which is
    what keeps Aero Snap working when the bar is pulled against a screen edge.
    Resize borders are re-claimed in AgentWindow.nativeEvent instead — a bare
    FramelessWindowHint would have dropped WS_THICKFRAME and lost them, plus
    taskbar-aware maximise, in one go.
    """

    def __init__(self, window):
        super().__init__(window)
        self.setObjectName("titleBar")
        self.setFixedHeight(TITLEBAR_H)
        self._window = window

        row = QHBoxLayout(self)
        row.setContentsMargins(10, 3, 4, 3)
        row.setSpacing(4)

        mark = QLabel("◈")
        mark.setProperty("tone", "hi")
        row.addWidget(mark)

        self.title_label = QLabel(window.windowTitle())
        self.title_label.setObjectName("titleBarText")
        self.title_label.setMinimumWidth(0)
        self.title_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        row.addWidget(self.title_label)
        row.addStretch(1)

        self.min_button = self._control("win_min", "Minimize",
                                        "titleBarButton", 12)
        self.min_button.clicked.connect(self._window.showMinimized)
        row.addWidget(self.min_button)

        self.max_button = self._control("win_max", "Maximize",
                                        "titleBarButton", 12)
        self.max_button.clicked.connect(self._window.toggle_maximize)
        row.addWidget(self.max_button)

        self.close_button = self._control("win_close", "Close",
                                         "titleBarClose", 12)
        # close() keeps the existing contract: hide to tray, do not exit.
        self.close_button.clicked.connect(self._window.close)
        row.addWidget(self.close_button)

    def _control(self, icon, tooltip, object_name, icon_size) -> QPushButton:
        btn = _icon_button(icon, tooltip, size=TITLEBAR_H - 10,
                           icon_size=icon_size)
        btn.setObjectName(object_name)
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        if object_name == "titleBarClose":
            # The hover closure reads this attribute, so the glyph reddens
            # without a stylesheet rule the button's inline style would veto.
            btn._icon_hover = COLOR_DANGER
        return btn

    def sync_state(self):
        """Mirror window title and maximised state after any window change."""
        maximised = self._window.isMaximized()
        name = "win_restore" if maximised else "win_max"
        if self.max_button._icon_name != name:
            self.max_button._icon_name = name
            self.max_button.setToolTip("Restore" if maximised else "Maximize")
            self.max_button.setIcon(
                _svg_icon(name, self.max_button.iconSize().width(),
                          self.max_button._icon_color))
        self._refresh_title()

    def _refresh_title(self):
        text = self._window.windowTitle() or ""
        width = self.title_label.width()
        if width > 0:
            text = QFontMetrics(self.title_label.font()).elidedText(
                text, Qt.TextElideMode.ElideRight, width)
        self.title_label.setText(text)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refresh_title()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self._window.windowHandle()
            if handle is not None and handle.startSystemMove():
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._window.toggle_maximize()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


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
    html = re.sub(r"<style type=\"text/css\">.*?</style>", f"<style>{md_css()}</style>",
                  html, count=1, flags=re.DOTALL)
    return html


_SPEAKER_PREFIX = {
    "user": '<span style="color: #ffffff; font-weight: 700;">You:</span>&#160;',
    # No colour: the label inherits the active theme's bright primary.
    "agent": '<span style="font-weight: 700;">Kestrel:</span>&#160;',
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
        # Ignored, not Fixed: the column must accept whatever width the layout
        # hands it. Under a Fixed policy it demanded the label's unwrapped
        # width, and the stretch pair below then centred it mid-row.
        self._column.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Minimum
        )
        self._col_layout = QVBoxLayout(self._column)
        self._col_layout.setContentsMargins(0, 0, 0, 0)
        self._col_layout.setSpacing(0)

        row = zero_layout(QHBoxLayout(self))
        row.addWidget(self._column, 1)

    def _height_at_width(self):
        """Height the column needs at the width it actually gets.

        Qt asks a word-wrapped label for its hint at the *minimum* width, so a
        one-line message reported 252px and a long chat grew a scroll void
        below the last message. Measuring at the real width removes it.
        """
        parent = self.parentWidget()
        width = 0
        for candidate in (self._column.width(), self.width(),
                          parent.width() if parent else 0):
            if candidate > 1:
                width = candidate
                break
        if width <= 1:
            # Nothing has sized this row yet. The old width-agnostic hint is
            # better than zero, which would stop the body growing at all; the
            # exact number arrives with the first resize.
            return self._col_layout.sizeHint().height()
        total = 0
        for index in range(self._col_layout.count()):
            widget = self._col_layout.itemAt(index).widget()
            if widget is None:
                continue
            hinted = (widget.heightForWidth(width)
                      if widget.hasHeightForWidth()
                      else widget.sizeHint().height())
            total += max(hinted, widget.minimumSizeHint().height())
        return total

    def sizeHint(self):
        return QSize(0, self._height_at_width())

    def minimumSizeHint(self):
        # Width 0: QWidgetItem promotes this into the layout's hint, and the
        # column's real width comes from the viewport, never from the label.
        return QSize(0, self._height_at_width())

    def resizeEvent(self, event):
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
        banner_font = QFont(FONT_MONO)
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


class TaskLedgerCard(MessageBase):
    """Persistent manual task ledger embedded in the chat transcript."""

    focus_requested = Signal(str)
    action_requested = Signal(str, str)

    def __init__(self):
        super().__init__()
        self.focused_task_id = None
        self.task_buttons = []
        self._tasks = []
        self._error = ""

        frame = QFrame()
        frame.setObjectName("taskLedgerCard")
        frame.setStyleSheet(
            f"""
            QFrame#taskLedgerCard {{
                background: {COLOR_RAISED}; border: 1px solid {COLOR_BORDER};
                border-radius: 12px;
            }}
            QFrame#taskLedgerCard QLabel {{ background: transparent; }}
            """
        )
        outer = zero_layout(QVBoxLayout(frame))
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(8)

        header = QHBoxLayout()
        header.setSpacing(8)
        title = QLabel("TASK LEDGER")
        title.setTextFormat(Qt.TextFormat.PlainText)
        title.setStyleSheet(
            f'color: {COLOR_TEXT_MID}; font-family: "{FONT_MONO}"; '
            "font-size: 9px; font-weight: bold;")
        header.addWidget(title)
        self.summary_label = QLabel("0 open · 0 active · 0 done")
        self.summary_label.setTextFormat(Qt.TextFormat.PlainText)
        self.summary_label.setStyleSheet(
            f'color: {COLOR_TEXT_LOW}; font-family: "{FONT_MONO}"; '
            "font-size: 9px;")
        header.addWidget(self.summary_label)
        header.addStretch(1)
        self.add_button = QPushButton("Add task")
        self.add_button.setProperty("action", "add")
        self.add_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_button.setStyleSheet(
            f"""
            QPushButton {{
                background: {COLOR_ACCENT_DIM}; border: 1px solid {COLOR_ACCENT};
                border-radius: 7px; color: {COLOR_TEXT_HI}; padding: 4px 9px;
                font-family: "{FONT_UI}"; font-size: 10px;
            }}
            QPushButton:hover {{ background: {COLOR_HOVER}; }}
            """
        )
        self.add_button.clicked.connect(
            lambda: self.action_requested.emit("add", ""))
        header.addWidget(self.add_button)
        outer.addLayout(header)

        self.error_label = QLabel()
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet(
            f"color: {COLOR_WARN}; font-family: \"{FONT_UI}\"; font-size: 10px;")
        self.error_label.hide()
        outer.addWidget(self.error_label)

        self._list_host = QWidget()
        self._list_host.setStyleSheet("background: transparent;")
        self._list_layout = zero_layout(QVBoxLayout(self._list_host))
        self._list_layout.setSpacing(4)
        self._list_layout.addStretch(1)
        self._list_scroll = QScrollArea()
        self._list_scroll.setObjectName("taskLedgerList")
        self._list_scroll.setWidgetResizable(True)
        self._list_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._list_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list_scroll.setStyleSheet(
            "QScrollArea#taskLedgerList, QScrollArea#taskLedgerList > QWidget "
            "{ background: transparent; border: none; }")
        self._list_scroll.setWidget(self._list_host)
        self._list_scroll.setFixedHeight(40)
        outer.addWidget(self._list_scroll)
        self._col_layout.addWidget(frame)

    def set_ledger_state(self, state):
        self._tasks = list(state.get("tasks") or [])
        self.focused_task_id = state.get("focused_task_id")
        self._error = str(state.get("error") or "")
        open_count = sum(task["status"] != "done" for task in self._tasks)
        active_count = sum(task["status"] == "active" for task in self._tasks)
        done_count = sum(task["status"] == "done" for task in self._tasks)
        self.summary_label.setText(
            f"{open_count} open · {active_count} active · {done_count} done")
        self.error_label.setText(self._error)
        self.error_label.setVisible(bool(self._error))
        self.add_button.setEnabled(not self._error)
        self._rebuild_tasks()

    def _rebuild_tasks(self):
        while self._list_layout.count():
            item = self._list_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.task_buttons = []

        if not self._tasks:
            empty = QLabel("No tasks yet. Add one to start a persistent ledger.")
            empty.setTextFormat(Qt.TextFormat.PlainText)
            empty.setWordWrap(True)
            empty.setStyleSheet(
                f'color: {COLOR_TEXT_LOW}; font-family: "{FONT_UI}"; '
                "font-size: 11px;")
            self._list_layout.addWidget(empty)
            self._list_layout.addStretch(1)
            self._list_scroll.setFixedHeight(40)
            return

        for task in self._tasks:
            row = QFrame()
            row.setObjectName("taskLedgerRow")
            focused = task["id"] == self.focused_task_id
            row.setStyleSheet(
                f"""
                QFrame#taskLedgerRow {{
                    background: {COLOR_CODE_BG};
                    border: 1px solid {COLOR_ACCENT if focused else COLOR_BORDER};
                    border-radius: 8px;
                }}
                """
            )
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(6, 3, 6, 3)
            row_layout.setSpacing(4)

            status = {"planned": "OPEN", "active": "ACTIVE", "done": "DONE"}[
                task["status"]]
            title = QPushButton(f"{'› ' if focused else ''}{status}  {task['title']}")
            title.setProperty("taskId", task["id"])
            title.setProperty("focused", focused)
            title.setToolTip(task["title"])
            title.setCursor(Qt.CursorShape.PointingHandCursor)
            title.setSizePolicy(
                QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            title.setMinimumWidth(0)
            title.setFixedHeight(28)
            title.setStyleSheet(
                f"""
                QPushButton {{
                    background: transparent; border: none; text-align: left;
                    color: {COLOR_TEXT_USER}; padding: 2px 4px;
                    font-family: "{FONT_UI}"; font-size: 11px;
                }}
                QPushButton:hover {{ color: {COLOR_TEXT_HI}; }}
                QPushButton[focused="true"] {{ color: {COLOR_TEXT_HI}; }}
                """
            )
            title.clicked.connect(
                lambda _checked=False, task_id=task["id"]:
                self.focus_requested.emit(task_id))
            row_layout.addWidget(title, 1)
            self.task_buttons.append(title)

            actions = []
            if task["status"] == "planned":
                actions.append(("Start", "start"))
            if task["status"] != "done":
                actions.append(("Done", "done"))
            else:
                actions.append(("Reopen", "reopen"))
            actions.append(("Delete", "delete"))
            for label, action in actions:
                button = QPushButton(label)
                button.setProperty("action", action)
                button.setProperty("taskId", task["id"])
                button.setEnabled(not self._error)
                button.setCursor(Qt.CursorShape.PointingHandCursor)
                button.setFixedHeight(24)
                button.setStyleSheet(
                    f"""
                    QPushButton {{
                        background: {COLOR_RAISED}; border: 1px solid {COLOR_BORDER};
                        border-radius: 6px; color: {COLOR_TEXT_MID}; padding: 2px 7px;
                        font-family: "{FONT_UI}"; font-size: 9px;
                    }}
                    QPushButton:hover {{ color: {COLOR_TEXT_HI};
                        background: {COLOR_HOVER}; }}
                    """
                )
                button.clicked.connect(
                    lambda _checked=False, name=action, task_id=task["id"]:
                    self.action_requested.emit(name, task_id))
                row_layout.addWidget(button)
            self._list_layout.addWidget(row)

        self._list_layout.addStretch(1)
        self._list_scroll.setFixedHeight(min(180, max(40, len(self._tasks) * 38)))


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


class CopilotApprovalCard(QFrame):
    resolved = Signal(bool)

    def __init__(self, payload: dict):
        super().__init__()
        self.setObjectName("copilotApproval")
        self._payload = payload
        self._resolved = False
        self.setStyleSheet(
            f"QFrame#copilotApproval {{ background: {COLOR_CODE_BG};"
            f" border: 1px solid {COLOR_ACCENT}; border-radius: 8px; }}"
            f"QLabel {{ background: transparent; }}"
        )
        col = zero_layout(QVBoxLayout(self))
        col.setContentsMargins(12, 10, 12, 10)
        col.setSpacing(5)
        header = QLabel("APPROVE PLAN // ONE DECISION FOR UP TO FIVE ACTIONS")
        header.setWordWrap(True)
        header.setStyleSheet(
            f"color: {COLOR_ACCENT}; font-family: '{FONT_MONO}';"
            "font-size: 10px; font-weight: bold;")
        col.addWidget(header)
        target = payload.get("target")
        if not isinstance(target, dict):
            target = {}
        actions = payload.get("allowed_actions")
        action_text = ", ".join(str(item) for item in actions) if isinstance(
            actions, (list, tuple, set, frozenset)) else str(actions or "")
        for text in (
            f"Goal: {payload.get('goal', '')}",
            f"Target: {target.get('title', '')} — "
            f"{target.get('process', '')} (PID {target.get('pid', '')}, "
            f"HWND {target.get('hwnd', '')})",
            f"Actions: {action_text}; maximum {payload.get('max_steps', 5)}",
            str(payload.get("typing_rule", "")),
            str(payload.get("screenshot_disclosure", "")),
            str(payload.get("denial_or_timeout",
                            "Denial or timeout executes nothing.")),
        ):
            label = QLabel(text)
            label.setWordWrap(True)
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setStyleSheet(
                f"color: {COLOR_TEXT_USER}; font-family: '{FONT_UI}';"
                "font-size: 10px;")
            col.addWidget(label)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet(
            f"color: {COLOR_TEXT_MID}; font-family: '{FONT_MONO}'; font-size: 10px;")
        col.addWidget(self.status_label)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.approve_btn = QPushButton("Approve Plan")
        self.deny_btn = QPushButton("Deny")
        for button in (self.approve_btn, self.deny_btn):
            button.setFixedHeight(28)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.installEventFilter(self)
            button.setStyleSheet(
                f"QPushButton {{ background: {COLOR_HOVER};"
                f" color: {COLOR_TEXT_USER}; border: 1px solid {COLOR_BORDER};"
                f" border-radius: 6px; padding: 4px 12px;"
                f" font-family: '{FONT_UI}'; font-size: 10px; }}"
                f"QPushButton:hover {{ border-color: {COLOR_ACCENT}; }}"
            )
        self.approve_btn.setStyleSheet(
            f"QPushButton {{ background: {COLOR_ACCENT_DIM};"
            f" color: {COLOR_TEXT_USER}; border: 1px solid {COLOR_ACCENT};"
            f" border-radius: 6px; padding: 4px 12px;"
            f" font-family: '{FONT_UI}'; font-size: 10px; font-weight: bold; }}"
        )
        self.approve_btn.clicked.connect(lambda: self.resolve(True))
        self.deny_btn.clicked.connect(lambda: self.resolve(False))
        row.addWidget(self.approve_btn)
        row.addWidget(self.deny_btn)
        row.addStretch(1)
        col.addLayout(row)

        try:
            timeout_ms = max(
                1, int(float(payload.get("timeout_seconds", 60)) * 1000))
        except (TypeError, ValueError):
            timeout_ms = 60_000
        self._remaining_ms = timeout_ms
        self._timer = QTimer(self)
        self._timer.setInterval(min(1000, timeout_ms))
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self._update_countdown()

    def _update_countdown(self):
        seconds = max(1, (self._remaining_ms + 999) // 1000)
        self.status_label.setText(
            f"Waiting for your decision… {seconds}s. Enter does not approve.")

    def _tick(self):
        if self._resolved:
            return
        self._remaining_ms -= self._timer.interval()
        if self._remaining_ms <= 0:
            self._expire()
            return
        self._update_countdown()

    def _expire(self):
        self.resolve(False)

    def resolve(self, approved: bool):
        if self._resolved:
            return
        self._resolved = True
        self._timer.stop()
        self.approve_btn.setEnabled(False)
        self.deny_btn.setEnabled(False)
        self.status_label.setText(
            "Approved." if approved else "Denied — no action will start.")
        self.resolved.emit(bool(approved))

    def eventFilter(self, watched, event):
        if (event.type() == QEvent.Type.KeyPress
                and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)):
            return True
        return super().eventFilter(watched, event)


class CopilotApprovalBridge(QObject):
    requested = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._lock = threading.Lock()
        self._pending = {}
        self._closed = False

    def __call__(self, request: dict):
        future = concurrent.futures.Future()
        request_id = str(request.get("request_id") or "")
        with self._lock:
            closed = self._closed
            if not closed:
                self._pending[request_id] = future
        if closed:
            future.set_result(False)
            return future
        payload = dict(request)
        payload["future"] = future

        def cleanup(_):
            with self._lock:
                self._pending.pop(request_id, None)

        future.add_done_callback(cleanup)
        self.requested.emit(payload)
        return future

    def resolve(self, payload, approved: bool):
        request_id = str(payload.get("request_id") or "")
        with self._lock:
            future = self._pending.get(request_id)
        if future is not None and not future.done():
            with contextlib.suppress(Exception):
                future.set_result(bool(approved))

    def resolve_all(self, approved: bool):
        with self._lock:
            futures = list(self._pending.values())
        for future in futures:
            if not future.done():
                with contextlib.suppress(Exception):
                    future.set_result(bool(approved))

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            futures = list(self._pending.values())
        self.resolve_all(False)


# ----------------------------------------------------------------------
# Chat view with scroll-lock
# ----------------------------------------------------------------------
def _paste_image_path():
    """Absolute path where a clipboard screenshot is saved for the agent."""
    try:
        return get_config_module().memory_path("temp_paste.png")
    except Exception:
        base = (os.environ.get("LOCALAPPDATA")
                or str(Path.home() / "AppData" / "Local"))
        return os.path.join(base, "Kestrel", "AgentMemory", "temp_paste.png")


# No VEX ONNX model is trained yet. F9 push-to-talk stays fully functional;
# flip this only once a verified model ships.
WAKE_WORD_STARTUP_ENABLED = False

HISTORY_LIMIT = 50


class CommandHistory:
    """Up/Down recall over submitted prompts, with shell semantics.

    `entries` is oldest-first and the cursor sits one past the newest entry
    while the user types, so Down from the top of a recall returns the draft they
    had in place instead of dropping it. Consecutive repeats are not stored twice.
    """

    def __init__(self, limit=HISTORY_LIMIT):
        self.limit = limit
        self.entries = []
        self._index = 0
        self._draft = ""

    def add(self, text):
        """Record a submission and drop back to the bottom of the list."""
        text = (text or "").strip()
        if not text:
            return
        if text != (self.entries[-1] if self.entries else None):
            self.entries.append(text)
            if self.limit > 0:
                del self.entries[:-self.limit]
        self.reset()

    def reset(self):
        self._index = len(self.entries)
        self._draft = ""

    def prev(self, current=""):
        """One entry older, or None when already at the oldest."""
        if self._index == 0:
            return None
        if self._index == len(self.entries):
            self._draft = current
        self._index -= 1
        return self.entries[self._index]

    def next(self, current=""):
        """One entry newer, or None when already back at the live prompt."""
        if self._index >= len(self.entries):
            return None
        self._index += 1
        return self._draft if self._index == len(self.entries) else self.entries[self._index]


class TerminalInput(QPlainTextEdit):
    """Prompt editor with inline ghost completion and Up/Down history.

    Prefix matches paint the missing suffix after the caret, so Right accepts
    by appending it. Subsequence ("fuzzy") matches cannot be appended without
    corrupting the typed query, so they paint the full command as a dim hint
    and Right replaces the prompt with it. Tab cycles either kind.

    Up/Down walk previously submitted prompts, but only from the first or last
    line so a multi-line draft keeps ordinary cursor movement.
    """

    GHOST_OPACITY = 0.6
    MAX_FUZZY_MATCHES = 8

    def __init__(self, parent=None):
        super().__init__(parent)
        self._ghost = ""
        self._matches = []
        self._match_index = 0
        self._replace = False
        self.history = CommandHistory()
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

    def _history_wants(self, key) -> bool:
        """Take the arrow only when it cannot be moving the caret.

        A multi-line prompt keeps normal cursor movement between its lines, so
        recall fires only from the first block (Up) or the last (Down).
        """
        if not self.history.entries:
            return False
        block = self.textCursor().blockNumber()
        if key == Qt.Key.Key_Up:
            return block == 0
        return block == self.document().blockCount() - 1

    def _try_history(self, event) -> bool:
        """Recall on Up/Down. True once the arrow has been consumed."""
        key = event.key()
        if not self._history_wants(key):
            return False
        current = self.toPlainText()
        text = (self.history.prev(current) if key == Qt.Key.Key_Up
                else self.history.next(current))
        if text is None:
            # Nothing left in that direction: fall through to normal movement
            # rather than beeping at the end of the list.
            return False
        self.setPlainText(text)
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.setTextCursor(cursor)
        event.accept()
        return True

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
        if event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            if self._try_history(event):
                return
        elif (event.text().strip()
                and not event.modifiers() & (
                    Qt.KeyboardModifier.ControlModifier
                    | Qt.KeyboardModifier.AltModifier
                    | Qt.KeyboardModifier.MetaModifier)):
            # A fresh keystroke abandons whatever was recalled.
            self.history.reset()
        super().keyPressEvent(event)

    # -- context menu ---------------------------------------------------
    def _build_context_menu(self):
        """Explicit menu: the platform menu's actions are no-ops here."""
        menu = QMenu(self)
        has_selection = self.textCursor().hasSelection()

        cut = menu.addAction("Cut")
        cut.triggered.connect(self.cut)
        cut.setEnabled(has_selection)

        copy = menu.addAction("Copy")
        copy.triggered.connect(self.copy)
        copy.setEnabled(has_selection)

        mime = QApplication.clipboard().mimeData()
        paste = menu.addAction("Paste")
        paste.triggered.connect(self.paste)
        paste.setEnabled(bool(mime) and (
            mime.hasText() or mime.hasUrls() or mime.hasImage()))

        select_all = menu.addAction("Select All")
        select_all.triggered.connect(self.selectAll)
        return menu

    def contextMenuEvent(self, event):
        menu = self._build_context_menu()
        menu.exec(event.globalPos())
        event.accept()

    # -- clipboard: files and screenshots -------------------------------
    @staticmethod
    def _clipboard_paths(mime):
        if mime is None or not mime.hasUrls():
            return []
        paths = []
        for url in mime.urls():
            path = url.toLocalFile()
            if path:
                paths.append(os.path.normpath(path))
        return paths

    def _insert_paths(self, paths):
        text = ", ".join(paths)
        cursor = self.textCursor()
        start = (cursor.selectionStart() if cursor.hasSelection()
                 else cursor.position())
        plain = self.toPlainText()
        if start > 0 and plain[start - 1] not in " \t\n,;":
            text = ", " + text
        self.insertPlainText(text)
        self.ensureCursorVisible()

    def insertFromMimeData(self, source):
        """Paste files as paths and screenshots as a saved PNG path."""
        paths = self._clipboard_paths(source)
        if paths:
            self._insert_paths(paths)
            return
        if source is not None and source.hasImage():
            image = source.imageData()
            if isinstance(image, QPixmap):
                image = image.toImage()
            if isinstance(image, QImage) and not image.isNull():
                path = str(_paste_image_path())
                try:
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    if image.save(path, "PNG"):
                        self._insert_paths([os.path.normpath(path)])
                        return
                except OSError:
                    pass
        super().insertFromMimeData(source)

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


class _SpriteRelay(QObject):
    """Marshals tool-thread sprite commands onto the GUI thread.

    Tools run on a worker; the pet is a widget. AutoConnection picks the right
    hop on both sides — direct when the caller is already on the GUI thread,
    queued when it is not — so `sprite_bus` never touches a QWidget itself.
    """

    command = Signal(str, dict)

    def forward(self, action, params):
        self.command.emit(action, params)


class _KaliRelay(QObject):
    """Marshals a tool-thread approval request onto the GUI thread.

        The tool loop blocks while it asks (`kali_bridge` runs on the worker), so
        `request` hands the plan and a `finish` callback to the GUI thread and
        blocks on an Event for at most `timeout` seconds. `event.wait` returns
        False when it times out, so a plan whose dialog was never shown, was
        torn down, or was answered only after the wait expired is a deny: the
        answer can never arrive after the question.

        `finish` is single-use and per-call, so a second click, a late
        callback, or one request's answer cannot flip this one or land on a
        later request.
    """

    approval_requested = Signal(object, object)

    def request(self, plan, timeout):
        event = threading.Event()
        result = {"approved": False}
        answered = threading.Lock()

        def finish(approved):
            with answered:
                if event.is_set():
                    return
                result["approved"] = bool(approved)
                event.set()

        # `event.wait` raises ValueError for a negative timeout, and would
        # block forever on None; a caller that cannot wait the way it asked
        # gets the same immediate deny every other malformed path gives.
        try:
            timeout = max(0.0, float(timeout))
        except (TypeError, ValueError):
            return False
        try:
            self.approval_requested.emit(dict(plan), finish)
        except Exception:
            # A plan that cannot be copied never reaches the user, so it is a
            # deny now rather than a timeout later. (A slot that raises on the
            # GUI thread cannot be seen from here; Qt prints it and the wait
            # below still expires, which is also a deny.)
            event.set()
        return event.wait(timeout) and result["approved"]


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
    MESSAGE_GAP = 16

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
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.viewport().setAttribute(
            Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._body.setAttribute(
            Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.viewport().setAutoFillBackground(False)
        self._body.setAutoFillBackground(False)
        self.setStyleSheet(
            "QScrollArea#chatScroll, QWidget#chatViewport, QWidget#chatContent, "
            "QWidget#chatContent QWidget, QWidget#chatContent QLabel "
            "{ background: transparent; }")
        self.scanlines = ScanlineLayer(self.viewport())
        self.scanlines.setGeometry(
            0, 0, self.viewport().width(), self.viewport().height())
        self.scanlines.lower()
        # Kali execution asks for consent through kali_bus; this relay is the
        # only path to the dialog, so tearing it down means "deny".
        self._kali_relay = _KaliRelay(self)
        self._kali_relay.approval_requested.connect(self._show_kali_approval)
        kali_request = self._kali_relay.request
        kali_bus.register(kali_request)
        self.destroyed.connect(lambda *_: kali_bus.unregister(kali_request))
        self._body.raise_()

        self._pinned = True  # follow new content while at the bottom
        self.verticalScrollBar().valueChanged.connect(self._on_scroll)
        self.verticalScrollBar().rangeChanged.connect(self._on_range_changed)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        layer = getattr(self, "scanlines", None)
        if layer is not None:
            layer.resize(self.viewport().size())
            layer.lower()
            self._body.raise_()

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
        # One consistent MESSAGE_GAP block gap, never a trailing gap.
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
        # The message reports its height at the width it will get, which is not
        # known until the body lays it out. Nudge the chain so the body grows
        # now instead of waiting for a resize.
        widget.updateGeometry()
        self._body.updateGeometry()
        self._layout.invalidate()

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

            def finish_fade(w=widget, a=anim):
                try:
                    a.stop()
                    w.setGraphicsEffect(None)
                except RuntimeError:
                    pass
                if getattr(w, "_fade_anim", None) is a:
                    w._fade_anim = None
                a.deleteLater()

            anim.finished.connect(finish_fade)
            anim.start()
            widget._fade_anim = anim  # keep a reference alive

        if was_pinned:
            self._pinned = True
            QTimer.singleShot(0, self._scroll_to_bottom)

    @staticmethod
    def _discard_fade(widget: QWidget):
        anim = getattr(widget, "_fade_anim", None)
        if anim is not None:
            try:
                anim.stop()
            except RuntimeError:
                pass
            widget._fade_anim = None

    def remove_message(self, widget: QWidget):
        """Remove a message widget and its transcript entry."""
        if widget is None:
            return
        raw = getattr(widget, "raw_text", None)
        if raw is not None:
            kind = "user" if isinstance(widget, UserMessage) else "agent"
            try:
                self.chat_log.remove((kind, raw))
            except ValueError:
                pass
        gap = self._gap_items.pop(id(widget), None)
        if gap is not None:
            self._layout.removeItem(gap)
        self._layout.removeWidget(widget)
        self._discard_fade(widget)
        widget.setParent(None)
        widget.deleteLater()

    def clear(self):
        """Remove every message widget and reset the recorded log."""
        while self._layout.count() > 1:
            item = self._layout.takeAt(0)
            w = item.widget()
            if w is not None:
                self._discard_fade(w)
                w.setParent(None)
                w.deleteLater()
        self._gap_items.clear()
        self.chat_log.clear()

    def _show_kali_approval(self, plan, finish):
        """The one dialog for Kali execution. Default No; every failure is No.

        Only the dialog itself is guarded: `finish` just records an answer for
        the waiting worker (single lock, single assignment), and letting its
        failure surface beats catching it and calling `finish(False)` on top
        of a decision that may already have been delivered.
        """
        if plan is None:
            plan = {}
        try:
            reply = QMessageBox.question(
                self,
                "Kali lab approval",
                kali_bus.format_approval(plan),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
        except Exception:
            finish(False)
            return
        finish(reply == QMessageBox.StandardButton.Yes)


class NavRail(QFrame):
    DESTINATIONS = (
        ("Kestrel", "chat"),
        ("Tasks", "tasks"),
        ("Files", "export"),
        ("Voice", "pulse"),
        ("Memory", "clipboard"),
        ("Settings", "settings"),
    )

    def __init__(self):
        super().__init__()
        self.setObjectName("navRail")
        self.setFixedWidth(NAV_RAIL_W)
        self.setStyleSheet(
            f"QFrame#navRail {{ background: {COLOR_BG_ALT}; "
            f"border-right: 1px solid {COLOR_BORDER}; }}")
        layout = zero_layout(QVBoxLayout(self))
        layout.setContentsMargins(8, 14, 8, 12)
        layout.setSpacing(6)

        brand = QLabel("◈")
        brand.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand.setStyleSheet(
            f"color: {COLOR_ACCENT}; font-size: 25px; "
            f"font-family: \"{FONT_UI}\"; background: transparent;")
        layout.addWidget(brand)
        layout.addSpacing(12)

        self.buttons = {}
        for label, icon in self.DESTINATIONS:
            button = QToolButton()
            button.setText(label)
            button.setIcon(_svg_icon(icon, 20, COLOR_TEXT_MID))
            button.setIconSize(QSize(20, 20))
            button.setToolButtonStyle(
                Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setFixedHeight(58)
            button.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            button.setStyleSheet(
                f"""
                QToolButton {{
                    background: transparent; border: none; border-radius: 10px;
                    color: {COLOR_TEXT_MID}; font-family: "{FONT_UI}";
                    font-size: 10px; padding: 5px 0;
                }}
                QToolButton:hover {{ background: {COLOR_HOVER}; color: {COLOR_ACCENT}; }}
                QToolButton:pressed {{ background: {COLOR_SELECTED}; color: {COLOR_ACCENT}; }}
                """
            )
            layout.addWidget(button)
            self.buttons[label] = button
        layout.addStretch(1)

        version = QLabel("VEX // 01")
        version.setAlignment(Qt.AlignmentFlag.AlignCenter)
        version.setStyleSheet(
            f"color: {COLOR_TEXT_LOW}; font-family: \"{FONT_MONO}\"; "
            f"font-size: 8px; background: transparent;")
        layout.addWidget(version)


class TaskWorkspace(QFrame):
    focus_requested = Signal(str)

    def __init__(self):
        super().__init__()
        self.setObjectName("taskWorkspace")
        self.setFixedHeight(TASK_WORKSPACE_H)
        self.setStyleSheet(
            f"""
            QFrame#taskWorkspace {{
                background: {COLOR_CODE_BG}; border: 1px solid {COLOR_BORDER};
                border-radius: 12px;
            }}
            QFrame#taskWorkspace QLabel {{ background: transparent; }}
            """
        )
        layout = zero_layout(QHBoxLayout(self))
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(14)

        live = QWidget()
        live_layout = zero_layout(QVBoxLayout(live))
        live_layout.setSpacing(5)
        heading = QLabel("CURRENT TASK")
        heading.setStyleSheet(
            f"color: {COLOR_TEXT_MID}; font-family: \"{FONT_MONO}\"; "
            "font-size: 9px; font-weight: bold;")
        live_layout.addWidget(heading)

        self.task_label = QLabel("No active task")
        self.task_label.setWordWrap(True)
        self.task_label.setStyleSheet(
            f"color: {COLOR_TEXT_USER}; font-family: \"{FONT_UI}\"; "
            "font-size: 14px; font-weight: bold;")
        live_layout.addWidget(self.task_label)

        self.steps_label = QLabel("Activity will appear here while Kestrel works.")
        self.steps_label.setWordWrap(True)
        self.steps_label.setTextFormat(Qt.TextFormat.PlainText)
        self.steps_label.setStyleSheet(
            f"color: {COLOR_TEXT_MID}; font-family: \"{FONT_MONO}\"; "
            "font-size: 10px;")
        live_layout.addWidget(self.steps_label, 1)
        layout.addWidget(live, 3)

        separator = QFrame()
        separator.setFixedWidth(1)
        separator.setStyleSheet(f"background: {COLOR_BORDER};")
        layout.addWidget(separator)

        ledger = QWidget()
        ledger_layout = zero_layout(QVBoxLayout(ledger))
        ledger_layout.setSpacing(5)
        ledger_header = QHBoxLayout()
        ledger_header.setSpacing(6)
        ledger_heading = QLabel("TASKS")
        ledger_heading.setStyleSheet(
            f"color: {COLOR_TEXT_MID}; font-family: \"{FONT_MONO}\"; "
            "font-size: 9px; font-weight: bold;")
        ledger_header.addWidget(ledger_heading)
        self.ledger_summary = QLabel("0 open · 0 done")
        self.ledger_summary.setTextFormat(Qt.TextFormat.PlainText)
        self.ledger_summary.setStyleSheet(
            f"color: {COLOR_TEXT_LOW}; font-family: \"{FONT_MONO}\"; "
            "font-size: 9px;")
        ledger_header.addWidget(self.ledger_summary)
        ledger_header.addStretch(1)
        ledger_layout.addLayout(ledger_header)

        self.ledger_buttons = []
        self.focused_task_id = None
        self._ledger_list_layout = zero_layout(QVBoxLayout())
        self._ledger_list_layout.setSpacing(4)
        ledger_layout.addLayout(self._ledger_list_layout)
        ledger_layout.addStretch(1)
        layout.addWidget(ledger, 2)

    def set_ledger_state(self, state):
        tasks = list(state.get("tasks") or [])
        self.focused_task_id = state.get("focused_task_id")
        error = str(state.get("error") or "")
        open_tasks = [task for task in tasks if task["status"] != "done"]
        done_count = sum(task["status"] == "done" for task in tasks)
        if error:
            self.ledger_summary.setText("Ledger unavailable")
        else:
            self.ledger_summary.setText(
                f"{len(open_tasks)} open · {done_count} done")

        while self._ledger_list_layout.count():
            item = self._ledger_list_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.ledger_buttons = []

        if error:
            message = QLabel("Open Chat for the storage error.")
            message.setTextFormat(Qt.TextFormat.PlainText)
            message.setWordWrap(True)
            message.setStyleSheet(
                f"color: {COLOR_WARN}; font-family: \"{FONT_UI}\"; "
                "font-size: 10px;")
            self._ledger_list_layout.addWidget(message)
            return

        focused = next(
            (task for task in open_tasks if task["id"] == self.focused_task_id),
            None,
        )
        if focused is not None:
            open_tasks = [focused] + [task for task in open_tasks
                                     if task["id"] != focused["id"]]
        for task in open_tasks[:2]:
            button = QPushButton(
                f"{'› ' if task['id'] == self.focused_task_id else ''}"
                f"{task['title']}")
            button.setObjectName("ledgerTaskButton")
            button.setProperty("taskId", task["id"])
            button.setProperty(
                "focused", task["id"] == self.focused_task_id)
            button.setToolTip(task["title"])
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setSizePolicy(
                QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            button.setMinimumWidth(0)
            button.setFixedHeight(26)
            button.setStyleSheet(
                f"""
                QPushButton {{
                    background: {COLOR_RAISED}; border: 1px solid {COLOR_BORDER};
                    border-radius: 6px; color: {COLOR_TEXT_USER}; text-align: left;
                    padding: 3px 7px; font-family: "{FONT_UI}"; font-size: 10px;
                }}
                QPushButton:hover {{ background: {COLOR_HOVER};
                    color: {COLOR_TEXT_HI}; }}
                QPushButton[focused="true"] {{ border-color: {COLOR_ACCENT};
                    color: {COLOR_TEXT_HI}; }}
                """
            )
            button.clicked.connect(
                lambda _checked=False, task_id=task["id"]:
                self.focus_requested.emit(task_id))
            self._ledger_list_layout.addWidget(button)
            self.ledger_buttons.append(button)

        if not open_tasks:
            empty = QLabel("No open tasks")
            empty.setTextFormat(Qt.TextFormat.PlainText)
            empty.setStyleSheet(
                f"color: {COLOR_TEXT_LOW}; font-family: \"{FONT_UI}\"; "
                "font-size: 10px;")
            self._ledger_list_layout.addWidget(empty)

    def set_task_title(self, title):
        text = " ".join(str(title or "").split())
        self.task_label.setText(text or "No active task")

    def set_activity(self, steps):
        lines = [" ".join(str(line).split()) for line in (steps or [])
                 if str(line).strip()]
        self.steps_label.setText("\n".join(lines[-4:]) or "Waiting for the next step…")

    def set_activity_idle(self):
        self.task_label.setText("No active task")
        self.steps_label.setText("Activity will appear here while Kestrel works.")

    def steps_text(self):
        return self.steps_label.text()


class StateBar(QFrame):
    STATES = ("IDLE", "LISTENING", "THINKING", "SPEAKING")

    def __init__(self):
        super().__init__()
        self.setObjectName("stateBar")
        self.setFixedHeight(44)
        self.setStyleSheet(
            f"""
            QFrame#stateBar {{
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 {COLOR_ACCENT_DIM}, stop:0.18 {COLOR_BG_ALT});
                border-top: 1px solid {COLOR_ACCENT};
            }}
            QFrame#stateBar QLabel {{
                background: transparent; color: {COLOR_TEXT_LOW};
                font-family: "{FONT_MONO}"; font-size: 10px;
                font-weight: bold; padding: 4px 14px;
            }}
            """
        )
        row = QHBoxLayout(self)
        row.setContentsMargins(18, 0, 18, 0)
        row.setSpacing(4)
        row.addStretch(1)
        self.labels = {}
        for state in self.STATES:
            label = QLabel(state)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            row.addWidget(label)
            self.labels[state] = label
        row.addStretch(1)
        self.state = "IDLE"
        self.set_state("IDLE")

    def set_state(self, state):
        name = str(state or "IDLE").upper()
        if name == "CONTINUOUS":
            name = "LISTENING"
        if name not in self.STATES:
            name = "IDLE"
        self.state = name
        for label_state, label in self.labels.items():
            active = label_state == name
            label.setStyleSheet(
                f"background: transparent; color: "
                f"{COLOR_ACCENT if active else COLOR_TEXT_LOW}; "
                f"font-family: \"{FONT_MONO}\"; font-size: 10px; "
                f"font-weight: {'bold' if active else 'normal'}; "
                f"padding: 4px 14px;")


class OwlStage(QFrame):
    def __init__(self):
        super().__init__()
        self.setObjectName("owlStage")
        self.setMinimumWidth(260)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setStyleSheet(
            f"QFrame#owlStage {{ background: {COLOR_BG_ALT}; "
            f"border: 1px solid {COLOR_BORDER}; border-radius: 16px; }}")

        self.link_label = QLabel("HOLOGRAPHIC LINK // ONLINE", self)
        self.link_label.setStyleSheet(
            f"background: transparent; color: {COLOR_TEXT_MID}; "
            f"font-family: \"{FONT_MONO}\"; font-size: 9px;")
        self.link_label.setContentsMargins(16, 14, 16, 16)

        self.hud = ReactorHud(parent=self, opacity=0.92)
        self.pet = PetWidget(parent=self, opacity=1.0)
        self.pet.set_position("bottom_right")
        self.hud.set_pet_widget(self.pet)

        self._sprite_relay = _SpriteRelay(self)
        self._sprite_relay.command.connect(self.pet.play)
        forward = self._sprite_relay.forward
        sprite_bus.register(forward)
        self.destroyed.connect(lambda *_: sprite_bus.unregister(forward))
        self._fit_contents()

    def set_state(self, state, pet_state="idle"):
        self.hud.set_state(state)
        self.hud.set_pet_state(pet_state)

    def note_pet_activity(self):
        self.hud.note_pet_activity()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_contents()

    def _fit_contents(self):
        self.pet.sync_to_parent()
        side = max(220, min(self.width() - 32, self.height() - 120))
        self.hud.setFixedSize(side, side)
        self.hud.move(
            (self.width() - side) // 2,
            max(50, (self.height() - side) // 2 - 12),
        )
        self.pet.set_scale(max(1.6, min(2.3, self.width() / 210.0)))
        self.link_label.raise_()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        if not painter.isActive():
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        cx = self.width() // 2
        cy = int(self.height() * 0.80)
        for radius, alpha, width in ((150, 18, 10), (116, 30, 5), (82, 55, 2)):
            color = QColor(COLOR_ACCENT)
            color.setAlpha(alpha)
            painter.setPen(QPen(color, width))
            painter.drawEllipse(
                cx - radius, cy - radius // 4, radius * 2, radius // 2)
        color = QColor(COLOR_ACCENT)
        color.setAlpha(26)
        painter.setPen(QPen(color, 1))
        for offset in (-34, 0, 34):
            painter.drawLine(cx - 130, cy + offset, cx + 130, cy + offset)
        painter.end()


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
        self.setMinimumWidth(0)
        self.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
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

    CARD_TITLES = ("CORE", "ACTIVITY", "NOW PLAYING", "SYSTEM", "WEATHER",
                   "TELEGRAM")

    def __init__(self):
        super().__init__()
        self.setObjectName("rightPanel")
        self.setMinimumWidth(MIN_RIGHT_PANEL_W)
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
# Memory constellation — read-only manual user traits
# ----------------------------------------------------------------------

class ConstellationCanvas(QWidget):
    node_selected = Signal(int)

    def __init__(self):
        super().__init__()
        self.setObjectName("memoryConstellationCanvas")
        self.setMinimumSize(360, 260)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("Memory trait constellation")
        self.setAccessibleDescription(
            "Select a trait node with the mouse or arrow keys. "
            "Confidence controls node size, evidence controls halo rings, "
            "and newer traits appear closer to the center.")
        self.setStyleSheet(
            f"QWidget#memoryConstellationCanvas {{ background: {COLOR_BG}; }}")
        self.traits = []
        self.selected_index = -1
        self._nodes = []

    def set_traits(self, traits):
        self.traits = [dict(record) for record in traits]
        self.selected_index = 0 if self.traits else -1
        self._nodes = self._layout_nodes()
        if self.traits:
            self.node_selected.emit(0)
        self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._nodes = self._layout_nodes()
        self.update()

    @staticmethod
    def _timestamp(value):
        try:
            parsed = datetime.datetime.fromisoformat(
                str(value or "").strip().replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(
                    datetime.timezone.utc).replace(tzinfo=None)
            return parsed.timestamp()
        except (TypeError, ValueError, OSError, OverflowError):
            return None

    def _layout_nodes(self):
        if not self.traits:
            return []
        width = max(360, self.width())
        height = max(260, self.height())
        center_x = width / 2
        center_y = height / 2
        extent = min(width * 0.30, height * 0.34)
        radius_x = extent * 1.35
        radius_y = extent
        timestamps = [self._timestamp(item.get("last_updated"))
                      for item in self.traits]
        known = [value for value in timestamps if value is not None]
        newest = max(known) if known else 0.0
        oldest = min(known) if known else 0.0
        span = newest - oldest
        nodes = []
        for index, (record, timestamp) in enumerate(
                zip(self.traits, timestamps)):
            if timestamp is not None and span > 0:
                recency = (newest - timestamp) / span
            else:
                recency = index / max(1, len(self.traits) - 1)
            distance = extent * (0.42 + 0.58 * recency)
            angle = -math.pi / 2 + index * 2.399963229728653
            confidence = max(1, min(10, int(record["confidence"])))
            evidence = max(0, int(record["evidence_count"]))
            rings = 0
            while evidence > 0 and rings < 4:
                rings += 1
                evidence //= 2
            nodes.append({
                "index": index,
                "trait": dict(record),
                "x": center_x + math.cos(angle) * radius_x * (
                    0.42 + 0.58 * recency),
                "y": center_y + math.sin(angle) * radius_y * (
                    0.42 + 0.58 * recency),
                "radius": 11 + confidence * 1.1,
                "rings": rings,
                "distance": distance,
            })
        return nodes

    def _node_at(self, position):
        for node in self._nodes:
            dx = position.x() - node["x"]
            dy = position.y() - node["y"]
            if math.hypot(dx, dy) <= node["radius"] + 10:
                return node
        return None

    def select_node(self, index):
        if not self._nodes:
            return
        index = max(0, min(len(self._nodes) - 1, int(index)))
        if index == self.selected_index:
            return
        self.selected_index = index
        self.node_selected.emit(index)
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            node = self._node_at(event.pos())
            if node is not None:
                self.setFocus()
                self.select_node(node["index"])
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        node = self._node_at(event.pos())
        self.setToolTip(node["trait"]["trait"] if node is not None else "")
        self.setCursor(Qt.CursorShape.PointingHandCursor
                       if node is not None else Qt.CursorShape.ArrowCursor)
        super().mouseMoveEvent(event)

    def keyPressEvent(self, event):
        if not self._nodes:
            super().keyPressEvent(event)
            return
        key = event.key()
        if key in (Qt.Key.Key_Right, Qt.Key.Key_Down):
            self.select_node((self.selected_index + 1) % len(self._nodes))
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_Up):
            self.select_node((self.selected_index - 1) % len(self._nodes))
        elif key == Qt.Key.Key_Home:
            self.select_node(0)
        elif key == Qt.Key.Key_End:
            self.select_node(len(self._nodes) - 1)
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(event.rect(), QColor(COLOR_BG))
        center_x = self.width() / 2
        center_y = self.height() / 2
        accent = QColor(COLOR_ACCENT)

        if not self._nodes:
            halo = QColor(COLOR_BORDER)
            painter.setPen(QPen(halo, 1))
            for radius in (34, 52, 70):
                painter.drawEllipse(QRectF(
                    center_x - radius, center_y - radius,
                    radius * 2, radius * 2))
            painter.setPen(QColor(COLOR_TEXT_MID))
            painter.drawText(
                QRectF(center_x - 80, center_y - 24, 160, 48),
                Qt.AlignmentFlag.AlignCenter, "NO TRAITS")
            return

        for node in self._nodes:
            line = QColor(COLOR_ACCENT)
            line.setAlphaF(0.16)
            painter.setPen(QPen(line, 1))
            painter.drawLine(int(center_x), int(center_y),
                             int(node["x"]), int(node["y"]))

        painter.setPen(QPen(QColor(COLOR_BORDER), 1))
        painter.drawEllipse(QRectF(center_x - 30, center_y - 30, 60, 60))
        painter.setPen(QColor(COLOR_TEXT_MID))
        font = painter.font()
        font.setFamily(FONT_MONO)
        font.setPointSize(8)
        painter.setFont(font)
        painter.drawText(
            QRectF(center_x - 38, center_y - 20, 76, 40),
            Qt.AlignmentFlag.AlignCenter, "MANUAL\nPROFILE")

        ordered = sorted(self._nodes,
                         key=lambda node: node["index"] == self.selected_index)
        for node in ordered:
            confidence = node["trait"]["confidence"]
            selected = node["index"] == self.selected_index
            for ring in range(node["rings"]):
                radius = node["radius"] + 6 + ring * 4
                halo = QColor(COLOR_ACCENT)
                halo.setAlphaF(0.12 + ring * 0.04)
                painter.setPen(QPen(halo, 1))
                painter.drawEllipse(QRectF(
                    node["x"] - radius, node["y"] - radius,
                    radius * 2, radius * 2))
            fill = QColor(COLOR_ACCENT)
            fill.setAlphaF(0.62 if selected else 0.18 + confidence * 0.035)
            painter.setPen(QPen(accent, 2.4 if selected else 1.4))
            painter.setBrush(fill)
            painter.drawEllipse(QRectF(
                node["x"] - node["radius"], node["y"] - node["radius"],
                node["radius"] * 2, node["radius"] * 2))
            painter.setPen(QColor(COLOR_TEXT_MID))
            painter.drawText(
                QRectF(node["x"] + node["radius"] + 5,
                       node["y"] - 8, 28, 16),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                f"{node['index'] + 1:02d}")


class MemoryConstellationView(QFrame):
    back_requested = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName("memoryConstellationView")
        self.setStyleSheet(
            f"QFrame#memoryConstellationView {{ background: {COLOR_BG}; }}")
        outer = zero_layout(QVBoxLayout(self))
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(10)

        header = QHBoxLayout()
        header.setSpacing(8)
        self.back_button = QPushButton("Back to chat")
        self.back_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.back_button.clicked.connect(self.back_requested.emit)
        header.addWidget(self.back_button)
        title = QLabel("MEMORY CONSTELLATION")
        title.setProperty("tone", "hi")
        title.setStyleSheet(
            f'font-family: "{FONT_MONO}"; font-size: 13px; font-weight: bold;')
        header.addWidget(title)
        read_only = QLabel("READ ONLY")
        read_only.setProperty("tone", "dim")
        read_only.setStyleSheet(
            f'font-family: "{FONT_MONO}"; font-size: 9px;')
        header.addWidget(read_only)
        header.addStretch(1)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh_button.clicked.connect(self.refresh)
        header.addWidget(self.refresh_button)
        outer.addLayout(header)

        self.warning_label = QLabel(memory.USER_TRAITS_WARNING)
        self.warning_label.setObjectName("memoryWarning")
        self.warning_label.setWordWrap(True)
        self.warning_label.setTextFormat(Qt.TextFormat.PlainText)
        self.warning_label.setProperty("tone", "warn")
        self.warning_label.setStyleSheet(
            f"background: {COLOR_BG_ALT}; border: 1px solid {COLOR_BORDER};"
            f" border-radius: 8px; padding: 8px 10px;"
            f' font-family: "{FONT_MONO}"; font-size: 10px;')
        outer.addWidget(self.warning_label)

        self.status_label = QLabel("")
        self.status_label.setObjectName("memoryStatus")
        self.status_label.setWordWrap(True)
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setProperty("tone", "dim")
        self.status_label.setStyleSheet(
            f'font-family: "{FONT_UI}"; font-size: 11px;')
        outer.addWidget(self.status_label)

        body = QHBoxLayout()
        body.setSpacing(12)
        self.canvas = ConstellationCanvas()
        self.canvas.node_selected.connect(self._update_details)
        body.addWidget(self.canvas, 1)

        self.detail_panel = QFrame()
        self.detail_panel.setObjectName("memoryDetailPanel")
        self.detail_panel.setProperty("panel", "true")
        self.detail_panel.setFixedWidth(280)
        details = zero_layout(QVBoxLayout(self.detail_panel))
        details.setContentsMargins(14, 14, 14, 14)
        details.setSpacing(8)
        heading = QLabel("SELECTED TRAIT")
        heading.setProperty("role", "heading")
        details.addWidget(heading)
        self.detail_text = QLabel("Select a node to inspect its source data.")
        self.detail_text.setWordWrap(True)
        self.detail_text.setTextFormat(Qt.TextFormat.PlainText)
        self.detail_text.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.detail_text.setProperty("tone", "hi")
        self.detail_text.setStyleSheet(
            f'font-family: "{FONT_UI}"; font-size: 14px; font-weight: bold;')
        details.addWidget(self.detail_text)

        meta = zero_layout(QVBoxLayout())
        meta.setSpacing(6)
        confidence_row, self.confidence_label = self._detail_value(
            "CONFIDENCE", "—")
        evidence_row, self.evidence_label = self._detail_value(
            "EVIDENCE COUNT", "—")
        updated_row, self.updated_label = self._detail_value(
            "LAST UPDATED", "—")
        meta.addLayout(confidence_row)
        meta.addLayout(evidence_row)
        meta.addLayout(updated_row)
        details.addLayout(meta)
        details.addStretch(1)
        hint = QLabel(
            "Source: user_model.json. Kestrel does not extract, infer, "
            "or write traits from this view.")
        hint.setWordWrap(True)
        hint.setTextFormat(Qt.TextFormat.PlainText)
        hint.setProperty("tone", "dim")
        hint.setStyleSheet(
            f'font-family: "{FONT_UI}"; font-size: 10px;')
        details.addWidget(hint)
        body.addWidget(self.detail_panel)
        outer.addLayout(body, 1)
        self.refresh()

    @staticmethod
    def _detail_value(label, value):
        row = QHBoxLayout()
        row.setSpacing(8)
        name = QLabel(label)
        name.setProperty("tone", "dim")
        name.setStyleSheet(
            f'font-family: "{FONT_MONO}"; font-size: 9px;')
        text = QLabel(value)
        text.setProperty("tone", "hi")
        text.setStyleSheet(
            f'font-family: "{FONT_MONO}"; font-size: 11px;')
        row.addWidget(name)
        row.addStretch(1)
        row.addWidget(text)
        return row, text

    def refresh(self):
        self.set_snapshot(memory.inspect_user_model())

    def set_snapshot(self, result):
        status = result.get("status", "invalid")
        traits = list(result.get("traits") or [])
        messages = {
            "empty": "No manual traits yet. Add them directly to "
                     "user_model.json; Kestrel never extracts them here.",
            "missing": "user_model.json is missing. This view stays empty "
                       "until you create it manually.",
            "malformed": "user_model.json is malformed. No trait data is shown.",
            "unavailable": "user_model.json is unavailable. Check its "
                           "permissions, then choose Refresh.",
            "invalid": "user_model.json has an unsupported shape. "
                       "No trait data is shown.",
            "unsafe": "No safe manual traits to show. Unsafe entries were hidden.",
        }
        if status == "valid":
            count = len(traits)
            text = f"{count} manual trait{'s' if count != 1 else ''} · read only"
            filtered = []
            duplicates = int(result.get("duplicate_count", 0))
            unsafe = int(result.get("unsafe_count", 0))
            invalid = int(result.get("invalid_count", 0))
            if duplicates:
                filtered.append(
                    f"{duplicates} duplicate{'s' if duplicates != 1 else ''} ignored")
            if unsafe:
                filtered.append(
                    f"{unsafe} unsafe entr{'ies' if unsafe != 1 else 'y'} hidden")
            if invalid:
                filtered.append(
                    f"{invalid} invalid entr{'ies' if invalid != 1 else 'y'} ignored")
            if filtered:
                text += " · " + " · ".join(filtered)
        else:
            text = messages.get(status, messages["invalid"])
        self.status_label.setText(text)
        self.canvas.set_traits(traits)
        self._update_details(self.canvas.selected_index)

    def _update_details(self, index):
        if not self.canvas.traits or not 0 <= index < len(self.canvas.traits):
            self.detail_text.setText("Select a node to inspect its source data.")
            self.confidence_label.setText("—")
            self.evidence_label.setText("—")
            self.updated_label.setText("—")
            return
        record = self.canvas.traits[index]
        self.detail_text.setText(record["trait"])
        self.confidence_label.setText(f"{record['confidence']} / 10")
        self.evidence_label.setText(str(record["evidence_count"]))
        self.updated_label.setText(record["last_updated"] or "unknown")


# ----------------------------------------------------------------------
# Memory Manager — structured memories, global and project scoped
# ----------------------------------------------------------------------
def _memory_button(text, primary=False, danger=False):
    b = QPushButton(text)
    b.setFixedHeight(28)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    if primary:
        bg, hover, fg = COLOR_ACCENT, COLOR_ACCENT_HOVER, COLOR_BG
    elif danger:
        bg, hover, fg = "transparent", COLOR_HOVER, COLOR_DANGER
    else:
        bg, hover, fg = COLOR_RAISED, COLOR_HOVER, COLOR_TEXT_HI
    b.setStyleSheet(
        f"""
        QPushButton {{
            background: {bg}; border: none; border-radius: 6px;
            color: {fg}; font-family: "{FONT_UI}"; font-size: 11px;
            font-weight: bold; padding: 0 12px;
        }}
        QPushButton:hover {{ background: {hover}; }}
        """
    )
    return b


class MemoryEditDialog(QDialog):
    """Add/edit form for one structured memory."""

    def __init__(self, parent, record=None):
        super().__init__(parent)
        self._values = None
        self.setWindowTitle("Edit memory" if record else "Add memory")
        self.setModal(True)
        self.setMinimumSize(560, 430)
        self.setStyleSheet(
            f"""
            QDialog {{ background: {COLOR_BG}; }}
            QLabel {{
                color: {COLOR_TEXT_MID}; font-family: "{FONT_UI}";
                font-size: 11px; background: transparent;
            }}
            QLineEdit, QComboBox, QPlainTextEdit {{
                background: {COLOR_INPUT_BG}; border: 1px solid {COLOR_BORDER};
                border-radius: 6px; color: {COLOR_TEXT_HI};
                padding: 5px 8px; font-family: "{FONT_MONO}"; font-size: 12px;
            }}
            QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus {{
                border-color: {COLOR_ACCENT_DIM};
            }}
            QComboBox QAbstractItemView {{
                background: {COLOR_BG_ALT}; color: {COLOR_TEXT_HI};
                selection-background-color: {COLOR_SELECTED};
            }}
            """
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(6)

        layout.addWidget(QLabel("Title"))
        self.title_edit = QLineEdit(record["title"] if record else "")
        self.title_edit.setPlaceholderText("Optional — generated from the content")
        layout.addWidget(self.title_edit)

        layout.addWidget(QLabel("Scope"))
        self.scope_combo = QComboBox()
        self.scope_combo.setEditable(True)
        self.scope_combo.addItems(
            [memory.GLOBAL_SCOPE, memory.current_dir()])
        if record:
            self.scope_combo.setCurrentText(record["scope"])
        layout.addWidget(self.scope_combo)
        scope_hint = QLabel(
            "'global' applies everywhere. A folder or file path applies only "
            "when the agent runs inside that project.")
        scope_hint.setWordWrap(True)
        scope_hint.setStyleSheet(
            f'color: {COLOR_TEXT_LOW}; font-family: "{FONT_UI}"; '
            f"font-size: 10px; background: transparent;")
        layout.addWidget(scope_hint)

        layout.addWidget(QLabel("Keywords"))
        self.keywords_edit = QLineEdit(
            ", ".join(record.get("keywords") or []) if record else "")
        self.keywords_edit.setPlaceholderText(
            "Optional, comma separated — generated when left empty")
        layout.addWidget(self.keywords_edit)

        layout.addWidget(QLabel("Content"))
        self.content_edit = QPlainTextEdit(
            record["content"] if record else "")
        self.content_edit.setPlaceholderText("What should the agent remember?")
        layout.addWidget(self.content_edit, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        cancel = _memory_button("Cancel")
        cancel.clicked.connect(self.reject)
        save = _memory_button("Save", primary=True)
        save.clicked.connect(self._accept)
        row.addWidget(cancel)
        row.addWidget(save)
        layout.addLayout(row)

    def _accept(self):
        content = self.content_edit.toPlainText().strip()
        if not content:
            QMessageBox.warning(self, "Memory", "Content cannot be empty.")
            return
        keywords = [part.strip() for part in re.split(
            r"[,;]", self.keywords_edit.text()) if part.strip()]
        self._values = {
            "title": self.title_edit.text().strip(),
            "scope": self.scope_combo.currentText().strip() or memory.GLOBAL_SCOPE,
            "keywords": keywords,
            "content": content,
        }
        self.accept()

    def values(self):
        return self._values


class MemoryManager(QWidget):
    """In-window overlay listing every memory, with add/edit/delete."""

    def __init__(self, parent, on_close=None):
        super().__init__(parent)
        self._on_close = on_close
        self.setGeometry(parent.rect())
        self.setStyleSheet("background: rgba(0, 0, 0, 170);")
        self.mousePressEvent = lambda e: None

        panel = QFrame(self)
        panel.setObjectName("memoryPanel")
        panel.setStyleSheet(
            f"""
            QFrame#memoryPanel {{
                background: {COLOR_BG};
                border: 1px solid {COLOR_BORDER};
                border-radius: 12px;
            }}
            QFrame#memoryCard {{
                background: {COLOR_BG_ALT};
                border: 1px solid {COLOR_BORDER};
                border-radius: 9px;
            }}
            QScrollArea {{ background: transparent; border: none; }}
            """
        )
        panel.setFixedSize(780, 640)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(panel, 0, Qt.AlignmentFlag.AlignCenter)

        playout = QVBoxLayout(panel)
        playout.setContentsMargins(0, 0, 0, 0)
        playout.setSpacing(0)

        bar = QWidget()
        bar.setFixedHeight(HEADER_H)
        bar.setStyleSheet(
            f"background: {COLOR_SIDEBAR}; border-top-left-radius: 12px;"
            f" border-top-right-radius: 12px;")
        bar_row = QHBoxLayout(bar)
        bar_row.setContentsMargins(10, 0, 14, 0)
        back = _icon_button("back", "Close memory manager")
        back.clicked.connect(self._close)
        bar_row.addWidget(back)
        title = QLabel("Memory Manager")
        title.setStyleSheet(
            f'color: {COLOR_TEXT_HI}; font-family: "{FONT_UI}"; '
            f"font-size: 15px; font-weight: bold; background: transparent;")
        bar_row.addWidget(title)
        bar_row.addStretch(1)
        self._count_label = QLabel("")
        self._count_label.setStyleSheet(
            f'color: {COLOR_TEXT_LOW}; font-family: "{FONT_UI}"; '
            f"font-size: 10px; background: transparent;")
        bar_row.addWidget(self._count_label)
        playout.addWidget(bar)

        toolbar = QWidget()
        tool_row = QHBoxLayout(toolbar)
        tool_row.setContentsMargins(14, 10, 14, 6)
        project = QLabel(f"Current project: {memory.current_dir()}")
        project.setStyleSheet(
            f'color: {COLOR_TEXT_LOW}; font-family: "{FONT_MONO}"; '
            f"font-size: 10px; background: transparent;")
        project.setToolTip(
            "Project-scoped memories appear in the prompt when the agent "
            "runs in this directory.")
        tool_row.addWidget(project, 1)
        add = _memory_button("+ Add memory", primary=True)
        add.clicked.connect(self._add_clicked)
        tool_row.addWidget(add)
        playout.addWidget(toolbar)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setStyleSheet("background: transparent; border: none;")
        self._list_host = QWidget()
        self._list_host.setStyleSheet("background: transparent;")
        self._list_layout = QVBoxLayout(self._list_host)
        self._list_layout.setContentsMargins(14, 4, 14, 14)
        self._list_layout.setSpacing(8)
        self._scroll.setWidget(self._list_host)
        playout.addWidget(self._scroll, 1)

        hint = QLabel(
            "Global memories are injected in every project. "
            "Project memories are injected only when their scope matches "
            "the current working directory.")
        hint.setWordWrap(True)
        hint.setStyleSheet(
            f'color: {COLOR_TEXT_LOW}; font-family: "{FONT_UI}"; '
            f"font-size: 10px; background: transparent; padding: 0 14px 12px;")
        playout.addWidget(hint)

        self.refresh()

    def _close(self):
        if self._on_close is not None:
            self._on_close()

    def refresh(self):
        records = memory.list_memories(include_all=True)
        self._count_label.setText(
            f"{len(records)} memor{'y' if len(records) == 1 else 'ies'}")
        while self._list_layout.count():
            item = self._list_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        if not records:
            empty = QLabel(
                "No memories yet. Add one, or let the agent learn from "
                "successful tasks.")
            empty.setWordWrap(True)
            empty.setStyleSheet(
                f'color: {COLOR_TEXT_LOW}; font-family: "{FONT_UI}"; '
                f"font-size: 12px; background: transparent; padding: 24px;")
            self._list_layout.addWidget(empty)
        else:
            for record in records:
                self._list_layout.addWidget(self._memory_card(record))
        self._list_layout.addStretch(1)

    def _memory_card(self, record):
        card = QFrame()
        card.setObjectName("memoryCard")
        col = QVBoxLayout(card)
        col.setContentsMargins(14, 10, 14, 12)
        col.setSpacing(6)

        head = QHBoxLayout()
        head.setSpacing(8)
        title = QLabel(record["title"] or record["content"][:60])
        title.setWordWrap(True)
        title.setStyleSheet(
            f'color: {COLOR_TEXT_HI}; font-family: "{FONT_UI}"; '
            f"font-size: 13px; font-weight: bold; background: transparent;")
        head.addWidget(title, 1)
        col.addLayout(head)

        meta_row = QHBoxLayout()
        meta_row.setSpacing(8)
        scope = record["scope"]
        if scope == memory.GLOBAL_SCOPE:
            scope_text = "global"
        else:
            scope_text = os.path.basename(scope.rstrip("\\/")) or scope
        badge = QLabel(scope_text)
        badge.setToolTip(scope)
        badge.setStyleSheet(
            f'color: {COLOR_ACCENT}; background: {COLOR_ACCENT_DIM}; '
            f'border-radius: 6px; padding: 2px 7px; font-family: "{FONT_MONO}"; '
            f"font-size: 10px;")
        meta_row.addWidget(badge)
        if record.get("kind") == "lesson":
            kind = QLabel("lesson")
            kind.setStyleSheet(
                f'color: {COLOR_TEXT_MID}; border: 1px solid {COLOR_BORDER}; '
                f'border-radius: 6px; padding: 2px 7px; font-family: "{FONT_MONO}"; '
                f"font-size: 10px; background: transparent;")
            meta_row.addWidget(kind)
        created = QLabel((record.get("created_at") or "").replace("T", " ")[:16])
        created.setStyleSheet(
            f'color: {COLOR_TEXT_LOW}; font-family: "{FONT_MONO}"; '
            f"font-size: 10px; background: transparent;")
        meta_row.addWidget(created)
        meta_row.addStretch(1)

        edit = _memory_button("Edit")
        edit.clicked.connect(lambda _=False, r=record: self._edit_clicked(r))
        meta_row.addWidget(edit)
        delete = _memory_button("Delete", danger=True)
        delete.clicked.connect(
            lambda _=False, r=record: self._delete_clicked(r))
        meta_row.addWidget(delete)
        col.addLayout(meta_row)

        text = record["content"]
        if len(text) > 600:
            text = text[:597].rstrip() + "..."
        content = QLabel(text)
        content.setWordWrap(True)
        content.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        content.setStyleSheet(
            f'color: {COLOR_TEXT_MID}; font-family: "{FONT_MONO}"; '
            f"font-size: 11px; background: transparent;")
        col.addWidget(content)

        keywords = record.get("keywords") or []
        if keywords:
            meta = QLabel("keywords: " + ", ".join(keywords))
            meta.setWordWrap(True)
            meta.setStyleSheet(
                f'color: {COLOR_TEXT_LOW}; font-family: "{FONT_MONO}"; '
                f"font-size: 10px; background: transparent;")
            col.addWidget(meta)
        return card

    def _add_clicked(self):
        dialog = MemoryEditDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values() or {}
        if memory.add_memory(**values) is None:
            QMessageBox.information(
                self, "Memory", "A matching memory already exists.")
        self.refresh()

    def _edit_clicked(self, record):
        dialog = MemoryEditDialog(self, record)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values() or {}
        if not memory.update_memory(record["id"], **values):
            QMessageBox.warning(self, "Memory", "Could not update this memory.")
        self.refresh()

    def _delete_clicked(self, record):
        answer = QMessageBox.question(
            self, "Delete memory",
            f"Delete this memory?\n\n{record['title']}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            memory.delete_memory(record["id"])
            self.refresh()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self._close()
            return
        super().keyPressEvent(event)


# ----------------------------------------------------------------------
# Main window
# ----------------------------------------------------------------------
class _ActivityFilter(QObject):
    """Forward keys and clicks to the window they happened in, as pet activity.

    One instance for the whole application, not one per window: Qt dispatches
    every event to every installed filter, so a filter per AgentWindow makes the
    full test suite quadratic in the number of windows it builds — measured at
    ~95 s of extra runtime before this was shared.
    """

    def eventFilter(self, obj, event):
        if event.type() in (QEvent.Type.KeyPress,
                            QEvent.Type.MouseButtonPress):
            if isinstance(obj, QWidget):
                try:
                    note = getattr(obj.window(), "_note_pet_activity", None)
                except RuntimeError:
                    return False        # the widget was already destroyed
                if note is not None:
                    note()
        return False


_activity_filter = None


def _ensure_activity_filter():
    global _activity_filter
    if _activity_filter is None:
        app = QApplication.instance()
        if app is not None:
            _activity_filter = _ActivityFilter(app)
            app.installEventFilter(_activity_filter)
    return _activity_filter


class AgentWindow(QMainWindow):
    _command_done = Signal(str)      # background slash-command result
    _scheduled_triggered = Signal(str)  # scheduler fired (from its own thread)
    _event_result = Signal(str)
    _reminder_result = Signal(str, str)  # (result, reminder_text)
    _hotkey_changed = Signal(bool)   # F9 pressed/released (from keyboard thread)
    _voice_event = Signal(int, str)  # session generation and worker status
    _weather_ready = Signal(str)     # parsed weather text (from worker thread)
    _gui_visibility_request = Signal(bool, object)  # (show, completion event)
    _gui_capture_request = Signal(object, object)   # (event, result holder)
    _wake_word_detected = Signal(float)   # wake word score (from mic thread)
    _wake_word_error = Signal(str)        # listener startup/runtime error
    _tts_error = Signal(int, str)              # speech generation and error

    NOTICE_MS = 2000                      # transient terminal notices
    _spontaneous_message = Signal(str)    # proactive observation (worker thread)
    _spontaneous_finished = Signal()      # worker done; clear the busy flag
    _preload_ready = Signal(str)          # a heavy module finished importing
    task_ledger_changed = Signal(object)  # shared Chat/TaskWorkspace ledger state

    def __init__(self, task_ledger_path=None):
        super().__init__()
        resolve_fonts()
        # Native-message handler for the frameless shell; None where the OS
        # title bar stays (non-Windows), so nativeEvent() is a straight passthrough.
        self._chrome = FramelessChrome(self) if _FRAMELESS_SUPPORTED else None
        self._title_bar = None
        self.setWindowTitle("KESTREL")
        self.resize(1480, 820)
        self.setMinimumWidth(1360)
        self.setMinimumHeight(720)
        self.setContentsMargins(0, 0, 0, 0)

        self._worker = None
        self._processing = False
        # Stop mechanism: the flag is read by the GUI, the event by the agent
        # thread. A fresh event is created at the start of every turn.
        self._stop_requested = False
        self._stop_event = threading.Event()
        # Only agent turns can be cancelled; slash-command jobs cannot.
        self._turn_cancellable = False
        self._settings_overlay = None
        self._memory_overlay = None
        self._tray_icon = None
        self._tray_menu = None
        self._hotkey_installed = False
        self._recording = False
        self._transcribing = False
        self._voice_generation = 0
        self._speech_generation = 0
        self._voice_state = "idle"
        self._voice_ready = False
        self._voice_failed = False
        self._wake_ready = False
        self._wake_failed = False
        self._wake_preload_done = False
        self._tts_ready = False
        self._agent_ready = False
        self._preload_errors = {}
        self._voice_loading_notice_shown = False
        self._voice_error_shown = False
        self._f9_held = False
        self._pending_f9_start = False
        self._voice_start_event = threading.Event()
        self._retired_workers = []
        self._shutting_down = False
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
        self._hud_error_until = 0.0
        self._hud_poll_timer = None
        # Section 2 — Telegram document listener (started in start_services).
        self._telegram_service = None
        self._telegram_results = None
        self._telegram_timer = None
        self._telegram_messages = []
        self._event_bus = None
        # Section 4 — browser approval bridge (created after the chat view).
        self._browser_bridge = None
        self._copilot_bridge = None
        # Option 2 — OS control (hide/show bridge and kill switch).
        self._os_control = None
        self._gui_was_visible = True
        self._gui_was_minimized = False
        # Option 3 — wake-word listener (started in start_services).
        self._wake_word_service = None
        self._wake_word_error_shown = False
        self._wake_watchdog = None
        self._wake_restart_attempts = 0
        self._wake_watchdog_disabled = False
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
        self._lessons_timer = None
        self._gpu_unavailable = True
        self._command_done.connect(self._on_command_done)
        self._scheduled_triggered.connect(self._on_scheduled_trigger)
        self._event_result.connect(self._show_event_result)
        QApplication.instance().aboutToQuit.connect(self._on_about_to_quit)
        self._reminder_result.connect(self._reminder_done)
        self._hotkey_changed.connect(self._on_hotkey_changed)
        self._voice_event.connect(self._on_voice_event)
        self._weather_ready.connect(self._on_weather_ready)
        self._gui_visibility_request.connect(self._apply_gui_visibility)
        self._gui_capture_request.connect(self._apply_gui_capture)
        self._wake_word_detected.connect(self._on_wake_word)
        self._wake_word_error.connect(self._on_wake_word_error)
        self._tts_error.connect(self._on_tts_error)
        self._spontaneous_message.connect(self._on_spontaneous_message)
        self._spontaneous_finished.connect(self._on_spontaneous_finished)
        self._preload_ready.connect(self._on_preload_ready)
        self._preload_started = False
        self._task_ledger_path = task_ledger_path
        self.task_ledger = None
        self._task_ledger_error = ""
        self._task_ledger_card = None

        # Conversation history is filled after agent.py imports in the
        # background; an empty list keeps the window usable until then.
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
        root = zero_layout(QVBoxLayout(central))
        if _FRAMELESS_SUPPORTED:
            # The caption is collapsed into the client area by nativeEvent, so
            # this bar is the only way to move the window or reach the controls.
            self._title_bar = TitleBar(self)
            root.addWidget(self._title_bar)

        shell = QWidget()
        shell.setObjectName("appShell")
        shell_row = zero_layout(QHBoxLayout(shell))
        self.nav_rail = NavRail()
        self.workspace = self._build_workspace()
        self._load_task_ledger()
        shell_row.addWidget(self.nav_rail)
        shell_row.addWidget(self.workspace, 1)
        root.addWidget(shell, 1)

        self.setCentralWidget(central)
        self._wire_navigation()
        self._sync_hud_state()

        self._setup_browser_bridge()
        self._setup_os_control()

    def _build_workspace(self) -> QWidget:
        workspace = QWidget()
        workspace.setObjectName("workspace")
        workspace.setStyleSheet(
            f"QWidget#workspace {{ background: {COLOR_BG}; }}")
        layout = zero_layout(QVBoxLayout(workspace))
        layout.setContentsMargins(0, 12, 0, 0)
        layout.setSpacing(12)

        header = QFrame()
        header.setObjectName("workspaceHeader")
        header.setFixedHeight(48)
        header.setStyleSheet(
            f"QFrame#workspaceHeader {{ background: transparent; "
            f"border-bottom: 1px solid {COLOR_BORDER}; }}")
        header_row = QHBoxLayout(header)
        header_row.setContentsMargins(20, 0, 20, 0)
        title = QLabel("KESTREL")
        title.setStyleSheet(
            f"background: transparent; color: {COLOR_TEXT_USER}; "
            f"font-family: \"{FONT_UI}\"; font-size: 19px; font-weight: bold;")
        header_row.addWidget(title)
        header_row.addSpacing(12)
        self.header_status = QLabel("Ready")
        self.header_status.setStyleSheet(
            f"background: transparent; color: {COLOR_ACCENT}; "
            f"font-family: \"{FONT_MONO}\"; font-size: 11px;")
        header_row.addWidget(self.header_status)
        header_row.addStretch(1)
        new_chat = QToolButton()
        new_chat.setIcon(_svg_icon("newchat", 18, COLOR_TEXT_MID))
        new_chat.setToolTip("New chat")
        new_chat.setCursor(Qt.CursorShape.PointingHandCursor)
        new_chat.setStyleSheet(
            f"QToolButton {{ background: transparent; border: none; padding: 6px; }}"
            f"QToolButton:hover {{ background: {COLOR_HOVER}; }}")
        new_chat.clicked.connect(self.new_chat)
        header_row.addWidget(new_chat)
        layout.addWidget(header)

        body = QHBoxLayout()
        body.setContentsMargins(18, 0, 18, 0)
        body.setSpacing(16)
        self.conversation_column = QWidget()
        self.conversation_column.setObjectName("conversationColumn")
        self.conversation_column.setFixedWidth(TEXT_COLUMN_W)
        conversation_layout = zero_layout(QVBoxLayout(self.conversation_column))
        conversation_layout.setSpacing(12)
        self.chat = ChatView()
        self.memory_view = MemoryConstellationView()
        self.memory_view.back_requested.connect(self._show_chat_page)
        self.page_stack = QStackedWidget()
        self.page_stack.addWidget(self.chat)
        self.page_stack.addWidget(self.memory_view)
        self.task_workspace = TaskWorkspace()
        self.task_workspace.focus_requested.connect(
            self._on_task_focus_requested)
        self.task_ledger_changed.connect(
            self.task_workspace.set_ledger_state)
        conversation_layout.addWidget(self.page_stack, 1)
        conversation_layout.addWidget(self.task_workspace)
        self._main_col_layout = conversation_layout
        body.addWidget(self.conversation_column)

        self.owl_stage = OwlStage()
        body.addWidget(self.owl_stage, 1)
        layout.addLayout(body, 1)

        self.composer = self._build_input_area()
        layout.addWidget(self.composer)
        self.state_bar = StateBar()
        layout.addWidget(self.state_bar)
        return workspace

    def _wire_navigation(self):
        buttons = self.nav_rail.buttons
        buttons["Kestrel"].clicked.connect(self._show_chat_page)
        buttons["Tasks"].clicked.connect(self.open_schedule_dialog)
        buttons["Files"].clicked.connect(self._choose_attachments)
        buttons["Voice"].clicked.connect(self._toggle_voice)
        buttons["Memory"].clicked.connect(self._show_memory_page)
        buttons["Settings"].clicked.connect(self.open_settings)

    def _show_chat_page(self):
        self.page_stack.setCurrentWidget(self.chat)
        self.composer.show()
        if not self._processing:
            self.set_status("Ready", busy=False)
        self.input_field.setFocus()

    def _show_memory_page(self):
        self.page_stack.setCurrentWidget(self.memory_view)
        self.composer.hide()
        self.memory_view.refresh()
        self.set_status("Memory constellation · read only", busy=False)
        self.memory_view.canvas.setFocus()

    def _choose_attachments(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Attach files", "", "All files (*.*)")
        for path in paths:
            if path not in self.attachments:
                self.attachments.append(path)
        self._render_attachments()
        self.input_field.setFocus()

    def _update_voice_indicator(self):
        labels = {
            "idle": "F9 push-to-talk",
            "listening": "MIC ON · F9",
            "transcribing": "TRANSCRIBING",
            "thinking": "THINKING",
            "speaking": "SPEAKING",
            "cancelled": "CANCELLED",
            "error": "VOICE ERROR",
        }
        self.voice_button.setText(labels.get(self._voice_state, labels["idle"]))
        mic_active = self._voice_state == "listening"
        self.voice_button.setProperty("micActive", mic_active)
        self.voice_button.setToolTip(
            "Microphone is recording. Press F9 or Stop to cancel."
            if mic_active else "F9 push-to-talk")
        self.voice_button.style().unpolish(self.voice_button)
        self.voice_button.style().polish(self.voice_button)

    def _set_voice_state(self, state):
        self._voice_state = state
        self._update_voice_indicator()
        self._sync_hud_state()

    def _voice_active(self):
        if (self._recording or self._transcribing
                or self._continuous_listening):
            return True
        if self._voice_state in ("listening", "transcribing", "speaking"):
            return True
        return _tts_is_speaking()

    def _toggle_voice(self):
        if self._recording:
            self._stop_recording()
        elif (self._transcribing or self._voice_state == "speaking"
              or _tts_is_speaking()):
            self._cancel_voice()
        elif self._voice_ready:
            self._start_recording()
        else:
            self.set_status("Voice is not ready yet", busy=False)

    # ------------------------------------------------------------------
    # Phase 6b — panel data wiring (SYSTEM / WEATHER / NOW PLAYING)
    # ------------------------------------------------------------------
    def start_panel_timers(self):
        self.start_lessons_timer()

    def start_lessons_timer(self):
        """Run ExpeL at most once per 24h (timers must live on the GUI thread)."""
        self._lessons_timer = QTimer(self)
        self._lessons_timer.setInterval(LESSONS_INTERVAL_MS)
        self._lessons_timer.timeout.connect(self._run_experiential_learning)
        self._lessons_timer.start()
        QTimer.singleShot(
            LESSONS_STARTUP_DELAY_MS, self._run_experiential_learning)

    def _run_experiential_learning(self):
        """Distill traces on a worker thread; never blocks the UI."""
        try:
            import experiential_memory
            if not experiential_memory.should_extract():
                return
        except Exception:
            return

        def work():
            try:
                print(f"[lessons] {experiential_memory.extract_lessons()}")
            except Exception as exc:
                print(f"[lessons] failed: {exc}")

        threading.Thread(target=work, daemon=True).start()

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
        """Begin the ambient motion (card entrance) and HUD state polling.

        Called once after the window is shown. When ANIMATIONS_ENABLED is
        false the card entrance is skipped; the HUD still shows state colours.
        """
        if self._hud_poll_timer is None:
            timer = QTimer(self)
            timer.setInterval(250)
            timer.timeout.connect(self._sync_hud_state)
            timer.start()
            self._hud_poll_timer = timer
        self._sync_hud_state()

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
        if self._memory_overlay is not None:
            self._memory_overlay.setGeometry(self.rect())
        if getattr(self, "_central_widget", None) is not None:
            self._drop_overlay.setGeometry(self._central_widget.rect())
            if self._drop_overlay.isVisible():
                self._drop_overlay.raise_()

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
                and self._memory_overlay is not None):
            self._close_memory_manager()
            return
        if (event.key() == Qt.Key.Key_Escape
                and self._settings_overlay is not None):
            self._close_settings()
            return
        if (event.key() == Qt.Key.Key_Escape
                and (self._processing or self._voice_active())):
            self._request_stop()
            return
        super().keyPressEvent(event)

    # ------------------------------------------------------------------
    def _build_input_area(self) -> QWidget:
        outer = QWidget()
        outer.setObjectName("composer")
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

        self.input_pill = QFrame()
        self.input_pill.setObjectName("promptRow")
        self.input_pill.setStyleSheet(
            f"QFrame#promptRow {{ background: {COLOR_CODE_BG}; "
            f"border: 1px solid {COLOR_BORDER}; border-radius: 12px; }}")
        pill_row = QHBoxLayout(self.input_pill)
        pill_row.setContentsMargins(12, 8, 10, 8)
        pill_row.setSpacing(8)

        self.prompt_prefix = QLabel("› ")
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
        _ensure_activity_filter()

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

        self.send_button = QPushButton("Send")
        self.send_button.setObjectName("sendButton")
        self.send_button.setIcon(_svg_icon("send", 16, COLOR_ACCENT))
        self.send_button.setFixedSize(82, 38)
        self.send_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.send_button.setStyleSheet(
            f"""
            QPushButton {{
                background: {COLOR_ACCENT_DIM}; border: 1px solid {COLOR_ACCENT};
                border-radius: 8px; color: {COLOR_TEXT_USER};
                font-family: "{FONT_UI}"; font-size: 11px; font-weight: bold;
            }}
            QPushButton:hover {{ background: {COLOR_SELECTED}; }}
            QPushButton:pressed {{ background: {COLOR_HOVER}; }}
            """
        )
        self.send_button.clicked.connect(lambda: self.send())
        pill_row.addWidget(self.send_button)

        self.voice_button = QPushButton("F9 push-to-talk")
        self.voice_button.setObjectName("voiceButton")
        self.voice_button.setIcon(_svg_icon("mic", 16, COLOR_TEXT_MID))
        self.voice_button.setFixedSize(142, 38)
        self.voice_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.voice_button.setStyleSheet(
            f"""
            QPushButton#voiceButton {{
                background: {COLOR_HOVER}; border: 1px solid {COLOR_BORDER};
                border-radius: 8px; color: {COLOR_TEXT_MID};
                font-family: "{FONT_UI}"; font-size: 10px;
            }}
            QPushButton#voiceButton:hover {{ color: {COLOR_ACCENT}; border-color: {COLOR_ACCENT}; }}
            QPushButton#voiceButton:pressed {{ background: {COLOR_SELECTED}; }}
            QPushButton#voiceButton[micActive="true"] {{
                color: {COLOR_DANGER}; border-color: {COLOR_DANGER};
            }}
            """
        )
        self.voice_button.clicked.connect(self._toggle_voice)
        pill_row.addWidget(self.voice_button)

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

    def _request_stop(self):
        """Cancel audio or ask the running agent turn to abort."""
        if self._voice_active():
            self._cancel_voice()
            return
        if not (self._processing and self._turn_cancellable):
            return
        if self._stop_requested:
            return
        self._stop_requested = True
        self._stop_event.set()
        screen_copilot.cancel_current_task()
        self._detach_copilot_bridge()
        self._interrupt_speech()
        self._set_voice_state("cancelled")
        self.set_status("Stopping…", busy=True)

    def _cancel_voice(self, resume_wake=True):
        self._voice_generation += 1
        had_stt = (self._recording or self._transcribing
                   or self._continuous_listening)
        self._recording = False
        self._transcribing = False
        self._continuous_listening = False
        if self._continuous_enabled:
            self._continuous_paused = True
        if self._continuous_timer is not None:
            self._continuous_timer.stop()
        if had_stt:
            try:
                get_voice().cancel_recording()
            except Exception as exc:
                print(f"[voice] cancel failed: {exc}")
        self._interrupt_speech()
        self._set_voice_state("cancelled")
        self.set_status("Cancelled", busy=False)
        if resume_wake and not self._shutting_down:
            self._resume_wake_word()

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
            if self._is_ctrl_c(event):
                return self._on_ctrl_c()
            if (event.key() == Qt.Key.Key_Escape
                    and (self._processing or self._voice_active())):
                self._request_stop()
                return True
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                    return False  # let QPlainTextEdit insert the newline
                self.send()
                return True
        return super().eventFilter(obj, event)

    @staticmethod
    def _is_ctrl_c(event):
        modifiers = event.modifiers()
        return (event.key() == Qt.Key.Key_C
                and bool(modifiers & Qt.KeyboardModifier.ControlModifier)
                and not modifiers & (Qt.KeyboardModifier.AltModifier
                                     | Qt.KeyboardModifier.ShiftModifier))

    def _on_ctrl_c(self):
        """Shell interrupt: copy wins over interrupting when text is selected."""
        if self.input_field.textCursor().hasSelection():
            return False
        if self._voice_active():
            self._cancel_voice()
        elif self._processing:
            if not self._turn_cancellable:
                return False
            self._request_stop()
        else:
            self.input_field.clear()
        self.chat.add_message(SystemMessage("^C"))
        return True

    # ------------------------------------------------------------------
    # Send / receive
    # ------------------------------------------------------------------
    def set_status(self, text, busy=False):
        label = " ".join((text or "").split())
        self.setWindowTitle(f"KESTREL — {label}" if label else "KESTREL")
        header = getattr(self, "header_status", None)
        if header is not None:
            header.setText(label or "Ready")
        self._sync_hud_state()

    # The pet's sprite for each reactor state. States not listed — LISTENING,
    # CONTINUOUS, ERROR — leave it plainly awake.
    _PET_BY_STATE = {"THINKING": "thinking", "SPEAKING": "speaking"}
    # Busy normally means the laptop. These three tools are the agent reading
    # rather than planning, so they swap the pose to the book.
    _PET_READ_TOOLS = frozenset(
        {"read_file", "rag_search", "read_and_summarize"})
    # Step lines arrive as "Action: <tool>(<input>) -> Result: ...".
    _STEP_TOOL_LINE = re.compile(r"^Action:\s*([A-Za-z_][A-Za-z0-9_]*)\(")

    def _last_step_tool(self):
        """The tool named by the newest live step line, or None.

        Read back through _live_steps instead of keeping a flag, so it cannot
        outlive the turn — every turn start clears the list.
        """
        for line in reversed(getattr(self, "_live_steps", []) or []):
            match = self._STEP_TOOL_LINE.match(line)
            if match:
                return match.group(1)
        return None

    def _pet_state_for(self, name):
        if (name == "THINKING"
                and self._last_step_tool() in self._PET_READ_TOOLS):
            return "reading"
        return self._PET_BY_STATE.get(name, "idle")

    def _sync_hud_state(self):
        if (self._voice_state == "speaking"
                and not _tts_is_speaking()):
            self._voice_state = "idle"
            self._update_voice_indicator()
        stage = getattr(self, "owl_stage", None)
        if stage is None:
            return
        name = self._resolve_hud_state()
        pet_state = self._pet_state_for(name)
        stage.set_state(name, pet_state)
        bar = getattr(self, "state_bar", None)
        if bar is not None:
            bar.set_state(name)

    def _note_pet_activity(self):
        """Tell the pet the user or the agent just did something."""
        stage = getattr(self, "owl_stage", None)
        if stage is not None:
            stage.note_pet_activity()

    def _resolve_hud_state(self):
        state = getattr(self, "_voice_state", "idle")
        if (time.monotonic() < getattr(self, "_hud_error_until", 0.0)
                or state == "error"):
            return "ERROR"
        if state == "cancelled":
            return "IDLE"
        if getattr(self, "_recording", False) or state == "listening":
            if getattr(self, "_continuous_listening", False):
                return "CONTINUOUS"
            return "LISTENING"
        if getattr(self, "_transcribing", False) or state == "transcribing":
            return "THINKING"
        if state == "speaking" or _tts_is_speaking():
            return "SPEAKING"
        title = self.windowTitle().lower()
        busy = (
            getattr(self, "_processing", False)
            or state == "thinking"
            or any(
                word in title for word in (
                    "thinking", "working", "reading", "verifying",
                    "transcribing", "browsing", "stopping"))
        )
        return "THINKING" if busy else "IDLE"

    def nativeEvent(self, event_type, message):
        """Collapse the Win32 caption and re-claim the resize borders.

        Handled messages are the ones that make the frameless shell behave like
        a normal window: WM_NCCALCSIZE gives us the client area, WM_NCHITTEST
        turns the edge pixels back into resize grips. Everything else goes to
        Qt's own Windows back end untouched.
        """
        if self._chrome is not None:
            handled, result = self._chrome.handle(event_type, message)
            if handled:
                return True, result
        return super().nativeEvent(event_type, message)

    def toggle_maximize(self):
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

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
        if (event.type() in (QEvent.Type.WindowStateChange,
                             QEvent.Type.WindowTitleChange)
                and self._title_bar is not None):
            self._title_bar.sync_state()
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

    def send(self, persist_history=True):
        if self._processing:
            return
        text = self._get_input_text()
        attached = list(self.attachments)
        if not text and not attached:
            return
        self._note_pet_activity()
        # Every persisted submission joins the prompt history.
        if persist_history:
            self.input_field.history.add(text)

        # A new command cuts the assistant off mid-sentence and stops the
        # hands-free mic; the reply will re-arm it when the turn finishes.
        self._interrupt_speech()
        self._cancel_continuous_listening(resume_wake=False)

        # Slash commands bypass attachments (match agent_gui.send).
        if persist_history and text.startswith("/") and not attached:
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
        if persist_history:
            self._start_agent_turn(text, attached)
        else:
            self._start_agent_turn(
                text, attached, persist_history=False)

    def _interrupt_speech(self):
        """Cut off TTS immediately; a missing backend is not fatal."""
        self._speech_generation += 1
        if self._voice_state == "speaking":
            self._set_voice_state("idle")
        if not self._tts_ready:
            return
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
        if not self._agent_ready:
            return  # still preloading; there is no loaded task context yet
        try:
            import agent as _agent
            if not _agent.starts_new_task(text):
                return
        except Exception:
            return
        self._reset_task_context()

    def _start_agent_turn(self, text, attached_paths=None,
                          persist_history=True):
        """Kick off a background agent turn for `text`."""
        if persist_history:
            self._reset_task_context_if_new(text)
        # A new message starts a fresh, uncancelled turn: never inherit the
        # previous turn's stop request or its already-set event.
        self._stop_requested = False
        self._stop_event = threading.Event()
        self._detach_copilot_bridge()
        bridge = CopilotApprovalBridge(self)
        bridge.requested.connect(self._show_copilot_approval)
        self._copilot_bridge = bridge
        self._processing = True
        self._turn_cancellable = True
        self._set_voice_state("thinking")
        self.input_field.setEnabled(False)
        self.set_status("Reading attachments…" if attached_paths else "Thinking…",
                        busy=True)
        self._live_steps = []
        self.task_workspace.set_task_title(text)
        self.task_workspace.set_activity(["Thinking…"])
        self._thinking_message = SystemMessage("Thinking…")
        self.chat.add_message(self._thinking_message, fade=False)

        self._worker = AgentWorker(
            text, self.conversation_history, attached_paths,
            stop_event=self._stop_event,
            persist_history=persist_history,
            copilot_approval_handler=bridge,
        )
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
        self.task_workspace.set_activity(self._live_steps)
        # The pet's pose depends on which tool just reported, and set_status
        # does not fire mid-turn, so each step has to re-sync it.
        self._note_pet_activity()
        self._sync_hud_state()

    def _clear_thinking_message(self):
        message = self._thinking_message
        self._thinking_message = None
        self.chat.remove_message(message)

    def _end_turn(self):
        self._detach_copilot_bridge()
        self._processing = False
        self._note_pet_activity()
        self._turn_cancellable = False
        self._stop_requested = False
        worker = self._worker
        self._worker = None
        if worker is not None:
            # Keep the emitter alive while its thread is still unwinding; a
            # queued signal must never outlive its QObject.
            thread = getattr(worker, "_thread", None)
            if thread is not None and thread.is_alive():
                self._retired_workers.append(worker)
        self._retired_workers = [
            item for item in self._retired_workers
            if getattr(item, "_thread", None) is not None
            and item._thread.is_alive()
        ]
        self.input_field.setEnabled(True)
        self.input_field.setFocus()
        self.set_status("Ready", busy=False)
        # A scheduled task may have fired while this turn was running.
        self._drain_scheduled_queue()

    def _on_turn_completed(self, payload):
        message, step_log = payload
        cancelled = self._stop_requested
        self._last_step_log = step_log
        self._clear_thinking_message()
        self.chat.add_message(AssistantMessage(message, step_log=step_log))
        self.task_workspace.set_activity(step_log)
        if cancelled:
            self._set_voice_state("cancelled")
        else:
            self._speak_reply(message)
        self._end_turn()
        if not cancelled:
            self._schedule_continuous_listen()

    def _on_turn_failed(self, error_text):
        cancelled = self._stop_requested
        self._clear_thinking_message()
        self.chat.add_message(AssistantMessage(error_text))
        if not cancelled:
            self._hud_error_until = time.monotonic() + 2.5
        self._end_turn()
        self._set_voice_state("cancelled" if cancelled else "error")
        if not cancelled:
            QTimer.singleShot(2600, self._sync_hud_state)
            self._schedule_continuous_listen()

    def _speak_reply(self, message):
        """Speak a reply if TTS_AUTO_SPEAK is on. Matches agent_gui.handle_result.

        speak() queues to its own worker thread and returns immediately, so no
        extra threading is needed here.
        """
        if not message:
            self._set_voice_state("idle")
            return
        self._speech_generation += 1
        generation = self._speech_generation
        try:
            auto_speak = str(
                read_config_file_raw().get("TTS_AUTO_SPEAK", "true")
            ).strip().lower() in ("true", "1", "yes", "on")
        except Exception:
            auto_speak = True
        if not auto_speak:
            self._set_voice_state("idle")
            return
        if not self._tts_ready:
            self._set_voice_state("idle")
            return
        try:
            voice = get_voice_output()
            voice.set_error_callback(
                lambda error: self._tts_error.emit(generation, str(error)))
            queued = voice.speak(message)
            if not queued:
                error = voice.last_error()
                if error:
                    self._on_tts_error(generation, error)
                else:
                    self._set_voice_state("idle")
            else:
                self._set_voice_state("speaking")
        except Exception as e:
            self._on_tts_error(generation, f"speak failed: {e}")

    def _on_tts_error(self, generation, message):
        """Show the first speech failure as a dim system line."""
        if generation != self._speech_generation:
            return
        print(f"[tts] {message}")
        self._set_voice_state("error")
        self._hud_error_until = time.monotonic() + 2.5
        self._sync_hud_state()
        QTimer.singleShot(2600, self._sync_hud_state)
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
            "/memories": self.open_memory_manager,
            "/tasks": self.open_schedule_dialog,
            "/index": self.on_index_documents,
            "/index-docs": self.on_index_documents,
            "/export": self.export_conversation,
            "/copy": self.copy_all,
            "/activity": self.toggle_log,
            "/log": self.toggle_log,
            "/tg-clear": self._clear_telegram_messages,
            "/clear": self.new_chat,
            "/new": self.new_chat,
        }
        handler = ui_commands.get(cmd)
        if handler is not None:
            handler()
            return True

        # /task and /autonomous run the execution engine: the worker calls
        # agent.run_agent_turn, which routes the prefix to the ReAct loop.
        if cmd in ("/task", "/autonomous"):
            if not args:
                self.chat.add_message(UserMessage(text))
                self.chat.add_message(AssistantMessage(
                    "ERROR: /task needs a goal."))
                return True
            self._reset_task_context()
            self.chat.add_message(UserMessage(text))
            self._start_agent_turn(text)
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
        self._turn_cancellable = False
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
    # Memory Manager overlay
    # ------------------------------------------------------------------
    def open_memory_manager(self):
        self._close_settings()
        if self._memory_overlay is not None:
            self._memory_overlay.deleteLater()
            self._memory_overlay = None
        overlay = MemoryManager(self, on_close=self._close_memory_manager)
        self._memory_overlay = overlay
        overlay.show()
        overlay.raise_()

    def _close_memory_manager(self):
        if self._memory_overlay is not None:
            self._memory_overlay.deleteLater()
            self._memory_overlay = None

    # ------------------------------------------------------------------
    # Settings overlay — in-window, dim backdrop, centred panel
    # ------------------------------------------------------------------
    def open_settings(self):
        self._close_memory_manager()
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
        section(lay, "DeepSeek (text, vision, search)"); field(lay, "API key", "DEEPSEEK_API_KEY", secret=True)
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
        field(lay, "Max steps per turn", "MAX_STEPS_PER_TURN")
        field(lay, "Temperature (0.0-1.0)", "TEMPERATURE")
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
        mem_btn = footer_button("Memories")
        mem_btn.clicked.connect(self.open_memory_manager)
        br.addWidget(default_btn)
        br.addWidget(mem_btn)
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
    # Persistent task ledger
    # ------------------------------------------------------------------
    def _load_task_ledger(self):
        try:
            self.task_ledger = task_ledger.TaskLedger(self._task_ledger_path)
            self._task_ledger_error = ""
        except task_ledger.TaskLedgerError as exc:
            self.task_ledger = None
            self._task_ledger_error = str(exc)
        self._refresh_task_ledger()

    def _refresh_task_ledger(self):
        if self.task_ledger is None:
            tasks = []
            focused_task_id = None
        else:
            tasks = self.task_ledger.list_tasks()
            focused_task_id = self.task_ledger.focused_task_id
        self.task_ledger_changed.emit({
            "tasks": tasks,
            "focused_task_id": focused_task_id,
            "error": self._task_ledger_error,
        })

    def _add_task_ledger_card(self):
        if self._task_ledger_card is not None:
            self.task_ledger_changed.disconnect(
                self._task_ledger_card.set_ledger_state)
            self.chat.remove_message(self._task_ledger_card)
        card = TaskLedgerCard()
        card.focus_requested.connect(self._on_task_focus_requested)
        card.action_requested.connect(self._on_task_action_requested)
        self.task_ledger_changed.connect(card.set_ledger_state)
        self.chat.add_message(card, fade=False)
        self._task_ledger_card = card
        self._refresh_task_ledger()

    def _on_task_focus_requested(self, task_id):
        self._on_task_action_requested("focus", task_id)

    def _on_task_action_requested(self, action, task_id):
        if self.task_ledger is None:
            self._task_ledger_error = "Task ledger is unavailable."
            self._refresh_task_ledger()
            return
        try:
            if action == "add":
                title, accepted = QInputDialog.getText(
                    self, "Add task", "Task title:")
                if not accepted:
                    return
                self.task_ledger.add_task(title)
            elif action == "focus":
                self.task_ledger.focus_task(task_id)
            elif action == "start":
                self.task_ledger.activate_task(task_id)
            elif action == "done":
                self.task_ledger.complete_task(task_id)
            elif action == "reopen":
                self.task_ledger.reopen_task(task_id)
            elif action == "delete":
                reply = QMessageBox.question(
                    self, "Delete task",
                    "Delete this task from the persistent ledger?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if reply != QMessageBox.StandardButton.Yes:
                    return
                self.task_ledger.delete_task(task_id)
            else:
                raise ValueError(f"Unknown task action: {action}")
        except (task_ledger.TaskLedgerError, OSError, ValueError, KeyError) as exc:
            self._task_ledger_error = str(exc) or "Task ledger update failed."
            self._refresh_task_ledger()
            return
        self._task_ledger_error = ""
        self._refresh_task_ledger()

    # ------------------------------------------------------------------
    # Chat / UI handlers (formerly driven by the sidebar)
    # ------------------------------------------------------------------
    def new_chat(self):
        """Clear the on-screen chat and the saved conversation."""
        if self._processing and self._turn_cancellable:
            self._stop_requested = True
            self._stop_event.set()
            screen_copilot.cancel_current_task()
            self._detach_copilot_bridge()
            worker = self._worker
            if worker is not None:
                with contextlib.suppress(RuntimeError):
                    worker.completed.disconnect(self._on_turn_completed)
                with contextlib.suppress(RuntimeError):
                    worker.failed.disconnect(self._on_turn_failed)
                with contextlib.suppress(RuntimeError):
                    worker.step.disconnect(self._on_worker_step)
            self._end_turn()
        else:
            self._detach_copilot_bridge()
        self._show_chat_page()
        self.conversation_history.clear()
        try:
            get_agent().save_conversation_history(self.conversation_history)
        except Exception:
            pass
        if self._task_ledger_card is not None:
            self.task_ledger_changed.disconnect(
                self._task_ledger_card.set_ledger_state)
        self.chat.clear()
        self._task_ledger_card = None
        self._add_task_ledger_card()
        self._telegram_messages.clear()
        self._last_step_log = None
        self._live_steps = []
        self._thinking_message = None
        self.task_workspace.set_activity_idle()
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
                lines.append(f"Kestrel: {text}")
            else:
                lines.append(f"— {text}")
        return "\n\n".join(lines)

    def copy_all(self):
        """Copy the whole conversation to the clipboard."""
        if not self.chat.chat_log:
            self.show_notice("Nothing to copy")
            return
        QApplication.clipboard().setText(self._chat_as_text())
        self.show_notice("Copied to clipboard")

    def show_notice(self, text: str):
        """Dim terminal notice that removes itself.

        Housekeeping like a clipboard ack is not conversation: it would pile up
        in the transcript, the scroll range and every "Copy all" export.
        """
        message = SystemMessage(text)
        self.chat.add_message(message, fade=False)

        def dismiss(widget=message):
            try:
                parent = widget.parent()
            except RuntimeError:
                return
            if parent is not None:
                self.chat.remove_message(widget)

        QTimer.singleShot(self.NOTICE_MS, dismiss)

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
        title = "Kestrel Conversation"
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
        self.chat.add_message(ReadyMessage("KESTREL Ready..."), fade=False)
        self.chat.add_message(SystemMessage(
            "System Online. Type /help for commands."), fade=False)
        self._add_task_ledger_card()
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
    # Screen copilot
    # ------------------------------------------------------------------
    def _show_copilot_approval(self, payload):
        bridge = self.sender()
        if bridge is None:
            future = payload.get("future")
            if future is not None and not future.done():
                with contextlib.suppress(Exception):
                    future.set_result(False)
            return
        if bridge is not self._copilot_bridge:
            bridge.resolve(payload, False)
            return
        if (self._shutting_down or not self._processing
                or self._stop_event.is_set()):
            bridge.resolve(payload, False)
            return
        if self.chat._body.findChildren(CopilotApprovalCard):
            bridge.resolve(payload, False)
            return

        self._show_chat_page()
        card = CopilotApprovalCard(payload)
        self.chat.add_message(card, fade=False)

        def resolved(approved, request=payload, card=card, bridge=bridge):
            bridge.resolve(request, approved)
            self.chat.remove_message(card)
            if self._processing:
                self.set_status(
                    "Continuing\u2026" if approved else "Plan denied\u2026",
                    busy=True)

        card.resolved.connect(resolved)
        activity = list(self._live_steps[-4:])
        activity.append("Waiting for plan approval\u2026")
        self.task_workspace.set_activity(activity)
        self.set_status("Waiting for Screen Copilot approval\u2026", busy=True)

    def _clear_copilot_approval(self):
        for card in list(self.chat._body.findChildren(CopilotApprovalCard)):
            if card._resolved:
                self.chat.remove_message(card)
            else:
                card.resolve(False)

    def _detach_copilot_bridge(self):
        bridge = self._copilot_bridge
        self._copilot_bridge = None
        if bridge is not None:
            bridge.close()
        self._clear_copilot_approval()

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
            os_control.set_gui_capture_handler(self._capture_gui_window)
            os_control.ensure_kill_switch()
            try:
                import browser_agent
                os_control.add_kill_callback(
                    browser_agent.cancel_current_task)
            except Exception:
                pass
            os_control.add_kill_callback(screen_copilot.cancel_current_task)
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

    def _capture_gui_window(self):
        """Called from the agent worker; blocks until Qt has grabbed the window.

        Returns (png_bytes, width, height) in physical pixels, or None when the
        window is not mapped. Only this window is captured, never the desktop,
        so the agent can actually see its own GUI.
        """
        event = threading.Event()
        holder = []
        self._gui_capture_request.emit(event, holder)
        if not event.wait(timeout=2.5):
            raise RuntimeError("GUI capture timed out")
        return holder[0] if holder else None

    def _apply_gui_capture(self, event, holder):
        try:
            handle = self.windowHandle()
            screen = handle.screen() if handle is not None else None
            screen = screen or QApplication.primaryScreen()
            if screen is None or not self.isVisible():
                return
            shot = screen.grabWindow(int(self.winId()))
            if shot.isNull():
                return
            buffer = QBuffer()
            buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            shot.save(buffer, "PNG")
            holder.append((bytes(buffer.data()), shot.width(), shot.height()))
        finally:
            event.set()

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
        if not WAKE_WORD_STARTUP_ENABLED:
            if not self._wake_watchdog_disabled:
                self._wake_watchdog_disabled = True
                print("[wake] startup disabled pending a trained VEX model; "
                      "F9 push-to-talk is available")
            return
        if not self._wake_ready:
            self.set_status(
                "Wake word unavailable" if self._wake_failed
                else "Voice loading…", busy=False)
            return
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
                self._wake_watchdog_disabled = True
                return
            self._ensure_wake_watchdog()

            model = str(cfg.get("WAKE_WORD_MODEL") or "vex")
            threshold = float(cfg.get("WAKE_WORD_THRESHOLD", 0.5))
            cooldown = float(cfg.get("WAKE_WORD_COOLDOWN", 1.5))
            device_raw = str(cfg.get("WAKE_WORD_DEVICE", "") or "").strip()
            if device_raw:
                device_index = int(device_raw)
            else:
                # PyAudio otherwise uses the Windows default input, which is
                # a loopback cable on this machine and cannot hear the user.
                try:
                    device_index = get_voice().resolve_input_device()[0]
                except Exception as exc:
                    print(f"[wake] microphone selection failed: {exc}")
                    device_index = None

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
            self._wake_restart_attempts = 0
            self._ensure_wake_watchdog()
            print(
                f"[wake] listening ({service.model_spec}, "
                f"{service.inference_framework})")
        except Exception as e:
            self._wake_word_service = None
            print(f"[wake] failed to start: {e}")
            if not self._wake_word_error_shown:
                self._wake_word_error_shown = True
                self.chat.add_message(AssistantMessage(f"Wake word disabled: {e}"))

    def _ensure_wake_watchdog(self):
        if self._wake_watchdog is not None:
            return
        timer = QTimer(self)
        timer.setInterval(15000)
        timer.timeout.connect(self._check_wake_word_health)
        timer.start()
        self._wake_watchdog = timer

    def _check_wake_word_health(self):
        """Restart the listener if it stopped without a deliberate shutdown."""
        if self._wake_watchdog_disabled:
            return
        service = self._wake_word_service
        if service is None:
            if self._wake_restart_attempts >= 3:
                self._wake_watchdog_disabled = True
                return
            self._wake_restart_attempts += 1
            self._start_wake_word_service()
            return
        try:
            running = bool(service.is_running())
        except Exception:
            running = False
        if running:
            self._wake_restart_attempts = 0
            return
        print("[wake] listener stopped unexpectedly; restarting")
        self._wake_word_service = None
        try:
            service.stop()
        except Exception:
            pass
        if self._wake_restart_attempts >= 3:
            self._wake_watchdog_disabled = True
            self._on_wake_word_error(
                "listener stopped repeatedly; wake word disabled")
            return
        self._wake_restart_attempts += 1
        self._start_wake_word_service()

    def _on_wake_word(self, score):
        """GUI-thread slot: acknowledge, open the mic, auto-stop after a window."""
        try:
            self._handle_wake_word(score)
        except Exception as exc:
            print(f"[wake] wake flow failed: {exc}")
            self._wake_triggered = False
            self._resume_wake_word()

    def _handle_wake_word(self, score):
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
        if not self._tts_ready:
            return False
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
        try:
            self._hotkey_press()
            # The wake path has no physical key release, so stop it ourselves.
            QTimer.singleShot(self._wake_record_ms, self._wake_word_auto_stop)
        except Exception as exc:
            print(f"[wake] could not open mic: {exc}")
            self._wake_triggered = False
            self._resume_wake_word()

    def _wake_word_auto_stop(self):
        if not self._wake_triggered:
            return
        self._wake_triggered = False
        try:
            self._hotkey_release()
        except Exception as exc:
            print(f"[wake] could not close mic: {exc}")
            self._resume_wake_word()

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
                released = service.pause()
            except Exception:
                return
            if released is False:
                print("[wake] microphone release timed out")

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
                "pauses it. Press F9 to resume."))
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
                "Continuous listening ON \u2014 speak without pressing F9. "
                "60s of silence pauses it."))
            self._schedule_continuous_listen()
        else:
            self._cancel_continuous_listening()
            self.chat.add_message(SystemMessage(
                "Continuous listening OFF \u2014 press F9 to talk."))

    def _start_continuous_if_configured(self):
        if not self._continuous_enabled:
            return
        self.chat.add_message(SystemMessage(
            "Continuous listening ON \u2014 speak without pressing F9. "
            "60s of silence pauses it."))
        self._schedule_continuous_listen(1000)

    def _schedule_continuous_listen(self, delay_ms=300):
        """Re-arm hands-free listening once the turn and its speech are done."""
        if not self._continuous_enabled or self._continuous_paused:
            return
        if self._processing or self._recording or self._transcribing:
            return
        if self._continuous_timer is None:
            self._continuous_timer = QTimer(self)
            self._continuous_timer.setSingleShot(True)
            self._continuous_timer.timeout.connect(self._try_continuous_listen)
        self._continuous_timer.start(max(0, int(delay_ms)))

    def _try_continuous_listen(self):
        if not self._continuous_enabled or self._continuous_paused:
            return
        if self._processing or self._recording or self._transcribing:
            return
        if not self._voice_ready:
            if self._voice_failed:
                return
            self._show_voice_loading()
            self._schedule_continuous_listen(500)
            return
        if self._tts_ready:
            try:
                if get_voice_output().is_speaking():
                    self._schedule_continuous_listen(300)
                    return
            except Exception:
                pass
        self._start_recording(continuous=True)

    def _cancel_continuous_listening(self, resume_wake=True):
        self._voice_generation += 1
        if self._continuous_timer is not None:
            self._continuous_timer.stop()
        had_stt = (self._recording or self._transcribing
                   or self._continuous_listening)
        self._continuous_listening = False
        self._recording = False
        self._transcribing = False
        if had_stt:
            try:
                get_voice().cancel_recording()
            except Exception:
                pass
        self._set_voice_state("idle")
        if resume_wake:
            self._resume_wake_word()
            # /continuous off must not leave the cyan HUD or a stale
            # "Listening" title behind.
            self.set_status("Ready", busy=False)

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
        """Wire tray, hotkey, scheduler and background preloads. Call after
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

        # Proactive watchers reuse the same handler as the scheduler; this is
        # wired after agent.py finishes importing in the background preloader.
        self._start_event_bus()
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

        # Voice, Kokoro, wake word, agent and RAG import in background threads
        # after the window is painted. Each reports back through a signal.
        self._start_background_preload()

        # Section 2 — Telegram document listener. Opt-in via config; config
        # and authorisation problems surface in the transcript, not as a
        # startup exception.
        self._start_telegram_doc_service()

        # Hands-free loop when CONTINUOUS_LISTENING is on at launch.
        self._start_continuous_if_configured()

        # Option A — proactive thoughts. Off by default (screenshots leave the
        # machine); a missing vision key simply produces no messages.
        self._start_spontaneous_thoughts()

    # -- Background preload (boot time) ---------------------------------
    def _start_background_preload(self):
        """Import the heavy modules off the GUI thread after first paint."""
        if self._preload_started:
            return
        self._preload_started = True
        if not self._processing and not self._recording and not self._transcribing:
            self.set_status("Voice loading…", busy=False)
        for name, delay in (
            ("voice", 0.0),
            ("voice_output", 0.0),
            ("wake_word", 0.0),
            ("agent", 0.0),
            ("rag_tool", 3.0),
        ):
            threading.Thread(
                target=self._preload_module,
                args=(name, delay),
                daemon=True,
                name=f"preload-{name}",
            ).start()

    def _preload_module(self, name, delay):
        if delay:
            time.sleep(delay)
        try:
            module = importlib.import_module(name)
            if name == "voice":
                # Import alone leaves Whisper lazy; load it and validate the
                # microphone before F9/wake are marked ready.
                module.warmup()
            print(f"[preload] {name} ready")
            if not self._shutting_down:
                self._preload_ready.emit(name)
        except Exception as exc:
            self._preload_errors[name] = str(exc)
            print(f"[preload] {name} failed: {exc}")
            if not self._shutting_down:
                self._preload_ready.emit(f"{name}:error")

    def _on_preload_ready(self, name):
        """GUI-thread slot: finish wiring that needs an imported module."""
        if name.endswith(":error"):
            module = name[:-len(":error")]
            if module == "voice":
                self._voice_failed = True
                self._pending_f9_start = False
                detail = str(self._preload_errors.get(module, "")).strip()
                self.set_status(
                    "Voice unavailable — check the transcript", busy=False)
                if not self._voice_error_shown:
                    self._voice_error_shown = True
                    message = (
                        "Voice unavailable — Whisper or the microphone "
                        "failed to load")
                    if detail:
                        message += f": {detail}"
                    self.chat.add_message(AssistantMessage(
                        message + "\nCheck the terminal for the full traceback."))
            elif module == "wake_word":
                self._wake_failed = True
                self._wake_ready = False
                self.set_status(
                    "Voice ready — wake word unavailable", busy=False)
            elif module == "voice_output":
                self._tts_ready = False
                if not self._tts_error_shown:
                    self._tts_error_shown = True
                    self.chat.add_message(SystemMessage(
                        "Speech output unavailable — check the terminal."))
            return
        if name == "voice":
            self._voice_ready = True
            print("[voice] ready")
            if (self._wake_preload_done and self._wake_ready
                    and self._wake_word_service is None):
                self._start_wake_word_service()
            if self._f9_held and self._pending_f9_start:
                self._pending_f9_start = False
                self._start_recording()
            elif not self._processing and not self._recording and not self._transcribing:
                if self._wake_ready:
                    text = "Ready"
                elif self._wake_failed:
                    text = "Voice ready — wake word unavailable"
                else:
                    text = "Voice ready — wake word loading…"
                self.set_status(text, busy=False)
        elif name == "voice_output":
            self._tts_ready = True
            try:
                voice = get_voice_output()
                generation = self._speech_generation
                voice.set_error_callback(
                    lambda error: self._tts_error.emit(generation, str(error)))
                voice.warmup()
                print("[tts] warmup requested")
            except Exception as exc:
                print(f"[tts] warmup skipped: {exc}")
        elif name == "agent":
            self._agent_ready = True
            try:
                agent = get_agent()
                agent.register_watcher_callback(
                    self._scheduled_triggered.emit)
                if not self._processing:
                    history = agent.load_conversation_history()
                    if history and not self.conversation_history:
                        self.conversation_history.extend(history)
                        self.chat.add_message(SystemMessage(
                            f"Resumed — {len(history)} messages from last session."))
            except Exception as exc:
                print(f"[agent] background init failed: {exc}")
        elif name == "wake_word":
            self._wake_ready = True
            self._wake_failed = False
            self._wake_preload_done = True
            if self._voice_ready:
                self._start_wake_word_service()
            if self._voice_ready and not self._processing and not self._recording and not self._transcribing:
                self.set_status("Ready", busy=False)

    # -- Telegram document listener (Section 2) -------------------------
    def _start_event_bus(self):
        if self._event_bus is not None:
            return
        try:
            from event_bus import EventBus
            bus = EventBus(on_result=self._event_result.emit)
            if bus.start():
                self._event_bus = bus
        except Exception as exc:
            self._show_event_result(f"Event bus not started: {exc}")

    def _on_about_to_quit(self):
        self._shutting_down = True
        self._cancel_voice(resume_wake=False)
        wake_service = self._wake_word_service
        self._wake_word_service = None
        if wake_service is not None:
            try:
                wake_service.stop()
            except Exception:
                pass
        self._stop_requested = True
        self._stop_event.set()
        self._detach_copilot_bridge()
        screen_copilot.shutdown()
        self._stop_event_bus()

    def _stop_event_bus(self):
        bus, self._event_bus = self._event_bus, None
        if bus is not None:
            bus.stop()

    def _show_event_result(self, text):
        self.chat.add_message(UntrustedTextMessage(text, source="EVENT BUS"))

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
            message = UntrustedTextMessage(
                f"{sender} — {file_name}\n\n{payload}",
                source="TELEGRAM DOCS")
            self.chat.add_message(message)
            self._telegram_messages.append(message)
        elif kind in ("rejected", "error"):
            self.chat.add_message(UntrustedTextMessage(
                f"{kind}: {payload}", source="TELEGRAM DOCS"))
        elif kind == "service_error":
            self.chat.add_message(UntrustedTextMessage(
                f"listener stopped: {payload}", source="TELEGRAM DOCS"))
            self._stop_telegram_doc_service()

    def _clear_telegram_messages(self):
        for message in self._telegram_messages:
            self.chat.remove_message(message)
        self._telegram_messages.clear()

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
        font = QFont(FONT_MONO)
        font.setPixelSize(34)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QPen(QColor(COLOR_BG)))
        painter.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, "K")
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
            icon.setToolTip("Kestrel")

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

    def _live_agent_workers(self):
        workers = list(self._retired_workers)
        if self._worker is not None:
            workers.insert(0, self._worker)
        return [
            worker for worker in workers
            if getattr(worker, "_thread", None) is not None
            and worker._thread.is_alive()
        ]

    def _quit_when_agent_workers_stop(self):
        if self._live_agent_workers():
            QTimer.singleShot(50, self._quit_when_agent_workers_stop)
            return
        QApplication.instance().quit()

    def quit_app(self):
        if self._shutting_down:
            return
        self._shutting_down = True
        self._cancel_voice(resume_wake=False)
        self._stop_requested = True
        self._stop_event.set()
        self._detach_copilot_bridge()
        screen_copilot.shutdown()
        self._stop_event_bus()
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
            os_control.set_gui_capture_handler(None)
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
        self._quit_when_agent_workers_stop()

    def closeEvent(self, event):
        # Match the old GUI: closing the window hides to tray, it does not exit.
        event.ignore()
        if self._voice_active():
            self._cancel_voice(resume_wake=False)
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

    def _show_voice_loading(self):
        self.set_status("Voice loading…", busy=False)
        if not self._voice_loading_notice_shown:
            self._voice_loading_notice_shown = True
            self.chat.add_message(SystemMessage(
                "Voice loading… F9 activates once the speech models "
                "finish importing."))

    def _on_hotkey_changed(self, pressed):
        """GUI-thread slot for F9 press/release."""
        if pressed:
            self._f9_held = True
            if not self._voice_ready:
                if self._voice_failed:
                    self.set_status(
                        "Voice unavailable — check the terminal", busy=False)
                else:
                    self._pending_f9_start = True
                    self._show_voice_loading()
                return
            if self._continuous_enabled:
                # F9 is advertised as the way out of "Mic sleeping".
                self._continuous_paused = False
            self._start_recording()
            return
        self._f9_held = False
        self._pending_f9_start = False
        self._wake_triggered = False
        self._stop_recording()

    def _start_recording(self, continuous=False):
        if not self._voice_ready:
            if self._voice_failed:
                self.set_status(
                    "Voice unavailable — check the terminal", busy=False)
            else:
                self._pending_f9_start = True
                self._show_voice_loading()
            return
        if self._recording or self._transcribing or self._processing:
            return
        self._voice_generation += 1
        generation = self._voice_generation
        self._recording = True
        self._transcribing = False
        self._continuous_listening = bool(continuous)
        self._voice_start_event = threading.Event()
        start_event = self._voice_start_event
        self._set_voice_state("listening")
        self.set_status(
            "Listening (Continuous)…" if continuous
            else "Recording… release F9 to transcribe",
            busy=True)

        def work():
            try:
                if generation != self._voice_generation:
                    return
                self._release_audio_before_recording()
                if generation != self._voice_generation or not self._recording:
                    return
                if continuous:
                    get_voice().start_recording(
                        on_transcript=lambda text, reason: self._voice_event.emit(
                            generation, f"continuous:{reason}:{text}"),
                        idle_timeout=CONTINUOUS_IDLE_SECONDS)
                else:
                    get_voice().start_recording()
                if generation == self._voice_generation:
                    self._voice_event.emit(generation, "recording_started")
            except Exception as e:
                if generation == self._voice_generation:
                    self._voice_event.emit(
                        generation, f"error:Mic error: {e}")
            finally:
                start_event.set()

        threading.Thread(target=work, daemon=True).start()

    def _release_audio_before_recording(self):
        """Release the wake mic and TTS output before opening the STT mic."""
        self._pause_wake_word()
        if self._wake_word_service is not None:
            time.sleep(0.1)  # give the OS time to hand the device over
        self._interrupt_speech()
        if not self._tts_ready:
            return
        try:
            tts = get_voice_output()
            wait_idle = getattr(tts, "wait_until_idle", None)
            if callable(wait_idle):
                wait_idle(timeout=2.0)
        except Exception as exc:
            print(f"[voice] could not release TTS output: {exc}")

    def _stop_recording(self):
        if not self._recording or self._transcribing:
            return
        generation = self._voice_generation
        self._recording = False
        self._continuous_listening = False
        self._transcribing = True
        self._set_voice_state("transcribing")
        self.set_status("Transcribing…", busy=True)
        start_event = self._voice_start_event

        def work():
            # A very quick F9 tap can release before start_recording() has
            # opened the mic; wait for that worker instead of racing it.
            start_event.wait(timeout=2.0)
            if generation != self._voice_generation:
                return
            try:
                text = get_voice().stop_and_transcribe()
                if generation == self._voice_generation:
                    self._voice_event.emit(generation, f"transcript:{text}")
            except Exception as e:
                if generation == self._voice_generation:
                    self._voice_event.emit(
                        generation, f"error:Transcribe error: {e}")

        threading.Thread(target=work, daemon=True).start()

    def _on_voice_event(self, generation, payload):
        """GUI-thread slot for recording/transcription results."""
        if generation != self._voice_generation:
            return
        if payload.startswith("error:"):
            detail = payload[len("error:"):].strip() or "Unknown voice error"
            self._voice_generation += 1
            self._recording = False
            self._transcribing = False
            self._continuous_listening = False
            self._set_voice_state("error")
            self.set_status(detail, busy=False)
            self.chat.add_message(AssistantMessage(f"Voice error: {detail}"))
            self._resume_wake_word()
            return
        if payload == "recording_started":
            return
        if payload.startswith("continuous:"):
            _, reason, text = payload.split(":", 2)
            self._voice_generation += 1
            self._recording = False
            self._transcribing = False
            self._continuous_listening = False
            if reason == "error":
                self._continuous_paused = True
                self._set_voice_state("error")
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
                self._set_voice_state("idle")
                self._resume_wake_word()
                sleeping = "Mic sleeping. Press F9 to talk."
                self.set_status(sleeping, busy=False)
                self.chat.add_message(SystemMessage(sleeping))
                return
            self._handle_transcript(text)
            return
        if payload.startswith("transcript:"):
            self._voice_generation += 1
            self._handle_transcript(payload[len("transcript:"):])
            if not self._continuous_enabled:
                self._resume_wake_word()

    def _handle_transcript(self, text):
        # A finished transcript is a command: send it, don't stage it.
        self._recording = False
        self._transcribing = False
        text = (text or "").strip()
        if not text:
            self._set_voice_state("idle")
            self.set_status("No speech detected — try again", busy=False)
            if self._continuous_enabled and not self._continuous_paused:
                self._schedule_continuous_listen()
            return
        self._set_voice_state("thinking")
        self.input_field.setPlainText(text)
        self.input_field.setFocus()
        self.send(persist_history=False)

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
                app_id="Kestrel",
                title="⏰ Reminder",
                msg=reminder_text,
                duration="long",
            ).show()
        except Exception:
            pass


_crash_log_handle = None
_crash_log_lock = threading.Lock()


def crash_log_path() -> Path:
    return Path(get_config_module().memory_path("crash.log"))


def _append_crash_log(text: str) -> None:
    handle = _crash_log_handle
    if handle is None:
        return
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with _crash_log_lock:
            handle.write(f"\n[{stamp}] {text.rstrip()}\n")
            handle.flush()
    except Exception:
        pass


# A turn still running after this much wall time earns a stack snapshot. It is
# a threshold, not a hang detector: a legitimate turn can reach
# MAX_STEPS_PER_TURN (30) provider calls of up to LLM_TIMEOUT (120 s) each, so
# no budget is both useful and impossible to cross honestly. The dump costs one
# all-thread snapshot in crash.log, which is also the answer to "why is this
# 10-minute turn taking ten minutes".
TURN_WATCHDOG_SECONDS = 600.0


def arm_turn_watchdog(timeout=TURN_WATCHDOG_SECONDS):
    """Dump every thread's stack to the crash log if the turn never returns.

    `dump_traceback_later` takes no `all_threads` argument because it always
    dumps all of them, which is the whole point: the thread that is stuck is
    never the one doing the dumping. The dump goes to the crash log, not
    stderr, because a windowed `pythonw` process has no stderr anyone reads --
    that is exactly why the hang this covers left no evidence behind. `repeat`
    stays off so a long turn costs the log one snapshot, not one per interval.
    Each snapshot is headed `Timeout (<budget>!)` followed by one stack per
    thread, which is how to tell it apart from a fatal-signal dump below it.
    """
    handle = _crash_log_handle
    if handle is None:
        return
    faulthandler.dump_traceback_later(timeout, repeat=False, file=handle)


def end_turn_watchdog():
    """Disarm the per-turn dump so an idle app never writes one."""
    faulthandler.cancel_dump_traceback_later()


def install_crash_logging() -> Path | None:
    """Log Python failures and native fault stacks under AgentMemory."""
    global _crash_log_handle
    if _crash_log_handle is not None:
        return Path(_crash_log_handle.name)
    try:
        path = crash_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a", encoding="utf-8", buffering=1)
        handle.write(
            "\n=== Kestrel start "
            f"{datetime.datetime.now().isoformat(timespec='seconds')} ===\n")
        handle.flush()
        _crash_log_handle = handle
        faulthandler.enable(file=handle, all_threads=True)
    except Exception as exc:
        print(f"[crash] log unavailable: {exc}")
        return None

    previous_main_hook = sys.excepthook
    previous_thread_hook = threading.excepthook

    def main_hook(exc_type, exc_value, exc_tb):
        if not issubclass(exc_type, KeyboardInterrupt):
            _append_crash_log(
                "unhandled exception in main thread\n"
                + "".join(traceback.format_exception(
                    exc_type, exc_value, exc_tb)))
        previous_main_hook(exc_type, exc_value, exc_tb)

    def thread_hook(args):
        thread_name = args.thread.name if args.thread is not None else "unknown"
        if not issubclass(args.exc_type, SystemExit):
            _append_crash_log(
                f"unhandled exception in thread {thread_name}\n"
                + "".join(traceback.format_exception(
                    args.exc_type, args.exc_value, args.exc_traceback)))
        previous_thread_hook(args)

    sys.excepthook = main_hook
    threading.excepthook = thread_hook
    print(f"[crash] logging to {path}")
    return path


def main():
    install_crash_logging()
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    # A terminal caret blinks; setCursorWidth(8) gives it the block weight.
    app.setCursorFlashTime(CURSOR_FLASH_MS)
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
    # Let Qt paint the window before services/imports can touch the main thread.
    QTimer.singleShot(0, window.start_services)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
