"""Software-rendered reactor HUD for the terminal-style AgentBot window.

The visual budget follows Mark-LVI's HudCanvas: QPainter only, cached static
geometry, integrated phase instead of absolute-time rotation, and a paint rate
lower than the animation step rate.
"""
from __future__ import annotations

import math
import time

from PySide6.QtCore import QLineF, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QPainter, QPen, QPixmap, QRadialGradient
from PySide6.QtWidgets import QWidget

from audio_bus import audio_bus as default_audio_bus


class ReactorHud(QWidget):
    SIZE = 240
    STATES = ("IDLE", "LISTENING", "THINKING", "SPEAKING", "ERROR")

    _MINT = QColor("#70FFD4")
    _AMBER = QColor("#FFD166")
    _RED = QColor("#FF8F9C")
    _BG = QColor("#000000")

    # (rotation speed, arc colour)
    _SPECS = {
        "IDLE": (0.9, _MINT),
        "LISTENING": (1.2, _MINT),
        "THINKING": (2.8, _AMBER),
        "SPEAKING": (1.4, QColor("#8CFFE0")),
        "ERROR": (0.55, _RED),
    }

    def __init__(self, bus=None, parent=None):
        super().__init__(parent)
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self._bus = bus or default_audio_bus
        self._state = "IDLE"
        self._phase = 0.0
        self._level = 0.0
        self._paint_tick = 0
        self._last_tick = time.monotonic()
        self._tick_cache = None
        self._build_tick_cache()

        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)

    # -- public state ---------------------------------------------------
    @property
    def state(self) -> str:
        return self._state

    @property
    def level(self) -> float:
        return self._level

    def set_state(self, state_name: str) -> None:
        name = str(state_name or "").strip().upper()
        if name not in self._SPECS:
            name = "IDLE"
        if name != self._state:
            self._state = name
            self.update()

    # -- animation ------------------------------------------------------
    def showEvent(self, event):
        self._last_tick = time.monotonic()
        if not self._timer.isActive():
            self._timer.start()
        super().showEvent(event)

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def _tick(self):
        now = time.monotonic()
        dt = max(0.0, min(0.1, now - self._last_tick))
        self._last_tick = now

        speed = self._SPECS[self._state][0]
        self._phase = (self._phase + dt * speed) % 360.0

        target = 0.0
        if self._state in ("LISTENING", "SPEAKING"):
            _, rms = self._bus.get_latest()
            gain = 12.0 if self._bus.source == "mic" else 4.0
            target = min(1.0, rms * gain)
        attack = 0.45 if target > self._level else 0.10
        self._level += (target - self._level) * attack

        self._paint_tick = (self._paint_tick + 1) % 2
        if self._paint_tick == 0 and self._on_screen():
            self.update()

    def _on_screen(self) -> bool:
        if not self.isVisible():
            return False
        window = self.window()
        return not (window.isMinimized() or window.isHidden())

    # -- static layer ---------------------------------------------------
    @staticmethod
    def _blend(color: QColor, alpha: float) -> QColor:
        k = max(0.0, min(1.0, float(alpha)))
        return QColor(
            int(color.red() * k),
            int(color.green() * k),
            int(color.blue() * k),
        )

    def _build_tick_cache(self):
        pixmap = QPixmap(self.SIZE, self.SIZE)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        if not painter.isActive():
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        cx = cy = self.SIZE / 2.0
        radius = self.SIZE * 0.38
        major, minor = [], []
        for index in range(72):
            angle = math.radians(index * 5.0)
            ca, sa = math.cos(angle), math.sin(angle)
            if index % 3 == 0:
                major.append(QLineF(
                    cx + ca * radius * 0.885, cy + sa * radius * 0.885,
                    cx + ca * radius * 0.985, cy + sa * radius * 0.985))
            else:
                minor.append(QLineF(
                    cx + ca * radius * 0.945, cy + sa * radius * 0.945,
                    cx + ca * radius * 0.985, cy + sa * radius * 0.985))
        painter.setPen(QPen(self._blend(self._MINT, 0.42), 1.3))
        painter.drawLines(major)
        painter.setPen(QPen(self._blend(self._MINT, 0.18), 1.0))
        painter.drawLines(minor)
        painter.end()
        self._tick_cache = pixmap

    # -- painting -------------------------------------------------------
    def paintEvent(self, event):
        painter = QPainter(self)
        if not painter.isActive():
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), self._BG)

        width, height = self.width(), self.height()
        cx, cy = width / 2.0, height / 2.0
        radius = min(width, height) * 0.38
        color = self._SPECS[self._state][1]
        level = self._level

        self._paint_atmosphere(painter, cx, cy, radius, color, level)
        if self._tick_cache is not None:
            painter.drawPixmap(0, 0, self._tick_cache)
        self._paint_frame(painter, cx, cy, radius, width, height)
        self._paint_arcs(painter, cx, cy, radius, color)
        self._paint_voice_ring(painter, cx, cy, radius, color, level)
        painter.setPen(QPen(self._blend(color, 0.30 + 0.45 * level), 1.5))
        inner = radius * 0.355
        painter.drawEllipse(
            QRectF(cx - inner, cy - inner, inner * 2, inner * 2))
        painter.end()

    def _paint_atmosphere(self, painter, cx, cy, radius, color, level):
        painter.setPen(Qt.PenStyle.NoPen)
        lift = 1.0 + 0.55 * level + (
            0.18 if self._state == "SPEAKING" else 0.0)
        for grad_radius, alpha in ((radius * 0.70, 0.30), (radius * 0.34, 0.34)):
            gradient = QRadialGradient(cx, cy, grad_radius)
            gradient.setColorAt(0.0, self._blend(color, min(0.95, alpha * lift)))
            gradient.setColorAt(
                0.45, self._blend(color, min(0.95, alpha * lift * 0.52)))
            gradient.setColorAt(
                0.78, self._blend(color, min(0.95, alpha * lift * 0.18)))
            gradient.setColorAt(1.0, self._BG)
            painter.setBrush(QBrush(gradient))
            painter.drawEllipse(QRectF(
                cx - grad_radius, cy - grad_radius,
                grad_radius * 2, grad_radius * 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)

    def _paint_frame(self, painter, cx, cy, radius, width, height):
        painter.setPen(QPen(self._blend(self._MINT, 0.34), 1.0))
        painter.drawEllipse(QRectF(
            cx - radius, cy - radius, radius * 2, radius * 2))
        painter.setPen(QPen(self._blend(self._MINT, 0.16), 1.0))
        outer = radius * 0.93
        painter.drawEllipse(QRectF(
            cx - outer, cy - outer, outer * 2, outer * 2))

        gap = radius * 0.62
        painter.setPen(QPen(self._blend(self._MINT, 0.16), 1.0))
        painter.drawLine(QLineF(cx - width / 2.0, cy, cx - gap, cy))
        painter.drawLine(QLineF(cx + gap, cy, cx + width / 2.0, cy))
        painter.drawLine(QLineF(cx, cy - height / 2.0, cx, cy - gap))
        painter.drawLine(QLineF(cx, cy + gap, cx, cy + height / 2.0))

        margin = min(width, height) * 0.035
        arm = min(width, height) * 0.055
        painter.setPen(QPen(self._blend(self._MINT, 0.45), 1.4))
        for sx, sy in ((1, 1), (-1, 1), (1, -1), (-1, -1)):
            x = cx + sx * (width / 2.0 - margin)
            y = cy + sy * (height / 2.0 - margin)
            painter.drawLine(QLineF(x, y, x - sx * arm, y))
            painter.drawLine(QLineF(x, y, x, y - sy * arm))

    def _paint_arcs(self, painter, cx, cy, radius, color):
        bands = (
            (0.955, 118, 2, +1, color, 0.75, 2.0),
            (0.845, 82, 3, -1, color, 0.38, 1.3),
            (0.760, 150, 1, +1, color, 0.45, 1.6),
            (0.660, 64, 4, -1, color, 0.26, 1.1),
            (0.545, 128, 2, +1, color, 0.30, 1.2),
        )
        for index, (factor, span, count, direction, band_color, alpha, width) in enumerate(bands):
            rad = radius * factor
            painter.setPen(QPen(self._blend(band_color, alpha), width))
            box = QRectF(cx - rad, cy - rad, rad * 2, rad * 2)
            base = (self._phase * (9 + index * 6) * direction) % 360.0
            for segment in range(count):
                painter.drawArc(
                    box,
                    int((base + segment * (360.0 / count)) * 16),
                    int(span * 16),
                )

    def _paint_voice_ring(self, painter, cx, cy, radius, color, level):
        ring = radius * 0.415
        lines = []
        for index in range(60):
            angle = math.radians(index * (360.0 / 60.0))
            ca, sa = math.cos(angle), math.sin(angle)
            wobble = 0.5 + 0.5 * math.sin(self._phase * 2.3 + index * 0.42)
            idle = radius * (
                0.012 + 0.008 * math.sin(self._phase * 1.2 + index * 0.7))
            height = idle + radius * 0.20 * level * wobble
            lines.append(QLineF(
                cx + ca * ring, cy + sa * ring,
                cx + ca * (ring + height), cy + sa * (ring + height)))
        painter.setPen(QPen(
            self._blend(color, 0.25 + 0.5 * level), 1.6))
        painter.drawLines(lines)
