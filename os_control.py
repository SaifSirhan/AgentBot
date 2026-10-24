"""OS-level automation tools with explicit safety guards.

Capabilities: capture the primary monitor, click absolute screenshot pixels,
type text, and read a pixel colour.

Guards:
* Ctrl+Alt+Shift+Esc latches a kill switch. Every tool refuses to act while
  it is latched, and long typing is split into chunks that re-check it.
* PyAutoGUI's corner failsafe stays enabled.
* Coordinates are validated against the primary monitor before any action.

Coordinates are pixels in the screenshot returned by ``screenshot_background``
(origin at the primary monitor's top-left). Windows DPI awareness is set before
PyAutoGUI/MSS use the screen so 125%/150% scaling does not skew coordinates.
MSS is imported lazily: the rest of the app (and the kill switch) still works
when it is not installed.
"""

from __future__ import annotations

import ctypes
import datetime
import os
import re
import threading
import time
from pathlib import Path
from typing import Callable, Optional, Tuple

# ----------------------------------------------------------------------
# DPI awareness (must happen before PyAutoGUI touches the screen)
# ----------------------------------------------------------------------
def _enable_dpi_awareness() -> None:
    if os.name != "nt":
        return
    try:
        user32 = ctypes.windll.user32
        if hasattr(user32, "SetProcessDpiAwarenessContext"):
            # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
            if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
                return
    except Exception:
        pass
    try:
        # 2 = PROCESS_PER_MONITOR_DPI_AWARE
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


_enable_dpi_awareness()

import pyautogui  # noqa: E402 - after DPI setup on purpose
import pyperclip  # noqa: E402

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.03


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def get_dpi_scale() -> float:
    """Effective DPI scale of the primary monitor (1.0, 1.25, 1.5, ...)."""
    if os.name != "nt":
        return 1.0
    try:
        user32 = ctypes.windll.user32
        monitor = user32.MonitorFromPoint(_POINT(0, 0), 1)  # primary
        dpi_x = ctypes.c_uint()
        dpi_y = ctypes.c_uint()
        ctypes.windll.shcore.GetDpiForMonitor(
            monitor, 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y))
        if dpi_x.value:
            return dpi_x.value / 96.0
    except Exception:
        pass
    try:
        dpi = ctypes.windll.user32.GetDpiForSystem()
        if dpi:
            return dpi / 96.0
    except Exception:
        pass
    try:
        dc = ctypes.windll.user32.GetDC(0)
        if dc:
            dpi = ctypes.windll.gdi32.GetDeviceCaps(dc, 88)  # LOGPIXELSX
            ctypes.windll.user32.ReleaseDC(0, dc)
            if dpi:
                return dpi / 96.0
    except Exception:
        pass
    return 1.0


def _dpi_awareness() -> int:
    """0 = unaware, 1 = system aware, 2 = per-monitor aware, -1 = unknown."""
    if os.name != "nt":
        return -1
    try:
        value = ctypes.c_int()
        ctypes.windll.shcore.GetProcessDpiAwareness(None, ctypes.byref(value))
        return int(value.value)
    except Exception:
        return -1


def dpi_info() -> dict:
    return {
        "scale": round(get_dpi_scale(), 3),
        "awareness": _dpi_awareness(),
    }


# ----------------------------------------------------------------------
# Errors
# ----------------------------------------------------------------------
class AutomationAborted(Exception):
    """The kill switch or PyAutoGUI failsafe stopped the action."""


class OSControlUnavailable(Exception):
    """A required OS-control dependency is missing or unusable."""


# ----------------------------------------------------------------------
# Kill switch: Ctrl+Alt+Shift+Esc
# ----------------------------------------------------------------------
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_SHIFT = 0x10
VK_ESCAPE = 0x1B

_kill_lock = threading.RLock()
_kill_event = threading.Event()
_kill_thread: Optional[threading.Thread] = None
_kill_stop: Optional[threading.Event] = None
_kill_callbacks: list = []


def _watch_kill_switch(stop: threading.Event) -> None:
    triggered = False
    while not stop.wait(0.05):
        try:
            pressed = all(
                ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000
                for vk in (VK_CONTROL, VK_MENU, VK_SHIFT, VK_ESCAPE)
            )
        except Exception:
            return
        if pressed and not triggered:
            trigger_kill_switch()
        triggered = bool(pressed)


