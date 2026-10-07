"""GIF storage and video reaction for the Telegram bridge.

Two jobs:

1. GIFs are STORED, not described. Group GIFs are memes and reaction images,
   and a vision model reading frames produces captions ("an older man in a blue
   shirt raises his clenched fist") where the chat wants the reaction the GIF
   was doing the work of ("bro is fuming"). Many of those GIFs are carried by
   their audio or by a reference the frames can't show, so no prompt recovers
   it. Store them, deduplicate by content hash, and build the library first —
   tagging can come later, when there's something to tag and a real corpus to
   judge it against.

2. Videos DO get a reaction, because a video's meaning is usually legible from
   its frames plus its audio. Frames become tags, audio goes through Whisper,
   and a text model turns both into one short line.

Requires: pip install imageio imageio-ffmpeg   (ffmpeg binary is bundled)
"""

import datetime
import hashlib
import json
import os
import shutil
import subprocess
import tempfile

import imageio.v3 as iio
import numpy as np
from PIL import Image

# Long clips decode slowly; past this we sample from the frames we've seen.
_MAX_SCAN_FRAMES = 1500

# How many recent chat messages get handed to the reaction pass. Enough to
# carry a thread, short enough that the prompt stays cheap.
_CONTEXT_MESSAGES = 5

# Video limits. Telegram bots can't download files over 20MB, so this is a
# backstop against a file that somehow got through.
_MAX_VIDEO_SECONDS = 60
# Whisper on CPU runs well under realtime for these, but the whole point is to
# keep the reply prompt — a minute of audio is ~3s, and more than that is
# buying accuracy nobody in the group will notice.
_MAX_AUDIO_SECONDS = 30

# Pass 1 of the reaction: compress frames to tags, never sentences. When this
# asked for sentences the model echoed them straight back as the reply and the
# bot narrated the clip instead of reacting to it.
_TAG_PROMPT = (
    "These are evenly-spaced frames from one video, in order. "
    "List the subject, mood, and action in each frame. "
    "Format: subject;mood;action. Max 5 words per field. "
    "No sentences, no prose, no markdown."
)

# Pass 2. The WRONG-tone examples do more work than the right-tone ones: left
# to itself the model opens with "This video shows...".
_REACTION_PROMPT = (
    "You just watched a video someone sent in a group chat.\n"
    "React like a person in this group would. One short line, lowercase, no "
    "period. Do not describe the video. Do not narrate it. Just react.\n"
    "RIGHT tone: \"bro is fuming\", \"peak\", \"losing it\", "
    "\"that's actually insane\", \"no way\", \"wait what\".\n"
    "WRONG tone: \"A man yells at a camera...\", \"This video shows...\", "
    "\"The content depicts...\".\n"
    "If it doesn't clearly connect to anything above, just react to the clip "
    "itself in the same register. 2 to 6 words, maximum."
)

# A small model given licence to "keep it short" will sometimes answer the
# instruction instead of the question. Catch the obvious shape rather than
# spending another API call repairing it.
_META_PREFIXES = (
    "the answer is", "answer:", "based on", "here is", "here's",
    "sure,", "as requested", "i'm not able", "i cannot", "i can't tell",
    "this video shows", "the video shows", "the content depicts",
    "a video of", "this clip shows",
)

# ---------------------------
# GIF library
# ---------------------------
GROUP_GIFS_DIR = os.path.join(
    os.environ.get("USERPROFILE", ""), "Downloads", "GroupGifs")
BY_HASH_DIR = os.path.join(GROUP_GIFS_DIR, "by_hash")
OCCURRENCES_FILE = os.path.join(GROUP_GIFS_DIR, "occurrences.jsonl")


def _resolve_gif_dir():
    """Where stored GIFs live. Evaluated at call time so tests can redirect
    the library by patching GROUP_GIFS_DIR."""
    return GROUP_GIFS_DIR or os.path.join(
        os.environ.get("USERPROFILE", ""), "Downloads", "GroupGifs")


