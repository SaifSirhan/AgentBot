"""
Telegram sender via Telethon (user account, not bot).

Logs in as YOUR personal Telegram account, so it can message anyone in
your contacts — not just people who have messaged a bot first.

Setup (one time):
  1. Get api_id + api_hash from https://my.telegram.org  (API Development Tools)
  2. setx TELEGRAM_API_ID "12345678"
     setx TELEGRAM_API_HASH "abcdef..."
  3. Run once:   python telegram_user.py login
     Enter your phone in international format (+60...) and the SMS/app code.
     Session is saved to agent_user_session.session — you only do this once.

Anti-ban guardrails:
  - Randomized 2-6 second delay before each send
  - FloodWaitError is respected
  - One message per call
  - Fuzzy contact matching (won't miss because of a typo)

Delete flow:
  - Text-match delete returns a PREVIEW by default, deletes only with 'confirm'
  - "latest"/"last"/"newest" special-cased to target the newest outgoing message

Requires: pip install telethon
"""

import os
import sys
import time
import random
import asyncio

SESSION_NAME = "agent_user_session"

try:
    from telethon import TelegramClient, errors
    from telethon.tl.types import User
    TELETHON_AVAILABLE = True
except ImportError:
    TELETHON_AVAILABLE = False


# ------------------------------------------------------------------
# Credentials + client
# ------------------------------------------------------------------
def _credentials():
    import config as _cfg
    c = _cfg.load_config()
    api_id_raw = (c.get("TELEGRAM_API_ID") or "").strip()
    api_hash = (c.get("TELEGRAM_API_HASH") or "").strip()
    if not api_id_raw or not api_hash:
        raise RuntimeError(
            "TELEGRAM_API_ID / TELEGRAM_API_HASH not set. "
            "Get them from https://my.telegram.org → API Development Tools."
        )
    try:
        api_id = int(api_id_raw)
    except ValueError:
        raise RuntimeError(f"TELEGRAM_API_ID is not a number: {api_id_raw!r}")
    return api_id, api_hash


def _make_client():
    api_id, api_hash = _credentials()
    return TelegramClient(SESSION_NAME, api_id, api_hash)


# ------------------------------------------------------------------
# CLI: one-time login
# ------------------------------------------------------------------
def _cli_login():
    if not TELETHON_AVAILABLE:
        print("Telethon not installed. Run: pip install telethon")
        return 1

    api_id, api_hash = _credentials()
    print(f"Using api_id={api_id}")
    client = TelegramClient(SESSION_NAME, api_id, api_hash)

    async def go():
        await client.start()
        me = await client.get_me()
        print("\n✓ Logged in successfully.")
        print(f"  Name     : {me.first_name or ''} {me.last_name or ''}".rstrip())
        print(f"  Username : @{me.username}" if me.username else "  Username : (none)")
        print(f"  Phone    : {me.phone}")
        print(f"  User ID  : {me.id}")
        print(f"\nSession saved to: {SESSION_NAME}.session")
        print("You won't need to log in again.")
        await client.disconnect()

    asyncio.run(go())
    return 0


# ------------------------------------------------------------------
# Fuzzy contact finder
# ------------------------------------------------------------------
async def _find_dialog(client, query):
    """
    Find a dialog matching `query`. Tries in order:
      1. Exact name / username match
      2. Every word in the query appears in the name
      3. Any distinctive word (≥4 chars) appears in the name
      4. Whole-query substring match
    """
    q = query.lower().strip()
    q_words = [w for w in q.split() if len(w) > 1]

    if q in ("me", "self", "saved", "saved messages"):
        return "me"

    exact = None
    all_words_match = None
    any_word_match = None
    partial = None

    async for dialog in client.iter_dialogs():
        name = (dialog.name or "").lower()
        username = ""
        try:
            if dialog.entity and getattr(dialog.entity, "username", None):
                username = dialog.entity.username.lower()
        except Exception:
            pass

        haystack = f"{name} {username}"

        # 1. Exact match — stop immediately
        if q == name or q == username or q == f"@{username}":
            exact = dialog
            break

        # 2. Every word in the query is present
        if not all_words_match and q_words and all(w in haystack for w in q_words):
            all_words_match = dialog
            continue

        # 3. Any distinctive word (≥4 chars) matches
        if not any_word_match:
            for w in q_words:
                if len(w) >= 4 and w in haystack:
                    any_word_match = dialog
                    break

        # 4. Whole-query substring
        if not partial and q in haystack:
            partial = dialog

    return exact or all_words_match or any_word_match or partial


