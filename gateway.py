"""
Instagram gateway for AgentBot.

Runs as a daemon thread inside the GUI process. Polls Instagram DMs as the
user's own account (via instagrapi — same philosophy as telegram_user.py
using Telethon). Feeds inbound messages through agent.run_agent_turn and
sends the reply back.

Only replies to inbound messages. Never sends unsolicited DMs.
Hard-caps outbound messages at 10/hour to reduce ban risk.

This module owns its own conversation history, separate from the GUI's
conversation.json, so Instagram DMs never mix with the desktop chat.
"""
from __future__ import annotations
import os, json, time, threading, traceback
from pathlib import Path
from datetime import datetime

try:
    from instagrapi import Client
    from instagrapi.exceptions import (
        LoginRequired, ChallengeRequired, RateLimitError, PleaseWaitFewMinutes,
    )
    _INSTAGRAPI_OK = True
except Exception:
    _INSTAGRAPI_OK = False

import agent as _agent

_IG_SESSION = Path(os.environ.get("LOCALAPPDATA", "")) / "AgentMemory" / "ig_session.json"
_IG_STATE   = Path(os.environ.get("LOCALAPPDATA", "")) / "AgentMemory" / "ig_state.json"
_IG_LOG     = Path(os.environ.get("LOCALAPPDATA", "")) / "AgentMemory" / "ig_activity.log"

_IG_HISTORY = Path(os.environ.get("LOCALAPPDATA", "")) / "AgentMemory" / "conversation_instagram.json"
_IG_HISTORY_CAP = 40  # matches agent.MAX_HISTORY_ENTRIES

_active_adapter = None


