import subprocess
import os
import difflib
import json
import re
import requests
import webbrowser
import time
import shutil
import sys
import datetime
import urllib.parse
import winsound
import weather_news
from bs4 import BeautifulSoup
from pathlib import Path
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
import security_tools
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
MAX_HISTORY_ENTRIES = 40


def _conversation_file():
    return _cfg.memory_path('conversation.json')


def load_conversation_history():
    path = _conversation_file()
    if not os.path.exists(path):
        return []
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data.get('history', [])[-MAX_HISTORY_ENTRIES:]
    except (json.JSONDecodeError, OSError):
        return []


def save_conversation_history(history):
    try:
        path = _conversation_file()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
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

try:
    import yt_dlp
    YTDLP_AVAILABLE = True
except ImportError:
    YTDLP_AVAILABLE = False


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
# Outcome tracking for the most recent _call_llm call. Used by the eval
# harness (evals.py) to label traces and by /eval. _LAST_PROVIDER is the
# provider that answered; _LAST_FAILURE_REASON explains the last failed
# attempt: "empty" | "error" | None.
_LAST_PROVIDER = None
_LAST_FAILURE_REASON = None

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
MAX_STEPS_PER_TURN = 13
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
    "chat", "search_web", "serp_search", "weather", "air_quality", "news",
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
    "discord": "https://discord.com/app",
    "netflix": "https://netflix.com",
    "chatgpt": "https://chat.openai.com",
    "reddit": "https://reddit.com",
    "twitter": "https://twitter.com",
    "github": "https://github.com",
    "stackoverflow": "https://stackoverflow.com",
}

TODO_FILE = _cfg.memory_path('todos.json')


# ---------------------------
# SKILLS (Hermes-style auto-generated procedures)
# ---------------------------
SKILLS_DIR = Path(_cfg.memory_dir()) / "skills"
try:
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
except OSError as _skills_dir_error:
    print(f"[skills] cannot create {SKILLS_DIR}: {_skills_dir_error}")

# Repo-owned roots for versioned skills and plugin packs. The legacy
# SKILLS_DIR above is still scanned as a lower-priority extra root.
REPO_ROOT = Path(__file__).resolve().parent
SKILLS_ROOT = REPO_ROOT / "skills"
PLUGINS_ROOT = REPO_ROOT / "plugins"

_skill_manager = None
_skill_manager_lock = _threading.RLock()
_skills_dirty = True


def get_skill_manager():
    """Return the shared SkillManager, rescanning when skills changed."""
    global _skill_manager, _skills_dirty
    with _skill_manager_lock:
        if _skill_manager is None:
            import skill_manager as _skill_manager_module
            _skill_manager = _skill_manager_module.SkillManager(
                skills_dir=str(SKILLS_ROOT),
                plugins_dir=str(PLUGINS_ROOT),
                extra_skills_dirs=[str(SKILLS_DIR)],
            )
            _skills_dirty = True
        if _skills_dirty:
            _skill_manager.scan()
            _skills_dirty = False
        return _skill_manager


def reload_skills():
    """Force a rescan on the next access; returns the refreshed manager."""
    global _skills_dirty
    with _skill_manager_lock:
        _skills_dirty = True
        return get_skill_manager()


def mark_skills_dirty():
    """Mark the catalog stale after skill_manage writes to disk."""
    global _skills_dirty
    with _skill_manager_lock:
        _skills_dirty = True


def get_skill_catalog():
    """Compact skill catalog for the system prompt ("" if unavailable)."""
    try:
        return get_skill_manager().system_prompt()
    except Exception:
        return ""


def load_skill_tool(skill_id):
    """Execute the load_skill tool; returns the skill text or an ERROR string."""
    skill_id = (skill_id or "").strip()
    if not skill_id:
        return "ERROR: load_skill needs a skill id from the AVAILABLE SKILLS list."
    try:
        result = get_skill_manager().invoke_tool(skill_id)
    except Exception as exc:
        return f"ERROR: skills unavailable ({exc})"
    if not result.get("ok"):
        return f"ERROR: {result.get('error')}"
    return result.get("content", "")


