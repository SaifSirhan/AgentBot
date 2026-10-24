"""Tests for wake_word.py using fake PyAudio/openWakeWord objects.

No microphone is opened and no real model is loaded.

Run: python -m pytest tests/test_wake_word.py -v
Or:  python tests/test_wake_word.py
"""
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

import wake_word


class FakeStream:
    def __init__(self):
        self.closed = False
        self.stopped = False

    def read(self, frames, exception_on_overflow=False):
        time.sleep(0.005)
        return b"\x00\x00" * frames

    def stop_stream(self):
        self.stopped = True

    def close(self):
        self.closed = True


class FakeAudio:
    def __init__(self):
        self.streams = []
        self.open_kwargs = []
        self.terminated = False

    def open(self, **kwargs):
        stream = FakeStream()
        self.streams.append(stream)
        self.open_kwargs.append(kwargs)
        return stream

    def terminate(self):
        self.terminated = True


class FakePyAudioModule:
    paInt16 = 8

    def __init__(self, audio):
        self.audio = audio

    def PyAudio(self):
        return self.audio


def make_model_cls(score_holder, models):
    class FakeModel:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.resets = 0
            models.append(self)

        def predict(self, frame):
            return {"agent": score_holder["value"]}

        def reset(self):
            self.resets += 1

    return FakeModel


class WakeWordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wake_word_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.model_file = self.tmp / "agent.onnx"
        self.model_file.write_bytes(b"fake-model")

        self.score = {"value": 0.0}
        self.audio = FakeAudio()
        self.models = []
        model_cls = make_model_cls(self.score, self.models)
        patcher = mock.patch.object(
            wake_word, "_load_dependencies",
            lambda: (FakePyAudioModule(self.audio), np, model_cls),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_service(self, on_detect, **kwargs):
        kwargs.setdefault("threshold", 0.5)
        kwargs.setdefault("cooldown", 0.0)
        return wake_word.WakeWordService(
            on_detect=on_detect,
            model=str(self.model_file),
            **kwargs,
        )

    def wait_for(self, predicate, timeout=2.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    # ------------------------------------------------------------------
    # Detection
    # ------------------------------------------------------------------
    def test_detection_closes_stream_and_pauses(self):
        detected = threading.Event()
        scores = []

        def on_detect(score):
            scores.append(score)
            detected.set()

        service = self.make_service(on_detect)
        self.score["value"] = 0.9
        service.start()
        try:
            self.assertTrue(detected.wait(2.0), "wake word was not detected")
            self.assertAlmostEqual(scores[0], 0.9)
            self.assertTrue(service.is_paused())
            self.assertTrue(self.audio.streams[0].closed)
            self.assertFalse(self.audio.terminated)
            self.assertTrue(self.models[0].resets >= 1)
        finally:
            service.stop()

        self.assertTrue(self.audio.terminated)

    def test_below_threshold_does_not_trigger(self):
        calls = []
        service = self.make_service(calls.append)
        self.score["value"] = 0.2
        service.start()
        try:
            time.sleep(0.2)
            self.assertEqual(calls, [])
        finally:
            service.stop()

    def test_pause_before_start_blocks_then_resume_detects(self):
        detected = threading.Event()
        service = self.make_service(lambda score: detected.set())
        service.pause()
        self.score["value"] = 0.9
        service.start()
        try:
            time.sleep(0.2)
            self.assertFalse(detected.is_set())
            service.resume()
            self.assertTrue(detected.wait(2.0))
        finally:
            service.stop()

    def test_resume_reopens_the_microphone(self):
        detected = threading.Event()
        service = self.make_service(lambda score: detected.set())
        self.score["value"] = 0.9
        service.start()
        try:
            self.assertTrue(detected.wait(2.0))
            opens = len(self.audio.streams)
            self.score["value"] = 0.0
            service.resume()
            self.assertTrue(
                self.wait_for(lambda: len(self.audio.streams) > opens),
                "mic stream was not reopened after resume",
            )
        finally:
            service.stop()

    def test_callback_error_is_reported_and_resumes(self):
        errors = []

        def boom(score):
            raise RuntimeError("callback boom")

        service = self.make_service(boom, on_error=errors.append, cooldown=5.0)
        self.score["value"] = 0.9
        service.start()
        try:
            self.assertTrue(self.wait_for(lambda: bool(errors)))
            self.assertIn("callback boom", errors[0])
            self.assertFalse(service.is_paused())
        finally:
            service.stop()

    # ------------------------------------------------------------------
    # Model / audio plumbing
    # ------------------------------------------------------------------
    def test_custom_model_path_and_framework(self):
        service = self.make_service(lambda score: None)
        self.score["value"] = 0.0
        service.start()
        try:
            self.assertTrue(self.wait_for(lambda: bool(self.models)))
            self.assertEqual(
                self.models[0].kwargs,
                {"wakeword_models": [str(self.model_file)],
                 "inference_framework": "onnx"},
            )
        finally:
            service.stop()

    def test_device_index_is_forwarded(self):
        service = self.make_service(lambda score: None, device_index=3)
        service.start()
        try:
            self.assertTrue(self.wait_for(lambda: bool(self.audio.open_kwargs)))
            self.assertEqual(self.audio.open_kwargs[0]["input_device_index"], 3)
            self.assertEqual(self.audio.open_kwargs[0]["rate"], 16_000)
            self.assertEqual(self.audio.open_kwargs[0]["channels"], 1)
            self.assertEqual(self.audio.open_kwargs[0]["frames_per_buffer"], 1_280)
        finally:
            service.stop()

    def test_stop_closes_stream_and_is_idempotent(self):
        service = self.make_service(lambda score: None)
        service.start()
        try:
            self.assertTrue(self.wait_for(lambda: bool(self.audio.streams)))
            service.stop()
            service.stop()
            self.assertTrue(self.audio.streams[0].closed)
            self.assertTrue(self.audio.terminated)
            self.assertFalse(service.is_running())
        finally:
            service.stop()

    def test_missing_model_paths_raise(self):
        with self.assertRaisesRegex(wake_word.WakeWordError, "not found"):
            wake_word.WakeWordService(
                on_detect=lambda score: None,
                model=str(self.tmp / "missing.onnx"),
            )

        with mock.patch.object(wake_word, "MODEL_DIRS", (self.tmp / "empty",)):
            with self.assertRaisesRegex(wake_word.WakeWordError, "No 'agent'"):
                wake_word.WakeWordService(
                    on_detect=lambda score: None, model="agent")

    def test_pretrained_name_passes_through(self):
        self.assertEqual(wake_word.resolve_model("hey_jarvis"), "hey_jarvis")

    def test_missing_dependency_reports_error(self):
        errors = []
        with mock.patch.object(
            wake_word, "_load_dependencies",
            side_effect=ImportError("pyaudio missing"),
        ):
            service = self.make_service(
                lambda score: None, on_error=errors.append)
            service.start()
            try:
                self.assertTrue(self.wait_for(lambda: bool(errors)))
                self.assertIn("pyaudio missing", errors[0])
                self.assertFalse(service.is_running())
            finally:
                service.stop()

    def test_invalid_threshold_and_cooldown_are_clamped(self):
        service = self.make_service(
            lambda score: None, threshold=9, cooldown=-1)
        self.assertEqual(service._threshold, 1.0)
        self.assertEqual(service._cooldown, 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
