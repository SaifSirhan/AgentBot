"""
Telegram bridge for the agent.

Lets you message your PC agent from your phone via Telegram.
Messages go through the SAME agent.run_agent_turn() as the GUI - so the
agent retains all its tools (browser, files, screen, reminders, etc).

The agent runs on the PC - so "open youtube" opens it on the PC's browser,
not the phone's. This makes it a remote control for your PC.

Requires: pip install requests
"""

import time
import os
import collections
import tempfile
import requests
import io
import contextlib
import threading

import agent

# ---------------------------
# CONFIGURATION
# ---------------------------
# The bot token is read from config.json / environment — never hardcode it
# here, this file is tracked by git. Same pattern as agent.py.
try:
    import config as _cfg
    _c = _cfg.load_config()
except Exception:
    _c = {}

TELEGRAM_TOKEN = (_c.get("TELEGRAM_BOT_TOKEN", "") or "").strip()
# Leave as "" to allow ANYONE who finds your bot. STRONGLY recommended to
# set this to your own Telegram user ID (get it from @userinfobot).
ALLOWED_USER_ID = 1242339032

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# Separate conversation history from the GUI, so phone chats and PC chats
# don't collide. Both use the same agent brain.
conversation_history = []

# One lock so concurrent messages don't corrupt state
turn_lock = threading.Lock()

# How long a video gets, from download to reply. Past this the reaction is
# dropped in favour of a short honest line — a chat has moved on by then.
_VIDEO_BUDGET = 25.0

# Telegram caps GIFs at ~10s, so anything longer is a genuine video even when it
# arrives with a mime type that could be either.
_GIF_MAX_SECONDS = 10.0

# Files downloaded only to settle the GIF-vs-video tie, keyed by file_id, so the
# branch that wins doesn't fetch the same bytes again.
_pending_download = {}

# The chat whose message is currently being handled. Tool calls like
# reply_with_gif() run deep inside agent.run_agent_turn() and have no way to
# know which chat asked — in a group that's the difference between answering
# the group and DMing the user. Set for the duration of each turn.
_chat_ctx = threading.local()


def set_current_chat(chat_id):
    _chat_ctx.chat_id = chat_id


def get_current_chat():
    return getattr(_chat_ctx, "chat_id", None)


# ---------------------------
# Telegram helpers
# ---------------------------
def send_message(chat_id, text):
    """Send a message to a Telegram chat. Splits long messages."""
    if not text:
        text = "(no reply)"
    # Telegram has a 4096 char limit per message
    for i in range(0, len(text), 4000):
        chunk = text[i:i+4000]
        try:
            requests.post(f"{TELEGRAM_API}/sendMessage", json={
                "chat_id": chat_id,
                "text": chunk,
            }, timeout=10)
        except Exception as e:
            print(f"[Telegram] Failed to send: {e}")


def get_updates(offset=None):
    try:
        r = requests.get(f"{TELEGRAM_API}/getUpdates", params={
            "timeout": 30,
            "offset": offset,
        }, timeout=40)
        return r.json().get("result", [])
    except Exception as e:
        print(f"[Telegram] getUpdates error: {e}")
        return []


def send_typing(chat_id):
    try:
        requests.post(f"{TELEGRAM_API}/sendChatAction",
                      json={"chat_id": chat_id, "action": "typing"})
    except Exception:
        pass


# ---------------------------
# Typing keepalive
# ---------------------------
# Telegram's typing indicator expires after ~5s. Video processing takes tens of
# seconds, so a single send_typing() leaves the chat looking dead for the whole
# wait. Re-send every few seconds for as long as we're actually working.
def _typing_keepalive(chat_id, stop_event, interval=4.0):
    while not stop_event.wait(interval):
        send_typing(chat_id)