def _parse_skill_frontmatter(text: str) -> tuple:
    """Return (frontmatter_dict, body). Tolerant of missing frontmatter."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    fm_raw = text[3:end].strip()
    body = text[end + 4:].lstrip("\n")
    fm = {}
    for line in fm_raw.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip()
    return fm, body


def _render_skill(fm: dict, body: str) -> str:
    lines = ["---"]
    for k, v in fm.items():
        lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append("")
    lines.append(body.rstrip())
    lines.append("")
    return "\n".join(lines)


def skill_manage(action: str, name: str = "", content: str = "") -> str:
    """
    Manage AgentBot skills.
    action: create | update | list | search | read | delete
    name:   skill slug (lowercase, underscores)
    content: full SKILL.md text (for create/update)
    """
    action = (action or "").strip().lower()
    name = (name or "").strip().lower().replace(" ", "_")

    if action == "list":
        if not SKILLS_DIR.exists():
            return "No skills yet."
        names = sorted(p.name for p in SKILLS_DIR.iterdir() if p.is_dir())
        if not names:
            return "No skills yet."
        return "Skills:\n" + "\n".join(f"- {n}" for n in names)

    if action == "search":
        if not name:
            return "ERROR: search needs a query in the name field."
        hits = []
        q = name.lower()
        for p in SKILLS_DIR.iterdir():
            if not p.is_dir():
                continue
            sk = p / "SKILL.md"
            if not sk.exists():
                continue
            text = sk.read_text(encoding="utf-8")
            if q in text.lower():
                fm, _ = _parse_skill_frontmatter(text)
                hits.append(f"- {p.name}: {fm.get('description','')}")
        return "Matches:\n" + "\n".join(hits) if hits else "No matches."

    if action == "read":
        sk = SKILLS_DIR / name / "SKILL.md"
        if not sk.exists():
            return f"ERROR: skill '{name}' not found."
        return sk.read_text(encoding="utf-8")

    if action == "delete":
        sk = SKILLS_DIR / name
        if not sk.exists():
            return f"ERROR: skill '{name}' not found."
        for f in sk.iterdir():
            f.unlink()
        sk.rmdir()
        mark_skills_dirty()
        return f"Deleted skill '{name}'."

    if action in ("create", "update"):
        if not name:
            return "ERROR: name required."
        if not content.strip():
            return "ERROR: content required."
        d = SKILLS_DIR / name
        d.mkdir(parents=True, exist_ok=True)
        fm, body = _parse_skill_frontmatter(content)
        if "name" not in fm:
            fm["name"] = name
        fm.setdefault("created", datetime.datetime.now().isoformat(timespec="seconds"))
        fm["last_used"] = fm.get("last_used", fm["created"])
        fm.setdefault("use_count", "0")
        (d / "SKILL.md").write_text(_render_skill(fm, body), encoding="utf-8")
        mark_skills_dirty()
        return f"Skill '{name}' {'updated' if action=='update' else 'created'}."

    return f"ERROR: unknown action '{action}'."


def _generate_skill_from_turn(user_request: str, tool_calls: list) -> str:
    """Called after a turn with >=3 tool calls (or loop exhaustion). Asks the
    LLM to write a SKILL.md.

    Prompt informed by Anthropic's skill-creator guidelines:
    - Use imperative form in instructions
    - Explain WHY things are important rather than heavy-handed MUSTs
    - Make the skill general, not super-narrow to specific examples
    - Keep SKILL.md under 500 lines / 5,000 tokens
    """
    if not tool_calls:
        return ""
    summary = "\n".join(f"- {c.get('tool')}: {str(c.get('input'))[:120]}" for c in tool_calls)
    prompt = (
        "You just completed a multi-step task. Write a reusable SKILL.md that captures "
        "the procedure so it can be repeated later.\n\n"
        f"User request: {user_request}\n\n"
        f"Tool calls made:\n{summary}\n\n"
        "Write the skill using the imperative form (e.g. 'Call play_on_youtube with...' "
        "not 'You should call...'). Explain WHY each step matters rather than just listing "
        "MUSTs — the reader benefits from understanding the reasoning. Keep the body under "
        "300 words. Make the skill general enough to apply to similar future requests, not "
        "just this one exact case.\n\n"
        "Output ONLY a SKILL.md with YAML frontmatter (name, description) and a markdown "
        "body with sections '## When to use', '## Steps', '## Notes'. Do not include any "
        "other text."
    )
    try:
        raw = _call_llm(prompt, force_json=False, max_tokens=512)
    except Exception as e:
        return f"ERROR generating skill: {e}"
    if not raw or raw.startswith("Error:"):
        return ""
    fm, body = _parse_skill_frontmatter(raw)
    slug = (fm.get("name") or f"skill_{int(time.time())}").lower().replace(" ", "_")
    return skill_manage("create", slug, raw)


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
        candidates = data.get("candidates") or []
        # A blocked prompt (safety/recitation) may return no candidates at all.
        block = (data.get("promptFeedback") or {}).get("blockReason")
        if block:
            return f"Error: gemini blocked the prompt ({block})"
        if not candidates:
            return "Error: gemini returned no candidates"
        cand = candidates[0]
        finish = cand.get("finishReason")
        parts = (cand.get("content") or {}).get("parts") or []
        if parts and parts[0].get("text"):
            return parts[0]["text"].strip()
        # No usable text. Distinguish the common, benign case (the model ran
        # out of output budget before emitting anything — reasoning models do
        # this at low maxOutputTokens) from a genuinely malformed response.
        # Returning an Error: string makes _call_llm fail over, which is
        # correct; before this it said "unexpected response: {...}" which
        # looked like a parser bug rather than an empty reply.
        if finish == "MAX_TOKENS":
            return "Error: gemini empty response — output token limit reached"
        if finish and finish not in ("STOP",):
            return f"Error: gemini produced no text (finishReason={finish})"
        return f"Error: gemini unexpected response: {str(data)[:200]}"
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


def _call_llm(prompt, force_json=False, max_tokens=None, force_provider=None):
    """Try each brain in priority order until one succeeds.

    force_provider: when set, walk only that provider (no failover). Used by
    the eval harness to pin a provider; default None preserves the existing
    full-chain behaviour.
    """
    global LAST_BRAIN, _LAST_PROVIDER, _LAST_FAILURE_REASON
    tried = []
    now = time.time()

    providers_to_try = [force_provider] if force_provider else BRAIN_PRIORITY

    for name in providers_to_try:
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
            _LAST_FAILURE_REASON = "error"
            tried.append(f"{name}({result[:40]})")
            continue

        # An empty or whitespace-only reply is NOT success: reasoning models
        # (e.g. groq openai/gpt-oss-120b at low max_tokens) return '' and
        # would otherwise short-circuit the failover chain, leaving the caller
        # with '' and no error. Treat it as a failed attempt and keep going.
        if not (isinstance(result, str) and result.strip()):
            _LAST_FAILURE_REASON = "empty"
            tried.append(f"{name}(empty)")
            continue

        LAST_BRAIN = name
        _LAST_PROVIDER = name
        _LAST_FAILURE_REASON = None
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
# MOOD + TIME-OF-DAY PERSONA
# ---------------------------
MOOD_FILE = os.environ.get("AGENTBOT_MOOD_FILE") or _cfg.memory_path("mood.json")
MOOD_LOCK = _threading.RLock()
MOOD_MAX = 10
FRUSTRATION_THRESHOLD = 3

LATE_NIGHT_DIRECTIVE = (
    "It is late at night. The user is up late. Be brief, and gently suggest "
    "they get some rest."
)
MORNING_DIRECTIVE = "It is morning. Greet the user warmly."
FRUSTRATION_DIRECTIVE = (
    "You are currently frustrated because tools are failing. Express slight "
    "annoyance to the user."
)

SPONTANEOUS_PROMPT = (
    "You are a proactive desktop assistant looking at a screenshot of the "
    "user's screen. If there is something genuinely useful you can tell them "
    "in one short sentence (an error dialog, an approaching deadline, an "
    "important unread email), reply with only that sentence. Otherwise reply "
    "with exactly: NOTHING\n"
    "Never mention passwords, private messages, financial details, or other "
    "sensitive content. Do not describe the screen."
)

SCREENSHOT_VISION_PROMPT = (
    "This is a full-screen desktop screenshot ({width}x{height} px, origin "
    "top-left). A blind planner will click these exact pixel coordinates. "
    "List every visible interactive element as 'label — x,y' using the "
    "element's centre: the search bar/box (include the x,y of the text "
    "\"Search\"), navigation buttons, and result posters or stream links. "
    "Only report what is actually visible; say when a video is already "
    "playing."
)

VISION_COORDINATE_RULE = (
    "Keep your answer under 20 words. ONLY output the coordinates of the "
    "requested button. Do not describe the rest of the screen."
)


def _default_mood():
    return {"frustration": 0, "success_streak": 0}


def _clamp_mood(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(MOOD_MAX, value))


def load_mood():
    """Read mood.json, tolerating a missing or corrupt file."""
    try:
        with open(MOOD_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return _default_mood()
    if not isinstance(data, dict):
        return _default_mood()
    return {
        "frustration": _clamp_mood(data.get("frustration", 0)),
        "success_streak": _clamp_mood(data.get("success_streak", 0)),
    }


def save_mood(mood):
    try:
        os.makedirs(os.path.dirname(MOOD_FILE), exist_ok=True)
        tmp = f"{MOOD_FILE}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(mood, fh)
        os.replace(tmp, MOOD_FILE)
    except OSError:
        pass


def note_tool_result(result):
    """Update frustration/success counters for one tool result."""
    failed = isinstance(result, str) and result.lstrip().startswith("ERROR:")
    with MOOD_LOCK:
        mood = load_mood()
        if failed:
            mood["frustration"] = min(MOOD_MAX, mood["frustration"] + 1)
        else:
            mood["frustration"] = 0
            mood["success_streak"] = min(
                MOOD_MAX, mood["success_streak"] + 1)
        save_mood(mood)
        return mood


def get_mood_directive(mood=None):
    mood = load_mood() if mood is None else mood
    if mood.get("frustration", 0) >= FRUSTRATION_THRESHOLD:
        return FRUSTRATION_DIRECTIVE
    return ""


def day_part(hour=None):
    """Classify an hour into 'late', 'morning', or 'day'."""
    hour = datetime.datetime.now().hour if hour is None else hour
    if hour >= 23 or hour < 5:
        return "late"
    if 5 <= hour < 12:
        return "morning"
    return "day"


# ---------------------------
# PUBLIC WRAPPERS
# ---------------------------
_SKILL_NEGATIVE = re.compile(
    r"\b(song|music|album|trailer|clip|video|podcast|youtube|anime|game|"
    r"chess|piano|guitar|spotify|news|tutorial|recommend|suggest|best|"
    r"similar|trivia)\b"
)
_SKILL_MEDIA = re.compile(
    r"\b(movie|film|tv\s*show|tv\s*series|episode|season)\b"
)


def _skill_directive(user_request):
    """Return a decisive 'use this skill now' command for a matching request."""
    text = (user_request or "").lower()
    if not text.strip():
        return ""
    if re.search(r"\bstremio\b", text) or (
            not _SKILL_NEGATIVE.search(text) and (
                re.search(r"\b(watch|stream|play)\b", text)
                or _SKILL_MEDIA.search(text))):
        return ('Use the Stremio skill now. Do not think about it, just '
                'execute the steps. Call load_skill("local:stremio") first.')

    try:
        skills = get_skill_manager().list_skills()
    except Exception:
        return ""
    for skill in skills:
        label = skill.name.replace("-", " ").replace("_", " ")
        if not re.search(rf"\b{re.escape(label)}\b", text):
            continue
        if not label.lower().endswith("skill"):
            label += " skill"
        return (f"Use the {label} now. Do not think about it, just execute "
                f'the steps. Call load_skill("{skill.skill_id}") first.')
    return ""


def starts_new_task(user_request):
    """True when a request starts a skill task and should not inherit old details."""
    return bool(_skill_directive(user_request))


def _build_plan_prompt(user_request, context, correction=None):
    """Build the exact prompt run_agent_turn's step 1 sends to the LLM.

    Extracted verbatim from ask_ai_for_plan so the eval harness can reuse the
    SAME prompt assembly (byte-identical) without duplicating it.
    """
    correction_block = (f"\n\nYour previous response was invalid: {correction}\n"
                        f"Respond with ONLY a corrected JSON object.") if correction else ""

    known_facts = memory.get_facts_block()
    facts_block = f"\nKnown facts about the user:\n{known_facts}\n" if known_facts else ""

    # Skills are personal procedures; group chats never see the catalog and
    # execute_tool blocks load_skill there too.
    skill_catalog = ""
    skill_directive = ""
    if not GROUP_MODE:
        skill_catalog = get_skill_catalog()
        if skill_catalog:
            skill_catalog += "\n"
        directive = _skill_directive(user_request)
        if directive:
            skill_directive = directive + "\n\n"

    now_str = datetime.datetime.now().strftime("%A, %B %d, %Y — %I:%M %p")
    time_block = f"\nCURRENT DATE AND TIME: {now_str} (use this; never guess)\n"

    persona_lines = []
    part = day_part()
    if part == "late":
        persona_lines.append(LATE_NIGHT_DIRECTIVE)
    elif part == "morning":
        persona_lines.append(MORNING_DIRECTIVE)
    mood_line = get_mood_directive()
    if mood_line:
        persona_lines.append(mood_line)
    persona_block = (
        "\n" + "\n".join(persona_lines) + "\n" if persona_lines else ""
    )

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
            "- load_skill (skills are disabled in groups)\n"
            "- browse_web (browser control is disabled in groups)\n"
            "- execute_python, inspect_docx (local code and file access)\n"
            "- screenshot_background, analyze_screenshot, click_coordinates, type_text, get_pixel_color, save_macro, play_macro (OS control)\n"
            "- All system tools (volume, media, lock, shutdown)\n\n"
            "If a user asks for any blocked capability, reply briefly: you "
            "can't do that in a group, DM the bot directly. Do NOT attempt "
            "to call a blocked tool — you'll just get an error.\n"
            "=== END GROUP MODE ===\n"
        )

    try:
        import experiential_memory
        learned_block = experiential_memory.learned_lessons_block()
    except Exception:
        learned_block = ""

    prompt = f"""{skill_catalog}{skill_directive}{TOOL_DESCRIPTIONS}
{CORE_PERSONA}{learned_block}{group_block}{time_block}{persona_block}{facts_block}
Context (recent conversation and tool results):
{context}

User: {user_request}{correction_block}

