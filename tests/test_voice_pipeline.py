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


class BlockingStream(AbortableStream):
    def __init__(self, **_kwargs):
        super().__init__()
        self.write_started = threading.Event()
        self.release_write = threading.Event()
        self.order = []

    def write(self, data):
        self.order.append("write-start")
        self.write_started.set()
        self.release_write.wait(timeout=5.0)
        self.order.append("write-end")
        super().write(data)

    def abort(self):
        self.order.append("abort")
        super().abort()

    def stop(self):
        self.order.append("stop")
        super().stop()

    def close(self):
        self.order.append("close")
        super().close()


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
                voice_output, "sanitize_for_speech", side_effect=lambda t: t),
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
        first_chunk_played = threading.Event()

        class SignalingStream(AbortableStream):
            def write(self, data):
                super().write(data)
                first_chunk_played.set()

        stream = SignalingStream()
        seen_streams = []
        chunks = [
            (None, None, np.array([0.1], dtype=np.float32)),
            (None, None, np.array([0.2], dtype=np.float32)),
        ]

        def pipeline(piece, voice=None, speed=None):
            seen_streams.append(voice_output._active_stream)
            yield chunks[0]
            # Synthesize sentence 1 ahead of playback, then interrupt only
            # after the consumer has actually played it.
            saw_playback = first_chunk_played.wait(2.0)
            voice_output.stop()
            if saw_playback:
                yield chunks[1]

        with mock.patch.object(voice_output.sd, "OutputStream",
                               return_value=stream), \
                mock.patch.object(
                    voice_output, "_get_pipeline", return_value=pipeline):
            voice_output._speak_blocking("hello")

        self.assertIs(seen_streams[0], stream)
        self.assertEqual(len(stream.written), 1)
        self.assertEqual(stream.aborted, 1)
        self.assertEqual(stream.closed, 1)
        self.assertTrue(voice_output._cancel_event.is_set())

    def test_stop_waits_for_an_in_flight_write_before_touching_the_stream(self):
        stream = BlockingStream()
        chunk = (None, None, np.array([0.1], dtype=np.float32))

        def pipeline(piece, voice=None, speed=None):
            yield chunk
            yield chunk

        self.addCleanup(stream.release_write.set)
        with mock.patch.object(voice_output.sd, "OutputStream",
                               return_value=stream), \
                mock.patch.object(
                    voice_output, "_get_pipeline", return_value=pipeline):
            worker = threading.Thread(
                target=voice_output._speak_blocking, args=("hello",),
                daemon=True)
            worker.start()
            self.assertTrue(stream.write_started.wait(2.0))
            stopper = threading.Thread(target=voice_output.stop, daemon=True)
            stopper.start()
            time.sleep(0.2)
            self.assertEqual(stream.order, ["write-start"])
            self.assertEqual(stream.aborted, 0)
            self.assertEqual(stream.closed, 0)
            stream.release_write.set()
            worker.join(2.0)
            stopper.join(2.0)

        self.assertFalse(worker.is_alive())
        self.assertFalse(stopper.is_alive())
        self.assertEqual(stream.order.count("write-start"), 1)
        self.assertEqual(len(stream.written), 1)
        teardown = [
            index for index, event in enumerate(stream.order)
            if event in ("abort", "stop", "close")
        ]
        self.assertTrue(teardown)
        self.assertGreater(min(teardown), stream.order.index("write-end"))
        self.assertEqual(stream.stopped, 1)
        self.assertEqual(stream.closed, 1)

    def test_stop_defers_teardown_when_a_write_outlives_the_timeout(self):
        stream = BlockingStream()
        chunk = (None, None, np.array([0.1], dtype=np.float32))

        def pipeline(piece, voice=None, speed=None):
            yield chunk

        self.addCleanup(stream.release_write.set)
        with mock.patch.object(voice_output.sd, "OutputStream",
                               return_value=stream), \
                mock.patch.object(
                    voice_output, "_get_pipeline", return_value=pipeline):
            worker = threading.Thread(
                target=voice_output._speak_blocking, args=("hello",),
                daemon=True)
            worker.start()
            self.assertTrue(stream.write_started.wait(2.0))
            started = time.monotonic()
            voice_output.stop()
            waited = time.monotonic() - started
            self.assertGreaterEqual(waited, 0.9)
            self.assertEqual(stream.aborted, 0)
            self.assertEqual(stream.closed, 0)
            stream.release_write.set()
            worker.join(2.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(stream.stopped, 1)
        self.assertEqual(stream.closed, 1)
        self.assertGreater(
            stream.order.index("close"), stream.order.index("write-end"))

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

    def stop_and_transcribe(self):
        return ""


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
                mock.patch.object(
                    g, "get_voice_output",
                    return_value=FakeTts(speaking=False)), \
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

    def test_f9_waits_for_wake_release_before_opening_the_mic(self):
        stt = FakeStt()
        pause_entered = threading.Event()
        release_mic = threading.Event()

        def pause():
            pause_entered.set()
            release_mic.wait(2.0)

        with mock.patch.object(g, "get_voice", return_value=stt), \
                mock.patch.object(
                    g, "get_voice_output",
                    return_value=FakeTts(speaking=False)), \
                mock.patch.object(
                    self.window, "_pause_wake_word", side_effect=pause):
            self.window._start_recording()
            self.assertTrue(pause_entered.wait(2.0))
            time.sleep(0.15)
            self.assertFalse(
                stt.started.is_set(),
                "recording started before the wake listener released")
            release_mic.set()
            self.assertTrue(stt.started.wait(2.0))

    def test_release_during_mic_prep_does_not_open_the_mic(self):
        stt = FakeStt()
        pause_entered = threading.Event()
        release_mic = threading.Event()

        def pause():
            pause_entered.set()
            release_mic.wait(2.0)

        with mock.patch.object(g, "get_voice", return_value=stt), \
                mock.patch.object(
                    g, "get_voice_output",
                    return_value=FakeTts(speaking=False)), \
                mock.patch.object(
                    self.window, "_pause_wake_word", side_effect=pause):
            self.window._start_recording()
            self.assertTrue(pause_entered.wait(2.0))
            self.window._recording = False  # F9 released while releasing
            release_mic.set()
            time.sleep(0.25)
            self.assertFalse(
                stt.started.is_set(),
                "released F9 still opened the microphone")

    def test_f9_stops_tts_and_waits_for_output_before_the_mic(self):
        stt = FakeStt()
        idle = threading.Event()
        wait_started = threading.Event()

        class WaitingTts(FakeTts):
            def __init__(self):
                super().__init__(queued=True, speaking=True)
                self.timeout = None

            def stop(self):
                super().stop()
                self.speaking = False

            def wait_until_idle(self, timeout=None):
                self.timeout = timeout
                wait_started.set()
                idle.wait(2.0)

        tts = WaitingTts()
        with mock.patch.object(g, "get_voice", return_value=stt), \
                mock.patch.object(g, "get_voice_output", return_value=tts), \
                mock.patch.object(self.window, "_pause_wake_word"):
            self.window._start_recording()
            self.assertTrue(wait_started.wait(2.0))
            self.assertEqual(tts.stops, 1)
            self.assertEqual(tts.timeout, 2.0)
            self.assertFalse(
                stt.started.is_set(),
                "recording started before TTS released the device")
            idle.set()
            self.assertTrue(stt.started.wait(2.0))

    def test_voice_preload_failure_is_visible(self):
        self.window._on_preload_ready("voice:error")
        self.assertTrue(self.window._voice_failed)
        self.assertIn("Voice unavailable", self.window.windowTitle())

    def test_transcription_error_is_written_to_transcript(self):
        self.window._on_voice_event("error:Transcribe error: whisper failed")
        texts = [text for _kind, text in self.window.chat.chat_log]
        self.assertTrue(
            any("whisper failed" in text for text in texts), texts)

    def test_preload_error_detail_reaches_the_transcript(self):
        self.window._preload_errors["voice"] = "Whisper model missing"
        self.window._on_preload_ready("voice:error")
        texts = [text for _kind, text in self.window.chat.chat_log]
        self.assertTrue(
            any("Whisper model missing" in text for text in texts), texts)

    def test_wake_preload_failure_survives_voice_ready(self):
        self.window._voice_ready = False
        self.window._on_preload_ready("wake_word:error")
        self.assertTrue(self.window._wake_failed)
        self.window._on_preload_ready("voice")
        self.assertIn(
            "wake word unavailable", self.window.windowTitle().lower())

    def test_wake_service_waits_for_voice_preload(self):
        self.window._voice_ready = False
        self.window._wake_ready = False
        self.window._wake_preload_done = False
        with mock.patch.object(
                self.window, "_start_wake_word_service") as start:
            self.window._on_preload_ready("wake_word")
        start.assert_not_called()
        with mock.patch.object(
                self.window, "_start_wake_word_service") as start:
            self.window._on_preload_ready("voice")
        start.assert_called_once()

    def test_wake_listener_uses_the_resolved_stt_microphone(self):
        captured = {}

        class FakeService:
            model_spec = "fake"
            inference_framework = "fake"

            def __init__(self, **kwargs):
                captured.update(kwargs)

            def start(self):
                pass

        fake_wake = mock.Mock(WakeWordService=FakeService)
        fake_voice = mock.Mock()
        fake_voice.resolve_input_device.return_value = (3, "USB Microphone")
        self.window._wake_word_service = None
        with mock.patch.dict(sys.modules, {"wake_word": fake_wake}), \
                mock.patch.object(
                    g, "get_voice", return_value=fake_voice), \
                mock.patch.object(
                    self.window, "_ensure_wake_watchdog"), \
                mock.patch.object(
                    g, "read_config_file_raw", return_value={
                        "WAKE_WORD_ENABLED": True,
                        "WAKE_WORD_DEVICE": "",
                        "WAKE_WORD_MODEL": "hey_jarvis",
                    }):
            self.window._start_wake_word_service()
        self.assertEqual(captured["device_index"], 3)

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
