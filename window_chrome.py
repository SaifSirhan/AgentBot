"""Windows frameless chrome: drop the OS caption without losing edge resize.

Windows-only and stdlib-only (ctypes) — no new dependency. The window keeps its
native resize frame and DWM shadow; WM_NCCALCSIZE collapses the caption into the
client area and WM_NCHITTEST re-claims the border pixels as grab handles. That
combination is why Aero Snap, taskbar-aware maximize and 8-way resizing keep
working, which a bare Qt.FramelessWindowHint would have cost us: that flag also
strips WS_THICKFRAME, so the OS stops hit-testing the edges entirely.

The pure helpers (hit_test, lparam_point, client_rect, resize_border) take real
numbers and return real Win32 codes, so they are unit-testable with no window;
FramelessChrome below is deliberately thin ctypes glue.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

# Message identifiers (winuser.h).
WM_NCCALCSIZE = 0x0083
WM_NCHITTEST = 0x0084

# Hit-test return codes (winuser.h). Only the ones this module can produce.
HTCLIENT = 1
HTLEFT = 10
HTRIGHT = 11
HTTOP = 12
HTTOPLEFT = 13
HTTOPRIGHT = 14
HTBOTTOM = 15
HTBOTTOMLEFT = 16
HTBOTTOMRIGHT = 17

SM_CXFRAME = 32        # sizing border width, incl. the invisible part
SM_CYFRAME = 33        # sizing border height
SM_CXPADDEDBORDER = 92 # extra grab band Windows adds for touch

MONITOR_DEFAULTTONEAREST = 2


class NCCALCSIZE_PARAMS(ctypes.Structure):
    """lParam of WM_NCCALCSIZE when wParam is TRUE.

    rgrc[0] is the proposed new client rectangle, in screen coordinates; we
    rewrite it. rgrc[1]/rgrc[2] carry the old window/client rects and are only
    read if needed, so they stay untouched.
    """

    _fields_ = [
        ("rgrc", wintypes.RECT * 3),
        ("lppos", ctypes.c_void_p),  # POINTER(WINDOWPOS); not needed here
    ]


def hit_test(x, y, *, width, height, border, maximized=False):
    """Win32 hit-test code for the point (x, y) inside a width x height window.

    Coordinates are window-relative physical pixels, so the caller must convert
    from screen coordinates first (a client-area mapToGlobal() is in Qt's
    device-independent pixels and lies on a scaled desktop).
    """
    if maximized or border <= 0:
        return HTCLIENT
    near_left = x < border
    near_right = x >= width - border
    near_top = y < border
    near_bottom = y >= height - border
    if near_top and near_left:
        return HTTOPLEFT
    if near_top and near_right:
        return HTTOPRIGHT
    if near_bottom and near_left:
        return HTBOTTOMLEFT
    if near_bottom and near_right:
        return HTBOTTOMRIGHT
    if near_left:
        return HTLEFT
    if near_right:
        return HTRIGHT
    if near_top:
        return HTTOP
    if near_bottom:
        return HTBOTTOM
    return HTCLIENT


def lparam_point(lparam):
    """Decode WM_NCHITTEST lParam into a signed (x, y) screen point.

    Both halves are signed 16-bit: a monitor placed left or above the primary
    reports negative coordinates, and an unsigned read would land the pointer
    thousands of pixels away and silently disable resizing there.
    """
    raw = lparam & 0xFFFFFFFF
    x = raw & 0xFFFF
    y = (raw >> 16) & 0xFFFF
    if x >= 0x8000:
        x -= 0x10000
    if y >= 0x8000:
        y -= 0x10000
    return x, y


def client_rect(window_rect, *, maximized, border):
    """Client rectangle (left, top, right, bottom) for a given window rect.

    Windowed: the whole window becomes client, which is what hides the caption.
    Maximised: Windows inflates the window rect past the monitor by the invisible
    resize border, so the same rect would push content off-screen; inset it back.
    """
    left, top, right, bottom = window_rect
    if maximized:
        return (left + border, top + border, right - border, bottom - border)
    return (left, top, right, bottom)


def _user32():
    return ctypes.windll.user32


def resize_border():
    """Grab-band thickness in physical pixels: sizing border + padded border."""
    user32 = _user32()
    return (user32.GetSystemMetrics(SM_CXFRAME)
            + user32.GetSystemMetrics(SM_CXPADDEDBORDER))


class FramelessChrome:
    """Handles the native messages that keep a caption-less window resizable.

    `window` is the top-level widget; only `isMaximized()` is consulted, so the
    state stays whatever Qt already knows and this class never mutates geometry.
    """

    def __init__(self, window, border=None):
        self._window = window
        self._border = resize_border() if border is None else border

    @property
    def border(self):
        return self._border

    def handle(self, event_type, message):
        """Return (handled, result) for a Qt native event.

        Unhandled messages fall through with (False, 0) so Qt's own Windows
        back end still sees them.
        """
        if event_type not in (b"windows_generic_MSG",
                              b"windows_dispatcher_MSG"):
            return False, 0
        try:
            msg = wintypes.MSG.from_address(int(message))
        except (OSError, ValueError):
            return False, 0
        if msg.message == WM_NCCALCSIZE:
            return self._on_ncalcsize(msg)
        if msg.message == WM_NCHITTEST:
            return self._on_nchittest(msg)
        return False, 0

    def _on_ncalcsize(self, msg):
        # wParam FALSE means the OS is only asking for the size, not handing us
        # a rectangle to rewrite — the default handler must stay in charge.
        if not msg.wParam:
            return False, 0
        try:
            params = ctypes.cast(
                msg.lParam, ctypes.POINTER(NCCALCSIZE_PARAMS)).contents
        except (OSError, ValueError):
            return False, 0
        rect = params.rgrc[0]
        inset = client_rect(
            (rect.left, rect.top, rect.right, rect.bottom),
            maximized=bool(self._window.isMaximized()),
            border=self._border,
        )
        rect.left, rect.top, rect.right, rect.bottom = inset
        # 0 = "client area is what you just wrote into rgrc[0]".
        return True, 0

    def _on_nchittest(self, msg):
        rect = wintypes.RECT()
        # MSG's first field is spelled hWnd in the CPython this project runs on;
        # reading it wrong makes every hit-test raise and silently drop resize.
        if not _user32().GetWindowRect(msg.hWnd, ctypes.byref(rect)):
            return False, 0
        screen_x, screen_y = lparam_point(msg.lParam)
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        code = hit_test(
            screen_x - rect.left,
            screen_y - rect.top,
            width=width,
            height=height,
            border=self._border,
            maximized=bool(self._window.isMaximized()),
        )
        if code == HTCLIENT:
            # Let Qt do its normal client-area hit-testing (hover, click, drag).
            return False, 0
        return True, code
