"""Tests for the voice pipeline: VAD, auto-send, wake ack and continuous mode.

Run: python -m unittest tests.test_voice_pipeline -v
"""
import os
import queue
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import agent_gui_qt as g  # noqa: E402
import config  # noqa: E402
import voice  # noqa: E402
import voice_output  # noqa: E402


class ConfigVoiceDefaultsTests(unittest.TestCase):
    def test_continuous_listening_off_by_default(self):
        self.assertIs(config.DEFAULTS["CONTINUOUS_LISTENING"], False)

    def test_wake_record_window_is_three_and_a_half_seconds(self):
        self.assertEqual(config.DEFAULTS["WAKE_RECORD_MS"], 3500)


class VadTimeoutTests(unittest.TestCase):
    def setUp(self):
        voice._is_recording = False
        voice._recording_chunks = []
        voice._stream = None
        voice._on_transcript = None

    def test_silence_timeout_is_three_and_a_half_seconds(self):
        self.assertEqual(voice.SILENCE_TIMEOUT, 3.5)

    def test_speech_ends_after_silence_timeout(self):
        args = dict(
            speech_detected=True,
            last_speech_at=100.0,
            recording_started_at=90.0,
            idle_timeout=None,
        )
        self.assertIsNone(voice.auto_stop_reason(103.4, **args))
        self.assertEqual(voice.auto_stop_reason(103.5, **args), "silence")

    def test_no_speech_uses_idle_timeout_in_continuous_mode(self):
        args = dict(
            speech_detected=False,
            last_speech_at=0.0,
            recording_started_at=0.0,
            idle_timeout=60.0,
        )
        self.assertIsNone(voice.auto_stop_reason(59.9, **args))
        self.assertEqual(voice.auto_stop_reason(60.0, **args), "idle")

    def test_no_speech_without_idle_timeout_uses_silence_window(self):
        args = dict(
            speech_detected=False,
            last_speech_at=0.0,
            recording_started_at=10.0,
            idle_timeout=None,
        )
        self.assertIsNone(voice.auto_stop_reason(13.4, **args))
        self.assertEqual(voice.auto_stop_reason(13.5, **args), "no_speech")


class FakeStream:
    def __init__(self, **_kwargs):
        self.started = False
        self.stopped = False
        self.closed = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


class VoiceMonitorTests(unittest.TestCase):
    def setUp(self):
        voice._is_recording = False
        voice._recording_chunks = []
        voice._stream = None
        voice._on_transcript = None

    def tearDown(self):
        voice.cancel_recording()

    def test_monitor_reports_no_speech_and_stops(self):
        done = threading.Event()
        results = []
        stream = FakeStream()

        def on_transcript(text, reason):
            results.append((text, reason))
            done.set()

        with mock.patch.object(
                voice.sd, "InputStream", return_value=stream):
            voice.start_recording(
                on_transcript=on_transcript, silence_timeout=0.05)

        self.assertTrue(done.wait(2.0), "monitor callback never fired")
        self.assertEqual(results, [("", "no_speech")])
        self.assertFalse(voice._is_recording)
        self.assertTrue(stream.closed)

    def test_monitor_reports_transcription_errors(self):
        done = threading.Event()
        results = []
        stream = FakeStream()

        def on_transcript(text, reason):
            results.append((text, reason))
            done.set()

        with mock.patch.object(
                voice.sd, "InputStream", return_value=stream), \
                mock.patch.object(
                    voice, "stop_and_transcribe",
                    side_effect=RuntimeError("whisper failed")):
            voice.start_recording(
                on_transcript=on_transcript, silence_timeout=0.05)
            self.assertTrue(done.wait(2.0), "monitor callback never fired")

        self.assertEqual(results, [("", "error")])


