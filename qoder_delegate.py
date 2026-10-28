"""Send a build request to the Qoder IDE through clipboard and keyboard.

Qoder's documented Windows shortcut for Open/Close Chat is Ctrl+L. The tool
uses pyperclip for the prompt, pygetwindow to find a verified Qoder window,
and pyautogui to open chat, paste, and submit. It returns immediately after
submission; waiting and verification belong to the ReAct loop.
"""

from __future__ import annotations

import dataclasses
import os
import threading
import time
from typing import Callable, Optional, Sequence

# Qoder docs: Open/Close Chat is Ctrl+L on Windows. The Qoder window is
# verified as foreground before each key, but the chat input's own focus
# cannot be confirmed without vision; the loop's screenshot step catches it.
CHAT_SHORTCUT = ("ctrl", "l")
DEFAULT_GOAL = "Server running or Build successful"
MAX_PROMPT_CHARS = 4_000
MAX_GOAL_CHARS = 500
FOCUS_ATTEMPTS = 3
FOCUS_SETTLE_SECONDS = 0.3
SHORTCUT_SETTLE_SECONDS = 0.4
PASTE_SETTLE_SECONDS = 0.3
SUBMIT_SETTLE_SECONDS = 0.4
SUCCESS_MESSAGE = "Prompt sent to Qoder IDE. Waiting for completion."

_delegate_lock = threading.Lock()


class DelegateError(Exception):
    """The request could not be sent safely."""


class _Aborted(Exception):
    pass


class _FocusLost(Exception):
    pass


@dataclasses.dataclass(frozen=True)
class QoderWindow:
    hwnd: int
    title: str
    process: str
    left: int
    top: int
    width: int
    height: int
    native: object = None


def _validate(prompt, goal) -> tuple[str, str]:
    if not isinstance(prompt, str):
        raise DelegateError("prompt must be a string.")
    text = prompt.strip()
    if not text:
        raise DelegateError("delegate_to_qoder needs a prompt.")
    if len(text) > MAX_PROMPT_CHARS:
        raise DelegateError(
            f"prompt is too long ({len(text)} chars; max {MAX_PROMPT_CHARS}).")
    if "\x00" in text:
        raise DelegateError("prompt contains a NUL character.")
    if goal is None:
        goal = DEFAULT_GOAL
    if not isinstance(goal, str):
        raise DelegateError("goal must be a string.")
    goal = goal.strip() or DEFAULT_GOAL
    if len(goal) > MAX_GOAL_CHARS:
        raise DelegateError(
            f"goal is too long ({len(goal)} chars; max {MAX_GOAL_CHARS}).")
    return text, goal


# ----------------------------------------------------------------------
# Window discovery
# ----------------------------------------------------------------------
def _load_pygetwindow():
    try:
        import pygetwindow as gw
        return gw
    except Exception:
        return None


def _window_pid(hwnd: int) -> int:
    if os.name != "nt":
        return 0
    try:
        import ctypes

        user32 = ctypes.windll.user32
        try:
            user32.GetWindowThreadProcessId.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        except Exception:
            pass
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(
            ctypes.c_void_p(int(hwnd)), ctypes.byref(pid))
        return int(pid.value)
    except Exception:
        return 0


def _process_name(pid: int) -> str:
    try:
        import psutil
        return str(psutil.Process(int(pid)).name() or "")
    except Exception:
        return ""


def find_qoder_windows() -> list[QoderWindow]:
    """Return verified Qoder windows, preferring an explicit Qoder IDE install.

    A title alone is not enough: the owning process must also be Qoder. This
    prevents pasting a prompt into a browser tab or document named "Qoder".
    """
    gw = _load_pygetwindow()
    if gw is None:
        return []
    try:
        candidates = gw.getAllWindows()
    except Exception:
        return []

    verified: list[QoderWindow] = []
    for native in candidates:
        try:
            title = str(getattr(native, "title", "") or "").strip()
            hwnd = int(getattr(native, "_hWnd", 0) or 0)
        except Exception:
            continue
        if not hwnd or "qoder" not in title.lower():
            continue
        pid = _window_pid(hwnd)
        if not pid or pid == os.getpid():
            continue
        process = _process_name(pid)
        if "qoder" not in process.lower():
            continue
        try:
            left = int(getattr(native, "left", 0) or 0)
            top = int(getattr(native, "top", 0) or 0)
            width = int(getattr(native, "width", 0) or 0)
            height = int(getattr(native, "height", 0) or 0)
        except Exception:
            continue
        if width <= 0 or height <= 0:
            continue
        verified.append(QoderWindow(
            hwnd=hwnd,
            title=title,
            process=process,
            left=left,
            top=top,
            width=width,
            height=height,
            native=native,
        ))

    def rank(window: QoderWindow) -> tuple:
        explicit_ide = (
            "qoder ide" in window.process.lower()
            or "qoder ide" in window.title.lower()
        )
        return (
            explicit_ide,
            window.width * window.height,
            window.title.lower() == "qoder",
        )

    verified.sort(key=rank, reverse=True)
    return verified


