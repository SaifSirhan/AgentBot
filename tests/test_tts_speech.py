"""Tests for TTS text sanitization and producer-consumer prefetching.

Run: python -m unittest tests.test_tts_speech -v
"""
import queue
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np  # noqa: E402

import voice_output  # noqa: E402


class SanitizeForSpeechTests(unittest.TestCase):
    def test_code_blocks_are_removed(self):
        spoken = voice_output.sanitize_for_speech(
            "Run this:\n```python\nprint('hi')\n```\nDone.")
        self.assertNotIn("print", spoken)
        self.assertNotIn("```", spoken)
        self.assertIn("Done", spoken)

    def test_windows_and_posix_paths_become_the_file(self):
        spoken = voice_output.sanitize_for_speech(
            r"Open C:\Users\USER\AgentBot\voice_output.py and "
            r"/usr/bin/python now.")
        self.assertEqual(spoken, "Open the file and the file now.")
        self.assertNotIn("Users", spoken)
        self.assertNotIn("usr", spoken)

    def test_unc_paths_become_the_file(self):
        spoken = voice_output.sanitize_for_speech(
            r"Open \\server\share\report.txt now.")
        self.assertEqual(spoken, "Open the file now.")

    def test_urls_are_removed(self):
        spoken = voice_output.sanitize_for_speech(
            "See https://example.com/a/b?q=1 for details.")
        self.assertNotIn("example", spoken)
        self.assertNotIn("http", spoken)
        self.assertIn("details", spoken)

    def test_markdown_symbols_are_stripped(self):
        spoken = voice_output.sanitize_for_speech(
            "## Title\n**bold** and _italic_ and ~gone~ and `code`")
        for symbol in ("#", "*", "_", "~", "`"):
            self.assertNotIn(symbol, spoken)
        self.assertIn("bold", spoken)
        self.assertIn("italic", spoken)
        self.assertIn("code", spoken)

    def test_remaining_slashes_are_spoken(self):
        spoken = voice_output.sanitize_for_speech(
            "Use and/or, not a\\b.")
        self.assertNotIn("/", spoken)
        self.assertNotIn("\\", spoken)
        self.assertIn("and slash or", spoken)
        self.assertIn("a slash b", spoken)

    def test_sanitizing_is_idempotent(self):
        text = "**Hi** at C:\\Temp\\x.py: https://x.dev/a ```code``` end"
        once = voice_output.sanitize_for_speech(text)
        self.assertEqual(once, voice_output.sanitize_for_speech(once))

    def test_code_only_text_becomes_empty(self):
        self.assertEqual(
            voice_output.sanitize_for_speech("```python\nprint(1)\n```"), "")


class SplitForTtsTests(unittest.TestCase):
    def test_long_unpunctuated_text_is_hard_wrapped_near_150_chars(self):
        text = " ".join(["word"] * 100)
        chunks = voice_output._split_for_tts(text)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 150 for chunk in chunks))
        self.assertEqual(" ".join(chunks), text)

    def test_sentences_stay_whole_when_they_fit(self):
        chunks = voice_output._split_for_tts(
            "First sentence. Second sentence! Third?")
        self.assertEqual(
            chunks, ["First sentence.", "Second sentence!", "Third?"])


class _BlockingStream:
    def __init__(self, **_kwargs):
        self.written = []
        self.write_started = threading.Event()
        self.release = threading.Event()
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
        self.written.append(np.asarray(data))
        self.write_started.set()
        self.release.wait(5.0)