def _load_ig_history() -> list:
    """Return the Instagram-scoped conversation as a flat list[str]."""
    if not _IG_HISTORY.exists():
        return []
    try:
        data = json.loads(_IG_HISTORY.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(data, list):
        return data[-_IG_HISTORY_CAP:]
    if isinstance(data, dict):
        return data.get("history", [])[-_IG_HISTORY_CAP:]
    return []


def _save_ig_history(hist: list) -> None:
    """Persist the Instagram-scoped conversation (flat list[str], capped)."""
    _IG_HISTORY.parent.mkdir(parents=True, exist_ok=True)
    _IG_HISTORY.write_text(
        json.dumps(list(hist)[-_IG_HISTORY_CAP:], indent=2),
        encoding="utf-8",
    )


def _extract_reply(step_log) -> str:
    """Pull the user-facing reply out of run_agent_turn's step_log.

    run_agent_turn returns a list[str] of log lines, not the reply string.
    Mirror agent.run_agent_turn's own extraction: scan backwards for the last
    chat action (or a Done: line) and strip the 'AI: ' prefix if present.
    """
    if not step_log:
        return ""
    for line in reversed(step_log):
        if not isinstance(line, str):
            continue
        if line.startswith("Action: chat(") and "-> Result: " in line:
            text = line.split("-> Result: ", 1)[-1]
            if text.startswith("AI: "):
                text = text[4:]
            return text.strip()
        if line.startswith("Done:"):
            return line[len("Done:"):].strip()
    return ""


def get_active_adapter():
    return _active_adapter


def _log(line: str):
    try:
        _IG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _IG_LOG.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {line}\n")
    except Exception:
        pass


class InstagramAdapter:
    def __init__(self, config: dict):
        self.config = config
        self.client = Client()
        self.poll_interval = int(config.get("INSTAGRAM_POLL_INTERVAL", 15))
        self._running = False
        self._send_times: list = []
        self._last_seen: dict = self._load_state()

    def _load_state(self) -> dict:
        if not _IG_STATE.exists():
            return {}
        try:
            return json.loads(_IG_STATE.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_state(self):
        _IG_STATE.parent.mkdir(parents=True, exist_ok=True)
        _IG_STATE.write_text(json.dumps(self._last_seen, indent=2), encoding="utf-8")

    def login(self) -> str:
        if not _INSTAGRAPI_OK:
            return "ERROR: instagrapi not installed. Run: pip install instagrapi"
        user = self.config.get("INSTAGRAM_USERNAME", "")
        pw   = self.config.get("INSTAGRAM_PASSWORD", "")
        proxy = self.config.get("INSTAGRAM_PROXY", "")
        if not user or not pw:
            return "ERROR: INSTAGRAM_USERNAME / INSTAGRAM_PASSWORD not set in config."
        if proxy:
            try:
                self.client.set_proxy(proxy)
            except Exception as e:
                return f"ERROR: bad proxy: {e}"
        if _IG_SESSION.exists():
            try:
                self.client.load_settings(_IG_SESSION)
            except Exception:
                pass
        try:
            self.client.login(user, pw)
            self.client.dump_settings(_IG_SESSION)
            _log("login ok")
            return "ok"
        except ChallengeRequired:
            _log("login challenge")
            return ("ERROR: Instagram wants a security challenge. Log in from the "
                    "official IG app or web once to clear it, then retry.")
        except PleaseWaitFewMinutes:
            _log("login throttled")
            return "ERROR: Instagram throttled the login. Wait a few minutes and retry."
        except Exception as e:
            _log(f"login fail: {e}")
            return f"ERROR: Instagram login failed: {e}"

    def start(self):
        self._running = True
        t = threading.Thread(target=self._poll_loop, daemon=True, name="ig-gateway")
        t.start()

    def stop(self):
        self._running = False

    def _poll_loop(self):
        while self._running:
            try:
                threads = self.client.direct_threads(amount=10)
                for thread in threads:
                    self._process_thread(thread)
                self._save_state()
            except RateLimitError:
                _log("rate limited — backing off 60s")
                time.sleep(60)
            except PleaseWaitFewMinutes:
                _log("throttled — backing off 120s")
                time.sleep(120)
            except LoginRequired:
                _log("session expired — attempting re-login")
                if self.login().startswith("ERROR"):
                    time.sleep(300)
            except Exception:
                _log("poll error:\n" + traceback.format_exc())
            time.sleep(self.poll_interval)

    def _process_thread(self, thread):
        tid = str(thread.id)
        if not thread.messages:
            return
        last = thread.messages[0]
        last_id = str(last.id)
        if self._last_seen.get(tid) == last_id:
            return
        self._last_seen[tid] = last_id

        if str(last.user_id) == str(self.client.user_id):
            return

        text = getattr(last, "text", "") or ""
        if not text.strip():
            return

        _log(f"inbound thread={tid} from={last.user_id}: {text[:80]}")

        hist = _load_ig_history()
        hist.append(f"user: {text}")
        try:
            step_log = _agent.run_agent_turn(text, hist)
            reply = _extract_reply(step_log)
        except Exception as e:
            reply = f"(error: {e})"

        if reply and not reply.startswith("ERROR"):
            self._send(tid, reply)
            hist.append(f"assistant: {reply}")
        _save_ig_history(hist)

    def _can_send(self) -> bool:
        now = time.time()
        self._send_times = [t for t in self._send_times if now - t < 3600]
        return len(self._send_times) < 10

    def _send(self, thread_id: str, text: str):
        if not self._can_send():
            _log(f"send cap reached — dropping reply to {thread_id}")
            return
        try:
            self.client.direct_send(text, thread_ids=[thread_id])
            self._send_times.append(time.time())
            _log(f"sent to {thread_id}: {text[:80]}")
        except Exception as e:
            _log(f"send fail to {thread_id}: {e}")

    def send_to_self(self, text: str):
        try:
            self.client.direct_send(text, user_ids=[self.client.user_id])
        except Exception as e:
            _log(f"send_to_self fail: {e}")


def start_gateway(config: dict):
    """Called from agent_gui at startup. Returns the adapter, or an ERROR string."""
    global _active_adapter
    if not config.get("INSTAGRAM_ENABLED"):
        return None
    if not _INSTAGRAPI_OK:
        return "ERROR: instagrapi not installed."
    adapter = InstagramAdapter(config)
    result = adapter.login()
    if result.startswith("ERROR"):
        _log(f"gateway not started: {result}")
        return result
    adapter.start()
    _active_adapter = adapter
    _log("gateway started")
    return adapter
