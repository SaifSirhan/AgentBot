"""ReactorHud and chat-background wiring tests (offscreen Qt).

Run: python -m unittest tests.test_hud_core -v
"""
import math
import os
import random
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np  # noqa: E402
from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication, QSizePolicy, QWidget  # noqa: E402

from audio_bus import AudioBus  # noqa: E402
from hud_core import (  # noqa: E402
    PARTICLE_CAP,
    Particle,
    ParticleField,
    ReactorHud,
    SpikeDetector,
    burst_count,
)

STATES = (
    "IDLE", "LISTENING", "CONTINUOUS", "THINKING", "SPEAKING", "ERROR")


class _CanvasParent(QWidget):
    """A parent that fills a known colour, the way the chat viewport does."""

    def __init__(self, color="#0E1116"):
        super().__init__()
        self.color = QColor(color)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.color)
        painter.end()

    @property
    def floor(self):
        return self.color.red() + self.color.green() + self.color.blue()


class _ExplodingBus:
    """Raises on any read, so a paint path that touched the AudioBus fails."""

    def get_latest(self):
        raise AssertionError("paint path read the AudioBus")

    @property
    def source(self):
        raise AssertionError("paint path read the AudioBus")


class ReactorHudTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.bus = AudioBus()
        self.hud = ReactorHud(bus=self.bus)
        self.hud.show()
        self.app.processEvents()

    def tearDown(self):
        self.hud.hide()
        self.hud.deleteLater()
        self.app.processEvents()

    def test_defaults_to_240_square_and_idle(self):
        self.assertEqual(self.hud.width(), 240)
        self.assertEqual(self.hud.height(), 240)
        self.assertEqual(self.hud.state, "IDLE")

    def test_set_state_accepts_every_state_and_renders(self):
        for state in STATES:
            with self.subTest(state=state):
                self.hud.set_state(state)
                self.app.processEvents()
                self.assertEqual(self.hud.state, state)
                pixmap = self.hud.grab()
                self.assertFalse(pixmap.isNull())
                self.assertEqual((pixmap.width(), pixmap.height()), (240, 240))

    def test_unknown_state_falls_back_to_idle(self):
        self.hud.set_state("NOPE")
        self.assertEqual(self.hud.state, "IDLE")

    def _wait_until(self, predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.01)
        return predicate()

    def test_published_audio_raises_the_voice_level_while_speaking(self):
        self.hud.set_state("SPEAKING")
        self.bus.publish(np.full(1024, 0.5, dtype=np.float32), "tts")
        self.assertTrue(
            self._wait_until(lambda: self.hud.level > 0.05),
            f"level stayed at {self.hud.level}")

    def test_states_differ_by_geometry_and_brightness(self):
        styles = ReactorHud._STYLES
        self.assertEqual(
            len({style.mode for style in styles.values()}), 5)
        self.assertLess(styles["IDLE"].brightness, styles["THINKING"].brightness)
        self.assertEqual(styles["ERROR"].speed, 0.0)
        self.assertEqual(styles["ERROR"].mode, "block")
        self.assertEqual(styles["LISTENING"].mode, "spikes")
        self.assertEqual(styles["THINKING"].mode, "sharp")
        self.assertEqual(styles["SPEAKING"].mode, "pulse")
        self.assertEqual(styles["CONTINUOUS"].mode, "pulse")

    def test_continuous_is_bright_cyan_and_brighter_than_idle(self):
        continuous = ReactorHud._STYLES["CONTINUOUS"]
        self.assertEqual(continuous.brightness, 1.0)
        cyan = continuous.color
        self.assertGreater(cyan.green(), 200)
        self.assertGreater(cyan.blue(), 200)
        self.assertLess(cyan.red(), 120)
        self.assertGreater(
            continuous.brightness, ReactorHud._STYLES["IDLE"].brightness)

    def test_speaking_core_radius_pulses_with_audio_rms(self):
        self.hud.set_state("SPEAKING")
        self.assertTrue(self._wait_until(lambda: self.hud._phase > 0.0))
        quiet = self.hud.core_radius_ratio
        self.bus.publish(np.full(1024, 0.8, dtype=np.float32), "tts")
        self.assertTrue(
            self._wait_until(
                lambda: self.hud.core_radius_ratio > quiet + 0.01),
            f"core stayed at {self.hud.core_radius_ratio}")


