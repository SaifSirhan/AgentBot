"""Thread-safe hand-off between the agent's tools and the live pet widget.

Tools run on a worker thread and the pet is a QWidget, so the two may never talk
directly. The GUI installs a listener that marshals a command onto its own
thread (a Qt signal with the default AutoConnection does exactly that); the tool
layer only ever calls `sprite_controller.play`, which reports False when no
window is showing a pet so a headless run gets an honest answer instead of a
crash.
"""
from __future__ import annotations

import threading

# The commands `PetWidget.play` understands. Kept here, not in hud_core, so the
# tool layer can validate an action without importing Qt.
SPRITE_ACTIONS = ("fly_to", "fly_around", "pop_out", "pop_in", "set_scale",
                  "set_position")
SPRITE_ANCHORS = ("bottom_right", "bottom_left", "top_right", "top_left")

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


def dispatch(action: str, params: dict | None = None) -> bool:
    """Hand one command to the live pet. False when nothing is attached.

    A raised exception means the window was torn down between the attach check
    and the emit, which must not take a tool turn down with it.
    """
    with _lock:
        listener = _listener
    if listener is None:
        return False
    try:
        listener(str(action), dict(params or {}))
    except Exception:
        return False
    return True


class SpriteController:
    """The handle the assistant's tool layer holds.

    `play` is the whole surface: one animation name plus its parameters. It is
    deliberately not a QObject — the tool thread must be able to call it — and
    it never blocks: the widget animates itself off the HUD's existing tick.
    """

    def play(self, animation: str, params: dict | None = None) -> bool:
        return dispatch(animation, params)

    @property
    def attached(self) -> bool:
        return is_attached()


sprite_controller = SpriteController()