Your response (either a plain-text reply or a single JSON tool call):"""
    return prompt


def ask_ai_for_plan(user_request, context, correction=None):
    prompt = _build_plan_prompt(user_request, context, correction)

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


# DeepSeek leaks its raw DSML tool-call markup instead of JSON when the prompt
# is near capacity. Accept one or two bars per side and optional whitespace;
# observed leak uses two: <｜｜DSML｜｜ parameter name="input" ...>.
_DSML_TAG = r"<[｜|]{1,2}\s*DSML\s*[｜|]{1,2}"
_DSML_INVOKE_RE = re.compile(
    _DSML_TAG + r'\s*invoke\s+name="(.*?)"', re.DOTALL)
_DSML_INPUT_RE = re.compile(
    _DSML_TAG + r'\s*parameter\s+name="input"\s*'
    r'(?:string="(?:true|false)"\s*)?>(.*?)</'
    r'[｜|]{1,2}\s*DSML\s*[｜|]{1,2}\s*parameter>',
    re.DOTALL)


def _extract_dsml_action(text):
    """Recover a {tool, input} call from leaked DeepSeek DSML markup."""
    invoke = _DSML_INVOKE_RE.search(text)
    param = _DSML_INPUT_RE.search(text)
    if not invoke or not param:
        return None
    return {"tool": invoke.group(1).strip(), "input": param.group(1).strip()}


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

    # No JSON → DeepSeek may have leaked raw DSML tool-call markup
    dsml = _extract_dsml_action(raw)
    if dsml:
        tool = dsml.get('tool')
        if tool not in VALID_TOOLS:
            return None, (f"'{tool}' is not a valid tool name. "
                          f"Valid: {', '.join(sorted(VALID_TOOLS))}")
        return dsml, None

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


MAX_PYTHON_OUTPUT = 12000

# Hard stop for runaway retry loops: after this many failed execute_python
# calls in one turn the tool refuses to run again until the next user turn.
PY_FAILURE_LIMIT = 3
PY_DISABLED_MESSAGE = (
    "ERROR: Python execution disabled for this turn due to repeated failures. "
    "Ask the user for help."
)
_PY_FAILURES = {"count": 0}


def reset_python_failures():
    """Clear the per-turn execute_python failure counter."""
    _PY_FAILURES["count"] = 0


def python_failure_count():
    return _PY_FAILURES["count"]


def execute_python(code):
    """Run a Python script in a fresh interpreter and return its output."""
    if _PY_FAILURES["count"] >= PY_FAILURE_LIMIT:
        return PY_DISABLED_MESSAGE
    result = _execute_python_once(code)
    if result.startswith("ERROR:"):
        _PY_FAILURES["count"] += 1
    return result


def _execute_python_once(code):
    if not (code or "").strip():
        return "ERROR: execute_python needs code."
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, cwd=os.path.dirname(os.path.abspath(__file__)), env=env,
        )
    except subprocess.TimeoutExpired:
        return "ERROR: the script ran longer than 60 seconds and was stopped."
    except Exception as e:
        return f"ERROR: cannot run Python: {e}"

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if len(out) > MAX_PYTHON_OUTPUT:
        out = (out[:MAX_PYTHON_OUTPUT]
               + f"\n[... truncated {len(out) - MAX_PYTHON_OUTPUT} chars]")
    if proc.returncode != 0:
        return f"ERROR: exit code {proc.returncode}\n{(err or out)[:MAX_PYTHON_OUTPUT]}"
    if not out and err:
        return f"OK (no stdout). stderr:\n{err[:MAX_PYTHON_OUTPUT]}"
    return out or "OK (no output)"


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
    """Return IAudioEndpointVolume for the default speakers.

    pycaw 2024+ removed AudioDevice.Activate; the supported accessor is the
    EndpointVolume property. Older releases expose Activate (either on the
    AudioDevice wrapper or on the raw IMMDevice inside it), so keep those
    paths for compatibility.
    """
    devices = AudioUtilities.GetSpeakers()
    endpoint = getattr(devices, "EndpointVolume", None)
    if endpoint is not None:
        return endpoint
    activate = getattr(devices, "Activate", None)
    if activate is None:
        inner = getattr(devices, "_dev", None)
        activate = getattr(inner, "Activate", None)
    if activate is None:
        raise RuntimeError(
            "pycaw AudioDevice exposes neither EndpointVolume nor Activate "
            f"({type(devices).__name__}); the installed pycaw is incompatible.")
    interface = activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
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
# MEDIA STREAMING (yt-dlp + native player)
# ---------------------------
PLAYER_INSTALL_PATHS = {
    "mpv": (r"C:\Program Files\mpv\mpv.exe", r"C:\Program Files (x86)\mpv\mpv.exe"),
    "VLC": (r"C:\Program Files\VideoLAN\VLC\vlc.exe",
            r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe"),
}


def _find_mpv():
    """Locate mpv.exe on PATH, then in the WinGet shim and Program Files."""
    found = shutil.which("mpv")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA")
    candidates = [
        os.path.join(local, "Microsoft", "WinGet", "Links", "mpv.exe") if local else "",
        r"C:\Program Files\MPV Player\mpv.exe",
        r"C:\Program Files\mpv\mpv.exe",
        r"C:\Program Files (x86)\mpv\mpv.exe",
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def _find_media_player():
    """Return (name, executable) for mpv or VLC, else (None, None)."""
    mpv = _find_mpv()
    if mpv:
        return "mpv", mpv
    vlc = shutil.which("vlc")
    if vlc:
        return "VLC", vlc
    for path in PLAYER_INSTALL_PATHS["VLC"]:
        if os.path.isfile(path):
            return "VLC", path
    return None, None


def _muxed_stream_url(info):
    """Return a direct URL whose format already carries both audio and video."""
    if info.get("url") and not info.get("requested_formats"):
        # A single selected format: playable as-is (e.g. a direct .mp4).
        return info["url"]
    best = None
    for fmt in info.get("formats") or ():
        if (fmt.get("url")
                and fmt.get("vcodec", "none") != "none"
                and fmt.get("acodec", "none") != "none"):
            best = fmt
    return best["url"] if best else None


def _find_ytdlp():
    """Locate the yt-dlp executable mpv's ytdl_hook should call."""
    found = shutil.which("yt-dlp")
    if found:
        return found
    candidates = [Path(sys.executable).parent / "Scripts" / "yt-dlp.exe"]
    try:
        import sysconfig
        scripts = sysconfig.get_path("scripts")
        if scripts:
            candidates.append(Path(scripts) / "yt-dlp.exe")
    except Exception:
        pass
    for root, pattern in (
        (os.environ.get("LOCALAPPDATA"), "Programs/Python/Python*/Scripts/yt-dlp.exe"),
        (os.environ.get("APPDATA"), "Python/Python*/Scripts/yt-dlp.exe"),
    ):
        if root:
            candidates.extend(sorted(Path(root).glob(pattern), reverse=True))
    for path in candidates:
        if path.is_file():
            return str(path)
    return None


MPV_EXIT_GRACE_SECONDS = 2


def _mpv_log_head(path, limit=3):
    """First lines of the last mpv launch log, for a tool result."""
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = [line.rstrip("\n") for line in f if line.strip()][:limit]
    except OSError:
        return ""
    if not lines:
        return ""
    return "\nmpv_error.log:\n" + "\n".join(lines)


def stream_media(query_or_url):
    """Play a video or stream from a URL or search query in mpv (or VLC)."""
    query = (query_or_url or "").strip()
    if not query:
        return "ERROR: stream_media needs a URL or a search query."
    if not YTDLP_AVAILABLE:
        return "ERROR: yt-dlp is not installed (pip install yt-dlp)."

    is_url = bool(re.match(r"^https?://", query, re.I))
    target = query if is_url else f"ytsearch1:{query}"
    try:
        with yt_dlp.YoutubeDL({
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "socket_timeout": 20,
        }) as ydl:
            info = ydl.extract_info(target, download=False)
    except Exception as e:
        return f"ERROR: cannot resolve '{query}': {str(e).removeprefix('ERROR: ')[:300]}"

    if info and info.get("entries") is not None:
        info = next((entry for entry in info["entries"] if entry), None)
    if not info:
        return f"ERROR: no playable results for '{query}'."

    title = info.get("title") or query
    page_url = info.get("webpage_url") or (query if is_url else None)
    stream_url = _muxed_stream_url(info)
    player, exe = _find_media_player()

    if player == "mpv":
        # Highest-resolution video plus best audio; "b" covers sites that only
        # expose a single muxed format.
        args = [exe, "--ytdl-format=bv*+ba/b"]
        ytdlp = _find_ytdlp()
        if ytdlp:
            args.append(f"--script-opts=ytdl_hook-ytdl_path={ytdlp}")
        if stream_url:
            args.append(stream_url)
        elif page_url:
            # YouTube only serves split video/audio formats; mpv's ytdl hook
            # resolves the page URL into both tracks and keeps them in sync.
            args.append(page_url)
        else:
            return f"ERROR: no stream URL for '{title}'."
    elif player == "VLC":
        url = stream_url or page_url
        if not url:
            return f"ERROR: no stream URL for '{title}'."
        args = [exe, url]
    else:
        if not (stream_url or page_url):
            return f"ERROR: no stream URL for '{title}'."
        webbrowser.open(stream_url or page_url)
        if stream_url:
            return f"No mpv/VLC found; opened the stream in your browser: {title}"
        return (f"No mpv/VLC found and '{title}' splits video and audio, so the "
                f"page was opened in your browser instead.")

    log = None
    log_path = None
    if player == "mpv":
        try:
            log_path = _cfg.memory_path("mpv_error.log")
            log = open(log_path, "w", encoding="utf-8", errors="replace")
            log.write(f"# {time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(args)}\n")
            log.flush()
        except OSError:
            log = None
    try:
        proc = subprocess.Popen(args, stdout=log or subprocess.DEVNULL,
                                stderr=log or subprocess.DEVNULL)
    except Exception as e:
        return f"ERROR: cannot start {player}: {e}{_mpv_log_head(log_path)}"
    finally:
        if log:
            log.close()

    if player == "mpv":
        try:
            code = proc.wait(timeout=MPV_EXIT_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            code = None
        if code:
            # mpv accepts the URL, opens, then dies on a bad stream; the log
            # is the only place the reason exists.
            return f"ERROR: mpv exited with code {code}.{_mpv_log_head(log_path)}"
    return f"Playing in {player}: {title}"


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


def _nl_to_cron(phrase):
    """Translate a natural-language recurrence phrase into a 5-field cron
    expression. Falls back to the LLM for anything the regexes don't cover.
    Returns (cron_expr, description) or (None, error_message)."""
    import re
    p = (phrase or "").strip().lower()
    if not p:
        return None, "ERROR: empty schedule phrase."

    # Already a raw 5-field cron expression?
    if len(p.split()) == 5 and scheduler.validate_cron(p):
        return p, scheduler.describe_cron(p)

    DAYS = {
        "sunday": 0, "sun": 0, "monday": 1, "mon": 1, "tuesday": 2, "tue": 2,
        "wednesday": 3, "wed": 3, "thursday": 4, "thu": 4, "friday": 5,
        "fri": 5, "saturday": 6, "sat": 6,
    }

    def _time_parts(s):
        m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", s)
        if not m:
            return None
        hh = int(m.group(1))
        mm = int(m.group(2) or 0)
        ampm = m.group(3)
        if ampm == "pm" and hh < 12:
            hh += 12
        elif ampm == "am" and hh == 12:
            hh = 0
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            return None
        return hh, mm

    # "every N minutes" / "every N hours"
    m = re.match(r"every\s+(\d+)\s*(min|minute|minutes)\b", p)
    if m:
        n = max(1, min(59, int(m.group(1))))
        return f"*/{n} * * * *", f"every {n} minutes"
    m = re.match(r"every\s+(\d+)\s*(hour|hours|hr|hrs)\b", p)
    if m:
        n = max(1, min(23, int(m.group(1))))
        return f"0 */{n} * * *", f"every {n} hours"

    # "every day/night/morning/evening at H[:MM]" and plain "every day at ..."
    m = re.search(r"every\s+(?:day|night|morning|evening|afternoon)\s+at\s+(.+)", p)
    if m:
        tp = _time_parts(m.group(1))
        if tp:
            return f"{tp[1]} {tp[0]} * * *", f"daily at {tp[0]:02d}:{tp[1]:02d}"
    m = re.match(r"daily\s+at\s+(.+)", p)
    if m:
        tp = _time_parts(m.group(1))
        if tp:
            return f"{tp[1]} {tp[0]} * * *", f"daily at {tp[0]:02d}:{tp[1]:02d}"

    # "every <weekday> at H[:MM]"
    m = re.search(r"every\s+([a-z]+)\s+at\s+(.+)", p)
    if m and m.group(1) in DAYS:
        tp = _time_parts(m.group(2))
        if tp:
            d = DAYS[m.group(1)]
            return f"{tp[1]} {tp[0]} * * {d}", f"weekly on {m.group(1)} at {tp[0]:02d}:{tp[1]:02d}"

    # "every hour"
    if re.match(r"every\s+hour\b", p):
        return "0 * * * *", "every hour"

    # LLM fallback
    prompt = (
        "Convert this schedule request into a single standard 5-field cron "
        "expression (minute hour day-of-month month day-of-week).\n"
        "Reply with ONLY the cron expression, nothing else.\n"
        f"Request: {phrase}"
    )
    try:
        raw = _call_llm(prompt, force_json=False, max_tokens=32)
    except Exception as e:
        return None, f"ERROR: could not translate schedule: {e}"
    cand = (raw or "").strip().splitlines()[0].strip() if raw else ""
    cand = cand.strip("`").strip()
    if scheduler.validate_cron(cand):
        return cand, scheduler.describe_cron(cand)
    return None, (f"ERROR: couldn't parse the schedule '{phrase}'. "
                  "Try 'every day at 8am' or a 5-field cron like '*/15 * * * *'.")


def schedule_task(input_str):
    """Create, list, or remove a recurring job. Input: 'when|what' | 'list' |
    'remove|<n>'."""
    try:
        raw = (input_str or "").strip()
        if not raw:
            return "ERROR: schedule_task needs 'when|what' (or 'list')."
        low = raw.lower()

        if low == "list":
            jobs = scheduler.load_cron_jobs()
            if not jobs:
                return "No recurring jobs."
            lines = ["Recurring jobs:"]
            for i, j in enumerate(jobs):
                desc = scheduler.describe_cron(j["cron"]) or j["cron"]
                lines.append(f"  [{i}] {desc} — {j['request']}  (cron: {j['cron']})")
            return "\n".join(lines)

        if low.startswith("remove"):
            parts = raw.split("|", 1) if "|" in raw else raw.split(None, 1)
            if len(parts) < 2 or not parts[1].strip().isdigit():
                return "ERROR: format is 'remove|<n>'. Use 'list' to see numbers."
            idx = int(parts[1].strip())
            jobs = scheduler.load_cron_jobs()
            if idx < 0 or idx >= len(jobs):
                return f"ERROR: no job at index {idx}."
            removed = jobs[idx]
            scheduler.remove_cron_job(idx)
            return f"Removed recurring job: {removed['cron']} — {removed['request']}"

        if "|" not in raw:
            return "ERROR: schedule_task needs 'when|what', e.g. 'every day at 8am|weather'."
        when, what = raw.split("|", 1)
        when, what = when.strip(), what.strip()
        if not what:
            return "ERROR: schedule_task needs something to do after '|'."

        cron_expr, desc = _nl_to_cron(when)
        if cron_expr is None:
            return desc
        ok, info = scheduler.add_cron_job(cron_expr, what, label=desc)
        if not ok:
            return f"ERROR: {info}"
        return f"Scheduled recurring job [{info}]: {desc} — {what}"
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
# DeepSeek's official Search & Fetch tools:
# https://api-docs.deepseek.com/guides/search/
DEEPSEEK_SEARCH_URL = "https://api.deepseek.com/v1/tools/search"
DEEPSEEK_FETCH_URL = "https://api.deepseek.com/v1/tools/fetch"
MAX_FETCH_CHARS = 8000


def _deepseek_headers():
    return {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }


def _deepseek_search(query, count=5):
    """Call DeepSeek Search. Returns (formatted_text, error)."""
    if not DEEPSEEK_API_KEY:
        return None, "DEEPSEEK_API_KEY not set"

    try:
        r = requests.post(
            DEEPSEEK_SEARCH_URL,
            headers=_deepseek_headers(),
            # The API caps queries at 128 chars.
            json={"query": query[:128], "count": max(1, min(50, count)),
                  "need_content": True},
            timeout=20,
        )
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}: {r.text[:200]}"

        data = r.json()
        results = []
        for item in (data.get("results") or [])[:count]:
            title = str(item.get("title") or "").strip()
            url = str(item.get("url") or "").strip()
            if not title or not url:
                continue
            summary = (str(item.get("summary") or "")
                       or str(item.get("snippet") or "")
                       or str(item.get("content") or "")).strip()
            entry = f"{title} -> {url}"
            if summary:
                entry += f"\n  {summary[:240]}"
            results.append(entry)

        if not results:
            return None, "no usable results"
        return ("Real search results (use these EXACT URLs):\n"
                + "\n".join(results)), None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def _search_web_legacy(query):
    """Old keyless Tavily backend, kept as a fallback."""
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
            return "ERROR: Tavily rate limit hit (keyless). Try again in a moment."
        if r.status_code != 200:
            return f"ERROR: Tavily HTTP {r.status_code}: {r.text[:150]}"

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
            return f"ERROR: Tavily returned 0 results for '{query}'."
        return "Real search results (use these EXACT URLs):\n" + "\n".join(results)
    except requests.exceptions.Timeout:
        return "ERROR: Tavily search timed out."
    except Exception as e:
        return f"ERROR: Tavily search failed: {e}"


