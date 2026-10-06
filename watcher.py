"""
Folder watcher - fires a callback when files appear/change/disappear.

Design mirrors scheduler.py: the watcher runs on its own background thread
and calls a registered callback. agent.py exposes register_watcher_callback()
so the GUI and telegram bridge can each plug in their own handler.

Requires: pip install watchdog
"""

import os
import threading
import time

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    WATCHDOG_AVAILABLE = True
except ImportError:
    WATCHDOG_AVAILABLE = False

_observer = None
_watch_path = None
_callback = None
_lock = threading.Lock()

# Suppress duplicate events (a single file copy can fire 3-4 events)
_recent_events = {}
_DEDUPE_SECONDS = 3


def _dedupe(event_type, path):
    key = (event_type, path)
    now = time.time()
    last = _recent_events.get(key, 0)
    if now - last < _DEDUPE_SECONDS:
        return True
    _recent_events[key] = now
    # clean old entries
    for k in list(_recent_events):
        if now - _recent_events[k] > 60:
            del _recent_events[k]
    return False


if WATCHDOG_AVAILABLE:
    class _Handler(FileSystemEventHandler):
        def _fire(self, event_type, path):
            if _dedupe(event_type, path):
                return
            if _callback:
                try:
                    _callback(f"FILE WATCHER: {event_type} -> {path}")
                except Exception as e:
                    print(f"[Watcher] Callback error: {e}")

        def on_created(self, event):
            self._fire("created", event.src_path)

        def on_modified(self, event):
            self._fire("modified", event.src_path)

        def on_deleted(self, event):
            self._fire("deleted", event.src_path)

        def on_moved(self, event):
            self._fire("moved", event.dest_path)


def set_callback(cb):
    """Register the callback fired on file events."""
    global _callback
    _callback = cb


def start_watch(path):
    """Start watching a folder. Returns a status message."""
    global _observer, _watch_path

    if not WATCHDOG_AVAILABLE:
        return "ERROR: watchdog not installed. Run: pip install watchdog"

    path = os.path.expandvars(os.path.expanduser(path))
    if not os.path.isdir(path):
        return f"ERROR: Not a folder: {path}"

    with _lock:
        if _observer is not None:
            return f"ERROR: Already watching '{_watch_path}'. Stop the current watch first."

        _observer = Observer()
        _observer.schedule(_Handler(), path, recursive=False)
        _observer.start()
        _watch_path = path

    return f"Now watching: {path}"


def stop_watch():
    """Stop the current folder watch."""
    global _observer, _watch_path

    with _lock:
        if _observer is None:
            return "ERROR: No active watch."

        _observer.stop()
        _observer.join(timeout=3)
        old = _watch_path
        _observer = None
        _watch_path = None

    return f"Stopped watching: {old}"


def is_watching():
    return _observer is not None, _watch_path