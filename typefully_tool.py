"""
Typefully social media scheduling for AgentBot.
Create, schedule, and publish posts to X, LinkedIn, Threads, Bluesky, Mastodon.
"""
import os, requests

def _get_key() -> str:
    key = os.environ.get("TYPEFULLY_KEY", "")
    if not key:
        try:
            from config import load_config
            key = load_config().get("TYPEFULLY_KEY", "")
        except Exception:
            pass
    return key

def post_to_social(content: str, schedule_time: str = None) -> str:
    """Create a Typefully draft and optionally schedule it.
    schedule_time: ISO 8601 timestamp, "now", or "next-free-slot"."""
    key = _get_key()
    if not key:
        return "ERROR: TYPEFULLY_KEY not set. Get one from https://typefully.com/?settings=api"

    try:
        # Create a draft
        r = requests.post(
            "https://api.typefully.com/v2/drafts",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"content": content},
            timeout=15,
        )
        data = r.json()
        if r.status_code not in (200, 201):
            return f"ERROR: Typefully draft failed: {data}"
        draft_id = data.get("id") or data.get("draft", {}).get("id")
        if not draft_id:
            return f"ERROR: no draft id returned: {data}"

        if schedule_time:
            sr = requests.post(
                f"https://api.typefully.com/v2/drafts/{draft_id}/schedule",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"publish_at": schedule_time},
                timeout=15,
            )
            if sr.status_code not in (200, 201):
                return f"Draft created ({draft_id}) but scheduling failed: {sr.text}"
            return f"Draft {draft_id} scheduled for {schedule_time}."

        return f"Draft {draft_id} created (not yet scheduled)."

    except Exception as e:
        return f"ERROR: Typefully request failed: {e}"

def list_queue() -> str:
    """List upcoming scheduled posts."""
    key = _get_key()
    if not key:
        return "ERROR: TYPEFULLY_KEY not set."
    try:
        r = requests.get(
            "https://api.typefully.com/v2/social-sets",
            headers={"Authorization": f"Bearer {key}"},
            timeout=15,
        )
        return r.text[:3000]
    except Exception as e:
        return f"ERROR: {e}"