def ensure_kill_switch() -> bool:
    """Start the global hotkey watcher once. Returns True if it is running."""
    global _kill_thread, _kill_stop
    if os.name != "nt":
        return False
    with _kill_lock:
        if _kill_thread is not None and _kill_thread.is_alive():
            return True
        _kill_stop = threading.Event()
        _kill_thread = threading.Thread(
            target=_watch_kill_switch,
            args=(_kill_stop,),
            name="os-kill-switch",
            daemon=True,
        )
        _kill_thread.start()
        return True


def stop_kill_switch() -> None:
    global _kill_thread, _kill_stop
    with _kill_lock:
        stop = _kill_stop
        thread = _kill_thread
        _kill_stop = None
        _kill_thread = None
    if stop is not None:
        stop.set()
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=1.0)


def trigger_kill_switch() -> None:
    """Latch the kill switch and notify registered abort callbacks."""
    _kill_event.set()
    with _kill_lock:
        callbacks = list(_kill_callbacks)
    for callback in callbacks:
        try:
            callback()
        except Exception:
            pass


def reset_kill_switch() -> None:
    """Clear the latch (tests / explicit recovery; not exposed as a tool)."""
    _kill_event.clear()


def kill_switch_active() -> bool:
    return _kill_event.is_set()


def add_kill_callback(callback: Callable[[], None]) -> None:
    with _kill_lock:
        if callback not in _kill_callbacks:
            _kill_callbacks.append(callback)


def _guard() -> None:
    ensure_kill_switch()
    if _kill_event.is_set():
        raise AutomationAborted(
            "kill switch is latched (Ctrl+Alt+Shift+Esc); restart or reset it")


# ----------------------------------------------------------------------
# GUI hide/show (installed by the Qt app)
# ----------------------------------------------------------------------
_gui_lock = threading.RLock()
_gui_hide: Optional[Callable[[], None]] = None
_gui_show: Optional[Callable[[], None]] = None


def set_gui_visibility_handlers(
    hide: Optional[Callable[[], None]],
    show: Optional[Callable[[], None]],
) -> None:
    global _gui_hide, _gui_show
    with _gui_lock:
        _gui_hide = hide
        _gui_show = show


def _visibility_handlers() -> Tuple[Optional[Callable], Optional[Callable]]:
    with _gui_lock:
        return _gui_hide, _gui_show


# ----------------------------------------------------------------------
# Screen helpers
# ----------------------------------------------------------------------
SCREENSHOT_DIR = (Path(os.environ.get("LOCALAPPDATA") or Path.home())
                  / "AgentMemory" / "screenshots")
SCREENSHOT_SETTLE_SECONDS = 0.35
MAX_TYPE_CHARS = 5_000
TYPE_CHUNK = 40


def _load_mss():
    try:
        import mss as mss_module
        if not hasattr(mss_module, "tools"):
            import mss.tools  # noqa: F401
        return mss_module
    except Exception as exc:
        raise OSControlUnavailable(
            "mss is not installed (pip install mss)") from exc


def _primary_monitor() -> dict:
    mss_module = _load_mss()
    with mss_module.mss() as sct:
        monitor = sct.monitors[1]
    return {
        "left": int(monitor["left"]),
        "top": int(monitor["top"]),
        "width": int(monitor["width"]),
        "height": int(monitor["height"]),
    }


def _parse_xy(input_str: str) -> Tuple[int, int]:
    parts = re.split(r"[,\s]+", (input_str or "").strip())
    parts = [p for p in parts if p]
    if len(parts) != 2:
        raise ValueError('format is "x,y"')
    try:
        return int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise ValueError('x and y must be integers (format "x,y")') from exc


def _validate_xy(x: int, y: int, monitor: dict) -> None:
    if not (0 <= x < monitor["width"]) or not (0 <= y < monitor["height"]):
        raise ValueError(
            f"({x}, {y}) is outside the primary monitor "
            f"({monitor['width']}x{monitor['height']})")


def _dpi_label() -> str:
    scale = get_dpi_scale()
    awareness = {0: "unaware", 1: "system-aware", 2: "per-monitor-aware"}.get(
        _dpi_awareness(), "unknown")
    return f"{scale:.0%} scale, {awareness}"


