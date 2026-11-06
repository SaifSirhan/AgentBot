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

`PetWidget` is the desktop pet that shares that canvas. It is a sibling layer
rather than a seventh reactor state: it plays `assets/pet/frames/<pose>/` into the
bottom-right of the chat viewport under the same alpha-only rule, and the HUD's
existing tick drives both its breathing bob and its frame clock, so it never
needs a timer of its own.
"""
from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QPointF, QLineF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QCursor,
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

# -- desktop pet -------------------------------------------------------
PET_ASSET_DIR = Path(__file__).resolve().parent / "assets" / "pet"
# One directory per pose under frames/, holding <pose>_<n>.png cycle frames plus
# an optional <pose>_1_blink.png twin. `blinking` used to be a state name; under
# the animation engine it is a flag layered on a pose, so it is not a pose.
PET_FRAMES_DIR = PET_ASSET_DIR / "frames"
PET_POSES = ("idle", "thinking", "sleeping", "speaking", "reading",
             "walking", "flying", "looking")
PET_FALLBACK = "idle"
PET_BLINK_KEY = "1_blink"
# Cycles per second. None means the pose ships a single frame, so there is
# nothing to advance and the pet simply holds it. Speaking runs faster than the
# other cycles because its wave is a mouth: at 8 fps the two-frame pulse beats
# four times a second, which is about the syllable rate of the TTS it trails.
PET_FPS = {"idle": None, "walking": 6.0, "flying": 8.0, "thinking": 2.0,
           "reading": 2.0, "speaking": 8.0, "sleeping": None,
           "looking": 2.0}
# The offline alignment step builds every frame onto this canvas with the feet on
# a fixed baseline, so the pose can change without the pet shifting. The asset
# guard checks it: a frame at another size would move the bird mid-cycle.
PET_CANVAS = 300
# Idle-only micro-behaviours. Both ride the HUD's tick, so neither needs a
# timer of its own.
PET_BLINK_DURATION = 0.18
PET_BLINK_GAP = (4.0, 7.0)
# Two shipped blink twins are defective: the thinking one puts a hole in the arm
# over the laptop and the reading one puts a hole beside the book. Until that art
# is redrawn the pet holds its eyes open in those poses rather than flashing a
# punched-out frame every few seconds.
PET_BLINK_SKIP = ("thinking", "reading")
# Roaming: after this long with nothing happening the pet walks itself around
# the bottom of the viewport. 40 px/s is about a body length a second.
PET_ROAM_AFTER = 45.0
PET_ROAM_SPEED = 40.0
PET_ROAM_PAUSE = (2.0, 5.0)
PET_ROAM_FLY_CHANCE = 0.2
PET_ROAM_ARC = 40.0
# Sleep: a pet left alone for half an hour goes to bed, and only ten minutes at
# night. Night is the local clock, 22:00 to 07:00.
PET_SLEEP_AFTER = 1800.0
PET_SLEEP_AFTER_NIGHT = 600.0
PET_NIGHT_START = 22
PET_NIGHT_END = 7
# Floating Zzz: three glyphs, staggered a third of a period apart, each rising
# PET_ZZZ_RISE px over PET_ZZZ_PERIOD s while it fades in and back out.
PET_ZZZ_COUNT = 3
PET_ZZZ_PERIOD = 2.8
PET_ZZZ_RISE = 46.0
# Idle variation: after this much quiet the pet now and then plays its `looking`
# cycle for a few seconds — glance, peck, glance — then picks up where it was.
PET_IDLE_VARIATION_AFTER = 120.0
PET_IDLE_VARIATION_DURATION = (4.0, 7.0)
PET_IDLE_VARIATION_GAP = (60.0, 150.0)
# Cursor proximity: within this many pixels of the sprite rect the pet comes to
# the front of the message body, and drops back after this many seconds away.
PET_PROXIMITY_PX = 120.0
PET_PROXIMITY_RELEASE_S = 1.5
PET_HEIGHT = 190
PET_MARGIN = 24
PET_OPACITY = 0.85
# Assistant-controllable motion. Everything below rides the HUD's existing tick:
# a command sets a target and a duration, and `advance(dt)` walks it there — no
# second timer, no sleep on the UI thread. `PET_SCALE` can be changed in code or
# live through `set_scale`.
PET_SCALE = 1.0
PET_SCALE_RANGE = (0.4, 3.0)
PET_FLY_SPEED = 420.0          # px/s, so distance sets the duration
PET_FLY_MIN_S = 0.35
PET_FLY_MAX_S = 1.8
PET_POP_OUT_S = 0.45
PET_POP_IN_S = 0.35
PET_POP_SCALE = 0.35           # scale the pet pops in from / shrinks to
PET_ANCHORS = ("bottom_right", "bottom_left", "top_right", "top_left")
# A `fly_around` tour as fractions of the viewport, then back to the anchor.
PET_TOUR = ((0.18, 0.22), (0.78, 0.18), (0.82, 0.60), (0.20, 0.64))
# `sin(elapsed * PET_BOB_RATE) * PET_BOB_AMPLITUDE` — a 5 px rise over a ~3.1 s
# cycle. Driven by the HUD's own tick, so the pet never needs a timer of its own.
PET_BOB_RATE = 2.0
PET_BOB_AMPLITUDE = 5.0


def pet_is_night(hour: int) -> bool:
    """Whether an hour of the local clock falls in the short-sleep window."""
    hour = int(hour) % 24
    return hour >= PET_NIGHT_START or hour < PET_NIGHT_END


def pet_sleep_after(hour: int) -> float:
    """Seconds of quiet before the pet sleeps at that hour."""
    return PET_SLEEP_AFTER_NIGHT if pet_is_night(hour) else PET_SLEEP_AFTER


def pet_bob(elapsed: float, rate: float = PET_BOB_RATE,
            amplitude: float = PET_BOB_AMPLITUDE) -> float:
    """Vertical offset for the pet at `elapsed` seconds of animation time.

    Takes an accumulated elapsed value rather than calling a clock itself, so a
    test can drive it from a fake tick and get an exact number.
    """
    return math.sin(max(0.0, float(elapsed)) * rate) * amplitude


def pet_frame_index(elapsed: float, fps, frame_count: int) -> int:
    """Which frame of a cycle shows `elapsed` seconds in.

    Pure and clock-free for the same reason as `pet_bob`. A pose with no fps, or
    with one frame, always shows frame 0 rather than dividing by zero.
    """
    if fps is None or frame_count < 2:
        return 0
    return int(max(0.0, float(elapsed)) * float(fps)) % int(frame_count)


def pet_zzz_progress(elapsed: float, period: float = PET_ZZZ_PERIOD,
                     count: int = PET_ZZZ_COUNT):
    """How far through its rise each floating Z is, as a tuple of 0..1.

    Glyph i starts a `1 / count` slice after glyph i-1, which is what makes the
    Zs trail one another instead of pulsing in lockstep. Clock-free and pure so
    a test can drive it from a fake tick, exactly like `pet_bob`.
    """
    count = max(1, int(count))
    period = max(1e-6, float(period))
    elapsed = max(0.0, float(elapsed))
    return tuple((elapsed / period + index / float(count)) % 1.0
                 for index in range(count))


def pet_ease(progress: float) -> float:
    """Smoothstep 0..1: zero velocity at both ends, so flight eases in and out.

    Pure and clamped for the same reason as the other pet helpers: a test can
    assert the endpoints and the midpoint exactly instead of timing an animation.
    """
    p = min(1.0, max(0.0, float(progress)))
    return p * p * (3.0 - 2.0 * p)


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
        self._pet = None
        self._pet_requested = PET_FALLBACK
        self._bob_elapsed = 0.0
        self._bob = 0.0
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

    @property
    def bob(self) -> float:
        return self._bob

    @property
    def pet(self):
        return self._pet

    @property
    def pet_state(self) -> str:
        """What the pet is drawn as, or the requested name while detached."""
        if self._pet is not None:
            return self._pet.pet_state
        return self._pet_requested

    def set_pet_widget(self, pet) -> None:
        """Adopt the pet; the HUD keeps the only animation timer."""
        self._pet = pet
        if pet is not None:
            pet.set_state(self._pet_requested)
            pet.set_bob(self._bob)

    def set_pet_state(self, state: str) -> None:
        """One of PET_POSES. Unknown names and missing art fall back to idle."""
        name = str(state or "").strip().lower()
        if name not in PET_POSES:
            name = PET_FALLBACK
        self._pet_requested = name
        if self._pet is not None:
            self._pet.set_state(name)

    def note_pet_activity(self) -> None:
        """Reset the pet's idle clock. Any real activity in the window calls this."""
        if self._pet is not None:
            self._pet.note_activity()

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

        # The pet carries no timer; this tick is the animation clock.
        self._bob_elapsed += dt
        self._bob = pet_bob(self._bob_elapsed)
        if self._pet is not None:
            self._pet.set_bob(self._bob)
            self._pet.advance(dt)

        self._paint_tick = (self._paint_tick + 1) % 2
        if self._paint_tick == 0 and self._on_screen():
            self.update()
            if self._pet is not None:
                self._pet.update()

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


