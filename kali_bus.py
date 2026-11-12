"""Thread-safe approval hand-off for Kali tasks.

The agent loop runs on a worker thread and the confirmation dialog is a QWidget,
so the two may never talk directly. The GUI installs a listener that marshals a
plan onto its own thread and blocks that thread until the user answers; the tool
layer only ever calls `request_approval`, which reports False when no window is
listening — so a headless run is denied by default instead of failing open.

This module has no Qt import on purpose: it is the one approval surface, and it
must be importable and testable without a display.

`format_approval` builds the text shown in the dialog from model output, which
is attacker-influenceable. Two bounds keep that text honest:

  * control characters — C0 (including \\n, \\x1b, \\x00, \\r) and DEL — are
    replaced with "?". A raw ESC/CR can erase or overwrite a line so an
    out-of-scope command is shown as something benign; a NUL can split a string
    for whatever consumes it downstream.
  * length — each argv token is cut at MAX_TOKEN_CHARS, each text field at
    MAX_FIELD_CHARS, the target list at MAX_FIELD_CHARS and the commands at
    MAX_STEPS, and the whole body is capped at MAX_TEXT_CHARS, each cut marked
    with an explicit "N more ... omitted" line so a megabyte-wide wildcard or
    a 5000-step plan cannot hang the dialog's layout.
"""
from __future__ import annotations

import shlex
import threading

# Bounds for the dialog text. Generous enough for a real plan (a full nmap
# command line is a few hundred characters) and small enough that a hostile one
# renders without a stall.
MAX_TOKEN_CHARS = 300      # one argv token
MAX_FIELD_CHARS = 2000     # objective, risk, and the whole target line
MAX_TARGETS = 50           # entries in the target line
MAX_STEPS = 500            # numbered commands
MAX_TEXT_CHARS = 20000     # the whole rendered message

_lock = threading.Lock()
_listener = None


def register(listener) -> None:
    """Install the GUI's marshaller. Passing None detaches whatever is there."""
    global _listener
    with _lock:
        _listener = listener


def unregister(listener=None) -> None:
    """Detach `listener`, or clear the slot when None is passed."""
    global _listener
    with _lock:
        if listener is None or _listener is listener:
            _listener = None


def is_attached() -> bool:
    with _lock:
        return _listener is not None


def request_approval(plan, timeout=120) -> bool:
    """Ask the desktop for consent. Anything but an explicit truthy answer is no.

    No listener means no dialog was ever shown, so the answer is no; a listener
    that raises (the window was torn down mid-call) is also a no.
    """
    with _lock:
        listener = _listener
    if listener is None:
        return False
    try:
        return bool(listener(dict(plan), timeout))
    except Exception:
        return False


# -- text rendering ---------------------------------------------------------

_CONTROL = {code: "?" for code in list(range(0x20)) + [0x7F]}


def _cap(text: str, limit: int) -> str:
    """Cut to `limit` characters and say how many were dropped."""
    if len(text) > limit:
        dropped = len(text) - limit
        text = (text[:limit]
                + " ... (%d more characters omitted)" % dropped)
    return text


def _clean(text) -> str:
    """Replace control characters, then bound the field length."""
    return _cap(str(text).translate(_CONTROL), MAX_FIELD_CHARS)


def _token(arg) -> str:
    """Bound one argv token, still a single shell word after quoting.

    The marker goes inside the word rather than appended after it, so the
    rendered line cannot read as an extra argument that was never asked for.
    """
    text = str(arg).translate(_CONTROL)
    if len(text) > MAX_TOKEN_CHARS:
        dropped = len(text) - MAX_TOKEN_CHARS
        text = text[:MAX_TOKEN_CHARS] + "...(%d more characters omitted)" % dropped
    return text


def _command(step) -> str:
    if not isinstance(step, dict):
        return ""
    argv = step.get("argv", [])
    if isinstance(argv, str) or not isinstance(argv, (list, tuple)):
        return ""
    return shlex.join(_token(arg) for arg in argv)


def _targets(plan) -> str:
    targets = plan.get("targets", [])
    if isinstance(targets, str) or not isinstance(targets, (list, tuple)):
        return "none"
    total = len(targets)
    rendered = []
    used = 0
    for target in targets[:MAX_TARGETS]:
        entry = _clean(target)
        if used + len(entry) + 2 > MAX_FIELD_CHARS:
            break
        rendered.append(entry)
        used += len(entry) + 2
    if not rendered:
        return "none"
    text = ", ".join(rendered)
    missing = total - len(rendered)
    if missing > 0:
        text += " ... (%d more targets omitted)" % missing
    return _cap(text, MAX_FIELD_CHARS)


def _numbered(commands) -> list:
    lines = []
    used = 0
    for index, command in enumerate(commands, 1):
        line = "%d. %s" % (index, command)
        if used + len(line) + 1 > MAX_TEXT_CHARS:
            break
        lines.append(line)
        used += len(line) + 1
    if len(lines) < len(commands):
        lines.append("... (%d more commands omitted)"
                     % (len(commands) - len(lines)))
    return lines


def format_approval(plan) -> str:
    """Render a plan for the confirmation dialog. Never raises on odd shapes."""
    if not isinstance(plan, dict):
        plan = {}
    steps = plan.get("steps", [])
    if isinstance(steps, str) or not isinstance(steps, (list, tuple)):
        steps = []
    commands = [_command(step) for step in steps]
    shown = commands[:MAX_STEPS]
    lines = [
        "Kali lab task",
        _clean(plan.get("objective", "") or ""),
        "Risk: %s" % _clean(plan.get("risk", "unknown") or "unknown"),
        "Targets: %s" % _targets(plan),
        "",
        "Commands:",
    ]
    lines.extend(_numbered(shown))
    if len(commands) > MAX_STEPS:
        lines.append("... (%d more commands omitted)"
                     % (len(commands) - MAX_STEPS))
    return "\n".join(lines)
