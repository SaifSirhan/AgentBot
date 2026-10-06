"""
Gmail reader - uses Gmail REST API over HTTPS (port 443).

Works on any network, unlike the old IMAP version which needed port 993.

Setup (one time):
1. Place credentials.json (from Google Cloud Console) in the AgentBot folder
2. First run of any Gmail function opens a browser for OAuth login
3. token.json is saved automatically for all future runs

Requires: pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client
"""

import os
import base64
import json
from datetime import datetime

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Where credentials.json lives (same folder as agent.py)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_FILE = os.path.join(BASE_DIR, "credentials.json")

# Where the saved login token goes
TOKEN_FILE = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'AgentMemory', 'gmail_token.json')

# Scopes - read-only is safest. Change to 'gmail.modify' if you want to
# mark read / move to trash in the future.
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def _get_service():
    """Return an authenticated Gmail API service."""
    creds = None

    # Load saved token if it exists
    if os.path.exists(TOKEN_FILE):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        except Exception:
            creds = None

    # If no valid token, do the OAuth flow
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                creds = None

        if not creds:
            if not os.path.exists(CREDENTIALS_FILE):
                raise RuntimeError(
                    f"credentials.json not found at {CREDENTIALS_FILE}. "
                    f"Download it from Google Cloud Console → Credentials, rename it to "
                    f"credentials.json, and put it in the AgentBot folder."
                )
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)

        # Save token
        os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
        with open(TOKEN_FILE, 'w', encoding='utf-8') as f:
            f.write(creds.to_json())

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _header(headers, name):
    """Get a header value by name from a message's header list."""
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _decode_body(payload):
    """Recursively extract plain-text body from a Gmail message payload."""
    if "parts" in payload:
        # Prefer text/plain
        for part in payload["parts"]:
            mime = part.get("mimeType", "")
            if mime == "text/plain":
                data = part.get("body", {}).get("data", "")
                if data:
                    return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        # Fallback: recurse
        for part in payload["parts"]:
            result = _decode_body(part)
            if result:
                return result
        return ""
    else:
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        return ""


def check_unread(count=5):
    """Return the most recent N unread emails (subject, from, date)."""
    try:
        service = _get_service()
    except Exception as e:
        return f"ERROR: {e}"

    try:
        results = service.users().messages().list(
            userId="me", q="is:unread", maxResults=count
        ).execute()

        messages = results.get("messages", [])
        if not messages:
            return "No unread emails."

        lines = []
        for msg_ref in messages:
            msg = service.users().messages().get(
                userId="me", id=msg_ref["id"], format="metadata",
                metadataHeaders=["Subject", "From", "Date"]
            ).execute()

            headers = msg.get("payload", {}).get("headers", [])
            subject = _header(headers, "Subject") or "(no subject)"
            sender = _header(headers, "From")
            date = _header(headers, "Date")

            # Format date nicely
            try:
                dt = datetime.strptime(date.split(" (")[0].strip(),
                                       "%a, %d %b %Y %H:%M:%S %z")
                date_short = dt.strftime("%Y-%m-%d %H:%M")
            except Exception:
                date_short = date[:30]

            lines.append(f"[{msg_ref['id']}] {subject}  —  {sender}  ({date_short})")

        return f"{len(lines)} unread email(s):\n" + "\n".join(lines)
    except HttpError as e:
        return f"ERROR: Gmail API: {e}"
    except Exception as e:
        return f"ERROR: {e}"


def read_email(uid):
    """Read the full body of an email by ID (the [xxxxx] part from check_unread)."""
    if not uid or not uid.strip():
        return "ERROR: gmail_read needs an email ID."
    try:
        service = _get_service()
    except Exception as e:
        return f"ERROR: {e}"

    try:
        msg = service.users().messages().get(
            userId="me", id=uid.strip(), format="full"
        ).execute()

        payload = msg.get("payload", {})
        headers = payload.get("headers", [])
        subject = _header(headers, "Subject") or "(no subject)"
        sender = _header(headers, "From")
        to = _header(headers, "To")
        date = _header(headers, "Date")

        body = _decode_body(payload)
        if not body:
            body = "(no plain-text body — message may be HTML-only)"

        body = body.strip()[:3000]
        return f"From: {sender}\nTo: {to}\nDate: {date}\nSubject: {subject}\n\n{body}"
    except HttpError as e:
        return f"ERROR: Gmail API: {e}"
    except Exception as e:
        return f"ERROR: {e}"


def search_emails(query, count=5):
    """Search emails by keyword. Uses Gmail's own search syntax."""
    if not query or not query.strip():
        return "ERROR: gmail_search needs a keyword."
    try:
        service = _get_service()
    except Exception as e:
        return f"ERROR: {e}"

    try:
        results = service.users().messages().list(
            userId="me", q=query.strip(), maxResults=count
        ).execute()

        messages = results.get("messages", [])
        if not messages:
            return f"No emails matching '{query}'."

        lines = []
        for msg_ref in messages:
            msg = service.users().messages().get(
                userId="me", id=msg_ref["id"], format="metadata",
                metadataHeaders=["Subject", "From", "Date"]
            ).execute()

            headers = msg.get("payload", {}).get("headers", [])
            subject = _header(headers, "Subject") or "(no subject)"
            sender = _header(headers, "From")
            date = _header(headers, "Date")

            try:
                dt = datetime.strptime(date.split(" (")[0].strip(),
                                       "%a, %d %b %Y %H:%M:%S %z")
                date_short = dt.strftime("%Y-%m-%d %H:%M")
            except Exception:
                date_short = date[:30]

            lines.append(f"[{msg_ref['id']}] {subject}  —  {sender}  ({date_short})")

        return f"{len(lines)} match(es) for '{query}':\n" + "\n".join(lines)
    except HttpError as e:
        return f"ERROR: Gmail API: {e}"
    except Exception as e:
        return f"ERROR: {e}"


if __name__ == "__main__":
    print("Testing Gmail API...")
    print()
    print(check_unread(3))