"""
Central config — reads API keys from a JSON file the user edits via the GUI,
falling back to environment variables.

Location: %APPDATA%\\AgentBot\\config.json
"""
import os
import json

APP_DIR = os.path.join(os.environ.get("APPDATA", ""), "AgentBot")
CONFIG_PATH = os.path.join(APP_DIR, "config.json")

DEFAULTS = {
    "RAG_AUTO_INDEX_FOLDERS": [
        "C:\\Users\\USER\\Downloads\\Fire Writing",
        "C:\\Users\\USER\\Downloads\\AgentBot",
        "C:\\Users\\USER\\Downloads\\sistem-dc-v2"
        
    ],
    "GROQ_API_KEY": "",
    "GEMINI_API_KEY": "",
    "OPENROUTER_API_KEY": "",
    "CEREBRAS_API_KEY": "",
    "MISTRAL_API_KEY": "",
    "CLOUDFLARE_API_KEY": "",
    "CLOUDFLARE_ACCOUNT_ID": "",
    "COHERE_API_KEY": "",
    "HUGGINGFACE_API_KEY": "",
    "DEEPSEEK_API_KEY": "",
    "SERPAPI_KEY": "",
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
    "AGENT_BRAIN_PRIORITY": (
        "meta,groq,gemini,openrouter,cerebras,mistral,cloudflare,"
        "cohere,huggingface,deepseek,ollama"
    ),
    "OLLAMA_MODEL": "granite4.1:3b",
    "OLLAMA_URL": "http://localhost:11434/api/generate",
    
    "TTS_ENABLED": "true",
"TTS_AUTO_SPEAK": "true",
"TTS_VOICE": "af_heart",
"TTS_SPEED": "1.0",
"TTS_LANG": "a",
"TTS_MAX_CHARS": "2000",
# Phase 6c — GUI motion (pulses, fades, transitions). When false, every
# animated element renders in its final state immediately.
"ANIMATIONS_ENABLED": True,
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
    
    