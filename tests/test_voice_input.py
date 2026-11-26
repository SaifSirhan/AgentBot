"""Voice input routing, Whisper warmup, and model lifecycle tests.

No microphone or Whisper model is opened; sounddevice is faked.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np  # noqa: E402
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


class VoiceAutoStopCancellationTests(unittest.TestCase):
    def setUp(self):
        self._old = (
            voice._is_recording,
            voice._on_transcript,
            voice._recording_generation,
            voice._idle_timeout,
            voice._stream,
        )
        voice._recording_generation = 5
        voice._is_recording = True
        voice._idle_timeout = 60.0
        self.addCleanup(self._restore)

    def _restore(self):
        (voice._is_recording, voice._on_transcript,
         voice._recording_generation, voice._idle_timeout,
         voice._stream) = self._old

    def test_cancel_during_transcription_drops_the_late_callback(self):
        calls = []
        voice._on_transcript = lambda text, reason: calls.append(
            (text, reason))

        def transcribe_while_cancelled():
            voice.cancel_recording()
            return "stale words"

        with mock.patch.object(
                voice, "auto_stop_reason", return_value="idle"), \
                mock.patch.object(
                    voice, "stop_and_transcribe",
                    side_effect=transcribe_while_cancelled):
            voice._monitor_recording(5)

        self.assertEqual(calls, [])
        self.assertFalse(voice._is_recording)

    def test_normal_auto_stop_still_delivers_the_transcript(self):
        calls = []
        voice._on_transcript = lambda text, reason: calls.append(
            (text, reason))

        with mock.patch.object(
                voice, "auto_stop_reason", return_value="idle"), \
                mock.patch.object(
                    voice, "stop_and_transcribe", return_value="hello"):
            voice._monitor_recording(5)

        self.assertEqual(calls, [("hello", "idle")])

    def test_cancel_while_the_mic_opens_closes_the_stream(self):
        stream = FakeInputStream()

        def open_while_cancelled(_device_index, _callback):
            voice.cancel_recording()
            return stream

        with mock.patch.object(
                voice, "resolve_input_device",
                return_value=(1, "Microphone Array")), \
                mock.patch.object(
                    voice, "_open_input_stream",
                    side_effect=open_while_cancelled):
            voice.start_recording()

        self.assertFalse(voice._is_recording)
        self.assertEqual(stream.stopped, 1)
        self.assertEqual(stream.closed, 1)
        self.assertIsNone(voice._stream)


class VoiceTempFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="kestrel_voice_test_")
        self.addCleanup(self.temp.cleanup)
        self.saved = (
            voice._is_recording,
            voice._recording_chunks,
            voice._stream,
            voice._on_transcript,
            voice._idle_timeout,
        )
        voice._is_recording = True
        voice._recording_chunks = [
            np.zeros((voice._SAMPLERATE, 1), dtype=np.int16)
        ]
        voice._stream = None
        voice._on_transcript = None
        voice._idle_timeout = None
        self.addCleanup(self._restore)

    def _restore(self):
        (voice._is_recording, voice._recording_chunks, voice._stream,
         voice._on_transcript, voice._idle_timeout) = self.saved

    def test_successful_transcription_deletes_the_temporary_wav(self):
        paths = []

        def transcribe(path):
            paths.append(Path(path))
            self.assertTrue(paths[-1].exists())
            return "hello"

        with mock.patch.object(
                voice.tempfile, "gettempdir", return_value=self.temp.name), \
                mock.patch.object(voice, "transcribe", side_effect=transcribe):
            self.assertEqual(voice.stop_and_transcribe(), "hello")

        self.assertEqual(len(paths), 1)
        self.assertFalse(paths[0].exists())

    def test_failed_transcription_still_deletes_the_temporary_wav(self):
        paths = []

        def fail(path):
            paths.append(Path(path))
            raise RuntimeError("whisper failed")

        with mock.patch.object(
                voice.tempfile, "gettempdir", return_value=self.temp.name), \
                mock.patch.object(voice, "transcribe", side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, "whisper failed"):
                voice.stop_and_transcribe()

        self.assertEqual(len(paths), 1)
        self.assertFalse(paths[0].exists())

    def test_temporary_wav_paths_do_not_collide(self):
        audio = np.zeros((16, 1), dtype=np.int16)
        with mock.patch.object(
                voice.tempfile, "gettempdir", return_value=self.temp.name):
            first = Path(voice._save_wav(audio, voice._SAMPLERATE))
            second = Path(voice._save_wav(audio, voice._SAMPLERATE))
        self.addCleanup(first.unlink, missing_ok=True)
        self.addCleanup(second.unlink, missing_ok=True)
        self.assertNotEqual(first, second)
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())


class WhisperLifecycleTests(unittest.TestCase):
    def test_model_load_uses_only_the_existing_local_cache(self):
        model = object()
        with mock.patch.object(voice, "_whisper_model", None), \
                mock.patch.object(
                    voice, "WhisperModel", return_value=model) as constructor:
            self.assertIs(voice.get_whisper_model(), model)
        constructor.assert_called_once_with(
            voice.WHISPER_MODEL_SIZE,
            device="cpu",
            compute_type="int8",
            local_files_only=True,
        )

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
