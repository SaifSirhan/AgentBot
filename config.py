"""
Central config — reads API keys from a JSON file the user edits via the GUI,
falling back to environment variables.

Location: %APPDATA%\\Kestrel\\config.json
"""
import os
import json

APP_DIR = os.path.join(os.environ.get("APPDATA", ""), "Kestrel")
CONFIG_PATH = os.path.join(APP_DIR, "config.json")

DEFAULTS = {
    "TRIGGERS": [],
    "RAG_AUTO_INDEX_FOLDERS": [
        
        
        
    ],
    # Where conversation/facts/mood/RAG/state live. Empty = %LOCALAPPDATA%\Kestrel\AgentMemory.
    # Point it at a dedicated drive (e.g. D:\AgentBrain); the environment
    # variable of the same name overrides the file.
    "AGENT_MEMORY_DIR": "",
    # One client: deepseek-flash serves text, vision and search/fetch.
    "DEEPSEEK_API_KEY": "",
    "CRAWLBASE_KEY": "",
    "FIRECRAWL_KEY": "",
    "TYPEFULLY_KEY": "",
    "INSTAGRAM_ENABLED": False,
    "INSTAGRAM_USERNAME": "",
    "INSTAGRAM_PASSWORD": "",
    "INSTAGRAM_PROXY": "",
    "INSTAGRAM_POLL_INTERVAL": 15,
    "TELEGRAM_BOT_TOKEN": "",
    "TELEGRAM_USER_ID": "",
    "TELEGRAM_API_ID": "",
    "TELEGRAM_API_HASH": "",
    
    # TTS backend: "kokoro" (local). "none" disables speech output.
    "TTS_BACKEND": "kokoro",
    "TTS_ENABLED": "true",
    "TTS_AUTO_SPEAK": "true",
    "TTS_VOICE": "af_heart",
    "TTS_SPEED": "1.0",
    "TTS_LANG": "a",
    "TTS_MAX_CHARS": "0",
# Section 4 — restricted browser agent. Disabled until both the flag and at
# least one HTTPS origin allowlist entry are set. Vision sends screenshots to
# an external model, so it stays off until explicitly enabled.
"BROWSER_ENABLED": True,
"BROWSER_ALLOWED_ORIGINS": "https://example.com,https://quotes.toscrape.com",
"BROWSER_HEADLESS": False,
"BROWSER_MAX_STEPS": 15,
"BROWSER_TASK_TIMEOUT": 180,
"BROWSER_APPROVAL_TIMEOUT": 120,
"BROWSER_VISION_ENABLED": False,
"BROWSER_VISION_MODEL": "deepseek-flash",
# Option 3 — wake word. Off by default; enable only after installing
# openwakeword/pyaudio and providing a custom "vex" model (none is
# pre-trained).
    "WAKE_WORD_ENABLED": True,
    "WAKE_WORD_MODEL": "vex",
    "WAKE_WORD_THRESHOLD": 0.5,
    "WAKE_WORD_DEVICE": "",
    "WAKE_WORD_COOLDOWN": 1.5,
    # Optional STT microphone index or name. Empty = auto-pick a physical
    # microphone, skipping VB-Audio/CABLE and other loopback devices.
    "MIC_DEVICE": "",
    # How long a wake-word trigger listens before auto-releasing F9.
    "WAKE_RECORD_MS": 3500,
    # Hands-free loop: after each reply the mic reopens without the wake
    # word. 60s of silence pauses it and re-arms the wake word. /continuous
    # toggles it at runtime.
    "CONTINUOUS_LISTENING": False,
# Option A — proactive thoughts. Off by default: when enabled, a screenshot
# is sent to the vision model every two hours.
"SPONTANEOUS_THOUGHTS_ENABLED": False,
# Phase 6c — GUI motion (pulses, fades, transitions). When false, every
# animated element renders in its final state immediately.
"ANIMATIONS_ENABLED": True,
# GUI theme file under themes/ (holographic_dark | brutalist | crt_green).
"GUI_THEME": "holographic_dark",
}


def _ensure_dir():
    try:
        os.makedirs(APP_DIR, exist_ok=True)
    except Exception:
        pass


def load_config():
    """Return dict of config values. Missing keys use DEFAULTS."""
    _ensure_dir()
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    # Env vars override file (so power users can keep using setx)
    for k in list(cfg.keys()):
        env_val = os.environ.get(k)
        if env_val:
            cfg[k] = env_val
    return cfg


def save_config(cfg):
    """Write dict to disk."""
    _ensure_dir()
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        return True, None
    except Exception as e:
        return False, str(e)


def get(key, default=""):
    return load_config().get(key, default)


DEFAULT_MEMORY_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
    "Kestrel", "AgentMemory",
)


def memory_dir():
    """Return the active AgentMemory directory, creating it when possible."""
    configured = (load_config().get("AGENT_MEMORY_DIR") or "").strip()
    if configured:
        path = os.path.abspath(os.path.expandvars(os.path.expanduser(configured)))
    else:
        path = DEFAULT_MEMORY_DIR
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass
    return path


def memory_path(*parts):
    """Return a path inside the active AgentMemory directory."""
    return os.path.join(memory_dir(), *parts)
    
    