# ------------------------------------------------------------------
# Message lookup by text
# ------------------------------------------------------------------
async def _find_message_by_text(client, target, text_query, all_matches=False, limit=200):
    """
    Search recent messages for one containing text_query (case-insensitive).
    Returns (message, count) or (list_of_messages, count) if all_matches=True.
    """
    q = text_query.lower().strip()
    matches = []

    async for msg in client.iter_messages(target, limit=limit):
        if not msg.text:
            continue
        if q in msg.text.lower():
            matches.append(msg)
            if not all_matches and len(matches) >= 1:
                break

    if not matches:
        return (None, 0) if not all_matches else ([], 0)

    return (matches[0], len(matches)) if not all_matches else (matches, len(matches))


# ------------------------------------------------------------------
# Async: send
# ------------------------------------------------------------------
async def _send_async(contact, message):
    client = _make_client()
    try:
        await client.connect()
    except Exception as e:
        return f"ERROR: could not connect to Telegram: {e}"

    try:
        if not await client.is_user_authorized():
            await client.disconnect()
            return "ERROR: not logged in. Run: python telegram_user.py login"

        target = await _find_dialog(client, contact)
        if target is None:
            await client.disconnect()
            return f"ERROR: no chat found matching '{contact}'. Check the exact name in Telegram."

        await asyncio.sleep(random.uniform(2.0, 6.0))

        try:
            await client.send_message(target, message)
            display_name = getattr(target, "name", None) or contact
            await client.disconnect()
            return f"Sent Telegram to '{display_name}': {message[:80]}"
        except errors.FloodWaitError as e:
            wait = int(e.seconds)
            await client.disconnect()
            return f"ERROR: Telegram rate-limited (FloodWait {wait}s). Try again in {wait}s."
        except errors.UserPrivacyRestrictedError:
            await client.disconnect()
            return f"ERROR: can't message '{contact}' — privacy settings block it."
        except errors.UserIsBlockedError:
            await client.disconnect()
            return f"ERROR: '{contact}' has blocked you or you've blocked them."
        except Exception as e:
            await client.disconnect()
            return f"ERROR: send failed: {e}"
    except Exception as e:
        try:
            await client.disconnect()
        except Exception:
            pass
        return f"ERROR: telegram_send failed: {e}"


# ------------------------------------------------------------------
# Async: send a file (image/document)
# ------------------------------------------------------------------
async def _send_file_async(contact, path):
    client = _make_client()
    try:
        await client.connect()
    except Exception as e:
        return f"ERROR: could not connect to Telegram: {e}"

    try:
        if not await client.is_user_authorized():
            await client.disconnect()
            return "ERROR: not logged in. Run: python telegram_user.py login"

        target = await _find_dialog(client, contact)
        if target is None:
            await client.disconnect()
            return f"ERROR: no chat found matching '{contact}'. Check the exact name in Telegram."

        await asyncio.sleep(random.uniform(2.0, 6.0))

        try:
            with open(path, "rb") as f:
                msg = await client.send_file(target, f, caption="")
            try:
                msg_id = msg.id
            except AttributeError:
                msg_id = msg[0].id if isinstance(msg, list) and msg else "?"
            display_name = getattr(target, "name", None) or contact
            await client.disconnect()
            return f"Sent image to '{display_name}' (msg id {msg_id})"
        except errors.FloodWaitError as e:
            wait = int(e.seconds)
            await client.disconnect()
            return f"ERROR: Telegram rate-limited (FloodWait {wait}s). Try again in {wait}s."
        except errors.UserPrivacyRestrictedError:
            await client.disconnect()
            return f"ERROR: can't message '{contact}' — privacy settings block it."
        except errors.UserIsBlockedError:
            await client.disconnect()
            return f"ERROR: '{contact}' has blocked you or you've blocked them."
        except Exception as e:
            await client.disconnect()
            return f"ERROR: send file failed: {e}"
    except Exception as e:
        try:
            await client.disconnect()
        except Exception:
            pass
        return f"ERROR: send_file failed: {e}"