class PetWidget(QWidget):
    """The desktop pet, anchored to the bottom-right of the chat viewport.

    Sized to the whole viewport and anchoring inside itself at paint time:
    the background HUD's 800 px box runs past the bottom of the chat at every
    window size (measured 32 to 152 px of overflow at 1280x800 down to 900x560),
    so an offset measured from that box would push the pet off-screen. The
    viewport is the only rectangle that is guaranteed visible, and its bottom
    edge sits above the input field, so the pet can never cover text entry —
    `WA_TransparentForMouseEvents` makes that doubly true.

    It carries no timer. The HUD's existing tick owns the animation clock and
    pushes each frame's bob in through `set_bob` and each frame's elapsed time in
    through `advance`, which is what plays the pose cycles, blinks the pet
    mid-stride and wanders it while it is genuinely idle.
    """

    HEIGHT = PET_HEIGHT
    MARGIN = PET_MARGIN

    # True when the pet wants to be in front of the message body. The chat view
    # owns the stacking, so it listens rather than being polled.
    layer_changed = Signal(bool)

    def __init__(self, parent=None, asset_dir=PET_ASSET_DIR,
                 opacity: float = PET_OPACITY, rng: random.Random = None,
                 hour_of_day=None, cursor=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAutoFillBackground(False)
        self._frames_dir = Path(asset_dir) / "frames"
        self._opacity = max(0.0, min(1.0, float(opacity)))
        self._rng = rng if rng is not None else random.Random()
        # Both injected so the sleep schedule and the proximity ring can be
        # driven from a test without touching the real clock or the real mouse.
        self._hour = hour_of_day if hour_of_day is not None else (
            lambda: time.localtime().tm_hour)
        self._cursor = cursor if cursor is not None else self._cursor_position
        self._requested = None
        self._pose = None
        self._pose_elapsed = 0.0
        self._frame_index = 0
        self._blinking = False
        self._blink_time = 0.0
        self._roaming = False
        self._roam_x = 0.0
        self._roam_start = 0.0
        self._roam_target = 0.0
        self._roam_y = 0.0
        self._roam_pause = 0.0
        self._roam_flying = False
        self._facing = 1
        self._sleeping = False
        self._near = False
        self._near_time = 0.0
        self._idle_time = 0.0
        self._next_blink = 0.0
        # Sleep is its own clock: while it is asleep the pose cycle is stopped,
        # but the Zzz glyphs still need an elapsed time to ride up the screen.
        self._sleep_elapsed = 0.0
        self._variation = False
        self._variation_left = 0.0
        self._variation_return = None
        self._next_variation = 0.0
        # Assistant-controllable motion. `_fly` is the leg being flown, `_tour`
        # the points still queued behind it, and `_pop` the scale/fade flourish.
        self._scale = max(PET_SCALE_RANGE[0],
                          min(PET_SCALE_RANGE[1], float(PET_SCALE)))
        self._anchor = "bottom_right"
        self._fly = None
        self._tour = []
        self._pop = None
        self._pop_scale = 1.0
        self._fade = 1.0
        self._bob = 0.0
        self._drawn = (None, None)
        self._sprite = None
        self._cycles = {}
        self._twins = {}
        self._cache = {}
        # Awake from the first frame: a pet that draws nothing until the GUI
        # happens to sync would leave the corner empty at startup.
        self.set_state(PET_FALLBACK)
        self._arm_variation(PET_IDLE_VARIATION_AFTER)

    # -- state ----------------------------------------------------------
    @property
    def pet_state(self) -> str:
        """The pose actually drawn, after the fallbacks; '' when there is no art."""
        return self._drawn[0] or ""

    @property
    def requested_state(self) -> str:
        """What the GUI last asked for, before any blink or wander."""
        return self._requested or ""

    @property
    def pose(self) -> str:
        """The pose the animation engine is playing, fallbacks not applied."""
        return self._pose or ""

    @property
    def frame_index(self) -> int:
        """Position within the current pose's discovered frame list."""
        return self._frame_index

    @property
    def blinking(self) -> bool:
        """Whether the blink twin is standing in for the current frame."""
        return self._blinking

    @property
    def roaming(self) -> bool:
        """Whether the pet is walking or flying itself around the viewport."""
        return self._roaming

    @property
    def roam_x(self) -> float:
        """Horizontal offset from the home corner, negative is to the left."""
        return self._roam_x

    @property
    def facing(self) -> int:
        """1 when facing right, -1 when the blit is mirrored for left."""
        return self._facing

    @property
    def sleeping(self) -> bool:
        return self._sleeping

    @property
    def wants_front(self) -> bool:
        """Whether the cursor is close enough to bring the pet over the text."""
        return self._near

    @property
    def override(self):
        """The idle behaviour currently layered on, if any."""
        if self._blinking:
            return "blinking"
        if self._variation:
            return "looking"
        if self._roaming:
            return "flying" if self._roam_flying else "walking"
        return None

    @property
    def idle_time(self) -> float:
        return self._idle_time

    @property
    def bob(self) -> float:
        return self._bob

    @property
    def sprite(self):
        return self._sprite

    def set_state(self, state: str) -> None:
        """Record what the GUI wants and restart that pose from frame zero.

        An unchanged request is ignored, so a status line that fires repeatedly
        cannot keep re-arming the blink and stop the pet from ever blinking.
        """
        name = str(state or "").strip().lower()
        if name not in PET_POSES:
            name = PET_FALLBACK
        if name == self._requested:
            return
        self._requested = name
        self._wake()
        self._cancel_flight()
        self._idle_time = 0.0
        self._arm_blink()
        self._arm_variation(PET_IDLE_VARIATION_AFTER)
        self._set_pose(name)

    def note_activity(self) -> None:
        """Anything the user or the agent does resets the clock and wakes the pet.

        Called for messages, transcripts, tool turns, keys and clicks inside the
        window, and for the cursor entering the proximity ring.
        """
        self._idle_time = 0.0
        self._arm_blink()
        self._arm_variation(PET_IDLE_VARIATION_AFTER)
        self._wake()

    def _wake(self) -> None:
        """Drop sleep, blink, roam and the idle variation, and go back to what
        was asked for."""
        awake = (self._sleeping or self._roaming or self._blinking
                 or self._variation)
        self._sleeping = False
        self._blinking = False
        self._blink_time = 0.0
        self._roaming = False
        self._roam_pause = 0.0
        self._roam_y = 0.0
        self._variation = False
        self._variation_left = 0.0
        self._sleep_elapsed = 0.0
        if awake:
            self._set_pose(self._requested)

    def _set_pose(self, pose: str) -> None:
        """Switch pose and rewind its cycle."""
        if pose == self._pose:
            return
        self._pose = pose
        self._pose_elapsed = 0.0
        self._frame_index = 0
        if pose != "sleeping":
            self._sleep_elapsed = 0.0
        self._refresh()

    def advance(self, dt: float) -> None:
        """Play the cycle, blink, roam, vary and sleep on the HUD's tick seconds.

        A blink freezes the cycle instead of interrupting it: the twin stands in
        for the current frame for `PET_BLINK_DURATION` and playback resumes where
        it left off, so a walking pet blinks mid-stride. Blinking is awake
        behaviour and layers on every pose that ships a twin — `sleeping` does not
        have one, because its eyes are already shut, and the two poses in
        `PET_BLINK_SKIP` hold their eyes open until their art is redrawn.

        The idle variation is the same freeze in the other direction: after a
        couple of quiet minutes the pet stops, plays its `looking` cycle for a
        few seconds, and then puts back whatever pose it was in.
        """
        dt = max(0.0, float(dt))
        self._idle_time += dt
        if self._sleeping or self._pose == "sleeping":
            self._sleep_elapsed += dt
        self._poll_cursor(dt)
        moving = self._update_motion(dt)
        if self._blinking:
            self._blink_time += dt
            if self._blink_time >= PET_BLINK_DURATION:
                self._blinking = False
                self._blink_time = 0.0
                self._arm_blink()
            self._refresh()
            return
        if self._sleeping:
            return
        # Bedtime outranks the flourish: a pet that has been alone long enough
        # goes to sleep even if a variation is playing or it is mid-walk.
        if (self._requested == PET_FALLBACK
                and self._idle_time >= pet_sleep_after(self._hour())):
            self._fall_asleep()
            return
        if moving:
            # A flight or pop owns the pet: no roaming, no variation, and the
            # pose cycle still plays (the flying frames while it is airborne).
            self._advance_cycle(dt)
            self._refresh()
            return
        if self._variation:
            self._variation_left -= dt
            self._advance_cycle(dt)
            if self._variation_left <= 0.0:
                self._end_variation()
            self._refresh()
            return
        if self._roaming:
            self._step_roam(dt)
        else:
            self._advance_cycle(dt)
            if (self._requested == PET_FALLBACK
                    and self._idle_time >= PET_ROAM_AFTER):
                self._start_roam()
                return
        if (self._requested == PET_FALLBACK
                and not (self._roaming and self._roam_flying)
                and self._idle_time >= self._next_variation):
            self._start_variation()
            return
        if (self._idle_time >= self._next_blink and self._twin(self._pose)
                and self._pose not in PET_BLINK_SKIP):
            self._blinking = True
            self._blink_time = 0.0
        self._refresh()

    def _advance_cycle(self, dt: float) -> None:
        self._pose_elapsed += dt
        self._frame_index = pet_frame_index(
            self._pose_elapsed, PET_FPS.get(self._pose),
            len(self._cycle(self._pose)))

    # -- roaming --------------------------------------------------------
    def _start_roam(self) -> None:
        self._roaming = True
        self._roam_pause = 0.0
        self._pick_roam_target()

    def _pick_roam_target(self) -> None:
        """Choose the next spot along the bottom, and how to get there."""
        travel = max(0.0, self.width() - self._sprite_width() - 2 * self.MARGIN)
        self._roam_start = self._roam_x
        self._roam_target = -self._rng.uniform(0.0, travel)
        self._roam_flying = self._rng.random() < PET_ROAM_FLY_CHANCE
        self._roam_y = 0.0
        self._set_pose("flying" if self._roam_flying else "walking")

    def _step_roam(self, dt: float) -> None:
        if self._roam_pause > 0.0:
            self._roam_pause -= dt
            if self._roam_pause <= 0.0:
                self._pick_roam_target()
            self._refresh()
            return
        remaining = self._roam_target - self._roam_x
        step = PET_ROAM_SPEED * dt
        if abs(remaining) <= step:
            self._roam_x = self._roam_target
            self._roam_y = 0.0
            self._roam_pause = self._rng.uniform(*PET_ROAM_PAUSE)
            self._set_pose("idle")
        else:
            self._roam_x += math.copysign(step, remaining)
            self._facing = 1 if remaining > 0 else -1
            if self._roam_flying:
                span = self._roam_target - self._roam_start
                progress = 0.0 if span == 0 else (
                    (self._roam_x - self._roam_start) / span)
                progress = max(0.0, min(1.0, progress))
                self._roam_y = -math.sin(math.pi * progress) * PET_ROAM_ARC
            self._advance_cycle(dt)
        self._refresh()

    def _sprite_width(self) -> float:
        sprite = self._sprite
        if sprite is None:
            return float(self.HEIGHT)
        return sprite.width() / (sprite.devicePixelRatio() or 1.0)

    def _sprite_rect(self) -> QRectF:
        rect = self._drawn_rect()
        return QRectF(rect.x(), rect.y() + self._bob,
                      rect.width(), rect.height())

    # -- sleep ----------------------------------------------------------
    def _fall_asleep(self) -> None:
        self._sleeping = True
        self._blinking = False
        self._blink_time = 0.0
        self._roaming = False
        self._roam_pause = 0.0
        self._roam_y = 0.0
        self._variation = False
        self._variation_left = 0.0
        self._sleep_elapsed = 0.0
        self._cancel_flight()
        self._pop = None
        self._pop_scale = 1.0
        self._fade = 1.0
        self._set_pose("sleeping")

    # -- cursor proximity -----------------------------------------------
    def _cursor_position(self):
        return self.mapFromGlobal(QCursor.pos())

    def _poll_cursor(self, dt: float) -> None:
        """Raise in front within the ring, drop back after the release delay.

        `WA_TransparentForMouseEvents` means the pet never receives an enter or
        leave event, so this polls instead — on the tick that is already running,
        never a timer or a global filter of its own.
        """
        ring = PET_PROXIMITY_PX
        near = self._sprite_rect().adjusted(
            -ring, -ring, ring, ring).contains(QPointF(self._cursor()))
        if near:
            self._near_time = 0.0
            if not self._near:
                self._near = True
                # Reaching the pet is activity too, so it wakes and stops roaming.
                self.note_activity()
                self.layer_changed.emit(True)
            return
        if not self._near:
            return
        self._near_time += dt
        if self._near_time >= PET_PROXIMITY_RELEASE_S:
            self._near = False
            self.layer_changed.emit(False)

    def _arm_blink(self) -> None:
        self._next_blink = self._idle_time + self._rng.uniform(*PET_BLINK_GAP)

    def _arm_variation(self, gap: float) -> None:
        self._next_variation = self._idle_time + max(0.0, float(gap))

    def _start_variation(self) -> None:
        """Play the `looking` cycle for a few seconds, then put the pose back."""
        self._variation = True
        self._variation_return = self._pose
        self._variation_left = self._rng.uniform(*PET_IDLE_VARIATION_DURATION)
        self._set_pose("looking")

    def _end_variation(self) -> None:
        self._variation = False
        self._variation_left = 0.0
        self._arm_variation(self._rng.uniform(*PET_IDLE_VARIATION_GAP))
        # A variation can interrupt a walk, so the pose to come back to is the
        # one that was playing when it started, not simply idle.
        self._set_pose((self._variation_return if self._roaming
                        else self._requested) or PET_FALLBACK)

    # -- assistant control ----------------------------------------------
    def play(self, action: str, params: dict = None) -> str:
        """Run one named sprite command. Returns a one-line status.

        Reached from the assistant's `sprite_control` tool through
        `sprite_bus.sprite_controller`; the call is a slot on the GUI thread and
        only sets targets, so it returns immediately.
        """
        action = str(action or "").strip().lower()
        params = dict(params or {})
        if action == "fly_to":
            try:
                x = float(params.get("x"))
                y = float(params.get("y"))
            except (TypeError, ValueError):
                return "ERROR: fly_to needs numeric x and y."
            self.fly_to(x, y)
            return "flying to (%.0f, %.0f)" % (x, y)
        if action == "fly_around":
            self.fly_around()
            return "flying around"
        if action == "pop_out":
            self.pop_out()
            return "popping out"
        if action == "pop_in":
            self.pop_in()
            return "popping in"
        if action == "set_scale":
            try:
                value = float(params.get("scale"))
            except (TypeError, ValueError):
                return "ERROR: set_scale needs a number."
            return "scale is now %.2fx" % self.set_scale(value)
        if action == "set_position":
            return "anchored %s" % self.set_position(params.get("anchor"))
        return "ERROR: unknown sprite action %r." % action

    def _take_control(self) -> None:
        """A command from the assistant outranks the automatic behaviour."""
        self._wake()
        self._idle_time = 0.0
        self._arm_blink()
        self._arm_variation(PET_IDLE_VARIATION_AFTER)

    def _cancel_flight(self) -> None:
        self._fly = None
        self._tour = []

    def _sprite_size(self):
        """The sprite's drawn size in widget pixels, before any pop scaling."""
        sprite = self._sprite
        height = float(self.HEIGHT) * self._scale
        if sprite is not None:
            height = sprite.height() / (sprite.devicePixelRatio() or 1.0)
        return self._sprite_width(), height

    def _home_xy(self):
        """Where the anchor preset puts the sprite, before roaming or flight."""
        width, height = self._sprite_size()
        right = max(0.0, self.width() - width - self.MARGIN)
        bottom = max(0.0, self.height() - height - self.MARGIN)
        x = float(self.MARGIN) if self._anchor.endswith("left") else right
        y = float(self.MARGIN) if self._anchor.startswith("top") else bottom
        return x, y

    def _position(self):
        """Current top-left of the sprite: the anchor plus the live offset.

        Flight and roaming both write the offset, so a pet that lands somewhere
        stays there — the offset is the single source of truth for "not home".
        """
        home_x, home_y = self._home_xy()
        return home_x + self._roam_x, home_y + self._roam_y

    def _drawn_rect(self) -> QRectF:
        """Rect the frame is blitted into, with the pop scale applied.

        Scaling is anchored at the bottom centre so the feet stay planted while
        the pet pops in or out.
        """
        width, height = self._sprite_size()
        drawn_w = width * self._pop_scale
        drawn_h = height * self._pop_scale
        x, y = self._position()
        x += (width - drawn_w) / 2.0
        y += height - drawn_h
        return QRectF(x, y, drawn_w, drawn_h)

    def _clamped_target(self, x, y):
        width, height = self._sprite_size()
        max_x = max(0.0, self.width() - width)
        max_y = max(0.0, self.height() - height)
        return min(max(0.0, float(x)), max_x), min(max(0.0, float(y)), max_y)

    def set_scale(self, factor) -> float:
        """Resize the sprite live. Returns the scale actually applied."""
        try:
            value = float(factor)
        except (TypeError, ValueError):
            return self._scale
        if not math.isfinite(value):
            return self._scale
        low, high = PET_SCALE_RANGE
        self._scale = max(low, min(high, value))
        # Frames are cached at the drawn height, so a new scale has to reload.
        self._cache.clear()
        self._drawn = (None, None)
        self._take_control()
        self._refresh()
        self.update()
        return self._scale

    def set_position(self, anchor) -> str:
        """Park the pet in a corner preset. Returns the preset applied."""
        name = (str(anchor or "").strip().lower()
                .replace("-", "_").replace(" ", "_"))
        if name not in PET_ANCHORS:
            name = "bottom_right"
        self._anchor = name
        self._cancel_flight()
        self._roam_x = 0.0
        self._roam_y = 0.0
        self._take_control()
        self.update()
        return name

    def fly_to(self, x, y) -> None:
        """Glide to a top-left position (widget pixels) with an ease in/out."""
        self._cancel_flight()
        self._take_control()
        self._begin_flight(x, y)

    def fly_around(self) -> None:
        """Fly a loop of viewport waypoints, then return to the anchor."""
        self._cancel_flight()
        self._take_control()
        home_x, home_y = self._home_xy()
        self._tour = [(self.width() * fx, self.height() * fy)
                      for fx, fy in PET_TOUR]
        self._tour.append((home_x, home_y))
        self._start_next_leg()

    def _start_next_leg(self) -> None:
        if not self._tour:
            self._set_pose(self._requested)
            return
        x, y = self._tour.pop(0)
        self._begin_flight(x, y)

    def _begin_flight(self, x, y) -> None:
        target_x, target_y = self._clamped_target(x, y)
        start_x, start_y = self._position()
        distance = math.hypot(target_x - start_x, target_y - start_y)
        duration = min(PET_FLY_MAX_S,
                       max(PET_FLY_MIN_S, distance / PET_FLY_SPEED))
        self._fly = {"x": start_x, "y": start_y,
                     "x0": start_x, "y0": start_y,
                     "x1": target_x, "y1": target_y,
                     "t": 0.0, "duration": duration}
        self._facing = 1 if target_x >= start_x else -1
        self._set_pose("flying")

    def pop_out(self) -> None:
        """Scale up and fade in at the pet's spot."""
        self._take_control()
        self._pop = {"s0": PET_POP_SCALE, "s1": 1.0, "f0": 0.0, "f1": 1.0,
                     "t": 0.0, "duration": PET_POP_OUT_S}
        self._pop_scale = PET_POP_SCALE
        self._fade = 0.0
        self.update()

    def pop_in(self) -> None:
        """Shrink back down and fade out."""
        self._take_control()
        self._pop = {"s0": self._pop_scale, "s1": PET_POP_SCALE,
                     "f0": self._fade, "f1": 0.0,
                     "t": 0.0, "duration": PET_POP_IN_S}

    def _update_motion(self, dt: float) -> bool:
        """Advance any flight or pop; True while one is still running."""
        active = False
        if self._fly is not None:
            active = True
            fly = self._fly
            fly["t"] += dt
            progress = pet_ease(fly["t"] / fly["duration"])
            fly["x"] = fly["x0"] + (fly["x1"] - fly["x0"]) * progress
            fly["y"] = fly["y0"] + (fly["y1"] - fly["y0"]) * progress
            home_x, home_y = self._home_xy()
            self._roam_x = fly["x"] - home_x
            self._roam_y = fly["y"] - home_y
            if fly["t"] >= fly["duration"]:
                self._fly = None
                if self._tour:
                    self._start_next_leg()
                else:
                    self._set_pose(self._requested)
        if self._pop is not None:
            active = True
            pop = self._pop
            pop["t"] += dt
            progress = pet_ease(pop["t"] / pop["duration"])
            self._pop_scale = pop["s0"] + (pop["s1"] - pop["s0"]) * progress
            self._fade = pop["f0"] + (pop["f1"] - pop["f0"]) * progress
            if pop["t"] >= pop["duration"]:
                self._pop = None
                self._pop_scale = pop["s1"]
                self._fade = pop["f1"]
        return active

    def _resolve(self):
        """(pose, frame key, pixmap) to draw, after the fallbacks in the spec.

        A missing pose directory, a missing frame or an unreadable PNG falls back
        to idle frame 1 and then to drawing nothing. A pose with no blink twin
        simply does not blink; it is never swapped for a different sprite.
        """
        if self._blinking:
            twin = self._load(self._pose, PET_BLINK_KEY)
            if twin is not None:
                return self._pose, PET_BLINK_KEY, twin
        cycle = self._cycle(self._pose)
        if cycle:
            number = cycle[min(self._frame_index, len(cycle) - 1)]
            frame = self._load(self._pose, number)
            if frame is not None:
                return self._pose, str(number), frame
        fallback = self._load(PET_FALLBACK, 1)
        if fallback is not None:
            return PET_FALLBACK, "1", fallback
        return None, None, None

    def _refresh(self) -> None:
        pose, key, pixmap = self._resolve()
        if (pose, key) == self._drawn:
            return
        self._drawn = (pose, key)
        self._sprite = pixmap
        self.update()

    def set_bob(self, offset: float) -> None:
        """Store the current frame's vertical offset.

        Deliberately does not repaint: the HUD requests the update on its own
        ~30 FPS throttle and only while the window is on screen, so a minimized
        window still stops moving.
        """
        self._bob = float(offset)

    # -- geometry -------------------------------------------------------
    def sync_to_parent(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        self.setGeometry(parent.rect())

    # -- sprites --------------------------------------------------------
    def _cycle(self, pose):
        """Frame numbers on disk for `pose`, in numeric order, twins excluded.

        Cached for the life of the widget, so a pose with no directory costs one
        glob rather than one per frame per tick.
        """
        if pose is None:
            return []
        if pose in self._cycles:
            return self._cycles[pose]
        numbers = []
        for path in (self._frames_dir / pose).glob("%s_*.png" % pose):
            parts = path.stem.split("_")
            if parts[-1] == "blink":
                continue
            try:
                numbers.append(int(parts[-1]))
            except ValueError:
                continue
        numbers.sort()
        self._cycles[pose] = numbers
        return numbers

    def _twin(self, pose):
        """Whether this pose ships a blink twin at all."""
        if pose is None:
            return False
        if pose not in self._twins:
            self._twins[pose] = (self._frames_dir / pose /
                                 ("%s_%s.png" % (pose, PET_BLINK_KEY))).is_file()
        return self._twins[pose]

    def _load(self, pose, key):
        """One frame scaled to the pet's height, cached including its misses."""
        if pose is None or key is None:
            return None
        cache_key = (pose, str(key))
        if cache_key in self._cache:
            return self._cache[cache_key]
        path = self._frames_dir / pose / ("%s_%s.png" % (pose, key))
        pixmap = None
        if path.is_file():
            source = QPixmap(str(path))
            if not source.isNull():
                dpr = max(1.0, float(self.devicePixelRatio() or 1.0))
                pixmap = source.scaledToHeight(
                    max(1, int(round(self.HEIGHT * self._scale * dpr))),
                    Qt.TransformationMode.SmoothTransformation)
                pixmap.setDevicePixelRatio(dpr)
        self._cache[cache_key] = pixmap
        return pixmap

    # -- painting -------------------------------------------------------
    def paintEvent(self, event):
        self._paint(QPainter(self))

    def _paint(self, painter):
        """Blit one pet frame. No opaque clear, and never Source mode.

        Same rule the HUD's layers follow: this widget covers the entire
        viewport, so a CompositionMode_Source fill would erase the chat canvas
        underneath it and the window would flush black through the hole.
        """
        if not painter.isActive():
            return
        try:
            painter.setRenderHint(
                QPainter.RenderHint.SmoothPixmapTransform)
            painter.setCompositionMode(
                QPainter.CompositionMode.CompositionMode_SourceOver)
            pixmap = self.sprite
            if pixmap is None or self._fade <= 0.01:
                return
            painter.setOpacity(self._opacity * self._fade)
            rect = self._drawn_rect()
            x, y = rect.x(), rect.y() + self._bob
            if self._pop_scale == 1.0:
                # Steady state: blit the cached pixmap at its native size, the
                # same fast path as before pops existed.
                if self._facing < 0:
                    painter.save()
                    painter.translate(x + rect.width(), y)
                    painter.scale(-1.0, 1.0)
                    painter.drawPixmap(QPointF(0.0, 0.0), pixmap)
                    painter.restore()
                else:
                    painter.drawPixmap(QPointF(x, y), pixmap)
            else:
                target = QRectF(x, y, rect.width(), rect.height())
                source = QRectF(pixmap.rect())
                if self._facing < 0:
                    painter.save()
                    painter.translate(x + rect.width(), y)
                    painter.scale(-1.0, 1.0)
                    painter.drawPixmap(QRectF(0.0, 0.0, rect.width(),
                                              rect.height()), pixmap, source)
                    painter.restore()
                else:
                    painter.drawPixmap(target, pixmap, source)
            if self._drawn[0] == "sleeping":
                self._paint_zzz(painter, x, y, rect.width())
        finally:
            painter.end()

    def _paint_zzz(self, painter, x, y, width):
        """Three Zs drifting up off the sleeping pet's head.

        Drawn rather than shipped: the sleep pose is a single frame, so a glyph
        baked into it would sit still. The Z is three strokes, not text — a font
        lookup depends on the host's families and renders as tofu when the
        family is missing, while a stroked path is identical everywhere. Drawn
        after the sprite, in the same SourceOver / true-alpha mode as everything
        else on this layer, and only while the sleeping pose is on screen.
        """
        size = max(10.0, self.HEIGHT * 0.13)
        glyph = size * 0.62
        stroke = max(1.0, size * 0.10)
        for index, progress in enumerate(
                pet_zzz_progress(self._sleep_elapsed)):
            alpha = math.sin(math.pi * progress)
            if alpha <= 0.02:
                continue
            px = (x + width * 0.72 + math.cos(progress * math.pi) * width * 0.04
                  + index * size * 0.30)
            py = y + size * 0.6 - progress * PET_ZZZ_RISE
            pen = QPen(QColor(150, 236, 255, int(210 * alpha)))
            pen.setWidthF(stroke)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.drawLine(QPointF(px, py - size), QPointF(px + glyph, py - size))
            painter.drawLine(QPointF(px + glyph, py - size), QPointF(px, py))
            painter.drawLine(QPointF(px, py), QPointF(px + glyph, py))

