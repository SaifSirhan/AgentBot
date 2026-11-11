"""Client for MusicAppDesktop's local Kestrel bridge.

The bridge listens on 127.0.0.1 with an ephemeral port and writes a bearer
token to MusicApp's userData directory. Nothing here prints or logs the token.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

TOKEN_FILE_ENV = "KESTREL_MUSICAPP_BRIDGE"
MAX_RESPONSE_BYTES = 1_000_000
DEFAULT_TIMEOUT = 10

ACTIONS = frozenset({
    "state", "play", "pause", "toggle", "next", "previous", "seek",
    "go_back", "go_forward", "volume", "mute", "shuffle", "repeat",
    "like", "dislike", "fullscreen", "queue_list", "queue_add",
    "queue_move", "queue_remove", "queue_play_index", "queue_clear",
    "search", "load", "navigate",
})

PAGES = frozenset({
    "home", "explore", "new_releases", "charts", "moods_genres",
    "library", "playlists", "songs", "albums", "artists",
    "subscribed_artists", "uploads", "uploaded_songs", "uploaded_albums",
    "uploaded_artists", "mixed_for_you",
})


def _default_token_file():
    appdata = os.environ.get("APPDATA")
    if appdata:
        return os.path.join(appdata, "MusicApp", "kestrel-bridge.json")
    return os.path.join(os.path.expanduser("~"), ".config", "MusicApp",
                        "kestrel-bridge.json")


DEFAULT_TOKEN_FILE = _default_token_file()


class MusicAppUnavailable(RuntimeError):
    """The app is closed, still starting, or its token file is unreadable."""


class MusicAppError(RuntimeError):
    """The bridge answered with an error; ``status`` is the HTTP status."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def token_path():
    return os.environ.get(TOKEN_FILE_ENV) or DEFAULT_TOKEN_FILE


def load_credentials(path=None):
    path = path or token_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise MusicAppUnavailable(
            "MusicAppDesktop bridge credentials are not available") from exc

    port = data.get("port") if isinstance(data, dict) else None
    token = data.get("token") if isinstance(data, dict) else None
    if (not isinstance(port, int) or isinstance(port, bool)
            or not 1 <= port <= 65535):
        raise MusicAppUnavailable(
            "MusicAppDesktop bridge credentials are malformed")
    if not isinstance(token, str) or len(token) < 16:
        raise MusicAppUnavailable(
            "MusicAppDesktop bridge credentials are malformed")
    return port, token


def _http_error_message(exc):
    try:
        body = exc.read(4096).decode("utf-8", "replace")
        payload = json.loads(body)
        message = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(message, str) and message:
            return message
    except (OSError, ValueError):
        pass
    return "HTTP %d" % exc.code