class _Typing:
    """Context manager: hold the typing indicator up for the block's duration."""

    def __init__(self, chat_id, interval=4.0):
        self.chat_id = chat_id
        self.interval = interval
        self._stop = None
        self._thread = None

    def __enter__(self):
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=_typing_keepalive,
            args=(self.chat_id, self._stop, self.interval),
            daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        if self._stop:
            self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
        return False


# ---------------------------
# Recent-chat buffer (for GIF context)
# ---------------------------
# The bot answers each message in isolation, so a GIF arriving after "bro look
# at this" was read with no idea what it was reacting to. Keep the tail of the
# conversation and hand it to the GIF context pass.
#
# Populated from observed messages only — Telegram's getUpdates drops messages
# older than 24h, so fetching a backlog isn't possible without a persistent
# offset, and with privacy mode ON the bot never sees ordinary group chatter
# at all. Bounded so a long-running process can't grow without limit.
_RECENT_LIMIT = 30
_recent_messages = collections.deque(maxlen=_RECENT_LIMIT)


def record_recent(name, text):
    """Remember one message for the GIF context pass."""
    text = (text or "").strip()
    if text:
        _recent_messages.append({"name": (name or "").strip(), "text": text})


def _recent_excluding_tail(n, tail_text):
    """The previous n buffered messages, minus the one we're handling now.

    The buffer is appended to before the handler runs, so the trigger message
    (or a GIF caption) would otherwise show up in its own context block.
    """
    items = list(_recent_messages)
    if items and tail_text and items[-1]["text"] == tail_text.strip():
        items = items[:-1]
    return items[-n:]


# ---------------------------
# Images
# ---------------------------
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")


def send_photo(chat_id, path, caption=""):
    """Send an image file to a chat as the BOT. Returns True on success."""
    try:
        with open(path, "rb") as f:
            r = requests.post(
                f"{TELEGRAM_API}/sendPhoto",
                data={"chat_id": chat_id, "caption": caption[:1024]},
                files={"photo": (os.path.basename(path), f)},
                timeout=60)
        if r.status_code != 200:
            print(f"[Telegram] sendPhoto HTTP {r.status_code}: {r.text[:200]}")
            return False
        return True
    except Exception as e:
        print(f"[Telegram] sendPhoto failed: {e}")
        return False


def send_animation(chat_id, url, caption=""):
    """Send a GIF to a chat by URL. Returns True on success.

    Telegram fetches the URL itself, so there's no download step — but the
    URL must be publicly reachable, and it decides the filename.
    """
    try:
        r = requests.post(
            f"{TELEGRAM_API}/sendAnimation",
            json={"chat_id": chat_id, "animation": url,
                  "caption": caption[:1024]},
            timeout=60)
        if r.status_code != 200:
            print(f"[Telegram] sendAnimation HTTP {r.status_code}: {r.text[:200]}")
            return False
        return True
    except Exception as e:
        print(f"[Telegram] sendAnimation failed: {e}")
        return False


def send_animation_to_current_chat(url, caption=""):
    """Send a GIF to the chat being handled right now (used by reply_with_gif).

    Falls back to the owner's DM when there's no chat context — the agent may
    be driven from the GUI rather than from Telegram.
    """
    chat_id = get_current_chat() or ALLOWED_USER_ID
    if not chat_id:
        return "ERROR: no current Telegram chat and no ALLOWED_USER_ID set"
    if send_animation(chat_id, url, caption):
        return "GIF sent."
    return "ERROR: Telegram rejected the GIF"


def _extract_generated_images(step_log):
    """Paths from 'Action: generate_image(...) -> Result: Image saved: <path>' lines."""
    paths = []
    for line in step_log or []:
        if not line.startswith("Action: generate_image(") or "-> Result: " not in line:
            continue
        result = line.split("-> Result: ", 1)[-1].strip()
        if not result.startswith("Image saved:"):
            continue
        p = result.split("Image saved:", 1)[1].strip().strip('"').strip("'")
        if os.path.isfile(p) and p.lower().endswith(_IMAGE_EXTS) and p not in paths:
            paths.append(p)
    return paths