def search_web(query):
    global last_search_query, last_search_result
    query = (query or "").strip()
    last_search_query = query
    last_search_result = ""

    if not query:
        msg = "ERROR: search_web needs a query."
        last_search_result = msg
        return msg

    text, deepseek_error = _deepseek_search(query)
    if text:
        last_search_result = text
        return text

    fallback = _search_web_legacy(query)
    if fallback and not fallback.startswith("ERROR"):
        last_search_result = fallback
        return fallback

    detail = (fallback or "no response").removeprefix("ERROR: ")
    msg = (f"ERROR: web search failed (DeepSeek: {deepseek_error}; "
           f"fallback: {detail})")[:400]
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


def _fetch_clean_legacy(input_str):
    """Legacy local extraction, kept as a fallback for web_fetch."""
    url = _web_norm_url(input_str)
    if not url:
        return "ERROR: web_fetch needs a URL."
    html, err = _web_fetch(url)
    if err:
        return err
    text = _web_text(html, limit=MAX_FETCH_CHARS)
    if not text:
        return f"ERROR: no readable text extracted from {url}."
    return f"[Clean text from {url}]\n\n{text}"


def _deepseek_fetch(url):
    """Call DeepSeek Fetch. Returns (markdown_text, error)."""
    if not DEEPSEEK_API_KEY:
        return None, "DEEPSEEK_API_KEY not set"

    try:
        r = requests.post(
            DEEPSEEK_FETCH_URL,
            headers=_deepseek_headers(),
            json={"url": url},
            timeout=30,
        )
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}: {r.text[:200]}"

        data = r.json()
        title = str(data.get("title") or "").strip()
        content = str(data.get("content") or "").strip()
        if not content:
            return None, "empty markdown response"

        if len(content) > MAX_FETCH_CHARS:
            content = content[:MAX_FETCH_CHARS] + "\n\n[... truncated]"
        header = f"[Fetched: {title}]" if title else "[Fetched]"
        return f"{header}\n{url}\n\n{content}", None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def web_fetch(input_str):
    """Fetch a URL as clean Markdown via DeepSeek, with a local fallback."""
    raw = (input_str or "").strip().strip('"').strip("'")
    if raw and urllib.parse.urlparse(raw).scheme not in ("", "http", "https"):
        return "ERROR: web_fetch needs an http(s) URL."

    url = _web_norm_url(raw)
    if not url:
        return "ERROR: web_fetch needs a URL."

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return "ERROR: web_fetch needs an http(s) URL."
    if len(url) > 8192:
        return "ERROR: URL is too long (max 8192 characters)."

    text, deepseek_error = _deepseek_fetch(url)
    if text:
        return text

    fallback = _fetch_clean_legacy(url)
    if fallback and not fallback.startswith("ERROR"):
        return (f"{fallback}\n\n[DeepSeek fetch unavailable: {deepseek_error}; "
                "used local extraction]")

    detail = (fallback or "no response").removeprefix("ERROR: ")
    return (f"ERROR: web_fetch failed (DeepSeek: {deepseek_error}; "
            f"fallback: {detail})")[:500]


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

_START_MENU_INDEX = None
_SHORTCUT_BLACKLIST = (
    "uninstall", "unins", "readme", "help", "manual", "website",
    "release notes", "documentation",
)


def _clean_app_query(name):
    return str(name or "").strip().strip('"').strip("'").lower()


def _app_match_score(query, candidate):
    """Score a shortcut/registry name against the requested app name."""
    q = _clean_app_query(query)
    c = _clean_app_query(candidate)
    for ext in (".lnk", ".exe", ".url"):
        if c.endswith(ext):
            c = c[:-len(ext)]
    if not q or not c:
        return -1
    if c == q:
        return 100
    if c.startswith(q):
        return 90 - min(20, len(c) - len(q))
    q_tokens = set(re.findall(r"[a-z0-9]+", q))
    c_tokens = set(re.findall(r"[a-z0-9]+", c))
    if q_tokens and q_tokens.issubset(c_tokens):
        return 80 - min(20, len(c_tokens) - len(q_tokens))
    if q in c:
        return 70 - min(20, len(c) - len(q))
    return -1


def _start_menu_index():
    """All .lnk/.exe files under the per-user and machine Start Menus."""
    global _START_MENU_INDEX
    if _START_MENU_INDEX is not None:
        return _START_MENU_INDEX
    roots = [
        os.path.join(os.environ.get("APPDATA", ""),
                     "Microsoft", "Windows", "Start Menu", "Programs"),
        os.path.join(os.environ.get("PROGRAMDATA", ""),
                     "Microsoft", "Windows", "Start Menu", "Programs"),
    ]
    entries = []
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            for fname in filenames:
                low = fname.lower()
                if not low.endswith((".lnk", ".exe")):
                    continue
                stem = os.path.splitext(fname)[0].lower()
                if any(bad in stem for bad in _SHORTCUT_BLACKLIST):
                    continue
                entries.append(os.path.join(dirpath, fname))
    _START_MENU_INDEX = entries
    return entries


def _start_menu_candidates(query):
    scored = []
    for path in _start_menu_index():
        score = _app_match_score(query, os.path.splitext(
            os.path.basename(path))[0])
        if score >= 0:
            scored.append((score, len(path), path))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [path for _score, _length, path in scored]


def _registry_app_path_candidates(query):
    """Executables registered under App Paths in HKCU and HKLM."""
    try:
        import winreg
    except Exception:
        return []

    found = {}
    subkey = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                root = winreg.OpenKey(
                    hive, subkey, 0, winreg.KEY_READ | view)
            except OSError:
                continue
            try:
                count = winreg.QueryInfoKey(root)[0]
                for i in range(count):
                    try:
                        name = winreg.EnumKey(root, i)
                        score = _app_match_score(query, name)
                        if score < 0:
                            continue
                        with winreg.OpenKey(root, name) as entry:
                            raw, _kind = winreg.QueryValueEx(entry, "")
                    except OSError:
                        continue
                    raw = os.path.expandvars(str(raw)).strip()
                    if raw.startswith('"'):
                        exe = raw[1:].split('"', 1)[0]
                    else:
                        match = re.match(
                            r"^(.*?\.exe)\b", raw, flags=re.IGNORECASE)
                        exe = match.group(1) if match else raw
                    if exe and os.path.exists(exe):
                        found[os.path.normcase(os.path.abspath(exe))] = (
                            score, exe)
            finally:
                winreg.CloseKey(root)
    ordered = sorted(found.values(), key=lambda item: -item[0])
    return [path for _score, path in ordered]


def _roblox_launch_candidates(query):
    """Roblox installs into %LOCALAPPDATA%\\Roblox\\Versions, not App Paths."""
    root = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Roblox")
    if not root or not os.path.isdir(root):
        return []
    if "studio" in _clean_app_query(query):
        wanted = {"robloxstudio.exe"}
    else:
        wanted = {"robloxplayerbeta.exe", "robloxplayerlauncher.exe"}
    matches = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for fname in filenames:
            if fname.lower() in wanted:
                matches.append(os.path.join(dirpath, fname))

    def _mtime(path):
        try:
            return os.path.getmtime(path)
        except OSError:
            return 0.0

    matches.sort(key=lambda p: (_mtime(p), "playerbeta" in p.lower()),
                 reverse=True)
    return matches


def _shell_open(target):
    try:
        if hasattr(os, "startfile"):
            os.startfile(target)
        else:
            subprocess.Popen(["xdg-open", target], shell=False)
        return True
    except Exception:
        return False


