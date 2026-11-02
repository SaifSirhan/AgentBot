"""Software-rendered reactor HUD for the terminal-style AgentBot window.

States are distinguishable without color: idle is dim and slow, listening is
dominated by a 60-spike ring, thinking is a fast white scanner, speaking
pulses its core with the live audio RMS, and error is a thick static block.
Continuous listening reuses the pulse geometry in bright cyan so an open
hands-free mic never looks like idle.

Amplitude is carried by geometry, never by hue: louder audio grows the core
radius, lengthens the voice spikes, thickens the bloom ring and spawns more and
larger particles. The layer paints no opaque backdrop — every glow stop is a
true-alpha colour, so the HUD adds light over the chat canvas instead of
darkening it, and it sets WA_TransparentForMouseEvents so text under it stays
clickable.
"""
from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass

from PySide6.QtCore import QPointF, QLineF, QRectF, Qt, QTimer
from PySide6.QtGui import (
    QBrush,
    QColor,
    QPainter,
    QPen,
    QPixmap,
    QPolygonF,
    QRadialGradient,
)
from PySide6.QtWidgets import QWidget

from audio_bus import audio_bus as default_audio_bus


@dataclass(frozen=True)
class HudStyle:
    speed: float
    color: QColor
    brightness: float
    mode: str


PARTICLE_CAP = 50
BURST_MIN = 5
BURST_MAX = 10
TAU = 2.0 * math.pi


@dataclass
class Particle:
    """One short-lived geometric burst element.

    Position is polar (angle + radius from the core) because every particle
    leaves the centre; converting to cartesian only happens at paint time.
    """

    alive: bool = False
    angle: float = 0.0
    radius: float = 0.0
    speed: float = 0.0
    size: float = 0.0
    growth: float = 0.0
    width: float = 0.0
    spin: float = 0.0
    heading: float = 0.0
    age: float = 0.0
    life: float = 1.0
    attack: float = 0.06
    sides: int = 4

    @property
    def progress(self) -> float:
        return min(1.0, self.age / self.life) if self.life > 0.0 else 1.0

    @property
    def fade(self) -> float:
        """Rises over `attack` then decays linearly, so nothing pops at full."""
        rise = min(1.0, self.age / self.attack) if self.attack > 0.0 else 1.0
        return rise * (1.0 - self.progress)

    def advance(self, dt: float) -> None:
        if not self.alive:
            return
        self.age += dt
        if self.age >= self.life:
            self.alive = False
            return
        self.radius += self.speed * dt
        self.size += self.growth * dt
        self.heading = (self.heading + self.spin * dt) % 360.0


class ParticleField:
    """Fixed pool of burst particles, reused in place.

    The cap is structural: the list is allocated once and slots are flagged, so
    a frame never appends, shrinks or reallocates, and the count cannot exceed
    the pool no matter how fast onsets arrive.
    """

    def __init__(self, cap: int = PARTICLE_CAP, rng: random.Random = None):
        self.cap = max(1, int(cap))
        self.rng = rng if rng is not None else random.Random()
        self._pool = [Particle() for _ in range(self.cap)]

    def __len__(self) -> int:
        return sum(1 for particle in self._pool if particle.alive)

    @property
    def items(self):
        return self._pool

    def alive(self):
        return [particle for particle in self._pool if particle.alive]

    def clear(self) -> None:
        for particle in self._pool:
            particle.alive = False

    def spawn(self, count: int, amplitude: float, base_radius: float) -> int:
        """Seed up to `count` free slots. Returns how many actually started."""
        wanted = max(0, min(int(count), self.cap))
        free = [p for p in self._pool if not p.alive][:wanted]
        if not free:
            return 0
        amplitude = max(0.0, min(1.0, float(amplitude)))
        rng = self.rng
        # Even spread plus jitter. Pure random angles clump, and clumps read as
        # noise instead of as a burst leaving the core.
        step = TAU / len(free)
        for index, particle in enumerate(free):
            particle.alive = True
            particle.angle = index * step + rng.uniform(-step * 0.35, step * 0.35)
            particle.radius = base_radius * rng.uniform(0.10, 0.30)
            particle.speed = base_radius * (0.35 + 0.85 * amplitude)
            particle.size = base_radius * (0.035 + 0.055 * amplitude)
            particle.growth = base_radius * (0.05 + 0.10 * amplitude)
            particle.width = 1.0 + 2.5 * amplitude
            particle.spin = rng.uniform(-140.0, 140.0)
            particle.heading = rng.uniform(0.0, 360.0)
            particle.age = 0.0
            particle.life = rng.uniform(0.45, 0.85)
            particle.sides = 3 if rng.random() < 0.40 else 4
        return len(free)

    def advance(self, dt: float) -> None:
        for particle in self._pool:
            particle.advance(dt)


