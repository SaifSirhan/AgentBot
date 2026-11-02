"""Filesystem storage for oversized tool outputs.

Artifacts live under %LOCALAPPDATA%\\AgentMemory\\artifacts as <uuid>.txt;
a <uuid>.json sidecar records the source and size for provenance.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

ARTIFACT_THRESHOLD_CHARS = 2000
SUMMARY_PREVIEW_CHARS = 200

_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def artifacts_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / "AppData" / "Local"
    return root / "AgentMemory" / "artifacts"


def _normalize_id(artifact_id: Any) -> str:
    text = str(artifact_id or "").strip().lower().replace("-", "")
    if not _ID_RE.fullmatch(text):
        raise ValueError(f"Invalid artifact id: {artifact_id!r}")
    return text


def _as_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, indent=2, default=repr)


def save_artifact(content: Any, source: str = "agent") -> str:
    """Persist content and return the artifact id used by read_artifact."""
    artifact_id = uuid.uuid4().hex
    directory = artifacts_dir()
    directory.mkdir(parents=True, exist_ok=True)
    text = _as_text(content)
    temporary = directory / f"{artifact_id}.txt.tmp"
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, directory / f"{artifact_id}.txt")
    finally:
        temporary.unlink(missing_ok=True)
    metadata = {
        "id": artifact_id,
        "source": str(source or "agent"),
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "chars": len(text),
    }
    (directory / f"{artifact_id}.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return artifact_id


def read_artifact(artifact_id: Any) -> str:
    """Return the full saved text; raises ValueError or FileNotFoundError."""
    normalized = _normalize_id(artifact_id)
    return (artifacts_dir() / f"{normalized}.txt").read_text(encoding="utf-8")
