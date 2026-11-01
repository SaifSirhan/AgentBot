"""
Text-to-speech via Kokoro-82M (local, offline, high quality).

- Persistent worker thread (safe from GUI daemon threads)
- Model warmup at app startup so first reply is instant
- Sanitizes markdown, file paths, URLs, and slashes before synthesis
- Prefetches audio chunks: synthesis runs ahead of playback in a small queue
- Splits long text into short chunks so CPU synthesis keeps up

Requires:
    pip install kokoro soundfile sounddevice
"""

import re
import queue
import threading
import time
import warnings
import os
import numpy as np

from audio_bus import audio_bus

# Silence PyTorch / HuggingFace chatter
warnings.filterwarnings("ignore")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

try:
    import sounddevice as sd
    AUDIO_OK = True
except ImportError as e:
    AUDIO_OK = False
    print(f"[tts] sounddevice missing: {e}")
    print("[tts] Run: pip install kokoro soundfile sounddevice")


# =============================================================
# Error reporting (the GUI runs under pythonw, so prints vanish)
# =============================================================
_error_callback = None
_last_error = ""
_audio_error_reported = False


def set_error_callback(callback):
    """Register a callable that receives speech-failure messages."""
    global _error_callback
    _error_callback = callback


def last_error():
    return _last_error


def _report_error(message):
    global _last_error
    _last_error = str(message or "")
    print(f"[tts] {_last_error}")
    callback = _error_callback
    if callback is None:
        return
    try:
        callback(_last_error)
    except Exception:
        pass


# =============================================================
# DEFAULTS (config.json overrides these)
# =============================================================
VOICE = "af_heart"   # af_heart, af_bella, am_michael, bf_emma, bm_george
SPEED = "1.0"        # 1.0 = normal. Try 0.9-1.15
LANG  = "a"          # 'a' = American English, 'b' = British English


# =============================================================
# Text cleanup
# =============================================================
# A Windows drive/UNC path. Segments cannot contain whitespace: allowing it
# lets the intermediate group swallow prose before a later slash. The final
# segment stops at a word boundary so trailing prose is not swallowed. The
# lookbehind keeps it from matching the "C:" inside a URL or a word.
_WINDOWS_PATH_RE = re.compile(
    r"(?<![\w:/])(?:[A-Za-z]:[\\/]|\\\\)"
    r"(?:[^\\/:*?\"<>|\r\n\s]+[\\/])*"
    r"[^\\/:*?\"<>|\r\n\s]*?"
    r"(?=$|[\s,;:!?])"
)
# Unix absolute path such as /usr/bin/python.
_POSIX_PATH_RE = re.compile(
    r"(?<![\w:])/(?:[A-Za-z0-9._@+-]+/)+[A-Za-z0-9._@+-]*"
)
_URL_RE = re.compile(r"\b(?:https?://|www\.)\S+", re.IGNORECASE)


def sanitize_for_speech(text):
    """Turn LLM markdown/output into text that is safe and natural to speak.

    Code blocks and URLs are removed, file paths become "the file", markdown
    punctuation is stripped, and any remaining slash is spoken as "slash".
    """
    if not text:
        return ""

    s = str(text).strip()
    if s.startswith("AI: "):
        s = s[4:]

    # [From: C:\...] source tags
    s = re.sub(r"\[From:[^\]]*\]", "", s)

    # Fenced code blocks: drop the code and the fences entirely.
    s = re.sub(r"```.*?```", " ", s, flags=re.DOTALL)
    s = s.replace("```", " ")

    # Inline code: keep the words, drop the backticks.
    s = re.sub(r"`([^`]*)`", r"\1", s)

    # Markdown links and images: keep the label, discard the target.
    s = re.sub(r"!?\[([^\]]+)\]\([^)]+\)", r"\1", s)

    # Bare URLs are not worth reading aloud.
    s = _URL_RE.sub(" ", s)

    # Paths are spoken as a single phrase instead of character by character.
    s = _WINDOWS_PATH_RE.sub(" the file ", s)
    s = _POSIX_PATH_RE.sub(" the file ", s)

    # Markdown emphasis/header markers.
    s = re.sub(r"[*#_~`]+", "", s)

    # Headers, bullets, numbered lines, blockquotes, and horizontal rules.
    s = re.sub(r"^#{1,6}\s*", "", s, flags=re.MULTILINE)
    s = re.sub(r"^\s*[-*+]\s+", "", s, flags=re.MULTILINE)
    s = re.sub(r"^\s*\d+\.\s+", "", s, flags=re.MULTILINE)
    s = re.sub(r"^\s*>\s?", "", s, flags=re.MULTILINE)
    s = re.sub(r"^\s*[-*_]{3,}\s*$", "", s, flags=re.MULTILINE)

    # Anything that still looks like a slash is spoken, not skipped.
    s = re.sub(r"[\\/]+", " slash ", s)

    # Emojis.
    s = re.sub(
        r"[\U0001F300-\U0001FAFF\U00002600-\U000027BF"
        r"\U0001F1E6-\U0001F1FF]+", "", s)

    # Whitespace and line breaks.
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\s*\n\s*", ". ", s)
    s = re.sub(r"\.\s*\.", ".", s)
    s = re.sub(r"\s+", " ", s).strip()

    if not re.search(r"[A-Za-z0-9]", s):
        return ""
    return s


