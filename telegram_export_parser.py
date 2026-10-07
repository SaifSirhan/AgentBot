"""Flatten a Telegram Desktop JSON export into per-month text files for RAG.

Why per-month files rather than one file per chat: RAG chunks by document, so a
single 56k-message file gives no usable citation and one enormous embedding
job. A file per month keeps chunks attributable ("2024-06.txt") and lets a
re-export of one period be diffed against the last.

Nothing here ever prints message content — only counts. stdout from this module
ends up in the GUI log and the console, and this data is a private group chat.
"""

import datetime
import json
import os
import re
import sys
from collections import defaultdict

from telegram_scrubber import record_stats, scrub_line, scrub_stats

# Deliberately outside the repo, outside Downloads, outside Documents. This data
# is a full private group chat and must not sit next to code that gets committed.
OUTPUT_DIR = r"C:\Users\USER\PrivateExport"
BLOCKLIST_FILE = os.path.join(OUTPUT_DIR, "blocklist.txt")

_BLOCKLIST_TEMPLATE = (
    "# One word or phrase per line. Any message containing one is dropped\n"
    "# from the index entirely.\n"
    "#\n"
    "# Matching is case-insensitive substring, so keep entries specific:\n"
    "# a short common word will silently delete a lot of normal chat.\n"
    "# Lines starting with # are ignored.\n"
)


