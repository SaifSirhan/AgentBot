"""
Text-to-speech via Kokoro-82M (local, offline, high quality).

- Persistent worker thread (safe from GUI daemon threads)
- Model warmup at app startup so first reply is instant
- Streaming playback: speaks the first sentence in ~1s, rest follows
- Auto-strips markdown so TTS doesn't read "asterisk asterisk"
- Splits long text into short chunks so CPU synthesis keeps up

Requires:
    pip install kokoro soundfile sounddevice
"""

import re
import queue
import threading
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
def _clean_for_speech(text):
    """Turn LLM markdown output into something a human would actually say aloud."""
    if not text:
        return ""

    s = text.strip()
    if s.startswith("AI: "):
        s = s[4:]

    # Strip [From: C:\...] source tags
    s = re.sub(r"\[From:[^\]]*\]", "", s)

    # Code blocks — replace with a spoken marker
    s = re.sub(r"```[a-zA-Z0-9_+-]*\n.*?```", " code block ", s, flags=re.DOTALL)

    # Inline code: keep words, drop backticks
    s = re.sub(r"`([^`]*)`", r"\1", s)

    # Markdown emphasis
    s = s.replace("**", "").replace("__", "").replace("~~", "")
    s = re.sub(r"(?<!\w)\*([^*\n]+)\*(?!\w)", r"\1", s)
    s = re.sub(r"(?<!\w)_([^_\n]+)_(?!\w)", r"\1", s)

    # Headers
    s = re.sub(r"^#{1,6}\s*", "", s, flags=re.MULTILINE)

    # Markdown links [text](url) -> text
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)

    # Bare URLs -> "link"
    s = re.sub(r"https?://\S+", "link", s)

    # Emojis
    s = re.sub(r"[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF]+", "", s)

    # Bullets, numbers, blockquotes, horizontal rules
    s = re.sub(r"^\s*[-*+]\s+", "", s, flags=re.MULTILINE)
    s = re.sub(r"^\s*\d+\.\s+", "", s, flags=re.MULTILINE)
    s = re.sub(r"^\s*>\s?", "", s, flags=re.MULTILINE)
    s = re.sub(r"^\s*[-*_]{3,}\s*$", "", s, flags=re.MULTILINE)

    # Any leftover stray markers
    s = s.replace("*", "").replace("#", "")

    # Whitespace
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{2,}", ". ", s)

    return s.strip()


def _split_for_tts(text, max_chars=90):
    """Split into small chunks — easier for Kokoro on CPU to keep up."""
    parts = re.split(r"(?<=[.!?])\s+", text)
    out = []
    for part in parts:
        if len(part) > max_chars:
            sub = re.split(r"(?<=[,;—])\s+", part)
            buf = ""
            for piece in sub:
                if len(buf) + len(piece) + 1 <= max_chars:
                    buf = (buf + " " + piece).strip()
                else:
                    if buf:
                        out.append(buf)
                    buf = piece
            if buf:
                out.append(buf)
        else:
            out.append(part)
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
            return None


# =============================================================
# Worker thread
# =============================================================
_speech_queue = queue.Queue()
_worker_started = False
_worker_lock = threading.Lock()
_worker_busy = threading.Event()


def _worker_loop():
    print("[tts] worker started")
    while True:
        try:
            text = _speech_queue.get()
        except Exception:
            continue
        if text is None:
            break
        _worker_busy.set()
        try:
            _speak_blocking(text)
        except Exception as e:
            _report_error(f"speech failed: {e}")
            import traceback
            traceback.print_exc()
        finally:
            _speech_queue.task_done()
            if _speech_queue.empty():
                _worker_busy.clear()


def _speak_blocking(text):
    """Stream audio: play each chunk as it's generated."""
    usable, backend_error = _load_backend_status()
    if not usable:
        if backend_error:
            _report_error(backend_error)
        return

    voice, speed, lang, enabled = _load_tts_config()
    if not enabled:
        print("[tts] disabled in settings")
        return

    cleaned = _clean_for_speech(text)
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

    chunk_count = 0
    try:
        for piece in _split_for_tts(cleaned):
            for chunk in pipeline(piece, voice=voice, speed=speed):
                try:
                    audio = chunk[2]
                except (IndexError, TypeError):
                    continue
                if audio is None:
                    continue
                if hasattr(audio, "detach"):
                    audio = audio.detach().cpu().numpy()
                audio = np.asarray(audio, dtype=np.float32).reshape(-1, 1)
                if audio.size == 0:
                    continue
                audio_bus.publish(audio, "tts")
                stream.write(audio)
                chunk_count += 1
        print(f"[tts] done ({chunk_count} chunks played)")
    finally:
        try:
            stream.stop()
            stream.close()
        except Exception:
            pass


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

    cleaned = _clean_for_speech(text)
    if not cleaned:
        return False

    _ensure_worker()
    _speech_queue.put(cleaned)
    return True


def stop():
    """Interrupt current speech and clear the queue."""
    try:
        while True:
            _speech_queue.get_nowait()
            _speech_queue.task_done()
    except queue.Empty:
        pass
    try:
        sd.stop()
    except Exception:
        pass


def is_speaking():
    """True while speech is queued or playing."""
    return _worker_busy.is_set() or not _speech_queue.empty()


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