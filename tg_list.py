"""Quick helper: list recent message IDs for a contact.
Usage: python tg_list.py "JEE"

import sys
import os
from telethon.sync import TelegramClient

if len(sys.argv) < 2:
    print("Usage: python tg_list.py \"ContactName\" [limit]")
    sys.exit(1)

target = sys.argv[1]
limit = int(sys.argv[2]) if len(sys.argv) > 2 else 5

api_id = int(os.environ["TELEGRAM_API_ID"])
api_hash = os.environ["TELEGRAM_API_HASH"]

with TelegramClient("agent_user_session", api_id, api_hash) as client:
    print(f"Recent {limit} messages in '{target}':\n")
    for msg in client.iter_messages(target, limit=limit):
        text = (msg.text or "").replace("\n", " ")[:60]
        direction = "→ (you sent)" if msg.out else "← (they sent)"
        print(f"ID: {msg.id:>8}  {direction}  {text}") """