def call(action, params=None, timeout=DEFAULT_TIMEOUT, path=None):
    if action not in ACTIONS:
        raise MusicAppError(
            400, "unknown action %r. Use one of: %s"
            % (action, ", ".join(sorted(ACTIONS))))

    port, token = load_credentials(path)
    payload = {"action": action}
    if params:
        payload.update(params)
    request = urllib.request.Request(
        "http://127.0.0.1:%d/v1/action" % port,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + token,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise MusicAppError(exc.code, _http_error_message(exc)) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise MusicAppUnavailable(
            "MusicAppDesktop bridge is not reachable") from exc

    if len(body) > MAX_RESPONSE_BYTES:
        raise MusicAppError(502, "MusicAppDesktop bridge response is too large")
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise MusicAppError(502, "MusicAppDesktop bridge returned bad JSON") \
            from exc
    if not isinstance(parsed, dict) or parsed.get("ok") is not True:
        raise MusicAppError(502, "MusicAppDesktop bridge returned an error")
    return parsed.get("result")


def _clock(seconds):
    try:
        total = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        return "0:00"
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return "%d:%02d:%02d" % (hours, minutes, secs)
    return "%d:%02d" % (minutes, secs)


def _dict(value):
    return value if isinstance(value, dict) else {}


def _list(value):
    return value if isinstance(value, list) else []


def _track_line(item):
    title = str(item.get("title") or "unknown track")
    artist = str(item.get("artist") or "unknown artist")
    duration = item.get("duration")
    suffix = " (%s)" % duration if isinstance(duration, str) and duration else ""
    return "%s — %s%s" % (title, artist, suffix)


def _state_text(result):
    result = _dict(result)
    song = _dict(result.get("song"))
    if not song:
        return "MusicApp: nothing is loaded."

    player = _dict(result.get("player"))
    queue = _dict(result.get("queue"))
    player_state = player.get("playerState")
    paused = song.get("paused") is True or player_state == 2
    verb = "paused" if paused else "playing"
    if player_state in (0, 5) and not paused:
        verb = "ready"

    elapsed = player.get("currentTime", song.get("elapsed"))
    duration = player.get("duration", song.get("duration"))
    title = song.get("title") or "unknown track"
    artist = song.get("artist") or "unknown artist"
    parts = ['MusicApp: %s "%s" by %s (%s/%s)'
             % (verb, title, artist, _clock(elapsed), _clock(duration))]

    volume = player.get("volume")
    if isinstance(volume, (int, float)):
        parts.append("volume %d%%" % int(volume))
    if player.get("muted") is True:
        parts.append("muted")
    if player.get("shuffle") is True:
        parts.append("shuffle on")
    count = queue.get("count")
    if isinstance(count, int):
        parts.append("%d queued" % count)
    url = player.get("url")
    if isinstance(url, str) and url:
        parts.append("page %s" % url)
    return ", ".join(parts) + "."


def _search_text(result):
    items = _list(_dict(result).get("items"))
    if not items:
        return "MusicApp search returned no matches."
    lines = []
    for index, item in enumerate(items[:10], 1):
        item = _dict(item)
        video_id = item.get("videoId")
        suffix = " [%s]" % video_id if isinstance(video_id, str) else ""
        lines.append("%d. %s%s" % (index, _track_line(item), suffix))
    return "MusicApp search: " + " | ".join(lines)


def _queue_text(result):
    result = _dict(result)
    items = _list(result.get("items"))
    if not items:
        return "MusicApp queue is empty."
    lines = []
    for index, item in enumerate(items[:15], 1):
        item = _dict(item)
        marker = ">" if item.get("selected") is True else " "
        lines.append("%s %d. %s" % (marker, index, _track_line(item)))
    return "MusicApp queue (%d): %s" % (result.get("count", len(items)),
                                          " | ".join(lines))


def describe(action, result):
    if action == "state":
        return _state_text(result)
    if action == "search":
        return _search_text(result)
    if action == "queue_list":
        return _queue_text(result)
    if action == "load":
        video_id = _dict(result).get("videoId") or "the track"
        return "MusicApp: loading %s." % video_id
    if action == "navigate":
        page = _dict(result).get("page") or "requested"
        return "MusicApp: opened the %s page." % page
    if action == "queue_add":
        video_id = _dict(result).get("videoId") or "the track"
        return "MusicApp: queued %s." % video_id

    messages = {
        "play": "resumed playback",
        "pause": "paused",
        "toggle": "toggled playback",
        "next": "skipped to the next track",
        "previous": "went back to the previous track",
        "seek": "sought to a new position",
        "go_back": "rewound",
        "go_forward": "fast-forwarded",
        "volume": "changed the volume",
        "mute": "toggled mute",
        "shuffle": "toggled shuffle",
        "repeat": "changed the repeat mode",
        "like": "liked this track",
        "dislike": "disliked this track",
        "fullscreen": "changed fullscreen",
        "queue_move": "reordered the queue",
        "queue_remove": "removed a queued track",
        "queue_play_index": "jumped to a queued track",
        "queue_clear": "cleared the queue",
    }
    return "MusicApp: %s." % messages.get(action, action)
