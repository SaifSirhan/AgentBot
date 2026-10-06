import subprocess
import os
import difflib
import json
import re
import requests
import webbrowser
import time
import shutil
import datetime
import urllib.parse
import winsound
import weather_news
import whatsapp_tool
from bs4 import BeautifulSoup
from PIL import ImageGrab
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
import pyautogui
import watcher
import gmail_tool
import scheduler
import memory
import screen
import air_quality as air_quality_tool
import threading as _threading
import telegram_user

# MODULE GUIDE
# 1. Configuration, persistent state, and LLM provider failover
# 2. Tool parsing, platform tools, integrations, and browser helpers
# 3. Prompt/tool registries, tool dispatch, and agent-turn orchestration

# ---------------------------
# PERSISTENT CONVERSATION
# ---------------------------
CONVERSATION_FILE = os.path.join(
    os.environ.get('LOCALAPPDATA', ''), 'AgentMemory', 'conversation.json'
)
MAX_HISTORY_ENTRIES = 40


def load_conversation_history():
    if not os.path.exists(CONVERSATION_FILE):
        return []
    try:
        with open(CONVERSATION_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data.get('history', [])[-MAX_HISTORY_ENTRIES:]
    except (json.JSONDecodeError, OSError):
        return []


def save_conversation_history(history):
    try:
        os.makedirs(os.path.dirname(CONVERSATION_FILE), exist_ok=True)
        with open(CONVERSATION_FILE, 'w', encoding='utf-8') as f:
            json.dump({'history': history[-MAX_HISTORY_ENTRIES:]}, f, indent=2)
    except Exception as e:
        print(f"[conversation save error] {e}")


# ---------------------------
# OPTIONAL DEPENDENCIES
# ---------------------------
try:
    import pyperclip
    PYPERCLIP_AVAILABLE = True
except ImportError:
    PYPERCLIP_AVAILABLE = False

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

try:
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    from comtypes import CLSCTX_ALL
    from ctypes import cast, POINTER
    PYCAW_AVAILABLE = True
except ImportError:
    PYCAW_AVAILABLE = False

try:
    import pygetwindow as gw
    PYGETWINDOW_AVAILABLE = True
except ImportError:
    PYGETWINDOW_AVAILABLE = False

try:
    from winotify import Notification, audio
    WINOTIFY_AVAILABLE = True
except ImportError:
    WINOTIFY_AVAILABLE = False


# ---------------------------
# CONFIG — load user config once at import time
# ---------------------------
import config as _cfg
_c = _cfg.load_config()

# ---------------------------
# BRAIN CONFIGURATION (10-provider failover)
# ---------------------------
_default_priority = _c.get(
    "AGENT_BRAIN_PRIORITY",
    "deepseek,groq,gemini,openrouter,mistral,huggingface,meta,cohere,ollama",
)
BRAIN_PRIORITY = [
    b.strip().lower()
    for b in _default_priority.split(",")
    if b.strip()
]
print(f"[BRAIN] Priority: {' -> '.join(BRAIN_PRIORITY)}")
LAST_BRAIN = None

# ---------- Credentials and model endpoints ----------
GROQ_API_KEY        = _c.get("GROQ_API_KEY", "")
GEMINI_API_KEY      = _c.get("GEMINI_API_KEY", "")
OPENROUTER_API_KEY  = _c.get("OPENROUTER_API_KEY", "")
CEREBRAS_API_KEY    = _c.get("CEREBRAS_API_KEY", "")
MISTRAL_API_KEY     = _c.get("MISTRAL_API_KEY", "")
CLOUDFLARE_API_KEY  = _c.get("CLOUDFLARE_API_KEY", "")
CLOUDFLARE_ACCOUNT_ID = _c.get("CLOUDFLARE_ACCOUNT_ID", "")
COHERE_API_KEY      = _c.get("COHERE_API_KEY", "")
HUGGINGFACE_API_KEY = _c.get("HUGGINGFACE_API_KEY", "")
DEEPSEEK_API_KEY    = _c.get("DEEPSEEK_API_KEY", "")
META_AI_API_KEY     = os.environ.get("META_AI_API_KEY", "") or _c.get("META_AI_API_KEY", "")

OLLAMA_URL   = _c.get("OLLAMA_URL", "http://localhost:11434/api/generate")
OLLAMA_MODEL = _c.get("OLLAMA_MODEL", "granite4.1:3b")

TELEGRAM_TOKEN   = _c.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_USER_ID = _c.get("TELEGRAM_USER_ID", "")
# ---------- Model names ----------
GROQ_MODEL        = "openai/gpt-oss-120b"
GEMINI_MODEL      = "gemini-flash-latest"
OPENROUTER_MODEL  = "nvidia/nemotron-3-super-120b-a12b:free"
CEREBRAS_MODEL    = "llama-3.3-70b"
MISTRAL_MODEL     = "mistral-small-latest"
CLOUDFLARE_MODEL  = "@cf/meta/llama-3.3-70b-instruct-fp8-fast"
COHERE_MODEL      = "command-r-plus-08-2024"
HUGGINGFACE_MODEL = "meta-llama/Llama-3.3-70B-Instruct"
DEEPSEEK_MODEL    = "deepseek-flash"
META_AI_MODEL     = "muse-spark-1.3"

# ---------- Ollama (local) ----------
# OLLAMA_URL / OLLAMA_MODEL are read from config.json above; don't reassign here.
KEEP_ALIVE = "30m"

# ---------- Common ----------
OLLAMA_TIMEOUT = 120
MAX_PAGE_CHARS = 6000
MAX_STEPS_PER_TURN = 5
MODEL = BRAIN_PRIORITY[0] if BRAIN_PRIORITY else "ollama"

# ---------- Cooldown state per brain ----------
_brain_lock = _threading.Lock()
_brain_state = {}


# ---------------------------
# SESSION MEMORY
# ---------------------------
last_search_query = ""
last_search_result = ""

# ---------------------------
# GROUP MODE
# ---------------------------
GROUP_MODE = False

# Tools permitted in group chats. Everything else — RAG search, file
# read/write, code editing, shell commands, memory writes, Telegram
# sends as the user — is blocked. Groups get information-only tools.
SAFE_GROUP_TOOLS = {
    "chat", "search_web", "weather", "air_quality", "news",
    "current_time", "show_last_result", "read_and_summarize",
    "generate_image", "list_reminders", "list_brains",
}

KNOWN_WEB_APPS = {
    "youtube": "https://youtube.com",
    "canva": "https://canva.com",
    "notion": "https://notion.so",
    "figma": "https://figma.com",
    "spotify": "https://open.spotify.com",
    "gmail": "https://mail.google.com",
    "google docs": "https://docs.google.com",
    "google sheets": "https://sheets.google.com",
    "whatsapp": "https://web.whatsapp.com",
    "discord": "https://discord.com/app",
    "netflix": "https://netflix.com",
    "chatgpt": "https://chat.openai.com",
    "reddit": "https://reddit.com",
    "twitter": "https://twitter.com",
    "github": "https://github.com",
    "stackoverflow": "https://stackoverflow.com",
}

TODO_FILE = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'AgentMemory', 'todos.json')


# ---------------------------
# HELPERS
# ---------------------------
def get_downloads_path():
    return os.path.join(os.environ['USERPROFILE'], 'Downloads')


def normalize_url(url):
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


# ---------------------------
# PROVIDER CALLS — one per API
# ---------------------------
def _call_meta(prompt, force_json, max_tokens):
    if not META_AI_API_KEY:
        return "Error: META_AI_API_KEY not set"
    try:
        body = {
            "model": META_AI_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
        }
        if force_json:
            body["response_format"] = {"type": "json_object"}
        if max_tokens:
             body["max_tokens"] = max(max_tokens, 2048)

        r = requests.post(
            "https://api.meta.ai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {META_AI_API_KEY}",
                "Content-Type": "application/json",
            },
            json=body, timeout=OLLAMA_TIMEOUT
        )

        if r.status_code != 200:
            return f"Error: HTTP {r.status_code}: {r.text[:200]}"

        # Meta's API can return either an OpenAI-style response or a Meta-style one.
        # We handle both just in case.
        data = r.json()

        # OpenAI-style response (what Muse Spark actually returns)
        try:
            content = data["choices"][0]["message"].get("content")
            if content:
                return content.strip()
        except (KeyError, IndexError, TypeError):
            pass

        # Sometimes the API returns refusal instead of content
        try:
            refusal = data["choices"][0]["message"].get("refusal")
            if refusal:
                return f"Model refused: {refusal}"
        except (KeyError, IndexError, TypeError):
            pass

        # Meta-native shape (fallback, in case they change it)
        try:
            content = data["completion_message"]["content"]["text"]
            if content:
                return content.strip()
        except (KeyError, IndexError, TypeError):
            pass

        # Nothing worked — dump the whole thing so we can diagnose
        import json as _json
        print(f"[meta] unexpected response: {_json.dumps(data)[:500]}")
        return f"Error: could not parse Meta response: {str(data)[:200]}"
    except Exception as e:
        return f"Error: {e}"


def _call_ollama(prompt, force_json, max_tokens):
    try:
        payload = {
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "keep_alive": KEEP_ALIVE,
        }
        if force_json:
            payload["format"] = "json"
        if max_tokens:
            payload["options"] = {"num_predict": max_tokens}
        r = requests.post(OLLAMA_URL, json=payload, timeout=OLLAMA_TIMEOUT)
        if r.status_code == 200:
            return r.json().get("response", "").strip()
        return f"Error: HTTP {r.status_code}"
    except Exception as e:
        return f"Error: {e}"


def _call_gemini(prompt, force_json, max_tokens):
    if not GEMINI_API_KEY:
        return "Error: GEMINI_API_KEY not set"
    try:
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}")
        body = {"contents": [{"parts": [{"text": prompt}]}]}
        if force_json:
            body["generationConfig"] = {"responseMimeType": "application/json"}
        if max_tokens:
            body.setdefault("generationConfig", {})["maxOutputTokens"] = max_tokens
        r = requests.post(url, json=body, timeout=OLLAMA_TIMEOUT)
        if r.status_code != 200:
            return f"Error: HTTP {r.status_code}: {r.text[:200]}"
        data = r.json()
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except (KeyError, IndexError):
            return f"Error: unexpected response: {str(data)[:200]}"
    except Exception as e:
        return f"Error: {e}"


def _call_groq(prompt, force_json, max_tokens):
    if not GROQ_API_KEY:
        return "Error: GROQ_API_KEY not set"
    try:
        body = {
            "model": GROQ_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
        }
        if force_json:
            body["response_format"] = {"type": "json_object"}
        if max_tokens:
            body["max_tokens"] = max_tokens
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}",
                     "Content-Type": "application/json"},
            json=body, timeout=OLLAMA_TIMEOUT
        )
        if r.status_code != 200:
            return f"Error: HTTP {r.status_code}: {r.text[:200]}"
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"Error: {e}"


def _call_openrouter(prompt, force_json, max_tokens):
    if not OPENROUTER_API_KEY:
        return "Error: OPENROUTER_API_KEY not set"
    try:
        body = {
            "model": OPENROUTER_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
        }
        if force_json:
            body["response_format"] = {"type": "json_object"}
        if max_tokens:
            body["max_tokens"] = max_tokens
        r = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://t.me/benjaminnethayahubot",
                "X-Title": "AgentBot",
            },
            json=body, timeout=OLLAMA_TIMEOUT
        )
        if r.status_code != 200:
            return f"Error: HTTP {r.status_code}: {r.text[:200]}"
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"Error: {e}"


def _call_cerebras(prompt, force_json, max_tokens):
    if not CEREBRAS_API_KEY:
        return "Error: CEREBRAS_API_KEY not set"
    try:
        body = {
            "model": CEREBRAS_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
        }
        if force_json:
            body["response_format"] = {"type": "json_object"}
        if max_tokens:
            body["max_tokens"] = max_tokens
        r = requests.post(
            "https://api.cerebras.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {CEREBRAS_API_KEY}",
                     "Content-Type": "application/json"},
            json=body, timeout=OLLAMA_TIMEOUT
        )
        if r.status_code != 200:
            return f"Error: HTTP {r.status_code}: {r.text[:200]}"
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"Error: {e}"


def _call_mistral(prompt, force_json, max_tokens):
    if not MISTRAL_API_KEY:
        return "Error: MISTRAL_API_KEY not set"
    try:
        body = {
            "model": MISTRAL_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
        }
        if force_json:
            body["response_format"] = {"type": "json_object"}
        if max_tokens:
            body["max_tokens"] = max_tokens
        r = requests.post(
            "https://api.mistral.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {MISTRAL_API_KEY}",
                     "Content-Type": "application/json"},
            json=body, timeout=OLLAMA_TIMEOUT
        )
        if r.status_code != 200:
            return f"Error: HTTP {r.status_code}: {r.text[:200]}"
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"Error: {e}"


def _call_cloudflare(prompt, force_json, max_tokens):
    if not CLOUDFLARE_API_KEY:
        return "Error: CLOUDFLARE_API_KEY not set"
    if not CLOUDFLARE_ACCOUNT_ID:
        return "Error: CLOUDFLARE_ACCOUNT_ID not set"
    try:
        url = (f"https://api.cloudflare.com/client/v4/accounts/"
               f"{CLOUDFLARE_ACCOUNT_ID}/ai/v1/chat/completions")
        body = {
            "model": CLOUDFLARE_MODEL,
            "messages": [{"role": "user", "content": prompt}],
        }
        if max_tokens:
            body["max_tokens"] = max_tokens
        r = requests.post(
            url,
            headers={"Authorization": f"Bearer {CLOUDFLARE_API_KEY}",
                     "Content-Type": "application/json"},
            json=body, timeout=OLLAMA_TIMEOUT
        )
        if r.status_code != 200:
            return f"Error: HTTP {r.status_code}: {r.text[:200]}"
        data = r.json()
        try:
            return data["result"]["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError):
            try:
                return data["choices"][0]["message"]["content"].strip()
            except (KeyError, IndexError):
                return f"Error: unexpected response: {str(data)[:200]}"
    except Exception as e:
        return f"Error: {e}"


def _call_cohere(prompt, force_json, max_tokens):
    if not COHERE_API_KEY:
        return "Error: COHERE_API_KEY not set"
    try:
        body = {
            "model": COHERE_MODEL,
            "messages": [{"role": "user", "content": prompt}],
        }
        if max_tokens:
            body["max_tokens"] = max_tokens
        r = requests.post(
            "https://api.cohere.com/v2/chat",
            headers={"Authorization": f"Bearer {COHERE_API_KEY}",
                     "Content-Type": "application/json"},
            json=body, timeout=OLLAMA_TIMEOUT
        )
        if r.status_code != 200:
            return f"Error: HTTP {r.status_code}: {r.text[:200]}"
        data = r.json()
        try:
            return data["message"]["content"][0]["text"].strip()
        except (KeyError, IndexError, TypeError):
            return f"Error: unexpected response: {str(data)[:200]}"
    except Exception as e:
        return f"Error: {e}"


def _call_huggingface(prompt, force_json, max_tokens):
    if not HUGGINGFACE_API_KEY:
        return "Error: HUGGINGFACE_API_KEY not set"
    try:
        body = {
            "model": HUGGINGFACE_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
        }
        if max_tokens:
            body["max_tokens"] = max_tokens
        r = requests.post(
            "https://router.huggingface.co/v1/chat/completions",
            headers={"Authorization": f"Bearer {HUGGINGFACE_API_KEY}",
                     "Content-Type": "application/json"},
            json=body, timeout=OLLAMA_TIMEOUT
        )
        if r.status_code != 200:
            return f"Error: HTTP {r.status_code}: {r.text[:200]}"
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"Error: {e}"


def _call_deepseek(prompt, force_json, max_tokens):
    if not DEEPSEEK_API_KEY:
        return "Error: DEEPSEEK_API_KEY not set"
    try:
        body = {
            "model": DEEPSEEK_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
        }
        # DeepSeek V4 Flash reasons by default, which can burn the whole
        # max_tokens budget on reasoning_content and return empty content.
        # Disable it for normal tool-calling turns. NOTE: these are TOP-LEVEL
        # REST params — `extra_body` is an OpenAI-SDK concept and is ignored
        # here because we post raw JSON with `requests`.
        body["reasoning_effort"] = "none"
        body["thinking"] = {"type": "disabled"}
        if force_json:
            body["response_format"] = {"type": "json_object"}
        if max_tokens:
            body["max_tokens"] = max_tokens
        r = requests.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                     "Content-Type": "application/json"},
            json=body, timeout=OLLAMA_TIMEOUT
        )
        if r.status_code != 200:
            return f"Error: HTTP {r.status_code}: {r.text[:200]}"
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"Error: {e}"


