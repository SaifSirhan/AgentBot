"""Replayable OS-control macros stored as JSON under the memory directory.

A macro is a JSON list of steps:

    [{"action": "click", "x": 100, "y": 200},
     {"action": "type", "text": "hello"},
     {"action": "wait", "seconds": 1}]

Storage defaults to %LOCALAPPDATA%\\AgentMemory\\macros and follows
AGENT_MEMORY_DIR when configured, because that is the app's active memory root.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, List

import config

MACRO_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")
MAX_STEPS = 100
MAX_TYPE_CHARS = 5_000
MAX_WAIT_SECONDS = 60.0


class MacroError(Exception):
    """A macro could not be saved, loaded, or run."""


def macros_dir() -> Path:
    path = Path(config.memory_path("macros"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def normalise_name(name: str) -> str:
    slug = (name or "").strip().lower().replace(" ", "_")
    if not MACRO_NAME_RE.fullmatch(slug):
        raise MacroError(
            "macro name must be letters, digits, _ or - (max 64)")
    return slug


def macro_path(name: str) -> Path:
    return macros_dir() / f"{normalise_name(name)}.json"


def _validate_steps(steps: Any) -> List[dict]:
    if not isinstance(steps, list):
        raise MacroError("macro must be a JSON list of steps")
    if not steps:
        raise MacroError("macro has no steps")
    if len(steps) > MAX_STEPS:
        raise MacroError(f"macro has too many steps (max {MAX_STEPS})")

    clean: List[dict] = []
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise MacroError(f"step {index} is not an object")
        action = step.get("action")
        if action == "click":
            try:
                x = int(step["x"])
                y = int(step["y"])
            except (KeyError, TypeError, ValueError):
                raise MacroError(
                    f"step {index}: click needs integer x and y") from None
            if x < 0 or y < 0:
                raise MacroError(
                    f"step {index}: coordinates must be non-negative")
            clean.append({"action": "click", "x": x, "y": y})
        elif action == "type":
            text = step.get("text")
            if not isinstance(text, str) or not text:
                raise MacroError(f"step {index}: type needs non-empty text")
            if len(text) > MAX_TYPE_CHARS:
                raise MacroError(
                    f"step {index}: text is too long (max {MAX_TYPE_CHARS})")
            clean.append({"action": "type", "text": text})
        elif action == "wait":
            try:
                seconds = float(step.get("seconds", 0))
            except (TypeError, ValueError):
                raise MacroError(
                    f"step {index}: wait needs numeric seconds") from None
            if not 0 <= seconds <= MAX_WAIT_SECONDS:
                raise MacroError(
                    f"step {index}: wait must be 0-{MAX_WAIT_SECONDS:g}s")
            clean.append({"action": "wait", "seconds": seconds})
        else:
            raise MacroError(f"step {index}: unknown action {action!r}")
    return clean


def save_macro(name: str, steps: Any) -> str:
    """Validate and write one macro. `steps` may be a list or JSON text."""
    if isinstance(steps, str):
        try:
            steps = json.loads(steps)
        except json.JSONDecodeError as exc:
            raise MacroError(f"steps are not valid JSON: {exc}") from exc
    clean = _validate_steps(steps)

    path = macro_path(name)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(clean, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        raise MacroError(f"could not save macro: {exc}") from exc
    variables = required_variables(clean)
    detail = f"; variables: {', '.join(variables)}" if variables else ""
    return (f"Saved macro '{normalise_name(name)}' "
            f"({len(clean)} steps{detail}).")


def load_macro(name: str) -> List[dict]:
    path = macro_path(name)
    if not path.is_file():
        raise MacroError(f"macro not found: {normalise_name(name)}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MacroError(f"could not read macro: {exc}") from exc
    return _validate_steps(data)


def list_macros() -> List[str]:
    try:
        return sorted(p.stem for p in macros_dir().glob("*.json") if p.is_file())
    except OSError:
        return []


def required_variables(steps: List[dict]) -> List[str]:
    """Placeholder names used by type steps, sorted."""
    names = set()
    for step in steps:
        if isinstance(step, dict) and step.get("action") == "type":
            text = str(step.get("text") or "")
            names.update(match.group(1) for match in _PLACEHOLDER_RE.finditer(text))
    return sorted(names)


def _normalise_variables(variables: Any) -> dict:
    if variables is None:
        return {}
    if not isinstance(variables, dict):
        raise MacroError("variables must be a JSON object")
    clean = {}
    for key, value in variables.items():
        if not isinstance(key, str) or not key.strip():
            raise MacroError("variable names must be non-empty strings")
        if isinstance(value, str):
            clean[key] = value
        elif isinstance(value, (int, float, bool)):
            clean[key] = str(value)
        else:
            raise MacroError(f"variable '{key}' must be a string or number")
    return clean


def _substitute(text: str, variables: dict) -> str:
    return _PLACEHOLDER_RE.sub(
        lambda match: variables.get(match.group(1), match.group(0)), text)


def _sleep_interruptible(seconds: float, os_control) -> bool:
    """Sleep in slices; return False when the kill switch latches."""
    end = time.time() + seconds
    while True:
        if os_control.kill_switch_active():
            return False
        remaining = end - time.time()
        if remaining <= 0:
            return True
        time.sleep(min(0.2, remaining))


def run_macro(name: str, variables: Any = None) -> str:
    """Execute a saved macro, filling {{VAR}} placeholders in typed text.

    Missing variables abort before any click or keystroke, so a half-filled
    macro can never run. Clicks go through click_coordinates, which performs
    exactly one pyautogui click and a 0.5s post-click settle.
    """
    steps = load_macro(name)
    try:
        variables = _normalise_variables(variables)
    except MacroError as exc:
        return f"ERROR: {exc}"
    missing = [v for v in required_variables(steps) if v not in variables]
    if missing:
        return (f"ERROR: macro '{normalise_name(name)}' needs variables: "
                f"{', '.join(missing)}. Pass them as name|{{\"VAR\":\"value\"}}.")
    import os_control

    for step in steps:
        if os_control.kill_switch_active():
            return "ABORTED: kill switch is latched (Ctrl+Alt+Shift+Esc)."
        action = step["action"]
        if action == "click":
            result = os_control.click_coordinates(f"{step['x']},{step['y']}")
            if result.startswith(("ABORTED", "ERROR")):
                return result
        elif action == "type":
            result = os_control.type_text(_substitute(step["text"], variables))
            if result.startswith(("ABORTED", "ERROR")):
                return result
        elif action == "wait":
            if not _sleep_interruptible(step["seconds"], os_control):
                return "ABORTED: kill switch is latched (Ctrl+Alt+Shift+Esc)."

    return f"Played macro '{normalise_name(name)}' ({len(steps)} steps)."