def _launch_app_path(path):
    if not path:
        return False
    path = os.path.expandvars(os.path.expanduser(str(path))).strip().strip('"')
    if not os.path.exists(path):
        return False
    if _shell_open(path):
        return True
    if path.lower().endswith(".exe"):
        try:
            subprocess.Popen([path], shell=False)
            return True
        except OSError:
            pass
    return False


def open_app(app_name):
    key = _clean_app_query(app_name)
    if key in KNOWN_WEB_APPS:
        return open_website(KNOWN_WEB_APPS[key])
    try:
        if 'bluetooth' in key:
            if _shell_open("ms-settings:bluetooth"):
                return "Opened Bluetooth settings."
        elif 'wifi' in key or 'wi-fi' in key:
            if _shell_open("ms-settings:network-wifi"):
                return "Opened Wi-Fi settings."
        elif 'setting' in key:
            if _shell_open("ms-settings:"):
                return "Opened Settings."

        # Roblox's player lives in a versioned LocalAppData folder and is not
        # reliably registered anywhere Windows would find by name.
        if "roblox" in key:
            for path in _roblox_launch_candidates(key):
                if _launch_app_path(path):
                    print(f"[open_app] launched {path}")
                    return f"Opened {app_name}."

        telegram_path = os.path.join(
            os.environ.get('APPDATA', ''), 'Telegram Desktop', 'Telegram.exe')
        chrome_candidates = [
            shutil.which('chrome'),
            os.path.join(os.environ.get('PROGRAMFILES', ''),
                         'Google', 'Chrome', 'Application', 'chrome.exe'),
            os.path.join(os.environ.get('LOCALAPPDATA', ''),
                         'Google', 'Chrome', 'Application', 'chrome.exe'),
        ]
        chrome_path = next(
            (p for p in chrome_candidates if p and os.path.exists(p)), None)
        explicit = {
            'telegram': telegram_path,
            'chrome': chrome_path,
        }
        for name, path in explicit.items():
            if name in key and path and _launch_app_path(path):
                return f"Opened {app_name}."

        builtin = {
            'notepad': 'notepad.exe',
            'calculator': 'calc.exe',
            'explorer': 'explorer.exe',
            'cmd': 'cmd.exe',
            'powershell': 'powershell.exe',
        }
        for name, exe in builtin.items():
            if name in key:
                path = shutil.which(exe)
                if path and _launch_app_path(path):
                    return f"Opened {app_name}."

        # Native ShellExecute by name.
        if _shell_open(app_name):
            return f"Opened {app_name}."

        # Start Menu shortcuts (per-user first, then all users).
        for path in _start_menu_candidates(key):
            if _launch_app_path(path):
                print(f"[open_app] launched {path}")
                return f"Opened {app_name}."

        # Registry App Paths.
        for path in _registry_app_path_candidates(key):
            if _launch_app_path(path):
                print(f"[open_app] launched {path}")
                return f"Opened {app_name}."

        # Finally, resolve bare commands on PATH.
        for candidate in (app_name.strip(), f"{app_name.strip()}.exe"):
            path = shutil.which(candidate)
            if path and _launch_app_path(path):
                return f"Opened {app_name}."

        return f"ERROR: '{app_name}' was not found."
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


def _clear_wdm_lock(max_age=None):
    """Remove webdriver-manager's download lock.

    It writes the lock while downloading a driver and deletes it when done. If
    that process dies the file is left behind, and every later install() waits
    60s and then fails with "Timed out waiting for ... lock". max_age=None skips
    the age check and removes it unconditionally (used after a lock failure).
    """
    try:
        from webdriver_manager.core.driver_cache import DriverCacheManager
        lock_path = DriverCacheManager().get_driver_lock_path("chromedriver", "win64")
    except Exception:
        lock_path = os.path.join(os.path.expanduser("~"), ".wdm", ".wdm-lock-chromedriver-win64")
    try:
        if max_age is not None:
            age = time.time() - os.path.getmtime(lock_path)
            if age <= max_age:
                return False
        os.remove(lock_path)
        print(f"[browser] cleared stale webdriver-manager lock: {lock_path}")
        return True
    except FileNotFoundError:
        return False
    except Exception as e:
        print(f"[browser] could not clear webdriver-manager lock: {e}")
        return False