def _largest_photo_file_id(message):
    """Compressed photo: message["photo"] is a list of sizes — take the biggest."""
    photos = message.get("photo") or []
    if not photos:
        return None
    best = max(photos,
               key=lambda p: (p.get("file_size") or 0,
                              (p.get("width") or 0) * (p.get("height") or 0)))
    return best.get("file_id")


def _image_document_file_id(message):
    """Uncompressed image sent as a file: document with an image/* mime type."""
    doc = message.get("document")
    if not doc:
        return None
    mime = (doc.get("mime_type") or "").lower()
    if mime.startswith("image/"):
        return doc.get("file_id")
    return None


def detect_image_file_id(message):
    """Return (file_id, is_image) for photo or image-document messages."""
    return _largest_photo_file_id(message) or _image_document_file_id(message)


def detect_gif_file_id(message):
    """file_id for a GIF, else None.

    Telegram converts GIFs to silent MP4s and puts them in message["animation"].
    The same clip sent "as file" arrives in message["document"] as video/mp4
    (NOT image/gif — Telegram transcodes on send), which is why video/mp4 has to
    be claimed here or the GIF falls through to the video branch and gets a
    spoken reaction it was never meant to get.

    That makes this detector overlap the video one on video/mp4 documents. The
    tie is broken by duration in handle_message: Telegram caps GIFs at ~10s, so
    a longer mp4 document is a genuine video. Check this BEFORE
    detect_image_file_id, which would otherwise claim image/gif documents as
    ordinary stills.
    """
    anim = message.get("animation")
    if anim and anim.get("file_id"):
        return anim["file_id"]

    doc = message.get("document")
    if doc and doc.get("file_id"):
        mime = (doc.get("mime_type") or "").lower()
        if mime in ("image/gif", "video/mp4"):
            return doc["file_id"]
    return None


def detect_video_file_id(message):
    """file_id for a real video, else None.

    message["video"] is a sent video; a video/* document is one sent "as
    file". Note this overlaps detect_gif_file_id on video/* documents —
    animations live in message["animation"], so a video/* document is a real
    video and the two detectors don't actually compete.
    """
    vid = message.get("video")
    if vid and vid.get("file_id"):
        return vid["file_id"]

    doc = message.get("document")
    if doc and doc.get("file_id"):
        mime = (doc.get("mime_type") or "").lower()
        if mime.startswith("video/"):
            return doc["file_id"]
    return None


def _probe_video_seconds(path):
    """Duration of a downloaded file, or None. Delegates to telegram_media so
    the ffmpeg invocation lives in one place."""
    try:
        import telegram_media
        return telegram_media.probe_duration(path)
    except Exception as e:
        print(f"[Telegram] duration probe unavailable: {e}")
        return None


def download_telegram_image(file_id, message_id):
    """Resolve file_id via getFile then download it. Returns (path, error)."""
    try:
        r = requests.get(f"{TELEGRAM_API}/getFile",
                         params={"file_id": file_id}, timeout=30)
        data = r.json()
    except Exception as e:
        return None, f"getFile request failed: {e}"

    if not data.get("ok"):
        return None, f"getFile failed: {data.get('description')}"

    file_path = (data.get("result") or {}).get("file_path")
    if not file_path:
        return None, "getFile returned no file_path"

    ext = os.path.splitext(file_path)[1].lower() or ".jpg"
    dest = os.path.join(tempfile.gettempdir(), f"tg_image_{message_id}{ext}")

    try:
        fr = requests.get(
            f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}",
            timeout=60)
        if fr.status_code != 200:
            return None, f"download HTTP {fr.status_code}"
        with open(dest, "wb") as f:
            f.write(fr.content)
    except Exception as e:
        return None, f"download failed: {e}"

    if not os.path.isfile(dest) or os.path.getsize(dest) == 0:
        return None, "downloaded file is empty"
    return dest, None


