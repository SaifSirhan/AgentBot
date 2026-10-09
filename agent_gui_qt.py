"""
AgentBot GUI — PySide6 port.

Phase 0: environment check and empty window. No real functionality yet.

This file will eventually replace agent_gui.py. Until the port is complete,
agent_gui.py remains the live GUI and this file is only run manually.
"""
from __future__ import annotations
import sys
from PySide6.QtWidgets import QApplication, QMainWindow, QLabel, QWidget, QVBoxLayout
from PySide6.QtCore import Qt


class AgentWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Agent")
        self.resize(1100, 720)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        placeholder = QLabel("AgentBot — PySide6 port, Phase 0")
        placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(placeholder)

        self.setCentralWidget(central)


def main():
    app = QApplication(sys.argv)
    window = AgentWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
