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
    """file_id for a GIF / short silent video, else None.

    Telegram converts GIFs to silent MP4s and puts them in message["animation"];
    the same clip sent "as file" arrives in message["document"] with a video/* or
    image/gif mime type. Check this BEFORE detect_image_file_id, which would
    otherwise claim image/gif documents as ordinary stills.
    """
    anim = message.get("animation")
    if anim and anim.get("file_id"):
        return anim["file_id"]

    doc = message.get("document")
    if doc and doc.get("file_id"):
        mime = (doc.get("mime_type") or "").lower()
        if mime.startswith("video/") or mime == "image/gif":
            return doc["file_id"]
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

                chat_id = message["chat"]["id"]
                user_id = message["from"]["id"]
                text = message.get("text", "").strip()
                caption = (message.get("caption") or "").strip()
                message_id = message.get("message_id", 0)

                gif_file_id = detect_gif_file_id(message)
                has_gif = bool(gif_file_id)
                # An image/gif document matches both detectors — GIF wins, so
                # it never falls through to the single-still vision path.
                image_file_id = None if has_gif else detect_image_file_id(message)
                has_image = bool(image_file_id)
                is_other_document = (bool(message.get("document"))
                                     and not has_image and not has_gif)

                if not text and not has_image and not has_gif and not is_other_document:
                    continue

                # Access control
                if ALLOWED_USER_ID and user_id != ALLOWED_USER_ID:
                    send_message(chat_id, "Unauthorized.")
                    continue

                # --- GIF / short silent video: decode frames, one vision call ---
                if has_gif:
                    print(f"\n[Telegram] <{user_id}> [gif] {caption}")
                    send_typing(chat_id)
                    question = caption or "Describe what's happening in this GIF"
                    gif_path = None
                    try:
                        import telegram_media
                    except ImportError as e:
                        print(f"[Telegram] GIF support unavailable: {e}")
                        send_message(chat_id, "GIF support needs "
                                              "`pip install imageio imageio-ffmpeg`.")
                        continue
                    try:
                        gif_path, err = download_telegram_image(gif_file_id, message_id)
                        if not gif_path:
                            print(f"[Telegram] gif download failed: {err}")
                            send_message(chat_id, "I couldn't download that GIF — try again?")
                            continue
                        reply = telegram_media.describe_gif(gif_path, question)
                        conversation_history.append(f"User: [gif] {question}")
                        conversation_history.append(f"AI: {reply}")
                        send_message(chat_id, reply)
                    finally:
                        if gif_path:
                            try:
                                os.remove(gif_path)
                            except Exception:
                                pass
                    continue

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
                            continue
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
                    continue

                # --- Non-image document ---
                if is_other_document:
                    print(f"\n[Telegram] <{user_id}> [document] {caption or '(no caption)'}")
                    send_message(chat_id, "I can only read text and images right now.")
                    continue

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
                    continue

                if text == "/clear":
                    conversation_history.clear()
                    send_message(chat_id, "🧹 Conversation history cleared.")
                    continue

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

            time.sleep(1)

        except KeyboardInterrupt:
            print("\n[Telegram] Stopping...")
            break
        except Exception as e:
            print(f"[Telegram] Loop error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()