def describe_telegram_image(path, question):
    """Vision first, OCR fallback, then an honest 'can't read it' reply."""
    try:
        reply = agent.describe_image(f"{path}|{question}")
    except Exception as e:
        reply = f"ERROR: {e}"

    if reply and not reply.startswith("ERROR:"):
        return reply

    # Vision unavailable (no DeepSeek key / HTTP error) — fall back to OCR.
    try:
        import file_tools
        ocr = file_tools._read_image(path)
        if ocr and not ocr.startswith("[") and ocr.strip():
            return f"(vision unavailable — OCR text instead)\n{ocr}"
    except Exception as e:
        print(f"[Telegram] OCR fallback failed: {e}")

    print(f"[Telegram] vision failed: {reply[:200]}")
    return "I can see there's an image but can't read it right now."


# ---------------------------
# Run a turn (blocking, safe for the telegram thread)
# ---------------------------
def run_agent_for_telegram(user_input):
    """Runs agent.run_agent_turn and returns (reply_text, generated_image_paths)."""
    log_capture = io.StringIO()
    with turn_lock:
        conversation_history.append(f"User: {user_input}")
        with contextlib.redirect_stdout(log_capture):
            step_log = agent.run_agent_turn(user_input, conversation_history)

    # Extract final reply from step log (same logic as the GUI)
    final = None
    for line in reversed(step_log):
        if line.startswith("Action: chat(") and "-> Result: " in line:
            final = line.split("-> Result: ", 1)[-1]
            break
        if line.startswith("Done:"):
            final = line
            break

    if final is None:
        final = "(no reply produced - check PC console for details)"

    # Strip "AI: " prefix if present
    if final.startswith("AI: "):
        final = final[4:]

    return final, _extract_generated_images(step_log)


# ---------------------------
# Message handler
# ---------------------------
def _display_name(message):
    """Best-effort speaker name. Empty for private chats — the bot only ever
    talks to its owner there, so a name adds nothing."""
    chat = message.get("chat") or {}
    if chat.get("type") not in ("group", "supergroup"):
        return ""
    sender = message.get("from") or {}
    return (sender.get("first_name")
            or sender.get("username")
            or str(sender.get("id") or ""))