class PrefetchTests(unittest.TestCase):
    def setUp(self):
        voice_output._cancel_event.clear()
        voice_output._output_idle.set()
        with voice_output._active_stream_lock:
            voice_output._active_stream = None

    def tearDown(self):
        voice_output._cancel_event.clear()
        voice_output._output_idle.set()
        with voice_output._active_stream_lock:
            voice_output._active_stream = None

    def test_audio_queue_is_bounded_to_three_chunks(self):
        self.assertEqual(voice_output._AUDIO_QUEUE_MAXSIZE, 3)

    def test_synthesis_runs_ahead_of_blocked_playback(self):
        stream = _BlockingStream()
        second_synthesized = threading.Event()
        chunks = {
            "one": (None, None, np.array([0.1], dtype=np.float32)),
            "two": (None, None, np.array([0.2], dtype=np.float32)),
        }

        def pipeline(piece, voice=None, speed=None):
            if piece == "two":
                second_synthesized.set()
            yield chunks[piece]

        errors = []

        def run():
            try:
                voice_output._speak_blocking("one. two.")
            except Exception as exc:  # noqa: BLE001 - surfaced by the test
                errors.append(exc)

        with mock.patch.multiple(
                voice_output,
                _load_backend_status=mock.Mock(return_value=(True, "")),
                _load_tts_config=mock.Mock(
                    return_value=("af_heart", 1.0, "a", True)),
                _load_max_chars=mock.Mock(return_value=10000),
                _get_pipeline=mock.Mock(return_value=pipeline),
                _split_for_tts=mock.Mock(return_value=["one", "two"]),
                sanitize_for_speech=mock.Mock(side_effect=lambda t: t),
                sd=mock.Mock(OutputStream=mock.Mock(return_value=stream))):
            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            self.assertTrue(stream.write_started.wait(2.0))
            self.assertTrue(
                second_synthesized.wait(1.5),
                "second chunk was not synthesized while playback was blocked")
            stream.release.set()
            worker.join(3.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(stream.written), 2)
        np.testing.assert_allclose(stream.written[0], [[0.1]])
        np.testing.assert_allclose(stream.written[1], [[0.2]])

    def test_sanitized_text_reaches_the_pipeline(self):
        stream = _BlockingStream()
        stream.release.set()
        received = []

        def pipeline(piece, voice=None, speed=None):
            received.append(piece)
            return iter(())

        with mock.patch.multiple(
                voice_output,
                _load_backend_status=mock.Mock(return_value=(True, "")),
                _load_tts_config=mock.Mock(
                    return_value=("af_heart", 1.0, "a", True)),
                _load_max_chars=mock.Mock(return_value=10000),
                _get_pipeline=mock.Mock(return_value=pipeline),
                _split_for_tts=mock.Mock(side_effect=lambda text: [text]),
                sd=mock.Mock(OutputStream=mock.Mock(return_value=stream))):
            voice_output._speak_blocking("**Bold** at C:\\Temp\\x.py")

        self.assertEqual(received, ["Bold at the file"])

    def test_synthesis_failure_is_reported_and_playback_ends(self):
        stream = _BlockingStream()
        stream.release.set()

        def pipeline(piece, voice=None, speed=None):
            raise RuntimeError("synth boom")
            yield  # pragma: no cover

        with mock.patch.multiple(
                voice_output,
                _load_backend_status=mock.Mock(return_value=(True, "")),
                _load_tts_config=mock.Mock(
                    return_value=("af_heart", 1.0, "a", True)),
                _load_max_chars=mock.Mock(return_value=10000),
                _get_pipeline=mock.Mock(return_value=pipeline),
                _split_for_tts=mock.Mock(return_value=["hello"]),
                sanitize_for_speech=mock.Mock(side_effect=lambda t: t),
                sd=mock.Mock(OutputStream=mock.Mock(return_value=stream))), \
                mock.patch.object(voice_output, "_report_error") as report:
            voice_output._speak_blocking("hello")

        report.assert_called_once_with("speech failed: synth boom")
        self.assertEqual(stream.written, [])
        self.assertEqual(stream.closed, 1)

    def test_wait_until_idle_tracks_a_deferred_stop(self):
        stream = _BlockingStream()
        chunk = (None, None, np.array([0.1], dtype=np.float32))

        def pipeline(piece, voice=None, speed=None):
            yield chunk

        with mock.patch.multiple(
                voice_output,
                _load_backend_status=mock.Mock(return_value=(True, "")),
                _load_tts_config=mock.Mock(
                    return_value=("af_heart", 1.0, "a", True)),
                _load_max_chars=mock.Mock(return_value=10000),
                _get_pipeline=mock.Mock(return_value=pipeline),
                _split_for_tts=mock.Mock(return_value=["hello"]),
                sanitize_for_speech=mock.Mock(side_effect=lambda t: t),
                sd=mock.Mock(OutputStream=mock.Mock(return_value=stream))):
            worker = threading.Thread(
                target=voice_output._speak_blocking, args=("hello",),
                daemon=True)
            worker.start()
            self.assertTrue(stream.write_started.wait(2.0))
            stopper = threading.Thread(target=voice_output.stop, daemon=True)
            stopper.start()
            stopper.join(2.0)
            self.assertFalse(stopper.is_alive())
            self.assertEqual(stream.closed, 0)
            self.assertFalse(voice_output.wait_until_idle(timeout=0.1))
            stream.release.set()
            worker.join(3.0)
            self.assertFalse(worker.is_alive())
            self.assertTrue(voice_output.wait_until_idle(timeout=1.0))

        self.assertEqual(stream.closed, 1)

    def test_speak_queues_sanitized_text(self):
        queued = queue.Queue()
        with mock.patch.multiple(
                voice_output,
                _speech_queue=queued,
                _ensure_worker=mock.Mock(),
                _load_backend_status=mock.Mock(return_value=(True, "")),
                AUDIO_OK=True):
            self.assertTrue(
                voice_output.speak("**Hi** at C:\\Temp\\x.py"))
            generation, text = queued.get_nowait()

        self.assertIsInstance(generation, int)
        self.assertEqual(text, "Hi at the file")


