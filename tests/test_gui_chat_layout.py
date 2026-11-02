"""Chat message column alignment (offscreen Qt).

Regression cover for a column that could land centred instead of flush left:
the message used to pin its column's width from resizeEvent, which resized the
child after the row had already placed it, so a freshly added message kept a
stale offset (and a stale wrap height) until the next resize.

Run: python -m unittest tests.test_gui_chat_layout -v
"""
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).parent.parent))

from PySide6.QtCore import QPoint  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

import agent_gui_qt as g  # noqa: E402

LONG_TEXT = ("A longer agent reply that should wrap across the full width of "
             "the terminal column with room to spare, and land at the same "
             "left margin as the user lines above it.")


class MessageColumnTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = g.AgentWindow()
        self.window.resize(1200, 760)
        self.window.show()
        for _ in range(10):
            self.app.processEvents()
        self.addCleanup(self.window.deleteLater)

    def tearDown(self):
        self.window.close()
        self.app.processEvents()

    def _add(self, *messages):
        for message in messages:
            self.window.chat.add_message(message, fade=False)
        self.app.processEvents()
        return list(messages)

    def _column(self, message):
        return message._column

    def _labels(self, message):
        return [c for c in self._column(message).findChildren(QLabel)
                if c.text()]

    def test_user_and_agent_columns_share_one_left_margin(self):
        messages = self._add(
            g.UserMessage("does the pill read as lifted?"),
            g.AssistantMessage("Agent prose stays **left-aligned**."),
            g.UserMessage("and another line from me"),
            g.AssistantMessage(LONG_TEXT),
        )
        screen_x = {
            round(self._labels(m)[0].mapTo(self.window, QPoint(0, 0)).x())
            for m in messages
        }
        self.assertEqual(len(screen_x), 1,
                         f"columns landed on different margins: {screen_x}")

    def test_columns_are_flush_left_inside_their_message(self):
        messages = self._add(
            g.UserMessage("short"),
            g.AssistantMessage(LONG_TEXT),
        )
        for message in messages:
            with self.subTest(kind=type(message).__name__):
                self.assertEqual(self._column(message).x(), 0)

    def test_columns_take_the_viewport_width_and_never_overflow_it(self):
        messages = self._add(
            g.UserMessage("short"),
            g.AssistantMessage(LONG_TEXT),
        )
        viewport = self.window.chat.viewport().width()
        for message in messages:
            with self.subTest(kind=type(message).__name__):
                self.assertLessEqual(self._column(message).width(), viewport)
                self.assertEqual(self._column(message).width(),
                                 message.width())

    def test_a_long_reply_wraps_at_add_time_without_needing_a_resize(self):
        # The stale state was visible as an extra wrapped line: the label was
        # measured against a narrow column, then widened without re-laying out.
        message = self._add(g.AssistantMessage(LONG_TEXT))[0]
        height_at_add = message.height()
        self.window.resize(1201, 761)
        self.app.processEvents()
        self.window.resize(1200, 760)
        self.app.processEvents()
        self.assertEqual(message.height(), height_at_add,
                         "height changed only after a resize, so the initial "
                         "wrap was measured against the wrong column width")

    def test_columns_follow_a_narrower_viewport(self):
        messages = self._add(
            g.UserMessage("short"),
            g.AssistantMessage(LONG_TEXT),
        )
        self.window.resize(900, 700)
        self.app.processEvents()
        viewport = self.window.chat.viewport().width()
        for message in messages:
            with self.subTest(kind=type(message).__name__):
                self.assertEqual(self._column(message).width(), viewport)
                self.assertEqual(self._column(message).x(), 0)


    def test_one_line_messages_reserve_one_line_of_height(self):
        """The wrapped label's height hint was measured at its minimum width,
        so a single line asked for 252px and the body grew into a void."""
        messages = self._add(*[
            g.UserMessage(f"line {i}: the quick brown fox jumps over the dog")
            for i in range(12)
        ])
        for message in messages:
            with self.subTest(height=message.sizeHint().height()):
                self.assertLessEqual(message.sizeHint().height(),
                                     message.height() + 4)

    def test_scroll_bottom_lands_on_the_last_message(self):
        """The scrollbar maximum must match the last rendered message, not a
        phantom body height. Enough lines to overflow the viewport, or the
        content simply sits at the top and the check means nothing."""
        messages = self._add(*[
            g.UserMessage(f"line {i}: the quick brown fox jumps over the dog")
            for i in range(60)
        ])
        chat = self.window.chat
        self.assertGreater(chat._body.height(), chat.viewport().height(),
                           "test needs overflowing content")
        bar = chat.verticalScrollBar()
        bar.setValue(bar.maximum())
        self.app.processEvents()
        last = messages[-1]
        bottom = last.mapTo(chat.viewport(), QPoint(0, last.height())).y()
        self.assertLessEqual(
            abs(bottom - chat.viewport().height()),
            chat.BOTTOM_EPSILON + 4,
            "scrolling to the end left %d px of void below the last message"
            % (chat.viewport().height() - bottom))


if __name__ == "__main__":
    unittest.main(verbosity=2)