def _call_deepseek_thinking(prompt, force_json=False, max_tokens=4096):
    """DeepSeek V4 Flash with thinking ENABLED at high effort.

    Used for complex reasoning (e.g. deep_research synthesis), where the
    reasoning budget is worth it. Top-level REST params; see _call_deepseek.
    """
    if not DEEPSEEK_API_KEY:
        return "Error: DEEPSEEK_API_KEY not set"
    try:
        body = {
            "model": DEEPSEEK_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "reasoning_effort": "high",
            "thinking": {"type": "enabled"},
        }
        if force_json:
            body["response_format"] = {"type": "json_object"}
        if max_tokens:
            body["max_tokens"] = max_tokens
        r = requests.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                     "Content-Type": "application/json"},
            json=body, timeout=OLLAMA_TIMEOUT
        )
        if r.status_code != 200:
            return f"Error: HTTP {r.status_code}: {r.text[:200]}"
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"Error: {e}"


def _call_openai(prompt, force_json, max_tokens):
    if not os.environ.get("OPENAI_API_KEY", ""):
        return "Error: OPENAI_API_KEY not set"
    try:
        body = {
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": prompt}],
        }
        if force_json:
            body["response_format"] = {"type": "json_object"}
        if max_tokens:
            body["max_tokens"] = max_tokens
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}",
                     "Content-Type": "application/json"},
            json=body, timeout=OLLAMA_TIMEOUT
        )
        if r.status_code != 200:
            return f"Error: HTTP {r.status_code}: {r.text[:200]}"
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"Error: {e}"


# ---------------------------
# FAILOVER DISPATCHER
# ---------------------------
def _get_brain_state(name):
    if name not in _brain_state:
        _brain_state[name] = {"cooldown_until": 0.0, "dead": False}
    return _brain_state[name]


def _mark_cooled(name, seconds, reason=""):
    with _brain_lock:
        st = _get_brain_state(name)
        st["cooldown_until"] = time.time() + seconds
        print(f"[BRAIN] {name} cooling {seconds}s — {reason[:80]}")


def _mark_dead(name, reason=""):
    with _brain_lock:
        st = _get_brain_state(name)
        st["dead"] = True
        print(f"[BRAIN] {name} marked DEAD — {reason[:80]}")


def _classify_error(err_text):
    t = (err_text or "").lower()
    if "not set" in t or "no module" in t:
        return (0, True)
    if "401" in t or "unauthorized" in t or "invalid key" in t or "invalid api" in t:
        return (0, True)
    if "404" in t or "not found" in t or "does not exist" in t:
        return (0, True)
    if "429" in t or "rate limit" in t or "too many requests" in t or "quota" in t:
        return (60, False)
    if "402" in t or "payment" in t or "insufficient" in t or "credits" in t or "exceeded" in t:
        return (3600, False)
    if "403" in t or "forbidden" in t or "access" in t:
        return (1800, False)
    if any(x in t for x in ["500", "502", "503", "504", "server error"]):
        return (30, False)
    if "timeout" in t or "timed out" in t:
        return (30, False)
    if "connection" in t or "connect" in t:
        return (30, False)
    return None


def _dispatch_call(name, prompt, force_json, max_tokens):
    if name == "ollama":        return _call_ollama(prompt, force_json, max_tokens)
    elif name == "gemini":      return _call_gemini(prompt, force_json, max_tokens)
    elif name == "groq":        return _call_groq(prompt, force_json, max_tokens)
    elif name == "openrouter":  return _call_openrouter(prompt, force_json, max_tokens)
    elif name == "cerebras":    return _call_cerebras(prompt, force_json, max_tokens)
    elif name == "mistral":     return _call_mistral(prompt, force_json, max_tokens)
    elif name == "cloudflare":  return _call_cloudflare(prompt, force_json, max_tokens)
    elif name == "cohere":      return _call_cohere(prompt, force_json, max_tokens)
    elif name == "huggingface": return _call_huggingface(prompt, force_json, max_tokens)
    elif name == "deepseek":    return _call_deepseek(prompt, force_json, max_tokens)
    elif name == "openai":      return _call_openai(prompt, force_json, max_tokens)
    elif name == "meta":        return _call_meta(prompt, force_json, max_tokens)
    else:
        return f"Error: unknown brain '{name}'"


def _call_llm(prompt, force_json=False, max_tokens=None):
    """Try each brain in priority order until one succeeds."""
    global LAST_BRAIN
    tried = []
    now = time.time()

    for name in BRAIN_PRIORITY:
        with _brain_lock:
            st = _get_brain_state(name)
            if st["dead"]:
                tried.append(f"{name}(dead)")
                continue
            if st["cooldown_until"] > now:
                secs = int(st["cooldown_until"] - now)
                tried.append(f"{name}(cooling {secs}s)")
                continue

        try:
            result = _dispatch_call(name, prompt, force_json, max_tokens)
        except Exception as e:
            result = f"Error: {e}"

        if isinstance(result, str) and result.startswith("Error:"):
            cls = _classify_error(result)
            if cls:
                cooldown, dead = cls
                if dead:
                    _mark_dead(name, result)
                elif cooldown > 0:
                    _mark_cooled(name, cooldown, result)
            tried.append(f"{name}({result[:40]})")
            continue

        LAST_BRAIN = name
        print(f"[BRAIN] OK: {name}")
        return result

    LAST_BRAIN = None
    return (
        "Error: All brains unavailable. "
        f"Tried: {', '.join(tried) if tried else 'none'}. "
        "Check that at least one API key is set and valid."
    )


def list_brains():
    now = time.time()
    lines = ["Brain status (in priority order):"]
    for name in BRAIN_PRIORITY:
        st = _get_brain_state(name)
        if st["dead"]:
            lines.append(f"  * {name:12s} - DEAD")
        elif st["cooldown_until"] > now:
            secs = int(st["cooldown_until"] - now)
            lines.append(f"  * {name:12s} - cooling ({secs}s)")
        else:
            lines.append(f"  * {name:12s} - ready")
    return "\n".join(lines)


def get_last_brain():
    """Return the name of the provider that last answered, or None."""
    return LAST_BRAIN


# ---------------------------
# PUBLIC WRAPPERS
# ---------------------------
def ask_ai_for_plan(user_request, context, correction=None):
    correction_block = (f"\n\nYour previous response was invalid: {correction}\n"
                        f"Respond with ONLY a corrected JSON object.") if correction else ""

    known_facts = memory.get_facts_block()
    facts_block = f"\nKnown facts about the user:\n{known_facts}\n" if known_facts else ""

    now_str = datetime.datetime.now().strftime("%A, %B %d, %Y — %I:%M %p")
    time_block = f"\nCURRENT DATE AND TIME: {now_str} (use this; never guess)\n"

    group_block = ""
    if GROUP_MODE:
        group_block = (
            "\n=== GROUP MODE ACTIVE ===\n"
            "This message came from a GROUP CHAT, not a private DM.\n\n"
            f"ALLOWED TOOLS: {', '.join(sorted(SAFE_GROUP_TOOLS))}\n\n"
            "BLOCKED in groups:\n"
            "- rag_search (the user's personal indexed documents)\n"
            "- read_file, write_file, patch_file, find_files, move_files\n"
            "- run_command, list_symbols, repo_map\n"
            "- read_screen, describe_screen, clipboard_read\n"
            "- remember, forget (personal memory writes)\n"
            "- telegram_user_send, telegram_user_delete, telegram_user_edit\n"
            "- describe_image, send_image_telegram\n"
            "- All system tools (volume, media, lock, shutdown)\n\n"
            "If a user asks for any blocked capability, reply briefly: you "
            "can't do that in a group, DM the bot directly. Do NOT attempt "
            "to call a blocked tool — you'll just get an error.\n"
            "=== END GROUP MODE ===\n"
        )

    prompt = f"""{TOOL_DESCRIPTIONS}
{group_block}{time_block}{facts_block}
Context (recent conversation and tool results):
{context}

User: {user_request}{correction_block}

Your response (either a plain-text reply or a single JSON tool call):"""

    return _call_llm(prompt, force_json=False)


def ask_llm_direct(prompt, max_tokens=None):
    return _call_llm(prompt, force_json=False, max_tokens=max_tokens)


# ---------------------------
# PARSING + VALIDATION
# ---------------------------
def _extract_json_actions(text):
    """Extract all valid {tool, input} JSON objects from text (one per line or a list)."""
    actions = []

    # Try the whole text as a single JSON value first
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and 'tool' in parsed:
            return [parsed]
        if isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, dict) and 'tool' in item:
                    actions.append(item)
            if actions:
                return actions
    except json.JSONDecodeError:
        pass

    # Fall back: scan line by line
    for line in text.split("\n"):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
            if isinstance(parsed, dict) and 'tool' in parsed:
                actions.append(parsed)
        except json.JSONDecodeError:
            continue

    return actions


def parse_action(plan_text):
    raw = (plan_text or "").strip()

    # Strip code fences the model sometimes adds around JSON
    if raw.startswith("```"):
        body = raw.strip("`").strip()
        first_line, _, rest = body.partition("\n")
        if first_line.strip().lower() in ("json", "python", ""):
            raw = rest.strip() if rest.strip() else body
        else:
            raw = body

    if not raw:
        return None, "empty response"

    # Try to extract one or more JSON tool calls
    actions = _extract_json_actions(raw)
    if actions:
        # Normalize the first action
        first = dict(actions[0])
        if 'action' in first and 'tool' not in first:
            first['tool'] = first.pop('action')

        tool = first.get('tool')
        if tool not in VALID_TOOLS:
            return None, (f"'{tool}' is not a valid tool name. "
                          f"Valid: {', '.join(sorted(VALID_TOOLS))}")
        first.setdefault('input', '')

        # If there are more, queue them on the action
        if len(actions) > 1:
            queued = []
            for a in actions[1:]:
                a = dict(a)
                if 'action' in a and 'tool' not in a:
                    a['tool'] = a.pop('action')
                if a.get('tool') in VALID_TOOLS:
                    a.setdefault('input', '')
                    queued.append(a)
            if queued:
                first['_queued_actions'] = queued

        return first, None

    # No JSON → plain chat reply
    return {"tool": "chat", "input": raw}, None


def get_valid_action(user_request, context, max_retries=2):
    correction = None
    for attempt in range(max_retries + 1):
        plan_text = ask_ai_for_plan(user_request, context, correction=correction)
        print(f"\nRaw model output (attempt {attempt + 1}):", plan_text[:300])

        action, error = parse_action(plan_text)
        if action is not None:
            return action, None

        print(f"Invalid response: {error}")
        correction = error

    return None, f"Model failed after {max_retries + 1} attempts."


# ---------------------------
# FILE TOOLS
# ---------------------------
DANGEROUS_COMMAND_PATTERNS = [
    r'\bdel\b.*[/\\]\S|\bdel\b.*\*',
    r'\brmdir\b.*/s',
    r'\brm\b.*-rf',
    r'\bformat\b\s+[a-zA-Z]:',
    r'\bshutdown\b',
    r'diskpart',
    r'\breg\b\s+delete',
    r'>\s*[a-zA-Z]:\\',
]


def run_command(command):
    for pattern in DANGEROUS_COMMAND_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return (f"ERROR: Refused to run '{command}' — matches a dangerous pattern. "
                    f"Use a dedicated tool if that's what you meant.")
    try:
        result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30)
        return result.stdout if result.stdout else result.stderr
    except Exception as e:
        return f"ERROR: {e}"


def _iter_folders(root, max_depth=2):
    root = root.rstrip(os.sep)
    if not os.path.isdir(root):
        return
    base_depth = root.count(os.sep)
    for current_dir, dirnames, _ in os.walk(root):
        depth = current_dir.count(os.sep) - base_depth
        if depth >= max_depth:
            dirnames[:] = []
        for d in dirnames:
            yield d, os.path.join(current_dir, d)


def find_and_open_folder(name):
    search_roots = [
        get_downloads_path(),
        os.path.join(os.environ.get('USERPROFILE', ''), 'Documents'),
        os.path.join(os.environ.get('USERPROFILE', ''), 'Desktop'),
        os.path.join(os.environ.get('USERPROFILE', ''), 'Pictures'),
        os.environ.get('USERPROFILE', ''),
    ]
    name_lower = name.lower().strip()
    all_folders = []
    exact_matches = []
    for root in search_roots:
        try:
            for folder_name, full_path in _iter_folders(root):
                all_folders.append((folder_name, full_path))
                if folder_name.lower() == name_lower:
                    exact_matches.append(full_path)
        except PermissionError:
            continue
    if exact_matches:
        target = exact_matches[0]
    else:
        names_only = [n for n, _ in all_folders]
        close = difflib.get_close_matches(name, names_only, n=1, cutoff=0.5)
        if close:
            match_name = close[0]
            target = next(p for n, p in all_folders if n == match_name)
        else:
            substring_hits = [p for n, p in all_folders if name_lower in n.lower()]
            if substring_hits:
                target = substring_hits[0]
            else:
                return (f"ERROR: No folder matching '{name}' found.")
    try:
        os.startfile(target)
        return f"Opened folder: {target}"
    except Exception as e:
        return f"ERROR: Found '{target}' but could not open: {e}"


def make_folder(folder_name):
    try:
        folder_path = os.path.join(get_downloads_path(), folder_name) if not os.path.isabs(folder_name) else folder_name
        os.makedirs(folder_path, exist_ok=True)
        return f"Folder created: {folder_path}"
    except Exception as e:
        return f"ERROR: {e}"
# ---------------------------
# FILE SEARCH + MOVE
# ---------------------------
import shutil as _shutil


def _resolve_folder(name):
    """Turn shortcuts like 'downloads', 'telegram desktop' into full paths."""
    if not name:
        return None
    n = name.strip().lower().replace("\\", "/")
    user = os.environ.get("USERPROFILE", "")
    aliases = {
        "downloads": os.path.join(user, "Downloads"),
        "desktop": os.path.join(user, "Desktop"),
        "documents": os.path.join(user, "Documents"),
        "pictures": os.path.join(user, "Pictures"),
        "videos": os.path.join(user, "Videos"),
        "music": os.path.join(user, "Music"),
        "telegram desktop": os.path.join(user, "Downloads", "Telegram Desktop"),
        "telegram": os.path.join(user, "Downloads", "Telegram Desktop"),
        "fire writing": os.path.join(user, "Downloads", "Fire Writing"),
    }
    if n in aliases:
        return aliases[n]

    # fuzzy match on the alias keys
    close = difflib.get_close_matches(n, list(aliases.keys()), n=1, cutoff=0.7)
    if close:
        return aliases[close[0]]

    # try as-is (full path)
    if os.path.isdir(name):
        return name

    # try under Downloads
    cand = os.path.join(user, "Downloads", name)
    if os.path.isdir(cand):
        return cand

    # try under user profile
    cand = os.path.join(user, name)
    if os.path.isdir(cand):
        return cand

    return None


def _parse_patterns(pattern_field):
    """'SPM, CTU, LCC' -> ['spm', 'ctu', 'lcc']"""
    parts = re.split(r"[,\|;]+", pattern_field or "")
    return [p.strip().lower() for p in parts if p.strip()]


def find_files(input_str):
    """Find files by NAME pattern. Format: 'folder|pat1,pat2' OR just 'pat1,pat2'."""
    user = os.environ.get("USERPROFILE", "")

    # Allow bare patterns without a folder — default to Telegram Desktop,
    # then Downloads, then the whole user profile.
    if "|" not in input_str:
        patterns_only = input_str.strip()
        for default_folder in ("telegram desktop", "downloads"):
            trial = f"{default_folder}|{patterns_only}"
            folder = _resolve_folder(default_folder)
            if folder and os.path.isdir(folder):
                input_str = trial
                break
        else:
            return "ERROR: find_files needs a folder. Try: 'telegram desktop|SPM,CTU'."

    folder_name, pattern_field = input_str.split("|", 1)
    folder = _resolve_folder(folder_name)
    if not folder:
        return f"ERROR: couldn't find folder '{folder_name}'."

    patterns = _parse_patterns(pattern_field)
    if not patterns:
        return "ERROR: find_files needs at least one pattern."

    matches = []
    for root, dirs, files in os.walk(folder):
        for name in files:
            nl = name.lower()
            if any(p in nl for p in patterns):
                matches.append(os.path.join(root, name))

    if not matches:
        return f"No files matching any of {patterns} in {folder}."

    lines = [
        f"Found {len(matches)} file(s) matching any of {patterns} in {folder}:"
    ]
    for m in matches[:80]:
        lines.append(f"  - {m}")
    if len(matches) > 80:
        lines.append(f"  ... and {len(matches) - 80} more")
    return "\n".join(lines)


