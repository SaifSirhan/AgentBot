"""GIF search via Giphy — for the bot replying with a reaction GIF.

Needs a free API key from https://developers.giphy.com/dashboard/
(create an app, choose the API, not the SDK). Set it as an environment
variable so it can't end up in a tracked file:

    setx GIPHY_API_KEY "your-key-here"     (Windows, then restart terminal)

Giphy returns media URLs; the Telegram bot API can send one directly by URL
in sendAnimation, so there's no download step here.
"""

import os
import requests

GIPHY_API_KEY = os.environ.get("GIPHY_API_KEY", "")

_SEARCH_URL = "https://api.giphy.com/v1/gifs/search"
# Giphy's "pg-13" is the raunchiest tier that still excludes explicit content —
# this posts into a chat with other people in it, so keep it trimmed.
_RATING = "pg-13"


def search_gif(query, limit=1):
    """Return a direct GIF URL matching query, or None.

    Picks the downsized media so the upload stays under Telegram's 50MB
    animation limit; "original" can be many MB for a single clip.
    """
    query = (query or "").strip()
    if not query:
        return None
    if not GIPHY_API_KEY:
        print("[GIF] GIPHY_API_KEY not set — see gif_tool.py header")
        return None

    try:
        r = requests.get(
            _SEARCH_URL,
            params={"api_key": GIPHY_API_KEY, "q": query, "limit": limit,
                    "rating": _RATING, "lang": "en"},
            timeout=10,
        )
        if r.status_code != 200:
            print(f"[GIF] Giphy HTTP {r.status_code}: {r.text[:200]}")
            return None
        data = r.json().get("data") or []
        if not data:
            return None
        images = data[0].get("images") or {}
        for key in ("downsized_medium", "downsized", "original"):
            url = (images.get(key) or {}).get("url")
            if url:
                return url
        return None
    except Exception as e:
        print(f"[GIF] Giphy search failed: {type(e).__name__}: {e}")
        return None


def get_gif_for_emotion(emotion):
    """Search Giphy for a GIF matching an emotion/context."""
    return search_gif(emotion)