def _split_for_tts(text, max_chars=150):
    """Split text into chunks of about max_chars, preferring sentence breaks."""
    out = []
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(sentence) <= max_chars:
            out.append(sentence)
            continue
        # Prefer clause boundaries, then hard-wrap anything still too long.
        buf = ""
        for piece in re.split(r"(?<=[,;—])\s+", sentence):
            piece = piece.strip()
            while len(piece) > max_chars:
                if buf:
                    out.append(buf)
                    buf = ""
                cut = piece.rfind(" ", 0, max_chars + 1)
                if cut <= 0:
                    cut = max_chars
                out.append(piece[:cut].strip())
                piece = piece[cut:].strip()
            if not piece:
                continue
            if len(buf) + len(piece) + 1 <= max_chars:
                buf = (buf + " " + piece).strip()
            else:
                if buf:
                    out.append(buf)
                buf = piece
        if buf:
            out.append(buf)
    return [c.strip() for c in out if c.strip()]


# =============================================================
# Config loader
# =============================================================
def _load_tts_config():
    voice = VOICE
    speed = 1.0
    lang = LANG
    enabled = True

    try:
        import config
        cfg = config.load_config()
    except Exception:
        return voice, speed, lang, enabled

    try:
        v = (cfg.get("TTS_VOICE") or "").strip()
        if v:
            voice = v

        s = (cfg.get("TTS_SPEED") or "").strip()
        if s:
            try:
                speed = float(s)
            except ValueError:
                speed = 1.0

        l = (cfg.get("TTS_LANG") or "").strip()
        if l:
            lang = l

        en = str(cfg.get("TTS_ENABLED", "true")).strip().lower()
        enabled = en in ("true", "1", "yes", "on", "")
    except Exception:
        pass

    return voice, speed, lang, enabled


def _load_backend_status():
    """Return (usable, message) for TTS_BACKEND.

    Only the local Kokoro engine is implemented; "none"/"off" is the explicit
    opt-out. Anything else is a config error worth surfacing.
    """
    try:
        import config
        cfg = config.load_config()
    except Exception:
        return True, ""
    backend = str(cfg.get("TTS_BACKEND", "kokoro") or "").strip().lower()
    if not backend or backend == "kokoro":
        return True, ""
    if backend in ("none", "off", "disabled"):
        return False, ""  # explicit opt-out: stay silent
    return False, (
        f"unknown TTS_BACKEND '{backend}' \u2014 expected 'kokoro' or 'none'")


def _load_max_chars():
    try:
        import config
        cfg = config.load_config()
        return max(80, int(str(cfg.get("TTS_MAX_CHARS", "2000")).strip()))
    except Exception:
        return 2000


# =============================================================
# Kokoro weights: use the local HuggingFace cache when it is complete
# =============================================================
_KOKORO_REPO_DIR = "models--hexgrad--Kokoro-82M"