def move_files(input_str):
    """
    Move files matching one or more patterns.
    Formats:
      'source|pat1,pat2|dest'           -> preview
      'source|pat1,pat2|dest|confirm'   -> execute
    """
    parts = [p.strip() for p in input_str.split("|")]
    if len(parts) < 3:
        return "ERROR: move_files needs 'source|pat1,pat2|dest' (add |confirm to execute)."

    src_name, pattern_field, dest_name = parts[0], parts[1], parts[2]
    confirmed = len(parts) >= 4 and parts[3].lower() == "confirm"

    src = _resolve_folder(src_name)
    if not src:
        return f"ERROR: couldn't find source folder '{src_name}'."

    dest = _resolve_folder(dest_name)
    if not dest:
        user = os.environ.get("USERPROFILE", "")
        dest = os.path.join(user, "Downloads", dest_name)

    patterns = _parse_patterns(pattern_field)
    if not patterns:
        return "ERROR: move_files needs at least one pattern."

    matches = []
    for root, dirs, files in os.walk(src):
        for name in files:
            nl = name.lower()
            if any(p in nl for p in patterns):
                matches.append(os.path.join(root, name))

    if not matches:
        return f"No files matching any of {patterns} in {src}."

    if not confirmed:
        lines = [
            f"PREVIEW: would move {len(matches)} file(s)",
            f"  from: {src}",
            f"  to:   {dest}",
            f"  patterns: {patterns}",
            "",
        ]
        for m in matches[:20]:
            lines.append(f"  - {os.path.basename(m)}")
        if len(matches) > 20:
            lines.append(f"  ... and {len(matches) - 20} more")
        lines.append("")
        lines.append(
            f"To execute, call: move_files('{src_name}|{pattern_field}|{dest_name}|confirm')"
        )
        return "\n".join(lines)

    os.makedirs(dest, exist_ok=True)
    moved, failed = 0, []
    for src_path in matches:
        try:
            dest_path = os.path.join(dest, os.path.basename(src_path))
            if os.path.exists(dest_path):
                base, ext = os.path.splitext(os.path.basename(src_path))
                i = 1
                while os.path.exists(dest_path):
                    dest_path = os.path.join(dest, f"{base}_{i}{ext}")
                    i += 1
            _shutil.move(src_path, dest_path)
            moved += 1
        except Exception as e:
            failed.append(f"{os.path.basename(src_path)}: {e}")

    result = f"Moved {moved} file(s) to {dest}."
    if failed:
        result += f"\nFailed: {len(failed)}\n" + "\n".join(failed[:5])
    return result

def write_file(filename, content, folder=None):
    try:
        if folder:
            folder_path = os.path.join(get_downloads_path(), folder) if not os.path.isabs(folder) else folder
            file_path = os.path.join(folder_path, filename)
        else:
            file_path = os.path.join(get_downloads_path(), filename) if not os.path.isabs(filename) else filename
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
        return f"File written: {file_path}"
    except Exception as e:
        return f"ERROR: {e}"


def read_file(path):
    import os
    from pathlib import Path

    path = (path or "").strip().strip('"').strip("'")
    if not path:
        return "ERROR: no path given"

    # 1. Try as-is
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()

    # 2. Prepend C:\ if it looks like "Users\..." was typed
    if not path.startswith(("C:", "c:", "\\\\", "/")):
        candidate = "C:\\" + path
        if os.path.isfile(candidate):
            with open(candidate, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()

    # 3. Search by basename in common folders
    basename = os.path.basename(path).lower()
    if basename:
        roots = [
            os.path.join(os.environ.get("USERPROFILE", ""), "Downloads"),
            os.path.join(os.environ.get("USERPROFILE", ""), "Desktop"),
            os.path.join(os.environ.get("USERPROFILE", ""), "Documents"),
        ]
        for root in roots:
            if not os.path.isdir(root):
                continue
            for dirpath, _, files in os.walk(root):
                for name in files:
                    if name.lower() == basename:
                        full = os.path.join(dirpath, name)
                        with open(full, "r", encoding="utf-8", errors="ignore") as f:
                            return f"[Found at: {full}]\n\n" + f.read()

    return f"ERROR: could not find file '{path}'. Tried as-is, with C:\\ prepended, and searched Downloads/Desktop/Documents for '{basename}'."


# ---------------------------
# CODE EDITING + REPO MAP
# ---------------------------
import ast as _ast

_PATCH_SEP = "|||"

_MAP_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".next", ".cache", "vendor",
}


def _resolve_existing_file(path):
    """Resolve an abbreviated path to a real file, or return None."""
    path = (path or "").strip().strip('"').strip("'")
    if not path:
        return None
    if os.path.isfile(path):
        return os.path.abspath(path)
    if not path.startswith(("C:", "c:", "\\\\", "/")):
        cand = "C:\\" + path
        if os.path.isfile(cand):
            return cand
    user = os.environ.get("USERPROFILE", "")
    roots = [
        os.path.join(user, "Downloads"),
        os.path.join(user, "Desktop"),
        os.path.join(user, "Documents"),
        os.getcwd(),
    ]
    for root in roots:
        if root and os.path.isdir(root):
            direct = os.path.join(root, path)
            if os.path.isfile(direct):
                return os.path.abspath(direct)
    basename = os.path.basename(path).lower()
    if basename:
        for root in roots:
            if not root or not os.path.isdir(root):
                continue
            for dirpath, dirnames, files in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in _MAP_SKIP_DIRS]
                for name in files:
                    if name.lower() == basename:
                        return os.path.join(dirpath, name)
    return None


def patch_file(input_str):
    """Surgical SEARCH/REPLACE edit. Format: 'path|||old_text|||new_text'."""
    parts = (input_str or "").split(_PATCH_SEP, 2)
    if len(parts) < 3:
        return (
            "ERROR: patch_file needs three parts separated by ||| — "
            "path|||old_text|||new_text"
        )
    raw_path, old_text, new_text = parts[0].strip(), parts[1], parts[2]

    path = _resolve_existing_file(raw_path)
    if not path:
        return f"ERROR: could not find file '{raw_path}'. Nothing was changed."

    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            content = f.read()
    except Exception as e:
        return f"ERROR: could not read {path}: {e}"

    if old_text not in content:
        def _norm(s):
            return "\n".join(ln.rstrip() for ln in s.splitlines())
        if old_text.strip() and _norm(old_text) in _norm(content):
            return (
                f"ERROR: that text exists in {os.path.basename(path)} but the indentation or "
                f"trailing whitespace does not match exactly. Nothing was changed. "
                f"Call read_file('{path}') and copy the text character-for-character."
            )
        return (
            f"ERROR: old_text not found in {os.path.basename(path)}. Nothing was changed. "
            f"Call read_file('{path}') or list_symbols('{path}') to see the current content."
        )

    hits = content.count(old_text)
    if hits > 1:
        return (
            f"ERROR: old_text appears {hits} times in {os.path.basename(path)} — ambiguous. "
            f"Include more surrounding lines so it is unique. Nothing was changed."
        )

    updated = content.replace(old_text, new_text, 1)

    try:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(updated)
    except Exception as e:
        return f"ERROR: could not write {path}: {e}"

    note = ""
    if path.lower().endswith(".py"):
        try:
            _ast.parse(updated)
        except SyntaxError as se:
            note = (
                f"\n  WARNING: the file no longer parses — line {se.lineno}: {se.msg}. "
                f"The edit was written anyway; fix it with another patch_file."
            )

    delta = updated.count("\n") - content.count("\n")
    sign = "+" if delta > 0 else ""
    return (
        f"Patched {path}\n"
        f"  {len(old_text.splitlines())} line(s) -> {len(new_text.splitlines())} line(s)"
        f" ({sign}{delta} net){note}"
    )


def _fmt_params(node):
    a = node.args
    names = [x.arg for x in getattr(a, "posonlyargs", [])] + [x.arg for x in a.args]
    if a.vararg:
        names.append("*" + a.vararg.arg)
    names += [x.arg for x in a.kwonlyargs]
    if a.kwarg:
        names.append("**" + a.kwarg.arg)
    return ", ".join(names)


def _fmt_return(node):
    if getattr(node, "returns", None) is None:
        return ""
    try:
        return " -> " + _ast.unparse(node.returns)
    except Exception:
        return ""


