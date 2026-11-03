"""ReactorHud and chat-background wiring tests (offscreen Qt).

Run: python -m unittest tests.test_hud_core -v
"""
import math
import os
import random
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np  # noqa: E402
from PySide6.QtCore import QPoint, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication, QSizePolicy, QWidget  # noqa: E402

from audio_bus import AudioBus  # noqa: E402
from hud_core import (  # noqa: E402
    PARTICLE_CAP,
    PET_ASSET_DIR,
    PET_BLINK_DURATION,
    PET_BLINK_GAP,
    PET_BOB_AMPLITUDE,
    PET_MARGIN,
    PET_STATES,
    PET_WANDER_AFTER,
    PET_WANDER_DURATION,
    Particle,
    ParticleField,
    PetWidget,
    ReactorHud,
    SpikeDetector,
    burst_count,
    pet_bob,
)

STATES = (
    "IDLE", "LISTENING", "CONTINUOUS", "THINKING", "SPEAKING", "ERROR")
# Every sprite committed under assets/pet/. The five the GUI can ask for plus
# blinking and walking, which the pet layers over its own idle time.
SHIPPED_PET_ART = ("idle", "thinking", "sleeping", "speaking", "reading",
                   "blinking", "walking")
HAS_PET_ART = all(
    (PET_ASSET_DIR / ("%s.png" % name)).is_file() for name in SHIPPED_PET_ART)


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


def _write_sprite(asset_dir, name):
    """One solid opaque PNG at a known size, so a test can see exactly where it
    landed. Returns the path."""
    pixmap = QPixmap(100, 100)
    pixmap.fill(QColor("#25F2FF"))
    path = Path(asset_dir) / ("%s.png" % name)
    pixmap.save(str(path), "PNG")
    return path


def _make_sprite_dir(root, *names):
    asset_dir = Path(root)
    for name in names:
        _write_sprite(asset_dir, name)
    return asset_dir