class MaxCharsSpeechTests(unittest.TestCase):
    """TTS_MAX_CHARS used to slice the reply mid-word and append "...", which
    is what made long answers stop after a sentence or two."""

    def spoken(self, text, max_chars):
        stream = _BlockingStream()
        stream.release.set()
        received = []

        def pipeline(piece, voice=None, speed=None):
            received.append(piece)
            return iter(())

        with mock.patch.multiple(
                voice_output,
                _load_backend_status=mock.Mock(return_value=(True, "")),
                _load_tts_config=mock.Mock(
                    return_value=("af_heart", 1.0, "a", True)),
                _load_max_chars=mock.Mock(return_value=max_chars),
                _get_pipeline=mock.Mock(return_value=pipeline),
                _split_for_tts=mock.Mock(side_effect=lambda t: [t]),
                sanitize_for_speech=mock.Mock(side_effect=lambda t: t),
                sd=mock.Mock(OutputStream=mock.Mock(return_value=stream))):
            voice_output._speak_blocking(text)
        return received

    def test_zero_limit_speaks_the_whole_reply(self):
        text = "This is a sentence. " * 40
        self.assertEqual(self.spoken(text, 0), [text])

    def test_a_positive_limit_breaks_at_a_word_boundary(self):
        text = ("The quick brown fox jumps over the lazy dog while the "
                "cat watches quietly from the windowsill")
        spoken = self.spoken(text, 40)[0]
        self.assertTrue(text.startswith(spoken))
        self.assertFalse(spoken.endswith("..."), "cut with an ellipsis")
        self.assertLessEqual(len(spoken), 40)
        self.assertGreaterEqual(len(spoken), 30, "threw away too much")
        following = text[len(spoken):len(spoken) + 1]
        self.assertIn(following, (" ", "."), "sliced through a word")


class KokoroImportGuardTests(unittest.TestCase):
    """kokoro registers its loguru sink with sys.stderr while importing.

    Under pythonw.exe sys.stderr is None, which loguru rejects. The guard must
    supply a real sink for the import and restore the original state.
    """

    def test_import_gets_a_real_stderr_under_pythonw(self):
        sentinel = object()
        seen = []

        class RecordingKokoro(types.ModuleType):
            def __getattr__(self, name):
                if name == "KPipeline":
                    seen.append(sys.stderr)
                    return sentinel
                raise AttributeError(name)

        original_stderr = sys.stderr
        with mock.patch.dict(sys.modules, {"kokoro": RecordingKokoro("kokoro")}):
            sys.stderr = None
            try:
                self.assertIs(voice_output._import_kokoro_pipeline(), sentinel)
                self.assertEqual(len(seen), 1)
                self.assertIsNotNone(seen[0],
                                     "kokoro must see a real stderr sink")
                self.assertIsNone(sys.stderr,
                                  "the guard must restore sys.stderr")
            finally:
                sys.stderr = original_stderr
                sink = voice_output._kokoro_log_sink
                voice_output._kokoro_log_sink = None
                if sink is not None:
                    sink.close()


if __name__ == "__main__":
    unittest.main()
