"""Manual GUI smoke test for the frameless window chrome, on a live desktop.

unittest discovery skips this file (it is not test_*.py) because it needs a real
Win32 window: it asserts the OS caption is gone, that all eight edges and
corners still hit-test as resize grips, and that maximise lands inside the
monitor work area. The pure maths behind those messages is covered by
tests/test_window_chrome.py; this covers the plumbing.

Run directly: python tests/gui_chrome_smoke.py
Exits non-zero if any check fails.
"""
import ctypes
import os
import sys
from ctypes import wintypes
from pathlib import Path

os.environ.pop("QT_QPA_PLATFORM", None)  # a real window, not offscreen

ROOT = Path(r"C:\Users\USER\AgentBot")
sys.path.insert(0, str(ROOT))

from PySide6.QtGui import QFontDatabase  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import agent_gui_qt as g  # noqa: E402
import window_chrome as wc  # noqa: E402

if sys.platform != "win32":
    print("SKIP: frameless chrome is Windows-only")
    sys.exit(0)

user32 = ctypes.windll.user32
user32.SendMessageW.restype = ctypes.c_long
user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                wintypes.WPARAM, wintypes.LPARAM]

CODE_NAMES = {
    1: "HTCLIENT", 10: "HTLEFT", 11: "HTRIGHT", 12: "HTTOP",
    13: "HTTOPLEFT", 14: "HTTOPRIGHT", 15: "HTBOTTOM",
    16: "HTBOTTOMLEFT", 17: "HTBOTTOMRIGHT",
}

failures = []


def check(label, condition, detail=""):
    verdict = "PASS" if condition else "FAIL"
    print(f"  {label:<46} {verdict}{'  ' + detail if detail else ''}")
    if not condition:
        failures.append(label)


app = QApplication([])
app.setStyle("Fusion")
app.setFont(g.terminal_font())
app.setStyleSheet(g.theme_stylesheet(g.read_theme_name()))

win = g.AgentWindow()
win.resize(1180, 740)
win.show()
app.processEvents()

hwnd = wintypes.HWND(int(win.windowHandle().winId()))


def window_rect():
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def client_size():
    r = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(r))
    return r.right - r.left, r.bottom - r.top


def client_origin():
    pt = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return pt.x, pt.y


def monitor_and_work_area():
    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    user32.GetMonitorInfoW(user32.MonitorFromWindow(hwnd, 2),
                           ctypes.byref(info))
    m, w = info.rcMonitor, info.rcWork
    return ((m.left, m.top, m.right, m.bottom),
            (w.left, w.top, w.right, w.bottom))


def hit_test_at(screen_x, screen_y):
    lparam = ((screen_y & 0xFFFF) << 16) | (screen_x & 0xFFFF)
    return user32.SendMessageW(hwnd, wc.WM_NCHITTEST, 0, lparam)


print("platform:", app.platformName(), "| theme:", g.read_theme_name())
ui_family, mono_family = g.resolve_fonts()
print(f"fonts: UI={ui_family!r} MONO={mono_family!r}")
families = set(QFontDatabase.families())
for name in g._MONO_FAMILIES:
    print(f"  installed {name + ':':<18} {name in families}")

print("\n[1] caption is hidden")
rect = window_rect()
cw, ch = client_size()
origin = client_origin()
check("client area starts at the window origin (no OS caption)",
      origin == (rect[0], rect[1]), f"client at {origin}, window at {rect[:2]}")
check("custom title bar is present", win._title_bar is not None
      and win._title_bar.isVisible(),
      f"height {win._title_bar.height() if win._title_bar else '-'}px")

print("\n[2] every edge and corner is still a resize grip")
border = win._chrome.border
print(f"  grab band: {border}px")
left, top, right, bottom = rect
probes = [
    ("top-left corner", left + 2, top + 2, wc.HTTOPLEFT),
    ("top edge", left + cw // 2, top + 2, wc.HTTOP),
    ("top-right corner", right - 2, top + 2, wc.HTTOPRIGHT),
    ("left edge", left + 2, top + ch // 2, wc.HTLEFT),
    ("right edge", right - 2, top + ch // 2, wc.HTRIGHT),
    ("bottom-left corner", left + 2, bottom - 2, wc.HTBOTTOMLEFT),
    ("bottom edge", left + cw // 2, bottom - 2, wc.HTBOTTOM),
    ("bottom-right corner", right - 2, bottom - 2, wc.HTBOTTOMRIGHT),
    ("centre is client", left + cw // 2, top + ch // 2, wc.HTCLIENT),
]
for label, x, y, expected in probes:
    got = hit_test_at(x, y)
    check(f"{label} -> {CODE_NAMES[expected]}", got == expected,
          f"got {CODE_NAMES.get(got, got)}")

print("\n[3] drag surface uses the native move loop")
check("QWindow.startSystemMove is available",
      hasattr(win.windowHandle(), "startSystemMove"))

print("\n[4] maximise stays inside the work area")
win.showMaximized()
app.processEvents()
monitor, work = monitor_and_work_area()
rect2 = window_rect()
cw2, ch2 = client_size()
origin2 = client_origin()
print(f"  monitor {monitor}  work area {work}")
print(f"  maximised window {rect2}  client {cw2}x{ch2} at {origin2}")
check("Qt reports the window maximised", win.isMaximized())
check("client is inset from the inflated window rect",
      (rect2[0] + border, rect2[1] + border) == origin2,
      f"window {rect2[:2]} + {border}px = client {origin2}")
check("client fits the work area (taskbar not covered)",
      origin2[0] >= work[0] and origin2[1] >= work[1]
      and origin2[0] + cw2 <= work[2] and origin2[1] + ch2 <= work[3],
      f"client {origin2} {cw2}x{ch2} vs work {work}")
check("title bar survives maximise", win._title_bar.isVisible())
check("maximise glyph switched to restore",
      win._title_bar.max_button._icon_name == "win_restore")

shot = ROOT / "scratch" / "gui_chrome_smoke.png"
win.grab().save(str(shot))
print("\nscreenshot:", shot)

win.showNormal()
app.processEvents()
check("restore returns the window to normal", not win.isMaximized())

print(f"\n{len(failures)} failure(s)")
for label in failures:
    print("  -", label)
sys.exit(1 if failures else 0)