def _kokoro_cache_ready():
    """True when the HF cache holds the config, weights and voice files."""
    import glob
    roots = []
    hub_cache = os.environ.get("HUGGINGFACE_HUB_CACHE")
    if hub_cache:
        roots.append(hub_cache)
    hf_home = os.environ.get("HF_HOME")
    if hf_home:
        roots.append(os.path.join(hf_home, "hub"))
    roots.append(os.path.join(
        os.path.expanduser("~"), ".cache", "huggingface", "hub"))

    for root in roots:
        model_dir = os.path.join(root, _KOKORO_REPO_DIR)
        if not os.path.isdir(model_dir):
            continue
        pattern = os.path.join(model_dir, "snapshots", "*", "config.json")
        if not glob.glob(pattern):
            continue
        if not glob.glob(os.path.join(
                model_dir, "snapshots", "*", "kokoro-v1_0.pth")):
            continue
        if not glob.glob(os.path.join(
                model_dir, "snapshots", "*", "voices", "*.pt")):
            continue
        return True
    return False


def _force_cached_downloads():
    """Pin kokoro's HF calls to the local cache (no network round-trips).

    kokoro imports hf_hub_download into both model.py and pipeline.py, so
    patch those module references rather than the global huggingface_hub
    helper. Other app components keep their normal network behaviour.
    """
    import kokoro.model as kokoro_model
    import kokoro.pipeline as kokoro_pipeline

    for module in (kokoro_model, kokoro_pipeline):
        original = getattr(module, "hf_hub_download", None)
        if original is None or getattr(original, "_agentbot_cache_only", False):
            continue

        def cached(*args, __original=original, **kwargs):
            # Cache first (no network, immune to hub hiccups), then fall back
            # to a normal download when the file genuinely is not cached yet.
            kwargs["local_files_only"] = True
            try:
                return __original(*args, **kwargs)
            except Exception:
                kwargs.pop("local_files_only", None)
                return __original(*args, **kwargs)

        cached._agentbot_cache_only = True
        module.hf_hub_download = cached


# =============================================================
# Pipeline cache
# =============================================================
_pipelines = {}
_pipeline_lock = threading.Lock()


def _get_pipeline(lang_code):
    cached_pipeline = _pipelines.get(lang_code)
    if cached_pipeline is not None:
        return cached_pipeline

    with _pipeline_lock:
        cached_pipeline = _pipelines.get(lang_code)
        if cached_pipeline is not None:
            return cached_pipeline
        try:
            from kokoro import KPipeline
            device = "cpu"
            try:
                import torch
                if torch.cuda.is_available():
                    device = "cuda"
            except Exception:
                pass
            offline = _kokoro_cache_ready()
            if offline:
                _force_cached_downloads()
                print(
                    f"[tts] loading pipeline from local cache "
                    f"(lang={lang_code}, device={device})")
            else:
                print(f"[tts] loading pipeline (lang={lang_code}, device={device})")
            pipeline = KPipeline(lang_code=lang_code, device=device)
            # Prime the configured voice while the cache-only patch is active.
            try:
                voice = _load_tts_config()[0]
                load_voice = getattr(pipeline, "load_voice", None)
                if voice and load_voice is not None:
                    load_voice(voice)
            except Exception as e:
                print(f"[tts] voice preload failed: {e}")
            _pipelines[lang_code] = pipeline
            print("[tts] pipeline loaded")
            return pipeline
        except Exception as e:
            # Never cache a failure: one transient network or ONNX error must
            # not mute speech for the rest of the session.
            _pipelines.pop(lang_code, None)
            _report_error(f"Kokoro pipeline failed to load: {e}")
            try:
                import traceback
                traceback.print_exc()
            except Exception:
                pass
            return None


# =============================================================
# Worker thread
# =============================================================
_speech_queue = queue.Queue()
_worker_started = False
_worker_lock = threading.Lock()
_worker_busy = threading.Event()
_active_stream = None
_active_stream_lock = threading.Lock()
# Cleared while an output stream is open, set once it is fully closed. Input
# code waits on this before opening the microphone.
_output_idle = threading.Event()
_output_idle.set()
# PortAudio streams are not safe to write, abort, or close concurrently. Every
# one of those operations happens while holding this lock.
_stream_lock = threading.Lock()
_cancel_event = threading.Event()
_speech_generation = 0
_generation_lock = threading.Lock()
# Playback keeps at most three synthesized chunks queued ahead of the audio
# device, enough to cover synthesis without eating reply latency.
_AUDIO_QUEUE_MAXSIZE = 3
_AUDIO_SENTINEL = object()
# A cancelled producer can still be finishing a Kokoro call when the next reply
# starts, so pipeline use is serialized across replies.
_synthesis_lock = threading.Lock()