class VoiceOutputStateTests(unittest.TestCase):
    def setUp(self):
        self.speech_queue = queue.Queue()
        self.patches = [
            mock.patch.object(voice_output, "_speech_queue", self.speech_queue),
            mock.patch.object(voice_output, "_ensure_worker"),
            mock.patch.object(
                voice_output, "_load_backend_status", return_value=(True, "")),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(voice_output.stop)

    def test_is_speaking_tracks_the_queue(self):
        self.assertFalse(voice_output.is_speaking())
        self.assertTrue(voice_output.speak("Hello there."))
        self.assertTrue(voice_output.is_speaking())
        voice_output.stop()
        self.assertFalse(voice_output.is_speaking())


class AbortableStream:
    def __init__(self, **_kwargs):
        self.written = []
        self.aborted = 0
        self.stopped = 0
        self.closed = 0

    def start(self):
        pass

    def abort(self):
        self.aborted += 1

    def stop(self):
        self.stopped += 1

    def close(self):
        self.closed += 1

    def write(self, data):
        self.written.append(data)


class VoiceOutputInterruptTests(unittest.TestCase):
    def setUp(self):
        voice_output._cancel_event.clear()
        voice_output._worker_busy.clear()
        self.queue = queue.Queue()
        self.patches = [
            mock.patch.object(voice_output, "_speech_queue", self.queue),
            mock.patch.object(
                voice_output, "_load_backend_status", return_value=(True, "")),
            mock.patch.object(
                voice_output, "_load_tts_config",
                return_value=("af_heart", 1.0, "a", True)),
            mock.patch.object(voice_output, "_load_max_chars", return_value=10000),
            mock.patch.object(
                voice_output, "_clean_for_speech", side_effect=lambda t: t),
            mock.patch.object(
                voice_output, "_split_for_tts", return_value=["hello"]),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(voice_output._cancel_event.clear)
        self.addCleanup(self._reset_stream)

    def _reset_stream(self):
        with voice_output._active_stream_lock:
            voice_output._active_stream = None

    def test_stop_aborts_the_active_stream_and_clears_the_queue(self):
        stream = AbortableStream()
        with voice_output._active_stream_lock:
            voice_output._active_stream = stream
        self.queue.put("pending sentence")
        voice_output.stop()
        self.assertTrue(self.queue.empty())
        self.assertEqual(stream.aborted, 1)
        self.assertEqual(stream.stopped, 1)
        self.assertEqual(stream.closed, 1)
        self.assertTrue(voice_output._cancel_event.is_set())
        self.assertFalse(voice_output.is_speaking())

    def test_speak_blocking_stops_after_an_interrupt(self):
        stream = AbortableStream()
        seen_streams = []
        chunks = [
            (None, None, np.array([0.1], dtype=np.float32)),
            (None, None, np.array([0.2], dtype=np.float32)),
        ]

        def pipeline(piece, voice=None, speed=None):
            seen_streams.append(voice_output._active_stream)
            yield chunks[0]
            voice_output.stop()
            yield chunks[1]

        with mock.patch.object(voice_output.sd, "OutputStream",
                               return_value=stream), \
                mock.patch.object(
                    voice_output, "_get_pipeline", return_value=pipeline):
            voice_output._speak_blocking("hello")

        self.assertIs(seen_streams[0], stream)
        self.assertEqual(len(stream.written), 1)
        self.assertTrue(voice_output._cancel_event.is_set())

    def test_reply_dequeued_before_stop_is_not_spoken(self):
        stale_generation = voice_output._current_generation()
        voice_output.stop()
        stream = AbortableStream()
        with mock.patch.object(voice_output.sd, "OutputStream",
                               return_value=stream) as ctor, \
                mock.patch.object(voice_output, "_get_pipeline") as pipeline:
            voice_output._speak_blocking(
                "hello", generation=stale_generation)
        self.assertFalse(ctor.called)
        self.assertFalse(pipeline.called)
        self.assertEqual(stream.written, [])

    def test_pipeline_failure_logs_without_an_error_callback(self):
        class FailingPipeline:
            def __init__(self, **_kwargs):
                raise RuntimeError("weights missing")

        previous = voice_output._error_callback
        voice_output._error_callback = None
        voice_output._pipelines.clear()
        self.addCleanup(setattr, voice_output, "_error_callback", previous)
        self.addCleanup(voice_output._pipelines.clear)
        fake_kokoro = mock.Mock(KPipeline=FailingPipeline)
        with mock.patch.dict(sys.modules, {"kokoro": fake_kokoro}), \
                mock.patch.object(
                    voice_output, "_kokoro_cache_ready", return_value=False), \
                mock.patch("builtins.print") as printed:
            self.assertIsNone(voice_output._get_pipeline("a"))
        self.assertIn("Kokoro pipeline failed to load", voice_output.last_error())
        emitted = " ".join(str(call) for call in printed.call_args_list)
        self.assertIn("Kokoro pipeline failed to load", emitted)


class FakeTts:
    def __init__(self, queued=True, speaking=False):
        self.queued = queued
        self.speaking = speaking
        self.spoken = []
        self.stops = 0

    def speak(self, text):
        self.spoken.append(text)
        return self.queued

    def stop(self):
        self.stops += 1

    def is_speaking(self):
        return self.speaking

    def last_error(self):
        return ""


class FakeWakeService:
    def __init__(self, running=True):
        self.running = running
        self.stopped = 0

    def is_running(self):
        return self.running

    def stop(self):
        self.stopped += 1


class FakeStt:
    def __init__(self):
        self.kwargs = None
        self.started = threading.Event()
        self.cancelled = 0

    def start_recording(self, **kwargs):
        self.kwargs = kwargs
        self.started.set()

    def cancel_recording(self):
        self.cancelled += 1


class GuiVoicePipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = g.AgentWindow()
        self.window._continuous_enabled = False
        self.window._continuous_paused = False
        self.window._continuous_listening = False
        self.window._processing = False
        self.window._recording = False
        self.window._voice_ready = True
        self.window._wake_ready = True
        self.window._voice_failed = False
        self.window._tts_ready = True
        self.window._agent_ready = True
        self.window._f9_held = False
        self.window._pending_f9_start = False
        for _ in range(10):
            self.app.processEvents()

    def tearDown(self):
        self.window._wake_triggered = False
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def test_send_stops_tts_before_the_turn(self):
        tts = FakeTts()
        self.window.input_field.setPlainText("hello")
        with mock.patch.object(g, "get_voice_output", return_value=tts), \
                mock.patch.object(
                    self.window, "_start_agent_turn") as start_turn:
            self.window.send()
        self.assertEqual(tts.stops, 1)
        start_turn.assert_called_once()

    def test_send_before_tts_preload_does_not_import_or_block(self):
        self.window._tts_ready = False
        self.window.input_field.setPlainText("hello")
        with mock.patch.object(
                g, "get_voice_output",
                side_effect=AssertionError("TTS imported on the GUI thread")), \
                mock.patch.object(self.window, "_start_agent_turn") as start:
            self.window.send()
        start.assert_called_once()

    def test_empty_send_does_not_touch_tts(self):
        tts = FakeTts()
        with mock.patch.object(g, "get_voice_output", return_value=tts), \
                mock.patch.object(self.window, "_start_agent_turn"):
            self.window.send()
        self.assertEqual(tts.stops, 0)

    def test_transcript_auto_sends_and_interrupts_tts(self):
        tts = FakeTts()
        with mock.patch.object(g, "get_voice_output", return_value=tts), \
                mock.patch.object(
                    self.window, "_start_agent_turn") as start_turn:
            self.window._handle_transcript("  open the browser  ")
        self.assertEqual(self.window.input_field.toPlainText(), "")
        self.assertEqual(tts.stops, 1)
        start_turn.assert_called_once()
        self.assertEqual(start_turn.call_args.args[0], "open the browser")

    def test_empty_transcript_is_not_sent(self):
        with mock.patch.object(self.window, "send") as send:
            self.window._handle_transcript("   ")
        send.assert_not_called()
        self.assertIn("No speech", self.window.windowTitle())

    def test_continuous_listening_is_discoverable(self):
        self.assertIn("/continuous", g.SLASH_COMMANDS)
        self.assertIn("/continuous on|off", g.HELP_TEXT)

    def test_continuous_on_persists_and_starts_listening(self):
        with mock.patch.object(g, "write_config_keys") as write, \
                mock.patch.object(
                    self.window, "_schedule_continuous_listen") as schedule:
            self.assertTrue(
                self.window._handle_slash_command("/continuous on"))
        self.assertTrue(self.window._continuous_enabled)
        write.assert_called_once_with({"CONTINUOUS_LISTENING": True})
        schedule.assert_called_once_with()

    def test_continuous_off_persists_and_cancels(self):
        self.window._continuous_enabled = True
        with mock.patch.object(g, "write_config_keys") as write, \
                mock.patch.object(
                    self.window, "_cancel_continuous_listening") as cancel:
            self.assertTrue(
                self.window._handle_slash_command("/continuous off"))
        self.assertFalse(self.window._continuous_enabled)
        write.assert_called_once_with({"CONTINUOUS_LISTENING": False})
        cancel.assert_called_once()

    def test_continuous_usage_error_does_not_persist(self):
        with mock.patch.object(g, "write_config_keys") as write:
            self.assertTrue(
                self.window._handle_slash_command("/continuous maybe"))
        write.assert_not_called()
        self.assertFalse(self.window._continuous_enabled)

    def test_continuous_idle_pauses_and_returns_to_wake_word(self):
        self.window._continuous_enabled = True
        self.window._continuous_listening = True
        self.window._recording = True
        with mock.patch.object(
                self.window, "_resume_wake_word") as resume:
            self.window._on_voice_event("continuous:idle:")
        self.assertFalse(self.window._recording)
        self.assertFalse(self.window._continuous_listening)
        self.assertTrue(self.window._continuous_paused)
        resume.assert_called_once()
        widgets = [
            self.window.chat._layout.itemAt(index).widget()
            for index in range(self.window.chat._layout.count())
        ]
        notices = [w for w in widgets if isinstance(w, g.SystemMessage)]
        self.assertTrue(notices)
        labels = [
            label.text()
            for notice in notices
            for label in notice.findChildren(g.QLabel)
        ]
        self.assertTrue(
            any("60s of silence" in text for text in labels), labels)

    def test_continuous_transcript_sends_without_resuming_wake(self):
        self.window._continuous_enabled = True
        self.window._continuous_listening = True
        self.window._recording = True
        with mock.patch.object(
                self.window, "_handle_transcript") as handle, \
                mock.patch.object(
                    self.window, "_resume_wake_word") as resume:
            self.window._on_voice_event(
                "continuous:silence:open the browser")
        handle.assert_called_once_with("open the browser")
        resume.assert_not_called()
        self.assertFalse(self.window._recording)
        self.assertFalse(self.window._continuous_listening)

    def test_continuous_transcription_error_pauses(self):
        self.window._continuous_enabled = True
        self.window._continuous_listening = True
        self.window._recording = True
        with mock.patch.object(
                self.window, "_handle_transcript") as handle, \
                mock.patch.object(
                    self.window, "_resume_wake_word") as resume:
            self.window._on_voice_event("continuous:error:")
        handle.assert_not_called()
        resume.assert_called_once()
        self.assertTrue(self.window._continuous_paused)
        self.assertFalse(self.window._recording)

    def test_normal_transcript_still_resumes_wake_word(self):
        with mock.patch.object(
                self.window, "_handle_transcript") as handle, \
                mock.patch.object(
                    self.window, "_resume_wake_word") as resume:
            self.window._on_voice_event("transcript:hello")
        handle.assert_called_once_with("hello")
        resume.assert_called_once()

    def test_completed_turn_rearms_continuous_listening(self):
        with mock.patch.object(self.window, "_speak_reply"), \
                mock.patch.object(self.window, "_end_turn"), \
                mock.patch.object(
                    self.window, "_schedule_continuous_listen") as schedule:
            self.window._on_turn_completed(("done", []))
        schedule.assert_called_once_with()

    def test_continuous_recording_uses_sixty_second_idle_timeout(self):
        stt = FakeStt()
        with mock.patch.object(g, "get_voice", return_value=stt), \
                mock.patch.object(self.window, "_pause_wake_word"):
            self.window._start_recording(continuous=True)
            self.assertTrue(
                stt.started.wait(2.0), "recording never started")
        self.assertEqual(stt.kwargs["idle_timeout"], 60.0)
        self.assertTrue(callable(stt.kwargs["on_transcript"]))
        self.assertTrue(self.window._continuous_listening)

    def test_wake_word_speaks_ack_then_opens_mic(self):
        tts = FakeTts(queued=True, speaking=True)
        presses = []
        self.window._hotkey_press = lambda event=None: presses.append("press")
        with mock.patch.object(g, "get_voice_output", return_value=tts), \
                mock.patch.object(
                    g, "read_config_file_raw",
                    return_value={"WAKE_RECORD_MS": 100}):
            self.window._on_wake_word(0.9)

        self.assertEqual(tts.spoken, ["Yeah?"])
        self.assertEqual(tts.stops, 1)
        self.assertEqual(presses, [], "mic opened before the ack finished")

        tts.speaking = False
        deadline = time.time() + 2.0
        while time.time() < deadline and not presses:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertEqual(presses, ["press"])
        self.window._wake_triggered = False

    def test_wake_word_opens_mic_immediately_without_tts(self):
        tts = FakeTts(queued=False)
        presses = []
        self.window._hotkey_press = lambda event=None: presses.append("press")
        with mock.patch.object(g, "get_voice_output", return_value=tts), \
                mock.patch.object(
                    g, "read_config_file_raw",
                    return_value={"WAKE_RECORD_MS": 100}):
            self.window._on_wake_word(0.9)
        self.assertEqual(tts.spoken, ["Yeah?"])
        self.assertEqual(presses, ["press"])
        self.window._wake_triggered = False

    def test_wake_flow_failure_resumes_the_listener(self):
        with mock.patch.object(
                g, "get_voice_output", side_effect=RuntimeError("tts down")), \
                mock.patch.object(
                    self.window, "_open_mic_for_wake",
                    side_effect=RuntimeError("mic busy")), \
                mock.patch.object(
                    self.window, "_resume_wake_word") as resume:
            self.window._on_wake_word(0.9)
        resume.assert_called_once()
        self.assertFalse(self.window._wake_triggered)

    def test_wake_watchdog_restarts_a_dead_listener(self):
        service = FakeWakeService(running=False)
        self.window._wake_word_service = service
        self.window._wake_watchdog_disabled = False
        self.window._wake_restart_attempts = 0
        with mock.patch.object(
                self.window, "_start_wake_word_service") as start:
            self.window._check_wake_word_health()
        self.assertEqual(service.stopped, 1)
        start.assert_called_once()
        self.assertIsNone(self.window._wake_word_service)

    def test_f9_before_voice_ready_shows_loading_without_recording(self):
        self.window._voice_ready = False
        with mock.patch.object(self.window, "_start_recording") as start:
            self.window._on_hotkey_changed(True)
        start.assert_not_called()
        self.assertTrue(self.window._pending_f9_start)
        self.assertIn("Voice loading", self.window.windowTitle())
        self.window._on_hotkey_changed(False)
        self.assertFalse(self.window._pending_f9_start)

    def test_pending_f9_starts_once_voice_finishes_loading(self):
        self.window._voice_ready = False
        self.window._f9_held = True
        self.window._pending_f9_start = True
        with mock.patch.object(self.window, "_start_recording") as start:
            self.window._on_preload_ready("voice")
        self.assertTrue(self.window._voice_ready)
        start.assert_called_once()

    def test_start_recording_waits_for_voice_preload(self):
        self.window._voice_ready = False
        with mock.patch.object(g, "get_voice") as get_voice:
            self.window._start_recording()
        get_voice.assert_not_called()
        self.assertIn("Voice loading", self.window.windowTitle())

    def test_voice_preload_failure_is_visible(self):
        self.window._on_preload_ready("voice:error")
        self.assertTrue(self.window._voice_failed)
        self.assertIn("Voice unavailable", self.window.windowTitle())

    def test_wake_preload_failure_survives_voice_ready(self):
        self.window._voice_ready = False
        self.window._on_preload_ready("wake_word:error")
        self.assertTrue(self.window._wake_failed)
        self.window._on_preload_ready("voice")
        self.assertIn(
            "wake word unavailable", self.window.windowTitle().lower())

    def test_wake_watchdog_does_not_touch_a_healthy_listener(self):
        service = FakeWakeService(running=True)
        self.window._wake_word_service = service
        with mock.patch.object(
                self.window, "_start_wake_word_service") as start:
            self.window._check_wake_word_health()
        self.assertEqual(service.stopped, 0)
        start.assert_not_called()
        self.assertIs(self.window._wake_word_service, service)


if __name__ == "__main__":
    unittest.main()
