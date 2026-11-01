"""Voice input routing, Whisper warmup, and model lifecycle tests.

No microphone or Whisper model is opened; sounddevice is faked.
"""
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import voice  # noqa: E402


class FakeInputStream:
    def __init__(self, **_kwargs):
        self.started = False
        self.stopped = 0
        self.closed = 0

    def start(self):
        self.started = True

    def stop(self):
        self.stopped += 1

    def close(self):
        self.closed += 1


class VoiceDeviceSelectionTests(unittest.TestCase):
    def setUp(self):
        self._old_device = voice._input_device
        voice._input_device = None
        self.addCleanup(self._restore)
        self.devices = [
            {"name": "CABLE Output (VB-Audio Virtual Cable)",
             "max_input_channels": 16},
            {"name": "Microphone Array (AMD Audio Device)",
             "max_input_channels": 2},
            {"name": "Stereo Mix (Realtek HD Audio)",
             "max_input_channels": 2},
        ]

    def _restore(self):
        voice._input_device = self._old_device

    def _fake_sd(self, default_index=0, supported=None):
        supported = (
            set(range(len(self.devices))) if supported is None else supported)
        fake = mock.Mock()
        fake.default = SimpleNamespace(device=[default_index, 5])
        fake.query_devices.return_value = self.devices

        def check_input_settings(device=None, **_kwargs):
            if device not in supported:
                raise RuntimeError("unsupported sample rate")

        fake.check_input_settings.side_effect = check_input_settings
        return fake

    def test_virtual_default_is_skipped_for_a_physical_microphone(self):
        fake = self._fake_sd(default_index=0)
        with mock.patch.object(voice, "sd", fake):
            index, name = voice.resolve_input_device()
        self.assertEqual(index, 1)
        self.assertIn("Microphone Array", name)

    def test_only_virtual_inputs_raise_a_loud_error(self):
        self.devices = [
            {"name": "CABLE Output (VB-Audio Virtual Cable)",
             "max_input_channels": 16},
            {"name": "Stereo Mix (Realtek HD Audio)",
             "max_input_channels": 2},
        ]
        fake = self._fake_sd(default_index=0)
        with mock.patch.object(voice, "sd", fake):
            with self.assertRaisesRegex(RuntimeError, "No physical microphone"):
                voice.resolve_input_device()

    def test_explicit_mic_device_override_is_honored(self):
        fake = self._fake_sd(default_index=0)
        with mock.patch.object(voice, "sd", fake), \
                mock.patch.dict(os.environ, {"AGENT_MIC_DEVICE": "2"}):
            index, _name = voice.resolve_input_device()
        self.assertEqual(index, 2)

    def test_start_recording_routes_the_selected_device(self):
        fake = self._fake_sd(default_index=0)
        fake.InputStream.side_effect = FakeInputStream
        with mock.patch.object(voice, "sd", fake), \
                mock.patch.object(
                    voice, "resolve_input_device",
                    return_value=(1, "Microphone Array")):
            voice.start_recording()
            try:
                kwargs = fake.InputStream.call_args.kwargs
                self.assertEqual(kwargs["device"], 1)
                self.assertEqual(kwargs["dtype"], "int16")
            finally:
                voice.cancel_recording()

    def test_mic_open_failure_resets_recording_and_reports_device(self):
        fake = self._fake_sd(default_index=0)
        fake.InputStream.side_effect = RuntimeError("device busy")
        with mock.patch.object(voice, "sd", fake), \
                mock.patch.object(
                    voice, "resolve_input_device",
                    return_value=(1, "Microphone Array")), \
                mock.patch.object(voice.time, "sleep") as sleep:
            with self.assertRaisesRegex(
                    RuntimeError, "Microphone Array.*device busy"):
                voice.start_recording()
        self.assertEqual(fake.InputStream.call_count, 2)
        sleep.assert_called_once_with(0.2)
        self.assertFalse(voice._is_recording)
        self.assertIsNone(voice._on_transcript)

    def test_mic_open_retries_a_transient_error_then_succeeds(self):
        fake = self._fake_sd(default_index=0)
        stream = FakeInputStream()
        fake.InputStream.side_effect = [
            RuntimeError("Error opening InputStream: device unavailable"),
            stream,
        ]
        with mock.patch.object(voice, "sd", fake), \
                mock.patch.object(
                    voice, "resolve_input_device",
                    return_value=(1, "Microphone Array")), \
                mock.patch.object(voice.time, "sleep") as sleep:
            voice.start_recording()
            try:
                self.assertTrue(stream.started)
            finally:
                voice.cancel_recording()
        self.assertEqual(fake.InputStream.call_count, 2)
        sleep.assert_called_once_with(0.2)


class WhisperLifecycleTests(unittest.TestCase):
    def test_warmup_loads_whisper_and_validates_the_microphone(self):
        model = object()
        with mock.patch.object(
                voice, "resolve_input_device",
                return_value=(1, "Microphone Array")) as resolve, \
                mock.patch.object(
                    voice, "get_whisper_model", return_value=model) as load:
            self.assertIs(voice.warmup(), model)
        resolve.assert_called_once()
        load.assert_called_once()

    def test_transcribe_keeps_the_warmed_model_loaded(self):
        class FakeModel:
            def transcribe(self, audio_path, **kwargs):
                return iter([SimpleNamespace(text=" hello ")]), None

        with mock.patch.object(
                voice, "get_whisper_model", return_value=FakeModel()), \
                mock.patch.object(
                    voice, "unload_whisper_model") as unload:
            self.assertEqual(voice.transcribe("probe.wav"), "hello")
        unload.assert_not_called()


if __name__ == "__main__":
    unittest.main()