def _current_generation() -> int:
    with _generation_lock:
        return _speech_generation


def _worker_loop():
    print("[tts] worker started")
    while True:
        try:
            item = _speech_queue.get()
        except Exception:
            continue
        if isinstance(item, tuple) and len(item) == 2:
            generation, text = item
        else:
            generation, text = None, item
        if text is None:
            break
        _worker_busy.set()
        try:
            _speak_blocking(text, generation=generation)
        except Exception as e:
            _report_error(f"speech failed: {e}")
            import traceback
            traceback.print_exc()
        finally:
            _speech_queue.task_done()
            if _speech_queue.empty():
                _worker_busy.clear()


def _speak_blocking(text, generation=None):
    """Speak one reply with synthesis and playback running concurrently."""
    global _active_stream
    _cancel_event.clear()

    def cancelled():
        # A stop() bumps the generation, so a reply that was already dequeued
        # when the user typed a new message is dropped instead of speaking.
        return _cancel_event.is_set() or (
            generation is not None and generation != _current_generation())

    if cancelled():
        return
    usable, backend_error = _load_backend_status()
    if not usable:
        if backend_error:
            _report_error(backend_error)
        return

    voice, speed, lang, enabled = _load_tts_config()
    if not enabled:
        print("[tts] disabled in settings")
        return

    cleaned = sanitize_for_speech(text)
    if not cleaned:
        print("[tts] nothing to speak after cleanup")
        return

    max_chars = _load_max_chars()
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars] + "..."

    print(f"[tts] generating ({len(cleaned)} chars, voice={voice}, speed={speed})")

    pipeline = _get_pipeline(lang)
    if pipeline is None:
        return

    # Open the audio stream once, keep it for the whole reply
    try:
        stream = sd.OutputStream(
            samplerate=24000,
            channels=1,
            dtype="float32",
            blocksize=4800,
            latency='high',
        )
        stream.start()
    except Exception as e:
        _report_error(f"could not open audio output: {e}")
        return

    with _active_stream_lock:
        _active_stream = stream
    _output_idle.clear()

    audio_queue = queue.Queue(maxsize=_AUDIO_QUEUE_MAXSIZE)
    producer_stop = threading.Event()

    def synthesize():
        """Producer: turn sentences into audio chunks ahead of playback."""
        error = None
        try:
            with _synthesis_lock:
                for piece in _split_for_tts(cleaned):
                    if cancelled() or producer_stop.is_set():
                        return
                    for chunk in pipeline(piece, voice=voice, speed=speed):
                        if cancelled() or producer_stop.is_set():
                            return
                        try:
                            audio = chunk[2]
                        except (IndexError, TypeError):
                            continue
                        if audio is None:
                            continue
                        if hasattr(audio, "detach"):
                            audio = audio.detach().cpu().numpy()
                        audio = np.ascontiguousarray(
                            audio, dtype=np.float32).reshape(-1, 1)
                        if audio.size == 0:
                            continue
                        while not (cancelled() or producer_stop.is_set()):
                            try:
                                audio_queue.put(audio, timeout=0.1)
                                break
                            except queue.Full:
                                continue
                        if cancelled() or producer_stop.is_set():
                            return
        except Exception as e:
            error = e
        finally:
            try:
                audio_queue.put_nowait(_AUDIO_SENTINEL)
            except queue.Full:
                # The consumer drains until it sees the producer exit.
                pass
            if error is not None:
                _report_error(f"speech failed: {error}")

    producer = threading.Thread(
        target=synthesize, daemon=True, name="TTS-Synth")
    producer.start()

    chunk_count = 0
    interrupted = False
    try:
        while True:
            if cancelled():
                interrupted = True
                break
            try:
                audio = audio_queue.get(timeout=0.1)
            except queue.Empty:
                if not producer.is_alive():
                    break
                continue
            if audio is _AUDIO_SENTINEL:
                break
            if cancelled():
                interrupted = True
                break
            audio_bus.publish(audio, "tts")
            with _stream_lock:
                if cancelled():
                    interrupted = True
                    break
                try:
                    stream.write(audio)
                except Exception:
                    if cancelled():
                        interrupted = True
                        break
                    raise
            chunk_count += 1
        if interrupted:
            print(f"[tts] interrupted ({chunk_count} chunks played)")
        else:
            print(f"[tts] done ({chunk_count} chunks played)")
    finally:
        producer_stop.set()
        producer.join(timeout=2.0)
        with _active_stream_lock:
            if _active_stream is stream:
                _active_stream = None
        _close_stream(stream)
        _output_idle.set()