# ------------------------------------------------------------------
# Async: delete by text (preview/confirm)
# ------------------------------------------------------------------
async def _delete_by_text_async(contact, text_query, all_matches=False, confirmed=False):
    client = _make_client()
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            return "ERROR: Not logged in. Run: python telegram_user.py login"

        target = await _find_dialog(client, contact)
        if target is None:
            await client.disconnect()
            return f"ERROR: No chat found matching '{contact}'."

        result, count = await _find_message_by_text(client, target, text_query, all_matches)

        if all_matches:
            if not result:
                await client.disconnect()
                return f"ERROR: No messages containing '{text_query}' found in '{contact}'."
            if not confirmed:
                lines = [f"PREVIEW: Will delete {len(result)} message(s) in '{contact}' "
                         f"containing '{text_query}':"]
                for m in result[:10]:
                    text = (m.text or "").replace("\n", " ")[:60]
                    who = "you" if m.out else "them"
                    lines.append(f"  ID {m.id} ({who}): {text}")
                if len(result) > 10:
                    lines.append(f"  … and {len(result) - 10} more")
                lines.append("")
                lines.append(f"To confirm: telegram_user_delete('{contact}|{text_query}|all|confirm')")
                await client.disconnect()
                return "\n".join(lines)
            ids = [m.id for m in result]
            await client.delete_messages(target, ids, revoke=True)
            await client.disconnect()
            return f"Deleted {len(ids)} messages containing '{text_query}' in '{contact}'."

        if result is None:
            await client.disconnect()
            return f"ERROR: No message containing '{text_query}' found in '{contact}'."

        preview_text = (result.text or "").replace("\n", " ")[:100]
        who = "sent by you" if result.out else "sent by them"

        if not confirmed:
            await client.disconnect()
            return (f"PREVIEW: Will delete message {result.id} in '{contact}' "
                    f"({who}):\n  \"{preview_text}\"\n\n"
                    f"To confirm: telegram_user_delete('{contact}|{text_query}|confirm')")

        await client.delete_messages(target, result.id, revoke=True)
        await client.disconnect()
        return f"Deleted message {result.id} in '{contact}' (matched '{text_query}'): {preview_text}"

    except Exception as e:
        try:
            await client.disconnect()
        except Exception:
            pass
        return f"ERROR: delete by text failed: {e}"


# ------------------------------------------------------------------
# Async: preview latest outgoing message
# ------------------------------------------------------------------
async def _preview_latest_async(contact):
    client = _make_client()
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            return "ERROR: Not logged in. Run: python telegram_user.py login"

        target = await _find_dialog(client, contact)
        if target is None:
            await client.disconnect()
            return f"ERROR: No chat found matching '{contact}'."

        async for msg in client.iter_messages(target, limit=40):
            if not msg.out:
                continue
            preview = (msg.text or "<non-text message>").replace("\n", " ")[:120]
            await client.disconnect()
            return (f"PREVIEW: Latest outgoing message in '{contact}':\n"
                    f"  \"{preview}\"\n\n"
                    f"To delete: telegram_user_delete('{contact}|latest|confirm')")

        await client.disconnect()
        return f"ERROR: No outgoing messages found in '{contact}'."
    except Exception as e:
        try:
            await client.disconnect()
        except Exception:
            pass
        return f"ERROR: preview latest failed: {e}"