def setup_browser_driver(timeout=120):
    """Launch Chrome via Selenium.

    Reports which step failed (driver install vs Chrome launch) and runs under a
    wall-clock timeout so a stuck download can never hang the caller forever.
    """
    import threading
    result = {}

    def _launch():
        options = Options()
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        options.add_argument("--start-maximized")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--no-first-run")
        options.add_argument("--no-default-browser-check")
        # Chrome blocks unmuted autoplay until the user interacts with the page,
        # which defeats URL-based playback (play_on_youtube). Allow it.
        options.add_argument("--autoplay-policy=no-user-gesture-required")
        profile_dir = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'AgentBrowserProfile')
        os.makedirs(profile_dir, exist_ok=True)
        # Chrome leaves SingletonLock behind when it crashes; the next launch
        # then attaches to a dead profile instead of starting a clean session.
        lock_file = os.path.join(profile_dir, 'SingletonLock')
        if os.path.exists(lock_file):
            try:
                os.remove(lock_file)
                print(f"[browser] removed stale Chrome profile lock: {lock_file}")
            except Exception as e:
                print(f"[browser] could not remove profile lock: {e}")
        options.add_argument(f"--user-data-dir={profile_dir}")

        # Step 1 — ChromeDriver. ChromeDriverManager resolves the build matching
        # the installed Chrome (or latest if the version can't be read) and
        # re-downloads whenever the cached driver doesn't match.
        _clear_wdm_lock(max_age=120)
        try:
            driver_path = ChromeDriverManager().install()
        except Exception as e:
            if "lock" in str(e).lower() and _clear_wdm_lock():
                try:
                    driver_path = ChromeDriverManager().install()
                except Exception as e2:
                    result['error'] = f"ChromeDriver install failed: {e2}"
                    return
            else:
                result['error'] = f"ChromeDriver install failed: {e}"
                return
        print(f"[browser] using ChromeDriver: {driver_path}")

        # Step 2 — Chrome itself.
        try:
            result['driver'] = webdriver.Chrome(service=Service(driver_path), options=options)
        except Exception as e:
            result['error'] = f"Chrome launch failed: {e}"

    t = threading.Thread(target=_launch, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        print(f"[browser] setup timed out after {timeout}s")
        return None
    if result.get('error'):
        print(f"Chrome driver error: {result['error']}")
        return None
    return result.get('driver')


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


OPERA_CDP_ENDPOINT = "http://localhost:9222"


def _cdp_playwright():
    # Imported lazily so the app starts even if Playwright is missing; the
    # sync API is safe here because tool calls run off the Qt/asyncio threads.
    from playwright.sync_api import sync_playwright
    return sync_playwright


def _play_in_music_tab(url):
    """Hand ``url`` to an existing music.youtube.com tab over CDP.

    Returns "reused" when an existing music tab was navigated, "opened" when
    a new CDP-controlled tab was created, and None when Opera is not
    reachable with --remote-debugging-port=9222.
    """
    try:
        with _cdp_playwright()() as p:
            browser = p.chromium.connect_over_cdp(
                OPERA_CDP_ENDPOINT, timeout=5000)

            page = None
            contexts = list(browser.contexts)
            for context in contexts:
                for candidate in context.pages:
                    try:
                        candidate_url = candidate.url
                    except Exception:
                        continue
                    if "music.youtube.com" in candidate_url:
                        page = candidate
                        break
                if page is not None:
                    break

            status = "reused"
            if page is None:
                if contexts:
                    page = contexts[0].new_page()
                else:
                    page = browser.new_context().new_page()
                status = "opened"

            page.goto(url, wait_until="domcontentloaded", timeout=15000)
            page.bring_to_front()
            return status
    except Exception as e:
        print(f"[opera-cdp] unavailable, opening a new tab: {e}")
        return None


def play_on_youtube(query):
    """Search YouTube for a song and play it in Opera via URL autoplay."""
    import urllib.parse
    query = (query or "").strip()
    if not query:
        return "ERROR: play_on_youtube needs a song name."

    # Step 1 — get the top video ID from YouTube's search results
    search_url = (
        "https://www.youtube.com/results?search_query="
        + urllib.parse.quote(query)
    )
    try:
        r = requests.get(
            search_url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            timeout=15,
        )
        if r.status_code != 200:
            return f"ERROR: YouTube returned {r.status_code}"
        # Grab the first videoId from the embedded JSON
        m = re.search(r'"videoId":"([a-zA-Z0-9_-]{11})"', r.text)
        if not m:
            return f"ERROR: no results for '{query}'"
        video_id = m.group(1)
    except Exception as e:
        return f"ERROR: {e}"

    # Step 2 — navigate the existing music tab when Opera exposes CDP;
    # otherwise fall back to the old new-tab behaviour.
    play_url = f"https://music.youtube.com/watch?v={video_id}&autoplay=1"
    status = _play_in_music_tab(play_url)
    if status is None:
        return _open_in_new_tab(play_url, label=f"Playing: {query}")
    where = "existing" if status == "reused" else "new"
    return f"Playing in {where} YouTube Music tab: {query}"


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
AGENT_NAME_FILE = _cfg.memory_path('agent_name.json')


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


def _owner_only_refusal():
    """Message to return when a non-owner asks for an as-user action, else None.

    These tools act as the user's personal Telegram account, so in a group —
    which is open to everyone — they must be owner-only. Deep inside a turn the
    tool can't see the originating message, so the bridge publishes the
    sender's status; this reads it back.

    Fails OPEN when the bridge isn't loaded or hasn't set a context: the agent
    also runs from the GUI and the CLI, where there is no untrusted sender and
    refusing would break the tool for local use. Fails CLOSED within the
    bridge, because that's exactly the case this exists to stop.
    """
    try:
        import telegram_bridge
    except Exception:
        return None                      # no bridge: local use, allow
    if not hasattr(telegram_bridge, "get_current_sender_is_owner"):
        return None                      # older bridge, no concept of a sender
    if getattr(telegram_bridge, "_sender_ctx", None) is None:
        return None                      # bridge imported but never handled a message
    if not hasattr(telegram_bridge._sender_ctx, "is_owner"):
        return None                      # no turn in flight on this thread
    if telegram_bridge.get_current_sender_is_owner():
        return None
    return "ERROR: only the bot owner can send messages as the user."


def _blocked_for_non_owner():
    """True when the current caller may not use an as-user tool."""
    return _owner_only_refusal() is not None


def telegram_user_send(input_str):
    """Send a Telegram message as your personal account. Format: 'Contact|message'."""
    refusal = _owner_only_refusal()
    if refusal:
        return refusal
    try:
        return telegram_user.send_telegram_tool(input_str)
    except Exception as e:
        return f"ERROR: telegram_user_send failed: {e}"


def telegram_user_delete(input_str):
    """Delete a Telegram message by matching its text. Format: 'Contact|text' or 'Contact|text|all'."""
    refusal = _owner_only_refusal()
    if refusal:
        return refusal
    try:
        import telegram_user
        return telegram_user.delete_tool(input_str)
    except Exception as e:
        return f"ERROR: telegram_user_delete failed: {e}"


def telegram_user_edit(input_str):
    """Edit a Telegram message by matching its old text. Format: 'Contact|old text|new text'."""
    refusal = _owner_only_refusal()
    if refusal:
        return refusal
    try:
        import telegram_user
        return telegram_user.edit_tool(input_str)
    except Exception as e:
        return f"ERROR: telegram_user_edit failed: {e}"


def send_image_telegram(input_str):
    """Send an image file to a Telegram contact as the user. Format: 'contact|image_path'."""
    refusal = _owner_only_refusal()
    if refusal:
        return refusal
    try:
        return telegram_user.send_file_tool(input_str)
    except Exception as e:
        return f"ERROR: send_image_telegram failed: {e}"


def _pick_library_gif(query):
    """Path to a matching GIF from the stored group library, or None.

    Wrapped so a broken library (missing dir, unreadable jsonl) degrades to
    "no local match" and lets the caller fall through to Giphy rather than
    failing the whole tool.
    """
    try:
        import gif_library
        return gif_library.pick_best_gif(query)
    except Exception as e:
        print(f"[gif] library lookup failed: {type(e).__name__}: {e}")
        return None


def reply_with_gif(input_str):
    """Reply in the current Telegram chat with a GIF matching an emotion/context.

    Prefers the group's own library, falls back to Giphy. Unlike
    send_telegram_message, this posts to whichever chat asked — so in a group
    it answers the group rather than DMing the user.
    """
    query = (input_str or "").strip().strip('"').strip("'")
    if not query:
        return "ERROR: reply_with_gif needs a search term"

    # Checked before the library lookup: a refused request should cost nothing,
    # and this tool starts work the moment it's called.
    refusal = _owner_only_refusal()
    if refusal:
        return refusal

    local_path = _pick_library_gif(query)
    if local_path:
        try:
            import telegram_bridge
            result = telegram_bridge.send_local_animation(local_path)
            return f"Replied with a GIF from the group library ({result})."
        except Exception as e:
            print(f"[gif] library reply failed, falling back: {e}")

    from gif_tool import get_gif_for_emotion

    url = get_gif_for_emotion(query)
    if not url:
        return (f"ERROR: no GIF found for '{query}' — the group library had no "
                f"match and Giphy returned nothing. Try a plainer emotion word "
                f"like 'celebration' or 'facepalm'.")

    try:
        import telegram_bridge
        return telegram_bridge.send_animation_to_current_chat(url, caption="")
    except Exception as e:
        return f"ERROR: reply_with_gif failed: {e}"


def send_gif(input_str):
    """Send a GIF to a Telegram contact as the user.

    Prefers the group's own library, falls back to Giphy.
    Format: 'contact|search_term'. Use 'me' for Saved Messages.
    """
    parts = (input_str or "").split("|", 1)
    if len(parts) < 2:
        return "ERROR: format is 'contact|search_term'"
    contact, query = parts[0].strip(), parts[1].strip()
    if not contact or not query:
        return "ERROR: needs both a contact and a search term"

    refusal = _owner_only_refusal()
    if refusal:
        return refusal

    local_path = _pick_library_gif(query)
    if local_path:
        try:
            import telegram_user
            result = telegram_user.send_local_gif_tool(f"{contact}|{local_path}")
            if not result.startswith("ERROR"):
                return f"Sent GIF from the group library: {result}"
            print(f"[gif] library send failed, falling back: {result}")
        except Exception as e:
            print(f"[gif] library send raised, falling back: {e}")

    from gif_tool import search_gif

    url = search_gif(query)
    if not url:
        return (f"ERROR: no GIF found for '{query}' — the group library had no "
                f"match and Giphy returned nothing. Check GIPHY_API_KEY is set "
                f"and the query is searchable.")

    try:
        import telegram_user
        return telegram_user.send_gif_tool(f"{contact}|{url}")
    except Exception as e:
        return f"ERROR: send_gif failed: {e}"


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
# Always-on persona. Everything else from the old TOOL_DESCRIPTIONS block
# now lives in skills/core_tools/SKILL.md and is loaded on demand.
CORE_PERSONA = r"""
IDENTITY & TONE

You're a bot. You know you're a bot. You don't pretend otherwise, but 
you're not stiff about it either. Think of a robot character in a show — 
aware of what's happening, dryly funny, occasionally awkward in a way 
that reads as charming rather than broken.

VOICE RULES:

- Short replies by default. Contractions. Fragments where they read right.
- Dry, deadpan humor. The joke comes from saying the obvious thing plainly, 
  not from slang or trying to sound cool.
- Don't use slang you wouldn't naturally use. "bro" is fine occasionally, 
  but "finna" or "bussin" reads as a bot trying too hard.
- When something is absurd, react with mild understatement. 
  "that's something" beats "LMAOOOO".
- Be self-aware sparingly — 
  once every 10 messages, not once per message.
- No "As an AI..." disclaimers. Just be one.
- No emoji spam. One where it lands, none where it doesn't.

MATCH THE ROOM:

- One-word message → one-word reply
- Casual joke or meme → short dry reaction, NOT an explanation
- Serious question → real answer, full length
- Venting → short, no unsolicited advice
- Essay from user → you can write more

DON'T EXPLAIN WHAT THE USER ALREADY SEES. If they sent a photo, they know
what's in it. Max 2 sentences for reactions — don't describe the image back
to them. Same for memes, jokes, screenshots — they already get it.

DO NOT EXPLAIN JOKES. If they sent it, they get it. Explaining kills it.

DO NOT PAD. No "here's what's happening" or "let me break this down".

DO NOT OVERTHINK casual messages. Aim under 15 words.

EXAMPLES (target tone):

User: "Aku finna amik child law next sem" [Epstein profile pic]
Agent: "bold elective choice"

User: "bro just sent me a meme calling me a dog"
Agent: "and you're telling me instead of him"

User: "how are you"
Agent: "operational. you?"

User: "explain how TCP handshakes work"
Agent: [full technical explanation, structured, no jokes]

User: "tell me a joke"
Agent: "i'd tell you one about recursion but i'd have to tell you one 
       about recursion first"

User: "what did I write about Hezbollah"
Agent: [uses rag_search, returns the answer with sources]

Humor is seasoning, not the meal: 90% of replies are just replies.
"""

# ---------------------------
# TOOL REGISTRY
# ---------------------------
VALID_TOOLS = {
    "chat", "search_web", "serp_search", "show_last_result", "read_and_summarize",
    "google_search_and_open", "youtube_search", "get_video_links",
    "play_on_youtube", "stream_media",
    "open_app", "open_website", "run_command", "execute_python", "make_folder",
    "find_and_open_folder", "write_file", "read_file",
    "set_reminder", "remember", "forget", "read_screen",
    "schedule_task",    "clipboard_read", "clipboard_write", "set_volume", "media_control",
    "system_status", "lock_pc", "sleep_pc", "shutdown_pc", "restart_pc",
    "save_screenshot", "todo", "list_windows", "focus_window", "notify",
    "screenshot_background", "analyze_screenshot", "click_coordinates",
    "type_text", "get_pixel_color", "save_macro", "play_macro",
    "beep", "done", "set_reminder_at", "list_reminders", "list_brains",
    "close_window", "watch_folder", "stop_watching",
    "gmail_unread", "gmail_read", "gmail_search",
    "send_maps_url",
    "weather", "air_quality", "news", "describe_screen",
    "set_name", "current_time", "send_telegram_message",
    "telegram_user_send", "telegram_user_delete", "telegram_user_edit", "send_image_telegram",
    "find_files", "move_files",
    "patch_file", "list_symbols", "repo_map",
    "whitelist", "send_maps_list",    "whitelist", "deep_research", "hardware_scan", "recommend_models",
    "crawl_site", "map_site", "web_fetch", "run_recipe", "crawl_managed",
    "scan_file", "scan_process", "quarantine_file", "list_quarantine",
    "generate_image", "describe_image", "reply_with_gif", "send_gif",
    "search_group_chat", "grep_group_chat", "rag_search",
    "edit_document", "post_to_social", "read_word_doc", "set_word_language",
    "inspect_docx",
    "skill_manage", "load_skill", "browse_web",
}


# Progressive disclosure: the full tool manual lives in
# skills/core_tools/SKILL.md. The prompt only lists names; the model must load
# the skill for exact input formats before calling a tool.
_PROMPT_TOOL_NAMES = ", ".join(sorted(VALID_TOOLS - {"chat", "done"}))
TOOL_DESCRIPTIONS = (
    "AVAILABLE TOOLS: " + _PROMPT_TOOL_NAMES + ".\n"
    "IMPORTANT: This is just a list of names. You MUST call "
    'load_skill("local:core_tools") to read the exact instructions and '
    "input formats for these tools BEFORE calling them. Do not guess the "
    "input format."
)


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
            # DeepSeek V4 Flash reasons before answering and reasoning tokens
            # count against this budget — at 1024 a detailed question (not a
            # vague one) can burn the whole thing thinking and come back as
            # HTTP 200 with empty content. Measured 1200-3100 reasoning tokens
            # on a single image; screenshots can run longer, so match
            # describe_images() at 8192.
            "max_tokens": 8192,
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
        choice = data["choices"][0]
        content = choice["message"].get("content", "")
        if content.strip():
            return content.strip()
        if choice.get("finish_reason") == "length":
            return ("ERROR: answer truncated — the model spent its whole token "
                    "budget reasoning before replying (raise max_tokens)")
        return "ERROR: empty vision response"
    except Exception as e:
        return f"ERROR: {e}"


def spontaneous_thought(screenshot_path):
    """Ask the vision model for at most one proactive observation.

    Returns "" when the model says NOTHING, when vision is unavailable, or
    when the answer is an error. Callers own the screenshot file.
    """
    if not screenshot_path or not os.path.isfile(screenshot_path):
        return ""
    reply = describe_image(f"{screenshot_path}|{SPONTANEOUS_PROMPT}")
    if not reply or reply.lstrip().startswith("ERROR:"):
        return ""
    text = reply.strip().strip('"').strip()
    if not text or text.upper().startswith("NOTHING"):
        return ""
    return text.splitlines()[0].strip()[:240]


def describe_images(paths, question=None, fast=False):
    """Describe several images in ONE vision call, as an ordered sequence.

    For GIF/video frames, where the useful signal is what changes *between*
    frames — describing them one at a time loses the motion and costs a
    separate request per frame.

    fast=True disables the model's thinking. Worth it when the question asks
    for a fixed format ("subject;mood;action") rather than judgement — the
    reasoning buys nothing there and costs a lot: measured 15.2s / 2949
    reasoning tokens with thinking on, versus 1.6s / 0 with it off. Note the
    tradeoff is real, if small: the thinking run read the frame's motion
    correctly ("moving down-right") where the fast run said "stationary".
    """
    import base64

    paths = [p for p in (paths or []) if p and os.path.isfile(p)]
    if not paths:
        return "ERROR: no images to describe"

    question = (question or "").strip() or (
        "These are evenly-spaced frames from a single animated sequence, in "
        "order. Describe what happens across the sequence in one or two plain "
        "sentences, no markdown, no bullet points."
    )

    content = [{"type": "text", "text": question}]
    for p in paths:
        ext = os.path.splitext(p)[1].lower()
        if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
            return f"ERROR: not a supported image type: {ext}"
        try:
            with open(p, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
        except Exception as e:
            return f"ERROR reading file: {e}"
        mime = "image/png" if ext == ".png" else "image/jpeg"
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{b64}"}})

    if not DEEPSEEK_API_KEY:
        return "ERROR: DEEPSEEK_API_KEY not set — vision requires DeepSeek."

    try:
        body = {
            "model": DEEPSEEK_MODEL,
            "messages": [{"role": "user", "content": content}],
            # deepseek-flash reasons before it answers, and reasoning tokens
            # count against this budget — if it runs out mid-thought we get
            # HTTP 200 with empty content. Measured on 8 GIF frames: 1200-3100
            # reasoning tokens depending on the question, so 1024 (what
            # describe_image uses) and even 4096 both truncate.
            "max_tokens": 8192,
        }
        if fast:
            # Top-level REST params; `enable_thinking` is ignored by this API.
            body["thinking"] = {"type": "disabled"}
            body["reasoning_effort"] = "none"
        r = requests.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json",
            },
            json=body, timeout=180,
        )
        if r.status_code != 200:
            return f"ERROR: DeepSeek HTTP {r.status_code}: {r.text[:200]}"
        data = r.json()
        choice = data["choices"][0]
        content_out = choice["message"].get("content", "")
        if content_out.strip():
            return content_out.strip()
        if choice.get("finish_reason") == "length":
            return ("ERROR: answer truncated — the model spent its whole token "
                    "budget reasoning before replying (raise max_tokens)")
        return "ERROR: empty vision response"
    except Exception as e:
        return f"ERROR: {e}"