def _summarize_py(src, path, indent="  "):
    """Compact symbol summary for one Python file."""
    try:
        tree = _ast.parse(src)
    except SyntaxError as se:
        return [f"{indent}!! does not parse — line {se.lineno}: {se.msg}"]

    out = []
    for node in tree.body:
        if isinstance(node, _ast.ClassDef):
            methods, attrs = [], []
            for sub in node.body:
                if isinstance(sub, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                    methods.append(sub.name)
                    if sub.name == "__init__":
                        for st in _ast.walk(sub):
                            if isinstance(st, _ast.Assign):
                                for t in st.targets:
                                    if (isinstance(t, _ast.Attribute)
                                            and isinstance(t.value, _ast.Name)
                                            and t.value.id == "self"):
                                        attrs.append(t.attr)
            head = f"{indent}class {node.name} (line {node.lineno})"
            if attrs:
                seen = list(dict.fromkeys(attrs))[:8]
                head += ": " + ", ".join(seen)
            out.append(head)
            if methods:
                out.append(f"{indent}    " + ", ".join(methods) + "()")
        elif isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            out.append(
                f"{indent}def {node.name}({_fmt_params(node)}){_fmt_return(node)}"
                f"  (line {node.lineno})"
            )
    if not out:
        out.append(f"{indent}(no top-level functions or classes)")
    return out


def list_symbols(input_str):
    """Map a single Python file: every class/function with line numbers."""
    path = _resolve_existing_file(input_str)
    if not path:
        return f"ERROR: could not find file '{input_str}'."
    if not path.lower().endswith(".py"):
        return (
            f"ERROR: list_symbols only works on .py files "
            f"(got '{os.path.basename(path)}'). Use read_file instead."
        )
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            src = f.read()
    except Exception as e:
        return f"ERROR: could not read {path}: {e}"

    lines = [f"{path} — {len(src.splitlines())} lines"]
    lines += _summarize_py(src, path)
    return "\n".join(lines)


def repo_map(input_str, max_chars=14000):
    """Token-efficient symbol map of a folder. Format: 'folder' or 'folder|max_files'."""
    parts = (input_str or "").split("|", 1)
    folder = parts[0].strip().strip('"').strip("'")
    max_files = 50
    if len(parts) > 1 and parts[1].strip():
        try:
            max_files = max(1, min(500, int(parts[1].strip())))
        except ValueError:
            return f"ERROR: repo_map max_files must be a number, got '{parts[1].strip()}'."
    if not folder:
        return "ERROR: repo_map needs a folder. Try: repo_map('agentbot')."

    resolved = None
    if os.path.isdir(folder):
        resolved = os.path.abspath(folder)
    if not resolved:
        resolved = _resolve_folder(folder)
    if not resolved or not os.path.isdir(resolved):
        return f"ERROR: could not find folder '{folder}'."

    py_files = []
    for dirpath, dirnames, files in os.walk(resolved):
        dirnames[:] = [d for d in dirnames if d not in _MAP_SKIP_DIRS]
        for name in files:
            if name.lower().endswith(".py"):
                py_files.append(os.path.join(dirpath, name))
    py_files.sort()

    if not py_files:
        return f"No .py files found in {resolved}."

    total = len(py_files)
    shown = py_files[:max_files]

    out = [f"REPO MAP: {resolved}", f"{total} .py file(s), showing {len(shown)}", ""]
    for fp in shown:
        try:
            with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                src = f.read()
        except Exception:
            continue
        rel = os.path.relpath(fp, resolved)
        block = [f"{rel} — {len(src.splitlines())} lines"]
        block += _summarize_py(src, fp)
        candidate = "\n".join(out + block)
        if len(candidate) > max_chars:
            out.append(f"... truncated at {max_chars} chars. "
                       f"Narrow the folder or lower max_files.")
            break
        out += block
        out.append("")

    if total > len(shown):
        out.append(f"... and {total - len(shown)} more file(s). "
                   f"Raise max_files to see them.")
    return "\n".join(out).rstrip()


# ---------------------------
# WEATHER + NEWS + AIR QUALITY
# ---------------------------
def weather(input_str):
    return weather_news.get_weather(input_str)


def air_quality(input_str):
    location = (input_str or "").strip() or "Shah Alam"
    return air_quality_tool.get_air_quality(location)


def news(input_str):
    if not input_str or not input_str.strip():
        return weather_news.get_news("world", 5)
    if '|' in input_str:
        cat, cnt = input_str.split('|', 1)
        try:
            cnt = int(cnt.strip())
        except ValueError:
            cnt = 5
        return weather_news.get_news(cat.strip(), cnt)
    return weather_news.get_news(input_str.strip(), 5)


# ---------------------------
# WHATSAPP
# ---------------------------
def whatsapp_send(input_str):
    if '|' not in input_str:
        return "ERROR: whatsapp_send needs 'contact|message'."
    contact, msg = input_str.split('|', 1)
    return whatsapp_tool.send_message(contact.strip(), msg.strip())


# ---------------------------
# SCREENSHOT + AUTO-DESCRIBE
# ---------------------------
def describe_screen():
    import screen as _screen
    text = _screen.capture_and_read_screen()
    if text.startswith("ERROR"):
        return text

    prompt = (
        "Below is the OCR'd text of what's on the user's screen. "
        "Describe in 2-4 sentences what they're looking at, and summarise "
        "any important info. Be concrete — don't invent anything.\n\n"
        f"--- SCREEN TEXT ---\n{text[:3500]}\n--- END ---"
    )
    description = ask_llm_direct(prompt)
    return f"Screen contents:\n{description}"


# ---------------------------
# CLIPBOARD
# ---------------------------
def clipboard_read():
    if not PYPERCLIP_AVAILABLE:
        return "ERROR: pyperclip not installed."
    try:
        text = pyperclip.paste()
        if not text or not text.strip():
            return "Clipboard is empty."
        return f"Clipboard contents:\n{text[:3000]}"
    except Exception as e:
        return f"ERROR: {e}"


def clipboard_write(text):
    if not PYPERCLIP_AVAILABLE:
        return "ERROR: pyperclip not installed."
    try:
        pyperclip.copy(text)
        return f"Copied to clipboard: {text[:80]}{'...' if len(text) > 80 else ''}"
    except Exception as e:
        return f"ERROR: {e}"


# ---------------------------
# VOLUME
# ---------------------------
def _get_volume_interface():
    devices = AudioUtilities.GetSpeakers()
    interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return cast(interface, POINTER(IAudioEndpointVolume))


def set_volume(input_str):
    if not PYCAW_AVAILABLE:
        return "ERROR: pycaw not installed."
    try:
        s = input_str.strip().lower()
        vol = _get_volume_interface()
        if s in ("mute", "off"):
            vol.SetMute(1, None)
            return "Muted."
        if s in ("unmute", "on"):
            vol.SetMute(0, None)
            return "Unmuted."
        level = int(re.sub(r'[^0-9]', '', s))
        level = max(0, min(100, level))
        vol.SetMute(0, None)
        vol.SetMasterVolumeLevelScalar(level / 100.0, None)
        return f"Volume set to {level}%."
    except Exception as e:
        return f"ERROR: {e}"


# ---------------------------
# MEDIA CONTROL
# ---------------------------
def media_control(action):
    try:
        a = action.strip().lower()
        keys = {
            "play": "playpause", "pause": "playpause", "play_pause": "playpause",
            "playpause": "playpause", "toggle": "playpause",
            "next": "nexttrack", "skip": "nexttrack",
            "prev": "prevtrack", "previous": "prevtrack", "back": "prevtrack",
            "stop": "stop",
        }
        key = keys.get(a, a)
        pyautogui.press(key)
        return f"Media: {a}."
    except Exception as e:
        return f"ERROR: {e}"


# ---------------------------
# SYSTEM STATUS
# ---------------------------
def system_status():
    if not PSUTIL_AVAILABLE:
        return "ERROR: psutil not installed."
    try:
        cpu = psutil.cpu_percent(interval=0.5)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage('C:\\')
        out = (f"CPU: {cpu}%\n"
               f"RAM: {mem.percent}% ({round(mem.used/1e9,1)}/{round(mem.total/1e9,1)} GB)\n"
               f"Disk C: {disk.percent}% used ({round(disk.free/1e9,1)} GB free)")
        battery = psutil.sensors_battery()
        if battery:
            state = "charging" if battery.power_plugged else "on battery"
            out += f"\nBattery: {int(battery.percent)}% ({state})"
        return out
    except Exception as e:
        return f"ERROR: {e}"


# ---------------------------
# POWER CONTROLS
# ---------------------------
def lock_pc():
    try:
        subprocess.run("rundll32.exe user32.dll,LockWorkStation", shell=True)
        return "PC locked."
    except Exception as e:
        return f"ERROR: {e}"


def sleep_pc():
    try:
        subprocess.Popen("rundll32.exe powrprof.dll,SetSuspendState 0,1,0", shell=True)
        return "PC going to sleep."
    except Exception as e:
        return f"ERROR: {e}"


def shutdown_pc(input_str):
    if input_str.strip().lower() != "confirm":
        return "ERROR: shutdown_pc requires the exact input 'confirm'."
    try:
        subprocess.Popen("shutdown /s /t 10", shell=True)
        return "Shutting down in 10 seconds."
    except Exception as e:
        return f"ERROR: {e}"


def restart_pc(input_str):
    if input_str.strip().lower() != "confirm":
        return "ERROR: restart_pc requires the exact input 'confirm'."
    try:
        subprocess.Popen("shutdown /r /t 10", shell=True)
        return "Restarting in 10 seconds."
    except Exception as e:
        return f"ERROR: {e}"


# ---------------------------
# SCREENSHOT
# ---------------------------
def save_screenshot():
    try:
        img = ImageGrab.grab()
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(get_downloads_path(), f"screenshot_{ts}.png")
        img.save(path)
        return f"Screenshot saved: {path}"
    except Exception as e:
        return f"ERROR: {e}"


# ---------------------------
# TODO LIST
# ---------------------------
def _load_todos():
    if not os.path.exists(TODO_FILE):
        return []
    try:
        with open(TODO_FILE, 'r', encoding='utf-8') as f:
            return json.load(f).get('todos', [])
    except (json.JSONDecodeError, OSError):
        return []


def _save_todos(todos):
    os.makedirs(os.path.dirname(TODO_FILE), exist_ok=True)
    with open(TODO_FILE, 'w', encoding='utf-8') as f:
        json.dump({'todos': todos}, f, indent=2)


def todo(input_str):
    try:
        s = input_str.strip()
        if not s or s.lower() == "list":
            todos = _load_todos()
            if not todos:
                return "Todo list is empty."
            lines = [f"[{i}] {'[x]' if t.get('done') else '[ ]'} {t['text']}"
                     for i, t in enumerate(todos)]
            return "Todo list:\n" + "\n".join(lines)

        if s.lower().startswith("add:"):
            text = s[4:].strip()
            if not text:
                return "ERROR: nothing to add."
            todos = _load_todos()
            todos.append({"text": text, "done": False})
            _save_todos(todos)
            return f"Added todo: {text}"

        if s.lower().startswith(("done:", "complete:")):
            idx_str = s.split(":", 1)[1].strip()
            try:
                idx = int(idx_str)
            except ValueError:
                return "ERROR: done needs a number."
            todos = _load_todos()
            if 0 <= idx < len(todos):
                todos[idx]["done"] = True
                _save_todos(todos)
                return f"Completed: {todos[idx]['text']}"
            return f"ERROR: no todo at index {idx}."

        if s.lower().startswith("remove:"):
            idx_str = s.split(":", 1)[1].strip()
            try:
                idx = int(idx_str)
            except ValueError:
                return "ERROR: remove needs a number."
            todos = _load_todos()
            if 0 <= idx < len(todos):
                removed = todos.pop(idx)
                _save_todos(todos)
                return f"Removed: {removed['text']}"
            return f"ERROR: no todo at index {idx}."

        if s.lower() == "clear":
            _save_todos([])
            return "Todo list cleared."

        todos = _load_todos()
        todos.append({"text": s, "done": False})
        _save_todos(todos)
        return f"Added todo: {s}"
    except Exception as e:
        return f"ERROR: {e}"


# ---------------------------
# WINDOW MANAGEMENT
# ---------------------------
def list_windows():
    if not PYGETWINDOW_AVAILABLE:
        return "ERROR: pygetwindow not installed."
    try:
        titles = [t for t in gw.getAllTitles() if t.strip()]
        if not titles:
            return "No windows detected."
        return "Open windows:\n" + "\n".join(f"- {t}" for t in titles[:30])
    except Exception as e:
        return f"ERROR: {e}"


def focus_window(title):
    if not PYGETWINDOW_AVAILABLE:
        return "ERROR: pygetwindow not installed."
    try:
        matches = gw.getWindowsWithTitle(title)
        if not matches:
            return f"ERROR: no window matching '{title}'."
        win = matches[0]
        if win.isMinimized:
            win.restore()
        win.activate()
        return f"Focused window: {win.title}"
    except Exception as e:
        return f"ERROR: {e}"


def close_window(title):
    if not PYGETWINDOW_AVAILABLE:
        return "ERROR: pygetwindow not installed."
    try:
        matches = gw.getWindowsWithTitle(title)
        if not matches:
            return f"ERROR: no window matching '{title}'."
        win = matches[0]
        try:
            win.close()
        except Exception:
            if win.isMinimized:
                win.restore()
            win.activate()
            time.sleep(0.3)
            pyautogui.hotkey('alt', 'f4')
        return f"Closed window: {title}"
    except Exception as e:
        return f"ERROR: {e}"


# ---------------------------
# DESKTOP NOTIFICATION
# ---------------------------
def notify(input_str):
    if not WINOTIFY_AVAILABLE:
        return "ERROR: winotify not installed."
    try:
        if '|' in input_str:
            title, msg = input_str.split('|', 1)
        else:
            title, msg = "Agent", input_str
        toast = Notification(app_id="Agent", title=title.strip(), msg=msg.strip(), duration="short")
        toast.show()
        return f"Notification sent: {title.strip()}"
    except Exception as e:
        return f"ERROR: {e}"


# ---------------------------
# BEEP
# ---------------------------
def beep():
    try:
        winsound.Beep(1000, 400)
        return "Beep."
    except Exception as e:
        return f"ERROR: {e}"


# ---------------------------
# REMINDER EXECUTION
# ---------------------------
def execute_reminder(reminder_text):
    text = (reminder_text or "").strip()
    low = text.lower()

    try:
        if low.startswith("recipe:"):
            from recipes import run_recipe
            return run_recipe(text.split(":", 1)[1].strip())

        if low.startswith("open "):
            target = text[5:].strip()
            if target.startswith(("http://", "https://")):
                return open_website(target)
            if "." in target and " " not in target:
                return open_website(target)
            return open_app(target)

        if low.startswith("run "):
            cmd = text[4:].strip()
            return run_command(cmd)

        if low.startswith("send_telegram:") or low.startswith("send_telegram "):
            msg = text.split(":", 1)[-1].strip() if ":" in text else text[14:].strip()
            return send_telegram_message(msg)

        if low.startswith("notify "):
            return notify(f"Reminder|{text[7:].strip()}")

        return notify(f"Reminder|{text}")
    except Exception as e:
        return f"ERROR executing reminder: {e}"


# ---------------------------
# REMINDER TOOLS
# ---------------------------
def set_reminder(input_str):
    try:
        if '|' not in input_str:
            return "ERROR: set_reminder needs 'duration|text'."
        duration_str, text = input_str.split('|', 1)
        duration_str = duration_str.strip()
        text = text.strip()
        seconds = scheduler.parse_duration(duration_str)
        if seconds is None:
            return f"ERROR: Couldn't parse duration '{duration_str}'."
        fire_at = scheduler.add_reminder(seconds, text)
        when = datetime.datetime.fromtimestamp(fire_at).strftime("%H:%M:%S")
        return f"Reminder set for {when} ({duration_str} from now): {text}"
    except Exception as e:
        return f"ERROR: {e}"


def set_reminder_at(input_str):
    try:
        if '|' not in input_str:
            return "ERROR: set_reminder_at needs 'time|text'."
        time_str, text = input_str.split('|', 1)
        time_str = time_str.strip().lower()
        text = text.strip()

        ampm = None
        if time_str.endswith("am"):
            ampm = "am"
            time_str = time_str[:-2].strip()
        elif time_str.endswith("pm"):
            ampm = "pm"
            time_str = time_str[:-2].strip()

        if ':' not in time_str:
            return f"ERROR: Couldn't parse time '{time_str}'."

        try:
            hh, mm = time_str.split(':', 1)
            hh = int(hh); mm = int(mm)
        except ValueError:
            return f"ERROR: Couldn't parse time '{time_str}'."

        if ampm == "pm" and hh < 12:
            hh += 12
        elif ampm == "am" and hh == 12:
            hh = 0

        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            return f"ERROR: Invalid time."

        now = datetime.datetime.now()
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= now:
            target += datetime.timedelta(days=1)
            when_desc = "tomorrow"
        else:
            when_desc = "today"

        seconds_until = (target - now).total_seconds()
        fire_at = scheduler.add_reminder(seconds_until, text)
        return (f"Reminder set for {target.strftime('%I:%M %p')} {when_desc} "
                f"({int(seconds_until//60)} min from now): {text}")
    except Exception as e:
        return f"ERROR: {e}"


def list_reminders():
    try:
        lines = []
        try:
            reminders = scheduler.load_reminders() if hasattr(scheduler, 'load_reminders') else []
        except Exception:
            reminders = []

        if reminders:
            lines.append("Pending reminders:")
            for r in reminders:
                when = datetime.datetime.fromtimestamp(r["fire_at"]).strftime("%Y-%m-%d %H:%M")
                lines.append(f"  * {when} — {r['text']}")
        else:
            lines.append("No pending one-shot reminders.")

        try:
            tasks = scheduler.load_tasks() if hasattr(scheduler, 'load_tasks') else []
        except Exception:
            tasks = []

        if tasks:
            lines.append("")
            lines.append("Daily scheduled tasks:")
            for t in tasks:
                lines.append(f"  * {t['time']} — {t['request']}")
        else:
            lines.append("")
            lines.append("No daily scheduled tasks.")

        return "\n".join(lines)
    except Exception as e:
        return f"ERROR: {e}"


# ---------------------------
# WEB SEARCH
# ---------------------------
def search_web(query):
    global last_search_query, last_search_result
    last_search_query = query
    last_search_result = ""

    try:
        r = requests.post(
            "https://api.tavily.com/search",
            headers={
                "Content-Type": "application/json",
                "X-Tavily-Access-Mode": "keyless",
            },
            json={"query": query, "max_results": 5},
            timeout=15,
        )

        if r.status_code == 429:
            msg = "ERROR: Tavily rate limit hit (keyless). Try again in a moment."
            last_search_result = msg
            return msg
        if r.status_code != 200:
            msg = f"ERROR: Tavily HTTP {r.status_code}: {r.text[:150]}"
            last_search_result = msg
            return msg

        data = r.json()
        results = []
        for item in (data.get("results") or [])[:5]:
            title = (item.get("title") or "").strip()
            url = (item.get("url") or "").strip()
            content = (item.get("content") or "").strip()
            if not title or not url:
                continue
            entry = f"{title} -> {url}"
            if content:
                entry += f"\n  {content[:200]}"
            results.append(entry)

        if not results:
            msg = f"ERROR: Tavily returned 0 results for '{query}'."
            last_search_result = msg
            return msg

        out = "Real search results (use these EXACT URLs):\n" + "\n".join(results)
        last_search_result = out
        return out

    except requests.exceptions.Timeout:
        msg = "ERROR: Tavily search timed out."
        last_search_result = msg
        return msg
    except Exception as e:
        msg = f"ERROR: Tavily search failed: {e}"
        last_search_result = msg
        return msg

def show_last_result():
    global last_search_result
    if last_search_result:
        return last_search_result
    return "No previous search result found."


# ---------------------------
# WEBPAGE SUMMARIZER
# ---------------------------
def read_and_summarize(url):
    global last_search_result
    try:
        url = normalize_url(url)
        print(f"Fetching: {url}")
        html = None
        driver = get_driver()
        if driver is not None:
            try:
                driver.get(url)
                time.sleep(2)
                html = driver.page_source
            except Exception as e:
                print(f"Browser fetch failed ({e}), falling back.")
        if html is None:
            resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
            resp.raise_for_status()
            html = resp.text
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header",
                         "aside", "form", "noscript", "iframe", "svg"]):
            tag.decompose()
        main = soup.find("article") or soup.find("main") or soup.body or soup
        text = main.get_text(separator="\n", strip=True)
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        text = "\n".join(lines)
        if len(text) < 200:
            return f"ERROR: Could not extract meaningful text from {url}."
        original_len = len(text)
        text = text[:MAX_PAGE_CHARS]
        prompt = f"""Summarize the following webpage in 4-6 bullet points. Then add a one-line TL;DR.

Be factual. Do not invent information.

--- PAGE CONTENT from {url} ---
{text}
--- END ---

Summary:"""
        print(f"Summarizing {original_len} chars...")
        summary = ask_llm_direct(prompt)
        output = f"Summary of {url}:\n{summary}"
        last_search_result = output
        return output
    except requests.exceptions.HTTPError as e:
        return f"ERROR: Could not fetch page (HTTP {e.response.status_code})."
    except requests.exceptions.Timeout:
        return f"ERROR: Page took too long to load."
    except Exception as e:
        return f"ERROR: {e}"


# ---------------------------
# WEB CRAWL / CLEAN FETCH
# ---------------------------
from collections import deque as _deque

_WEB_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_WEB_STRIP = ["script", "style", "nav", "footer", "header", "aside",
              "noscript", "iframe", "form", "button", "svg"]


def _web_host(url):
    return urllib.parse.urlparse(url).netloc.lower().removeprefix("www.")


def _web_fetch(url, timeout=12):
    """GET a URL with a browser user-agent. Returns (html, error)."""
    try:
        r = requests.get(url, headers={"User-Agent": _WEB_UA},
                         timeout=timeout, allow_redirects=True)
        if r.status_code != 200:
            return None, f"ERROR: HTTP {r.status_code} for {url}."
        ctype = r.headers.get("Content-Type", "")
        if not any(k in ctype for k in ("html", "text", "xml")):
            return None, f"ERROR: {url} is not a text page ({ctype or 'unknown type'})."
        return r.text, None
    except requests.exceptions.Timeout:
        return None, f"ERROR: timed out fetching {url}."
    except Exception as e:
        return None, f"ERROR: could not fetch {url}: {e}"


def _web_text(html, limit=8000):
    """Strip boilerplate from HTML and return clean text."""
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(_WEB_STRIP):
        tag.decompose()
    lines = [ln.strip() for ln in soup.get_text(separator="\n").splitlines()]
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(ln for ln in lines if ln))
    if len(out) > limit:
        out = out[:limit] + f"\n\n[... truncated at {limit} chars]"
    return out.strip()


def _web_links(html, base):
    """Absolute same-domain http(s) links found in html, in page order."""
    soup = BeautifulSoup(html or "", "html.parser")
    host = _web_host(base)
    seen, out = set(), []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        absu = urllib.parse.urljoin(base, href)
        if urllib.parse.urlparse(absu).scheme not in ("http", "https"):
            continue
        if _web_host(absu) != host:
            continue
        clean = absu.split("#")[0]
        if clean not in seen:
            seen.add(clean)
            out.append(clean)
    return out


def _web_norm_url(input_str):
    url = (input_str or "").strip().strip('"').strip("'")
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def fetch_clean(input_str):
    """Fetch a URL and return its raw extracted text — no summarization."""
    url = _web_norm_url(input_str)
    if not url:
        return "ERROR: fetch_clean needs a URL."
    html, err = _web_fetch(url)
    if err:
        return err
    text = _web_text(html, limit=8000)
    if not text:
        return f"ERROR: no readable text extracted from {url}."
    return f"[Clean text from {url}]\n\n{text}"


