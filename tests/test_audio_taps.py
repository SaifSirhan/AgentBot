"""Verify mic and Kokoro playback publish into the AudioBus.

Run: python -m unittest tests.test_audio_taps -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np  # noqa: E402

import voice  # noqa: E402
import voice_output  # noqa: E402
from audio_bus import AudioBus  # noqa: E402


class _FakeStream:
    def __init__(self):
        self.written = []

    def start(self):
        pass

    def stop(self):
        pass

    def close(self):
        pass

    def write(self, data):
        self.written.append(np.asarray(data))


class KokoroTapTests(unittest.TestCase):
    def test_tts_chunk_is_published_before_playback(self):
        bus = AudioBus()
        stream = _FakeStream()
        chunk = (None, None, np.array([0.25, -0.25], dtype=np.float32))

        def pipeline(piece, voice=None, speed=None):
            return iter([chunk])

        with mock.patch.object(voice_output, "audio_bus", bus), \
                mock.patch.object(voice_output, "_load_backend_status", return_value=(True, "")), \
                mock.patch.object(voice_output, "_load_tts_config", return_value=("af_heart", "1.0", "a", True)), \
                mock.patch.object(voice_output, "_load_max_chars", return_value=10000), \
                mock.patch.object(voice_output, "_clean_for_speech", side_effect=lambda text: text), \
                mock.patch.object(voice_output, "_split_for_tts", return_value=["hello"]), \
                mock.patch.object(voice_output, "_get_pipeline", return_value=pipeline), \
                mock.patch.object(voice_output, "sd") as fake_sd:
            fake_sd.OutputStream.return_value = stream
            voice_output._speak_blocking("hello")

        samples, rms = bus.get_latest()
        self.assertEqual(samples.size, 2)
        np.testing.assert_allclose(samples, [0.25, -0.25])
        self.assertAlmostEqual(rms, 0.25, places=4)
        self.assertEqual(bus.source, "tts")
        self.assertEqual(len(stream.written), 1)


class MicrophoneTapTests(unittest.TestCase):
    def test_microphone_callback_publishes_normalized_samples(self):
        bus = AudioBus()
        captured = {}

        def fake_input_stream(**kwargs):
            captured["callback"] = kwargs["callback"]
            return _FakeStream()

        with mock.patch.object(voice, "audio_bus", bus), \
                mock.patch.object(voice.sd, "InputStream", side_effect=fake_input_stream):
            voice.start_recording()
            try:
                callback = captured["callback"]
                callback(
                    np.full((256, 1), 8192, dtype=np.int16),
                    256,
                    None,
                    None,
                )
            finally:
                voice.cancel_recording()

        samples, rms = bus.get_latest()
        self.assertEqual(samples.size, 256)
        self.assertAlmostEqual(float(samples[-1]), 0.25, places=4)
        self.assertAlmostEqual(rms, 0.25, places=4)
        self.assertEqual(bus.source, "mic")


if __name__ == "__main__":
    unittest.main()