class SpikeDetector:
    """Onset detector over the HUD's already-smoothed level.

    Compares the fast level against its own slow baseline and then waits out a
    refractory window, so one shout is one burst rather than one burst per
    frame. It is fed a number the GUI thread already computed and never touches
    the AudioBus lock, which stays the tick path's business.
    """

    def __init__(self, threshold: float = 0.06, minimum: float = 0.08,
                 baseline_tau: float = 0.35, refractory: float = 0.14):
        self.threshold = float(threshold)
        self.minimum = float(minimum)
        self.baseline_tau = float(baseline_tau)
        self.refractory = float(refractory)
        self.baseline = 0.0
        self._cooldown = 0.0

    def reset(self) -> None:
        self.baseline = 0.0
        self._cooldown = 0.0

    def update(self, level: float, dt: float) -> float:
        """Advance by `dt`; returns the onset strength (0.0 when no spike)."""
        dt = max(0.0, float(dt))
        level = max(0.0, float(level))
        self._cooldown = max(0.0, self._cooldown - dt)
        if dt > 0.0:
            k = dt / (dt + self.baseline_tau)
            self.baseline += (level - self.baseline) * k
        if self._cooldown > 0.0:
            return 0.0
        rise = level - self.baseline
        if level < self.minimum or rise < self.threshold:
            return 0.0
        self._cooldown = self.refractory
        # Drag the baseline up with the onset, or a passage that stays loud
        # keeps clearing the threshold every refractory window.
        self.baseline = max(self.baseline, level - self.threshold)
        return rise


def burst_count(strength: float, span: float = 0.35,
                low: int = BURST_MIN, high: int = BURST_MAX) -> int:
    """5..10 particles, scaling with how far the onset cleared the baseline."""
    ratio = max(0.0, min(1.0, strength / span)) if span > 0.0 else 1.0
    return int(round(low + (high - low) * ratio))