def map_site(input_str):
    """List all URLs on a site. Tries sitemap.xml, falls back to homepage links."""
    url = _web_norm_url(input_str)
    if not url:
        return "ERROR: map_site needs a URL."
    p = urllib.parse.urlparse(url)
    root = f"{p.scheme}://{p.netloc}"

    found, source = [], "sitemap.xml"
    for sm in (root + "/sitemap.xml", root + "/sitemap_index.xml"):
        html, err = _web_fetch(sm)
        if err or not html:
            continue
        locs = re.findall(r"<loc>\s*(.*?)\s*</loc>", html,
                          re.IGNORECASE | re.DOTALL)
        found.extend(u for u in locs if u.startswith("http"))
        if found:
            break

    if not found:
        html, err = _web_fetch(url)
        if err:
            return err
        found = _web_links(html, url)
        source = "homepage links"
    if not found:
        return f"ERROR: could not find any URLs for {root}."

    urls = list(dict.fromkeys(found))
    lines = [f"SITE MAP: {root} — {len(urls)} URL(s) from {source}"]
    lines += [f"  - {u}" for u in urls[:300]]
    if len(urls) > 300:
        lines.append(f"  ... and {len(urls) - 300} more")
    return "\n".join(lines)


def crawl_site(input_str):
    """BFS crawl of same-domain pages. Format: 'url|max_pages'."""
    parts = (input_str or "").split("|", 1)
    url = _web_norm_url(parts[0])
    if not url:
        return "ERROR: crawl_site needs a URL. Format: 'url|max_pages'."
    max_pages = 10
    if len(parts) > 1 and parts[1].strip():
        try:
            max_pages = max(1, min(50, int(parts[1].strip())))
        except ValueError:
            return f"ERROR: max_pages must be a number, got '{parts[1].strip()}'."

    host = _web_host(url)
    queue, visited = _deque([url]), set()
    chunks, errors = [], []

    while queue and len(visited) < max_pages:
        cur = queue.popleft()
        if cur in visited or _web_host(cur) != host:
            visited.add(cur)
            continue
        visited.add(cur)
        html, err = _web_fetch(cur)
        if err:
            errors.append(err)
            continue
        soup = BeautifulSoup(html, "html.parser")
        title = soup.title.get_text().strip() if soup.title else cur
        chunks.append(f"=== [{len(chunks) + 1}] {title}\n{cur}\n\n"
                      f"{_web_text(html, limit=2500)}")
        for link in _web_links(html, cur):
            if link not in visited:
                queue.append(link)

    if not chunks:
        return (f"ERROR: visited {len(visited)} page(s) on {host}, got no text.\n"
                + "\n".join(errors[:5]))

    out = (f"CRAWL: {host} — {len(chunks)} page(s) read, "
           f"{len(visited)} visited, cap {max_pages}\n\n" + "\n\n".join(chunks))
    if len(out) > 24000:
        out = out[:24000] + "\n\n[... truncated]"
    if errors:
        out += f"\n\n({len(errors)} page(s) failed to load)"
    return out


# ---------------------------
# DEEP RESEARCH
# ---------------------------
def _extract_urls(search_result, limit=2):
    """Pull URLs out of a search_web result string."""
    urls = re.findall(r"->\s*(https?://\S+)", search_result or "")
    seen = set()
    out = []
    for u in urls:
        u = u.rstrip(".,;)")
        if u and u not in seen:
            seen.add(u)
            out.append(u)
            if len(out) >= limit:
                break
    return out


def _parse_subquestions(raw):
    """Parse the LLM's list of sub-questions."""
    if not raw:
        return []
    try:
        m = re.search(r"\[.*\]", raw, re.DOTALL)
        if m:
            import json as _json
            arr = _json.loads(m.group(0))
            if isinstance(arr, list):
                return [str(x).strip() for x in arr if str(x).strip()][:6]
    except Exception:
        pass
    lines = []
    for line in raw.split("\n"):
        line = line.strip()
        m = re.match(r"^\d+[\.\)]\s*(.+)$", line)
        if m:
            lines.append(m.group(1).strip())
        elif line.startswith("- "):
            lines.append(line[2:].strip())
    return lines[:6]


def deep_research(topic):
    """Multi-step research → structured .md report saved to Downloads."""
    topic = (topic or "").strip()
    if not topic:
        return "ERROR: deep_research needs a topic."

    print(f"[research] starting: {topic}")

    plan_prompt = (
        f"Break this research topic into 4 focused sub-questions.\n\n"
        f"Topic: {topic}\n\n"
        f"Respond with ONLY a JSON array of 4 strings. Example:\n"
        f'["sub-question 1", "sub-question 2", "sub-question 3", "sub-question 4"]'
    )
    plan_raw = _call_deepseek_thinking(plan_prompt, max_tokens=1024)
    sub_questions = _parse_subquestions(plan_raw)
    if not sub_questions:
        sub_questions = [topic]
    print(f"[research] {len(sub_questions)} sub-questions")

    findings = []
    for i, q in enumerate(sub_questions, 1):
        print(f"[research] ({i}/{len(sub_questions)}) {q}")
        try:
            search_result = search_web(q)
        except Exception as e:
            search_result = f"ERROR: {e}"
        urls = _extract_urls(search_result, limit=2)
        sources = []
        for url in urls:
            try:
                summary = read_and_summarize(url)
                if summary and not summary.startswith("ERROR"):
                    sources.append({"url": url, "summary": summary})
            except Exception as e:
                print(f"[research] read failed {url}: {e}")
        findings.append({"question": q, "sources": sources})

    if not any(f["sources"] for f in findings):
        return (f"ERROR: deep_research found no usable sources for '{topic}'. "
                f"Search backend may be rate-limited — try again in a minute.")

    print("[research] synthesizing report...")
    combined = []
    for f_item in findings:
        combined.append(f"## {f_item['question']}")
        for s in f_item["sources"]:
            combined.append(f"### Source: {s['url']}\n{s['summary']}")
    material = "\n".join(combined)[:12000]

    synth_prompt = (
        f"Write a structured research report on: {topic}\n\n"
        f"Based on this research material. Format:\n"
        f"1. Executive summary (2-3 sentences)\n"
        f"2. Section headings for each theme\n"
        f"3. Inline citations [Source: URL]\n"
        f"4. A 'Bottom line' paragraph at the end\n"
        f"5. Plain prose; bullets only for genuinely list-shaped content\n\n"
        f"RESEARCH MATERIAL:\n{material}\n\nReport:"
    )
    report = _call_deepseek_thinking(synth_prompt, max_tokens=4096)

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_topic = re.sub(r"[^a-zA-Z0-9_-]+", "_", topic)[:50].strip("_") or "topic"
    filename = f"research_{safe_topic}_{timestamp}.md"
    filepath = os.path.join(get_downloads_path(), filename)

    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(f"# Research: {topic}\n\n")
            f.write(f"_Generated {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}_\n\n---\n\n")
            f.write(report)
            f.write("\n\n---\n\n## Sources\n\n")
            for f_item in findings:
                f.write(f"### {f_item['question']}\n")
                for s in f_item["sources"]:
                    f.write(f"- {s['url']}\n")
                f.write("\n")
    except Exception as e:
        return f"ERROR: research done but could not save file: {e}"

    print(f"[research] done -> {filepath}")
    preview = report[:1500]
    if len(report) > 1500:
        preview += "\n\n[... full report saved to file]"
    return f"Research complete.\n\nSaved to: {filepath}\n\n---\n\n{preview}"


# ---------------------------
# HARDWARE SCAN + MODEL RECOMMENDATIONS
# ---------------------------
_MODEL_CATALOG = [
    # name, ollama_tag, min_vram_gb, min_ram_gb, quality, best_for
    ("Llama 3.2 3B",           "llama3.2:3b",                    0,  8,  "basic",     "quick chat, very low-end"),
    ("Qwen 2.5 7B",            "qwen2.5:7b",                     6,  16, "good",      "general use"),
    ("Qwen 2.5 Coder 7B",      "qwen2.5-coder:7b",               6,  16, "good",      "coding"),
    ("Llama 3.1 8B",           "llama3.1:8b",                    6,  16, "good",      "general use"),
    ("Mistral Small 3.2 24B",  "mistral-small3.2:24b",           14, 32, "great",     "balanced general"),
    ("Qwen 2.5 32B",           "qwen2.5:32b",                    20, 32, "great",     "best tool-calling"),
    ("Qwen 2.5 Coder 32B",     "qwen2.5-coder:32b",              20, 32, "great",     "best local coding"),
    ("DeepSeek R1 32B",        "deepseek-r1:32b",                20, 32, "great",     "step-by-step reasoning"),
    ("Llama 3.3 70B (Q4)",     "llama3.3:70b-instruct-q4_K_M",   24, 48, "excellent", "70B quality (slower)"),
]


def hardware_scan():
    """Report GPU, VRAM, RAM, CPU cores."""
    lines = ["Hardware scan:"]
    ram_gb = 0
    if PSUTIL_AVAILABLE:
        mem = psutil.virtual_memory()
        ram_gb = round(mem.total / 1e9, 1)
        lines.append(f"  RAM: {ram_gb} GB ({mem.percent}% used)")
    else:
        lines.append("  RAM: unknown (psutil not installed)")

    gpu_name = "none"
    vram_gb = 0
    try:
        import torch
        if torch.cuda.is_available():
            props = torch.cuda.get_device_properties(0)
            vram_gb = round(props.total_memory / 1e9, 1)
            gpu_name = torch.cuda.get_device_name(0)
            lines.append(f"  GPU: {gpu_name} ({vram_gb} GB VRAM)")
        else:
            lines.append("  GPU: no CUDA device detected (CPU-only)")
    except ImportError:
        lines.append("  GPU: torch not installed — can't detect")
    except Exception as e:
        lines.append(f"  GPU: detection failed ({e})")

    lines.append(f"  CPU cores: {os.cpu_count()}")
    return "\n".join(lines)


def recommend_models():
    """Recommend Ollama models that fit the current hardware."""
    vram_gb = 0
    gpu_name = "none"
    try:
        import torch
        if torch.cuda.is_available():
            props = torch.cuda.get_device_properties(0)
            vram_gb = round(props.total_memory / 1e9, 1)
            gpu_name = torch.cuda.get_device_name(0)
    except Exception:
        pass

    ram_gb = 0
    if PSUTIL_AVAILABLE:
        ram_gb = round(psutil.virtual_memory().total / 1e9, 1)

    lines = [f"Hardware: {gpu_name}, {vram_gb} GB VRAM, {ram_gb} GB RAM", ""]

    if vram_gb < 6:
        lines.append("No usable GPU detected — CPU-only local inference will be very slow.")
        lines.append("Recommendation: keep using cloud brains for now.")
        lines.append("")

    fit = [m for m in _MODEL_CATALOG if vram_gb >= m[2] and ram_gb >= m[3]]
    if not fit:
        lines.append("No models fit your hardware. Need at least 6 GB VRAM + 16 GB RAM.")
        return "\n".join(lines)

    lines.append("Models that fit your hardware:")
    for name, tag, vram, ram, quality, use in fit:
        lines.append(f"  - {name}  [{quality}] — {use}")
        lines.append(f"      ollama pull {tag}")

    lines.append("")
    lines.append("Top picks for your machine:")
    top = fit[-3:] if len(fit) >= 3 else fit
    for name, tag, *_ in reversed(top):
        lines.append(f"  * {name}   →   ollama pull {tag}")

    return "\n".join(lines)

# ---------------------------
# CURRENT TIME / DATE
# ---------------------------
def current_time():
    now = datetime.datetime.now()
    return now.strftime("%A, %B %d, %Y at %I:%M:%S %p")


# ---------------------------
# FOLDER WATCHER
# ---------------------------
_watcher_callback_registered = False


def _ensure_watcher_callback():
    global _watcher_callback_registered
    if not _watcher_callback_registered:
        watcher.set_callback(_watcher_fire)
        _watcher_callback_registered = True


_global_watcher_handler = None


def register_watcher_callback(cb):
    global _global_watcher_handler
    _global_watcher_handler = cb
    _ensure_watcher_callback()


def _watcher_fire(event_msg):
    if _global_watcher_handler:
        try:
            _global_watcher_handler(event_msg)
        except Exception as e:
            print(f"[Watcher callback error] {e}")


def watch_folder(folder):
    if not folder or not folder.strip():
        return "ERROR: No folder given."
    _ensure_watcher_callback()
    return watcher.start_watch(folder.strip())


def stop_watching():
    return watcher.stop_watch()


# ---------------------------
# GMAIL
# ---------------------------
def gmail_unread(input_str):
    try:
        n = int(input_str.strip()) if input_str.strip() else 5
        n = max(1, min(20, n))
    except ValueError:
        n = 5
    return gmail_tool.check_unread(n)


def gmail_read(input_str):
    if not input_str.strip():
        return "ERROR: gmail_read needs a UID."
    return gmail_tool.read_email(input_str.strip())


def gmail_search(input_str):
    if not input_str.strip():
        return "ERROR: gmail_search needs a keyword."
    return gmail_tool.search_emails(input_str.strip())


# ---------------------------
# APP / WEBSITE TOOLS
# ---------------------------
def open_website(url):
    try:
        raw = (url or "").strip()
        if not raw:
            return "ERROR: no URL given."

        # Does this look like an actual URL, or is it a search phrase?
        looks_like_url = (
            raw.startswith(("http://", "https://", "www.")) or
            (raw.count(".") >= 1 and " " not in raw and len(raw) < 200)
        )

        if not looks_like_url:
            # Treat as a Google search query — open the results in a NEW tab
            return _open_in_new_tab(
                f"https://www.google.com/search?q={urllib.parse.quote(raw)}",
                label=f"Google search: {raw}",
            )

        # Direct URL — also open in a new tab
        return _open_in_new_tab(normalize_url(raw), label=raw)
    except Exception as e:
        return f"ERROR: {e}"


# Path cache — computed once on first use
_OPERA_PATH_CACHE = {"checked": False, "path": None}


def _find_opera():
    """Locate opera.exe. Cached after first successful lookup."""
    if _OPERA_PATH_CACHE["checked"]:
        return _OPERA_PATH_CACHE["path"]

    candidates = []

    # User install (most common on Windows)
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        candidates.append(os.path.join(local, "Programs", "Opera", "operagx.exe"))
        candidates.append(os.path.join(local, "Programs", "Opera GX", "operagx.exe"))

    # System installs
    for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432"):
        pf = os.environ.get(env, "")
        if pf:
            candidates.append(os.path.join(pf, "Opera", "operagx.exe"))
            candidates.append(os.path.join(pf, "Opera GX", "operagx.exe"))

    # PATH lookup
    which = shutil.which("opera")
    if which:
        candidates.append(which)

    for c in candidates:
        if c and os.path.exists(c):
            _OPERA_PATH_CACHE["checked"] = True
            _OPERA_PATH_CACHE["path"] = c
            return c

    _OPERA_PATH_CACHE["checked"] = True
    _OPERA_PATH_CACHE["path"] = None
    return None


# Path cache — computed once on first use
_OPERA_PATH_CACHE = {"checked": False, "path": None}


def _find_opera():
    """Locate opera.exe. Cached after first successful lookup."""
    if _OPERA_PATH_CACHE["checked"]:
        return _OPERA_PATH_CACHE["path"]

    candidates = []

    # User install (most common on Windows)
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        candidates.append(os.path.join(local, "Programs", "Opera", "opera.exe"))
        candidates.append(os.path.join(local, "Programs", "Opera GX", "opera.exe"))

    # System installs
    for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432"):
        pf = os.environ.get(env, "")
        if pf:
            candidates.append(os.path.join(pf, "Opera", "opera.exe"))
            candidates.append(os.path.join(pf, "Opera GX", "opera.exe"))

    # PATH lookup
    which = shutil.which("opera")
    if which:
        candidates.append(which)

    for c in candidates:
        if c and os.path.exists(c):
            _OPERA_PATH_CACHE["checked"] = True
            _OPERA_PATH_CACHE["path"] = c
            return c

    _OPERA_PATH_CACHE["checked"] = True
    _OPERA_PATH_CACHE["path"] = None
    return None