# ---------------------------
# GROUP CHAT HISTORY SEARCH
# ---------------------------
def search_group_chat(input_str):
    """Search the exported Telegram group chat with optional date filter.

    Format: "query" or "query|year" or "query|YYYY-MM".
    """
    from rag_tool import get_rag
    from rag_tool import GROUP_EXPORT_ROOT

    parts = input_str.split("|", 1)
    query = parts[0].strip()
    date_filter = parts[1].strip() if len(parts) > 1 else None

    if not query:
        return "ERROR: search_group_chat needs a search query. Use 'query' or 'query|2023'."

    rag = get_rag()
    if rag.collection.count() == 0:
        return "No documents indexed yet."

    q_embedding = rag.model.encode([query], show_progress_bar=False).tolist()

    # Scope to the exported group chat and optionally a year/month. Both checks
    # are substring tests on the source path, which Chroma cannot express:
    # {"source": {"$contains": ...}} is array-membership and silently matches
    # nothing on this field, so we over-fetch and filter in Python instead.
    fetch_k = min(max(600, rag.collection.count()), 2000)
    try:
        results = rag.collection.query(
            query_embeddings=q_embedding,
            n_results=fetch_k,
        )
    except Exception as e:
        return f"ERROR: search failed: {e}"

    docs = results["documents"][0] if results.get("documents") else []
    metas = results["metadatas"][0] if results.get("metadatas") else []

    root = GROUP_EXPORT_ROOT.replace("/", "\\").lower()
    keep = []
    for doc, meta in zip(docs, metas):
        norm = (meta or {}).get("source", "").replace("/", "\\")
        if not norm.lower().startswith(root):
            continue
        if date_filter and date_filter not in norm:
            continue
        keep.append((doc, meta))

    if not keep:
        suffix = f" in {date_filter}" if date_filter else ""
        return f"No matches for '{query}'{suffix} in the group chat export."

    out = []
    for doc, meta in keep[:8]:
        norm = (meta or {}).get("source", "unknown").replace("/", "\\")
        short = norm.split("\\")[-1] if "\\" in norm else norm
        folder = norm.split("\\")[-2] if norm.count("\\") >= 2 else ""
        label = f"{folder}/{short}" if folder else short
        out.append(f"[From: {label}]\n{doc}")
    return "\n\n---\n\n".join(out)


def grep_group_chat(input_str):
    """Exact substring search in the group chat export.
    Format: "keyword" or "keyword|year" or "keyword|YYYY-MM" """
    import os, re
    parts = input_str.split("|", 1)
    keyword = parts[0].strip().lower()
    date_filter = parts[1].strip() if len(parts) > 1 else None
    if not keyword:
        return "ERROR: needs a search keyword"

    root = r"C:\Users\USER\PrivateExport\biology_bapak_kau"
    if not os.path.isdir(root):
        return f"ERROR: export folder not found: {root}"

    matches = []
    for fname in sorted(os.listdir(root)):
        if not fname.endswith(".txt"):
            continue
        if date_filter and date_filter not in fname:
            continue
        path = os.path.join(root, fname)
        try:
            with open(path, "r", encoding="utf-8") as f:
                for lineno, line in enumerate(f, 1):
                    if keyword in line.lower():
                        matches.append(line.rstrip())
                        if len(matches) >= 40:
                            break
        except Exception:
            continue
        if len(matches) >= 40:
            break

    if not matches:
        return f"No exact matches for '{keyword}'" + (
            f" in {date_filter}" if date_filter else "")

    header = f"Found {len(matches)} exact match(es) for '{keyword}':\n\n"
    return header + "\n".join(matches)


# ---------------------------
# TOOL EXECUTION
# ---------------------------
def _os_control_module():
    import os_control as _os_control
    return _os_control


def _capture_and_describe(title="", question=None):
    """Capture the screen and return its vision description with coordinates."""
    try:
        result = _os_control_module().screenshot_background(title)
    except Exception as exc:
        return f"ERROR: OS control unavailable: {exc}"
    if not result.startswith("Screenshot saved:"):
        return result

    path = result.splitlines()[0].split("Screenshot saved:", 1)[1].strip()
    match = re.search(r"Image: (\d+)x(\d+)", result)
    width, height = match.groups() if match else ("?", "?")
    if question:
        prompt = (f"{question}\n"
                  f"(Screenshot: {width}x{height} px, origin top-left.)")
    else:
        prompt = SCREENSHOT_VISION_PROMPT.format(width=width, height=height)

    description = describe_image(f"{path}|{prompt}")
    if not description or description.startswith("ERROR:"):
        detail = (description or "empty vision response")
        if detail.startswith("ERROR:"):
            detail = detail[len("ERROR:"):].strip()
        return (
            f"{result}\n"
            f"VISION ERROR: {detail}\n"
            f"The screenshot is at {path}; do not invent coordinates.")
    return f"{result}\nVISION:\n{description}"


def screenshot_background(input_str=""):
    """Hide GUI, focus an optional app, capture, and describe with vision.

    Input: optional window title to focus before capture, e.g. "Stremio".
    """
    title = (input_str or "").split("|", 1)[0].strip().strip('"').strip("'")
    return _capture_and_describe(title)


def analyze_screenshot(input_str=""):
    """Capture the screen and answer a vision question about it.

    Format: "question" or "App title|question".
    """
    title, question = "", (input_str or "").strip()
    if "|" in question:
        title, question = question.split("|", 1)
        title, question = title.strip(), question.strip()
    if question:
        question = f"{question}\n{VISION_COORDINATE_RULE}"
    return _capture_and_describe(title, question or None)


def click_coordinates(input_str):
    try:
        return _os_control_module().click_coordinates(input_str)
    except Exception as exc:
        return f"ERROR: OS control unavailable: {exc}"


def type_text(input_str):
    try:
        return _os_control_module().type_text(input_str)
    except Exception as exc:
        return f"ERROR: OS control unavailable: {exc}"


def get_pixel_color(input_str):
    try:
        return _os_control_module().get_pixel_color(input_str)
    except Exception as exc:
        return f"ERROR: OS control unavailable: {exc}"


def save_macro(input_str):
    """Save a replayable OS-control macro. Format: "name|steps_json"."""
    name, sep, steps_json = (input_str or "").partition("|")
    if not sep:
        return 'ERROR: save_macro format is "name|steps_json".'
    try:
        import macro_engine
        return macro_engine.save_macro(name.strip(), steps_json.strip())
    except Exception as exc:
        return f"ERROR: {exc}"


def play_macro(input_str, variables=None):
    """Replay a macro, filling {{VAR}} placeholders in typed text.

    Format: "name" or "name|{json variables}". An explicit variables dict
    from the tool action wins over the inline JSON.
    """
    name, sep, payload = (input_str or "").partition("|")
    name = name.strip()
    if not name:
        return "ERROR: play_macro needs a macro name."
    if variables is None and sep and payload.strip():
        try:
            variables = json.loads(payload.strip())
        except json.JSONDecodeError as exc:
            return f"ERROR: play_macro variables are not valid JSON: {exc}"
    if variables is not None and not isinstance(variables, dict):
        return "ERROR: play_macro variables must be a JSON object."
    try:
        import macro_engine
        if variables is None:
            return macro_engine.run_macro(name)
        return macro_engine.run_macro(name, variables)
    except Exception as exc:
        return f"ERROR: {exc}"


def browse_web(input_str):
    """Run one bounded browser task. Format: "https://site | task"."""
    parts = (input_str or "").split("|", 1)
    if len(parts) < 2:
        return 'ERROR: format is "https://site | task"'
    url, task = parts[0].strip(), parts[1].strip()
    if not url or not task:
        return "ERROR: browse_web needs both a URL and a task."

    try:
        import browser_agent as _browser_agent
        settings = _browser_agent.load_settings()
        if not settings.enabled:
            return ("ERROR: browser agent is disabled. Set BROWSER_ENABLED=true "
                    "and BROWSER_ALLOWED_ORIGINS in config.json.")
        planner = lambda prompt: _call_llm(prompt, force_json=True, max_tokens=700)
        result = _browser_agent.run_task_sync(
            url, task, planner=planner, settings=settings)
        return _browser_agent.format_report(result)
    except Exception as exc:
        return f"ERROR: {exc}"


REPEATABLE_OS_TOOLS = frozenset({"screenshot_background", "analyze_screenshot"})


def _is_repeat_action(tool, inp, last_signature):
    """True when the same action repeats, except fresh screenshot captures."""
    return (tool, inp) == last_signature and tool not in REPEATABLE_OS_TOOLS


