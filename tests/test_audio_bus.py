"""AudioBus ring-buffer tests.

Run: python -m unittest tests.test_audio_bus -v
"""
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np  # noqa: E402

from audio_bus import AudioBus  # noqa: E402


class AudioBusTests(unittest.TestCase):
    def test_empty_bus_returns_no_samples_and_zero_rms(self):
        bus = AudioBus()
        samples, rms = bus.get_latest()
        self.assertEqual(samples.size, 0)
        self.assertEqual(rms, 0.0)

    def test_publish_keeps_latest_1024_samples(self):
        bus = AudioBus()
        data = np.linspace(-0.5, 0.5, 2048, dtype=np.float32)
        bus.publish(data, "mic")
        samples, rms = bus.get_latest()
        self.assertEqual(samples.size, 1024)
        np.testing.assert_allclose(samples, data[-1024:])
        expected = float(np.sqrt(np.mean(np.square(data[-1024:]))))
        self.assertAlmostEqual(rms, expected, places=6)

    def test_publish_normalizes_int16_microphone_audio(self):
        bus = AudioBus()
        bus.publish(np.full(1024, 16384, dtype=np.int16), "mic")
        samples, rms = bus.get_latest()
        self.assertTrue(np.all(samples >= -1.0))
        self.assertTrue(np.all(samples <= 1.0))
        self.assertAlmostEqual(float(samples[-1]), 0.5, places=4)
        self.assertAlmostEqual(rms, 0.5, places=4)

    def test_publish_clips_out_of_range_float_audio(self):
        bus = AudioBus()
        bus.publish(np.array([2.0, -2.0], dtype=np.float32), "tts")
        samples, rms = bus.get_latest()
        self.assertTrue(np.all(samples >= -1.0))
        self.assertTrue(np.all(samples <= 1.0))
        self.assertAlmostEqual(rms, 1.0, places=4)

    def test_publish_records_source(self):
        bus = AudioBus()
        bus.publish(np.zeros(32, dtype=np.float32), "tts")
        self.assertEqual(bus.source, "tts")

    def test_concurrent_publishes_are_bounded_and_finite(self):
        bus = AudioBus()
        errors = []

        def worker(offset):
            try:
                for index in range(200):
                    value = (index + offset) / 1000.0
                    bus.publish(np.full(256, value, dtype=np.float32), "mic")
            except Exception as exc:  # pragma: no cover - diagnostic path
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(off,)) for off in (0, 1)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertEqual(errors, [])
        samples, rms = bus.get_latest()
        self.assertLessEqual(samples.size, 1024)
        self.assertTrue(np.all(np.isfinite(samples)))
        self.assertGreaterEqual(rms, 0.0)
        self.assertLessEqual(rms, 1.0)


if __name__ == "__main__":
    unittest.main()
