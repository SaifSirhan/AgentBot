"""Software-rendered reactor HUD for the terminal-style AgentBot window.

States are distinguishable without color: idle is dim and slow, listening is
dominated by a 60-spike ring, thinking is a fast white scanner, speaking
pulses its core with the live audio RMS, and error is a thick static block.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass

from PySide6.QtCore import QLineF, QRectF, Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QPainter, QPen, QPixmap, QRadialGradient
from PySide6.QtWidgets import QWidget

from audio_bus import audio_bus as default_audio_bus


@dataclass(frozen=True)
class HudStyle:
    speed: float
    color: QColor
    brightness: float
    mode: str


class ReactorHud(QWidget):
    SIZE = 240
    STATES = ("IDLE", "LISTENING", "THINKING", "SPEAKING", "ERROR")

    _MINT = QColor("#70FFD4")
    _WHITE = QColor("#ECFFF8")
    _RED = QColor("#FF5B6E")
    _BG = QColor("#000000")

    _STYLES = {
        "IDLE": HudStyle(0.45, _MINT, 0.30, "circles"),
        "LISTENING": HudStyle(1.0, _MINT, 0.85, "spikes"),
        "THINKING": HudStyle(3.2, _WHITE, 1.0, "sharp"),
        "SPEAKING": HudStyle(1.5, _WHITE, 1.0, "pulse"),
        "ERROR": HudStyle(0.0, _RED, 1.0, "block"),
    }

    def __init__(self, bus=None, parent=None, opacity: float = 1.0):
        super().__init__(parent)
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAutoFillBackground(False)
        self._bus = bus or default_audio_bus
        self._opacity = max(0.0, min(1.0, float(opacity)))
        self._state = "IDLE"
        self._phase = 0.0
        self._level = 0.0
        self._core_ratio = 0.32
        self._paint_tick = 0
        self._last_tick = time.monotonic()
        self._tick_cache = None
        self._tick_cache_size = (self.SIZE, self.SIZE)
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

    @property
    def opacity(self) -> float:
        return self._opacity

    @property
    def core_radius_ratio(self) -> float:
        return self._core_ratio

    def set_state(self, state_name: str) -> None:
        name = str(state_name or "").strip().upper()
        if name not in self._STYLES:
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

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if (self.width(), self.height()) != self._tick_cache_size:
            self._build_tick_cache()

    def _tick(self):
        now = time.monotonic()
        dt = max(0.0, min(0.1, now - self._last_tick))
        self._last_tick = now

        style = self._STYLES[self._state]
        self._phase = (self._phase + dt * style.speed) % 360.0

        target = 0.0
        if self._state in ("LISTENING", "SPEAKING"):
            _, rms = self._bus.get_latest()
            gain = 12.0 if self._bus.source == "mic" else 4.0
            target = min(1.0, rms * gain)
        attack = 0.45 if target > self._level else 0.10
        self._level += (target - self._level) * attack

        if self._state == "SPEAKING":
            breathing = 0.5 + 0.5 * math.sin(self._phase * 2.6)
            self._core_ratio = 0.24 + 0.10 * breathing + 0.34 * self._level
        elif self._state == "THINKING":
            self._core_ratio = 0.25
        elif self._state == "LISTENING":
            self._core_ratio = 0.30 + 0.04 * self._level
        elif self._state == "ERROR":
            self._core_ratio = 0.32
        else:
            self._core_ratio = 0.32

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

    def _alpha(self, alpha: float) -> float:
        return min(1.0, max(0.0, alpha * self._STYLES[self._state].brightness))

    def _build_tick_cache(self):
        width, height = max(1, self.width()), max(1, self.height())
        pixmap = QPixmap(width, height)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        if not painter.isActive():
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        cx, cy = width / 2.0, height / 2.0
        radius = min(width, height) * 0.38
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
        self._tick_cache_size = (width, height)

    # -- painting -------------------------------------------------------
    def paintEvent(self, event):
        painter = QPainter(self)
        if not painter.isActive():
            return
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setCompositionMode(
                QPainter.CompositionMode.CompositionMode_Source)
            painter.fillRect(self.rect(), Qt.GlobalColor.transparent)
            painter.setCompositionMode(
                QPainter.CompositionMode.CompositionMode_SourceOver)
            painter.setOpacity(self._opacity)

            width, height = self.width(), self.height()
            cx, cy = width / 2.0, height / 2.0
            radius = min(width, height) * 0.38
            style = self._STYLES[self._state]

            if style.mode == "block":
                self._paint_error_block(painter, cx, cy, radius, style.color)
                return

            self._paint_atmosphere(painter, cx, cy, radius, style)
            if style.mode != "sharp" and self._tick_cache is not None:
                painter.setOpacity(self._opacity * style.brightness)
                painter.drawPixmap(0, 0, self._tick_cache)
                painter.setOpacity(self._opacity)
            self._paint_frame(painter, cx, cy, radius, width, height, style)
            if style.mode == "sharp":
                self._paint_sharp_arcs(painter, cx, cy, radius, style.color)
            else:
                self._paint_arcs(painter, cx, cy, radius, style.color)
            if style.mode == "spikes":
                self._paint_voice_ring(
                    painter, cx, cy, radius, style.color, self._level,
                    aggressive=True)
            elif style.mode in ("circles", "pulse"):
                self._paint_voice_ring(
                    painter, cx, cy, radius, style.color, self._level,
                    aggressive=False)

            inner = radius * self._core_ratio
            painter.setPen(QPen(
                self._blend(style.color, self._alpha(0.35 + 0.45 * self._level)),
                2.2 if style.mode == "pulse" else 1.5))
            painter.drawEllipse(
                QRectF(cx - inner, cy - inner, inner * 2, inner * 2))
        finally:
            painter.end()

    def _paint_error_block(self, painter, cx, cy, radius, color):
        block = radius * 0.74
        rect = QRectF(cx - block / 2, cy - block / 2, block, block)
        painter.setBrush(QBrush(self._blend(color, self._alpha(0.24))))
        painter.setPen(QPen(
            self._blend(color, self._alpha(0.95)),
            max(6.0, radius * 0.075)))
        painter.drawRect(rect)
        bar = radius * 0.17
        painter.fillRect(
            QRectF(cx - radius * 0.54, cy - bar / 2, radius * 1.08, bar),
            self._blend(color, self._alpha(0.92)))
        painter.setBrush(Qt.BrushStyle.NoBrush)

    def _paint_atmosphere(self, painter, cx, cy, radius, style):
        painter.setPen(Qt.PenStyle.NoPen)
        lift = 1.0 + 0.55 * self._level
        if style.mode == "pulse":
            lift += 0.25 * self._level
        core = radius * self._core_ratio
        outer = radius * (0.72 if style.mode == "spikes" else 0.68)
        for grad_radius, alpha in ((outer, 0.22), (core, 0.38)):
            gradient = QRadialGradient(cx, cy, max(1.0, grad_radius))
            gradient.setColorAt(
                0.0, self._blend(
                    style.color, min(0.95, self._alpha(alpha) * lift)))
            gradient.setColorAt(
                0.45, self._blend(
                    style.color, min(0.95, self._alpha(alpha) * lift * 0.52)))
            gradient.setColorAt(
                0.78, self._blend(
                    style.color, min(0.95, self._alpha(alpha) * lift * 0.18)))
            gradient.setColorAt(1.0, self._BG)
            painter.setBrush(QBrush(gradient))
            painter.drawEllipse(QRectF(
                cx - grad_radius, cy - grad_radius,
                grad_radius * 2, grad_radius * 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)

    def _paint_frame(self, painter, cx, cy, radius, width, height, style):
        painter.setPen(QPen(
            self._blend(style.color, self._alpha(0.34)), 1.0))
        painter.drawEllipse(QRectF(
            cx - radius, cy - radius, radius * 2, radius * 2))
        painter.setPen(QPen(
            self._blend(style.color, self._alpha(0.16)), 1.0))
        outer = radius * 0.93
        painter.drawEllipse(QRectF(
            cx - outer, cy - outer, outer * 2, outer * 2))

        gap = radius * 0.62
        painter.setPen(QPen(
            self._blend(style.color, self._alpha(0.16)), 1.0))
        painter.drawLine(QLineF(cx - width / 2.0, cy, cx - gap, cy))
        painter.drawLine(QLineF(cx + gap, cy, cx + width / 2.0, cy))
        painter.drawLine(QLineF(cx, cy - height / 2.0, cx, cy - gap))
        painter.drawLine(QLineF(cx, cy + gap, cx, cy + height / 2.0))

        margin = min(width, height) * 0.035
        arm = min(width, height) * 0.055
        painter.setPen(QPen(
            self._blend(style.color, self._alpha(0.45)), 1.4))
        for sx, sy in ((1, 1), (-1, 1), (1, -1), (-1, -1)):
            x = cx + sx * (width / 2.0 - margin)
            y = cy + sy * (height / 2.0 - margin)
            painter.drawLine(QLineF(x, y, x - sx * arm, y))
            painter.drawLine(QLineF(x, y, x, y - sy * arm))

    def _paint_arcs(self, painter, cx, cy, radius, color):
        bands = (
            (0.955, 118, 2, +1, 0.75, 2.0),
            (0.845, 82, 3, -1, 0.38, 1.3),
            (0.760, 150, 1, +1, 0.45, 1.6),
            (0.660, 64, 4, -1, 0.26, 1.1),
            (0.545, 128, 2, +1, 0.30, 1.2),
        )
        for index, (factor, span, count, direction, alpha, width) in enumerate(
                bands):
            rad = radius * factor
            painter.setPen(QPen(
                self._blend(color, self._alpha(alpha)), width))
            box = QRectF(cx - rad, cy - rad, rad * 2, rad * 2)
            base = (self._phase * (9 + index * 6) * direction) % 360.0
            for segment in range(count):
                painter.drawArc(
                    box,
                    int((base + segment * (360.0 / count)) * 16),
                    int(span * 16),
                )

    def _paint_sharp_arcs(self, painter, cx, cy, radius, color):
        bands = (
            (0.94, 26, 4, +1, 0.95, 5.0),
            (0.80, 20, 3, -1, 0.72, 3.4),
            (0.66, 16, 2, +1, 0.52, 2.2),
        )
        for index, (factor, span, count, direction, alpha, width) in enumerate(
                bands):
            rad = radius * factor
            painter.setPen(QPen(
                self._blend(color, self._alpha(alpha)), width))
            box = QRectF(cx - rad, cy - rad, rad * 2, rad * 2)
            base = (self._phase * (22 + index * 8) * direction) % 360.0
            for segment in range(count):
                painter.drawArc(
                    box,
                    int((base + segment * (360.0 / count)) * 16),
                    int(span * 16),
                )

    def _paint_voice_ring(self, painter, cx, cy, radius, color, level,
                          *, aggressive=False):
        ring = radius * 0.415
        lines = []
        for index in range(60):
            angle = math.radians(index * (360.0 / 60.0))
            ca, sa = math.cos(angle), math.sin(angle)
            wobble = 0.5 + 0.5 * math.sin(self._phase * 2.3 + index * 0.42)
            if aggressive:
                base = radius * (0.055 + 0.035 * wobble)
                height = base + radius * 0.48 * level * wobble
            else:
                base = radius * (
                    0.012 + 0.008 * math.sin(
                        self._phase * 1.2 + index * 0.7))
                height = base + radius * 0.20 * level * wobble
            lines.append(QLineF(
                cx + ca * ring, cy + sa * ring,
                cx + ca * (ring + height), cy + sa * (ring + height)))
        alpha = 0.35 + 0.55 * level if aggressive else 0.25 + 0.5 * level
        painter.setPen(QPen(
            self._blend(color, self._alpha(alpha)),
            2.6 if aggressive else 1.6))
        painter.drawLines(lines)
