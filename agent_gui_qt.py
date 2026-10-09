"""
AgentBot GUI — PySide6 port.

Phase 1: skeleton layout. Sidebar, header and an empty chat area. No real
functionality yet — no chat rendering, no agent wiring.

This file will eventually replace agent_gui.py. Until the port is complete,
agent_gui.py remains the live GUI and this file is only run manually.

Palette values are inlined from gui_widgets.py. That module imports tkinter,
so it must NOT be imported here — the Qt port stays free of tkinter.
"""
from __future__ import annotations
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
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
# Mirrors gui_widgets.py. Keep in sync if the theme changes.
COLOR_BG         = "#0d0f14"
COLOR_SIDEBAR    = "#111319"
COLOR_INPUT_BG   = "#14171f"
COLOR_BORDER     = "#23262f"
COLOR_HOVER      = "#1c2029"
COLOR_SELECTED   = "#16241e"
COLOR_ACCENT     = "#3ecf8e"
COLOR_ACCENT_HOVER = "#2fb87c"
COLOR_TEXT_HI    = "#e8eaef"
COLOR_TEXT_MID   = "#9aa1af"
COLOR_TEXT_LOW   = "#5f6673"

FONT_UI = "Segoe UI"

SIDEBAR_W = 226
HEADER_H = 48


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


class Sidebar(QFrame):
    """Left collapsible drawer. Phase 1: structure only, no behaviour."""

    def __init__(self):
        super().__init__()
        self.setFixedWidth(SIDEBAR_W)
        self.setStyleSheet(f"background: {COLOR_SIDEBAR};")

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

        new_chat = QPushButton("  +   New chat")
        new_chat.setFixedHeight(36)
        new_chat.setCursor(Qt.CursorShape.PointingHandCursor)
        new_chat.setStyleSheet(
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
        layout.addWidget(new_chat)
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


class AgentWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Agent")
        self.resize(1100, 720)
        self.setStyleSheet(f"background: {COLOR_BG};")

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

        layout.addWidget(self._build_chat_area(), 1)
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

    def _build_chat_area(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
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
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(24, 16, 24, 16)
        body_layout.setSpacing(12)
        body_layout.addStretch(1)

        self.chat_body = body
        self.chat_layout = body_layout

        scroll.setWidget(body)
        return scroll

    # ------------------------------------------------------------------
    def toggle_sidebar(self):
        self.sidebar.setVisible(not self.sidebar.isVisible())


def main():
    app = QApplication(sys.argv)
    window = AgentWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