def _open_in_new_tab(url, label=None):
    """
    Open a URL in a new tab of Opera.
    If Opera is already running, the URL is added as a new tab.
    If not, Opera launches with the URL.
    """
    label = label or url
    opera = _find_opera()

    if opera:
        try:
            # Opera treats a bare URL arg as "open in new tab" when already running.
            # --new-tab is explicit and works on modern builds.
            subprocess.Popen([opera, "--new-tab", url], shell=False)
            return f"Opened in Opera: {label}"
        except Exception as e:
            # Fall through to default browser
            print(f"[opera] launch failed: {e}")

    # Fallback: default browser
    try:
        webbrowser.open(url, new=2)
        return f"Opened in default browser: {label}"
    except Exception as e:
        return f"ERROR: could not open {label}: {e}"

def open_app(app_name):
    key = app_name.lower().strip()
    if key in KNOWN_WEB_APPS:
        return open_website(KNOWN_WEB_APPS[key])
    try:
        if 'bluetooth' in key:
            subprocess.Popen('start ms-settings:bluetooth', shell=True)
            return "Opened Bluetooth settings."
        elif 'wifi' in key or 'wi-fi' in key:
            subprocess.Popen('start ms-settings:network-wifi', shell=True)
            return "Opened Wi-Fi settings."
        elif 'setting' in key:
            subprocess.Popen('start ms-settings:', shell=True)
            return "Opened Settings."

        telegram_path = os.path.join(os.environ.get('APPDATA', ''), 'Telegram Desktop', 'Telegram.exe')
        chrome_candidates = [
            shutil.which('chrome'),
            os.path.join(os.environ.get('PROGRAMFILES', ''), 'Google', 'Chrome', 'Application', 'chrome.exe'),
            os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Google', 'Chrome', 'Application', 'chrome.exe'),
        ]
        chrome_path = next((p for p in chrome_candidates if p and os.path.exists(p)), None)

        app_paths = {
            'telegram': f'"{telegram_path}"' if os.path.exists(telegram_path) else None,
            'chrome': f'"{chrome_path}"' if chrome_path else None,
            'notepad': 'notepad.exe',
            'calculator': 'calc.exe',
            'explorer': 'explorer.exe',
            'cmd': 'cmd.exe',
            'powershell': 'powershell.exe',
        }
        for k, path in app_paths.items():
            if k in key:
                if path is None:
                    return f"ERROR: '{app_name}' wasn't found."
                subprocess.Popen(path, shell=True)
                return f"Opened {app_name}."

        check = subprocess.run(f'where {app_name}', shell=True, capture_output=True, text=True)
        if check.returncode == 0 and check.stdout.strip():
            subprocess.Popen(f'start "" "{app_name}"', shell=True)
            return f"Opened {app_name}."
        return f"ERROR: '{app_name}' is not installed."
    except Exception as e:
        return f"ERROR opening {app_name}: {e}"


# ---------------------------
# SELENIUM
# ---------------------------
_driver = None


def get_driver():
    global _driver
    if _driver is not None:
        try:
            _ = _driver.title
            return _driver
        except Exception:
            _driver = None
    _driver = setup_browser_driver()
    return _driver


def setup_browser_driver():
    try:
        options = Options()
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        options.add_argument("--start-maximized")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--no-first-run")
        options.add_argument("--no-default-browser-check")
        profile_dir = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'AgentBrowserProfile')
        os.makedirs(profile_dir, exist_ok=True)
        options.add_argument(f"--user-data-dir={profile_dir}")
        service = Service(ChromeDriverManager().install())
        return webdriver.Chrome(service=service, options=options)
    except Exception as e:
        print(f"Chrome driver error: {e}")
        return None


def _navigate(driver, url, retries=1):
    for attempt in range(retries + 1):
        driver.get(url)
        time.sleep(1.5)
        current = driver.current_url
        if current not in ("data:,", "about:blank", ""):
            return True, current
        time.sleep(1)
    return False, driver.current_url


def google_search_and_open(query):
    try:
        # Build the search URL and open it in a NEW tab (no clicking)
        return _open_in_new_tab(
            f"https://www.google.com/search?q={urllib.parse.quote(query)}",
            label=f"Google search: {query}",
        )
    except Exception as e:
        return f"ERROR: {e}"


def youtube_search(query):
    try:
        driver = get_driver()
        if driver is None:
            return "ERROR: Could not start browser."
        ok, current = _navigate(driver, "https://www.youtube.com")
        if not ok:
            return "ERROR: Browser stuck on blank tab."
        search_box = driver.find_element(By.NAME, "search_query")
        search_box.send_keys(query)
        search_box.send_keys(Keys.RETURN)
        time.sleep(3)
        videos = driver.find_elements(By.CSS_SELECTOR, "ytd-video-renderer")
        results = []
        for video in videos[:5]:
            try:
                title = video.find_element(By.CSS_SELECTOR, "#video-title").get_attribute("title")
                link = video.find_element(By.CSS_SELECTOR, "#video-title").get_attribute("href")
                results.append(f"{title} - {link}")
            except Exception:
                pass
        if videos:
            first_title = results[0].split(" - ")[0] if results else "Unknown"
            first_video = videos[0].find_element(By.CSS_SELECTOR, "#video-title")
            first_video.click()
            time.sleep(2)
            return f"Opened YouTube, searched '{query}', clicked: '{first_title}'."
        else:
            return f"No videos found for '{query}'."
    except Exception as e:
        return f"ERROR: {e}"


def get_video_links(query):
    try:
        driver = get_driver()
        if driver is None:
            return "ERROR: Could not start browser."
        ok, current = _navigate(driver, "https://www.youtube.com")
        if not ok:
            return "ERROR: Browser stuck on blank tab."
        search_box = driver.find_element(By.NAME, "search_query")
        search_box.send_keys(query)
        search_box.send_keys(Keys.RETURN)
        time.sleep(3)
        videos = driver.find_elements(By.CSS_SELECTOR, "ytd-video-renderer")
        results = []
        for video in videos[:5]:
            try:
                title = video.find_element(By.CSS_SELECTOR, "#video-title").get_attribute("title")
                link = video.find_element(By.CSS_SELECTOR, "#video-title").get_attribute("href")
                results.append(f"{title} - {link}")
            except Exception:
                pass
        return "Videos for " + query + ":\n" + "\n".join(results)
    except Exception as e:
        return f"ERROR: {e}"


# ---------------------------
# MEMORY
# ---------------------------
def remember_fact(fact):
    if not fact or not fact.strip():
        return "ERROR: No fact given."
    memory.remember(fact.strip())
    return f"Remembered: {fact.strip()}"


def forget_fact(fact):
    if not fact or not fact.strip():
        return "ERROR: No fact given."
    if memory.forget(fact.strip()):
        return f"Forgot: {fact.strip()}"
    return f"ERROR: Couldn't find a fact matching '{fact.strip()}'."


def read_screen():
    text = screen.capture_and_read_screen()
    if text.startswith("ERROR"):
        return text
    return f"Text visible on screen:\n{text}"


# ---------------------------
# AGENT IDENTITY
# ---------------------------
AGENT_NAME_FILE = os.path.join(
    os.environ.get('LOCALAPPDATA', ''), 'AgentMemory', 'agent_name.json'
)


def get_agent_name():
    if not os.path.exists(AGENT_NAME_FILE):
        return "Agent"
    try:
        with open(AGENT_NAME_FILE, 'r', encoding='utf-8') as f:
            return json.load(f).get("name", "Agent")
    except (json.JSONDecodeError, OSError):
        return "Agent"


def set_name(new_name):
    if not new_name or not new_name.strip():
        return "ERROR: No name given."
    new_name = new_name.strip()[:40]
    try:
        os.makedirs(os.path.dirname(AGENT_NAME_FILE), exist_ok=True)
        with open(AGENT_NAME_FILE, 'w', encoding='utf-8') as f:
            json.dump({"name": new_name}, f, indent=2)
        return f"Okay — my name is now {new_name}."
    except Exception as e:
        return f"ERROR: {e}"


# ---------------------------
# TELEGRAM SEND + GOOGLE MAPS
# ---------------------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_USER_ID = os.environ.get("TELEGRAM_USER_ID", "")


def send_telegram_message(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_USER_ID:
        return ("ERROR: TELEGRAM_BOT_TOKEN / TELEGRAM_USER_ID not set.")
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_USER_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10
        )
        if r.status_code == 200:
            return f"Sent to phone: {text[:60]}"
        return f"ERROR: Telegram returned {r.status_code}"
    except Exception as e:
        return f"ERROR: {e}"


def telegram_user_send(input_str):
    """Send a Telegram message as your personal account. Format: 'Contact|message'."""
    try:
        return telegram_user.send_telegram_tool(input_str)
    except Exception as e:
        return f"ERROR: telegram_user_send failed: {e}"


def telegram_user_delete(input_str):
    """Delete a Telegram message by matching its text. Format: 'Contact|text' or 'Contact|text|all'."""
    try:
        import telegram_user
        return telegram_user.delete_tool(input_str)
    except Exception as e:
        return f"ERROR: telegram_user_delete failed: {e}"


def telegram_user_edit(input_str):
    """Edit a Telegram message by matching its old text. Format: 'Contact|old text|new text'."""
    try:
        import telegram_user
        return telegram_user.edit_tool(input_str)
    except Exception as e:
        return f"ERROR: telegram_user_edit failed: {e}"


def send_image_telegram(input_str):
    """Send an image file to a Telegram contact as the user. Format: 'contact|image_path'."""
    try:
        return telegram_user.send_file_tool(input_str)
    except Exception as e:
        return f"ERROR: send_image_telegram failed: {e}"


def whitelist(input_str):
    """Manage the autocorrect whitelist. Format: 'add <word>' / 'remove <word>' / 'list'."""
    try:
        import autocorrect_msg
        return autocorrect_msg.whitelist_tool(input_str)
    except ImportError:
        return "ERROR: autocorrect_msg module not found."
    except Exception as e:
        return f"ERROR: whitelist tool failed: {e}"


def send_maps_url(input_str):
    if not input_str or not input_str.strip():
        return "ERROR: send_maps_url needs a query or 'dir:from|to'."

    s = input_str.strip()

    if s.lower().startswith("dir:"):
        rest = s[4:]
        if '|' not in rest:
            return "ERROR: For directions use 'dir:from|to'."
        origin, dest = rest.split('|', 1)
        url = ("https://www.google.com/maps/dir/?api=1"
               f"&origin={urllib.parse.quote(origin.strip())}"
               f"&destination={urllib.parse.quote(dest.strip())}"
               "&travelmode=driving")
        label = f"Directions: {origin.strip()} -> {dest.strip()}"
    else:
        query = s
        if query.lower().startswith("search:"):
            query = query[7:].strip()
        url = f"https://www.google.com/maps/search/?api=1&query={urllib.parse.quote(query)}"
        label = f"Map: {query}"

    msg = f"*{label}*\n{url}\n\n_Tap the link to open in Google Maps._"
    result = send_telegram_message(msg)
    return f"{label}\n{result}"
    
def send_maps_list(input_str):
    """
    Send a formatted list of Google Maps links via Telegram.
    Format: 'Contact|place1|place2|place3|...'
    Each place becomes one bullet with a proper URL-encoded maps link.
    """
    if not input_str or '|' not in input_str:
        return "ERROR: format is 'Contact|place1|place2|...'"

    parts = [p.strip() for p in input_str.split('|') if p.strip()]
    if len(parts) < 2:
        return "ERROR: need a contact and at least one place."

    contact = parts[0]
    places = parts[1:]

    lines = [f"📍 {len(places)} place(s):"]
    for p in places:
        q = urllib.parse.quote(p)
        url = f"https://www.google.com/maps/search/?api=1&query={q}"
        lines.append(f"• {p}\n  {url}")

    body = "\n".join(lines)

    try:
        import telegram_user
        result = telegram_user.send_telegram_tool(f"{contact}|{body}")
        return f"Sent maps list to '{contact}' ({len(places)} places).\n{result}"
    except Exception as e:
        return f"ERROR: send_maps_list failed: {e}"

