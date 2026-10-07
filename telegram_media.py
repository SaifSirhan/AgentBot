"""GIF / short-video handling for the Telegram bridge.

Telegram delivers GIFs two ways: as a silent MP4 in message["animation"], and
as an image/gif document when the same clip is sent "as file". Both are decoded
here into evenly-spaced frames, which then go to the vision model in ONE call
so it can see the motion instead of a single still.

Requires: pip install imageio imageio-ffmpeg
imageio-ffmpeg bundles an ffmpeg binary, so no system install is needed.
imageio's auto-detected plugin reads GIFs (Pillow) and MP4s (FFMPEG) — do not
pass an explicit plugin, FFMPEG rejects GIFs.
"""

import os
import shutil
import tempfile

import imageio.v3 as iio
import numpy as np
from PIL import Image

# Long clips decode slowly; past this we sample from the frames we've seen.
_MAX_SCAN_FRAMES = 1500

# Matches the bot's casual-replies personality: without this the vision model
# answers a caption with multi-paragraph markdown and a bolded "**Answer:**".
_REPLY_STYLE = ("Answer in one or two plain sentences, no markdown, "
                "no bullet points.")


def _count_frames(path):
    """Decode-and-discard pass to learn the frame count.

    Costs a second decode (~100ms on a short clip) but keeps memory flat —
    holding every frame of a long clip would be hundreds of MB.
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


def describe_gif(gif_path, caption=None):
    """Extract frames and describe the whole sequence in one vision call.

    Always returns a user-sendable string; decode and vision failures are
    reported as plain text rather than raised, since the input is an
    arbitrary file from the internet.
    """
    import agent

    try:
        paths, frames_dir = extract_gif_frames(gif_path, num_frames=8)
    except Exception as e:
        print(f"[Telegram] GIF frame extraction failed: {type(e).__name__}: {e}")
        return "I couldn't decode that GIF."

    try:
        if not paths:
            return "I couldn't read any frames from that GIF."
        caption_text = (caption or "").strip()
        # The sequence preamble matters for the caption case too: without it
        # the model sees 8 unrelated stills instead of one animation.
        preamble = ("These are evenly-spaced frames from one animated "
                    "sequence, in order.")
        if caption_text:
            question = f"{preamble}\n\n{caption_text}\n\n{_REPLY_STYLE}"
        else:
            question = f"{preamble} Describe what happens across it. {_REPLY_STYLE}"
        # Never let an exception reach the polling loop: it would drop the
        # rest of the batch and leave the user with no reply at all.
        try:
            reply = agent.describe_images(paths, question)
        except Exception as e:
            print(f"[Telegram] GIF vision raised: {type(e).__name__}: {e}")
            return "I can see there's a GIF but can't read it right now."
        if not reply or reply.startswith("ERROR"):
            print(f"[Telegram] GIF vision failed: {str(reply)[:200]}")
            return "I can see there's a GIF but can't read it right now."
        return reply
    finally:
        cleanup_frames(frames_dir)
