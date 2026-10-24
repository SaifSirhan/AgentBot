"""
One-time Telethon login for the Telegram document listener.

Run from a terminal:
    python login_docs_session.py

This authorises the dedicated session (default ``agent_docs_session``) that
telegram_service.py uses. It is separate from agent_user_session, so the
existing /send, /edit and /delete commands keep working unchanged.
"""
import asyncio
import os

from telegram_service import (
    TELETHON_AVAILABLE,
    TelegramClient,
    TelegramDocConfigError,
    load_credentials,
    resolve_session_path,
)


def main() -> int:
    if not TELETHON_AVAILABLE:
        print("Telethon is not installed. Run: pip install telethon")
        return 1

    try:
        api_id, api_hash, session_name = load_credentials()
    except TelegramDocConfigError as exc:
        print(f"Config error: {exc}")
        print("Set TELEGRAM_API_ID and TELEGRAM_API_HASH in config.json first.")
        return 1

    session_path = resolve_session_path(session_name)
    lock_path = session_path + ".lock"
    if os.path.exists(lock_path):
        print(f"Refusing to log in: {lock_path} exists.")
        print("Close AgentBot first (or delete the lock file if it is stale).")
        return 1

    print(f"Session file: {session_path}")
    print("Log in once. AgentBot must stay closed until this finishes.\n")

    async def go():
        client = TelegramClient(session_path, api_id, api_hash)
        try:
            await client.start()
            me = await client.get_me()
            name = " ".join(
                part for part in (me.first_name, me.last_name) if part
            )
            print("\nLogged in successfully.")
            print(f"  Name     : {name or '(none)'}")
            if me.username:
                print(f"  Username : @{me.username}")
            else:
                print("  Username : (none)")
            print(f"  User ID  : {me.id}")
            print(f"  Session  : {session_path}")
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    try:
        asyncio.run(go())
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 1
    except Exception as exc:
        print(f"Login failed: {type(exc).__name__}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