# ----------------------------------------------------------------------
# Focus and input
# ----------------------------------------------------------------------
def _activate_native(window: QoderWindow) -> None:
    native = window.native
    if native is None:
        return
    try:
        if getattr(native, "isMinimized", False):
            native.restore()
    except Exception:
        pass
    try:
        native.activate()
    except Exception:
        pass


def _focus_window(
    window: QoderWindow,
    os_module,
    sleep: Callable[[float], None],
    attempts: int = FOCUS_ATTEMPTS,
) -> bool:
    for _ in range(attempts):
        _activate_native(window)
        try:
            os_module.activate_window(window.hwnd)
        except Exception:
            pass
        sleep(FOCUS_SETTLE_SECONDS)
        try:
            if os_module.foreground_window() == window.hwnd:
                return True
        except Exception:
            continue
    return False


def _check(os_module, window: QoderWindow) -> None:
    if getattr(os_module, "kill_switch_active", lambda: False)():
        raise _Aborted()
    if os_module.foreground_window() != window.hwnd:
        raise _FocusLost()


def _resolve_clipboard(clipboard):
    if clipboard is None:
        import pyperclip as clipboard
    return clipboard


def _resolve_keyboard(keyboard):
    if keyboard is None:
        import pyautogui as keyboard
    return keyboard


# ----------------------------------------------------------------------
# Public tool
# ----------------------------------------------------------------------
def delegate_to_qoder(
    prompt: str,
    goal: str = DEFAULT_GOAL,
    *,
    os_module=None,
    find_windows: Optional[Callable[[], Sequence[QoderWindow]]] = None,
    clipboard=None,
    keyboard=None,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Copy a prompt into Qoder's chat and submit it; return immediately."""
    try:
        prompt, goal = _validate(prompt, goal)
    except DelegateError as exc:
        return f"ERROR: {exc}"

    if not _delegate_lock.acquire(blocking=False):
        return "ERROR: another Qoder delegation is already running."
    previous = None
    copied = False
    try:
        try:
            if os_module is None:
                import os_control as os_module
            if find_windows is None:
                find_windows = find_qoder_windows
            clipboard = _resolve_clipboard(clipboard)
            keyboard = _resolve_keyboard(keyboard)
        except Exception as exc:
            return f"ERROR: Qoder delegation dependencies unavailable: {exc}"

        try:
            os_module.ensure_kill_switch()
        except Exception:
            pass
        if getattr(os_module, "kill_switch_active", lambda: False)():
            return "ABORTED: kill switch is latched (Ctrl+Alt+Shift+Esc)."

        try:
            windows = list(find_windows())
        except Exception as exc:
            return f"ERROR: could not inspect windows: {exc}"
        if not windows:
            return ("ERROR: no verified Qoder IDE window found. Open Qoder IDE "
                    "and try again; its title and process must contain "
                    "'Qoder'.")

        window = windows[0]
        if not _focus_window(window, os_module, sleep):
            return ("ERROR: could not bring Qoder IDE to the foreground; "
                    "refusing to type into another window.")

        try:
            previous = clipboard.paste()
        except Exception:
            previous = None
        try:
            clipboard.copy(prompt)
            copied = True
        except Exception as exc:
            return f"ERROR: could not copy the prompt to the clipboard: {exc}"

        try:
            _check(os_module, window)
            keyboard.hotkey(*CHAT_SHORTCUT)
            sleep(SHORTCUT_SETTLE_SECONDS)

            _check(os_module, window)
            keyboard.hotkey("ctrl", "v")
            sleep(PASTE_SETTLE_SECONDS)

            _check(os_module, window)
            keyboard.press("enter")
            sleep(SUBMIT_SETTLE_SECONDS)
        except _Aborted:
            return "ABORTED: kill switch is latched (Ctrl+Alt+Shift+Esc)."
        except _FocusLost:
            return ("ERROR: Qoder IDE lost the foreground before the prompt "
                    "was submitted; the prompt may be in the chat but was not "
                    "sent.")
        except Exception as exc:
            if type(exc).__name__ == "FailSafeException":
                return ("ABORTED: mouse moved to a screen corner "
                        "(PyAutoGUI failsafe).")
            return f"ERROR: keyboard input failed: {exc}"

        return SUCCESS_MESSAGE
    finally:
        if copied and previous is not None:
            try:
                clipboard.copy(previous)
            except Exception:
                pass
        _delegate_lock.release()