class HolographicLayerTests(unittest.TestCase):
    """The glow, scanline, bloom and particle layers added in Phase 2.

    Frames are painted over a known canvas the same way the widget paints them
    onto its parent — the layers are alpha-only, so no scratch buffer is needed.
    """

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.bus = AudioBus()
        self.hud = ReactorHud(bus=self.bus)
        self.hud.show()
        self.app.processEvents()

    def tearDown(self):
        self.hud.hide()
        self.hud.deleteLater()
        self.app.processEvents()

    def render_over(self, color="#808080", size=240):
        """Composite one frame directly onto a flat canvas and return it."""
        canvas = QImage(size, size, QImage.Format.Format_RGB32)
        canvas.fill(QColor(color))
        painter = QPainter(canvas)
        self.hud._paint(painter)
        return canvas

    # -- alpha, not dark --------------------------------------------------
    def test_glow_keeps_the_hue_and_sets_a_true_alpha(self):
        out = ReactorHud._glow(QColor("#00E5FF"), 0.5)
        self.assertEqual((out.red(), out.green(), out.blue()), (0, 229, 255))
        self.assertEqual(out.alpha(), 127)

    def test_glow_at_zero_is_fully_transparent(self):
        self.assertEqual(ReactorHud._glow(QColor("#00E5FF"), 0.0).alpha(), 0)

    def test_the_hud_adds_light_and_never_paints_a_dark_disc(self):
        canvas = self.render_over()
        # Deliberately off both crosshair axes (they run to the widget edge by
        # design) and outside the reactor circle. The old opaque gradient stop
        # darkened everything inside that disc; nothing sampled here may change.
        untouched = [(2, 2), (237, 2), (2, 237), (237, 237),
                     (60, 2), (200, 237)]
        for x, y in untouched:
            with self.subTest(point=(x, y)):
                self.assertEqual(
                    QColor(canvas.pixel(x, y)), QColor("#808080"),
                    "a pixel outside the reactor disc was changed")

    def test_no_state_ever_darkens_the_canvas(self):
        """The HUD is light-only: sampled across the whole frame, no pixel may
        end up darker than the canvas in any state. Both the old opaque gradient
        stop and the RGB-scaled `_blend` strokes broke this — a low-alpha layer
        used to paint near-black over the chat instead of translucent colour."""
        floor = 14 + 17 + 22  # #0E1116, the holographic canvas
        for state in STATES:
            with self.subTest(state=state):
                self.hud.set_state(state)
                canvas = self.render_over(color="#0E1116")
                for y in range(0, 240, 4):
                    for x in range(0, 240, 4):
                        colour = canvas.pixelColor(x, y)
                        self.assertGreaterEqual(
                            colour.red() + colour.green() + colour.blue(),
                            floor,
                            "pixel (%d, %d) was darkened" % (x, y))

    def test_overlay_child_never_erasures_the_canvas_it_sits_on(self):
        """The dark disc was never a gradient: `_paint` opened with a Source-mode
        fill that erased the parent's pixels behind the HUD, so the window
        flushed black through the hole. Checked as a live child, because the
        damage only happens against a parent's backing store."""
        parent = _CanvasParent()
        parent.setFixedSize(240, 240)
        hud = ReactorHud(bus=self.bus, parent=parent)
        hud.setGeometry(0, 0, 240, 240)
        parent.show()
        self.app.processEvents()
        try:
            for state in STATES:
                with self.subTest(state=state):
                    hud.set_state(state)
                    hud.repaint()
                    self.app.processEvents()
                    image = parent.grab().toImage()
                    for y in range(0, 240, 3):
                        for x in range(0, 240, 3):
                            colour = image.pixelColor(x, y)
                            self.assertGreaterEqual(
                                colour.red() + colour.green() + colour.blue(),
                                parent.floor,
                                "the HUD erased the canvas at (%d, %d)" % (x, y))
        finally:
            parent.hide()
            parent.deleteLater()
            self.app.processEvents()

    def test_paint_path_never_reads_the_audio_bus(self):
        # The bus lock belongs to the tick. A paint that read it would couple
        # the repaint rate to the mic callback thread.
        self.hud._bus = _ExplodingBus()
        self.hud.set_state("LISTENING")
        self.render_over()

    # -- scanlines ---------------------------------------------------------
    def test_scanline_chords_stay_inside_the_circle(self):
        cx = cy = 120.0
        radius = 91.0
        for line in ReactorHud.scanline_chords(cx, cy, radius, 0.0, 6.0):
            for point in (line.p1(), line.p2()):
                self.assertLessEqual(
                    math.hypot(point.x() - cx, point.y() - cy), radius + 0.01)

    def test_scanline_chords_are_periodic_and_drift(self):
        base = ReactorHud.scanline_chords(120, 120, 90, 0.0, 6.0)
        shifted = ReactorHud.scanline_chords(120, 120, 90, 3.0, 6.0)
        self.assertGreater(len(base), 10)
        self.assertAlmostEqual(base[0].p1().y() + 3.0,
                               shifted[0].p1().y(), places=6)
        self.assertAlmostEqual(base[1].p1().y() - base[0].p1().y(), 6.0,
                               places=6)

    def test_scanline_geometry_matches_the_spec(self):
        self.assertEqual(ReactorHud.SCAN_WIDTH, 2.0)
        self.assertLess(ReactorHud.SCAN_ALPHA, 0.25)

    # -- bloom ring ---------------------------------------------------------
    def test_bloom_uses_three_passes_fat_dim_to_thin_bright(self):
        self.assertEqual(
            ReactorHud.BLOOM_PASSES, ((8.0, 0.10), (3.0, 0.40), (1.0, 1.00)))

    def test_ring_thickness_grows_with_amplitude_not_with_hue(self):
        quiet = [ReactorHud.bloom_width(w, 0.0)
                 for w, _alpha in ReactorHud.BLOOM_PASSES]
        loud = [ReactorHud.bloom_width(w, 1.0)
                for w, _alpha in ReactorHud.BLOOM_PASSES]
        for before, after in zip(quiet, loud):
            self.assertGreater(after, before)

    def test_bloom_passes_keep_their_alpha_order(self):
        alphas = [alpha for _width, alpha in ReactorHud.BLOOM_PASSES]
        self.assertEqual(alphas, sorted(alphas),
                         "the widest pass must be the dimmest")

    # -- particles ----------------------------------------------------------
    def test_pool_is_capped_structurally(self):
        field = ParticleField()
        self.assertEqual(len(field.items), PARTICLE_CAP)
        for _round in range(50):
            field.spawn(burst_count(0.35), 0.5, 100.0)
        self.assertEqual(len(field), PARTICLE_CAP)

    def test_burst_count_spans_five_to_ten(self):
        self.assertEqual(burst_count(0.0), 5)
        self.assertEqual(burst_count(0.35), 10)
        self.assertEqual(burst_count(9.0), 10)
        self.assertTrue(all(5 <= burst_count(s / 20.0) <= 10
                            for s in range(21)))

    def test_particles_expire(self):
        field = ParticleField(rng=random.Random(7))
        field.spawn(8, 0.6, 100.0)
        self.assertEqual(len(field), 8)
        for _frame in range(120):
            field.advance(0.01)
        self.assertEqual(len(field), 0)

    def test_amplitude_scales_particle_geometry(self):
        quiet = ParticleField(cap=8, rng=random.Random(3))
        loud = ParticleField(cap=8, rng=random.Random(3))
        quiet.spawn(5, 0.0, 100.0)
        loud.spawn(5, 1.0, 100.0)
        for index in range(5):
            self.assertGreater(loud.items[index].size,
                               quiet.items[index].size)
            self.assertGreater(loud.items[index].width,
                               quiet.items[index].width)
            self.assertGreater(loud.items[index].speed,
                               quiet.items[index].speed)

    def test_burst_spreads_around_the_core(self):
        field = ParticleField(cap=20, rng=random.Random(11))
        field.spawn(10, 0.5, 100.0)
        angles = sorted(p.angle for p in field.alive())
        gaps = [b - a for a, b in zip(angles, angles[1:])]
        self.assertGreater(min(gaps), 0.0)
        self.assertLess(max(gaps), 2.0 * (2.0 * math.pi / 10.0))

    def test_particles_are_triangles_or_squares(self):
        field = ParticleField(cap=40, rng=random.Random(5))
        field.spawn(20, 0.7, 100.0)
        self.assertTrue({p.sides for p in field.alive()} <= {3, 4})

    def test_fade_rises_then_falls(self):
        particle = Particle(age=0.0, life=1.0, attack=0.1)
        self.assertAlmostEqual(particle.fade, 0.0, places=6)
        # Half way through the attack, nine tenths of the way through life.
        particle.age = 0.05
        self.assertAlmostEqual(particle.fade, 0.5 * 0.95, places=6)
        particle.age = 1.0
        self.assertAlmostEqual(particle.fade, 0.0, places=6)

    def test_particle_leaves_the_core_and_expands(self):
        field = ParticleField(cap=4, rng=random.Random(2))
        field.spawn(1, 0.8, 100.0)
        particle = field.alive()[0]
        start_radius, start_size = particle.radius, particle.size
        for _frame in range(20):
            field.advance(0.01)
        self.assertGreater(particle.radius, start_radius)
        self.assertGreater(particle.size, start_size)

    # -- onset detection -----------------------------------------------------
    def test_detector_fires_once_on_an_onset(self):
        detector = SpikeDetector()
        self.assertGreater(detector.update(0.5, 0.016), 0.0)
        self.assertEqual(detector.update(0.5, 0.016), 0.0,
                         "the refractory window must swallow the same onset")

    def test_detector_ignores_steady_and_quiet_audio(self):
        detector = SpikeDetector()
        for _step in range(200):
            detector.update(0.30, 0.016)
        self.assertEqual(detector.update(0.30, 0.016), 0.0,
                         "sustained loudness must not keep firing")
        quiet = SpikeDetector()
        for _step in range(50):
            self.assertEqual(quiet.update(0.01, 0.016), 0.0)

    def test_detector_reset_returns_to_the_floor(self):
        detector = SpikeDetector()
        for _step in range(100):
            detector.update(0.4, 0.016)
        detector.reset()
        self.assertEqual(detector.baseline, 0.0)
        self.assertGreater(detector.update(0.4, 0.016), 0.0)

    # -- wiring on the widget -------------------------------------------------
    def test_ring_rotates_once_every_twenty_seconds(self):
        self.assertAlmostEqual(360.0 / ReactorHud.RING_DEG_PER_SEC, 20.0,
                               places=6)
        # A driven clock: _tick clamps dt to 0.1 s, so ten ticks is exactly one
        # second of wall time and one eighth of the ring's revolution.
        clock = [1000.0]
        self.hud._last_tick = clock[0]
        with mock.patch("hud_core.time.monotonic", lambda: clock[0]):
            for _frame in range(10):
                clock[0] += 0.1
                self.hud._tick()
        self.assertAlmostEqual(self.hud.ring_angle, 18.0, places=3)

    def test_a_mic_onset_spawns_particles_through_the_tick(self):
        self.hud.set_state("LISTENING")
        self.bus.publish(np.full(1024, 0.9, dtype=np.float32), "mic")
        for _frame in range(10):
            self.hud._tick()
            self.app.processEvents()
        self.assertGreater(len(self.hud.particles), 0)
        self.assertLessEqual(len(self.hud.particles), PARTICLE_CAP)

    def test_idle_and_thinking_states_do_not_emit_particles(self):
        for state in ("IDLE", "THINKING", "ERROR"):
            with self.subTest(state=state):
                self.hud.particles.clear()
                self.hud.set_state(state)
                self.bus.publish(np.full(1024, 0.9, dtype=np.float32), "mic")
                for _frame in range(10):
                    self.hud._tick()
                    self.app.processEvents()
                self.assertEqual(len(self.hud.particles), 0)

    def test_hud_never_eats_mouse_clicks(self):
        self.assertTrue(self.hud.testAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents))


class CoreCardIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_right_panel_keeps_core_first_without_an_inline_hud(self):
        import agent_gui_qt as gui

        panel = gui.RightPanel()
        try:
            self.assertEqual(panel.CARD_TITLES[0], "CORE")
            self.assertIn("CORE", panel.cards)
            self.assertFalse(panel.cards["CORE"].findChildren(ReactorHud))
            self.assertGreaterEqual(panel.minimumWidth(), 250)
            for card in panel.cards.values():
                self.assertEqual(
                    card.sizePolicy().horizontalPolicy(),
                    QSizePolicy.Policy.Ignored)
        finally:
            panel.deleteLater()
            self.app.processEvents()

    def test_chat_has_a_centered_large_translucent_background_hud(self):
        import agent_gui_qt as gui

        window = gui.AgentWindow()
        try:
            hud = window.chat.background_hud
            self.assertIsInstance(hud, ReactorHud)
            self.assertEqual((hud.width(), hud.height()), (800, 800))
            self.assertIsNone(hud.graphicsEffect())
            self.assertAlmostEqual(hud.opacity, 0.15, places=3)
            self.assertGreaterEqual(
                window.right_panel.minimumWidth(), 250)
        finally:
            window.close()
            window.deleteLater()
            self.app.processEvents()

    def test_agent_status_maps_to_hud_states(self):
        import agent_gui_qt as gui

        window = gui.AgentWindow()
        try:
            hud = window.chat.background_hud

            window.set_status("Thinking…", busy=True)
            self.assertEqual(hud.state, "THINKING")

            window._recording = True
            window.set_status("Listening…", busy=True)
            self.assertEqual(hud.state, "LISTENING")

            window._continuous_listening = True
            window.set_status("Listening (Continuous)…", busy=True)
            self.assertEqual(hud.state, "CONTINUOUS")
            self.assertEqual(
                window.windowTitle(), "Agent — Listening (Continuous)…")

            window._on_voice_event("continuous:idle:")
            self.assertEqual(
                window.windowTitle(),
                "Agent — Mic sleeping. Say 'Hey Jarvis' or press F9.")
            self.assertEqual(hud.state, "IDLE")

            window._recording = False
            window._continuous_listening = False
            with mock.patch.object(gui, "_tts_is_speaking", return_value=True):
                window.set_status("Ready", busy=False)
                self.assertEqual(hud.state, "SPEAKING")

            with mock.patch.object(gui, "_tts_is_speaking", return_value=False):
                window.set_status("Ready", busy=False)
                self.assertEqual(hud.state, "IDLE")

            window._hud_error_until = time.monotonic() + 5.0
            window.set_status("Ready", busy=False)
            self.assertEqual(hud.state, "ERROR")
        finally:
            window.close()
            window.deleteLater()
            self.app.processEvents()

    def test_continuous_off_clears_the_hud_and_title(self):
        import agent_gui_qt as gui

        window = gui.AgentWindow()
        try:
            hud = window.chat.background_hud
            window._recording = True
            window._continuous_listening = True
            window.set_status("Listening (Continuous)…", busy=True)
            self.assertEqual(hud.state, "CONTINUOUS")

            with mock.patch.object(gui, "get_voice") as get_voice:
                window._cancel_continuous_listening()

            get_voice.return_value.cancel_recording.assert_called_once()
            self.assertFalse(window._recording)
            self.assertFalse(window._continuous_listening)
            self.assertEqual(window.windowTitle(), "Agent — Ready")
            self.assertEqual(hud.state, "IDLE")
        finally:
            window.close()
            window.deleteLater()
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