def handle_message(message):
    """Handle one incoming Telegram message. Returns nothing; never raises.

    The caller wraps this so one bad message can't kill the polling loop.
    """
    chat_id = message["chat"]["id"]
    user_id = message["from"]["id"]
    text = message.get("text", "").strip()
    caption = (message.get("caption") or "").strip()
    message_id = message.get("message_id", 0)

    gif_file_id = detect_gif_file_id(message)
    has_gif = bool(gif_file_id)
    video_file_id = detect_video_file_id(message)
    has_video = bool(video_file_id)

    # A video/mp4 document matches both detectors. Telegram caps GIFs at ~10s,
    # so duration decides: a 3-minute mp4 sent as a file is a real video and
    # deserves the reaction the GIF branch would have silently swallowed.
    # message["document"]["duration"] is free; only probe the file when absent.
    if has_gif and has_video:
        doc = message.get("document") or {}
        dur = doc.get("duration")
        if dur is None:
            # Header probe needs the bytes, so download before deciding. The
            # temp file is handed to the branch below so nothing is fetched
            # twice.
            probed, err = download_telegram_image(video_file_id, message_id)
            if probed:
                dur = _probe_video_seconds(probed)
                _pending_download[video_file_id] = probed
        if dur is not None and dur > _GIF_MAX_SECONDS:
            has_gif = False          # real video: let the video branch have it
        else:
            has_video = False        # short clip: treat as the GIF it is

    # An image/gif document matches both detectors — GIF wins, so
    # it never falls through to the single-still vision path.
    image_file_id = None if (has_gif or has_video) else detect_image_file_id(message)
    has_image = bool(image_file_id)
    is_other_document = (bool(message.get("document"))
                         and not has_image and not has_gif and not has_video)

    if not (text or has_image or has_gif or has_video or is_other_document):
        return

    # Access control
    if ALLOWED_USER_ID and user_id != ALLOWED_USER_ID:
        send_message(chat_id, "Unauthorized.")
        return

    # Keep this message for the context pass of a GIF that follows, and scoped
    # to this chat so reply_with_gif() answers the right conversation.
    record_recent(_display_name(message), text or caption)
    set_current_chat(chat_id)

    # --- GIF: store it, don't describe it ---
    # Group GIFs are memes; frame descriptions are the wrong tool (see the
    # module docstring in telegram_media). Silent, no reply.
    if has_gif:
        print(f"\n[Telegram] <{user_id}> [gif] {caption}")
        gif_path = None
        try:
            import telegram_media
        except ImportError as e:
            print(f"[Telegram] GIF storage unavailable: {e}")
            return
        try:
            gif_path = _pending_download.pop(gif_file_id, None)
            if not gif_path:
                gif_path, err = download_telegram_image(gif_file_id, message_id)
                if not gif_path:
                    print(f"[Telegram] gif download failed: {err}")
                    return
            recent = [m["text"] for m in _recent_excluding_tail(5, text or caption)]
            digest, is_new = telegram_media.save_group_gif(
                gif_path,
                chat_id=chat_id,
                sender=_display_name(message) or str(user_id),
                sender_id=user_id,
                caption=caption,
                recent_messages=recent,
            )
            print(f"[Telegram] gif {'new' if is_new else 'already known'}: "
                  f"{digest[:8]}")
        except Exception as e:
            print(f"[Telegram] gif store failed: {type(e).__name__}: {e}")
        finally:
            if gif_path:
                try:
                    os.remove(gif_path)
                except Exception:
                    pass
        return

    # --- Video: watch it and react ---
    if has_video:
        print(f"\n[Telegram] <{user_id}> [video] {caption}")
        video_path = None
        # Deadline covers download + frames + optional Whisper + the two
        # model calls. Past it the chat has moved on and a late reaction is
        # worse than a short honest one.
        deadline = time.monotonic() + _VIDEO_BUDGET
        try:
            import telegram_media
        except ImportError as e:
            print(f"[Telegram] video support unavailable: {e}")
            send_message(chat_id, "video support needs "
                                  "`pip install imageio imageio-ffmpeg`.")
            return
        try:
            with _Typing(chat_id):
                video_path = _pending_download.pop(video_file_id, None)
                if not video_path:
                    video_path, err = download_telegram_image(video_file_id, message_id)
                    if not video_path:
                        print(f"[Telegram] video download failed: {err}")
                        send_message(chat_id, "I couldn't download that video — try again?")
                        return

                duration = telegram_media.probe_duration(video_path)
                if duration and duration > telegram_media._MAX_VIDEO_SECONDS:
                    print(f"[Telegram] video too long: {duration:.0f}s")
                    send_message(chat_id, "that's a bit long for me to watch in "
                                          "a group chat — send a clip under a minute")
                    return

                recent = _recent_excluding_tail(5, text or caption)
                reply = telegram_media.react_to_video(
                    video_path, recent_messages=recent, caption=caption,
                    deadline=deadline)
            conversation_history.append(f"User: [video] {caption}")
            conversation_history.append(f"AI: {reply}")
            send_message(chat_id, reply)
        except Exception as e:
            print(f"[Telegram] video handler failed: {type(e).__name__}: {e}")
        finally:
            if video_path:
                try:
                    os.remove(video_path)
                except Exception:
                    pass
        return

    # --- Image: download, then route through the vision pipeline ---
    if has_image:
        print(f"\n[Telegram] <{user_id}> [image] {caption}")
        send_typing(chat_id)
        question = caption or "Describe this image"
        img_path = None
        try:
            img_path, err = download_telegram_image(image_file_id, message_id)
            if not img_path:
                print(f"[Telegram] image download failed: {err}")
                send_message(chat_id, "I couldn't download that image — try again?")
                return
            reply = describe_telegram_image(img_path, question)
            conversation_history.append(f"User: [image] {question}")
            conversation_history.append(f"AI: {reply}")
            send_message(chat_id, reply)
        finally:
            if img_path:
                try:
                    os.remove(img_path)
                except Exception:
                    pass
        return

    # --- Non-image document ---
    if is_other_document:
        print(f"\n[Telegram] <{user_id}> [document] {caption or '(no caption)'}")
        send_message(chat_id, "I can only read text and images right now.")
        return

    print(f"\n[Telegram] <{user_id}> {text}")

    if text == "/start":
        send_message(chat_id,
            "🤖 Agent online.\n"
            "Send me anything - I'll run it on your PC.\n"
            "Examples:\n"
            "  • open youtube and search lofi\n"
            "  • remind me in 5 minutes to drink water\n"
            "  • what's on my screen\n"
            "  • summarize https://example.com\n"
            "  • send me a photo and I'll tell you what's in it"
        )
        return

    if text == "/clear":
        conversation_history.clear()
        send_message(chat_id, "🧹 Conversation history cleared.")
        return

    # Tell the user we're working on it
    send_typing(chat_id)

    # Run the agent (blocks the loop — fine for a single user)
                    # Detect if this came from a group chat
    chat_type = message["chat"]["type"]
    is_group = chat_type in ("group", "supergroup")

    # Set the flag before running the turn
    agent.GROUP_MODE = is_group
    try:
        reply, gen_images = run_agent_for_telegram(text)
    finally:
        agent.GROUP_MODE = False   # always reset
    print(f"[Telegram] AI: {reply[:100]}...")
    send_message(chat_id, reply)
    for img in gen_images:
        if send_photo(chat_id, img):
            print(f"[Telegram] sent generated image: {img}")