class ReactorHud(QWidget):
    SIZE = 240
    STATES = (
        "IDLE", "LISTENING", "CONTINUOUS", "THINKING", "SPEAKING", "ERROR")

    _MINT = QColor("#70FFD4")
    _CYAN = QColor("#25F2FF")
    _WHITE = QColor("#ECFFF8")
    _RED = QColor("#FF5B6E")
    # Projection colour for the layers that carry no state meaning: scanlines
    # and particles. Kept separate from the state colours so a state change can
    # never be the only cue for something.
    _HOLO = QColor("#00E5FF")

    # One revolution every 20 s. Independent of each state's `speed`, which is
    # about agitation, not rotation.
    RING_DEG_PER_SEC = 18.0
    SCAN_PERIOD = 6.0
    SCAN_WIDTH = 2.0
    SCAN_ALPHA = 0.16
    SCAN_SPEED = 16.0

    # Thick/dim, medium, hairline/bright: three passes over the same ellipse is
    # what makes a neon tube read as glowing rather than as an outline.
    BLOOM_PASSES = ((8.0, 0.10), (3.0, 0.40), (1.0, 1.00))

    _STYLES = {
        "IDLE": HudStyle(0.45, _MINT, 0.30, "circles"),
        "LISTENING": HudStyle(1.0, _MINT, 0.85, "spikes"),
        "CONTINUOUS": HudStyle(2.0, _CYAN, 1.0, "pulse"),
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
        self._ring_angle = 0.0
        self._scan_offset = 0.0
        self._particles = ParticleField()
        self._spikes = SpikeDetector()
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

    @property
    def ring_angle(self) -> float:
        return self._ring_angle

    @property
    def particles(self) -> ParticleField:
        return self._particles

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
        if self._state in ("LISTENING", "CONTINUOUS", "SPEAKING"):
            _, rms = self._bus.get_latest()
            gain = 12.0 if self._bus.source == "mic" else 4.0
            target = min(1.0, rms * gain)
        attack = 0.45 if target > self._level else 0.10
        self._level += (target - self._level) * attack

        if self._state in ("SPEAKING", "CONTINUOUS"):
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

        self._ring_angle = (self._ring_angle
                            + dt * self.RING_DEG_PER_SEC) % 360.0
        self._scan_offset = (self._scan_offset
                             + dt * self.SCAN_SPEED) % self.SCAN_PERIOD

        # Only the audio-driven states can spike: thinking and error carry no
        # signal, and idle must not fire on its own decay toward silence.
        if self._state in ("LISTENING", "CONTINUOUS", "SPEAKING"):
            strength = self._spikes.update(self._level, dt)
        else:
            self._spikes.update(0.0, dt)
            strength = 0.0
        if strength > 0.0:
            self._particles.spawn(burst_count(strength), self._level,
                                  min(self.width(), self.height()) * 0.38)
        self._particles.advance(dt)

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
    def _glow(color: QColor, alpha: float) -> QColor:
        """Same hue at a true alpha.

        The old `_blend` helper scaled RGB and stayed opaque, so a low alpha gave
        a near-black pixel: over the chat it darkened the canvas rather than
        lighting it. Every layer uses this form now.
        """
        out = QColor(color)
        out.setAlpha(int(255 * max(0.0, min(1.0, float(alpha)))))
        return out

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
        painter.setPen(QPen(self._glow(self._MINT, 0.42), 1.3))
        painter.drawLines(major)
        painter.setPen(QPen(self._glow(self._MINT, 0.18), 1.0))
        painter.drawLines(minor)
        # An asymmetric index mark. A 72-fold symmetric ring looks identical at
        # every angle, so the slow rotation would otherwise be invisible — and
        # it is a shape, not a hue, so it survives a colour-blind palette too.
        painter.setPen(QPen(self._glow(self._MINT, 0.72), 2.4))
        painter.drawLine(QLineF(cx, cy - radius * 0.86, cx, cy - radius))
        painter.end()
        self._tick_cache = pixmap
        self._tick_cache_size = (width, height)

    # -- painting -------------------------------------------------------
    def paintEvent(self, event):
        self._paint(QPainter(self))

    def _paint(self, painter):
        """Render one frame onto `painter`.

        Split out from paintEvent so the layers can be painted onto a known
        canvas in tests — the no-dark-disc and paint-path-touches-no-bus
        assertions are impossible against a widget.

        There is deliberately no opaque clear here. This widget is a translucent
        overlay, so its painter targets the parent's pixels: a Source fill would
        erase the chat canvas behind the HUD and the window would flush black
        through the hole — that erase, not any gradient, is what the old dark
        disc actually was. Qt repaints the parent beneath each frame, so frames
        do not stack.
        """
        if not painter.isActive():
            return
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
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

            self._paint_glow(painter, cx, cy, radius, style)
            self._paint_scanlines(painter, cx, cy, radius)
            if style.mode != "sharp" and self._tick_cache is not None:
                painter.setOpacity(self._opacity * style.brightness)
                # The ring turns once every 20 s regardless of state agitation.
                painter.save()
                painter.translate(cx, cy)
                painter.rotate(self._ring_angle)
                painter.translate(-cx, -cy)
                painter.drawPixmap(0, 0, self._tick_cache)
                painter.restore()
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
                self._glow(style.color, self._alpha(0.35 + 0.45 * self._level)),
                2.2 if style.mode == "pulse" else 1.5))
            painter.drawEllipse(
                QRectF(cx - inner, cy - inner, inner * 2, inner * 2))

            self._paint_particles(painter, cx, cy)
        finally:
            painter.end()

    def _paint_error_block(self, painter, cx, cy, radius, color):
        block = radius * 0.74
        rect = QRectF(cx - block / 2, cy - block / 2, block, block)
        painter.setBrush(QBrush(self._glow(color, self._alpha(0.24))))
        painter.setPen(QPen(
            self._glow(color, self._alpha(0.95)),
            max(6.0, radius * 0.075)))
        painter.drawRect(rect)
        bar = radius * 0.17
        painter.fillRect(
            QRectF(cx - radius * 0.54, cy - bar / 2, radius * 1.08, bar),
            self._glow(color, self._alpha(0.92)))
        painter.setBrush(Qt.BrushStyle.NoBrush)

    def _paint_glow(self, painter, cx, cy, radius, style):
        """Two soft alpha halos around the core.

        This used to end on an opaque black gradient stop, which is exactly the
        dark disc that sat over the chat canvas. Every stop is now a true-alpha
        colour and the rim is fully transparent, so the layer only ever adds
        light.
        """
        painter.setPen(Qt.PenStyle.NoPen)
        lift = 1.0 + 0.55 * self._level
        if style.mode == "pulse":
            lift += 0.25 * self._level
        core = radius * self._core_ratio
        outer = radius * (0.72 if style.mode == "spikes" else 0.68)
        for grad_radius, alpha in ((outer, 0.22), (core, 0.38)):
            peak = min(0.95, self._alpha(alpha) * lift)
            gradient = QRadialGradient(cx, cy, max(1.0, grad_radius))
            gradient.setColorAt(0.0, self._glow(style.color, peak))
            gradient.setColorAt(0.45, self._glow(style.color, peak * 0.52))
            gradient.setColorAt(0.78, self._glow(style.color, peak * 0.18))
            gradient.setColorAt(1.0, self._glow(style.color, 0.0))
            painter.setBrush(QBrush(gradient))
            painter.drawEllipse(QRectF(
                cx - grad_radius, cy - grad_radius,
                grad_radius * 2, grad_radius * 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)

    @staticmethod
    def scanline_chords(cx, cy, radius, offset, period):
        """Horizontal chords of the reactor circle, `period` px apart.

        Chords rather than full-width lines: the overlay fades at the rim on its
        own, so no clip region is needed and it never reads as a square laid
        over the chat.
        """
        lines = []
        y = cy - radius + offset
        while y < cy + radius:
            dy = y - cy
            half = math.sqrt(max(0.0, radius * radius - dy * dy))
            lines.append(QLineF(cx - half, y, cx + half, y))
            y += period
        return lines

    def _paint_scanlines(self, painter, cx, cy, radius):
        """Slowly drifting 2px bars across the projection volume."""
        lines = self.scanline_chords(cx, cy, radius, self._scan_offset,
                                     self.SCAN_PERIOD)
        if not lines:
            return
        painter.setPen(QPen(
            self._glow(self._HOLO, self._alpha(self.SCAN_ALPHA)),
            self.SCAN_WIDTH))
        painter.drawLines(lines)

    @staticmethod
    def bloom_width(base: float, level: float) -> float:
        """Ring thickness for an amplitude. This is the colour-blind channel:
        loudness is measurable as width, not inferred from a hue."""
        return base * (1.0 + 0.55 * max(0.0, min(1.0, float(level))))

    def _paint_bloom_ring(self, painter, cx, cy, radius, color, level):
        """The main ring in three passes: fat/dim, medium, hairline/bright."""
        box = QRectF(cx - radius, cy - radius, radius * 2, radius * 2)
        for base_width, alpha in self.BLOOM_PASSES:
            painter.setPen(QPen(
                self._glow(color, self._alpha(alpha)),
                self.bloom_width(base_width, level)))
            painter.drawEllipse(box)

    def _paint_particles(self, painter, cx, cy):
        for particle in self._particles.alive():
            alpha = self._alpha(0.80) * particle.fade
            if alpha <= 0.0:
                continue
            x = cx + math.cos(particle.angle) * particle.radius
            y = cy + math.sin(particle.angle) * particle.radius
            painter.save()
            painter.translate(x, y)
            painter.rotate(particle.heading)
            painter.setPen(QPen(self._glow(self._HOLO, alpha), particle.width))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            half = particle.size / 2.0
            if particle.sides == 3:
                painter.drawPolygon(self._triangle(half))
            else:
                painter.drawRect(QRectF(-half, -half, particle.size,
                                        particle.size))
            painter.restore()

    @staticmethod
    def _triangle(half: float) -> QPolygonF:
        """Point-up triangle inscribed in the same radius as a square's half."""
        points = []
        for step in range(3):
            angle = -math.pi / 2.0 + step * TAU / 3.0
            points.append(QPointF(math.cos(angle) * half * 1.15,
                                  math.sin(angle) * half * 1.15))
        return QPolygonF(points)

    def _paint_frame(self, painter, cx, cy, radius, width, height, style):
        # The main ring carries the bloom; the 0.93 ring below stays a hairline
        # so the assembly keeps a crisp outer edge.
        self._paint_bloom_ring(painter, cx, cy, radius, style.color,
                               self._level)
        painter.setPen(QPen(
            self._glow(style.color, self._alpha(0.16)), 1.0))
        outer = radius * 0.93
        painter.drawEllipse(QRectF(
            cx - outer, cy - outer, outer * 2, outer * 2))

        gap = radius * 0.62
        painter.setPen(QPen(
            self._glow(style.color, self._alpha(0.16)), 1.0))
        painter.drawLine(QLineF(cx - width / 2.0, cy, cx - gap, cy))
        painter.drawLine(QLineF(cx + gap, cy, cx + width / 2.0, cy))
        painter.drawLine(QLineF(cx, cy - height / 2.0, cx, cy - gap))
        painter.drawLine(QLineF(cx, cy + gap, cx, cy + height / 2.0))

        margin = min(width, height) * 0.035
        arm = min(width, height) * 0.055
        painter.setPen(QPen(
            self._glow(style.color, self._alpha(0.45)), 1.4))
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
                self._glow(color, self._alpha(alpha)), width))
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
                self._glow(color, self._alpha(alpha)), width))
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
            self._glow(color, self._alpha(alpha)),
            2.6 if aggressive else 1.6))
        painter.drawLines(lines)
