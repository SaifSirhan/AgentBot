"""
AgentBot GUI — PySide6 port.

Phase 2: chat rendering. User and assistant messages, markdown, code blocks
with copy buttons, and scroll-lock behaviour. Demo messages only — no agent
wiring (that is Phase 3).

This file will eventually replace agent_gui.py. Until the port is complete,
agent_gui.py remains the live GUI and this file is only run manually.

Palette values are inlined from gui_widgets.py (the mint accent has been
shifted greener for this port). That module imports tkinter, so it must NOT
be imported here — the Qt port stays free of tkinter.
"""
from __future__ import annotations
import re
import sys

from PySide6.QtCore import (
    Qt,
    QEasingCurve,
    QPropertyAnimation,
    QTimer,
)
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

# --------------------------- palette ---------------------------
# Mirrors gui_widgets.py, with the mint accent shifted greener (hue 153->147,
# saturation 60%->70%). Keep backgrounds/borders/text in sync with the theme.
COLOR_BG         = "#0d0f14"
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
        outer.addWidget(self.code_label)

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
# Messages
# ----------------------------------------------------------------------
class MessageBase(QWidget):
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
    """Full-width prose, no background."""

    def __init__(self, text: str):
        super().__init__()
        self._render(text)


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
        self.setMinimumWidth(0)
        self.setMaximumWidth(SIDEBAR_W)
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
        for glyph, label in (
            ("\u2699", "Settings"),
            ("\U0001f4c5", "Tasks"),
            ("\U0001f4da", "Index docs"),
            ("\U0001f50d", "Activity log"),
            ("\U0001f4cb", "Copy all"),
            ("\U0001f4e4", "Export chat"),
        ):
            bottom_layout.addWidget(_nav_button(glyph, label))
        layout.addWidget(bottom)


# ----------------------------------------------------------------------
# Main window
# ----------------------------------------------------------------------
class AgentWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Agent")
        self.resize(1100, 720)
        self.setStyleSheet(f"background: {COLOR_BG};")

        self._sidebar_anim = None

        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.sidebar = Sidebar()
        root.addWidget(self.sidebar)

        root.addWidget(self._build_main_column(), 1)
        self.setCentralWidget(central)

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

        dot = QLabel("\u25cf")
        dot.setStyleSheet(f"color: {COLOR_ACCENT}; font-size: 10px;")
        row.addWidget(dot)

        status = QLabel("Ready")
        status.setStyleSheet(
            f'color: {COLOR_TEXT_LOW}; font-family: "{FONT_UI}"; font-size: 11px;'
        )
        row.addWidget(status)
        row.addStretch(1)

        row.addWidget(_icon_button("\u21bb", "New chat", size=28))
        row.addWidget(_icon_button("\u2699", "Settings"))

        return header

    # ------------------------------------------------------------------
    def toggle_sidebar(self):
        visible = self.sidebar.isVisible()
        if visible:
            start, end = SIDEBAR_W, 0
        else:
            self.sidebar.setVisible(True)
            start, end = 0, SIDEBAR_W

        anim = QPropertyAnimation(self.sidebar, b"maximumWidth", self)
        anim.setDuration(200)
        anim.setStartValue(start)
        anim.setEndValue(end)
        anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        if visible:
            anim.finished.connect(lambda: self.sidebar.setVisible(False))
        anim.start()
        self._sidebar_anim = anim

    def load_demo_messages(self):
        self.chat.add_message(
            UserMessage("What does the retry helper in agent.py do?")
        )
        self.chat.add_message(
            AssistantMessage(
                "## Short answer\n\n"
                "It wraps a callable and retries it a fixed number of times with a "
                "**backoff delay** between attempts.\n\n"
                "### How it works\n\n"
                "- Catches the configured exception types\n"
                "- Sleeps `base_delay * attempt` seconds\n"
                "- Re-raises on the final attempt so failures are not swallowed"
            )
        )
        self.chat.add_message(
            AssistantMessage(
                "Here is the shape of it:\n\n"
                "```python\n"
                "def retry(fn, attempts=3, base_delay=0.5):\n"
                "    for i in range(attempts):\n"
                "        try:\n"
                "            return fn()\n"
                "        except Exception:\n"
                "            if i == attempts - 1:\n"
                "                raise\n"
                "            time.sleep(base_delay * (i + 1))\n"
                "```\n\n"
                "Call it with `retry(fetch_page, attempts=5)` to raise the ceiling."
            )
        )
        self.chat.add_message(
            UserMessage("Got it — where is it used?")
        )
        self.chat.force_scroll_bottom()


def main():
    app = QApplication(sys.argv)
    window = AgentWindow()
    window.load_demo_messages()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
