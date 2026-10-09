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
import os, json
from pathlib import Path

_IG_HISTORY = Path(os.environ.get("LOCALAPPDATA", "")) / "AgentMemory" / "conversation_instagram.json"
_IG_HISTORY_CAP = 40  # matches agent.MAX_HISTORY_ENTRIES


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