# ---------------------------
# Main polling loop
# ---------------------------
def main():
    if not TELEGRAM_TOKEN:
        print("[Telegram] No TELEGRAM_BOT_TOKEN found in config.json or the "
              "environment. Add it in the GUI settings (or setx "
              "TELEGRAM_BOT_TOKEN \"...\"), then restart. Exiting.")
        return
    print("[Telegram] Bridge starting...")
    print(f"[Telegram] Bot: @benjaminnethayahubot")
    print(f"[Telegram] Allowed user: {ALLOWED_USER_ID or 'ANYONE'}")
    print("[Telegram] Send messages to the bot from your phone. Ctrl+C to stop.\n")
    
    # Route folder-watcher events to Telegram
    def on_watcher_event(event_msg):
        if ALLOWED_USER_ID:
            send_message(ALLOWED_USER_ID, f"[Watcher] {event_msg}")
        else:
            print(f"[Watcher] {event_msg}")

    agent.register_watcher_callback(on_watcher_event)
    last_update_id = 0
    while True:
        try:
            updates = get_updates(offset=last_update_id + 1)
            for update in updates:
                last_update_id = update["update_id"]

                message = update.get("message")
                if not message:
                    continue

                # One bad message must not take down the loop, and must not
                # advance past the rest of this batch without answering them.
                try:
                    handle_message(message)
                except Exception as e:
                    print(f"[Telegram] Message handler error: {type(e).__name__}: {e}")
                    try:
                        send_message(message["chat"]["id"],
                                     "Something went wrong handling that — try again?")
                    except Exception:
                        pass

            time.sleep(1)

        except KeyboardInterrupt:
            print("\n[Telegram] Stopping...")
            break
        except Exception as e:
            print(f"[Telegram] Loop error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()