# ----------------------------------------------------------------------
# Tools
# ----------------------------------------------------------------------
def screenshot_background() -> str:
    """Hide the GUI, capture the primary monitor, restore the GUI."""
    hide_attempted = False
    try:
        _guard()
        mss_module = _load_mss()  # fail before hiding the window
        hide, _ = _visibility_handlers()
        hide_note = "no GUI handler"
        if hide is not None:
            hide_attempted = True
            try:
                hide()
                hide_note = "hidden"
                time.sleep(SCREENSHOT_SETTLE_SECONDS)
            except Exception as exc:
                hide_note = f"hide failed ({exc})"

        _guard()
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = SCREENSHOT_DIR / f"screen_{stamp}.png"

        with mss_module.mss() as sct:
            monitor = dict(sct.monitors[1])
            shot = sct.grab(monitor)
            png = mss_module.tools.to_png(shot.rgb, shot.size)
        path.write_bytes(png)

        return (
            f"Screenshot saved: {path}\n"
            f"Image: {shot.size[0]}x{shot.size[1]} pixels "
            f"(primary monitor at {monitor['left']},{monitor['top']})\n"
            f"DPI: {_dpi_label()}\n"
            f"GUI: {hide_note}\n"
            "Coordinates for click_coordinates/get_pixel_color are pixels "
            "from this image's top-left."
        )
    except AutomationAborted as exc:
        return f"ABORTED: {exc}"
    except OSControlUnavailable as exc:
        return f"ERROR: {exc}"
    except Exception as exc:
        return f"ERROR: screenshot failed: {exc}"
    finally:
        if hide_attempted:
            _, show = _visibility_handlers()
            if show is not None:
                try:
                    show()
                except Exception:
                    pass


def click_coordinates(input_str: str) -> str:
    """Left-click at screenshot pixel coordinates. Format: "x,y"."""
    try:
        _guard()
        x, y = _parse_xy(input_str)
        monitor = _primary_monitor()
        _validate_xy(x, y, monitor)
        absolute_x = monitor["left"] + x
        absolute_y = monitor["top"] + y

        pyautogui.moveTo(absolute_x, absolute_y, duration=0.1)
        _guard()
        pyautogui.click()
        return (f"Clicked ({x}, {y}) — absolute screen "
                f"({absolute_x}, {absolute_y}).")
    except AutomationAborted as exc:
        return f"ABORTED: {exc}"
    except pyautogui.FailSafeException:
        return "ABORTED: mouse moved to a screen corner (PyAutoGUI failsafe)."
    except OSControlUnavailable as exc:
        return f"ERROR: {exc}"
    except ValueError as exc:
        return f"ERROR: {exc}"
    except Exception as exc:
        return f"ERROR: click failed: {exc}"


def type_text(text: str) -> str:
    """Type text into the focused window (ASCII fast path, clipboard Unicode)."""
    try:
        _guard()
        text = "" if text is None else str(text)
        if not text:
            return "ERROR: type_text needs text."
        if len(text) > MAX_TYPE_CHARS:
            return (f"ERROR: text is too long ({len(text)} chars; "
                    f"max {MAX_TYPE_CHARS}).")

        if text.isascii() and not any(c in text for c in "\n\r\t"):
            for start in range(0, len(text), TYPE_CHUNK):
                _guard()
                pyautogui.write(text[start:start + TYPE_CHUNK], interval=0.01)
            return f"Typed {len(text)} characters."

        # Clipboard paste handles Unicode and newlines; restore the clipboard.
        previous = None
        try:
            previous = pyperclip.paste()
        except Exception:
            pass
        try:
            pyperclip.copy(text)
            _guard()
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.15)
        finally:
            if previous is not None:
                try:
                    pyperclip.copy(previous)
                except Exception:
                    pass
        return f"Pasted {len(text)} characters into the focused window."

    except AutomationAborted as exc:
        return f"ABORTED: {exc}"
    except pyautogui.FailSafeException:
        return "ABORTED: mouse moved to a screen corner (PyAutoGUI failsafe)."
    except Exception as exc:
        return f"ERROR: type_text failed: {exc}"


def get_pixel_color(input_str: str) -> str:
    """Return the hex colour at screenshot pixel coordinates. Format: "x,y"."""
    try:
        _guard()
        x, y = _parse_xy(input_str)
        monitor = _primary_monitor()
        _validate_xy(x, y, monitor)
        mss_module = _load_mss()

        with mss_module.mss() as sct:
            shot = sct.grab({
                "left": monitor["left"] + x,
                "top": monitor["top"] + y,
                "width": 1,
                "height": 1,
            })
            try:
                red, green, blue = shot.pixel(0, 0)
            except Exception:
                red, green, blue = shot.rgb[0], shot.rgb[1], shot.rgb[2]

        return f"#{red:02X}{green:02X}{blue:02X} at ({x}, {y})"
    except AutomationAborted as exc:
        return f"ABORTED: {exc}"
    except OSControlUnavailable as exc:
        return f"ERROR: {exc}"
    except ValueError as exc:
        return f"ERROR: {exc}"
    except Exception as exc:
        return f"ERROR: pixel read failed: {exc}"