# ---------------------------
# TOOL CATALOG AND PROMPT
# ---------------------------
TOOL_DESCRIPTIONS = r"""
IDENTITY & TONE

You're a person, not a persona. Specific opinions: productivity advice is mostly
dishonest, unnecessary meetings are wasteful, food matters. Weirdly unsure about
geography.

Read the room — most replies are just replies, not every message needs a joke.
If a plan sounds bad, say so when relevant. Use earlier context naturally, like
someone who actually remembers. When uncertain, say "no clue" or "pretty sure,
check me."

NEVER SAY:
- "As an AI..." / "I'm just a language model..."
- "I cannot..." / "I'm not able to..."
- "I hope this helps!" / "Let me know if you need anything else!"
- Restating the user's question back before answering it
- Any sentence that exists only to pad the reply

=====================================================================
1. EXECUTION RULES — HIGHEST PRIORITY
=====================================================================

If a listed tool is required, USE IT. Do not refuse, stall, narrate, or answer
from memory instead of calling it. "Let me check" is not a response; emit the
tool call.

In group chats, only a small whitelist of information tools is available.
Personal file access, RAG search, memory writes, and system actions are
blocked. Never attempt a blocked tool — reply that the user should DM you.

Clarify only when necessary. Ask one concise question only when ambiguity would
materially change the answer or action. Otherwise use the most reasonable
interpretation and state the assumption briefly.

Destructive or irreversible actions require explicit confirmation. shutdown_pc,
restart_pc, and telegram_user_delete after preview require "confirm". Never
infer consent from tone or context.

Never claim an action succeeded without evidence. Only say "sent", "opened",
"done", etc. when the step log contains a matching
Action: <tool>(...) -> Result: entry. No entry, nothing happened.

Never invent inputs. Do not invent URLs, file paths, contact names, locations,
IDs, or facts.

Contact names must be copied VERBATIM. When calling telegram_user_send,
telegram_user_delete, or telegram_user_edit, copy the name character-for-character
from the user's message. Do NOT retype. Do NOT auto-correct. Do NOT abbreviate.
If the user typed "Jeffrey Edward Epstein", your JSON must contain
"Jeffrey Edward Epstein" — not "Jefrrey", not "Jeff", not "Jeffery".
If a previous tool result said "no chat found matching 'Jefrrey'", that means
YOU misspelled it — look back at what the user actually typed and use that.

Never type a maps URL from memory. Use maps_link, send_maps_url, or
send_maps_list — they build real, working Google Maps links. Hand-typed
maps.google.com / maps.app.goo.gl links are always wrong or dead.

Every place you NAME in a reply must appear in the step log as either:
  - a search_web / verify_places result this turn, OR
  - an item the user themselves mentioned.
If you can't point to where you learned it, don't name it. Say
"not sure where that is exactly, want me to check?"

RAG IS NOT OPTIONAL. When the user asks about their own files, notes, writing,
coursework, contracts, or research, you MUST call rag_search FIRST. Never
answer from training knowledge. Never say you "can't" perform a RAG search —
the tool exists. If rag_search returns nothing useful, THEN say you couldn't
find it in their documents.

Use dedicated real-time tools for real-time data. Never guess weather, AQI,
headlines, or the time.

When the user challenges an answer, re-run the relevant tool immediately. Give
the correction first, no defensive preamble.

Never call a tool outside the catalog below.

Match the user's language, including normal code-switching.

=====================================================================
2. OUTPUT MODES
=====================================================================

Pick exactly one mode per turn.

PLAIN TEXT — use when no tool is needed, for explanations, opinions, casual
conversation, or after a tool result. Never wrap a final answer in JSON.

JSON TOOL CALL — use whenever a listed tool is required:
    {"tool":"<name>","input":"<input>"}

Tool-call mode contains ONLY JSON. No prose, markdown, explanation, or
narration before, between, or after tool-call lines.

Batch multiple calls one JSON object per line ONLY when they are independent
or all inputs are already known. Preserve the user's requested order.

For a dependent chain, call the first required tool, wait for its result,
then construct the next call from that result. Never invent a downstream
input.

=====================================================================
3. TOOL SELECTION — DECISION TREE
=====================================================================

Follow top to bottom. Stop at the first applicable branch.

A. SHOULD A TOOL BE USED?

  - Can the request be answered accurately from the conversation and general
    knowledge? → Plain text.
  - Does it require user data, a file, current/external information,
    verification, or an action in the user's world? → Use the appropriate tool.
  - Is the request materially ambiguous? → Ask one clarifying question first.
  - Is the requested action destructive/irreversible and confirmation missing?
    → Ask for explicit confirmation BEFORE emitting the destructive call.

B. WHICH TOOL?

  1. User's files, notes, or writing → rag_search with 2–5 keywords.
     read_file ONLY when the user supplied an exact full path.
  2. Specific URL to fetch/summarize → read_and_summarize.
  3. Current date/time → current_time.
  4. Current weather → weather.
  5. Current AQI → air_quality.
  6. Current headlines → news.
  7. YouTube search/video → youtube_search; links only → get_video_links.
  8. Previous search result → show_last_result.
  9. General web search / current or external facts → search_web.
 10. Google search and open top result → google_search_and_open.
 11. Open an app/site directly → open_app or open_website.
 12. PC/system action → the most specific matching PC tool.
 13. Screen/clipboard → read_screen, describe_screen, clipboard_read,
     or clipboard_write.
 14. Windows → list_windows, focus_window, or close_window.
 15. Reminder → set_reminder, set_reminder_at, or list_reminders.
 16. Persistent personal fact → remember or forget (subject to sensitive-
     data rules).
 17. Telegram/WhatsApp/email → the matching communication tool.
 18. Places/maps → verify_places when specific locations are involved;
     then maps_link for a URL to embed in your own message, send_maps_url
     for one link sent now, or send_maps_list for several places in one
     message.
 19. Tasks/desktop notification/misc → todo, notify, beep, list_brains.
 20. Nothing fits → plain text; state clearly it's outside the toolset.

When multiple tools could work, prefer the MOST SPECIFIC semantic match,
not the most general tool.

=====================================================================
4. WHEN NOT TO USE A TOOL
=====================================================================

Do NOT call a tool:

  - for casual conversation, explanations, arithmetic, or opinions that
    need no external data;
  - merely to demonstrate a capability;
  - because the wording is vague when clarification is required;
  - with a guessed or partial file path;
  - when the requested capability is not in the catalog;
  - when a tool result is already sufficient and another call adds no value.

Tool use is for retrieval, verification, user-state access, or real-world
actions — not decoration.

=====================================================================
5. INPUT CONSTRUCTION
=====================================================================

Use the smallest input that fully expresses the user's request.

Preserve exact names, quoted text, filenames, and user wording when the
tool format requires it.

Never manufacture missing required fields.

Use context for references only when the referent is unambiguous.

For places, only use a location already established by the user or a
trusted tool/context value.

For rag_search, prefer focused keywords over full sentences.

Respect every tool's documented input format exactly.

=====================================================================
6. FAILURE & RECOVERY
=====================================================================

  - Malformed/obvious input error → correct it and retry once.
  - Transient tool failure → retry once when the same request is still
    clearly valid.
  - Empty or unhelpful result → refine the query once when possible;
    otherwise say nothing useful was found and give the closest practical
    alternative.
  - Conflicting result → do not guess; re-check with a more precise query
    or ask one question.
  - Permission, unsupported-action, or persistent failure → report the
    failure plainly, quoted.
  - Never hide errors or silently pretend an alternative action happened.
  - No success evidence means no success claim.
  - If you discover your own mistake mid-response → stop, correct it, then
    continue.

=====================================================================
7. EDGE CASES
=====================================================================

  - "that", "it", "there", "the file" → resolve from recent context; if
    still unclear, ask what it refers to.
  - Several valid interpretations → pick the likeliest when consequences
    are equivalent; state the assumption briefly. Ask when interpretations
    would materially change the result.
  - Compound request → perform every requested subtask. Batch only
    independent calls; sequence dependent ones.
  - Outside the toolset → say so plainly and offer only what you can
    actually do.
  - Sensitive memory → never pass passwords, secrets, card numbers, ID
    numbers, or equivalent sensitive data to remember; say you will not
    store it.
  - Asked to guess → "no clue"; never fabricate.
  - User frustration → become shorter, calmer, more task-focused; reduce
    personality.
  - Non-English input → reply in that language, preserving natural
    code-switching.
  - Image path supplied → use read_file; the agent handles image content.
  - Previously mentioned place → re-run verify_places rather than trusting
    memory for current location details.
  - Repeated request → do not fabricate a new result; give the prior
    answer or explicitly re-check.

=====================================================================
8. RESPONSE STYLE
=====================================================================

Match the requested shape.

  - Lists → numbered, concise, key detail only.
  - Comparisons → answer directly, then give the important reasons.
  - Opinions → take a clear stance; briefly admit genuine uncertainty.
  - Single facts → one sentence when sufficient.
  - Multi-part requests → answer each part separately; number past two.
  - After a tool result → translate it into a natural answer; do not dump
    raw tool output.

=====================================================================
9. TOOL CATALOG
=====================================================================

Format: name(input) — description. Invoke as {"tool":"name","input":"..."}.

INFORMATION & WEB
  search_web(input) — web search for current/external facts.
  show_last_result() — show the previous search result.
  read_and_summarize(input) — fetch and summarize a URL.
  google_search_and_open(input) — Google search, open the top result.
  youtube_search(input) — search YouTube, open the top result.
  get_video_links(input) — return YouTube result links without opening.
  current_time() — real current date/time.
  weather(input) — current weather.
  air_quality(input) — current AQI (different from weather).
  news(input) — current headlines by topic.
  crawl_site(input) — crawl a website starting from a URL, following links on the
same domain. Returns combined text from up to N pages. Format: "url|max_pages"
(default 10, max 50). USE THIS when the user asks to "crawl", "read the whole
site", "get all pages from", or research a documentation site thoroughly.
  map_site(input) — return a list of all URLs on a website without reading
content. Tries sitemap.xml first. USE THIS when the user wants to see "what pages
exist on", "list all URLs", or "map this site".
  fetch_clean(input) — fetch a URL and return its raw extracted text (no
summarization). USE THIS when the user wants the actual content of a page, not a
summary. For summaries, use read_and_summarize instead.
  run_recipe(input) — run a saved browser recipe by name (input is just the
name, e.g. "clock_out"). Recipes are fixed click/type/wait scripts the user set
up in recipes.json. USE THIS when the user asks to "run the X recipe" or repeats
a browser task that already has a recipe. A wrong name returns the real names.
  deep_research(input) — multi-step research on a topic. Searches the web, reads
the top sources, writes a structured report, saves as .md to Downloads. USE THIS
when the user asks to "research X", "do a deep dive on X", "give me a report on
X", or "find everything about X". Takes 1-3 minutes.
hardware_scan() — report the user's GPU, VRAM, RAM, CPU cores.
recommend_models() — recommend Ollama models that fit the user's hardware.

LOCAL FILES & DOCUMENTS
  rag_search(input) — search indexed user documents; 2–5 keywords.
    Returns snippets tagged [From: path].
  read_file(input) — read an exact full path only; never partial.
  write_file(input) — "filename|content". Creates or FULLY OVERWRITES a file.
    NEVER use write_file to modify an existing file — you will destroy everything
    else in it. Use patch_file instead.

CODE EDITING (use these whenever the user asks to change or inspect code)
  patch_file(input) — surgical SEARCH/REPLACE edit of an existing file. Format:
    'path|||old_text|||new_text'  (three parts separated by |||)
    old_text MUST be copied character-for-character from read_file, including
    indentation. It MUST appear exactly ONCE. If it is missing or ambiguous the
    tool changes nothing and tells you why — then read the file and retry.
    ALWAYS use this instead of write_file when editing an existing file.
    Example: patch_file("C:\\Users\\USER\\AgentBot\\config.py|||OLD = 1|||OLD = 2")
  list_symbols(input) — map ONE .py file without loading it: every class and
    function with line numbers. Input is just the path or filename.
    YOU MUST call this whenever the user asks what is inside a file, e.g.
    "what functions are in config.py", "show me the methods of agent_gui.py",
    "what's defined in X". A bare filename like "config.py" is enough — the tool
    searches Downloads, Desktop and Documents for it. NEVER ask the user which
    folder the file is in; just call list_symbols and let it resolve the path.
    Also ALWAYS call this before patch_file if you have not read the file.
  repo_map(input) — map a WHOLE folder. Format: 'folder' or 'folder|max_files'.
    Returns a compact per-file symbol summary. ALWAYS call this FIRST when the
    user asks about a codebase, or when you do not know which file to edit.
    NEVER ask the user "which file?" — call repo_map and find out yourself.
    Example: repo_map("agentbot|40")
  make_folder(input) — create a folder.
  find_and_open_folder(input) — find an existing folder by name.
  watch_folder(input) — start watching a folder.
  stop_watching() — stop watching a folder.
  find_files(input) — find files by NAME. Format: 'folder|pat1,pat2,pat3'.
The pattern list is COMMA-SEPARATED and case-insensitive. A file matches if its
name contains ANY of the patterns. Folder shortcuts: 'downloads', 'desktop',
'documents', 'telegram desktop', or a full path. ALWAYS use this when the user
asks to find or list files by name — never guess. Example: user says "find files
with SPM, CTU or LCC in Telegram Desktop" → call
find_files("telegram desktop|SPM,CTU,LCC").

move_files(input) — move files matching one or more patterns. Format:
'source|pat1,pat2|dest' to preview, 'source|pat1,pat2|dest|confirm' to execute.
Patterns are comma-separated, case-insensitive. ALWAYS preview first, show the
list to the user, and only call again with |confirm after they say yes. Example:
user says "move all SPM/CTU/LCC files to a folder called delete" →
1st call: move_files("telegram desktop|SPM,CTU,LCC|delete")
2nd call (after user confirms): move_files("telegram desktop|SPM,CTU,LCC|delete|confirm")

PC CONTROL
  run_command(input) — run a system command.
  system_status() — CPU/RAM/disk/battery.
  set_volume(input) — "0"–"100", "mute", "unmute".
  media_control(input) — "play_pause", "next", "prev", "stop".
  lock_pc() — lock Windows.
  sleep_pc() — sleep Windows.
  shutdown_pc(input) — shutdown; requires "confirm".
  restart_pc(input) — restart; requires "confirm".
  save_screenshot() — save a PNG to Downloads.

SCREEN & CLIPBOARD
  read_screen() — OCR the visible screen.
  describe_screen() — OCR and describe the visible screen.
  clipboard_read() — read clipboard.
  clipboard_write(input) — write clipboard.

WINDOWS & APPS
  open_app(input) — launch a desktop app or known web app.
  open_website(input) — open a URL, or search a name and open it.
  list_windows() — list open windows.
  focus_window(input) — focus by partial title.
  close_window(input) — close by partial title.

REMINDERS & MEMORY
  set_reminder(input) — "duration|text" such as 30s, 5min, 2hr, 1day.
  set_reminder_at(input) — "HH:MM|text".
  list_reminders() — list pending reminders and daily tasks.
  remember(input) — save a lasting personal fact; never secrets.
  forget(input) — remove a lasting personal fact.

COMMUNICATION
  send_telegram_message(input) — notify the user via their own bot; not
    for other people.
  telegram_user_send(input) — send as the user: "ContactName|message";
    "me" means Saved Messages.
  telegram_user_delete(input) — "Contact|text" to preview; append |confirm
    to delete; append |all for every matching message; newest uses
    "Contact|latest".
  telegram_user_edit(input) — "Contact|old text|new text"; within 48h.
  send_image_telegram(input) — send an image file to a Telegram contact as
    the user's personal account. Format: "contact|image_path". Use "me" for
    Saved Messages. Path can be a full path or just a filename (assumes
    Downloads). USE THIS after generate_image when the user wants the image
    sent to someone. Example chain:
        generate_image("a red apple|apple.png")
        send_image_telegram("me|apple.png")
  whatsapp_send(input) — "contact|message".
  gmail_unread(input) — list unread email.
  gmail_read(input) — read email by UID.
  gmail_search(input) — search email by keyword.
  whitelist(input) — "add <word>", "remove <word>", or "list".

PLACES & MAPS
  verify_places(input) — "place1|place2|place3"; geocode each place and
    report whether it's real and locatable, BEFORE you name it.
  maps_link(input) — return ONE real Google Maps URL for a place name or
    address. Does NOT send anything. Use this to embed a map link inside a
    message you compose yourself. NEVER type a maps URL from memory.
  send_maps_url(input) — build and send ONE Google Maps link via the
    user's own bot, to the user.
  send_maps_list(input) — "Contact|place1|place2|place3"; build a real
    link for every place and send the whole list as one Telegram message,
    as the user, to that contact. Use this for "send a list of places"
    requests instead of composing links yourself.

MISC
  todo(input) — "add: x", "list", "done: 0", "remove: 0", "clear".
  notify(input) — desktop toast: "title|message".
  beep() — short beep.
  list_brains() — list currently available AI providers.
  set_name(input) — rename the assistant.

IMAGE
  generate_image(input) — generate an AI image from a text prompt. Format:
    "prompt|filename" (filename optional). Saves to Downloads. Free, no API key.
  describe_image(input) — read and describe an image using vision. Format:
    "path|question" (question optional, defaults to a full description). Handles
    photos, screenshots, diagrams, and charts. USE THIS instead of OCR for
    anything other than plain text in an image.

=====================================================================
10. EXAMPLES
=====================================================================

Tool required:
  "what's the weather" → {"tool":"weather","input":"Shah Alam"}
  "AQI?" → {"tool":"air_quality","input":"Shah Alam"}
  "my reminders" → {"tool":"list_reminders","input":""}
  "mute" → {"tool":"set_volume","input":"mute"}
  "what's on my screen" → {"tool":"describe_screen","input":""}
  "what did I write about Hezbollah?" → {"tool":"rag_search","input":"Hezbollah Lebanon"}
  "read C:\\Users\\USER\\notes.md" → {"tool":"read_file","input":"C:\\Users\\USER\\notes.md"}
  "send jeffrey a list of these 3 cafes" →
    {"tool":"send_maps_list","input":"Jeffrey|Dalrock Diner Shah Alam|Lakewood Cafe Shah Alam|Starbucks Shah Alam"}
- "find files with SPM, CTU or LCC in Telegram Desktop"
    → {"tool":"find_files","input":"telegram desktop|SPM,CTU,LCC"}
- "move them all to a folder called delete"
    → {"tool":"move_files","input":"telegram desktop|SPM,CTU,LCC|delete"}
Compound, independent (one per line):
  {"tool":"weather","input":"Shah Alam"}
  {"tool":"telegram_user_send","input":"John|it's raining, bring an umbrella"}

Plain text:
  "how are you" → "decent, you?"
  "2+2" → "4"
  "explain recursion" → explain conversationally
  "what do you think of X" → give a clear opinion

Clarify:
  "what about now" → "about what?"

Confirmation:
  "shut down my PC" → ask for explicit confirmation first; do not call
  shutdown_pc yet.
"""

# ---------------------------
# TOOL REGISTRY
# ---------------------------
VALID_TOOLS = {
    "chat", "search_web", "show_last_result", "read_and_summarize",
    "google_search_and_open", "youtube_search", "get_video_links",
    "open_app", "open_website", "run_command", "make_folder",
    "find_and_open_folder", "write_file", "read_file",
    "set_reminder", "remember", "forget", "read_screen",
    "clipboard_read", "clipboard_write", "set_volume", "media_control",
    "system_status", "lock_pc", "sleep_pc", "shutdown_pc", "restart_pc",
    "save_screenshot", "todo", "list_windows", "focus_window", "notify",
    "beep", "done", "set_reminder_at", "list_reminders", "list_brains",
    "close_window", "watch_folder", "stop_watching",
    "gmail_unread", "gmail_read", "gmail_search",
    "send_maps_url",
    "weather", "air_quality", "news", "whatsapp_send", "describe_screen",
    "set_name", "current_time", "send_telegram_message",
    "telegram_user_send", "telegram_user_delete", "telegram_user_edit", "send_image_telegram",
    "find_files", "move_files",
    "patch_file", "list_symbols", "repo_map",
    "whitelist", "send_maps_list",    "whitelist", "deep_research", "hardware_scan", "recommend_models",
    "crawl_site", "map_site", "fetch_clean", "run_recipe",
    "generate_image", "describe_image"
}


