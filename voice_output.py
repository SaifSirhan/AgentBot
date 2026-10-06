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


# =============================================================
# Pipeline cache
# =============================================================
_pipelines = {}
_pipeline_lock = threading.Lock()


def _get_pipeline(lang_code):
    if lang_code in _pipelines:
        return _pipelines[lang_code]

    with _pipeline_lock:
        if lang_code in _pipelines:
            return _pipelines[lang_code]
        try:
            from kokoro import KPipeline
            device = "cpu"
            try:
                import torch
                if torch.cuda.is_available():
                    device = "cuda"
            except Exception:
                pass
            print(f"[tts] loading pipeline (lang={lang_code}, device={device})")
            _pipelines[lang_code] = KPipeline(lang_code=lang_code, device=device)
            print("[tts] pipeline loaded")
        except Exception as e:
            print(f"[tts] could not create Kokoro pipeline: {e}")
            _pipelines[lang_code] = None
        return _pipelines[lang_code]


# =============================================================
# Worker thread
# =============================================================
_speech_queue = queue.Queue()
_worker_started = False
_worker_lock = threading.Lock()


def _worker_loop():
    print("[tts] worker started")
    while True:
        try:
            text = _speech_queue.get()
        except Exception:
            continue
        if text is None:
            break
        try:
            _speak_blocking(text)
        except Exception as e:
            print(f"[tts] speak failed: {e}")
            import traceback
            traceback.print_exc()
        _speech_queue.task_done()


def _speak_blocking(text):
    """Stream audio: play each chunk as it's generated."""
    voice, speed, lang, enabled = _load_tts_config()
    if not enabled:
        print("[tts] disabled in settings")
        return

    cleaned = _clean_for_speech(text)
    if not cleaned:
        print("[tts] nothing to speak after cleanup")
        return

    if len(cleaned) > 2000:
        cleaned = cleaned[:2000] + "..."

    print(f"[tts] generating ({len(cleaned)} chars, voice={voice}, speed={speed})")

    pipeline = _get_pipeline(lang)
    if pipeline is None:
        print("[tts] pipeline unavailable")
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
        print(f"[tts] could not open audio stream: {e}")
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
    """Queue text for speech. Returns immediately."""
    if not text or not text.strip():
        return
    if not AUDIO_OK:
        print("[tts] audio backend unavailable")
        return

    cleaned = _clean_for_speech(text)
    if not cleaned:
        return

    _ensure_worker()
    _speech_queue.put(cleaned)


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


def warmup():
    """Preload the pipeline in the background so the first reply is instant."""
    def _load():
        try:
            voice, speed, lang, enabled = _load_tts_config()
            if not enabled:
                print("[tts] disabled — skipping warmup")
                return
            print("[tts] warming up...")
            _get_pipeline(lang)
        except Exception as e:
            print(f"[tts] warmup failed: {e}")

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