# ------------------------------------------------------------------
# Async: delete latest outgoing message
# ------------------------------------------------------------------
async def _delete_latest_async(contact, only_outgoing=True):
    client = _make_client()
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            return "ERROR: Not logged in. Run: python telegram_user.py login"

        target = await _find_dialog(client, contact)
        if target is None:
            await client.disconnect()
            return f"ERROR: No chat found matching '{contact}'."

        latest = None
        async for msg in client.iter_messages(target, limit=40):
            if only_outgoing and not msg.out:
                continue
            latest = msg
            break

        if latest is None:
            await client.disconnect()
            return f"ERROR: No outgoing messages found in '{contact}'."

        preview = (latest.text or "<non-text message>").replace("\n", " ")[:100]
        await client.delete_messages(target, latest.id, revoke=True)
        await client.disconnect()
        return f"Deleted latest message in '{contact}': \"{preview}\""
    except Exception as e:
        try:
            await client.disconnect()
        except Exception:
            pass
        return f"ERROR: delete latest failed: {e}"


# ------------------------------------------------------------------
# Async: edit by text
# ------------------------------------------------------------------
async def _edit_by_text_async(contact, old_text, new_text):
    client = _make_client()
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            return "ERROR: Not logged in. Run: python telegram_user.py login"

        target = await _find_dialog(client, contact)
        if target is None:
            await client.disconnect()
            return f"ERROR: No chat found matching '{contact}'."

        result, _ = await _find_message_by_text(client, target, old_text, all_matches=False)
        if result is None:
            await client.disconnect()
            return f"ERROR: No message containing '{old_text}' found in '{contact}'."

        if not result.out:
            await client.disconnect()
            return "ERROR: The matched message was sent by them, not you. Can only edit your own."

        await client.edit_message(target, result.id, new_text)
        await client.disconnect()
        return (f"Edited message {result.id} in '{contact}'. "
                f"Old: '{(result.text or '')[:40]}…' → New: '{new_text[:60]}'")
    except Exception as e:
        try:
            await client.disconnect()
        except Exception:
            pass
        return f"ERROR: edit by text failed: {e}"


# ------------------------------------------------------------------
# Public sync API
# ------------------------------------------------------------------
def send_message(contact, message):
    if not TELETHON_AVAILABLE:
        return "ERROR: Telethon not installed. Run: pip install telethon"
    if not contact or not message:
        return "ERROR: send_message needs both contact and message."
    contact = contact.strip()
    message = message.strip()
    if not contact or not message:
        return "ERROR: contact and message cannot be empty."
    try:
        return asyncio.run(_send_async(contact, message))
    except Exception as e:
        return f"ERROR: telegram_send failed: {e}"


def delete_message_by_text(contact, text_query, all_matches=False, confirmed=False):
    if not TELETHON_AVAILABLE:
        return "ERROR: Telethon not installed."
    if not contact or not text_query:
        return "ERROR: delete_message_by_text needs contact and text_query."
    try:
        return asyncio.run(_delete_by_text_async(
            contact.strip(), text_query.strip(), all_matches, confirmed
        ))
    except Exception as e:
        return f"ERROR: {e}"


def delete_latest_tool(contact):
    """Delete the most recent OUTGOING message to a contact (no text needed)."""
    if not TELETHON_AVAILABLE:
        return "ERROR: Telethon not installed."
    if not contact or not contact.strip():
        return "ERROR: needs a contact name."
    try:
        return asyncio.run(_delete_latest_async(contact.strip(), only_outgoing=True))
    except Exception as e:
        return f"ERROR: {e}"


def preview_latest_tool(contact):
    """Preview (without deleting) the most recent outgoing message."""
    if not TELETHON_AVAILABLE:
        return "ERROR: Telethon not installed."
    if not contact or not contact.strip():
        return "ERROR: needs a contact name."
    try:
        return asyncio.run(_preview_latest_async(contact.strip()))
    except Exception as e:
        return f"ERROR: {e}"


