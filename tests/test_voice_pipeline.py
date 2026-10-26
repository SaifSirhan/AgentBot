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


if __name__ == "__main__":
    unittest.main()