def _ensure_worker():
    global _worker_started
    if _worker_started:
        return
    with _worker_lock:
        if _worker_started:
            return
        t = threading.Thread(target=_worker_loop, daemon=True, name="TTS-Worker")
        t.start()
        _worker_started = True


# =============================================================
# Public API
# =============================================================
def speak(text):
    """Queue text for speech. Returns True when the text was queued."""
    global _audio_error_reported, _last_error
    _last_error = ""
    if not text or not text.strip():
        return False

    usable, backend_error = _load_backend_status()
    if not usable:
        if backend_error:
            _report_error(backend_error)
        return False

    if not AUDIO_OK:
        if not _audio_error_reported:
            _audio_error_reported = True
            _report_error(
                "audio output unavailable \u2014 install kokoro, soundfile "
                "and sounddevice")
        return False

    cleaned = sanitize_for_speech(text)
    if not cleaned:
        return False

    _ensure_worker()
    with _generation_lock:
        generation = _speech_generation
    _speech_queue.put((generation, cleaned))
    return True


def _close_stream_locked(stream):
    """Stop and close one stream exactly once. Caller holds _stream_lock."""
    if getattr(stream, "_agentbot_closed", False):
        return
    try:
        stream._agentbot_closed = True
    except Exception:
        pass
    try:
        stream.stop()
    except Exception:
        pass
    try:
        stream.close()
    except Exception:
        pass


def _close_stream(stream):
    """Stop and close one playback stream under the PortAudio lock."""
    with _stream_lock:
        _close_stream_locked(stream)


def stop():
    """Interrupt current speech and clear the queue."""
    global _active_stream, _speech_generation
    _cancel_event.set()
    with _generation_lock:
        _speech_generation += 1
    with _active_stream_lock:
        stream = _active_stream
        _active_stream = None
    if stream is not None:
        # Wait for an in-flight write() to return before aborting. Closing a
        # PortAudio stream underneath its writer is an access violation, not a
        # Python exception (see crash.log).
        if _stream_lock.acquire(timeout=1.0):
            try:
                if not getattr(stream, "_agentbot_closed", False):
                    abort = getattr(stream, "abort", None)
                    if callable(abort):
                        try:
                            abort()
                        except Exception:
                            pass
                    _close_stream_locked(stream)
            finally:
                _stream_lock.release()
            with _active_stream_lock:
                if _active_stream is None:
                    _output_idle.set()
        else:
            # The writer will observe _cancel_event and close the stream in
            # its own finally block. Never abort underneath it.
            print("[tts] stop deferred: write still in flight")
    try:
        while True:
            _speech_queue.get_nowait()
            _speech_queue.task_done()
    except queue.Empty:
        pass
    _worker_busy.clear()


def is_speaking():
    """True while speech is queued or playing."""
    return _worker_busy.is_set() or not _speech_queue.empty()


def wait_until_idle(timeout=2.0):
    """Block until nothing is queued and the output stream is fully closed."""
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        if not is_speaking() and _output_idle.is_set():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.02, remaining))


def warmup():
    """Preload the pipeline in the background so the first reply is instant."""
    def _load():
        try:
            usable, backend_error = _load_backend_status()
            if not usable:
                if backend_error:
                    _report_error(backend_error)
                return
            voice, speed, lang, enabled = _load_tts_config()
            if not enabled:
                print("[tts] disabled — skipping warmup")
                return
            print("[tts] warming up...")
            _get_pipeline(lang)
        except Exception as e:
            _report_error(f"warmup failed: {e}")

    threading.Thread(target=_load, daemon=True, name="TTS-Warmup").start()


# =============================================================
# Self-test
# =============================================================
if __name__ == "__main__":
    print("Testing Kokoro TTS via worker thread...")
    print(f"audio backend: {AUDIO_OK}")

    speak("This is the first message.")
    speak("And this is the second one, spoken in order.")

    _speech_queue.join()
    print("Done.")