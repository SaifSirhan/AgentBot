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
import requests
import io
import contextlib
import threading

import agent

# ---------------------------
# CONFIGURATION
# ---------------------------
TELEGRAM_TOKEN = "8204464038:AAEfK8vszZqB0AXsrwgoB9vGE1h9f0ZZw8I"
# Leave as "" to allow ANYONE who finds your bot. STRONGLY recommended to
# set this to your own Telegram user ID (get it from @userinfobot).
ALLOWED_USER_ID =  1242339032

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


# ---------------------------
# Run a turn (blocking, safe for the telegram thread)
# ---------------------------
def run_agent_for_telegram(user_input):
    """Runs agent.run_agent_turn and returns the final reply text."""
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

    return final


# ---------------------------
# Main polling loop
# ---------------------------
def main():
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

                if not text:
                    continue

                # Access control
                if ALLOWED_USER_ID and user_id != ALLOWED_USER_ID:
                    send_message(chat_id, "Unauthorized.")
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
                        "  • summarize https://example.com"
                    )
                    continue

                if text == "/clear":
                    conversation_history.clear()
                    send_message(chat_id, "🧹 Conversation history cleared.")
                    continue

                # Tell the user we're working on it
                try:
                    requests.post(f"{TELEGRAM_API}/sendChatAction",
                                  json={"chat_id": chat_id, "action": "typing"})
                except Exception:
                    pass

                # Run the agent (blocks the loop — fine for a single user)
                                # Detect if this came from a group chat
                chat_type = message["chat"]["type"]
                is_group = chat_type in ("group", "supergroup")

                # Set the flag before running the turn
                agent.GROUP_MODE = is_group
                try:
                    reply = run_agent_for_telegram(text)
                finally:
                    agent.GROUP_MODE = False   # always reset
                print(f"[Telegram] AI: {reply[:100]}...")
                send_message(chat_id, reply)

            time.sleep(1)

        except KeyboardInterrupt:
            print("\n[Telegram] Stopping...")
            break
        except Exception as e:
            print(f"[Telegram] Loop error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()