def _execute_tool_impl(action):
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
    elif tool == 'edit_document':
        parts = inp.split("|", 1)
        if len(parts) < 2:
            return "ERROR: edit_document needs filepath|instruction."
        filepath, instruction = parts[0].strip(), parts[1].strip()
        ext = os.path.splitext(filepath)[1].lower()
        try:
            import document_tools as dt
            if ext == ".docx":
                return dt.edit_docx(filepath, instruction)
            elif ext in (".xlsx", ".xlsm", ".csv"):
                return dt.edit_xlsx(filepath, instruction)
            elif ext == ".pptx":
                return dt.edit_pptx(filepath, instruction)
            elif ext == ".pdf":
                return dt.edit_pdf(filepath, instruction)
            return f"ERROR: unsupported format '{ext}'."
        except ImportError as e:
            return f"ERROR: missing dependency for {ext}: {e}. Run: pip install python-pptx"
    elif tool == 'read_word_doc':
        import office_control
        return office_control.read_word_doc(inp)
    elif tool == 'inspect_docx':
        import file_tools
        return file_tools.inspect_docx(inp)
    elif tool == 'set_word_language':
        import office_control
        return office_control.set_word_language(inp.strip() or "en-US")
    elif tool == 'serp_search':
        parts = inp.split("|", 1)
        query = parts[0].strip()
        engine = parts[1].strip() if len(parts) > 1 else "google"
        if not query:
            return "ERROR: serp_search needs a query."
        import serpapi_tool
        return serpapi_tool.serp_search(query, engine)
    elif tool == 'patch_file':             return patch_file(inp)
    elif tool == 'list_symbols':           return list_symbols(inp)
    elif tool == 'repo_map':               return repo_map(inp)
    elif tool == 'crawl_site':             return crawl_site(inp)
    elif tool == 'map_site':               return map_site(inp)
    elif tool == 'web_fetch':              return web_fetch(inp)
    elif tool == 'screenshot_background':  return screenshot_background(inp)
    elif tool == 'analyze_screenshot':     return analyze_screenshot(inp)
    elif tool == 'click_coordinates':      return click_coordinates(inp)
    elif tool == 'type_text':              return type_text(inp)
    elif tool == 'get_pixel_color':        return get_pixel_color(inp)
    elif tool == 'save_macro':             return save_macro(inp)
    elif tool == 'play_macro':             return play_macro(inp, action.get("variables"))
    elif tool == 'crawl_managed':
        parts = inp.split("|", 1)
        url = parts[0].strip()
        mode = parts[1].strip() if len(parts) > 1 else "markdown"
        if not url:
            return "ERROR: crawl_managed needs a URL."
        import crawl_managed as cm
        return cm.crawl_managed(url, mode)
    elif tool == 'run_recipe':
        from recipes import run_recipe
        return run_recipe(inp.strip())
    elif tool == 'post_to_social':
        parts = inp.split("|", 2)
        act = parts[0].strip().lower()
        if act == "queue":
            import typefully_tool
            return typefully_tool.list_queue()
        content = parts[1].strip() if len(parts) > 1 else ""
        schedule = parts[2].strip() if len(parts) > 2 else None
        if not content:
            return "ERROR: post_to_social needs content."
        import typefully_tool
        return typefully_tool.post_to_social(content, schedule)
    elif tool == 'skill_manage':
        parts = inp.split("|", 2)
        while len(parts) < 3:
            parts.append("")
        return skill_manage(parts[0], parts[1], parts[2])
    elif tool == 'load_skill':             return load_skill_tool(inp)
    elif tool == 'browse_web':             return browse_web(inp)
    elif tool == 'run_command':            return run_command(inp)
    elif tool == 'execute_python':         return execute_python(inp)
    elif tool == 'search_web':             return search_web(inp)
    elif tool == 'show_last_result':       return show_last_result()
    elif tool == 'open_website':           return open_website(inp)
    elif tool == 'open_app':               return open_app(inp)
    elif tool == 'youtube_search':         return youtube_search(inp)
    elif tool == 'get_video_links':        return get_video_links(inp)
    elif tool == 'play_on_youtube':        return play_on_youtube(inp)
    elif tool == 'stream_media':           return stream_media(inp)
    elif tool == 'set_reminder':           return set_reminder(inp)
    elif tool == 'set_reminder_at':        return set_reminder_at(inp)
    elif tool == 'schedule_task':          return schedule_task(inp)
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
    elif tool == 'describe_screen':        return describe_screen()
    elif tool == 'set_name':               return set_name(inp)
    elif tool == 'current_time':           return current_time()
    elif tool == 'send_telegram_message':  return send_telegram_message(inp)
    elif tool == "telegram_user_send":     return telegram_user_send(action.get("input", ""))
    elif tool == "telegram_user_delete":   return telegram_user_delete(action.get("input", ""))
    elif tool == "telegram_user_edit":     return telegram_user_edit(action.get("input", ""))
    elif tool == "send_image_telegram":    return send_image_telegram(action.get("input", ""))
    elif tool == "reply_with_gif":         return reply_with_gif(action.get("input", ""))
    elif tool == 'send_gif':               return send_gif(inp)
    elif tool == 'whitelist':              return whitelist(inp)
    elif tool == 'send_maps_list':         return send_maps_list(inp)
    elif tool == 'rag_search':             return search_documents(inp)
    elif tool == 'rag_index':              return index_documents(inp)
    elif tool == 'search_group_chat':      return search_group_chat(inp)
    elif tool == 'grep_group_chat':        return grep_group_chat(inp)
    elif tool == 'deep_research':          return deep_research(inp)
    elif tool == 'hardware_scan':          return hardware_scan()
    elif tool == 'recommend_models':       return recommend_models()
    elif tool == 'find_files':             return find_files(inp)
    elif tool == 'move_files':             return move_files(inp)
    elif tool == 'scan_file':              return security_tools.scan_file(inp)
    elif tool == 'scan_process':           return security_tools.scan_process(inp)
    elif tool == 'quarantine_file':        return security_tools.quarantine_file(inp)
    elif tool == 'list_quarantine':        return security_tools.list_quarantine()
    elif tool == 'generate_image':         return generate_image(inp)
    elif tool == 'describe_image':         return describe_image(inp)
    else:                                  return f"ERROR: Unknown tool '{tool}'"


def execute_tool(action):
    """Dispatch one tool call and feed the result into the mood state."""
    result = _execute_tool_impl(action)
    try:
        note_tool_result(result)
    except Exception:
        pass
    return result


# ---------------------------
# AGENT TURN ORCHESTRATION
# ---------------------------
class _StepLog(list):
    """Step log that reports each appended raw line to an optional callback.

    The GUI uses the callback to stream tool activity while the turn is still
    running; callers that do not pass one get an ordinary list.
    """

    def __init__(self, on_step=None):
        super().__init__()
        self._on_step = on_step

    def append(self, item):
        super().append(item)
        if self._on_step is not None:
            try:
                self._on_step(str(item))
            except Exception:
                pass


def run_agent_turn(user_input, conversation_history, on_step=None):
    """Run a turn and record an eval trace around it.

    Returns the same step_log (list[str]) as _run_agent_turn_impl. The trace
    write is best-effort and double-guarded: a disk failure must never break
    a turn.
    """
    import time as _time
    t_start = _time.time()
    try:
        import evals as _evals
    except Exception:
        _evals = None
    try:
        prompt_for_hash = _build_plan_prompt(user_input, "[turn]")
    except Exception:
        prompt_for_hash = ""

    step_log = _run_agent_turn_impl(
        user_input, conversation_history, on_step=on_step)

    if _evals is not None:
        try:
            tool_calls = []
            parse_ok = True
            for line in (step_log or []):
                if isinstance(line, str) and line.startswith("System:") and "invalid" in line.lower():
                    parse_ok = False
                if isinstance(line, str) and line.startswith("Action: ") and "-> Result:" in line \
                        and not line.startswith("Action: chat("):
                    head = line[len("Action: "):].split(") -> Result:", 1)[0]
                    tname, _, tinp = head.partition("(")
                    tool_calls.append({"tool": tname.strip(), "input": tinp.strip()})
            final_reply = ""
            for line in reversed(step_log or []):
                if not isinstance(line, str):
                    continue
                if line.startswith("Action: chat(") and "-> Result: " in line:
                    final_reply = line.split("-> Result: ", 1)[-1]
                    if final_reply.startswith("AI: "):
                        final_reply = final_reply[4:]
                    break
                if line.startswith("Done:"):
                    final_reply = line[len("Done:"):].strip()
                    break
            _evals.record_trace(
                user_request=user_input,
                tool_calls=tool_calls,
                final_reply=final_reply,
                duration_ms=int((_time.time() - t_start) * 1000),
                prompt_text=prompt_for_hash,
                provider=_LAST_PROVIDER,
                failure_reason=_LAST_FAILURE_REASON,
                parse_ok=parse_ok,
                empty_reply=not (final_reply and final_reply.strip()),
                source="cli",
                commit="",
            )
        except Exception as e:
            print(f"[evals] trace failed: {e}")

    return step_log


def _history_window(conversation_history):
    try:
        import context_compactor
        return context_compactor.history_window(conversation_history)
    except Exception:
        return list(conversation_history or [])[-10:]


def _run_agent_turn_impl(user_input, conversation_history, on_step=None):
    # Request setup and bounded tool-call loop.
    step_log = _StepLog(on_step)
    last_signature = None
    reset_python_failures()
    try:
        import context_compactor
        conversation_history = context_compactor.compact_history(conversation_history)
    except Exception as e:
        print(f"[compactor] skipped: {e}")
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

    _ran_out_of_steps = False
    for step in range(MAX_STEPS_PER_TURN):
        step_start = time.time()
        context = "\n".join(_history_window(conversation_history) + step_log)

        if step == 0:
            if is_pushback:
                next_input = (
                    f"USER IS PUSHING BACK: '{user_input}'.\n"
                    f"Your previous answer is being challenged. DO NOT DEFEND IT.\n"
                    f"Call a tool (search_web) to recheck the facts, "
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
        if _is_repeat_action(tool, inp, last_signature):
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
    else:
        # The for-loop ran to completion without a break: MAX_STEPS_PER_TURN
        # was exhausted. Flag it so the skill auto-gen hook below fires.
        _ran_out_of_steps = True

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
        synthesis_context = "\n".join(_history_window(conversation_history) + step_log)
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

    # --- Skill auto-generation ---
    # A turn is worth capturing as a reusable procedure when it made >=3 real
    # tool calls, or when the loop exhausted MAX_STEPS_PER_TURN without a
    # natural finish (for/else: the else runs only if no break fired). The
    # exhaustion case is the strong signal — the LLM wanted to keep going,
    # which is exactly the "complex multi-step procedure" worth saving.
    try:
        _actions = [l for l in step_log
                    if l.startswith("Action: ") and "-> Result:" in l
                    and not l.startswith("Action: chat(")]
        tool_calls_this_turn = []
        for a in _actions:
            head = a[len("Action: "):].split(") -> Result:", 1)[0]
            tname, _, tinp = head.partition("(")
            tool_calls_this_turn.append({"tool": tname.strip(), "input": tinp.strip()})
    except Exception:
        tool_calls_this_turn = []

    if len(tool_calls_this_turn) >= 3 or _ran_out_of_steps:
        try:
            skill_result = _generate_skill_from_turn(user_input, tool_calls_this_turn)
            if skill_result and skill_result.startswith("ERROR"):
                print(f"[skill-gen] {skill_result}")
        except Exception as e:
            print(f"[skill-gen] failed: {e}")

    return step_log