class PetSpriteTests(unittest.TestCase):
    """State selection and the missing-file fallback, against synthetic art."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.asset_dir = _make_sprite_dir(
            self._tmp.name, "idle", "thinking")
        self.pet = PetWidget(parent=None, asset_dir=self.asset_dir)
        self.pet.resize(400, 400)

    def tearDown(self):
        self.pet.deleteLater()
        self._tmp.cleanup()
        self.app.processEvents()

    def test_the_pet_state_set_matches_the_committed_sprites(self):
        self.assertEqual(PET_STATES, SHIPPED_PET_ART)

    def test_a_named_state_loads_its_sprite(self):
        self.pet.set_state("thinking")
        self.assertEqual(self.pet.pet_state, "thinking")
        self.assertIsNotNone(self.pet.sprite)

    def test_state_names_are_case_insensitive(self):
        self.pet.set_state("  THINKING ")
        self.assertEqual(self.pet.pet_state, "thinking")

    def test_unknown_state_falls_back_to_idle(self):
        self.pet.set_state("dancing")
        self.assertEqual(self.pet.pet_state, "idle")

    def test_a_missing_sprite_falls_back_to_idle(self):
        # sleeping.png was never written to this fixture directory.
        self.pet.set_state("sleeping")
        self.assertEqual(self.pet.pet_state, "idle")
        self.assertEqual(self.pet.requested_state, "sleeping")

    def test_a_new_sprite_is_picked_up_without_a_code_change(self):
        """The promise behind the fallback: a state name already in PET_STATES
        draws idle while its file is absent and resolves to it on its own once
        the file lands. Only a new PetWidget instance is needed, because the
        missing-file result is cached for the life of the widget."""
        self.pet.set_state("speaking")
        self.assertEqual(self.pet.pet_state, "idle")
        self.assertEqual(self.pet.requested_state, "speaking")

        _write_sprite(self.asset_dir, "speaking")
        fresh = PetWidget(parent=None, asset_dir=self.asset_dir)
        try:
            fresh.set_state("speaking")
            self.assertEqual(fresh.pet_state, "speaking")
            self.assertIsNotNone(fresh.sprite)
        finally:
            fresh.deleteLater()

    def test_no_art_at_all_draws_nothing(self):
        empty = _make_sprite_dir(tempfile.mkdtemp())
        pet = PetWidget(parent=None, asset_dir=empty)
        pet.resize(200, 200)
        pet.set_state("thinking")
        self.assertEqual(pet.pet_state, "")
        self.assertIsNone(pet.sprite)
        pet.deleteLater()

    def test_sprites_are_scaled_to_the_pet_height(self):
        sprite = self.pet.sprite
        dpr = sprite.devicePixelRatio() or 1.0
        self.assertAlmostEqual(sprite.height() / dpr, self.pet.HEIGHT,
                               places=3)

    @unittest.skipUnless(HAS_PET_ART, "assets/pet/*.png not present")
    def test_the_shipped_art_loads_for_every_committed_sprite(self):
        pet = PetWidget(parent=None, asset_dir=PET_ASSET_DIR)
        try:
            for state in SHIPPED_PET_ART:
                with self.subTest(state=state):
                    pet.set_state(state)
                    self.assertEqual(pet.pet_state, state)
                    self.assertIsNotNone(pet.sprite)
        finally:
            pet.deleteLater()


class PetPaintTests(unittest.TestCase):
    """Where the pet lands, and that it only ever blends."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.asset_dir = _make_sprite_dir(self._tmp.name, "idle")
        self.pet = PetWidget(parent=None, asset_dir=self.asset_dir)
        self.pet.resize(400, 400)
        self.pet.set_state("idle")

    def tearDown(self):
        self.pet.deleteLater()
        self._tmp.cleanup()
        self.app.processEvents()

    def render_over(self, color="#808080", size=(400, 400)):
        canvas = QImage(size[0], size[1], QImage.Format.Format_RGB32)
        canvas.fill(QColor(color))
        painter = QPainter(canvas)
        self.pet._paint(painter)
        return canvas

    def painted_bounds(self, canvas, floor):
        xs, ys = [], []
        for y in range(canvas.height()):
            for x in range(canvas.width()):
                if canvas.pixelColor(x, y) != floor:
                    xs.append(x)
                    ys.append(y)
        if not xs:
            return None
        return (min(xs), min(ys), max(xs), max(ys))

    def test_the_pet_sits_bottom_right_and_keeps_its_margin(self):
        sprite = self.pet.sprite
        dpr = sprite.devicePixelRatio() or 1.0
        width = sprite.width() / dpr
        height = sprite.height() / dpr
        expected_x = 400 - width - PET_MARGIN
        expected_y = 400 - height - PET_MARGIN
        floor = QColor("#808080")
        bounds = self.painted_bounds(self.render_over(), floor)
        self.assertIsNotNone(bounds, "the pet painted nothing")
        self.assertEqual(
            bounds, (int(expected_x), int(expected_y),
                     int(expected_x + width) - 1,
                     int(expected_y + height) - 1))

    def test_the_pet_never_reaches_the_corner_it_sits_in(self):
        # The margin is what keeps the sprite off the window edge; the bob can
        # only lift it, so the bottom-right pixel must always stay canvas.
        floor = QColor("#808080")
        for bob in (-5.0, 0.0, 5.0):
            with self.subTest(bob=bob):
                self.pet.set_bob(bob)
                canvas = self.render_over()
                self.assertEqual(canvas.pixelColor(399, 399), floor)

    def test_the_pet_is_transparent_for_mouse_events(self):
        self.assertTrue(self.pet.testAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents))

    def test_the_pet_adds_light_and_never_erasures_the_canvas(self):
        """The Phase 2 rule, applied to the new layer. A CompositionMode_Source
        fill here would wipe the chat viewport, which the pet covers entirely."""
        parent = _CanvasParent()
        parent.setFixedSize(400, 400)
        pet = PetWidget(parent=parent, asset_dir=self.asset_dir)
        pet.setGeometry(parent.rect())
        pet.set_state("idle")
        parent.show()
        self.app.processEvents()
        try:
            for bob in (-5.0, 0.0, 5.0):
                with self.subTest(bob=bob):
                    pet.set_bob(bob)
                    pet.repaint()
                    self.app.processEvents()
                    image = parent.grab().toImage()
                    for y in range(0, 400, 3):
                        for x in range(0, 400, 3):
                            colour = image.pixelColor(x, y)
                            self.assertGreaterEqual(
                                colour.red() + colour.green() + colour.blue(),
                                parent.floor,
                                "the pet erased the canvas at (%d, %d)"
                                % (x, y))
        finally:
            parent.hide()
            parent.deleteLater()
            self.app.processEvents()

    @unittest.skipUnless(HAS_PET_ART, "assets/pet/*.png not present")
    def test_the_shipped_art_does_not_darken_the_holographic_canvas(self):
        """The reactor layers are light-only. The falcon is not a glow, so this
        is measured rather than assumed: every sprite is sampled over #0E1116
        and no composited pixel may fall below that floor."""
        pet = PetWidget(parent=None, asset_dir=PET_ASSET_DIR)
        pet.resize(400, 400)
        floor = 0x0E + 0x11 + 0x16
        try:
            for state in SHIPPED_PET_ART:
                with self.subTest(state=state):
                    pet.set_state(state)
                    canvas = QImage(400, 400, QImage.Format.Format_RGB32)
                    canvas.fill(QColor("#0E1116"))
                    pet._paint(QPainter(canvas))
                    for y in range(0, 400, 3):
                        for x in range(0, 400, 3):
                            colour = canvas.pixelColor(x, y)
                            self.assertGreaterEqual(
                                colour.red() + colour.green() + colour.blue(),
                                floor,
                                "pixel (%d, %d) was darkened" % (x, y))
        finally:
            pet.deleteLater()