def _load_blocklist():
    """Words/phrases to drop lines for. One per line, # comments ignored."""
    if not os.path.exists(BLOCKLIST_FILE):
        os.makedirs(os.path.dirname(BLOCKLIST_FILE), exist_ok=True)
        with open(BLOCKLIST_FILE, "w", encoding="utf-8") as f:
            f.write(_BLOCKLIST_TEMPLATE)
        return []
    out = []
    with open(BLOCKLIST_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(line)
    return out


def _flatten_text(text_field):
    """Telegram's text is a string, or a list of strings and rich-text objects.

    Every rich-text variant carries its content under "text" — checked against
    a real export, which used link, mention, mention_name, hashtag, phone,
    email, bold, italic, code, spoiler, blockquote and others.
    """
    if isinstance(text_field, str):
        return text_field
    if isinstance(text_field, list):
        parts = []
        for chunk in text_field:
            if isinstance(chunk, str):
                parts.append(chunk)
            elif isinstance(chunk, dict):
                parts.append(chunk.get("text", ""))
        return "".join(parts)
    return ""


def _describe_media(msg):
    """A short placeholder for a message whose whole content is an attachment.

    A real export had ~12,600 of these (photos, stickers, voice notes, files).
    Skipping them loses the shape of the conversation — long stretches vanish
    and a month file can look empty when it was busy. A placeholder keeps the
    timeline honest without pretending to know what the image showed.
    """
    mtype = msg.get("media_type")
    if mtype == "sticker":
        emoji = msg.get("sticker_emoji")
        return f"[sticker {emoji}]" if emoji else "[sticker]"
    if mtype in ("video_file", "animation"):
        return "[video]"
    if mtype == "voice_message":
        return "[voice message]"
    if mtype == "video_message":
        return "[video message]"
    if mtype in ("audio_file", "music"):
        return "[audio]"
    if mtype == "contact":
        return "[contact card]"
    if mtype == "location":
        return "[location]"
    if mtype == "poll":
        return "[poll]"
    if msg.get("photo") or mtype == "photo":
        return "[photo]"
    if msg.get("file_name") or msg.get("file"):
        return f"[file: {msg.get('file_name') or 'unnamed'}]"
    return ""


def _safe_dirname(name, limit=50):
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", name or "chat").strip("_")
    return (safe or "chat")[:limit]


def parse_json_export(json_path, output_dir=None):
    """Parse result.json into per-month scrubbed text files.

    Returns a stats dict. Never raises on malformed input rows: a bad date or
    an unexpected field shape skips that message and is counted, because a
    56k-message export should not fail entirely over one odd row.
    """
    output_dir = output_dir or OUTPUT_DIR
    os.makedirs(output_dir, exist_ok=True)

    blocklist = _load_blocklist()
    stats = scrub_stats()

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    chat_name = data.get("name", "Unknown Chat")
    messages = data.get("messages", [])

    chat_dir = os.path.join(output_dir, _safe_dirname(chat_name))
    os.makedirs(chat_dir, exist_ok=True)

    buckets = defaultdict(list)
    skipped_service = 0
    skipped_bad_date = 0
    no_sender = 0

    for msg in messages:
        if msg.get("type") == "service":
            skipped_service += 1
            continue

        try:
            dt = datetime.datetime.fromisoformat(msg.get("date", ""))
        except Exception:
            skipped_bad_date += 1
            continue

        sender = msg.get("from")
        if not sender:
            # Channel posts and some forwarded entries have no "from".
            no_sender += 1
            sender = "Unknown"

        text = _flatten_text(msg.get("text", "")).strip()
        if not text:
            # Attachment-only message: keep its place in the timeline.
            text = _describe_media(msg)
            if not text:
                continue

        line = f"[{dt:%Y-%m-%d %H:%M}] {sender}: {text}"

        cleaned, was_modified, reasons = scrub_line(line, blocklist)
        dropped = cleaned is None
        record_stats(stats, dropped, was_modified, reasons)
        if dropped:
            continue

        buckets[dt.strftime("%Y-%m")].append(cleaned)

    written = 0
    total_lines = 0
    for key, lines in sorted(buckets.items()):
        with open(os.path.join(chat_dir, f"{key}.txt"), "w",
                  encoding="utf-8") as f:
            f.write(f"# {chat_name} — {key}\n\n")
            f.write("\n".join(lines))
        written += 1
        total_lines += len(lines)

    # A file per month means an empty month still produces a header-only file,
    # which indexes as a document with no content. Drop those.
    empty = [k for k, v in buckets.items() if not v]
    if empty:
        for key in empty:
            try:
                os.remove(os.path.join(chat_dir, f"{key}.txt"))
            except Exception:
                pass
        written -= len(empty)

    report_path = _write_report(output_dir, chat_name, stats,
                                skipped_service, skipped_bad_date, no_sender)

    return {
        "chat_name": chat_name,
        "files_written": written,
        "total_messages": total_lines,
        "skipped_service": skipped_service,
        "skipped_bad_date": skipped_bad_date,
        "no_sender": no_sender,
        "dropped_lines": stats["dropped_lines"],
        "modified_lines": stats["modified_lines"],
        "empty_months_removed": len(empty),
        "output_dir": chat_dir,
        "report_path": report_path,
    }


def _write_report(output_dir, chat_name, stats, skipped_service,
                  skipped_bad_date, no_sender):
    """Scrub counts, no content. Overwrites; it describes the latest run."""
    report_path = os.path.join(output_dir, "scrub_report.txt")
    clean = (stats["total_lines"] - stats["dropped_lines"]
             - stats["modified_lines"])
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(f"Scrub report for: {chat_name}\n")
        f.write(f"Generated: {datetime.datetime.now().isoformat()}\n\n")
        f.write(f"Lines considered:            {stats['total_lines']}\n")
        f.write(f"Dropped (address/blocklist): {stats['dropped_lines']}\n")
        f.write(f"Redacted (modified):         {stats['modified_lines']}\n")
        f.write(f"Unchanged:                   {clean}\n\n")
        f.write("Skipped before scrubbing:\n")
        f.write(f"  service messages:  {skipped_service}\n")
        f.write(f"  unparsable dates:  {skipped_bad_date}\n")
        f.write(f"  no sender field:   {no_sender}\n\n")
        f.write("Redactions by type:\n")
        for reason, count in sorted(stats["reasons"].items(),
                                    key=lambda x: -x[1]):
            f.write(f"  {reason}: {count}\n")
        f.write("\nReview this file BEFORE indexing the output folder.\n")
    return report_path


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python telegram_export_parser.py <path_to_result.json>")
        sys.exit(1)
    result = parse_json_export(sys.argv[1])
    # Counts only — never message content.
    print(f"Chat: {result['chat_name']}")
    print(f"Files written: {result['files_written']}")
    print(f"Messages indexed: {result['total_messages']}")
    print(f"Service messages skipped: {result['skipped_service']}")
    print(f"Lines dropped (address/blocklist): {result['dropped_lines']}")
    print(f"Lines redacted: {result['modified_lines']}")
    print(f"Output: {result['output_dir']}")
    print(f"Scrub report: {result['report_path']}")