def edit_message_by_text(contact, old_text, new_text):
    if not TELETHON_AVAILABLE:
        return "ERROR: Telethon not installed."
    if not contact or not old_text or not new_text:
        return "ERROR: edit_message_by_text needs contact, old_text, new_text."
    try:
        return asyncio.run(_edit_by_text_async(
            contact.strip(), old_text.strip(), new_text.strip()
        ))
    except Exception as e:
        return f"ERROR: {e}"


# ------------------------------------------------------------------
# Agent tool wrappers
# ------------------------------------------------------------------
def send_telegram_tool(input_str):
    """Format: 'Contact|message'"""
    if "|" not in (input_str or ""):
        return "ERROR: format is 'Contact|message' (e.g. 'JEE|helo')"
    contact, _, message = input_str.partition("|")
    return send_message(contact.strip(), message.strip())


def send_file_tool(input_str):
    """Format: 'contact|image_path' (bare filename assumes ~/Downloads)."""
    if not TELETHON_AVAILABLE:
        return "ERROR: Telethon not installed. Run: pip install telethon"
    parts = (input_str or "").split("|", 1)
    if len(parts) < 2:
        return "ERROR: format is 'contact|image_path'"
    contact = parts[0].strip()
    path = parts[1].strip().strip('"').strip("'")
    if not contact or not path:
        return "ERROR: format is 'contact|image_path'"
    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        path = os.path.join(os.path.expanduser("~"), "Downloads", path)
    if not os.path.isfile(path):
        return f"ERROR: file not found: {path}"
    try:
        return asyncio.run(_send_file_async(contact, path))
    except Exception as e:
        return f"ERROR: {e}"


def delete_tool(input_str):
    """
    Format: 'Contact|text'                → preview match by text
    Format: 'Contact|text|confirm'        → delete match
    Format: 'Contact|text|all'            → preview all matches
    Format: 'Contact|text|all|confirm'    → delete all matches
    Format: 'Contact|latest'              → preview latest outgoing
    Format: 'Contact|latest|confirm'      → delete latest outgoing
    """
    parts = [p.strip() for p in (input_str or "").split("|")]
    if len(parts) < 2:
        return "ERROR: format is 'Contact|text' (add '|confirm' to delete)"

    contact = parts[0]
    target = parts[1]
    flags = [p.lower() for p in parts[2:]]
    confirmed = "confirm" in flags

    if target.lower() in ("latest", "last", "newest", "the latest", "the last"):
        if confirmed:
            return delete_latest_tool(contact)
        return preview_latest_tool(contact)

    all_matches = "all" in flags
    return delete_message_by_text(contact, target, all_matches, confirmed)


def edit_tool(input_str):
    """Format: 'Contact|old text|new text'"""
    parts = (input_str or "").split("|", 2)
    if len(parts) < 3:
        return "ERROR: format is 'Contact|old text|new text'"
    return edit_message_by_text(parts[0].strip(), parts[1].strip(), parts[2].strip())


# ------------------------------------------------------------------
# CLI entry
# ------------------------------------------------------------------
if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "login":
        sys.exit(_cli_login())

    if len(sys.argv) > 1:
        print(send_telegram_tool(sys.argv[1]))
    else:
        print("Usage:")
        print("  python telegram_user.py login                       # one-time login")
        print("  python telegram_user.py \"JEE|hello there\"           # send a message")
        print()
        print("Python API:")
        print("  from telegram_user import send_message, delete_message_by_text,")
        print("      edit_message_by_text, delete_latest_tool, preview_latest_tool")
        print()
        print("  send_message('JEE', 'hello')")
        print("  delete_message_by_text('JEE', 'hello')                # preview")
        print("  delete_message_by_text('JEE', 'hello', confirmed=True) # delete")
        print("  delete_latest_tool('Jeffrey')                          # newest outgoing")
        print("  preview_latest_tool('Jeffrey')                         # peek, no delete")
        print("  edit_message_by_text('JEE', 'hello', 'hi there')")