class PetAnimationTests(unittest.TestCase):
    """The breathing bob rides the HUD's existing tick — no second timer."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.asset_dir = _make_sprite_dir(self._tmp.name, *PET_STATES)
        self.bus = AudioBus()
        self.hud = ReactorHud(bus=self.bus)
        self.pet = PetWidget(parent=self.hud, asset_dir=self.asset_dir)
        self.pet.resize(400, 400)
        self.hud.set_pet_widget(self.pet)
        self.hud.show()
        self.app.processEvents()

    def tearDown(self):
        self.hud.hide()
        self.hud.deleteLater()
        self._tmp.cleanup()
        self.app.processEvents()

    def test_bob_peaks_at_five_pixels_on_a_three_second_cycle(self):
        self.assertAlmostEqual(pet_bob(0.0), 0.0)
        # sin(t * 2) peaks at t = pi/4 and troughs at 3*pi/4, so one full bob
        # cycle is pi seconds — a ~3.1 s rise and fall.
        self.assertAlmostEqual(pet_bob(math.pi / 4), PET_BOB_AMPLITUDE)
        self.assertAlmostEqual(pet_bob(3.0 * math.pi / 4),
                               -PET_BOB_AMPLITUDE)
        self.assertAlmostEqual(pet_bob(math.pi), 0.0, places=9)
        self.assertLessEqual(max(abs(pet_bob(t / 100.0))
                                 for t in range(629)), PET_BOB_AMPLITUDE)

    def test_bob_is_pushed_to_the_pet_by_the_huds_tick(self):
        clock = [1000.0]
        self.hud._last_tick = clock[0]
        with mock.patch("hud_core.time.monotonic", lambda: clock[0]):
            for _frame in range(10):
                clock[0] += 0.1
                self.hud._tick()
        self.assertAlmostEqual(self.pet.bob, pet_bob(1.0), places=9)
        self.assertAlmostEqual(self.hud.bob, math.sin(2.0) * 5.0, places=9)

    def test_the_pet_adds_no_second_timer(self):
        self.assertEqual(len(self.hud.findChildren(QTimer)), 1)
        self.assertEqual(self.pet.findChildren(QTimer), [])

    def test_set_bob_does_not_repaint_on_its_own(self):
        # Repaint stays on the HUD's ~30 FPS throttle so a minimized window
        # still stops moving; set_bob only stores the number.
        with mock.patch.object(self.pet, "update") as update:
            self.pet.set_bob(3.0)
        update.assert_not_called()
        self.assertEqual(self.pet.bob, 3.0)

    def test_a_state_set_before_the_pet_is_attached_survives_attach(self):
        hud = ReactorHud(bus=self.bus)
        pet = PetWidget(parent=hud, asset_dir=self.asset_dir)
        try:
            hud.set_pet_state("thinking")
            self.assertEqual(hud.pet_state, "thinking")
            hud.set_pet_widget(pet)
            self.assertEqual(pet.pet_state, "thinking")
        finally:
            pet.deleteLater()
            hud.deleteLater()

    def test_an_unknown_pet_state_lands_on_idle_through_the_hud(self):
        self.hud.set_pet_state("dancing")
        self.assertEqual(self.pet.pet_state, "idle")
        self.hud.set_pet_state("speaking")
        self.assertEqual(self.pet.pet_state, "speaking")
        self.hud.set_pet_state("sleeping")
        self.assertEqual(self.pet.pet_state, "sleeping")


class PetBehaviourTests(unittest.TestCase):
    """Blink and wander: idle-only habits driven off the HUD's tick seconds.

    A fixed rng and an explicit `advance(dt)` keep these deterministic — no
    wall clock and no real timer is involved.
    """

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.asset_dir = _make_sprite_dir(self._tmp.name, *PET_STATES)
        self.pet = PetWidget(parent=None, asset_dir=self.asset_dir,
                             rng=random.Random(11))
        self.pet.resize(400, 400)

    def tearDown(self):
        self.pet.deleteLater()
        self._tmp.cleanup()
        self.app.processEvents()

    def _advance(self, pet, seconds, step=0.1):
        for _tick in range(int(round(seconds / step))):
            pet.advance(step)

    def test_blink_lights_for_its_duration_then_returns(self):
        self.assertEqual(self.pet.pet_state, "idle")
        self._advance(self.pet, self.pet._next_blink - 0.2)
        self.assertEqual(self.pet.pet_state, "idle", "blinked early")
        self._advance(self.pet, 0.3)
        self.assertEqual(self.pet.pet_state, "blinking")
        self.assertEqual(self.pet.override, "blinking")
        self._advance(self.pet, PET_BLINK_DURATION)
        self.assertEqual(self.pet.pet_state, "idle")
        self.assertIsNone(self.pet.override)

    def test_the_first_blink_is_due_within_the_declared_gap(self):
        low, high = PET_BLINK_GAP
        for seed in range(12):
            pet = PetWidget(parent=None, asset_dir=self.asset_dir,
                            rng=random.Random(seed))
            with self.subTest(seed=seed):
                self.assertTrue(low <= pet._next_blink <= high,
                                "seed %d scheduled a blink at %.2f s"
                                % (seed, pet._next_blink))
            pet.deleteLater()
        self.app.processEvents()

    def test_blinks_only_fire_while_the_pet_is_idle(self):
        for state in ("reading", "speaking", "sleeping", "thinking"):
            with self.subTest(state=state):
                pet = PetWidget(parent=None, asset_dir=self.asset_dir,
                                rng=random.Random(5))
                pet.set_state(state)
                self._advance(pet, 30.0)
                self.assertEqual(pet.pet_state, state)
                self.assertIsNone(pet.override)
                self.assertEqual(pet.idle_time, 0.0,
                                 "idle clock ran while not idle")
                pet.deleteLater()

    def test_a_state_change_drops_an_in_flight_blink(self):
        self._advance(self.pet, self.pet._next_blink + 0.1)
        self.assertEqual(self.pet.pet_state, "blinking")
        self.pet.set_state("speaking")
        self.assertEqual(self.pet.pet_state, "speaking")
        self.assertIsNone(self.pet.override)

    def test_repeated_identical_requests_do_not_starve_the_blink(self):
        """A chatty status line calls set_pet_state over and over; the early
        return on an unchanged name must not keep re-arming the blink."""
        quiet = PetWidget(parent=None, asset_dir=self.asset_dir,
                          rng=random.Random(11))
        noisy = PetWidget(parent=None, asset_dir=self.asset_dir,
                          rng=random.Random(11))
        try:
            for _tick in range(300):                     # 30 s of idle
                for _repeat in range(25):
                    noisy.set_state("idle")
                quiet.advance(0.1)
                noisy.advance(0.1)
            self.assertEqual(noisy.idle_time, quiet.idle_time)
            self.assertAlmostEqual(quiet._next_blink, noisy._next_blink,
                                   places=9)
        finally:
            quiet.deleteLater()
            noisy.deleteLater()

    def test_the_pet_wanders_after_a_long_idle_and_returns(self):
        self._advance(self.pet, PET_WANDER_AFTER + 0.1)
        self.assertEqual(self.pet.override, "walking")
        self.assertEqual(self.pet.pet_state, "walking")
        # A margin, not the exact duration: 0.1 s added a step at a time sums
        # to just under 12.0, and the wander ends on the tick that clears it.
        self._advance(self.pet, PET_WANDER_DURATION + 0.5)
        self.assertEqual(self.pet.pet_state, "idle")
        self.assertIsNone(self.pet.override)

    def test_walking_does_not_restart_on_the_tick_after_it_ends(self):
        self._advance(self.pet, PET_WANDER_AFTER + PET_WANDER_DURATION + 0.2)
        self.assertEqual(self.pet.pet_state, "idle")
        self.assertLess(self.pet.idle_time, 1.0,
                        "the idle clock was not reset after the wander")
        self._advance(self.pet, 2.0)
        self.assertNotEqual(self.pet.pet_state, "walking")

    def test_the_hud_tick_is_what_drives_the_idle_behaviours(self):
        """advance() is called from ReactorHud._tick, not from a timer of the
        pet's own, so prove the wiring rather than only the method.

        The blink is sampled across the whole run, not at the final instant:
        it only lasts ~0.18 s, so a single end-of-loop read can land after it
        has already returned to idle.
        """
        bus = AudioBus()
        hud = ReactorHud(bus=bus)
        pet = PetWidget(parent=hud, asset_dir=self.asset_dir,
                        rng=random.Random(11))
        pet.resize(400, 400)
        hud.set_pet_widget(pet)
        try:
            clock = [1000.0]
            hud._last_tick = clock[0]
            seen = set()
            with mock.patch("hud_core.time.monotonic", lambda: clock[0]):
                for _tick in range(100):        # 10 s of driven idle
                    clock[0] += 0.1
                    hud._tick()
                    seen.add(pet.pet_state)
            self.assertIn("blinking", seen,
                          "the HUD tick never blinked the pet, saw %s" % seen)
            self.assertIn("idle", seen)
        finally:
            pet.deleteLater()
            hud.deleteLater()


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

    def test_pet_is_attached_to_the_chat_viewport_and_to_the_hud(self):
        import agent_gui_qt as gui

        window = gui.AgentWindow()
        try:
            pet = window.chat.pet
            hud = window.chat.background_hud
            self.assertIsInstance(pet, PetWidget)
            self.assertIs(hud.pet, pet)
            self.assertIs(pet.parentWidget(), window.chat.viewport())
            self.assertEqual(pet.geometry(), window.chat.viewport().rect())
            self.assertTrue(pet.testAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents))
        finally:
            window.close()
            window.deleteLater()
            self.app.processEvents()

    def test_the_pet_tracks_the_agent_status_through_one_hook(self):
        """Thinking while busy, speaking over TTS, asleep when the mic times
        out, awake again when the turn ends — all from _sync_hud_state, which
        set_status already calls, so no per-call-site pet code is needed."""
        import agent_gui_qt as gui

        window = gui.AgentWindow()
        try:
            pet = window.chat.pet
            hud = window.chat.background_hud
            window._recording = False
            window._continuous_listening = False
            window._live_steps = []
            with mock.patch.object(gui, "_tts_is_speaking", return_value=False):
                window.set_status("Ready", busy=False)
                self.assertEqual(pet.pet_state, "idle")

                window.set_status("Thinking…", busy=True)
                self.assertEqual(pet.pet_state, "thinking")
                self.assertEqual(hud.state, "THINKING")

                # With no tool reported yet, every busy status is the laptop.
                for status in ("Working…", "Browsing…", "Transcribing…",
                               "Verifying…"):
                    with self.subTest(status=status):
                        window.set_status(status, busy=True)
                        self.assertEqual(pet.pet_state, "thinking")

                window.set_status(
                    "Mic sleeping. Say 'Hey Jarvis' or press F9.", busy=False)
                self.assertEqual(pet.pet_state, "sleeping")
                self.assertEqual(hud.state, "IDLE")

                window._processing = True
                window.set_status("Thinking…", busy=True)
                self.assertEqual(pet.pet_state, "thinking")
                window._end_turn()
                self.assertEqual(pet.pet_state, "idle")
                self.assertTrue(window.input_field.isEnabled())

                window.set_status("Ready", busy=False)
                with mock.patch.object(gui, "_tts_is_speaking",
                                       return_value=True):
                    window.set_status("Speaking…", busy=False)
                    self.assertEqual(pet.requested_state, "speaking")
                    self.assertEqual(pet.pet_state, "speaking")
                    self.assertEqual(hud.state, "SPEAKING")

                # Listening and error have no sprite, so the pet stays plainly
                # awake while the reactor shows them.
                window._recording = True
                window.set_status("Listening…", busy=True)
                self.assertEqual(pet.pet_state, "idle")
                self.assertEqual(hud.state, "LISTENING")
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


class PetToolPoseTests(unittest.TestCase):
    """Busy is the laptop; the book only while a read tool is what the agent
    last reported. Steps arrive after execution, so the pose tracks the newest
    reported tool rather than one that is in flight.

    One window for the class: building an AgentWindow is the expensive part of
    these tests, and every piece of state they touch is reset per test below.
    """

    @classmethod
    def setUpClass(cls):
        import agent_gui_qt as gui

        cls.app = QApplication.instance() or QApplication([])
        cls.gui = gui
        cls.window = gui.AgentWindow()

    @classmethod
    def tearDownClass(cls):
        cls.window.close()
        cls.window.deleteLater()
        cls.app.processEvents()

    def setUp(self):
        gui = self.gui
        window = self.window
        self.pet = window.chat.pet
        window._recording = False
        window._continuous_listening = False
        window._hud_error_until = 0.0
        window._live_steps = []
        self._tts = mock.patch.object(gui, "_tts_is_speaking",
                                      return_value=False)
        self._tts.start()
        window._processing = True
        window.set_status("Thinking…", busy=True)

    def tearDown(self):
        self._tts.stop()
        self.window._processing = False
        self.app.processEvents()

    def _report(self, tool, result="ok"):
        self.window._on_worker_step(
            "Action: %s(some_input) -> Result: %s" % (tool, result))

    def test_the_pose_is_the_laptop_before_any_tool_reports(self):
        self.assertEqual(self.pet.pet_state, "thinking")

    def test_each_read_tool_swaps_the_pose_to_the_book(self):
        for tool in ("read_file", "rag_search", "read_and_summarize"):
            with self.subTest(tool=tool):
                self.window._live_steps = []
                self._report(tool)
                self.assertEqual(self.pet.pet_state, "reading")

    def test_other_tools_keep_the_laptop(self):
        for tool in ("search_web", "write_file", "patch_file", "open_app",
                     "system_status", "chat", "find_files", "grep_group_chat"):
            with self.subTest(tool=tool):
                self.window._live_steps = []
                self._report(tool)
                self.assertEqual(self.pet.pet_state, "thinking")

    def test_the_book_is_shown_for_a_failed_read_too(self):
        """The step is appended after execution either way, and the agent is
        still dealing with the read when the error comes back."""
        self.window._on_worker_step(
            "Action: read_file(nope.txt) -> ERROR: no such file")
        self.assertEqual(self.pet.pet_state, "reading")

    def test_the_newest_step_wins(self):
        self._report("read_file")
        self.assertEqual(self.pet.pet_state, "reading")
        self._report("search_web")
        self.assertEqual(self.pet.pet_state, "thinking")
        self._report("rag_search")
        self.assertEqual(self.pet.pet_state, "reading")

    def test_non_tool_lines_leave_the_pose_alone(self):
        self._report("read_file")
        for line in ("System: Repeated action - stopped.",
                    "Done: the answer",
                    "AI: thinking out loud"):
            with self.subTest(line=line):
                self.window._on_worker_step(line)
                self.assertEqual(self.pet.pet_state, "reading",
                                 "a non-tool line reset the pose")

    def test_a_step_moves_the_pet_without_any_status_change(self):
        """set_status does not fire mid-turn, so _on_worker_step has to re-sync
        the HUD itself or the book would never appear during a turn."""
        self.assertEqual(self.pet.pet_state, "thinking")
        with mock.patch.object(self.window, "set_status") as set_status:
            self._report("read_and_summarize")
        self.assertEqual(self.pet.pet_state, "reading")
        set_status.assert_not_called()

    def test_the_book_does_not_leak_into_the_next_turn(self):
        self._report("read_file")
        self.assertEqual(self.pet.pet_state, "reading")
        # Exactly what a new send does.
        self.window._live_steps = []
        self.window._processing = True
        self.window.set_status("Thinking…", busy=True)
        self.assertEqual(self.pet.pet_state, "thinking")

    def test_the_book_never_shows_when_the_agent_is_not_busy(self):
        self._report("read_file")
        self.assertEqual(self.pet.pet_state, "reading")
        self.window._processing = False
        self.window.set_status("Ready", busy=False)
        self.assertEqual(self.pet.pet_state, "idle")


if __name__ == "__main__":
    unittest.main()