def save_group_gif(gif_path, chat_id, sender, sender_id, caption,
                   recent_messages):
    """Store a GIF by content hash and append an occurrence record.

    Content-addressed rather than timestamped: the same meme gets re-sent for
    months, and one file with many occurrence rows is a far more useful corpus
    than hundreds of copies. The occurrence log is append-only so a crash can
    cost at most the line being written.

    Returns (hash, is_new).
    """
    root = _resolve_gif_dir()
    by_hash = os.path.join(root, "by_hash")
    occurrences = os.path.join(root, "occurrences.jsonl")
    os.makedirs(by_hash, exist_ok=True)

    h = hashlib.sha256()
    with open(gif_path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    digest = h.hexdigest()

    ext = os.path.splitext(gif_path)[1].lower() or ".gif"
    stored_path = os.path.join(by_hash, digest[:16] + ext)
    is_new = not os.path.exists(stored_path)
    if is_new:
        # COPY not MOVE: a rename across volumes into the library would leave
        # nothing to clean up, and the caller owns the temp file's lifetime.
        # Short. Hash collisions are not a real concern; a half-written file
        # from a crash very much is, and ffmpeg rejects that faster than a
        # partial file would mislead a later reader.
        shutil.copy2(gif_path, stored_path)

    record = {
        "hash": digest,
        "chat_id": chat_id,
        "timestamp": datetime.datetime.now().astimezone().isoformat(),
        # sender_id is the stable key — display names change, ids don't.
        "sender": sender,
        "sender_id": sender_id,
        "caption": caption or None,
        "recent_messages": list(recent_messages or [])[-_CONTEXT_MESSAGES:],
    }
    with open(occurrences, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    return digest, is_new


def library_stats():
    """Count unique GIFs and total occurrences."""
    occurrences = os.path.join(_resolve_gif_dir(), "occurrences.jsonl")
    if not os.path.exists(occurrences):
        return "Library empty."
    unique = set()
    total = 0
    with open(occurrences, "r", encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("hash"):
                unique.add(r["hash"])
            total += 1
    return f"{len(unique)} unique GIFs, {total} total occurrences"


# ---------------------------
# Frame extraction (shared by the video path)
# ---------------------------
def _count_frames(path):
    """Decode-and-discard pass to learn the frame count.

    Costs a second decode but keeps memory flat — holding every frame of a
    long clip would be hundreds of MB.
    """
    n = 0
    for _ in iio.imiter(path):
        n += 1
        if n >= _MAX_SCAN_FRAMES:
            break
    return n


def _sample_indices(total, num_frames):
    """Evenly-spaced indices covering the first AND last frame."""
    if total <= num_frames:
        return list(range(total))
    idx = np.linspace(0, total - 1, num_frames).round().astype(int).tolist()
    out = []
    for i in idx:
        if i not in out:
            out.append(i)
    return out


def _save_frame(arr, frames_dir, index):
    img = Image.fromarray(np.asarray(arr))
    if img.mode not in ("RGB", "L"):
        # GIF frames can carry a palette or alpha; flatten onto white so
        # transparent regions don't reach the vision model as black.
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        img = bg
    out = os.path.join(frames_dir, f"frame_{index:02d}.png")
    img.save(out)
    return out


def extract_gif_frames(gif_path, num_frames=8):
    """Decode evenly-spaced frames from a GIF/MP4 to PNGs.

    Returns (paths, frames_dir). The caller owns frames_dir and must pass it
    to cleanup_frames() — paths alone aren't enough to remove the directory.
    """
    total = _count_frames(gif_path)
    if total == 0:
        return [], None

    keep = _sample_indices(total, num_frames)
    wanted = set(keep)
    last_wanted = max(wanted)

    frames_dir = tempfile.mkdtemp(prefix="tg_gif_")
    paths = []
    try:
        for i, arr in enumerate(iio.imiter(gif_path)):
            if i in wanted:
                paths.append(_save_frame(arr, frames_dir, len(paths)))
            if i >= last_wanted:
                break
        return paths, frames_dir
    except Exception:
        shutil.rmtree(frames_dir, ignore_errors=True)
        raise


def cleanup_frames(frames_dir):
    if frames_dir:
        shutil.rmtree(frames_dir, ignore_errors=True)


# ---------------------------
# Video reaction
# ---------------------------
def _ffmpeg_exe():
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def probe_duration(video_path):
    """Duration in seconds, or None if it can't be determined.

    Reads the container header only — instant, and works on files the decoder
    would choke on. Used to reject long clips BEFORE spending time extracting
    frames, which is why it doesn't just count frames.
    """
    try:
        exe = _ffmpeg_exe()
        # ffmpeg with no output file exits non-zero after printing the header;
        # that's expected, the duration is in the stderr it already emitted.
        p = subprocess.run(
            [exe, "-i", video_path, "-hide_banner"],
            capture_output=True, text=True, errors="replace", timeout=30,
        )
        for line in p.stderr.splitlines():
            if "Duration:" not in line:
                continue
            stamp = line.split("Duration:", 1)[1].split(",", 1)[0].strip()
            hh, mm, ss = stamp.split(":")
            return int(hh) * 3600 + int(mm) * 60 + float(ss)
    except Exception as e:
        print(f"[Telegram] duration probe failed: {type(e).__name__}: {e}")
    return None


def has_audio_stream(video_path):
    """True if the file carries an audio stream. Cheap header probe."""
    try:
        p = subprocess.run(
            [_ffmpeg_exe(), "-i", video_path, "-hide_banner"],
            capture_output=True, text=True, errors="replace", timeout=30,
        )
        return "Audio:" in p.stderr
    except Exception:
        return False


def extract_audio(video_path, max_seconds=_MAX_AUDIO_SECONDS):
    """Pull mono 16kHz WAV from a video for Whisper. Returns path or None.

    Re-encodes rather than extracting the original stream because Whisper
    wants 16kHz mono, and a video's audio is usually 48kHz stereo AAC.
    """
    out = os.path.join(tempfile.gettempdir(),
                       f"tg_audio_{os.getpid()}_{abs(hash(video_path)) & 0xFFFFFFFF}.wav")
    try:
        p = subprocess.run(
            [_ffmpeg_exe(), "-y", "-i", video_path,
             "-t", str(max_seconds), "-vn", "-ac", "1", "-ar", "16000",
             "-f", "wav", out],
            capture_output=True, text=True, errors="replace", timeout=90,
        )
        if p.returncode != 0 or not os.path.isfile(out) or os.path.getsize(out) == 0:
            print(f"[Telegram] audio extract failed: {p.stderr[-300:]}")
            return None
        return out
    except Exception as e:
        print(f"[Telegram] audio extract raised: {type(e).__name__}: {e}")
        return None


def transcribe_audio(audio_path):
    """Whisper transcript, or None. Never raises — audio is a bonus signal."""
    try:
        import voice
        text = voice.transcribe(audio_path)
        text = (text or "").strip()
        return text or None
    except Exception as e:
        print(f"[Telegram] transcribe failed: {type(e).__name__}: {e}")
        return None


def _render_context(messages):
    """Recent chat as bullet lines, oldest first."""
    lines = []
    for m in (messages or [])[-_CONTEXT_MESSAGES:]:
        if isinstance(m, dict):
            who = (m.get("name") or "").strip()
            what = (m.get("text") or "").strip()
        else:
            who, what = "", str(m or "").strip()
        if what:
            lines.append(f"- {who + ': ' if who else ''}{what}")
    return "\n".join(lines) or "(no recent messages before this video)"


def _tame_reaction(text):
    """Trim a reaction to one short line; drop description-shaped replies.

    Returns "" when the reply is a description or answers the instruction, so
    the caller can fall back rather than posting narration into the chat.
    """
    line = " ".join((text or "").split()).strip().strip('"').strip("'")
    if not line:
        return ""
    low = line.lower()
    if any(low.startswith(p) for p in _META_PREFIXES):
        return ""
    return line


def _usable_tags(tags):
    """True if the vision pass returned something the reaction can use.

    Not a quality bar — just enough to distinguish tags from an error string,
    an empty reply, or a paragraph the model wrote instead of following the
    format.
    """
    if not tags or tags.startswith("ERROR"):
        return False
    lines = [ln for ln in tags.strip().splitlines() if ln.strip()]
    return bool(lines) and all(";" in ln for ln in lines)


def react_to_video(video_path, recent_messages=None, caption=None,
                   deadline=None):
    """Watch a video and react to it in one short line.

    Always returns a user-sendable string. `deadline` is a time.monotonic()
    value; when passed, each network stage checks it and the function degrades
    to a short honest line rather than blowing past the bridge's timeout and
    replying to a chat that has moved on.
    """
    import time
    import agent

    def _expired():
        return deadline is not None and time.monotonic() > deadline

    try:
        paths, frames_dir = extract_gif_frames(video_path, num_frames=6)
    except Exception as e:
        print(f"[Telegram] video frame extraction failed: {type(e).__name__}: {e}")
        return "couldn't read that one"

    try:
        if not paths:
            return "couldn't read that one"

        caption_text = (caption or "").strip()

        # Pass 1 — tags. One call for all 6 frames: describing them one at a
        # time loses the motion and costs 6 requests. Tags are a fixed format,
        # so we ask for them with thinking OFF: measured 15.2s -> 1.6s, and
        # this call is the entire latency of the pipeline. If the fast read
        # comes back useless we pay for thinking on a retry.
        try:
            tags = agent.describe_images(paths, _TAG_PROMPT, fast=True)
        except Exception as e:
            print(f"[Telegram] video vision raised: {type(e).__name__}: {e}")
            return "can't read that one right now"
        if not _usable_tags(tags) and not _expired():
            print(f"[Telegram] fast tags unusable, retrying with thinking: "
                  f"{str(tags)[:120]}")
            try:
                tags = agent.describe_images(paths, _TAG_PROMPT)
            except Exception as e:
                print(f"[Telegram] video vision retry raised: {type(e).__name__}: {e}")
        if not _usable_tags(tags):
            print(f"[Telegram] video vision failed: {str(tags)[:200]}")
            return "can't read that one right now"

        if _expired():
            return "watched it but ran out of time to react"

        # Audio is optional and slow, so it's the first thing dropped when the
        # clock is short.
        transcript = None
        if has_audio_stream(video_path):
            audio_path = extract_audio(video_path)
            if audio_path:
                try:
                    transcript = transcribe_audio(audio_path)
                finally:
                    try:
                        os.remove(audio_path)
                    except Exception:
                        pass

        prompt_parts = [f"Frames showed:\n{tags}\n"]
        if transcript:
            prompt_parts.append(f"Audio transcript:\n{transcript}\n")
        prompt_parts.append(
            f"Recent chat messages (oldest first):\n"
            f"{_render_context(recent_messages)}\n")
        if caption_text:
            prompt_parts.append(f"Caption on the video: {caption_text}\n")
        prompt_parts.append(_REACTION_PROMPT)

        try:
            reaction = agent.ask_llm_direct("\n".join(prompt_parts))
        except Exception as e:
            print(f"[Telegram] video reaction raised: {type(e).__name__}: {e}")
            return "peak"

        reaction = _tame_reaction(reaction)
        # A description is worse than no reaction at all — the whole reason
        # this replaced the GIF pipeline.
        if not reaction or reaction.startswith(("Error:", "ERROR")):
            print(f"[Telegram] video reaction rejected: {str(reaction)[:200]}")
            return "peak"
        return reaction
    finally:
        cleanup_frames(frames_dir)
