"""Win32 frameless chrome maths (offscreen-safe: no window is created).

These helpers decide whether the caption is hidden and whether each screen edge
still grabs a resize grip, so they are tested on real numbers rather than by
eyeballing a window. The native-message plumbing itself needs a live HWND and is
covered by scratch/probe_native.py on a Windows desktop.

Run: python -m unittest tests.test_window_chrome -v
"""
import ctypes
import sys
import unittest
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import window_chrome as wc  # noqa: E402

WIDTH, HEIGHT, BORDER = 1200, 800, 9


class HitCodeTests(unittest.TestCase):
    def test_codes_match_winuser_h(self):
        # A wrong constant would silently return a code Windows ignores.
        self.assertEqual(
            [wc.HTCLIENT, wc.HTLEFT, wc.HTRIGHT, wc.HTTOP, wc.HTTOPLEFT,
             wc.HTTOPRIGHT, wc.HTBOTTOM, wc.HTBOTTOMLEFT, wc.HTBOTTOMRIGHT],
            [1, 10, 11, 12, 13, 14, 15, 16, 17])

    def test_message_ids_match_winuser_h(self):
        self.assertEqual([wc.WM_NCCALCSIZE, wc.WM_NCHITTEST], [0x0083, 0x0084])


class HitTestTests(unittest.TestCase):
    def code(self, x, y, maximized=False):
        return wc.hit_test(x, y, width=WIDTH, height=HEIGHT, border=BORDER,
                           maximized=maximized)

    def test_all_eight_grips_are_distinct_and_placed(self):
        cases = [
            (0, 0, wc.HTTOPLEFT),
            (WIDTH - 1, 0, wc.HTTOPRIGHT),
            (0, HEIGHT - 1, wc.HTBOTTOMLEFT),
            (WIDTH - 1, HEIGHT - 1, wc.HTBOTTOMRIGHT),
            (WIDTH // 2, 0, wc.HTTOP),
            (WIDTH // 2, HEIGHT - 1, wc.HTBOTTOM),
            (0, HEIGHT // 2, wc.HTLEFT),
            (WIDTH - 1, HEIGHT // 2, wc.HTRIGHT),
        ]
        seen = set()
        for x, y, expected in cases:
            got = self.code(x, y)
            self.assertEqual(got, expected, f"point ({x}, {y})")
            seen.add(got)
        self.assertEqual(len(seen), 8, "every grip needs its own code")

    def test_interior_is_client(self):
        self.assertEqual(self.code(WIDTH // 2, HEIGHT // 2), wc.HTCLIENT)

    def test_grip_band_boundary_is_exact(self):
        self.assertEqual(self.code(BORDER - 1, HEIGHT // 2), wc.HTLEFT)
        self.assertEqual(self.code(BORDER, HEIGHT // 2), wc.HTCLIENT)

    def test_corner_beats_edge(self):
        # A point inside both bands must resize diagonally, not on one axis.
        self.assertEqual(self.code(2, 2), wc.HTTOPLEFT)
        self.assertEqual(self.code(2, HEIGHT - 2), wc.HTBOTTOMLEFT)

    def test_maximized_window_has_no_resize_grips(self):
        for x, y in [(0, 0), (0, HEIGHT // 2), (WIDTH - 1, HEIGHT - 1)]:
            self.assertEqual(self.code(x, y, maximized=True), wc.HTCLIENT)

    def test_zero_border_never_grips(self):
        self.assertEqual(wc.hit_test(0, 0, width=WIDTH, height=HEIGHT,
                                     border=0), wc.HTCLIENT)


class LParamTests(unittest.TestCase):
    def test_positive_point_decodes_x_then_y(self):
        self.assertEqual(wc.lparam_point((400 << 16) | 250), (250, 400))

    def test_negative_point_on_a_left_monitor(self):
        # -300 and -120 as their unsigned 16-bit halves (65536 + value).
        lparam = (0xFF88 << 16) | 0xFED4
        self.assertEqual(wc.lparam_point(lparam), (-300, -120))

    def test_sign_boundary_is_negative(self):
        self.assertEqual(wc.lparam_point(0x8000), (-32768, 0))

    def test_max_unsigned_stays_negative(self):
        self.assertEqual(wc.lparam_point((0xFFFF << 16) | 0xFFFF), (-1, -1))


class ClientRectTests(unittest.TestCase):
    def test_windowed_keeps_the_whole_window(self):
        self.assertEqual(
            wc.client_rect((100, 100, 1200, 900), maximized=False, border=9),
            (100, 100, 1200, 900))

    def test_maximized_insets_the_invisible_border(self):
        # Measured on a 1920x1080 desktop: Windows inflates the maximized window
        # rect by the 9px grab band, and the inset puts the client back inside.
        self.assertEqual(
            wc.client_rect((-9, -9, 1929, 1089), maximized=True, border=9),
            (0, 0, 1920, 1080))


class FramelessChromeGuardTests(unittest.TestCase):
    class _Window:
        def isMaximized(self):
            return False

    def setUp(self):
        self.chrome = wc.FramelessChrome(self._Window(), border=BORDER)

    def message_at(self, **fields):
        msg = wintypes.MSG()
        for name, value in fields.items():
            setattr(msg, name, value)
        return ctypes.addressof(msg), msg

    def test_non_windows_event_type_is_passed_through(self):
        self.assertEqual(self.chrome.handle(b"xcb_generic_EVENT", 0),
                         (False, 0))

    def test_unrelated_message_is_passed_through(self):
        address, _ = self.message_at(message=0x00F0)  # WM_SETTEXT
        self.assertEqual(
            self.chrome.handle(b"windows_generic_MSG", address), (False, 0))

    def test_nccalcsize_without_a_rectangle_is_left_to_the_os(self):
        # wParam FALSE means "size only"; rewriting rgrc[0] here would be wrong.
        address, _ = self.message_at(message=wc.WM_NCCALCSIZE, wParam=0)
        self.assertEqual(
            self.chrome.handle(b"windows_generic_MSG", address), (False, 0))

    def test_hit_test_with_an_unresolvable_handle_falls_through(self):
        # Failure path: GetWindowRect on a null HWND returns 0, and the message
        # must go back to Qt rather than reporting a grip at (0, 0).
        address, _ = self.message_at(message=wc.WM_NCHITTEST, hWnd=None,
                                     lParam=(400 << 16) | 250)
        self.assertEqual(
            self.chrome.handle(b"windows_generic_MSG", address), (False, 0))

    def test_border_is_reported_for_the_caller(self):
        self.assertEqual(self.chrome.border, BORDER)


if __name__ == "__main__":
    unittest.main(verbosity=2)
