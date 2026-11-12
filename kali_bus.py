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
    for whatever consumes it downstream; a bare newline could forge an extra
    numbered step whose command was never in the plan.
  * length — one argv token at MAX_TOKEN_CHARS, a text field (objective, risk,
    target line) at MAX_FIELD_CHARS, the target line at MAX_TARGETS entries,
    the command list at MAX_STEPS, and the command body at MAX_TEXT_CHARS. Each
    cut carries an explicit "N more ... omitted" marker so a megabyte-wide
    wildcard or a 5000-step plan cannot make the dialog unreadable or stall its
    layout.

The work is bounded as well as the output, because this runs on the GUI
thread: only the first MAX_STEPS steps are looked at, each value is cut before
it is translated or quoted, and formatting stops once the text budget is
spent. Exactly one "N more commands omitted" notice closes the command list,
and the first command is always shown even when it alone exceeds the budget —
an empty command list would ask the reader to approve nothing visible.
"""
from __future__ import annotations

import re
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

# What a 5 MB token is cut to before it costs anything else. MAX_TOKEN_CHARS
# plus room for the control characters within it, all of which the cap can
# drop: the cut is wide enough that the rendered token is unchanged.
MAX_SCRUB_CHARS = 4 * MAX_TOKEN_CHARS

_CONTROLS = re.compile("[%s]" % "".join(
    re.escape(chr(code)) for code in list(range(0x20)) + [0x7F]))


def _cap(text: str, limit: int) -> str:
    """Cut to `limit` characters and say how many were dropped."""
    if len(text) > limit:
        text = text[:limit] + " ... (%d more characters omitted)" % (
            len(text) - limit)
    return text


def _bounded_str(value):
    """`str(value)`, but never over more text than MAX_SCRUB_CHARS.

    A huge argv token usually arrives as a `str`, and translating a 50 MB
    token only for the cap to throw it away is exactly the GUI-thread stall
    this renderer exists to prevent. So a `str` is cut *before* it is
    translated; any other object still goes through `str()` (no cheap length
    is available) and is cut right after. Either way, at most a small
    constant of text is translated, quoted or joined per value.
    """
    if isinstance(value, str):
        text = str.__str__(value)  # the raw text; no __str__ call, no scan
    else:
        text = str(value)
    if len(text) > MAX_SCRUB_CHARS:
        text = text[:MAX_SCRUB_CHARS]
    return text


def _scrub(text: str) -> str:
    """Replace C0/DEL with "?" over already-bounded input."""
    return _CONTROLS.sub("?", text)


def _clean(text) -> str:
    """Replace control characters, then bound the field length."""
    return _cap(_scrub(_bounded_str(text)), MAX_FIELD_CHARS)


def _token(arg) -> str:
    """Bound one argv token, still a single shell word after quoting.

    The marker goes inside the word rather than appended after it, so the
    rendered line cannot read as an extra argument that was never asked for.
    """
    text = _bounded_str(arg)
    if len(text) > MAX_TOKEN_CHARS:
        text = text[:MAX_TOKEN_CHARS] + "...(%d more characters omitted)" % (
            len(text) - MAX_TOKEN_CHARS)
    return _scrub(text)


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


def _numbered(steps, total: int) -> list:
    """Number commands until the text budget is spent.

    `steps` is already sliced to MAX_STEPS, and formatting stops at the first
    command that does not fit: past that point nothing is quoted, joined or
    even looked at again. Exactly one omission notice is appended, counting
    every command the reader never sees — cut by the step cap or the budget.
    """
    lines = []
    used = 0
    for index, step in enumerate(steps, 1):
        line = "%d. %s" % (index, _command(step))
        if used + len(line) + 1 > MAX_TEXT_CHARS:
            break
        lines.append(line)
        used += len(line) + 1
    if not lines and steps:
        # A command wider than the whole budget would otherwise leave the list
        # empty, and the reader would be asked to approve nothing visible. Show
        # the first command cut to fit; the cut is spelled out by `_cap`.
        room = MAX_TEXT_CHARS - len("1. ") - 64
        lines.append("1. " + _cap(_command(steps[0]), max(1, room)))
    if len(lines) < total:
        lines.append("... (%d more commands omitted)" % (total - len(lines)))
    return lines


def format_approval(plan) -> str:
    """Render a plan for the confirmation dialog. Never raises on odd shapes.

    The work is bounded, not just the output: the step list is sliced before
    any command is formatted, each token and field is cut before it is
    translated, and formatting stops once the text budget is spent.
    """
    if not isinstance(plan, dict):
        plan = {}
    steps = plan.get("steps", [])
    if isinstance(steps, str) or not isinstance(steps, (list, tuple)):
        steps = []
    total = len(steps)
    # One bounded slice, taken before any command is formatted, so no step
    # past the cap is ever looked at. `total` is the whole plan: the omission
    # notice at the end counts from it.
    steps = steps[:MAX_STEPS]
    lines = [
        "Kali lab task",
        _clean(plan.get("objective", "") or ""),
        "Risk: %s" % _clean(plan.get("risk", "unknown") or "unknown"),
        "Targets: %s" % _targets(plan),
        "",
        "Commands:",
    ]
    lines.extend(_numbered(steps, total))
    return "\n".join(lines)