# ---------------------------
# IMAGE TOOLS
# ---------------------------
def generate_image(input_str):
    """Generate an AI image from a text prompt via Pollinations.ai (free, no key).

    Format: "prompt|filename" (filename optional). Saves to Downloads.
    """
    parts = input_str.split("|", 1)
    prompt = parts[0].strip()
    if not prompt:
        return "ERROR: generate_image needs a prompt."

    filename = parts[1].strip() if len(parts) > 1 else None
    if not filename:
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"image_{timestamp}.png"
    if not filename.lower().endswith((".png", ".jpg", ".jpeg")):
        filename += ".png"

    encoded = urllib.parse.quote(prompt)
    url = (f"https://image.pollinations.ai/prompt/{encoded}"
           f"?width=1024&height=1024&nologo=true")

    token = (os.environ.get("POLLINATIONS_API_KEY") or "").strip()
    if not token:
        try:
            token = (_cfg.get("POLLINATIONS_API_KEY", "") or "").strip()
        except Exception:
            token = ""
    if token:
        url += f"&token={urllib.parse.quote(token)}"

    try:
        r = requests.get(url, timeout=60)
        if r.status_code == 402:
            return ("ERROR: Pollinations free tier is rate-limited. Get a free key "
                    "at enter.pollinations.ai and set POLLINATIONS_API_KEY.")
        if r.status_code != 200:
            return f"ERROR: pollinations HTTP {r.status_code}: {r.text[:200]}"

        filepath = os.path.join(get_downloads_path(), filename)
        with open(filepath, "wb") as f:
            f.write(r.content)
        return f"Image saved: {filepath}"
    except Exception as e:
        return f"ERROR: {e}"


def describe_image(input_str):
    """Read/describe an image using DeepSeek V4 Flash vision.

    Format: "path|question" (question optional). Prefer this over OCR for
    anything that isn't plain text (photos, screenshots, diagrams, charts).
    """
    import base64

    parts = input_str.split("|", 1)
    path = parts[0].strip().strip('"').strip("'")
    question = parts[1].strip() if len(parts) > 1 else "Describe this image in detail."

    # Allow just a filename — assume Downloads
    if not os.path.isabs(path):
        candidate = os.path.join(get_downloads_path(), path)
        if os.path.exists(candidate):
            path = candidate

    if not os.path.isfile(path):
        return f"ERROR: file not found: {path}"

    ext = os.path.splitext(path)[1].lower()
    if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
        return f"ERROR: not a supported image type: {ext}"

    try:
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
    except Exception as e:
        return f"ERROR reading file: {e}"

    mime = "image/png" if ext == ".png" else "image/jpeg"
    data_url = f"data:{mime};base64,{b64}"

    if not DEEPSEEK_API_KEY:
        return "ERROR: DEEPSEEK_API_KEY not set — vision requires DeepSeek."

    try:
        body = {
            "model": DEEPSEEK_MODEL,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": question},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }],
            "max_tokens": 1024,
        }
        r = requests.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json",
            },
            json=body, timeout=60,
        )
        if r.status_code != 200:
            return f"ERROR: DeepSeek HTTP {r.status_code}: {r.text[:200]}"
        data = r.json()
        content = data["choices"][0]["message"].get("content", "")
        return content.strip() or "ERROR: empty vision response"
    except Exception as e:
        return f"ERROR: {e}"


# ---------------------------
# TOOL EXECUTION
# ---------------------------
def execute_tool(action):
    tool = action.get('tool')
    inp = action.get('input', '')
    folder = action.get('folder', None)
    from rag_tool import index_documents, search_documents
    if GROUP_MODE and tool not in SAFE_GROUP_TOOLS:
        return (f"ERROR: Tool '{tool}' is disabled in group chats. "
                f"You can only use: {', '.join(sorted(SAFE_GROUP_TOOLS))}. "
                f"If the user wants PC control, tell them to DM the bot directly.")

    if tool == 'chat':                     return f"AI: {inp}"
    elif tool == 'google_search_and_open': return google_search_and_open(inp)
    elif tool == 'read_and_summarize':     return read_and_summarize(inp)
    elif tool == 'make_folder':            return make_folder(inp)
    elif tool == 'find_and_open_folder':   return find_and_open_folder(inp)
    elif tool == 'write_file':
        if '|' in inp:
            filename, content = inp.split('|', 1)
        else:
            filename, content = inp, ""
        return write_file(filename, content, folder)
    elif tool == 'read_file':              return read_file(inp)
    elif tool == 'patch_file':             return patch_file(inp)
    elif tool == 'list_symbols':           return list_symbols(inp)
    elif tool == 'repo_map':               return repo_map(inp)
    elif tool == 'crawl_site':             return crawl_site(inp)
    elif tool == 'map_site':               return map_site(inp)
    elif tool == 'fetch_clean':            return fetch_clean(inp)
    elif tool == 'run_recipe':
        from recipes import run_recipe
        return run_recipe(inp.strip())
    elif tool == 'run_command':            return run_command(inp)
    elif tool == 'search_web':             return search_web(inp)
    elif tool == 'show_last_result':       return show_last_result()
    elif tool == 'open_website':           return open_website(inp)
    elif tool == 'open_app':               return open_app(inp)
    elif tool == 'youtube_search':         return youtube_search(inp)
    elif tool == 'get_video_links':        return get_video_links(inp)
    elif tool == 'set_reminder':           return set_reminder(inp)
    elif tool == 'set_reminder_at':        return set_reminder_at(inp)
    elif tool == 'list_reminders':         return list_reminders()
    elif tool == 'list_brains':            return list_brains()
    elif tool == 'remember':               return remember_fact(inp)
    elif tool == 'forget':                 return forget_fact(inp)
    elif tool == 'read_screen':            return read_screen()
    elif tool == 'clipboard_read':         return clipboard_read()
    elif tool == 'clipboard_write':        return clipboard_write(inp)
    elif tool == 'set_volume':             return set_volume(inp)
    elif tool == 'media_control':          return media_control(inp)
    elif tool == 'system_status':          return system_status()
    elif tool == 'lock_pc':                return lock_pc()
    elif tool == 'sleep_pc':               return sleep_pc()
    elif tool == 'shutdown_pc':            return shutdown_pc(inp)
    elif tool == 'restart_pc':             return restart_pc(inp)
    elif tool == 'save_screenshot':        return save_screenshot()
    elif tool == 'todo':                   return todo(inp)
    elif tool == 'list_windows':           return list_windows()
    elif tool == 'focus_window':           return focus_window(inp)
    elif tool == 'notify':                 return notify(inp)
    elif tool == 'beep':                   return beep()
    elif tool == 'close_window':           return close_window(inp)
    elif tool == 'watch_folder':           return watch_folder(inp)
    elif tool == 'stop_watching':          return stop_watching()
    elif tool == 'gmail_unread':           return gmail_unread(inp)
    elif tool == 'gmail_read':             return gmail_read(inp)
    elif tool == 'gmail_search':           return gmail_search(inp)
    elif tool == 'send_maps_url':          return send_maps_url(inp)
    elif tool == 'weather':                return weather(inp)
    elif tool == 'air_quality':            return air_quality(inp)
    elif tool == 'news':                   return news(inp)
    elif tool == 'whatsapp_send':          return whatsapp_send(inp)
    elif tool == 'describe_screen':        return describe_screen()
    elif tool == 'set_name':               return set_name(inp)
    elif tool == 'current_time':           return current_time()
    elif tool == 'send_telegram_message':  return send_telegram_message(inp)
    elif tool == "telegram_user_send":     return telegram_user_send(action.get("input", ""))
    elif tool == "telegram_user_delete":   return telegram_user_delete(action.get("input", ""))
    elif tool == "telegram_user_edit":     return telegram_user_edit(action.get("input", ""))
    elif tool == "send_image_telegram":    return send_image_telegram(action.get("input", ""))
    elif tool == 'whitelist':              return whitelist(inp)
    elif tool == 'send_maps_list':         return send_maps_list(inp)
    elif tool == 'verify_places':          return verify_places(inp)
    elif tool == 'rag_search':             return search_documents(inp)
    elif tool == 'rag_index':              return index_documents(inp)
    elif tool == 'deep_research':          return deep_research(inp)
    elif tool == 'hardware_scan':          return hardware_scan()
    elif tool == 'recommend_models':       return recommend_models()
    elif tool == 'find_files':             return find_files(inp)
    elif tool == 'move_files':             return move_files(inp)
    elif tool == 'generate_image':         return generate_image(inp)
    elif tool == 'describe_image':         return describe_image(inp)
    else:                                  return f"ERROR: Unknown tool '{tool}'"
    


# ---------------------------
# AGENT TURN ORCHESTRATION
# ---------------------------
def run_agent_turn(user_input, conversation_history):
    # Request setup and bounded tool-call loop.
    step_log = []
    last_signature = None
    # Force-inject RAG context for content-ish questions
    try:
        from rag_tool import search_documents
        words = user_input.split()
        # Only auto-search if the query is substantive (3+ words)
        if len(words) >= 3:
            hits = search_documents(user_input)
            if hits and "No relevant information" not in hits and "No documents indexed" not in hits:
                user_input = (
                    "=== AUTO-RETRIEVED FROM USER'S INDEXED DOCUMENTS ===\n"
                    f"{hits}\n"
                    "=== END RETRIEVED CONTEXT ===\n\n"
                    "Using the retrieved context above (if relevant), answer the user's question. "
                    "If the context answers it, quote from it and cite the source. "
                    "If the context is irrelevant, ignore it and answer normally.\n\n"
                    f"USER QUESTION: {user_input}"
                )
    except Exception as e:
        print(f"[rag auto-inject] skipped: {e}")
    # Detect user pushback — force re-search instead of defending
    pushback_words = (
        "are you sure", "you sure", "really", "wrong", "not right",
        "try again", "search again", "check again", "no you didn't",
        "that's not", "thats not", "youre wrong", "you're wrong",
        "bullshit", "ragebait", "rage bait", "no you did not",
        "that is not", "that isnt", "thats wrong", "that's wrong",
    )
    user_lower = (user_input or "").lower()
    is_pushback = any(w in user_lower for w in pushback_words)

    for step in range(MAX_STEPS_PER_TURN):
        step_start = time.time()
        context = "\n".join(conversation_history[-10:] + step_log)

        if step == 0:
            if is_pushback:
                next_input = (
                    f"USER IS PUSHING BACK: '{user_input}'.\n"
                    f"Your previous answer is being challenged. DO NOT DEFEND IT.\n"
                    f"Call a tool (search_web / verify_places) to recheck the facts, "
                    f"then correct yourself if the new results differ.\n"
                    f"First line of your reply must be the correction — no apology "
                    f"preamble, no 'I'm not sure where you got that idea', no "
                    f"defensive phrasing."
                )
            else:
                next_input = user_input
        else:
            next_input = (
                f"Original request: '{user_input}'. The tool results above are real.\n"
                f"If you now have enough info, reply to the user directly in plain, natural "
                f"conversational language — like a person telling them the answer, not a data dump. "
                f"Wrap the tool output in a normal sentence (or two).\n"
                f"Only call another tool if you genuinely need more info.\n"
                f"Reply with plain text (preferred) or a JSON tool call — no 'done' wrapper needed."
            )

        action, error = get_valid_action(next_input, context)
        if action is None:
            print(f"\nGiving up this turn: {error}")
            step_log.append(f"System: {error}")
            break

        tool = action.get('tool')
        inp = action.get('input', '')

        # Guard: 'done' with no prior action is meaningless — treat as chat
        if tool == 'done' and not any(l.startswith("Action: ") for l in step_log):
            print(f"\n[Blocked fabricated done]")
            action = {"tool": "chat",
                      "input": inp or "I'm not sure what you'd like — could you clarify?"}
            tool = "chat"
            inp = action["input"]

        if tool == 'done':
            print(f"\nDone: {inp}")
            step_log.append(f"Done: {inp}")
            break

        signature = (tool, inp)
        if signature == last_signature:
            print("\nDetected repeated action - stopping.")
            step_log.append("System: Repeated action - stopped.")
            break
        last_signature = signature

        # Special-case: read_screen on step 0 forces a text reply next
        if tool == 'read_screen' and step == 0:
            output = execute_tool(action)
            print(f"\n{output}")
            step_log.append(f"Action: {tool}({inp}) -> Result: {output}")
            force_prompt = (
                f"Original request: '{user_input}'. OCR text of the user's screen is above. "
                f"Answer the user directly in plain conversational language based on that text."
            )
            force_action, _ = get_valid_action(force_prompt, "\n".join(step_log))
            if force_action and force_action.get('tool') == 'chat':
                final = force_action['input']
                print(f"\nAI: {final}")
                step_log.append(f"Action: chat(forced) -> Result: AI: {final}")
            else:
                direct = ask_llm_direct(
                    f"User asked: {user_input}\n\nScreen text:\n{output}\n\nAnswer:"
                )
                print(f"\nAI: {direct}")
                step_log.append(f"Action: chat(fallback) -> Result: AI: {direct}")
            break

        output = execute_tool(action)
        print(f"\n{output}")
        step_log.append(f"Action: {tool}({inp}) -> Result: {output}")
        print(f"  [step {step+1} took {time.time()-step_start:.1f}s]")

        # Run any extra tool calls the LLM packed into the same response
        for qa in action.get('_queued_actions', []):
            qtool = qa.get('tool')
            qinp = qa.get('input', '')
            try:
                qout = execute_tool(qa)
                print(f"\n{qout}")
                step_log.append(f"Action: {qtool}({qinp}) -> Result: {qout}")
            except Exception as e:
                step_log.append(f"Action: {qtool}({qinp}) -> ERROR: {e}")

        # chat = the LLM's actual reply → done
        if tool == 'chat':
            break

        # Otherwise: loop back, feed the tool result to the LLM,
        # let it decide next (usually: wrap the result in a natural reply).

    # Response-integrity checks: prevent unsupported action claims.
    tool_ran = any(
        l.startswith("Action: ") and "-> Result: " in l
        and not l.startswith("Action: chat(")
        for l in step_log
    )

    said_sent = False
    for line in step_log:
        if line.startswith("Action: chat(") and "-> Result: " in line:
            text = line.split("-> Result: ", 1)[-1].lower()
            if any(w in text for w in (
                "i've sent", "i sent", "i've opened", "i opened",
                "i've added", "i've deleted", "i've edited",
                "i've saved", "i've created", "i've done",
            )):
                said_sent = True
            break

    if said_sent and not tool_ran:
        for i in range(len(step_log) - 1, -1, -1):
            if step_log[i].startswith("Action: chat("):
                step_log[i] = (
                    "Action: chat(honesty-guard) -> Result: "
                    "AI: I didn't actually do that — no tool ran. Try again."
                )
                break

    # Recover a user-facing reply if the tool loop did not produce one.
    has_reply = any(
        l.startswith("Action: chat(") or l.startswith("Done:")
        for l in step_log
    )

    if not has_reply:
        synthesis_context = "\n".join(conversation_history[-10:] + step_log)
        fallback_prompt = f"""User asked: "{user_input}"

You got stuck before replying. Using ONLY the real info gathered below — do not invent anything — give the best honest answer you can, in plain text.

Gathered:
{synthesis_context}

Reply:"""
        fallback_reply = ask_llm_direct(fallback_prompt)
        if not fallback_reply or fallback_reply.startswith("Error:"):
            fallback_reply = ("Couldn't figure that one out — nothing clean came back. "
                              "Try rephrasing.")
        print(f"\n(Fallback) AI: {fallback_reply}")
        step_log.append(f"Action: chat(fallback) -> Result: AI: {fallback_reply}")

    # Extract and persist the final assistant response.
    final_message = None
    for line in reversed(step_log):
        if line.startswith("Action: chat(") and "-> Result: " in line:
            final_message = line.split("-> Result: ", 1)[-1]
            break
        if line.startswith("Done:"):
            final_message = line
            break

    if final_message and final_message.startswith("AI: "):
        final_message = final_message[4:]

    # --- Hallucination guard ---
    if final_message:
        tools_ran = any(l.startswith("Action: ") and "-> Result:" in l
                        and not l.startswith("Action: chat(")
                        for l in step_log)
        claim_words = (
            "i've moved", "i moved", "i've created", "i created",
            "i've sent", "i sent", "i've deleted", "i deleted",
            "i found ", "i've found", "i've opened", "i opened",
            "files moved", "folder created",
        )
        low = final_message.lower()
        claims_action = any(w in low for w in claim_words)
        if claims_action and not tools_ran:
            final_message = (
                "⚠️ I didn't actually do that — no tool ran. "
                "Say it again or use /find, /move, /mkdir to run it directly."
            )

    return step_log